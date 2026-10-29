"""Mirror screen in the tray menu (and the window): the main item shows the screen on the chosen
TV the best way it takes (Mirror screen for a TV that offers screen mirroring and does not need
FairPlay, the old cast for an Apple TV), with the old cast under More options; Mirror screen is
run as a cast is run, shown and stopped as a cast is, and one session at a time. Its settings
have a menu of their own.

The sessions go to the TCL 55QM7L of fake_mirror_tv.py, which runs on the controller's own
asyncio loop. Qt runs offscreen, on a controller that neither scans the network nor tests
encoders, with the settings and mirroring.json in a temporary folder.
"""

import asyncio
import concurrent.futures
import os
import threading
import time
from types import SimpleNamespace

import pytest

from fake_mirror_tv import CREDENTIALS, FakeMirrorTV
from mirrorbridge import ffmpeg, vdisplay
from mirrorbridge.config import Settings
from mirrorbridge.gui import i18n
from mirrorbridge.mirror import MirrorSession, profiles
from mirrorbridge.mirror.negotiation import MEDIA_FIRST
from mirrorbridge.mirror.session import AT_ONCE_SECONDS
from mirrorbridge.session import State
from mirrorbridge.vdisplay import DriverStatus, VddError

TCL_TXT = {"features": "0x7F8AD0,0x38BCF46", "srcvers": "377.40.00", "model": "55QM7L", "manufacturer": "TCL"}
APPLE_TV_TXT = {"features": "0x4A7FDFD5,0xBC157FDE", "srcvers": "690.7.1", "model": "AppleTV11,1"}  # bit 14
NO_SCREEN_TXT = {**TCL_TXT, "features": "0x7F8A50,0x38BCF46"}  # the TCL's, without bit 7 (screen mirroring)
SPEAKER_TXT = {"features": "0x445F8A00,0x1C340", "model": "Era 100", "manufacturer": "Sonos"}  # audio only
SHOW = "Show my screen on TCL 55QM7L"
OLD_WAY = "Transmit (old way, 3-7 s delay)"
MIRRORING_HINT = ("Your screen appears on the TV almost at once. No sound yet: for a video with sound, use Play a "
                  "video file.")
VIRTUAL = r"\\.\DISPLAY9"

_APP = None  # one QApplication for every test here; it must outlive them


def _ffmpeg() -> None:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264", mirror=True):
        pytest.skip("this FFmpeg has no libx264")


def _device(port: int = 7000, credentials=CREDENTIALS, txt=None, name: str = "TCL 55QM7L", host: str = "127.0.0.1"):
    """A TV as discovery finds it (devices.Device), with or without MirrorBridge's pairing."""
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import PairingRequirement, Protocol

    from mirrorbridge.devices import Device

    conf = AppleTV(host, name)
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
    return {action.text(): action for action in more.actions() if not action.isSeparator()}


def _main(tray):
    """The main item: the one after the status line."""
    return [action for action in tray.menu.actions() if not action.isSeparator()][2]


def _submenu(menu, title: str):
    return next(action.menu() for action in menu.actions() if action.text() == title)


def _choices(menu) -> dict:
    return {action.text(): action for action in menu.actions() if not action.isSeparator()}


def _checked(menu) -> list[str]:
    return [text for text, action in _choices(menu).items() if action.isChecked()]


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


