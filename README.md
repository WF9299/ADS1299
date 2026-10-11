# ADS1299 ESP32-C5 TONG V1

This repository contains the ESP32-C5-WROOM-1U-N8R8 port of the ADS1299 P300 acquisition firmware in `firmware/`, together with the Python PC monitor and diagnostic tools. It uses ESP-IDF 6.0.2, 8-channel acquisition at 1000 SPS, native Wi-Fi, TCP EEG streaming and UDP marker/control messages.

Start with `firmware/README.md` for build, network and first-power-on instructions. The approved port design is recorded in `docs/superpowers/specs/2026-09-24-esp32c5-port-design.md`.

## Repository and setup

GitLab: [ads1299_esp32c5_tong_v1](http://10.201.1.182:1081/zhaoboyuan/ads1299_esp32c5_tong_v1).

Open `firmware/` as the ESP-IDF project, not the outer repository directory. On a fresh clone, copy `firmware/components/app_config/include/app_config.example.h` to `app_config.h` in the same directory, then set the Wi-Fi SSID/password and the PC's current IP before building. The example's TCP IP is only a placeholder, not a detected server address.

`app_config.h` remains local and is ignored by Git; do not force-add it. Keep shared non-secret configuration changes reflected in `app_config.example.h`. Build output, downloaded managed components, `sdkconfig`, editor configuration and acquisition exports are not committed. `sdkconfig.defaults` and `dependencies.lock` are retained for reproducible configuration.

## Hardware validation status

As of 2026-10-10, the R8 board reports a stable ADS1299 ID of `0x3e`, passes the programmed register readback test, and produces DRDY near 1000 SPS. A cold joint at the ADS side of R25 was identified and repaired. Native Wi-Fi has obtained an IP address.

The TCP target must follow the PC's address when changing hotspots. End-to-end Monitor reception, strictly loss-free acquisition and analog EEG quality still require their own validation; the successful register/DRDY checks alone do not prove them. Startup logs showed initial sample losses and some misaligned frames, with later windows reporting `OK`.

## Sources

- Firmware baseline: `D:\Project\260720SSVEP_DISPLAY_JD\ads1299_p300_firmware`
- Latest PCB/schematic archive reviewed: `C:\Users\TONG\Desktop\ProPrj_ADS1299_TONG_V2_R8_RELEASE_2026-09-21_2026-09-24.epro`
- Schematic PDF supplied with that release: `C:\Users\TONG\Desktop\PDF_ADS1299_TONG_V2_R8_RELEASE_2026-09-21_2026-09-24.zip`
- Earlier R8 reconstruction baseline: `D:\Project\ADS1299_TONG_V2_自动连线\outputs\ADS1299_TONG_V2_L3_REBUILD_R8_2026-09-21.epro`
- Pin-map note: `C:\Users\TONG\Downloads\ADS1299_ESP32_Pin_Map.md`

The supplied EPRO remains on the Desktop; the hardware source project is unchanged. The active board is `PCB3_4L_L3_REBUILD_R8`.

## Verified hardware facts for the port

ADS1299: START GPIO0, PWDN GPIO1, SCLK GPIO6, MOSI GPIO8, MISO GPIO9, CS GPIO10, DRDY GPIO23, RESET GPIO24. The R8 schematic/PCB audit shows 4.99 kΩ series input resistors and 4.7 nF capacitors from each INxP to SRB1_REF; this matches the existing PC impedance compensation model.

The 2026-09-21 R8 export was independently checked for schematic/PCB pin-net consistency and copper connectivity. The 2026-09-24 release contains 37 computed fill fragments for all 32 editable copper zones and a local -2.5V_AVSS trace reroute; its PCB has the same 212 components and 127 nets as the earlier R8. The user confirmed that the release passed native DRC. This is a PCB check, not a substitute for first-power-on or on-body safety testing.
