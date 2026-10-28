"""Test this TV: which ways of sending it AirPlay video it takes, written down for a bug report.

Three tests, each on an AirPlay connection of its own, each time-boxed, and each stopped and
torn down before the next one starts:

1. POST /play (AirPlay video v1) with Apple's public HTTPS test video;
2. the play queue (AirPlay video v2) with the same video;
3. the play queue with a short real cast of MirrorBridge's test pattern, served over http://
   from this PC, watching whether the TV fetches the playlist and the video, and with which
   player (its User-Agent).

The first two tell "the TV refuses the request" from "the TV cannot reach this PC": Apple's
video comes from the internet. The person only watches the TV. Everything the TV answered
and said goes to the log and to a report file of its own, in the log folder.
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

TESTS = (PLAY, QUEUE, "live")
_NAMES = {
    PLAY: Text("probe_test_play", "POST /play (AirPlay video v1) with Apple's test video"),
    QUEUE: Text("probe_test_queue", "The play queue (AirPlay video v2) with Apple's test video"),
    "live": Text("probe_test_live", "The play queue with MirrorBridge's test pattern, from this PC"),
}

ProgressCallback = Callable[[int, int, Text], None]  # test number (from 1), number of tests, its name


@dataclass
class TestResult:
    """What one test found out."""

    test: str  # PLAY, QUEUE or "live"
    accepted: bool = False  # the TV took the URL (2xx to /play or to insertPlayQueueItem)
    playing: bool = False  # the TV said that it was playing (event channel, or /playback-info)
    steps: dict = field(default_factory=dict)  # each request's answer
    events: dict = field(default_factory=dict)  # what the TV said, with counts
    ended: str = ""  # why the TV ended the video before the test did, if it did
    error: Optional[BaseException] = None  # why the test ended early
    tv: Optional[Activity] = None  # what the TV fetched from this PC, once it reached it ("live" only)

    @property
    def name(self) -> Text:
        return _NAMES[self.test]

    @property
    def worked(self) -> bool:
        """The TV played the video, as far as MirrorBridge can tell, without an error: it
        fetched this PC's stream, or it said that it was playing Apple's video. Taking the
        request (a 2xx answer) is not enough."""
        if not self.accepted or self.error is not None:
            return False
        if self.test == "live":
            return self.tv is not None and self.tv.first_segment_at is not None
        return self.playing

    def verdict(self) -> Text:
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
        """What the tests say, from what was seen: the firewall and HTTPS are only named when
        the TV played Apple's video but never reached this PC for MirrorBridge's."""
        device = self.device
        live = next((result for result in self.results if result.test == "live"), None) or TestResult("live")
        public = [result for result in self.results if result.test != "live"]
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
    """Run the three tests on `device`, one after the other, and say what each found out."""
    with _Report(report_folder) as report_file:
        _LOGGER.info("%s %s is testing %s (%s): %s", APP_NAME, __version__, device.name, device.address,
                     device.details())
        airplay_patch.apply()
        tests = {
            PLAY: lambda: _public(manager, device, PLAY, public_url, seconds),
            QUEUE: lambda: _public(manager, device, QUEUE, public_url, seconds),
            "live": lambda: _live(manager, device, settings, seconds),
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
