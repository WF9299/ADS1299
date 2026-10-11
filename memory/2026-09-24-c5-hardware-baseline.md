# C5 PCB hardware baseline

- Latest reviewed hardware archive: `D:\Project\ADS1299_TONG_V2_自动连线\outputs\ADS1299_TONG_V2_L3_REBUILD_R8_2026-09-21.epro`, active PCB `PCB3_4L_L3_REBUILD_R8`.
- The R8 report records 212 components, 701 pads, 127 nets and 693/693 schematic pin matches; independent repour found zero unconnected and no new copper short/clearance faults. Native EasyEDA repour/DRC is outstanding.
- Digital ADS1299 connections are START=0, PWDN=1, SCLK=6, MOSI=8, MISO=9, CS=10, DRDY=23, RESET=24. Module is ESP32-C5-WROOM-1U-N8R8.
- Per-channel input circuit remains 4.99 kΩ in series to INxP and 4.7 nF from INxP to SRB1_REF, matching the current monitor impedance network model.
- Hardware separates GND and USB_HOST_GND; UART0 TX/RX pass through TLP2361 isolation. R8 has USB/UART selector and battery protection changes, distinct power nets and four supply jumpers.
- Do not modify the source EPRO from firmware work. Source R8 has editable L3 pours with fill caches removed; verify with native EasyEDA repour and DRC.
