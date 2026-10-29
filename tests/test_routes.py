"""The best way to show the screen on each TV (routes.py), and the big button that tries the next
way by itself when one fails before the picture reaches the TV (Controller.start_primary)."""

import os

import pytest

from mirrorbridge import ffmpeg, routes, vdisplay
from mirrorbridge.config import Settings
from mirrorbridge.session import State
from mirrorbridge.googlecast import CastDevice
from mirrorbridge.text import Text
from test_mirror_gui import APPLE_TV_TXT, _device

_APP = None


def _cast(host: str = "127.0.0.1", name: str = "TCL 55QM7L", identifier: str = "c0ffee", ca: str = "4101"):
    return CastDevice(name=name, host=host, identifier=identifier, model="55QM7L", properties={"ca": ca})


def _speaker_cast(host: str = "10.0.0.9"):
    return _cast(host=host, name="Nest Mini", identifier="beef", ca="2052")  # no video out


@pytest.fixture
def history(tmp_path):
    return routes.RouteHistory(tmp_path / "routes.json")


# -- one list of TVs -------------------------------------------------------------------------------


def test_a_tv_found_by_airplay_and_google_cast_is_listed_once():
    tcl = _device()
    screens = routes.merge([tcl], [_cast(), _cast(host="10.0.0.5", name="Chromecast", identifier="abc"),
                                   _speaker_cast()])
    assert [s.name for s in screens] == ["Chromecast", "TCL 55QM7L", "Nest Mini"]  # speakers last
    both = screens[1]
    assert both.airplay is tcl and both.cast.identifier == "c0ffee" and both.identifier == tcl.identifier
    assert both.answers_to("cast:c0ffee")  # chosen while only Google Cast had found it
    assert screens[0].identifier == "cast:abc" and screens[0].airplay is None
    assert [s.shows_video for s in screens] == [True, True, False]


def test_the_ways_each_tv_takes(history):
    assert routes.plan(routes.Screen(airplay=_device(), cast=_cast()), history) == [
        routes.AIRPLAY_MIRROR, routes.CAST_MIRROR, routes.AIRPLAY_CAST]
    assert routes.plan(routes.Screen(airplay=_device(txt=APPLE_TV_TXT)), history) == [  # with FairPlay
        routes.AIRPLAY_MIRROR, routes.AIRPLAY_CAST]
    assert routes.plan(routes.Screen(cast=_cast()), history) == [routes.CAST_MIRROR]
    assert routes.plan(routes.Screen(cast=_speaker_cast()), history) == []


# -- what worked before ----------------------------------------------------------------------------


def test_what_worked_last_goes_first_and_what_the_tv_refused_goes_last(history, tmp_path):
    tv = routes.Screen(airplay=_device(), cast=_cast())
    history.failed(tv.identifier, routes.AIRPLAY_MIRROR, "refused", lasting=True)
    assert routes.plan(tv, history) == [routes.CAST_MIRROR, routes.AIRPLAY_CAST, routes.AIRPLAY_MIRROR]
    history.worked(tv.identifier, routes.AIRPLAY_CAST)
    assert routes.plan(tv, history)[0] == routes.AIRPLAY_CAST

    again = routes.RouteHistory(tmp_path / "routes.json")  # kept for the next start
    assert routes.plan(tv, again) == routes.plan(tv, history)
    again.failed(tv.identifier, routes.AIRPLAY_CAST, "the TV was off", lasting=False)  # only noted
    assert routes.plan(tv, again)[0] == routes.AIRPLAY_CAST
    again.failed(tv.identifier, routes.AIRPLAY_CAST, "format", lasting=True)
    assert routes.plan(tv, again) == [routes.CAST_MIRROR, routes.AIRPLAY_MIRROR, routes.AIRPLAY_CAST]


def test_a_broken_routes_file_is_no_history(tmp_path):
    (tmp_path / "routes.json").write_text("[not json", encoding="utf-8")
    assert routes.RouteHistory(tmp_path / "routes.json").data == {}


def test_why_a_way_failed():
    assert routes.classify(RuntimeError(Text("gc_no_launch", "x"))) == routes.LASTING_FAILURE
    assert routes.classify(RuntimeError(Text("mirror_refused", "x"))) == routes.LASTING_FAILURE
    assert routes.classify(RuntimeError(Text("gc_no_connection", "x"))) == routes.PASSING
    assert routes.classify(RuntimeError("Connection refused")) == routes.PASSING
    assert routes.classify(RuntimeError(Text("encoder_stopped", "x"))) == routes.LOCAL_FAILURE
    assert routes.classify(vdisplay.VddError("lost")) == routes.LOCAL_FAILURE


