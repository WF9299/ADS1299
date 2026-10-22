# 2026-10-14 PC hotspot provisioning diagnosis

- User confirms TONG is this PC's Windows mobile hotspot, not a phone/router. Their supplied screenshot/logs are the observed outcome; avoid repeatedly asking for the same information.
- phy_version / 11ax coex / Set ps type:0 are informational initialization logs. Firmware explicitly sets WIFI_PS_NONE. No association success or failure is established by these messages.
- Read-only snapshot: WLAN connects to SEU-WLAN, IPv4 10.203.242.117/17. This is the campus uplink. GUI selected PC hotspot but entered this uplink as PC endpoint; use the actual hotspot-side address once available.
- Hidden adapter 本地连接*2 (Microsoft Wi-Fi Direct Virtual Adapter #2), index4, is Up. Its only observed IPv4 is 169.254.112.33/16, AddressState Tentative, DHCP enabled Yes. Default Get-NetAdapter misses this hidden adapter. This is an abnormal hotspot address state worth investigating, not proof of the device's exact failure reason.
- icssvc, SharedAccess, WlanSvc all Running. HNetCfg sharing-binding query failed; no confirmed sharing-source/target binding diagnosis. Do not claim the hotspot is off or the service is stopped.
- Recommend checking Windows mobile hotspot sharing source is WLAN and sharing medium Wi-Fi, cycling hotspot, then selecting its actual usable IPv4. Do not hard-code the customary 192.168.137.1 without observing it.
- set_network tests Wi-Fi/DHCP for about20 seconds; Monitor timeout25 seconds. PC endpoint does not affect initial SSID association/DHCP validation, only later TCP/discovery. Grey buttons alone indicate busy, not success.
- No business code, Windows network settings, services, device configuration or firmware changed in this diagnostic turn.
- Official references: https://docs.espressif.com/projects/esp-idf/zh_CN/v5.5/esp32c5/api-guides/wifi.html ; https://support.microsoft.com/zh-cn/windows/experience/connectivity-networking/use-your-windows-device-as-a-mobile-hotspot

## Updated user evidence

User supplied a newer ipconfig showing 本地连接*2 now has 192.168.137.1/24. This supersedes the earlier transient 169.254.112.33/Tentative snapshot; do not continue claiming the hotspot has no valid IPv4. The correct Monitor PC data endpoint for this TONG PC hotspot is currently 192.168.137.1, while campus WLAN remains 10.203.242.117. Advised changing the GUI endpoint and sending/validating via USB, without code changes or reflash. This does not yet confirm ESP association or EEG output.

## Successful device association and intentional post-validation disconnect

New user logs show successful TONG association on channel157 and DHCP: device192.168.137.150/24, gateway192.168.137.1. This supersedes earlier uncertainty about hotspot/DHCP reachability. About85ms later run->init/flush is consistent with the explicit set_network success branch: cfg.wifi_mode=0 and native_wifi_enable(false), then save configuration. Explain that provisioning validation and activating Wi-Fi data mode are separate actions; user must keep USB connected, select Wi-Fi(TCP), and click Apply data mode even if the combo already displays Wi-Fi. PC endpoint remains192.168.137.1, never the device .150.

Existing limitation: p300_stream_set_mode returns ESP_ERR_INVALID_STATE without s_stream_task, and main never starts that task if ADS initialization/acquisition startup fails. Thus previously observed ads_running=false may cause mode_busy_or_unavailable when applying Wi-Fi; treat that as an ADS/stream-start issue, not a hotspot/password failure. No firmware change in this diagnostic turn.

## Provisioning result UX fix

Repeated user confusion after successful validation led to a PC-only fix: network_verified results explicitly say Wi-Fi was intentionally stopped, and a button below the operation result applies existing Wi-Fi mode only on a user click. No automatic transport switch. Successful mode activation reports waiting for IP/sync/EEG, not a proven data connection. When known ads_running=false accompanies mode_busy_or_unavailable, show an ADS startup explanation. Three new regressions; full78 PC tests pass. Restart Monitor to load it, reconnect USB and enable the already saved network without re-provisioning. No firmware change or real-device mode change in this turn.
