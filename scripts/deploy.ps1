<#
.SYNOPSIS
    Build and deploy source frontend assets; managed desktop owns runtime restarts.

.DESCRIPTION
    Use -SkipRestart to build frontend and copy it into app/static. This script
    cannot start a standalone worker or stop the managed PostgreSQL supervisor.
    Start/restart the desktop app itself for runtime changes. Without -SkipRestart
    it refuses before building, copying files or touching a running process.
    The Docker-host equivalent is scripts/deploy.sh.

.PARAMETER SkipBuild
    Skip the frontend build/copy step.

.PARAMETER SkipRestart
    Required. Leave the managed backend and database running unchanged.

.PARAMETER Port
    Retained for command-line compatibility; no standalone readiness poll is run.

.PARAMETER ReadyTimeoutSec
    Retained for command-line compatibility; no standalone readiness poll is run.
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

if (-not $SkipRestart) {
    throw 'Standalone backend restart is unsupported. Start or restart AI Shuffle through the desktop app, which supervises its private database. Use -SkipRestart for a frontend-only build.'
}

# scripts/ is one level below the repo root.
$root   = Split-Path -Parent $PSScriptRoot
$src    = Join-Path $root 'frontend\build'
$dst    = Join-Path $root 'app\static'

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

Write-Step 'Frontend-only operation complete; managed services left unchanged.'
