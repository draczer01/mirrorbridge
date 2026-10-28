"""More options > Test this TV (and `mirrorbridge probe`): three ways of sending a TV video,
tried one after the other against the fake play-queue TV of test_patch.py, with a report.

The third test is a real cast of the test pattern, so it needs FFmpeg; the command line and
the dialogs are tested on reports made up here.
"""

import argparse
import asyncio
import importlib.util
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyatv import exceptions
from test_patch import PLAYING, TCL_FEATURES, TV_AGENT, QueueAtv, QueueTV

from mirrorbridge import airplay_patch, airplay_queue, cli, ffmpeg, probe
from mirrorbridge.airplay_patch import ReceiverRefusedVideo
from mirrorbridge.config import Settings
from mirrorbridge.gui import i18n
from mirrorbridge.hls_server import Activity
from mirrorbridge.session import CastSession

ROOT = Path(__file__).resolve().parent.parent
REPORT_FILE = Path("C:/Users/Ana/AppData/Roaming/MirrorBridge/probe-20261028-201500.log")
STEPS = {"setup": 200, "record": 200, "setup130": 200, "insert": 200, "isInterestedInDateRange": 200,
         "actionAtItemEnd": 200, "rate": 200}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(probe, "PAUSE_SECONDS", 0)
    yield
    i18n.set_language("en")
    airplay_patch._ROUTES.clear()


def _device(**changes):
    values = dict(name="Sala familiar", address="127.0.0.1", identifier="TCL", features=TCL_FEATURES, is_apple=False,
                  requires_password=False, needs_password=False, needs_pairing=False, can_cast_video=True,
                  status="paired", status_key="paired", is_paired=True,
                  details=lambda: "features=0x7F8AD0,0x38BCF46")
    return SimpleNamespace(**{**values, **changes})


class FakeManager:
    def __init__(self, tv=None, devices=()):
        self.tv = tv
        self.devices = list(devices)
        self.connected = 0

    async def scan(self, timeout=5, hosts=None):
        return self.devices

    async def connect(self, _device):
        self.connected += 1
        return QueueAtv(self.tv)


def _report(*results):
    return probe.ProbeReport("Sala familiar", list(results), REPORT_FILE)


REFUSED = probe.TestResult(airplay_patch.PLAY, steps={"play": 404},
                           error=airplay_patch.refusal(404, {"play": 404}, apple=False))
PLAYED = probe.TestResult(airplay_patch.QUEUE, accepted=True, playing=True, steps=STEPS)
TAKEN = probe.TestResult(airplay_patch.QUEUE, accepted=True, steps=STEPS)  # answered 2xx, and then nothing
FETCHED = probe.TestResult("live", accepted=True, playing=True, steps=STEPS,
                           tv=Activity(requests=3, user_agent=TV_AGENT, first_playlist_at=1.0, first_segment_at=2.0))
NOT_FETCHED = probe.TestResult("live", accepted=True, steps=STEPS)
LIVE_REFUSED = probe.TestResult("live", steps={**STEPS, "insert": 400}, error=airplay_patch.queue_refused(
    airplay_queue.QueueRefused("insert", 400), {**STEPS, "insert": 400}))


# -- against the fake TV ---------------------------------------------------------------------------


def _has_ffmpeg():
    try:
        ffmpeg.find_ffmpeg(None)
    except ffmpeg.FFmpegNotFound:
        return False
    return True


