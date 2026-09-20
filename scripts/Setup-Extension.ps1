$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .venv first: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e .'
}
& $python -c "import extension_daemon"
$extension = Join-Path $root 'extension'
Write-Host "Jev extension is ready: $extension"
Start-Process 'chrome.exe' 'chrome://extensions/'
Write-Host 'Enable Developer mode, choose Load unpacked, and select the extension folder shown above.'
