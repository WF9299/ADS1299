"""Adaptive four-timestamp UDP synchronization and P300 marker client."""

from __future__ import annotations

import argparse
import socket
import struct
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import IntEnum

MAGIC = b"\xA5\x51"
VERSION = 1
SYNC_REQ = 1
SYNC_REPLY = 2
MARKER = 3
MARKER_ACK = 4
SYNC_RESULT = 5

SYNC_REQ_STRUCT = struct.Struct(">2sBBIQ")
SYNC_REPLY_STRUCT = struct.Struct(">2sBBIQQQ")
SYNC_RESULT_STRUCT = struct.Struct(">2sBBIQQQQ")
MARKER_STRUCT = struct.Struct(">2sBBIB3xQ4x")
MARKER_ACK_STRUCT = struct.Struct(">2sBBIB3xII")


class MarkerStatus(IntEnum):
    OK = 0
    UNSYNCED = 1
    EXPIRED = 2
    INVALID_CODE = 3
    QUEUE_FULL = 4


@dataclass(frozen=True)
class MarkerAck:
    event_id: int
    status: MarkerStatus
    sample_seq: int
    error_us: int


def monotonic_us() -> int:
    return time.monotonic_ns() // 1_000


def build_sync_request(sequence: int, t1_pc_us: int) -> bytes:
    return SYNC_REQ_STRUCT.pack(MAGIC, VERSION, SYNC_REQ, sequence & 0xFFFFFFFF, t1_pc_us)


def parse_sync_reply(packet: bytes) -> tuple[int, int, int, int]:
    if len(packet) != SYNC_REPLY_STRUCT.size:
        raise ValueError("invalid SYNC_REPLY size")
    magic, version, packet_type, sequence, t1, t2, t3 = SYNC_REPLY_STRUCT.unpack(packet)
    if magic != MAGIC or version != VERSION or packet_type != SYNC_REPLY:
        raise ValueError("invalid SYNC_REPLY header")
    return sequence, t1, t2, t3


def build_sync_result(sequence: int, t1: int, t2: int, t3: int, t4: int) -> bytes:
    return SYNC_RESULT_STRUCT.pack(MAGIC, VERSION, SYNC_RESULT, sequence & 0xFFFFFFFF, t1, t2, t3, t4)


def build_marker(event_id: int, code: int, pc_monotonic_us: int) -> bytes:
    if not 0 <= code <= 255:
        raise ValueError("marker code must fit in uint8")
    return MARKER_STRUCT.pack(MAGIC, VERSION, MARKER, event_id & 0xFFFFFFFF, code, pc_monotonic_us)


def parse_marker_ack(packet: bytes) -> MarkerAck:
    if len(packet) != MARKER_ACK_STRUCT.size:
        raise ValueError("invalid MARKER_ACK size")
    magic, version, packet_type, event_id, status, sample_seq, error_us = MARKER_ACK_STRUCT.unpack(packet)
    if magic != MAGIC or version != VERSION or packet_type != MARKER_ACK:
        raise ValueError("invalid MARKER_ACK header")
    return MarkerAck(event_id, MarkerStatus(status), sample_seq, error_us)


