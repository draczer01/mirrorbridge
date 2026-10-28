"""Moving over from AirCast, the app's name up to 0.2.1: settings, pairings and Start with Windows.

Losing the TV's pairing is the worst that can happen here, so the copies are checked byte for byte,
and the AirCast folder must come out exactly as it went in.
"""

import asyncio
import json
import logging
import shutil
import sys
from types import SimpleNamespace

import pytest

from mirrorbridge import config, devices, migration
from mirrorbridge.config import Settings, app_data_dir, credentials_file

OLD_SETTINGS = {"latency_mode": "smooth", "last_device_id": "AA:BB:CC:DD:EE:FF", "tray_hint_shown": True,
                "manual_hosts": ["192.168.1.40"], "a_setting_0.2.1_had": 1}


def _snapshot(folder):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(folder.iterdir())}


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """%APPDATA% (~/.config off Windows) in tmp_path, holding what AirCast 0.2.1 left there."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = tmp_path / "AirCast"
    old.mkdir()
    (old / "settings.json").write_text(json.dumps(OLD_SETTINGS, indent=2), encoding="utf-8")
    (old / "pairings.json").write_text('{"version": 1, "devices": ["the TV\'s keys"]}', encoding="utf-8")
    (old / "aircast.log").write_text("12:00:00 INFO    aircast.gui.app: Bye\n", encoding="utf-8")
    (old / "aircast.lock").write_text("4242\nAirCast\nPC\n", encoding="utf-8")
    return SimpleNamespace(old=old, new=tmp_path / "MirrorBridge", before=_snapshot(old))


def test_first_start_copies_settings_and_pairings_and_leaves_aircast_alone(folders, caplog):
    with caplog.at_level(logging.INFO, logger="mirrorbridge.migration"):
        assert app_data_dir() == folders.new
    for name in ("settings.json", "pairings.json"):
        assert (folders.new / name).read_bytes() == (folders.old / name).read_bytes()
    assert sorted(p.name for p in folders.new.iterdir()) == ["pairings.json", "settings.json"]  # no log, no lock
    assert _snapshot(folders.old) == folders.before
    assert migration.copied()[-2:] == [folders.old / "settings.json", folders.old / "pairings.json"]
    assert f"Copied settings.json, pairings.json from {folders.old}" in caplog.text


def test_settings_and_pairings_are_read_from_the_copy(folders):
    """Settings.load and credentials_file are the first to ask for the folder: they get the copies."""
    settings = Settings.load()
    assert (settings.latency_mode, settings.last_device_id, settings.manual_hosts) == ("smooth", "AA:BB:CC:DD:EE:FF",
                                                                                     ["192.168.1.40"])
    assert credentials_file().read_bytes() == (folders.old / "pairings.json").read_bytes()


async def test_the_tv_stays_paired(folders):
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import Protocol
    from pyatv.storage.file_storage import FileStorage

    loop = asyncio.get_running_loop()
    tv = AppleTV("192.168.1.40", "Sala familiar")
    tv.add_service(ManualService("AA:BB:CC:DD:EE:FF", Protocol.AirPlay, 7000, {}))
    paired = FileStorage(str(folders.old / "pairings.json"), loop)  # as AirCast saved it
    (folders.old / "pairings.json").unlink()
    await paired.load()
    (await paired.get_settings(tv)).protocols.airplay.credentials = "the TV's keys"
    await paired.save()

    manager = devices.DeviceManager(loop)  # the first to ask for the folder
    await manager._ensure_loaded()  # pylint: disable=protected-access
    assert (await manager.storage.get_settings(tv)).protocols.airplay.credentials == "the TV's keys"


def test_a_file_mirrorbridge_already_has_is_never_replaced(folders):
    folders.new.mkdir()
    (folders.new / "settings.json").write_text('{"latency_mode": "low"}', encoding="utf-8")
    app_data_dir()
    assert (folders.new / "settings.json").read_text(encoding="utf-8") == '{"latency_mode": "low"}'
    assert (folders.new / "pairings.json").read_bytes() == (folders.old / "pairings.json").read_bytes()
    assert _snapshot(folders.old) == folders.before


def test_it_runs_once_per_process_and_then_only_fills_in_what_is_missing(folders, monkeypatch):
    app_data_dir()
    (folders.new / "settings.json").write_text('{"latency_mode": "low"}', encoding="utf-8")  # changed in the app
    (folders.new / "pairings.json").unlink()
    app_data_dir()  # same process: nothing is checked again
    assert not (folders.new / "pairings.json").exists()

    monkeypatch.setattr(migration, "_checked", set())  # the next start
    app_data_dir()
    assert (folders.new / "settings.json").read_text(encoding="utf-8") == '{"latency_mode": "low"}'
    assert (folders.new / "pairings.json").read_bytes() == (folders.old / "pairings.json").read_bytes()
    assert _snapshot(folders.old) == folders.before


def test_without_aircast_there_is_nothing_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert list(app_data_dir().iterdir()) == []
    assert Settings.load() == Settings()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MirrorBridge"]  # no AirCast folder made


def test_a_file_that_cannot_be_copied_stops_neither_the_others_nor_the_app(folders, monkeypatch, caplog):
    copy2 = shutil.copy2

    def unreadable_settings(src, dst):
        if src.name == "settings.json":
            raise PermissionError(13, "Access is denied", str(src))
        return copy2(src, dst)

    monkeypatch.setattr(migration.shutil, "copy2", unreadable_settings)
    with caplog.at_level(logging.WARNING, logger="mirrorbridge.migration"):
        assert app_data_dir() == folders.new
    assert "Access is denied" in caplog.text
    assert sorted(p.name for p in folders.new.iterdir()) == ["pairings.json"]  # and no half-made settings.json
    assert Settings.load() == Settings()


def test_any_other_failure_only_costs_the_old_settings(folders, monkeypatch, caplog):
    def broken(old_dir, new_dir):
        raise RuntimeError("something nobody thought of")

    monkeypatch.setattr(migration, "copy_old_files", broken)
    with caplog.at_level(logging.WARNING, logger="mirrorbridge.migration"):
        assert app_data_dir() == folders.new
    assert "something nobody thought of" in caplog.text
    assert Settings.load() == Settings()


def test_an_old_folder_that_is_a_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "AirCast").write_text("not a folder", encoding="utf-8")
    assert list(app_data_dir().iterdir()) == []


def test_a_copy_never_replaces_a_file_that_appeared_meanwhile(tmp_path):
    """Another MirrorBridge starting at the same moment may have copied (and changed) it already."""
    (tmp_path / "old.json").write_text("old", encoding="utf-8")
    (tmp_path / "new.json").write_text("already here", encoding="utf-8")
    assert not migration._copy_new(tmp_path / "old.json", tmp_path / "new.json")  # pylint: disable=protected-access
    assert (tmp_path / "new.json").read_text(encoding="utf-8") == "already here"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["new.json", "old.json"]  # no .part left over


def test_settings_from_0_2_1_load_with_the_new_ones_at_their_defaults(folders):
    settings = Settings.load()
    assert settings.extend_screen is False and settings.old_autostart_checked is False
    assert settings.tray_hint_shown is True


# -- Start with Windows ---------------------------------------------------------------------------

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
ON, OFF = b"\x02" + bytes(11), b"\x03" + bytes(11)  # Task Manager's Startup tab: odd first byte = disabled
AIRCAST_EXE = r'"C:\Users\Humberto\Downloads\AirCast.exe"'


class FakeWinreg:
    """The few winreg calls winintegration makes, on HKEY_CURRENT_USER only, held in `keys`."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE = 0x20019, 0x0002
    REG_SZ, REG_BINARY = 1, 3

    def __init__(self, keys):
        self.keys = keys
        self.fail_writes = False

    class Key:
        def __init__(self, values, writable):
            self.values, self.writable = values, writable

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def OpenKey(self, hive, path, reserved=0, access=KEY_READ):  # noqa: N802
        assert hive == self.HKEY_CURRENT_USER and reserved == 0
        if path not in self.keys:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return self.Key(self.keys[path], bool(access & self.KEY_SET_VALUE))

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        value = key.values[name]
        return value, self.REG_BINARY if isinstance(value, bytes) else self.REG_SZ

    def _writing(self, key):
        if not key.writable or self.fail_writes:
            raise PermissionError(5, "Access is denied")

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self._writing(key)
        key.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        self._writing(key)
        if name not in key.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del key.values[name]


