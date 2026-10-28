"""Extend screen: the virtual display driver protocol, its bookkeeping, and the cast session's use
of it. Everything runs without the driver (fakes stand in for it), on Linux and on Windows."""

import asyncio
import ctypes
import hashlib
import io
import json
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from mirrorbridge import ffmpeg, vdisplay
from mirrorbridge.config import Settings
from mirrorbridge.vdisplay import DriverStatus, VddError

FILE_READ_ACCESS, FILE_WRITE_ACCESS, METHOD_BUFFERED = 1, 2, 0
VIRTUAL = r"\\.\DISPLAY9"
MAIN_DISPLAY = r"\\.\DISPLAY1"
MAIN, RIGHT_OF_MAIN = (0, 0, 1920, 1080), (1920, 0, 3840, 1080)  # desktop rectangles


def ctl_code(device: int, function: int, method: int, access: int) -> int:
    return (device << 16) | (access << 14) | (function << 2) | method


def test_ioctl_codes_are_the_drivers_ctl_codes():
    both = FILE_READ_ACCESS | FILE_WRITE_ACCESS
    assert vdisplay.IOCTL_VDD_ADD == ctl_code(0x22, 0x801, METHOD_BUFFERED, both)
    assert vdisplay.IOCTL_VDD_REMOVE == ctl_code(0x22, 0x802, METHOD_BUFFERED, FILE_WRITE_ACCESS)
    assert vdisplay.IOCTL_VDD_UPDATE == ctl_code(0x22, 0x803, METHOD_BUFFERED, FILE_WRITE_ACCESS)
    assert vdisplay.IOCTL_VDD_VERSION == ctl_code(0x22, 0x804, METHOD_BUFFERED, both)


def test_remove_puts_the_index_in_the_second_byte():
    assert vdisplay.remove_payload(0) == b"\x00\x00"
    assert vdisplay.remove_payload(3) == b"\x00" + bytes([3])
    assert vdisplay.remove_payload(15) == b"\x00\x0f"


def test_uid_comes_from_parsec_monitors_only():
    parsec = r"\\?\DISPLAY#PSCCDD0#5&1a2b&0&UID257#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
    assert vdisplay.parse_uid(parsec) == 257
    assert vdisplay.parse_uid(parsec.lower()) == 257
    assert vdisplay.parse_uid(r"\\?\DISPLAY#GSM5B09#5&2c03&0&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}") is None
    assert vdisplay.parse_uid(r"\\?\DISPLAY#PSCCDD0#5&1a2b&0#{e6f07b5f}") is None


def test_mode_falls_back_when_4k_is_missing():
    offered = {(1920, 1080, 60), (1920, 1080, 30), (1280, 720, 60)}
    assert vdisplay.pick_mode((3840, 2160, 60), offered) is None  # keep the default 1080p
    assert vdisplay.pick_mode((1280, 720, 60), offered) == (1280, 720, 60)
    assert vdisplay.pick_mode((1920, 1080, 144), offered) == (1920, 1080, 60)


def test_every_extend_mode_is_a_driver_preset():
    assert set(vdisplay.EXTEND_MODES) == {"1080p", "720p", "4k"}
    assert vdisplay.EXTEND_MODES["4k"] == (3840, 2160, 60)
    assert vdisplay.EXTEND_MODES["720p"] == (1280, 720, 60)  # AirParrot's forced 720p


def test_errors_carry_a_code_and_the_driver_status():
    assert VddError("driver:disabled").status is DriverStatus.DISABLED
    assert VddError("lost").status is None
    assert "Win+P" in str(VddError("not_attached"))
    assert str(VddError("download", "timed out")).endswith(": timed out")


def test_every_string_has_english_and_spanish():
    from mirrorbridge.gui import i18n

    for key, entry in i18n._STRINGS.items():  # pylint: disable=protected-access
        assert entry.get("en") and entry.get("es"), key


def test_every_problem_has_english_and_spanish_text():
    # mirrorbridge.gui.extend shows Qt dialogs; Qt also fails to load without libEGL (ImportError)
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from mirrorbridge.gui import extend, i18n

    for status in DriverStatus:
        if status is not DriverStatus.OK:
            assert extend.STATUS_KEYS[status] in i18n._STRINGS  # pylint: disable=protected-access
    for key in list(extend.STATUS_KEYS.values()) + list(extend._ERROR_KEYS.values()):  # pylint: disable=protected-access
        assert key in i18n._STRINGS  # pylint: disable=protected-access
    i18n.set_language("es")
    try:
        assert extend.error_text(VddError("lost")).startswith("La pantalla virtual se cerró")
        assert extend.error_text(VddError("driver:restart_required")) == i18n.tr("vdd_st_restart")
        assert extend.error_text(RuntimeError("other")) == "other"
    finally:
        i18n.set_language("en")


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows path")
def test_driver_calls_fail_cleanly_off_windows():
    assert not vdisplay.available()
    with pytest.raises(VddError) as info:
        vdisplay.driver_status()
    assert info.value.code == "unsupported"


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 calls")
def test_win32_bindings_answer_without_the_driver():
    """Only queries: nothing here opens the driver or adds a monitor."""
    from mirrorbridge import winmonitors

    assert isinstance(vdisplay.driver_status(), DriverStatus)
    assert isinstance(vdisplay.interface_paths(), list)
    assert isinstance(vdisplay.vdd_sources(), list)
    assert isinstance(winmonitors.dxgi_map(), dict)


