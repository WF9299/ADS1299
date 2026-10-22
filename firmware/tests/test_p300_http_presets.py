import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from tools.p300_http_push import HTTP_PUSH_PRESETS, HttpPushSettings
from tools import p300_monitor


@unittest.skipIf(p300_monitor.QtWidgets is None, "Qt unavailable")
class HttpPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = p300_monitor.QtWidgets.QApplication.instance() or p300_monitor.QtWidgets.QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "http.json"
        HttpPushSettings("127.0.0.1", 25001, "/custom/path").save(self.path)
        self.saved = self.path.read_bytes()
        self.window = p300_monitor.MonitorWindow("127.0.0.1", 0, http_settings_path=self.path, auto_start=False)

    def tearDown(self):
        self.window.http_sender = None
        self.window.close()
        self.app.processEvents()
        self.folder.cleanup()

    def test_exact_presets_fill_fields_preserve_path_and_do_not_start_or_save(self):
        self.assertEqual(HTTP_PUSH_PRESETS, (("SSVEP测试", "127.0.0.1", 25001), ("SEU电视端", "10.201.1.182", 25000)))
        self.assertEqual(self.window.http_preset_combo.currentIndex(), 0)
        for index, (_label, ip, port) in enumerate(HTTP_PUSH_PRESETS):
            self.window.http_preset_combo.setCurrentIndex(index)
            self.assertEqual((self.window.http_ip_edit.text(), self.window.http_port_edit.value()), (ip, port))
            self.assertEqual(self.window.http_endpoint_edit.text(), "/custom/path")
            self.assertIsNone(self.window.http_sender)
            self.assertEqual(self.path.read_bytes(), self.saved)

    def test_manual_fields_match_preset_or_remain_custom(self):
        self.window.http_ip_edit.setText("10.2.3.4")
        self.window.http_port_edit.setValue(34567)
        self.assertEqual(self.window.http_preset_combo.currentIndex(), 2)
        self.window.http_preset_combo.setCurrentIndex(1)
        self.assertEqual(self.window.http_ip_edit.text(), "10.201.1.182")
        self.window.http_port_edit.setValue(25001)
        self.assertEqual(self.window.http_preset_combo.currentIndex(), 2)

    def test_active_sender_locks_preset_and_cannot_be_redirected(self):
        sender = Mock(settings=HttpPushSettings("127.0.0.1", 25001, "/custom/path"))
        self.window.http_sender = sender
        self.window._update_http_controls()
        self.assertFalse(self.window.http_preset_combo.isEnabled())
        self.window.http_preset_combo.setCurrentIndex(1)
        self.assertEqual(self.window.http_preset_combo.currentIndex(), 0)
        self.assertEqual((self.window.http_ip_edit.text(), self.window.http_port_edit.value()), ("127.0.0.1", 25001))
        sender.start.assert_not_called()
        sender.submit.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.saved)
        self.window.http_sender = None
        self.window._update_http_controls()
        self.assertTrue(self.window.http_preset_combo.isEnabled())


if __name__ == "__main__":
    unittest.main()