def test_the_main_item_shows_the_screen_on_the_chosen_tv(controller, monkeypatch):
    """It is what the window's big button does (start_primary), right under the status line; the
    old cast is under More options, first."""
    calls = []
    monkeypatch.setattr(controller, "start_primary", lambda *_args: calls.append("primary"))
    monkeypatch.setattr(controller, "start_cast", lambda *_args: calls.append("cast"))
    tray = _tray(controller)
    assert tray.menu.toolTipsVisible()
    item = _main(tray)
    assert item.text() == "Show my screen (choose a TV under Device)" and not item.isEnabled()  # no TV chosen
    assert item.toolTip() == ("No TV found. Check that the TV is on and that this computer is on the same Wi-Fi "
                              "network, then press Search again.")

    _choose(controller, _device())
    tray._rebuild()  # pylint: disable=protected-access
    texts = _texts(tray)
    assert texts[:3] == ["Open MirrorBridge", "Not casting", SHOW]
    assert not [text for text in texts if text.startswith(("Start casting", "Mirror screen ("))]
    item = _main(tray)
    assert item.isEnabled() and controller.primary_action() == "mirror" and item.toolTip() == MIRRORING_HINT
    item.trigger()
    more = _more(tray)
    assert list(more)[0] == OLD_WAY and more[OLD_WAY].isEnabled()
    assert more[OLD_WAY].toolTip() == ("The cast from before, for a TV that does not show your screen otherwise: it "
                                       "is a few seconds behind, but it works with more TVs and has sound.")
    more[OLD_WAY].trigger()
    assert calls == ["primary", "cast"]
    assert "Include computer sound (only when transmitting)" in texts  # Mirror screen has no sound yet
    assert _items(tray)["Play a video file…"].isEnabled()


def test_nothing_on_the_tv_tries_another_way_from_the_menu(controller, monkeypatch):
    """While the main item's way runs and the TV takes another, the menu has the window's "Try
    another way" under Stop; the TV's last way has none."""
    from mirrorbridge import routes
    from mirrorbridge.googlecast import CastDevice

    started = []  # the ways started, in place of the sessions
    monkeypatch.setattr(controller, "_start_route", lambda screen, route: started.append(route))
    monkeypatch.setattr(type(controller), "is_active", property(lambda self: bool(started)))
    controller.casts = [CastDevice("TCL 55QM7L", "127.0.0.1", identifier="tcl")]  # the TCL by Google Cast too
    _choose(controller, _device())
    another = "Nothing on the TV? Try another way"
    assert another not in _texts(_tray(controller))  # nothing runs
    controller.start_primary()
    tray = _tray(controller)
    assert _texts(tray)[2:4] == ["Stop casting", another]

    def next_way(tray) -> None:
        count = len(started)
        _items(tray)[another].trigger()
        assert controller._go_on()  # pylint: disable=protected-access  # once the session has stopped
        _until(lambda: len(started) == count + 1)

    next_way(tray)
    assert started == [routes.AIRPLAY_MIRROR, routes.CAST_MIRROR]
    assert controller.routes.last(controller.selected_screen.identifier, routes.AIRPLAY_MIRROR) == "failed"
    controller.set_language("es")
    tray = _tray(controller)
    assert _texts(tray)[2:4] == ["Detener transmisión", "¿No se ve en la TV? Probar otra forma"]  # the old cast
    controller.set_language("en")
    next_way(_tray(controller))
    assert started == [routes.AIRPLAY_MIRROR, routes.CAST_MIRROR, routes.AIRPLAY_CAST]
    assert _texts(_tray(controller))[2] == "Stop casting" and another not in _texts(_tray(controller))  # the last way


def test_mirror_screen_pairs_first(controller, monkeypatch):
    """Without MirrorBridge's pairing, the main item still mirrors: it pairs (the TV shows a PIN),
    then mirrors."""
    _choose(controller, _device(credentials=None))
    item = _main(_tray(controller))
    assert item.isEnabled() and item.toolTip() == (
        "The first time, the TV shows a 4-digit code: type it in when MirrorBridge asks. " + MIRRORING_HINT)
    asked = []
    monkeypatch.setattr(controller, "pair",
                        lambda device=None, then=None, always=False: asked.append((device.name, then, always)))
    item.trigger()
    assert asked == [("TCL 55QM7L", controller._mirror_after_pairing, True)]  # pylint: disable=protected-access
    assert not controller.is_active and controller.session is None
    controller._mirror_after_pairing()  # pylint: disable=protected-access  # a pairing that did not take
    assert not controller.is_active and len(asked) == 1  # nothing starts, and it does not pair again in a loop


