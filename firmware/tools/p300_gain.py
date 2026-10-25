"""ADS1299 runtime GAIN-mask UDP protocol and non-blocking client."""

from __future__ import annotations

import queue
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Callable

try:
    from .p300_common import CHANNELS, crc32c
except ImportError:  # Direct execution/import from the tools directory.
    from p300_common import CHANNELS, crc32c

MAGIC = b"\xA5\x51"
VERSION = 1
GAIN_CONTROL_REQUEST = 16
GAIN_CONTROL_RESULT = 17
GAIN_CONTROL_REQUEST_BYTES = 16
GAIN_CONTROL_RESULT_BYTES = 20

_REQUEST_PREFIX = struct.Struct(">2sBBIBB2x")
_RESULT_PREFIX = struct.Struct(">2sBBIBBBBI")
_CRC = struct.Struct(">I")


class GainOperation(IntEnum):
    QUERY = 0
    SET = 1


class GainStatus(IntEnum):
    OK = 0
    BUSY = 1
    INVALID_REQUEST = 2
    NO_TCP_STREAM = 3
    APPLY_FAILED = 4
    RESTORE_FAILED = 5
    INTERNAL_ERROR = 6


class GainFlags(IntFlag):
    CHANGED = 1 << 0
    CACHED = 1 << 1
    VERIFIED = 1 << 2


class GainProtocolError(ValueError):
    """Raised when a GAIN packet is malformed or unrelated."""


class GainTimeoutError(TimeoutError):
    """Raised when all same-request-ID attempts time out."""


class GainCancelledError(RuntimeError):
    """Raised when an asynchronous GAIN operation is cancelled."""


@dataclass(frozen=True)
class GainResultData:
    request_id: int
    status: GainStatus
    requested_gain: int
    actual_gain: int
    flags: GainFlags
    stream_id: int

    @property
    def verified(self) -> bool:
        return bool(self.flags & GainFlags.VERIFIED)



@dataclass(frozen=True)
class GainSetOutcome:
    result: GainResultData
    target_gain: int
    original_request_id: int
    reconciled: bool = False

    @property
    def applied(self) -> bool:
        return (
            self.result.status == GainStatus.OK
            and self.result.verified
            and self.result.actual_gain == self.target_gain
        )


def _request_id(value: int) -> int:
    result = int(value)
    if not 1 <= result <= 0xFFFFFFFF:
        raise ValueError("request_id must be a non-zero uint32")
    return result


GAINS = (1, 2, 4, 6, 8, 12, 24)


def _gain(value: int, *, allow_zero: bool) -> int:
    result = int(value)
    if result != value or result not in ((0, *GAINS) if allow_zero else GAINS):
        raise ValueError("gain must be one of 1, 2, 4, 6, 8, 12, 24")
    return result


def build_gain_control_request(
    request_id: int,
    operation: GainOperation | int,
    requested_gain: int = 0,
) -> bytes:
    request = _request_id(request_id)
    try:
        op = GainOperation(operation)
    except ValueError as error:
        raise ValueError("operation must be QUERY or SET") from error
    mask = _gain(requested_gain, allow_zero=op != GainOperation.SET)
    if op != GainOperation.SET and mask != 0:
        raise ValueError("QUERY requires gain 0; SET requires a supported gain")
    prefix = _REQUEST_PREFIX.pack(MAGIC, VERSION, GAIN_CONTROL_REQUEST, request, int(op), mask)
    return prefix + _CRC.pack(crc32c(prefix))


def parse_gain_control_request(packet: bytes) -> tuple[int, GainOperation, int]:
    if len(packet) != GAIN_CONTROL_REQUEST_BYTES:
        raise GainProtocolError(f"invalid GAIN request size: {len(packet)}")
    if crc32c(packet[:12]) != _CRC.unpack_from(packet, 12)[0]:
        raise GainProtocolError("GAIN request CRC32C mismatch")
    magic, version, packet_type, request_id, operation_value, requested_gain = (
        _REQUEST_PREFIX.unpack_from(packet, 0)
    )
    if magic != MAGIC or version != VERSION or packet_type != GAIN_CONTROL_REQUEST:
        raise GainProtocolError("invalid GAIN request magic, version, or type")
    if packet[10:12] != b"\x00\x00":
        raise GainProtocolError("nonzero GAIN request reserved bytes")
    try:
        operation = GainOperation(operation_value)
        build_gain_control_request(request_id, operation, requested_gain)
    except ValueError as error:
        raise GainProtocolError(str(error)) from error
    return request_id, operation, requested_gain


def build_gain_control_result(result: GainResultData) -> bytes:
    request_id = _request_id(result.request_id)
    try:
        status = GainStatus(result.status)
    except ValueError as error:
        raise ValueError("unknown GAIN status") from error
    requested = int(result.requested_gain)
    if not 0 <= requested <= 255:
        raise ValueError("requested gain must fit uint8")
    actual = _gain(result.actual_gain, allow_zero=True)
    flags = int(GainFlags(result.flags)) & 0xFF
    stream_id = int(result.stream_id)
    if not 0 <= stream_id <= 0xFFFFFFFF:
        raise ValueError("stream_id must fit uint32")
    prefix = _RESULT_PREFIX.pack(
        MAGIC, VERSION, GAIN_CONTROL_RESULT, request_id, int(status),
        requested, actual, flags, stream_id,
    )
    return prefix + _CRC.pack(crc32c(prefix))