@pytest.mark.skipif(not _has_ffmpeg(), reason="the third test casts the test pattern with FFmpeg")
async def test_the_probe_tries_each_way_and_says_what_the_tv_did(monkeypatch, tmp_path):
    wait_for_segments = CastSession._wait_for_segments

    async def one_segment(self, playlist, count=3, timeout=25, master=None):  # a quicker start
        await wait_for_segments(self, playlist, 1, timeout, master)

    monkeypatch.setattr(CastSession, "_wait_for_segments", one_segment)
    pyatv_level = logging.getLogger("pyatv").level
    handlers = list(logging.getLogger().handlers)
    progress = []
    settings = Settings(capture_audio=False, http_port=0, extend_screen=True)
    async with QueueTV(said=PLAYING, fetch=True) as tv:  # it plays until it is stopped
        manager = FakeManager(tv)
        report = await asyncio.wait_for(probe.run(
            manager, _device(), settings, seconds=0.5, public_url="http://127.0.0.1:9/bipbop.m3u8",
            progress=lambda *step: progress.append(step), report_folder=tmp_path,
        ), 120)

    assert progress == [(n, 3, probe._NAMES[test]) for n, test in enumerate(probe.TESTS, 1)]
    play, queue, live = report.results
    assert isinstance(play.error, ReceiverRefusedVideo) and play.error.code == 404 and not play.accepted
    assert play.verdict() == "refused (play=404)"  # no /play here, and no fallback in a test
    assert queue.accepted and queue.playing and queue.error is None, queue.error
    assert queue.verdict() == ("the TV took it and said it was playing (setup=200 record=200 setup130=200 insert=200 "
                               "isInterestedInDateRange=200 actionAtItemEnd=200 rate=200)")
    assert live.accepted and live.error is None, live.error
    assert live.tv.first_playlist_at is not None and live.tv.first_segment_at is not None
    assert live.verdict() == f"the TV took it and fetched the video (User-Agent: {TV_AGENT})"
    assert report.worked
    assert report.conclusion() == "Sala familiar should play your casts: it fetched MirrorBridge's video."
    assert [line.split(":")[0] for line in report.lines()] == [
        "1. POST /play (AirPlay video v1) with Apple's test video",
        "2. The play queue (AirPlay video v2) with Apple's test video",
        "3. The play queue with MirrorBridge's test pattern, from this PC",
    ]
    assert settings.capture_backend == "auto" and settings.extend_screen  # the user's own settings are kept

    # Each test on a connection of its own, and each video stopped and its session ended
    assert manager.connected == tv.connections == 3
    assert tv.names().count("stop") == tv.names().count("teardown") == 2
    assert not airplay_patch._ROUTES  # what the tests asked for says nothing about the TV's own way

    # The report: everything, pyatv's details included; the other logs as they were
    text = report.report_file.read_text(encoding="utf-8")
    assert report.report_file.parent == tmp_path and report.report_file.name.startswith("probe-")
    for words in ("is testing Sala familiar (127.0.0.1): features=0x7F8AD0,0x38BCF46",
                  "Test 1 of 3: refused (play=404)", "The TV says playbackState/playing",
                  "Ended the AirPlay video session: stop=200 teardown=200", "The TV fetched 3 files from this PC",
                  f"User-Agent: {TV_AGENT}", "DEBUG   pyatv", report.conclusion()):
        assert words in text, words
    assert logging.getLogger("pyatv").level == pyatv_level and logging.getLogger().handlers == handlers


async def test_a_tv_that_takes_nothing_is_said_so(tmp_path):
    async with QueueTV(refuse={"setup130": 501}) as tv:
        manager = FakeManager(tv)
        results = [await probe._public(manager, _device(), route, "http://127.0.0.1:9/x.m3u8", 0.2)
                   for route in (airplay_patch.PLAY, airplay_patch.QUEUE)]
        assert tv.names()[-1] == "teardown"
    report = _report(*results, LIVE_REFUSED)
    assert results[1].verdict() == "refused (setup=200 record=200 setup130=501)"
    assert not report.worked and report.conclusion() == "None of the tests got Sala familiar to take the video."


def test_a_tv_that_plays_only_the_internet_video_is_pointed_at_the_firewall():
    report = _report(REFUSED, PLAYED, NOT_FETCHED)
    assert NOT_FETCHED.verdict() == (
        "the TV took it but never fetched it from this PC (setup=200 record=200 setup130=200 insert=200 "
        "isInterestedInDateRange=200 actionAtItemEnd=200 rate=200)")
    assert report.worked and report.conclusion() == (
        "Sala familiar plays video from the internet, but never fetched MirrorBridge's own stream from this PC. "
        "Check that Windows Firewall allows MirrorBridge on private networks; if it does, this TV may only play "
        "video from HTTPS addresses.")


