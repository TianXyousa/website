from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Security,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from brec_integration import (
    BrecConfig,
    BrecIntegrationError,
    build_webhook_url,
    fetch_api_json,
    load_brec_config,
    resolve_recording_path,
    save_brec_config,
    scan_recordings,
    summarize_rooms,
)
from bili_upload_integration import (
    BiliUploadConfig,
    BiliUploadError,
    DEFAULT_DESC_TEMPLATE,
    DEFAULT_TAGS,
    DEFAULT_TID,
    DEFAULT_TITLE_TEMPLATE,
    describe_bili_upload,
    load_bili_upload_config,
    resolve_songcut_path,
    save_bili_upload_config,
    save_uploaded_cookie,
    upload_songcut_video,
)
from gpu_songcut_extractor import describe_gpu_model_backend
from songcut_automation import (
    SongRecognition,
    cleanup_processed_recordings,
    compute_aligned_boundaries,
    dedupe_path,
    describe_recognition_provider,
    describe_recording_cleanup,
    looks_like_singing,
    parse_bool_env,
    recognize_song_title,
    recognize_song_title_by_lyrics,
    register_processed_recording,
    rename_songcut_with_recognition,
    split_medley_intervals,
)
from songcut_extractor import (
    ExtractionOptions,
    Segment,
    SongcutExtractionError,
    _format_time_for_filename,
    build_recording_date_label,
    export_segment,
    extract_songcuts_from_source,
    find_ffmpeg_binary,
    measure_region_activity,
    metadata_path_for_segment,
    normalize_storage_name,
    read_segment_metadata,
    update_segment_metadata,
    write_segment_metadata,
)

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
AUDIO_DIR = ASSETS_DIR / "audio"
SONGCUT_DIR = ASSETS_DIR / "songcuts"
STATIC_DIR = BASE_DIR / "static"
ADMIN_VIEWS_DIR = BASE_DIR / "admin_views"
PRIVATE_VIEWS_DIR = BASE_DIR / "private_views"
BREC_CONFIG_PATH = Path(os.getenv("BREC_CONFIG_PATH", str(BASE_DIR / ".brec_integration.json")))
PROCESSED_RECORDINGS_PATH = Path(
    os.getenv("PROCESSED_RECORDINGS_PATH", str(BASE_DIR / ".processed_recordings.json"))
)
BILI_UPLOAD_CONFIG_PATH = Path(
    os.getenv("BILI_UPLOAD_CONFIG_PATH", str(BASE_DIR / ".bili_upload_config.json"))
)
BILI_UPLOAD_COOKIE_DIR = Path(
    os.getenv("BILI_UPLOAD_COOKIE_DIR", str(BASE_DIR / "data" / "app" / "bili_cookies"))
)
BILI_UPLOAD_TEMP_DIR = Path(
    os.getenv("BILI_UPLOAD_TEMP_DIR", str(BASE_DIR / "data" / "app" / "bili_upload_temp"))
)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
UPLOAD_PASSWORD = os.getenv("UPLOAD_PASSWORD", "")
ADMIN_COOKIE_NAME = "songcuts_admin_session"
ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 24 * 30

SUPPORTED_AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")
SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_AUDIO_EXTENSIONS + (
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".ts",
    ".m4v",
    ".flv",
)

CATEGORIES = [
    "打招呼",
    "撒娇",
    "怪叫",
    "怪话",
    "认同",
    "道歉",
    "疑问",
    "感谢",
    "高兴",
    "遗憾",
    "笨蛋",
    "生气",
    "阴阳怪气",
    "傻笑",
    "别!",
]

logger = logging.getLogger("uvicorn.error")
recent_brec_event_ids: deque[str] = deque(maxlen=200)


def emit_runtime(message: str) -> None:
    print(f"[songcuts] {message}", flush=True)
    logger.info(message)

app = FastAPI()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class BrecConfigPayload(BaseModel):
    api_base_url: str = "http://127.0.0.1:2356"
    workdir: str = ""
    api_username: str = ""
    api_password: str = ""
    webhook_secret: str = ""
    auto_extract: bool = False
    auto_category: str = "录播姬自动提取"
    ffmpeg_path: str = ""
    extraction_mode: str = "classic"
    output_format: str = "mp3"
    min_duration: float = 60.0
    max_silence: float = 6.0
    leading_padding: float = 6.0
    trailing_padding: float = 2.5
    min_active_ratio: float = 0.45


class BrecImportPayload(BaseModel):
    relative_path: str
    category: Optional[str] = None
    extraction_mode: Optional[str] = None
    min_duration: Optional[float] = None
    max_silence: Optional[float] = None
    leading_padding: Optional[float] = None
    trailing_padding: Optional[float] = None
    min_active_ratio: Optional[float] = None
    output_format: Optional[str] = None
    ffmpeg_path: Optional[str] = None


class BiliUploadConfigPayload(BaseModel):
    cookie_file: str = ""
    uploader_path: str = "biliup"
    tid: int = DEFAULT_TID
    title_template: str = DEFAULT_TITLE_TEMPLATE
    desc_template: str = DEFAULT_DESC_TEMPLATE
    dynamic_template: str = ""
    tags: str = DEFAULT_TAGS
    copyright: int = 2
    source: str = ""
    cover_image: str = ""


