"""Test this TV: which ways of sending it AirPlay video it takes, written down for a bug report.

Four tests, each on an AirPlay connection of its own, each time-boxed, and each stopped and
torn down before the next one starts:

1. POST /play (AirPlay video v1) with Apple's public HTTPS test video;
2. the play queue (AirPlay video v2) with the same video;
3. the play queue with a short real cast of MirrorBridge's test pattern, served over http://
   from this PC, watching whether the TV fetches the playlist and the video, and with which
   player (its User-Agent);
4. screen mirroring (mirror.probe_mirroring) with a moving test pattern for about 10 s: does
   the TV accept the mirroring session and keep the video open, and in which SETUP ordering,
   avcC ending and timing. What worked is saved for the TV, for Mirror screen. A TV that
   needs FairPlay (an Apple TV) is skipped.

The first two tell "the TV refuses the request" from "the TV cannot reach this PC": Apple's
video comes from the internet. The person only watches the TV, and during the last test looks
for the moving test pattern. Everything the TV answered and said goes to the log and to a
report file of its own, in the log folder.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import APP_NAME, __version__, airplay_patch
from .airplay_patch import PLAY, QUEUE, CastContext, ReceiverRefusedVideo, format_steps
from .config import Settings, app_data_dir
from .devices import Device, DeviceManager
from .hls_server import Activity
from .mirror import ProbeResult, Target, probe_mirroring
from .mirror.probe import ORDERING, TIMING, TRAILER
from .session import CastSession
from .text import Text

_LOGGER = logging.getLogger(__name__)

# Apple's "basic" 16:9 HLS example: H.264 and AAC in MPEG-TS segments, as MirrorBridge's own
# stream is, over HTTPS (ExoPlayer's demo list calls it "Apple 16x9 basic stream (TS)").
PUBLIC_STREAM = "https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_16x9/bipbop_16x9_variant.m3u8"
DEFAULT_SECONDS = 20  # how long each test watches the TV once it took the video
CONNECT_SECONDS = 20  # pyatv's connection to the TV
ANSWER_SECONDS = 90  # from asking the TV to play until it has answered (its first SETUP may take 60 s)
START_SECONDS = 90  # the test pattern's cast: FFmpeg filling the TV's playlist window, then the TV answering
STOP_SECONDS = 15  # stopping a test and tearing its session down
PAUSE_SECONDS = 3  # between tests, so that the TV's player is back to idle
POLL_SECONDS = 0.25
MIRROR_SECONDS = 10  # how long the screen mirroring test sends its test pattern, at most
MIRROR_LIMIT_SECONDS = 75  # that test in all: pair-verify, the SETUP shapes and the test pattern
# Both SETUP orderings, PTP (does the TV give a ClockID), and the other avcC ending if the TV
# drops the video. Without the silent audio stream is not tried: the TVs tried so far want it
# (mirror --probe tries that too).
MIRROR_VARIANTS = (ORDERING, TIMING, TRAILER)

MIRROR = "mirror"
TESTS = (PLAY, QUEUE, "live", MIRROR)
_NAMES = {
    PLAY: Text("probe_test_play", "POST /play (AirPlay video v1) with Apple's test video"),
    QUEUE: Text("probe_test_queue", "The play queue (AirPlay video v2) with Apple's test video"),
    "live": Text("probe_test_live", "The play queue with MirrorBridge's test pattern, from this PC"),
    MIRROR: Text("probe_test_mirror", "Screen mirroring with a moving test pattern, from this PC"),
}

ProgressCallback = Callable[[int, int, Text], None]  # test number (from 1), number of tests, its name


def watch_hint(test: str, seconds: float = DEFAULT_SECONDS) -> Optional[Text]:
    """What the person should look for on the TV while `test` runs, if anything in particular."""
    if test == MIRROR:
        return Text("probe_watch_mirror", "Watch the TV now: for about {seconds} s it should show a moving test "
                    "pattern, mirrored from this PC.", seconds=f"{min(seconds, MIRROR_SECONDS):g}")
    return None


@dataclass
class TestResult:
    """What one test found out."""

    test: str  # PLAY, QUEUE, "live" or MIRROR
    accepted: bool = False  # the TV took the URL (2xx to /play or to insertPlayQueueItem), or a mirroring session
    playing: bool = False  # the TV said that it was playing (event channel, or /playback-info)
    steps: dict = field(default_factory=dict)  # each request's answer
    events: dict = field(default_factory=dict)  # what the TV said, with counts
    ended: str = ""  # why the TV ended the video before the test did, if it did
    error: Optional[BaseException] = None  # why the test ended early
    tv: Optional[Activity] = None  # what the TV fetched from this PC, once it reached it ("live" only)
    mirror: Optional[ProbeResult] = None  # what the mirroring probe found out (MIRROR only)
    skipped: Optional[Text] = None  # why the test was not run

    @property
    def name(self) -> Text:
        return _NAMES[self.test]

    @property
    def worked(self) -> bool:
        """The TV played the video, as far as MirrorBridge can tell, without an error: it
        fetched this PC's stream, or it said that it was playing Apple's video. Taking the
        request (a 2xx answer) is not enough. Screen mirroring worked when the TV kept the
        video open while the test pattern went, and asked for the time."""
        if self.test == MIRROR:
            return self.mirror is not None and self.mirror.worked and self.error is None
        if not self.accepted or self.error is not None:
            return False
        if self.test == "live":
            return self.tv is not None and self.tv.first_segment_at is not None
        return self.playing

    def verdict(self) -> Text:
        if self.skipped is not None:
            return Text("probe_skipped", "skipped: {why}", why=self.skipped)
        steps = format_steps(self.steps)
        if isinstance(self.error, ReceiverRefusedVideo) and not self.accepted:
            if self.error.code is None:
                return Text("probe_no_answer", "no answer ({steps})", steps=steps)
            return Text("probe_refused", "refused ({steps})", steps=steps)
        if self.error is not None:
            message = self.error.args[0] if self.error.args and isinstance(self.error.args[0], str) else ""
            if isinstance(message, Text) and message.key == "tv_probe_advice":
                message = message.values["problem"]  # not "use Test this TV": this is it
            return Text("probe_failed", "failed: {error}", error=message or type(self.error).__name__)
        if self.test == MIRROR:
            return self._mirrored()
        verdict = self._taken()
        if self.ended:
            return Text("probe_then", "{verdict}, then {ended}", verdict=verdict, ended=self.ended)
        return verdict

    def _taken(self) -> Text:
        steps = format_steps(self.steps)
        if self.test == "live" and self.accepted:
            tv = self.tv or Activity()
            agent = Text("probe_agent", "User-Agent: {agent}", agent=tv.user_agent) if tv.user_agent else \
                Text("probe_no_agent", "no User-Agent")
            if tv.first_segment_at is not None:
                return Text("probe_fetched", "the TV took it and fetched the video ({agent})", agent=agent)
            if tv.first_playlist_at is not None:
                return Text("probe_playlist_only", "the TV took it and fetched the playlist, but none of the "
                            "video ({agent})", agent=agent)
            if tv.missing:
                return Text("probe_wrong_path", "the TV took it and reached this PC, but asked for {path}, which "
                            "MirrorBridge does not serve ({agent})", path=tv.missing_path, agent=agent)
            if tv.preflights:
                return Text("probe_preflight_only", "the TV took it and asked from a web page whether it may fetch "
                            "it (OPTIONS), but never fetched it ({agent})", agent=agent)
            return Text("probe_not_fetched", "the TV took it but never fetched it from this PC ({steps})",
                        steps=steps)
        if self.playing:
            return Text("probe_playing", "the TV took it and said it was playing ({steps})", steps=steps)
        if self.accepted:
            return Text("probe_took", "the TV took it but did not say that it was playing ({steps})", steps=steps)
        return Text("probe_no_answer", "no answer ({steps})", steps=steps)

    def _mirrored(self) -> Text:
        """Whether the TV accepted the mirroring session and kept the video open, and what worked."""
        result = self.mirror or ProbeResult("", "")
        check = next((check for check in result.streams if check.worked), None)
        if result.profile is not None and check is not None and check.report is not None:
            return Text("probe_mirrored", "the TV accepted the screen mirroring session and kept the video open: "
                        "{frames} frames of the test pattern went to it, and it asked for the time {times} times. "
                        "What worked: {how}", frames=check.report.frames_sent, times=check.report.ntp_requests,
                        how=result.profile.describe())
        if result.session_accepted:
            return Text("probe_mirror_no_video", "the TV accepted the screen mirroring session, but did not keep "
                        "the video open: {error}", error=result.conclusion())
        error = result.conclusion()
        if result.pair_verify != "ok" or error.key == "mirror_probe_time_up":  # not the TV saying no
            return Text("probe_failed", "failed: {error}", error=error)
        return Text("probe_mirror_refused", "the TV did not accept a screen mirroring session: {error}", error=error)


@dataclass
class ProbeReport:
    device: str
    results: list[TestResult]
    report_file: Optional[Path] = None

    def lines(self) -> list[Text]:
        return [Text("probe_line", "{number}. {test}: {result}", number=number, test=result.name,
                     result=result.verdict())
                for number, result in enumerate(self.results, 1)]

    @property
    def worked(self) -> bool:
        return any(result.worked for result in self.results)

    def conclusion(self) -> Text:
        """What the tests say, and Mirror screen when the TV took the mirroring test."""
        casting = self._casting()
        mirrored = any(result.test == MIRROR and result.worked for result in self.results)
        if not mirrored:
            return casting
        if casting.key == "probe_none":
            return Text("probe_mirror_only", "{device} did not take the video it was sent to cast, but Mirror "
                        "screen should work on this TV: it accepted screen mirroring and kept the video open.",
                        device=self.device)
        return Text("probe_mirror_works", "{conclusion} Mirror screen should work on this TV: it accepted screen "
                    "mirroring and kept the video open.", conclusion=casting)

    def _casting(self) -> Text:
        """What the cast tests say, from what was seen: the firewall and HTTPS are only named
        when the TV played Apple's video but never reached this PC for MirrorBridge's."""
        device = self.device
        live = next((result for result in self.results if result.test == "live"), None) or TestResult("live")
        public = [result for result in self.results if result.test in (PLAY, QUEUE)]
        if live.worked:
            return Text("probe_works", "{device} should play your casts: it fetched MirrorBridge's video.",
                        device=device)
        if live.tv is not None:  # it reached this PC: neither the firewall nor HTTPS stops it
            return Text("probe_live_not_played", "{device} reached this PC for MirrorBridge's stream, but did not "
                        "play it: {result}. Please send the report with your bug report.",
                        device=device, result=live.verdict())
        if live.accepted:
            if any(result.worked for result in public):
                return Text("probe_internet_only", "{device} plays video from the internet, but never fetched "
                            "MirrorBridge's own stream from this PC. Check that Windows Firewall allows MirrorBridge "
                            "on private networks; if it does, this TV may only play video from HTTPS addresses.",
                            device=device)
            if any(result.accepted for result in public):
                return Text("probe_player_silent", "{device} takes the video it is sent, but its player did not "
                            "start: it did not say that it played Apple's video, and never fetched MirrorBridge's "
                            "stream from this PC. Please send the report with your bug report.", device=device)
            return Text("probe_not_fetched_here", "{device} took MirrorBridge's stream but never fetched it from this "
                        "PC. Check that Windows Firewall allows MirrorBridge on private networks, and send the "
                        "report with your bug report.", device=device)
        if any(result.accepted for result in public):
            return Text("probe_live_refused", "{device} took Apple's video, but not MirrorBridge's stream: {result}. "
                        "Please send the report with your bug report.", device=device, result=live.verdict())
        return Text("probe_none", "None of the tests got {device} to take the video.", device=device)

    def where(self) -> Text:
        return Text("probe_report_file", "The full report is in {file}: please send it with your bug report.",
                    file=str(self.report_file or "mirrorbridge.log"))


async def _until(done: Callable[[], bool], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)


async def _finish(task: asyncio.Future) -> None:
    """Cancel a test's task, give it time to stop the video and tear its session down, and
    take whatever it raised (the test has already noted it)."""
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), STOP_SECONDS)
    except asyncio.TimeoutError:
        _LOGGER.warning("The test did not stop within %d s", STOP_SECONDS)


def _note(result: TestResult, cast: Optional[CastContext]) -> None:
    if cast is not None:
        result.accepted = result.accepted or cast.accepted_at is not None
        result.steps = dict(cast.steps)
        result.playing = cast.played
        if cast.queue is not None:
            result.events = dict(cast.queue.events)
            result.playing = cast.queue.played.is_set()
            result.ended = cast.queue.end_reason


async def _public(manager: DeviceManager, device: Device, route: str, url: str, seconds: float) -> TestResult:
    """Ask the TV to play Apple's test video by `route`, and watch it for `seconds`."""
    result = TestResult(route)
    cast = CastContext(apple=device.is_apple, live=False, password=device.requires_password,
                       device=device.identifier, features=device.features, route=route, fallback=False)
    atv = task = None
    try:
        atv = await asyncio.wait_for(manager.connect(device), CONNECT_SECONDS)
        task = asyncio.ensure_future(airplay_patch.play_url(atv, url, cast))
        await _until(lambda: task.done() or cast.accepted_at is not None, ANSWER_SECONDS)
        if cast.accepted_at is not None:
            await _until(task.done, seconds)
        if task.done():
            task.result()  # what the TV said, if it ended the test
        elif cast.accepted_at is None:
            result.error = TimeoutError(f"no answer within {ANSWER_SECONDS} s")
    except asyncio.CancelledError:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        result.error = ex
    finally:
        if task is not None:
            await _finish(task)
        if atv is not None:
            atv.close()
        _note(result, cast)
    return result


