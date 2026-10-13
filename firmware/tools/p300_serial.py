"""One owner for USB serial RX/TX, management, EEG and all device controls."""
from __future__ import annotations
import json
import queue
import secrets
import struct
import threading
import time
from tools.p300_common import decode_eeg_batch
from tools.p300_marker_client import build_sync_request, parse_sync_reply, build_sync_result, monotonic_us, ClockSyncState
from tools.p300_serial_protocol import (
    SerialParser, encode_frame, HELLO, HELLO_REPLY, MANAGE, MANAGE_REPLY,
    CONFIG, EEG, CONTROL, CONTROL_REPLY, LOG, HEARTBEAT, HEARTBEAT_REPLY,
)

def serial_ports() -> list[tuple[str, str]]:
    from serial.tools import list_ports
    return [(p.device, p.description) for p in list_ports.comports()]

class SerialSession:
    def __init__(self, port: str, baud: int, events: queue.Queue, *, port_factory=None):
        self.port_name, self.baud, self.events = port, int(baud), events
        self.port_factory = port_factory
        self.port = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.tx_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending: dict[tuple[int, int], queue.Queue] = {}
        self.reader = self.sync_thread = None
        self.parser = SerialParser()
        self.config = None
        self.forward_eeg = False
        self.connected = False
        self.dropped_events = 0
        self.last_heartbeat_reply = 0.0
        self.last_status: dict = {}
        self.peer_announced = False
        self.event_epoch = None
        self.sync_state = ClockSyncState(max_rtt_us=50_000, min_rtt_slack_us=5_000)

    def emit(self, event):
        if event[0].startswith("usb_"):
            event = (event[0], {**event[1], "session": self})
        if self.event_epoch is not None and event[0] in {"receiver", "peer", "config", "batch"}:
            event = ("receiver_epoch", self.event_epoch, event)
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1

    def send(self, kind, request_id, payload=b""):
        frame = encode_frame(kind, request_id, payload)
        with self.tx_lock:
            if self.stop_event.is_set() or self.port is None:
                raise ConnectionError("USB session closed")
            if self.port.write(frame) != len(frame):
                raise OSError("incomplete USB frame write")

    def _request(self, kind, expected, request_id, payload, timeout, retries=0, cancel=None):
        key = (expected, request_id)
        replies = queue.Queue(maxsize=4)
        with self.pending_lock:
            if key in self.pending:
                raise RuntimeError("request ID already pending")
            self.pending[key] = replies
        try:
            for _ in range(retries + 1):
                self.send(kind, request_id, payload)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if self.stop_event.is_set() or (cancel is not None and cancel.is_set()):
                        raise ConnectionError("USB request cancelled")
                    try:
                        response = replies.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
                        return response
                    except queue.Empty:
                        pass
            raise TimeoutError("USB request timed out")
        finally:
            with self.pending_lock:
                self.pending.pop(key, None)

    def manage(self, op, *, timeout=2.0, retries=0, **values):
        payload = json.dumps({"op": op, **values}, ensure_ascii=False, separators=(",", ":")).encode()
        data, _ = self._request(MANAGE, MANAGE_REPLY, secrets.randbits(32) or 1, payload, timeout, retries)
        result = json.loads(data)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(result.get("error", "management failed") if isinstance(result, dict) else "invalid management response")
        self.last_status = result
        if op in {"mode", "stream_restart"}:
            self.sync_state.reset()
        self.emit(("usb_status", result))
        return result

    def control_exchange(self, packet, timeout, retries=0, cancel=None):
        if len(packet) < 8:
            raise ValueError("invalid device control packet")
        request_id = struct.unpack_from(">I", packet, 4)[0]
        response, _ = self._request(CONTROL, CONTROL_REPLY, request_id, packet, timeout, retries, cancel)
        return response

    def open(self):
        try:
            self._open()
        except Exception:
            self.close()
            raise

    def _open(self):
        if self.baud not in (921600, 460800):
            raise ValueError("supported stream rates: 921600, 460800")
        if self.port_factory is None:
            import serial
            self.port_factory = serial.Serial
        # Create closed, set DTR/RTS before opening; do not enter manual boot mode.
        self.port = self.port_factory(port=None, baudrate=115200, timeout=.05, write_timeout=1)
        self.port.dtr = self.port.rts = False
        self.port.port = self.port_name
        self.port.open()
        self.reader = threading.Thread(target=self._read_loop, daemon=True, name="p300-usb-rx")
        self.reader.start()
        payload = json.dumps({"baud": self.baud}).encode()
        try:
            data, _ = self._request(HELLO, HELLO_REPLY, secrets.randbits(32) or 1, payload, 2)
        except TimeoutError:
            # A just-closed prior session reverts to 115200 after its 5 s lease expires.
            if self.stop_event.wait(3.3):
                raise ConnectionError("USB connection cancelled")
            data, _ = self._request(HELLO, HELLO_REPLY, secrets.randbits(32) or 1, payload, 2)
        hello = json.loads(data)
        if not isinstance(hello, dict) or hello.get("baud") != self.baud:
            raise RuntimeError("device rejected serial rate")
        time.sleep(.10)
        with self.tx_lock:
            self.port.baudrate = self.baud
        self.manage("confirm", timeout=2, retries=1)
        self.connected = True
        self.last_heartbeat_reply = time.monotonic()
        self.ready.set()
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="p300-usb-sync")
        self.sync_thread.start()

    def _read_loop(self):
        heartbeat_at = time.monotonic()
        try:
            while not self.stop_event.is_set():
                available = max(1, min(4096, self.port.in_waiting))
                chunk = self.port.read(available)
                if chunk:
                    for frame in self.parser.feed(chunk):
                        received_us = monotonic_us()
                        if frame.kind == HEARTBEAT_REPLY:
                            self.last_heartbeat_reply = time.monotonic()
                        with self.pending_lock:
                            target = self.pending.get((frame.kind, frame.request_id))
                        if target is not None:
                            try:
                                target.put_nowait((frame.payload, received_us))
                            except queue.Full:
                                pass
                        elif frame.kind == CONFIG:
                            config = json.loads(frame.payload)
                            if not isinstance(config, dict):
                                raise ValueError("invalid serial STREAM_CONFIG")
                            self.config = config
                            if self.forward_eeg:
                                if not self.peer_announced:
                                    self.emit(("peer", {"connected": True, "host": self.port_name, "port": self.baud}))
                                    self.emit(("receiver", {"state": "connected", "message": "串口 EEG 已连接"}))
                                    self.peer_announced = True
                                self.emit(("config", config))
                        elif frame.kind == EEG and self.forward_eeg and self.config is not None:
                            self.emit(("batch", self.config, decode_eeg_batch(frame.payload)))
                        elif frame.kind == LOG:
                            try:
                                message = json.loads(frame.payload.decode("utf-8", errors="replace"))
                            except ValueError:
                                message = {"message": "Invalid diagnostic log frame suppressed"}
                            self.emit(("usb_log", message))
                if self.connected and time.monotonic() >= heartbeat_at:
                    self.send(HEARTBEAT, secrets.randbits(32) or 1)
                    heartbeat_at = time.monotonic() + 1
                    if time.monotonic() - self.last_heartbeat_reply > 4:
                        raise TimeoutError("USB heartbeat lost")
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            if not self.stop_event.is_set():
                self.emit(("usb_error", {"error": str(error)}))
        finally:
            self.stop_event.set()
            self.connected = False
            self.ready.clear()
            self.sync_state.reset()
            self._close_port()

    def _sync_loop(self):
        while not self.stop_event.is_set():
            start = time.monotonic()
            self.sync_state.expire_if_needed(start)
            if self.last_status.get("mode") == "wifi":
                # The UDP clock owns Wi-Fi timing. USB remains a management link.
                try:
                    self.manage("status", timeout=1)
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    if not self.stop_event.is_set():
                        self.emit(("usb_sync", {"error": str(error)}))
                self.stop_event.wait(.5)
                continue
            request_id = secrets.randbits(32) or 1
            t1 = monotonic_us()
            packet = build_sync_request(request_id, t1)
            try:
                data, t4 = self._request(CONTROL, CONTROL_REPLY, request_id, packet, .2)
                echoed_id, echoed_t1, t2, t3 = parse_sync_reply(data)
                rtt = (t4-t1)-(t3-t2)
                if echoed_id != request_id or echoed_t1 != t1 or rtt < 0:
                    raise ValueError("invalid USB synchronization reply")
                accepted = self.sync_state.observe(t1, t2, t3, t4)
                if accepted:
                    self.send(CONTROL, request_id, build_sync_result(request_id, t1, t2, t3, t4))
                    self.emit(("usb_sync", {"rtt_us": rtt, "valid": self.sync_state.valid_measurements()}))
                    # Device consumes synchronization on its control task before anchoring.
                    time.sleep(.015)
                    self.manage("unix_time", unix_us=time.time_ns()//1000, pc_monotonic_us=monotonic_us(), timeout=1)
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                if not self.stop_event.is_set():
                    self.emit(("usb_sync", {"error": str(error)}))
            self.stop_event.wait(max(0, 1.0/self.sync_state.rate_hz()-(time.monotonic()-start)))

    def _close_port(self):
        with self.tx_lock:
            if self.port is not None:
                try:
                    self.port.close()
                except OSError:
                    pass

    def close(self):
        self.stop_event.set()
        self.connected = False
        self.ready.clear()
        self.sync_state.reset()
        self._close_port()
        for thread in (self.reader, self.sync_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)

class SerialReceiver(threading.Thread):
    def __init__(self, session: SerialSession, events: queue.Queue):
        super().__init__(daemon=True, name="p300-usb-receiver")
        self.session, self.events = session, events
        self.stop_event = threading.Event()
        self.parser_errors = self.dropped_events = 0
        self.event_epoch = None

    def emit(self, event):
        self.events.put(("receiver_epoch", self.event_epoch, event) if self.event_epoch is not None else event)

    def stop(self):
        self.stop_event.set()
        self.session.forward_eeg = False

    def run(self):
        try:
            self.session.event_epoch = self.event_epoch
            self.session.forward_eeg = True
            self.session.config = None
            self.session.peer_announced = False
            self.session.manage("mode", mode="serial")
            # A fresh confirmed session must receive authoritative config, not cached data.
            self.session.manage("stream_restart")
            while not self.stop_event.wait(.1) and self.session.connected:
                self.parser_errors = self.session.parser.errors
                self.dropped_events = self.session.dropped_events
        except (OSError, RuntimeError, TimeoutError) as error:
            self.emit(("receiver", {"state": "error", "message": str(error)}))
        finally:
            self.session.forward_eeg = False
            try:
                if self.session.connected:
                    self.session.manage("stream_pause", timeout=1)
            except (OSError, RuntimeError, TimeoutError):
                pass
            self.emit(("peer", {"connected": False, "host": self.session.port_name, "port": self.session.baud}))
            self.emit(("receiver", {"state": "stopped", "message": "串口 EEG 已停止"}))
