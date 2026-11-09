# Try MirrorBridge-Setup-<version>.exe the way people and the app's updates use it, on a Windows
# runner (it installs into this user's %LOCALAPPDATA%\Programs\MirrorBridge and removes it again):
#   1. install silently, as by hand, and check the installed exe and its uninstall entry;
#   2. install again as an update (/UPDATE) while MirrorBridge runs: the installer must wait for
#      it to quit instead of failing or leaving the old exe;
#   3. uninstall, and check that the program folder and its uninstall entry are gone.
#   .\packaging\test_installer.ps1 build\installer\MirrorBridge-Setup-0.4.0.exe
param([Parameter(Mandatory = $true)][string]$Setup)
$ErrorActionPreference = "Stop"

$version = python packaging\check_version.py
$dir = Join-Path $env:LOCALAPPDATA "Programs\MirrorBridge"
$exe = Join-Path $dir "MirrorBridge.exe"
$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F1C2D7A-4B3E-4E59-9C1A-6D2B5E7F9A10}_is1"
$logs = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }

function Run-Setup([string[]]$Arguments, [string]$Log) {
    $p = Start-Process -FilePath $Setup -ArgumentList ($Arguments + "/LOG=$Log") -PassThru -Wait
    if ($p.ExitCode -ne 0) {
        if (Test-Path $Log) { Get-Content $Log | Write-Host }
        throw "The installer exited with $($p.ExitCode) ($($Arguments -join ' '))"
    }
}

# 1. A silent install, as by hand.
Run-Setup @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-") "$logs\setup-1.log"
if (-not (Test-Path $exe)) { throw "$exe was not installed" }
foreach ($file in @("README.md", "LEEME.md", "LICENSE", "THIRD-PARTY-NOTICES.txt")) {
    if (-not (Test-Path (Join-Path $dir $file))) { throw "$file was not installed" }
}
$out = (& $exe --version | Out-String).Trim()
if ($out -ne "MirrorBridge $version") { throw "The installed exe printed '$out', not 'MirrorBridge $version'" }
$entry = Get-ItemProperty -Path $key
if ($entry.DisplayVersion -ne $version) { throw "The uninstall entry says version '$($entry.DisplayVersion)'" }
if ((Resolve-Path $entry.InstallLocation).Path.TrimEnd('\') -ne (Resolve-Path $dir).Path.TrimEnd('\')) {
    throw "The uninstall entry says it is in '$($entry.InstallLocation)', not $dir"
}
Write-Host "1. Installed MirrorBridge $version in $dir"

# 2. An update while MirrorBridge runs: the installer waits for it (its mutex and its exe).
$app = Start-Process -FilePath $exe -ArgumentList "gui" -PassThru
$deadline = (Get-Date).AddSeconds(30)
$held = $false
while ((Get-Date) -lt $deadline -and -not $app.HasExited) {
    try { $m = [System.Threading.Mutex]::OpenExisting("MirrorBridgeAppMutex"); $m.Dispose(); $held = $true; break }
    catch { Start-Sleep -Milliseconds 250 }
}
if (-not $held) { throw "MirrorBridge did not take its mutex within 30 s (exited: $($app.HasExited))" }
$update = Start-Process -FilePath $Setup -PassThru -ArgumentList @(
    "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/UPDATE", "/LOG=$logs\setup-2.log")
Start-Sleep -Seconds 8
if ($update.HasExited) {
    Get-Content "$logs\setup-2.log" | Write-Host
    throw "The update did not wait for the running MirrorBridge (it exited with $($update.ExitCode))"
}
# The app quits (as it does before an update); kill the whole tree, the one-file exe is two processes.
& taskkill /T /F /PID $app.Id | Out-Null
if (-not $update.WaitForExit(120000)) { throw "The update did not finish within 2 minutes of MirrorBridge quitting" }
if ($update.ExitCode -ne 0) {
    Get-Content "$logs\setup-2.log" | Write-Host
    throw "The update exited with $($update.ExitCode)"
}
$out = (& $exe --version | Out-String).Trim()
if ($out -ne "MirrorBridge $version") { throw "After the update the exe printed '$out'" }
Write-Host "2. The update waited for MirrorBridge to quit, then installed"

# 3. Uninstall (unins000.exe hands over to a copy of itself in TEMP and returns at once).
$uninstaller = Join-Path $dir "unins000.exe"
Start-Process -FilePath $uninstaller -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") -Wait
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline -and ((Test-Path $exe) -or (Test-Path $key))) { Start-Sleep -Milliseconds 500 }
if (Test-Path $exe) { throw "Uninstalling left $exe" }
if (Test-Path $key) { throw "Uninstalling left its uninstall entry" }
Write-Host "3. Uninstalled"
