from __future__ import annotations

import audioop
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Optional


class SongcutExtractionError(RuntimeError):
    """Raised when automatic songcut extraction cannot continue."""


@dataclass
class Segment:
    index: int
    start: float
    end: float
    duration: float
    active_ratio: float
    average_rms: float
    peak_rms: int
    output_filename: Optional[str] = None
    output_path: Optional[Path] = None


@dataclass
class ExtractionOptions:
    min_duration: float = 60.0
    max_silence: float = 2.8
    leading_padding: float = 1.5
    trailing_padding: float = 2.5
    min_active_ratio: float = 0.58
    analysis_window: float = 0.5
    output_format: str = "mp3"


@dataclass
class AnalysisSummary:
    total_duration: float
    threshold_rms: float
    noise_floor_rms: float
    loud_rms: float
    windows: int
    segments: list[Segment]


def normalize_storage_name(value: str, fallback: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def find_ffmpeg_binary(explicit_path: Optional[str] = None) -> Optional[str]:
    candidates = [
        explicit_path,
        str((Path.cwd() / "ffmpeg.exe").resolve()),
        str((Path.cwd() / "ffmpeg" / "bin" / "ffmpeg.exe").resolve()),
        str((Path.cwd() / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe").resolve()),
        str((Path.cwd() / ".tools" / "ffmpeg" / "bin" / "ffmpeg.exe").resolve()),
        "ffmpeg",
    ]

    for candidate in candidates:
        if not candidate:
            continue

        try:
            completed = subprocess.run(
                [candidate, "-version"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue

        if completed.returncode == 0:
            return candidate

    return None


def extract_songcuts_from_source(
    source_path: Path,
    songcut_root: Path,
    category: str,
    options: ExtractionOptions,
    ffmpeg_path: Optional[str] = None,
    temp_root: Optional[Path] = None,
) -> tuple[AnalysisSummary, str]:
    resolved_ffmpeg = find_ffmpeg_binary(ffmpeg_path)
    if not resolved_ffmpeg:
        raise SongcutExtractionError(
            "未找到 ffmpeg。请安装 ffmpeg，或把 ffmpeg.exe 放到项目根目录、ffmpeg/bin、tools/ffmpeg/bin，"
            "也可以在页面里手动填写 ffmpeg 路径。"
        )

    work_dir = Path(mkdtemp(prefix="songcut-work-", dir=str(temp_root or Path.cwd())))
    analysis_wav = work_dir / "analysis.wav"

    try:
        _decode_to_analysis_wav(resolved_ffmpeg, source_path, analysis_wav)
        summary = _analyze_wav(analysis_wav, options)

        output_dir = songcut_root / normalize_storage_name(category, fallback="自动提取")
        output_dir.mkdir(parents=True, exist_ok=True)

        if not summary.segments:
            return summary, resolved_ffmpeg

        base_name = normalize_storage_name(source_path.stem, fallback="songcut")
        for segment in summary.segments:
            output_filename = _build_output_filename(
                base_name=base_name,
                segment=segment,
                extension=options.output_format,
            )
            output_path = _dedupe_path(output_dir / output_filename)
            _export_segment(
                ffmpeg_path=resolved_ffmpeg,
                source_path=source_path,
                output_path=output_path,
                start=segment.start,
                end=segment.end,
                output_format=options.output_format,
            )
            segment.output_filename = output_path.name
            segment.output_path = output_path

        return summary, resolved_ffmpeg
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _decode_to_analysis_wav(ffmpeg_path: str, source_path: Path, target_path: Path) -> None:
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(target_path),
    ]
    _run_ffmpeg(command, "解码直播录播音频失败")


def _analyze_wav(wav_path: Path, options: ExtractionOptions) -> AnalysisSummary:
    with wave.open(str(wav_path), "rb") as wav_file:
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        total_frames = wav_file.getnframes()
        total_duration = total_frames / sample_rate if sample_rate else 0.0

        frames_per_window = max(1, int(sample_rate * options.analysis_window))
        energies: list[int] = []

        while True:
            data = wav_file.readframes(frames_per_window)
            if not data:
                break
            if channels > 1:
                data = audioop.tomono(data, sample_width, 0.5, 0.5)
            energies.append(audioop.rms(data, sample_width))

    if not energies:
        raise SongcutExtractionError("音频为空，无法分析直播内容")

    smoothed = _smooth_values(energies)
    noise_floor = _percentile(energies, 0.35)
    loud_rms = _percentile(energies, 0.90)
    threshold = max(120.0, noise_floor * 2.15, noise_floor + (loud_rms - noise_floor) * 0.18)
    if loud_rms > 0:
        threshold = min(threshold, loud_rms * 0.82)
    threshold = max(threshold, 120.0)

    active_windows = [value >= threshold for value in smoothed]
    segments = _detect_segments(
        energies=energies,
        active_windows=active_windows,
        total_duration=total_duration,
        options=options,
    )

    return AnalysisSummary(
        total_duration=total_duration,
        threshold_rms=threshold,
        noise_floor_rms=noise_floor,
        loud_rms=loud_rms,
        windows=len(energies),
        segments=segments,
    )


def _detect_segments(
    energies: list[int],
    active_windows: list[bool],
    total_duration: float,
    options: ExtractionOptions,
) -> list[Segment]:
    if not energies:
        return []

    gap_limit = max(1, round(options.max_silence / options.analysis_window))
    detected: list[Segment] = []
    start_index: Optional[int] = None
    last_active_index: Optional[int] = None

    def finalize_segment(segment_start: int, segment_end: int) -> None:
        window_count = segment_end - segment_start
        if window_count <= 0:
            return

        duration = window_count * options.analysis_window
        active_count = sum(1 for flag in active_windows[segment_start:segment_end] if flag)
        active_ratio = active_count / window_count
        average_rms = sum(energies[segment_start:segment_end]) / window_count
        peak_rms = max(energies[segment_start:segment_end])

        if duration < options.min_duration or active_ratio < options.min_active_ratio:
            return

        start_time = max(0.0, segment_start * options.analysis_window - options.leading_padding)
        end_time = min(total_duration, segment_end * options.analysis_window + options.trailing_padding)

        detected.append(
            Segment(
                index=0,
                start=start_time,
                end=end_time,
                duration=end_time - start_time,
                active_ratio=active_ratio,
                average_rms=average_rms,
                peak_rms=peak_rms,
            )
        )

    for index, is_active in enumerate(active_windows):
        if is_active:
            if start_index is None:
                start_index = index
            last_active_index = index
            continue

        if start_index is None or last_active_index is None:
            continue

        if index - last_active_index > gap_limit:
            finalize_segment(start_index, last_active_index + 1)
            start_index = None
            last_active_index = None

    if start_index is not None and last_active_index is not None:
        finalize_segment(start_index, last_active_index + 1)

    if not detected:
        return []

    merged: list[Segment] = []
    for segment in detected:
        if merged and segment.start <= merged[-1].end:
            previous = merged[-1]
            weighted_duration = previous.duration + segment.duration
            previous.active_ratio = (
                previous.active_ratio * previous.duration + segment.active_ratio * segment.duration
            ) / weighted_duration
            previous.average_rms = (
                previous.average_rms * previous.duration + segment.average_rms * segment.duration
            ) / weighted_duration
            previous.peak_rms = max(previous.peak_rms, segment.peak_rms)
            previous.end = max(previous.end, segment.end)
            previous.duration = previous.end - previous.start
            continue

        merged.append(segment)

    for index, segment in enumerate(merged, start=1):
        segment.index = index

    return merged


def _smooth_values(values: list[int]) -> list[float]:
    smoothed: list[float] = []
    for index in range(len(values)):
        left = max(0, index - 1)
        right = min(len(values), index + 2)
        window = values[left:right]
        smoothed.append(sum(window) / len(window))
    return smoothed


def _percentile(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0

    ordered = sorted(values)
    index = int((len(ordered) - 1) * ratio)
    return float(ordered[index])


def _build_output_filename(base_name: str, segment: Segment, extension: str) -> str:
    start_tag = _format_time_for_filename(segment.start)
    end_tag = _format_time_for_filename(segment.end)
    clean_extension = extension.lower().lstrip(".") or "mp3"
    return f"{base_name}_cut_{segment.index:02d}_{start_tag}-{end_tag}.{clean_extension}"


def _format_time_for_filename(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}h{minutes:02d}m{secs:02d}s"


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _export_segment(
    ffmpeg_path: str,
    source_path: Path,
    output_path: Path,
    start: float,
    end: float,
    output_format: str,
) -> None:
    base_command = [
        ffmpeg_path,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(source_path),
        "-vn",
    ]

    normalized_format = output_format.lower().lstrip(".")
    if normalized_format == "wav":
        _run_ffmpeg(base_command + [str(output_path)], "导出 WAV 歌切失败")
        return

    if normalized_format == "flac":
        _run_ffmpeg(base_command + ["-c:a", "flac", str(output_path)], "导出 FLAC 歌切失败")
        return

    if normalized_format in {"m4a", "aac"}:
        _run_ffmpeg(
            base_command + ["-c:a", "aac", "-b:a", "192k", str(output_path)],
            "导出 AAC 歌切失败",
        )
        return

    try:
        _run_ffmpeg(base_command + ["-c:a", "libmp3lame", "-q:a", "2", str(output_path)], "导出 MP3 歌切失败")
    except SongcutExtractionError:
        _run_ffmpeg(base_command + ["-c:a", "mp3", str(output_path)], "导出 MP3 歌切失败")


def _run_ffmpeg(command: list[str], context: str) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return

    stderr = (completed.stderr or completed.stdout or "").strip()
    snippet = stderr.splitlines()[-1] if stderr else "unknown ffmpeg error"
    raise SongcutExtractionError(f"{context}: {snippet}")
