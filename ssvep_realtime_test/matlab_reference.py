"""MATLAB reference processing: FIR resampling and window-local filtfilt.

This is an explicit numerical reference path, not a claim of bitwise equality
across MATLAB releases. Streaming FIR outputs are aligned to input sample zero;
we wait for the future half of the symmetric FIR instead of padding live data.
"""
import numpy as np
from scipy import signal


class FirResampler:
    def __init__(self, sampling_rate, output_rate=250, channels=8):
        if sampling_rate not in (250, 500, 1000, 2000):
            raise ValueError("不支持的EEG采样率")
        self.factor = sampling_rate // output_rate
        self.delay = 10 * self.factor if self.factor > 1 else 0
        self.taps = signal.firwin(20 * self.factor + 1, 1 / self.factor, window=("kaiser", 5.0)) if self.delay else None
        self.state = np.zeros((len(self.taps) - 1, channels)) if self.delay else None
        self.count = 0
        self.last_center = -1

    @property
    def trailing_input_samples(self):
        return self.count - 1 - self.last_center

    def apply(self, data):
        if not len(data):
            return data.copy()
        if self.taps is None:
            self.count += len(data)
            self.last_center = self.count - 1
            return data.copy()
        filtered, self.state = signal.lfilter(self.taps, [1.0], data, axis=0, zi=self.state)
        indices = np.arange(self.count, self.count + len(data))
        keep = (indices >= self.delay) & ((indices - self.delay) % self.factor == 0)
        self.count += len(data)
        if keep.any():
            self.last_center = int(indices[keep][-1] - self.delay)
        return filtered[keep]


def zero_phase_banks(data, notch=True, bands=((6., 90.), (14., 90.), (22., 90.)), fs=250):
    """Match the supplied script's order: separate notch, then bandpass passes.

    Classical MATLAB-style odd reflection: 3 * polynomial filter order.
    SOS avoids direct-form coefficient sensitivity without changing transfer functions.
    """
    clean = np.asarray(data, dtype=float)
    if notch:
        sos = signal.butter(2, (49.5, 50.5), btype="bandstop", fs=fs, output="sos")
        clean = signal.sosfiltfilt(sos, clean, axis=0, padtype="odd", padlen=12)
    result = []
    for band in bands:
        sos = signal.butter(4, band, btype="bandpass", fs=fs, output="sos")
        result.append(signal.sosfiltfilt(sos, clean, axis=0, padtype="odd", padlen=24))
    return result
