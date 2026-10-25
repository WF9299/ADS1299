"""HTTP output mapping must preserve acquired data and downstream time boundaries."""
import copy
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
from tools import p300_monitor
from tools.p300_common import EegBatch, batch_to_record
from tools.p300_http_push import (
    HTTP_CHANNEL_MODE_RAW, HTTP_CHANNEL_MODE_SIX, HttpPushSettings, map_http_channels,
)

# Integration uses the colocated SSVEP receiver, without binding a hardware port.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ssvep_realtime_test.receiver import create_server


def special(source7=3, source8=6, port=25001):
    return HttpPushSettings("127.0.0.1", port, "/api/bci/data", HTTP_CHANNEL_MODE_SIX, source7, source8)


def batch_record(index):
    values = np.arange(160, dtype=np.int32).reshape(20, 8) + index * 1000
    markers = np.zeros(20, dtype=np.uint16)
    markers[index % 20] = index + 1
    batch = EegBatch(123, 1, index * 20, 1_700_000_000_000_000 + index * 20_000, values, markers)
    return batch, batch_to_record(batch, {"vref_v": 4.5, "pga_gain": 12, "bias_mask": 255})


class ChannelSettingsTests(unittest.TestCase):
    def test_v1_migrates_without_redirecting_and_v2_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            old = {"version": 1, "server_ip": "10.20.30.40", "port": 25002, "endpoint": "/custom"}
            path.write_text(json.dumps(old), encoding="utf-8")
            settings, warning = HttpPushSettings.load_or_default(path)
            self.assertIsNone(warning)
            self.assertEqual(settings, HttpPushSettings("10.20.30.40", 25002, "/custom"))
            self.assertEqual(json.loads(path.read_text()), old)
            special(1, 4).save(path)
            loaded, warning = HttpPushSettings.load_or_default(path)
            self.assertIsNone(warning)
            self.assertEqual(loaded, special(1, 4))
            self.assertEqual(json.loads(path.read_text())["version"], 2)
            bad = json.loads(path.read_text())
            bad["channel8_source"] = 1
            path.write_text(json.dumps(bad))
            loaded, warning = HttpPushSettings.load_or_default(path)
            self.assertEqual(loaded, HttpPushSettings.defaults())
            self.assertIn("不同", warning)

    def test_invalid_sources_rejected_and_raw_mode_keeps_legacy_payload(self):
        for first, second in ((0, 2), (7, 2), (True, 2), (1.0, 2), ("1", 2), (3, 3)):
            with self.subTest(first=first, second=second), self.assertRaises(ValueError):
                special(first, second).normalized()
        with self.assertRaisesRegex(ValueError, "模式"):
            HttpPushSettings(channel_mode="invalid").normalized()
        _, record = batch_record(0)
        self.assertIs(map_http_channels(record, HttpPushSettings()), record)
        self.assertNotIn("channel_map", record)

    def test_every_distinct_pair_preserves_raw_rows_markers_and_metadata(self):
        batch, record = batch_record(0)
        original = copy.deepcopy(record)
        original_samples = batch.samples.copy()
        for first in range(1, 7):
            for second in range(1, 7):
                if first == second:
                    continue
                with self.subTest(first=first, second=second):
                    mapped = map_http_channels(record, special(first, second))
                    source = np.asarray(original["data"])
                    np.testing.assert_array_equal(mapped["data"], source[:, [0, 1, 2, 3, 4, 5, first - 1, second - 1]])
                    for key in set(record) - {"data"}:
                        self.assertEqual(mapped[key], original[key])
                    mapped["data"][0][6] += 123
                    self.assertEqual(record, original)
                    np.testing.assert_array_equal(batch.samples, original_samples)


