$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$componentsRoot = Join-Path $root 'components'
$expectedComponents = @('ads1299', 'app_config', 'board_config', 'eeg_acq', 'native_wifi', 'p300_stream', 'runtime_config', 'serial_protocol', 'uart_link', 'device_manager')
$actualComponents = @(Get-ChildItem -LiteralPath $componentsRoot -Directory | Select-Object -ExpandProperty Name | Sort-Object)
$difference = @(Compare-Object ($expectedComponents | Sort-Object) $actualComponents)
if ($difference.Count -ne 0) {
    throw "Unexpected firmware component set: $($difference | Out-String)"
}

$sourceFiles = @(
    Get-ChildItem -LiteralPath (Join-Path $root 'main'), $componentsRoot -Recurse -File |
        Where-Object { $_.Extension -in @('.c', '.h', '.cpp', '.hpp', '.txt') -or $_.Name -eq 'CMakeLists.txt' }
)
$forbidden = 'SSVEP_APP_|(?i)\b(ssvep|cca|fbcca|nurse|lvgl|brookesia|display|ethernet|rj45|esp32-c6|esp32c6|esp32p4|esp_hosted|hosted_wifi|wifi_remote)\b'
$matches = @($sourceFiles | Select-String -Pattern $forbidden)
if ($matches.Count -ne 0) {
    throw "Forbidden legacy firmware reference found:`n$($matches | Out-String)"
}

$board = Get-Content -LiteralPath (Join-Path $componentsRoot 'board_config\board_config.c') -Raw
$pins = [ordered]@{
    ads_drdy = 23
    ads_reset = 24
    ads_cs = 10
    ads_sclk = 6
    ads_mosi = 8
    ads_miso = 9
    ads_start = 0
    ads_pwdn = 1
}
foreach ($entry in $pins.GetEnumerator()) {
    if ($board -notmatch "\.$($entry.Key)\s*=\s*GPIO_NUM_$($entry.Value)\b") {
        throw "ADS1299 pin mismatch: $($entry.Key) must be GPIO$($entry.Value)."
    }
}

$acqSource = Get-Content -LiteralPath (Join-Path $componentsRoot 'eeg_acq\eeg_acq.c') -Raw
if ($acqSource -notmatch 'static\s+board_config_t\s+s_board_storage\s*;' -or
    $acqSource -notmatch 's_board_storage\s*=\s*\*board\s*;\s*s_board\s*=\s*&s_board_storage\s*;' -or
    $acqSource -match 's_board\s*=\s*board\s*;') {
    throw 'eeg_acq must own a copy of board_config; app_main board storage is temporary.'
}
if ($acqSource -match 'xTaskCreatePinnedToCore' -or $acqSource -notmatch 'xTaskCreate\(acq_task') {
    throw 'C5 acquisition task must use single-core-compatible xTaskCreate.'
}

$protocol = Get-Content -LiteralPath (Join-Path $componentsRoot 'p300_stream\include\p300_protocol.h') -Raw
$config = Get-Content -LiteralPath (Join-Path $componentsRoot 'app_config\include\app_config.h') -Raw
if ($config -notmatch '#define\s+P300_EEG_ACQ_TASK_PRIORITY\s+12U' -or
    $acqSource -notmatch 'P300_EEG_ACQ_TASK_PRIORITY') {
    throw 'C5 acquisition task must outrank stream and marker tasks.'
}
foreach ($required in @(
    '#define P300_EEG_CHANNELS 8U',
    '#define P300_EEG_BATCH_SAMPLES 20U',
    '#define P300_EEG_BATCH_BYTES 528U',
    '#define P300_SELF_TEST_REQUEST_BYTES 16U',
    '#define P300_SELF_TEST_RESULT_BYTES 20U',
    '#define P300_TX_CONTROL_REQUEST_BYTES 16U',
    '#define P300_TX_CONTROL_RESULT_BYTES 20U',
    '#define P300_SYNC_HISTORY                       64U',
    '#define P300_SAMPLE_RATE_HZ                     1000U',
    '#define P300_MARKER_PORT                        1235U'
)) {
    if (!$protocol.Contains($required) -and !$config.Contains($required)) {
        throw "Required protocol/config invariant is missing: $required"
    }
}

