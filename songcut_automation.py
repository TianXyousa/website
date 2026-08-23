from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
import wave

from songcut_extractor import (
    calculate_pcm_rms,
    find_ffmpeg_binary,
    move_segment_metadata,
    normalize_storage_name,
    update_segment_metadata,
)


@dataclass
class SampleMatch:
    """A single fingerprint query result, tied to where the sample was taken."""

    offset: float
    title: str
    artist: str = ""
    confidence: float = 0.0
    play_offset_ms: int = 0
    duration_ms: int = 0


@dataclass
class SongRecognition:
    title: str
    artist: str = ""
    confidence: float = 0.0
    provider: str = "acrcloud"
    matched_samples: int = 0
    sample_count: int = 0
    average_confidence: float = 0.0
    best_confidence: float = 0.0
    samples: list[SampleMatch] = field(default_factory=list)
    all_matches: list[SampleMatch] = field(default_factory=list)


@dataclass
class SongInterval:
    """A single song's extent inside the recording, derived from fingerprints."""

    start: float
    end: float
    title: str
    artist: str = ""
    confidence: float = 0.0
    matched_samples: int = 0
    samples: list[SampleMatch] = field(default_factory=list)


@dataclass
class BoundaryAlignment:
    start: float
    end: float
    shift_start: float
    shift_end: float
    track_duration: float
    matched_samples: int


def parse_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _python_module_available(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except Exception:
        return False


# Keep Windows CUDA wheels usable by CTranslate2.  The NVIDIA Python wheels
# install DLLs under ``site-packages/nvidia/*/bin`` but do not put those
# directories on PATH.  CTranslate2 can still enumerate the GPU in that
# state, yet the first Whisper inference fails with ``cublas64_12.dll is not
# found``.  ``os.add_dll_directory`` is the supported Windows loader hook;
# retain the handles for the lifetime of the process.
_CUDA_DLL_DIRECTORY_HANDLES: list[Any] = []
_CUDA_DLL_DIRECTORIES_REGISTERED: set[str] = set()


def _configure_cuda_runtime() -> list[str]:
    """Register pip-installed NVIDIA DLL directories before importing Whisper.

    This is intentionally best-effort and platform-gated.  Linux containers
    already expose CUDA libraries through the dynamic linker, while CPU-only
    installs simply return an empty list and continue as before.
    """

    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return []

    candidates: list[Path] = []
    site_roots = [Path(sys.prefix) / "Lib" / "site-packages"]
    try:
        import site

        site_roots.extend(Path(item) for item in site.getsitepackages())
    except Exception:
        pass

    seen: set[str] = set()
    for root in site_roots:
        nvidia_root = root / "nvidia"
        if not nvidia_root.is_dir():
            continue
        candidates.extend(sorted(nvidia_root.glob("*/bin")))

    registered: list[str] = []
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    path_keys = {entry.casefold() for entry in path_entries}
    for directory in candidates:
        key = str(directory.resolve()).casefold()
        if key in seen or key in _CUDA_DLL_DIRECTORIES_REGISTERED or not directory.is_dir():
            continue
        seen.add(key)
        try:
            _CUDA_DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(directory)))
            _CUDA_DLL_DIRECTORIES_REGISTERED.add(key)
            registered.append(str(directory))
            if key not in path_keys:
                path_entries.insert(0, str(directory))
                path_keys.add(key)
        except OSError:
            continue
    if registered:
        os.environ["PATH"] = os.pathsep.join(path_entries)
    return registered


def parse_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def parse_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def describe_whisper_runtime() -> dict[str, Any]:
    """Report the Whisper device that is requested and currently usable."""

    requested = (os.getenv("SONGCUT_WHISPER_DEVICE", "auto").strip().lower() or "auto")
    compute_type = os.getenv("SONGCUT_WHISPER_COMPUTE_TYPE", "").strip()
    cuda_device_count = 0
    try:
        _configure_cuda_runtime()
        import ctranslate2

        cuda_device_count = max(0, int(ctranslate2.get_cuda_device_count()))
    except Exception:
        pass
    return {
        "device_requested": requested,
        "device_resolved": _WHISPER_DEVICE_RESOLVED,
        "compute_type": compute_type or ("float16" if requested in {"auto", "cuda"} else "int8"),
        "cuda_device_count": cuda_device_count,
        "cuda_available": cuda_device_count > 0,
    }


def describe_recognition_provider() -> dict[str, Any]:
    host = os.getenv("ACRCLOUD_HOST", "").strip()
    access_key = os.getenv("ACRCLOUD_ACCESS_KEY", "").strip()
    access_secret = os.getenv("ACRCLOUD_ACCESS_SECRET", "").strip()

    masked_key = ""
    if access_key:
        if len(access_key) <= 8:
            masked_key = access_key[:2] + "*" * max(0, len(access_key) - 2)
        else:
            masked_key = f"{access_key[:4]}...{access_key[-4:]}"

    return {
        "provider": "acrcloud",
        "configured": bool(host and access_key and access_secret),
        "host": host,
        "access_key_masked": masked_key,
        "sample_duration_seconds": max(
            8.0,
            min(12.0, parse_float_env("ACRCLOUD_SAMPLE_DURATION_SECONDS", 12.0)),
        ),
        "max_samples": max(1, parse_int_env("ACRCLOUD_MAX_SAMPLES", 8)),
        "energy_sampling": parse_bool_env("ACRCLOUD_ENERGY_SAMPLE_ENABLED", True),
        "min_confidence": parse_float_env("ACRCLOUD_MIN_CONFIDENCE", 0.68),
        "min_confirmations": parse_int_env("ACRCLOUD_MIN_CONFIRMATIONS", 2),
        "retry_enabled": parse_bool_env("ACRCLOUD_RETRY_ENABLED", True),
        "retry_max_samples": max(1, parse_int_env("ACRCLOUD_RETRY_MAX_SAMPLES", 6)),
        "retry_grid_seconds": max(10.0, parse_float_env("ACRCLOUD_RETRY_GRID_SECONDS", 45.0)),
        "boundary_alignment": parse_bool_env("SONGCUT_BOUNDARY_ALIGNMENT_ENABLED", True),
        "lyric_recognition": {
            "enabled": parse_bool_env("SONGCUT_LYRIC_RECOGNITION_ENABLED", True),
            "whisper_model": os.getenv("SONGCUT_WHISPER_MODEL", "medium").strip() or "small",
            "whisper_available": _python_module_available("faster_whisper"),
            **describe_whisper_runtime(),
            "min_match": parse_float_env("SONGCUT_LYRIC_MIN_MATCH", 0.55),
            "max_windows": max(1, parse_int_env("SONGCUT_LYRIC_MAX_WINDOWS", 4)),
            "window_seconds": max(20.0, parse_float_env("SONGCUT_LYRIC_WINDOW_SECONDS", 45.0)),
        },
    }


def describe_recording_cleanup() -> dict[str, Any]:
    return {
        "enabled": parse_bool_env("RECORDING_CLEANUP_ENABLED", True),
        "threshold_gb": parse_float_env("RECORDING_CLEANUP_THRESHOLD_GB", 250.0),
        "target_gb": parse_float_env("RECORDING_CLEANUP_TARGET_GB", 220.0),
    }


def get_recognition_cache_path() -> Path:
    raw_value = os.getenv("SONG_RECOGNITION_CACHE_PATH", ".song_recognition_cache.json").strip()
    return Path(raw_value or ".song_recognition_cache.json").expanduser()


def load_recognition_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": {}}