# -- driver status and IOCTLs, with fake DLLs ---------------------------------------------------
# _dlls() hands out vdisplay._api, so a namespace of fake functions stands in for the Win32 DLLs
# (the real ones are never called, on Windows either). ctypes.byref(x)._obj is x.


class FakeSetupApi:
    """setupapi and cfgmgr32 over Display-class devices: (hardware IDs, devnode status, problem)."""

    def __init__(self, devices) -> None:
        self.devices = devices
        self.destroyed = False

    def SetupDiGetClassDevsW(self, *_args):
        return 0x1234

    def SetupDiEnumDeviceInfo(self, _devs, index, info):
        if index >= len(self.devices):
            return False
        info._obj.DevInst = index
        return True

    def SetupDiGetDeviceRegistryPropertyW(self, _devs, info, _prop, _regtype, buf, _size, _needed):
        for i, char in enumerate("\0".join(self.devices[info._obj.DevInst][0]) + "\0\0"):  # REG_MULTI_SZ
            buf[i] = char
        return True

    def SetupDiDestroyDeviceInfoList(self, _devs):
        self.destroyed = True
        return True

    def CM_Get_DevNode_Status(self, status, problem, devinst, _flags):
        _ids, status._obj.value, problem._obj.value = self.devices[devinst]
        return vdisplay.CR_SUCCESS


OTHER_GPU = (["PCI\\VEN_10DE&DEV_2484", "PCI\\VEN_10DE"], 0x8, 0)
DN_STARTED, DN_DRIVER_LOADED, DN_HAS_PROBLEM = 0x8, 0x2, 0x400


@pytest.mark.parametrize("status, problem, expected", [
    (DN_STARTED | DN_DRIVER_LOADED, 0, DriverStatus.OK),
    (DN_HAS_PROBLEM, 0x0E, DriverStatus.RESTART_REQUIRED),  # CM_PROB_NEED_RESTART
    (DN_HAS_PROBLEM, 0x16, DriverStatus.DISABLED),  # CM_PROB_DISABLED
    (DN_HAS_PROBLEM, 0x1D, DriverStatus.DISABLED),  # CM_PROB_HARDWARE_DISABLED
    (DN_HAS_PROBLEM, 0x20, DriverStatus.DISABLED),  # CM_PROB_DISABLED_SERVICE
    (DN_HAS_PROBLEM, 0x2B, DriverStatus.DRIVER_ERROR),  # CM_PROB_FAILED_POST_START
    (DN_HAS_PROBLEM, 0x0A, DriverStatus.UNKNOWN_PROBLEM),  # CM_PROB_FAILED_START
    (0, 0, DriverStatus.UNKNOWN_PROBLEM),  # present, but neither loaded nor started
])
def test_driver_status_reads_the_parsec_devnode(monkeypatch, status, problem, expected):
    fake = FakeSetupApi([OTHER_GPU, (["ROOT\\PARSEC\\VDA"], status, problem)])  # IDs are case-insensitive
    monkeypatch.setattr(vdisplay, "_api", types.SimpleNamespace(setupapi=fake, cfgmgr32=fake))
    assert vdisplay.driver_status() is expected and fake.destroyed


def test_driver_status_without_the_parsec_device_is_not_installed(monkeypatch):
    fake = FakeSetupApi([OTHER_GPU])
    monkeypatch.setattr(vdisplay, "_api", types.SimpleNamespace(setupapi=fake, cfgmgr32=fake))
    assert vdisplay.driver_status() is DriverStatus.NOT_INSTALLED and fake.destroyed


class FakeKernel32:
    """DeviceIoControl, then each GetOverlappedResultEx, scripted as (returned, last error)."""

    def __init__(self, call, *waits, value=0x2D) -> None:
        self.call, self.waits, self.value = call, list(waits), value
        self.error = 0
        self.log: list = []
        self.closed: list = []
        self._out = None

    def CreateEventW(self, *_args):
        return 0x99

    def DeviceIoControl(self, _handle, _code, _inbuf, _size, out, _outsize, _returned, _ov):
        self.log.append("DeviceIoControl")
        self._out = out._obj
        ok, self.error = self.call
        return self._done(ok)

    def GetOverlappedResultEx(self, _handle, _ov, _done, timeout_ms, _alertable):
        self.log.append(("wait", timeout_ms))
        ok, self.error = self.waits.pop(0)
        return self._done(ok)

    def _done(self, ok):
        if ok:
            self._out.value = self.value
        return ok

    def CancelIoEx(self, _handle, _ov):
        self.log.append("CancelIoEx")
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


PENDING, TIMED_OUT, DONE = (False, 997), (False, 258), (True, 0)


