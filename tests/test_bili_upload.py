import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bili_upload_integration import (
    BiliUploadConfig,
    BiliUploadError,
    _utf8_subprocess_env,
    format_biliup_error_detail,
    prepare_songcut_video_for_upload,
    upload_songcut_video,
)


class SubprocessEnvironmentTests(unittest.TestCase):
    def test_forces_utf8_for_windows_cli_output(self):
        env = _utf8_subprocess_env()

        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["NO_COLOR"], "1")


class BiliupErrorFormattingTests(unittest.TestCase):
    def test_extracts_code_and_message_from_colored_traceback(self):
        traceback = (
            '\x1b[1mResponseData { code: 21021, data: None, '
            'message: "稿件类型为转载时，转载来源不能为空" }\x1b[22m'
        )

        self.assertEqual(
            format_biliup_error_detail(traceback),
            "B 站投稿失败（code 21021）：稿件类型为转载时，转载来源不能为空",
        )


class PrepareSongcutVideoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.songcut = self.root / "song.mp3"
        self.songcut.write_bytes(b"audio")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _successful_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b"video")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    @patch("bili_upload_integration.subprocess.run")
    @patch("bili_upload_integration.find_ffmpeg_binary", return_value="ffmpeg")
    @patch("bili_upload_integration.read_segment_metadata", return_value={})
    def test_missing_source_metadata_uses_songcut_audio(
        self,
        _metadata,
        _find_ffmpeg,
        run,
    ):
        run.side_effect = self._successful_ffmpeg

        output, metadata, work_dir = prepare_songcut_video_for_upload(
            self.songcut,
            ffmpeg_path=None,
            temp_root=self.root,
        )

        command = run.call_args.args[0]
        self.assertEqual(metadata, {})
        self.assertTrue(output.exists())
        self.assertIn("lavfi", command)
        self.assertIn("color=c=0x111827:s=1920x1080:r=60", command)
        self.assertIn(str(self.songcut), command)
        self.assertIn("stillimage", command)
        self.assertIsNotNone(work_dir)
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    @patch("bili_upload_integration.subprocess.run")
    @patch("bili_upload_integration.find_ffmpeg_binary", return_value="ffmpeg")
    @patch("bili_upload_integration.read_segment_metadata")
    def test_deleted_source_file_uses_songcut_audio(
        self,
        metadata,
        _find_ffmpeg,
        run,
    ):
        metadata.return_value = {
            "source_path": str(self.root / "deleted.mp4"),
            "start": 10,
            "end": 20,
        }
        run.side_effect = self._successful_ffmpeg

        prepare_songcut_video_for_upload(
            self.songcut,
            ffmpeg_path=None,
            temp_root=self.root,
        )

        command = run.call_args.args[0]
        self.assertIn("lavfi", command)
        self.assertNotIn(str(self.root / "deleted.mp4"), command)

    @patch("bili_upload_integration.subprocess.run")
    @patch("bili_upload_integration.find_ffmpeg_binary", return_value="ffmpeg")
    @patch("bili_upload_integration.read_segment_metadata")
    def test_existing_source_video_preserves_original_clip(
        self,
        metadata,
        _find_ffmpeg,
        run,
    ):
        source = self.root / "recording.mp4"
        source.write_bytes(b"recording")
        metadata.return_value = {
            "source_path": str(source),
            "start": 10,
            "end": 20,
        }
        run.side_effect = self._successful_ffmpeg

        prepare_songcut_video_for_upload(
            self.songcut,
            ffmpeg_path=None,
            temp_root=self.root,
        )

        command = run.call_args.args[0]
        self.assertIn(str(source), command)
        self.assertIn("10.000", command)
        self.assertIn("20.000", command)
        self.assertNotIn("lavfi", command)
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("scale=1920:1080", video_filter)
        self.assertIn("pad=1920:1080", video_filter)
        self.assertIn("fps=60", video_filter)
        self.assertEqual(command[command.index("-level:v") + 1], "4.2")

    @patch("bili_upload_integration.find_ffmpeg_binary", return_value=None)
    @patch("bili_upload_integration.read_segment_metadata", return_value={})
    def test_ffmpeg_is_still_required(self, _metadata, _find_ffmpeg):
        with self.assertRaisesRegex(BiliUploadError, "ffmpeg"):
            prepare_songcut_video_for_upload(
                self.songcut,
                ffmpeg_path=None,
                temp_root=self.root,
            )


class UploadSongcutVideoTests(unittest.TestCase):
    @patch("bili_upload_integration.subprocess.run")
    @patch("bili_upload_integration.prepare_songcut_video_for_upload")
    @patch("bili_upload_integration.find_biliup_binary", return_value="biliup")
    def test_uses_web_submission_api(self, _find_biliup, prepare, run):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookie = root / "cookies.json"
            cookie.write_text("[]", encoding="utf-8")
            songcut = root / "song.mp3"
            songcut.write_bytes(b"audio")
            video = root / "song.mp4"
            video.write_bytes(b"video")
            prepare.return_value = (video, {}, None)
            run.return_value = subprocess.CompletedProcess(
                [],
                0,
                stdout="ok",
                stderr='ResponseData { code: 0, data: Some({"bvid":"BV1GJ411x7h7"}) }',
            )

            result = upload_songcut_video(
                songcut,
                BiliUploadConfig(cookie_file=str(cookie)),
                ffmpeg_path=None,
                temp_root=root,
            )

        command = run.call_args.args[0]
        submit_index = command.index("--submit")
        self.assertEqual(command[submit_index + 1], "web")
        copyright_index = command.index("--copyright")
        self.assertEqual(command[copyright_index + 1], "1")
        self.assertIn("BV1GJ411x7h7", result["output"])

    @patch("bili_upload_integration.prepare_songcut_video_for_upload")
    @patch("bili_upload_integration.find_biliup_binary", return_value="biliup")
    def test_repost_without_source_is_rejected_before_conversion(self, _find_biliup, prepare):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cookie = root / "cookies.json"
            cookie.write_text("[]", encoding="utf-8")
            songcut = root / "song.mp3"
            songcut.write_bytes(b"audio")

            with self.assertRaisesRegex(BiliUploadError, "转载来源"):
                upload_songcut_video(
                    songcut,
                    BiliUploadConfig(cookie_file=str(cookie), copyright=2, source=""),
                    ffmpeg_path=None,
                    temp_root=root,
                )

        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
