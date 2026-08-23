import os
import unittest

from songcut_extractor import (
    ExtractionOptions,
    _detect_segments,
    _expand_segment_end,
    _expand_segment_start,
    expand_boundary_over_audibility,
)

BOUNDARY_ENV_KEYS = (
    "SONGCUT_INTRO_SEARCH_SECONDS",
    "SONGCUT_INTRO_SILENCE_SECONDS",
    "SONGCUT_OUTRO_SEARCH_SECONDS",
    "SONGCUT_OUTRO_SILENCE_SECONDS",
)


def flags_from_runs(size: int, runs: list[tuple[int, int, str]]) -> tuple[list[bool], list[bool]]:
    """Build (audible, active) flag lists from [start, end) runs tagged silent/quiet/loud."""
    audible = [False] * size
    active = [False] * size
    for start, end, kind in runs:
        for index in range(max(0, start), min(size, end)):
            if kind in {"quiet", "loud"}:
                audible[index] = True
            if kind == "loud":
                active[index] = True
    return audible, active


class ExpandBoundaryOverAudibilityTests(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in BOUNDARY_ENV_KEYS}
        for key in BOUNDARY_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_extends_backward_over_quiet_intro_until_sustained_silence(self):
        # windows 20-39: quiet intro; 10-19: silence; 5-9: previous loud chat.
        audible, active = flags_from_runs(60, [(20, 40, "quiet"), (5, 10, "loud"), (40, 60, "loud")])
        expanded = expand_boundary_over_audibility(
            audible_windows=audible,
            active_windows=active,
            anchor=40,
            direction=-1,
            max_search_windows=50,
            silence_limit_windows=8,
        )
        self.assertEqual(expanded, 20)

    def test_stops_at_loud_content(self):
        # quiet intro 36-39 directly after loud chat at 30-35.
        audible, active = flags_from_runs(60, [(36, 40, "quiet"), (30, 36, "loud"), (40, 60, "loud")])
        expanded = expand_boundary_over_audibility(
            audible_windows=audible,
            active_windows=active,
            anchor=40,
            direction=-1,
            max_search_windows=50,
            silence_limit_windows=8,
        )
        self.assertEqual(expanded, 36)

    def test_returns_none_when_nothing_audible(self):
        audible, active = flags_from_runs(60, [(0, 40, "silent"), (40, 60, "loud")])
        expanded = expand_boundary_over_audibility(
            audible_windows=audible,
            active_windows=active,
            anchor=40,
            direction=-1,
            max_search_windows=50,
            silence_limit_windows=8,
        )
        self.assertIsNone(expanded)

    def test_search_budget_caps_expansion(self):
        audible, active = flags_from_runs(200, [(0, 100, "quiet"), (100, 200, "loud")])
        expanded = expand_boundary_over_audibility(
            audible_windows=audible,
            active_windows=active,
            anchor=100,
            direction=-1,
            max_search_windows=20,
            silence_limit_windows=80,
        )
        self.assertEqual(expanded, 80)

    def test_extends_forward_over_quiet_outro(self):
        # song body ends at 260 (exclusive); outro 261-290 quiet; silence after.
        audible, active = flags_from_runs(
            320,
            [(100, 261, "loud"), (261, 291, "quiet"), (291, 320, "silent")],
        )
        expanded = expand_boundary_over_audibility(
            audible_windows=audible,
            active_windows=active,
            anchor=261,
            direction=1,
            max_search_windows=120,
            silence_limit_windows=8,
        )
        self.assertEqual(expanded, 290)


class DetectSegmentsBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in BOUNDARY_ENV_KEYS}
        for key in BOUNDARY_ENV_KEYS:
            os.environ.pop(key, None)
        self.options = ExtractionOptions(
            min_duration=60.0,
            max_silence=6.0,
            merge_gap=18.0,
            leading_padding=1.5,
            trailing_padding=2.5,
            min_active_ratio=0.45,
            analysis_window=0.5,
            intro_search=30.0,
            intro_silence=4.0,
            outro_search=45.0,
            outro_silence=4.0,
        )

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _detect(self, size: int, runs: list[tuple[int, int, str]]):
        audible, active = flags_from_runs(size, runs)
        energies = [900 if flag else 20 for flag in active]
        return _detect_segments(
            energies=energies,
            active_windows=active,
            intro_windows=audible,
            total_duration=size * self.options.analysis_window,
            options=self.options,
        )

    def test_intro_and_outro_are_included(self):
        segments = self._detect(
            400,
            [
                (70, 84, "silent"),
                (84, 100, "quiet"),  # 8s quiet intro
                (100, 261, "loud"),  # singing
                (261, 291, "quiet"),  # 15s quiet outro
                (291, 400, "silent"),
            ],
        )
        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertAlmostEqual(segment.start, 84 * 0.5 - 1.5)  # 40.5
        self.assertAlmostEqual(segment.end, 291 * 0.5 + 2.5)  # 148.0

    def test_no_audible_intro_keeps_original_start(self):
        # Regression: silence used to pull a bogus 30s lookback prefix.
        segments = self._detect(
            400,
            [
                (0, 100, "silent"),
                (100, 261, "loud"),
                (261, 400, "silent"),
            ],
        )
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0].start, 100 * 0.5 - 1.5)

    def test_outro_expansion_stops_before_next_loud_block(self):
        # Outro 261-270 quiet, then >18s of silence keeps the chat block a
        # separate segment; the outro must not leak into it.
        segments = self._detect(
            500,
            [
                (100, 261, "loud"),
                (261, 271, "quiet"),  # 5s outro
                (271, 345, "silent"),  # 37s silence: > merge_gap, separate segments
                (345, 500, "loud"),  # chatting after the song
            ],
        )
        self.assertEqual(len(segments), 2)
        first = segments[0]
        self.assertAlmostEqual(first.end, 271 * 0.5 + 2.5)

    def test_wrapper_functions(self):
        audible, active = flags_from_runs(
            100, [(10, 20, "quiet"), (20, 60, "loud"), (60, 75, "quiet"), (75, 100, "silent")]
        )
        start = _expand_segment_start(20, audible, active, self.options)
        end = _expand_segment_end(60, audible, active, self.options)
        self.assertEqual(start, 10)
        self.assertEqual(end, 75)

    def test_long_quiet_interlude_fuses_into_one_cut(self):
        # Regression (glow case): a quiet interlude longer than merge_gap splits
        # the song into two raw ranges; both expanded cuts then claim the
        # interlude and overlap - they must fuse into one continuous cut.
        segments = self._detect(
            700,
            [
                (100, 240, "loud"),  # verse 1 (70s, passes min_duration)
                (240, 291, "quiet"),  # 25.5s quiet interlude (> merge_gap 18s)
                (291, 440, "loud"),  # verse 2
            ],
        )
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0].start, 100 * 0.5 - 1.5)  # 48.5
        self.assertAlmostEqual(segments[0].end, 440 * 0.5 + 2.5)  # 222.5

    def test_separate_songs_with_silence_stay_separate(self):
        # Two songs separated by real silence must NOT fuse, even when the
        # outro/intro expansions reach toward each other.
        segments = self._detect(
            700,
            [
                (100, 261, "loud"),
                (261, 271, "quiet"),
                (271, 349, "silent"),  # 39s silence: expansion stops inside it
                (349, 359, "quiet"),
                (359, 500, "loud"),
            ],
        )
        self.assertEqual(len(segments), 2)
        self.assertLess(segments[0].end, segments[1].start)


if __name__ == "__main__":
    unittest.main()