@pytest.fixture
def registry(monkeypatch):
    """A Windows registry for winintegration (on any OS) that starts out empty."""
    fake = FakeWinreg({RUN: {"OneDrive": r'"C:\OneDrive.exe" /background'}, APPROVED: {"OneDrive": ON}})
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    return fake


def _mirrorbridge_command():
    from mirrorbridge.gui import winintegration

    return winintegration._command_line()  # pylint: disable=protected-access


@pytest.mark.parametrize("command", [AIRCAST_EXE, r'"C:\Python312\pythonw.exe" -m aircast'])
def test_aircast_starting_with_windows_becomes_mirrorbridge_starting_with_windows(registry, command, caplog):
    from mirrorbridge.gui import winintegration

    registry.keys[RUN]["AirCast"] = command
    registry.keys[APPROVED]["AirCast"] = ON
    with caplog.at_level(logging.INFO, logger="mirrorbridge.migration"):
        assert migration.migrate_autostart()
    assert registry.keys[RUN] == {"OneDrive": r'"C:\OneDrive.exe" /background', "MirrorBridge": _mirrorbridge_command()}
    assert registry.keys[APPROVED] == {"OneDrive": ON}
    assert winintegration.autostart_enabled()
    assert "now starts MirrorBridge" in caplog.text


