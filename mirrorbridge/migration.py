"""Bring over what AirCast, as MirrorBridge was called up to version 0.2, left on this PC.

* Its settings and pairings: copied from %APPDATA%\\AirCast (~/.config/AirCast) the first time
  this process asks for its own folder (config.app_data_dir), so before anything reads them and
  the TV stays paired. A file MirrorBridge already has is never replaced, and the old folder is
  only read: an AirCast.exe that is still around keeps working with it.
* Its Start with Windows entry: moved over once, by the tray app (migrate_autostart).

AirCast.exe itself and its desktop shortcut are left alone; README.md tells people to delete them.
"""

from __future__ import annotations

import logging
import ntpath
import os
import shutil
import sys
import threading
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

OLD_NAME = "AirCast"
# All AirCast 0.2 kept in its folder that is worth keeping. Its lock file and log stay behind,
# and it had no Extend screen, so it never wrote a vdd_state.json.
OLD_FILES = ("settings.json", "pairings.json")

_lock = threading.Lock()
_checked: set[Path] = set()  # folders this process has already brought up to date
_copied: list[Path] = []  # what that copied, for the GUI's log, which starts later


def copy_old_files(old_dir: Path, new_dir: Path) -> list[Path]:
    """Copy each of OLD_FILES that new_dir lacks from old_dir; return the files copied.

    A file that cannot be copied is logged and skipped, so the others still come over.
    """
    copied: list[Path] = []
    if not old_dir.is_dir():
        return copied
    for name in OLD_FILES:
        src, dest = old_dir / name, new_dir / name
        try:
            if dest.exists() or not src.is_file():
                continue
            if _copy_new(src, dest):
                copied.append(src)
        except OSError as ex:
            _LOGGER.warning("Could not copy %s to %s: %s", src, new_dir, ex)
    return copied


def _copy_new(src: Path, dest: Path) -> bool:
    """Copy src to dest unless dest exists, even one that appears meanwhile, and never leave a
    half-written dest behind. True if it copied."""
    part = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    try:
        shutil.copy2(src, part)
        try:
            # Neither replaces an existing dest: os.rename does not on Windows, os.link never does.
            if sys.platform == "win32":
                os.rename(part, dest)
            else:
                os.link(part, dest)
        except FileExistsError:
            return False
        return True
    finally:
        part.unlink(missing_ok=True)


def bring_over(new_dir: Path) -> None:
    """Copy AirCast's settings and pairings into new_dir, if it lacks them.

    config.app_data_dir() calls this every time; it works once per folder and process, and
    never raises: at worst MirrorBridge starts with its own defaults.
    """
    with _lock:
        if new_dir in _checked:
            return
        _checked.add(new_dir)
        old_dir = new_dir.parent / OLD_NAME
        try:
            files = copy_old_files(old_dir, new_dir)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Could not bring over the settings in %s: %s", old_dir, ex)
            return
        if files:
            _copied.extend(files)
            _LOGGER.info("Copied %s from %s, where %s kept them", ", ".join(f.name for f in files),
                         old_dir, OLD_NAME)


def copied() -> list[Path]:
    """The files bring_over copied in this process, newest last."""
    with _lock:
        return list(_copied)


def is_old_autostart(command: str) -> bool:
    """Whether a Run value's command line starts AirCast: AirCast.exe, or "pythonw.exe -m aircast"
    from source. Another program that happens to use the same value name is not ours to touch."""
    command = command.strip()
    if command.startswith('"'):
        program, _, args = command[1:].partition('"')
    else:
        program, _, args = command.partition(" ")
    if ntpath.basename(program).lower() == f"{OLD_NAME.lower()}.exe":
        return True
    return args.split()[:2] == ["-m", OLD_NAME.lower()]


def migrate_autostart() -> bool:
    """Move AirCast's Start with Windows entry over to MirrorBridge.

    AirCast's Run value is removed, with its Task Manager on/off mark, and MirrorBridge (this
    program) starts with Windows instead, unless the user had turned AirCast off in Task Manager.
    A MirrorBridge entry that already exists is kept as it is. Returns False only when the
    registry could not be changed, so that the next start tries again; never raises.
    """
    if sys.platform != "win32":
        return True
    from .gui import winintegration

    try:
        command = winintegration.run_value(OLD_NAME)
        if command is None or not is_old_autostart(command):
            return True
        enabled = not winintegration.disabled_in_task_manager(OLD_NAME)
        if enabled and winintegration.run_value() is None:
            winintegration.set_autostart(True)  # first, so a failure below leaves both, not neither
        winintegration.delete_run_value(OLD_NAME)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not move %s's Start with Windows entry: %s", OLD_NAME, ex)
        return False
    if enabled:
        _LOGGER.info("Start with Windows: %s's entry (%s) now starts %s", OLD_NAME, command,
                     winintegration.VALUE_NAME)
    else:
        _LOGGER.info("Start with Windows: removed %s's entry (%s), which was turned off in Task Manager",
                     OLD_NAME, command)
    return True
