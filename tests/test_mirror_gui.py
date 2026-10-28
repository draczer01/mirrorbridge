"""Mirror screen in the tray menu (and the window): offered for a paired TV that does not need
FairPlay, run as a cast is run, shown and stopped as a cast is, and one session at a time.

The sessions go to the TCL 55QM7L of fake_mirror_tv.py, which runs on the controller's own
asyncio loop. Qt runs offscreen, on a controller that neither scans the network nor tests
encoders, with the settings and mirroring.json in a temporary folder.
"""

import asyncio
import concurrent.futures
import os
import threading
import time

import pytest

from fake_mirror_tv import CREDENTIALS, FakeMirrorTV
from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.gui import i18n
from mirrorbridge.mirror import MirrorSession, profiles
from mirrorbridge.mirror.negotiation import MEDIA_FIRST
from mirrorbridge.session import State

TCL_TXT = {"features": "0x7F8AD0,0x38BCF46", "srcvers": "377.40.00", "model": "55QM7L", "manufacturer": "TCL"}
APPLE_TV_TXT = {"features": "0x4A7FDFD5,0xBC157FDE", "srcvers": "690.7.1", "model": "AppleTV11,1"}  # bit 14
MIRROR = "Mirror screen (low latency, experimental)"

_APP = None  # one QApplication for every test here; it must outlive them


def _ffmpeg() -> None:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264", mirror=True):
        pytest.skip("this FFmpeg has no libx264")


def _device(port: int = 7000, credentials=CREDENTIALS, txt=None, name: str = "TCL 55QM7L"):
    """A TV as discovery finds it (devices.Device), with or without MirrorBridge's pairing."""
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import PairingRequirement, Protocol

    from mirrorbridge.devices import Device

    conf = AppleTV("127.0.0.1", name)
    conf.add_service(ManualService("fake-tv", Protocol.AirPlay, port, dict(txt or TCL_TXT), credentials=credentials,
                                   pairing_requirement=PairingRequirement.Mandatory))
    return Device(conf)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    global _APP  # pylint: disable=global-statement
    # Qt also fails to load without libEGL (ImportError)
    widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = widgets.QApplication.instance() or widgets.QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from mirrorbridge.gui.controller import Controller

    monkeypatch.setattr(Controller, "refresh_if_stale", lambda self, max_age=60: None)  # no network scan
    monkeypatch.setattr(Controller, "refresh_encoders_if_stale", lambda self, max_age=120: None)  # no FFmpeg
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [ffmpeg.Monitor(0, 0, 0, 1920, 1080, True)])
    monkeypatch.setattr(ffmpeg, "list_windows", lambda: [])
    c = Controller(Settings.load())
    # A small picture from FFmpeg's test source stands in for the screen.
    c.settings.encoder, c.settings.capture_backend, c.settings.capture_audio = "libx264", "test", False
    c.settings.framerate, c.settings.video_bitrate_kbps, c.settings.output_height = 30, 1500, 360
    yield c
    c.shutdown()
    i18n.set_language("en")


@pytest.fixture
def tv(controller):
    """The fake TCL, on the controller's loop, where the sessions run."""
    loop = controller.runner.loop
    tv = asyncio.run_coroutine_threadsafe(FakeMirrorTV().start(), loop).result(5)
    yield tv
    if controller.runner.thread.is_alive():
        asyncio.run_coroutine_threadsafe(tv.stop(), loop).result(5)
    else:  # the test quit the app: its loop has stopped
        loop.run_until_complete(tv.stop())


def _choose(controller, device) -> None:
    controller.devices = [device]
    controller.settings.last_device_id = device.identifier


def _tray(controller, opened=None):
    from mirrorbridge.gui.tray import TrayMenu

    tray = TrayMenu(controller, lambda: opened.append("window") if opened is not None else None, lambda: None)
    tray._rebuild()  # pylint: disable=protected-access
    return tray


def _items(tray) -> dict:
    return {action.text(): action for action in tray.menu.actions() if not action.isSeparator()}


def _texts(tray) -> list[str]:
    return [action.text() for action in tray.menu.actions() if not action.isSeparator()]


def _more(tray) -> dict:
    more = next(action.menu() for action in tray.menu.actions() if action.text() == i18n.tr("more"))
    return {action.text(): action for action in more.actions()}


def _until(condition, seconds: float = 10) -> None:
    from PySide6.QtWidgets import QApplication

    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QApplication.processEvents()
        time.sleep(0.01)


def _tasks(controller) -> set:
    """The controller loop's tasks that are MirrorBridge's (the fake TV's own are left out).

    On Windows the proactor loop also runs a task that accepts connections on each of the fake
    TV's TCP ports, and starts a new one for each connection it accepts: those are the fake TV's
    too, since mirroring itself listens only on UDP.
    """
    async def tasks():
        return asyncio.all_tasks() - {asyncio.current_task()}

    def theirs(task) -> bool:
        code = getattr(task.get_coro(), "cr_code", None)
        return code is not None and (code.co_filename.endswith("fake_mirror_tv.py") or (
            code.co_name == "accept_coro" and os.path.basename(code.co_filename) == "windows_events.py"))

    found = asyncio.run_coroutine_threadsafe(tasks(), controller.runner.loop).result(5)
    return {task for task in found if not theirs(task)}


