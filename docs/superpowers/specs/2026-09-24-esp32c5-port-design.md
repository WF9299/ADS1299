# ESP32-C5 ADS1299 firmware port

## Decision

Create an independent ESP-IDF 6.0.2 project under `D:\Project\ADS1299_TONG_V2_P\firmware`. Do not modify the existing P4/Hosted firmware or the R8 PCB. The released R8 board has passed native DRC according to the user.

## Hardware and acquisition

Use ESP32-C5-WROOM-1U-N8R8 with SPI2 and explicit ADS1299 pins: START 0, PWDN 1, SCLK 6, MOSI/DIN 8, MISO/DOUT 9, CS 10, DRDY 23, RESET 24. Preserve ADS1299 register configuration, 1000 SPS, SRB1 reference routing, BIAS control, impedance and internal-short diagnostics. Keep the independent PCNT DRDY counter where supported. On the single-core C5, create all tasks without P4 core affinity. Prefer PSRAM for the acquisition ring, retaining internal-RAM fallback.

## Connectivity and protocol

Replace P4 ESP-Hosted Wi-Fi with C5 native Wi-Fi STA. Preserve existing SSID/server configuration semantics, DHCP or optional static IPv4, SNTP, TCP client port 1234 and UDP marker/control port 1235. Keep all binary frame and control formats unchanged so the existing PC monitor, simulator and exports remain compatible. No EEG data transfer over the isolated UART0; reserve it for bootloader and diagnostics.

## Build and verification

Pin ESP-IDF to 6.0.2, target `esp32c5`, 8 MB flash and PSRAM; remove Hosted, remote Wi-Fi and P4-only settings/dependency setup. Update scope tests for the new target. Validate with host protocol/tools tests, C5 compile and size checks. Hardware verification after flashing must check manual BOOT/EN download, ADS1299 ID and register readback, DRDY near 1000 SPS, native Wi-Fi IP, TCP data and UDP control. A successful compile is not evidence that analog or wireless behavior has been proven on the board.
