$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $root
while ($true) {
    Clear-Host
    Write-Host 'JEV BROWSER - SETTINGS' -ForegroundColor Cyan
    Write-Host '1 - Save/change API key'
    Write-Host '2 - Open Chrome extension setup'
    Write-Host '3 - Generate and open MCP configuration'
    Write-Host '4 - Check local bridge status'
    Write-Host '5 - Open Turkish setup guide'
    Write-Host '0 - Exit'
    switch (Read-Host 'Choice') {
        '1' { & (Join-Path $PSScriptRoot 'Set-Key.ps1') }
        '2' { & (Join-Path $PSScriptRoot 'Setup-Extension.ps1') }
        '3' { & (Join-Path $PSScriptRoot 'Generate-McpConfig.ps1'); Start-Process notepad.exe -ArgumentList ('"' + (Join-Path $root 'mcp-config.json') + '"') }
        '4' { & '.\.venv\Scripts\python.exe' -c 'import json; from extension_client import ensure_daemon; print(json.dumps(ensure_daemon(), indent=2))' }
        '5' { Start-Process notepad.exe -ArgumentList ('"' + (Join-Path $root 'KURULUM.md') + '"') }
        '0' { exit }
    }
    [void](Read-Host 'Press Enter to return')
}