def test_an_apple_tv_is_cast_to(controller, monkeypatch):
    """Mirror screen needs FairPlay there: the main item casts, as the window's big button does."""
    _choose(controller, _device(txt=APPLE_TV_TXT, name="Living Room"))
    casts = []
    monkeypatch.setattr(controller, "start_cast", lambda *_args: casts.append("cast"))
    tray = _tray(controller)
    item = _main(tray)
    assert item.text() == "Show my screen on Living Room" and item.isEnabled()
    assert item.toolTip() == ("On an Apple TV your screen appears a few seconds behind, with sound. Instant "
                              "mirroring for Apple TV is coming in a later version.")
    item.trigger()
    assert casts == ["cast"] and controller.primary_action() == "cast"
    assert _more(tray)[OLD_WAY].isEnabled()  # the same cast
    controller.start_mirror()
    assert not controller.is_active


@pytest.mark.parametrize("txt", [NO_SCREEN_TXT, SPEAKER_TXT], ids=["no-bit-7", "speaker"])
def test_a_receiver_that_does_not_mirror_is_cast_to_or_left_out(controller, txt):
    from mirrorbridge.gui.window import MainWindow

    _choose(controller, _device(txt=txt, name="Kitchen"))
    item = _main(_tray(controller))
    assert item.text() == "Show my screen on Kitchen"
    if txt is NO_SCREEN_TXT:  # it takes a cast: the main item casts
        assert item.isEnabled() and item.toolTip() == "Your screen appears on this TV a few seconds behind, with sound."
    else:  # a speaker shows no picture
        assert not item.isEnabled() and item.toolTip() == "A speaker: it plays sound only and cannot show your screen"
    window = MainWindow(controller)
    # The window's big button casts to a TV that does not mirror, and is off for a speaker
    assert controller.primary_action() == ("cast" if txt is NO_SCREEN_TXT else "")
    assert window.show_btn.isEnabled() == (txt is NO_SCREEN_TXT)
    controller.start_mirror()
    assert not controller.is_active and controller.session is None
    controller.set_language("es")
    assert _main(_tray(controller)).toolTip() == (
        "Tu pantalla aparece en esta TV con unos segundos de retraso, con sonido." if txt is NO_SCREEN_TXT
        else "Una bocina: solo reproduce sonido y no puede mostrar tu pantalla")


def test_mirror_screen_in_spanish(controller):
    from mirrorbridge.gui.window import MainWindow

    controller.set_language("es")
    _choose(controller, _device())
    tray = _tray(controller)
    item = _main(tray)
    assert item.text() == "Mostrar mi pantalla en TCL 55QM7L" and item.isEnabled()
    assert item.toolTip().startswith("Tu pantalla aparece en la TV casi al instante.")
    assert list(_more(tray))[0] == "Transmitir (modo anterior, 3-7 s de retraso)"
    assert "Incluir el sonido del equipo (solo al transmitir)" in _texts(tray)
    quality = _submenu(tray.menu, "Calidad")
    assert [action.text() for action in quality.actions()] == [
        "Resolución", "Cuadros por segundo (al transmitir)", "Retraso (al transmitir)", "Tasa de bits", "Codificador"]
    mirror_settings = _submenu(tray.menu, "Ajustes de Duplicar pantalla")
    assert list(_choices(_submenu(mirror_settings, "Latencia"))) == ["Baja", "Normal", "Suave"]
    assert list(_choices(_submenu(mirror_settings, "Cuadros por segundo"))) == ["Automático", "30 fps", "60 fps"]
    window = MainWindow(controller)
    assert window.show_btn.text() == "Mostrar mi pantalla en TCL 55QM7L" and window.show_btn.isEnabled()
    assert window.action_hint.text().startswith("Tu pantalla aparece en la TV casi al instante.")

    _choose(controller, _device(credentials=None))
    assert _main(_tray(controller)).toolTip().startswith(
        "La primera vez, la TV muestra un código de 4 números: escríbelo cuando MirrorBridge lo pida. Tu pantalla")
    _choose(controller, _device(txt=APPLE_TV_TXT))
    assert _main(_tray(controller)).toolTip().startswith(
        "En una Apple TV tu pantalla aparece con unos segundos de retraso, con sonido.")
    controller.set_language("en")  # the window says it again, for the Apple TV now chosen: it casts
    assert window.show_btn.text() == "Show my screen on TCL 55QM7L" and window.show_btn.isEnabled()
    assert window.action_hint.text().startswith("On an Apple TV your screen appears a few seconds behind")
    assert window.cast_btn.isHidden()  # the big button already casts


