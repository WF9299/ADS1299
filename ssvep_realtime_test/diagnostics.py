"""Quality and local SNR measured on raw microvolts, before display filtering."""
import numpy as np
from scipy import signal


def summarize(raw, sampling_rate, fullscale, candidates, selected):
    frequencies, psd = signal.periodogram(np.nan_to_num(raw, nan=0, posinf=0, neginf=0), fs=sampling_rate,
                                          window="hann", detrend="constant", axis=0, scaling="density")
    keep = frequencies <= 100
    frequencies, psd = frequencies[keep], psd[keep]
    df = frequencies[1] - frequencies[0]
    snrs = []
    for frequency in candidates:
        distance = np.abs(frequencies - frequency)
        peak = distance <= max(df * 1.5, .35)
        noise = (distance >= max(df * 3, 1.0)) & (distance <= 3.0)
        noise &= np.abs(frequencies - 50) > 1
        if not noise.any():
            noise = (distance >= 1) & (distance <= 5)
        signal_power = psd[peak].mean(axis=0)
        noise_power = psd[noise].mean(axis=0)
        snrs.append(10 * np.log10(np.maximum(signal_power, 1e-24) / np.maximum(noise_power, 1e-24)))
    rows, used, excluded = [], [], {}
    for index in range(8):
        values = raw[:, index]
        channel = index + 1
        finite = bool(np.isfinite(values).all())
        reason = None
        row = {"channel": channel, "finite": finite}
        if finite:
            near = float(np.mean(np.abs(values) >= .98 * fullscale))
            clipped = float(np.mean(np.abs(values) >= .9999 * fullscale))
            row.update(dc_uv=float(values.mean()), rms_uv=float(values.std()), pp_uv=float(np.ptp(values)),
                       peak_fs_percent=float(np.max(np.abs(values)) / fullscale * 100),
                       near_rail_fraction=near, clipped_fraction=clipped,
                       mains_50_uv=float(np.sqrt(np.sum(psd[np.abs(frequencies - 50) <= max(df, .5), index]) * df)))
            if row["pp_uv"] <= 1e-9:
                reason = "恒定/无变化"
            elif near >= .05 or clipped >= .001:
                reason = "满量程削顶"
        else:
            reason = "非有限值"
        row["reason"] = reason
        rows.append(row)
        if channel in selected:
            if reason:
                excluded[str(channel)] = reason
            else:
                used.append(channel)
    return {"channels": rows, "frequencies": frequencies.tolist(), "psd_uv2_hz": psd.tolist(),
            "snr_db": np.asarray(snrs).tolist(), "fullscale_uv": fullscale}, used, excluded
