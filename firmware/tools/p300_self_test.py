"""ADS1299 internal-input-short control protocol and diagnostic metrics."""

from __future__ import annotations

import queue
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass
from enum import Enum, IntEnum, IntFlag
from typing import Callable

import numpy as np

try:
    from .p300_common import CHANNELS, EegBatch, codes_to_uv, crc32c
except ImportError:  # Direct import from the tools directory.
    from p300_common import CHANNELS, EegBatch, codes_to_uv, crc32c

MAGIC = b"\xA5\x51"
VERSION = 1
SELF_TEST_REQUEST = 8
SELF_TEST_RESULT = 9
SELF_TEST_REQUEST_BYTES = 16
SELF_TEST_RESULT_BYTES = 20
CONFIG_NORMAL = 1
CONFIG_INTERNAL_SHORT = 2

_REQUEST_PREFIX = struct.Struct(">2sBBIB3x")
_RESULT_PREFIX = struct.Struct(">2sBBIBBBBI")
_CRC = struct.Struct(">I")


class SelfTestStatus(IntEnum):
    OK = 0
    BUSY = 1
    INVALID_REQUEST = 2
    NO_TCP_STREAM = 3
    PAUSE_TIMEOUT = 4
    CONFIG_FAILED = 5
    RESTORE_FAILED = 6
    INTERNAL_ERROR = 7


class SelfTestFlags(IntFlag):
    CHANGED = 1 << 0
    CACHED = 1 << 1