@unittest.skipIf(p300_monitor.QtWidgets is None, "Qt unavailable")
class ChannelGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = p300_monitor.QtWidgets.QApplication.instance() or p300_monitor.QtWidgets.QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "http.json"
        HttpPushSettings("127.0.0.1", 25001).save(self.path)
        self.window = p300_monitor.MonitorWindow("127.0.0.1", 0, http_settings_path=self.path, auto_start=False)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.folder.cleanup()

    def select_special(self, first=3, second=6):
        self.window.http_channel_mode_combo.setCurrentIndex(1)
        self.window.http_channel7_combo.setCurrentIndex(first - 1)
        self.window.http_channel8_combo.setCurrentIndex(second - 1)

    def test_preset_independence_duplicate_feedback_and_active_lock(self):
        w = self.window
        self.assertEqual(w.http_channel_mode_combo.currentData(), HTTP_CHANNEL_MODE_RAW)
        self.assertFalse(w.http_channel7_combo.isEnabled())
        self.select_special()
        saved = self.path.read_bytes()
        w.http_preset_combo.setCurrentIndex(1)
        self.assertEqual(w.http_channel7_combo.currentData(), 3)
        self.assertEqual(w.http_channel8_combo.currentData(), 6)
        self.assertEqual(self.path.read_bytes(), saved)
        self.select_special(3, 3)
        self.assertFalse(w.http_button.isEnabled())
        self.assertIn("不同", w.http_channel_summary.text())
        w._start_http_push()
        self.assertIsNone(w.http_sender)
        self.assertIn("不同", w.http_status.text())
        self.select_special()
        w.http_preset_combo.setCurrentIndex(0)
        w._start_http_push()  # No EEG submitted, so no network request occurs.
        self.assertIsNotNone(w.http_sender)
        for combo in (w.http_channel_mode_combo, w.http_channel7_combo, w.http_channel8_combo):
            self.assertFalse(combo.isEnabled())
        w.http_channel_mode_combo.setCurrentIndex(0)
        w.http_channel7_combo.setCurrentIndex(0)
        self.assertEqual(w.http_channel_mode_combo.currentData(), HTTP_CHANNEL_MODE_SIX)
        self.assertEqual(w.http_channel7_combo.currentData(), 3)
        loaded, warning = HttpPushSettings.load_or_default(self.path)
        self.assertIsNone(warning)
        self.assertEqual(loaded, special())
        # A partial old segment is cleared on stop and never reused on restart.
        w.http_aggregator.ingest(*batch_record(0))
        self.assertEqual(w.http_aggregator.buffered_samples, 20)
        w._stop_http_push()
        self.assertEqual(w.http_aggregator.buffered_samples, 0)
        self.assertTrue(w.http_channel7_combo.isEnabled())
        w.http_channel_mode_combo.setCurrentIndex(0)
        w._start_http_push()
        self.assertEqual(w.http_settings.channel_mode, HTTP_CHANNEL_MODE_RAW)
        self.assertEqual(w.http_aggregator.buffered_samples, 0)

    def test_real_http_packet_preserves_physical_record_and_bias_metadata(self):
        packets = queue.Queue()
        server = create_server(0, packets)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
        thread.start()
        try:
            self.select_special()
            w = self.window
            w.http_port_edit.setValue(server.server_port)
            w.model.config.update(vref_v=4.5, pga_gain=12)
            w._start_http_push()
            raw = []
            expected_markers = []
            for index in range(10):
                batch, record = batch_record(index)
                before = copy.deepcopy(record)
                w._submit_http_batch(batch, record)
                self.assertEqual(record, before)
                raw.extend(before["data"])
                expected_markers.extend(before["markers"])
            received = packets.get(timeout=3)
            np.testing.assert_array_equal(received["data"], np.asarray(raw)[:, [0, 1, 2, 3, 4, 5, 2, 5]])
            self.assertEqual(received["markers"], expected_markers)
            self.assertEqual(received["timestamp"], 1_700_000_000.0)
            self.assertEqual(received["sampling_rate"], 1000)
            self.assertEqual(received["channel_map"], [1, 2, 3, 4, 5, 6, 3, 6])
            self.assertEqual(received["pga_gain"], 12)
            # Inspect the pre-HTTP object too: BIAS describes physical channels.
            w._stop_http_push()
            w.http_sender = Mock()
            for index in range(10):
                w._submit_http_batch(*batch_record(index))
            pushed = w.http_sender.submit.call_args.args[0]
            self.assertEqual(pushed["bias_mask"], 255)
            self.assertEqual(pushed["channels"], 8)
            self.assertEqual(pushed["units"], "uV")
            w.http_sender = None
        finally:
            self.window._stop_http_push()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
