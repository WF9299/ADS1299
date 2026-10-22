# C5 2.2.0 runtime BIAS power control

User approved the detailed design on 2026-10-22. Baseline snapshots: C5 429c50c, SSVEP ebf6741. This feature is independent of SSVEP signal processing.

Normal effective BIAS mask zero now means buffer OFF (CONFIG3=0xE8, BIAS_SENSP/N=0), nonzero means ON (0xEC plus selected P-input mask). ADC reference/SRB1/PGA/normal MUX remain configured. Runtime driver verifies all three registers. Pause/resume, self-test exit, impedance restoration and acquisition restart use the current effective mask, including zero. Boot still initializes the normal mask from P300_ADS_CHANNEL_MASK; no NVS persistence.

Protocol keeps v1 sizes and legacy QUERY/SET; DISABLE=2 requires zero mask, POWER_CONTROL flag=0x08 announces capability. VERIFIED zero means confirmed off; unverified zero means unknown. Python rejects a verified-off reply without the capability flag. Existing retries/cache and timeout-then-query apply to DISABLE. USB uses single SerialSession owner, Wi-Fi uses existing UDP.

Monitor impedance tab adds a draft enable checkbox, separately verified actual state, preserved selected channels while off, old-firmware mask-only fallback, and existing recording/HTTP/diagnostic interlocks. A successful switch issues a new stream id, TCP reconnect/config or USB CONFIG, ensuring raw segment BIAS metadata is current. batch_to_record and HTTP attach bias/bias_mask/bias_verified without altering EEG samples. Fault rollback preserves prior OFF as well as ON; rollback failure leaves unknown/paused and latches recovery until reboot.

Tests: 92 Python tests passed13.205s, including9 new protocol/USB/UDP/Qt and host-compiled unchanged production function fault injections. Extended C protocol/state-machine tests pass in actual project. ESP-IDF6.0.2 build2.2.0 passed: app1,050,880bytes, SHA256 f2cb76802a279d75709eec7214ee9c352233447be229674bdc7723b83a81be4c, app partition67%free. Source patch and backups verified; static ON/OFF/legacy previews checked. Build uses existing IDF paths recorded in the earlier dual-transport memory.

Release artifacts at D:/Project/260720SSVEP_DISPLAY_JD/outputs/ssvep_realtime/bias-power-2.2.0 (zip, three images/offsets/manifest, previews, source-patch backups). Local build at firmware/build. No flashing, COM takeover, visible stimulus, push to remote Git or actual BIASOUT validation. User must flash2.2.0 and restart Monitor, stop HTTP/recording, toggle and Apply, wait actual OFF/ON VERIFIED. Runtime reset returns ON.