# -- the big button --------------------------------------------------------------------------------


@pytest.fixture
def controller(tmp_path, monkeypatch):
    global _APP  # pylint: disable=global-statement
    widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = widgets.QApplication.instance() or widgets.QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from mirrorbridge.gui.controller import Controller

    monkeypatch.setattr(Controller, "refresh_if_stale", lambda self, max_age=60: None)
    monkeypatch.setattr(Controller, "refresh_encoders_if_stale", lambda self, max_age=120: None)
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [ffmpeg.Monitor(0, 0, 0, 1920, 1080, True)])
    monkeypatch.setattr(ffmpeg, "list_windows", lambda: [])
    c = Controller(Settings.load())
    started = []
    monkeypatch.setattr(c, "_start_route", lambda screen, route: started.append(route))
    c.started = started
    yield c
    c.shutdown()


def _settle():
    from PySide6.QtWidgets import QApplication

    for _ in range(5):
        QApplication.processEvents()


def test_the_big_button_tries_the_next_way_when_one_fails(controller):
    controller.devices, controller.casts = [_device()], [_cast()]
    controller.settings.last_device_id = controller.devices[0].identifier
    tv = controller.selected_screen.identifier

    controller.start_primary()
    assert controller.started == [routes.AIRPLAY_MIRROR]
    states = []
    controller.cast_state_changed.connect(lambda state, message: states.append(state))
    refused = RuntimeError(Text("mirror_refused", "The TV turned it down"))
    controller._on_cast_state(State.FAILED, refused, controller.selected_device)  # pylint: disable=protected-access
    assert states == [State.CONNECTING]  # the window says it is connecting, not that it failed
    assert controller._fall_back(RuntimeError(Text("mirror_refused", "The TV turned it down")))  # pylint: disable=protected-access
    _settle()
    assert controller.started == [routes.AIRPLAY_MIRROR, routes.CAST_MIRROR]  # nothing said, the next way
    controller._route_worked()  # pylint: disable=protected-access  # the picture reached the TV
    assert controller.routes.last_worked(tv) == ""  # Google Cast's CASTING is no proof of a picture
    assert controller.plan()[0] == routes.CAST_MIRROR  # but the TV refused AirPlay: first, the next time
    assert controller.primary_action() == "mirror"
    assert controller.device_line(controller.selected_screen) == "Ready"

    # Once it has worked, a later failure is the network's: nothing else is tried
    assert not controller._fall_back(RuntimeError("Connection reset"))  # pylint: disable=protected-access


def test_the_big_button_stops_trying_for_this_pcs_own_failures_and_after_stop(controller):
    controller.devices, controller.casts = [_device()], [_cast()]
    controller.settings.last_device_id = controller.devices[0].identifier
    controller.start_primary()
    assert not controller._fall_back(RuntimeError(Text("encoder_stopped", "x")))  # pylint: disable=protected-access
    _settle()
    assert controller.started == [routes.AIRPLAY_MIRROR]

    controller.start_primary()
    controller.stop_cast()  # the person pressed Stop while it started
    assert not controller._fall_back(RuntimeError(Text("mirror_refused", "x")))  # pylint: disable=protected-access


def test_the_last_way_failing_is_reported(controller):
    controller.devices, controller.casts = [_device(txt=APPLE_TV_TXT, name="Living Room")], []
    controller.settings.last_device_id = controller.devices[0].identifier
    controller.start_primary()
    assert controller.started == [routes.AIRPLAY_CAST]
    assert not controller._fall_back(RuntimeError("no answer"))  # pylint: disable=protected-access


def test_a_tv_only_google_cast_found_can_be_chosen(controller):
    controller.devices, controller.casts = [], [_cast(host="10.0.0.5", name="Chromecast", identifier="abc")]
    controller.select_device("cast:abc")
    assert controller.selected_screen.name == "Chromecast" and controller.selected_device is None
    controller.start_primary()
    assert controller.started == [routes.CAST_MIRROR]


