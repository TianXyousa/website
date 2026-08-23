from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bili_upload_integration import (
    BiliUploadConfig,
    BiliUploadError,
    upload_songcut_video,
)

MEMBER_BASE = "https://member.bilibili.com/x2/creative/web"
SEASONS_URL = f"{MEMBER_BASE}/seasons"
SEASON_ADD_URL = f"{MEMBER_BASE}/season/add"
EPISODES_ADD_URL = f"{MEMBER_BASE}/season/section/episodes/add"
ARC_SEARCH_URL = f"{MEMBER_BASE}/arc/search"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

REQUEST_TIMEOUT_SECONDS = 30
BV_PATTERN = re.compile(r"BV[1-9A-HJ-NP-Za-km-z]{10}")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class BiliSeasonError(RuntimeError):
    """Raised when Bilibili collection (合集) operations cannot continue."""


def parse_cookie_pairs(cookie_file: Path) -> dict[str, str]:
    """Extract name/value pairs from a biliup cookies.json (list or dict form)."""
    try:
        payload = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BiliSeasonError(f"cookies.json 解析失败: {exc}") from exc

    pairs: dict[str, str] = {}

    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("name") and "value" in item:
                pairs[str(item["name"])] = str(item.get("value", ""))
    elif isinstance(payload, dict):
        nested = payload.get("cookie_info")
        if isinstance(nested, dict) and isinstance(nested.get("cookies"), list):
            for item in nested["cookies"]:
                if isinstance(item, dict) and item.get("name") and "value" in item:
                    pairs[str(item["name"])] = str(item.get("value", ""))
        for key, value in payload.items():
            if isinstance(value, str):
                pairs.setdefault(str(key), value)

    if "SESSDATA" not in pairs:
        raise BiliSeasonError("cookies.json 中缺少 SESSDATA，无法访问创作中心接口。")
    return pairs


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _request_json(
    url: str,
    *,
    cookies: Optional[dict[str, str]] = None,
    form: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://member.bilibili.com/platform/upload-manager/season",
    }
    data: Any = None

    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urlencode(form).encode("utf-8")
    elif json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

    if cookies:
        headers["Cookie"] = _cookie_header(cookies)

    request = Request(url, data=data, headers=headers)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise BiliSeasonError(f"B 站接口请求失败 ({url}): {exc}") from exc

    if not isinstance(payload, dict):
        raise BiliSeasonError(f"B 站接口返回了无法解析的内容 ({url})。")
    return payload


def extract_bv_from_text(text: str) -> Optional[str]:
    match = BV_PATTERN.search(text or "")
    return match.group(0) if match else None


def list_seasons(cookie_file: Path) -> list[dict[str, Any]]:
    """List the account's 合集 (collections) with their default section ids."""
    cookies = parse_cookie_pairs(cookie_file)
    seasons: list[dict[str, Any]] = []
    page = 1

    while True:
        payload = _request_json(
            f"{SEASONS_URL}?{urlencode({'pn': page, 'ps': 30, 'order': 'mtime', 'sort': 'desc', 'draft': 0})}",
            cookies=cookies,
        )
        if payload.get("code") != 0:
            raise BiliSeasonError(f"获取合集列表失败: {payload.get('message') or payload}")

        data = payload.get("data") or {}
        items = data.get("seasons") or []
        for item in items:
            season = item.get("season") or {}
            season_id = season.get("id") or item.get("id")
            title = season.get("title") or item.get("title") or ""
            if not season_id:
                continue
            sections = ((item.get("sections") or {}).get("sections")) or []
            section_id = sections[0].get("id") if sections else None
            episodes = item.get("part_episodes") or []
            seasons.append(
                {
                    "id": int(season_id),
                    "title": str(title),
                    "section_id": int(section_id) if section_id else None,
                    "episode_count": len(episodes),
                    "state": season.get("state"),
                }
            )

        if len(items) < 30 or page >= 10:
            break
        page += 1

    return seasons


