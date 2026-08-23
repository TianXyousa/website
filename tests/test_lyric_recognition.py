import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from songcut_automation import (
    LyricCandidate,
    build_lyric_queries,
    cjk_lyric_containment,
    find_lyric_match,
    has_enough_lyric_content,
    normalize_lyric_text,
    recognize_song_title_by_lyrics,
    score_lyric_match,
    search_lrclib,
)

LYRIC_ENV_KEYS = (
    "SONGCUT_LYRIC_RECOGNITION_ENABLED",
    "SONGCUT_LYRIC_MIN_MATCH",
    "SONGCUT_LYRIC_MAX_WINDOWS",
    "SONGCUT_LYRIC_WINDOW_SECONDS",
    "SONGCUT_WHISPER_MODEL",
    "SONGCUT_WHISPER_DEVICE",
    "SONGCUT_WHISPER_COMPUTE_TYPE",
    "SONGCUT_LRCLIB_BASE_URL",
    "SONG_RECOGNITION_CACHE_PATH",
)


class LyricEnvTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in LYRIC_ENV_KEYS}
        for key in LYRIC_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class NormalizeTests(LyricEnvTestCase):
    def test_normalization_strips_punctuation_and_case(self):
        self.assertEqual(normalize_lyric_text("Hello, World!"), "hello world")
        self.assertEqual(normalize_lyric_text("夜に駆ける"), "夜に駆ける")

    def test_full_width_converted(self):
        self.assertEqual(normalize_lyric_text("ＬＩＧＨＴ"), "light")


class ScoreTests(LyricEnvTestCase):
    LYRICS_JA = "沈むように溶けてゆくように 二人だけの空が広がる夜に\nさよならも言えずに消えた電車 走り出す窓に垂れる..."
    LYRICS_EN = "I can feel the light shining in the night\nTake my hand and we can dance all night long"

    def test_cjk_transcript_inside_lyrics_scores_high(self):
        self.assertGreaterEqual(score_lyric_match("沈むように溶けてゆくように", self.LYRICS_JA), 0.9)

    def test_cjk_transcript_with_noise_still_matches(self):
        self.assertGreaterEqual(
            score_lyric_match("沈むように 溶けてゆくように です", self.LYRICS_JA), 0.7
        )

    def test_unrelated_cjk_scores_low(self):
        self.assertLess(score_lyric_match("今日はいい天気ですね散歩に行きます", self.LYRICS_JA), 0.4)

    def test_english_word_containment(self):
        self.assertGreaterEqual(score_lyric_match("feel the light shining in the night", self.LYRICS_EN), 0.8)
        self.assertLess(score_lyric_match("cooking dinner for the family tonight", self.LYRICS_EN), 0.4)

    def test_sliding_similarity_short_transcript(self):
        self.assertGreaterEqual(cjk_lyric_containment("さよならも言えずに", self.LYRICS_JA), 0.8)


class ContentGateTests(LyricEnvTestCase):
    def test_short_text_rejected(self):
        self.assertFalse(has_enough_lyric_content("hello world"))
        self.assertFalse(has_enough_lyric_content("短い"))

    def test_long_texts_accepted(self):
        self.assertTrue(has_enough_lyric_content("one two three four five six seven eight"))
        self.assertTrue(has_enough_lyric_content("夜に駆けるスピードで誰も止められない未来的な音乐"))


class BuildLyricQueriesTests(LyricEnvTestCase):
    def test_hallucination_brackets_stripped(self):
        # Regression (夏夜晚风 case): instrumental windows make Whisper emit
        # bracketed hallucinations like ["Piano Concerto..."] that must never
        # become search queries.
        transcript = '["Piano Concerto in D minor, Op. 3"] 燈火旋著雨波 隨著你的呼吸一動 你說你笑如夢'
        queries = build_lyric_queries(transcript)
        self.assertTrue(queries)
        for query in queries:
            self.assertNotIn("Piano", query)
            self.assertNotIn("[", query)
        self.assertTrue(any("燈火" in query for query in queries))

    def test_cjk_transcript_drops_latin_fragments(self):
        queries = build_lyric_queries("说话内容这里有一段中文歌词，随风把帆吹动，Piano Concerto in D minor Op. 3")
        self.assertTrue(queries)
        for query in queries:
            self.assertNotIn("Piano", query)
            self.assertNotIn("Concerto", query)

    def test_full_width_brackets_stripped(self):
        queries = build_lyric_queries("【字幕by索兰娅】讓你在我耳邊細語夏夜晚風的愛")
        self.assertTrue(queries)
        self.assertNotIn("字幕", queries[0])