class SelfTestGrade(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    WAITING = "WAITING"


class SelfTestProtocolError(ValueError):
    """Raised when a mode-control UDP packet is malformed or unrelated."""


class SelfTestTimeoutError(TimeoutError):
    """Raised after all same-request-ID retries time out."""


class SelfTestCancelledError(RuntimeError):
    """Raised when an asynchronous mode request is cancelled."""


@dataclass(frozen=True)
class SelfTestResultData:
    request_id: int
    status: SelfTestStatus
    requested_config_id: int
    actual_config_id: int
    flags: SelfTestFlags
    stream_id: int


@dataclass(frozen=True)
class ChannelSelfTestMetrics:
    rms_uv: float
    peak_to_peak_uv: float
    mains_50hz_uv: float
    grade: SelfTestGrade


@dataclass(frozen=True)
class SelfTestSnapshot:
    ready: bool
    sample_count: int
    channels: tuple[ChannelSelfTestMetrics, ...]
    overall: SelfTestGrade


def _validate_config_id(config_id: int) -> int:
    value = int(config_id)
    if value not in (CONFIG_NORMAL, CONFIG_INTERNAL_SHORT):
        raise ValueError("config_id must be 1 (NORMAL) or 2 (INTERNAL_SHORT)")
    return value


def build_self_test_request(request_id: int, target_config_id: int) -> bytes:
    if not 1 <= int(request_id) <= 0xFFFFFFFF:
        raise ValueError("request_id must be a non-zero uint32")
    target = _validate_config_id(target_config_id)
    prefix = _REQUEST_PREFIX.pack(
        MAGIC, VERSION, SELF_TEST_REQUEST, int(request_id), target
    )
    return prefix + _CRC.pack(crc32c(prefix))


def parse_self_test_request(packet: bytes) -> tuple[int, int]:
    if len(packet) != SELF_TEST_REQUEST_BYTES:
        raise SelfTestProtocolError(f"invalid self-test request size: {len(packet)}")
    if crc32c(packet[:12]) != _CRC.unpack_from(packet, 12)[0]:
        raise SelfTestProtocolError("self-test request CRC32C mismatch")
    magic, version, packet_type, request_id, target = _REQUEST_PREFIX.unpack_from(packet, 0)
    if magic != MAGIC or version != VERSION or packet_type != SELF_TEST_REQUEST:
        raise SelfTestProtocolError("invalid self-test request magic, version, or type")
    if request_id == 0:
        raise SelfTestProtocolError("self-test request_id must be non-zero")
    return request_id, _validate_config_id(target)


def build_self_test_result(result: SelfTestResultData) -> bytes:
    requested = _validate_config_id(result.requested_config_id)
    actual = _validate_config_id(result.actual_config_id)
    prefix = _RESULT_PREFIX.pack(
        MAGIC,
        VERSION,
        SELF_TEST_RESULT,
        int(result.request_id),
        int(result.status),
        requested,
        actual,
        int(result.flags),
        int(result.stream_id),
    )
    return prefix + _CRC.pack(crc32c(prefix))


def parse_self_test_result(
    packet: bytes, expected_request_id: int | None = None
) -> SelfTestResultData:
    if len(packet) != SELF_TEST_RESULT_BYTES:
        raise SelfTestProtocolError(f"invalid self-test result size: {len(packet)}")
    if crc32c(packet[:16]) != _CRC.unpack_from(packet, 16)[0]:
        raise SelfTestProtocolError("self-test result CRC32C mismatch")
    (
        magic,
        version,
        packet_type,
        request_id,
        status_value,
        requested_config_id,
        actual_config_id,
        flags,
        stream_id,
    ) = _RESULT_PREFIX.unpack_from(packet, 0)
    if magic != MAGIC or version != VERSION or packet_type != SELF_TEST_RESULT:
        raise SelfTestProtocolError("invalid self-test result magic, version, or type")
    if expected_request_id is not None and request_id != int(expected_request_id):
        raise SelfTestProtocolError("self-test result request_id mismatch")
    try:
        status = SelfTestStatus(status_value)
    except ValueError as error:
        raise SelfTestProtocolError(f"unknown self-test status: {status_value}") from error
    if requested_config_id not in (CONFIG_NORMAL, CONFIG_INTERNAL_SHORT) or actual_config_id not in (
        CONFIG_NORMAL,
        CONFIG_INTERNAL_SHORT,
    ):
        raise SelfTestProtocolError("invalid self-test config_id")
    return SelfTestResultData(
        request_id=request_id,
        status=status,
        requested_config_id=requested_config_id,
        actual_config_id=actual_config_id,
        flags=SelfTestFlags(flags),
        stream_id=stream_id,
    )


class InternalShortAnalyzer:
    """Keep a contiguous internal-short window and calculate per-channel metrics."""

    def __init__(self, sample_rate_hz: int = 1000, window_samples: int = 1000) -> None:
        if int(sample_rate_hz) <= 0 or int(window_samples) <= 1:
            raise ValueError("sample_rate_hz and window_samples must be positive")
        self.sample_rate_hz = int(sample_rate_hz)
        self.window_samples = int(window_samples)
        self.reset()

    @property
    def sample_count(self) -> int:
        return int(self._samples.shape[0])

    def reset(self) -> None:
        self._samples = np.empty((0, CHANNELS), dtype=np.float64)
        self._stream_id: int | None = None
        self._next_seq: int | None = None

    def ingest_uv(
        self,
        stream_id: int,
        config_id: int,
        start_seq: int,
        samples_uv: np.ndarray,
    ) -> None:
        values = np.asarray(samples_uv, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != CHANNELS or values.shape[0] == 0:
            raise ValueError(f"samples_uv must have shape (N, {CHANNELS})")
        if int(config_id) != CONFIG_INTERNAL_SHORT:
            self.reset()
            return

        stream = int(stream_id) & 0xFFFFFFFF
        sequence = int(start_seq) & 0xFFFFFFFF
        contiguous = self._stream_id == stream and self._next_seq == sequence
        if self._stream_id is not None and not contiguous:
            self.reset()
        self._stream_id = stream
        self._next_seq = (sequence + values.shape[0]) & 0xFFFFFFFF
        self._samples = np.vstack((self._samples, values))[-self.window_samples :]

    def ingest_batch(self, batch: EegBatch, vref_v: float = 4.5, pga_gain: float = 12.0) -> None:
        self.ingest_uv(
            batch.stream_id,
            batch.config_id,
            batch.start_seq,
            codes_to_uv(batch.samples, vref_v=vref_v, pga_gain=pga_gain),
        )

    @staticmethod
    def _grade(rms_uv: float, peak_to_peak_uv: float, mains_50hz_uv: float) -> SelfTestGrade:
        if rms_uv <= 1.0 and peak_to_peak_uv <= 10.0 and mains_50hz_uv <= 1.0:
            return SelfTestGrade.PASS
        if rms_uv <= 5.0 and peak_to_peak_uv <= 50.0 and mains_50hz_uv <= 5.0:
            return SelfTestGrade.WARN
        return SelfTestGrade.FAIL

    def snapshot(self) -> SelfTestSnapshot:
        if self.sample_count < self.window_samples:
            return SelfTestSnapshot(False, self.sample_count, (), SelfTestGrade.WAITING)
        centered = self._samples - np.mean(self._samples, axis=0, keepdims=True)
        rms = np.sqrt(np.mean(centered * centered, axis=0))
        peak_to_peak = np.ptp(centered, axis=0)
        window = np.hanning(self.window_samples)
        spectrum = np.fft.rfft(centered * window[:, None], axis=0)
        frequencies = np.fft.rfftfreq(self.window_samples, d=1.0 / self.sample_rate_hz)
        mains_index = int(np.argmin(np.abs(frequencies - 50.0)))
        mains = 2.0 * np.abs(spectrum[mains_index]) / np.sum(window)
        channels = tuple(
            ChannelSelfTestMetrics(
                float(rms[index]),
                float(peak_to_peak[index]),
                float(mains[index]),
                self._grade(float(rms[index]), float(peak_to_peak[index]), float(mains[index])),
            )
            for index in range(CHANNELS)
        )
        rank = {SelfTestGrade.PASS: 0, SelfTestGrade.WARN: 1, SelfTestGrade.FAIL: 2}
        overall = max((channel.grade for channel in channels), key=rank.__getitem__)
        return SelfTestSnapshot(True, self.sample_count, channels, overall)


class SelfTestClient:
    """UDP mode-control client; defaults to three 500-ms same-ID attempts."""

    def __init__(
        self,
        device_host: str,
        port: int = 1235,
        *,
        timeout_s: float = 0.5,
        retries: int = 2,
        events: queue.Queue | None = None,
        callback: Callable[[tuple[str, dict]], None] | None = None,
    ) -> None:
        if not device_host:
            raise ValueError("device_host is required")
        if not 1 <= int(port) <= 65535 or timeout_s <= 0.0 or retries < 0:
            raise ValueError("invalid port, timeout, or retry count")
        self.device_host = device_host
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self.events = events
        self.callback = callback
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._stop = threading.Event()

    @staticmethod
    def new_request_id() -> int:
        return secrets.randbits(32) or 1

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def switch(self, target_config_id: int, request_id: int | None = None) -> SelfTestResultData:
        request_id = self.new_request_id() if request_id is None else int(request_id)
        packet = build_self_test_request(request_id, target_config_id)
        destination_ip = socket.gethostbyname(self.device_host)
        destination = (destination_ip, self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        with self._lock:
            self._socket = sock
        try:
            for _attempt in range(self.retries + 1):
                if self._stop.is_set():
                    raise SelfTestCancelledError("self-test request cancelled")
                sock.sendto(packet, destination)
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline:
                    if self._stop.is_set():
                        raise SelfTestCancelledError("self-test request cancelled")
                    sock.settimeout(min(0.1, max(0.001, deadline - time.monotonic())))
                    try:
                        response, source = sock.recvfrom(64)
                    except TimeoutError:
                        continue
                    except OSError as error:
                        if self._stop.is_set():
                            raise SelfTestCancelledError("self-test request cancelled") from error
                        raise
                    if source != destination:
                        continue
                    try:
                        return parse_self_test_result(response, expected_request_id=request_id)
                    except SelfTestProtocolError:
                        continue
            raise SelfTestTimeoutError(
                f"self-test request {request_id} timed out after {self.retries + 1} attempts"
            )
        finally:
            with self._lock:
                if self._socket is sock:
                    self._socket = None
            sock.close()

    def _emit(self, state: str, request_id: int, **values: object) -> None:
        event = ("self_test", {"state": state, "request_id": request_id, **values})
        if self.events is not None:
            self.events.put(event)
        if self.callback is not None:
            self.callback(event)

    def start(self, target_config_id: int, request_id: int | None = None) -> int:
        request_id = self.new_request_id() if request_id is None else int(request_id)
        build_self_test_request(request_id, target_config_id)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a self-test request is already active")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(target_config_id, request_id),
                name="p300-self-test",
                daemon=True,
            )
            self._thread.start()
        return request_id

    def _run(self, target_config_id: int, request_id: int) -> None:
        self._emit("started", request_id, target_config_id=target_config_id)
        try:
            result = self.switch(target_config_id, request_id)
        except SelfTestCancelledError as error:
            self._emit("cancelled", request_id, message=str(error))
        except Exception as error:  # Forward worker failures to the GUI event queue.
            self._emit("error", request_id, message=str(error), error=error)
        else:
            self._emit("result", request_id, result=result)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