async def _live(manager: DeviceManager, device: Device, settings: Settings, seconds: float) -> TestResult:
    """A short real cast of the test pattern through the play queue: does the TV fetch it?"""
    result = TestResult("live")
    pattern = dataclasses.replace(settings, capture_backend="test", extend_screen=False, window_title="",
                                  capture_audio=False)
    session = CastSession(manager, device, pattern, route=QUEUE)
    task = asyncio.ensure_future(session.run())
    others: dict[str, str] = {}  # whoever else fetched the stream, by IP: their User-Agent

    def watch() -> bool:
        tv = session.tv_activity()
        if tv.requests or tv.missing or tv.preflights:  # the TV reached this PC
            result.tv = tv  # the server's own record: still there once the cast has ended
        others.update((ip, client.user_agent or "no User-Agent") for ip, client in session.clients().items()
                      if ip != device.address)
        return task.done()

    try:
        await _until(lambda: watch() or (session.cast is not None and session.cast.accepted_at is not None),
                     START_SECONDS)
        if session.cast is not None and session.cast.accepted_at is not None:
            await _until(watch, seconds)
        watch()
        if task.done():
            task.result()
        elif session.cast is None or session.cast.accepted_at is None:
            result.error = TimeoutError(f"the cast did not start within {START_SECONDS} s")
    except asyncio.CancelledError:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        result.error = ex
    finally:
        await session.stop()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), STOP_SECONDS)
        except asyncio.TimeoutError:
            await _finish(task)
        _note(result, session.cast)
    tv = result.tv or Activity()
    _LOGGER.info("The TV fetched %d files from this PC (a playlist: %s, video: %s, User-Agent: %s)%s%s%s",
                 tv.requests, "yes" if tv.first_playlist_at is not None else "no",
                 "yes" if tv.first_segment_at is not None else "no", tv.user_agent or "none",
                 f"; it also asked for {tv.missing} files MirrorBridge does not serve, first {tv.missing_path}"
                 if tv.missing else "", f"; it sent {tv.preflights} CORS preflights" if tv.preflights else "",
                 f"; also fetched by {others}" if others else "")
    return result


