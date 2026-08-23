import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bili_season_integration import (
    BiliSeasonError,
    add_video_to_season,
    ensure_season,
    extract_bv_from_text,
    find_video_bvid_by_title,
    list_seasons,
    parse_cookie_pairs,
    publish_songcut_collection,
)
from bili_upload_integration import BiliUploadConfig, BiliUploadError, resolve_songcut_path


def write_cookie_file(tmp: Path, payload) -> Path:
    cookie_path = tmp / "cookies.json"
    cookie_path.write_text(json.dumps(payload), encoding="utf-8")
    return cookie_path


def season_list_payload(*seasons: dict) -> dict:
    return {
        "code": 0,
        "data": {
            "seasons": [
                {
                    "season": {"id": item["id"], "title": item["title"], "state": 0},
                    "sections": {"sections": [{"id": item["section_id"]}]},
                    "part_episodes": [{"aid": index} for index in range(item.get("episodes", 0))],
                }
                for item in seasons
            ]
        },
    }


class ParseCookieTests(unittest.TestCase):
    def test_biliup_list_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = write_cookie_file(
                Path(tmp),
                [
                    {"name": "SESSDATA", "value": "sess-1", "httpOnly": True},
                    {"name": "bili_jct", "value": "csrf-1"},
                ],
            )
            pairs = parse_cookie_pairs(cookie_path)
            self.assertEqual(pairs["SESSDATA"], "sess-1")
            self.assertEqual(pairs["bili_jct"], "csrf-1")

    def test_nested_cookie_info_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = write_cookie_file(
                Path(tmp),
                {
                    "cookie_info": {
                        "cookies": [
                            {"name": "SESSDATA", "value": "sess-2"},
                            {"name": "bili_jct", "value": "csrf-2"},
                        ]
                    }
                },
            )
            pairs = parse_cookie_pairs(cookie_path)
            self.assertEqual(pairs["SESSDATA"], "sess-2")

    def test_missing_sessdata_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = write_cookie_file(Path(tmp), [{"name": "DedeUserID", "value": "1"}])
            with self.assertRaises(BiliSeasonError):
                parse_cookie_pairs(cookie_path)


class ExtractBvTests(unittest.TestCase):
    def test_extracts_from_biliup_output(self):
        text = "上传成功\nhttps://www.bilibili.com/video/BV1GJ411x7h7 感谢使用"
        self.assertEqual(extract_bv_from_text(text), "BV1GJ411x7h7")

    def test_no_bv_returns_none(self):
        self.assertIsNone(extract_bv_from_text("投稿成功，没有返回链接"))
        self.assertIsNone(extract_bv_from_text(""))


class ListSeasonsTests(unittest.TestCase):
    def test_parses_seasons_with_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = write_cookie_file(
                Path(tmp), [{"name": "SESSDATA", "value": "s"}, {"name": "bili_jct", "value": "c"}]
            )
            with patch(
                "bili_season_integration._request_json",
                return_value=season_list_payload(
                    {"id": 7, "title": "八月歌切", "section_id": 70, "episodes": 3}
                ),
            ):
                seasons = list_seasons(cookie_path)
        self.assertEqual(len(seasons), 1)
        self.assertEqual(seasons[0]["id"], 7)
        self.assertEqual(seasons[0]["title"], "八月歌切")
        self.assertEqual(seasons[0]["section_id"], 70)
        self.assertEqual(seasons[0]["episode_count"], 3)