def ensure_season(
    cookie_file: Path,
    title: str,
    *,
    cover_url: str = "",
    create_if_missing: bool = True,
) -> dict[str, Any]:
    """Find the 合集 titled `title`, creating it when allowed.

    Creating a season requires a cover; callers pass the first uploaded video's
    cover URL so a fresh collection reuses its first episode's artwork.
    """
    normalized_title = " ".join((title or "").split())
    if not normalized_title:
        raise BiliSeasonError("合集标题不能为空。")

    seasons = list_seasons(cookie_file)
    for season in seasons:
        if " ".join(str(season.get("title", "")).split()) == normalized_title:
            if not season.get("section_id"):
                raise BiliSeasonError(
                    f"合集「{normalized_title}」缺少默认小节信息，无法添加视频。"
                )
            return {**season, "created": False}

    if not create_if_missing:
        raise BiliSeasonError(f"没有找到名为「{normalized_title}」的合集。")

    cookies = parse_cookie_pairs(cookie_file)
    csrf = cookies.get("bili_jct", "")
    payload = _request_json(
        SEASON_ADD_URL,
        cookies=cookies,
        form={
            "title": normalized_title,
            "desc": "",
            "cover": cover_url,
            "csrf": csrf,
            "csrf_token": csrf,
        },
    )
    if payload.get("code") != 0:
        raise BiliSeasonError(
            f"创建合集「{normalized_title}」失败: {payload.get('message') or payload}"
        )

    # The create response only carries the new id; re-list to resolve the
    # default section id needed when adding episodes.
    created_id = payload.get("data")
    seasons = list_seasons(cookie_file)
    for season in seasons:
        if (created_id and season["id"] == int(created_id)) or season["title"] == normalized_title:
            if not season.get("section_id"):
                raise BiliSeasonError("新合集缺少默认小节信息，请稍后在创作中心确认。")
            return {**season, "created": True}

    raise BiliSeasonError("合集创建成功但无法定位新合集，请刷新合集列表重试。")


def fetch_video_info(bvid: str) -> dict[str, Any]:
    """Public view API: BV号 -> {aid, cid, title, pic}."""
    payload = _request_json(f"{VIEW_URL}?bvid={bvid}")
    if payload.get("code") != 0:
        raise BiliSeasonError(f"查询视频 {bvid} 信息失败: {payload.get('message') or payload}")
    data = payload.get("data") or {}
    aid = data.get("aid")
    cid = data.get("cid")
    if not aid or not cid:
        raise BiliSeasonError(f"视频 {bvid} 还在审核或信息不全，暂时无法加入合集。")
    return {
        "aid": int(aid),
        "cid": int(cid),
        "title": str(data.get("title", "")),
        "pic": str(data.get("pic", "")),
    }


def find_video_bvid_by_title(cookie_file: Path, title: str) -> Optional[str]:
    """Fallback lookup: search the account's own recent drafts by title."""
    cookies = parse_cookie_pairs(cookie_file)
    payload = _request_json(
        f"{ARC_SEARCH_URL}?{urlencode({'pn': 1, 'ps': 20, 'keyword': title})}",
        cookies=cookies,
    )
    if payload.get("code") != 0:
        return None

    data = payload.get("data") or {}
    entries = data.get("vlist") or data.get("arc_audits") or []
    for entry in entries:
        archive = entry.get("Archive") if isinstance(entry, dict) else None
        candidate = archive or entry
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("title", "")).strip() == str(title).strip():
            bvid = candidate.get("bvid")
            if bvid:
                return str(bvid)
    return None


def add_video_to_season(
    cookie_file: Path,
    *,
    section_id: int,
    aid: int,
    cid: int,
    title: str,
) -> None:
    cookies = parse_cookie_pairs(cookie_file)
    csrf = cookies.get("bili_jct", "")
    payload = _request_json(
        f"{EPISODES_ADD_URL}?csrf={csrf}&csrf_token={csrf}",
        cookies=cookies,
        json_body={
            "section_id": int(section_id),
            "episodes": [
                {
                    "aid": int(aid),
                    "cid": int(cid),
                    "title": str(title),
                    "charging_pay": 0,
                }
            ],
        },
    )
    if payload.get("code") != 0:
        raise BiliSeasonError(f"视频加入合集失败: {payload.get('message') or payload}")