def save_recognition_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_recognition_cache_key(segment_path: Path) -> str:
    digest = hashlib.sha1()
    with segment_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lookup_cached_recognition(cache_path: Path, cache_key: str) -> Optional[SongRecognition]:
    payload = load_recognition_cache(cache_path)
    item = payload.get("items", {}).get(cache_key)
    if not isinstance(item, dict):
        return None

    title = str(item.get("title", "")).strip()
    if not title:
        return None

    # Legacy entries lack per-sample fingerprint offsets; treat them as a miss
    # so recognition reruns and the alignment/medley data gets captured.
    if "samples" not in item:
        return None

    confidence = float(item.get("confidence", 0.0) or 0.0)
    if confidence < parse_float_env("ACRCLOUD_MIN_CONFIDENCE", 0.68):
        return None

    samples: list[SampleMatch] = []
    for raw in item.get("samples", []) or []:
        if not isinstance(raw, dict):
            continue
        sample_title = str(raw.get("title", "")).strip()
        if not sample_title:
            continue
        samples.append(
            SampleMatch(
                offset=float(raw.get("offset", 0.0) or 0.0),
                title=sample_title,
                artist=str(raw.get("artist", "")).strip(),
                confidence=float(raw.get("confidence", 0.0) or 0.0),
                play_offset_ms=int(raw.get("play_offset_ms", 0) or 0),
                duration_ms=int(raw.get("duration_ms", 0) or 0),
            )
        )

    def _parse_sample(raw: Any) -> Optional[SampleMatch]:
        if not isinstance(raw, dict):
            return None
        sample_title = str(raw.get("title", "")).strip()
        if not sample_title:
            return None
        return SampleMatch(
            offset=float(raw.get("offset", 0.0) or 0.0),
            title=sample_title,
            artist=str(raw.get("artist", "")).strip(),
            confidence=float(raw.get("confidence", 0.0) or 0.0),
            play_offset_ms=int(raw.get("play_offset_ms", 0) or 0),
            duration_ms=int(raw.get("duration_ms", 0) or 0),
        )

    all_matches = [sample for sample in (_parse_sample(raw) for raw in item.get("all_samples") or []) if sample]
    if not all_matches:
        all_matches = list(samples)
    return SongRecognition(
        title=title,
        artist=str(item.get("artist", "")).strip(),
        confidence=confidence,
        provider=str(item.get("provider", "acrcloud")).strip() or "acrcloud",
        matched_samples=int(item.get("matched_samples", 0) or 0),
        sample_count=int(item.get("sample_count", 0) or 0),
        average_confidence=float(item.get("average_confidence", 0.0) or 0.0),
        best_confidence=float(item.get("best_confidence", confidence) or confidence),
        samples=samples,
        all_matches=all_matches,
    )


