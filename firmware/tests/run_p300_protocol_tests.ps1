$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'components\p300_stream\p300_protocol.c'
$biasStateSource = Join-Path $root 'components\p300_stream\p300_bias_state.c'
$txControlSource = Join-Path $root 'components\p300_stream\p300_tx_control.c'
$txGroupSource = Join-Path $root 'components\p300_stream\p300_tx_group.c'
$mathSource = Join-Path $root 'components\eeg_acq\eeg_impedance_math.c'
$healthSource = Join-Path $root 'components\eeg_acq\eeg_drdy_health.c'
$header = Join-Path $root 'components\p300_stream\include\p300_protocol.h'
$test = Join-Path $PSScriptRoot 'test_p300_protocol.c'
$output = Join-Path $env:TEMP 'test_p300_protocol.exe'

if (!(Test-Path -LiteralPath $source) -or !(Test-Path -LiteralPath $biasStateSource) -or
    !(Test-Path -LiteralPath $txControlSource) -or
    !(Test-Path -LiteralPath $txGroupSource) -or
    !(Test-Path -LiteralPath $header) -or
    !(Test-Path -LiteralPath $mathSource) -or !(Test-Path -LiteralPath $healthSource)) {
    Write-Error 'FAIL: P300 protocol, impedance math, or DRDY health module is not implemented.'
}

$gcc = Get-Command gcc -ErrorAction SilentlyContinue
if ($null -eq $gcc) {
    Write-Error 'FAIL: gcc is required for the host protocol tests.'
}

& $gcc.Source -std=c11 -Wall -Wextra -Werror `
    "-I$($PSScriptRoot)\fixtures" `
    "-I$($root)\components\p300_stream\include" `
    "-I$($root)\components\eeg_acq\include" `
    "-I$($root)\components\app_config\include" `
    $source $biasStateSource $txControlSource $txGroupSource $mathSource $healthSource $test '-o' $output '-lm'
if ($LASTEXITCODE -ne 0) {
    throw 'FAIL: protocol test compilation failed.'
}

& $output
if ($LASTEXITCODE -ne 0) {
    throw 'FAIL: protocol assertions failed.'
}

Write-Host 'PASS: P300 protocol tests'
