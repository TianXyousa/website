from __future__ import annotations

import gc
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from songcut_extractor import (
    AnalysisSummary,
    ExtractionOptions,
    Segment,
    SongcutExtractionError,
    merge_overlapping_segments,
    refine_segment_boundaries,
)


DEFAULT_INASEG_BATCH_SIZE = 32
DEFAULT_INASEG_ENERGY_RATIO = 0.03
DEFAULT_INASEG_CHUNK_SECONDS = 800
DEFAULT_INASEG_CHUNK_OVERLAP_SECONDS = 90
DEFAULT_INASEG_MIN_MUSIC_RATIO = 0.55
DEFAULT_NOENERGY_BRIDGE_SECONDS = 4.0
DEFAULT_START_PADDING_SECONDS = 1.0
DEFAULT_END_PADDING_SECONDS = 4.0

logger = logging.getLogger("uvicorn.error")


def _emit(message: str) -> None:
    print(f"[gpu-model] {message}", flush=True)
    logger.info(message)


def describe_gpu_model_backend() -> dict[str, Any]:
    tensorflow_available = _python_module_available("tensorflow")
    segmenter_available = _python_module_available("inaSpeechSegmenter")

    cuda_available = False
    device_name = ""
    if tensorflow_available:
        try:
            import tensorflow as tf

            devices = tf.config.list_physical_devices("GPU")
            cuda_available = bool(devices)
            if devices:
                device_name = devices[0].name.rsplit("/", 1)[-1]
        except Exception:
            cuda_available = False
            device_name = ""

    return {
        "mode": "gpu-model",
        "backend": "inaSpeechSegmenter",
        "configured": tensorflow_available and segmenter_available,
        "cuda_available": cuda_available,
        "device_name": device_name,
        "tensorflow_available": tensorflow_available,
        "segmenter_available": segmenter_available,
        "batch_size": _int_env("SONGCUT_INASEG_BATCH_SIZE", DEFAULT_INASEG_BATCH_SIZE),
        "energy_ratio": _float_env("SONGCUT_INASEG_ENERGY_RATIO", DEFAULT_INASEG_ENERGY_RATIO),
        "chunk_seconds": _int_env("SONGCUT_INASEG_CHUNK_SECONDS", DEFAULT_INASEG_CHUNK_SECONDS),
        "chunk_overlap_seconds": _float_env(
            "SONGCUT_INASEG_CHUNK_OVERLAP_SECONDS", DEFAULT_INASEG_CHUNK_OVERLAP_SECONDS
        ),
        "min_music_ratio": _float_env("SONGCUT_INASEG_MIN_MUSIC_RATIO", DEFAULT_INASEG_MIN_MUSIC_RATIO),
        "boundary_refinement": os.getenv("SONGCUT_BOUNDARY_REFINEMENT_ENABLED", "true")
        .strip()
        .lower()
        not in {"0", "false", "no", "off"},
    }


def extract_segments_with_gpu_model(
    *,
    source_path: Path,
    ffmpeg_path: str,
    options: ExtractionOptions,
) -> AnalysisSummary:
    if not _python_module_available("tensorflow"):
        raise SongcutExtractionError("当前 GPU 镜像缺少 TensorFlow，无法运行旧项目的分段方案。")
    if not _python_module_available("inaSpeechSegmenter"):
        raise SongcutExtractionError("当前 GPU 镜像缺少 inaSpeechSegmenter，无法运行旧项目的分段方案。")

    total_duration = _probe_duration_seconds(source_path, ffmpeg_path)
    _emit(f"started for {source_path.name}")

    segmentation = _segment_media(
        source_path=source_path,
        total_duration=total_duration,
    )
    _emit(f"segmentation finished for {source_path.name}: {len(segmentation)} raw labels")

    segments = _build_segments_from_segmentation(
        segmentation=segmentation,
        total_duration=total_duration,
        options=options,
        source_path=source_path,
        ffmpeg_path=ffmpeg_path,
    )
    _emit(f"finished for {source_path.name}: {len(segmentation)} labels, {len(segments)} segments")

    return AnalysisSummary(
        total_duration=total_duration,
        threshold_rms=_float_env("SONGCUT_INASEG_ENERGY_RATIO", DEFAULT_INASEG_ENERGY_RATIO),
        noise_floor_rms=0.0,
        loud_rms=0.0,
        windows=len(segmentation),
        segments=segments,
    )


