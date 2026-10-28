"""Windows shell integration: start with Windows, desktop shortcut, opening folders."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

_LOGGER = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MirrorBridge"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def launch_command() -> tuple[str, str]:
    """(program, arguments) that start MirrorBridge in the tray."""
    if getattr(sys, "frozen", False):  # PyInstaller MirrorBridge.exe
        return sys.executable, ""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return (pythonw if os.path.exists(pythonw) else sys.executable), "-m mirrorbridge"


def _command_line() -> str:
    program, args = launch_command()
    return f'"{program}" {args}'.strip()


# Task Manager's Startup tab records its own on/off switch here; a disabled entry stays in
# the Run key but Windows skips it. First byte odd = disabled.
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def run_value(name: str = VALUE_NAME) -> str | None:
    """The command line a Run value holds; None when there is none."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def disabled_in_task_manager(name: str = VALUE_NAME) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            data, _ = winreg.QueryValueEx(key, name)
            return bool(data) and bool(data[0] & 1)
    except OSError:
        return False


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    # A value left by a copy of MirrorBridge.exe in another folder does not start this one.
    return run_value() == _command_line() and not disabled_in_task_manager()


def set_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    if enabled:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command_line())
        _clear_task_manager_mark(VALUE_NAME)  # so turning it on here really turns it on
    else:
        delete_run_value(VALUE_NAME)


def delete_run_value(name: str) -> None:
    """Remove a Run value, and its Task Manager mark with it."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
    _clear_task_manager_mark(name)


def _clear_task_manager_mark(name: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def create_desktop_shortcut(description: str = "MirrorBridge") -> str:
    """Create 'MirrorBridge.lnk' on the user's desktop (OneDrive-redirected desktops too)."""
    program, args = launch_command()
    # Values travel as environment variables, never spliced into the script, so no
    # path or user name can break PowerShell quoting.
    env = {
        **os.environ,
        "MIRRORBRIDGE_LNK_TARGET": program,
        "MIRRORBRIDGE_LNK_ARGS": args,
        "MIRRORBRIDGE_LNK_DIR": os.path.dirname(program),
        "MIRRORBRIDGE_LNK_DESC": description,
    }
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$p=Join-Path $d 'MirrorBridge.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        "$s.TargetPath=$env:MIRRORBRIDGE_LNK_TARGET;"
        "$s.Arguments=$env:MIRRORBRIDGE_LNK_ARGS;"
        "$s.WorkingDirectory=$env:MIRRORBRIDGE_LNK_DIR;"
        "$s.IconLocation=$env:MIRRORBRIDGE_LNK_TARGET+',0';"
        "$s.Description=$env:MIRRORBRIDGE_LNK_DESC;"
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


def open_device_manager() -> None:
    """Device Manager, where a disabled virtual display adapter is turned back on. Through the
    shell, so Windows can offer the elevation that enabling a device needs. By full path: the
    shell would open a devmgmt.msc in the current folder (often Downloads) first."""
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    os.startfile(os.path.join(system32, "devmgmt.msc"))  # type: ignore[attr-defined]  # pylint: disable=no-member
