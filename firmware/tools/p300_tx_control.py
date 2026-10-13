"""Runtime TCP EEG packet-group UDP protocol and non-blocking client."""

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
    from .p300_common import crc32c
except ImportError:  # Direct execution/import from the tools directory.
    from p300_common import crc32c

MAGIC = b"\xA5\x51"
VERSION = 1
TX_CONTROL_REQUEST = 12
TX_CONTROL_RESULT = 13
TX_CONTROL_REQUEST_BYTES = 16
TX_CONTROL_RESULT_BYTES = 20

_REQUEST_PREFIX = struct.Struct(">2sBBIBB2x")
_RESULT_PREFIX = struct.Struct(">2sBBIBBBBI")
_CRC = struct.Struct(">I")


class TxOperation(IntEnum):
    QUERY = 0
    SET = 1


class TxStatus(IntEnum):
    OK = 0
    BUSY = 1
    INVALID_REQUEST = 2
    NO_TCP_STREAM = 3
    INTERNAL_ERROR = 4


class TxFlags(IntFlag):
    CHANGED = 1 << 0
    CACHED = 1 << 1
    VERIFIED = 1 << 2


class TxProtocolError(ValueError):
    """Raised when a TX-control packet is malformed or unrelated."""


class TxTimeoutError(TimeoutError):
    """Raised when all same-request-ID attempts time out."""


class TxCancelledError(RuntimeError):
    """Raised when an asynchronous TX-control operation is cancelled."""


@dataclass(frozen=True)
class TxResultData:
    request_id: int
    status: TxStatus
    requested_frames: int
    actual_frames: int
    flags: TxFlags
    stream_id: int

    @property
    def verified(self) -> bool:
        return bool(self.flags & TxFlags.VERIFIED)


@dataclass(frozen=True)
class TxSetOutcome:
    result: TxResultData
    target_frames: int
    original_request_id: int
    reconciled: bool = False

    @property
    def applied(self) -> bool:
        return (
            self.result.status == TxStatus.OK
            and self.result.verified
            and self.result.actual_frames == self.target_frames
        )


def _request_id(value: int) -> int:
    result = int(value)
    if not 1 <= result <= 0xFFFFFFFF:
        raise ValueError("request_id must be a non-zero uint32")
    return result


def _frames(value: int, *, allow_zero: bool) -> int:
    result = int(value)
    valid = (0, 1, 2) if allow_zero else (1, 2)
    if result not in valid:
        raise ValueError("TX group frames must be 1 or 2")
    return result


def build_tx_control_request(
    request_id: int,
    operation: TxOperation | int,
    requested_frames: int = 0,
) -> bytes:
    request = _request_id(request_id)
    try:
        op = TxOperation(operation)
    except ValueError as error:
        raise ValueError("operation must be QUERY or SET") from error
    frames = _frames(requested_frames, allow_zero=op == TxOperation.QUERY)
    if (op == TxOperation.QUERY and frames != 0) or (
        op == TxOperation.SET and frames not in (1, 2)
    ):
        raise ValueError("QUERY requires 0 frames; SET requires 1 or 2")
    prefix = _REQUEST_PREFIX.pack(
        MAGIC, VERSION, TX_CONTROL_REQUEST, request, int(op), frames
    )
    return prefix + _CRC.pack(crc32c(prefix))


def parse_tx_control_request(packet: bytes) -> tuple[int, TxOperation, int]:
    if len(packet) != TX_CONTROL_REQUEST_BYTES:
        raise TxProtocolError(f"invalid TX-control request size: {len(packet)}")
    if crc32c(packet[:12]) != _CRC.unpack_from(packet, 12)[0]:
        raise TxProtocolError("TX-control request CRC32C mismatch")
    magic, version, packet_type, request_id, operation_value, frames = (
        _REQUEST_PREFIX.unpack_from(packet, 0)
    )
    if magic != MAGIC or version != VERSION or packet_type != TX_CONTROL_REQUEST:
        raise TxProtocolError("invalid TX-control request magic, version, or type")
    try:
        operation = TxOperation(operation_value)
        build_tx_control_request(request_id, operation, frames)
    except ValueError as error:
        raise TxProtocolError(str(error)) from error
    return request_id, operation, frames


def build_tx_control_result(result: TxResultData) -> bytes:
    request_id = _request_id(result.request_id)
    try:
        status = TxStatus(result.status)
    except ValueError as error:
        raise ValueError("unknown TX-control status") from error
    requested = _frames(result.requested_frames, allow_zero=True)
    actual = _frames(result.actual_frames, allow_zero=True)
    flags = int(TxFlags(result.flags)) & 0xFF
    stream_id = int(result.stream_id)
    if not 0 <= stream_id <= 0xFFFFFFFF:
        raise ValueError("stream_id must fit uint32")
    prefix = _RESULT_PREFIX.pack(
        MAGIC,
        VERSION,
        TX_CONTROL_RESULT,
        request_id,
        int(status),
        requested,
        actual,
        flags,
        stream_id,
    )
    return prefix + _CRC.pack(crc32c(prefix))


