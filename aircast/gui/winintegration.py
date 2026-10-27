"""Windows shell integration: start with Windows, desktop shortcut, opening folders."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

_LOGGER = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AirCast"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def launch_command() -> tuple[str, str]:
    """(program, arguments) that start AirCast in the tray."""
    if getattr(sys, "frozen", False):  # PyInstaller AirCast.exe
        return sys.executable, ""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return (pythonw if os.path.exists(pythonw) else sys.executable), "-m aircast"


def _command_line() -> str:
    program, args = launch_command()
    return f'"{program}" {args}'.strip()


# Task Manager's Startup tab records its own on/off switch here; a disabled entry stays in
# the Run key but Windows skips it. First byte odd = disabled.
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _disabled_in_task_manager() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            data, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(data) and bool(data[0] & 1)
    except OSError:
        return False


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    # A value left by a copy of AirCast.exe in another folder does not start this one.
    return value == _command_line() and not _disabled_in_task_manager()


def set_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command_line())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    # Clear a Task Manager "Disabled" mark so turning it on here really turns it on.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError:
        pass


def create_desktop_shortcut(description: str = "AirCast") -> str:
    """Create 'AirCast.lnk' on the user's desktop (OneDrive-redirected desktops too)."""
    program, args = launch_command()
    # Values travel as environment variables, never spliced into the script, so no
    # path or user name can break PowerShell quoting.
    env = {
        **os.environ,
        "AIRCAST_LNK_TARGET": program,
        "AIRCAST_LNK_ARGS": args,
        "AIRCAST_LNK_DIR": os.path.dirname(program),
        "AIRCAST_LNK_DESC": description,
    }
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$p=Join-Path $d 'AirCast.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        "$s.TargetPath=$env:AIRCAST_LNK_TARGET;"
        "$s.Arguments=$env:AIRCAST_LNK_ARGS;"
        "$s.WorkingDirectory=$env:AIRCAST_LNK_DIR;"
        "$s.IconLocation=$env:AIRCAST_LNK_TARGET+',0';"
        "$s.Description=$env:AIRCAST_LNK_DESC;"
        "$s.Save();Write-Output $p"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, timeout=30, creationflags=_NO_WINDOW, check=False, env=env,
    )
    out = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(err or "PowerShell failed")
    return out


def open_folder(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # pylint: disable=no-member
    else:
        subprocess.Popen(["xdg-open", path])  # pylint: disable=consider-using-with
