"""Runtime BIAS power protocol, transport and authoritative GUI behavior."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import queue
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.p300_bias import (
    BiasControlClient, BiasFlags, BiasOperation, BiasProtocolError, BiasResultData,
    BiasSetOutcome, BiasStatus, BiasTimeoutError, build_bias_control_request,
    build_bias_control_result, parse_bias_control_request, parse_bias_control_result,
)
from tools.p300_common import batch_to_record, crc32c
from tools.p300_serial import SerialSession
from tools.p300_simulator import SelfTestControlServer, SignalGenerator
from tools import p300_monitor
from test_p300_dual_transport import FakePort

CAP = BiasFlags.VERIFIED | BiasFlags.POWER_CONTROL


class BiasPowerTests(unittest.TestCase):
    def test_explicit_disable_and_legacy_set_remain_distinct(self):
        packet = build_bias_control_request(15, BiasOperation.DISABLE)
        self.assertEqual(len(packet), 16)
        self.assertEqual(parse_bias_control_request(packet), (15, BiasOperation.DISABLE, 0))
        for op, mask in ((BiasOperation.SET, 0), (BiasOperation.DISABLE, 1)):
            with self.assertRaises(ValueError):
                build_bias_control_request(1, op, mask)
        bad = bytearray(packet)
        bad[10] = 1
        bad[12:] = crc32c(bad[:12]).to_bytes(4, 'big')
        with self.assertRaises(BiasProtocolError):
            parse_bias_control_request(bad)
        for flags in (BiasFlags.VERIFIED, CAP):
            response = build_bias_control_result(BiasResultData(15, BiasStatus.OK, 0, 0, flags, 10))
            if flags == CAP:
                self.assertIs(parse_bias_control_result(response).enabled, False)
            else:
                with self.assertRaises(BiasProtocolError):
                    parse_bias_control_result(response)

    def test_disable_timeout_reconciles_verified_off_but_not_failed_rollback(self):
        calls = []
        def exchange(packet, *args):
            rid, operation, mask = parse_bias_control_request(packet)
            calls.append(operation)
            if operation == BiasOperation.DISABLE:
                raise TimeoutError('reply lost')
            return build_bias_control_result(BiasResultData(rid, BiasStatus.OK, mask, 0, CAP, 77))
        outcome = BiasControlClient('TEST', exchange=exchange).disable(1)
        self.assertTrue(outcome.reconciled and outcome.applied)
        self.assertEqual(calls, [BiasOperation.DISABLE, BiasOperation.QUERY])
        failed = BiasResultData(2, BiasStatus.RESTORE_FAILED, 0, 0, BiasFlags.POWER_CONTROL, 0)
        self.assertIsNone(parse_bias_control_result(build_bias_control_result(failed)).enabled)
        self.assertFalse(BiasSetOutcome(failed, 0, 2).applied)

    def test_udp_disable_query_restore_and_cached_request(self):
        generator = SignalGenerator(seed=3)
        server = SelfTestControlServer(generator, 0)
        server.start()
        self.assertTrue(server.ready.wait(2))
        try:
            self.assertIsNone(server.error)
            client = BiasControlClient('127.0.0.1', server._socket.getsockname()[1])
            self.assertTrue(client.query().power_control_supported)
            self.assertTrue(client.set_mask(0x35).applied)
            first = client.disable(123)
            self.assertTrue(first.applied)
            self.assertTrue(client.disable(123).result.flags & BiasFlags.CACHED)
            self.assertEqual(first.result.stream_id, client.query().stream_id)
            self.assertIs(client.query().enabled, False)
            self.assertTrue(client.set_mask(0x35).applied)
            self.assertIs(client.query().enabled, True)
        finally:
            server.stop()
            server.join(2)

    def test_serial_single_owner_disable_query_and_reenable(self):
        session = SerialSession('TEST_ONLY', 921600, queue.Queue(), port_factory=FakePort)
        try:
            session.open()
            client = BiasControlClient('TEST_ONLY', exchange=session.control_exchange)
            self.assertTrue(client.query().power_control_supported)
            self.assertTrue(client.disable().applied)
            self.assertIs(client.query().enabled, False)
            self.assertTrue(client.set_mask(0x35).applied)
            self.assertEqual(client.query().actual_mask, 0x35)
        finally:
            session.close()

    def test_raw_record_carries_bias_state_and_samples_unchanged(self):
        batch = SignalGenerator(seed=3).next_batch()
        before = batch_to_record(batch)
        after = batch_to_record(batch, {'bias': 'disabled', 'bias_mask': 0, 'bias_verified': True})
        self.assertEqual(before['data'], after['data'])
        self.assertEqual(after['bias_mask'], 0)
        self.assertEqual(after['bias'], 'disabled')
        self.assertNotIn('bias', before)


@unittest.skipIf(p300_monitor.QtWidgets is None, 'Qt unavailable')
class BiasPowerGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = p300_monitor.QtWidgets.QApplication.instance() or p300_monitor.QtWidgets.QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.window = p300_monitor.MonitorWindow('127.0.0.1', 0,
            http_settings_path=Path(self.folder.name)/'http.json', auto_start=False)
        self.window.connected = True
        self.window.peer_ip = 'TEST'
        self.window.has_valid_eeg = True

    def tearDown(self):
        self.window.writer = self.window.http_sender = None
        self.window.close()
        self.app.processEvents()
        self.folder.cleanup()

    def deliver(self, mask, flags=CAP, status=BiasStatus.OK, outcome=False):
        self.window.bias_last_request_id = 55
        self.window.bias_pending = True
        result = BiasResultData(55, status, mask, mask, flags, 80)
        self.window._handle_bias_event({'state': 'result', 'request_id': 55,
            'result': BiasSetOutcome(result, mask, 55) if outcome else result})

    def test_draft_disable_is_explicit_and_applied_off_keeps_channels(self):
        self.deliver(0x35)
        self.assertTrue(self.window.bias_enabled_checkbox.isEnabled())
        client = Mock()
        client.start_disable.return_value = 55
        client.start_set.return_value = 56
        self.window._ensure_bias_client = lambda: client
        self.window.bias_enabled_checkbox.setChecked(False)
        self.assertEqual(self.window.bias_actual_mask, 0x35)
        self.assertEqual(self.window._bias_checkbox_mask(), 0x35)
        self.window._apply_bias_mask()
        client.start_disable.assert_called_once_with()
        self.deliver(0, outcome=True)
        self.assertEqual(self.window.bias_actual_mask, 0)
        self.assertIn('关闭', self.window.bias_actual_label.text())
        self.assertEqual(self.window._bias_checkbox_mask(), 0x35)
        self.window.bias_enabled_checkbox.setChecked(True)
        self.window._apply_bias_mask()
        client.start_set.assert_called_once_with(0x35)

    def test_legacy_firmware_has_mask_controls_and_cannot_disable(self):
        self.deliver(0x35, BiasFlags.VERIFIED)
        self.assertFalse(self.window.bias_enabled_checkbox.isEnabled())
        self.assertTrue(self.window.bias_apply_button.isEnabled())
        self.assertIn('升级', self.window.bias_status.text())
        client = Mock()
        self.window._ensure_bias_client = lambda: client
        self.window.bias_enabled_checkbox.setChecked(False)
        self.assertFalse(self.window.bias_apply_button.isEnabled())
        self.window._apply_bias_mask()
        client.start_disable.assert_not_called()

    def test_switch_interlocks_and_unknown_state(self):
        self.deliver(0x35)
        self.window.bias_enabled_checkbox.setChecked(False)
        for attribute in ('writer', 'http_sender', 'impedance_active', 'self_test_active', 'tx_pending'):
            saved = getattr(self.window, attribute)
            setattr(self.window, attribute, True)
            self.window._update_bias_controls()
            self.assertFalse(self.window.bias_apply_button.isEnabled(), attribute)
            setattr(self.window, attribute, saved)
        self.deliver(0, BiasFlags.POWER_CONTROL, BiasStatus.INTERNAL_ERROR)
        self.assertIsNone(self.window.bias_actual_mask)
        self.assertIn('未知', self.window.bias_actual_label.text())
        self.assertFalse(self.window.bias_apply_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