$streamSource = Get-Content -LiteralPath (Join-Path $componentsRoot 'p300_stream\p300_stream.c') -Raw
if ($streamSource -match 'xTaskCreatePinnedToCore' -or
    $streamSource -notmatch 'xTaskCreate\(marker_task' -or
    $streamSource -notmatch 'xTaskCreate\(stream_task') {
    throw 'C5 network tasks must use single-core-compatible xTaskCreate.'
}
foreach ($requiredTxControl in @(
    'P300_UDP_TYPE_TX_CONTROL_REQUEST',
    'p300_tx_control_state_handle',
    'p300_tx_control_state_take_pending',
    'active_tx_group_frames()',
    'P300_TCP_COALESCE_MAX_WAIT_MS'
)) {
    if (!$streamSource.Contains($requiredTxControl)) {
        throw "Runtime TCP grouping invariant is missing: $requiredTxControl"
    }
}

$adsSource = Get-Content -LiteralPath (Join-Path $componentsRoot 'ads1299\ads1299.c') -Raw
foreach ($requiredSelfTest in @(
    '#define ADS1299_CHSET_GAIN12_SHORT 0x51',
    'ads1299_apply_internal_short_config',
    'ADS1299_REG_BIAS_SENSP, 0x00',
    'ADS1299_REG_LOFF_FLIP, 0x00',
    'ADS1299_REG_MISC1, 0x00',
    'ADS1299_REG_CONFIG4, 0x00'
)) {
    if (!$adsSource.Contains($requiredSelfTest)) {
        throw "ADS1299 internal-short invariant is missing: $requiredSelfTest"
    }
}
foreach ($requiredModeFlow in @(
    'EEG_ACQ_MODE_SETTLE_SAMPLES',
    'eeg_acq_set_config_id',
    'batch.config_id',
    'P300_UDP_TYPE_SELF_TEST_REQUEST'
)) {
    if (!$acqSource.Contains($requiredModeFlow) -and
        !(Get-Content -LiteralPath (Join-Path $componentsRoot 'p300_stream\p300_stream.c') -Raw).Contains($requiredModeFlow)) {
        throw "Runtime internal-short mode invariant is missing: $requiredModeFlow"
    }
}

$manifest = Get-Content -LiteralPath (Join-Path $root 'main\idf_component.yml') -Raw
foreach ($requiredDependency in @('version: "6.0.2"')) {
    if (!$manifest.Contains($requiredDependency)) {
        throw "Pinned dependency is missing from main/idf_component.yml: $requiredDependency"
    }
}

$defaults = Get-Content -LiteralPath (Join-Path $root 'sdkconfig.defaults') -Raw
foreach ($requiredDefault in @(
    'CONFIG_IDF_TARGET="esp32c5"',
    'CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y',
    'CONFIG_SPIRAM=y'
)) {
    if ($defaults -notmatch "(?m)^$([regex]::Escape($requiredDefault))\r?$") {
        throw "Required C5 sdkconfig default is missing: $requiredDefault"
    }
}
if ($defaults -match 'CONFIG_ESP_HOSTED|CONFIG_ESP_WIFI_REMOTE|esp32p4') {
    throw 'P4 Hosted settings remain in sdkconfig.defaults.'
}

$migrationFiles = @(
    (Join-Path $root 'main\idf_component.yml'),
    (Join-Path $root 'sdkconfig.defaults'),
    (Join-Path $root 'README.md')
)
$migrationMatches = @($migrationFiles | Select-String -Pattern 'ESP32-C6|esp32c6|5\.5\.3|0\.14\.\*')
if ($migrationMatches.Count -ne 0) {
    throw "Legacy IDF 5/C6 migration reference found:`n$($migrationMatches | Out-String)"
}

$lockFile = Join-Path $root 'dependencies.lock'
if (Test-Path -LiteralPath $lockFile) {
    $lockText = Get-Content -LiteralPath $lockFile -Raw
    foreach ($lockVersion in @('version: 6.0.2')) {
        if (!$lockText.Contains($lockVersion)) {
            throw "dependencies.lock is missing pinned version: $lockVersion"
        }
    }
}

$wifi = Get-Content -LiteralPath (Join-Path $componentsRoot 'native_wifi\native_wifi.c') -Raw
if ($wifi -match 'P300_WIFI_SSID|P300_WIFI_PASSWORD' -or $streamSource -match 'P300_TCP_SERVER_IP') {
    throw 'Runtime networking must not consume hardcoded SSID/password/PC address.'
}
$main = Get-Content -LiteralPath (Join-Path $root 'main\main.c') -Raw
if ($main.IndexOf('device_manager_start()') -gt $main.IndexOf('ads1299_init(') -or $main -match 'while\s*\(!native_wifi_is_connected') {
    throw 'USB management must start before ADS bring-up; no Wi-Fi blocking startup.'
}
Write-Host 'PASS: C5 dual-transport scope, R8 pins, dynamic network, IDF pin and protocol invariants'
