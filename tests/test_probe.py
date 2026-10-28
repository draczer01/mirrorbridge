"""More options > Test this TV (and `mirrorbridge probe`): four ways of sending a TV video,
tried one after the other against the fake play-queue TV of test_patch.py and the fake
mirroring TV of fake_mirror_tv.py, with a report.

The third test is a real cast of the test pattern and the fourth mirrors a test pattern, so
they need FFmpeg; the command line and the dialogs are tested on reports made up here too.
"""

import argparse
import asyncio
import contextlib
import importlib.util
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from fake_mirror_tv import CREDENTIALS, FakeMirrorTV
from pyatv import exceptions
from test_patch import PLAYING, TCL_FEATURES, TV_AGENT, QueueAtv, QueueTV

from mirrorbridge import airplay_patch, airplay_queue, cli, ffmpeg, probe
from mirrorbridge.airplay_patch import ReceiverRefusedVideo
from mirrorbridge.config import Settings
from mirrorbridge.gui import i18n
from mirrorbridge.hls_server import Activity
from mirrorbridge.mirror import ProbeResult, profiles
from mirrorbridge.mirror.h264 import TRAILER_FIXED
from mirrorbridge.mirror.negotiation import MEDIA_FIRST, Plan
from mirrorbridge.mirror.probe import SetupAttempt, StreamCheck
from mirrorbridge.mirror.session import SessionReport
from mirrorbridge.session import CastSession

ROOT = Path(__file__).resolve().parent.parent
REPORT_FILE = Path("C:/Users/Ana/AppData/Roaming/MirrorBridge/probe-20261028-201500.log")
APPLE_TV_FEATURES = 0x4A7FDFD5 | (0xBC157FDE << 32)  # bit 14: FairPlay
STEPS = {"setup": 200, "record": 200, "setup130": 200, "insert": 200, "isInterestedInDateRange": 200,
         "actionAtItemEnd": 200, "rate": 200}


@pytest.fixture(autouse=True)
def quick(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "PAUSE_SECONDS", 0)
    # %APPDATA% (~/.config off Windows) in tmp_path: mirroring.json, settings.json, the reports
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    yield
    i18n.set_language("en")
    airplay_patch._ROUTES.clear()


