# One-time setup: virtualenv, dependencies, .env files with a shared token, demo data.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#
# Safe to re-run: existing .env files are left alone so a configured station is
# never clobbered.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    $interpreter = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $interpreter) { throw "Python 3.12+ is required but was not found on PATH." }
    & $interpreter -m venv $venv
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -q -r (Join-Path $root "backend\requirements.txt") `
                          -r (Join-Path $root "bridge\requirements.txt")

# Both halves authenticate with the same shared secret, so it is generated once
# here rather than copy-pasted between two files by hand.
$token = & $python -c "import secrets;print(secrets.token_urlsafe(32))"

function New-EnvFile($component) {
    $target = Join-Path $root "$component\.env"
    if (Test-Path $target) {
        Write-Host "  $component\.env already exists - left unchanged" -ForegroundColor DarkGray
        return
    }
    $content = Get-Content (Join-Path $root "$component\.env.example") -Raw
    $content = $content -replace "change-me-to-a-long-random-string", $token
    Set-Content -Path $target -Value $content -Encoding utf8
    Write-Host "  wrote $component\.env" -ForegroundColor Green
}

Write-Host "Writing environment files..." -ForegroundColor Cyan
New-EnvFile "backend"
New-EnvFile "bridge"

Write-Host "Seeding demo patients..." -ForegroundColor Cyan
Push-Location (Join-Path $root "backend")
try { & $python seed.py } finally { Pop-Location }

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "  1. scripts\start-backend.ps1   then open http://127.0.0.1:8000"
Write-Host "  2. scripts\start-bridge.ps1    (add -Simulate to demo without a camera)"
