"""UDP one-shot impedance protocol, client, and per-channel calibration."""

from __future__ import annotations

import json
import math
import queue
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum, IntFlag
from pathlib import Path
from typing import Callable

try:
    from .p300_common import CHANNELS, crc32c
except ImportError:  # Direct execution/import from the tools directory.
    from p300_common import CHANNELS, crc32c

MAGIC = b"\xA5\x51"
VERSION = 1
IMPEDANCE_REQUEST = 6
IMPEDANCE_RESULT = 7
IMPEDANCE_REQUEST_BYTES = 16
IMPEDANCE_RESULT_BYTES = 84
INVALID_U32 = 0xFFFFFFFF
CALIBRATION_VERSION = 2
INPUT_NETWORK_MODEL = "series_r_parallel_c_v1"

_REQUEST_PREFIX = struct.Struct(">2sBBIB3x")
_RESULT_PREFIX = struct.Struct(">2sBBIBBBBHH")
_CHANNEL_VALUES = struct.Struct(">8I")
_CRC = struct.Struct(">I")


class ImpedanceStatus(IntEnum):
    OK = 0
    BUSY = 1
    INVALID_REQUEST = 2
    PAUSE_TIMEOUT = 3
    CONFIG_FAILED = 4
    SAMPLE_TIMEOUT = 5
    RESTORE_FAILED = 6
    INTERNAL_ERROR = 7


class ImpedanceFlags(IntFlag):
    DC_VALID = 1 << 0
    AC_VALID = 1 << 1
    BACKGROUND_SUBTRACTED = 1 << 2
    CLIPPED = 1 << 3
    CACHED = 1 << 4


class ImpedanceModelStatus(str, Enum):
    OK = "ok"
    CLAMPED_LOW = "clamped_low"
    INVALID_RAW = "invalid_raw"
    OUT_OF_RANGE = "out_of_range"


class ImpedanceProtocolError(ValueError):
    """Raised when a UDP impedance packet is malformed or unrelated."""


class ImpedanceTimeoutError(TimeoutError):
    """Raised after the initial request and one same-ID retry both time out."""


class ImpedanceCancelledError(RuntimeError):
    """Raised when an active impedance request is cancelled."""


@dataclass(frozen=True)
class ImpedanceResultData:
    request_id: int
    status: ImpedanceStatus
    tested_mask: int
    open_mask: int
    flags: ImpedanceFlags
    duration_ms: int
    sample_count: int
    nominal_ohm: tuple[int | None, ...]
    excitation_nv_peak: tuple[int | None, ...]

    def channel_tested(self, channel: int) -> bool:
        _validate_channel_index(channel)
        return bool(self.tested_mask & (1 << channel))

    def channel_open(self, channel: int) -> bool:
        _validate_channel_index(channel)
        return bool(self.open_mask & (1 << channel))


def _validate_channel_index(channel: int) -> None:
    if not 0 <= int(channel) < CHANNELS:
        raise IndexError(f"channel index must be in 0..{CHANNELS - 1}")


def _optional_u32(values: tuple[int, ...]) -> tuple[int | None, ...]:
    return tuple(None if value == INVALID_U32 else value for value in values)


def build_impedance_request(request_id: int, channel_mask: int = 0xFF) -> bytes:
    if not 1 <= int(request_id) <= 0xFFFFFFFF:
        raise ValueError("request_id must be a non-zero uint32")
    if not 1 <= int(channel_mask) <= 0xFF:
        raise ValueError("channel_mask must be a non-zero uint8")
    prefix = _REQUEST_PREFIX.pack(MAGIC, VERSION, IMPEDANCE_REQUEST, int(request_id), int(channel_mask))
    return prefix + _CRC.pack(crc32c(prefix))


