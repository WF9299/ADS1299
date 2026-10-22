import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import numpy as np
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication
from ssvep_realtime_test.config import Settings
from ssvep_realtime_test.display import frequency_buttons
from ssvep_realtime_test.receiver import WorkerController
from ssvep_realtime_test.stimulus import StimulusWindow
from ssvep_realtime_test.simulator import samples
from test_receiver import free_port, payload, post


def wait_result(controller, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = controller.poll()
        if result is not None and predicate(result):
            return result
        time.sleep(.025)
    raise AssertionError("没有等到预期接收结果")


class FrequencySwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        worker, log = Mock(), Mock()
        worker.begin.return_value = 200.
        worker.process.is_alive.return_value = True
        log.error, log.dropped = None, 0
        window = StimulusWindow(Settings(), self.app.screens()[0], worker, log)
        window.timer.stop()
        window.watchdog.stop()
        window.running = True
        window.epoch, window.analysis_epoch, window.warmup_start = 1., 100., 0.
        window.clock.last, window.clock.period = 1., 1 / 210
        window.resize(1280, 720)
        return window, worker, log

    def test_switch_is_committed_only_after_new_frequency_frame_and_old_result_is_ignored(self):
        window, worker, log = self.window()
        try:
            window.result = {"valid": True, "cca_frequency": 17}
            window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
            self.assertEqual(window.pending_frequency, 17.5)
            self.assertFalse(window.result["valid"])
            self.assertEqual(window.config.frequency, 17)
            worker.begin.assert_not_called()
            worker.poll.return_value = {"valid": True, "cca_frequency": 17, "analysis_epoch": 100}
            window.poll()
            self.assertFalse(window.result["valid"])
            # The old queued frame swapping must not label the new trial yet.
            with patch("ssvep_realtime_test.stimulus.time.perf_counter", return_value=1.005):
                window.swapped()
            worker.begin.assert_not_called()
            # Emulate a hidden new-frequency frame; no visible flashing required.
            window.painted_frequency = 17.5
            with patch("ssvep_realtime_test.stimulus.time.perf_counter", return_value=1.01), patch("ssvep_realtime_test.stimulus.time.time", return_value=199.5):
                window.swapped()
            worker.begin.assert_called_once_with(timestamp=199.5, mode="test", frequency=17.5, trial_id=1, transition_seconds=.5)
            self.assertEqual(window.analysis_epoch, 200)
            self.assertEqual(window.config.switch_frequencies, tuple(np.arange(13, 21.01, .5)))
            self.assertEqual(log.put.call_args_list[-2].args[1]["event"], "frequency_change")
            window.poll()
            self.assertFalse(window.result["valid"])
        finally:
            window.destroy()

    def test_mouse_jump_endpoints_home_and_cancel_unpresented_switch(self):
        window, worker, _ = self.window()
        try:
            rect, frequency = frequency_buttons(1280, 720, window.config)[-1]
            event = Mock()
            event.button.return_value, event.localPos.return_value = Qt.LeftButton, rect.center()
            window.mousePressEvent(event)
            self.assertEqual(window.pending_frequency, 21)
            window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
            self.assertEqual(window.pending_frequency, 21)
            window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Home, Qt.NoModifier))
            self.assertIsNone(window.pending_frequency)
            self.assertEqual(window.trial_id, 0)
            worker.begin.assert_not_called()
        finally:
            window.destroy()

    def test_request_arriving_after_paint_records_every_actually_swapped_frequency(self):
        window, worker, _ = self.window()
        try:
            window.request_frequency(17.5)
            window.painted_frequency = 17.5
            window.request_frequency(18.)
            with patch("ssvep_realtime_test.stimulus.time.perf_counter", return_value=1.005):
                window.swapped()
            self.assertEqual(window.config.frequency, 17.5)
            self.assertEqual(window.pending_frequency, 18)
            self.assertEqual(worker.begin.call_args.kwargs["frequency"], 17.5)
            window.painted_frequency = 18
            with patch("ssvep_realtime_test.stimulus.time.perf_counter", return_value=1.01):
                window.swapped()
            self.assertEqual(window.config.frequency, 18)
            self.assertEqual(window.trial_id, 2)
            self.assertIsNone(window.pending_frequency)
        finally:
            window.destroy()

    def test_real_http_switch_discards_previous_frequency_and_transition_samples(self):
        rng = np.random.default_rng(73)
        with tempfile.TemporaryDirectory() as folder:
            controller = WorkerController(Settings(port=free_port(), window_seconds=1, stale_seconds=2, update_seconds=.1), folder)
            controller.start()
            try:
                controller.begin(time.time() - 1.4, frequency=17., trial_id=1)
                post(controller.config.port, payload(time.time() - 1.2, samples(17, 1000, 0, 1200, rng)))
                initial = wait_result(controller, lambda r: r.get("valid"))
                self.assertEqual(initial["cca_frequency"], 17)
                self.assertTrue(initial["target_match"])
                self.assertFalse(initial["detected"])
                self.assertNotIn("thresholds", initial)
                onset = time.time()
                cutoff = controller.begin(onset, frequency=17.5, trial_id=2, transition_seconds=.05)
                post(controller.config.port, payload(onset - 1.1, samples(17, 1000, 0, 1000, rng)))
                reset = wait_result(controller, lambda r: r["analysis_epoch"] == cutoff)
                self.assertFalse(reset["valid"])
                self.assertNotIn("cca_frequency", reset)
                # A crossing packet contains deliberately strong old-frequency
                # data before the cutoff; only samples after it may enter the new window.
                for i in range(6):
                    start = cutoff - .1 + i * .2
                    data = samples(17.5, 1000, i * 200, 200, rng)
                    if i == 0:
                        data[:100] = samples(17, 1000, 0, 100, rng) * 100
                    time.sleep(max(0, start + .2 - time.time()))
                    self.assertEqual(post(controller.config.port, payload(start, data)), 200)
                    if i == 2:
                        partial = wait_result(controller, lambda r: r["trial_id"] == 2)
                        self.assertFalse(partial["valid"])
                fresh = wait_result(controller, lambda r: r.get("valid") and r["trial_id"] == 2)
                self.assertEqual((fresh["target_frequency"], fresh["cca_frequency"]), (17.5, 17.5))
                self.assertEqual(fresh["candidates"], list(np.arange(13.5, 21.51, .5)))
                self.assertEqual(fresh["candidate_center"], 17.5)
                self.assertGreaterEqual(fresh["analysis_data_start"], cutoff - .002)
                self.assertGreater(fresh["target_rho"], .9)
            finally:
                controller.stop()
            raw = [json.loads(s) for s in (Path(folder) / "raw_eeg.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(r["trial_id"] is None and r["control_trial_id"] == 2 for r in raw))
            self.assertTrue(any(r["trial_id"] == 2 and r["target_frequency"] == 17.5 for r in raw))


if __name__ == "__main__":
    unittest.main()