def publish_songcut_collection(
    *,
    songcut_paths: list[Path],
    config: BiliUploadConfig,
    ffmpeg_path: Optional[str],
    temp_root: Path,
    season_title: str = "",
    season_id: Optional[int] = None,
    create_if_missing: bool = True,
    upload_fn: Callable[..., dict[str, Any]] = upload_songcut_video,
) -> dict[str, Any]:
    """Upload songcuts via biliup and gather them into one Bilibili 合集.

    Uploads run first (each returns a BV号 parsed from biliup output, falling
    back to a title search); the collection is then created/reused and every
    video is appended to its default section. Failures are isolated per video
    so one bad cut never aborts the batch.
    """
    if not songcut_paths:
        raise BiliSeasonError("没有选择任何歌切。")

    cookie_path = Path(config.cookie_file).expanduser()
    if not config.cookie_file.strip() or not cookie_path.exists():
        raise BiliUploadError("没有找到 cookies.json，请先在后台上传 B 站登录文件。")

    season: Optional[dict[str, Any]] = None
    if season_id:
        matches = [item for item in list_seasons(cookie_path) if item["id"] == int(season_id)]
        if not matches:
            raise BiliSeasonError(f"找不到指定的合集 (id={season_id})。")
        season = matches[0]
        if not season.get("section_id"):
            raise BiliSeasonError("该合集缺少默认小节信息，无法添加视频。")

    results: list[dict[str, Any]] = []
    uploaded_infos: list[tuple[dict[str, Any], str]] = []
    first_cover = ""

    for songcut_path in songcut_paths:
        entry: dict[str, Any] = {
            "path": str(songcut_path),
            "filename": songcut_path.name,
            "ok": False,
            "uploaded": False,
            "added_to_season": False,
            "error": None,
        }
        try:
            upload_result = upload_fn(
                songcut_path,
                config,
                ffmpeg_path=ffmpeg_path,
                temp_root=temp_root,
            )
            entry["uploaded"] = True
            entry["title"] = upload_result.get("title", songcut_path.stem)

            bvid = (
                extract_bv_from_text(upload_result.get("stdout", ""))
                or extract_bv_from_text(str(upload_result))
                or find_video_bvid_by_title(cookie_path, entry["title"])
            )
            if not bvid:
                entry["error"] = "投稿成功但未能解析 BV号，已跳过合集归档（稍后可在创作中心手动添加）。"
                results.append(entry)
                continue

            info = fetch_video_info(bvid)
            entry["bvid"] = bvid
            entry["aid"] = info["aid"]
            if not first_cover:
                first_cover = info["pic"]
            uploaded_infos.append((info, str(entry["title"])))
            entry["ok"] = True
        except (BiliUploadError, BiliSeasonError) as exc:
            entry["error"] = str(exc)
        except Exception as exc:  # pragma: no cover
            entry["error"] = f"未预期的错误: {exc}"
        results.append(entry)

    if not uploaded_infos:
        return {
            "status": "failed",
            "message": "所有投稿都失败了，未创建/更新合集。",
            "results": results,
            "uploaded_count": 0,
            "season_added_count": 0,
        }

    if season is None:
        if not season_title.strip():
            raise BiliSeasonError("请指定合集标题或选择已有合集。")
        season = ensure_season(
            cookie_path,
            season_title.strip(),
            cover_url=first_cover,
            create_if_missing=create_if_missing,
        )

    added_count = 0
    for info, title in uploaded_infos:
        entry = next(item for item in results if item.get("aid") == info["aid"])
        try:
            # Newly uploaded videos can take a moment before the collection
            # API accepts them; a couple of gentle retries smooths that out.
            last_error: Optional[str] = None
            for attempt in range(3):
                try:
                    add_video_to_season(
                        cookie_path,
                        section_id=season["section_id"],
                        aid=info["aid"],
                        cid=info["cid"],
                        title=title,
                    )
                    last_error = None
                    break
                except BiliSeasonError as exc:
                    last_error = str(exc)
                    time.sleep(3)
            if last_error:
                entry["error"] = f"加入合集失败: {last_error}"
            else:
                entry["added_to_season"] = True
                added_count += 1
        except Exception as exc:  # pragma: no cover
            entry["error"] = f"加入合集失败: {exc}"

    return {
        "status": "success" if added_count else "partial",
        "message": f"投稿 {len(uploaded_infos)} 个，其中 {added_count} 个已加入合集。",
        "season": {"id": season["id"], "title": season["title"], "created": season.get("created", False)},
        "results": results,
        "uploaded_count": len(uploaded_infos),
        "season_added_count": added_count,
    }
