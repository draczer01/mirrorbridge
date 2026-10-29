#!/usr/bin/env bash
# Build MirrorBridge.exe from Linux with Wine and a conda-forge Windows Python (no Windows needed).
#   WINPY=/path/to/windows/python/env  ./packaging/build_exe_wine.sh
# The env needs: pip install -r packaging/requirements-build.txt
# FFmpeg 8.1.1 (packaging/ffmpeg_pin.json) is fetched and checked with this machine's Python first;
# FFMPEG_ZIP=/path/ffmpeg-8.1.1-essentials_build.zip takes a copy already here instead of the download.
# MIRRORBRIDGE_OTHER_FFMPEG=1 bundles MIRRORBRIDGE_FFMPEG or imageio-ffmpeg's instead, as in build_exe.ps1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${WINPY:?set WINPY to the Windows Python folder}"
export WINEDEBUG=-all
# Wine 9 lacks some C runtime functions numpy needs; use the real ucrtbase.dll from the Python env.
export WINEDLLOVERRIDES="ucrtbase=n,b"
export WINEPATH="Z:${WINPY//\//\\};Z:${WINPY//\//\\}\\Library\\bin;Z:${WINPY//\//\\}\\Scripts"
cd "$ROOT"
if [ "${MIRRORBRIDGE_OTHER_FFMPEG:-}" != 1 ]; then
    "${PYTHON:-python3}" -I packaging/fetch_ffmpeg.py ${FFMPEG_ZIP:+--zip "$FFMPEG_ZIP"}
fi
wine "$WINPY/python.exe" packaging/pyinstaller_wine.py --noconfirm --clean --distpath dist --workpath build/pyinstaller \
    packaging/MirrorBridge.spec </dev/null
ls -la dist/MirrorBridge.exe