class ClockSyncState:
    """PC-side mirror of the firmware RTT acceptance and adaptive-rate policy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rtts: deque[int] = deque(maxlen=64)
        self._last_valid_s: float | None = None

    def _expired(self, now_s: float) -> bool:
        return self._last_valid_s is not None and now_s - self._last_valid_s > 10.0

    def expire_if_needed(self, now_s: float | None = None) -> None:
        now_s = time.monotonic() if now_s is None else now_s
        with self._lock:
            if self._expired(now_s):
                self._rtts.clear()
                self._last_valid_s = None

    def observe(self, t1: int, t2: int, t3: int, t4: int, now_s: float | None = None) -> bool:
        now_s = time.monotonic() if now_s is None else now_s
        if t4 < t1 or t3 < t2:
            return False
        round_trip = t4 - t1
        server_time = t3 - t2
        if round_trip < server_time:
            return False
        rtt = round_trip - server_time
        with self._lock:
            if self._expired(now_s):
                self._rtts.clear()
                self._last_valid_s = None
            minimum = min(self._rtts) if self._rtts else None
            if rtt > 10_000 or (minimum is not None and rtt > minimum + 2_000):
                return False
            self._rtts.append(rtt)
            self._last_valid_s = now_s
            return True

    def synchronized(self, now_s: float | None = None) -> bool:
        now_s = time.monotonic() if now_s is None else now_s
        with self._lock:
            return bool(self._rtts) and not self._expired(now_s)

    def valid_measurements(self, now_s: float | None = None) -> int:
        now_s = time.monotonic() if now_s is None else now_s
        with self._lock:
            return 0 if self._expired(now_s) else len(self._rtts)

    def rate_hz(self, now_s: float | None = None) -> float:
        return 2.0 if self.valid_measurements(now_s) >= 16 else 10.0


class P300MarkerClient:
    """Maintain synchronization and send marker events to an ESP32-C5 device."""

    def __init__(
        self,
        host: str,
        port: int = 1235,
        *,
        sync_timeout_s: float = 0.05,
        ack_timeout_s: float = 0.10,
        ack_retries: int = 3,
    ) -> None:
        self.target = (host, port)
        self.sync_timeout_s = sync_timeout_s
        self.ack_timeout_s = ack_timeout_s
        self.ack_retries = ack_retries
        self.sync_state = ClockSyncState()
        self._stop = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self._sync_socket: socket.socket | None = None
        self._sequence = 0
        self._event_id = int(time.time_ns()) & 0xFFFFFFFF
        self._id_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="p300-marker")
        self.last_sync_error: str | None = None

    def __enter__(self) -> P300MarkerClient:
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()

    def start(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            return
        self._stop.clear()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="p300-clock-sync")
        self._sync_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sync_socket:
            self._sync_socket.close()
        if self._sync_thread:
            self._sync_thread.join(timeout=1.0)
        self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def synchronized(self) -> bool:
        return self.sync_state.synchronized()

    def wait_until_synchronized(self, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.synchronized:
                return True
            self._stop.wait(0.01)
        return self.synchronized

    def _next_event_id(self) -> int:
        with self._id_lock:
            self._event_id = (self._event_id + 1) & 0xFFFFFFFF
            return self._event_id

    def _sync_loop(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sync_socket:
                self._sync_socket = sync_socket
                sync_socket.connect(self.target)
                sync_socket.settimeout(self.sync_timeout_s)
                while not self._stop.is_set():
                    cycle_start = time.monotonic()
                    self.sync_state.expire_if_needed(cycle_start)
                    self._sequence = (self._sequence + 1) & 0xFFFFFFFF
                    t1 = monotonic_us()
                    try:
                        sync_socket.send(build_sync_request(self._sequence, t1))
                        reply = sync_socket.recv(64)
                        t4 = monotonic_us()
                        sequence, echoed_t1, t2, t3 = parse_sync_reply(reply)
                        if sequence != self._sequence or echoed_t1 != t1:
                            raise ValueError("stale SYNC_REPLY")
                        sync_socket.send(build_sync_result(sequence, t1, t2, t3, t4))
                        self.sync_state.observe(t1, t2, t3, t4, time.monotonic())
                        self.last_sync_error = None
                    except (OSError, ValueError) as error:
                        if not self._stop.is_set():
                            self.last_sync_error = str(error)
                    period = 1.0 / self.sync_state.rate_hz()
                    self._stop.wait(max(0.0, period - (time.monotonic() - cycle_start)))
        finally:
            self._sync_socket = None

    def send_marker(
        self,
        code: int = 1,
        *,
        event_id: int | None = None,
        pc_monotonic_us: int | None = None,
    ) -> MarkerAck:
        """Capture event time immediately, then block until its ACK or timeout."""
        event_id = self._next_event_id() if event_id is None else event_id & 0xFFFFFFFF
        event_time = monotonic_us() if pc_monotonic_us is None else pc_monotonic_us
        return self._exchange_marker(event_id, code, event_time)

    def send_marker_async(self, code: int = 1) -> Future[MarkerAck]:
        """Capture event time now and perform UDP/ACK work outside the display thread."""
        event_id = self._next_event_id()
        event_time = monotonic_us()
        return self._executor.submit(self._exchange_marker, event_id, code, event_time)

    def _exchange_marker(self, event_id: int, code: int, event_time: int) -> MarkerAck:
        packet = build_marker(event_id, code, event_time)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as marker_socket:
            marker_socket.connect(self.target)
            marker_socket.settimeout(self.ack_timeout_s)
            for _ in range(self.ack_retries + 1):
                marker_socket.send(packet)
                deadline = time.monotonic() + self.ack_timeout_s
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    marker_socket.settimeout(remaining)
                    try:
                        ack = parse_marker_ack(marker_socket.recv(64))
                    except socket.timeout:
                        break
                    except ValueError:
                        continue
                    if ack.event_id == event_id:
                        return ack
        raise TimeoutError(f"marker event {event_id} ACK timed out")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="ESP32-C5 IP address")
    parser.add_argument("--port", type=int, default=1235)
    parser.add_argument("--code", type=int, default=1)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--sync-timeout", type=float, default=5.0)
    args = parser.parse_args()

    with P300MarkerClient(args.host, args.port) as client:
        if not client.wait_until_synchronized(args.sync_timeout):
            raise SystemExit(f"clock synchronization failed: {client.last_sync_error or 'no valid reply'}")
        print(f"Synchronized ({client.sync_state.valid_measurements()} measurements)")
        for index in range(args.count):
            ack = client.send_marker(args.code)
            print(
                f"event={ack.event_id} status={ack.status.name} "
                f"sample_seq={ack.sample_seq} error_us={ack.error_us}"
            )
            if index + 1 < args.count:
                time.sleep(args.interval)


if __name__ == "__main__":
    main()