class BiliUploadSongcutPayload(BaseModel):
    path: str


def ensure_directories() -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SONGCUT_DIR.mkdir(parents=True, exist_ok=True)
    BILI_UPLOAD_COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    BILI_UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for category in CATEGORIES:
        (AUDIO_DIR / category).mkdir(parents=True, exist_ok=True)


def current_brec_config() -> BrecConfig:
    config = load_brec_config(BREC_CONFIG_PATH)
    if BREC_CONFIG_PATH.exists():
        return config

    return BrecConfig(
        api_base_url=os.getenv("BREC_DEFAULT_API_BASE_URL", config.api_base_url),
        workdir=os.getenv("BREC_DEFAULT_WORKDIR", config.workdir),
        api_username=os.getenv("BREC_DEFAULT_API_USERNAME", config.api_username),
        api_password=os.getenv("BREC_DEFAULT_API_PASSWORD", config.api_password),
        auto_category=os.getenv("BREC_DEFAULT_AUTO_CATEGORY", config.auto_category),
        ffmpeg_path=os.getenv("BREC_DEFAULT_FFMPEG_PATH", config.ffmpeg_path),
        output_format=os.getenv("BREC_DEFAULT_OUTPUT_FORMAT", config.output_format),
    )


def current_bili_upload_config() -> BiliUploadConfig:
    return load_bili_upload_config(BILI_UPLOAD_CONFIG_PATH)


def verify_password(api_key: str = Security(api_key_header)) -> str:
    if api_key != UPLOAD_PASSWORD:
        raise HTTPException(status_code=401, detail="无效密码")
    return api_key