def test_google_cast_reaching_the_tv_is_not_proof_of_a_picture(history):
    """On the TCL, Google Cast mirroring reached CASTING with the TV black: it is counted, but it
    does not become the way to try first."""
    tv = routes.Screen(airplay=_device(), cast=_cast())
    history.worked(tv.identifier, routes.CAST_MIRROR)
    assert history.last_worked(tv.identifier) == ""
    assert routes.plan(tv, history)[0] == routes.AIRPLAY_MIRROR


def test_nothing_on_the_tv_tries_the_next_way(controller, monkeypatch):
    controller.devices, controller.casts = [_device()], [_cast()]
    controller.settings.last_device_id = controller.devices[0].identifier
    tv = controller.selected_screen.identifier
    assert not controller.can_try_another_way()  # nothing running
    controller.start_primary()
    controller._route_worked()  # pylint: disable=protected-access
    monkeypatch.setattr(type(controller), "is_active", property(lambda self: True))
    assert controller.can_try_another_way()

    controller.try_another_way()  # the person sees nothing on the TV
    assert controller.routes.last(tv, routes.AIRPLAY_MIRROR) == "failed"
    assert controller._go_on()  # pylint: disable=protected-access  # once the session has stopped
    _settle()
    assert controller.started == [routes.AIRPLAY_MIRROR, routes.CAST_MIRROR]
    assert controller.plan()[-1] == routes.AIRPLAY_MIRROR  # last for this TV from now on

    controller.try_another_way()
    controller.stop_cast()  # Stop wins: nothing else starts
    assert not controller._go_on()  # pylint: disable=protected-access


def test_a_receiver_named_by_its_serial_number_gets_a_readable_name():
    box = routes.Screen(cast=CastDevice(name="1001B026D756D451", host="192.168.1.2", identifier="x", model="IZZI BOX"))
    assert box.name == "IZZI BOX (192.168.1.2)"
    assert routes.Screen(cast=CastDevice(name="1001B026D756D451", host="192.168.1.2")).name == "Google Cast (192.168.1.2)"
    assert routes.Screen(cast=_cast()).name == "TCL 55QM7L"


def test_google_cast_is_confirmed_by_the_frames_the_tv_showed(controller, monkeypatch):
    from types import SimpleNamespace

    controller.devices, controller.casts = [_device()], [_cast()]
    controller.settings.last_device_id = controller.devices[0].identifier
    tv = controller.selected_screen.identifier
    controller.routes.failed(tv, routes.AIRPLAY_MIRROR, "refused")  # Google Cast goes first
    running = []
    monkeypatch.setattr(type(controller), "is_active", property(lambda self: bool(running)))

    controller.start_primary()
    running.append(True)
    assert controller.started == [routes.CAST_MIRROR]
    controller.session = SimpleNamespace(frames_shown=120, frames_reported=True, codec="vp8")
    controller._route_worked()  # pylint: disable=protected-access
    assert controller.routes.last_worked(tv) == ""  # not yet: the TV has not said
    for unsure in (SimpleNamespace(frames_shown=0, frames_reported=False, codec="vp8"),  # no event log
                   SimpleNamespace(frames_shown=120, frames_reported=True, codec="h264")):  # H.264 counts black frames
        saved, controller.session = controller.session, unsure
        controller._check_shown(unsure, controller._attempt)  # pylint: disable=protected-access
        assert controller.routes.last_worked(tv) == "" and controller.routes.last(tv, routes.CAST_MIRROR) != "failed"
        controller.session = saved
    controller._check_shown(controller.session, controller._attempt)  # pylint: disable=protected-access
    assert controller.routes.last_worked(tv) == routes.CAST_MIRROR

    # A black TV: none shown, so the next way starts
    controller.session = SimpleNamespace(frames_shown=0, frames_reported=True, codec="vp8", stop=lambda: None)
    monkeypatch.setattr(controller.runner, "submit", lambda *a, **k: None)
    running.clear()
    controller.start_primary()
    running.append(True)
    controller._route_worked()  # pylint: disable=protected-access
    controller._check_shown(controller.session, controller._attempt)  # pylint: disable=protected-access
    assert controller.routes.last(tv, routes.CAST_MIRROR) == "failed" and controller.routes.last_worked(tv) == ""
    assert controller._go_on()  # pylint: disable=protected-access  # once the session has stopped
    _settle()
    assert controller.started[-1] == routes.AIRPLAY_CAST
    controller.session = None
