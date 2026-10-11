"""Contiguous raw EEG quality metrics for the desktop monitor."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import numpy as np

try:
    from .p300_common import CHANNELS, EegBatch, codes_to_uv
    from .p300_self_test import CONFIG_NORMAL
except ImportError:
    from p300_common import CHANNELS, EegBatch, codes_to_uv
    from p300_self_test import CONFIG_NORMAL

FS_CODE = 8_388_607


class QualityGrade(str, Enum):
    GOOD = "GOOD"
    WARN = "WARN"
    BAD = "BAD"
    COLLECTING = "COLLECTING"


@dataclass(frozen=True)
class ChannelQualityMetrics:
    dc_mv: float
    peak_fs_percent: float
    clipped_samples: int
    rms_uv: float
    peak_to_peak_uv: float
    mains_50hz_uv: float
    grade: QualityGrade
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SignalQualitySnapshot:
    ready: bool
    sample_count: int
    channels: tuple[ChannelQualityMetrics, ...]
    overall: QualityGrade
    error: str | None = None
    bypassed: bool = False


@dataclass(frozen=True)
class DisplayChannelQualityMetrics:
    rms_uv: float
    peak_to_peak_uv: float
    mains_50hz_uv: float
    grade: QualityGrade


@dataclass(frozen=True)
class DisplaySignalQualitySnapshot:
    ready: bool
    sample_count: int
    channels: tuple[DisplayChannelQualityMetrics, ...]
    overall: QualityGrade
    error: str | None = None
    bypassed: bool = False


def _mains_amplitude(values: np.ndarray, sample_rate_hz: int) -> np.ndarray:
    sample_count = int(values.shape[0])
    window = np.hanning(sample_count)
    indices = np.arange(sample_count, dtype=np.float64)
    basis = np.exp(-2j * np.pi * 50.0 * indices / float(sample_rate_hz))
    return (
        2.0
        * np.abs(np.sum(values * window[:, None] * basis[:, None], axis=0))
        / np.sum(window)
    )


def mains_suppression_db(raw_50hz_uv: float, display_50hz_uv: float) -> float:
    raw = abs(float(raw_50hz_uv))
    display = abs(float(display_50hz_uv))
    if not np.isfinite(raw) or not np.isfinite(display):
        raise ValueError("mains amplitudes must be finite")
    epsilon = np.finfo(np.float64).eps
    return float(20.0 * np.log10(max(raw, epsilon) / max(display, epsilon)))


class SignalQualityAnalyzer:
    def __init__(self, window_samples: int = 1000, refresh_interval_s: float = 0.5) -> None:
        if int(window_samples) <= 1 or float(refresh_interval_s) <= 0:
            raise ValueError("quality window and refresh interval must be positive")
        self.window_samples = int(window_samples)
        self.refresh_interval_s = float(refresh_interval_s)
        self.reset("startup")

    @property
    def sample_count(self) -> int:
        return int(self._codes.shape[0])

    def reset(self, reason: str = "manual") -> None:
        self._codes = np.empty((0, CHANNELS), dtype=np.int32)
        self._uv = np.empty((0, CHANNELS), dtype=np.float64)
        self._stream_id: int | None = None
        self._config_id: int | None = None
        self._sample_rate_hz: int | None = None
        self._next_seq: int | None = None
        self._last_snapshot_time = 0.0
        self._cached_snapshot: SignalQualitySnapshot | None = None
        self.last_reset_reason = reason
        self.error: str | None = None
        self.bypassed = False

    def ingest_batch(
        self,
        batch: EegBatch,
        sample_rate_hz: int,
        vref_v: float = 4.5,
        pga_gain: float = 12.0,
    ) -> None:
        try:
            rate = int(sample_rate_hz)
            codes = np.asarray(batch.samples, dtype=np.int32)
            if rate <= 0 or codes.ndim != 2 or codes.shape[1] != CHANNELS or codes.shape[0] == 0:
                raise ValueError("invalid rate or EEG sample shape")
            uv = codes_to_uv(codes, float(vref_v), float(pga_gain))
            if not np.all(np.isfinite(uv)):
                raise ValueError("EEG quality input contains NaN or Inf")
        except (TypeError, ValueError) as error:
            self.reset("invalid-input")
            self.error = str(error)
            return

        if int(batch.config_id) != CONFIG_NORMAL:
            self.reset("config")
            self.bypassed = True
            return

        stream_id = int(batch.stream_id) & 0xFFFFFFFF
        config_id = int(batch.config_id)
        start_seq = int(batch.start_seq) & 0xFFFFFFFF
        boundary_reason: str | None = None
        if self._stream_id is not None:
            if stream_id != self._stream_id:
                boundary_reason = "stream"
            elif config_id != self._config_id:
                boundary_reason = "config"
            elif rate != self._sample_rate_hz:
                boundary_reason = "sample-rate"
            elif start_seq != self._next_seq:
                boundary_reason = "sequence"
        if boundary_reason is not None:
            self.reset(boundary_reason)

        self._stream_id = stream_id
        self._config_id = config_id
        self._sample_rate_hz = rate
        self._next_seq = (start_seq + codes.shape[0]) & 0xFFFFFFFF
        self._codes = np.vstack((self._codes, codes))[-self.window_samples :]
        self._uv = np.vstack((self._uv, uv))[-self.window_samples :]
        self.bypassed = False

    @staticmethod
    def _grade(
        dc_uv: float,
        rms_uv: float,
        peak_to_peak_uv: float,
        mains_50hz_uv: float,
        clipped_samples: int,
    ) -> tuple[QualityGrade, tuple[str, ...]]:
        bad: list[str] = []
        warn: list[str] = []
        metrics = (
            (abs(dc_uv), 50_000.0, 100_000.0, "DC偏置"),
            (rms_uv, 100.0, 500.0, "RMS过大"),
            (peak_to_peak_uv, 500.0, 2_000.0, "峰峰值过大"),
            (mains_50hz_uv, 10.0, 50.0, "50Hz过大"),
        )
        for value, good_limit, warn_limit, reason in metrics:
            if value > warn_limit:
                bad.append(reason)
            elif value > good_limit:
                warn.append(reason)
        if clipped_samples:
            bad.append("削顶")
        if bad:
            return QualityGrade.BAD, tuple(bad + warn)
        if warn:
            return QualityGrade.WARN, tuple(warn)
        return QualityGrade.GOOD, ()

    def snapshot(self, *, force: bool = False, now: float | None = None) -> SignalQualitySnapshot:
        current_time = time.monotonic() if now is None else float(now)
        if (
            not force
            and self._cached_snapshot is not None
            and current_time - self._last_snapshot_time < self.refresh_interval_s
        ):
            return self._cached_snapshot
        if self.bypassed:
            snapshot = SignalQualitySnapshot(
                False, 0, (), QualityGrade.COLLECTING, bypassed=True
            )
        elif self.error is not None:
            snapshot = SignalQualitySnapshot(
                False, self.sample_count, (), QualityGrade.COLLECTING, error=self.error
            )
        elif self.sample_count < self.window_samples:
            snapshot = SignalQualitySnapshot(
                False, self.sample_count, (), QualityGrade.COLLECTING
            )
        else:
            uv = self._uv[-self.window_samples :]
            codes = self._codes[-self.window_samples :]
            dc = np.mean(uv, axis=0)
            centered = uv - dc[None, :]
            rms = np.sqrt(np.mean(centered * centered, axis=0))
            peak_to_peak = np.ptp(centered, axis=0)
            peak_fs = 100.0 * np.max(np.abs(codes.astype(np.int64)), axis=0) / FS_CODE
            clipped = np.sum(np.abs(codes.astype(np.int64)) >= 0.98 * FS_CODE, axis=0)
            mains = _mains_amplitude(centered, int(self._sample_rate_hz))
            channels: list[ChannelQualityMetrics] = []
            rank = {QualityGrade.GOOD: 0, QualityGrade.WARN: 1, QualityGrade.BAD: 2}
            for channel in range(CHANNELS):
                grade, reasons = self._grade(
                    float(dc[channel]), float(rms[channel]), float(peak_to_peak[channel]),
                    float(mains[channel]), int(clipped[channel]),
                )
                channels.append(ChannelQualityMetrics(
                    dc_mv=float(dc[channel]) / 1000.0,
                    peak_fs_percent=float(peak_fs[channel]),
                    clipped_samples=int(clipped[channel]),
                    rms_uv=float(rms[channel]),
                    peak_to_peak_uv=float(peak_to_peak[channel]),
                    mains_50hz_uv=float(mains[channel]),
                    grade=grade,
                    reasons=reasons,
                ))
            overall = max((item.grade for item in channels), key=rank.__getitem__)
            snapshot = SignalQualitySnapshot(True, self.sample_count, tuple(channels), overall)
        self._cached_snapshot = snapshot
        self._last_snapshot_time = current_time
        return snapshot


class DisplaySignalQualityAnalyzer:
    """Quality metrics for the actual reference/filter display copy."""

    def __init__(self, window_samples: int = 1000, refresh_interval_s: float = 0.5) -> None:
        if int(window_samples) <= 1 or float(refresh_interval_s) <= 0:
            raise ValueError("quality window and refresh interval must be positive")
        self.window_samples = int(window_samples)
        self.refresh_interval_s = float(refresh_interval_s)
        self.reset("startup")

    @property
    def sample_count(self) -> int:
        return int(self._uv.shape[0])

    def reset(self, reason: str = "manual") -> None:
        self._uv = np.empty((0, CHANNELS), dtype=np.float64)
        self._stream_id: int | None = None
        self._config_id: int | None = None
        self._sample_rate_hz: int | None = None
        self._next_seq: int | None = None
        self._last_snapshot_time = 0.0
        self._cached_snapshot: DisplaySignalQualitySnapshot | None = None
        self.last_reset_reason = reason
        self.error: str | None = None
        self.bypassed = False

    def ingest_batch(
        self,
        batch: EegBatch,
        sample_rate_hz: int,
        display_values: np.ndarray,
    ) -> None:
        try:
            rate = int(sample_rate_hz)
            values = np.asarray(display_values, dtype=np.float64)
            if (
                rate <= 0
                or values.ndim != 2
                or values.shape[1] != CHANNELS
                or values.shape[0] != len(batch.samples)
                or values.shape[0] == 0
            ):
                raise ValueError("invalid rate or display sample shape")
            if not np.all(np.isfinite(values)):
                raise ValueError("display quality input contains NaN or Inf")
        except (TypeError, ValueError) as error:
            self.reset("invalid-input")
            self.error = str(error)
            return

        if int(batch.config_id) != CONFIG_NORMAL:
            self.reset("config")
            self.bypassed = True
            return

        stream_id = int(batch.stream_id) & 0xFFFFFFFF
        config_id = int(batch.config_id)
        start_seq = int(batch.start_seq) & 0xFFFFFFFF
        boundary_reason: str | None = None
        if self._stream_id is not None:
            if stream_id != self._stream_id:
                boundary_reason = "stream"
            elif config_id != self._config_id:
                boundary_reason = "config"
            elif rate != self._sample_rate_hz:
                boundary_reason = "sample-rate"
            elif start_seq != self._next_seq:
                boundary_reason = "sequence"
        if boundary_reason is not None:
            self.reset(boundary_reason)

        self._stream_id = stream_id
        self._config_id = config_id
        self._sample_rate_hz = rate
        self._next_seq = (start_seq + values.shape[0]) & 0xFFFFFFFF
        self._uv = np.vstack((self._uv, values))[-self.window_samples :]
        self.bypassed = False

    @staticmethod
    def _mains_grade(value: float) -> QualityGrade:
        if value > 50.0:
            return QualityGrade.BAD
        if value > 10.0:
            return QualityGrade.WARN
        return QualityGrade.GOOD

    def snapshot(
        self, *, force: bool = False, now: float | None = None
    ) -> DisplaySignalQualitySnapshot:
        current_time = time.monotonic() if now is None else float(now)
        if (
            not force
            and self._cached_snapshot is not None
            and current_time - self._last_snapshot_time < self.refresh_interval_s
        ):
            return self._cached_snapshot
        if self.bypassed:
            snapshot = DisplaySignalQualitySnapshot(
                False, 0, (), QualityGrade.COLLECTING, bypassed=True
            )
        elif self.error is not None:
            snapshot = DisplaySignalQualitySnapshot(
                False, self.sample_count, (), QualityGrade.COLLECTING, error=self.error
            )
        elif self.sample_count < self.window_samples:
            snapshot = DisplaySignalQualitySnapshot(
                False, self.sample_count, (), QualityGrade.COLLECTING
            )
        else:
            values = self._uv[-self.window_samples :]
            centered = values - np.mean(values, axis=0, keepdims=True)
            rms = np.sqrt(np.mean(centered * centered, axis=0))
            peak_to_peak = np.ptp(centered, axis=0)
            mains = _mains_amplitude(centered, int(self._sample_rate_hz))
            channels = tuple(
                DisplayChannelQualityMetrics(
                    rms_uv=float(rms[channel]),
                    peak_to_peak_uv=float(peak_to_peak[channel]),
                    mains_50hz_uv=float(mains[channel]),
                    grade=self._mains_grade(float(mains[channel])),
                )
                for channel in range(CHANNELS)
            )
            rank = {
                QualityGrade.GOOD: 0,
                QualityGrade.WARN: 1,
                QualityGrade.BAD: 2,
            }
            overall = max((item.grade for item in channels), key=rank.__getitem__)
            snapshot = DisplaySignalQualitySnapshot(
                True, self.sample_count, channels, overall
            )
        self._cached_snapshot = snapshot
        self._last_snapshot_time = current_time
        return snapshot