def parse_tx_control_result(
    packet: bytes,
    expected_request_id: int | None = None,
) -> TxResultData:
    if len(packet) != TX_CONTROL_RESULT_BYTES:
        raise TxProtocolError(f"invalid TX-control result size: {len(packet)}")
    if crc32c(packet[:16]) != _CRC.unpack_from(packet, 16)[0]:
        raise TxProtocolError("TX-control result CRC32C mismatch")
    values = _RESULT_PREFIX.unpack_from(packet, 0)
    magic, version, packet_type, request_id = values[:4]
    if magic != MAGIC or version != VERSION or packet_type != TX_CONTROL_RESULT:
        raise TxProtocolError("invalid TX-control result magic, version, or type")
    if expected_request_id is not None and request_id != int(expected_request_id):
        raise TxProtocolError("TX-control result request_id mismatch")
    try:
        status = TxStatus(values[4])
        requested = _frames(values[5], allow_zero=True)
        actual = _frames(values[6], allow_zero=True)
    except ValueError as error:
        raise TxProtocolError(str(error)) from error
    result = TxResultData(
        request_id=request_id,
        status=status,
        requested_frames=requested,
        actual_frames=actual,
        flags=TxFlags(values[7]),
        stream_id=values[8],
    )
    if result.verified and result.actual_frames not in (1, 2):
        raise TxProtocolError("verified TX-control result has no active frame count")
    return result


class TxControlClient:
    """TX grouping client with same-ID retries and SET timeout reconciliation."""

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
        if (
            not device_host
            or not 1 <= int(port) <= 65535
            or timeout_s <= 0
            or retries < 0
        ):
            raise ValueError("invalid TX-control client host, port, timeout, or retries")
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

    def _exchange(self, packet: bytes, request_id: int) -> TxResultData:
        if self.transport_exchange is not None:
            try:
                return parse_tx_control_result(self.transport_exchange(packet, self.timeout_s, self.retries, self._stop), request_id)
            except TimeoutError as error:
                raise TxTimeoutError(str(error)) from error
        destination = (socket.gethostbyname(self.device_host), self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        with self._lock:
            self._socket = sock
        try:
            for _attempt in range(self.retries + 1):
                if self._stop.is_set():
                    raise TxCancelledError("TX-control request cancelled")
                sock.sendto(packet, destination)
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline:
                    if self._stop.is_set():
                        raise TxCancelledError("TX-control request cancelled")
                    sock.settimeout(
                        min(0.1, max(0.001, deadline - time.monotonic()))
                    )
                    try:
                        response, source = sock.recvfrom(64)
                    except TimeoutError:
                        continue
                    except OSError as error:
                        if self._stop.is_set():
                            raise TxCancelledError(
                                "TX-control request cancelled"
                            ) from error
                        raise
                    if source != destination:
                        continue
                    try:
                        return parse_tx_control_result(response, request_id)
                    except TxProtocolError:
                        continue
            raise TxTimeoutError(
                f"TX-control request {request_id} timed out after "
                f"{self.retries + 1} attempts"
            )
        finally:
            with self._lock:
                if self._socket is sock:
                    self._socket = None
            sock.close()

    def query(self, request_id: int | None = None) -> TxResultData:
        request_id = (
            self.new_request_id() if request_id is None else _request_id(request_id)
        )
        return self._exchange(
            build_tx_control_request(request_id, TxOperation.QUERY, 0),
            request_id,
        )

    def set_frames(
        self,
        target_frames: int,
        request_id: int | None = None,
    ) -> TxSetOutcome:
        target = _frames(target_frames, allow_zero=False)
        request_id = (
            self.new_request_id() if request_id is None else _request_id(request_id)
        )
        try:
            result = self._exchange(
                build_tx_control_request(request_id, TxOperation.SET, target),
                request_id,
            )
            return TxSetOutcome(result, target, request_id, False)
        except TxTimeoutError:
            return TxSetOutcome(self.query(), target, request_id, True)

    def _emit(self, state: str, request_id: int, **values: object) -> None:
        event = ("tx_control", {"state": state, "request_id": request_id, **values})
        if self.events is not None:
            self.events.put(event)
        if self.callback is not None:
            self.callback(event)

    def start_query(self, request_id: int | None = None) -> int:
        request_id = (
            self.new_request_id() if request_id is None else _request_id(request_id)
        )
        return self._start_worker("query", request_id, 0)

    def start_set(
        self,
        target_frames: int,
        request_id: int | None = None,
    ) -> int:
        target = _frames(target_frames, allow_zero=False)
        request_id = (
            self.new_request_id() if request_id is None else _request_id(request_id)
        )
        return self._start_worker("set", request_id, target)

    def _start_worker(
        self,
        operation: str,
        request_id: int,
        target_frames: int,
    ) -> int:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a TX-control request is already active")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(operation, request_id, target_frames),
                name="p300-tx-control",
                daemon=True,
            )
            self._thread.start()
        return request_id

    def _run(
        self,
        operation: str,
        request_id: int,
        target_frames: int,
    ) -> None:
        self._emit(
            "started",
            request_id,
            operation=operation,
            target_frames=target_frames,
        )
        try:
            result: object = (
                self.query(request_id)
                if operation == "query"
                else self.set_frames(target_frames, request_id)
            )
        except TxCancelledError as error:
            self._emit(
                "cancelled",
                request_id,
                operation=operation,
                target_frames=target_frames,
                message=str(error),
            )
        except Exception as error:
            self._emit(
                "error",
                request_id,
                operation=operation,
                target_frames=target_frames,
                message=str(error),
                error=error,
            )
        else:
            self._emit(
                "result",
                request_id,
                operation=operation,
                target_frames=target_frames,
                result=result,
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
