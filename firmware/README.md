# ADS1299 P300 acquisition on ESP32-C5-WROOM-1U-N8R8

Independent ESP-IDF 6.0.2 firmware for the 2026-09-24 R8 release board. The ESP32-C5 drives an ADS1299 at 1000 SPS, streams 8-channel EEG batches to the existing PC monitor over TCP port 1234, and accepts marker/synchronization and diagnostic commands over UDP port 1235. It uses the C5's native Wi-Fi STA; there is no ESP-Hosted/SDIO coprocessor.

## Hardware mapping

| ADS1299 | ESP32-C5 GPIO |
|---|---:|
| START | 0 |
| PWDN# | 1 |
| SCLK | 6 |
| DIN / MOSI | 8 |
| DOUT / MISO | 9 |
| CS# | 10 |
| DRDY# | 23 |
| RESET# | 24 |

The firmware uses SPI2, mode 1, 1 MHz. The ADS1299 common REF electrode enters SRB1; do not remove the SRB1 register configuration. The BIAS electrode uses the ADS1299 feedback path. The PC impedance model includes the board's 4.99 kΩ / 4.7 nF input network; the included calibration file is uncalibrated and needs a new board-specific measurement.

On the single-core C5, the acquisition task runs above the TCP/UDP application tasks. Wi-Fi modem sleep is disabled to reduce 20 ms batch and marker-sync latency; this uses more battery power.

## Configure, build and flash

Use the ESP-IDF 6.0.2 PowerShell environment:

```powershell
cd D:\Project\ADS1299_TONG_V2_P\firmware
if (-not (Test-Path components\app_config\include\app_config.h)) {
    Copy-Item components\app_config\include\app_config.example.h components\app_config\include\app_config.h
}
# Review components\app_config\include\app_config.h for the Wi-Fi credentials
# and P300_TCP_SERVER_IP for the PC running the monitor.
idf.py build
idf.py -p COMx flash monitor
```

On a fresh Git clone, `app_config.h` is absent by design: it contains local network credentials and is ignored by Git. The tracked `app_config.example.h` supplies the full configuration without a real password. Set `P300_WIFI_SSID`, `P300_WIFI_PASSWORD` and `P300_TCP_SERVER_IP` in the local header; the example IP is only a placeholder. Existing local headers are not overwritten by the setup command above. Never force-add the local header or build images containing its password to Git. If shared non-secret settings change, update the example as well.

Target and memory defaults are in `sdkconfig.defaults`: `esp32c5`, 8 MB flash and PSRAM. The PC monitor listens on `0.0.0.0:1234`; allow inbound TCP 1234 and UDP 1235 in the PC firewall. The C5 is the TCP client, so `P300_TCP_SERVER_IP` is the PC's IP on the same reachable network, not the C5's DHCP address.

The board exposes USB-to-UART0 through optical isolation, with manual BOOT and EN switches. If automatic flashing does not enter download mode, hold BOOT, pulse EN, release EN, then release BOOT before flashing. No EEG data stream is emitted on UART0.

After flashing, release BOOT and press EN with the serial monitor open. For ADS bring-up, JP1/JP2/JP3 must connect their own source/load pins. Verify TP5 = +2.5 V, TP2 = +3.3 V and TP7 = -2.5 V against system GND. At the ADS end, RESET and PWDN must be high; CLKSEL is pulled high through R28 (10 kOhm) to select the internal clock. R25 (33 Ohm) connects C5 GPIO1 to PWDN. A low PWDN shuts down the ADS; a low CLKSEL selects an external clock, which this board does not provide. The ADS ID and register checks must pass before Wi-Fi/TCP initialization begins.

The SPI driver holds CS active for at least 3 us after each transaction and checks the ADS1299 fixed ID bits while allowing different silicon revisions. Invalid or unstable IDs abort ADS startup and are never reported as a passed SPI test. These software checks do not verify the physical rail voltages or continuity of the control lines.

## PC tools and validation

```powershell
python -m pip install -r tools\requirements-p300-tools.txt
python tools\p300_monitor.py --host 0.0.0.0 --port 1234
.\tests\check_firmware_scope.ps1
.\tests\run_p300_protocol_tests.ps1
.\tests\run_p300_tools_tests.ps1
```

The TCP binary EEG_BATCH v1 format and UDP marker, impedance, internal-short, BIAS and send-rate control messages are unchanged from the prior monitor. The PC monitor's JSONL/JSON/CSV/HTTP output remains the PC's responsibility.

The copied PC monitor includes pre-existing, uncommitted UI changes from the source working tree. Its current tool suite passes 50 of 53 tests; three GUI tests still expect the former calibration group, TCP-rate button and side-panel width. This is not a C5 firmware build failure. Those GUI differences need a separate decision before treating the PC test suite as fully green.

Before attaching electrodes, verify supply rails and isolation, ADS1299 ID/register readback, DRDY rate near 1000 SPS, Wi-Fi IP, TCP batch integrity and UDP controls. Observe the board's RUN/CHARGE handling instructions; a successful build or schematic DRC pass does not validate on-body safety or analog signal quality.
