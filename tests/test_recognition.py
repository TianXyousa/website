import os
import tempfile
import unittest
from pathlib import Path

from songcut_automation import (
    SampleMatch,
    SongRecognition,
    build_retry_sample_offsets,
    choose_recognition_result,
    compute_aligned_boundaries,
    lookup_cached_recognition,
    normalize_recognition_key,
    store_cached_recognition,
)

RECOGNITION_ENV_KEYS = (
    "ACRCLOUD_MIN_CONFIDENCE",
    "ACRCLOUD_HIGH_CONFIDENCE",
    "ACRCLOUD_MIN_CONFIRMATIONS",
    "ACRCLOUD_RETRY_ENABLED",
    "ACRCLOUD_RETRY_MAX_SAMPLES",
    "ACRCLOUD_RETRY_GRID_SECONDS",
    "ACRCLOUD_SAMPLE_MIN_DISTANCE_SECONDS",
    "SONGCUT_BOUNDARY_ALIGNMENT_ENABLED",
    "SONGCUT_ALIGNMENT_MIN_SHIFT_SECONDS",
    "SONGCUT_ALIGNMENT_TOLERANCE_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_EXTEND_START_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_EXTEND_END_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_SHRINK_SECONDS",
    "SONGCUT_ALIGNMENT_MIN_TRACK_DURATION_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_TRACK_DURATION_SECONDS",
)


class RecognitionEnvTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in RECOGNITION_ENV_KEYS}
        for key in RECOGNITION_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class NormalizeRecognitionKeyTests(RecognitionEnvTestCase):
    def test_case_and_spacing(self):
        self.assertEqual(
            normalize_recognition_key("Lemon", "米津玄師"),
            normalize_recognition_key("LEMON", "米津 玄師"),
        )

    def test_live_and_bracket_suffixes(self):
        self.assertEqual(
            normalize_recognition_key("花に亡霊", "ヨルシカ"),
            normalize_recognition_key("花に亡霊 (Live)", "ヨルシカ"),
        )

    def test_full_width_brackets(self):
        self.assertEqual(
            normalize_recognition_key("アイネクライネ", "米津玄師"),
            normalize_recognition_key("アイネクライネ（Live）", "米津玄師"),
        )

    def test_different_songs_stay_distinct(self):
        self.assertNotEqual(
            normalize_recognition_key("Lemon", "米津玄師"),
            normalize_recognition_key("Flamingo", "米津玄師"),
        )


class ChooseRecognitionResultTests(RecognitionEnvTestCase):
    def test_artistless_samples_merge_into_confirmed_group(self):
        # ACRCloud can return the same track with/without artist; both must count.
        matches = [
            SampleMatch(offset=20, title="Lemon", artist="米津玄師", confidence=0.90),
            SampleMatch(offset=80, title="LEMON (Live)", artist="", confidence=0.88),
        ]
        result = choose_recognition_result(matches)
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Lemon")
        self.assertEqual(result.artist, "米津玄師")
        self.assertEqual(result.matched_samples, 2)
        self.assertEqual(len(result.samples), 2)

    def test_conflicting_songs_pick_most_confirmed(self):
        matches = [
            SampleMatch(offset=20, title="Song A", artist="Artist A", confidence=0.93),
            SampleMatch(offset=80, title="Song B", artist="Artist B", confidence=0.85),
            SampleMatch(offset=140, title="Song B", artist="Artist B", confidence=0.86),
        ]
        result = choose_recognition_result(matches)
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Song B")
        self.assertEqual(result.matched_samples, 2)

    def test_single_high_confidence_sample_accepted(self):
        matches = [SampleMatch(offset=20, title="Song A", artist="Artist A", confidence=0.95)]
        result = choose_recognition_result(matches)
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Song A")

    def test_single_weak_sample_rejected(self):
        matches = [SampleMatch(offset=20, title="Song A", artist="Artist A", confidence=0.70)]
        self.assertIsNone(choose_recognition_result(matches))

    def test_two_artist_variants_of_different_songs_not_merged(self):
        # Two distinct artists for the same title must stay separate groups.
        matches = [
            SampleMatch(offset=20, title="Lemon", artist="米津玄師", confidence=0.90),
            SampleMatch(offset=80, title="Lemon", artist="Someone Else", confidence=0.88),
        ]
        self.assertIsNone(choose_recognition_result(matches))


class BuildRetrySampleOffsetsTests(RecognitionEnvTestCase):
    def test_covers_unused_positions(self):
        used = [18.0, 84.0, 135.0, 186.0, 234.0, 264.0]
        offsets = build_retry_sample_offsets(
            total_duration=300.0,
            sample_duration=12.0,
            used_offsets=used,
            grid_seconds=45.0,
            max_samples=6,
        )
        self.assertTrue(offsets)
        self.assertLessEqual(len(offsets), 6)
        for offset in offsets:
            self.assertTrue(
                all(abs(offset - used_offset) >= 6.0 for used_offset in used),
                f"retry offset {offset} overlaps a used offset",
            )

    def test_short_segment_has_no_retry(self):
        self.assertEqual(
            build_retry_sample_offsets(
                total_duration=10.0,
                sample_duration=12.0,
                used_offsets=[0.0],
            ),
            [],
        )

    def test_respects_grid_and_distance(self):
        offsets = build_retry_sample_offsets(
            total_duration=200.0,
            sample_duration=12.0,
            used_offsets=[100.0],
            grid_seconds=45.0,
            max_samples=6,
        )
        self.assertEqual(offsets, [0.0, 45.0, 90.0, 135.0, 180.0])