def test_the_device_menu_lists_every_tv_however_it_was_found(controller):
    """AirPlay and Google Cast TVs (a TV found both ways once), with what choosing each means; a
    speaker is listed but cannot be chosen. The main item shows the screen on a Google Cast TV too."""
    from mirrorbridge.googlecast import CastDevice

    controller.devices = [_device(), _device(txt=SPEAKER_TXT, name="Era 100", host="192.0.2.9")]
    controller.casts = [CastDevice("Sala & cocina", "192.0.2.7", identifier="abc", model="Chromecast"),
                        CastDevice("TCL 55QM7L", "127.0.0.1", identifier="tcl")]  # the TCL again, by Google Cast
    tray = _tray(controller)
    devices = _submenu(tray.menu, "Device")
    tvs = {text: action for text, action in _choices(devices).items() if action.isCheckable()}
    assert list(tvs) == ["Sala && cocina  (Ready)", "TCL 55QM7L  (Ready)",
                         "Era 100  (A speaker: it plays sound only and cannot show your screen)"]
    assert [action.isEnabled() for action in tvs.values()] == [True, True, False]
    assert not any(action.isChecked() for action in tvs.values())

    tvs["Sala && cocina  (Ready)"].trigger()
    assert controller.settings.last_device_id == "cast:abc" and Settings.load().last_device_id == "cast:abc"
    tray._rebuild()  # pylint: disable=protected-access
    item = _main(tray)
    assert item.text() == "Show my screen on Sala && cocina" and item.isEnabled()
    assert item.toolTip() == "Your screen and its sound appear on the TV almost at once."  # as the window says
    # Google Cast mirroring has the sound and Quality's frame rate, so they are named without "when transmitting"
    texts = _texts(tray)
    assert "Include computer sound" in texts and "Include computer sound (only when transmitting)" not in texts
    assert [action.text() for action in _submenu(tray.menu, "Quality").actions()] == [
        "Resolution", "Frame rate", "Delay (when transmitting)", "Bitrate", "Encoder"]
    devices = _submenu(tray.menu, "Device: Sala && cocina")
    assert _checked(devices) == ["Sala && cocina  (Ready)"]
    items = _choices(devices)
    assert not items["Pair with PIN…"].isEnabled() and not items["Forget pairing"].isEnabled()  # AirPlay's
    assert not _items(tray)["Play a video file…"].isEnabled() and not _more(tray)[OLD_WAY].isEnabled()
    assert not _more(tray)["Test this TV…"].isEnabled()