class EnsureSeasonTests(unittest.TestCase):
    def setUp(self):
        import os

        self._tmp = tempfile.TemporaryDirectory()
        self.cookie_path = write_cookie_file(
            Path(self._tmp.name),
            [{"name": "SESSDATA", "value": "s"}, {"name": "bili_jct", "value": "c"}],
        )
        self._env_backup = os.environ.get("BILI_UPLOAD_CONFIG_PATH")
        os.environ.pop("BILI_UPLOAD_CONFIG_PATH", None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_existing_title_reused_without_create(self):
        responses = [season_list_payload({"id": 9, "title": "直播歌切", "section_id": 90})]
        with patch("bili_season_integration._request_json", side_effect=responses) as request:
            season = ensure_season(self.cookie_path, " 直播歌切 ")
        self.assertEqual(season["id"], 9)
        self.assertFalse(season["created"])
        self.assertEqual(request.call_count, 1)  # only the list call, no create

    def test_creates_when_missing(self):
        responses = [
            season_list_payload(),
            {"code": 0, "data": 42},
            season_list_payload({"id": 42, "title": "新合集", "section_id": 420}),
        ]
        with patch("bili_season_integration._request_json", side_effect=responses) as request:
            season = ensure_season(self.cookie_path, "新合集", cover_url="http://x/c.jpg")
        self.assertEqual(season["id"], 42)
        self.assertTrue(season["created"])
        self.assertEqual(request.call_count, 3)
        create_call = request.call_args_list[1]
        self.assertEqual(create_call.args[0], "https://member.bilibili.com/x2/creative/web/season/add")
        self.assertEqual(create_call.kwargs["form"]["title"], "新合集")
        self.assertEqual(create_call.kwargs["form"]["csrf"], "c")
        self.assertEqual(create_call.kwargs["form"]["cover"], "http://x/c.jpg")

    def test_missing_and_no_create_raises(self):
        with patch(
            "bili_season_integration._request_json",
            return_value=season_list_payload(),
        ):
            with self.assertRaises(BiliSeasonError):
                ensure_season(self.cookie_path, "不存在", create_if_missing=False)


class AddVideoTests(unittest.TestCase):
    def test_add_posts_json_with_section_and_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = write_cookie_file(
                Path(tmp),
                [{"name": "SESSDATA", "value": "s"}, {"name": "bili_jct", "value": "c"}],
            )
            with patch(
                "bili_season_integration._request_json", return_value={"code": 0, "data": None}
            ) as request:
                add_video_to_season(
                    cookie_path, section_id=90, aid=123, cid=456, title="夏夜晚风 - 伍佰"
                )
        call = request.call_args
        self.assertIn("csrf=c", call.args[0])
        body = call.kwargs["json_body"]
        self.assertEqual(body["section_id"], 90)
        self.assertEqual(body["episodes"][0]["aid"], 123)
        self.assertEqual(body["episodes"][0]["cid"], 456)
        self.assertEqual(body["episodes"][0]["title"], "夏夜晚风 - 伍佰")


class PublishCollectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cookie_path = write_cookie_file(
            Path(self._tmp.name),
            [{"name": "SESSDATA", "value": "s"}, {"name": "bili_jct", "value": "c"}],
        )
        self.config = BiliUploadConfig(cookie_file=str(self.cookie_path))
        self.paths = [Path(self._tmp.name) / "a.mp3", Path(self._tmp.name) / "b.mp3"]
        for path in self.paths:
            path.write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def _upload_fn(self, songcut_path, config, **kwargs):
        stdout = "投稿成功 https://www.bilibili.com/video/BV1GJ411x7h7"
        if songcut_path.name == "b.mp3":
            raise BiliUploadError("转码失败")
        return {"status": "success", "title": f"标题-{songcut_path.name}", "stdout": stdout}

    def test_publish_uploads_then_adds_to_season(self):
        with patch(
            "bili_season_integration.fetch_video_info",
            return_value={"aid": 1, "cid": 11, "title": "t", "pic": "http://x/1.jpg"},
        ), patch(
            "bili_season_integration.ensure_season",
            return_value={"id": 5, "title": "合集", "section_id": 50, "created": True},
        ) as ensure, patch(
            "bili_season_integration.add_video_to_season", return_value=None
        ) as add:
            result = publish_songcut_collection(
                songcut_paths=self.paths,
                config=self.config,
                ffmpeg_path=None,
                temp_root=Path(self._tmp.name),
                season_title="合集",
                upload_fn=self._upload_fn,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["uploaded_count"], 1)
        self.assertEqual(result["season_added_count"], 1)
        ensure.assert_called_once_with(self.cookie_path, "合集", cover_url="http://x/1.jpg", create_if_missing=True)
        add.assert_called_once()
        self.assertEqual(add.call_args.kwargs["aid"], 1)

        failed = next(item for item in result["results"] if item["filename"] == "b.mp3")
        self.assertFalse(failed["uploaded"])
        self.assertIn("转码失败", failed["error"])

    def test_falls_back_to_title_search_without_bv(self):
        def upload_no_bv(songcut_path, config, **kwargs):
            return {"status": "success", "title": "仅标题", "stdout": "成功但无链接"}

        with patch(
            "bili_season_integration.fetch_video_info",
            return_value={"aid": 2, "cid": 22, "title": "t", "pic": "http://x/2.jpg"},
        ), patch(
            "bili_season_integration.find_video_bvid_by_title", return_value="BV1GJ411x7h7"
        ) as find, patch(
            "bili_season_integration.ensure_season",
            return_value={"id": 5, "title": "合集", "section_id": 50},
        ), patch("bili_season_integration.add_video_to_season"):
            result = publish_songcut_collection(
                songcut_paths=self.paths[:1],
                config=self.config,
                ffmpeg_path=None,
                temp_root=Path(self._tmp.name),
                season_title="合集",
                upload_fn=upload_no_bv,
            )

        find.assert_called_once_with(self.cookie_path, "仅标题")
        self.assertEqual(result["uploaded_count"], 1)

    def test_all_failed_skips_season_work(self):
        def failing_upload(songcut_path, config, **kwargs):
            raise BiliUploadError("不可用")

        with patch("bili_season_integration.ensure_season") as ensure:
            result = publish_songcut_collection(
                songcut_paths=self.paths,
                config=self.config,
                ffmpeg_path=None,
                temp_root=Path(self._tmp.name),
                season_title="合集",
                upload_fn=failing_upload,
            )

        self.assertEqual(result["status"], "failed")
        ensure.assert_not_called()

    def test_existing_season_id_skips_create(self):
        with patch(
            "bili_season_integration.list_seasons",
            return_value=[{"id": 8, "title": "已有", "section_id": 80, "episode_count": 0}],
        ), patch(
            "bili_season_integration.fetch_video_info",
            return_value={"aid": 3, "cid": 33, "title": "t", "pic": ""},
        ), patch("bili_season_integration.ensure_season") as ensure, patch(
            "bili_season_integration.add_video_to_season"
        ):
            result = publish_songcut_collection(
                songcut_paths=self.paths[:1],
                config=self.config,
                ffmpeg_path=None,
                temp_root=Path(self._tmp.name),
                season_id=8,
                upload_fn=self._upload_fn,
            )

        ensure.assert_not_called()
        self.assertEqual(result["season"]["id"], 8)


if __name__ == "__main__":
    unittest.main()


class ResolveSongcutPathTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        target = self.root / "试运行-0823" / "夏夜晚风 - 伍佰 & China Blue.mp3"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        self.target = target

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_url_quoted_chinese_path(self):
        from urllib.parse import quote

        quoted = quote(str(self.target.relative_to(self.root)))
        resolved = resolve_songcut_path(self.root, f"/assets/songcuts/{quoted}")
        self.assertEqual(resolved, self.target.resolve(strict=False))

    def test_resolves_plain_path(self):
        resolved = resolve_songcut_path(self.root, str(self.target.relative_to(self.root)))
        self.assertEqual(resolved, self.target.resolve(strict=False))

    def test_missing_file_lists_both_attempts(self):
        with self.assertRaises(BiliUploadError) as ctx:
            resolve_songcut_path(self.root, "%E4%B8%8D%E5%AD%98%E5%9C%A8.mp3")
        self.assertIn("不存在", str(ctx.exception))
