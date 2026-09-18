# Start everything: the backend, the camera bridge, and the chair-side view.
#
#   scripts\start.ps1             everything, real cameras
#   scripts\start.ps1 -Lan        also reachable from a phone on the same Wi-Fi
#   scripts\start.ps1 -Simulate   demo mode, no camera needed
#   scripts\start.ps1 -Tray       bridge runs as a tray icon instead of a window
#
# The two services stay separate processes on purpose - in a real practice the
# backend serves the whole clinic and a bridge runs on each operatory PC. This
# script just saves you launching them by hand, and each keeps its own window so
# you can still read its log.
#
# Stop everything with scripts\stop.ps1 (or close the two windows).

param(
    [switch]$Lan,
    [switch]$Simulate,
    [switch]$Tray
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) { throw "Run scripts\setup.ps1 first." }

# --- Refuse to start on top of something already running -------------------
# Two backends cannot share port 8000; the second would bind-fail and exit,
# which reads like a crash. Better to say so plainly before starting anything.
$busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = Get-Process -Id $busy.OwningProcess -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "  Port 8000 is already in use by PID $($busy.OwningProcess) ($($owner.ProcessName))." -ForegroundColor Yellow
    Write-Host "  SnapChart is probably already running. Open http://127.0.0.1:8000/" -ForegroundColor Yellow
    Write-Host "  To restart it cleanly:  scripts\stop.ps1" -ForegroundColor DarkGray
    Write-Host ""
    return
}

if ($Lan) { $env:HOST = "0.0.0.0" }
if ($Simulate) { $env:ENABLED_SOURCES = "simulator" }

# --- Backend ---------------------------------------------------------------
# Launched through `cmd /c start` rather than Start-Process alone so each
# service gets a console of its own that outlives this script. Closing the
# window you typed the command in must not take the clinic's server with it.
function Start-Detached {
    param([string]$Title, [string]$WorkingDirectory, [string[]]$Arguments)

    $quoted = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c start `"$Title`" /D `"$WorkingDirectory`" `"$python`" $quoted" `
        -WindowStyle Hidden | Out-Null
}

Write-Host ""
Write-Host "  Starting backend..." -NoNewline
Start-Detached -Title "SnapChart backend" `
    -WorkingDirectory (Join-Path $root "backend") -Arguments @("run_server.py")

# Wait for it to answer rather than guessing at a sleep: the bridge's first
# heartbeat should land on a server that is actually up.
$ready = $false
foreach ($attempt in 1..40) {
    Start-Sleep -Milliseconds 500
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/bridge/status" `
            -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ready = $true
        break
    } catch { }
}

if (-not $ready) {
    Write-Host " failed." -ForegroundColor Red
    Write-Host "  The backend did not answer within 20s. Check its window for the error." -ForegroundColor Yellow
    return
}
Write-Host " ready." -ForegroundColor Green

# --- Bridge ----------------------------------------------------------------
Write-Host "  Starting camera bridge..." -NoNewline
$arguments = @("run_bridge.py")
if ($Tray) { $arguments += "--tray" }

Start-Detached -Title "SnapChart bridge" `
    -WorkingDirectory (Join-Path $root "bridge") -Arguments $arguments
Write-Host " started." -ForegroundColor Green

# --- Open the chair-side view ----------------------------------------------
Start-Process "http://127.0.0.1:8000/"

Write-Host ""
Write-Host "  Chair-side view : http://127.0.0.1:8000/" -ForegroundColor Cyan
if ($Lan) {
    $address = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
        Select-Object -First 1).IPAddress
    if ($address) {
        Write-Host "  Phone capture   : http://${address}:8000/capture.html" -ForegroundColor Cyan
        Write-Host "  (open that on a phone on the same Wi-Fi)" -ForegroundColor DarkGray
    }
}
if ($Simulate) {
    Write-Host "  Simulator mode  : photos are replayed from bridge\sample-captures" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "  Stop everything : scripts\stop.ps1" -ForegroundColor DarkGray
Write-Host ""