def parse_impedance_result(packet: bytes, expected_request_id: int | None = None) -> ImpedanceResultData:
    if len(packet) != IMPEDANCE_RESULT_BYTES:
        raise ImpedanceProtocolError(f"invalid impedance result size: {len(packet)}")
    expected_crc = _CRC.unpack_from(packet, 80)[0]
    if crc32c(packet[:80]) != expected_crc:
        raise ImpedanceProtocolError("impedance result CRC32C mismatch")

    magic, version, packet_type, request_id, status_value, tested_mask, open_mask, flags, duration_ms, sample_count = (
        _RESULT_PREFIX.unpack_from(packet, 0)
    )
    if magic != MAGIC or version != VERSION or packet_type != IMPEDANCE_RESULT:
        raise ImpedanceProtocolError("invalid impedance result magic, version, or type")
    if expected_request_id is not None and request_id != int(expected_request_id):
        raise ImpedanceProtocolError("impedance result request_id mismatch")
    try:
        status = ImpedanceStatus(status_value)
    except ValueError as error:
        raise ImpedanceProtocolError(f"unknown impedance status: {status_value}") from error

    nominal = _CHANNEL_VALUES.unpack_from(packet, 16)
    excitation = _CHANNEL_VALUES.unpack_from(packet, 48)
    return ImpedanceResultData(
        request_id=request_id,
        status=status,
        tested_mask=tested_mask,
        open_mask=open_mask,
        flags=ImpedanceFlags(flags),
        duration_ms=duration_ms,
        sample_count=sample_count,
        nominal_ohm=_optional_u32(nominal),
        excitation_nv_peak=_optional_u32(excitation),
    )


