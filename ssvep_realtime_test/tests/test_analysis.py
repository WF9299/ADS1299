import os
for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"
import unittest
import numpy as np
from ssvep_realtime_test.analysis import Analyzer, Preprocessor, basis, canonical_correlation
from ssvep_realtime_test.config import Settings, parse_candidates, square_pixels, stimulus_white
from ssvep_realtime_test.simulator import samples


class NumericalTests(unittest.TestCase):
    def test_physical_geometry_and_phase(self):
        self.assertEqual(square_pixels(Settings(), 2560, 1440), (214, 214))
        self.assertEqual(square_pixels(Settings(scale_x=2), 2560, 1440), (428, 214))
        frames = np.array([stimulus_white(17, i / 210) for i in range(2100)])
        rises = np.count_nonzero(frames[1:] & ~frames[:-1])
        self.assertAlmostEqual(rises / 10, 17, delta=0.11)
        self.assertAlmostEqual(frames.mean(), 0.5, delta=0.02)

    def test_settings_reject_bad_values_and_add_target(self):
        c = Settings(frequency=17.25, candidates=parse_candidates("8:30:0.5")).validate()
        self.assertIn(17.25, c.candidates)
        for settings in (Settings(channels=()), Settings(frequency=float("nan")), Settings(analysis_profile="focused", candidates=(17,)),
                         Settings(window_seconds=0), Settings(channels=(9,))):
            with self.assertRaises(ValueError):
                settings.validate()

    def test_stateful_arbitrary_chunk_boundaries(self):
        x = samples(17, 1000, 0, 4037, np.random.default_rng(1))
        one = Preprocessor(1000, True).apply(x)
        streaming = Preprocessor(1000, True)
        pieces = [[] for _ in range(3)]
        cuts = [1, 4, 7, 201, 202, 511, 999, 1012, 2048, 4037]
        start = 0
        for end in cuts:
            for index, data in enumerate(streaming.apply(x[start:end])):
                pieces[index].append(data)
            start = end
        for expected, chunks in zip(one, pieces):
            np.testing.assert_allclose(np.concatenate(chunks), expected, atol=1e-10)

    def test_antialias_rejects_out_of_band_signal(self):
        def amplitude(f):
            t = np.arange(5000) / 1000
            x = np.tile(np.sin(2 * np.pi * f * t)[:, None], (1, 8))
            return np.std(Preprocessor(1000, False).apply(x)[0][-500:, 0])
        self.assertLess(amplitude(233) / amplitude(17), 0.01)

    def test_known_frequencies_and_channel_quality(self):
        for frequency in (10.5, 17, 23):
            with self.subTest(frequency=frequency):
                a = Analyzer(Settings(analysis_profile="wide", harmonics=3))
                data = samples(frequency, 1000, 0, 4000, np.random.default_rng(5))
                data[:, 4] = 0
                data[:, 5] = 375000
                data[:, 6] = np.nan
                original = data.copy()
                for start in range(0, len(data), 200):
                    a.feed(data[start:start + 200], 1000)
                r = a.result()
                self.assertTrue(r["valid"])
                self.assertEqual(r["cca_frequency"], frequency)
                self.assertEqual(r["fbcca_frequency"], frequency)
                self.assertEqual(r["used_channels"], [1, 2, 3, 4, 8])
                self.assertEqual(set(r["excluded"]), {"5", "6", "7"})
                self.assertGreater(r["cca_rho"], 0.85)
                self.assertTrue(all(0 <= r <= 1 for r in r["fbcca_rhos"]))
                np.testing.assert_array_equal(original, data)

    def test_invalid_window_and_reset(self):
        a = Analyzer(Settings(analysis_profile="focused", harmonics=2))
        self.assertFalse(a.result()["valid"])
        a.feed(np.zeros((4000, 8)), 1000)
        self.assertEqual(a.result()["status"], "无有效通道")
        a.reset()
        self.assertFalse(a.result()["valid"])
        self.assertIsNone(a.preprocessor)

    def test_target_with_mains_interference(self):
        a = Analyzer(Settings())
        data = samples(17, 1000, 0, 5000, np.random.default_rng(11))
        data += 50 * np.sin(2 * np.pi * 50 * np.arange(5000) / 1000)[:, None]
        a.feed(data, 1000)
        result = a.result()
        self.assertEqual(result["cca_frequency"], 17)
        self.assertEqual(result["fbcca_frequency"], 17)

    def test_rate_change_discards_old_window(self):
        a = Analyzer(Settings(analysis_profile="focused", harmonics=2))
        a.feed(samples(17, 1000, 0, 4000, np.random.default_rng(5)), 1000)
        self.assertTrue(a.result()["valid"])
        a.feed(np.ones((100, 8)), 500)
        self.assertFalse(a.result()["valid"])

    def test_rank_deficient_cca_is_bounded(self):
        t = np.arange(500) / 250
        sine = np.sin(2 * np.pi * 17 * t)
        xb = basis(np.column_stack((sine, sine, np.ones(500))))
        self.assertEqual(xb.shape[1], 1)
        self.assertAlmostEqual(canonical_correlation(xb, basis(sine[:, None])), 1)
        self.assertEqual(canonical_correlation(basis(np.zeros((500, 2))), xb), 0)


if __name__ == "__main__":
    unittest.main()
