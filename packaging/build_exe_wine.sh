#!/usr/bin/env bash
# Build AirCast.exe from Linux with Wine and a conda-forge Windows Python (no Windows needed).
#   WINPY=/path/to/windows/python/env  ./packaging/build_exe_wine.sh
# The env needs: pip install -r packaging/requirements-build.txt
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${WINPY:?set WINPY to the Windows Python folder}"
export WINEDEBUG=-all
# Wine 9 lacks some C runtime functions numpy needs; use the real ucrtbase.dll from the Python env.
export WINEDLLOVERRIDES="ucrtbase=n,b"
export WINEPATH="Z:${WINPY//\//\\};Z:${WINPY//\//\\}\\Library\\bin;Z:${WINPY//\//\\}\\Scripts"
cd "$ROOT"
wine "$WINPY/python.exe" packaging/pyinstaller_wine.py --noconfirm --clean --distpath dist --workpath build/pyinstaller \
    packaging/AirCast.spec </dev/null
ls -la dist/AirCast.exe
