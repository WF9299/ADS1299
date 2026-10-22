"""Shared P300 EEG_BATCH v1 codec and recording helpers."""

from __future__ import annotations

import csv
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CHANNELS = 8
EEG_JSON_DEVICE = "seu"
BATCH_SAMPLES = 20
SAMPLE_BYTES = 25
BATCH_BYTES = 528
HEADER_BYTES = 24
CRC_OFFSET = BATCH_BYTES - 4


@dataclass(frozen=True)
class EegBatch:
    stream_id: int
    config_id: int
    start_seq: int
    timestamp_us: int
    samples: np.ndarray
    markers: np.ndarray


def crc32c(data: bytes) -> int:
    """Return the CRC-32C (Castagnoli) used by the firmware."""
    crc = 0xFFFFFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return (~crc) & 0xFFFFFFFF


def _encode_int24(value: int) -> bytes:
    if not -8_388_608 <= int(value) <= 8_388_607:
        raise ValueError(f"int24 sample outside range: {value}")
    return (int(value) & 0xFFFFFF).to_bytes(3, "big")


def _decode_int24(data: bytes) -> int:
    value = int.from_bytes(data, "big")
    return value - 0x1000000 if value & 0x800000 else value


def encode_eeg_batch(batch: EegBatch) -> bytes:
    samples = np.asarray(batch.samples, dtype=np.int32)
    markers = np.asarray(batch.markers, dtype=np.uint16)
    if samples.shape != (BATCH_SAMPLES, CHANNELS):
        raise ValueError(f"samples must have shape ({BATCH_SAMPLES}, {CHANNELS})")
    if markers.shape != (BATCH_SAMPLES,):
        raise ValueError(f"markers must have shape ({BATCH_SAMPLES},)")
    if np.any(markers > 255):
        raise ValueError("EEG_BATCH v1 marker codes must fit in uint8")

    frame = bytearray(BATCH_BYTES)
    frame[:4] = b"\xA5\x5A\x11\x03"
    struct.pack_into(
        ">IHIQ", frame, 4, batch.stream_id & 0xFFFFFFFF, batch.config_id & 0xFFFF,
        batch.start_seq & 0xFFFFFFFF, batch.timestamp_us & 0xFFFFFFFFFFFFFFFF,
    )
    frame[22] = BATCH_SAMPLES
    frame[23] = 0xFF
    offset = HEADER_BYTES
    for sample, marker in zip(samples, markers, strict=True):
        for value in sample:
            frame[offset : offset + 3] = _encode_int24(int(value))
            offset += 3
        frame[offset] = int(marker)
        offset += 1
    struct.pack_into(">I", frame, CRC_OFFSET, crc32c(frame[:CRC_OFFSET]))
    return bytes(frame)


def decode_eeg_batch(frame: bytes) -> EegBatch:
    if len(frame) != BATCH_BYTES:
        raise ValueError(f"invalid EEG_BATCH size: {len(frame)}")
    if frame[:4] != b"\xA5\x5A\x11\x03":
        raise ValueError("invalid EEG_BATCH magic, version, or type")
    if frame[22] != BATCH_SAMPLES or frame[23] != 0xFF:
        raise ValueError("unsupported EEG_BATCH sample count or channel mask")
    expected_crc = struct.unpack_from(">I", frame, CRC_OFFSET)[0]
    actual_crc = crc32c(frame[:CRC_OFFSET])
    if expected_crc != actual_crc:
        raise ValueError("EEG_BATCH CRC32C mismatch")

    stream_id, config_id, start_seq, timestamp_us = struct.unpack_from(">IHIQ", frame, 4)
    samples = np.empty((BATCH_SAMPLES, CHANNELS), dtype=np.int32)
    markers = np.empty(BATCH_SAMPLES, dtype=np.uint16)
    offset = HEADER_BYTES
    for index in range(BATCH_SAMPLES):
        for channel in range(CHANNELS):
            samples[index, channel] = _decode_int24(frame[offset : offset + 3])
            offset += 3
        markers[index] = frame[offset]
        offset += 1
    return EegBatch(stream_id, config_id, start_seq, timestamp_us, samples, markers)


