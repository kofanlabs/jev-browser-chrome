$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $root

Write-Host 'Jev Browser for Chrome - Windows installer' -ForegroundColor Cyan
$python = Get-Command py.exe -ErrorAction SilentlyContinue
if ($python) {
    & py.exe -3 -c 'import sys; assert sys.version_info >= (3,12)' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required: https://www.python.org/downloads/' }
    & py.exe -3 -m venv .venv
} else {
    & python.exe -c 'import sys; assert sys.version_info >= (3,12)' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required: https://www.python.org/downloads/' }
    & python.exe -m venv .venv
}

& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
& '.\.venv\Scripts\python.exe' -m pip install -e .
& '.\.venv\Scripts\python.exe' -c 'import extension_daemon; print("Local bridge files created.")'
& (Join-Path $PSScriptRoot 'Generate-McpConfig.ps1')

Write-Host ''
Write-Host 'Installation complete.' -ForegroundColor Green
Write-Host '1. Ayarlar.cmd: save your Jev API key.'
Write-Host '2. In Chrome, enable Developer mode and Load unpacked: extension'
Write-Host '3. Add mcp-config.json to your MCP host, then restart the host.'
Start-Process 'chrome.exe' 'chrome://extensions/'
Start-Process explorer.exe -ArgumentList ('/select,"' + (Join-Path $root 'extension\manifest.json') + '"')
