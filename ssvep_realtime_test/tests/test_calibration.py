import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from ssvep_realtime_test.analysis import Analyzer
from ssvep_realtime_test.calibration import Calibration
from ssvep_realtime_test.config import Settings
from ssvep_realtime_test.diagnostics import summarize
from ssvep_realtime_test.simulator import samples


class QualityCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.config = Settings(analysis_profile="focused", harmonics=2, candidates=(12., 13., 17.), baseline_windows=3, response_windows=2)
        self.analyzer = Analyzer(self.config)
        self.calibration = Calibration(self.config)
        self.context = {"sampling_rate": 1000, "fullscale_uv": 375000., "metadata_known": True, "simulation": True}
        self.calibration.set_context(self.context)
        self.rng = np.random.default_rng(91)
        self.end = 0

    def window(self, phase, frequency=None):
        data = self.rng.normal(0, 20, (4000, 8)) if frequency is None else samples(frequency, 1000, round(self.end * 1000), 4000, self.rng)
        self.analyzer.feed(data, 1000)
        self.end += 4
        return self.calibration.decorate(self.analyzer, self.analyzer.result(channels=self.calibration.channels), self.end)

    def fit(self):
        self.calibration.restart("baseline")
        for _ in range(3):
            baseline = self.window("baseline")
        self.assertEqual(baseline["action"], "start_response")
        self.analyzer.reset()
        self.calibration.restart("response")
        for _ in range(2):
            response = self.window("response", 17)
        self.assertEqual(response["action"], "start_test")
        self.analyzer.reset()
        self.calibration.restart("test")

    def test_raw_dc_does_not_disappear_in_diagnostics(self):
        t = np.arange(4000) / 1000
        data = np.tile((100000 + 10 * np.sin(2 * np.pi * 17 * t))[:, None], (1, 8))
        d, used, excluded = summarize(data, 1000, 375000, (12, 17), tuple(range(1, 9)))
        self.assertAlmostEqual(d["channels"][0]["dc_uv"], 100000)
        self.assertAlmostEqual(d["channels"][0]["rms_uv"], np.sqrt(50), places=4)
        self.assertGreater(d["snr_db"][1][0], 30)
        self.assertEqual(len(used), 8)
        self.assertFalse(excluded)
        data[:, 0] = 187500
        d, used, excluded = summarize(data, 1000, 187500, (12, 17), tuple(range(1, 9)))
        self.assertIn("1", excluded)
        self.assertEqual(d["channels"][0]["clipped_fraction"], 1)

    def test_calibration_does_not_count_overlapping_windows(self):
        self.calibration.restart("baseline")
        self.window("baseline")
        for _ in range(5):
            self.analyzer.feed(self.rng.normal(0, 20, (200, 8)), 1000)
            self.end += .2
            r = self.calibration.decorate(self.analyzer, self.analyzer.result(), self.end)
        self.assertEqual(r["calibration"]["collected"], 1)
        self.assertNotIn("action", r)

    def test_signal_passes_after_confirmation_noise_and_wrong_frequency_reject(self):
        self.fit()
        frozen = json.dumps(self.calibration.model, sort_keys=True)
        self.assertLessEqual(len(self.calibration.channels), 3)
        self.assertFalse(self.window("test", 17)["detected"])
        self.assertTrue(self.window("test", 17)["detected"])
        for _ in range(8):
            self.assertFalse(self.window("test")["detected"])
        self.assertFalse(self.window("test", 13)["detected"])
        self.assertEqual(json.dumps(self.calibration.model, sort_keys=True), frozen)

    def test_no_model_rejects_even_clean_target(self):
        result = self.window("test", 17)
        self.assertTrue(result["valid"])
        self.assertFalse(result["detected"])
        self.assertIn("未标定", result["decision"])

    def test_metadata_or_selected_channel_failure_rejects(self):
        self.fit()
        channel = self.calibration.channels[0]
        data = samples(17, 1000, 0, 4000, self.rng)
        data[:, channel - 1] = 375000
        self.analyzer.feed(data, 1000)
        r = self.calibration.decorate(self.analyzer, self.analyzer.result(channels=self.calibration.channels), self.end + 4)
        self.assertFalse(r["detected"])
        self.assertIn("质量不足", r["decision"])
        self.calibration.set_context({**self.context, "fullscale_uv": 187500})
        self.assertIsNone(self.calibration.model)

    def test_save_load_scope_and_malformed_model(self):
        self.fit()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.json"
            path.write_text(json.dumps(self.calibration.model), encoding="utf-8")
            loaded = Calibration(self.config, path)
            loaded.set_context(self.context)
            self.assertIsNotNone(loaded.model)
            changed = Calibration(Settings(window_seconds=5, candidates=(12., 13., 17.)), path)
            self.assertIsNone(changed.model)
            self.calibration.model["thresholds"] = {}
            path.write_text(json.dumps(self.calibration.model), encoding="utf-8")
            self.assertIsNone(Calibration(self.config, path).model)

    def test_settings_migrate_only_legacy_defaults_and_keep_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"window_seconds": 2, "frequency": 17, "scale_x": 1.1}), encoding="utf-8")
            migrated = Settings.load(path)
            self.assertEqual(migrated.window_seconds, 4)
            self.assertEqual(migrated.scale_x, 1.1)
            migrated.window_seconds = 2
            migrated.save(path)
            self.assertEqual(Settings.load(path).window_seconds, 2)
        with self.assertRaisesRegex(ValueError, "二次谐波"):
            Settings(frequency=23, analysis_profile="focused").validate()

    def test_focused_targets_with_two_harmonics(self):
        for frequency in (12, 13, 17):
            a = Analyzer(Settings(frequency=frequency, analysis_profile="focused", harmonics=2))
            a.feed(samples(frequency, 1000, 0, 4000, self.rng), 1000)
            r = a.result()
            self.assertEqual(r["cca_frequency"], frequency)
            self.assertEqual(r["fbcca_frequency"], frequency)


if __name__ == "__main__":
    unittest.main()