@pytest.mark.parametrize("script, result, log, leaked", [
    ((DONE, DONE), 0x2D, ["DeviceIoControl", ("wait", 1000)], 0),  # finished at once
    ((PENDING, DONE), 0x2D, ["DeviceIoControl", ("wait", 1000)], 0),
    ((PENDING, (False, 31)), None, ["DeviceIoControl", ("wait", 1000)], 0),  # finished, with an error
    # Failed at once (the device is gone): nothing was queued, so no wait, no cancel and no leak.
    (((False, 50),), None, ["DeviceIoControl"], 0),
    (((False, 22),), None, ["DeviceIoControl"], 0),
    ((PENDING, TIMED_OUT, (False, 995)), None, ["DeviceIoControl", ("wait", 1000), "CancelIoEx", ("wait", 2000)], 0),
    # Finished before the cancel took effect: a slow ADD still counts.
    ((PENDING, TIMED_OUT, DONE), 0x2D, ["DeviceIoControl", ("wait", 1000), "CancelIoEx", ("wait", 2000)], 0),
    # Never finishes: its buffers and event are kept for the kernel.
    ((PENDING, TIMED_OUT, TIMED_OUT), None, ["DeviceIoControl", ("wait", 1000), "CancelIoEx", ("wait", 2000)], 1),
])
def test_ioctl_waits_are_bounded_and_never_free_busy_buffers(monkeypatch, script, result, log, leaked):
    k32 = FakeKernel32(*script)
    monkeypatch.setattr(vdisplay, "_api", types.SimpleNamespace(kernel32=k32))
    monkeypatch.setattr(vdisplay, "_LEAKED", [])
    monkeypatch.setattr(ctypes, "get_last_error", lambda: k32.error, raising=False)
    assert vdisplay.VddDevice(5).ioctl(vdisplay.IOCTL_VDD_ADD, timeout_ms=1000) == result
    assert k32.log == log and len(vdisplay._LEAKED) == leaked  # pylint: disable=protected-access
    assert k32.closed == ([] if leaked else [0x99])


# -- finding the new monitor and extending the desktop ----------------------------------------


def test_extended_means_its_own_spot_next_to_the_pcs_screen():
    parsec = {VIRTUAL}
    assert vdisplay.is_extended(VIRTUAL, {MAIN_DISPLAY: MAIN, VIRTUAL: RIGHT_OF_MAIN}, parsec)
    assert vdisplay.is_extended(VIRTUAL, {MAIN_DISPLAY: MAIN, VIRTUAL: (-1280, 0, 0, 720)}, parsec)
    assert not vdisplay.is_extended(VIRTUAL, {MAIN_DISPLAY: MAIN, VIRTUAL: MAIN}, parsec)  # Duplicate
    assert not vdisplay.is_extended(VIRTUAL, {MAIN_DISPLAY: MAIN, VIRTUAL: (0, 0, 1280, 720)}, parsec)
    assert not vdisplay.is_extended(VIRTUAL, {VIRTUAL: MAIN}, parsec)  # Second screen only
    assert vdisplay.is_extended(VIRTUAL, {VIRTUAL: MAIN}, parsec, own_screen=False)  # a PC with no screen
    assert not vdisplay.is_extended(VIRTUAL, {MAIN_DISPLAY: MAIN}, parsec)  # not on the desktop


class FakeDesktop:
    """What Windows shows while wait_for_source polls for monitor index 2 (UID 258): the desktop
    rectangles, and whether the monitor is on a source yet. Win+P "Extend" puts it right of the
    main screen (unless `obeys` is False)."""

    def __init__(self, rects, on_a_source=True, known=True, obeys=True) -> None:
        self.rects, self.on_a_source, self.known, self.obeys = dict(rects), on_a_source, known, obeys
        self.extend_calls = 0

    def sources(self):
        return [vdisplay.VddSource(VIRTUAL, 258, VIRTUAL in self.rects, True)] if self.on_a_source else []

    def extend(self) -> bool:
        self.extend_calls += 1
        if self.obeys and self.known:
            self.on_a_source, self.rects = True, {MAIN_DISPLAY: MAIN, VIRTUAL: RIGHT_OF_MAIN}
        return True


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(vdisplay, "EXTEND_AFTER_S", 0.0)

    def make(rects, **kwargs) -> FakeDesktop:
        fake = FakeDesktop(rects, **kwargs)
        monkeypatch.setattr(vdisplay, "vdd_sources", fake.sources)
        monkeypatch.setattr(vdisplay, "desktop_rects", lambda: dict(fake.rects))
        monkeypatch.setattr(vdisplay, "extend_desktop", fake.extend)
        monkeypatch.setattr(vdisplay, "_monitor_known", lambda uid: fake.known and uid == 258)
        return fake

    return make


def test_a_screen_windows_extended_by_itself_is_kept_as_it_is(desktop):
    fake = desktop({MAIN_DISPLAY: MAIN, VIRTUAL: RIGHT_OF_MAIN})
    assert vdisplay.wait_for_source(2, timeout=2) == VIRTUAL and fake.extend_calls == 0


@pytest.mark.parametrize("rects, on_a_source", [
    ({MAIN_DISPLAY: MAIN, VIRTUAL: MAIN}, True),  # Duplicate: both screens in the same spot
    ({VIRTUAL: MAIN}, True),  # Second screen only: the PC's own screen is off
    ({MAIN_DISPLAY: MAIN}, True),  # PC screen only: the virtual screen is not on the desktop
    ({MAIN_DISPLAY: MAIN}, False),  # PC screen only, the monitor on no source at all
])
def test_a_remembered_topology_is_switched_to_extend_once(desktop, rects, on_a_source):
    fake = desktop(rects, on_a_source=on_a_source)
    assert vdisplay.wait_for_source(2, timeout=2) == VIRTUAL and fake.extend_calls == 1


def test_extend_waits_until_windows_has_the_new_monitor(desktop):
    fake = desktop({MAIN_DISPLAY: MAIN}, on_a_source=False, known=False)
    with pytest.raises(VddError) as info:
        vdisplay.wait_for_source(2, timeout=0.5)
    assert info.value.code == "not_attached" and fake.extend_calls == 0  # the user's own screens untouched
    fake.known = True
    assert vdisplay.wait_for_source(2, timeout=2) == VIRTUAL and fake.extend_calls == 1


def test_a_pc_without_a_screen_of_its_own_needs_no_extend(desktop):
    fake = desktop({VIRTUAL: MAIN})
    assert vdisplay.wait_for_source(2, timeout=2, own_screen=False) == VIRTUAL and fake.extend_calls == 0