def _segment_media(*, source_path: Path, total_duration: float) -> list[tuple[str, float, float]]:
    batch_size = max(1, _int_env("SONGCUT_INASEG_BATCH_SIZE", DEFAULT_INASEG_BATCH_SIZE))
    energy_ratio = _float_env("SONGCUT_INASEG_ENERGY_RATIO", DEFAULT_INASEG_ENERGY_RATIO)
    chunk_seconds = max(0, _int_env("SONGCUT_INASEG_CHUNK_SECONDS", DEFAULT_INASEG_CHUNK_SECONDS))
    overlap_seconds = max(
        0.0, _float_env("SONGCUT_INASEG_CHUNK_OVERLAP_SECONDS", DEFAULT_INASEG_CHUNK_OVERLAP_SECONDS)
    )
    ranges = _build_processing_ranges(total_duration, chunk_seconds, overlap_seconds)

    items: list[tuple[str, float, float, int]] = []
    for index, (start_sec, stop_sec) in enumerate(ranges):
        _emit(
            f"segment chunk {index + 1}/{len(ranges)} started"
            + (f": {start_sec:.1f}-{stop_sec:.1f}s" if start_sec is not None and stop_sec is not None else "")
        )
        chunk_result = _run_inaseg_chunk(
            source_path=source_path,
            batch_size=batch_size,
            energy_ratio=energy_ratio,
            start_sec=start_sec,
            stop_sec=stop_sec,
        )
        for label, start, end in chunk_result:
            items.append((label, start, end, index))

    resolved = _resolve_chunk_overlaps(items, ranges)
    return _merge_touching_labels(_normalize_segmentation(resolved))


def _resolve_chunk_overlaps(
    items: list[tuple[str, float, float, int]],
    ranges: list[tuple[Optional[int], Optional[int]]],
) -> list[tuple[str, float, float]]:
    """Deduplicate labels produced by overlapping chunks.

    Labels sitting close to a processing edge are less reliable, so when labels
    from different chunks overlap in time the one farther from its chunk edge
    wins; overlapping labels that agree are merged.
    """

    def edge_distance(item: tuple[str, float, float, int]) -> float:
        _, start, end, chunk_index = item
        chunk_start, chunk_stop = ranges[chunk_index]
        if chunk_start is None or chunk_stop is None:
            return 1e9
        return max(0.0, min(start - float(chunk_start), float(chunk_stop) - end))

    resolved: list[tuple[str, float, float, float]] = []
    for item in sorted(items, key=lambda entry: (entry[1], entry[2])):
        label, start, end = item[0], item[1], item[2]
        distance = edge_distance(item)
        if resolved:
            prev_label, prev_start, prev_end, prev_distance = resolved[-1]
            if start < prev_end - 0.5:
                if label == prev_label:
                    resolved[-1] = (
                        prev_label,
                        min(prev_start, start),
                        max(prev_end, end),
                        max(prev_distance, distance),
                    )
                elif distance > prev_distance:
                    resolved[-1] = (label, start, end, distance)
                continue
        resolved.append((label, start, end, distance))

    return [(label, start, end) for label, start, end, _ in resolved]


def _run_inaseg_chunk(
    *,
    source_path: Path,
    batch_size: int,
    energy_ratio: float,
    start_sec: int | None,
    stop_sec: int | None,
) -> list[tuple[str, float, float]]:
    from inaSpeechSegmenter import Segmenter
    import tensorflow as tf

    segmenter = Segmenter(
        vad_engine="sm",
        detect_gender=False,
        energy_ratio=energy_ratio,
        batch_size=batch_size,
    )
    raw_segments = segmenter(
        str(source_path),
        start_sec=start_sec,
        stop_sec=stop_sec,
    )

    chunk_base = float(start_sec or 0)
    chunk_duration = float((stop_sec or 0) - (start_sec or 0)) if start_sec is not None and stop_sec is not None else None
    normalized: list[tuple[str, float, float]] = []
    for entry in raw_segments:
        if len(entry) != 3:
            continue
        label = str(entry[0]).strip()
        start = float(entry[1])
        end = float(entry[2])
        if chunk_duration is not None and end <= chunk_duration + 1:
            start += chunk_base
            end += chunk_base
        normalized.append((label, max(0.0, start), max(0.0, end)))

    try:
        tf.keras.backend.clear_session()
    except Exception:
        pass
    gc.collect()
    return normalized


def _build_processing_ranges(
    total_duration: float,
    chunk_seconds: int,
    overlap_seconds: float = 0.0,
) -> list[tuple[Optional[int], Optional[int]]]:
    safe_total = max(0, int(total_duration))
    if chunk_seconds <= 0 or safe_total <= 0 or safe_total <= chunk_seconds:
        return [(None, None)]

    overlap = max(0.0, min(float(overlap_seconds), chunk_seconds * 0.5))
    ranges: list[tuple[Optional[int], Optional[int]]] = []
    start = 0
    while True:
        stop = min(safe_total, start + chunk_seconds)
        ranges.append((start, stop))
        if stop >= safe_total:
            break
        # overlap is clamped below chunk_seconds, so start strictly advances.
        start = max(0, int(round(stop - overlap)))
    return ranges or [(None, None)]


