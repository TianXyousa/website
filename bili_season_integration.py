from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bili_upload_integration import (
    BiliUploadConfig,
    BiliUploadError,
    build_songcut_upload_title,
    upload_songcut_video,
)

MEMBER_BASE = "https://member.bilibili.com/x2/creative/web"
SEASONS_URL = f"{MEMBER_BASE}/seasons"
SEASON_ADD_URL = f"{MEMBER_BASE}/season/add"
EPISODES_ADD_URL = f"{MEMBER_BASE}/season/section/episodes/add"
ARCHIVES_URL = "https://member.bilibili.com/x/web/archives"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRY_ATTEMPTS = 3
REQUEST_RETRY_BACKOFF_SECONDS = 0.8
# BVIDs are case-sensitive, but biliup has emitted the prefix in different
# cases across versions.  Keep the payload alphabet strict while accepting a
# lower-case prefix and normalising it back to ``BV`` for API calls.
BV_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])BV[1-9A-HJ-NP-Za-km-z]{10}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
DEFAULT_UPLOAD_COOLDOWN_SECONDS = 60.0
RECENT_UPLOAD_REUSE_SECONDS = 2 * 60 * 60
BVID_LOOKUP_ATTEMPTS = 5
BVID_LOOKUP_BACKOFF_SECONDS = 1.0
RATE_LIMIT_RETRY_GUIDANCE = (
    "请等待 B 站解除限流后，再手动重试当前及标记为“未尝试”的歌曲，避免连续点击投稿。"
)
_RATE_LIMIT_PATTERNS = (
    re.compile(r"\bcode\s*[:=]?\s*21566\b", re.IGNORECASE),
    re.compile(r"\bcode\s*[:=]?\s*601\b", re.IGNORECASE),
)

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
    last_error: Optional[BaseException] = None
    for attempt in range(REQUEST_RETRY_ATTEMPTS):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            # HTTP status codes are real server responses; retrying them tends
            # to repeat authentication/WAF failures and slows the UI down.
            raise BiliSeasonError(f"B 站接口请求失败 ({url}): {exc}") from exc
        except (URLError, OSError, ValueError) as exc:
            last_error = exc
            if attempt + 1 >= REQUEST_RETRY_ATTEMPTS:
                hint = "；B 站可能暂时拒绝连接，请稍后重试或检查代理/网络" if isinstance(exc, (URLError, OSError)) else ""
                raise BiliSeasonError(f"B 站接口请求失败 ({url}): {exc}{hint}") from exc
            time.sleep(REQUEST_RETRY_BACKOFF_SECONDS * (2**attempt))
    else:  # pragma: no cover - defensive guard for future retry changes
        raise BiliSeasonError(f"B 站接口请求失败 ({url}): {last_error}") from last_error

    if not isinstance(payload, dict):
        raise BiliSeasonError(f"B 站接口返回了无法解析的内容 ({url})。")
    return payload


def extract_bv_from_text(text: str) -> Optional[str]:
    match = BV_PATTERN.search(text or "")
    if not match:
        return None
    value = match.group(0)
    return "BV" + value[2:]


def extract_bv_from_value(value: Any) -> Optional[str]:
    """Extract a BVID from biliup's text or structured return payload.

    Recent biliup builds do not always print a clickable URL.  Some versions
    return ``bvid``/``bv_id`` in a JSON-shaped object instead, so checking only
    ``stdout`` silently loses a successful submission.
    """
    if isinstance(value, str):
        return extract_bv_from_text(value)
    if isinstance(value, dict):
        for key in ("bvid", "bv_id", "bv", "video_url", "url", "link"):
            candidate = value.get(key)
            if candidate:
                bvid = extract_bv_from_value(candidate)
                if bvid:
                    return bvid
        for candidate in value.values():
            bvid = extract_bv_from_value(candidate)
            if bvid:
                return bvid
    elif isinstance(value, (list, tuple, set)):
        for candidate in value:
            bvid = extract_bv_from_value(candidate)
            if bvid:
                return bvid
    return None


def is_bili_upload_rate_limit_error(error: Any) -> bool:
    """Recognize both App and Web uploader rate-limit responses."""
    text = str(error or "")
    lowered = text.lower()
    return (
        any(pattern.search(text) for pattern in _RATE_LIMIT_PATTERNS)
        or "投稿过于频繁" in text
        or "上传视频过快" in text
        or "upload rate limit" in lowered
    )


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


