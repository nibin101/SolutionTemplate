# Start the ingest API and the chair-side UI.
#
#   scripts\start-backend.ps1        loopback only (http://127.0.0.1:8000)
#   scripts\start-backend.ps1 -Lan   also reachable from phones on the same Wi-Fi
#
# -Lan binds 0.0.0.0 for this run only; backend\.env is not rewritten. It prints
# the address to open on the phone. Windows Firewall must allow inbound TCP 8000
# once - see the hint printed below, which needs an elevated shell.

param([switch]$Lan)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) { throw "Run scripts\setup.ps1 first." }

if ($Lan) {
    $env:HOST = "0.0.0.0"
    $rule = Get-NetFirewallRule -DisplayName "SnapChart 8000" -ErrorAction SilentlyContinue
    if (-not $rule) {
        Write-Host "Firewall rule 'SnapChart 8000' not found." -ForegroundColor Yellow
        Write-Host "If the phone cannot connect, run this once in an ADMIN PowerShell:" -ForegroundColor Yellow
        Write-Host '  New-NetFirewallRule -DisplayName "SnapChart 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow' -ForegroundColor DarkGray
        Write-Host ""
    }
}

Push-Location (Join-Path $root "backend")
try { & $python run_server.py } finally { Pop-Location }
