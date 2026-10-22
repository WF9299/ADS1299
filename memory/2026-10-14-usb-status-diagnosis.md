# 2026-10-14 USB connection status diagnosis

- User confirms the new firmware is flashed. Screenshot showed COM7 CP210x and a default IDF-monitor occupancy reminder, with no visible specific error.
- Read-only handle inspection identified the active p300_monitor.py as the COM7 owner. Do not infer another IDF monitor from the old label. User then exited Monitor normally.
- Independent real-board handshake at 115200 followed by 921600 and status query succeeded in 0.32 seconds, zero parser errors. Device P300_3844BE373428: mode=serial, paired=true, ads_running=false, time_ready=false, IP 0.0.0.0. Test session was closed; no pairing, network settings, transport mode, or firmware changes were made to the board.
- GUI issue: early USB status events may be filtered before ConnectionPanel adopts the new session. Completion previously did not replay status. Without ADS acquisition, serial clock updates do not provide a later status, leaving the default reminder visible.
- Fix: neutral initial text; USB connect/disconnect progress and errors beside button; replay latest session status at pair completion, with interrupted-session handling. Three new regression tests reproduce the failure before the fix; all 75 PC tests pass after it.
- User should restart the Monitor to load the fix; no reflash is needed. ADS startup remains unverified/failed according to the returned running flag and needs startup logs. A working USB management link does not imply EEG acquisition or sustained throughput has passed.
- Windows handle-query API reference consulted: https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntqueryobject . System handle inspection is diagnostic only, not added to the product.
