# ADS1299 P300 acquisition on ESP32-C5-WROOM-1U-N8R8

Independent ESP-IDF 6.0.2 firmware for the R8 board. Version 2.1.0 adds USB provisioning and selectable USB serial/native Wi-Fi transport for 8ch/1000SPS ADS1299 data. Wi-Fi uses TCP1234, control UDP1235 and paired discovery UDP1236. No ESP-Hosted/SDIO coprocessor is used.

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
# This header now holds acquisition/algorithm settings, not the active network.
idf.py build
idf.py -p COMx flash
```

Copy the example on a fresh clone for sampling/algorithm parameters. Existing local headers are preserved/ignored; legacy SSID/password/PC-address macros are no longer consumed. Configure through Monitor/USB, with successful credentials persisted in the device's `p300_network` NVS namespace. Passwords are never returned in status. NVS persistence does not imply encryption.

Target/memory remain `esp32c5`, 8MB Flash/PSRAM. The Monitor normally listens on `0.0.0.0:1234`. Paired discovery or USB supplies the actual PC endpoint; allow required application/ports on the intended trusted interface. The software never disables the firewall. With a PC hotspot the target is its hotspot-side address, not the campus/Tailscale/VMware/board address.

UART0 is optically isolated, with manual BOOT/EN. If automatic flashing cannot enter download mode, hold BOOT, pulse EN, release EN, then release BOOT before flashing. UART boots at 115200 and negotiates 921600 (or 460800) for the binary session. Close IDF monitor before connecting the GUI; COM cannot be shared. Do not bypass isolation with ordinary USB-TTL.

After flashing, release BOOT and press EN. JP1/JP2/JP3 must connect their own source/load pins: TP5 = +2.5V, TP2 = +3.3V and TP7 = -2.5V against system GND. RESET/PWDN must be high; CLKSEL is pulled high through R28 (10kOhm) for the internal clock. R25 (33Ohm) connects GPIO1 to PWDN. A low PWDN shuts down ADS; CLKSEL low selects an external clock not provided by this board. ADS ID/register checks must pass for EEG, while USB management remains available on acquisition failure.

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

The 78 PC tests (53 original, 9 dual-transport and 16 recovery regressions) pass. Related GUI issues were repaired: external-resistor calibration (do not replace board 4.99kOhm), verified 1/2-frame send control and readable side-panel width. A fake serial peer validates existing controls, not real COM timing/noise/throughput.

## First use: USB / campus hotspot

Discovery defaults to UDP1236; advanced deployments can compile with `idf.py -DP300_DISCOVERY_PORT=1237 build` and start Monitor with matching `--discovery-port 1237`. Port changes are separate from USB/runtime Wi-Fi credential configuration.

1. Open **连接/配网**, select the CP210x COM port and click **USB 连接并配对**. It negotiates the selected rate and pairs the Monitor. Reconnecting immediately may require the prior five-second lease to expire.
2. For offline EEG, select **USB 串口** and apply. Wi-Fi is disabled; clock sync, EEG, Marker and diagnostics all use serial. Check SPS/CRC/sequence and self-test before formal recording.
3. For campus use, PC stays on the campus WLAN and completes its normal authentication. In the helper open **Windows 热点设置**, share WLAN over a separate 2.4GHz hotspot and set its own name/strong password. The board never receives campus account credentials.
4. Click **检测网络/热点能力** and select the actual hotspot-side PC IPv4. A Wi-Fi Direct adapter alone is not proof of active broadcasting. Copy hotspot credentials if allowed, or enter matching name/password once.
5. Click **通过 USB 下发并验证网络**. Association/DHCP must succeed before new credentials replace the old configuration; this operation returns to serial mode. Select Wi-Fi and apply, then verify discovery, synchronization and actual TCP samples before unplugging USB.
6. Monitor receives EEG on the hotspot and sends HTTP on the PC's campus route. The board needs no direct campus-server/public Internet access. Availability depends on driver/campus policy; keep serial as fallback and do not bypass policy.

Optional automatic hotspot control requires `tools/requirements-p300-windows-hotspot.txt` and compatible Windows capabilities. Missing wiFiControl permission/driver support still needs Settings/manual setup; installing packages or running as administrator does not guarantee access. Credential read requires explicit consent. Optional PC persistence/pairing uses user-scoped DPAPI, not plaintext JSON. Monitor neither resets the Windows hotspot on startup nor closes the user's hotspot on exit.

Switching input stops current recording/HTTP; start a new recording/push explicitly after the new stream is verified. Stream IDs distinguish boundaries. FFT/filters/export schema and JSON device `seu` are unchanged. Discovery HMAC is not EEG encryption: use a trusted AP/hotspot. Timestamp mapping uses acquisition time, not USB arrival time, and baud rate alone does not guarantee 1ms stimulus timing.

Before attaching electrodes, verify supply rails and isolation, ADS1299 ID/register readback, DRDY rate near 1000 SPS, Wi-Fi IP, TCP batch integrity and UDP controls. Observe the board's RUN/CHARGE handling instructions; a successful build or schematic DRC pass does not validate on-body safety or analog signal quality.

## Recovery behavior and verified software build

- Discovery sends the same signed request to the configured PC address and to broadcast. A network that filters broadcasts can use the USB-configured PC address, provided unicast UDP1236/UDP1235 and TCP1234 are allowed. Both routes require the paired Monitor HMAC response; invalid packets do not terminate the discovery service.
- Expired discovery clocks are released even without new requests. Receiver/session identities prevent queued events from an old link from changing the new connection.
- A failed USB handshake or lost cable closes the COM handle. The GUI returns to the connect state. Losing USB while receiving serial EEG stops recording/HTTP; losing the USB management cable during Wi-Fi EEG leaves the Wi-Fi receiver running.
- Serial synchronization uses a rolling 64-measurement history, 50ms maximum RTT and 5ms minimum-RTT slack. After 10 seconds without an accepted measurement it restarts at 10Hz; after 16 accepted measurements it returns to 2Hz. UDP retains its 10ms/2ms limits. Wi-Fi mode uses UDP for clock synchronization while USB remains available for management.
- Editing the PC address in the connection page uses the displayed IPv4 text, even if a different adapter was previously selected.

Verified on 2026-10-13: 72 PC tests, host C protocol tests, C5 scope checks, hidden-window connection-page render and ESP-IDF 6.0.2 build. The application image is build/ads1299_c5_p300_firmware.bin, 1,049,584 bytes, SHA256 359484C923704471ED64437273AC62736240E6541B1782C5A1587306385C698C. This is an application image; use idf.py flash to flash the matching bootloader/partition/application set.

Real-board acceptance remains:
1. At both 921600 and 460800, run serial EEG for at least 10 minutes and inspect SPS, CRC, sequence gaps and queue drops; exercise Marker, impedance, internal short and BIAS.
2. Unplug/reconnect USB, verify COM release/re-pairing, normal-mode restoration after internal short, and that recording/HTTP never silently resumes across a serial disconnect.
3. Validate the actual PC campus uplink and hotspot. Change the PC address, use USB to update the endpoint, and test unicast discovery where broadcasts are filtered.
4. Switch serial/Wi-Fi repeatedly, check new stream IDs and re-synchronization, then restart recording/HTTP explicitly. While in Wi-Fi, unplug only the management USB link and verify data continues.
5. Measure stimulus-to-sample timing on the actual link. Passing host tests does not establish hardware latency, loss-free capture or analog EEG quality.

### USB connection diagnostics (2026-10-14)

The USB status beside the connection button now reports connection progress, the actual device status, and connection errors. The previous default reminder about IDF monitor was not an occupancy check. Pair completion restores the latest handshake status even when early events arrived before the GUI adopted the session; this matters when ADS startup failed and clock synchronization cannot refresh the status.

On the connected C5 board, COM7 successfully negotiated 115200 to 921600 and answered a status request with zero serial parser errors. Device P300_3844BE373428 reported serial mode, paired=true and ads_running=false. This verifies the USB management path only, not EEG acquisition or sustained serial throughput. After restarting the Monitor, select COM7/921600 and connect; investigate ADS startup logs separately if ADS remains stopped. No firmware change or new flash is required for this GUI fix.

Provisioning validation does not leave Wi-Fi enabled: the device verifies association/DHCP, saves the network and stops Wi-Fi. The Monitor now states this explicitly and provides **启用 Wi-Fi 数据模式** below the operation result. Keep USB connected and click it to apply the saved Wi-Fi mode; network validation never switches transport automatically. Mode activation still requires the firmware stream task. If ADS startup failed, the GUI explains the resulting mode_busy_or_unavailable instead of treating it as a Wi-Fi password failure. These GUI changes need only a Monitor restart, not a firmware flash.


### SSVEP display filter (2026-10-20)

The display/FFT filter selector now includes **SSVEP 6–90 Hz + 50 Hz narrow stop (realtime)**.
It uses a Butterworth order-2 49.5–50.5 Hz bandstop followed by an order-4 6–90 Hz bandpass,
with continuous causal SOS state across packets. This matches the design bands/orders of
`filter_eeg.m`; it is not the same operation as the SSVEP program's window-local forward/backward
zero-phase filtering. Sampling rate must exceed 180 SPS. Official recordings, raw quality
statistics and localhost HTTP still use raw microvolts. Internal-short data bypass display
filters. No firmware change or reflashing is needed; restart Monitor to see the new mode.


### HTTP destination presets (2026-10-20)

The Record tab includes **SSVEP测试 — 127.0.0.1:25001** and
**SEU电视端 — 10.201.1.182:25000**, plus Custom. Selecting a preset fills IP and port;
the endpoint path is preserved (normally `/api/bci/data`). It does not start HTTP or
overwrite the last saved destination. Start HTTP saves the current fields as before.
Presets and destination fields are locked while HTTP is active; stop before switching.