def build_admin_session_value(expires_at: int) -> str:
    message = f"admin:{expires_at}"
    digest = hmac.new(
        UPLOAD_PASSWORD.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{expires_at}.{digest}"


def has_valid_admin_session(request: Request) -> bool:
    raw_value = request.cookies.get(ADMIN_COOKIE_NAME, "")
    if not raw_value or "." not in raw_value:
        return False

    expires_text, _ = raw_value.split(".", 1)
    try:
        expires_at = int(expires_text)
    except ValueError:
        return False

    if expires_at < int(time.time()):
        return False

    expected_value = build_admin_session_value(expires_at)
    return hmac.compare_digest(raw_value, expected_value)


def create_admin_redirect_response(location: str) -> RedirectResponse:
    return RedirectResponse(url=location, status_code=303)


def set_admin_cookie(response: RedirectResponse, request: Request) -> None:
    expires_at = int(time.time()) + ADMIN_SESSION_TTL_SECONDS
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=build_admin_session_value(expires_at),
        max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def clear_admin_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(key=ADMIN_COOKIE_NAME, path="/")


def verify_admin_access(request: Request, api_key: str = Security(api_key_header)) -> str:
    if api_key == UPLOAD_PASSWORD or has_valid_admin_session(request):
        return "ok"
    raise HTTPException(status_code=401, detail="需要管理员权限")


def build_asset_url(*parts: str) -> str:
    return "/" + "/".join(quote(part) for part in parts)


def build_extraction_options(
    extraction_mode: str,
    min_duration: float,
    max_silence: float,
    leading_padding: float,
    trailing_padding: float,
    min_active_ratio: float,
    output_format: str,
) -> ExtractionOptions:
    return ExtractionOptions(
        extraction_mode=(extraction_mode or "classic").strip().lower() or "classic",
        min_duration=max(15.0, float(min_duration)),
        max_silence=max(0.3, float(max_silence)),
        leading_padding=max(0.0, float(leading_padding)),
        trailing_padding=max(0.0, float(trailing_padding)),
        min_active_ratio=min(max(float(min_active_ratio), 0.1), 0.95),
        output_format=(output_format or "mp3").strip().lower() or "mp3",
    )


def options_from_brec_config(config: BrecConfig) -> ExtractionOptions:
    return build_extraction_options(
        extraction_mode=config.extraction_mode,
        min_duration=config.min_duration,
        max_silence=config.max_silence,
        leading_padding=config.leading_padding,
        trailing_padding=config.trailing_padding,
        min_active_ratio=config.min_active_ratio,
        output_format=config.output_format,
    )


def format_extraction_result(
    source_label: str,
    category: str,
    extraction_mode: str,
    summary: Any,
    resolved_ffmpeg: str,
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    for segment in summary.segments:
        if segment.output_filename is None or segment.output_path is None:
            continue

        relative_path = segment.output_path.relative_to(SONGCUT_DIR).parts
        segments.append(
            {
                "index": segment.index,
                "title": Path(segment.output_filename).stem,
                "filename": segment.output_filename,
                "path": build_asset_url("assets", "songcuts", *relative_path),
                "recognized": bool(segment.recognition_title),
                "recognized_title": segment.recognition_title,
                "recognized_artist": segment.recognition_artist,
                "recognition_provider": segment.recognition_provider,
                "recognition_confidence": round(segment.recognition_confidence, 3),
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "duration": round(segment.duration, 2),
                "active_ratio": round(segment.active_ratio, 3),
                "aligned_by": segment.aligned_by,
                "alignment_shift_start": round(segment.alignment_shift_start, 2),
                "alignment_shift_end": round(segment.alignment_shift_end, 2),
            }
        )

    recognized_count = sum(1 for segment in segments if segment["recognized"])
    aligned_count = sum(1 for segment in segments if segment["aligned_by"])
    message = "已完成自动提取" if segments else "分析完成，但没有找到符合条件的完整唱段"
    return {
        "status": "success",
        "message": message,
        "category": category,
        "extraction_mode": extraction_mode,
        "source": source_label,
        "saved_count": len(segments),
        "recognized_count": recognized_count,
        "aligned_count": aligned_count,
        "ffmpeg_path": resolved_ffmpeg,
        "analysis": {
            "total_duration": round(summary.total_duration, 2),
            "threshold_rms": round(summary.threshold_rms, 2),
            "noise_floor_rms": round(summary.noise_floor_rms, 2),
            "loud_rms": round(summary.loud_rms, 2),
            "windows": summary.windows,
        },
        "segments": segments,
    }


def result_segment_paths(result: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for segment in result.get("segments", []):
        raw_path = segment.get("path", "")
        if not raw_path.startswith("/assets/songcuts/"):
            continue
        relative_text = unquote(raw_path.replace("/assets/songcuts/", "", 1))
        paths.append(SONGCUT_DIR / Path(relative_text))
    return paths


def enrich_segments_with_song_titles(
    summary: Any,
    resolved_ffmpeg: str,
    *,
    source_path: Optional[Path] = None,
    options: Optional[ExtractionOptions] = None,
) -> None:
    has_source = source_path is not None and source_path.is_file()
    new_segments: list[Any] = []

    for segment in summary.segments:
        if segment.output_path is None or segment.output_filename is None:
            new_segments.append(segment)
            continue

        try:
            recognition = recognize_song_title(
                segment_path=segment.output_path,
                duration=segment.duration,
                ffmpeg_path=resolved_ffmpeg,
            )
            if not recognition and has_source:
                recognition = _maybe_recognize_by_lyrics(
                    ffmpeg_path=resolved_ffmpeg,
                    source_path=source_path,
                    segment=segment,
                    summary=summary,
                )
            if not recognition:
                logger.info("Song title not recognized for %s", segment.output_path)
                new_segments.append(segment)
                continue

            replaced = None
            suppress_rename = False
            if has_source:
                replaced, suppress_rename = _maybe_split_segment(
                    ffmpeg_path=resolved_ffmpeg,
                    source_path=source_path,
                    segment=segment,
                    recognition=recognition,
                    summary=summary,
                    options=options,
                )

            if suppress_rename:
                # Fingerprint matched the BGM track playing under chat: keep the
                # cut under its original name, only note the match in metadata.
                logger.info(
                    "recognition for %s looks like background music, keeping original name",
                    segment.output_path,
                )
                update_segment_metadata(
                    segment.output_path,
                    bgm_suspected=True,
                    bgm_match_title=recognition.title,
                    bgm_match_artist=recognition.artist,
                )
                new_segments.append(segment)
                continue

            if replaced is not None:
                new_segments.extend(replaced)
                continue

            if has_source:
                alignment = compute_aligned_boundaries(
                    segment.start,
                    segment.end,
                    recognition,
                    source_total_duration=summary.total_duration,
                )
                if alignment is not None:
                    _reexport_aligned_segment(
                        ffmpeg_path=resolved_ffmpeg,
                        source_path=source_path,
                        segment=segment,
                        alignment=alignment,
                        output_format=(options.output_format if options else "mp3"),
                    )

            renamed_path = rename_songcut_with_recognition(segment.output_path, recognition)
            segment.recognition_title = recognition.title
            segment.recognition_artist = recognition.artist
            segment.recognition_provider = recognition.provider
            segment.recognition_confidence = recognition.confidence
            segment.output_path = renamed_path
            segment.output_filename = renamed_path.name
            new_segments.append(segment)
        except Exception as exc:  # pragma: no cover
            logger.warning("Song title recognition failed for %s: %s", segment.output_path, exc)
            new_segments.append(segment)

    summary.segments = new_segments
    for index, segment in enumerate(summary.segments, start=1):
        segment.index = index


def _maybe_recognize_by_lyrics(
    *,
    ffmpeg_path: str,
    source_path: Path,
    segment: Any,
    summary: Any,
) -> Optional[SongRecognition]:
    """Lyric-based fallback for karaoke covers fingerprinting cannot match.

    Chat-heavy blocks are skipped up front (singing gate on the source audio)
    so Whisper time is only spent where someone actually sings.
    """
    if not parse_bool_env("SONGCUT_LYRIC_RECOGNITION_ENABLED", True):
        return None
    if segment.output_path is None:
        return None

    absolute_threshold = summary.threshold_rms if getattr(summary, "threshold_rms", 0) >= 100 else None
    activity = measure_region_activity(
        ffmpeg_path=ffmpeg_path,
        source_path=source_path,
        start=segment.start,
        end=segment.end,
        absolute_threshold=absolute_threshold,
    )
    if activity is not None and not looks_like_singing(activity):
        logger.info(
            "lyric recognition skipped for %s: region looks like chat, not singing",
            segment.output_path,
        )
        return None

    try:
        recognition = recognize_song_title_by_lyrics(
            segment_path=segment.output_path,
            duration=segment.duration,
            ffmpeg_path=ffmpeg_path,
        )
    except Exception as exc:
        logger.warning("lyric recognition failed for %s: %s", segment.output_path, exc)
        return None

    if recognition:
        logger.info(
            "lyric recognition matched %s - %s (score-based conf %.2f) for %s",
            recognition.title,
            recognition.artist,
            recognition.confidence,
            segment.output_path,
        )
    return recognition


def _maybe_split_segment(
    *,
    ffmpeg_path: str,
    source_path: Path,
    segment: Any,
    recognition: SongRecognition,
    summary: Any,
    options: Optional[ExtractionOptions],
) -> tuple[Optional[list[Any]], bool]:
    """Split a multi-song cut into per-song files using fingerprint offsets.

    Returns (replacement_segments, suppress_rename). suppress_rename marks a
    fingerprint that matched background music under chat: the cut stays as-is
    and must not be renamed to the BGM track title.
    """
    if not parse_bool_env("SONGCUT_MEDLEY_SPLIT_ENABLED", True):
        return None, False

    intervals = split_medley_intervals(
        segment.start,
        segment.end,
        recognition.all_matches,
        source_total_duration=summary.total_duration,
    )
    if not intervals:
        return None, False

    absolute_threshold = summary.threshold_rms if getattr(summary, "threshold_rms", 0) >= 100 else None
    singing_intervals: list[Any] = []
    for interval in intervals:
        activity = measure_region_activity(
            ffmpeg_path=ffmpeg_path,
            source_path=source_path,
            start=interval.start,
            end=interval.end,
            absolute_threshold=absolute_threshold,
        )
        if activity is None or looks_like_singing(activity):
            singing_intervals.append(interval)
            continue
        logger.info(
            "interval %s %.1f-%.1fs looks like BGM under chat "
            "(sustained %.0fs, cv %.2f, active %.2f) - excluded from split",
            interval.title,
            interval.start,
            interval.end,
            activity.longest_active_run_seconds,
            activity.cv,
            activity.active_ratio,
        )

    if not singing_intervals:
        # Everything fingerprinted here is background music: keep the whole cut
        # under its original name instead of mislabeling it as a songcut.
        return None, True

    segment_duration = segment.end - segment.start
    covered = sum(interval.end - interval.start for interval in singing_intervals)
    single_song_like = (
        len(singing_intervals) == 1
        and segment_duration > 0
        and covered >= segment_duration * 0.8
    )
    if single_song_like:
        return None, False

    return _export_split_parts(
        ffmpeg_path=ffmpeg_path,
        source_path=source_path,
        segment=segment,
        intervals=singing_intervals,
        recognition=recognition,
        summary=summary,
        options=options,
    ), False


def _export_split_parts(
    *,
    ffmpeg_path: str,
    source_path: Path,
    segment: Any,
    intervals: list[Any],
    recognition: SongRecognition,
    summary: Any,
    options: Optional[ExtractionOptions],
) -> Optional[list[Any]]:
    if segment.output_path is None:
        return None

    output_dir = segment.output_path.parent
    original_metadata = read_segment_metadata(segment.output_path) or {}
    category = str(original_metadata.get("category", "")) or "自动提取"
    output_format = (options.output_format if options else "mp3") or "mp3"
    lead_pad = max(5.0, options.leading_padding if options else 5.0)
    trail_pad = max(5.0, options.trailing_padding if options else 5.0)
    min_residual = max(60.0, options.min_duration if options else 60.0)
    base_name = build_recording_date_label(source_path)
    total = summary.total_duration or segment.end

    staged: list[tuple[Any, Path]] = []
    residual_parts: list[tuple[float, float, Path]] = []
    try:
        for position, interval in enumerate(intervals, start=1):
            start = max(0.0, interval.start - lead_pad)
            end = min(total, interval.end + trail_pad)
            if end - start < 30.0:
                continue

            staged_path = dedupe_path(
                output_dir / f"__split_tmp_{segment.index}_{position}.{output_format}"
            )
            export_segment(
                ffmpeg_path=ffmpeg_path,
                source_path=source_path,
                output_path=staged_path,
                start=start,
                end=end,
                output_format=output_format,
            )
            write_segment_metadata(
                staged_path,
                source_path=source_path,
                category=category,
                start=start,
                end=end,
                duration=end - start,
                output_format=output_format,
            )
            staged.append((interval, staged_path))

        if not staged:
            return None

        residuals = _subtract_ranges(
            segment.start,
            segment.end,
            [(max(segment.start, iv.start - lead_pad), min(segment.end, iv.end + trail_pad)) for iv in intervals],
        )
        for start, end in residuals:
            if end - start < min_residual:
                continue
            residual_path = dedupe_path(
                output_dir
                / (
                    f"{base_name}_part_{_format_time_for_filename(start)}-"
                    f"{_format_time_for_filename(end)}.{output_format}"
                )
            )
            export_segment(
                ffmpeg_path=ffmpeg_path,
                source_path=source_path,
                output_path=residual_path,
                start=start,
                end=end,
                output_format=output_format,
            )
            write_segment_metadata(
                residual_path,
                source_path=source_path,
                category=category,
                start=start,
                end=end,
                duration=end - start,
                output_format=output_format,
            )
            residual_parts.append((start, end, residual_path))
    except (SongcutExtractionError, OSError) as exc:
        logger.warning("medley split failed for %s (%s), keeping original cut", segment.output_path, exc)
        for _, path in staged:
            _remove_segment_file(path)
        for _, _, path in residual_parts:
            _remove_segment_file(path)
        return None

    # Everything staged fine: retire the original file and rename song parts.
    parts: list[Any] = []
    for interval, staged_path in staged:
        part_recognition = SongRecognition(
            title=interval.title,
            artist=interval.artist,
            confidence=interval.confidence,
            provider="acrcloud",
            matched_samples=interval.matched_samples,
            samples=list(interval.samples),
        )
        final_path = rename_songcut_with_recognition(staged_path, part_recognition)
        update_segment_metadata(
            final_path,
            aligned_by="acrcloud-medley",
            alignment_track_duration=round(interval.end - interval.start, 3),
            alignment_matched_samples=interval.matched_samples,
        )
        metadata = read_segment_metadata(final_path) or {}
        parts.append(
            _build_split_segment(
                final_path=final_path,
                metadata=metadata,
                recognition=part_recognition,
                aligned_by="acrcloud-medley",
            )
        )

    for start, end, residual_path in residual_parts:
        metadata = read_segment_metadata(residual_path) or {}
        parts.append(
            _build_split_segment(
                final_path=residual_path,
                metadata=metadata,
                recognition=None,
                aligned_by="",
                time_start=start,
                time_end=end,
            )
        )

    _remove_segment_file(segment.output_path)
    logger.info(
        "split %s into %d song cut(s) + %d residual cut(s)",
        segment.output_path.name,
        len(staged),
        len(residual_parts),
    )
    parts.sort(key=lambda item: item.start)
    return parts


def _build_split_segment(
    *,
    final_path: Path,
    metadata: dict,
    recognition: Optional[SongRecognition],
    aligned_by: str,
    time_start: Optional[float] = None,
    time_end: Optional[float] = None,
) -> Segment:
    seg_start = time_start if time_start is not None else float(metadata.get("start", 0.0))
    seg_end = time_end if time_end is not None else float(metadata.get("end", 0.0))
    segment = Segment(
        index=0,
        start=round(seg_start, 3),
        end=round(seg_end, 3),
        duration=round(seg_end - seg_start, 3),
        active_ratio=1.0,
        average_rms=0.0,
        peak_rms=0,
        output_filename=final_path.name,
        output_path=final_path,
        aligned_by=aligned_by,
    )
    if recognition is not None:
        segment.recognition_title = recognition.title
        segment.recognition_artist = recognition.artist
        segment.recognition_provider = recognition.provider
        segment.recognition_confidence = recognition.confidence
    return segment


def _subtract_ranges(
    start: float,
    end: float,
    covered: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    remaining = [(start, end)]
    for cover_start, cover_end in sorted(covered):
        next_round: list[tuple[float, float]] = []
        for piece_start, piece_end in remaining:
            if cover_end <= piece_start or cover_start >= piece_end:
                next_round.append((piece_start, piece_end))
                continue
            if cover_start > piece_start:
                next_round.append((piece_start, cover_start))
            if cover_end < piece_end:
                next_round.append((cover_end, piece_end))
        remaining = next_round
    return [(piece_start, piece_end) for piece_start, piece_end in remaining if piece_end - piece_start > 1.0]


def _remove_segment_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        metadata_path_for_segment(path).unlink(missing_ok=True)
    except OSError:
        pass


def _reexport_aligned_segment(
    *,
    ffmpeg_path: str,
    source_path: Path,
    segment: Any,
    alignment: Any,
    output_format: str,
) -> None:
    """Re-cut one songcut from the recording so the full intro/outro is kept."""
    if segment.output_path is None:
        return

    staged_path = segment.output_path.with_name(f"{segment.output_path.stem}.reexport{segment.output_path.suffix}")
    try:
        export_segment(
            ffmpeg_path=ffmpeg_path,
            source_path=source_path,
            output_path=staged_path,
            start=alignment.start,
            end=alignment.end,
            output_format=output_format,
        )
        staged_path.replace(segment.output_path)
    except (SongcutExtractionError, OSError) as exc:
        staged_path.unlink(missing_ok=True)
        logger.warning(
            "aligned re-export failed for %s (%s), keeping original boundaries",
            segment.output_path,
            exc,
        )
        return

    segment.start = round(alignment.start, 3)
    segment.end = round(alignment.end, 3)
    segment.duration = round(alignment.end - alignment.start, 3)
    segment.alignment_shift_start = round(alignment.shift_start, 3)
    segment.alignment_shift_end = round(alignment.shift_end, 3)
    segment.aligned_by = "acrcloud"
    update_segment_metadata(
        segment.output_path,
        start=segment.start,
        end=segment.end,
        duration=segment.duration,
        aligned_by=segment.aligned_by,
        alignment_shift_start=segment.alignment_shift_start,
        alignment_shift_end=segment.alignment_shift_end,
        alignment_track_duration=alignment.track_duration,
        alignment_matched_samples=alignment.matched_samples,
    )
    logger.info(
        "aligned songcut boundaries for %s: start %+.1fs, end %+.1fs",
        segment.output_path.name,
        alignment.shift_start,
        alignment.shift_end,
    )


def extract_from_path(
    source_path: Path,
    category: str,
    options: ExtractionOptions,
    ffmpeg_path: Optional[str] = None,
    source_label: Optional[str] = None,
) -> dict[str, Any]:
    if not source_path.is_file():
        raise SongcutExtractionError(f"找不到录播文件: {source_path}")
    if source_path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        raise SongcutExtractionError(f"暂不支持该文件类型: {source_path.suffix or '未知'}")

    safe_category = normalize_storage_name(category, fallback="自动提取")
    summary, resolved_ffmpeg = extract_songcuts_from_source(
        source_path=source_path,
        songcut_root=SONGCUT_DIR,
        category=safe_category,
        options=options,
        ffmpeg_path=(ffmpeg_path or "").strip() or None,
    )
    enrich_segments_with_song_titles(
        summary,
        resolved_ffmpeg,
        source_path=source_path,
        options=options,
    )
    return format_extraction_result(
        source_label=source_label or source_path.name,
        category=safe_category,
        extraction_mode=options.extraction_mode,
        summary=summary,
        resolved_ffmpeg=resolved_ffmpeg,
    )


def scan_songcuts(category: Optional[str] = None, search: Optional[str] = None) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    search_term = search.casefold() if search else None

    for path in sorted(SONGCUT_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            continue

        relative_path = path.relative_to(SONGCUT_DIR)
        parts = relative_path.parts
        track_category = parts[0] if len(parts) > 1 else "未分类"

        if category and track_category != category:
            continue

        title = path.stem
        relative_text = str(relative_path).casefold()
        if search_term and search_term not in title.casefold() and search_term not in relative_text:
            continue

        result.setdefault(track_category, []).append(
            {
                "filename": path.name,
                "title": title,
                "path": build_asset_url("assets", "songcuts", *parts),
                "category": track_category,
            }
        )

    return result


def scan_audio_library(category: Optional[str] = None) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    categories = [category] if category else CATEGORIES

    for category_name in categories:
        category_dir = AUDIO_DIR / category_name
        if not category_dir.exists():
            continue

        files = []
        for item in sorted(category_dir.iterdir()):
            if not item.is_file() or item.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue

            files.append(
                {
                    "filename": item.name,
                    "path": build_asset_url("assets", "audio", category_name, item.name),
                    "category": category_name,
                }
            )

        result[category_name] = files

    return result


def build_brec_summary(request: Request) -> dict[str, Any]:
    config = current_brec_config()
    recent_files = scan_recordings(config=config, limit=20)
    api_summary: dict[str, Any] = {
        "configured": bool(config.api_base_url.strip()),
        "available": False,
        "version": None,
        "room_count": 0,
        "rooms": [],
        "error": None,
    }

    try:
        version = fetch_api_json(config, "/api/version")
        rooms_raw = fetch_api_json(config, "/api/room")
        rooms = summarize_rooms(rooms_raw)
        api_summary.update(
            {
                "available": True,
                "version": version,
                "room_count": len(rooms),
                "rooms": rooms[:20],
            }
        )
    except BrecIntegrationError as exc:
        api_summary["error"] = str(exc)

    return {
        "config": config.to_dict(),
        "api": api_summary,
        "recordings": recent_files,
        "supported_media_extensions": list(SUPPORTED_MEDIA_EXTENSIONS),
        "webhook_url": build_webhook_url(str(request.base_url).rstrip("/"), config.webhook_secret),
        "automation": {
            "recognition": describe_recognition_provider(),
            "cleanup": describe_recording_cleanup(),
            "gpu_model": describe_gpu_model_backend(),
        },
    }


def build_bili_upload_summary() -> dict[str, Any]:
    config = current_bili_upload_config()
    return {
        "config": config.to_dict(),
        "status": describe_bili_upload(config),
    }


def background_extract_brec_file(relative_path: str, config_snapshot: dict[str, Any]) -> None:
    config = BrecConfig.from_dict(config_snapshot)
    try:
        source_path = resolve_recording_path(config, relative_path)
        result = extract_from_path(
            source_path=source_path,
            category=config.auto_category,
            options=options_from_brec_config(config),
            ffmpeg_path=config.ffmpeg_path,
            source_label=relative_path,
        )
        register_processed_recording(
            manifest_path=PROCESSED_RECORDINGS_PATH,
            source_path=source_path,
            outputs=result_segment_paths(result),
            song_titles=[segment.get("title", "") for segment in result.get("segments", [])],
        )
        if config.workdir:
            cleanup_processed_recordings(Path(config.workdir), PROCESSED_RECORDINGS_PATH)
        logger.info("BililiveRecorder auto import finished: %s", result["source"])
    except Exception as exc:  # pragma: no cover
        logger.exception("BililiveRecorder auto import failed for %s: %s", relative_path, exc)


ensure_directories()


@app.get("/admin/login")
async def get_admin_login(request: Request):
    if has_valid_admin_session(request):
        return create_admin_redirect_response("/admin/songcuts")
    return FileResponse(ADMIN_VIEWS_DIR / "admin-login.html")


@app.post("/admin/login")
async def post_admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if username != ADMIN_USERNAME or password != UPLOAD_PASSWORD:
        return create_admin_redirect_response("/admin/login?error=1")

    response = create_admin_redirect_response("/admin/songcuts")
    set_admin_cookie(response, request)
    return response


@app.post("/admin/logout")
async def post_admin_logout():
    response = create_admin_redirect_response("/admin/login")
    clear_admin_cookie(response)
    return response


@app.get("/admin/songcuts")
async def get_admin_songcuts(request: Request):
    if not has_valid_admin_session(request):
        return create_admin_redirect_response("/admin/login")
    return FileResponse(PRIVATE_VIEWS_DIR / "songcuts-admin.html")


@app.post("/api/upload-audio")
async def upload_audio(
    file: UploadFile = File(...),
    category: str = Form("其他"),
    _: str = Depends(verify_admin_access),
):
    try:
        if category not in CATEGORIES:
            return JSONResponse(
                status_code=400,
                content={"message": f"无效分类: {category}. 有效分类: {', '.join(CATEGORIES)}"},
            )

        target_path = AUDIO_DIR / category / Path(file.filename).name
        with target_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        return {"filename": target_path.name, "category": category, "status": "success"}
    except Exception as exc:  # pragma: no cover
        return JSONResponse(status_code=500, content={"message": f"上传失败: {exc}"})


@app.get("/api/audio-list")
async def get_audio_list(category: Optional[str] = None):
    if category and category not in CATEGORIES:
        return JSONResponse(status_code=400, content={"message": f"无效分类: {category}"})
    return {"categories": scan_audio_library(category=category)}


@app.get("/api/categories")
async def get_categories():
    return {"categories": CATEGORIES}


@app.get("/api/songcuts")
async def get_songcuts(category: Optional[str] = None, search: Optional[str] = None):
    categories = scan_songcuts(category=category, search=search)
    total = sum(len(files) for files in categories.values())
    return {"categories": categories, "total": total}


@app.get("/api/songcut-categories")
async def get_songcut_categories():
    categories = sorted(scan_songcuts().keys(), key=lambda value: value.casefold())
    return {"categories": categories}


@app.get("/api/songcuts/extractor-info")
async def get_songcut_extractor_info(_: str = Depends(verify_admin_access)):
    ffmpeg_path = find_ffmpeg_binary()
    return {
        "ffmpeg_available": bool(ffmpeg_path),
        "ffmpeg_path": ffmpeg_path,
        "supported_media_extensions": list(SUPPORTED_MEDIA_EXTENSIONS),
        "songcut_categories": sorted(scan_songcuts().keys(), key=lambda value: value.casefold()),
        "extraction_modes": ["classic", "gpu-model"],
        "gpu_model": describe_gpu_model_backend(),
        "recognition": describe_recognition_provider(),
        "cleanup": describe_recording_cleanup(),
        "bili_upload": build_bili_upload_summary(),
    }


@app.post("/api/songcuts/extract")
async def extract_songcuts(
    file: UploadFile = File(...),
    category: str = Form("自动提取"),
    extraction_mode: str = Form("classic"),
    min_duration: float = Form(60.0),
    max_silence: float = Form(6.0),
    leading_padding: float = Form(6.0),
    trailing_padding: float = Form(2.5),
    min_active_ratio: float = Form(0.45),
    output_format: str = Form("mp3"),
    ffmpeg_path: str = Form(""),
    _: str = Depends(verify_admin_access),
):
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="请选择要处理的直播录播文件")

    temp_dir = Path(tempfile.mkdtemp(prefix="songcut-upload-", dir=BASE_DIR))
    source_path = temp_dir / filename

    try:
        emit_runtime(
            f"manual extraction started: file={filename}, mode={extraction_mode}, format={output_format}"
        )
        with source_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        result = extract_from_path(
            source_path=source_path,
            category=category,
            options=build_extraction_options(
                extraction_mode=extraction_mode,
                min_duration=min_duration,
                max_silence=max_silence,
                leading_padding=leading_padding,
                trailing_padding=trailing_padding,
                min_active_ratio=min_active_ratio,
                output_format=output_format,
            ),
            ffmpeg_path=ffmpeg_path,
            source_label=filename,
        )
        emit_runtime(
            "manual extraction finished: "
            f"file={filename}, mode={extraction_mode}, saved={result.get('saved_count', 0)}, "
            f"recognized={result.get('recognized_count', 0)}"
        )
        return result
    except SongcutExtractionError as exc:
        print(
            f"[songcuts] manual extraction failed: file={filename}, mode={extraction_mode}, error={exc}",
            flush=True,
        )
        logger.warning("manual extraction failed: file=%s, mode=%s, error=%s", filename, extraction_mode, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/api/brec/summary")
async def get_brec_summary(request: Request, _: str = Depends(verify_admin_access)):
    return build_brec_summary(request)


@app.get("/api/bili-upload/summary")
async def get_bili_upload_summary(_: str = Depends(verify_admin_access)):
    return build_bili_upload_summary()


@app.post("/api/bili-upload/config")
async def set_bili_upload_config(
    payload: BiliUploadConfigPayload,
    _: str = Depends(verify_admin_access),
):
    config = save_bili_upload_config(BILI_UPLOAD_CONFIG_PATH, BiliUploadConfig.from_dict(payload.model_dump()))
    return {
        "status": "success",
        "message": "已保存 B 站投稿配置",
        "config": config.to_dict(),
        "status_info": describe_bili_upload(config),
    }


@app.post("/api/bili-upload/cookie")
async def upload_bili_cookie(
    file: UploadFile = File(...),
    _: str = Depends(verify_admin_access),
):
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="请选择 cookies.json 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="cookies.json 为空")

    try:
        saved_path = save_uploaded_cookie(BILI_UPLOAD_COOKIE_DIR, filename, content)
        config = current_bili_upload_config()
        config.cookie_file = str(saved_path)
        saved_config = save_bili_upload_config(BILI_UPLOAD_CONFIG_PATH, config)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存 cookies.json 失败: {exc}") from exc

    return {
        "status": "success",
        "message": "已保存 cookies.json",
        "config": saved_config.to_dict(),
        "status_info": describe_bili_upload(saved_config),
    }


@app.post("/api/bili-upload/upload")
async def upload_songcut_to_bili(
    payload: BiliUploadSongcutPayload,
    _: str = Depends(verify_admin_access),
):
    config = current_bili_upload_config()
    brec_config = current_brec_config()

    try:
        songcut_path = resolve_songcut_path(SONGCUT_DIR.resolve(strict=False), payload.path)
        result = upload_songcut_video(
            songcut_path,
            config,
            ffmpeg_path=brec_config.ffmpeg_path or None,
            temp_root=BILI_UPLOAD_TEMP_DIR,
        )
        return result
    except BiliUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/brec/config")
async def set_brec_config(
    payload: BrecConfigPayload,
    request: Request,
    _: str = Depends(verify_admin_access),
):
    config = save_brec_config(BREC_CONFIG_PATH, BrecConfig.from_dict(payload.model_dump()))
    return {
        "status": "success",
        "message": "已保存 BililiveRecorder 接入配置",
        **build_brec_summary(request),
        "config": config.to_dict(),
    }


@app.post("/api/brec/import")
async def import_brec_recording(
    payload: BrecImportPayload,
    _: str = Depends(verify_admin_access),
):
    config = current_brec_config()

    try:
        source_path = resolve_recording_path(config, payload.relative_path)
        options = build_extraction_options(
            extraction_mode=payload.extraction_mode or config.extraction_mode,
            min_duration=payload.min_duration if payload.min_duration is not None else config.min_duration,
            max_silence=payload.max_silence if payload.max_silence is not None else config.max_silence,
            leading_padding=payload.leading_padding if payload.leading_padding is not None else config.leading_padding,
            trailing_padding=payload.trailing_padding if payload.trailing_padding is not None else config.trailing_padding,
            min_active_ratio=payload.min_active_ratio if payload.min_active_ratio is not None else config.min_active_ratio,
            output_format=payload.output_format or config.output_format,
        )
        category = payload.category or config.auto_category
        ffmpeg_path = payload.ffmpeg_path or config.ffmpeg_path

        result = extract_from_path(
            source_path=source_path,
            category=category,
            options=options,
            ffmpeg_path=ffmpeg_path,
            source_label=payload.relative_path,
        )
        register_processed_recording(
            manifest_path=PROCESSED_RECORDINGS_PATH,
            source_path=source_path,
            outputs=result_segment_paths(result),
            song_titles=[segment.get("title", "") for segment in result.get("segments", [])],
        )
        if config.workdir:
            cleanup_processed_recordings(Path(config.workdir), PROCESSED_RECORDINGS_PATH)
        return result
    except (BrecIntegrationError, SongcutExtractionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/brec/webhook")
async def receive_brec_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    secret: str = "",
):
    config = current_brec_config()
    if config.webhook_secret and secret != config.webhook_secret:
        raise HTTPException(status_code=403, detail="Webhook secret 不匹配")

    payload = await request.json()
    event_type = payload.get("EventType")
    event_id = payload.get("EventId")
    event_data = payload.get("EventData") or {}
    relative_path = event_data.get("RelativePath")

    if event_id and event_id in recent_brec_event_ids:
        return {"status": "ignored", "reason": "duplicate-event", "event_id": event_id}

    if event_type != "FileClosed":
        return {"status": "ignored", "reason": "unsupported-event", "event_type": event_type}

    if not config.auto_extract:
        return {"status": "ignored", "reason": "auto-extract-disabled"}

    if not relative_path:
        return {"status": "ignored", "reason": "missing-relative-path"}

    if Path(relative_path).suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
        return {"status": "ignored", "reason": "unsupported-file", "relative_path": relative_path}

    if event_id:
        recent_brec_event_ids.append(event_id)

    background_tasks.add_task(background_extract_brec_file, relative_path, config.to_dict())
    return {"status": "accepted", "event_type": event_type, "relative_path": relative_path}


app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
