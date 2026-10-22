import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QImage, QKeyEvent, QPainter
from PyQt5.QtWidgets import QApplication
from ssvep_realtime_test.config import Settings
from ssvep_realtime_test.display import draw_scene, result_lines
from ssvep_realtime_test.session import SessionLog
from ssvep_realtime_test.setup_dialog import SetupDialog
from ssvep_realtime_test.stimulus import FrameClock, StimulusWindow
from ssvep_realtime_test.diagnostic_view import render_panel
from ssvep_realtime_test.analysis import Analyzer
from ssvep_realtime_test.simulator import samples
import numpy as np


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_setup_hidden_and_selected_values(self):
        dialog = SetupDialog(Settings(), self.app.screens())
        dialog.setAttribute(Qt.WA_DontShowOnScreen)
        dialog.frequency.setValue(18.5)
        dialog.channels.setText("1,3,8")
        config = dialog.values()
        self.assertEqual(config.frequency, 18.5)
        self.assertEqual(config.channels, (1, 3, 8))
        dialog.close()

    def test_legacy_custom_candidates_survive_switching_matlab_profile(self):
        dialog = SetupDialog(Settings(analysis_profile="focused", candidates=(12., 13., 17.)), self.app.screens())
        dialog.profile.setCurrentIndex(dialog.profile.findData("matlab"))
        self.assertTrue(dialog.candidates.isReadOnly())
        self.assertEqual(len(dialog.values().candidates), 17)
        dialog.profile.setCurrentIndex(dialog.profile.findData("wide"))
        self.assertEqual(dialog.values().candidates, (12., 13., 17.))
        self.assertFalse(dialog.candidates.isReadOnly())
        dialog.close()

    def test_static_scene_center_size_black_background(self):
        image = QImage(2560, 1440, QImage.Format_RGB32)
        painter = QPainter(image)
        draw_scene(painter, 2560, 1440, 1, Settings(), True, None, "静态预览", True)
        painter.end()
        self.assertEqual(image.pixelColor(0, 0).name(), "#000000")
        whites = [x for x in range(image.width()) if image.pixelColor(x, 720).name() == "#ffffff"]
        self.assertEqual(len(whites), 214)
        self.assertEqual((min(whites) + max(whites) + 1) / 2, 1280)
        # Same panel at 200% scaling keeps 214 physical pixels, 107 logical pixels.
        scaled = QImage(2560, 1440, QImage.Format_RGB32)
        scaled.setDevicePixelRatio(2)
        painter = QPainter(scaled)
        draw_scene(painter, 1280, 720, 2, Settings(), True, None, "静态预览")
        painter.end()
        self.assertEqual(sum(scaled.pixelColor(x, 720).name() == "#ffffff" for x in range(2560)), 214)

    def test_invalid_hides_old_scores(self):
        self.assertFalse(any("0.999" in line for line in result_lines({"valid": False, "status": "断流", "cca_rho": .999})))

    def test_frame_clock_missed_frames(self):
        clock = FrameClock()
        for i in range(211):
            clock.swap(i / 210)
        self.assertAlmostEqual(clock.calibrate(), 210)
        clock.swap(213 / 210)
        self.assertEqual(clock.missed, 2)

    def test_window_constructs_without_showing_or_strobing(self):
        window = StimulusWindow(Settings(), self.app.screens()[0], None, None)
        self.assertFalse(window.isVisible())
        self.assertFalse(window.running)
        window.timer.stop()
        window.watchdog.stop()
        with patch("ssvep_realtime_test.stimulus.QCoreApplication.quit") as quit_app:
            window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
            quit_app.assert_called_once()
        window.destroy()

    def test_session_drains_and_preserves_distinct_scores(self):
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(folder, Settings())
            log.put("frame", (1, 1, 4.76, 1, 0, 0))
            log.put("result", {"cca_rho": .8, "fbcca_score": 1.5})
            log.close()
            self.assertIsNone(log.error)
            self.assertEqual(len((Path(folder) / "frames.csv").read_text().splitlines()), 2)
            self.assertEqual(json.loads((Path(folder) / "analysis.jsonl").read_text())["fbcca_score"], 1.5)
            self.assertTrue(json.loads((Path(folder) / "logging_status.json").read_text())["writer_finished"])

    def test_long_swap_gap_invalidates_window_and_ignores_old_worker_result(self):
        worker, log = Mock(), Mock()
        worker.begin.return_value = 123.0
        worker.process.is_alive.return_value = True
        log.error, log.dropped = None, 0
        window = StimulusWindow(Settings(analysis_profile="focused"), self.app.screens()[0], worker, log)
        window.timer.stop()
        window.watchdog.stop()
        window.running = True
        window.epoch = 1.0
        window.analysis_epoch = 100.0
        window.warmup_start = 1.0
        window.clock.last = 1.0
        window.clock.period = 1 / 210
        window.result = {"valid": True, "cca_frequency": 17}
        with patch("ssvep_realtime_test.stimulus.time.perf_counter", return_value=4.0):
            window.swapped()
        worker.begin.assert_called_once()
        self.assertFalse(window.result["valid"])
        self.assertEqual(window.analysis_epoch, 123.0)
        self.assertEqual(log.put.call_args_list[0].args[1]["event"], "stimulus_gap")
        worker.poll.return_value = {"valid": True, "analysis_epoch": 100.0, "cca_frequency": 17}
        window.poll()
        self.assertFalse(window.result["valid"])
        worker.poll.return_value = {"valid": False, "analysis_epoch": 123.0, "status": "积累 EEG"}
        window.poll()
        self.assertEqual(window.result["status"], "积累 EEG")
        window.destroy()

    def test_baseline_action_starts_response_only_at_next_swap(self):
        worker, log = Mock(), Mock()
        log.error, log.dropped = None, 0
        worker.process.is_alive.return_value = True
        worker.begin.return_value = 124
        worker.poll.return_value = {"valid": False, "action": "start_response", "analysis_epoch": 123}
        window = StimulusWindow(Settings(analysis_profile="focused"), self.app.screens()[0], worker, log)
        window.timer.stop()
        window.watchdog.stop()
        window.running, window.analysis_epoch = True, 123
        window.clock.last, window.clock.period = 1., 1 / 210
        window.poll()
        self.assertEqual(window.phase, "response")
        self.assertTrue(window.begin_pending)
        worker.begin.assert_not_called()
        with patch("ssvep_realtime_test.stimulus.time.perf_counter", return_value=1.005):
            window.swapped()
        worker.begin.assert_called_once_with(mode="response")
        self.assertEqual(window.analysis_epoch, 124)
        window.destroy()

    def test_short_diagnostic_delay_preserves_static_baseline_but_resets_flicker(self):
        for phase, reset in (("baseline", False), ("response", True), ("test", True)):
            with self.subTest(phase=phase):
                worker, log = Mock(), Mock()
                worker.begin.return_value = 124
                window = StimulusWindow(Settings(), self.app.screens()[0], worker, log)
                window.timer.stop()
                window.watchdog.stop()
                window.running, window.phase = True, phase
                window.epoch, window.analysis_epoch = 1., 123
                window.clock.last, window.clock.period = 1., 1 / 210
                window.result = {"valid": True, "calibration": {"collected": 4}}
                with patch("ssvep_realtime_test.stimulus.time.perf_counter", return_value=1.139):
                    window.swapped()
                self.assertEqual(worker.begin.called, reset)
                self.assertEqual(window.analysis_epoch, 124 if reset else 123)
                if not reset:
                    self.assertEqual(window.result["calibration"]["collected"], 4)
                window.destroy()

    def test_diagnostic_panel_and_static_gray_baseline(self):
        analyzer = Analyzer(Settings(analysis_profile="focused", harmonics=2))
        analyzer.feed(samples(17, 1000, 0, 4000, np.random.default_rng(3)), 1000)
        result = analyzer.result()
        result.update(detected=False, decision="未检出可靠响应")
        panel = render_panel(result, 1, 17)
        self.assertFalse(panel.isNull())
        image = QImage(2560, 1440, QImage.Format_RGB32)
        painter = QPainter(image)
        draw_scene(painter, 2560, 1440, 1, Settings(), None, result, "静态基线", panel=panel)
        painter.end()
        self.assertEqual(image.pixelColor(1280, 720).name(), "#808080")
        self.assertIn("未检出可靠响应", result_lines(result))


if __name__ == "__main__":
    unittest.main()
