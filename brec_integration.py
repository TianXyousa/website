from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

SUPPORTED_RECORDING_EXTENSIONS = (
    ".mp3",
    ".wav",
    ".ogg",
    ".m4a",
    ".flac",
    ".aac",
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".ts",
    ".m4v",
    ".flv",
)


@dataclass
class BrecConfig:
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

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "BrecConfig":
        if not isinstance(data, dict):
            return cls()

        allowed = {field.name for field in fields(cls)}
        normalized = {key: value for key, value in data.items() if key in allowed}
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["api_base_url"] = normalize_api_base_url(self.api_base_url)
        payload["workdir"] = self.workdir.strip()
        payload["api_username"] = self.api_username.strip()
        payload["api_password"] = self.api_password
        payload["webhook_secret"] = self.webhook_secret.strip()
        payload["auto_category"] = self.auto_category.strip() or "录播姬自动提取"
        payload["ffmpeg_path"] = self.ffmpeg_path.strip()
        return payload


class BrecIntegrationError(RuntimeError):
    """Raised when BililiveRecorder integration fails."""


def normalize_api_base_url(value: str) -> str:
    cleaned = (value or "").strip().rstrip("/")
    return cleaned or "http://127.0.0.1:2356"


def load_brec_config(path: Path) -> BrecConfig:
    if not path.exists():
        return BrecConfig()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BrecConfig()

    return BrecConfig.from_dict(data)


def save_brec_config(path: Path, config: BrecConfig) -> BrecConfig:
    config = BrecConfig.from_dict(config.to_dict())
    path.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def resolve_workdir(config: BrecConfig) -> Optional[Path]:
    if not config.workdir.strip():
        return None
    return Path(config.workdir.strip()).expanduser().resolve(strict=False)


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_recording_path(config: BrecConfig, relative_path: str) -> Path:
    workdir = resolve_workdir(config)
    if workdir is None:
        raise BrecIntegrationError("还没有设置录播姬工作目录。")
    if not workdir.exists():
        raise BrecIntegrationError(f"录播姬工作目录不存在: {workdir}")

    candidate = (workdir / relative_path).resolve(strict=False)
    if not path_is_within(candidate, workdir):
        raise BrecIntegrationError("录播路径超出了录播姬工作目录，已拒绝访问。")
    if not candidate.is_file():
        raise BrecIntegrationError(f"找不到录播文件: {candidate}")
    return candidate


def scan_recordings(config: BrecConfig, limit: int = 20, query: str = "") -> list[dict[str, Any]]:
    workdir = resolve_workdir(config)
    if workdir is None or not workdir.exists():
        return []

    keyword = query.casefold().strip()
    items: list[dict[str, Any]] = []

    for path in workdir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_RECORDING_EXTENSIONS:
            continue

        relative_path = path.relative_to(workdir)
        haystack = f"{path.name} {relative_path}".casefold()
        if keyword and keyword not in haystack:
            continue

        stat = path.stat()
        items.append(
            {
                "filename": path.name,
                "title": path.stem,
                "relative_path": relative_path.as_posix(),
                "absolute_path": str(path),
                "directory": relative_path.parent.as_posix() if relative_path.parent != Path(".") else "",
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )

    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return items[: max(1, limit)]


def fetch_api_json(config: BrecConfig, api_path: str, timeout: float = 5.0) -> Any:
    base_url = normalize_api_base_url(config.api_base_url)
    url = urljoin(f"{base_url}/", api_path.lstrip("/"))

    headers = {"Accept": "application/json"}
    if config.api_username or config.api_password:
        token = f"{config.api_username}:{config.api_password}".encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(token).decode('ascii')}"

    request = Request(url=url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read().decode(charset)
    except HTTPError as exc:
        raise BrecIntegrationError(f"录播姬 API 请求失败: HTTP {exc.code}") from exc
    except URLError as exc:
        raise BrecIntegrationError(f"无法连接录播姬 API: {exc.reason}") from exc
    except OSError as exc:
        raise BrecIntegrationError(f"调用录播姬 API 失败: {exc}") from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BrecIntegrationError("录播姬 API 返回的不是有效 JSON。") from exc


def summarize_rooms(raw_rooms: Any) -> list[dict[str, Any]]:
    if isinstance(raw_rooms, dict):
        if isinstance(raw_rooms.get("Rooms"), list):
            raw_rooms = raw_rooms["Rooms"]
        elif isinstance(raw_rooms.get("rooms"), list):
            raw_rooms = raw_rooms["rooms"]

    if not isinstance(raw_rooms, list):
        return []

    items: list[dict[str, Any]] = []
    for room in raw_rooms:
        if not isinstance(room, dict):
            continue
        items.append(
            {
                "room_id": room.get("RoomId") or room.get("roomId") or room.get("Id") or room.get("id"),
                "short_id": room.get("ShortId") or room.get("shortId") or 0,
                "name": room.get("Name") or room.get("name") or "",
                "title": room.get("Title") or room.get("title") or "",
                "recording": bool(room.get("Recording") if "Recording" in room else room.get("recording")),
                "streaming": bool(room.get("Streaming") if "Streaming" in room else room.get("streaming")),
            }
        )
    return items


def build_webhook_url(base_url: str, secret: str) -> str:
    normalized_base = base_url.rstrip("/")
    if secret:
        return f"{normalized_base}/api/brec/webhook?secret={quote(secret)}"
    return f"{normalized_base}/api/brec/webhook"
