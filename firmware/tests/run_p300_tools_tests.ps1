$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw 'python is required for the P300 PC tool tests.'
}

Push-Location $root
try {
    & $python.Source -m unittest discover -s tests -p 'test_p300*.py' -v
    if ($LASTEXITCODE -ne 0) {
        throw 'P300 PC tool tests failed.'
    }
} finally {
    Pop-Location
}
