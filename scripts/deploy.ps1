<#
.SYNOPSIS
    Native Windows deploy: build the SvelteKit frontend, deploy it into
    app/static, restart the backend, and poll /health/ready until green.

.DESCRIPTION
    The unified deploy for the NATIVE Windows install (Phase 4.5). Equivalent to
    the manual cycle:
      1. cd frontend; npm run build   (stamps sw.js CACHE_VERSION via prebuild)
      2. refresh app/static (clear hashed _app, copy build/* over)
      3. kill the running app.main process tree (parent + worker, no orphans)
      4. relaunch detached, logging to run.log / run.err.log
      5. poll http://localhost:<Port>/health/ready until 200 (DB + pool ready)

    If the build fails, the script aborts BEFORE touching the running backend,
    so a broken build never leaves the app down. The Docker-host equivalent is
    scripts/deploy.sh.

.PARAMETER SkipBuild
    Restart the backend only (no frontend rebuild/deploy).

.PARAMETER SkipRestart
    Rebuild + deploy frontend only (leave the backend running).

.PARAMETER Port
    Port the backend listens on (default 8000). Used for the readiness poll.

.PARAMETER ReadyTimeoutSec
    How long to wait for /health/ready to go green (default 60).
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$SkipRestart,
    [int]$Port = 8000,
    [int]$ReadyTimeoutSec = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# scripts/ is one level below the repo root.
$root   = Split-Path -Parent $PSScriptRoot
$src    = Join-Path $root 'frontend\build'
$dst    = Join-Path $root 'app\static'
$python = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }  # fall back to PATH python

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if (-not $SkipBuild) {
    Write-Step 'Building frontend (npm run build)...'
    Push-Location (Join-Path $root 'frontend')
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed (exit $LASTEXITCODE) - backend left untouched" }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path (Join-Path $src 'index.html'))) {
        throw 'build/index.html missing - build produced no output, aborting'
    }

    Write-Step 'Deploying build -> app/static...'
    if (Test-Path (Join-Path $dst '_app')) {
        Remove-Item (Join-Path $dst '_app') -Recurse -Force   # clear stale content-hashed chunks
    }
    Copy-Item (Join-Path $src '*') $dst -Recurse -Force
    Write-Host '    deployed frontend -> app/static' -ForegroundColor Green
}

if ($SkipRestart) {
    Write-Step 'Skipping backend restart (-SkipRestart). Done.'
    return
}

Write-Step 'Stopping running backend (app.main tree)...'
# @(...) so an empty match doesn't throw under StrictMode when nothing is running.
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*app.main*' })
foreach ($proc in $procs) { taskkill /F /T /PID $proc.ProcessId 2>&1 | Out-Null }
Start-Sleep -Milliseconds 1000

Write-Step 'Launching backend...'
$p = Start-Process -FilePath $python -ArgumentList '-m','app.main' `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $root 'run.log') `
        -RedirectStandardError  (Join-Path $root 'run.err.log') `
        -WindowStyle Hidden -PassThru
Write-Host "    launched parent PID $($p.Id)" -ForegroundColor Green

Write-Step "Waiting for /health/ready on port $Port (timeout ${ReadyTimeoutSec}s)..."
$deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$Port/health/ready" `
                    -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # Not up yet (connection refused) or 503 (not ready) — keep polling.
    }
    Start-Sleep -Seconds 2
}

if ($ready) {
    Write-Host "==> Backend READY on http://localhost:$Port/health/ready" -ForegroundColor Green
} else {
    Write-Warning "Backend not ready after ${ReadyTimeoutSec}s - check run.err.log"
    Write-Host '--- run.err.log (tail) ---' -ForegroundColor DarkGray
    if (Test-Path (Join-Path $root 'run.err.log')) {
        Get-Content (Join-Path $root 'run.err.log') -Tail 8
    }
    exit 1
}