def find_video_bvid_by_title(
    cookie_file: Path,
    title: str,
    *,
    newer_than: Optional[int] = None,
) -> Optional[str]:
    """Search the account's archive list for a title and return its BVID.

    ``x/web/archives`` currently returns the useful records under
    ``data.arc_audits[*].Archive``.  The endpoint is eventually consistent
    after a submission, and some records do not carry timestamps, so a missing
    timestamp must not make an otherwise exact match disappear.
    """
    cookies = parse_cookie_pairs(cookie_file)
    normalized_title = _normalize_archive_title(title)

    # A normal account page has fewer than 20 records, but walk additional
    # pages when needed so a delayed publication is not hidden behind older
    # uploads.  Stop as soon as the endpoint reports the final page.
    for page in range(1, 11):
        payload = _request_json(
            f"{ARCHIVES_URL}?{urlencode({'status': 'is_pubing,pubed,not_pubed', 'pn': page, 'ps': 50})}",
            cookies=cookies,
        )
        if payload.get("code") != 0:
            return None

        data = payload.get("data") or {}
        entries = data.get("arc_audits") or data.get("archives") or data.get("vlist") or []
        if isinstance(entries, dict):
            entries = entries.get("list") or entries.get("archives") or []
        if not isinstance(entries, list):
            entries = []

        for entry in entries:
            archive = entry.get("Archive") if isinstance(entry, dict) else None
            candidate = archive if isinstance(archive, dict) else entry
            if not isinstance(candidate, dict):
                continue
            candidate_title = _normalize_archive_title(candidate.get("title", ""))
            if candidate_title != normalized_title:
                continue

            if newer_than is not None:
                timestamps = [candidate.get("ctime"), candidate.get("ptime")]
                valid_timestamps = [
                    int(value)
                    for value in timestamps
                    if str(value or "").strip().isdigit()
                ]
                # A few ``is_pubing`` records omit both timestamps.  Treat
                # that as unknown rather than incorrectly rejecting a match.
                if valid_timestamps and max(valid_timestamps) < int(newer_than):
                    continue

            bvid = extract_bv_from_value(
                candidate.get("bvid")
                or candidate.get("bv_id")
                or candidate.get("bvid_str")
            )
            if bvid:
                return bvid

        page_info = data.get("page") or {}
        total = page_info.get("count") if isinstance(page_info, dict) else None
        if len(entries) < 50 or (total is not None and page * 50 >= int(total)):
            break
    return None


def _normalize_archive_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split()).strip()