def test_a_tv_that_only_takes_the_request_is_not_said_to_play_it():
    """A 2xx answer is not playback: neither the firewall nor HTTPS is blamed for what was not seen."""
    report = _report(REFUSED, TAKEN, NOT_FETCHED)
    assert TAKEN.verdict().startswith("the TV took it but did not say that it was playing (setup=200")
    assert not TAKEN.worked and not report.worked
    assert report.conclusion() == (
        "Sala familiar takes the video it is sent, but its player did not start: it did not say that it played "
        "Apple's video, and never fetched MirrorBridge's stream from this PC. Please send the report with your bug "
        "report.")


def test_a_tv_that_takes_apples_video_but_refuses_mirrorbridges_is_told_which_step():
    report = _report(REFUSED, PLAYED, LIVE_REFUSED)
    assert report.conclusion().startswith(
        "Sala familiar took Apple's video, but not MirrorBridge's stream: refused (setup=200 record=200 setup130=200 "
        "insert=400")
    assert "Firewall" not in report.conclusion()


@pytest.mark.parametrize("tv, words", [
    (Activity(requests=1, user_agent=TV_AGENT, first_playlist_at=1.0),
     "the TV took it and fetched the playlist, but none of the video"),
    (Activity(missing=2, missing_path="/live.m3u8", user_agent=TV_AGENT),
     "the TV took it and reached this PC, but asked for /live.m3u8, which MirrorBridge does not serve"),
    (Activity(preflights=1, user_agent=TV_AGENT),
     "the TV took it and asked from a web page whether it may fetch it (OPTIONS), but never fetched it"),
])
def test_a_tv_that_reached_this_pc_is_not_pointed_at_the_firewall(tv, words):
    live = probe.TestResult("live", accepted=True, steps=STEPS, tv=tv)
    report = _report(REFUSED, PLAYED, live)
    assert live.verdict() == f"{words} (User-Agent: {TV_AGENT})" and not live.worked
    assert report.conclusion() == (f"Sala familiar reached this PC for MirrorBridge's stream, but did not play it: "
                                   f"{words} (User-Agent: {TV_AGENT}). Please send the report with your bug report.")


async def test_a_tv_that_hangs_up_after_taking_apples_video_has_not_played_it(monkeypatch):
    class HangingUpTV(QueueTV):
        """It takes the item, then drops the connection on the next command."""

        def _respond(self, name, request, state):
            if name == "isInterestedInDateRange":
                raise ConnectionResetError("hung up")
            return super()._respond(name, request, state)

        async def _answer_in_turn(self, pending, writer, session, state):
            try:
                await super()._answer_in_turn(pending, writer, session, state)
            except ConnectionResetError:
                writer.close()

    async with HangingUpTV(said=()) as tv:
        result = await probe._public(FakeManager(tv), _device(), airplay_patch.QUEUE, "http://127.0.0.1:9/x.m3u8", 2)
    assert result.accepted and not result.worked
    assert result.steps["isInterestedInDateRange"] == "connection lost"
    assert result.verdict() == ("failed: The TV ended the AirPlay session before it played the video: the TV closed "
                                "the connection.")  # without "use Test this TV": this is it


def test_the_firewall_is_not_named_for_apples_video():
    error = airplay_queue.media_error({"domain": "CoreMediaErrorDomain", "code": -17221}, "devstreaming-cdn.apple.com")
    assert str(error) == ("The TV's player could not play the video (error -17221, CoreMediaErrorDomain). Its player "
                          "could not download the video from devstreaming-cdn.apple.com.")
    assert "Firewall" in airplay_queue.media_error({"domain": "CoreMediaErrorDomain", "code": -17221})


def test_a_video_the_tv_ended_early_says_so():
    ended = probe.TestResult(airplay_patch.QUEUE, accepted=True, playing=True, steps={"insert": 200},
                             ended=airplay_queue.END_HUNG_UP)
    assert ended.verdict() == "the TV took it and said it was playing (insert=200), then the TV closed the connection"
    i18n.set_language("es")
    assert i18n.say(ended.verdict()) == ("la TV lo aceptó y dijo que lo estaba reproduciendo (insert=200); después, "
                                         "la TV cerró la conexión")


