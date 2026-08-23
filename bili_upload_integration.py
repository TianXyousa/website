from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Optional

from songcut_extractor import (
    extract_date_label,
    find_ffmpeg_binary,
    read_segment_metadata,
)


DEFAULT_TID = 31
DEFAULT_TITLE_TEMPLATE = "{title}"
DEFAULT_DESC_TEMPLATE = (
    "\u76f4\u64ad\u7ffb\u5531\u6b4c\u5207\n"
    "\u5f55\u64ad\u65e5\u671f\uff1a{recording_date}\n"
    "\u65f6\u95f4\u6bb5\uff1a{start_label} - {end_label}\n"
    "\u539f\u5f55\u64ad\uff1a{source_name}"
)
DEFAULT_TAGS = "\u76f4\u64ad\u5207\u7247,\u7ffb\u5531"
DEFAULT_UNKNOWN_DATE = "\u672a\u77e5\u65e5\u671f"
DEFAULT_FALLBACK_TITLE = "\u76f4\u64ad\u7ffb\u5531\u6b4c\u5207"


class BiliUploadError(RuntimeError):
    """Raised when Bilibili upload integration cannot continue."""


@dataclass
class BiliUploadConfig:
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

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "BiliUploadConfig":
        if not isinstance(data, dict):
            return cls()
        allowed = {field.name for field in fields(cls)}
        normalized = {key: value for key, value in data.items() if key in allowed}
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cookie_file"] = self.cookie_file.strip()
        payload["uploader_path"] = self.uploader_path.strip() or "biliup"
        payload["tid"] = max(1, int(self.tid or DEFAULT_TID))
        payload["title_template"] = self.title_template.strip() or DEFAULT_TITLE_TEMPLATE
        payload["desc_template"] = self.desc_template.strip() or cls_default_desc()
        payload["dynamic_template"] = self.dynamic_template.strip()
        payload["tags"] = normalize_tags_text(self.tags)
        payload["copyright"] = 1 if int(self.copyright or 2) == 1 else 2
        payload["source"] = self.source.strip()
        payload["cover_image"] = self.cover_image.strip()
        return payload


def cls_default_desc() -> str:
    return DEFAULT_DESC_TEMPLATE


def normalize_tags_text(value: str) -> str:
    parts: list[str] = []
    for raw in str(value or "").replace("\n", ",").split(","):
        text = raw.strip()
        if text and text not in parts:
            parts.append(text)
    return ",".join(parts)


def load_bili_upload_config(path: Path) -> BiliUploadConfig:
    if not path.exists():
        return BiliUploadConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BiliUploadConfig()
    return BiliUploadConfig.from_dict(data)


def save_bili_upload_config(path: Path, config: BiliUploadConfig) -> BiliUploadConfig:
    path.parent.mkdir(parents=True, exist_ok=True)
    config = BiliUploadConfig.from_dict(config.to_dict())
    path.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def find_biliup_binary(explicit_path: str = "") -> Optional[str]:
    candidates = [explicit_path.strip(), "biliup"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            completed = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        if completed.returncode == 0:
            return candidate
    return None


def describe_bili_upload(config: BiliUploadConfig) -> dict[str, Any]:
    cookie_path = Path(config.cookie_file).expanduser() if config.cookie_file.strip() else None
    resolved_uploader = find_biliup_binary(config.uploader_path)
    return {
        "configured": bool(config.cookie_file.strip()),
        "cookie_file": config.cookie_file.strip(),
        "cookie_exists": bool(cookie_path and cookie_path.exists()),
        "uploader_available": bool(resolved_uploader),
        "uploader_path": resolved_uploader or config.uploader_path or "biliup",
        "tid": int(config.tid or DEFAULT_TID),
        "tags": normalize_tags_text(config.tags),
    }


def save_uploaded_cookie(upload_root: Path, filename: str, content: bytes) -> Path:
    upload_root.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename or "cookies.json").name or "cookies.json"
    if not safe_name.endswith(".json"):
        safe_name = f"{safe_name}.json"
    target_path = upload_root / safe_name
    target_path.write_bytes(content)
    return target_path


def resolve_songcut_path(songcut_root: Path, raw_path: str) -> Path:
    relative_text = raw_path.strip().replace("\\", "/").lstrip("/")
    if relative_text.startswith("assets/songcuts/"):
        relative_text = relative_text.replace("assets/songcuts/", "", 1)
    candidate = (songcut_root / relative_text).resolve(strict=False)
    try:
        candidate.relative_to(songcut_root.resolve(strict=False))
    except ValueError as exc:
        raise BiliUploadError("歌切路径超出了 songcuts 目录。") from exc
    if not candidate.is_file():
        raise BiliUploadError(f"找不到歌切文件: {candidate}")
    return candidate