def find_video_bvid_after_upload(
    cookie_file: Path,
    title: str,
    *,
    newer_than: Optional[int] = None,
    attempts: int = BVID_LOOKUP_ATTEMPTS,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> Optional[str]:
    """Poll the archives endpoint until a just-submitted BVID is visible."""
    sleep_fn = sleep_fn or time.sleep
    attempts = max(1, int(attempts or 1))
    for attempt in range(attempts):
        try:
            if newer_than is None:
                bvid = find_video_bvid_by_title(cookie_file, title)
            else:
                bvid = find_video_bvid_by_title(
                    cookie_file,
                    title,
                    newer_than=newer_than,
                )
        except BiliSeasonError:
            bvid = None
        if bvid:
            return bvid
        if attempt + 1 < attempts:
            sleep_fn(BVID_LOOKUP_BACKOFF_SECONDS * (2**attempt))
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
        # The current web API uses camelCase ``sectionId`` and accepts the
        # CSRF token both in the query string and JSON body.  ``section_id``
        # is silently ignored by Bilibili and leaves the collection empty.
        f"{EPISODES_ADD_URL}?csrf={csrf}",
        cookies=cookies,
        json_body={
            "sectionId": int(section_id),
            "episodes": [
                {
                    "aid": int(aid),
                    "cid": int(cid),
                    "title": str(title),
                    "charging_pay": 0,
                }
            ],
            "csrf": csrf,
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
    upload_cooldown_seconds: float = DEFAULT_UPLOAD_COOLDOWN_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    reuse_recent_uploads: bool = True,
    recent_upload_reuse_seconds: int = RECENT_UPLOAD_REUSE_SECONDS,
) -> dict[str, Any]:
    """Upload songcuts via biliup and gather them into one Bilibili 合集.

    Uploads run first (each returns a BV号 parsed from biliup output, falling
    back to a title search); the collection is then created/reused and every
    video is appended to its default section. Ordinary failures are isolated
    per video. A Bilibili rate-limit response stops the batch immediately so
    the remaining files can be retried later without making the limit worse.
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
    rate_limited_at: Optional[str] = None
    skipped_count = 0
    reused_count = 0

    for index, songcut_path in enumerate(songcut_paths):
        entry: dict[str, Any] = {
            "path": str(songcut_path),
            "filename": songcut_path.name,
            "ok": False,
            "uploaded": False,
            "added_to_season": False,
            "skipped": False,
            "error": None,
        }
        submission_succeeded = False
        rate_limit_hit = False
        try:
            bvid: Optional[str] = None
            expected_title = build_songcut_upload_title(songcut_path, config)
            if reuse_recent_uploads:
                try:
                    bvid = find_video_bvid_by_title(
                        cookie_path,
                        expected_title,
                        newer_than=int(time.time()) - max(0, int(recent_upload_reuse_seconds)),
                    )
                except BiliSeasonError:
                    # A lookup failure must not prevent a normal upload. The
                    # post-upload output still gets a chance to provide BV号.
                    bvid = None

            if bvid:
                entry["uploaded"] = True
                entry["reused_existing"] = True
                entry["title"] = expected_title
                reused_count += 1
            else:
                upload_result = upload_fn(
                    songcut_path,
                    config,
                    ffmpeg_path=ffmpeg_path,
                    temp_root=temp_root,
                )
                submission_succeeded = True
                entry["uploaded"] = True
                entry["title"] = upload_result.get("title", expected_title)

                bvid = (
                    extract_bv_from_value(upload_result)
                    or find_video_bvid_after_upload(
                        cookie_path,
                        entry["title"],
                        sleep_fn=sleep_fn,
                    )
                )
            if not bvid:
                entry["error"] = "投稿成功但未能解析 BV号，已跳过合集归档（稍后可在创作中心手动添加）。"
            else:
                info = fetch_video_info(bvid)
                entry["bvid"] = bvid
                entry["aid"] = info["aid"]
                if not first_cover:
                    first_cover = info["pic"]
                uploaded_infos.append((info, str(entry["title"])))
                entry["ok"] = True
        except (BiliUploadError, BiliSeasonError) as exc:
            if is_bili_upload_rate_limit_error(exc):
                rate_limit_hit = True
                rate_limited_at = datetime.now().astimezone().isoformat(timespec="seconds")
                entry["rate_limited"] = True
                entry["error"] = "B 站限制了投稿频率，本批次已暂停。"
            else:
                entry["error"] = str(exc)
        except Exception as exc:  # pragma: no cover
            entry["error"] = f"未预期的错误: {exc}"
        results.append(entry)

        if rate_limit_hit:
            remaining_paths = songcut_paths[index + 1 :]
            skipped_count = len(remaining_paths)
            for remaining_path in remaining_paths:
                results.append(
                    {
                        "path": str(remaining_path),
                        "filename": remaining_path.name,
                        "ok": False,
                        "uploaded": False,
                        "added_to_season": False,
                        "skipped": True,
                        "skip_reason": "rate_limit",
                        "error": "检测到 B 站限流，本次未尝试投稿；请稍后重试。",
                    }
                )
            break

        if (
            submission_succeeded
            and index < len(songcut_paths) - 1
            and float(upload_cooldown_seconds or 0) > 0
        ):
            sleep_fn(float(upload_cooldown_seconds))

    if not uploaded_infos:
        if rate_limited_at:
            retry_count = sum(1 for item in results if not item.get("uploaded"))
            return {
                "status": "rate_limited",
                "message": f"B 站触发投稿限流，已停止本批次；{retry_count} 个歌切等待稍后重试。",
                "results": results,
                "uploaded_count": 0,
                "season_added_count": 0,
                "skipped_count": skipped_count,
                "reused_count": reused_count,
                "rate_limited_at": rate_limited_at,
                "retry_guidance": RATE_LIMIT_RETRY_GUIDANCE,
            }
        return {
            "status": "failed",
            "message": "所有投稿都失败了，未创建/更新合集。",
            "results": results,
            "uploaded_count": 0,
            "season_added_count": 0,
            "reused_count": reused_count,
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

    if rate_limited_at:
        retry_count = sum(1 for item in results if not item.get("uploaded"))
        status = "rate_limited"
        message = (
            f"B 站触发投稿限流，已停止本批次；已处理 {len(uploaded_infos)} 个，"
            f"其中 {added_count} 个已加入合集，另有 {retry_count} 个等待稍后重试。"
        )
    else:
        status = "success" if added_count else "partial"
        reuse_text = f"（复用最近已投稿 {reused_count} 个）" if reused_count else ""
        message = f"处理 {len(uploaded_infos)} 个稿件{reuse_text}，其中 {added_count} 个已加入合集。"

    response = {
        "status": status,
        "message": message,
        "season": {"id": season["id"], "title": season["title"], "created": season.get("created", False)},
        "results": results,
        "uploaded_count": len(uploaded_infos),
        "season_added_count": added_count,
        "reused_count": reused_count,
    }
    if rate_limited_at:
        response.update(
            {
                "skipped_count": skipped_count,
                "rate_limited_at": rate_limited_at,
                "retry_guidance": RATE_LIMIT_RETRY_GUIDANCE,
            }
        )
    return response
