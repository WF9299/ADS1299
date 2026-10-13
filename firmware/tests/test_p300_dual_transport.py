"""Offline integration tests; never activate a real hotspot or open a real COM port."""
from __future__ import annotations
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.p300_common import EegBatch, encode_eeg_batch, crc32c
from tools.p300_serial_protocol import *
from tools.p300_serial import SerialSession, SerialReceiver
from tools.p300_discovery import new_pairing, sign, make_discovery_reply
from tools.p300_windows_network import parse_personal_wifi_profile, NetworkUnavailableError, ProtectedSettings, hotspot_status
from tools.p300_marker_client import monotonic_us, build_time_anchor, P300MarkerClient, MarkerStatus
from tools.p300_bias import BiasControlClient, BiasResultData, BiasStatus, BiasFlags, build_bias_control_result, parse_bias_control_request
from tools.p300_tx_control import TxControlClient, TxResultData, TxStatus, TxFlags, build_tx_control_result, parse_tx_control_request
from tools.p300_self_test import SelfTestClient, SelfTestResultData, SelfTestStatus, SelfTestFlags, build_self_test_result, parse_self_test_request
from tools.p300_impedance import ImpedanceClient

class FakePort:
    """In-memory peer, including management, four timestamps and existing controls."""
    def __init__(self, **kwargs):
        self.port = kwargs.get("port")
        self.baudrate = kwargs.get("baudrate")
        self.dtr = self.rts = False
        self.closed = False
        self.buffer = bytearray()
        self.condition = threading.Condition()
        self.parser = SerialParser()
        self.mode = "serial"
        self.seq = 0
        self.stream_id = 1234
        self.config_id = 1
        self.tx_frames = 1
        self.epoch_ready = False
        self.streaming = False
        self.commands = []

    def open(self):
        self.producer = threading.Thread(target=self.produce, daemon=True)
        self.producer.start()
        self.feed(b"ESP-ROM: boot log\r\n")

    def close(self):
        self.closed = True
        with self.condition:
            self.condition.notify_all()
        self.producer.join(timeout=1)

    @property
    def in_waiting(self):
        with self.condition:
            return len(self.buffer)

    def read(self, n):
        with self.condition:
            if not self.buffer and not self.closed:
                self.condition.wait(.05)
            output = bytes(self.buffer[:n])
            del self.buffer[:n]
            return output

    def feed(self, data):
        with self.condition:
            self.buffer.extend(data)
            self.condition.notify_all()

    def reply(self, kind, request_id, payload=b""):
        self.feed(encode_frame(kind, request_id, payload))

    def config(self):
        self.reply(CONFIG, 0, json.dumps({"device_id": "TEST_ONLY", "sample_rate_hz": 1000,
            "config_id": self.config_id, "stream_id": self.stream_id, "vref_v": 4.5,
            "pga_gain": 12, "tx_group_frames": self.tx_frames}).encode())

    def produce(self):
        deadline = time.monotonic()
        while not self.closed:
            if self.streaming and self.epoch_ready:
                batch = EegBatch(self.stream_id, self.config_id, self.seq,
                    1_800_000_000_000_000+self.seq*1000, np.full((20, 8), self.seq, dtype=np.int32),
                    np.array([0]*19+[1], dtype=np.uint16))
                self.reply(EEG, self.seq, encode_eeg_batch(batch))
                self.seq += 20
            deadline += .02
            time.sleep(max(0, deadline-time.monotonic()))

    def write(self, data):
        for frame in self.parser.feed(data):
            if frame.kind == HELLO:
                self.reply(HELLO_REPLY, frame.request_id, json.dumps({"baud": json.loads(frame.payload)["baud"]}).encode())
            elif frame.kind == HEARTBEAT:
                self.reply(HEARTBEAT_REPLY, frame.request_id)
            elif frame.kind == MANAGE:
                request = json.loads(frame.payload)
                self.commands.append(request["op"])
                if request["op"] == "unix_time":
                    self.epoch_ready = True
                if request["op"] == "stream_pause":
                    self.streaming = False
                if request["op"] == "stream_restart":
                    self.streaming = True
                    self.stream_id += 1
                    self.config()
                self.reply(MANAGE_REPLY, frame.request_id, b'{"ok":true,"ads_running":true,"mode":"serial"}')
            elif frame.kind == CONTROL:
                packet = frame.payload
                kind = packet[3]
                rid = int.from_bytes(packet[4:8], "big")
                if kind == 1:
                    t1 = int.from_bytes(packet[8:16], "big")
                    t2 = monotonic_us()+123456
                    self.reply(CONTROL_REPLY, rid, struct.pack(">2sBBIQQQ", b"\xa5\x51", 1, 2, rid, t1, t2, t2+5))
                elif kind == 3:
                    status = 0 if self.config_id == 1 else 5
                    self.reply(CONTROL_REPLY, rid, struct.pack(">2sBBIB3xII", b"\xa5\x51", 1, 4, rid, status, self.seq, 100))
                elif kind == 10:
                    _, _, mask = parse_bias_control_request(packet)
                    self.reply(CONTROL_REPLY, rid, build_bias_control_result(BiasResultData(rid, BiasStatus.OK, mask, mask or 255, BiasFlags.VERIFIED, self.stream_id)))
                elif kind == 12:
                    _, _, frames = parse_tx_control_request(packet)
                    self.tx_frames = frames or self.tx_frames
                    self.reply(CONTROL_REPLY, rid, build_tx_control_result(TxResultData(rid, TxStatus.OK, frames, self.tx_frames, TxFlags.VERIFIED, self.stream_id)))
                elif kind == 8:
                    _, target = parse_self_test_request(packet)
                    self.config_id = target
                    self.stream_id += 1
                    self.reply(CONTROL_REPLY, rid, build_self_test_result(SelfTestResultData(rid, SelfTestStatus.OK, target, target, SelfTestFlags.CHANGED, self.stream_id)))
                    self.config()
                elif kind == 6:
                    prefix = struct.pack(">2sBBIBBBBHH8I8I", b"\xa5\x51", 1, 7, rid, 0, 255, 0, 3, 2000, 1024,
                                         *([10000]*8), *([305577]*8))
                    self.reply(CONTROL_REPLY, rid, prefix+struct.pack(">I", crc32c(prefix)))
        return len(data)