def test_aircast_turned_off_in_task_manager_stays_off(registry):
    from mirrorbridge.gui import winintegration

    registry.keys[RUN]["AirCast"] = AIRCAST_EXE
    registry.keys[APPROVED]["AirCast"] = OFF
    assert migration.migrate_autostart()
    assert "AirCast" not in registry.keys[RUN] and "MirrorBridge" not in registry.keys[RUN]
    assert registry.keys[APPROVED] == {"OneDrive": ON}
    assert not winintegration.autostart_enabled()


def test_another_programs_aircast_entry_is_left_alone(registry):
    registry.keys[RUN]["AirCast"] = r'"C:\Program Files\AirCast Pro\castpro.exe" --tray'
    registry.keys[APPROVED]["AirCast"] = ON
    before = json.dumps(registry.keys, default=repr)
    assert migration.migrate_autostart()
    assert json.dumps(registry.keys, default=repr) == before


def test_without_an_aircast_entry_nothing_changes(registry):
    before = json.dumps(registry.keys, default=repr)
    assert migration.migrate_autostart()
    assert json.dumps(registry.keys, default=repr) == before


def test_a_mirrorbridge_entry_that_exists_is_kept_as_it_is(registry):
    """Turned off in Task Manager on purpose, say: moving AirCast's entry must not turn it back on."""
    registry.keys[RUN].update({"AirCast": AIRCAST_EXE, "MirrorBridge": r'"D:\Apps\MirrorBridge.exe"'})
    registry.keys[APPROVED].update({"AirCast": ON, "MirrorBridge": OFF})
    assert migration.migrate_autostart()
    assert registry.keys[RUN]["MirrorBridge"] == r'"D:\Apps\MirrorBridge.exe"' and "AirCast" not in registry.keys[RUN]
    assert registry.keys[APPROVED] == {"OneDrive": ON, "MirrorBridge": OFF}


def test_a_registry_that_cannot_be_changed_is_tried_again_next_start(registry, caplog):
    registry.keys[RUN]["AirCast"] = AIRCAST_EXE
    registry.fail_writes = True
    with caplog.at_level(logging.WARNING, logger="mirrorbridge.migration"):
        assert not migration.migrate_autostart()
    assert registry.keys[RUN]["AirCast"] == AIRCAST_EXE and "MirrorBridge" not in registry.keys[RUN]
    assert "Access is denied" in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows path")
def test_off_windows_there_is_no_registry_to_touch(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", None)  # importing it would fail
    assert migration.migrate_autostart()


@pytest.mark.parametrize("command, old", [
    (AIRCAST_EXE, True),
    (r"C:\Tools\AIRCAST.EXE", True),
    (r'"C:\Users\Humberto\AppData\Local\Programs\Python\Python312\pythonw.exe" -m aircast', True),
    (r'"C:\Python312\pythonw.exe" -m aircast gui', True),
    (r'"C:\Python312\pythonw.exe" -m aircastle', False),
    (r'"C:\Program Files\AirCast Pro\castpro.exe" --tray', False),
    (r'"C:\Tools\notaircast.exe"', False),
    ("", False),
])
def test_only_aircasts_own_command_counts_as_aircast(command, old):
    assert migration.is_old_autostart(command) is old


def test_settings_remember_that_the_entry_was_moved(tmp_path):
    path = tmp_path / "settings.json"
    Settings(old_autostart_checked=True).save(path)
    assert Settings.load(path).old_autostart_checked


def test_config_hands_its_folder_to_the_migration(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(migration, "bring_over", seen.append)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.app_data_dir() == tmp_path / "MirrorBridge"
    assert seen == [tmp_path / "MirrorBridge"]