# -- the command line --------------------------------------------------------------------------------


def test_the_command_line_prints_each_test_and_where_the_report_is(monkeypatch, capsys):
    calls = []

    async def run(manager, device, settings, *, seconds, public_url, progress):
        calls.append((device.name, seconds, public_url))
        progress(1, 3, probe._NAMES[airplay_patch.PLAY])
        failed = probe.TestResult("live", accepted=True, steps=STEPS, error=exceptions.PlaybackError(
            airplay_queue.media_error({"domain": "CoreMediaErrorDomain", "code": -17221})))
        return _report(REFUSED, PLAYED, failed)

    monkeypatch.setattr(cli, "DeviceManager", lambda _loop: FakeManager(devices=[_device()]))
    monkeypatch.setattr(probe, "run", run)
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings()))
    pyatv_level = logging.getLogger("pyatv").level
    try:
        code = cli.main(["probe", "Sala familiar", "--seconds", "5"])
    finally:
        logging.getLogger("pyatv").setLevel(pyatv_level)
    out = capsys.readouterr().out
    assert calls == [("Sala familiar", 5.0, probe.PUBLIC_STREAM)]
    assert "Test 1 of 3: POST /play (AirPlay video v1) with Apple's test video..." in out
    assert "1. POST /play (AirPlay video v1) with Apple's test video: refused (play=404)" in out
    assert ("3. The play queue with MirrorBridge's test pattern, from this PC: failed: The TV's player could not play "
            "the video (error -17221, CoreMediaErrorDomain). Its player could not download the video") in out
    assert "Sala familiar plays video from the internet, but never fetched MirrorBridge's own stream" in out
    assert "If the TV asks whether to allow MirrorBridge, choose Allow." in out
    assert f"The full report is in {REPORT_FILE}: please send it with your bug report." in out
    assert code == 0  # something worked: the TV played the video from the internet


def test_errors_name_the_test_as_the_command_line_runs_it():
    error = airplay_patch.queue_refused(airplay_queue.QueueRefused("insert", 400), {"insert": 400})
    assert cli._cli_words(str(error), _device()).endswith(
        'Use probe -d "Sala familiar" to see which way of sending video it takes, and send the test\'s report with '
        'your bug report.')