class DualTransportTests(unittest.TestCase):
    def test_envelope_fragmentation_noise_and_limits(self):
        frame = encode_frame(EEG, 0x10203040, b"hello")
        parser = SerialParser()
        output = []
        for byte in b"boot text\r\n"+frame+frame:
            output += parser.feed(bytes([byte]))
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0].payload, b"hello")
        corrupted = bytearray(frame)
        corrupted[-1] ^= 1
        self.assertEqual(parser.feed(corrupted+frame)[0].request_id, 0x10203040)
        invalid = struct.pack(">2sBBII", b"\xa5\xc3", 1, EEG, 1, 0xffffffff)
        self.assertEqual(len(parser.feed(invalid+frame)), 1)
        self.assertLessEqual(len(parser.buffer), MAX_PAYLOAD+16)
        with self.assertRaises(ValueError):
            encode_frame(EEG, 1, bytes(MAX_PAYLOAD+1))

    def test_c_python_golden_and_c_parser(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder)/"serial-test.exe"
            subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-I"+str(root/"components/serial_protocol/include"), str(root/"components/serial_protocol/serial_protocol.c"),
                str(root/"tests/test_serial_protocol.c"), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)
            golden = subprocess.check_output([str(binary), "--golden"]).decode().strip()
            self.assertEqual(golden, encode_frame(EEG, 0x10203040, b"hello").hex())

    def test_pairing_rejects_unknown_wrong_key_nonce_and_port(self):
        pairing = new_pairing()
        nonce = "01"*16
        request = {"type": "P300_DISCOVER", "version": 1, "monitor_id": pairing["monitor_id"], "nonce": nonce}
        request["signature"] = sign(pairing["pair_key"], f"P300_DISCOVER|1|{pairing['monitor_id']}|{nonce}")
        reply = make_discovery_reply(request, pairing, 1234)
        self.assertEqual(reply["port"], 1234)
        for change in ({"signature": "0"*64}, {"monitor_id": "other"}, {"nonce": "evil"}, {"version": 2}):
            self.assertIsNone(make_discovery_reply(request|change, pairing, 1234))
        self.assertIsNone(make_discovery_reply(request, pairing, 0))

    def test_time_anchor_wire_layout_crc(self):
        packet = build_time_anchor(123, 1800000000000000, 123456789)
        self.assertEqual(len(packet), 28)
        self.assertEqual(struct.unpack_from(">I", packet, 24)[0], crc32c(packet[:24]))

    def test_campus_profile_is_not_exported_as_psk(self):
        profile = '<WLANProfile><SSIDConfig><SSID><name>Campus</name></SSID></SSIDConfig><MSM><security><authEncryption><authentication>WPA2</authentication><useOneX>true</useOneX></authEncryption></security></MSM></WLANProfile>'
        with self.assertRaises(NetworkUnavailableError):
            parse_personal_wifi_profile(profile)
        personal = profile.replace("WPA2</", "WPA2PSK</").replace("true</useOneX", "false</useOneX").replace("</security>", "<sharedKey><protected>false</protected><keyMaterial>test-only-password</keyMaterial></sharedKey></security>")
        self.assertEqual(parse_personal_wifi_profile(personal)["ssid"], "Campus")
        with self.assertRaises(NetworkUnavailableError):
            parse_personal_wifi_profile(personal.replace("false</protected", "true</protected"))

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_protected_settings_not_plaintext_and_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ProtectedSettings(Path(folder)/"settings.dat")
            secret = "test-only-password-not-real"
            store.save({"network": {"password": secret}})
            self.assertNotIn(secret.encode(), store.path.read_bytes())
            self.assertEqual(store.load()["network"]["password"], secret)

    def test_hotspot_capability_fallback_no_system_mutation(self):
        with mock.patch("tools.p300_windows_network._tethering_manager", side_effect=NetworkUnavailableError("policy")):
            self.assertEqual(hotspot_status(), {"available": False, "reason": "policy"})

    def test_serial_session_receiver_and_all_diagnostic_controls_offline(self):
        events = queue.Queue()
        session = SerialSession("TEST_ONLY", 921600, events, port_factory=FakePort)
        receiver = None
        try:
            session.open()
            receiver = SerialReceiver(session, events)
            receiver.start()
            samples = 0
            connected = False
            deadline = time.monotonic()+2
            while time.monotonic()<deadline and samples<600:
                event = events.get(timeout=.5)
                if event[0] == "peer":
                    connected = event[1]["connected"]
                if event[0] == "batch":
                    self.assertTrue(connected)
                    self.assertEqual(event[2].samples.shape, (20, 8))
                    samples += 20
            self.assertGreaterEqual(samples, 600)
            exchange = session.control_exchange
            marker = P300MarkerClient("TEST_ONLY", exchange=exchange, sync_ready=lambda: session.port.epoch_ready)
            marker.start()
            self.assertTrue(marker.synchronized)
            self.assertEqual(marker.send_marker().status, MarkerStatus.OK)
            bias = BiasControlClient("TEST_ONLY", exchange=exchange)
            self.assertTrue(bias.query().verified)
            self.assertEqual(bias.set_mask(0x35).result.actual_mask, 0x35)
            tx = TxControlClient("TEST_ONLY", exchange=exchange)
            self.assertEqual(tx.set_frames(2).result.actual_frames, 2)
            self_test = SelfTestClient("TEST_ONLY", exchange=exchange)
            self.assertEqual(self_test.switch(2).actual_config_id, 2)
            self.assertEqual(marker.send_marker().status, MarkerStatus.ACQUISITION_PAUSED)
            self.assertEqual(self_test.switch(1).actual_config_id, 1)
            impedance = ImpedanceClient("TEST_ONLY", exchange=exchange)
            self.assertEqual(impedance.measure().sample_count, 1024)
            self.assertEqual(session.parser.errors, 0)
            self.assertIn("unix_time", session.port.commands)
            marker.stop()
        finally:
            if receiver:
                receiver.stop()
                receiver.join(timeout=2)
            session.close()

    def test_gui_endpoint_roles_and_stale_receiver_events(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from tools.p300_monitor import MonitorWindow, QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = MonitorWindow("127.0.0.1", 0, auto_start=False)
        try:
            panel = window.network_panel
            wlan = {"name": "WLAN", "description": "Wi-Fi adapter", "addresses": ["10.1.2.3"], "hotspot_candidate": False}
            hotspot = {"name": "Hotspot", "description": "Wi-Fi Direct", "addresses": ["192.168.140.1"], "hotspot_candidate": True}
            result = {"label": "test", "result": {"interfaces": [wlan, hotspot], "hotspot": {"available": False, "reason": "policy"}}}
            panel.handle("network_done", result)
            self.assertEqual(panel.endpoint.currentData(), "192.168.140.1")
            self.assertFalse(panel.hotspot_start.isEnabled())
            panel.source.setCurrentIndex(1)
            self.assertEqual(panel.endpoint.currentData(), "10.1.2.3")
            panel.handle("network_done", {"label": "test", "result": {"interfaces": [wlan], "hotspot": {"available": False, "reason": "policy"}}})
            panel.source.setCurrentIndex(0)
            self.assertEqual(panel.endpoint.currentText(), "")
            window.active_receiver_epoch = object()
            window.events.put(("receiver_epoch", object(), ("peer", {"connected": True, "host": "wrong", "port": 1234})))
            window._drain_events()
            self.assertFalse(window.connected)
        finally:
            window.close()
            app.processEvents()

if __name__ == "__main__":
    unittest.main()
