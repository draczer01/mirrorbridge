@echo off
rem Run AirCast from source (most people should just use AirCast.exe). Creates a local
rem Python environment on first run, then starts AirCast in the tray.
setlocal
cd /d "%~dp0"
where py >nul 2>nul || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
    echo First run: setting up AirCast...
    py -3 -m venv .venv || (pause & exit /b 1)
    .venv\Scripts\python -m pip install --upgrade pip >nul
    .venv\Scripts\python -m pip install -e . || (pause & exit /b 1)
)
rem FFmpeg comes with the imageio-ffmpeg package, so nothing else to install.
start "" .venv\Scripts\pythonw.exe -m aircast %*
