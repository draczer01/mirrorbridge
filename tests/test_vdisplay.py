"""Extend screen: the virtual display driver protocol, its bookkeeping, and the cast session's use
of it. Everything runs without the driver (fakes stand in for it), on Linux and on Windows."""

import asyncio
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

from aircast import ffmpeg, vdisplay
from aircast.config import Settings
from aircast.vdisplay import DriverStatus, VddError

FILE_READ_ACCESS, FILE_WRITE_ACCESS, METHOD_BUFFERED = 1, 2, 0
VIRTUAL = r"\\.\DISPLAY9"


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
    from aircast.gui import i18n

    for key, entry in i18n._STRINGS.items():  # pylint: disable=protected-access
        assert entry.get("en") and entry.get("es"), key


def test_every_problem_has_english_and_spanish_text():
    pytest.importorskip("PySide6.QtWidgets")  # aircast.gui.extend shows Qt dialogs
    from aircast.gui import extend, i18n

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
    from aircast import winmonitors

    assert isinstance(vdisplay.driver_status(), DriverStatus)
    assert isinstance(vdisplay.interface_paths(), list)
    assert isinstance(vdisplay.vdd_sources(), list)
    assert isinstance(winmonitors.dxgi_map(), dict)


# -- monitors left behind ------------------------------------------------------------


def test_left_behind_monitors_are_removed_only_when_safe():
    entries = [
        {"index": 0, "uid": 256, "pid": 111, "time": 2000},  # its AirCast died; still attached
        {"index": 1, "uid": 257, "pid": 222, "time": 2000},  # its AirCast still runs
        {"index": 2, "uid": 258, "pid": 333, "time": 500},  # from before the last restart
        {"index": 3, "uid": 259, "pid": 444, "time": 2000},  # already gone
    ]
    remove, keep = vdisplay.split_state(entries, {256, 257, 258}, lambda pid: pid == 222, booted_at=1000)
    assert remove == [0]
    assert keep == [entries[1]]


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
    assert info.value.code == "download"
    assert list(tmp_path.iterdir()) == []  # a file that does not match is never kept

    monkeypatch.setattr(vdisplay, "VDD_INSTALLER_SHA256", hashlib.sha256(payload).hexdigest())
    seen = []
    path = vdisplay.download_installer(tmp_path, lambda done, total: seen.append((done, total)))
    assert path.read_bytes() == payload and path.name == "parsec-vdd-0.45.0.0.exe"
    assert seen[-1] == (len(payload), len(payload))
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


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
        self.closed = False

    def update(self) -> bool:
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


def test_keepalive_reports_a_monitor_windows_removed(monkeypatch):
    monkeypatch.setattr(vdisplay, "PING_INTERVAL_S", 0.01)
    lost = []
    keepalive = vdisplay.KeepAlive(FakeDevice(), lambda: lost.append(1))
    keepalive.watch(lambda: False)
    keepalive.start()
    keepalive.join(5)
    assert lost == [1]


@pytest.fixture
def fake_parsec(tmp_path, monkeypatch):
    device = FakeDevice()
    monkeypatch.setattr(vdisplay, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(vdisplay, "driver_status", lambda: DriverStatus.OK)
    monkeypatch.setattr(vdisplay.VddDevice, "open", classmethod(lambda cls: device))
    monkeypatch.setattr(vdisplay, "vdd_sources", lambda: [])
    monkeypatch.setattr(vdisplay, "_present", lambda uid: False)
    monkeypatch.setattr(vdisplay, "supported_modes", lambda name: {(1920, 1080, 60), (3840, 2160, 60)})
    monkeypatch.setattr(vdisplay, "set_mode", lambda name, *mode: device.calls.append(("mode", name, mode)) or True)
    monkeypatch.setattr(vdisplay, "wait_for_source", lambda index, cancel=None: VIRTUAL)
    return device


def test_parsec_screen_adds_and_removes_its_monitor(fake_parsec, tmp_path):
    screen = vdisplay.backend()()
    assert screen.start(3840, 2160, 60) == VIRTUAL
    assert fake_parsec.calls == ["add", ("mode", VIRTUAL, (3840, 2160, 60))]
    assert (tmp_path / vdisplay.STATE_FILE).exists()  # written down while it exists
    screen.stop()
    screen.stop()  # a second stop does nothing
    assert fake_parsec.calls[-1] == ("remove", 2) and fake_parsec.calls.count(("remove", 2)) == 1
    assert fake_parsec.closed and not (tmp_path / vdisplay.STATE_FILE).exists()


def test_parsec_screen_removes_its_monitor_when_start_fails(fake_parsec, monkeypatch):
    def never_attached(index, cancel=None):
        raise VddError("not_attached")

    monkeypatch.setattr(vdisplay, "wait_for_source", never_attached)
    with pytest.raises(VddError, match="Win\\+P"):
        vdisplay.backend()().start()
    assert fake_parsec.calls == ["add", ("remove", 2)] and fake_parsec.closed


def test_stop_during_start_makes_start_give_up(fake_parsec, monkeypatch):
    waiting = threading.Event()

    def slow_attach(index, cancel=None):
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


class FakeManager:
    async def connect(self, _device):
        async def play_url(_url):
            await asyncio.sleep(3600)

        return types.SimpleNamespace(stream=types.SimpleNamespace(play_url=play_url), close=lambda: None)


@pytest.fixture
def extend_session(tmp_path, monkeypatch):
    """A CastSession whose virtual screen and encoder are fakes that log what happens, in order."""
    from aircast.session import CastSession

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

    def make(launch, **settings):
        async def launch_encoder(self, _ff, monitor, region=None):
            events.append(("launch", monitor.device, self.backend, self.encoder))
            await launch(self)

        monkeypatch.setattr(CastSession, "_launch_encoder", launch_encoder)
        settings = Settings(**{"extend_screen": True, "bind_address": "127.0.0.1", "http_port": 0,
                               "capture_audio": False, **settings})
        device = types.SimpleNamespace(name="TV", address="127.0.0.1")
        states = []
        session = CastSession(FakeManager(), device, settings, lambda s, m: states.append(s), work_dir=tmp_path)
        return session, events, states

    return make


async def test_cancel_while_starting_removes_the_screen_after_ffmpeg(extend_session):
    from aircast.session import State

    async def hang(_session):
        await asyncio.sleep(3600)

    session, events, states = extend_session(hang, extend_mode="4k", monitor_index=0, window_title="Notepad")
    task = asyncio.ensure_future(session.run())
    while not any(isinstance(e, tuple) and e[0] == "launch" for e in events):
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
    from aircast.session import State

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


async def test_lost_screen_ends_the_cast_with_its_own_message(extend_session):
    from aircast.session import State

    async def runs(_session):
        return None

    session, events, states = extend_session(runs)
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


async def test_test_capture_ignores_extend_screen(extend_session):
    async def dies(_session):
        raise RuntimeError("FFmpeg exited")

    session, events, _states = extend_session(dies, capture_backend="test")
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(session.run(), 10)
    assert "xscreen.stop" not in events and not any(e[0] == "start" for e in events if isinstance(e, tuple))


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
    from aircast.cli import build_parser

    args = build_parser().parse_args(["cast", "--extend", "--extend-mode", "720p"])
    assert args.extend and args.extend_mode == "720p"
    assert build_parser().parse_args(["vdd", "install", "-y"]).yes
    assert build_parser().parse_args(["vdd", "status"]).vdd_command == "status"
