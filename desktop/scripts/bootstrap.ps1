param([string]$Root = (Join-Path $env:LOCALAPPDATA 'AI Shuffle Source'))
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = [IO.Path]::GetFullPath($Root)
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$desktop = Split-Path $PSScriptRoot -Parent
$node = Join-Path $Root 'bootstrap-node\node.exe'
$pointer = Join-Path $Root 'node-path.txt'
if (Test-Path $pointer) {
    $candidate = (Get-Content -LiteralPath $pointer -Raw).Trim()
    if ($candidate.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $candidate)) { $node = $candidate }
}
if (!(Test-Path -LiteralPath $node)) {
    $version = '22.23.2'
    $name = "node-v$version-win-x64.zip"
    $temporary = Join-Path $Root ('node-download-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    try {
        Write-Host 'Downloading the first-run Node runtime from nodejs.org...'
        $checksums = (Invoke-WebRequest -UseBasicParsing "https://nodejs.org/dist/v$version/SHASUMS256.txt").Content
        $line = ($checksums -split "`n" | Where-Object { $_ -match ('\s+' + [regex]::Escape($name) + '\s*$') })
        if (@($line).Count -ne 1) { throw 'Node checksum entry was not found.' }
        $expected = ($line.Trim() -split '\s+')[0]
        $archive = Join-Path $temporary $name
        Invoke-WebRequest -UseBasicParsing "https://nodejs.org/dist/v$version/$name" -OutFile $archive
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash -ine $expected) { throw 'Node download checksum did not match.' }
        Expand-Archive -LiteralPath $archive -DestinationPath $temporary
        Move-Item -LiteralPath (Join-Path $temporary "node-v$version-win-x64") -Destination (Join-Path $Root 'bootstrap-node')
    } finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
$starter = Join-Path $Root 'starter'
# Rerunning a newer downloaded bootstrap refreshes the verifier/key, not user data.
& $node (Join-Path $desktop 'scripts\refresh-starter.mjs') $Root
if ($LASTEXITCODE -ne 0) { throw 'Starter refresh failed. Your existing application and data were retained.' }
$launch = Join-Path $Root 'Start AI Shuffle.cmd'
$command = '@echo off' + "`r`n" + 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0starter\scripts\bootstrap.ps1" -Root "%~dp0."' + "`r`n"
[IO.File]::WriteAllText($launch, $command, [Text.Encoding]::ASCII)
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'AI Shuffle.lnk'))
$shortcut.TargetPath = $launch
$shortcut.WorkingDirectory = $Root
$shortcut.Save()
& $node (Join-Path $starter 'scripts\bootstrap.mjs') --root $Root
if ($LASTEXITCODE -ne 0) { throw "AI Shuffle exited with code $LASTEXITCODE. Your existing application and data were retained." }
