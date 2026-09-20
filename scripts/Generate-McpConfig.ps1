$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$config = @{
    mcpServers = @{
        'jev-browser-chrome' = @{
            command = (Join-Path $root '.venv\Scripts\python.exe')
            args = @((Join-Path $root 'chrome_mcp.py'))
        }
    }
}
$json = $config | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $root 'mcp-config.json'), $json, [Text.UTF8Encoding]::new($false))
Write-Host ('MCP configuration created: ' + (Join-Path $root 'mcp-config.json')) -ForegroundColor Green
