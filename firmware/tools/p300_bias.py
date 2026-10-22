"""ADS1299 runtime BIAS-mask UDP protocol and non-blocking client."""

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
BIAS_CONTROL_REQUEST = 10
BIAS_CONTROL_RESULT = 11
BIAS_CONTROL_REQUEST_BYTES = 16
BIAS_CONTROL_RESULT_BYTES = 20

_REQUEST_PREFIX = struct.Struct(">2sBBIBB2x")
_RESULT_PREFIX = struct.Struct(">2sBBIBBBBI")
_CRC = struct.Struct(">I")


class BiasOperation(IntEnum):
    QUERY = 0
    SET = 1
    DISABLE = 2


class BiasStatus(IntEnum):
    OK = 0
    BUSY = 1
    INVALID_REQUEST = 2
    NO_TCP_STREAM = 3
    APPLY_FAILED = 4
    RESTORE_FAILED = 5
    INTERNAL_ERROR = 6


class BiasFlags(IntFlag):
    CHANGED = 1 << 0
    CACHED = 1 << 1
    VERIFIED = 1 << 2
    POWER_CONTROL = 1 << 3


class BiasProtocolError(ValueError):
    """Raised when a BIAS packet is malformed or unrelated."""


class BiasTimeoutError(TimeoutError):
    """Raised when all same-request-ID attempts time out."""


class BiasCancelledError(RuntimeError):
    """Raised when an asynchronous BIAS operation is cancelled."""


@dataclass(frozen=True)
class BiasResultData:
    request_id: int
    status: BiasStatus
    requested_mask: int
    actual_mask: int
    flags: BiasFlags
    stream_id: int

    @property
    def verified(self) -> bool:
        return bool(self.flags & BiasFlags.VERIFIED)

    @property
    def power_control_supported(self) -> bool:
        return bool(self.flags & BiasFlags.POWER_CONTROL)

    @property
    def enabled(self) -> bool | None:
        return self.actual_mask != 0 if self.verified else None


@dataclass(frozen=True)
class BiasSetOutcome:
    result: BiasResultData
    target_mask: int
    original_request_id: int
    reconciled: bool = False

    @property
    def applied(self) -> bool:
        return (
            self.result.status == BiasStatus.OK
            and self.result.verified
            and self.result.actual_mask == self.target_mask
            and (self.target_mask != 0 or self.result.power_control_supported)
        )


def _request_id(value: int) -> int:
    result = int(value)
    if not 1 <= result <= 0xFFFFFFFF:
        raise ValueError("request_id must be a non-zero uint32")
    return result


def _mask(value: int, *, allow_zero: bool) -> int:
    result = int(value)
    minimum = 0 if allow_zero else 1
    if not minimum <= result <= 0xFF:
        raise ValueError("BIAS mask must fit uint8 and SET mask must be non-zero")
    return result


def build_bias_control_request(
    request_id: int,
    operation: BiasOperation | int,
    requested_mask: int = 0,
) -> bytes:
    request = _request_id(request_id)
    try:
        op = BiasOperation(operation)
    except ValueError as error:
        raise ValueError("operation must be QUERY, SET or DISABLE") from error
    mask = _mask(requested_mask, allow_zero=op != BiasOperation.SET)
    if op != BiasOperation.SET and mask != 0:
        raise ValueError("QUERY and DISABLE require mask 0; SET requires a non-zero mask")
    prefix = _REQUEST_PREFIX.pack(MAGIC, VERSION, BIAS_CONTROL_REQUEST, request, int(op), mask)
    return prefix + _CRC.pack(crc32c(prefix))