def test_the_exe_opens_a_console_for_every_command():
    spec = importlib.util.spec_from_file_location("mirrorbridge_entry", ROOT / "packaging" / "mirrorbridge_entry.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    commands = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
    assert entry.SUBCOMMANDS == set(commands) - {"gui"}
    assert entry._wants_console(["-v", "probe"])


# -- the tray menu and the dialogs, offscreen ---------------------------------------------------------

_APP = None


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
    yield c
    c.shutdown()


def _more_options(controller):
    from mirrorbridge.gui.tray import TrayMenu

    tray = TrayMenu(controller, lambda: None, lambda: None)
    tray._rebuild()
    more = next(a.menu() for a in tray.menu.actions() if a.text() == i18n.tr("more"))
    return {a.text(): a for a in more.actions()}


def _until(condition, seconds=5):
    import time

    from PySide6.QtWidgets import QApplication

    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        QApplication.processEvents()
        time.sleep(0.01)


def test_test_this_tv_runs_the_tests_and_shows_what_they_found(controller, monkeypatch):
    import threading

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPushButton

    controller.set_language("es")
    assert not _more_options(controller)["Probar esta TV…"].isEnabled()  # no TV chosen
    device = _device()
    controller.devices = [device]
    controller.settings.last_device_id = device.identifier
    assert _more_options(controller)["Probar esta TV…"].isEnabled()

    release, shown, intro = threading.Event(), [], []
    report = _report(REFUSED, PLAYED, FETCHED)

    async def run(manager, tv, settings, *, progress):
        progress(2, 3, probe._NAMES[airplay_patch.QUEUE])
        while not release.is_set():
            await asyncio.sleep(0.01)
        return report

    monkeypatch.setattr(probe, "run", run)
    monkeypatch.setattr(controller, "show_probe_report", shown.append)

    def start() -> None:  # inside the first dialog's exec()
        dialog = QApplication.activeModalWidget()
        intro.append(dialog.text())
        next(b for b in dialog.findChildren(QPushButton) if b.text() == "Empezar la prueba").click()

    QTimer.singleShot(0, start)
    controller.test_tv()
    assert intro and intro[0].startswith("MirrorBridge probará tres formas de enviar un video a Sala familiar")
    _until(lambda: "prueba 2" in controller.status()[1])
    assert controller.is_active and controller.status() == ("busy", "Probando Sala familiar: prueba 2 de 3…")
    assert not _more_options(controller)["Probar esta TV…"].isEnabled()  # one test at a time
    release.set()
    _until(lambda: controller.probe_future is None)
    assert shown == [report] and not controller.is_active


def test_stop_casting_stops_the_test(controller, monkeypatch):
    import threading

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    device = _device()
    controller.devices = [device]
    controller.settings.last_device_id = device.identifier
    started, errors, torn_down = [], [], threading.Event()
    release = threading.Event()

    async def run(manager, tv, settings, *, progress):
        started.append(True)
        try:
            await asyncio.sleep(60)
        finally:  # stopping the video on the TV and ending its session take a while
            while not release.is_set():
                await asyncio.sleep(0.01)
            torn_down.set()

    monkeypatch.setattr(probe, "run", run)
    monkeypatch.setattr(controller, "show_error", lambda *args: errors.append(args))
    def start() -> None:
        dialog = QApplication.activeModalWidget()
        next(b for b in dialog.buttons() if dialog.buttonRole(b) == QMessageBox.AcceptRole).click()

    QTimer.singleShot(0, start)
    controller.test_tv()
    _until(lambda: started)
    controller.stop_cast()
    _until(lambda: controller.status()[1] == "Testing Sala familiar: Stopping…")
    # Until the test has torn its session down, nothing else may start
    assert controller.is_active and not _more_options(controller)["Test this TV…"].isEnabled()
    release.set()
    _until(lambda: controller.probe_future is None)
    assert torn_down.is_set() and not errors and controller.status()[0] == "idle"


def test_quit_waits_for_the_test_to_stop(controller, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    device = _device()
    controller.devices = [device]
    controller.settings.last_device_id = device.identifier
    started, torn_down = [], []

    async def run(manager, tv, settings, *, progress):
        started.append(True)
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.3)
            torn_down.append(True)

    monkeypatch.setattr(probe, "run", run)

    def start() -> None:
        dialog = QApplication.activeModalWidget()
        next(b for b in dialog.buttons() if dialog.buttonRole(b) == QMessageBox.AcceptRole).click()

    QTimer.singleShot(0, start)
    controller.test_tv()
    _until(lambda: started)
    controller.shutdown()
    assert torn_down


def test_the_report_dialog_speaks_the_users_language(controller):
    from PySide6.QtWidgets import QPushButton

    controller.set_language("es")
    box = controller.probe_report_box(_report(REFUSED, PLAYED, FETCHED))
    assert box.windowTitle() == "Terminó la prueba de Sala familiar"
    assert box.text() == "Sala familiar debería reproducir tus transmisiones: descargó el video de MirrorBridge."
    assert box.informativeText().splitlines() == [
        "1. POST /play (video AirPlay v1) con el video de prueba de Apple: rechazado (play=404)",
        "2. La cola de reproducción (video AirPlay v2) con el video de prueba de Apple: la TV lo aceptó y dijo que lo "
        "estaba reproduciendo (setup=200 record=200 setup130=200 insert=200 isInterestedInDateRange=200 "
        "actionAtItemEnd=200 rate=200)",
        f"3. La cola de reproducción con la imagen de prueba de MirrorBridge, desde este equipo: la TV lo aceptó y "
        f"descargó el video (User-Agent: {TV_AGENT})",
        "",
        f"El informe completo está en {REPORT_FILE}: mándalo con tu reporte del problema.",
    ]
    assert sorted(b.text() for b in box.findChildren(QPushButton)) == ["Abrir carpeta de registros", "Aceptar"]
