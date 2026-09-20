$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Once .venv kurun: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e .'
}
& $python -c "import extension_daemon"
$extension = Join-Path $root 'extension'
Write-Host "Jev eklentisi hazir: $extension"
Start-Process 'chrome.exe' 'chrome://extensions/'
Write-Host 'Developer mode acin, Load unpacked secin ve yukaridaki extension klasorunu secin.'
