"""Stateful streaming filters and rank-aware CCA; input is raw microvolts."""
import time
import numpy as np
from scipy import signal
from .diagnostics import summarize
from .matlab_reference import FirResampler, zero_phase_banks

OUTPUT_RATE = 250
BANDS = ((6.0, 90.0), (14.0, 90.0), (22.0, 90.0))


def basis(matrix):
    matrix = np.asarray(matrix, dtype=float)
    matrix = matrix - matrix.mean(axis=0)
    u, s, _ = np.linalg.svd(matrix, full_matrices=False)
    if not len(s) or s[0] <= 1e-12:
        return u[:, :0]
    return u[:, s > max(s[0] * 1e-8, 1e-12)]


def canonical_correlation(x_basis, y_basis):
    if x_basis.shape[1] == 0 or y_basis.shape[1] == 0:
        return 0.0
    singular = np.linalg.svd(x_basis.T @ y_basis, compute_uv=False)
    return float(np.clip(singular[0], 0, 1))


class StatefulSOS:
    def __init__(self, sos):
        self.sos = sos
        self.state = None

    def apply(self, data):
        if self.state is None:
            self.state = signal.sosfilt_zi(self.sos)[:, :, None] * data[0][None, None, :]
        result, self.state = signal.sosfilt(self.sos, data, axis=0, zi=self.state)
        return result


class Preprocessor:
    def __init__(self, sampling_rate, notch, bands=BANDS):
        if sampling_rate not in (250, 500, 1000, 2000):
            raise ValueError("支持输入 250/500/1000/2000 SPS，降采样到 250 SPS")
        self.factor = sampling_rate // OUTPUT_RATE
        self.count = 0
        self.antialias = StatefulSOS(signal.cheby1(8, 0.05, 100, fs=sampling_rate, output="sos")) if self.factor > 1 else None
        b, a = signal.iirnotch(50, 35, fs=OUTPUT_RATE)
        self.notch = StatefulSOS(signal.tf2sos(b, a)) if notch else None
        self.banks = [StatefulSOS(signal.butter(4, band, btype="bandpass", fs=OUTPUT_RATE, output="sos")) for band in bands]

    def apply(self, data):
        filtered = self.antialias.apply(data) if self.antialias else data.copy()
        offset = (-self.count) % self.factor
        self.count += len(data)
        reduced = filtered[offset::self.factor]
        if not len(reduced):
            return [np.empty((0, 8)) for _ in BANDS]
        if self.notch:
            reduced = self.notch.apply(reduced)
        return [bank.apply(reduced) for bank in self.banks]


