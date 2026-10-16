<#
.SYNOPSIS
  Late restore point acceptance (desktop/LAUNCHER.md, "Launchers without
  restore points") against the real 0.9.6 installer, inside Windows Sandbox.

.DESCRIPTION
  Run from a checkout of the branch under test on Windows 10/11 Pro or
  Enterprise with Windows Sandbox enabled, and gh (signed in, with read access
  to the release repository), git, python and Node 20.11 or newer on PATH.

    1. builds desktop/ in this checkout (npm ci when needed, npm run build)
    2. downloads the Windows installer of -Tag and checks it against the
       release's SHA256SUMS
    3. prepares four source archives of HEAD under an ephemeral release key
       (verify-late-restore-point.mjs prepare)
    4. starts Windows Sandbox with this checkout, node.exe, the installer and
       the prepared folder mapped read-only and one output folder writable;
       inside it, verify-late-restore-point.mjs run installs the app and drives
       it through the late copy, a failed copy with Try again, a kill before
       the fence and a kill after PostgreSQL started
    5. waits for the result and prints the pass/fail summary; exits 0 only on PASS

  Nothing is installed on the host. The app, its profile and its database
  exist only inside the sandbox, never in the real per-user install or
  %APPDATA%\ai-shuffle-desktop, and closing the sandbox window discards them.
  The sandbox stays open at the end for inspection; close it when done.
  Each revision is built inside the sandbox like an in-app update, which
  needs the network: a full run takes one to three hours.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File desktop\scripts\late-restore-acceptance.ps1
#>
param(
  [string]$Output = (Join-Path $env:TEMP ('shuffle-late-acceptance-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))),
  [string]$Tag = 'v0.9.6',
  [string]$Repository = 'HobbyCoders/ai-shuffle-private',
  [int]$TimeoutMinutes = 240,
  [int]$MemoryMB = 8192
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3

function Invoke-Checked([string]$File, [string[]]$Arguments) {
  & $File @Arguments
  if ($LASTEXITCODE -ne 0) { throw "$File $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$desktop = Join-Path $repo 'desktop'
$sandboxExe = Join-Path $env:windir 'System32\WindowsSandbox.exe'
if (-not (Test-Path $sandboxExe)) {
  throw 'Windows Sandbox is not enabled. Enable it (as administrator: Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM, then restart), or run "node desktop\scripts\verify-late-restore-point.mjs run" on a virtual machine you will throw away.'
}
if (Get-Process -Name 'WindowsSandbox*' -ErrorAction SilentlyContinue) { throw 'Windows Sandbox is already running, and only one can run at a time. Close it first.' }
foreach ($tool in 'gh', 'git', 'python', 'node', 'npm') {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not on PATH" }
}
$nodeVersion = [version](& node -p 'process.versions.node')
if ($nodeVersion -lt [version]'20.11.0') { throw "Node 20.11 or newer is needed (found $nodeVersion)" }
if (Test-Path $Output) { throw "$Output already exists; choose a new -Output folder" }

$installerDir = Join-Path $Output 'installer'
$prepared = Join-Path $Output 'prepared'
$tools = Join-Path $Output 'tools'
$results = Join-Path $Output 'results'
New-Item -ItemType Directory -Force -Path $installerDir, $tools, $results | Out-Null

Write-Host '== Building desktop/ in this checkout'
Push-Location $desktop
try {
  if (-not (Test-Path (Join-Path $desktop 'node_modules\@electron\asar'))) { Invoke-Checked 'npm' @('ci') }
  Invoke-Checked 'npm' @('run', 'build')
} finally { Pop-Location }

Write-Host "== Downloading the $Tag Windows installer from $Repository"
Invoke-Checked 'gh' @('release', 'download', $Tag, '--repo', $Repository, '--pattern', '*-win-x64.exe', '--pattern', 'SHA256SUMS', '--dir', $installerDir)
$installer = @(Get-ChildItem -Path $installerDir -Filter '*-win-x64.exe')
if ($installer.Count -ne 1) { throw "Expected one Windows installer in $Tag, found $($installer.Count)" }
$installer = $installer[0]
$sums = Get-Content (Join-Path $installerDir 'SHA256SUMS') | Where-Object { $_ -match ('^([0-9a-f]{64}) [ *]?' + [regex]::Escape($installer.Name) + '$') }
if (-not $sums) { throw "SHA256SUMS of $Tag does not list $($installer.Name)" }
$expected = ([regex]::Match($sums, '^[0-9a-f]{64}')).Value
$actual = (Get-FileHash -Algorithm SHA256 $installer.FullName).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "$($installer.Name) does not match SHA256SUMS" }
Write-Host "$($installer.Name) matches SHA256SUMS"

Write-Host '== Preparing the source archives (ephemeral release key)'
Push-Location $repo
try { Invoke-Checked 'node' @((Join-Path $desktop 'scripts\verify-late-restore-point.mjs'), 'prepare', $prepared) } finally { Pop-Location }

# node.exe alone runs the acceptance script; nothing else of the host's Node is mapped.
Copy-Item -Path (Get-Command node).Source -Destination (Join-Path $tools 'node.exe')
$inside = 'C:\acceptance'
$runCmd = @"
@echo off
set PATH=$inside\tools;%PATH%
set SHUFFLE_ACCEPTANCE_SANDBOX=1
node "$inside\repo\desktop\scripts\verify-late-restore-point.mjs" run "$inside\installer\$($installer.Name)" "$inside\prepared" "$inside\results" > "$inside\results\run.log" 2>&1
echo %ERRORLEVEL%> "$inside\results\exit-code.txt"
"@
Set-Content -Path (Join-Path $tools 'run.cmd') -Value $runCmd -Encoding ASCII

function Mapped([string]$HostFolder, [string]$SandboxFolder, [bool]$ReadOnly) {
  $h = [System.Security.SecurityElement]::Escape($HostFolder)
  $s = [System.Security.SecurityElement]::Escape($SandboxFolder)
  "    <MappedFolder><HostFolder>$h</HostFolder><SandboxFolder>$s</SandboxFolder><ReadOnly>$($ReadOnly.ToString().ToLowerInvariant())</ReadOnly></MappedFolder>"
}
$wsb = Join-Path $Output 'late-restore-acceptance.wsb'
$config = @"
<Configuration>
  <Networking>Enable</Networking>
  <MemoryInMB>$MemoryMB</MemoryInMB>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <PrinterRedirection>Disable</PrinterRedirection>
  <MappedFolders>
$(Mapped $repo "$inside\repo" $true)
$(Mapped $installerDir "$inside\installer" $true)
$(Mapped $prepared "$inside\prepared" $true)
$(Mapped $tools "$inside\tools" $true)
$(Mapped $results "$inside\results" $false)
  </MappedFolders>
  <LogonCommand><Command>cmd.exe /c $inside\tools\run.cmd</Command></LogonCommand>
</Configuration>
"@
Set-Content -Path $wsb -Value $config -Encoding UTF8

Write-Host "== Starting Windows Sandbox ($wsb)"
Write-Host "   Progress: $results\run.log"
Start-Process -FilePath $sandboxExe -ArgumentList "`"$wsb`""
$exitFile = Join-Path $results 'exit-code.txt'
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
while (-not (Test-Path $exitFile)) {
  if ((Get-Date) -gt $deadline) { throw "No result after $TimeoutMinutes minutes; see $results\run.log" }
  if (-not (Get-Process -Name 'WindowsSandbox*' -ErrorAction SilentlyContinue)) { throw "Windows Sandbox closed before the run finished; see $results\run.log" }
  Start-Sleep -Seconds 15
}
Start-Sleep -Seconds 2
$code = (Get-Content $exitFile -Raw).Trim()
$summary = Join-Path $results 'summary.txt'
Write-Host ''
if (Test-Path $summary) { Get-Content $summary | Write-Host }
else { Write-Host 'No summary was written. Last lines of run.log:'; Get-Content (Join-Path $results 'run.log') -Tail 40 | Write-Host }
Write-Host ''
Write-Host "Evidence: $results (summary.txt, acceptance.json, run.log, app logs)"
Write-Host 'Close the Windows Sandbox window to discard the test install.'
if ($code -ne '0' -or -not (Test-Path $summary) -or -not ((Get-Content $summary -TotalCount 1) -match ': PASS$')) { exit 1 }
exit 0
