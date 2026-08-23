import os
import unittest

from songcut_automation import looks_like_singing
from songcut_extractor import RegionActivity, region_activity_from_energies

GATE_ENV_KEYS = (
    "SONGCUT_MIN_SUSTAINED_ACTIVE_SECONDS",
    "SONGCUT_MAX_ACTIVITY_CV",
    "SONGCUT_MIN_ACTIVITY_RATIO",
)


class LooksLikeSingingEnvTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in GATE_ENV_KEYS}
        for key in GATE_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class RegionActivityTests(LooksLikeSingingEnvTestCase):
    def test_stats_from_synthetic_energies(self):
        # 40 windows of 0.5s: 30 active (15s run), 10 quiet.
        energies = [9000] * 30 + [100] * 10
        flags = [value >= 2558 for value in energies]
        activity = region_activity_from_energies(energies, window_seconds=0.5, active_flags=flags)
        self.assertAlmostEqual(activity.active_ratio, 0.75)
        self.assertAlmostEqual(activity.longest_active_run_seconds, 15.0)
        self.assertAlmostEqual(activity.quiet_ratio, 0.25)
        self.assertGreater(activity.cv, 0.5)


class LooksLikeSingingTests(LooksLikeSingingEnvTestCase):
    # Values measured on the real trial recording (2026-03-20 stream).
    SUNG = RegionActivity(mean_rms=3236, cv=0.51, active_ratio=0.67, longest_active_run_seconds=34.0, quiet_ratio=0.10)
    SUNG_SLOW_SONG = RegionActivity(mean_rms=2255, cv=0.58, active_ratio=0.44, longest_active_run_seconds=14.0, quiet_ratio=0.12)
    BGM_UNDER_CHAT = RegionActivity(mean_rms=1695, cv=0.87, active_ratio=0.25, longest_active_run_seconds=5.0, quiet_ratio=0.18)
    BGM_DANCE = RegionActivity(mean_rms=2757, cv=0.68, active_ratio=0.44, longest_active_run_seconds=6.0, quiet_ratio=0.04)
    CHAT = RegionActivity(mean_rms=2754, cv=0.65, active_ratio=0.47, longest_active_run_seconds=6.0, quiet_ratio=0.06)

    def test_sung_regions_pass(self):
        self.assertTrue(looks_like_singing(self.SUNG))
        self.assertTrue(looks_like_singing(self.SUNG_SLOW_SONG))

    def test_bgm_and_chat_rejected(self):
        self.assertFalse(looks_like_singing(self.BGM_UNDER_CHAT))  # 天天
        self.assertFalse(looks_like_singing(self.BGM_DANCE))  # Heatstroke
        self.assertFalse(looks_like_singing(self.CHAT))

    def test_short_run_but_steady_energy_is_kept(self):
        # A quiet real song with only 8s runs but stable energy: keep it.
        steady = RegionActivity(mean_rms=2000, cv=0.40, active_ratio=0.55, longest_active_run_seconds=8.0, quiet_ratio=0.05)
        self.assertTrue(looks_like_singing(steady))

    def test_thresholds_tunable(self):
        # Raising min_run alone keeps steady-energy regions (by design); also
        # tightening the CV bound is needed to reject this region.
        os.environ["SONGCUT_MIN_SUSTAINED_ACTIVE_SECONDS"] = "40"
        os.environ["SONGCUT_MAX_ACTIVITY_CV"] = "0.2"
        self.assertFalse(looks_like_singing(self.SUNG))


if __name__ == "__main__":
    unittest.main()