def test_mirror_screen_settings_are_for_the_next_session(controller):
    """Mirror screen settings, next to What to cast: Latency (Normal) and Frame rate (Automatic),
    saved at once. Choosing a Latency puts More options > Debug > TV wait back on Automatic; the
    casts' own frame rate and delay, under Quality, are left alone."""
    controller.update_settings(mirror_lead_ms=200)
    tray = _tray(controller)
    texts = _texts(tray)
    assert texts.index("Mirror screen settings") == texts.index("What to cast") + 1
    mirror_settings = _submenu(tray.menu, "Mirror screen settings")
    assert [action.text() for action in mirror_settings.actions()] == ["Latency", "Frame rate"]
    latency, fps = _submenu(mirror_settings, "Latency"), _submenu(mirror_settings, "Frame rate")
    assert list(_choices(latency)) == ["Low", "Normal", "Smooth"] and _checked(latency) == ["Normal"]
    assert latency.toolTipsVisible() and [action.toolTip() for action in latency.actions()] == [
        "The TV shows each frame 60 ms after it was captured: the least delay, for a cable or a strong Wi-Fi signal.",
        "The TV shows each frame 100 ms after it was captured, or as saved for this TV: the default.",
        "The TV shows each frame 200 ms after it was captured: a little more delay, and fewer skips when the Wi-Fi "
        "falters."]
    assert list(_choices(fps)) == ["Automatic", "30 fps", "60 fps"] and _checked(fps) == ["Automatic"]
    assert fps.toolTipsVisible() and _choices(fps)["Automatic"].toolTip() == (
        "60 fps when the graphics card encodes the video, 30 when the processor does or the screen capture is slow; "
        "never more than the TV takes.")
    quality = _submenu(tray.menu, "Quality")
    assert [action.text() for action in quality.actions()] == [
        "Resolution", "Frame rate (when transmitting)", "Delay (when transmitting)", "Bitrate", "Encoder"]

    _choices(fps)["60 fps"].trigger()
    s = controller.settings
    assert (s.mirror_framerate, s.mirror_lead_ms) == (60, 200)  # the frame rate leaves the TV wait alone
    _choices(latency)["Smooth"].trigger()
    assert (s.mirror_latency, s.mirror_framerate, s.mirror_lead_ms) == ("smooth", 60, 0)
    saved = Settings.load()
    assert (saved.mirror_latency, saved.mirror_framerate, saved.mirror_lead_ms) == ("smooth", 60, 0)
    assert (s.framerate, s.latency_mode) == (30, "low")

    tray._rebuild()  # pylint: disable=protected-access
    mirror_settings = _submenu(tray.menu, "Mirror screen settings")
    assert _checked(_submenu(mirror_settings, "Latency")) == ["Smooth"]
    assert _checked(_submenu(mirror_settings, "Frame rate")) == ["60 fps"]
    _choices(_submenu(mirror_settings, "Frame rate"))["Automatic"].trigger()
    assert s.mirror_framerate == 0

    # A settings.json edited by hand: each shows what a session uses (Normal, Automatic)
    s.mirror_latency, s.mirror_framerate = "fast", 45
    tray._rebuild()  # pylint: disable=protected-access
    mirror_settings = _submenu(tray.menu, "Mirror screen settings")
    assert _checked(_submenu(mirror_settings, "Latency")) == ["Normal"]
    assert _checked(_submenu(mirror_settings, "Frame rate")) == ["Automatic"]


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

    _main(tray).trigger()  # the main item: Mirror screen, for this TV
    assert controller.is_active and controller.mirroring
    session = controller.session
    assert isinstance(session, MirrorSession) and session.source == "desktop"
    assert controller.status()[0] == "busy"
    _until(lambda: controller.cast_state == State.CASTING, 30)

    assert controller.status() == ("active", "Mirroring to TCL 55QM7L")
    assert tray.tray.toolTip() == "MirrorBridge - Mirroring to TCL 55QM7L"
    assert window.status_label.text() == "Your screen is on TCL 55QM7L"
    assert window.show_btn.text() == "Stop showing on TCL 55QM7L" and not window.file_btn.isEnabled()
    tray._rebuild()  # pylint: disable=protected-access
    items = _items(tray)
    assert _main(tray).text() == "Stop mirroring" and SHOW not in items
    assert not items["Play a video file…"].isEnabled()
    assert not _more(tray)["Test this TV…"].isEnabled() and not _more(tray)[OLD_WAY].isEnabled()

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
    assert window.show_btn.text() == "Show my screen on TCL 55QM7L" and window.file_btn.isEnabled()
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
    assert window.status_label.text() == f"No funcionó: {error}"
    assert controller.cast_state == State.FAILED and controller.status()[0] == "idle"
    _until(lambda: _tasks(controller) == before, 5)


def test_mirror_screen_goes_on_with_gdi_capture_when_ddagrab_fails(controller, tv, monkeypatch):
    """As a cast does on a PC whose graphics driver refuses Desktop Duplication: FFmpeg's first
    choice ends before its first frame, and the session the TV set up goes on with GDI capture."""
    from mirrorbridge.mirror import session as mirror_session

    _ffmpeg()
    build = mirror_session.build_command

    def command(ff, settings, *, backend="", **kw):
        if backend == "ddagrab":  # FFmpeg ends at once, as when DXGI says no
            return [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "nullsrc", "-vf", "ddagrab_refused",
                    "-f", "flv", "pipe:1"]
        return build(ff, settings, backend="test" if backend == "gdigrab" else backend, **kw)  # GDI's picture

    monkeypatch.setattr(ffmpeg, "pick_capture_backend", lambda *args: "ddagrab")
    monkeypatch.setattr(mirror_session, "build_command", command)
    _choose(controller, _device(tv.port))
    notes, states = [], []
    controller.notify.connect(lambda *note: notes.append(note))
    controller.cast_state_changed.connect(lambda state, message: states.append((state, str(message))))
    controller.start_mirror()
    _until(lambda: controller.cast_state == State.CASTING, 30)
    starting = [message for state, message in states if state == State.STARTING]
    assert starting[-2:] == ["x264 (CPU) via ddagrab", "x264 (CPU) via gdigrab"]
    _until(lambda: tv.videos and len(tv.videos[0].frames) >= 5, 15)
    controller.stop_cast()
    _until(lambda: controller.session_future is None, 30)
    assert notes == [("Mirroring to TCL 55QM7L", "", False), ("Screen mirroring stopped", "", False)]
    assert controller.cast_state == State.STOPPED and tv.records == 1 and tv.teardowns == 1


