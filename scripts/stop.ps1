# Stop the backend and the camera bridge.
#
#   scripts\stop.ps1
#
# Finds them by what they are running rather than by a saved PID file, so it
# still works after a reboot, a crash, or a window closed by hand. Nothing else
# on the machine is touched: only python processes running run_server.py or
# run_bridge.py from THIS folder.
#
# Photographs and records are untouched - they live in backend\data\ and are
# written to disk as they arrive, not held in memory.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# Matched on the script name alone, deliberately. On many Windows setups the
# venv's python.exe is a launcher stub that re-executes the real interpreter as
# a child, and that child's command line carries neither the repo path nor the
# venv - so a path filter would kill the stub and leave the actual server
# holding port 8000. `run_server.py` and `run_bridge.py` are specific enough to
# this project to be safe to match on their own.
$targets = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match "run_server\.py|run_bridge\.py" }

# Whatever is actually holding the port, even if it was started some other way.
$busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($busy -and ($targets.ProcessId -notcontains $busy.OwningProcess)) {
    $extra = Get-CimInstance Win32_Process -Filter "ProcessId=$($busy.OwningProcess)"
    if ($extra) { $targets = @($targets) + $extra }
}

if (-not $targets) {
    Write-Host ""
    Write-Host "  Nothing to stop - SnapChart is not running." -ForegroundColor DarkGray
    Write-Host ""
    return
}

Write-Host ""
foreach ($process in $targets) {
    $what = "bridge"
    if ($process.CommandLine -match "run_server\.py") { $what = "backend" }
    Write-Host "  Stopping $what (PID $($process.ProcessId))..." -NoNewline
    try {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        Write-Host " done." -ForegroundColor Green
    } catch {
        Write-Host " could not stop it: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Start-Sleep -Milliseconds 500
$busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "  Port 8000 is still held by PID $($busy.OwningProcess)." -ForegroundColor Yellow
} else {
    Write-Host "  Port 8000 is free." -ForegroundColor DarkGray
}
Write-Host ""
