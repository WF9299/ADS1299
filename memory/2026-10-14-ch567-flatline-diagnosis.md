# 2026-10-14 CH5/6/7 flatline in USB stream

- User screenshot: COM7:921600 serial mode, ADS running, synchronized, config_id1; sequence loss0, frame errors0, about953SPS at that instant. CH1-4 and8 show large repetitive waveforms, CH5-7 show lines near zero with about+-5uV axis. This supersedes the earlier transient ADS-not-running observation; do not keep claiming ADS is currently stopped.
- Current private app_config selected numeric field only: P300_ADS_CHANNEL_MASK=0xFF. ADS normal CHnSET is0x50 for all enabled channels. ADS frame read, acquisition copy, batch packing, USB/TCP decode and plot each preserve8 channels; no transport-specific CH5-7 suppression found.
- Synthetic distinct positive/negative24-bit values on all8 channels survived USB envelope + fragmented parser + EEG decode unchanged. This checks software path, not current board values.
- Plot always subtracts a rolling mean, including raw display mode. A constant rail can therefore appear as zero. Synthetic check using actual display and quality functions: positive rail8388607 -> raw DC+375mV, peak100%FS, clipped1000/1000, zero displayed; negative rail equivalent -375mV; zero code -> DC0, FS0, clipped0 and zero displayed.
- Asked user whetherCH5/6/7 are attached to electrodes/signal or floating, and for quality-tab DC, peak%FS and clipped count. No answer yet at time of record. Cannot conclude saturation, disabled channel, wiring fault or hardware damage from screenshot alone.
- Do not seize COM7 while Monitor owns it. No code or device changes this turn. CRC/sequence integrity does not verify analog signal quality. Need raw metrics before proposing hardware repair.
- TI reference (24-bit rails and fullscale): https://www.ti.com/lit/ds/symlink/ads1299.pdf section9.5.1.
