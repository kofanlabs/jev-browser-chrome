$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -Force
Write-Host '1 - TypeSafe direct (recommended)'
Write-Host '2 - Vercel AI Gateway'
$choice = Read-Host 'Provider [1]'
if ($choice -notin @('', '1', '2')) { throw 'Invalid provider.' }
$provider = if ($choice -eq '2') { 'vercel' } else { 'typesafe' }
$keyName = if ($provider -eq 'vercel') { 'vercel-key.dpapi' } else { 'jev-key.dpapi' }
Write-Host 'Paste the API key. Characters are hidden and plaintext is never written to disk.'
$secret = Read-Host 'API key' -AsSecureString
if ($secret.Length -lt 20) { $secret.Dispose(); throw 'The key is empty or too short.' }
$configPath = Join-Path $PSScriptRoot '..\config'
New-Item -ItemType Directory -Force -Path $configPath | Out-Null
try { $secret | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $configPath $keyName) -Encoding ASCII }
finally { $secret.Dispose() }
[IO.File]::WriteAllText((Join-Path $configPath 'provider.json'), ('{"provider":"' + $provider + '"}'))
Write-Host 'Saved with Windows DPAPI protection for this Windows account.' -ForegroundColor Green
