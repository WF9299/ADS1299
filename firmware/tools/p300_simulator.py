"""Deterministic TCP EEG_BATCH v1 signal simulator for the P300 monitor."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from pathlib import Path
import sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.p300_common import BATCH_SAMPLES, CHANNELS, EegBatch, encode_eeg_batch
from tools.p300_self_test import (
    CONFIG_INTERNAL_SHORT,
    CONFIG_NORMAL,
    SelfTestFlags,
    SelfTestProtocolError,
    SelfTestResultData,
    SelfTestStatus,
    build_self_test_result,
    parse_self_test_request,
)
from tools.p300_bias import (
    BiasFlags,
    BiasOperation,
    BiasProtocolError,
    BiasResultData,
    BiasStatus,
    build_bias_control_result,
    parse_bias_control_request,
)
from tools.p300_tx_control import (
    TxFlags,
    TxOperation,
    TxProtocolError,
    TxResultData,
    TxStatus,
    build_tx_control_result,
    parse_tx_control_request,
)

SAMPLE_RATE = 1000
VREF_V = 4.5
PGA_GAIN = 12.0
UV_PER_CODE = VREF_V / (PGA_GAIN * 8_388_607.0) * 1_000_000.0


def build_stream_config(
    config_id: int = CONFIG_NORMAL,
    stream_id: int = 1,
    tx_group_frames: int = 1,
) -> dict:
    return {
        "protocol": "P300 EEG_BATCH v1",
        "sampling_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "device": "BrainCo-Simulator",
        "vref_v": VREF_V,
        "pga_gain": PGA_GAIN,
        "eeg_type": 1,
        "config_id": int(config_id),
        "stream_id": int(stream_id),
        "tx_group_frames": int(tx_group_frames),
        "tx_group_period_ms": 20 * int(tx_group_frames),
    }


class SignalGenerator:
    def __init__(
        self,
        noise_uv: float = 2.0,
        line_noise_uv: float = 0.0,
        marker_period_s: float = 2.0,
        seed: int | None = 1,
        self_test_profile: str = "pass",
        quality_profile: str = "normal",
    ) -> None:
        if marker_period_s < BATCH_SAMPLES / SAMPLE_RATE:
            raise ValueError("marker_period_s must be at least one batch (0.02 s)")
        self.noise_uv = noise_uv
        self.line_noise_uv = line_noise_uv
        if self_test_profile not in {"pass", "warn", "fail"}:
            raise ValueError("self_test_profile must be pass, warn, or fail")
        if quality_profile not in {"normal", "mains", "dc", "clipped"}:
            raise ValueError("quality_profile must be normal, mains, dc, or clipped")
        self.self_test_profile = self_test_profile
        self.quality_profile = quality_profile
        self.marker_period_samples = round(marker_period_s * SAMPLE_RATE)
        self.rng = np.random.default_rng(seed)
        self.sequence = 0
        self.timestamp_us = time.time_ns() // 1_000
        self.channel_phase = np.linspace(0.0, np.pi, CHANNELS, endpoint=False)
        self.marker_sequences: list[int] = []
        self.config_id = CONFIG_NORMAL
        self.stream_id = 1
        self.bias_mask = 0xFF
        self.tx_group_frames = 1
        self._lock = threading.Lock()

    def set_config_id(self, config_id: int) -> bool:
        if config_id not in (CONFIG_NORMAL, CONFIG_INTERNAL_SHORT):
            raise ValueError("config_id must be NORMAL or INTERNAL_SHORT")
        with self._lock:
            if self.config_id == config_id:
                return False
            self.config_id = config_id
            self.stream_id = (self.stream_id + 1) & 0xFFFFFFFF or 1
            return True

    def set_bias_mask(self, bias_mask: int) -> bool:
        value = int(bias_mask)
        if not 1 <= value <= 0xFF:
            raise ValueError("bias_mask must be a non-zero uint8")
        with self._lock:
            if self.bias_mask == value:
                return False
            self.bias_mask = value
            self.stream_id = (self.stream_id + 1) & 0xFFFFFFFF or 1
            return True

    def set_tx_group_frames(self, frames: int) -> bool:
        value = int(frames)
        if value not in (1, 2):
            raise ValueError("tx_group_frames must be 1 or 2")
        with self._lock:
            if self.tx_group_frames == value:
                return False
            self.tx_group_frames = value
            return True

    def get_tx_group_frames(self) -> int:
        with self._lock:
            return self.tx_group_frames

    def _internal_short_uv(self, indices: np.ndarray) -> np.ndarray:
        amplitude_uv, noise_uv = {
            "pass": (0.4, 0.15),
            "warn": (2.0, 0.25),
            "fail": (8.0, 0.5),
        }[self.self_test_profile]
        seconds = indices / SAMPLE_RATE
        tone = amplitude_uv * np.sin(2.0 * np.pi * 50.0 * seconds[:, None])
        noise = self.rng.normal(0.0, noise_uv, (BATCH_SAMPLES, CHANNELS))
        return tone + noise

    def _p300_uv(self, sample_indices: np.ndarray) -> np.ndarray:
        response = np.zeros(sample_indices.shape, dtype=np.float64)
        retained: list[int] = []
        for marker_sequence in self.marker_sequences:
            elapsed = sample_indices - marker_sequence
            response += 8.0 * np.exp(-0.5 * ((elapsed - 300.0) / 70.0) ** 2)
            if elapsed[-1] < 800:
                retained.append(marker_sequence)
        self.marker_sequences = retained
        return response

    def next_batch(self) -> EegBatch:
        with self._lock:
            indices = np.arange(self.sequence, self.sequence + BATCH_SAMPLES)
            markers = np.zeros(BATCH_SAMPLES, dtype=np.uint16)
            if self.config_id == CONFIG_INTERNAL_SHORT:
                signal_uv = self._internal_short_uv(indices)
            else:
                for index, sequence in enumerate(indices):
                    if sequence % self.marker_period_samples == 0:
                        markers[index] = 1
                        self.marker_sequences.append(int(sequence))
                seconds = indices / SAMPLE_RATE
                alpha = 8.0 * np.sin(2.0 * np.pi * 10.0 * seconds[:, None] + self.channel_phase)
                line_amplitude = 80.0 if self.quality_profile == "mains" else self.line_noise_uv
                line = line_amplitude * np.sin(2.0 * np.pi * 50.0 * seconds[:, None])
                p300 = self._p300_uv(indices)[:, None] * np.linspace(0.6, 1.2, CHANNELS)[None, :]
                noise = self.rng.normal(0.0, self.noise_uv, (BATCH_SAMPLES, CHANNELS))
                signal_uv = alpha + line + p300 + noise
                if self.quality_profile == "dc":
                    signal_uv += 125_000.0
                elif self.quality_profile == "clipped":
                    signal_uv += 374_000.0
            codes = np.rint(signal_uv / UV_PER_CODE)
            codes = np.clip(codes, -8_388_608, 8_388_607).astype(np.int32)
            batch = EegBatch(
                self.stream_id,
                self.config_id,
                self.sequence,
                self.timestamp_us,
                codes,
                markers,
            )
            self.sequence += BATCH_SAMPLES
            self.timestamp_us += BATCH_SAMPLES * 1_000_000 // SAMPLE_RATE
            return batch


class SelfTestControlServer(threading.Thread):
    """UDP self-test, BIAS, and runtime TCP grouping server for GUI testing."""

    def __init__(
        self,
        generator: SignalGenerator,
        port: int = 1235,
        bias_set_status: BiasStatus | str = BiasStatus.OK,
    ) -> None:
        super().__init__(name="p300-simulator-self-test", daemon=True)
        self.generator = generator
        self.port = int(port)
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.error: OSError | None = None
        self._socket: socket.socket | None = None
        self._cache: dict[int, SelfTestResultData] = {}
        self._bias_cache: dict[int, BiasResultData] = {}
        self._tx_cache: dict[int, TxResultData] = {}
        if isinstance(bias_set_status, str):
            bias_set_status = BiasStatus[bias_set_status.upper()]
        self.bias_set_status = BiasStatus(bias_set_status)

    def _handle_self_test(self, packet: bytes) -> bytes | None:
        try:
            request_id, target = parse_self_test_request(packet)
        except (SelfTestProtocolError, ValueError):
            return None
        result = self._cache.get(request_id)
        cached = result is not None
        if result is None:
            changed = self.generator.set_config_id(target)
            result = SelfTestResultData(
                request_id=request_id,
                status=SelfTestStatus.OK,
                requested_config_id=target,
                actual_config_id=self.generator.config_id,
                flags=SelfTestFlags.CHANGED if changed else SelfTestFlags(0),
                stream_id=self.generator.stream_id,
            )
            self._cache[request_id] = result
        if cached:
            result = SelfTestResultData(
                result.request_id,
                result.status,
                result.requested_config_id,
                result.actual_config_id,
                result.flags | SelfTestFlags.CACHED,
                result.stream_id,
            )
        return build_self_test_result(result)

    def _handle_bias(self, packet: bytes) -> bytes | None:
        try:
            request_id, operation, requested_mask = parse_bias_control_request(packet)
        except (BiasProtocolError, ValueError):
            return None
        result = self._bias_cache.get(request_id)
        cached = result is not None
        if result is None:
            if operation == BiasOperation.QUERY:
                result = BiasResultData(
                    request_id, BiasStatus.OK, 0, self.generator.bias_mask,
                    BiasFlags.VERIFIED, self.generator.stream_id,
                )
            elif self.bias_set_status == BiasStatus.OK:
                changed = self.generator.set_bias_mask(requested_mask)
                flags = BiasFlags.VERIFIED | (BiasFlags.CHANGED if changed else BiasFlags(0))
                result = BiasResultData(
                    request_id, BiasStatus.OK, requested_mask,
                    self.generator.bias_mask, flags, self.generator.stream_id,
                )
            elif self.bias_set_status == BiasStatus.RESTORE_FAILED:
                result = BiasResultData(
                    request_id, BiasStatus.RESTORE_FAILED, requested_mask, 0,
                    BiasFlags(0), 0,
                )
            else:
                flags = (
                    BiasFlags.VERIFIED
                    if self.bias_set_status == BiasStatus.APPLY_FAILED
                    else BiasFlags(0)
                )
                result = BiasResultData(
                    request_id, self.bias_set_status, requested_mask,
                    self.generator.bias_mask, flags, self.generator.stream_id,
                )
            self._bias_cache[request_id] = result
        if cached:
            result = BiasResultData(
                result.request_id, result.status, result.requested_mask,
                result.actual_mask, result.flags | BiasFlags.CACHED, result.stream_id,
            )
        return build_bias_control_result(result)

    def _handle_tx_control(self, packet: bytes) -> bytes | None:
        try:
            request_id, operation, requested_frames = parse_tx_control_request(packet)
        except (TxProtocolError, ValueError):
            return None
        result = self._tx_cache.get(request_id)
        cached = result is not None
        if result is None:
            if operation == TxOperation.QUERY:
                result = TxResultData(
                    request_id,
                    TxStatus.OK,
                    0,
                    self.generator.get_tx_group_frames(),
                    TxFlags.VERIFIED,
                    self.generator.stream_id,
                )
            else:
                changed = self.generator.set_tx_group_frames(requested_frames)
                result = TxResultData(
                    request_id,
                    TxStatus.OK,
                    requested_frames,
                    self.generator.get_tx_group_frames(),
                    TxFlags.VERIFIED |
                    (TxFlags.CHANGED if changed else TxFlags(0)),
                    self.generator.stream_id,
                )
            self._tx_cache[request_id] = result
        if cached:
            result = TxResultData(
                result.request_id,
                result.status,
                result.requested_frames,
                result.actual_frames,
                result.flags | TxFlags.CACHED,
                result.stream_id,
            )
        return build_tx_control_result(result)

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", self.port))
            sock.settimeout(0.1)
            self._socket = sock
        except OSError as error:
            self.error = error
            self.ready.set()
            return
        self.ready.set()
        try:
            while not self.stop_event.is_set():
                try:
                    packet, source = sock.recvfrom(64)
                except TimeoutError:
                    continue
                except OSError:
                    break
                response = self._handle_self_test(packet)
                if response is None:
                    response = self._handle_bias(packet)
                if response is None:
                    response = self._handle_tx_control(packet)
                if response is not None:
                    sock.sendto(response, source)
        finally:
            sock.close()
            self._socket = None

    def stop(self) -> None:
        self.stop_event.set()
        sock = self._socket
        if sock is not None:
            sock.close()


def run_simulator(
    host: str,
    port: int,
    generator: SignalGenerator,
    duration_s: float | None,
    control_port: int | None = None,
    bias_set_status: BiasStatus = BiasStatus.OK,
) -> None:
    config = json.dumps(
        build_stream_config(
            generator.config_id,
            generator.stream_id,
            generator.get_tx_group_frames(),
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    start = time.monotonic()
    next_send = start
    control = (
        None if control_port is None
        else SelfTestControlServer(generator, control_port, bias_set_status)
    )
    if control is not None:
        control.start()
        control.ready.wait(1.0)
        if control.error is not None:
            raise OSError(f"cannot bind UDP control port {control_port}: {control.error}")
    try:
        with socket.create_connection((host, port), timeout=5.0) as connection:
            connection.sendall(len(config).to_bytes(4, "big") + config)
            print(f"Connected to {host}:{port}; sending {SAMPLE_RATE} SPS, Ctrl+C to stop.")
            pending_frames: list[bytes] = []
            pending_target = generator.get_tx_group_frames()
            while duration_s is None or time.monotonic() - start < duration_s:
                target = generator.get_tx_group_frames()
                if pending_frames and target != pending_target:
                    connection.sendall(b"".join(pending_frames))
                    pending_frames.clear()
                if not pending_frames:
                    pending_target = target
                pending_frames.append(encode_eeg_batch(generator.next_batch()))
                if len(pending_frames) >= pending_target:
                    connection.sendall(b"".join(pending_frames))
                    pending_frames.clear()
                next_send += BATCH_SAMPLES / SAMPLE_RATE
                time.sleep(max(0.0, next_send - time.monotonic()))
            if pending_frames:
                connection.sendall(b"".join(pending_frames))
    finally:
        if control is not None:
            control.stop()
            control.join(timeout=1.0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--noise-uv", type=float, default=2.0)
    parser.add_argument("--line-noise-uv", type=float, default=0.0)
    parser.add_argument("--marker-period", type=float, default=2.0, help="seconds")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--self-test-profile",
        choices=("pass", "warn", "fail"),
        default="pass",
        help="internal-short diagnostic profile",
    )
    parser.add_argument(
        "--quality-profile",
        choices=("normal", "mains", "dc", "clipped"),
        default="normal",
        help="NORMAL EEG quality diagnostic profile",
    )
    parser.add_argument(
        "--bias-set-status",
        choices=("ok", "busy", "apply_failed", "restore_failed"),
        default="ok",
        help="simulated response to BIAS SET; QUERY always returns actual state",
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=1235,
        help="UDP type-8/9 control port; use 0 to disable",
    )
    parser.add_argument("--duration", type=float, help="seconds; omit to run until Ctrl+C")
    args = parser.parse_args()
    generator = SignalGenerator(
        args.noise_uv,
        args.line_noise_uv,
        args.marker_period,
        args.seed,
        args.self_test_profile,
        args.quality_profile,
    )
    try:
        run_simulator(
            args.host,
            args.port,
            generator,
            args.duration,
            None if args.control_port == 0 else args.control_port,
            BiasStatus[args.bias_set_status.upper()],
        )
    except (ConnectionError, OSError) as error:
        raise SystemExit(f"TCP connection failed: {error}") from error


if __name__ == "__main__":
    main()