def test_a_tv_that_ends_mirroring_says_so(controller, tv):
    _ffmpeg()
    _choose(controller, _device(tv.port))
    notes = []
    controller.notify.connect(lambda *note: notes.append(note))
    controller.start_mirror()
    _until(lambda: controller.cast_state == State.CASTING, 30)
    _until(lambda: tv.videos and tv.videos[0].frames, 15)
    time.sleep(AT_ONCE_SECONDS + 0.5)  # well past it: a TV that took the picture
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


# -- with Extend screen ---------------------------------------------------------------------------------


def _virtual_screen(monkeypatch, status: DriverStatus = DriverStatus.OK) -> SimpleNamespace:
    """Extend screen's driver, as a fake that logs what it is asked, with Windows listing its
    monitor to the right of the PC's own while it is there. FFmpeg's test source stands in for it."""
    log = SimpleNamespace(events=[], screens=[])
    main = ffmpeg.Monitor(0, 0, 0, 1920, 1080, True, 0, device=r"\\.\DISPLAY1")
    added = ffmpeg.Monitor(1, 1920, 0, 1920, 1080, False, 1, device=VIRTUAL)

    class Screen(vdisplay.VirtualScreen):
        name = "fake"

        def __init__(self, on_lost=None) -> None:
            super().__init__(on_lost)
            log.screens.append(self)

        @classmethod
        def status(cls) -> DriverStatus:
            return status

        def start(self, width: int, height: int, hz: int) -> str:
            log.events.append(("start", width, height, hz))
            if status is not DriverStatus.OK:
                raise VddError(f"driver:{status.value}")
            log.events.append("on the desktop")
            return VIRTUAL

        def stop(self) -> None:
            log.events.append("stop")

    def monitors() -> list:
        there = "on the desktop" in log.events and log.events[-1] != "stop"
        return [main, added] if there else [main]

    monkeypatch.setattr(vdisplay, "available", lambda: True)
    monkeypatch.setattr(vdisplay, "backend", lambda name=vdisplay.DEFAULT_BACKEND: Screen)
    monkeypatch.setattr(ffmpeg, "list_monitors", monitors)
    monkeypatch.setattr(ffmpeg, "pick_capture_backend", lambda *args: "test")
    return log


def test_mirror_screen_sends_the_virtual_screen_as_a_cast_does(controller, tv, monkeypatch):
    _ffmpeg()
    driver = _virtual_screen(monkeypatch)
    tv.reconfigure(width=640, height=360)  # the virtual screen is fitted into the TV's
    controller.settings.extend_screen, controller.settings.capture_backend = True, "auto"
    _choose(controller, _device(tv.port))
    notes, extending = [], []
    controller.notify.connect(lambda *note: notes.append(note))
    controller.cast_state_changed.connect(
        lambda state, _message: extending.append(controller.status()) if state == State.EXTENDING else None)
    controller.start_mirror()
    _until(lambda: controller.cast_state == State.CASTING, 30)

    assert extending == [("busy", "Creating the virtual screen…")]  # in the tray's tooltip, as for a cast
    assert driver.events == [("start", 1920, 1080, 60), "on the desktop"] and controller.virtual_device == VIRTUAL
    assert str(controller.cast_message) == "Mirroring the extended screen to TCL 55QM7L (640x360, 30 fps)"
    # Once: where the new screen is.
    assert notes == [("Mirroring to TCL 55QM7L", "Drag windows past the right edge of your screen to see them on the "
                      "TV.", False)]
    assert controller.settings.extend_tip_shown
    tray = _tray(controller)
    source = next(action.menu() for action in tray.menu.actions() if action.text() == "What to cast")
    # The screen this session added is not offered as a display to cast.
    assert [action.text() for action in source.actions() if action.text().startswith("Display")] == [
        "Display 1: 1920x1080 (main)"]
    controller.stop_cast()
    _until(lambda: controller.session_future is None, 30)
    assert driver.events[-1] == "stop" and driver.events.count("stop") == 1 and controller.virtual_device == ""
    assert notes[-1] == ("Screen mirroring stopped", "", False) and len(driver.screens) == 1


