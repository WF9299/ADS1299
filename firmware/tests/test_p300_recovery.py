
"""Recovery regressions using fake COM peers and loopback UDP only."""
from __future__ import annotations
import json
import os
import queue
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_p300_dual_transport import FakePort
from tools.p300_discovery import DiscoveryServer, new_pairing, sign
from tools.p300_marker_client import ClockSyncState
from tools.p300_serial import SerialSession
from tools.p300_serial_protocol import HELLO_REPLY


class BadHelloPort(FakePort):
    def reply(self, kind, rid, payload):
        super().reply(kind, rid, b"[]" if kind == HELLO_REPLY else payload)


class UnpluggedPort(FakePort):
    lost = False

    @property
    def in_waiting(self):
        if self.lost:
            raise OSError("fake cable disconnected")
        return super().in_waiting


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return bool(predicate())


class RecoveryTests(unittest.TestCase):
    def test_serial_clock_expires_and_relearns_a_slower_link(self):
        clock = ClockSyncState(max_rtt_us=50_000, min_rtt_slack_us=5_000)
        for i in range(16):
            self.assertTrue(clock.observe(0, 100, 100, 1000, now_s=i/10))
        self.assertEqual(clock.rate_hz(now_s=2), 2)
        self.assertFalse(clock.observe(0, 100, 100, 15000, now_s=3))
        self.assertFalse(clock.synchronized(now_s=12))
        self.assertEqual(clock.rate_hz(now_s=12), 10)
        self.assertTrue(clock.observe(0, 100, 100, 15000, now_s=12))
        self.assertEqual(clock.valid_measurements(now_s=12), 1)
        self.assertFalse(clock.observe(0, 100, 100, 51000, now_s=12.1))
        clock.reset()
        self.assertEqual(clock.valid_measurements(now_s=12.1), 0)
        # The UDP default retains its stricter RTT limit.
        self.assertFalse(ClockSyncState().observe(0, 100, 100, 15000, now_s=0))

    def test_failed_handshake_releases_port_and_reader(self):
        session = SerialSession("FAKE", 921600, queue.Queue(), port_factory=BadHelloPort)
        with self.assertRaisesRegex(RuntimeError, "rejected serial rate"):
            session.open()
        self.assertTrue(session.port.closed)
        self.assertFalse(session.connected)
        self.assertFalse(session.reader.is_alive())
        self.assertEqual(session.pending, {})

    def test_cable_loss_releases_port_without_waiting_for_gui(self):
        session = SerialSession("FAKE", 460800, queue.Queue(), port_factory=UnpluggedPort)
        try:
            session.open()
            session.port.lost = True
            self.assertTrue(wait_until(lambda: session.stop_event.is_set() and session.port.closed))
            self.assertFalse(session.connected)
            self.assertFalse(session.ready.is_set())
            self.assertTrue(wait_until(lambda: not session.sync_thread.is_alive()))
        finally:
            session.close()

    def test_wifi_management_does_not_send_serial_clock_packets(self):
        session = SerialSession("FAKE", 921600, queue.Queue())
        session.last_status = {"mode": "wifi"}
        def status(*args, **kwargs):
            session.stop_event.set()
            return {"mode": "wifi"}
        with mock.patch.object(session, "manage", side_effect=status) as manage, \
             mock.patch.object(session, "_request") as exchange:
            session._sync_loop()
        manage.assert_called_once_with("status", timeout=1)
        exchange.assert_not_called()

    def test_unicast_discovery_survives_invalid_packet_and_starts_clock(self):
        pairing = new_pairing()
        nonce = "01"*16
        request = {"type": "P300_DISCOVER", "version": 1,
                   "monitor_id": pairing["monitor_id"], "nonce": nonce}
        request["signature"] = sign(pairing["pair_key"], f"P300_DISCOVER|1|{pairing['monitor_id']}|{nonce}")
        events = queue.Queue()
        epoch = object()
        with mock.patch("tools.p300_discovery.P300MarkerClient") as factory:
            clock = factory.return_value
            clock.synchronized = True
            clock.last_sync_error = None
            server = DiscoveryServer(pairing, lambda: 1234, events, port=0)
            server.event_epoch = epoch
            server.start()
            try:
                self.assertTrue(server.ready.wait(2))
                self.assertIsNotNone(server.bound_port)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
                    peer.settimeout(2)
                    target = ("127.0.0.1", server.bound_port)
                    peer.sendto(json.dumps(request | {"signature": "\u4e2d"*64}).encode(), target)
                    peer.sendto(json.dumps(request).encode(), target)
                    reply = json.loads(peer.recvfrom(1024)[0])
                self.assertEqual(reply["nonce"], nonce)
                self.assertEqual(reply["signature"], sign(pairing["pair_key"],
                    f"P300_MONITOR|1|{pairing['monitor_id']}|{nonce}|1234"))
                event = events.get(timeout=2)
                self.assertEqual(event[:2], ("receiver_epoch", epoch))
                self.assertEqual(event[2][0], "discovery")
                factory.assert_called_once_with("127.0.0.1", anchor_unix_time=True)
                clock.start.assert_called_once()
                self.assertTrue(server.is_alive())
            finally:
                server.stop()
                server.join(timeout=2)
            self.assertFalse(server.is_alive())
            clock.stop.assert_called_once()

    def test_discovery_expires_all_slots_without_incoming_packets(self):
        server = DiscoveryServer(new_pairing(), lambda: 1234, queue.Queue(), port=0)
        stopped = [threading.Event() for _ in range(8)]
        for i, event in enumerate(stopped):
            client = mock.Mock()
            client.stop.side_effect = event.set
            server.clocks[f"192.0.2.{i+1}"] = (client, time.monotonic()-31)
        server.start()
        try:
            self.assertTrue(server.ready.wait(2))
            self.assertTrue(all(event.wait(1) for event in stopped))
            self.assertEqual(server.clocks, {})
        finally:
            server.stop()
            server.join(timeout=2)


class ConnectionRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from tools.p300_monitor import MonitorWindow, QtWidgets
        cls.Window = MonitorWindow
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.store_patch = mock.patch("tools.p300_connection_ui.ProtectedSettings")
        self.store_patch.start().return_value.load.return_value = {}
        self.ports_patch = mock.patch("tools.p300_connection_ui.serial_ports", return_value=[])
        self.ports_patch.start()
        self.window = self.Window("127.0.0.1", 0, auto_start=False)
        self.panel = self.window.network_panel

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.ports_patch.stop()
        self.store_patch.stop()

    def test_usb_pair_completion_restores_status_lost_during_handshake(self):
        status = {"device_id": "P300_TEST", "mode": "serial",
                  "ads_running": False, "ip": "0.0.0.0", "time_ready": False}
        session = mock.Mock(connected=True, last_status=status)
        # Status can arrive while the connection worker is still pairing/saving.
        self.panel.handle("usb_status", {**status, "session": session})
        self.panel.session = session
        self.panel.busy = True
        self.panel.handle("network_done", {"label": "USB 配对", "result": {"connected": True}})
        self.assertIn("USB 已连接", self.panel.usb_status.text())
        self.assertIn("P300_TEST", self.panel.usb_status.text())
        self.assertIn("ADS 未启动", self.panel.usb_status.text())
        self.assertEqual(self.panel.connect_button.text(), "断开 USB")
        self.assertFalse(self.panel.busy)

    def test_usb_connection_errors_are_visible_beside_connect_button(self):
        for error in ("PermissionError(13, Access is denied)", "USB request timed out"):
            with self.subTest(error=error):
                self.panel.handle("network_done", {"label": "USB 配对", "error": error})
                self.assertIn("USB 连接失败", self.panel.usb_status.text())
                self.assertIn(error, self.panel.usb_status.text())
        self.assertIn("USB request timed out", self.panel.operation_status.text())

    def test_usb_pending_and_disconnect_clear_previous_status(self):
        self.assertNotIn("占用", self.panel.usb_status.text())
        self.panel.com.addItem("Test port", "FAKE")
        with mock.patch.object(self.panel, "work"):
            self.panel.connect_usb()
        self.assertIn("正在连接", self.panel.usb_status.text())
        self.assertIn("FAKE", self.panel.usb_status.text())
        self.panel.handle("network_done", {"label": "断开 USB", "result": None})
        self.assertIn("USB 已断开", self.panel.usb_status.text())

    def test_network_validation_explains_disconnect_without_activating_wifi(self):
        panel = self.panel
        panel.session = mock.Mock(connected=True)
        result = {"network_verified": True, "mode": "serial", "ip": "0.0.0.0",
                  "warning": "PC settings could not be saved"}
        with mock.patch.object(self.window, "_activate_transport") as activate:
            panel.handle("network_done", {"label": "下发网络（最长约 20 秒）", "result": result})
        activate.assert_not_called()
        panel.session.manage.assert_not_called()
        self.assertIn("验证结束后 Wi-Fi 已关闭", panel.operation_status.text())
        self.assertIn("启用 Wi-Fi 数据模式", panel.operation_status.text())
        self.assertIn(result["warning"], panel.operation_status.text())
        self.assertTrue(panel.wifi_activate_button.isEnabled())

    def test_wifi_activation_button_sends_mode_before_starting_receiver(self):
        panel = self.panel
        self.assertFalse(panel.wifi_activate_button.isEnabled())
        panel.session = mock.Mock(connected=True)
        panel.mode.setCurrentIndex(panel.mode.findData("serial"))
        panel.update_buttons()
        with mock.patch.object(panel, "work", side_effect=lambda label, fn: fn()):
            with mock.patch.object(self.window, "_pause_transport_output") as pause:
                with mock.patch.object(self.window, "_activate_transport") as activate:
                    panel.wifi_activate_button.click()
                    pause.assert_called_once()
                    panel.session.manage.assert_called_once_with("mode", mode="wifi", timeout=4)
                    activate.assert_not_called()
                    panel.handle("network_done", {"label": "切换数据模式", "result": {"activate": "wifi"}})
                    activate.assert_called_once_with("wifi")
        self.assertIn("等待设备取得 IP", panel.operation_status.text())

    def test_failed_mode_change_reports_known_ads_startup_failure(self):
        self.panel.session = mock.Mock(connected=True, last_status={"ads_running": False})
        self.panel.handle("network_done", {"label": "切换数据模式", "error": "mode_busy_or_unavailable"})
        self.assertIn("ADS 未启动", self.panel.operation_status.text())
        self.assertIn("mode_busy_or_unavailable", self.panel.operation_status.text())

    def test_manual_pc_address_overrides_previous_dropdown_selection(self):
        panel = self.panel
        panel.ssid.setEditText("TestNetwork")
        panel.password.setText("test-only-password")
        panel.endpoint.addItem("Previous interface", "192.0.2.10")
        panel.endpoint.setCurrentIndex(0)
        panel.endpoint.setEditText("192.0.2.20")
        session = mock.Mock(connected=True)
        session.manage.return_value = {"ok": True}
        panel.session = session
        with mock.patch.object(panel, "work", side_effect=lambda label, fn: fn()), \
             mock.patch.object(self.window, "_pause_transport_output"):
            panel.send_network()
        self.assertEqual(session.manage.call_args.kwargs["host"], "192.0.2.20")

    def test_invalid_pc_address_does_not_stop_existing_stream(self):
        self.panel.ssid.setEditText("TestNetwork")
        self.panel.endpoint.setEditText("not-an-address")
        with mock.patch.object(self.window, "_pause_transport_output") as pause, \
             mock.patch.object(self.panel, "work") as work:
            self.panel.send_network()
        pause.assert_not_called()
        work.assert_not_called()

    def test_usb_failure_clears_serial_session_and_stops_formal_output(self):
        session = mock.Mock(connected=False)
        self.panel.session = session
        self.window.data_transport = "serial"
        with mock.patch.object(self.window, "_pause_transport_output") as pause:
            self.panel.handle("usb_error", {"error": "fake unplug", "session": session})
        pause.assert_called_once()
        session.close.assert_called_once()
        self.assertIsNone(self.panel.session)
        self.assertTrue(self.panel.com.isEnabled())

    def test_old_usb_error_cannot_close_new_session_or_wifi_stream(self):
        current = mock.Mock(connected=True)
        self.panel.session = current
        self.window.data_transport = "wifi"
        with mock.patch.object(self.window, "_pause_transport_output") as pause:
            self.panel.handle("usb_error", {"error": "old unplug", "session": object()})
            self.assertIs(self.panel.session, current)
            current.close.assert_not_called()
            self.panel.handle("usb_error", {"error": "unplug management", "session": current})
        pause.assert_not_called()
        current.close.assert_called_once()
        self.assertIsNone(self.panel.session)


if __name__ == "__main__":
    unittest.main()