class ComputeAlignedBoundariesTests(RecognitionEnvTestCase):
    def _recognition(self, samples: list[SampleMatch]) -> SongRecognition:
        return SongRecognition(
            title="Lemon",
            artist="米津玄師",
            confidence=0.9,
            samples=samples,
        )

    def test_intro_and_outro_extended_from_fingerprint_offsets(self):
        # Segment covers 100-260s. Sample at segment-relative 30s matched 60s
        # into a 240s track: the song actually starts at absolute 70s and ends
        # at 310s, so both boundaries should be extended.
        samples = [
            SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=60000, duration_ms=240000),
            SampleMatch(offset=90.0, title="Lemon", confidence=0.90, play_offset_ms=120000, duration_ms=240000),
            SampleMatch(offset=150.0, title="Lemon", confidence=0.88, play_offset_ms=180000, duration_ms=240000),
        ]
        alignment = compute_aligned_boundaries(
            100.0,
            260.0,
            self._recognition(samples),
            source_total_duration=400.0,
        )
        self.assertIsNotNone(alignment)
        self.assertAlmostEqual(alignment.start, 70.0)
        self.assertAlmostEqual(alignment.end, 290.0)  # end extension capped at 30s
        self.assertAlmostEqual(alignment.shift_start, -30.0)
        self.assertAlmostEqual(alignment.shift_end, 30.0)

    def test_inconsistent_samples_rejected(self):
        samples = [
            SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=60000, duration_ms=240000),
            SampleMatch(offset=90.0, title="Lemon", confidence=0.90, play_offset_ms=200000, duration_ms=240000),
        ]
        alignment = compute_aligned_boundaries(
            100.0,
            260.0,
            self._recognition(samples),
        )
        self.assertIsNone(alignment)

    def test_needs_at_least_two_usable_samples(self):
        samples = [
            SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=60000, duration_ms=240000),
        ]
        self.assertIsNone(
            compute_aligned_boundaries(100.0, 260.0, self._recognition(samples))
        )

    def test_implausible_track_duration_ignored(self):
        samples = [
            SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=5000, duration_ms=10000),
            SampleMatch(offset=90.0, title="Lemon", confidence=0.90, play_offset_ms=20000, duration_ms=10000),
        ]
        self.assertIsNone(
            compute_aligned_boundaries(100.0, 260.0, self._recognition(samples))
        )

    def test_disabled_via_env(self):
        os.environ["SONGCUT_BOUNDARY_ALIGNMENT_ENABLED"] = "false"
        samples = [
            SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=60000, duration_ms=240000),
            SampleMatch(offset=90.0, title="Lemon", confidence=0.90, play_offset_ms=120000, duration_ms=240000),
        ]
        self.assertIsNone(
            compute_aligned_boundaries(100.0, 260.0, self._recognition(samples))
        )

    def test_small_shift_below_threshold_ignored(self):
        # Sample math lands within 2s of the current boundaries: no re-cut needed.
        samples = [
            SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=29000, duration_ms=159000),
            SampleMatch(offset=90.0, title="Lemon", confidence=0.90, play_offset_ms=89000, duration_ms=159000),
        ]
        self.assertIsNone(
            compute_aligned_boundaries(100.0, 260.0, self._recognition(samples))
        )


class CacheTests(RecognitionEnvTestCase):
    def test_roundtrip_with_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            recognition = SongRecognition(
                title="Lemon",
                artist="米津玄師",
                confidence=0.9,
                samples=[SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=60000, duration_ms=240000)],
                all_matches=[SampleMatch(offset=30.0, title="Lemon", confidence=0.92, play_offset_ms=60000, duration_ms=240000)],
            )
            store_cached_recognition(cache_path, "key1", Path("seg.mp3"), recognition)
            loaded = lookup_cached_recognition(cache_path, "key1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.title, "Lemon")
            self.assertEqual(len(loaded.samples), 1)
            self.assertEqual(loaded.samples[0].play_offset_ms, 60000)
            self.assertEqual(len(loaded.all_matches), 1)

    def test_legacy_entry_without_samples_is_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            store_cached_recognition(
                cache_path, "key1", Path("seg.mp3"), SongRecognition(title="Lemon", confidence=0.9)
            )
            # Rewrite as a legacy entry by dropping the new fields.
            import json

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            for key in ("samples", "all_samples"):
                payload["items"]["key1"].pop(key, None)
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(lookup_cached_recognition(cache_path, "key1"))


if __name__ == "__main__":
    unittest.main()
