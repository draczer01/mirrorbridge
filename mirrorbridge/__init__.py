"""MirrorBridge: cast a Windows screen (and system audio) to Apple TV and other AirPlay TVs."""

import sys

__version__ = "0.3.0"
APP_NAME = "MirrorBridge"
# The command people type: the exe itself, or the command that installing from source adds.
CLI_NAME = f"{APP_NAME}.exe" if getattr(sys, "frozen", False) else "mirrorbridge"
