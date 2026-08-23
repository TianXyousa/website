from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Optional
from urllib.parse import unquote

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
UPLOAD_WIDTH = 1920
UPLOAD_HEIGHT = 1080
UPLOAD_FPS = 60
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
BILI_RESPONSE_ERROR_PATTERN = re.compile(
    r"code:\s*(-?\d+).*?message:\s*\"([^\"]+)\"",
    re.DOTALL,
)


class BiliUploadError(RuntimeError):
    """Raised when Bilibili upload integration cannot continue."""


def _utf8_subprocess_env() -> dict[str, str]:
    """Keep biliup/ffmpeg diagnostics readable on Windows code-page consoles."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("NO_COLOR", "1")
    return env


@dataclass
class BiliUploadConfig:
    cookie_file: str = ""
    uploader_path: str = "biliup"
    tid: int = DEFAULT_TID
    title_template: str = DEFAULT_TITLE_TEMPLATE
    desc_template: str = DEFAULT_DESC_TEMPLATE
    dynamic_template: str = ""
    tags: str = DEFAULT_TAGS
    copyright: int = 1
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
        payload["copyright"] = 1 if int(self.copyright or 1) == 1 else 2
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


def format_biliup_error_detail(value: str) -> str:
    """Turn biliup/Rust tracebacks into a concise Bilibili response message."""
    cleaned = ANSI_ESCAPE_PATTERN.sub("", str(value or "")).strip()
    response_match = BILI_RESPONSE_ERROR_PATTERN.search(cleaned)
    if response_match:
        code, message = response_match.groups()
        return f"B 站投稿失败（code {code}）：{message}"
    return cleaned


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
    """Locate the biliup CLI.

    Order: explicit path, PATH lookup, then next to the running interpreter
    (pip installs biliup.exe into the venv's Scripts/bin directory, which is
    not on PATH when the app is started via `python -m uvicorn`).
    """
    candidates: list[str] = []
    if explicit_path.strip():
        candidates.append(explicit_path.strip())

    which = shutil.which("biliup")
    if which:
        candidates.append(which)

    exe_dir = Path(sys.executable).resolve().parent
    for name in ("biliup.exe", "biliup"):
        sibling = exe_dir / name
        if sibling.is_file():
            candidates.append(str(sibling))

    # The app is sometimes launched with a parent Conda interpreter while its
    # uploader dependencies live in this project's .venv. Check that location
    # explicitly so a service restart does not make biliup disappear.
    project_dir = Path(__file__).resolve().parent
    venv_bin_dir = project_dir / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    for name in ("biliup.exe", "biliup"):
        project_venv_binary = venv_bin_dir / name
        if project_venv_binary.is_file():
            candidates.append(str(project_venv_binary))

    candidates.append("biliup")

    for candidate in candidates:
        try:
            completed = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_utf8_subprocess_env(),
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
    """Resolve a songcut reference from the API/前端 (URL path or plain path).

    The /api/songcuts list and extract results return URL-quoted paths
    (Chinese category/file names become %XX sequences), so try the raw text
    first and fall back to the percent-decoded form.
    """

    def try_resolve(text: str) -> Optional[Path]:
        relative_text = text.strip().replace("\\", "/").lstrip("/")
        if relative_text.startswith("assets/songcuts/"):
            relative_text = relative_text.replace("assets/songcuts/", "", 1)
        if not relative_text:
            return None
        candidate = (songcut_root / relative_text).resolve(strict=False)
        try:
            candidate.relative_to(songcut_root.resolve(strict=False))
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    for text in (raw_path, unquote(raw_path)):
        candidate = try_resolve(text)
        if candidate is not None:
            return candidate

    decoded = unquote(raw_path)
    if decoded != raw_path:
        attempted = f"{raw_path} 或 {decoded}"
    else:
        attempted = raw_path
    raise BiliUploadError(f"找不到歌切文件: {attempted}")


def prepare_songcut_video_for_upload(
    songcut_path: Path,
    *,
    ffmpeg_path: Optional[str],
    temp_root: Path,
) -> tuple[Path, dict[str, Any], Optional[Path]]:
    metadata = read_segment_metadata(songcut_path) or {}
    if songcut_path.suffix.lower() == ".mp4":
        return songcut_path, metadata, None

    resolved_ffmpeg = find_ffmpeg_binary(ffmpeg_path)
    if not resolved_ffmpeg:
        raise BiliUploadError("没有找到 ffmpeg，暂时无法生成投稿用 MP4。")

    source_path_text = str(metadata.get("source_path", "")).strip()
    source_path = Path(source_path_text) if source_path_text else None
    start = float(metadata.get("start", 0.0) or 0.0)
    end = float(metadata.get("end", 0.0) or 0.0)
    can_reuse_source_video = bool(source_path and source_path.is_file() and end > start)

    work_dir = Path(mkdtemp(prefix="bili-upload-", dir=str(temp_root)))
    output_path = work_dir / f"{songcut_path.stem}.mp4"
    if can_reuse_source_video:
        video_filter = (
            f"scale={UPLOAD_WIDTH}:{UPLOAD_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={UPLOAD_WIDTH}:{UPLOAD_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,fps={UPLOAD_FPS}"
        )
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
            "-vf",
            video_filter,
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level:v",
            "4.2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        conversion_mode = "源录播"
    else:
        # Older/manual cuts may have no durable source_path, or may point at a
        # temporary slice that was already cleaned up. The MP3 itself is still
        # sufficient for a valid Bilibili video, so pair it with a lightweight
        # static background instead of forcing the user to re-extract it.
        command = [
            resolved_ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x111827:s={UPLOAD_WIDTH}x{UPLOAD_HEIGHT}:r={UPLOAD_FPS}",
            "-i",
            str(songcut_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "stillimage",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level:v",
            "4.2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        conversion_mode = "歌切音频"

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_utf8_subprocess_env(),
        check=False,
    )
    if completed.returncode != 0 or not output_path.exists():
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "unknown ffmpeg error"
        shutil.rmtree(work_dir, ignore_errors=True)
        raise BiliUploadError(f"生成投稿视频失败（{conversion_mode}）: {tail}")

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

    copyright_type = 1 if int(config.copyright or 1) == 1 else 2
    if copyright_type == 2 and not config.source.strip():
        raise BiliUploadError("投稿类型为转载时必须填写转载来源；如果是自制，请选择“自制”后重新发布。")

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
            "--submit",
            "web",
            str(upload_video_path),
            "--title",
            title,
            "--tid",
            str(max(1, int(config.tid or DEFAULT_TID))),
            "--desc",
            description,
            "--copyright",
            str(copyright_type),
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

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_utf8_subprocess_env(),
            check=False,
        )
        if completed.returncode != 0:
            detail = format_biliup_error_detail(completed.stderr or completed.stdout or "")
            raise BiliUploadError(detail or "biliup 投稿失败。")

        stdout = (completed.stdout or "").strip()
        stderr = ANSI_ESCAPE_PATTERN.sub("", completed.stderr or "").strip()
        output = "\n".join(part for part in (stdout, stderr) if part)
        return {
            "status": "success",
            "title": title,
            "video_path": str(upload_video_path),
            "songcut_path": str(songcut_path),
            "stdout": stdout,
            "stderr": stderr,
            "output": output,
            "video_profile": f"{UPLOAD_WIDTH}x{UPLOAD_HEIGHT}@{UPLOAD_FPS}fps",
            "used_temp_video": temp_dir is not None,
        }
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


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


def build_songcut_upload_title(songcut_path: Path, config: BiliUploadConfig) -> str:
    """Render the exact title without converting or uploading the songcut."""
    metadata = read_segment_metadata(songcut_path) or {}
    placeholder_video_path = songcut_path.with_suffix(".mp4")
    context = build_upload_context(songcut_path, placeholder_video_path, metadata)
    return trim_bilibili_title(render_template(config.title_template, context) or context["title"])


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