def test_an_extend_that_does_not_take_ends_with_win_p_advice(desktop):
    fake = desktop({MAIN_DISPLAY: MAIN, VIRTUAL: MAIN}, obeys=False)
    with pytest.raises(VddError, match="Win\\+P"):
        vdisplay.wait_for_source(2, timeout=0.5)
    assert fake.extend_calls == 1


def test_waiting_for_the_monitor_can_be_cancelled(desktop):
    desktop({MAIN_DISPLAY: MAIN}, on_a_source=False, known=False)
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    with pytest.raises(VddError) as info:
        vdisplay.wait_for_source(2, timeout=5, cancel=cancel)
    assert info.value.code == "cancelled" and time.monotonic() - started < 1


# -- monitors left behind ------------------------------------------------------------


def test_left_behind_monitors_are_removed_only_when_safe():
    entries = [
        {"index": 0, "uid": 256, "pid": 111, "time": 2000},  # its MirrorBridge died; still attached
        {"index": 1, "uid": 257, "pid": 222, "time": 2000},  # its MirrorBridge still runs
        {"index": 2, "uid": 258, "pid": 333, "time": 500},  # from before the last restart
        {"index": 3, "uid": 259, "pid": 444, "time": 2000},  # already gone
    ]
    remove, keep = vdisplay.split_state(entries, {256, 257, 258}, lambda pid: pid == 222, booted_at=1000)
    assert remove == [0]
    assert keep == [entries[1]]


@pytest.fixture
def left_behind(tmp_path, monkeypatch):
    """A state file with a monitor whose MirrorBridge died and one whose MirrorBridge still runs."""
    entries = [{"index": 0, "uid": 256, "pid": 111, "time": 2000}, {"index": 1, "uid": 257, "pid": 222, "time": 2000}]
    monkeypatch.setattr(vdisplay, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(vdisplay, "vdd_sources", lambda: [
        vdisplay.VddSource(VIRTUAL, 256, True, True), vdisplay.VddSource(r"\\.\DISPLAY8", 257, True, True),
    ])
    monkeypatch.setattr(vdisplay, "_process_alive", lambda pid: pid == 222)
    monkeypatch.setattr(vdisplay, "_boot_time", lambda: 1000.0)
    vdisplay._write_state(entries)  # pylint: disable=protected-access
    return entries


def test_startup_removes_the_monitor_a_dead_mirrorbridge_left(left_behind, monkeypatch):
    device = FakeDevice()
    monkeypatch.setattr(vdisplay, "driver_status", lambda: DriverStatus.OK)
    monkeypatch.setattr(vdisplay.VddDevice, "open", classmethod(lambda cls: device))
    vdisplay.recover_stale()
    assert device.calls == [("remove", 0)] and device.closed
    assert vdisplay._read_state() == [left_behind[1]]  # pylint: disable=protected-access


def test_startup_without_the_driver_only_crosses_out_dead_entries(left_behind, monkeypatch):
    def never(cls):
        raise AssertionError("the driver must not be opened")

    monkeypatch.setattr(vdisplay, "driver_status", lambda: DriverStatus.NOT_INSTALLED)
    monkeypatch.setattr(vdisplay.VddDevice, "open", classmethod(never))
    vdisplay.recover_stale()
    assert vdisplay._read_state() == [left_behind[1]]  # pylint: disable=protected-access


def test_state_file_remembers_and_forgets(tmp_path, monkeypatch):
    monkeypatch.setattr(vdisplay, "app_data_dir", lambda: tmp_path)
    vdisplay._remember(4)  # pylint: disable=protected-access
    saved = json.loads((tmp_path / vdisplay.STATE_FILE).read_text())["displays"]
    assert [(e["index"], e["uid"], e["pid"]) for e in saved] == [(4, 260, os.getpid())]
    vdisplay._forget(4)  # pylint: disable=protected-access
    assert not (tmp_path / vdisplay.STATE_FILE).exists()
    (tmp_path / vdisplay.STATE_FILE).write_text("{half written")
    assert vdisplay._read_state() == []  # pylint: disable=protected-access


# -- downloading the installer --------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def test_download_keeps_only_the_pinned_file(tmp_path, monkeypatch):
    payload = b"MZ" + b"\x00" * 200_000
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(payload))
    with pytest.raises(VddError) as info:
        vdisplay.download_installer(tmp_path)
    assert info.value.code == "mismatch"
    assert list(tmp_path.iterdir()) == []  # a file that does not match is never kept

    monkeypatch.setattr(vdisplay, "VDD_INSTALLER_SHA256", hashlib.sha256(payload).hexdigest())
    seen = []
    path = vdisplay.download_installer(tmp_path, lambda done, total: seen.append((done, total)))
    assert path.read_bytes() == payload and path.name == "parsec-vdd-0.45.0.0.exe"
    assert seen[-1] == (len(payload), len(payload))
    assert [p.name for p in tmp_path.iterdir()] == [path.name]

    monkeypatch.setattr(vdisplay, "VDD_INSTALLER_MAX_BYTES", 100_000)
    path.unlink()
    with pytest.raises(VddError) as info:
        vdisplay.download_installer(tmp_path)
    assert info.value.code == "mismatch" and list(tmp_path.iterdir()) == []


