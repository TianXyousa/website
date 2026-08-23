import os
import unittest

from gpu_songcut_extractor import (
    _build_processing_ranges,
    _build_segments_from_segmentation,
    _resolve_chunk_overlaps,
)
from songcut_extractor import ExtractionOptions

GPU_ENV_KEYS = (
    "SONGCUT_INASEG_BATCH_SIZE",
    "SONGCUT_INASEG_ENERGY_RATIO",
    "SONGCUT_INASEG_CHUNK_SECONDS",
    "SONGCUT_INASEG_CHUNK_OVERLAP_SECONDS",
    "SONGCUT_INASEG_MIN_MUSIC_RATIO",
    "SONGCUT_INASEG_NOENERGY_BRIDGE_SECONDS",
    "SONGCUT_BOUNDARY_REFINEMENT_ENABLED",
)


class GpuSegmentationEnvTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in GPU_ENV_KEYS}
        for key in GPU_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class BuildProcessingRangesTests(GpuSegmentationEnvTestCase):
    def test_single_range_when_short_enough(self):
        self.assertEqual(_build_processing_ranges(700, 800, 90), [(None, None)])

    def test_ranges_cover_stream_with_overlap(self):
        ranges = _build_processing_ranges(1700, 800, 90)
        self.assertEqual(ranges, [(0, 800), (710, 1510), (1420, 1700)])
        # Every consecutive pair must overlap so songs at chunk borders survive.
        for (_, prev_stop), (next_start, _) in zip(ranges, ranges[1:]):
            self.assertLess(next_start, prev_stop)

    def test_zero_overlap_falls_back_to_abutting_ranges(self):
        ranges = _build_processing_ranges(1600, 800, 0)
        self.assertEqual(ranges, [(0, 800), (800, 1600)])


class ResolveChunkOverlapsTests(GpuSegmentationEnvTestCase):
    def test_conflict_keeps_label_far_from_chunk_edge(self):
        ranges = [(0, 800), (710, 1510)]
        items = [
            ("music", 100.0, 201.0, 0),
            ("voice", 199.0, 205.0, 1),
            ("music", 205.0, 260.0, 1),
        ]
        resolved = _resolve_chunk_overlaps(items, ranges)
        self.assertEqual(resolved, [("music", 100.0, 201.0), ("music", 205.0, 260.0)])

    def test_agreeing_labels_merge(self):
        ranges = [(0, 800), (710, 1510)]
        items = [
            ("music", 100.0, 800.0, 0),
            ("music", 710.0, 900.0, 1),
        ]
        resolved = _resolve_chunk_overlaps(items, ranges)
        self.assertEqual(resolved, [("music", 100.0, 900.0)])

    def test_song_spanning_chunk_border_survives(self):
        # A song 700-900s detected by both chunks must come out continuous.
        ranges = [(0, 800), (710, 1600)]
        items = [
            ("music", 700.0, 800.0, 0),
            ("music", 710.0, 900.0, 1),
        ]
        resolved = _resolve_chunk_overlaps(items, ranges)
        self.assertEqual(resolved, [("music", 700.0, 900.0)])


class BuildSegmentsTests(GpuSegmentationEnvTestCase):
    def _options(self):
        return ExtractionOptions(
            min_duration=60.0,
            max_silence=6.0,
            leading_padding=1.0,
            trailing_padding=4.0,
        )

    def test_continuous_song_is_kept(self):
        segmentation = [("music", 10.0, 310.0)]
        segments = _build_segments_from_segmentation(
            segmentation=segmentation,
            total_duration=320.0,
            options=self._options(),
        )
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].index, 1)
        self.assertAlmostEqual(segments[0].start, 9.0)
        self.assertAlmostEqual(segments[0].end, 314.0)

    def test_bgm_under_speech_is_filtered_by_music_ratio(self):
        # Flickering 6s music / 6s voice labels merge into one padded range but
        # only ~50% is music: classic talk-with-BGM false positive.
        labels: list[tuple[str, float, float]] = []
        position = 10.0
        while position < 190.0:
            labels.append(("music", position, position + 6.0))
            labels.append(("voice", position + 6.0, position + 12.0))
            position += 12.0
        segments = _build_segments_from_segmentation(
            segmentation=labels,
            total_duration=240.0,
            options=self._options(),
        )
        self.assertEqual(segments, [])

    def test_music_ratio_filter_can_be_disabled(self):
        os.environ["SONGCUT_INASEG_MIN_MUSIC_RATIO"] = "0"
        labels: list[tuple[str, float, float]] = []
        position = 10.0
        while position < 190.0:
            labels.append(("music", position, position + 6.0))
            labels.append(("voice", position + 6.0, position + 12.0))
            position += 12.0
        segments = _build_segments_from_segmentation(
            segmentation=labels,
            total_duration=240.0,
            options=self._options(),
        )
        self.assertEqual(len(segments), 1)

    def test_short_interludes_inside_song_still_pass(self):
        # Brief talking inside a long music run must not be dropped.
        segmentation = [
            ("music", 10.0, 120.0),
            ("voice", 120.0, 126.0),
            ("music", 126.0, 300.0),
        ]
        segments = _build_segments_from_segmentation(
            segmentation=segmentation,
            total_duration=310.0,
            options=self._options(),
        )
        self.assertEqual(len(segments), 1)

    def test_noenergy_gaps_are_bridged(self):
        segmentation = [
            ("music", 10.0, 120.0),
            ("noEnergy", 120.0, 123.0),
            ("music", 123.0, 300.0),
        ]
        segments = _build_segments_from_segmentation(
            segmentation=segmentation,
            total_duration=310.0,
            options=self._options(),
        )
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0].duration, 295.0, places=1)


if __name__ == "__main__":
    unittest.main()