def parse_bias_control_request(packet: bytes) -> tuple[int, BiasOperation, int]:
    if len(packet) != BIAS_CONTROL_REQUEST_BYTES:
        raise BiasProtocolError(f"invalid BIAS request size: {len(packet)}")
    if crc32c(packet[:12]) != _CRC.unpack_from(packet, 12)[0]:
        raise BiasProtocolError("BIAS request CRC32C mismatch")
    magic, version, packet_type, request_id, operation_value, requested_mask = (
        _REQUEST_PREFIX.unpack_from(packet, 0)
    )
    if magic != MAGIC or version != VERSION or packet_type != BIAS_CONTROL_REQUEST:
        raise BiasProtocolError("invalid BIAS request magic, version, or type")
    if packet[10:12] != b"\x00\x00":
        raise BiasProtocolError("nonzero BIAS request reserved bytes")
    try:
        operation = BiasOperation(operation_value)
        build_bias_control_request(request_id, operation, requested_mask)
    except ValueError as error:
        raise BiasProtocolError(str(error)) from error
    return request_id, operation, requested_mask


def build_bias_control_result(result: BiasResultData) -> bytes:
    request_id = _request_id(result.request_id)
    try:
        status = BiasStatus(result.status)
    except ValueError as error:
        raise ValueError("unknown BIAS status") from error
    requested = _mask(result.requested_mask, allow_zero=True)
    actual = _mask(result.actual_mask, allow_zero=True)
    flags = int(BiasFlags(result.flags)) & 0xFF
    stream_id = int(result.stream_id)
    if not 0 <= stream_id <= 0xFFFFFFFF:
        raise ValueError("stream_id must fit uint32")
    prefix = _RESULT_PREFIX.pack(
        MAGIC, VERSION, BIAS_CONTROL_RESULT, request_id, int(status),
        requested, actual, flags, stream_id,
    )
    return prefix + _CRC.pack(crc32c(prefix))


def parse_bias_control_result(
    packet: bytes,
    expected_request_id: int | None = None,
) -> BiasResultData:
    if len(packet) != BIAS_CONTROL_RESULT_BYTES:
        raise BiasProtocolError(f"invalid BIAS result size: {len(packet)}")
    if crc32c(packet[:16]) != _CRC.unpack_from(packet, 16)[0]:
        raise BiasProtocolError("BIAS result CRC32C mismatch")
    values = _RESULT_PREFIX.unpack_from(packet, 0)
    magic, version, packet_type, request_id = values[:4]
    if magic != MAGIC or version != VERSION or packet_type != BIAS_CONTROL_RESULT:
        raise BiasProtocolError("invalid BIAS result magic, version, or type")
    if expected_request_id is not None and request_id != int(expected_request_id):
        raise BiasProtocolError("BIAS result request_id mismatch")
    try:
        status = BiasStatus(values[4])
    except ValueError as error:
        raise BiasProtocolError(f"unknown BIAS status: {values[4]}") from error
    result = BiasResultData(
        request_id=request_id,
        status=status,
        requested_mask=values[5],
        actual_mask=values[6],
        flags=BiasFlags(values[7]),
        stream_id=values[8],
    )
    if status == BiasStatus.RESTORE_FAILED and (
        result.actual_mask != 0 or result.stream_id != 0 or result.verified
    ):
        raise BiasProtocolError("RESTORE_FAILED result claims a verified register state")
    if result.verified and result.actual_mask == 0 and not result.power_control_supported:
        raise BiasProtocolError("verified BIAS off requires power-control capability")
    return result


def recommend_bias_mask(impedance_result: object, calibration: object, max_ohm: float = 50_000.0) -> int:
    """Return tested, non-open channels whose compensated electrode value is valid."""
    if int(getattr(impedance_result, "status")) != 0:
        return 0
    flags = int(getattr(impedance_result, "flags"))
    if flags & 0x03 != 0x03 or flags & 0x08:
        return 0
    tested_mask = int(getattr(impedance_result, "tested_mask"))
    open_mask = int(getattr(impedance_result, "open_mask"))
    nominal = tuple(getattr(impedance_result, "nominal_ohm"))
    recommended = 0
    for channel in range(CHANNELS):
        bit = 1 << channel
        if not tested_mask & bit or open_mask & bit or nominal[channel] is None:
            continue
        estimate = calibration.estimate(channel, nominal[channel])
        value = getattr(estimate, "display_ohm", None)
        if value is not None and 0.0 <= float(value) <= float(max_ohm):
            recommended |= bit
    return recommended