def test_a_cached_installer_is_used_only_if_it_still_matches(tmp_path, monkeypatch):
    payload = b"MZ" + b"\x01" * 1000
    monkeypatch.setattr(vdisplay, "VDD_INSTALLER_SHA256", hashlib.sha256(payload).hexdigest())
    cached = tmp_path / "parsec-vdd-0.45.0.0.exe"
    cached.write_bytes(b"MZ swapped since it was checked")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResponse(payload))
    assert vdisplay.download_installer(tmp_path).read_bytes() == payload  # downloaded again

    def offline(*_a, **_k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    assert vdisplay.download_installer(tmp_path) == cached  # a file that matches needs no network
    cached.write_bytes(b"MZ swapped again")
    with pytest.raises(VddError) as info:
        vdisplay.download_installer(tmp_path)
    assert info.value.code == "download" and not cached.exists()  # never left to be run


def test_download_errors_become_vdd_errors(tmp_path, monkeypatch):
    def offline(*_a, **_k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(VddError) as info:
        vdisplay.download_installer(tmp_path)
    assert info.value.code == "download" and "no route" in str(info.value)


# -- keep-alive and the Parsec backend, with a fake driver ------------------------------------


class FakeDevice:
    def __init__(self, index: int = 2, answers: bool = True) -> None:
        self.index = index
        self.answers = answers
        self.calls: list = []
        self.pings: list[float] = []
        self.closed = False

    def update(self) -> bool:
        self.pings.append(time.monotonic())
        return self.answers

    def version(self):
        return (0, 45)

    def add(self) -> int:
        self.calls.append("add")
        return self.index

    def remove(self, index: int) -> bool:
        self.calls.append(("remove", index))
        return True

    def close(self) -> None:
        self.closed = True


def test_keepalive_reports_a_silent_driver_once(monkeypatch):
    monkeypatch.setattr(vdisplay, "PING_INTERVAL_S", 0.01)
    lost = []
    keepalive = vdisplay.KeepAlive(FakeDevice(answers=False), lambda: lost.append(1))
    keepalive.start()
    keepalive.join(5)
    assert lost == [1] and not keepalive.is_alive()


def test_presence_watch_reports_a_monitor_windows_removed(monkeypatch):
    monkeypatch.setattr(vdisplay, "PRESENCE_INTERVAL_S", 0.01)
    lost = []
    watch = vdisplay.PresenceWatch(lambda: False, lambda: lost.append(1))
    watch.start()
    watch.join(5)
    assert lost == [1] and not watch.is_alive()


@pytest.fixture
def fake_parsec(tmp_path, monkeypatch):
    device = FakeDevice()
    monkeypatch.setattr(vdisplay, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(vdisplay, "driver_status", lambda: DriverStatus.OK)
    monkeypatch.setattr(vdisplay, "interface_paths", lambda guid=vdisplay.VDD_INTERFACE_GUID: [r"\\?\ROOT#DISPLAY#0000"])
    monkeypatch.setattr(vdisplay.VddDevice, "open", classmethod(lambda cls: device))
    monkeypatch.setattr(vdisplay, "vdd_sources", lambda: [])
    monkeypatch.setattr(vdisplay, "desktop_rects", lambda: {MAIN_DISPLAY: MAIN})
    monkeypatch.setattr(vdisplay, "_present", lambda uid: False)
    monkeypatch.setattr(vdisplay, "supported_modes", lambda name: {(1920, 1080, 60), (3840, 2160, 60)})
    monkeypatch.setattr(vdisplay, "set_mode", lambda name, *mode: device.calls.append(("mode", name, mode)) or True)

    def attached(index, cancel=None, own_screen=True):
        device.own_screen = own_screen
        return VIRTUAL

    monkeypatch.setattr(vdisplay, "wait_for_source", attached)
    return device


def test_parsec_screen_adds_and_removes_its_monitor(fake_parsec, tmp_path):
    screen = vdisplay.backend()()
    assert screen.start(3840, 2160, 60) == VIRTUAL
    assert fake_parsec.calls == ["add", ("mode", VIRTUAL, (3840, 2160, 60))]
    assert fake_parsec.own_screen  # the PC's own display was on the desktop before ADD
    assert (tmp_path / vdisplay.STATE_FILE).exists()  # written down while it exists
    screen.stop()
    screen.stop()  # a second stop does nothing
    assert fake_parsec.calls[-1] == ("remove", 2) and fake_parsec.calls.count(("remove", 2)) == 1
    assert fake_parsec.closed and not (tmp_path / vdisplay.STATE_FILE).exists()


def test_parsec_screen_removes_its_monitor_when_start_fails(fake_parsec, monkeypatch):
    def never_attached(index, **_kwargs):
        raise VddError("not_attached")

    monkeypatch.setattr(vdisplay, "wait_for_source", never_attached)
    with pytest.raises(VddError, match="Win\\+P"):
        vdisplay.backend()().start()
    assert fake_parsec.calls == ["add", ("remove", 2)] and fake_parsec.closed


def test_stop_during_start_makes_start_give_up(fake_parsec, monkeypatch):
    waiting = threading.Event()

    def slow_attach(index, cancel=None, **_kwargs):
        waiting.set()
        if cancel.wait(5):
            raise VddError("cancelled")
        return VIRTUAL

    monkeypatch.setattr(vdisplay, "wait_for_source", slow_attach)
    screen = vdisplay.backend()()
    errors = []
    thread = threading.Thread(target=lambda: errors.append(pytest.raises(VddError, screen.start)))
    thread.start()
    assert waiting.wait(5)
    started = time.monotonic()
    screen.stop()
    thread.join(5)
    assert time.monotonic() - started < 3
    assert errors and errors[0].value.code == "cancelled"
    assert fake_parsec.calls == ["add", ("remove", 2)] and fake_parsec.closed


def test_driver_problem_stops_start_before_anything_is_added(fake_parsec, monkeypatch):
    monkeypatch.setattr(vdisplay, "driver_status", lambda: DriverStatus.NOT_INSTALLED)
    with pytest.raises(VddError) as info:
        vdisplay.backend()().start()
    assert info.value.status is DriverStatus.NOT_INSTALLED and fake_parsec.calls == []


def test_a_started_driver_without_its_device_interface_is_not_ready(fake_parsec, monkeypatch):
    monkeypatch.setattr(vdisplay, "interface_paths", lambda guid=vdisplay.VDD_INTERFACE_GUID: [])
    assert vdisplay.backend().status() is DriverStatus.DRIVER_ERROR
    with pytest.raises(VddError) as info:
        vdisplay.backend()().start()
    assert info.value.status is DriverStatus.DRIVER_ERROR and fake_parsec.calls == []


def test_pings_never_wait_for_the_presence_check(fake_parsec, monkeypatch):
    monkeypatch.setattr(vdisplay, "PRESENCE_INTERVAL_S", 0.01)
    windows_busy = threading.Event()
    monkeypatch.setattr(vdisplay, "_present", lambda uid: windows_busy.wait(5))  # a display mode change
    screen = vdisplay.backend()()
    screen.start()
    fake_parsec.pings.clear()
    time.sleep(1.2)  # longer than the driver's watchdog
    pings = list(fake_parsec.pings)
    monkeypatch.setattr(vdisplay, "_present", lambda uid: False)
    windows_busy.set()
    screen.stop()
    assert len(pings) >= 6 and max(b - a for a, b in zip(pings, pings[1:])) < 0.5


def test_the_cast_hears_once_that_the_screen_is_gone(fake_parsec, monkeypatch):
    monkeypatch.setattr(vdisplay, "PING_INTERVAL_S", 0.01)
    monkeypatch.setattr(vdisplay, "PRESENCE_INTERVAL_S", 0.01)
    fake_parsec.answers = False  # the driver stops answering, and Windows no longer has the monitor
    lost = []
    screen = vdisplay.backend()(on_lost=lambda: lost.append(1))
    screen.start()
    time.sleep(0.2)
    screen.stop()
    assert lost == [1]


def test_removing_the_screen_on_purpose_is_not_a_loss(fake_parsec, monkeypatch):
    monkeypatch.setattr(vdisplay, "PRESENCE_INTERVAL_S", 0.01)
    present = [True]
    monkeypatch.setattr(vdisplay, "_present", lambda uid: present[0])
    real_remove = fake_parsec.remove

    def remove(index):
        present[0] = False
        time.sleep(0.1)  # time for two presence checks, were one still running
        return real_remove(index)

    fake_parsec.remove = remove
    lost = []
    screen = vdisplay.backend()(on_lost=lambda: lost.append(1))
    screen.start()
    screen.stop()
    assert lost == [] and fake_parsec.calls[-1] == ("remove", 2)


# -- the cast session, with a fake virtual screen -----------------------------------------------


class FakeScreen(vdisplay.VirtualScreen):
    name = "fake"
    events: list = []
    last: "FakeScreen | None" = None

    def __init__(self, on_lost=None) -> None:
        super().__init__(on_lost)
        FakeScreen.last = self

    @classmethod
    def status(cls) -> DriverStatus:
        return DriverStatus.OK

    def start(self, width: int, height: int, hz: int) -> str:
        self.events.append(("start", width, height, hz))
        return VIRTUAL

    def stop(self) -> None:
        self.events.append("xscreen.stop")


# FFmpeg's master playlist for other brands' TVs (see ffmpeg.MASTER_PLAYLIST)
MASTER = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=25000000,RESOLUTION=3840x2160,CODECS="avc1.640033,mp4a.40.2"\nstream.m3u8\n'


class FakeManager:
    async def connect(self, _device):
        async def play_url(_url):
            await asyncio.sleep(3600)

        return types.SimpleNamespace(stream=types.SimpleNamespace(play_url=play_url), close=lambda: None)


@pytest.fixture
def extend_session(tmp_path, monkeypatch):
    """A CastSession whose virtual screen and encoder are fakes that log what happens, in order."""
    from mirrorbridge.session import CastSession

    events: list = []
    FakeScreen.events = events
    monkeypatch.setattr(vdisplay, "available", lambda: True)
    monkeypatch.setattr(vdisplay, "backend", lambda name=vdisplay.DEFAULT_BACKEND: FakeScreen)
    monkeypatch.setattr(ffmpeg, "find_ffmpeg", lambda *_a: "ffmpeg")
    monkeypatch.setattr(ffmpeg, "pick_encoder", lambda *_a: "h264_nvenc")
    monkeypatch.setattr(ffmpeg, "pick_capture_backend", lambda *_a: "ddagrab")
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [
        ffmpeg.Monitor(0, 0, 0, 1920, 1080, True, 0, device=r"\\.\DISPLAY1"),
        ffmpeg.Monitor(1, 1920, 0, 3840, 2160, False, 1, device=VIRTUAL),
    ])
    real_stop_encoder = CastSession._stop_encoder

    async def stop_encoder(self):
        events.append("stop_encoder")
        await real_stop_encoder(self)

    monkeypatch.setattr(CastSession, "_stop_encoder", stop_encoder)

    def make(launch, is_apple=True, **settings):
        async def launch_encoder(self, _ff, monitor, region=None):
            events.append(("launch", monitor.device, self.backend, self.encoder))
            if not self.for_apple:
                (self._work_dir / ffmpeg.MASTER_PLAYLIST).write_text(MASTER)
            await launch(self)

        monkeypatch.setattr(CastSession, "_launch_encoder", launch_encoder)
        settings = Settings(**{"extend_screen": True, "bind_address": "127.0.0.1", "http_port": 0,
                               "capture_audio": False, **settings})
        device = types.SimpleNamespace(name="TV", address="127.0.0.1", is_apple=is_apple, requires_password=False)
        states = []
        session = CastSession(FakeManager(), device, settings, lambda s, m: states.append(s), work_dir=tmp_path)
        return session, events, states

    return make


async def test_cancel_while_starting_removes_the_screen_after_ffmpeg(extend_session):
    from mirrorbridge.session import State

    async def hang(_session):
        await asyncio.sleep(3600)

    session, events, states = extend_session(hang, extend_mode="4k", monitor_index=0, window_title="Notepad")
    task = asyncio.ensure_future(session.run())
    while not any(isinstance(e, tuple) and e[0] == "launch" for e in events):
        assert not task.done()
        await asyncio.sleep(0.01)
    assert session.virtual_device == VIRTUAL
    await session.stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    # 4K was asked for, the virtual screen was cast (not display 1 or the window) natively.
    assert events[0] == ("start", 3840, 2160, 60)
    assert events[1][:2] == ("launch", VIRTUAL)
    assert session.settings.window_title == "" and session.settings.output_height == 0
    assert session.settings.video_bitrate_kbps >= 20000
    assert events[-2:] == ["stop_encoder", "xscreen.stop"] and events.count("xscreen.stop") == 1
    assert State.EXTENDING in states and states[-1] == State.STOPPED
    assert session.finished.is_set() and session.virtual_device == ""


async def test_failed_cast_removes_the_screen_after_the_last_ffmpeg(extend_session):
    from mirrorbridge.session import State

    async def dies(_session):
        raise RuntimeError("FFmpeg exited (code 1)")

    session, events, states = extend_session(dies)
    with pytest.raises(RuntimeError, match="FFmpeg exited"):
        await asyncio.wait_for(session.run(), 10)
    launches = [e for e in events if isinstance(e, tuple) and e[0] == "launch"]
    assert [e[2:] for e in launches] == [("ddagrab", "h264_nvenc"), ("gdigrab", "h264_nvenc"), ("gdigrab", "libx264")]
    assert events[0] == ("start", 1920, 1080, 60)
    assert events[-2:] == ["stop_encoder", "xscreen.stop"] and events.count("xscreen.stop") == 1
    assert states[-1] == State.FAILED


@pytest.mark.parametrize("is_apple", [True, False])
async def test_lost_screen_ends_the_cast_with_its_own_message(extend_session, is_apple):
    from mirrorbridge.session import State

    async def runs(_session):
        return None

    session, events, states = extend_session(runs, is_apple=is_apple)
    task = asyncio.ensure_future(session.run())
    while State.CONNECTING not in states:
        assert not task.done()
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    threading.Thread(target=FakeScreen.last.on_lost).start()  # as the keep-alive thread would
    with pytest.raises(VddError) as info:
        await asyncio.wait_for(task, 10)
    assert info.value.code == "lost" and session.error is info.value
    assert events[-2:] == ["stop_encoder", "xscreen.stop"]
    assert states[-1] == State.FAILED


async def test_another_brands_tv_gets_its_own_stream_of_the_virtual_screen(extend_session, monkeypatch):
    """Another brand's TV gets the stream made for such TVs (2 s segments, a master playlist
    with FRAME-RATE and AVERAGE-BANDWIDTH) of the virtual screen at its own size. When the TV
    stops fetching, the cast ends as any other, and the screen goes once FFmpeg has stopped."""
    import aiohttp

    from mirrorbridge import session as session_module
    from mirrorbridge.session import State

    monkeypatch.setattr(session_module, "IDLE_REQUEST_TIMEOUT", 0.5)
    launched = []

    async def writes_a_segment(session):
        launched.append((session.for_apple, ffmpeg.segment_seconds(session.settings, session.for_apple),
                         session.settings.output_height))
        (session._work_dir / "stream.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\nseg_00000.ts\n")
        (session._work_dir / "seg_00000.ts").write_bytes(b"\x47" * 188)

    session, events, states = extend_session(writes_a_segment, is_apple=False, extend_mode="4k")
    task = asyncio.ensure_future(session.run())
    while State.CONNECTING not in states:
        assert not task.done()
        await asyncio.sleep(0.01)
    assert session.stream_url.endswith("/master.m3u8")
    async with aiohttp.ClientSession() as http:  # the TV (127.0.0.1 here) plays a moment, then stops
        async with http.get(session.stream_url) as resp:
            master = await resp.text()
        for name in ("stream.m3u8", "seg_00000.ts"):
            async with http.get(session.stream_url.replace("master.m3u8", name)) as resp:
                assert resp.status == 200
    await asyncio.wait_for(task, 10)
    assert launched == [(False, 2.0, 0)] and events[1][:2] == ("launch", VIRTUAL)
    # 4K's 20 Mbps (not the 8 Mbps of the settings) plus the audio's 160 kbps
    assert "FRAME-RATE=30.000,AVERAGE-BANDWIDTH=20160000" in master
    assert State.CASTING in states and states[-1] == State.STOPPED
    assert events[-2:] == ["stop_encoder", "xscreen.stop"] and events.count("xscreen.stop") == 1


async def test_test_capture_ignores_extend_screen(extend_session):
    async def dies(_session):
        raise RuntimeError("FFmpeg exited")

    session, events, _states = extend_session(dies, capture_backend="test")
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session.run(), 10)
    assert "xscreen.stop" not in events and not any(e[0] == "start" for e in events if isinstance(e, tuple))


# -- asking before installing ------------------------------------------------------------------


class FakeController:
    """What ExtendScreenSetup uses of the GUI controller, without Qt."""

    def __init__(self, extend_screen: bool) -> None:
        self.settings = Settings(extend_screen=extend_screen)
        self.vdd_status = None
        self.notes: list = []
        self.notify = types.SimpleNamespace(emit=lambda *args: self.notes.append(args))
        self.settings_changed = types.SimpleNamespace(emit=lambda: None)

    def update_settings(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.settings, key, value)

    def run_blocking(self, *_args, **_kwargs):
        raise AssertionError("nothing may run in the background before the user agrees")


@pytest.fixture
def setup_without_consent(monkeypatch):
    # mirrorbridge.gui.extend shows Qt dialogs; Qt also fails to load without libEGL (ImportError)
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from mirrorbridge.gui import extend

    ran: list = []
    monkeypatch.setattr(vdisplay, "download_installer", lambda *_a, **_k: ran.append("download"))
    monkeypatch.setattr(vdisplay, "run_installer_elevated", lambda *_a, **_k: ran.append("installer"))
    monkeypatch.setattr(extend.ExtendScreenSetup, "_consent", lambda self: False)  # "Not now"
    return extend, ran


def test_no_download_without_consent(setup_without_consent):
    extend, ran = setup_without_consent
    c = FakeController(extend_screen=False)
    setup = extend.ExtendScreenSetup(c)
    setup.busy = True  # as choose() leaves it while the check runs
    setup._checked(DriverStatus.NOT_INSTALLED)  # pylint: disable=protected-access
    assert ran == [] and not c.settings.extend_screen and not setup.busy and c.notes == []


def test_a_cast_that_finds_no_driver_turns_extend_off(setup_without_consent):
    from mirrorbridge.gui import i18n

    extend, ran = setup_without_consent
    c = FakeController(extend_screen=True)
    setup = extend.ExtendScreenSetup(c)
    setup.handle_status(DriverStatus.NOT_INSTALLED)
    assert ran == [] and not c.settings.extend_screen and not setup.busy  # so Start casts a display again
    assert c.notes[-1][1] == i18n.tr("vdd_extend_off")


def test_install_starts_only_after_consent(setup_without_consent, monkeypatch):
    extend, ran = setup_without_consent
    monkeypatch.setattr(extend.ExtendScreenSetup, "_consent", lambda self: True)
    monkeypatch.setattr(extend.ExtendScreenSetup, "_install", lambda self: ran.append("_install"))
    setup = extend.ExtendScreenSetup(FakeController(extend_screen=False))
    setup.busy = True
    setup._checked(DriverStatus.NOT_INSTALLED)  # pylint: disable=protected-access
    assert ran == ["_install"] and setup.busy  # the install ends the check when it is done


# -- settings and command line ------------------------------------------------------------------


def test_extend_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    Settings(extend_screen=True, extend_mode="4k", extend_tip_shown=True).save(path)
    loaded = Settings.load(path)
    assert (loaded.extend_screen, loaded.extend_mode, loaded.extend_tip_shown) == (True, "4k", True)
    path.write_text(json.dumps({"monitor_index": 1}))  # a settings file from before this feature
    older = Settings.load(path)
    assert (older.extend_screen, older.extend_mode, older.monitor_index) == (False, "1080p", 1)


def test_command_line_options():
    from mirrorbridge.cli import build_parser

    args = build_parser().parse_args(["cast", "--extend", "--extend-mode", "720p"])
    assert args.extend and args.extend_mode == "720p"
    assert build_parser().parse_args(["vdd", "install", "-y"]).yes
    assert build_parser().parse_args(["vdd", "status"]).vdd_command == "status"


def _driver(monkeypatch, status: DriverStatus) -> None:
    class Driver(FakeScreen):
        @classmethod
        def status(cls) -> DriverStatus:
            return status

    monkeypatch.setattr(vdisplay, "backend", lambda name=vdisplay.DEFAULT_BACKEND: Driver)


async def test_vdd_install_explains_an_installer_that_would_not_start(monkeypatch, capsys, tmp_path):
    from mirrorbridge import cli

    _driver(monkeypatch, DriverStatus.NOT_INSTALLED)
    monkeypatch.setattr(vdisplay, "download_installer", lambda progress=None: tmp_path / "parsec-vdd.exe")

    def refused(_installer):
        raise VddError("install", "The file cannot be accessed by the system. (1920)")

    monkeypatch.setattr(vdisplay, "run_installer_elevated", refused)
    assert await cli._vdd_install(cli.build_parser().parse_args(["vdd", "install", "-y"])) == 1
    out = capsys.readouterr().out
    assert "(1920)" in out and "winget install" in out


async def test_vdd_install_after_an_install_that_needs_a_restart_only_says_so(monkeypatch, capsys):
    from mirrorbridge import cli

    _driver(monkeypatch, DriverStatus.RESTART_REQUIRED)
    monkeypatch.setattr(vdisplay, "download_installer", lambda **_k: pytest.fail("downloaded again"))
    assert await cli._vdd_install(cli.build_parser().parse_args(["vdd", "install", "-y"])) == 0
    assert "Restart Windows" in capsys.readouterr().out
