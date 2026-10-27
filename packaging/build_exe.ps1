# Build the standalone AirCast.exe (FFmpeg included) on Windows.
# Usage, from the repo root in PowerShell:  .\packaging\build_exe.ps1
$ErrorActionPreference = "Stop"
python -m pip install --upgrade pip
python -m pip install -r packaging\requirements-build.txt
python -m pip install --no-deps -e .
pyinstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\AirCast.spec
Write-Host "Built dist\AirCast.exe"