# -- the menu item ------------------------------------------------------------------------------------


def test_mirror_screen_is_offered_for_a_paired_tv_of_another_brand(controller):
    tray = _tray(controller)
    assert tray.menu.toolTipsVisible()
    item = _items(tray)[MIRROR]
    assert not item.isEnabled()  # no TV chosen
    assert item.toolTip().startswith("Your screen on the TV a fraction of a second behind")

    _choose(controller, _device())
    tray._rebuild()  # pylint: disable=protected-access
    texts = _texts(tray)
    assert texts[texts.index("Start casting to TCL 55QM7L") + 1] == MIRROR  # next to Start casting
    item = _items(tray)[MIRROR]
    assert item.isEnabled() and item.toolTip() == (
        "Your screen on the TV a fraction of a second behind, instead of a cast's few seconds. For paired AirPlay 2 "
        "TVs of other brands (TCL, Roku, Samsung, LG…); no sound yet.")


def test_mirror_screen_needs_the_pairing(controller):
    _choose(controller, _device(credentials=None))
    item = _items(_tray(controller))[MIRROR]
    assert not item.isEnabled() and item.toolTip() == "Pair with this TV first: Device > Pair with PIN."
    controller.start_mirror()
    assert not controller.is_active and controller.session is None


def test_an_apple_tv_is_told_mirroring_is_coming(controller):
    _choose(controller, _device(txt=APPLE_TV_TXT, name="Living Room"))
    tray = _tray(controller)
    item = _items(tray)[MIRROR]
    assert not item.isEnabled() and item.toolTip() == (
        "Screen mirroring to Apple TVs and Macs is coming in a later version. For now, cast to them with Start "
        "casting.")
    assert _items(tray)["Start casting to Living Room"].isEnabled()  # casting is still there
    controller.start_mirror()
    assert not controller.is_active


def test_mirror_screen_in_spanish(controller):
    from mirrorbridge.gui.window import MainWindow

    controller.set_language("es")
    _choose(controller, _device())
    tray = _tray(controller)
    item = _items(tray)["Duplicar pantalla (baja latencia, experimental)"]
    assert item.isEnabled() and item.toolTip().startswith("Tu pantalla en la TV con una fracción de segundo de retraso")
    window = MainWindow(controller)
    assert window.mirror_btn.text() == "Duplicar pantalla (baja latencia, experimental)"
    assert window.mirror_btn.isEnabled() and window.mirror_btn.toolTip() == item.toolTip()

    _choose(controller, _device(credentials=None))
    item = _items(_tray(controller))["Duplicar pantalla (baja latencia, experimental)"]
    assert item.toolTip() == "Primero empareja esta TV: Dispositivo > Emparejar con PIN."
    _choose(controller, _device(txt=APPLE_TV_TXT))
    item = _items(_tray(controller))["Duplicar pantalla (baja latencia, experimental)"]
    assert item.toolTip() == ("La duplicación de pantalla en Apple TV y Mac llegará en una versión futura. Por ahora, "
                              "transmite a ellos con Transmitir.")
    controller.set_language("en")
    assert window.mirror_btn.text() == MIRROR and not window.mirror_btn.isEnabled()


# -- a session, from the tray menu ---------------------------------------------------------------------


def test_mirror_screen_runs_like_a_cast(controller, tv):
    from mirrorbridge.gui.window import MainWindow

    _ffmpeg()
    _choose(controller, _device(tv.port))
    opened, notes, states = [], [], []
    controller.notify.connect(lambda *note: notes.append(note))
    controller.cast_state_changed.connect(lambda state, message: states.append((state, str(message))))
    window = MainWindow(controller)
    before = _tasks(controller)
    tray = _tray(controller, opened)

    _items(tray)[MIRROR].trigger()
    assert controller.is_active and controller.mirroring
    session = controller.session
    assert isinstance(session, MirrorSession) and session.source == "desktop"
    assert controller.status()[0] == "busy"
    _until(lambda: controller.cast_state == State.CASTING, 30)

    assert controller.status() == ("active", "Mirroring to TCL 55QM7L")
    assert tray.tray.toolTip() == "MirrorBridge - Mirroring to TCL 55QM7L"
    assert window.status_label.text() == "Casting: Mirroring this PC's screen to TCL 55QM7L (640x360, 30 fps)"
    assert window.cast_btn.text() == "Stop mirroring" and not window.mirror_btn.isEnabled()
    tray._rebuild()  # pylint: disable=protected-access
    items = _items(tray)
    assert "Stop mirroring" in items and "Start casting to TCL 55QM7L" not in items
    assert not items[MIRROR].isEnabled() and not items["Play a video file…"].isEnabled()
    assert not _more(tray)["Test this TV…"].isEnabled()

    # One session at a time: a cast, another mirror or Test this TV do not start
    controller.start_cast()
    controller.start_mirror()
    controller.test_tv()
    controller.play_file("video.mp4")
    assert controller.session is session and controller.file_future is None and controller.probe_future is None

    _until(lambda: len(tv.videos[0].frames) >= 10, 15)
    items["Stop mirroring"].trigger()  # the Stop item, as for a cast
    _until(lambda: controller.session_future is None, 30)
    assert session.finished.is_set() and session.error is None
    assert session._ffmpeg.returncode is not None  # pylint: disable=protected-access
    assert tv.records == 1 and tv.teardowns == 1 and len(tv.videos[0].frames) >= 10
    assert [state for state, _ in states if state in (State.CONNECTING, State.CASTING, State.STOPPING)] == [
        State.CONNECTING, State.CASTING, State.STOPPING]
    assert states[-1] == (State.STOPPED, "Stopped")
    assert notes == [("Mirroring to TCL 55QM7L", "", False), ("Screen mirroring stopped", "", False)]
    assert controller.status() == ("idle", "Not casting") and not controller.mirroring
    assert window.cast_btn.text() == "Start casting" and window.mirror_btn.isEnabled()
    _until(lambda: _tasks(controller) == before, 5)  # nothing left running
    assert opened == [] and not window.isVisible()  # it never opens the window by itself
    saved = profiles.load("fake-tv")  # what worked, asked for first next time
    assert saved is not None and saved.ordering == MEDIA_FIRST