class FindLyricMatchTests(LyricEnvTestCase):
    def test_picks_containing_candidate(self):
        candidates = [
            LyricCandidate("別の歌", "誰か", "全然違う歌詞がここにある"),
            LyricCandidate("夜に駆ける", "YOASOBI", "沈むように溶けてゆくように 二人だけの空が広がる夜に"),
        ]
        with patch("songcut_automation.search_lyric_candidates", return_value=candidates), patch(
            "songcut_automation.find_popular_artist_for_title", return_value=None
        ):
            result = find_lyric_match(["沈むように溶けてゆくように"])
        self.assertIsNotNone(result)
        title, artist, score, windows = result
        self.assertEqual(title, "夜に駆ける")
        self.assertEqual(artist, "YOASOBI")
        self.assertGreaterEqual(score, 0.55)
        self.assertEqual(windows, 1)

    def test_rejects_when_nothing_matches(self):
        candidates = [LyricCandidate("別の歌", "誰か", "全然違う歌詞")]
        with patch("songcut_automation.search_lyric_candidates", return_value=candidates):
            self.assertIsNone(find_lyric_match(["まったく関係のない話をしています"]))

    def test_multiple_windows_boost_winner(self):
        candidates = [
            LyricCandidate("夜に駆ける", "YOASOBI", "沈むように溶けてゆくように 二人だけの空が広がる夜に さよならも言えずに"),
            LyricCandidate("その他", "他者", "別の歌詞"),
        ]
        with patch("songcut_automation.search_lyric_candidates", return_value=candidates), patch(
            "songcut_automation.find_popular_artist_for_title", return_value=None
        ):
            result = find_lyric_match(["沈むように溶けてゆくように", "さよならも言えずに"])
        self.assertIsNotNone(result)
        title, _artist, _score, windows = result
        self.assertEqual(title, "夜に駆ける")
        self.assertEqual(windows, 2)

    def test_cover_version_resolved_to_popular_artist(self):
        candidates = [
            LyricCandidate("发如雪（R&B）", "裤裤", "你若撒野 今生我把酒奉陪"),
        ]
        with patch("songcut_automation.search_lyric_candidates", return_value=candidates), patch(
            "songcut_automation.find_popular_artist_for_title", return_value="周杰伦"
        ) as refine:
            result = find_lyric_match(["你若撒野 今生我把酒奉陪"])
        self.assertIsNotNone(result)
        title, artist, _score, _windows = result
        self.assertEqual(title, "发如雪")
        self.assertEqual(artist, "周杰伦")
        refine.assert_called_once_with("发如雪")


class RecognizeByLyricsTests(LyricEnvTestCase):
    def test_end_to_end_with_mocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment = Path(tmp) / "seg.mp3"
            segment.write_bytes(b"fake audio")
            os.environ["SONG_RECOGNITION_CACHE_PATH"] = str(Path(tmp) / "cache.json")

            with patch(
                "songcut_automation.collect_lyric_transcripts",
                return_value=["沈むように溶けてゆくように"],
            ), patch(
                "songcut_automation.search_lyric_candidates",
                return_value=[LyricCandidate("夜に駆ける", "YOASOBI", "沈むように溶けてゆくように 二人だけの空が広がる夜に")],
            ), patch(
                "songcut_automation.find_popular_artist_for_title",
                return_value=None,
            ):
                recognition = recognize_song_title_by_lyrics(segment, duration=200.0)
            self.assertIsNotNone(recognition)
            self.assertEqual(recognition.title, "夜に駆ける")
            self.assertEqual(recognition.provider, "lyrics")
            self.assertGreaterEqual(recognition.confidence, 0.68)

    def test_disabled_via_env(self):
        os.environ["SONGCUT_LYRIC_RECOGNITION_ENABLED"] = "false"
        self.assertIsNone(recognize_song_title_by_lyrics(Path("x.mp3"), 200.0))

    def test_empty_transcripts_return_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            segment = Path(tmp) / "seg.mp3"
            segment.write_bytes(b"fake audio")
            os.environ["SONG_RECOGNITION_CACHE_PATH"] = str(Path(tmp) / "cache.json")
            with patch("songcut_automation.collect_lyric_transcripts", return_value=[]):
                self.assertIsNone(recognize_song_title_by_lyrics(segment, 200.0))


if __name__ == "__main__":
    unittest.main()