def parse_gain_control_result(
    packet: bytes,
    expected_request_id: int | None = None,
) -> GainResultData:
    if len(packet) != GAIN_CONTROL_RESULT_BYTES:
        raise GainProtocolError(f"invalid GAIN result size: {len(packet)}")
    if crc32c(packet[:16]) != _CRC.unpack_from(packet, 16)[0]:
        raise GainProtocolError("GAIN result CRC32C mismatch")
    values = _RESULT_PREFIX.unpack_from(packet, 0)
    magic, version, packet_type, request_id = values[:4]
    if magic != MAGIC or version != VERSION or packet_type != GAIN_CONTROL_RESULT:
        raise GainProtocolError("invalid GAIN result magic, version, or type")
    if expected_request_id is not None and request_id != int(expected_request_id):
        raise GainProtocolError("GAIN result request_id mismatch")
    try:
        status = GainStatus(values[4])
    except ValueError as error:
        raise GainProtocolError(f"unknown GAIN status: {values[4]}") from error
    result = GainResultData(
        request_id=request_id,
        status=status,
        requested_gain=values[5],
        actual_gain=values[6],
        flags=GainFlags(values[7]),
        stream_id=values[8],
    )
    if status == GainStatus.RESTORE_FAILED and (
        result.actual_gain != 0 or result.stream_id != 0 or result.verified
    ):
        raise GainProtocolError("RESTORE_FAILED result claims a verified register state")
    if int(result.flags) & ~7:
        raise GainProtocolError("unknown gain result flags")
    if result.verified and (result.actual_gain not in GAINS or result.stream_id == 0):
        raise GainProtocolError("verified gain result has invalid gain/stream")
    return result



class GainControlClient:
    """GAIN UDP client with same-ID retries and SET timeout reconciliation."""

    def __init__(
        self,
        device_host: str,
        port: int = 1235,
        *,
        timeout_s: float = 0.3,
        retries: int = 2,
        events: queue.Queue | None = None,
        callback: Callable[[tuple[str, dict]], None] | None = None,
        exchange=None,
    ) -> None:
        if not device_host or not 1 <= int(port) <= 65535 or timeout_s <= 0 or retries < 0:
            raise ValueError("invalid GAIN client host, port, timeout, or retries")
        self.device_host = device_host
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self.events = events
        self.callback = callback
        self.transport_exchange = exchange
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

    def _exchange(self, packet: bytes, request_id: int) -> GainResultData:
        if self.transport_exchange is not None:
            try:
                return parse_gain_control_result(self.transport_exchange(packet, self.timeout_s, self.retries, self._stop), request_id)
            except TimeoutError as error:
                raise GainTimeoutError(str(error)) from error
        destination = (socket.gethostbyname(self.device_host), self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        with self._lock:
            self._socket = sock
        try:
            for _attempt in range(self.retries + 1):
                if self._stop.is_set():
                    raise GainCancelledError("GAIN request cancelled")
                sock.sendto(packet, destination)
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline:
                    if self._stop.is_set():
                        raise GainCancelledError("GAIN request cancelled")
                    sock.settimeout(min(0.1, max(0.001, deadline - time.monotonic())))
                    try:
                        response, source = sock.recvfrom(64)
                    except TimeoutError:
                        continue
                    except OSError as error:
                        if self._stop.is_set():
                            raise GainCancelledError("GAIN request cancelled") from error
                        raise
                    if source != destination:
                        continue
                    try:
                        return parse_gain_control_result(response, request_id)
                    except GainProtocolError:
                        continue
            raise GainTimeoutError(
                f"GAIN request {request_id} timed out after {self.retries + 1} attempts"
            )
        finally:
            with self._lock:
                if self._socket is sock:
                    self._socket = None
            sock.close()

    def query(self, request_id: int | None = None) -> GainResultData:
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        return self._exchange(
            build_gain_control_request(request_id, GainOperation.QUERY, 0), request_id
        )

    def set_gain(self, target_gain: int, request_id: int | None = None) -> GainSetOutcome:
        target = _gain(target_gain, allow_zero=False)
        return self._set_target(target, GainOperation.SET, request_id)

    def _set_target(self, target: int, operation: GainOperation,
                    request_id: int | None) -> GainSetOutcome:
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        try:
            result = self._exchange(
                build_gain_control_request(request_id, operation, target), request_id
            )
            return GainSetOutcome(result, target, request_id, False)
        except GainTimeoutError:
            query_result = self.query()
            return GainSetOutcome(query_result, target, request_id, True)

    def _emit(self, state: str, request_id: int, **values: object) -> None:
        event = ("gain", {"state": state, "request_id": request_id, **values})
        if self.events is not None:
            self.events.put(event)
        if self.callback is not None:
            self.callback(event)

    def start_query(self, request_id: int | None = None) -> int:
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        return self._start_worker("query", request_id, 0)

    def start_set(self, target_gain: int, request_id: int | None = None) -> int:
        target = _gain(target_gain, allow_zero=False)
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        return self._start_worker("set", request_id, target)

    def _start_worker(self, operation: str, request_id: int, target_gain: int) -> int:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a GAIN request is already active")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(operation, request_id, target_gain),
                name="p300-gain-control",
                daemon=True,
            )
            self._thread.start()
        return request_id

    def _run(self, operation: str, request_id: int, target_gain: int) -> None:
        self._emit("started", request_id, operation=operation, target_gain=target_gain)
        try:
            if operation == "query":
                result: object = self.query(request_id)
            else:
                result = self.set_gain(target_gain, request_id)
        except GainCancelledError as error:
            self._emit(
                "cancelled", request_id, operation=operation,
                target_gain=target_gain, message=str(error),
            )
        except Exception as error:
            self._emit(
                "error", request_id, operation=operation,
                target_gain=target_gain, message=str(error), error=error,
            )
        else:
            self._emit(
                "result", request_id, operation=operation,
                target_gain=target_gain, result=result,
            )

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