class BatchStreamParser:
    """Incrementally parse the TCP config prefix followed by fixed batch frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.config: dict | None = None

    def feed(self, data: bytes) -> list[EegBatch]:
        self._buffer.extend(data)
        batches: list[EegBatch] = []
        if self.config is None:
            if len(self._buffer) < 4:
                return batches
            length = int.from_bytes(self._buffer[:4], "big")
            if not 2 <= length <= 16_384:
                raise ValueError(f"invalid STREAM_CONFIG length: {length}")
            if len(self._buffer) < 4 + length:
                return batches
            try:
                decoded = json.loads(bytes(self._buffer[4 : 4 + length]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("invalid STREAM_CONFIG JSON") from error
            if not isinstance(decoded, dict):
                raise ValueError("STREAM_CONFIG must be a JSON object")
            self.config = decoded
            del self._buffer[: 4 + length]
        while len(self._buffer) >= BATCH_BYTES:
            frame = bytes(self._buffer[:BATCH_BYTES])
            del self._buffer[:BATCH_BYTES]
            batches.append(decode_eeg_batch(frame))
        return batches


def codes_to_uv(codes: np.ndarray, vref_v: float = 4.5, pga_gain: float = 12.0) -> np.ndarray:
    if vref_v <= 0 or pga_gain <= 0:
        raise ValueError("vref_v and pga_gain must be positive")
    return np.asarray(codes, dtype=np.float64) * (vref_v / (pga_gain * 8_388_607.0)) * 1_000_000.0


def batch_to_record(batch: EegBatch, config: dict | None = None) -> dict:
    config = config or {}
    sampling_rate = int(config.get("sampling_rate", config.get("sample_rate_hz", 1000)))
    channels = int(config.get("channels", CHANNELS))
    if channels != CHANNELS:
        raise ValueError(f"only {CHANNELS}-channel EEG_BATCH v1 is supported")
    values = codes_to_uv(
        batch.samples,
        float(config.get("vref_v", 4.5)),
        float(config.get("pga_gain", 12.0)),
    )
    return {
        "data": values.tolist(),
        "timestamp": batch.timestamp_us / 1_000_000.0,
        "sampling_rate": sampling_rate,
        "channels": channels,
        "device": EEG_JSON_DEVICE,
        "batch": True,
        "markers": [int(value) for value in batch.markers],
        "eeg_type": int(config.get("eeg_type", 1)),
        **{key: config[key] for key in ("bias", "bias_mask", "bias_verified") if key in config},
    }


def export_jsonl_to_json(source: Path | str, target: Path | str) -> int:
    """Stream JSONL batches to a single JSON document without loading all data."""
    count = 0
    with Path(source).open("r", encoding="utf-8") as input_file, Path(target).open("w", encoding="utf-8") as output_file:
        output_file.write('{"batches":[')
        for line in input_file:
            if not line.strip():
                continue
            json.loads(line)  # Reject incomplete/corrupt lines rather than exporting invalid JSON.
            if count:
                output_file.write(",")
            output_file.write(line.strip())
            count += 1
        output_file.write("]}\n")
    return count


def export_jsonl_to_csv(source: Path | str, target: Path | str) -> int:
    """Stream JSONL batches to one CSV row per sample and return row count."""
    rows = 0
    header = ["timestamp", *(f"ch{index}_uv" for index in range(1, CHANNELS + 1)), "marker"]
    with Path(source).open("r", encoding="utf-8") as input_file, Path(target).open(
        "w", encoding="utf-8", newline=""
    ) as output_file:
        writer = csv.writer(output_file)
        writer.writerow(header)
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            data = record.get("data")
            markers = record.get("markers")
            rate = int(record.get("sampling_rate", 0))
            timestamp = float(record.get("timestamp", 0.0))
            if rate <= 0 or not isinstance(data, list) or not isinstance(markers, list):
                raise ValueError(f"invalid JSONL record at line {line_number}")
            if len(data) != len(markers):
                raise ValueError(f"data/markers length mismatch at line {line_number}")
            for index, (sample, marker) in enumerate(zip(data, markers, strict=True)):
                if not isinstance(sample, list) or len(sample) != CHANNELS:
                    raise ValueError(f"sample must contain {CHANNELS} channels at line {line_number}")
                writer.writerow([timestamp + index / rate, *sample, int(marker)])
                rows += 1
    return rows
