from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Optional


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
    recognition_title: str = ""
    recognition_artist: str = ""
    recognition_provider: str = ""
    recognition_confidence: float = 0.0
    output_filename: Optional[str] = None
    output_path: Optional[Path] = None
    alignment_shift_start: float = 0.0
    alignment_shift_end: float = 0.0
    aligned_by: str = ""


@dataclass
class ExtractionOptions:
    extraction_mode: str = "classic"
    min_duration: float = 60.0
    max_silence: float = 6.0
    merge_gap: float = 18.0
    leading_padding: float = 1.5
    trailing_padding: float = 2.5
    min_active_ratio: float = 0.45
    analysis_window: float = 0.5
    intro_search: float = 30.0
    intro_silence: float = 4.0
    outro_search: float = 45.0
    outro_silence: float = 4.0
    output_format: str = "mp3"


@dataclass
class AnalysisSummary:
    total_duration: float
    threshold_rms: float
    noise_floor_rms: float
    loud_rms: float
    windows: int
    segments: list[Segment] = field(default_factory=list)


def metadata_path_for_segment(segment_path: Path) -> Path:
    return segment_path.with_suffix(f"{segment_path.suffix}.json")


def write_segment_metadata(
    segment_path: Path,
    *,
    source_path: Path,
    category: str,
    start: float,
    end: float,
    duration: float,
    output_format: str,
) -> None:
    payload = {
        "source_path": str(source_path.resolve(strict=False)),
        "source_name": source_path.name,
        "category": category,
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(duration, 3),
        "output_format": output_format.lower().lstrip("."),
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    metadata_path = metadata_path_for_segment(segment_path)
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_segment_metadata(segment_path: Path) -> Optional[dict[str, Any]]:
    metadata_path = metadata_path_for_segment(segment_path)
    if not metadata_path.exists():
        return None
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def update_segment_metadata(segment_path: Path, **changes: Any) -> None:
    payload = read_segment_metadata(segment_path) or {}
    payload.update(changes)
    metadata_path = metadata_path_for_segment(segment_path)
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def move_segment_metadata(old_path: Path, new_path: Path) -> None:
    old_metadata = metadata_path_for_segment(old_path)
    if not old_metadata.exists():
        return

    new_metadata = metadata_path_for_segment(new_path)
    if new_metadata.exists():
        new_metadata.unlink()
    old_metadata.rename(new_metadata)


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
            "没有找到 ffmpeg。请先安装 ffmpeg，或者把 ffmpeg 放到项目目录内，"
            "也可以在后台手动填写 ffmpeg 路径。"
        )

    work_dir = Path(mkdtemp(prefix="songcut-work-", dir=str(temp_root or Path.cwd())))
    analysis_wav = work_dir / "analysis.wav"

    try:
        if (options.extraction_mode or "classic").strip().lower() == "gpu-model":
            from gpu_songcut_extractor import extract_segments_with_gpu_model

            summary = extract_segments_with_gpu_model(
                source_path=source_path,
                ffmpeg_path=resolved_ffmpeg,
                options=options,
            )
        else:
            _decode_to_analysis_wav(resolved_ffmpeg, source_path, analysis_wav)
            summary = _analyze_wav(analysis_wav, options)

        output_dir = songcut_root / normalize_storage_name(category, fallback="自动提取")
        output_dir.mkdir(parents=True, exist_ok=True)

        if not summary.segments:
            return summary, resolved_ffmpeg

        base_name = build_recording_date_label(source_path)
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
            write_segment_metadata(
                output_path,
                source_path=source_path,
                category=category,
                start=segment.start,
                end=segment.end,
                duration=segment.duration,
                output_format=options.output_format,
            )

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
            energies.append(calculate_pcm_rms(data, sample_width, channels))

    if not energies:
        raise SongcutExtractionError("音频为空，无法分析直播内容。")

    smoothed = _smooth_values(energies)
    noise_floor = _percentile(energies, 0.35)
    loud_rms = _percentile(energies, 0.90)
    threshold = max(120.0, noise_floor * 2.15, noise_floor + (loud_rms - noise_floor) * 0.18)
    if loud_rms > 0:
        threshold = min(threshold, loud_rms * 0.82)
    threshold = max(threshold, 120.0)

    active_windows = [value >= threshold for value in smoothed]
    intro_threshold = max(32.0, min(90.0, noise_floor * 1.08))
    intro_windows = [value >= intro_threshold for value in smoothed]
    options = apply_boundary_env_overrides(options)
    segments = _detect_segments(
        energies=energies,
        active_windows=active_windows,
        intro_windows=intro_windows,
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
    intro_windows: list[bool],
    total_duration: float,
    options: ExtractionOptions,
) -> list[Segment]:
    if not energies:
        return []

    gap_limit = max(1, round(options.max_silence / options.analysis_window))
    merge_gap_limit = max(gap_limit, round(options.merge_gap / options.analysis_window))
    raw_ranges: list[tuple[int, int]] = []
    start_index: Optional[int] = None
    last_active_index: Optional[int] = None

    def build_segment(segment_start: int, segment_end: int) -> Optional[Segment]:
        window_count = segment_end - segment_start
        if window_count <= 0:
            return None

        duration = window_count * options.analysis_window
        active_count = sum(1 for flag in active_windows[segment_start:segment_end] if flag)
        active_ratio = active_count / window_count
        average_rms = sum(energies[segment_start:segment_end]) / window_count
        peak_rms = max(energies[segment_start:segment_end])

        if duration < options.min_duration or active_ratio < options.min_active_ratio:
            return None

        expanded_start = _expand_segment_start(
            segment_start=segment_start,
            intro_windows=intro_windows,
            active_windows=active_windows,
            options=options,
        )
        expanded_end = _expand_segment_end(
            segment_end=segment_end,
            intro_windows=intro_windows,
            active_windows=active_windows,
            options=options,
        )
        start_time = max(0.0, expanded_start * options.analysis_window - options.leading_padding)
        end_time = min(total_duration, expanded_end * options.analysis_window + options.trailing_padding)

        return Segment(
            index=0,
            start=start_time,
            end=end_time,
            duration=end_time - start_time,
            active_ratio=active_ratio,
            average_rms=average_rms,
            peak_rms=peak_rms,
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
            raw_ranges.append((start_index, last_active_index + 1))
            start_index = None
            last_active_index = None

    if start_index is not None and last_active_index is not None:
        raw_ranges.append((start_index, last_active_index + 1))

    if not raw_ranges:
        return []

    merged_ranges: list[tuple[int, int]] = []
    current_start, current_end = raw_ranges[0]
    for next_start, next_end in raw_ranges[1:]:
        if next_start - current_end <= merge_gap_limit:
            current_end = next_end
            continue
        merged_ranges.append((current_start, current_end))
        current_start, current_end = next_start, next_end
    merged_ranges.append((current_start, current_end))

    detected: list[Segment] = []
    for segment_start, segment_end in merged_ranges:
        segment = build_segment(segment_start, segment_end)
        if segment is not None:
            detected.append(segment)

    return merge_overlapping_segments(detected)


def merge_overlapping_segments(segments: list[Segment]) -> list[Segment]:
    """Fuse cuts whose expanded intro/outro ranges overlap into single cuts.

    A song with a quiet interlude longer than the merge gap splits into two
    raw ranges; boundary expansion then makes both cuts claim the interlude,
    so the exported files overlap. Overlapping cuts duplicate audio between
    files - fuse them back into one continuous cut instead.
    """
    if len(segments) <= 1:
        return segments

    merged: list[Segment] = []
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        if merged and segment.start <= merged[-1].end:
            previous = merged[-1]
            previous.end = max(previous.end, segment.end)
            previous.duration = previous.end - previous.start
            previous.active_ratio = max(previous.active_ratio, segment.active_ratio)
            previous.peak_rms = max(previous.peak_rms, segment.peak_rms)
            previous.average_rms = (previous.average_rms + segment.average_rms) / 2.0
            continue
        merged.append(segment)

    for index, segment in enumerate(merged, start=1):
        segment.index = index
    return merged


def expand_boundary_over_audibility(
    *,
    audible_windows: list[bool],
    active_windows: list[bool],
    anchor: int,
    direction: int,
    max_search_windows: int,
    silence_limit_windows: int,
) -> Optional[int]:
    """Extend a segment boundary over quiet-but-audible music (intro/outro).

    Scans away from `anchor` (the first window index outside the segment on the
    scanning side). The boundary keeps extending while windows are audible but
    not active (quiet instrumental passages), and stops at sustained silence, at
    loud active content such as speech, or when the search budget runs out.

    Returns the furthest audible window index reached, or None when no audible
    audio was found (the caller should keep the original boundary).
    """
    if not audible_windows or max_search_windows <= 0 or direction not in (-1, 1):
        return None

    size = len(audible_windows)
    if direction < 0:
        indices = range(anchor - 1, anchor - max_search_windows - 1, -1)
    else:
        # The outro side starts at the anchor itself: for an exclusive boundary
        # index the anchor is already the first window outside the segment.
        indices = range(anchor, anchor + max_search_windows)

    best: Optional[int] = None
    silence_run = 0

    for index in indices:
        if index < 0 or index >= size:
            break

        if active_windows[index]:
            break

        if audible_windows[index]:
            best = index
            silence_run = 0
            continue

        silence_run += 1
        if silence_run >= silence_limit_windows:
            break

    return best


def _expand_segment_start(
    segment_start: int,
    intro_windows: list[bool],
    active_windows: list[bool],
    options: ExtractionOptions,
) -> int:
    expanded = expand_boundary_over_audibility(
        audible_windows=intro_windows,
        active_windows=active_windows,
        anchor=segment_start,
        direction=-1,
        max_search_windows=max(1, round(options.intro_search / options.analysis_window)),
        silence_limit_windows=max(1, round(options.intro_silence / options.analysis_window)),
    )
    return expanded if expanded is not None and expanded < segment_start else segment_start


def _expand_segment_end(
    segment_end: int,
    intro_windows: list[bool],
    active_windows: list[bool],
    options: ExtractionOptions,
) -> int:
    expanded = expand_boundary_over_audibility(
        audible_windows=intro_windows,
        active_windows=active_windows,
        anchor=segment_end,
        direction=1,
        max_search_windows=max(1, round(options.outro_search / options.analysis_window)),
        silence_limit_windows=max(1, round(options.outro_silence / options.analysis_window)),
    )
    expanded_end = expanded + 1 if expanded is not None else segment_end
    return max(segment_end, expanded_end)


def apply_boundary_env_overrides(options: ExtractionOptions) -> ExtractionOptions:
    return replace(
        options,
        intro_search=max(0.0, _env_float("SONGCUT_INTRO_SEARCH_SECONDS", options.intro_search)),
        intro_silence=max(0.5, _env_float("SONGCUT_INTRO_SILENCE_SECONDS", options.intro_silence)),
        outro_search=max(0.0, _env_float("SONGCUT_OUTRO_SEARCH_SECONDS", options.outro_search)),
        outro_silence=max(0.5, _env_float("SONGCUT_OUTRO_SILENCE_SECONDS", options.outro_silence)),
    )


@dataclass
class RegionActivity:
    """Energy statistics for one region, used to tell singing from BGM/chat."""

    mean_rms: float
    cv: float
    active_ratio: float
    longest_active_run_seconds: float
    quiet_ratio: float


def region_activity_from_energies(
    energies: list[int],
    *,
    window_seconds: float,
    active_flags: list[bool],
) -> RegionActivity:
    count = len(energies)
    mean = sum(energies) / count if count else 0.0
    variance = sum((value - mean) ** 2 for value in energies) / count if count else 0.0
    cv = (variance ** 0.5) / mean if mean > 0 else 0.0
    active_ratio = sum(1 for flag in active_flags if flag) / count if count else 0.0
    quiet_ratio = sum(1 for value in energies if value < 600) / count if count else 0.0

    longest_run = 0
    current_run = 0
    for flag in active_flags:
        if flag:
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0

    return RegionActivity(
        mean_rms=mean,
        cv=cv,
        active_ratio=active_ratio,
        longest_active_run_seconds=longest_run * window_seconds,
        quiet_ratio=quiet_ratio,
    )


def measure_region_activity(
    *,
    ffmpeg_path: str,
    source_path: Path,
    start: float,
    end: float,
    absolute_threshold: Optional[float] = None,
    window_seconds: float = 0.5,
) -> Optional[RegionActivity]:
    """Measure singing-likeness of [start, end] directly from the recording.

    Uses the stream-wide active threshold when available (classic mode); falls
    back to a local percentile threshold for pipelines that don't produce one.
    """
    if end - start < 10.0:
        return None

    work_dir = Path(mkdtemp(prefix="songcut-activity-"))
    try:
        wav_path = work_dir / "region.wav"
        try:
            _decode_window_wav(ffmpeg_path, source_path, wav_path, start=start, duration=end - start)
        except SongcutExtractionError:
            return None
        energies, actual_window = _read_wav_energies(wav_path, window_seconds)
        if not energies:
            return None

        if absolute_threshold is not None and absolute_threshold >= 100:
            active_flags = [value >= absolute_threshold for value in energies]
        else:
            _, active_flags = _energy_flags_for_neighborhood(energies)

        return region_activity_from_energies(
            energies,
            window_seconds=actual_window,
            active_flags=active_flags,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def calculate_pcm_rms(data: bytes, sample_width: int, channels: int = 1) -> int:
    if not data or sample_width <= 0:
        return 0

    safe_channels = max(1, channels)
    frame_width = sample_width * safe_channels
    frame_count = len(data) // frame_width
    if frame_count <= 0:
        return 0

    total_square = 0.0
    for frame_index in range(frame_count):
        frame_start = frame_index * frame_width
        sample_sum = 0.0
        for channel_index in range(safe_channels):
            sample_start = frame_start + channel_index * sample_width
            sample_sum += _decode_pcm_sample(data[sample_start:sample_start + sample_width], sample_width)
        sample_value = sample_sum / safe_channels
        total_square += sample_value * sample_value

    return int(math.sqrt(total_square / frame_count))


def _decode_pcm_sample(raw_sample: bytes, sample_width: int) -> int:
    if sample_width == 1:
        return raw_sample[0] - 128 if raw_sample else 0
    return int.from_bytes(raw_sample, byteorder="little", signed=True)



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


def build_recording_date_label(source_path: Path) -> str:
    candidates = [
        source_path.stem,
        source_path.name,
    ]
    for text in candidates:
        label = extract_date_label(text)
        if label:
            return label

    try:
        modified = datetime.fromtimestamp(source_path.stat().st_mtime)
        return modified.strftime("%Y-%m-%d")
    except OSError:
        return "recording"


def extract_date_label(text: str) -> Optional[str]:
    patterns = [
        r"(?P<year>20\d{2})[.\-_/](?P<month>\d{1,2})[.\-_/](?P<day>\d{1,2})",
        r"(?P<year>20\d{2})(?P<month>\d{2})(?P<day>\d{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue

        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        try:
            parsed = datetime(year, month, day)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d")

    return None


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


def export_segment(
    ffmpeg_path: str,
    source_path: Path,
    output_path: Path,
    start: float,
    end: float,
    output_format: str,
) -> None:
    _export_segment(
        ffmpeg_path=ffmpeg_path,
        source_path=source_path,
        output_path=output_path,
        start=start,
        end=end,
        output_format=output_format,
    )


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
        _run_ffmpeg(
            base_command + ["-c:a", "libmp3lame", "-q:a", "2", str(output_path)],
            "导出 MP3 歌切失败",
        )
    except SongcutExtractionError:
        _run_ffmpeg(base_command + ["-c:a", "mp3", str(output_path)], "导出 MP3 歌切失败")


def _run_ffmpeg(command: list[str], context: str) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return

    stderr = (completed.stderr or completed.stdout or "").strip()
    snippet = stderr.splitlines()[-1] if stderr else "unknown ffmpeg error"
    raise SongcutExtractionError(f"{context}: {snippet}")


def refine_segment_boundaries(
    *,
    ffmpeg_path: str,
    source_path: Path,
    segments: list[Segment],
    total_duration: float,
    options: ExtractionOptions,
    temp_dir: Optional[Path] = None,
) -> None:
    """Extend segment boundaries over quiet intros/outros using local energy scans.

    Used by model-based pipelines (e.g. inaSpeechSegmenter) whose labels can clip
    quiet instrumental passages: for every segment the neighbourhood of each
    boundary is decoded and scanned for audible-but-quiet music so the complete
    intro/outro stays inside the exported cut.
    """
    if not segments:
        return

    options = apply_boundary_env_overrides(options)
    work_dir = Path(mkdtemp(prefix="songcut-refine-", dir=str(temp_dir))) if temp_dir else Path(
        mkdtemp(prefix="songcut-refine-")
    )

    try:
        for segment in segments:
            head_start = refine_boundary_with_energy(
                ffmpeg_path=ffmpeg_path,
                source_path=source_path,
                boundary=segment.start,
                direction=-1,
                search_seconds=options.intro_search,
                silence_seconds=options.intro_silence,
                guard_seconds=2.0,
                temp_dir=work_dir,
                token=f"s{segment.index}h",
                total_duration=total_duration,
            )
            if head_start is not None:
                segment.start = max(0.0, round(head_start, 3))

            tail_end = refine_boundary_with_energy(
                ffmpeg_path=ffmpeg_path,
                source_path=source_path,
                boundary=segment.end,
                direction=1,
                search_seconds=options.outro_search,
                silence_seconds=options.outro_silence,
                guard_seconds=2.0,
                temp_dir=work_dir,
                token=f"s{segment.index}t",
                total_duration=total_duration,
            )
            if tail_end is not None:
                segment.end = min(total_duration, round(tail_end, 3))

            if segment.end > segment.start:
                segment.duration = round(segment.end - segment.start, 3)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def refine_boundary_with_energy(
    *,
    ffmpeg_path: str,
    source_path: Path,
    boundary: float,
    direction: int,
    search_seconds: float,
    silence_seconds: float,
    guard_seconds: float,
    temp_dir: Path,
    token: str,
    total_duration: float,
    analysis_window: float = 0.5,
) -> Optional[float]:
    """Return an expanded absolute time for one boundary, or None to keep it."""
    if search_seconds <= 0:
        return None

    window_seconds = analysis_window
    if direction < 0:
        region_start = max(0.0, boundary - search_seconds - guard_seconds)
        region_end = max(region_start + window_seconds, boundary + guard_seconds)
    else:
        region_start = max(0.0, boundary - guard_seconds)
        region_limit = total_duration if total_duration > 0 else boundary + search_seconds + guard_seconds
        region_end = min(
            max(region_start + window_seconds, boundary + search_seconds + guard_seconds),
            region_limit,
        )

    if region_end - region_start < window_seconds * 2:
        return None

    wav_path = temp_dir / f"refine_{token}.wav"
    _decode_window_wav(
        ffmpeg_path,
        source_path,
        wav_path,
        start=region_start,
        duration=region_end - region_start,
    )
    energies, actual_window = _read_wav_energies(wav_path, window_seconds)
    if not energies:
        return None

    audible_flags, active_flags = _energy_flags_for_neighborhood(energies)
    anchor = round((boundary - region_start) / actual_window)
    anchor = max(0, min(len(energies), anchor))

    expanded = expand_boundary_over_audibility(
        audible_windows=audible_flags,
        active_windows=active_flags,
        anchor=anchor,
        direction=direction,
        max_search_windows=max(1, round(search_seconds / actual_window)),
        silence_limit_windows=max(1, round(silence_seconds / actual_window)),
    )
    if expanded is None:
        return None

    return region_start + expanded * actual_window


def _decode_window_wav(
    ffmpeg_path: str,
    source_path: Path,
    target_path: Path,
    *,
    start: float,
    duration: float,
) -> None:
    command = [
        ffmpeg_path,
        "-y",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{max(0.1, duration):.3f}",
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
    _run_ffmpeg(command, "解码边界分析音频失败")


def _read_wav_energies(wav_path: Path, window_seconds: float = 0.5) -> tuple[list[int], float]:
    energies: list[int] = []
    with wave.open(str(wav_path), "rb") as wav_file:
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        frames_per_window = max(1, int(sample_rate * window_seconds))
        actual_window_seconds = frames_per_window / sample_rate if sample_rate else window_seconds

        while True:
            data = wav_file.readframes(frames_per_window)
            if not data:
                break
            energies.append(calculate_pcm_rms(data, sample_width, channels))
    return energies, actual_window_seconds


def _energy_flags_for_neighborhood(energies: list[int]) -> tuple[list[bool], list[bool]]:
    noise_floor = _percentile(energies, 0.35)
    loud_rms = _percentile(energies, 0.90)
    audible_threshold = max(24.0, min(90.0, noise_floor * 1.12))
    active_threshold = max(60.0, noise_floor * 2.15, noise_floor + (loud_rms - noise_floor) * 0.18)
    if loud_rms > 0:
        active_threshold = min(active_threshold, loud_rms * 0.82)

    audible = [value >= audible_threshold for value in energies]
    active = [value >= active_threshold for value in energies]
    return audible, active
