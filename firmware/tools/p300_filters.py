"""Stateful display/FFT filters for the P300 desktop monitor."""

from __future__ import annotations

from enum import Enum

import numpy as np

try:
    from scipy import signal as scipy_signal
except ImportError:  # Raw monitor mode remains usable without the optional dependency.
    scipy_signal = None


SCIPY_AVAILABLE = scipy_signal is not None
NORMAL_CONFIG_ID = 1
MAINS_STOP_LOW_HZ = 48.0
MAINS_STOP_HIGH_HZ = 52.0
MAINS_STOP_ORDER = 4
P300_LOW_HZ = 0.5
P300_HIGH_HZ = 30.0
P300_BANDPASS_ORDER = 4


class FilterMode(str, Enum):
    RAW = "raw"
    NOTCH_50 = "notch_50"
    P300 = "p300"


FILTER_MODE_LABELS = {
    FilterMode.RAW: "原始（仅波形去直流）",
    FilterMode.NOTCH_50: "48–52 Hz工频带阻",
    FilterMode.P300: "P300 0.5–30 Hz + 工频抑制",
}


class FilterUnavailableError(RuntimeError):
    """Raised when a requested filtered mode cannot be constructed."""


class RealtimeEegFilter:
    """Causal multichannel SOS filter with stream-continuity tracking."""

    def __init__(self, mode: FilterMode | str = FilterMode.RAW, channels: int = 8) -> None:
        self.channels = int(channels)
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        self._mode = FilterMode(mode)
        self._sos: np.ndarray | None = None
        self._zi: np.ndarray | None = None
        self._sample_rate_hz: float | None = None
        self._stream_id: int | None = None
        self._config_id: int | None = None
        self._expected_seq: int | None = None
        self.reset_count = 0
        self.last_reset_reason: str | None = None

    @property
    def mode(self) -> FilterMode:
        return self._mode

    @property
    def active(self) -> bool:
        return self._zi is not None

    def set_mode(self, mode: FilterMode | str) -> None:
        selected = FilterMode(mode)
        if selected == self._mode:
            return
        self._mode = selected
        self.reset("mode")

    def reset(self, reason: str = "manual") -> None:
        self._sos = None
        self._zi = None
        self._sample_rate_hz = None
        self._stream_id = None
        self._config_id = None
        self._expected_seq = None
        self.reset_count += 1
        self.last_reset_reason = str(reason)

    @staticmethod
    def _design_sos(mode: FilterMode, sample_rate_hz: float) -> np.ndarray:
        if scipy_signal is None:
            raise FilterUnavailableError(
                "SciPy不可用；请安装requirements-p300-tools.txt后启用显示滤波"
            )
        if not np.isfinite(sample_rate_hz) or sample_rate_hz <= 2.0 * MAINS_STOP_HIGH_HZ:
            raise ValueError("显示滤波要求sampling_rate大于104 Hz")

        mains_stop_sos = scipy_signal.butter(
            MAINS_STOP_ORDER,
            (MAINS_STOP_LOW_HZ, MAINS_STOP_HIGH_HZ),
            btype="bandstop",
            fs=sample_rate_hz,
            output="sos",
        )
        if mode == FilterMode.NOTCH_50:
            return np.asarray(mains_stop_sos, dtype=np.float64)
        if mode == FilterMode.P300:
            bandpass_sos = scipy_signal.butter(
                P300_BANDPASS_ORDER,
                (P300_LOW_HZ, P300_HIGH_HZ),
                btype="bandpass",
                fs=sample_rate_hz,
                output="sos",
            )
            return np.vstack((bandpass_sos, mains_stop_sos)).astype(np.float64, copy=False)
        raise ValueError(f"filter mode {mode!s} does not use SOS coefficients")

    def _reset_for_boundary(
        self,
        sample_rate_hz: float,
        stream_id: int,
        config_id: int,
        start_seq: int,
    ) -> None:
        reason = None
        if self._sample_rate_hz is not None and sample_rate_hz != self._sample_rate_hz:
            reason = "sample-rate"
        elif self._stream_id is not None and stream_id != self._stream_id:
            reason = "stream"
        elif self._config_id is not None and config_id != self._config_id:
            reason = "config"
        elif self._expected_seq is not None and start_seq != self._expected_seq:
            reason = "sequence"
        if reason is not None:
            self.reset(reason)

    def process(
        self,
        values: np.ndarray,
        *,
        sample_rate_hz: float,
        stream_id: int,
        config_id: int,
        start_seq: int,
    ) -> np.ndarray:
        data = np.asarray(values, dtype=np.float64)
        if data.ndim != 2 or data.shape[1] != self.channels:
            raise ValueError(f"expected Nx{self.channels} display samples")
        if not np.all(np.isfinite(data)):
            raise ValueError("display samples contain NaN or Inf")
        if self._mode == FilterMode.RAW:
            return data.copy()
        if config_id != NORMAL_CONFIG_ID:
            if self.active or self._stream_id is not None:
                self.reset("config")
            return data.copy()
        if scipy_signal is None:
            raise FilterUnavailableError(
                "SciPy不可用；请安装requirements-p300-tools.txt后启用显示滤波"
            )

        rate = float(sample_rate_hz)
        stream = int(stream_id) & 0xFFFFFFFF
        config = int(config_id)
        sequence = int(start_seq) & 0xFFFFFFFF
        self._reset_for_boundary(rate, stream, config, sequence)
        if self._sos is None:
            self._sos = self._design_sos(self._mode, rate)
        if not len(data):
            return data.copy()
        if self._zi is None:
            base_zi = scipy_signal.sosfilt_zi(self._sos)
            self._zi = base_zi[:, :, None] * data[0][None, None, :]

        filtered, self._zi = scipy_signal.sosfilt(
            self._sos,
            data,
            axis=0,
            zi=self._zi,
        )
        self._sample_rate_hz = rate
        self._stream_id = stream
        self._config_id = config
        self._expected_seq = (sequence + len(data)) & 0xFFFFFFFF
        return np.asarray(filtered, dtype=np.float64)
