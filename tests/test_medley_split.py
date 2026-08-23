import os
import unittest

from songcut_automation import (
    SampleMatch,
    SongRecognition,
    compute_aligned_boundaries,
    split_medley_intervals,
)

SPLIT_ENV_KEYS = (
    "SONGCUT_BOUNDARY_ALIGNMENT_ENABLED",
    "SONGCUT_ALIGNMENT_TOLERANCE_SECONDS",
    "SONGCUT_ALIGNMENT_MIN_TRACK_DURATION_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_TRACK_DURATION_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_EXTEND_START_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_EXTEND_END_SECONDS",
    "SONGCUT_ALIGNMENT_MAX_SHRINK_SECONDS",
    "SONGCUT_ALIGNMENT_MIN_SHIFT_SECONDS",
    "ACRCLOUD_MIN_CONFIRMATIONS",
)


class MedleyEnvTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in SPLIT_ENV_KEYS}
        for key in SPLIT_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def match(offset: float, title: str, play_offset_s: float, track_s: float, confidence: float = 0.9) -> SampleMatch:
    return SampleMatch(
        offset=offset,
        title=title,
        artist="歌手",
        confidence=confidence,
        play_offset_ms=int(play_offset_s * 1000),
        duration_ms=int(track_s * 1000),
    )


class SplitMedleyIntervalsTests(MedleyEnvTestCase):
    def test_two_songs_inside_one_block(self):
        # Block 100-900s. Song A lives at ~120-360, song B at ~500-740.
        matches = [
            match(50, "歌A", 30, 240),
            match(150, "歌A", 130, 240),
            match(250, "歌A", 230, 240),
            match(430, "歌B", 30, 240),
            match(530, "歌B", 130, 240),
            match(630, "歌B", 230, 240),
        ]
        intervals = split_medley_intervals(100.0, 900.0, matches)
        self.assertEqual(len(intervals), 2)
        self.assertAlmostEqual(intervals[0].start, 120.0)
        self.assertAlmostEqual(intervals[0].end, 360.0)
        self.assertEqual(intervals[0].title, "歌A")
        self.assertAlmostEqual(intervals[1].start, 500.0)
        self.assertAlmostEqual(intervals[1].end, 740.0)
        self.assertEqual(intervals[1].title, "歌B")

    def test_single_song_cluster_inside_long_block(self):
        # A 13-minute block that contains one recognized 4-minute song.
        matches = [
            match(100, "Heatstroke", 40, 230),
            match(200, "Heatstroke", 140, 230),
            match(280, "Heatstroke", 220, 230),
        ]
        intervals = split_medley_intervals(1835.0, 2639.0, matches)
        self.assertEqual(len(intervals), 1)
        self.assertAlmostEqual(intervals[0].start, 1895.0, places=1)
        self.assertAlmostEqual(intervals[0].end, 2125.0, places=1)

    def test_single_occurrence_matches_are_ignored(self):
        # One stray match cannot define a song interval on its own.
        matches = [match(100, "歌A", 40, 230)]
        self.assertEqual(split_medley_intervals(100.0, 900.0, matches), [])

    def test_same_song_played_twice_yields_two_intervals(self):
        matches = [
            match(50, "歌A", 30, 240),
            match(150, "歌A", 130, 240),
            match(450, "歌A", 30, 240),
            match(550, "歌A", 130, 240),
        ]
        intervals = split_medley_intervals(100.0, 900.0, matches)
        self.assertEqual(len(intervals), 2)

    def test_overlapping_different_songs_keep_better_supported(self):
        # Song B's implied interval overlaps song A's but has fewer samples.
        matches = [
            match(50, "歌A", 30, 240),
            match(150, "歌A", 130, 240),
            match(250, "歌A", 230, 240),
            match(60, "歌B", 10, 240),
            match(90, "歌B", 40, 240),
        ]
        intervals = split_medley_intervals(100.0, 900.0, matches)
        titles = [interval.title for interval in intervals]
        self.assertIn("歌A", titles)
        self.assertNotIn("歌B", titles)

    def test_short_implausible_tracks_ignored(self):
        matches = [
            match(50, "广告BGM", 10, 15),
            match(150, "广告BGM", 20, 15),
        ]
        self.assertEqual(split_medley_intervals(100.0, 900.0, matches), [])


class OversizedAlignmentGuardTests(MedleyEnvTestCase):
    def test_alignment_skipped_for_multisong_block(self):
        # 800s cut vs 230s track: boundary alignment must not fire.
        samples = [
            match(100, "Heatstroke", 40, 230),
            match(200, "Heatstroke", 140, 230),
        ]
        recognition = SongRecognition(title="Heatstroke", confidence=0.9, samples=samples)
        self.assertIsNone(compute_aligned_boundaries(1835.0, 2639.0, recognition))

    def test_alignment_still_applies_for_single_song_cut(self):
        # 275s cut vs 222s track: just over one song, alignment allowed.
        samples = [
            match(30, "ヒッチコック", 32, 222),
            match(90, "ヒッチコック", 92, 222),
        ]
        recognition = SongRecognition(title="ヒッチコック", confidence=0.9, samples=samples)
        alignment = compute_aligned_boundaries(818.0, 1093.0, recognition)
        self.assertIsNotNone(alignment)
        self.assertAlmostEqual(alignment.start, 816.0)
        # Implied end 1038 needs a 55s trim; live covers run long, so only a
        # small end trim (10s) is allowed to keep the outro intact.
        self.assertAlmostEqual(alignment.end, 1083.0)

    def test_end_extension_still_captures_missing_outro(self):
        # The cut ends 28s before the track does: extend to recover the outro.
        samples = [
            match(30, "Song", 32, 222),
            match(90, "Song", 92, 222),
        ]
        recognition = SongRecognition(title="Song", confidence=0.9, samples=samples)
        alignment = compute_aligned_boundaries(816.0, 1008.0, recognition)
        self.assertIsNotNone(alignment)
        self.assertAlmostEqual(alignment.start, 814.0)
        self.assertAlmostEqual(alignment.end, 1036.0)


if __name__ == "__main__":
    unittest.main()