def _device(mirror_tv=None, **changes):
    """The TV as devices.Device shows it; its AirPlay service is `mirror_tv`'s, paired, if given."""
    airplay = None
    if mirror_tv is not None:
        airplay = SimpleNamespace(port=mirror_tv.port, credentials=CREDENTIALS, properties={"srcvers": "377.40.00"})
    values = dict(name="Sala familiar", address="127.0.0.1", identifier="TCL", features=TCL_FEATURES, is_apple=False,
                  requires_password=False, needs_password=False, needs_pairing=False, can_cast_video=True,
                  status="paired", status_key="paired", is_paired=True, airplay=airplay,
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


def _mirrored(**report):
    """The fourth test, as it ends when the TV kept the video open."""
    plan = Plan(MEDIA_FIRST)
    check = StreamCheck(plan, TRAILER_FIXED, worked=True,
                        report=SessionReport(frames_sent=300, ntp_requests=48, **report))
    result = ProbeResult("Sala familiar", "127.0.0.1:7000", setups=[SetupAttempt(plan, worked=True)], streams=[check],
                         profile=profiles.MirrorProfile.of(plan, TRAILER_FIXED))
    return probe.TestResult(probe.MIRROR, accepted=True, mirror=result)


MIRRORED = _mirrored()


# -- against the fake TV ---------------------------------------------------------------------------


def _has_ffmpeg():
    try:
        ff = ffmpeg.find_ffmpeg(None)
    except ffmpeg.FFmpegNotFound:
        return False
    return ffmpeg.encoder_works(ff, "libx264", mirror=True)  # the test pattern, mirrored


@pytest.fixture
async def mirror_tv():
    tv = await FakeMirrorTV().start()
    yield tv
    await tv.stop()


@contextlib.asynccontextmanager
async def _apples_video():
    """Stands in for Apple's test video, which the fake TV fetches before it says it plays.
    (Not an unused port: Windows takes about 2 s to refuse a connection, longer than the probe
    watches the TV in these tests.)"""
    async def playlist(_request):
        return web.Response(text="#EXTM3U\n#EXT-X-ENDLIST\n")

    app = web.Application()
    app.router.add_get("/bipbop.m3u8", playlist)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/bipbop.m3u8"
    finally:
        await runner.cleanup()


@pytest.mark.skipif(not _has_ffmpeg(), reason="the last two tests send a test pattern with FFmpeg's x264")
async def test_the_probe_tries_each_way_and_says_what_the_tv_did(monkeypatch, tmp_path, mirror_tv):
    wait_for_segments = CastSession._wait_for_segments

    async def one_segment(self, playlist, count=3, timeout=25, master=None):  # a quicker start
        await wait_for_segments(self, playlist, 1, timeout, master)

    monkeypatch.setattr(CastSession, "_wait_for_segments", one_segment)
    pyatv_level = logging.getLogger("pyatv").level
    handlers = list(logging.getLogger().handlers)
    progress = []
    settings = Settings(capture_audio=False, http_port=0, extend_screen=True, encoder="libx264", output_height=360,
                        video_bitrate_kbps=1500)
    async with _apples_video() as public_url, QueueTV(said=PLAYING, fetch=True) as tv:  # it plays until stopped
        manager = FakeManager(tv)
        report = await asyncio.wait_for(probe.run(
            manager, _device(mirror_tv), settings, seconds=2, public_url=public_url,
            progress=lambda *step: progress.append(step), report_folder=tmp_path,
        ), 180)

    assert progress == [(n, 4, probe._NAMES[test]) for n, test in enumerate(probe.TESTS, 1)]
    play, queue, live, mirrored = report.results
    assert isinstance(play.error, ReceiverRefusedVideo) and play.error.code == 404 and not play.accepted
    assert play.verdict() == "refused (play=404)"  # no /play here, and no fallback in a test
    assert queue.accepted and queue.playing and queue.error is None, queue.error
    assert queue.verdict() == ("the TV took it and said it was playing (setup=200 record=200 setup130=200 insert=200 "
                               "isInterestedInDateRange=200 actionAtItemEnd=200 rate=200)")
    assert live.accepted and live.error is None, live.error
    assert live.tv.first_playlist_at is not None and live.tv.first_segment_at is not None
    assert live.verdict() == f"the TV took it and fetched the video (User-Agent: {TV_AGENT})"
    # Screen mirroring: the TCL refuses control-first and takes media-first; the test pattern went
    assert mirrored.worked and mirrored.accepted and mirrored.error is None
    check = mirrored.mirror.streams[0]
    assert mirrored.verdict() == (
        f"the TV accepted the screen mirroring session and kept the video open: {check.report.frames_sent} frames of "
        f"the test pattern went to it, and it asked for the time {check.report.ntp_requests} times. What worked: "
        f"media-first, with the audio stream, NTP timing, avcC ending 02000000, lead 100 ms")
    assert check.report.frames_sent >= 20 and check.report.ntp_requests >= 3
    assert mirrored.mirror.ptp == "answered without timingPeerInfo.ClockID"  # PTP is tried, not kept
    saved = profiles.load("TCL")  # for Mirror screen, under the device's identifier
    assert saved is not None and (saved.ordering, saved.trailer) == (MEDIA_FIRST, TRAILER_FIXED)
    assert report.worked
    assert report.conclusion() == ("Sala familiar should play your casts: it fetched MirrorBridge's video. Mirror "
                                   "screen should work on this TV: it accepted screen mirroring and kept the video "
                                   "open.")
    assert [line.split(":")[0] for line in report.lines()] == [
        "1. POST /play (AirPlay video v1) with Apple's test video",
        "2. The play queue (AirPlay video v2) with Apple's test video",
        "3. The play queue with MirrorBridge's test pattern, from this PC",
        "4. Screen mirroring with a moving test pattern, from this PC",
    ]
    assert settings.capture_backend == "auto" and settings.extend_screen  # the user's own settings are kept

    # Each test on a connection of its own, and each video stopped and its session ended
    assert manager.connected == tv.connections == 3
    assert tv.names().count("stop") == tv.names().count("teardown") == 2
    assert not airplay_patch._ROUTES  # what the tests asked for says nothing about the TV's own way
    assert mirror_tv.teardowns == len(mirror_tv.orderings) and mirror_tv.records == 1
    await asyncio.sleep(0.1)
    assert all(conn.closed for conn in mirror_tv.connections)

    # The report: everything, pyatv's details included; the other logs as they were
    text = report.report_file.read_text(encoding="utf-8")
    assert report.report_file.parent == tmp_path and report.report_file.name.startswith("probe-")
    for words in ("is testing Sala familiar (127.0.0.1): features=0x7F8AD0,0x38BCF46",
                  "Test 1 of 4: refused (play=404)", "The TV says playbackState/playing",
                  "Ended the AirPlay video session: stop=200 teardown=200", "The TV fetched 3 files from this PC",
                  f"User-Agent: {TV_AGENT}", "DEBUG   pyatv", "Test 4 of 4: the TV accepted the screen mirroring "
                  "session and kept the video open", "SETUP control-first, with the audio stream, NTP timing: "
                  "failed: refused with 400", "Screen mirroring works with Sala familiar: media-first",
                  report.conclusion()):
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


# -- the fourth test: screen mirroring ---------------------------------------------------------------


async def test_a_tv_that_needs_fairplay_skips_the_mirroring_test(mirror_tv):
    result = await probe._mirroring(_device(mirror_tv, features=APPLE_TV_FEATURES, is_apple=True), Settings(), 2)
    assert result.verdict() == ("skipped: the TV needs FairPlay for screen mirroring, as Apple TVs and Macs do, which "
                                "MirrorBridge does not have yet")
    assert not result.worked and result.mirror is None and mirror_tv.connections == []  # nothing was sent
    report = _report(REFUSED, PLAYED, FETCHED, result)
    assert report.lines()[3].startswith("4. Screen mirroring with a moving test pattern, from this PC: skipped: the TV "
                                        "needs FairPlay")
    assert report.conclusion() == "Sala familiar should play your casts: it fetched MirrorBridge's video."
    i18n.set_language("es")
    assert i18n.say(report.lines()[3]) == (
        "4. La duplicación de pantalla con una imagen de prueba en movimiento, desde este equipo: omitida: la TV "
        "necesita FairPlay para duplicar la pantalla, como los Apple TV y los Mac, y MirrorBridge todavía no lo tiene")


async def test_an_unpaired_tv_skips_the_mirroring_test(mirror_tv):
    device = _device(mirror_tv)
    device.airplay.credentials = None
    result = await probe._mirroring(device, Settings(), 2)
    assert result.verdict() == ("skipped: MirrorBridge is not paired with this TV, and screen mirroring needs it: use "
                                "Device > Pair with PIN first")
    assert mirror_tv.connections == []
    i18n.set_language("es")
    assert i18n.say(result.verdict()).endswith("usa primero Dispositivo > Emparejar con PIN")


@pytest.mark.skipif(not _has_ffmpeg(), reason="the mirroring test sends a test pattern with FFmpeg's x264")
async def test_a_tv_that_drops_the_mirrored_video_is_said_so(mirror_tv):
    mirror_tv.reconfigure(drop_video_after=5)
    settings = Settings(encoder="libx264", output_height=360, video_bitrate_kbps=1500)
    result = await asyncio.wait_for(probe._mirroring(_device(mirror_tv), settings, 2), 120)
    assert result.accepted and not result.worked and result.error is None
    assert result.verdict().startswith("the TV accepted the screen mirroring session, but did not keep the video open: "
                                       "The TV closed the video connection after ")
    assert [check.trailer for check in result.mirror.streams] == ["02000000", "fdf8f800"]  # both avcC endings
    assert profiles.load("TCL") is None  # nothing that works to save
    assert mirror_tv.teardowns == len(mirror_tv.orderings)
    report = _report(REFUSED, PLAYED, FETCHED, result)
    assert report.conclusion() == "Sala familiar should play your casts: it fetched MirrorBridge's video."


async def test_a_tv_that_refuses_mirroring_is_said_so(mirror_tv):
    mirror_tv.reconfigure(refuse=("control-first", "media-first"))
    result = await asyncio.wait_for(probe._mirroring(_device(mirror_tv), Settings(), 2), 120)
    assert not result.accepted and not result.worked
    assert result.verdict().startswith("the TV did not accept a screen mirroring session: The TV turned down screen "
                                       "mirroring (pair-verify=ok setup ")
    assert mirror_tv.videos == [] and mirror_tv.records == 0


async def test_a_tv_whose_pairing_is_gone_fails_the_mirroring_test(mirror_tv):
    mirror_tv.reconfigure(m4_error=True)
    result = await asyncio.wait_for(probe._mirroring(_device(mirror_tv), Settings(), 2), 60)
    assert result.verdict() == ("failed: Sala familiar did not accept MirrorBridge's pairing (pair-verify: M4 error 2 "
                                "(Authentication)). Use Device > Forget pairing, then cast again and type the new PIN "
                                "the TV shows.")
    assert mirror_tv.codes("SETUP") == []


async def test_the_mirroring_test_has_a_time_limit_and_tears_down(monkeypatch, mirror_tv):
    monkeypatch.setattr(probe, "MIRROR_LIMIT_SECONDS", 1.5)
    mirror_tv.reconfigure(setup_delay=10)  # it takes its time to answer each SETUP
    started = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(probe._mirroring(_device(mirror_tv), Settings(), 2), 30)
    assert asyncio.get_running_loop().time() - started < 10
    assert not result.worked and result.verdict() == (
        "failed: The screen mirroring test was stopped after 1.5 s, before it found a way that works.")
    await asyncio.sleep(0.1)
    assert mirror_tv.connections and all(conn.closed for conn in mirror_tv.connections)
    i18n.set_language("es")
    assert i18n.say(result.verdict()) == (
        "falló: La prueba de duplicación de pantalla se detuvo a los 1.5 s, antes de encontrar una forma que funcione.")


async def test_stopping_the_mirroring_test_tears_its_session_down(mirror_tv):
    mirror_tv.reconfigure(setup_delay=10)
    task = asyncio.ensure_future(probe._mirroring(_device(mirror_tv), Settings(), 2))
    while not mirror_tv.connections:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    await asyncio.sleep(0.1)
    assert all(conn.closed for conn in mirror_tv.connections)


def test_the_conclusion_says_mirror_screen_should_work():
    report = _report(REFUSED, PLAYED, FETCHED, MIRRORED)
    assert report.lines()[3] == (
        "4. Screen mirroring with a moving test pattern, from this PC: the TV accepted the screen mirroring session "
        "and kept the video open: 300 frames of the test pattern went to it, and it asked for the time 48 times. What "
        "worked: media-first, with the audio stream, NTP timing, avcC ending 02000000, lead 100 ms")
    assert report.conclusion().endswith("Mirror screen should work on this TV: it accepted screen mirroring and kept "
                                        "the video open.")
    nothing_cast = _report(REFUSED, probe.TestResult(airplay_patch.QUEUE, steps={"setup130": 501}), LIVE_REFUSED,
                           MIRRORED)
    assert nothing_cast.worked and nothing_cast.conclusion() == (
        "Sala familiar did not take the video it was sent to cast, but Mirror screen should work on this TV: it "
        "accepted screen mirroring and kept the video open.")
    i18n.set_language("es")
    assert i18n.say(report.conclusion()) == (
        "Sala familiar debería reproducir tus transmisiones: descargó el video de MirrorBridge. Duplicar pantalla "
        "debería funcionar en esta TV: aceptó la duplicación de pantalla y mantuvo abierto el video.")
    assert i18n.say(report.lines()[3]).startswith(
        "4. La duplicación de pantalla con una imagen de prueba en movimiento, desde este equipo: la TV aceptó la "
        "sesión de duplicación de pantalla y mantuvo abierto el video: recibió 300 fotogramas de la imagen de prueba y "
        "pidió la hora 48 veces. Lo que funcionó: media-first")


# -- the command line --------------------------------------------------------------------------------


def test_the_command_line_prints_each_test_and_where_the_report_is(monkeypatch, capsys):
    calls = []

    async def run(manager, device, settings, *, seconds, public_url, progress):
        calls.append((device.name, seconds, public_url))
        progress(1, 4, probe._NAMES[airplay_patch.PLAY])
        progress(4, 4, probe._NAMES[probe.MIRROR])
        failed = probe.TestResult("live", accepted=True, steps=STEPS, error=exceptions.PlaybackError(
            airplay_queue.media_error({"domain": "CoreMediaErrorDomain", "code": -17221})))
        return _report(REFUSED, PLAYED, failed, MIRRORED)

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
    assert "Test 1 of 4: POST /play (AirPlay video v1) with Apple's test video..." in out
    assert ("Test 4 of 4: Screen mirroring with a moving test pattern, from this PC...\n"
            "Watch the TV now: for about 5 s it should show a moving test pattern, mirrored from this PC.\n") in out
    assert "1. POST /play (AirPlay video v1) with Apple's test video: refused (play=404)" in out
    assert ("3. The play queue with MirrorBridge's test pattern, from this PC: failed: The TV's player could not play "
            "the video (error -17221, CoreMediaErrorDomain). Its player could not download the video") in out
    assert ("4. Screen mirroring with a moving test pattern, from this PC: the TV accepted the screen mirroring "
            "session and kept the video open") in out
    assert ("Sala familiar plays video from the internet, but never fetched MirrorBridge's own stream from this PC. "
            "Check that Windows Firewall allows MirrorBridge on private networks; if it does, this TV may only play "
            "video from HTTPS addresses. Mirror screen should work on this TV") in out
    assert "If the TV asks whether to allow MirrorBridge, choose Allow. This takes about three minutes." in out
    assert f"The full report is in {REPORT_FILE}: please send it with your bug report." in out
    assert code == 0  # something worked: the TV played the video from the internet


@pytest.mark.skipif(not _has_ffmpeg(), reason="the mirroring test sends a test pattern with FFmpeg's x264")
async def test_the_command_line_runs_the_mirroring_test_too(monkeypatch, capsys, mirror_tv, tmp_path):
    """`probe` as the owner runs it, with the three cast tests' results made up here."""
    async def public(_manager, _device, route, _url, _seconds):
        return REFUSED if route == airplay_patch.PLAY else PLAYED

    async def live(_manager, _device, _settings, _seconds):
        return FETCHED

    async def choose(_manager, args):
        assert args.device == "Sala familiar"
        return _device(mirror_tv)

    monkeypatch.setattr(probe, "_public", public)
    monkeypatch.setattr(probe, "_live", live)
    monkeypatch.setattr(cli, "_choose", choose)
    monkeypatch.setattr(cli, "DeviceManager", lambda _loop: FakeManager())
    Settings(encoder="libx264", output_height=360, video_bitrate_kbps=1500).save()
    pyatv_level = logging.getLogger("pyatv").level
    try:
        args = cli.build_parser().parse_args(["probe", "Sala familiar", "--seconds", "2"])
        code = await asyncio.wait_for(cli.cmd_probe(args), 120)
    finally:
        logging.getLogger("pyatv").setLevel(pyatv_level)
    out = capsys.readouterr().out
    assert "Watch the TV now: for about 2 s it should show a moving test pattern, mirrored from this PC." in out
    assert ("4. Screen mirroring with a moving test pattern, from this PC: the TV accepted the screen mirroring "
            "session and kept the video open: ") in out
    assert "What worked: media-first, with the audio stream, NTP timing, avcC ending 02000000, lead 100 ms" in out
    assert "Mirror screen should work on this TV: it accepted screen mirroring and kept the video open." in out
    assert code == 0 and mirror_tv.records == 1 and mirror_tv.teardowns == len(mirror_tv.orderings)
    reports = list((tmp_path / "MirrorBridge").glob("probe-*.log"))
    assert len(reports) == 1 and "Test 4 of 4: the TV accepted the screen mirroring session" in reports[0].read_text(
        encoding="utf-8")
    assert profiles.load("TCL").ordering == MEDIA_FIRST


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

    release, mirroring, shown, intro, notes = threading.Event(), threading.Event(), [], [], []
    report = _report(REFUSED, PLAYED, FETCHED, MIRRORED)
    controller.notify.connect(lambda *note: notes.append(note))

    async def run(manager, tv, settings, *, progress):
        progress(2, 4, probe._NAMES[airplay_patch.QUEUE])
        while not mirroring.is_set():
            await asyncio.sleep(0.01)
        progress(4, 4, probe._NAMES[probe.MIRROR])
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
    assert intro and intro[0].startswith("MirrorBridge probará cuatro formas de enviar un video a Sala familiar")
    assert ("Durante la última prueba, la duplicación de pantalla, debería mostrar una imagen de prueba en movimiento "
            "durante unos 10 segundos.") in intro[0]
    _until(lambda: "prueba 2" in controller.status()[1])
    assert controller.is_active and controller.status() == ("busy", "Probando Sala familiar: prueba 2 de 4…")
    assert not _more_options(controller)["Probar esta TV…"].isEnabled()  # one test at a time
    assert notes == []
    mirroring.set()
    _until(lambda: "prueba 4" in controller.status()[1])
    assert controller.status() == ("busy", "Probando Sala familiar: prueba 4 de 4, mira la TV…")
    assert notes == [("Probar Sala familiar", "Mira la TV ahora: durante unos 10 s debería mostrar una imagen de "
                      "prueba en movimiento, duplicada desde este equipo.", False)]
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
    box = controller.probe_report_box(_report(REFUSED, PLAYED, FETCHED, MIRRORED))
    assert box.windowTitle() == "Terminó la prueba de Sala familiar"
    assert box.text() == ("Sala familiar debería reproducir tus transmisiones: descargó el video de MirrorBridge. "
                          "Duplicar pantalla debería funcionar en esta TV: aceptó la duplicación de pantalla y "
                          "mantuvo abierto el video.")
    assert box.informativeText().splitlines() == [
        "1. POST /play (video AirPlay v1) con el video de prueba de Apple: rechazado (play=404)",
        "2. La cola de reproducción (video AirPlay v2) con el video de prueba de Apple: la TV lo aceptó y dijo que lo "
        "estaba reproduciendo (setup=200 record=200 setup130=200 insert=200 isInterestedInDateRange=200 "
        "actionAtItemEnd=200 rate=200)",
        f"3. La cola de reproducción con la imagen de prueba de MirrorBridge, desde este equipo: la TV lo aceptó y "
        f"descargó el video (User-Agent: {TV_AGENT})",
        "4. La duplicación de pantalla con una imagen de prueba en movimiento, desde este equipo: la TV aceptó la "
        "sesión de duplicación de pantalla y mantuvo abierto el video: recibió 300 fotogramas de la imagen de prueba y "
        "pidió la hora 48 veces. Lo que funcionó: media-first, with the audio stream, NTP timing, avcC ending "
        "02000000, lead 100 ms",
        "",
        f"El informe completo está en {REPORT_FILE}: mándalo con tu reporte del problema.",
    ]
    assert sorted(b.text() for b in box.findChildren(QPushButton)) == ["Abrir carpeta de registros", "Aceptar"]