@dataclass(frozen=True)
class InputNetworkModel:
    model: str = INPUT_NETWORK_MODEL
    series_ohm: float = 4_990.0
    shunt_capacitance_f: float = 4.7e-9
    lead_off_frequency_hz: float = 31.25
    max_parallel_fraction: float = 0.9

    def validate(self) -> None:
        if self.model != INPUT_NETWORK_MODEL:
            raise ValueError(f"unsupported input network model: {self.model}")
        positive_values = (
            self.series_ohm,
            self.shunt_capacitance_f,
            self.lead_off_frequency_hz,
        )
        if not all(math.isfinite(float(value)) and float(value) > 0.0 for value in positive_values):
            raise ValueError("input network resistance, capacitance, and frequency must be finite and positive")
        if not math.isfinite(float(self.max_parallel_fraction)) or not 0.0 < self.max_parallel_fraction < 1.0:
            raise ValueError("input network max_parallel_fraction must be finite and in (0, 1)")

    @property
    def capacitive_susceptance_s(self) -> float:
        self.validate()
        return 2.0 * math.pi * self.lead_off_frequency_hz * self.shunt_capacitance_f

    @property
    def capacitive_limit_ohm(self) -> float:
        return 1.0 / self.capacitive_susceptance_s

    def compensate(self, nominal_ohm: float | int | None) -> tuple[float | None, ImpedanceModelStatus]:
        self.validate()
        if nominal_ohm is None:
            return None, ImpedanceModelStatus.INVALID_RAW
        value = float(nominal_ohm)
        if not math.isfinite(value) or value < 0.0:
            return None, ImpedanceModelStatus.INVALID_RAW
        if value == 0.0:
            return 0.0, ImpedanceModelStatus.CLAMPED_LOW

        susceptance = self.capacitive_susceptance_s
        capacitive_limit = 1.0 / susceptance
        if value >= capacitive_limit * self.max_parallel_fraction:
            return None, ImpedanceModelStatus.OUT_OF_RANGE
        inverse_resistance_squared = 1.0 / (value * value) - susceptance * susceptance
        if not math.isfinite(inverse_resistance_squared) or inverse_resistance_squared <= 0.0:
            return None, ImpedanceModelStatus.OUT_OF_RANGE

        loop_ohm = 1.0 / math.sqrt(inverse_resistance_squared)
        corrected = loop_ohm - self.series_ohm
        if not math.isfinite(corrected):
            return None, ImpedanceModelStatus.OUT_OF_RANGE
        if corrected <= 0.0:
            return 0.0, ImpedanceModelStatus.CLAMPED_LOW
        return corrected, ImpedanceModelStatus.OK

    @classmethod
    def from_dict(cls, document: object) -> "InputNetworkModel":
        if not isinstance(document, dict):
            raise ValueError("input_network must be an object")
        try:
            network = cls(
                model=str(document["model"]),
                series_ohm=float(document["series_ohm"]),
                shunt_capacitance_f=float(document["shunt_capacitance_f"]),
                lead_off_frequency_hz=float(document["lead_off_frequency_hz"]),
                max_parallel_fraction=float(document["max_parallel_fraction"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid input_network configuration") from error
        network.validate()
        return network

    def to_dict(self) -> dict:
        self.validate()
        return {
            "model": self.model,
            "series_ohm": self.series_ohm,
            "shunt_capacitance_f": self.shunt_capacitance_f,
            "lead_off_frequency_hz": self.lead_off_frequency_hz,
            "max_parallel_fraction": self.max_parallel_fraction,
        }


@dataclass
class ChannelCalibration:
    gain: float = 1.0
    offset_ohm: float = 0.0
    calibrated: bool = False

    def validate(self) -> None:
        if not math.isfinite(self.gain) or self.gain <= 0.0:
            raise ValueError("calibration gain must be finite and positive")
        if not math.isfinite(self.offset_ohm):
            raise ValueError("calibration offset_ohm must be finite")


@dataclass(frozen=True)
class ImpedanceEstimate:
    nominal_ohm: float | None
    network_compensated_ohm: float | None
    display_ohm: float | None
    model_status: ImpedanceModelStatus
    calibrated: bool


@dataclass
class ImpedanceCalibration:
    device: str = "ESP32-C5-ADS1299-R8"
    channels: list[ChannelCalibration] = field(
        default_factory=lambda: [ChannelCalibration() for _ in range(CHANNELS)]
    )
    input_network: InputNetworkModel = field(default_factory=InputNetworkModel)
    version: int = CALIBRATION_VERSION
    load_warning: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def default(cls, device: str = "ESP32-C5-ADS1299-R8") -> "ImpedanceCalibration":
        return cls(device=device)

    @classmethod
    def from_dict(cls, document: object) -> "ImpedanceCalibration":
        if not isinstance(document, dict):
            raise ValueError("calibration must be a JSON object")
        version = document.get("version")
        if version not in (1, CALIBRATION_VERSION):
            raise ValueError(f"unsupported calibration version: {version}")
        raw_channels = document.get("channels")
        if not isinstance(raw_channels, list) or len(raw_channels) != CHANNELS:
            raise ValueError(f"calibration must contain exactly {CHANNELS} channels")
        channels: list[ChannelCalibration] = []
        for index, item in enumerate(raw_channels):
            if not isinstance(item, dict):
                raise ValueError(f"calibration channel {index + 1} must be an object")
            try:
                channel = ChannelCalibration(
                    gain=float(item["gain"]),
                    offset_ohm=float(item["offset_ohm"]),
                    calibrated=bool(item["calibrated"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid calibration channel {index + 1}") from error
            channel.validate()
            channels.append(channel)
        device = document.get("device", "ESP32-C5-ADS1299-R8")
        if not isinstance(device, str) or not device:
            raise ValueError("calibration device must be a non-empty string")
        if version == 1:
            if any(channel.calibrated for channel in channels):
                raise ValueError("calibration version 1 contains calibrated channels and requires recalibration")
            return cls(
                device=device,
                channels=channels,
                load_warning="calibration version 1 migrated to version 2 with the default input network",
            )
        input_network = InputNetworkModel.from_dict(document.get("input_network"))
        return cls(device=device, channels=channels, input_network=input_network)

    @classmethod
    def load(cls, path: Path | str) -> "ImpedanceCalibration":
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read calibration file: {error}") from error
        return cls.from_dict(document)

    @classmethod
    def load_or_default(
        cls, path: Path | str, device: str = "ESP32-C5-ADS1299-R8"
    ) -> tuple["ImpedanceCalibration", str | None]:
        try:
            calibration = cls.load(path)
            return calibration, calibration.load_warning
        except ValueError as error:
            return cls.default(device), str(error)

    def to_dict(self) -> dict:
        if len(self.channels) != CHANNELS:
            raise ValueError(f"calibration must contain exactly {CHANNELS} channels")
        for channel in self.channels:
            channel.validate()
        self.input_network.validate()
        return {
            "version": CALIBRATION_VERSION,
            "device": self.device,
            "input_network": self.input_network.to_dict(),
            "channels": [
                {
                    "gain": channel.gain,
                    "offset_ohm": channel.offset_ohm,
                    "calibrated": channel.calibrated,
                }
                for channel in self.channels
            ],
        }

    def save(self, path: Path | str) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(destination)

    def estimate(self, channel: int, nominal_ohm: float | int | None) -> ImpedanceEstimate:
        _validate_channel_index(channel)
        calibration = self.channels[channel]
        calibration.validate()
        compensated, status = self.input_network.compensate(nominal_ohm)
        nominal = None if nominal_ohm is None else float(nominal_ohm)
        display = None
        if compensated is not None:
            corrected = compensated * calibration.gain + calibration.offset_ohm
            if math.isfinite(corrected) and corrected >= 0.0:
                display = corrected
        return ImpedanceEstimate(
            nominal_ohm=nominal,
            network_compensated_ohm=compensated,
            display_ohm=display,
            model_status=status,
            calibrated=calibration.calibrated,
        )

    def apply(self, channel: int, nominal_ohm: float | int | None) -> float | None:
        return self.estimate(channel, nominal_ohm).display_ohm

    def set_two_point(
        self,
        channel: int,
        low_nominal_ohm: float,
        high_nominal_ohm: float,
        *,
        low_reference_ohm: float = 10_000.0,
        high_reference_ohm: float = 47_000.0,
    ) -> None:
        _validate_channel_index(channel)
        values = (low_nominal_ohm, high_nominal_ohm, low_reference_ohm, high_reference_ohm)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("two-point calibration values must be finite")
        low_compensated, low_status = self.input_network.compensate(low_nominal_ohm)
        high_compensated, high_status = self.input_network.compensate(high_nominal_ohm)
        if low_status != ImpedanceModelStatus.OK or high_status != ImpedanceModelStatus.OK:
            raise ValueError("calibration points must be inside the valid input-network range")
        assert low_compensated is not None and high_compensated is not None
        denominator = high_compensated - low_compensated
        reference_span = float(high_reference_ohm) - float(low_reference_ohm)
        if denominator <= 0.0 or reference_span <= 0.0:
            raise ValueError("high calibration point must exceed the low point")
        gain = reference_span / denominator
        offset = float(low_reference_ohm) - gain * low_compensated
        calibration = ChannelCalibration(gain=gain, offset_ohm=offset, calibrated=True)
        calibration.validate()
        self.channels[channel] = calibration


def build_impedance_report(result: ImpedanceResultData, calibration: ImpedanceCalibration) -> dict:
    channels: list[dict] = []
    for channel in range(CHANNELS):
        nominal = result.nominal_ohm[channel]
        if not result.channel_tested(channel):
            estimate = None
            model_status = "not_tested"
        elif result.channel_open(channel):
            estimate = None
            model_status = "open"
        elif result.status != ImpedanceStatus.OK:
            estimate = None
            model_status = "measurement_failed"
        else:
            estimate = calibration.estimate(channel, nominal)
            model_status = estimate.model_status.value
        channels.append(
            {
                "channel": channel + 1,
                "nominal_ohm": nominal,
                "network_compensated_ohm": None if estimate is None else estimate.network_compensated_ohm,
                "display_ohm": None if estimate is None else estimate.display_ohm,
                "model_status": model_status,
                "excitation_nv_peak": result.excitation_nv_peak[channel],
                "open": result.channel_open(channel),
                "calibrated": calibration.channels[channel].calibrated,
            }
        )
    return {
        "request_id": result.request_id,
        "status": result.status.name,
        "tested_mask": result.tested_mask,
        "open_mask": result.open_mask,
        "flags": int(result.flags),
        "duration_ms": result.duration_ms,
        "sample_count": result.sample_count,
        "input_network": calibration.input_network.to_dict(),
        "calibration": calibration.to_dict(),
        "channels": channels,
    }


class ImpedanceClient:
    """One-shot UDP client with a single same-request-ID retry."""

    def __init__(
        self,
        device_host: str,
        port: int = 1235,
        *,
        timeout_s: float = 5.0,
        retries: int = 1,
        events: queue.Queue | None = None,
        callback: Callable[[tuple[str, dict]], None] | None = None,
        exchange=None,
    ) -> None:
        if not device_host:
            raise ValueError("device_host is required")
        if not 1 <= int(port) <= 65535:
            raise ValueError("port must be in 1..65535")
        if timeout_s <= 0.0 or retries < 0:
            raise ValueError("timeout_s must be positive and retries non-negative")
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

    def _emit(self, state: str, request_id: int, **values: object) -> None:
        event = ("impedance", {"state": state, "request_id": request_id, **values})
        if self.events is not None:
            self.events.put(event)
        if self.callback is not None:
            self.callback(event)

    def measure(self, channel_mask: int = 0xFF, request_id: int | None = None) -> ImpedanceResultData:
        request_id = self.new_request_id() if request_id is None else int(request_id)
        packet = build_impedance_request(request_id, channel_mask)
        if self.transport_exchange is not None:
            try:
                return parse_impedance_result(self.transport_exchange(packet, self.timeout_s, self.retries, self._stop), request_id)
            except TimeoutError as error:
                raise ImpedanceTimeoutError(str(error)) from error
        destination_ip = socket.gethostbyname(self.device_host)
        destination = (destination_ip, self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        with self._lock:
            self._socket = sock
        try:
            for _attempt in range(self.retries + 1):
                if self._stop.is_set():
                    raise ImpedanceCancelledError("impedance request cancelled")
                sock.sendto(packet, destination)
                deadline = time.monotonic() + self.timeout_s
                while True:
                    if self._stop.is_set():
                        raise ImpedanceCancelledError("impedance request cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        break
                    sock.settimeout(min(remaining, 0.1))
                    try:
                        response, source = sock.recvfrom(512)
                    except TimeoutError:
                        continue
                    except OSError as error:
                        if self._stop.is_set():
                            raise ImpedanceCancelledError("impedance request cancelled") from error
                        raise
                    if source[0] != destination_ip or source[1] != self.port:
                        continue
                    try:
                        return parse_impedance_result(response, expected_request_id=request_id)
                    except ImpedanceProtocolError:
                        continue
            raise ImpedanceTimeoutError(
                f"impedance request {request_id} timed out after {self.retries + 1} attempts"
            )
        finally:
            with self._lock:
                if self._socket is sock:
                    self._socket = None
            sock.close()

    def start(self, channel_mask: int = 0xFF, request_id: int | None = None) -> int:
        request_id = self.new_request_id() if request_id is None else int(request_id)
        build_impedance_request(request_id, channel_mask)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("an impedance request is already active")
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(channel_mask, request_id),
                name="p300-impedance",
                daemon=True,
            )
            self._thread.start()
        return request_id

    def _run(self, channel_mask: int, request_id: int) -> None:
        self._emit("started", request_id, channel_mask=channel_mask)
        try:
            result = self.measure(channel_mask, request_id)
        except ImpedanceCancelledError as error:
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