def test_a_failed_mirror_is_reported_as_a_failed_cast_is(controller, tv):
    from mirrorbridge.gui.window import MainWindow

    _ffmpeg()
    tv.reconfigure(m4_error=True)  # the TV dropped the pairing
    controller.set_language("es")
    _choose(controller, _device(tv.port))
    notes = []
    controller.notify.connect(lambda *note: notes.append(note))
    window = MainWindow(controller)
    before = _tasks(controller)
    controller.start_mirror()
    _until(lambda: controller.session_future is None, 30)
    error = ("TCL 55QM7L no aceptó el emparejamiento de MirrorBridge (pair-verify: M4 error 2 (Authentication)). Usa "
             "Dispositivo > Olvidar emparejamiento, vuelve a transmitir y escribe el PIN nuevo que muestra la TV.")
    assert notes == [("La duplicación de pantalla falló", error, True)]
    assert window.status_label.text() == f"Falló: {error}"
    assert controller.cast_state == State.FAILED and controller.status()[0] == "idle"
    _until(lambda: _tasks(controller) == before, 5)


def test_a_tv_that_ends_mirroring_says_so(controller, tv):
    _ffmpeg()
    _choose(controller, _device(tv.port))
    notes = []
    controller.notify.connect(lambda *note: notes.append(note))
    controller.start_mirror()
    _until(lambda: controller.cast_state == State.CASTING, 30)
    _until(lambda: sum(1 for frame in tv.videos[0].frames if frame[1]) >= 2, 15)  # past the second keyframe
    controller.runner.loop.call_soon_threadsafe(tv.end_mirroring)
    _until(lambda: controller.session_future is None, 30)
    assert notes[-1] == ("Screen mirroring stopped", "The TV ended screen mirroring", False)


def test_quit_stops_mirroring(controller, tv):
    _ffmpeg()
    _choose(controller, _device(tv.port))
    controller.start_mirror()
    _until(lambda: controller.cast_state == State.CASTING, 30)
    session = controller.session
    controller.shutdown()
    assert session.finished.is_set() and tv.teardowns == 1
    assert session._ffmpeg.returncode is not None  # pylint: disable=protected-access
    _until(lambda: controller.session is None)
    controller.runner.thread.join(5)


# -- one session at a time ----------------------------------------------------------------------------


class _Cast:
    """A cast that runs until it is stopped."""

    def __init__(self, _manager, _device, _settings, on_status) -> None:
        self.on_status = on_status
        self.virtual_device = ""
        self.error = None
        self.finished = threading.Event()
        self._stop = None

    async def run(self) -> None:
        self._stop = asyncio.Event()
        self.on_status(State.CASTING, "")
        try:
            await self._stop.wait()
        finally:
            self.on_status(State.STOPPED, "Stopped")
            self.finished.set()

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()


def test_a_cast_or_a_test_keeps_mirror_screen_off(controller, monkeypatch):
    from mirrorbridge.gui import controller as controller_module

    monkeypatch.setattr(controller_module, "CastSession", _Cast)
    _choose(controller, _device())
    controller.start_cast()
    _until(lambda: controller.cast_state == State.CASTING)
    tray = _tray(controller)
    items = _items(tray)
    assert "Stop casting" in items and not items[MIRROR].isEnabled()
    controller.start_mirror()
    assert isinstance(controller.session, _Cast) and not controller.mirroring
    assert controller.status() == ("active", "Casting to TCL 55QM7L")
    items["Stop casting"].trigger()
    _until(lambda: controller.session_future is None)
    assert _items(_tray(controller))[MIRROR].isEnabled()

    controller.probe_future = concurrent.futures.Future()  # Test this TV is running
    try:
        assert not _items(_tray(controller))[MIRROR].isEnabled()
        controller.start_mirror()
        assert controller.session is None
    finally:
        controller.probe_future = None