class Analyzer:
    def __init__(self, config):
        self.config = config.validate()
        self.references = {}
        self.fullscale = config.fullscale_uv
        self.range_source = "配置量程（未收到 Monitor 元信息）"
        self.reset()

    def reset(self):
        self.preprocessor = None
        self.sampling_rate = None
        self.filtered = [np.empty((0, 8)) for _ in BANDS]
        self.raw = np.empty((0, 8))
        self.reduced = np.empty((0, 8))
        self.total_samples = 0

    def feed(self, data, sampling_rate):
        if sampling_rate != self.sampling_rate:
            self.reset()
            self.sampling_rate = sampling_rate
            self.preprocessor = (FirResampler(sampling_rate) if self.config.analysis_profile == "matlab" else
                                 Preprocessor(sampling_rate, self.config.notch, self.config.bands))
        data = np.asarray(data, dtype=float)
        if data.ndim != 2 or data.shape[1] != 8:
            raise ValueError("EEG 必须为 N×8")
        # Preserve raw non-finite/rail evidence for channel exclusion. A bad channel
        # must not contaminate the independent filters of other channels.
        safe = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        self.total_samples += len(data)
        count = round(self.config.window_seconds * OUTPUT_RATE)
        if self.config.analysis_profile == "matlab":
            reduced = self.preprocessor.apply(safe)
            self.reduced = np.concatenate((self.reduced, reduced))[-count:]
            extra = self.preprocessor.delay + self.preprocessor.factor
        else:
            banks = self.preprocessor.apply(safe)
            for i, new in enumerate(banks):
                self.filtered[i] = np.concatenate((self.filtered[i], new))[-count:]
            extra = 0
        self.raw = np.concatenate((self.raw, data))[-(round(self.config.window_seconds * sampling_rate) + extra):]

    def reference(self, frequency, count, band):
        key = (frequency, count, band)
        if key not in self.references:
            t = (np.arange(count) + (1 if self.config.analysis_profile == "matlab" else 0)) / OUTPUT_RATE
            harmonics = [frequency * k for k in range(1, self.config.harmonics + 1)
                         if (self.config.analysis_profile == "matlab" and band == 0) or
                         self.config.bands[band][0] <= frequency * k <= self.config.bands[band][1]]
            refs = [fun(2 * np.pi * f * t) for f in harmonics for fun in (np.sin, np.cos)]
            self.references[key] = basis(np.column_stack(refs)) if refs else np.empty((count, 0))
        return self.references[key]

    def snapshot(self):
        if self.config.analysis_profile == "matlab":
            lag = self.preprocessor.trailing_input_samples
            end = len(self.raw) - lag + self.preprocessor.factor - 1
            raw = self.raw[max(0, end - round(self.config.window_seconds * self.sampling_rate)):end]
            banks = zero_phase_banks(self.reduced, self.config.notch, self.config.bands)
        else:
            raw, banks = self.raw, self.filtered
        diagnostics, used, excluded = summarize(raw, self.sampling_rate, self.fullscale,
                                               self.config.candidates, self.config.channels)
        return {"banks": [data.copy() for data in banks], "diagnostics": diagnostics,
                "used": used, "excluded": excluded}

    def result(self, channels=None, snapshot=None):
        start = time.perf_counter()
        required = round(self.config.window_seconds * OUTPUT_RATE)
        available = len(self.reduced) if self.config.analysis_profile == "matlab" else len(self.filtered[0])
        if snapshot is None and available < required:
            return {"valid": False, "status": f"积累 EEG：{available / OUTPUT_RATE:.1f}/{self.config.window_seconds:g} 秒"}
        snapshot = self.snapshot() if snapshot is None else snapshot
        diagnostics, excluded = snapshot["diagnostics"], snapshot["excluded"]
        used = [c for c in snapshot["used"] if channels is None or c in channels]
        extra = {"diagnostics": diagnostics, "range_source": self.range_source}
        if not used:
            return {"valid": False, "status": "无有效通道", "used_channels": [], "excluded": excluded, **extra}
        indices = np.array(used) - 1
        rhos = np.zeros((len(BANDS), len(self.config.candidates)))
        for b, data in enumerate(snapshot["banks"]):
            xb = basis(data[:, indices])
            if b == 0 and xb.shape[1] == 0:
                return {"valid": False, "status": "分析信号有效秩为 0", "used_channels": used, "excluded": excluded, **extra}
            for j, frequency in enumerate(self.config.candidates):
                rhos[b, j] = canonical_correlation(xb, self.reference(frequency, required, b))
        weights = np.arange(1, len(BANDS) + 1, dtype=float) ** -1.25 + 0.25
        scores = weights @ rhos ** 2
        eligible = [i for i, f in enumerate(self.config.candidates) if f not in self.config.excluded_frequencies]
        ci = max(eligible, key=lambda i: rhos[0, i])
        fi = max(eligible, key=lambda i: scores[i])
        target = self.config.candidates.index(self.config.frequency)
        other = [i for i in eligible if i != target]
        candidate_snrs = np.median(np.asarray(diagnostics["snr_db"])[:, indices], axis=1)
        return {"valid": True, "status": "实时分析", "analysis_profile": self.config.analysis_profile,
                "used_channels": used, "excluded": excluded,
                "cca_frequency": self.config.candidates[ci], "cca_rho": float(rhos[0, ci]),
                "target_rho": float(rhos[0, target]), "fbcca_frequency": self.config.candidates[fi],
                "fbcca_score": float(scores[fi]), "fbcca_rhos": rhos[:, fi].tolist(),
                "target_score": float(scores[target]), "target_rhos": rhos[:, target].tolist(),
                "candidates": list(self.config.candidates), "cca_rhos": rhos[0].tolist(),
                "eligible_candidates": [self.config.candidates[i] for i in eligible],
                "excluded_frequencies": list(self.config.excluded_frequencies),
                "target_excluded": self.config.frequency in self.config.excluded_frequencies,
                "cca_raw_frequency": self.config.candidates[int(rhos[0].argmax())],
                "fbcca_raw_frequency": self.config.candidates[int(scores.argmax())],
                "fbcca_scores": scores.tolist(), "window_seconds": self.config.window_seconds,
                "target_snr_db": float(candidate_snrs[target]), "candidate_snr_db": candidate_snrs.tolist(),
                "target_margin": float(scores[target] - max(scores[other])),
                "cca_margin": float(rhos[0, target] - max(rhos[0, other])), **extra,
                "cca_winner_margin": float(np.sort(rhos[0, eligible])[-1] - np.sort(rhos[0, eligible])[-2]),
                "analysis_ms": (time.perf_counter() - start) * 1000}