async def _mirroring(device: Device, settings: Settings, seconds: float) -> TestResult:
    """Screen mirroring with the test pattern, within MIRROR_LIMIT_SECONDS in all: does the
    TV accept the session and keep the video open? What worked is saved for the TV."""
    result = TestResult(MIRROR)
    target = Target.from_device(device)
    if target.needs_fairplay:
        result.skipped = Text("probe_mirror_fairplay", "the TV needs FairPlay for screen mirroring, as Apple TVs "
                              "and Macs do, which MirrorBridge does not have yet")
        _LOGGER.info("Screen mirroring is not tried: %s needs FairPlay", device.name)
        return result
    if not target.credentials:  # pair-verify keys the session and the video
        result.skipped = Text("probe_mirror_unpaired", "MirrorBridge is not paired with this TV, and screen mirroring "
                              "needs it: use Device > Pair with PIN first")
        _LOGGER.info("Screen mirroring is not tried: MirrorBridge is not paired with %s", device.name)
        return result
    try:
        mirrored = result.mirror = await probe_mirroring(
            target, settings, seconds=min(seconds, MIRROR_SECONDS), variants=MIRROR_VARIANTS,
            limit=MIRROR_LIMIT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("The screen mirroring test failed", exc_info=True)
        result.error = ex
    else:
        result.accepted = mirrored.session_accepted
    return result


class _AsBefore(logging.Filter):
    """Lets a handler that was there before the test show only what it showed then."""

    def __init__(self, levels: dict[str, int]) -> None:
        super().__init__()
        self.levels = levels  # each raised logger's level before, by name ("" = the root)

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        while name and name not in self.levels:
            name = name.rpartition(".")[0]
        return record.levelno >= self.levels[name]


class _Report:
    """A log file of the test's own, in the log folder, with everything logged while it runs,
    pyatv's details too. The console and mirrorbridge.log go on showing what they did."""

    LOGGERS = ("", "pyatv")

    def __init__(self, folder: Optional[Path]) -> None:
        folder = folder or app_data_dir()
        self.path = folder / time.strftime("probe-%Y%m%d-%H%M%S.log")
        self.handler = logging.FileHandler(self.path, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        self.handler.setLevel(logging.DEBUG)
        self._loggers = [logging.getLogger(name or None) for name in self.LOGGERS]
        self._levels = [logger.level for logger in self._loggers]
        self._filter = _AsBefore({name: logger.getEffectiveLevel()
                                  for name, logger in zip(self.LOGGERS, self._loggers)})

    def __enter__(self) -> "_Report":
        root = self._loggers[0]
        for handler in root.handlers:
            handler.addFilter(self._filter)
        root.addHandler(self.handler)
        for logger in self._loggers:
            logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *_exc) -> None:
        for logger, level in zip(self._loggers, self._levels):
            logger.setLevel(level)
        root = self._loggers[0]
        root.removeHandler(self.handler)
        self.handler.close()
        for handler in root.handlers:
            handler.removeFilter(self._filter)


async def run(
    manager: DeviceManager,
    device: Device,
    settings: Settings,
    *,
    seconds: float = DEFAULT_SECONDS,
    public_url: str = PUBLIC_STREAM,
    progress: Optional[ProgressCallback] = None,
    report_folder: Optional[Path] = None,
) -> ProbeReport:
    """Run the four tests on `device`, one after the other, and say what each found out."""
    with _Report(report_folder) as report_file:
        _LOGGER.info("%s %s is testing %s (%s): %s", APP_NAME, __version__, device.name, device.address,
                     device.details())
        airplay_patch.apply()
        tests = {
            PLAY: lambda: _public(manager, device, PLAY, public_url, seconds),
            QUEUE: lambda: _public(manager, device, QUEUE, public_url, seconds),
            "live": lambda: _live(manager, device, settings, seconds),
            MIRROR: lambda: _mirroring(device, settings, seconds),
        }
        results = []
        for number, test in enumerate(TESTS, 1):
            if number > 1:
                await asyncio.sleep(PAUSE_SECONDS)
            if progress is not None:
                progress(number, len(TESTS), _NAMES[test])
            _LOGGER.info("Test %d of %d: %s", number, len(TESTS), _NAMES[test])
            results.append(await tests[test]())
            _LOGGER.info("Test %d of %d: %s", number, len(TESTS), results[-1].verdict())
        report = ProbeReport(device.name, results, report_file.path)
        for line in report.lines():
            _LOGGER.info("%s", line)
        _LOGGER.info("%s", report.conclusion())
        return report
