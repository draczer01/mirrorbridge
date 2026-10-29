# Build the standalone MirrorBridge.exe (FFmpeg included) on Windows.
# Usage, from the repo root in PowerShell:
#   .\packaging\build_exe.ps1                       # downloads FFmpeg 8.1.1 from GitHub the first time (109 MB)
#   .\packaging\build_exe.ps1 -FFmpegZip C:\path\ffmpeg-8.1.1-essentials_build.zip   # a copy already here
#   .\packaging\build_exe.ps1 -AllowOtherFFmpeg     # bundle $env:MIRRORBRIDGE_FFMPEG, or imageio-ffmpeg's 7.1
# Whichever ffmpeg.exe goes inside is run first: unless it says it is the pinned version
# (packaging\ffmpeg_pin.json) and lists the scale_d3d11 filter, the build stops (with
# -AllowOtherFFmpeg it only warns). Nothing here redirects a program's standard error: with "Stop"
# below, Windows PowerShell would then stop at the first INFO line PyInstaller writes there.
param(
    [string]$FFmpegZip = "",
    [switch]$AllowOtherFFmpeg
)
$ErrorActionPreference = "Stop"
python -m pip install --upgrade pip
python -m pip install -r packaging\requirements-build.txt
python -m pip install --no-deps -e .

$pin = Get-Content -Raw packaging\ffmpeg_pin.json | ConvertFrom-Json
$optIn = $env:MIRRORBRIDGE_OTHER_FFMPEG
try {
    if ($AllowOtherFFmpeg) {
        if ($FFmpegZip) { throw "-FFmpegZip gives the pinned FFmpeg and -AllowOtherFFmpeg another one: use only one" }
        $env:MIRRORBRIDGE_OTHER_FFMPEG = "1"
    } else {
        Remove-Item Env:MIRRORBRIDGE_OTHER_FFMPEG -ErrorAction SilentlyContinue
        $fetch = @("-I", "packaging\fetch_ffmpeg.py")
        if ($FFmpegZip) { $fetch += @("--zip", $FFmpegZip) }
        $fetched = python @fetch
        if ($LASTEXITCODE -ne 0) { throw "packaging\fetch_ffmpeg.py could not get FFmpeg $($pin.version) (see above)" }
        Write-Host "FFmpeg $($pin.version): $fetched"
    }

    # What MirrorBridge.spec will bundle, run to see what it is.
    $ffmpeg = python -I packaging\fetch_ffmpeg.py --bundled
    if ($LASTEXITCODE -ne 0) { throw "packaging\fetch_ffmpeg.py --bundled failed (see above)" }
    $ffmpeg = "$($ffmpeg | Select-Object -Last 1)".Trim()
    $version = & $ffmpeg -hide_banner -version | Out-String
    if ($LASTEXITCODE -ne 0) { throw "$ffmpeg -version exited with $LASTEXITCODE" }
    $version = ($version.Trim() -split "\r?\n")[0]
    $filters = & $ffmpeg -hide_banner -filters | Out-String
    if ($LASTEXITCODE -ne 0) { throw "$ffmpeg -filters exited with $LASTEXITCODE" }
    $problems = @()
    if ($version -notmatch ('^ffmpeg version n?' + [regex]::Escape($pin.version) + '(-|\s|$)')) {
        $problems += "it is not FFmpeg $($pin.version) ('$version')"
    }
    if ($filters -notmatch '\bscale_d3d11\b') { $problems += "it has no scale_d3d11 filter, so no scaling on the GPU" }
    if ($problems) {
        $message = "The FFmpeg that would go inside MirrorBridge.exe, ${ffmpeg}: " + ($problems -join "; ")
        if (-not $AllowOtherFFmpeg) { throw "$message. Pass -AllowOtherFFmpeg to build with it anyway." }
        Write-Warning $message
    }
    Write-Host "FFmpeg inside MirrorBridge.exe: $ffmpeg ($version)"

    pyinstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\MirrorBridge.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller exited with $LASTEXITCODE" }
} finally {
    # Leave the shell as it was: an opt-in left behind would let a later build bundle another FFmpeg.
    if ($null -eq $optIn) { Remove-Item Env:MIRRORBRIDGE_OTHER_FFMPEG -ErrorAction SilentlyContinue } else { $env:MIRRORBRIDGE_OTHER_FFMPEG = $optIn }
}
Write-Host "Built dist\MirrorBridge.exe"
