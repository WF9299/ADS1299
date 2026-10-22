import unittest
from unittest.mock import Mock
import numpy as np
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication
from ssvep_realtime_test.analysis import Analyzer
from ssvep_realtime_test.config import Settings
from ssvep_realtime_test.cca_chart import COLORS, PLOT, cca_bars, render_cca_panel
from ssvep_realtime_test.simulator import samples
from ssvep_realtime_test.stimulus import StimulusWindow


class CcaChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mask_changes_rank_without_changing_correlations(self):
        data = samples(16.5, 1000, 0, 5000, np.random.default_rng(3))
        results = []
        for masked in (False, True):
            analyzer = Analyzer(Settings(frequency=14, exclude_165=masked))
            analyzer.feed(data, 1000)
            results.append(analyzer.result())
        plain, masked = results
        self.assertEqual(plain["cca_frequency"], 16.5)
        self.assertEqual(masked["cca_raw_frequency"], 16.5)
        self.assertNotEqual(masked["cca_frequency"], 16.5)
        self.assertNotEqual(masked["fbcca_frequency"], 16.5)
        self.assertNotIn(16.5, masked["eligible_candidates"])
        np.testing.assert_array_equal(masked["cca_rhos"], plain["cca_rhos"])
        np.testing.assert_array_equal(masked["fbcca_scores"], plain["fbcca_scores"])
        bars = cca_bars(masked, 14)
        bar = next(b for b in bars if b["frequency"] == 16.5)
        self.assertEqual(bar["role"], "excluded")
        self.assertGreater(bar["rho"], .9)
        target = Analyzer(Settings(frequency=16.5, exclude_165=True))
        target.feed(data, 1000)
        self.assertTrue(target.result()["target_excluded"])

    def test_chart_preserves_real_rho_colors_and_fixed_vertical_axis(self):
        result = {"valid": True, "candidates": [16., 16.5, 17.], "cca_rhos": [.2, .9, .4],
                  "cca_frequency": 17., "excluded_frequencies": [16.5]}
        bars = cca_bars(result, 16.)
        self.assertEqual([b["role"] for b in bars], ["target", "excluded", "winner"])
        image = render_cca_panel(result, 16.)
        cell = PLOT.width() / 3
        for index, role in enumerate(("target", "excluded", "winner")):
            x = round(PLOT.left() + (index + .5) * cell)
            y = round(PLOT.bottom() - .1 * PLOT.height())
            self.assertEqual(image.pixelColor(x, y).name(), COLORS[role])
        matched = {**result, "cca_frequency": 16.}
        self.assertEqual(cca_bars(matched, 16.)[0]["role"], "match")

    def test_invalid_or_old_candidate_chart_never_reuses_scores(self):
        result = {"valid": True, "candidates": [16., 16.5], "cca_rhos": [.4, .8], "cca_frequency": 16.5}
        for invalid in ({**result, "valid": False}, {**result, "cca_rhos": [.4]},
                        {**result, "cca_rhos": [.4, float("nan")]}, {**result, "cca_rhos": [.4, 1.1]}):
            self.assertIsNone(cca_bars(invalid, 16))
            self.assertFalse(render_cca_panel(invalid, 16).isNull())
        self.assertIsNone(cca_bars(result, 16, expected_candidates=(12., 13.)))

    def test_hotkeys_and_worker_exit_clear_cached_bars(self):
        worker, log = Mock(), Mock()
        worker.process.is_alive.return_value = True
        worker.poll.return_value = None
        log.error, log.dropped = None, 0
        window = StimulusWindow(Settings(), self.app.screens()[0], worker, log)
        window.timer.stop()
        window.watchdog.stop()
        try:
            self.assertEqual(window.panel_mode, "cca")
            window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_D, Qt.NoModifier))
            self.assertEqual(window.panel_mode, "quality")
            window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_B, Qt.NoModifier))
            self.assertEqual(window.panel_mode, "cca")
            self.assertTrue(window.show_diagnostics)
            window.running = True
            window.result = {"valid": True, "cca_rho": .999}
            worker.process.is_alive.return_value = False
            window.poll()
            self.assertFalse(window.result["valid"])
            self.assertNotIn("cca_rho", window.result)
            self.assertFalse(window.panel.isNull())
        finally:
            window.destroy()

    def test_dynamic_window_boundary_disables_unsupported_stimulus_shortcuts(self):
        for frequency, key in ((10., Qt.Key_Left), (27., Qt.Key_Right)):
            window = StimulusWindow(Settings(frequency=frequency), self.app.screens()[0], Mock(), Mock())
            window.timer.stop()
            window.watchdog.stop()
            try:
                window.running = True
                window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
                self.assertIsNone(window.pending_frequency)
                self.assertEqual(window.config.frequency, frequency)
            finally:
                window.destroy()

    def test_masked_current_target_is_explicit_in_real_http_result(self):
        import time
        from test_receiver import free_port, payload, post
        from test_frequency_switch import wait_result
        from ssvep_realtime_test.receiver import WorkerController
        controller = WorkerController(Settings(frequency=16.5, exclude_165=True, window_seconds=1,
                                               stale_seconds=2, port=free_port()))
        controller.start()
        try:
            controller.begin(time.time() - 1.4, frequency=16.5, trial_id=1)
            post(controller.config.port, payload(time.time() - 1.2, samples(16.5, 1000, 0, 1200, np.random.default_rng(3))))
            result = wait_result(controller, lambda r: r.get("valid"))
            self.assertTrue(result["target_excluded"])
            self.assertFalse(result["target_match"])
            self.assertFalse(result["detected"])
            self.assertIn("已屏蔽", result["decision"])
            self.assertNotEqual(result["cca_frequency"], 16.5)
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
