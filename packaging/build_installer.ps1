# Build MirrorBridge-Setup-<version>.exe (packaging\installer.iss) from dist\MirrorBridge.exe and the
# notices in build\notices (packaging\third_party_notices.py). Run from the repo root on Windows:
#   .\packaging\build_installer.ps1 [-Out build\installer]
# Uses Inno Setup 6 (installed with Chocolatey when it is missing, as on GitHub's runners) and prints
# the installer's path last.
param([string]$Out = "build\installer")
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$version = python packaging\check_version.py
if ($LASTEXITCODE -ne 0) { throw "packaging\check_version.py failed" }
foreach ($need in @("dist\MirrorBridge.exe", "build\notices\THIRD-PARTY-NOTICES.txt")) {
    if (-not (Test-Path $need)) { throw "$need is missing: build the exe and the notices first" }
}

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Host "Inno Setup 6 is not installed: installing it with Chocolatey"
    choco install innosetup -y --no-progress
    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) { throw "Inno Setup did not install ($iscc is missing)" }
}

$root = (Get-Location).Path
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$outDir = (Resolve-Path $Out).Path
& $iscc /Qp "/DAppVersion=$version" "/DExeDir=$root\dist" "/DNoticesDir=$root\build\notices" "/O$outDir" packaging\installer.iss
$setup = Join-Path $outDir "MirrorBridge-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "ISCC did not write $setup" }
Write-Output $setup