def store_cached_recognition(
    cache_path: Path,
    cache_key: str,
    segment_path: Path,
    recognition: SongRecognition,
) -> None:
    payload = load_recognition_cache(cache_path)
    items = payload.setdefault("items", {})
    items[cache_key] = {
        "title": recognition.title,
        "artist": recognition.artist,
        "confidence": recognition.confidence,
        "provider": recognition.provider,
        "matched_samples": recognition.matched_samples,
        "sample_count": recognition.sample_count,
        "average_confidence": recognition.average_confidence,
        "best_confidence": recognition.best_confidence,
        "samples": [
            {
                "offset": round(sample.offset, 3),
                "title": sample.title,
                "artist": sample.artist,
                "confidence": round(sample.confidence, 4),
                "play_offset_ms": sample.play_offset_ms,
                "duration_ms": sample.duration_ms,
            }
            for sample in recognition.samples
        ],
        "all_samples": [
            {
                "offset": round(sample.offset, 3),
                "title": sample.title,
                "artist": sample.artist,
                "confidence": round(sample.confidence, 4),
                "play_offset_ms": sample.play_offset_ms,
                "duration_ms": sample.duration_ms,
            }
            for sample in recognition.all_matches
        ],
        "segment_path": str(segment_path.resolve(strict=False)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_recognition_cache(cache_path, payload)


def load_processed_recordings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": {}}


def save_processed_recordings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def register_processed_recording(
    manifest_path: Path,
    source_path: Path,
    outputs: list[Path],
    song_titles: list[str],
) -> None:
    payload = load_processed_recordings(manifest_path)
    items = payload.setdefault("items", {})
    key = str(source_path.resolve(strict=False))
    items[key] = {
        "source_path": key,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "outputs": [str(path) for path in outputs],
        "song_titles": song_titles,
    }
    save_processed_recordings(manifest_path, payload)


def cleanup_processed_recordings(workdir: Path, manifest_path: Path) -> dict[str, Any]:
    if not parse_bool_env("RECORDING_CLEANUP_ENABLED", True):
        return {"deleted": [], "freed_bytes": 0, "skipped": "disabled"}

    threshold_gb = parse_float_env("RECORDING_CLEANUP_THRESHOLD_GB", 250.0)
    target_gb = parse_float_env("RECORDING_CLEANUP_TARGET_GB", 220.0)
    if target_gb > threshold_gb:
        target_gb = threshold_gb

    usage = shutil.disk_usage(workdir)
    used_gb = usage.used / (1024**3)
    if used_gb < threshold_gb:
        return {"deleted": [], "freed_bytes": 0, "used_gb": round(used_gb, 2)}

    payload = load_processed_recordings(manifest_path)
    items = payload.get("items", {})
    ordered = sorted(items.values(), key=lambda item: item.get("recorded_at", ""))

    deleted: list[str] = []
    freed_bytes = 0

    for item in ordered:
        if used_gb <= target_gb:
            break

        source_path = Path(item.get("source_path", ""))
        try:
            resolved = source_path.resolve(strict=False)
        except OSError:
            continue

        if not resolved.exists():
            items.pop(str(source_path), None)
            continue

        try:
            file_size = resolved.stat().st_size
            resolved.unlink()
            deleted.append(str(resolved))
            freed_bytes += file_size
            items.pop(str(source_path), None)
        except OSError:
            continue

        usage = shutil.disk_usage(workdir)
        used_gb = usage.used / (1024**3)

    save_processed_recordings(manifest_path, payload)
    return {
        "deleted": deleted,
        "freed_bytes": freed_bytes,
        "used_gb": round(used_gb, 2),
        "threshold_gb": threshold_gb,
        "target_gb": target_gb,
    }


def recognize_song_title(
    segment_path: Path,
    duration: float,
    ffmpeg_path: Optional[str] = None,
) -> Optional[SongRecognition]:
    host = os.getenv("ACRCLOUD_HOST", "").strip()
    access_key = os.getenv("ACRCLOUD_ACCESS_KEY", "").strip()
    access_secret = os.getenv("ACRCLOUD_ACCESS_SECRET", "").strip()
    if not host or not access_key or not access_secret:
        return None

    resolved_ffmpeg = find_ffmpeg_binary(ffmpeg_path)
    if not resolved_ffmpeg:
        return None

    cache_path = get_recognition_cache_path()
    cache_key = build_recognition_cache_key(segment_path)
    cached_recognition = lookup_cached_recognition(cache_path, cache_key)
    if cached_recognition:
        return cached_recognition

    temp_dir = Path(mkdtemp(prefix="acr-sample-", dir=str(segment_path.parent)))
    matches: list[SampleMatch] = []

    try:
        sample_duration = max(8.0, min(12.0, parse_float_env("ACRCLOUD_SAMPLE_DURATION_SECONDS", 12.0)))
        sample_offsets = build_recognition_sample_offsets(
            segment_path=segment_path,
            total_duration=duration,
            sample_duration=sample_duration,
            ffmpeg_path=resolved_ffmpeg,
            temp_dir=temp_dir,
        )
        attempted = len(sample_offsets)
        matches = _recognize_at_offsets(
            ffmpeg_path=resolved_ffmpeg,
            segment_path=segment_path,
            sample_offsets=sample_offsets,
            sample_duration=sample_duration,
            temp_dir=temp_dir,
            host=host,
            access_key=access_key,
            access_secret=access_secret,
        )

        accepted = choose_recognition_result(matches)
        if accepted is None and parse_bool_env("ACRCLOUD_RETRY_ENABLED", True):
            retry_offsets = build_retry_sample_offsets(
                total_duration=duration,
                sample_duration=sample_duration,
                used_offsets=sample_offsets,
            )
            attempted += len(retry_offsets)
            retry_matches = _recognize_at_offsets(
                ffmpeg_path=resolved_ffmpeg,
                segment_path=segment_path,
                sample_offsets=retry_offsets,
                sample_duration=sample_duration,
                temp_dir=temp_dir,
                host=host,
                access_key=access_key,
                access_secret=access_secret,
            )
            matches.extend(retry_matches)
            accepted = choose_recognition_result(matches)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if accepted:
        accepted.sample_count = attempted
        store_cached_recognition(cache_path, cache_key, segment_path, accepted)
    return accepted


def _recognize_at_offsets(
    *,
    ffmpeg_path: str,
    segment_path: Path,
    sample_offsets: list[float],
    sample_duration: float,
    temp_dir: Path,
    host: str,
    access_key: str,
    access_secret: str,
) -> list[SampleMatch]:
    matches: list[SampleMatch] = []
    for index, offset in enumerate(sample_offsets, start=1):
        sample_path = temp_dir / f"sample_{index}.wav"
        try:
            _build_sample_clip(
                ffmpeg_path=ffmpeg_path,
                source_path=segment_path,
                sample_path=sample_path,
                start=max(0.0, offset),
                duration=sample_duration,
            )
        except RuntimeError:
            continue

        try:
            match = _recognize_sample_with_acrcloud(
                sample_path=sample_path,
                host=host,
                access_key=access_key,
                access_secret=access_secret,
                offset=round(offset, 3),
            )
        except RuntimeError:
            continue

        if match:
            matches.append(match)
    return matches


def rename_songcut_with_recognition(segment_path: Path, recognition: SongRecognition) -> Path:
    label = recognition.title
    if recognition.artist:
        label = f"{recognition.title} - {recognition.artist}"

    safe_label = normalize_storage_name(label, fallback=segment_path.stem)
    candidate = dedupe_path(segment_path.with_name(f"{safe_label}{segment_path.suffix.lower()}"))
    if candidate == segment_path:
        update_segment_metadata(
            segment_path,
            recognition_title=recognition.title,
            recognition_artist=recognition.artist,
            recognition_provider=recognition.provider,
            recognition_confidence=round(recognition.confidence, 4),
            recognition_matched_samples=recognition.matched_samples,
            recognition_sample_count=recognition.sample_count,
            recognition_average_confidence=round(recognition.average_confidence, 4),
            recognition_best_confidence=round(recognition.best_confidence, 4),
        )
        return segment_path

    segment_path.rename(candidate)
    move_segment_metadata(segment_path, candidate)
    update_segment_metadata(
        candidate,
        recognition_title=recognition.title,
        recognition_artist=recognition.artist,
        recognition_provider=recognition.provider,
        recognition_confidence=round(recognition.confidence, 4),
        recognition_matched_samples=recognition.matched_samples,
        recognition_sample_count=recognition.sample_count,
        recognition_average_confidence=round(recognition.average_confidence, 4),
        recognition_best_confidence=round(recognition.best_confidence, 4),
    )
    return candidate


def dedupe_path(path: Path) -> Path:
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


def build_sample_offsets(total_duration: float, sample_duration: float) -> list[float]:
    if total_duration <= sample_duration:
        return [0.0]

    max_offset = max(0.0, total_duration - sample_duration)
    intro_skip = max(0.0, min(parse_float_env("ACRCLOUD_SKIP_INTRO_SECONDS", 18.0), max_offset))
    candidates = [
        intro_skip,
        max(intro_skip, min(total_duration * 0.28, max_offset)),
        max(intro_skip, min(total_duration * 0.45, max_offset)),
        max(intro_skip, min(total_duration * 0.62, max_offset)),
        max(intro_skip, min(total_duration * 0.78, max_offset)),
        max(intro_skip, min(total_duration * 0.88, max_offset)),
    ]

    unique_offsets: list[float] = []
    for offset in candidates:
        rounded = round(offset, 2)
        if rounded not in unique_offsets:
            unique_offsets.append(rounded)
    return unique_offsets or [0.0]


def build_recognition_sample_offsets(
    *,
    segment_path: Path,
    total_duration: float,
    sample_duration: float,
    ffmpeg_path: str,
    temp_dir: Path,
) -> list[float]:
    base_offsets = build_sample_offsets(total_duration, sample_duration)
    if not parse_bool_env("ACRCLOUD_ENERGY_SAMPLE_ENABLED", True):
        return limit_sample_offsets(base_offsets, total_duration, sample_duration)

    energy_offsets = build_energy_sample_offsets(
        segment_path=segment_path,
        total_duration=total_duration,
        sample_duration=sample_duration,
        ffmpeg_path=ffmpeg_path,
        temp_dir=temp_dir,
    )
    return limit_sample_offsets(
        energy_offsets + base_offsets,
        total_duration,
        sample_duration,
    )


def limit_sample_offsets(
    offsets: list[float],
    total_duration: float,
    sample_duration: float,
) -> list[float]:
    if total_duration <= sample_duration:
        return [0.0]

    max_samples = max(1, min(16, parse_int_env("ACRCLOUD_MAX_SAMPLES", 8)))
    max_offset = max(0.0, total_duration - sample_duration)
    min_distance = max(1.0, parse_float_env("ACRCLOUD_SAMPLE_MIN_DISTANCE_SECONDS", sample_duration * 0.5))

    selected: list[float] = []
    for offset in offsets:
        safe_offset = round(max(0.0, min(float(offset), max_offset)), 2)
        if any(abs(safe_offset - current) < min_distance for current in selected):
            continue
        selected.append(safe_offset)
        if len(selected) >= max_samples:
            break

    return selected or [0.0]


def build_retry_sample_offsets(
    *,
    total_duration: float,
    sample_duration: float,
    used_offsets: list[float],
    grid_seconds: Optional[float] = None,
    max_samples: Optional[int] = None,
) -> list[float]:
    """Second-pass sample positions covering gaps the first pass missed."""
    if total_duration <= sample_duration:
        return []

    grid = max(5.0, grid_seconds if grid_seconds is not None else parse_float_env("ACRCLOUD_RETRY_GRID_SECONDS", 45.0))
    limit = max(1, max_samples if max_samples is not None else parse_int_env("ACRCLOUD_RETRY_MAX_SAMPLES", 6))
    min_distance = max(
        1.0,
        parse_float_env("ACRCLOUD_SAMPLE_MIN_DISTANCE_SECONDS", sample_duration * 0.5),
    )

    max_offset = max(0.0, total_duration - sample_duration)
    candidates: list[float] = []
    position = 0.0
    while position <= max_offset + 0.01:
        candidates.append(round(min(position, max_offset), 2))
        position += grid

    selected: list[float] = []
    for offset in candidates:
        if any(abs(offset - used) < min_distance for used in used_offsets):
            continue
        if any(abs(offset - current) < min_distance for current in selected):
            continue
        selected.append(offset)
        if len(selected) >= limit:
            break
    return selected


def build_energy_sample_offsets(
    *,
    segment_path: Path,
    total_duration: float,
    sample_duration: float,
    ffmpeg_path: str,
    temp_dir: Path,
) -> list[float]:
    if total_duration <= sample_duration:
        return [0.0]

    max_analysis_seconds = max(30.0, parse_float_env("ACRCLOUD_ENERGY_ANALYSIS_MAX_SECONDS", 900.0))
    if total_duration > max_analysis_seconds:
        return []

    analysis_wav = temp_dir / "analysis.wav"
    try:
        _decode_analysis_wav(ffmpeg_path, segment_path, analysis_wav)
        energies, window_seconds = _read_wav_rms(analysis_wav)
    except Exception:
        return []

    if not energies or window_seconds <= 0:
        return []

    max_offset = max(0.0, total_duration - sample_duration)
    intro_skip = max(0.0, min(parse_float_env("ACRCLOUD_SKIP_INTRO_SECONDS", 18.0), max_offset))
    hop_seconds = max(1.0, parse_float_env("ACRCLOUD_ENERGY_HOP_SECONDS", 4.0))
    windows_per_sample = max(1, round(sample_duration / window_seconds))
    min_active_ratio = min(max(parse_float_env("ACRCLOUD_ENERGY_MIN_ACTIVE_RATIO", 0.55), 0.0), 1.0)

    noise_floor = _percentile_values(energies, 0.35)
    loud_rms = _percentile_values(energies, 0.90)
    active_threshold = max(24.0, noise_floor * 1.35, noise_floor + (loud_rms - noise_floor) * 0.18)
    scored: list[tuple[float, float]] = []
    offset = intro_skip
    while offset <= max_offset + 0.01:
        start_index = max(0, round(offset / window_seconds))
        end_index = min(len(energies), start_index + windows_per_sample)
        window = energies[start_index:end_index]
        if len(window) < max(1, windows_per_sample // 2):
            offset += hop_seconds
            continue

        average = sum(window) / len(window)
        peak = max(window)
        active_ratio = sum(1 for value in window if value >= active_threshold) / len(window)
        if active_ratio < min_active_ratio:
            offset += hop_seconds
            continue

        variance = sum((value - average) ** 2 for value in window) / len(window)
        stability_penalty = math.sqrt(max(0.0, variance)) * 0.08
        center = offset + sample_duration / 2
        center_bias = 1.0 - min(0.35, abs(center - total_duration / 2) / max(total_duration, 1.0))
        score = (average * (0.75 + active_ratio * 0.25) + peak * 0.05 - stability_penalty) * center_bias
        scored.append((score, offset))
        offset += hop_seconds

    scored.sort(reverse=True)
    return [round(offset, 2) for _, offset in scored[: max(8, parse_int_env("ACRCLOUD_MAX_SAMPLES", 8) * 2)]]


def _decode_analysis_wav(ffmpeg_path: str, source_path: Path, target_path: Path) -> None:
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
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"构建 ACRCloud 能量分析样本失败: {stderr}")


def _read_wav_rms(wav_path: Path, window_seconds: float = 1.0) -> tuple[list[int], float]:
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


def _percentile_values(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int((len(ordered) - 1) * min(max(ratio, 0.0), 1.0))
    return float(ordered[index])


def choose_recognition_result(matches: list[SampleMatch]) -> Optional[SongRecognition]:
    if not matches:
        return None

    min_confidence = parse_float_env("ACRCLOUD_MIN_CONFIDENCE", 0.68)
    high_confidence = parse_float_env("ACRCLOUD_HIGH_CONFIDENCE", 0.90)
    min_confirmations = max(1, parse_int_env("ACRCLOUD_MIN_CONFIRMATIONS", 2))

    groups: dict[str, list[SampleMatch]] = {}
    for match in matches:
        groups.setdefault(normalize_recognition_key(match.title, match.artist), []).append(match)

    groups = _merge_title_only_groups(groups)

    ranked_groups = sorted(
        groups.values(),
        key=lambda group: (
            len(group),
            max(item.confidence for item in group),
            sum(item.confidence for item in group) / len(group),
        ),
        reverse=True,
    )
    best_group = ranked_groups[0]
    best_match = max(best_group, key=lambda item: item.confidence)
    avg_confidence = sum(item.confidence for item in best_group) / len(best_group)
    accepted_confidence = min(
        1.0,
        (avg_confidence * 0.65 + best_match.confidence * 0.35)
        * min(1.0, 0.82 + len(best_group) * 0.06),
    )

    if len(best_group) >= min_confirmations and avg_confidence >= min_confidence:
        return SongRecognition(
            title=best_match.title,
            artist=best_match.artist,
            confidence=accepted_confidence,
            provider="acrcloud",
            matched_samples=len(best_group),
            sample_count=len(matches),
            average_confidence=avg_confidence,
            best_confidence=best_match.confidence,
            samples=list(best_group),
            all_matches=list(matches),
        )

    if len(groups) == 1 and best_match.confidence >= high_confidence:
        return SongRecognition(
            title=best_match.title,
            artist=best_match.artist,
            confidence=best_match.confidence,
            provider="acrcloud",
            matched_samples=len(best_group),
            sample_count=len(matches),
            average_confidence=avg_confidence,
            best_confidence=best_match.confidence,
            samples=list(best_group),
            all_matches=list(matches),
        )

    return None


def _merge_title_only_groups(
    groups: dict[str, list[SampleMatch]],
) -> dict[str, list[SampleMatch]]:
    """Merge groups that share a normalized title when at most one carries an artist.

    Fingerprint services sometimes return the same track with an empty artist on
    part of the queries; without merging those fail to reach the confirmation
    threshold even though they describe the same song.
    """
    by_title: dict[str, list[str]] = {}
    for key in list(groups):
        title_part = key.split("|", 1)[0]
        by_title.setdefault(title_part, []).append(key)

    for title_part, keys in by_title.items():
        if len(keys) <= 1:
            continue

        with_artist = [key for key in keys if key.split("|", 1)[1]]
        if len(with_artist) > 1:
            continue

        target = with_artist[0] if with_artist else keys[0]
        for key in keys:
            if key != target:
                groups[target].extend(groups.pop(key))
    return groups


def normalize_recognition_key(title: str, artist: str = "") -> str:
    def clean(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        value = re.sub(r"[(\[（【<「『].*?[)\]）】>」』]", " ", value)
        value = re.sub(
            r"\b(live|remaster(?:ed)?(?:\s*\d{4})?|acoustic|instrumental|cover|version|ver)\b",
            " ",
            value,
        )
        value = re.sub(r"[^0-9a-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]+", "", value)
        return value

    return f"{clean(title)}|{clean(artist)}"


def _build_sample_clip(
    ffmpeg_path: str,
    source_path: Path,
    sample_path: Path,
    start: float,
    duration: float,
) -> None:
    command = [
        ffmpeg_path,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
    ]
    sample_filter = os.getenv("ACRCLOUD_SAMPLE_FILTER", "").strip()
    if sample_filter:
        command.extend(["-af", sample_filter])
    command.extend(["-c:a", "pcm_s16le", str(sample_path)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"构建 ACRCloud 识别样本失败: {stderr}")


def _recognize_sample_with_acrcloud(
    sample_path: Path,
    host: str,
    access_key: str,
    access_secret: str,
    offset: float = 0.0,
) -> Optional[SampleMatch]:
    parsed = urlparse(host if "://" in host else f"https://{host}")
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    endpoint = f"{scheme}://{netloc}/v1/identify"

    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    timestamp = str(int(time.time()))
    string_to_sign = "\n".join(
        [http_method, http_uri, access_key, data_type, signature_version, timestamp]
    )
    signature = base64.b64encode(
        hmac.new(
            access_secret.encode("ascii"),
            string_to_sign.encode("ascii"),
            digestmod=hashlib.sha1,
        ).digest()
    ).decode("ascii")

    sample_bytes = sample_path.read_bytes()
    boundary = f"----acr-{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in {
        "access_key": access_key,
        "sample_bytes": str(len(sample_bytes)),
        "timestamp": timestamp,
        "signature": signature,
        "data_type": data_type,
        "signature_version": signature_version,
    }.items():
        body.extend(_multipart_text(boundary, name, value))
    body.extend(_multipart_file(boundary, "sample", sample_path.name, _guess_audio_content_type(sample_path), sample_bytes))
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    request = Request(
        url=endpoint,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=bytes(body),
    )
    response = _read_json_response(request)
    status = response.get("status") or {}
    if status.get("code") != 0:
        return None

    metadata = response.get("metadata") or {}
    musics = metadata.get("music") or []
    if not musics:
        return None

    first = musics[0]
    title = str(first.get("title", "")).strip()
    if not title:
        return None

    artist = ""
    artists = first.get("artists") or []
    if artists and isinstance(artists[0], dict):
        artist = str(artists[0].get("name", "")).strip()

    confidence = 1.0
    score = first.get("score")
    if isinstance(score, (int, float)):
        confidence = min(max(float(score) / 100.0, 0.0), 1.0)

    play_offset_ms = _safe_int(first.get("play_offset_ms"))
    duration_ms = _safe_int(first.get("duration_ms"))

    return SampleMatch(
        offset=offset,
        title=title,
        artist=artist,
        confidence=confidence,
        play_offset_ms=play_offset_ms,
        duration_ms=duration_ms,
    )


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def compute_aligned_boundaries(
    current_start: float,
    current_end: float,
    recognition: SongRecognition,
    *,
    source_total_duration: Optional[float] = None,
) -> Optional[BoundaryAlignment]:
    """Recover the full intro/outro of a recognized song from fingerprint offsets.

    Every confirmed sample tells us where inside the original track it matched
    (play_offset_ms) and how long that track is (duration_ms), so the segment can
    be re-cut to the complete song boundaries: implied song start =
    sample offset - play offset, implied song end = implied start + track length.
    A robust median over the confirmed samples keeps this stable, and caps limit
    how far boundaries may move so a bad fingerprint match cannot gut a segment.
    """
    if not parse_bool_env("SONGCUT_BOUNDARY_ALIGNMENT_ENABLED", True):
        return None
    if current_end <= current_start:
        return None

    min_track = max(30.0, parse_float_env("SONGCUT_ALIGNMENT_MIN_TRACK_DURATION_SECONDS", 90.0))
    max_track = parse_float_env("SONGCUT_ALIGNMENT_MAX_TRACK_DURATION_SECONDS", 1200.0)
    usable = [
        sample
        for sample in recognition.samples
        if min_track <= sample.duration_ms / 1000.0 <= max_track and sample.play_offset_ms >= 0
    ]
    if len(usable) < 2:
        return None

    # sample.offset is relative to the exported cut; anchor it to the recording
    # timeline before comparing with the current boundaries.
    implied = [
        (
            current_start + sample.offset - sample.play_offset_ms / 1000.0,
            current_start + sample.offset - sample.play_offset_ms / 1000.0 + sample.duration_ms / 1000.0,
        )
        for sample in usable
    ]
    start_median = _median_of([start for start, _ in implied])
    end_median = _median_of([end for _, end in implied])

    # A cut far longer than the recognized track is a multi-song block: the
    # median boundaries would be clamped by caps into a semi-arbitrary trim.
    # Medley splitting (split_medley_intervals) owns that case instead.
    track_median = _median_of([sample.duration_ms / 1000.0 for sample in usable])
    if (current_end - current_start) > track_median * 1.5 + 60.0:
        return None

    tolerance = max(4.0, parse_float_env("SONGCUT_ALIGNMENT_TOLERANCE_SECONDS", 10.0))
    agreeing = sum(
        1 for start, end in implied if abs(start - start_median) <= tolerance and abs(end - end_median) <= tolerance
    )
    if agreeing < 2 or agreeing * 2 < len(implied):
        return None

    min_shift = max(0.5, parse_float_env("SONGCUT_ALIGNMENT_MIN_SHIFT_SECONDS", 2.0))
    max_extend_start = max(0.0, parse_float_env("SONGCUT_ALIGNMENT_MAX_EXTEND_START_SECONDS", 90.0))
    max_extend_end = max(0.0, parse_float_env("SONGCUT_ALIGNMENT_MAX_EXTEND_END_SECONDS", 30.0))
    # Live covers routinely run longer than the studio track (slower tempo,
    # ad-libs, chat between verses), so never trim the end aggressively: the
    # studio duration can only justify a small trim, otherwise the outro goes.
    max_shrink_start = max(0.0, parse_float_env("SONGCUT_ALIGNMENT_MAX_SHRINK_SECONDS", 30.0))
    max_shrink_end = max(0.0, parse_float_env("SONGCUT_ALIGNMENT_MAX_SHRINK_END_SECONDS", 10.0))

    upper_bound = source_total_duration if source_total_duration and source_total_duration > 0 else None
    new_start = max(0.0, min(max(start_median, current_start - max_extend_start), current_start + max_shrink_start))
    new_end = min(max(end_median, current_end - max_shrink_end), current_end + max_extend_end)
    if upper_bound is not None:
        new_end = min(new_end, upper_bound)

    if new_end - new_start < 30.0:
        return None

    shift_start = new_start - current_start
    shift_end = new_end - current_end
    if abs(shift_start) < min_shift and abs(shift_end) < min_shift:
        return None

    return BoundaryAlignment(
        start=round(new_start, 3),
        end=round(new_end, 3),
        shift_start=round(shift_start, 3),
        shift_end=round(shift_end, 3),
        track_duration=round(usable[0].duration_ms / 1000.0, 3),
        matched_samples=len(usable),
    )


def _median_of(values: list[float]) -> float:
    ordered = sorted(values)
    size = len(ordered)
    if size == 0:
        return 0.0
    middle = size // 2
    if size % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def split_medley_intervals(
    current_start: float,
    current_end: float,
    matches: list[SampleMatch],
    *,
    source_total_duration: Optional[float] = None,
) -> list[SongInterval]:
    """Locate each fingerprint-confirmed song inside a (possibly long) cut.

    Every confirmed sample implies where its track begins and ends on the
    recording timeline. Samples of the same song whose implied intervals agree
    form one SongInterval, so a long block holding several songs breaks into
    per-song intervals while unmatched chatter stays outside all of them.
    """
    if not matches or current_end <= current_start:
        return []

    min_track = max(30.0, parse_float_env("SONGCUT_ALIGNMENT_MIN_TRACK_DURATION_SECONDS", 90.0))
    max_track = parse_float_env("SONGCUT_ALIGNMENT_MAX_TRACK_DURATION_SECONDS", 1200.0)
    min_confirmations = max(2, parse_int_env("ACRCLOUD_MIN_CONFIRMATIONS", 2))
    tolerance = max(4.0, parse_float_env("SONGCUT_ALIGNMENT_TOLERANCE_SECONDS", 10.0))

    upper = source_total_duration if source_total_duration and source_total_duration > 0 else current_end + 600.0

    groups: dict[str, list[SampleMatch]] = {}
    for match in matches:
        if not (min_track <= match.duration_ms / 1000.0 <= max_track):
            continue
        groups.setdefault(normalize_recognition_key(match.title, match.artist), []).append(match)
    groups = _merge_title_only_groups(groups)

    intervals: list[SongInterval] = []
    for group in groups.values():
        if len(group) < min_confirmations:
            continue

        implied = sorted(
            (
                (
                    current_start + match.offset - match.play_offset_ms / 1000.0,
                    current_start + match.offset - match.play_offset_ms / 1000.0 + match.duration_ms / 1000.0,
                    match,
                )
                for match in group
            ),
            key=lambda item: (item[0], item[1]),
        )
        # Cluster samples whose implied intervals materially overlap.
        clusters: list[list[tuple[float, float, SampleMatch]]] = [[implied[0]]]
        for start, end, match in implied[1:]:
            cluster_end = max(item[1] for item in clusters[-1])
            if start <= cluster_end - tolerance:
                clusters[-1].append((start, end, match))
            else:
                clusters.append([(start, end, match)])

        for cluster in clusters:
            if len(cluster) < min_confirmations:
                continue
            starts = sorted(item[0] for item in cluster)
            ends = sorted(item[1] for item in cluster)
            interval_start = _median_of(starts)
            interval_end = _median_of(ends)
            if interval_end - interval_start < max_track_lower_bound(min_track):
                continue
            best = max((item[2] for item in cluster), key=lambda item: item.confidence)
            intervals.append(
                SongInterval(
                    start=round(max(0.0, interval_start), 3),
                    end=round(min(upper, interval_end), 3),
                    title=best.title,
                    artist=best.artist,
                    confidence=round(sum(item[2].confidence for item in cluster) / len(cluster), 4),
                    matched_samples=len(cluster),
                    samples=[item[2] for item in cluster],
                )
            )

    # Resolve overlaps between different songs: keep the better-supported one.
    resolved: list[SongInterval] = []
    for interval in sorted(intervals, key=lambda item: (item.start, item.end)):
        if resolved and interval.start < resolved[-1].end - tolerance:
            previous = resolved[-1]
            keep_new = (interval.matched_samples, interval.confidence) > (
                previous.matched_samples,
                previous.confidence,
            )
            if keep_new:
                resolved[-1] = interval
            continue
        resolved.append(interval)
    return resolved


def max_track_lower_bound(min_track: float) -> float:
    """Shortest interval we accept as a complete song (not a stray match)."""
    return max(45.0, min_track * 0.6)


def looks_like_singing(activity: Any) -> bool:
    """Distinguish a sung performance from background music under chat.

    Fingerprinting happily matches the BGM track playing while the streamer
    chats. A sung cut carries long stretches of sustained loud output (held
    notes, continuous accompaniment), while chat-over-BGM breaks the loud runs
    into syllable-sized pieces with high energy variance. Measured on real
    stream data: sung cuts sustain >=14s active runs (CV ~0.5); BGM under chat
    peaks at ~5-6s runs with CV >= 0.65.
    """
    min_run = max(1.0, parse_float_env("SONGCUT_MIN_SUSTAINED_ACTIVE_SECONDS", 10.0))
    max_cv = parse_float_env("SONGCUT_MAX_ACTIVITY_CV", 0.62)
    min_active = min(1.0, max(0.0, parse_float_env("SONGCUT_MIN_ACTIVITY_RATIO", 0.35)))

    if activity.longest_active_run_seconds >= min_run:
        return True
    return activity.cv <= max_cv and activity.active_ratio >= min_active


# ---------------------------------------------------------------------------
# Lyric-based recognition: fallback for karaoke covers that fingerprinting
# cannot identify (the sung audio never matches the studio recording).
# ---------------------------------------------------------------------------

LYRIC_PROVIDER = "lyrics"

_WHISPER_MODEL: Any = None
_WHISPER_DEVICE_RESOLVED: str = ""


@dataclass
class LyricCandidate:
    title: str
    artist: str
    lyrics: str


def recognize_song_title_by_lyrics(
    segment_path: Path,
    duration: float,
    ffmpeg_path: Optional[str] = None,
) -> Optional[SongRecognition]:
    """Identify a song by transcribing the vocals and searching lyric databases.

    Used after fingerprint recognition fails: karaoke covers change the vocal
    and mix, so ACRCloud has nothing to match, but the lyrics survive. Windows
    of the cut are transcribed with Whisper, searched against lrclib.net, and a
    candidate is accepted only when the transcript is largely contained in its
    lyrics.
    """
    if not parse_bool_env("SONGCUT_LYRIC_RECOGNITION_ENABLED", True):
        return None

    resolved_ffmpeg = find_ffmpeg_binary(ffmpeg_path)
    if not resolved_ffmpeg:
        return None

    cache_path = get_recognition_cache_path()
    cache_key = build_recognition_cache_key(segment_path)
    cached = lookup_cached_recognition(cache_path, cache_key)
    if cached:
        return cached

    transcripts = collect_lyric_transcripts(
        segment_path=segment_path,
        duration=duration,
        ffmpeg_path=resolved_ffmpeg,
    )
    usable = [text for text in transcripts if text and has_enough_lyric_content(text)]
    if not usable:
        return None

    matched = find_lyric_match(usable)
    if matched is None:
        return None

    title, artist, score, windows = matched
    min_match = parse_float_env("SONGCUT_LYRIC_MIN_MATCH", 0.55)
    confidence = min(
        0.98,
        max(0.68, 0.68 + 0.30 * (score - min_match) / max(0.01, 1.0 - min_match)),
    )
    recognition = SongRecognition(
        title=title,
        artist=artist,
        confidence=round(confidence, 4),
        provider=LYRIC_PROVIDER,
        matched_samples=windows,
        sample_count=len(usable),
    )
    store_cached_recognition(cache_path, cache_key, segment_path, recognition)
    return recognition


def collect_lyric_transcripts(
    *,
    segment_path: Path,
    duration: float,
    ffmpeg_path: str,
) -> list[str]:
    max_windows = max(1, parse_int_env("SONGCUT_LYRIC_MAX_WINDOWS", 4))
    window = max(20.0, parse_float_env("SONGCUT_LYRIC_WINDOW_SECONDS", 45.0))
    if duration <= 0:
        return []

    if duration <= window + 15.0:
        positions = [max(0.0, duration * 0.15)]
    else:
        positions = []
        for fraction in (0.15, 0.40, 0.65, 0.85)[:max_windows]:
            position = duration * fraction
            if position + window <= duration:
                positions.append(position)
        if not positions:
            positions = [max(0.0, duration - window)]

    temp_dir = Path(mkdtemp(prefix="lyric-sample-", dir=str(segment_path.parent)))
    transcripts: list[str] = []
    try:
        for index, position in enumerate(positions[:max_windows], start=1):
            sample_path = temp_dir / f"lyric_{index}.wav"
            try:
                _build_sample_clip(
                    ffmpeg_path=ffmpeg_path,
                    source_path=segment_path,
                    sample_path=sample_path,
                    start=position,
                    duration=min(window, duration - position),
                )
            except RuntimeError:
                continue
            text = transcribe_with_whisper(sample_path)
            if text:
                transcripts.append(text)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    unique: list[str] = []
    for text in transcripts:
        if text not in unique:
            unique.append(text)
    return unique


def transcribe_with_whisper(sample_path: Path) -> Optional[str]:
    """Transcribe one window; falls back to no-VAD when VAD eats the vocals.

    The Silero VAD shipped with faster-whisper is trained on speech, so melodic
    or melismatic singing is routinely classified as non-speech and dropped
    (observed: 40.6s removed from a 45s window of real singing). When the VAD
    pass leaves too little text, retry the same window without the filter -
    the strict lyric containment check downstream rejects any hallucinations
    the unfiltered pass may produce.
    """
    model = _get_whisper_model()
    if model is None:
        return None

    def run(vad_filter: bool) -> Optional[str]:
        try:
            segments, _info = model.transcribe(
                str(sample_path),
                beam_size=5,
                condition_on_previous_text=False,
                vad_filter=vad_filter,
            )
            return "".join(segment.text for segment in segments).strip() or None
        except Exception:
            return None

    text = run(vad_filter=True)
    if text and has_enough_lyric_content(text):
        return text

    relaxed = run(vad_filter=False)
    # Prefer whichever pass produced more usable text.
    if relaxed and (not text or len(normalize_lyric_text(relaxed)) > len(normalize_lyric_text(text))):
        return relaxed
    return text


def _get_whisper_model() -> Any:
    """Load the Whisper model once; None means unavailable (skip silently)."""
    global _WHISPER_MODEL, _WHISPER_DEVICE_RESOLVED
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL or None

    _configure_cuda_runtime()
    try:
        from faster_whisper import WhisperModel
    except Exception:
        _WHISPER_MODEL = False
        return None

    name = (os.getenv("SONGCUT_WHISPER_MODEL", "medium").strip() or "small")
    device = (os.getenv("SONGCUT_WHISPER_DEVICE", "auto").strip().lower() or "auto")
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute_type = os.getenv("SONGCUT_WHISPER_COMPUTE_TYPE", "").strip()

    if device == "cuda":
        model = _load_whisper(WhisperModel, name, "cuda", compute_type or "float16")
        if model is not None and _cuda_model_self_check(model):
            _WHISPER_MODEL = model
            _WHISPER_DEVICE_RESOLVED = "cuda"
            print(
                f"[songcuts] whisper ready: model={name}, device=cuda, "
                f"compute_type={compute_type or 'float16'}",
                flush=True,
            )
            return model
        # Broken CUDA runtime (e.g. missing cuBLAS): inference there hangs or
        # crashes instead of raising at load time, so fall back to CPU.
        print("[songcuts] whisper CUDA unusable, falling back to cpu", flush=True)
        device = "cpu"

    _WHISPER_MODEL = _load_whisper(WhisperModel, name, "cpu", compute_type or "int8")
    _WHISPER_DEVICE_RESOLVED = "cpu" if _WHISPER_MODEL is not None else ""
    if _WHISPER_MODEL is not None:
        print(
            f"[songcuts] whisper ready: model={name}, device=cpu, "
            f"compute_type={compute_type or 'int8'}",
            flush=True,
        )
    return _WHISPER_MODEL


def _load_whisper(whisper_cls: Any, name: str, device: str, compute_type: str) -> Any:
    try:
        return whisper_cls(name, device=device, compute_type=compute_type)
    except Exception as exc:
        print(f"[songcuts] whisper model load failed ({name}/{device}): {exc}", flush=True)
        return None


def _cuda_model_self_check(model: Any, timeout_seconds: float = 60.0) -> bool:
    """Run a tiny real inference to prove the CUDA runtime actually works.

    A machine can expose a CUDA device yet lack cuBLAS/cuDNN libraries; loading
    succeeds there but the first inference deadlocks forever. Probe with a
    snippet of silence under a thread timeout so we can fall back safely.
    """
    import threading

    try:
        with TemporaryDirectory(prefix="whisper-probe-") as _tmp:
            probe_path = Path(_tmp) / "probe.wav"
            with wave.open(str(probe_path), "wb") as probe_wav:
                probe_wav.setnchannels(1)
                probe_wav.setsampwidth(2)
                probe_wav.setframerate(16000)
                probe_wav.writeframes(bytes(9600))  # 0.3s silence

            result: dict[str, bool] = {}

            def run() -> None:
                try:
                    segments, _info = model.transcribe(str(probe_path), beam_size=1)
                    for _ in segments:
                        pass
                    result["ok"] = True
                except Exception:
                    result["ok"] = False

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout_seconds)
            return bool(result.get("ok"))
    except Exception:
        return False


def find_lyric_match(transcripts: list[str]) -> Optional[tuple[str, str, float, int]]:
    """Search lyrics for each transcript and return the best (title, artist, score, windows)."""
    min_match = parse_float_env("SONGCUT_LYRIC_MIN_MATCH", 0.55)
    max_queries = max(1, parse_int_env("SONGCUT_LYRIC_MAX_QUERIES", 3))
    best: dict[str, dict[str, Any]] = {}

    for text in transcripts:
        window_best: dict[str, float] = {}
        for query in build_lyric_queries(text)[:max_queries]:
            for candidate in search_lyric_candidates(query)[:12]:
                # The fragment only decides what to look up; acceptance is
                # scored against the whole transcript for strictness.
                score = score_lyric_match(text, candidate.lyrics)
                if score <= 0:
                    continue
                key = normalize_recognition_key(candidate.title, candidate.artist)
                if score > window_best.get(key, 0.0):
                    window_best[key] = score
                    entry = best.setdefault(
                        key,
                        {"title": candidate.title, "artist": candidate.artist, "score": 0.0, "windows": 0},
                    )
                    entry["title"] = candidate.title
                    entry["artist"] = candidate.artist

        for key, score in window_best.items():
            entry = best.get(key)
            if entry is None:
                continue
            if score > entry["score"]:
                entry["score"] = score
            if score >= min_match:
                entry["windows"] += 1

    qualified = [entry for entry in best.values() if entry["score"] >= min_match and entry["windows"] >= 1]
    if not qualified:
        return None

    ranked = sorted(qualified, key=lambda e: (e["windows"], e["score"]), reverse=True)
    winner = ranked[0]
    final_score = min(0.99, winner["score"] + 0.05 * (winner["windows"] - 1))

    title = clean_display_title(winner["title"])
    artist = clean_display_artist(winner["artist"])
    popular = find_popular_artist_for_title(title)
    if popular:
        artist = clean_display_artist(popular)
    return title, artist, final_score, winner["windows"]


def clean_display_title(name: str) -> str:
    """Drop trailing parenthetical suffixes like 发如雪（R&B） -> 发如雪."""
    cleaned = re.sub(r"[（(][^（）()]*[)）]\s*$", "", name.strip())
    return cleaned.strip() or name.strip()


def clean_display_artist(name: str) -> str:
    """Trim decoration like trailing dashes some NetEase entries carry."""
    cleaned = name.strip().rstrip("-－—_").strip()
    return cleaned or name.strip()


def clean_transcript_for_search(transcript: str) -> str:
    """Strip Whisper hallucination tags (e.g. ["Piano Concerto..."], 【字幕by...】)."""
    text = re.sub(r"\[[^\]]*\]", " ", transcript)
    text = re.sub(r"【[^】]*】", " ", text)
    return text.strip()


def build_lyric_queries(transcript: str) -> list[str]:
    """Split a transcript into short, lookup-friendly fragments.

    Lyric search engines match individual tokens, so a long ASR transcript full
    of misheard words retrieves nothing. Short phrases (8-20 chars for CJK,
    6-10 words for latin) have a much better chance of containing one correctly
    transcribed run; the strict containment scoring later guards the result.
    """
    text = clean_transcript_for_search(unicodedata.normalize("NFKC", transcript)).strip()
    if not text:
        return []

    pieces = [piece.strip() for piece in re.split(r"[，。！？、,.!?\n\r\t]+| {2,}", text) if piece.strip()]
    if not pieces:
        pieces = [text]

    queries: list[str] = []
    if cjk_char_ratio(text) >= 0.3:
        # Only CJK-dominant pieces: instrumental windows make Whisper
        # hallucinate Latin/classical-music titles that pollute retrieval.
        cjk_pieces = [piece for piece in pieces if cjk_char_ratio(piece) >= 0.4]

        # Prefer mid-length CJK pieces: long ones carry more misheard chars,
        # tiny ones match too many songs.
        def piece_rank(piece: str) -> tuple[int, int]:
            length = len(piece.replace(" ", ""))
            distance = abs(length - 14)
            return (distance, -length)

        for piece in sorted(cjk_pieces, key=piece_rank):
            compact = piece.replace(" ", "")
            if 8 <= len(compact) <= 30 and piece not in queries:
                queries.append(piece)
        # Fallback: sliding windows over the longest CJK piece.
        if not queries and cjk_pieces:
            longest = max(cjk_pieces, key=lambda p: len(p.replace(" ", ""))).replace(" ", "")
            for start in range(0, max(1, len(longest) - 8), 6):
                chunk = longest[start : start + 14]
                if len(chunk) >= 8:
                    queries.append(chunk)
    else:
        words = text.split()
        for size in (10, 7):
            for start in range(0, max(1, len(words) - size + 1), size):
                chunk = " ".join(words[start : start + size])
                if chunk not in queries:
                    queries.append(chunk)

    return queries or [text]


def find_popular_artist_for_title(title: str) -> Optional[str]:
    """Lyric searches mostly return cover versions; resolve the original artist.

    A song-name search on NetEase is relevance/popularity ranked, so the first
    exact-name hit is almost always the original recording.
    """
    if not title:
        return None
    try:
        songs = _netease_search(title, stype=1)
    except Exception:
        return None
    target = normalize_lyric_text(title).replace(" ", "")
    for song in songs[:5]:
        if normalize_lyric_text(song.get("name", "")).replace(" ", "") == target:
            artist = _netease_first_artist(song)
            if artist:
                return artist
    return None


def search_lyric_candidates(query: str) -> list[LyricCandidate]:
    """Gather (title, artist, lyrics) candidates for a transcript fragment.

    Primary source is NetEase Cloud Music's lyric-text search (works even with
    partially misheard words); lrclib keyword search is kept as a fallback.
    """
    candidates: list[LyricCandidate] = []
    seen_ids: set[int] = set()

    try:
        for song in _netease_search(query, stype=1006)[:6]:
            song_id = int(song.get("id", 0) or 0)
            if not song_id or song_id in seen_ids:
                continue
            seen_ids.add(song_id)
            title = str(song.get("name", "")).strip()
            if not title:
                continue
            lyrics = fetch_netease_lyrics(song_id)
            if lyrics:
                candidates.append(
                    LyricCandidate(
                        title=title,
                        artist=_netease_first_artist(song),
                        lyrics=lyrics,
                    )
                )
    except Exception:
        pass

    candidates.extend(search_lrclib(query)[:4])
    return candidates


def _netease_first_artist(song: dict) -> str:
    artists = song.get("artists") or []
    if artists and isinstance(artists[0], dict):
        return str(artists[0].get("name", "")).strip()
    return ""
def _netease_search(query: str, *, stype: int) -> list[dict]:
    base = (os.getenv("SONGCUT_NETEASE_API_BASE", "https://music.163.com").strip() or "https://music.163.com").rstrip("/")
    url = f"{base}/api/search/get?" + urlencode(
        {"s": query[:120], "type": stype, "offset": 0, "limit": 8, "total": "true"}
    )
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Referer": "https://music.163.com/",
        },
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    songs = (payload.get("result") or {}).get("songs") or []
    return [song for song in songs if isinstance(song, dict)]


def fetch_netease_lyrics(song_id: int) -> str:
    base = (os.getenv("SONGCUT_NETEASE_API_BASE", "https://music.163.com").strip() or "https://music.163.com").rstrip("/")
    url = f"{base}/api/song/lyric?id={int(song_id)}&lv=1&kv=1&tv=-1"
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Referer": "https://music.163.com/",
        },
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    lrc = (payload.get("lrc") or {}).get("lyric", "") or ""
    return re.sub(r"\[[^\]]*\]", " ", lrc).strip()


def search_lrclib(query: str) -> list[LyricCandidate]:
    base = (os.getenv("SONGCUT_LRCLIB_BASE_URL", "https://lrclib.net").strip() or "https://lrclib.net").rstrip("/")
    url = f"{base}/api/search?q={quote(query[:200])}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "songcuts-lyric/1.0"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError):
        return []

    candidates: list[LyricCandidate] = []
    if not isinstance(payload, list):
        return candidates
    for item in payload:
        if not isinstance(item, dict):
            continue
        title = str(item.get("trackName", "")).strip()
        lyrics = str(item.get("plainLyrics", "") or "").strip()
        if not title or not lyrics:
            continue
        candidates.append(
            LyricCandidate(
                title=title,
                artist=str(item.get("artistName", "")).strip(),
                lyrics=lyrics,
            )
        )
    return candidates


def normalize_lyric_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", " ", text)
    return " ".join(text.split())


def has_enough_lyric_content(text: str) -> bool:
    normalized = normalize_lyric_text(text)
    if not normalized:
        return False
    words = normalized.split()
    cjk_chars = sum(1 for char in normalized if is_cjk_char(char))
    return len(words) >= 8 or cjk_chars >= 12


def is_cjk_char(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
    )


def cjk_char_ratio(text: str) -> float:
    letters = [char for char in text if not char.isspace()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if is_cjk_char(char)) / len(letters)


def score_lyric_match(transcript: str, lyrics: str) -> float:
    """How much of the transcript is contained in the lyrics (0..1)."""
    t = normalize_lyric_text(transcript)
    l = normalize_lyric_text(lyrics)
    if not t or not l:
        return 0.0

    if cjk_char_ratio(t) >= 0.3:
        return cjk_lyric_containment(t, l)

    words = t.split()
    if not words:
        return 0.0
    hits = sum(1 for word in words if word in l)
    return hits / len(words)


def cjk_lyric_containment(transcript: str, lyrics: str) -> float:
    """Fraction of transcript characters found (in order) inside the lyrics.

    Matching blocks tolerate the small insertions/deletions Whisper makes on
    sung vocals, unlike a strict substring or whole-window similarity check.
    """
    t = transcript.replace(" ", "")
    l = lyrics.replace(" ", "")
    if not t or not l:
        return 0.0

    matcher = difflib.SequenceMatcher(None, t, l, autojunk=False)
    covered = sum(block.size for block in matcher.get_matching_blocks())
    return covered / len(t)


def _guess_audio_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".flac":
        return "audio/flac"
    if suffix in {".m4a", ".aac"}:
        return "audio/aac"
    return "audio/mpeg"


def _multipart_text(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def _multipart_file(boundary: str, name: str, filename: str, content_type: str, data: bytes) -> bytes:
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + data + b"\r\n"


def _read_json_response(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=60) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"ACRCloud 请求失败: HTTP {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ACRCloud 请求失败: {exc.reason}") from exc

    return json.loads(body)