def prepare_songcut_video_for_upload(
    songcut_path: Path,
    *,
    ffmpeg_path: Optional[str],
    temp_root: Path,
) -> tuple[Path, dict[str, Any], Optional[Path]]:
    metadata = read_segment_metadata(songcut_path) or {}
    if songcut_path.suffix.lower() == ".mp4":
        return songcut_path, metadata, None

    source_path_text = str(metadata.get("source_path", "")).strip()
    if not source_path_text:
        raise BiliUploadError("这段歌切缺少源录播元数据，请重新从录播提取一次再投稿。")

    source_path = Path(source_path_text)
    if not source_path.exists():
        raise BiliUploadError(f"找不到源录播文件: {source_path}")

    resolved_ffmpeg = find_ffmpeg_binary(ffmpeg_path)
    if not resolved_ffmpeg:
        raise BiliUploadError("没有找到 ffmpeg，暂时无法生成投稿用 MP4。")

    start = float(metadata.get("start", 0.0) or 0.0)
    end = float(metadata.get("end", 0.0) or 0.0)
    if end <= start:
        raise BiliUploadError("歌切元数据中的时间范围无效，无法生成投稿视频。")

    work_dir = Path(mkdtemp(prefix="bili-upload-", dir=str(temp_root)))
    output_path = work_dir / f"{songcut_path.stem}.mp4"
    command = [
        resolved_ffmpeg,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(source_path),
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not output_path.exists():
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "unknown ffmpeg error"
        raise BiliUploadError(f"生成投稿视频失败: {tail}")

    return output_path, metadata, work_dir


def upload_songcut_video(
    songcut_path: Path,
    config: BiliUploadConfig,
    *,
    ffmpeg_path: Optional[str],
    temp_root: Path,
) -> dict[str, Any]:
    uploader_binary = find_biliup_binary(config.uploader_path)
    if not uploader_binary:
        raise BiliUploadError("没有找到 biliup，请先安装 uploader 依赖。")

    cookie_path = Path(config.cookie_file).expanduser()
    if not config.cookie_file.strip() or not cookie_path.exists():
        raise BiliUploadError("没有找到 cookies.json，请先在后台上传 B 站登录文件。")

    upload_video_path, metadata, temp_dir = prepare_songcut_video_for_upload(
        songcut_path,
        ffmpeg_path=ffmpeg_path,
        temp_root=temp_root,
    )
    try:
        context = build_upload_context(songcut_path, upload_video_path, metadata)
        title = trim_bilibili_title(render_template(config.title_template, context) or context["title"])
        description = render_template(config.desc_template, context)
        dynamic_text = render_template(config.dynamic_template, context)
        tags_text = normalize_tags_text(config.tags)

        command = [
            uploader_binary,
            "-u",
            str(cookie_path),
            "upload",
            str(upload_video_path),
            "--title",
            title,
            "--tid",
            str(max(1, int(config.tid or DEFAULT_TID))),
            "--desc",
            description,
            "--copyright",
            str(1 if int(config.copyright or 2) == 1 else 2),
        ]
        if tags_text:
            command.extend(["--tag", tags_text])
        if dynamic_text:
            command.extend(["--dynamic", dynamic_text])
        if config.source.strip():
            command.extend(["--source", render_template(config.source, context)])
        if config.cover_image.strip():
            cover_path = Path(config.cover_image).expanduser()
            if cover_path.exists():
                command.extend(["--cover", str(cover_path)])

        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise BiliUploadError(detail or "biliup 投稿失败。")

        return {
            "status": "success",
            "title": title,
            "video_path": str(upload_video_path),
            "songcut_path": str(songcut_path),
            "stdout": (completed.stdout or "").strip(),
            "used_temp_video": temp_dir is not None,
        }
    finally:
        if temp_dir is not None:
            for item in temp_dir.iterdir():
                item.unlink(missing_ok=True)
            temp_dir.rmdir()


def build_upload_context(songcut_path: Path, upload_video_path: Path, metadata: dict[str, Any]) -> dict[str, str]:
    recording_date = extract_date_label(str(metadata.get("source_name", ""))) or extract_date_label(songcut_path.stem) or ""
    title = str(metadata.get("recognition_title", "")).strip() or songcut_path.stem
    artist = str(metadata.get("recognition_artist", "")).strip()
    if artist and title.endswith(f" - {artist}"):
        title = title[: -(len(artist) + 3)]

    start = float(metadata.get("start", 0.0) or 0.0)
    end = float(metadata.get("end", 0.0) or 0.0)
    duration = max(0.0, end - start)
    return {
        "title": title,
        "artist": artist,
        "filename": songcut_path.name,
        "source_name": str(metadata.get("source_name", "")).strip() or songcut_path.name,
        "recording_date": recording_date or DEFAULT_UNKNOWN_DATE,
        "category": str(metadata.get("category", "")).strip(),
        "start_label": format_duration_label(start),
        "end_label": format_duration_label(end),
        "duration_label": format_duration_label(duration),
        "video_filename": upload_video_path.name,
    }


def render_template(template: str, context: dict[str, str]) -> str:
    class SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return ""

    return str(template or "").format_map(SafeDict(context)).strip()


def trim_bilibili_title(value: str) -> str:
    text = " ".join((value or "").split()).strip()
    if not text:
        return DEFAULT_FALLBACK_TITLE
    return text[:80]


def format_duration_label(value: float) -> str:
    total_seconds = max(0, int(round(value)))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