def test_a_virtual_screen_that_goes_away_is_explained_in_spanish(controller, tv, monkeypatch):
    from mirrorbridge.gui.window import MainWindow

    _ffmpeg()
    driver = _virtual_screen(monkeypatch)
    tv.reconfigure(width=640, height=360)
    controller.settings.extend_screen, controller.settings.capture_backend = True, "auto"
    controller.set_language("es")
    _choose(controller, _device(tv.port))
    notes = []
    controller.notify.connect(lambda *note: notes.append(note))
    window = MainWindow(controller)
    controller.start_mirror()
    _until(lambda: controller.cast_state == State.CASTING, 30)
    threading.Thread(target=driver.screens[0].on_lost).start()  # sleep, as the driver's keep-alive sees it
    _until(lambda: controller.session_future is None, 30)
    error = ("La pantalla virtual se cerró (por ejemplo, al suspender el equipo). Vuelve a transmitir o a duplicar "
             "la pantalla.")
    assert notes[-1] == ("La duplicación de pantalla falló", error, True)
    assert window.status_label.text() == f"No funcionó: {error}" and driver.events[-1] == "stop"


def test_mirror_screen_offers_to_install_the_driver_as_a_cast_does(controller, tv, monkeypatch):
    from mirrorbridge.gui import extend

    _ffmpeg()
    driver = _virtual_screen(monkeypatch, DriverStatus.NOT_INSTALLED)
    asked = []
    monkeypatch.setattr(extend.ExtendScreenSetup, "_consent", lambda self: asked.append("install?") or False)  # Not now
    controller.update_settings(extend_screen=True, capture_backend="auto")
    _choose(controller, _device(tv.port))
    notes = []
    controller.notify.connect(lambda *note: notes.append(note))
    controller.start_mirror()
    _until(lambda: controller.session_future is None and not controller.extend.busy, 30)
    assert asked == ["install?"]  # Parsec's download and terms, as when a cast finds no driver
    # Extend screen is turned off, so the next Mirror screen sends the display; no "failed" on top.
    assert notes == [("Extend screen (virtual display)", "Extend screen is off for now, so Show my screen sends your "
                      "display. Choose it again once the driver works.", False)]
    assert not controller.settings.extend_screen and controller.vdd_status is DriverStatus.NOT_INSTALLED
    assert controller.cast_state == State.FAILED and not controller.is_active
    assert driver.events == [("start", 1920, 1080, 60), "stop"] and tv.connections == []  # the TV was not asked


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
    tray = _tray(controller)
    _more(tray)[OLD_WAY].trigger()  # the old cast, from More options
    _until(lambda: controller.cast_state == State.CASTING)
    tray._rebuild()  # pylint: disable=protected-access
    items = _items(tray)
    assert _main(tray).text() == "Stop casting" and SHOW not in items and not _more(tray)[OLD_WAY].isEnabled()
    controller.start_mirror()
    controller.start_primary()
    assert isinstance(controller.session, _Cast) and not controller.mirroring
    assert controller.status() == ("active", "Casting to TCL 55QM7L")
    items["Stop casting"].trigger()
    _until(lambda: controller.session_future is None)
    assert _main(_tray(controller)).text() == SHOW and _main(_tray(controller)).isEnabled()

    controller.probe_future = concurrent.futures.Future()  # Test this TV is running
    try:
        tray = _tray(controller)
        assert _main(tray).text() == "Stop casting" and SHOW not in _items(tray)
        controller.start_mirror()
        controller.start_primary()
        assert controller.session is None
    finally:
        controller.probe_future = None
