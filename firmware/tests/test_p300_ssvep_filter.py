"""SSVEP causal display filter; original HTTP/record isolation is tested in test_p300_tools."""
import unittest
import numpy as np
from tools.p300_filters import FilterMode, RealtimeEegFilter, SCIPY_AVAILABLE, scipy_signal


@unittest.skipUnless(SCIPY_AVAILABLE, "SciPy unavailable")
class SsvepFilterTests(unittest.TestCase):
    def test_ssvep_frequency_response_and_sampling_rate_guard(self):
        for rate in (250, 500, 1000, 2000):
            sos = RealtimeEegFilter._design_sos(FilterMode.SSVEP, rate)
            frequencies = np.array([1., 6., 17., 34., 48., 50., 68., 90.])
            _, response = scipy_signal.sosfreqz(sos, worN=frequencies, fs=rate)
            gain = np.abs(response)
            self.assertLess(gain[0], .002)
            self.assertAlmostEqual(gain[1], 2 ** -.5, delta=.001)
            self.assertGreater(gain[2], .99)
            self.assertGreater(gain[3], .99)
            self.assertGreater(gain[4], .99)
            self.assertLess(gain[5], .0001)
            self.assertGreater(gain[6], .95)
            self.assertAlmostEqual(gain[7], 2 ** -.5, delta=.001)
        with self.assertRaisesRegex(ValueError, "180"):
            RealtimeEegFilter._design_sos(FilterMode.SSVEP, 160)

    def test_ssvep_chunking_gap_reset_and_short_bypass(self):
        data = np.random.default_rng(32).normal(size=(2000, 8))
        whole = RealtimeEegFilter(FilterMode.SSVEP).process(data, sample_rate_hz=1000, stream_id=1, config_id=1, start_seq=0)
        stream = RealtimeEegFilter(FilterMode.SSVEP)
        parts = [stream.process(data[i:i+20], sample_rate_hz=1000, stream_id=1, config_id=1, start_seq=i) for i in range(0, 2000, 20)]
        np.testing.assert_allclose(np.concatenate(parts), whole, atol=1e-10)
        stream.process(data[:20], sample_rate_hz=1000, stream_id=1, config_id=1, start_seq=2500)
        self.assertEqual(stream.last_reset_reason, "sequence")
        bypass = stream.process(data[:20], sample_rate_hz=1000, stream_id=2, config_id=2, start_seq=0)
        np.testing.assert_array_equal(bypass, data[:20])
        self.assertFalse(stream.active)


if __name__ == "__main__":
    unittest.main()