class BiasControlClient:
    """BIAS UDP client with same-ID retries and SET timeout reconciliation."""

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
            raise ValueError("invalid BIAS client host, port, timeout, or retries")
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

    def _exchange(self, packet: bytes, request_id: int) -> BiasResultData:
        if self.transport_exchange is not None:
            try:
                return parse_bias_control_result(self.transport_exchange(packet, self.timeout_s, self.retries, self._stop), request_id)
            except TimeoutError as error:
                raise BiasTimeoutError(str(error)) from error
        destination = (socket.gethostbyname(self.device_host), self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        with self._lock:
            self._socket = sock
        try:
            for _attempt in range(self.retries + 1):
                if self._stop.is_set():
                    raise BiasCancelledError("BIAS request cancelled")
                sock.sendto(packet, destination)
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline:
                    if self._stop.is_set():
                        raise BiasCancelledError("BIAS request cancelled")
                    sock.settimeout(min(0.1, max(0.001, deadline - time.monotonic())))
                    try:
                        response, source = sock.recvfrom(64)
                    except TimeoutError:
                        continue
                    except OSError as error:
                        if self._stop.is_set():
                            raise BiasCancelledError("BIAS request cancelled") from error
                        raise
                    if source != destination:
                        continue
                    try:
                        return parse_bias_control_result(response, request_id)
                    except BiasProtocolError:
                        continue
            raise BiasTimeoutError(
                f"BIAS request {request_id} timed out after {self.retries + 1} attempts"
            )
        finally:
            with self._lock:
                if self._socket is sock:
                    self._socket = None
            sock.close()

    def query(self, request_id: int | None = None) -> BiasResultData:
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        return self._exchange(
            build_bias_control_request(request_id, BiasOperation.QUERY, 0), request_id
        )

    def set_mask(self, target_mask: int, request_id: int | None = None) -> BiasSetOutcome:
        target = _mask(target_mask, allow_zero=False)
        return self._set_target(target, BiasOperation.SET, request_id)

    def disable(self, request_id: int | None = None) -> BiasSetOutcome:
        return self._set_target(0, BiasOperation.DISABLE, request_id)

    def _set_target(self, target: int, operation: BiasOperation,
                    request_id: int | None) -> BiasSetOutcome:
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        try:
            result = self._exchange(
                build_bias_control_request(request_id, operation, target), request_id
            )
            return BiasSetOutcome(result, target, request_id, False)
        except BiasTimeoutError:
            query_result = self.query()
            return BiasSetOutcome(query_result, target, request_id, True)

    def _emit(self, state: str, request_id: int, **values: object) -> None:
        event = ("bias", {"state": state, "request_id": request_id, **values})
        if self.events is not None:
            self.events.put(event)
        if self.callback is not None:
            self.callback(event)

    def start_query(self, request_id: int | None = None) -> int:
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        return self._start_worker("query", request_id, 0)

    def start_set(self, target_mask: int, request_id: int | None = None) -> int:
        target = _mask(target_mask, allow_zero=False)
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        return self._start_worker("set", request_id, target)

    def start_disable(self, request_id: int | None = None) -> int:
        request_id = self.new_request_id() if request_id is None else _request_id(request_id)
        return self._start_worker("disable", request_id, 0)

    def _start_worker(self, operation: str, request_id: int, target_mask: int) -> int:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a BIAS request is already active")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(operation, request_id, target_mask),
                name="p300-bias-control",
                daemon=True,
            )
            self._thread.start()
        return request_id

    def _run(self, operation: str, request_id: int, target_mask: int) -> None:
        self._emit("started", request_id, operation=operation, target_mask=target_mask)
        try:
            if operation == "query":
                result: object = self.query(request_id)
            elif operation == "disable":
                result = self.disable(request_id)
            else:
                result = self.set_mask(target_mask, request_id)
        except BiasCancelledError as error:
            self._emit(
                "cancelled", request_id, operation=operation,
                target_mask=target_mask, message=str(error),
            )
        except Exception as error:
            self._emit(
                "error", request_id, operation=operation,
                target_mask=target_mask, message=str(error), error=error,
            )
        else:
            self._emit(
                "result", request_id, operation=operation,
                target_mask=target_mask, result=result,
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
