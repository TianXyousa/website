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
from urllib.parse import quote

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
from songcut_extractor import (
    ExtractionOptions,
    SongcutExtractionError,
    extract_songcuts_from_source,
    find_ffmpeg_binary,
    normalize_storage_name,
)

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
AUDIO_DIR = ASSETS_DIR / "audio"
SONGCUT_DIR = ASSETS_DIR / "songcuts"
BREC_CONFIG_PATH = Path(os.getenv("BREC_CONFIG_PATH", str(BASE_DIR / ".brec_integration.json")))
STATIC_DIR = BASE_DIR / "static"
ADMIN_VIEWS_DIR = BASE_DIR / "admin_views"

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
UPLOAD_PASSWORD = os.getenv("UPLOAD_PASSWORD", "Zh030226@")
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
    "小蛙招呼",
    "小蛙怪叫",
    "小蛙怪话",
    "蛙言蛙语",
    "认同",
    "道歉",
    "疑问",
    "感谢",
    "高兴",
    "遗憾",
    "笨蛋",
    "生气",
    "盐蛙",
    "蛙笑",
    "删！",
]

logger = logging.getLogger("songcuts")
recent_brec_event_ids: deque[str] = deque(maxlen=200)

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
    output_format: str = "mp3"
    min_duration: float = 60.0
    max_silence: float = 2.8
    leading_padding: float = 1.5
    trailing_padding: float = 2.5
    min_active_ratio: float = 0.58


class BrecImportPayload(BaseModel):
    relative_path: str
    category: Optional[str] = None
    min_duration: Optional[float] = None
    max_silence: Optional[float] = None
    leading_padding: Optional[float] = None
    trailing_padding: Optional[float] = None
    min_active_ratio: Optional[float] = None
    output_format: Optional[str] = None
    ffmpeg_path: Optional[str] = None


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

    expires_text, provided_digest = raw_value.split(".", 1)
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


def ensure_directories() -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    SONGCUT_DIR.mkdir(parents=True, exist_ok=True)
    for category in CATEGORIES:
        (AUDIO_DIR / category).mkdir(parents=True, exist_ok=True)


def build_asset_url(*parts: str) -> str:
    return "/" + "/".join(quote(part) for part in parts)


def current_brec_config() -> BrecConfig:
    config = load_brec_config(BREC_CONFIG_PATH)
    if BREC_CONFIG_PATH.exists():
        return config

    defaults = BrecConfig(
        api_base_url=os.getenv("BREC_DEFAULT_API_BASE_URL", config.api_base_url),
        workdir=os.getenv("BREC_DEFAULT_WORKDIR", config.workdir),
        api_username=os.getenv("BREC_DEFAULT_API_USERNAME", config.api_username),
        api_password=os.getenv("BREC_DEFAULT_API_PASSWORD", config.api_password),
        auto_category=os.getenv("BREC_DEFAULT_AUTO_CATEGORY", config.auto_category),
        ffmpeg_path=os.getenv("BREC_DEFAULT_FFMPEG_PATH", config.ffmpeg_path),
        output_format=os.getenv("BREC_DEFAULT_OUTPUT_FORMAT", config.output_format),
    )
    return defaults


def build_extraction_options(
    min_duration: float,
    max_silence: float,
    leading_padding: float,
    trailing_padding: float,
    min_active_ratio: float,
    output_format: str,
) -> ExtractionOptions:
    return ExtractionOptions(
        min_duration=max(15.0, float(min_duration)),
        max_silence=max(0.3, float(max_silence)),
        leading_padding=max(0.0, float(leading_padding)),
        trailing_padding=max(0.0, float(trailing_padding)),
        min_active_ratio=min(max(float(min_active_ratio), 0.1), 0.95),
        output_format=(output_format or "mp3").strip().lower() or "mp3",
    )


def options_from_brec_config(config: BrecConfig) -> ExtractionOptions:
    return build_extraction_options(
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
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "duration": round(segment.duration, 2),
                "active_ratio": round(segment.active_ratio, 3),
            }
        )

    return {
        "status": "success",
        "message": "已完成自动提取" if segments else "分析完成，但没有找到符合条件的完整唱段",
        "category": category,
        "source": source_label,
        "saved_count": len(segments),
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
    return format_extraction_result(
        source_label=source_label or source_path.name,
        category=safe_category,
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
    return FileResponse(ADMIN_VIEWS_DIR / "songcuts-admin.html")


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
    }


@app.post("/api/songcuts/extract")
async def extract_songcuts(
    file: UploadFile = File(...),
    category: str = Form("自动提取"),
    min_duration: float = Form(60.0),
    max_silence: float = Form(2.8),
    leading_padding: float = Form(1.5),
    trailing_padding: float = Form(2.5),
    min_active_ratio: float = Form(0.58),
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
        with source_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        result = extract_from_path(
            source_path=source_path,
            category=category,
            options=build_extraction_options(
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
        return result
    except SongcutExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/api/brec/summary")
async def get_brec_summary(request: Request, _: str = Depends(verify_admin_access)):
    return build_brec_summary(request)


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
            min_duration=payload.min_duration if payload.min_duration is not None else config.min_duration,
            max_silence=payload.max_silence if payload.max_silence is not None else config.max_silence,
            leading_padding=payload.leading_padding if payload.leading_padding is not None else config.leading_padding,
            trailing_padding=payload.trailing_padding if payload.trailing_padding is not None else config.trailing_padding,
            min_active_ratio=payload.min_active_ratio if payload.min_active_ratio is not None else config.min_active_ratio,
            output_format=payload.output_format or config.output_format,
        )
        category = payload.category or config.auto_category
        ffmpeg_path = payload.ffmpeg_path or config.ffmpeg_path

        return extract_from_path(
            source_path=source_path,
            category=category,
            options=options,
            ffmpeg_path=ffmpeg_path,
            source_label=payload.relative_path,
        )
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