def _build_segments_from_segmentation(
    *,
    segmentation: list[tuple[str, float, float]],
    total_duration: float,
    options: ExtractionOptions,
    source_path: Optional[Path] = None,
    ffmpeg_path: Optional[str] = None,
) -> list[Segment]:
    bridged = _bridge_noenergy_segments(
        segmentation,
        max_gap=min(
            max(options.max_silence, 0.0),
            _float_env("SONGCUT_INASEG_NOENERGY_BRIDGE_SECONDS", DEFAULT_NOENERGY_BRIDGE_SECONDS),
        ),
    )

    lead_pad = max(options.leading_padding, DEFAULT_START_PADDING_SECONDS)
    trail_pad = max(options.trailing_padding, DEFAULT_END_PADDING_SECONDS)

    music_labels = [(start, end) for label, start, end in bridged if label == "music" and end > start]
    merged_music = _merge_time_ranges([[start, end] for start, end in music_labels], max_gap=0.05)

    padded_runs: list[list[float]] = []
    for start, end in merged_music:
        padded_runs.append([max(0.0, start - lead_pad), min(total_duration, end + trail_pad)])

    merged = _merge_time_ranges(padded_runs, max_gap=max(0.0, options.max_silence))

    min_music_ratio = _float_env("SONGCUT_INASEG_MIN_MUSIC_RATIO", DEFAULT_INASEG_MIN_MUSIC_RATIO)
    refinement_enabled = os.getenv("SONGCUT_BOUNDARY_REFINEMENT_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    segments: list[Segment] = []
    for start, end in merged:
        if start < 5.0:
            continue
        duration = max(0.0, end - start)
        if duration < max(15.0, options.min_duration):
            continue

        if min_music_ratio > 0 and duration > 0:
            music_ratio = _music_overlap_seconds(merged_music, start, end) / duration
            if music_ratio < min_music_ratio:
                _emit(
                    f"drop candidate {start:.1f}-{end:.1f}s: "
                    f"music ratio {music_ratio:.2f} < {min_music_ratio:.2f} (likely BGM under speech)"
                )
                continue

        segments.append(
            Segment(
                index=0,
                start=round(start, 3),
                end=round(end, 3),
                duration=round(duration, 3),
                active_ratio=1.0,
                average_rms=0.0,
                peak_rms=0,
            )
        )

    if refinement_enabled and segments and source_path is not None and ffmpeg_path:
        try:
            refine_segment_boundaries(
                ffmpeg_path=ffmpeg_path,
                source_path=source_path,
                segments=segments,
                total_duration=total_duration,
                options=options,
            )
        except Exception as exc:
            _emit(f"boundary refinement skipped due to error: {exc}")

    segments = merge_overlapping_segments(segments)
    for index, segment in enumerate(segments, start=1):
        segment.index = index
    return segments


def _music_overlap_seconds(
    music_ranges: list[tuple[float, float]],
    start: float,
    end: float,
) -> float:
    total = 0.0
    for music_start, music_end in music_ranges:
        overlap = min(end, music_end) - max(start, music_start)
        if overlap > 0:
            total += overlap
    return total


def _bridge_noenergy_segments(
    segmentation: list[tuple[str, float, float]],
    *,
    max_gap: float,
) -> list[tuple[str, float, float]]:
    if not segmentation:
        return []

    items = list(segmentation)
    index = 1
    while index < len(items) - 1:
        label, start, end = items[index]
        if (
            label == "noEnergy"
            and end - start <= max_gap
            and items[index - 1][0] == items[index + 1][0]
        ):
            previous = items[index - 1]
            following = items[index + 1]
            items[index - 1] = (previous[0], previous[1], following[2])
            del items[index:index + 2]
            continue
        index += 1
    return items


def _normalize_segmentation(segmentation: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    items: list[tuple[str, float, float]] = []
    for label, start, end in sorted(segmentation, key=lambda item: (item[1], item[2])):
        clean_label = label.strip()
        if end <= start:
            continue
        items.append((clean_label, float(start), float(end)))
    return items


def _merge_touching_labels(segmentation: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    if not segmentation:
        return []

    merged = [segmentation[0]]
    for label, start, end in segmentation[1:]:
        prev_label, prev_start, prev_end = merged[-1]
        if label == prev_label and start <= prev_end + 0.05:
            merged[-1] = (prev_label, prev_start, max(prev_end, end))
            continue
        merged.append((label, start, end))
    return merged


def _merge_time_ranges(ranges: list[list[float]], *, max_gap: float) -> list[tuple[float, float]]:
    if not ranges:
        return []

    ordered = sorted((float(start), float(end)) for start, end in ranges if end > start)
    merged: list[tuple[float, float]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start - prev_end <= max_gap:
            merged[-1] = (prev_start, max(prev_end, end))
            continue
        merged.append((start, end))
    return merged


def _probe_duration_seconds(source_path: Path, ffmpeg_path: str) -> float:
    ffprobe_path = _resolve_ffprobe_binary(ffmpeg_path)
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SongcutExtractionError("无法读取录播时长，旧项目分段方案无法继续。")
    try:
        return max(0.0, float((completed.stdout or "").strip()))
    except ValueError as exc:
        raise SongcutExtractionError("录播时长解析失败，旧项目分段方案无法继续。") from exc


def _resolve_ffprobe_binary(ffmpeg_path: str) -> str:
    candidate = (ffmpeg_path or "").strip()
    if candidate.endswith("ffmpeg.exe"):
        return candidate[:-10] + "ffprobe.exe"
    if candidate.endswith("ffmpeg"):
        return candidate[:-6] + "ffprobe"
    return "ffprobe"


def _python_module_available(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except Exception:
        return False


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
