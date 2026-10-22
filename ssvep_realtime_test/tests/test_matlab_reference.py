import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
from scipy import linalg, signal
from ssvep_realtime_test.analysis import Analyzer
from ssvep_realtime_test.config import Settings
from ssvep_realtime_test.matlab_reference import FirResampler, zero_phase_banks


class MatlabReferenceTests(unittest.TestCase):
    def test_fir_alignment_and_arbitrary_packets_match_polyphase(self):
        data = np.random.default_rng(31).normal(size=(4037, 8))
        for fs in (250, 500, 1000, 2000):
            resampler = FirResampler(fs)
            pieces = []
            start = 0
            for end in (1, 4, 33, 212, 409, 1007, 2301, 4037):
                pieces.append(resampler.apply(data[start:end]))
                start = end
            actual = np.concatenate(pieces)
            expected = signal.resample_poly(data, 1, fs // 250, axis=0, window=("kaiser", 5.0))
            np.testing.assert_allclose(actual, expected[:len(actual)], atol=2e-14)
            self.assertEqual(resampler.last_center, (len(actual) - 1) * (fs // 250))

    def test_fir_prevents_high_frequency_alias(self):
        t = np.arange(8000) / 1000
        amplitude = []
        for frequency in (17, 233):
            data = np.tile(np.sin(2 * np.pi * frequency * t)[:, None], (1, 8))
            amplitude.append(np.std(FirResampler(1000).apply(data)[200:, 0]))
        self.assertLess(amplitude[1] / amplitude[0], .001)

    def test_zero_phase_matches_separate_direct_form_script_and_preserves_input(self):
        data = np.random.default_rng(4).normal(size=(1000, 8))
        original = data.copy()
        bn, an = signal.butter(2, (49.5, 50.5), btype="bandstop", fs=250)
        bb, ab = signal.butter(4, (6, 90), btype="bandpass", fs=250)
        expected = signal.filtfilt(bb, ab, signal.filtfilt(bn, an, data, axis=0, padlen=12), axis=0, padlen=24)
        np.testing.assert_allclose(zero_phase_banks(data)[0], expected, atol=2e-9)
        np.testing.assert_array_equal(data, original)

    def test_fourth_harmonic_and_independent_qr_cca_reference(self):
        config = Settings(frequency=17., window_seconds=4).validate()
        t = np.arange(1250) / 250
        rng = np.random.default_rng(7)
        data = rng.normal(0, 1, (1250, 8)) + 8 * np.sin(2 * np.pi * 68 * t)[:, None]
        analyzer = Analyzer(config)
        analyzer.feed(data, 250)
        result = analyzer.result()
        self.assertTrue(result["valid"])
        self.assertEqual(result["cca_frequency"], 17.)
        filtered = zero_phase_banks(data[-1000:])[0]
        qx, _ = linalg.qr(filtered - filtered.mean(axis=0), mode="economic")
        time = np.arange(1, 1001) / 250
        correlations = []
        for frequency in config.candidates:
            reference = np.column_stack([fun(2 * np.pi * k * frequency * time)
                                          for k in range(1, 5) for fun in (np.sin, np.cos)])
            qy, _ = linalg.qr(reference - reference.mean(axis=0), mode="economic")
            correlations.append(linalg.svdvals(qx.T @ qy)[0])
        np.testing.assert_allclose(result["cca_rhos"], correlations, atol=1e-12)

    def test_raw_quality_window_aligns_to_fir_output_sample_grid(self):
        data = np.tile(np.arange(4200)[:, None], (1, 8)).astype(float)
        analyzer = Analyzer(Settings())
        analyzer.feed(data, 1000)
        result = analyzer.result()
        # Last FIR output is centered at input4156; output sample coverage
        # ends at4160. Four seconds of raw quality must span160..4159.
        self.assertEqual(analyzer.preprocessor.last_center, 4156)
        self.assertAlmostEqual(result["diagnostics"]["channels"][0]["dc_uv"], data[160:4160, 0].mean())

    def test_dynamic_analysis_grid_keeps_stimulus_shortcuts_and_acquisition_settings(self):
        config = Settings().validate()
        self.assertEqual(config.candidates, tuple(np.arange(13, 21.01, .5)))
        config.frequency = 21
        config.validate()
        self.assertEqual(config.frequency_center, 17)
        self.assertEqual(config.switch_frequencies[0], 13)
        self.assertEqual(config.candidates, tuple(np.arange(17, 25.01, .5)))
        for frequency in (9.5, 27.5):
            with self.assertRaises(ValueError):
                Settings(frequency=frequency).validate()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            old = {"schema_version": 2, "frequency": 12, "channels": [3, 6, 8], "port": 25002,
                   "analysis_profile": "focused", "scale_x": 1.1, "window_seconds": 5}
            path.write_text(json.dumps(old))
            migrated = Settings.load(path)
            self.assertEqual((migrated.analysis_profile, migrated.harmonics, migrated.calibrate_before_run), ("matlab", 4, False))
            self.assertEqual((migrated.channels, migrated.port, migrated.scale_x, migrated.window_seconds), ((3, 6, 8), 25002, 1.1, 5))
            self.assertEqual(migrated.candidates, tuple(np.arange(8, 16.01, .5)))
            self.assertEqual(json.loads(path.read_text()), old)


if __name__ == "__main__":
    unittest.main()
