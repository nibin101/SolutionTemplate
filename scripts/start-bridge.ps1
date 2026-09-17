# Start the background camera bridge.
#
#   scripts\start-bridge.ps1            foreground, real cameras (logs to console)
#   scripts\start-bridge.ps1 -Tray      background, system-tray icon
#   scripts\start-bridge.ps1 -Simulate  demo mode, no camera needed
#   scripts\start-bridge.ps1 -Check     probe capture sources and exit

param(
    [switch]$Tray,
    [switch]$Simulate,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) { throw "Run scripts\setup.ps1 first." }

# -Simulate overrides the configured sources for this run only; bridge\.env is
# never rewritten, so the station keeps its real configuration.
if ($Simulate) { $env:ENABLED_SOURCES = "simulator" }

$arguments = @("run_bridge.py")
if ($Check) { $arguments += "--check" }
if ($Tray) { $arguments += "--tray" }

Push-Location (Join-Path $root "bridge")
try { & $python @arguments } finally { Pop-Location }
