"""One screen mirroring session: the desktop (or a test pattern) on the TV, a fraction of a second
behind, the way an iPhone or a Mac mirrors.

    pick the encoder -> connect and pair-verify -> GET /info (how big the TV's screen is)
    -> SETUP (negotiation.py; the TV's timing port is probed and its event channel opened as
    soon as an answer names them) -> video connection -> RECORD -> /feedback
    -> FFmpeg -> frames, each stamped with when to show it and encrypted -> TEARDOWN

It speaks to the tray and the command line as a cast does (session.CastSession): run() until
the session ends, stop() from anywhere on the loop, and on_status(State, message) on the way,
the messages and errors being Texts.

Delay comes first. A frame that cannot help the TV is not sent: when the TV (or the network)
takes the video slower than it comes, frames are dropped up to the next keyframe, and a TV
that takes nothing for VIDEO_STALL_SECONDS ends the session. So does FFmpeg giving no picture
(FIRST_FRAME_SECONDS, FRAME_GAP_SECONDS); setting up has START_SECONDS in all. FFmpeg ending
before its first frame (the driver refuses ddagrab, the GPU encoder is out of sessions) does
not: the session goes on with GDI capture, then with the CPU encoder, as a cast does.

A TV that drops the video connection before the second keyframe of a first session (nothing
saved for it yet) may not take the avcC ending it was sent: the session is set up once more
with the other one (h264.TRAILERS), and what works is saved.

With Extend screen on, the desktop is the virtual monitor a cast would add (extended.py): it is
added before the TV is asked, captured as any display is and fitted into the TV's screen, and
kept for the whole session, through the retry above and FFmpeg's fallbacks, so the windows on
it stay there. It goes when the session ends, however it ends; if it goes away first (sleep),
the session ends with VddError("lost"), as a cast does.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from .. import extended, ffmpeg, vdisplay
from ..config import Settings
from ..session import State, StatusCallback
from ..text import Text
from . import profiles
from .clock import NS, TIMING_PORTS, NtpResponder, PtsMapper, bind_udp, now_ns, to_ntp
from .control import FEATURE_1080P, ControlChannel, ReceiverInfo, Target, open_event_channel
from .datachannel import DataChannel
from .errors import MirrorError, answer_of, data_closed, data_refused, needs_fairplay, no_timing, refused
from .flv import CONFIG, FRAME
from .h264 import TRAILERS, avcc_record, frame_nals, is_idr, join_avcc, parse_avcc, parse_sps, split_avcc
from .negotiation import NTP, Established, Feedback, Negotiator
from .packets import VideoCipher, codec_packet, video_key
from .source import DESKTOP, TEST_PATTERN, FFmpegSource, FrameQueue, Unit, build_command, ended_text
from .stats import LOG_SECONDS, StageTimes

_LOGGER = logging.getLogger(__name__)

MAX_CANVAS = (1920, 1080)  # never more, whatever the TV's screen: 4K costs more delay than it is worth
FALLBACK_CANVAS = (1280, 720)  # a TV that gives no displays[] and no 1080p feature bit
NTP_GRACE_SECONDS = 10  # from RECORD until the TV must have asked for the time
NTP_QUIET_SECONDS = 15  # then, a pause this long in its requests is worth a warning
MAX_BACKLOG_FRAMES = 3  # frames waiting to be sent; beyond that, drop up to the next keyframe
QUEUE_SECONDS = 2  # frames kept at most while the TV takes none (source.FrameQueue)
SEND_BUFFER_FRAMES = 2  # the video connection's send buffers, in average frames
MIN_SEND_BUFFER = 16 * 1024
MAX_SEND_BUFFER = 256 * 1024
VIDEO_STALL_SECONDS = 5  # the TV taking none of the video this long ends the session
FIRST_FRAME_SECONDS = 15  # from starting FFmpeg to its first frame
FRAME_GAP_SECONDS = 10  # no frame from FFmpeg this long, once they came
START_SECONDS = 90  # connecting, pair-verify, GET /info, SETUP, the video connection and RECORD
TV_ENDED_SECONDS = 1.0  # after the video connection closes, for the control connection to close too
SUPERVISE_SECONDS = 0.25


def choose_canvas(info: Optional[ReceiverInfo], target: Target,
                  limit: tuple[int, int] = MAX_CANVAS) -> tuple[int, int]:
    """The largest picture to send: the TV's screen as /info gives it, within `limit`."""
    if info is not None and info.displays:
        width, height = info.displays[0].width, info.displays[0].height
        if info.max_width and info.max_height:
            width, height = min(width, info.max_width), min(height, info.max_height)
    elif target.has_feature(FEATURE_1080P) or (info is not None and info.features is not None
                                               and info.features >> FEATURE_1080P & 1):
        width, height = 1920, 1080
    else:
        width, height = FALLBACK_CANVAS
    scale = min(1.0, limit[0] / width, limit[1] / height)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def canvas_limit(settings: Settings, limit: tuple[int, int] = MAX_CANVAS) -> tuple[int, int]:
    """`limit`, or less if the output height setting (Quality > Resolution) asks for less."""
    height = settings.output_height // 2 * 2
    if not height or height >= limit[1]:
        return limit
    return min(limit[0], (height * 16 // 9 + 1) // 2 * 2), height


def extend_mode(mode: str) -> str:
    """The Extended screen size Mirror screen adds the virtual screen at: the one chosen, but no
    bigger than what mirroring sends (MAX_CANVAS). A 4K screen would be captured and shrunk to
    1080p again on every frame: on a real PC that took 30 to 170 ms a frame, past the TV's lead,
    against about 25 ms at 1080p."""
    width, height, _hz = vdisplay.EXTEND_MODES.get(mode, vdisplay.EXTEND_MODES["1080p"])
    return mode if width <= MAX_CANVAS[0] and height <= MAX_CANVAS[1] else "1080p"


def choose_fps(wanted: int, info: Optional[ReceiverInfo]) -> int:
    most = info.displays[0].max_fps if info is not None and info.displays and info.displays[0].max_fps else 60
    return max(1, min(wanted or 30, most))


def send_buffer(kbps: int, fps: int) -> int:
    """The video connection's send buffers (the kernel's and asyncio's): about
    SEND_BUFFER_FRAMES average frames, so that a slow network pushes back at once."""
    frame = kbps * 1000 // 8 // max(1, fps)
    return max(MIN_SEND_BUFFER, min(MAX_SEND_BUFFER, SEND_BUFFER_FRAMES * frame))


@dataclass
class SessionReport:
    """What happened, for the probe and the log."""

    steps: dict = field(default_factory=dict)
    info: Optional[ReceiverInfo] = None
    established: Optional[Established] = None
    canvas: Optional[tuple[int, int]] = None
    picture: Optional[tuple[int, int]] = None  # what the encoder really made (its SPS)
    fps: int = 0
    encoder: str = ""
    trailer: str = ""
    data_channel: str = "not opened"  # "open", "refused (...)", "closed by the TV"
    frames_sent: int = 0
    codec_packets: int = 0
    data_closed_after: Optional[int] = None  # frames sent when the TV closed the video connection
    timing_port: int = 0  # our NTP responder's UDP port
    ntp_requests: int = 0
    feedback: dict = field(default_factory=dict)
    latency: dict = field(default_factory=dict)
    first_frame_ms: Optional[float] = None  # from starting FFmpeg to the first frame sent
    retried: str = ""  # why the session was set up a second time, with the other avcC ending
    error: Optional[BaseException] = None


class _VideoClosed(Exception):
    """The TV closed the video connection (found when writing to it)."""


class MirrorSession:
    def __init__(
        self,
        target: Target,
        settings: Settings,
        on_status: Optional[StatusCallback] = None,
        *,
        source: str = DESKTOP,
        profile: Optional[profiles.MirrorProfile] = None,
        seconds: Optional[float] = None,
        remember: bool = True,
        max_canvas: tuple[int, int] = MAX_CANVAS,
    ) -> None:
        self.target = target
        self.settings = settings
        self.on_status = on_status or (lambda s, m: None)
        self.source = source
        saved = profile or profiles.load(target.identifier)
        self.profile = saved or profiles.MirrorProfile()
        self.saved = saved is not None  # given or saved: what is asked for is known to work
        self.seconds = seconds
        self.remember = remember
        self.max_canvas = max_canvas
        self.state = State.IDLE
        self.report = SessionReport(trailer=self.profile.trailer)
        self.error: Optional[Exception] = None
        self.finished = threading.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._starting = False
        self._negotiator: Optional[Negotiator] = None
        self._responder: Optional[NtpResponder] = None
        self._transports: list[asyncio.BaseTransport] = []
        self._events: Optional[asyncio.Task] = None  # opening the event channel, then its transport
        self._data: Optional[DataChannel] = None
        self._send_buffer = MIN_SEND_BUFFER
        self._feedback: Optional[Feedback] = None
        self._ffmpeg: Optional[FFmpegSource] = None
        self._capture: tuple = ()  # FFmpeg, the display and the window's region, for each way below
        self._sources: list[tuple[str, str]] = []  # the ways of capturing and encoding still to try
        self._using = ("", "")  # the way FFmpeg runs now: (capture backend, encoder)
        self._cipher: Optional[VideoCipher] = None
        self._frames: Optional[FrameQueue] = None
        self._stats = StageTimes()
        self._record_ns = 0
        self._stopping = False
        self._timing_probed = False
        self._timing_quiet = False
        self._dropped_at_once = False  # the TV closed the video before the second keyframe
        self._retried = False
        self._xscreen: Optional[extended.ExtendedScreen] = None  # Extend screen's virtual monitor
        self.virtual_device = ""  # \\.\DISPLAYn of the virtual screen while there is one

    # -- helpers -----------------------------------------------------------

    def _set(self, state: State, message: str = "") -> None:
        self.state = state
        _LOGGER.info("%s%s", state.value, f": {message}" if message else "")
        try:
            self.on_status(state, message)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Status callback failed")

    @property
    def established(self) -> Optional[Established]:
        return self.report.established

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """Mirror until stop(), the time limit, the TV ending it, or an error (raised)."""
        self._task = asyncio.current_task()
        try:
            if self._stop.is_set():
                self._set(State.STOPPED, "Cancelled")
                return
            try:
                ended = await self._attempt()
            except MirrorError as ex:
                other = self._other_trailer(ex)
                if other is None:
                    self._check_screen()  # the screen going away is what to say, if it did
                    raise
                await self._retry_with(other)
                if self._stop.is_set():
                    self._check_screen()
                    ended = "Stopped"
                else:
                    ended = await self._attempt()
            self._set(State.STOPPED, ended)
        except asyncio.CancelledError:
            self._set(State.STOPPED, "Cancelled")
            raise
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Mirroring failed", exc_info=True)
            self.error = self.report.error = ex
            self._set(State.FAILED, str(ex))
            raise
        finally:
            try:
                await asyncio.shield(self._cleanup())
            finally:
                self.finished.set()

    async def stop(self) -> None:
        """End the session. While it is still being set up, setting up is abandoned at once."""
        self._stop.set()
        if self._starting and self._task and not self._task.done():
            self._task.cancel()

    async def _attempt(self) -> str:
        self._starting = True
        try:
            await self._start()
        finally:
            self._starting = False
        return await self._supervise()

    def _other_trailer(self, ex: MirrorError) -> Optional[str]:
        """The other avcC ending, if the TV dropped the video at once on a first session."""
        if self._retried or self.saved or self._stop.is_set() or not self._dropped_at_once:
            return None
        _LOGGER.info("%s", ex)
        return next(trailer for trailer in TRAILERS if trailer != self.profile.trailer)

    async def _retry_with(self, trailer: str) -> None:
        first = self.report
        _LOGGER.info("The TV dropped the video at once with avcC ending %s: setting up again with %s",
                     first.trailer, trailer)
        self._set(State.STARTING, Text("mirror_retry_trailer", "The TV dropped the video at once: trying again "
                                       "with the other ending of the H.264 header"))
        await asyncio.shield(self._release())
        self._retried = True
        self.profile = dataclasses.replace(self.profile, trailer=trailer)
        self.report = SessionReport(trailer=trailer, retried=(
            f"the TV closed the video connection after {first.data_closed_after} frames with avcC ending "
            f"{first.trailer}"))

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        s = self.settings
        if self.target.needs_fairplay:
            raise needs_fairplay(self.target.name)
        self._stopping = self._timing_probed = self._timing_quiet = self._dropped_at_once = False
        self._set(State.STARTING, Text("checking_ffmpeg", "Checking FFmpeg and encoders"))
        ff = ffmpeg.find_ffmpeg(s.ffmpeg_path)
        encoder = await loop.run_in_executor(None, ffmpeg.pick_encoder, ff, s.encoder, True)
        self.report.encoder = encoder
        backend, monitor, region = TEST_PATTERN, None, None
        if self.source == DESKTOP:
            if extended.wanted(s):
                monitor = await self._virtual_screen()
                s = self.settings  # now without the display and window choices
            else:
                monitors = ffmpeg.list_monitors()
                monitor = monitors[max(0, min(s.monitor_index, len(monitors) - 1))] if monitors else None
                if s.window_title and s.capture_backend != "test":
                    monitor, region = ffmpeg.window_region(s.window_title, monitors)
                    _LOGGER.info("Window %r is at %s on %s", s.window_title, region, monitor.label)
            backend = await loop.run_in_executor(None, ffmpeg.pick_capture_backend, ff, s, monitor, region)

        self._set(State.CONNECTING, self.target.name)
        try:
            await asyncio.wait_for(self._set_up(), START_SECONDS)
        except asyncio.TimeoutError as ex:
            _LOGGER.info("Setting up took more than %d s", START_SECONDS)
            raise refused(None, self.report.steps, "setup") from ex
        est = self.report.established
        assert est is not None
        self._stats = StageTimes(kbps=s.video_bitrate_kbps, fps=self.report.fps, lead_ms=est.plan.lead_ms)
        self._frames = FrameQueue(self.report.fps * QUEUE_SECONDS)
        # If the first choice dies before its first frame (driver refuses ddagrab, GPU encoder out
        # of sessions), _pump goes on with GDI capture and then with the CPU encoder.
        self._capture = (ff, monitor, region)
        self._sources = [(backend, encoder)]
        if backend == "ddagrab":
            self._sources.append(("gdigrab", encoder))
        if encoder != "libx264":
            self._sources.append((self._sources[-1][0], "libx264"))
        self._start_source()

    async def _virtual_screen(self) -> ffmpeg.Monitor:
        """Extend screen's virtual monitor, added as a cast adds it (extended.py), the first time;
        a session set up again for the other avcC ending captures the same one."""
        if self._xscreen is None:
            self._set(State.EXTENDING)
            self._xscreen = extended.ExtendedScreen(on_lost=self._stop.set)
            mode = extend_mode(self.settings.extend_mode)
            if mode != self.settings.extend_mode:
                _LOGGER.info("Adding the virtual screen at %s, not %s: mirroring sends no more than %dx%d", mode,
                             self.settings.extend_mode, *MAX_CANVAS)
            monitor = await self._xscreen.start(mode)
            self.virtual_device = monitor.device
            self.settings = extended.screen_settings(self.settings)
            _LOGGER.info("Mirroring virtual screen %s: %dx%d, fitted into the TV's screen", monitor.device,
                         monitor.width, monitor.height)
        monitor = self._xscreen.monitor
        assert monitor is not None
        return monitor

    def _check_screen(self) -> None:
        """The virtual screen went away under the session (sleep, a display change): end it with
        the error a cast ends with, not as if it had been stopped."""
        if self._xscreen is not None and self._xscreen.lost:
            raise vdisplay.VddError("lost")

    def _start_source(self) -> None:
        """FFmpeg with the next way of capturing and encoding, its frames into the frame queue."""
        loop = asyncio.get_running_loop()
        ff, monitor, region = self._capture
        backend, encoder = self._using = self._sources.pop(0)
        width, height = self.report.canvas or FALLBACK_CANVAS
        cmd = build_command(ff, self.settings, source=self.source, encoder=encoder, width=width, height=height,
                            fps=self.report.fps, backend=backend, monitor=monitor, region=region)
        self.report.encoder = encoder
        label = ffmpeg.ENCODER_LABELS.get(encoder, encoder)
        self._set(State.STARTING, Text("encoder_via", "{encoder} via {backend}", encoder=label,
                                       backend="testsrc" if self.source == TEST_PATTERN else backend))
        frames, stats = self._frames, self._stats
        assert frames is not None

        def take(unit: Optional[Unit]) -> None:
            if unit is not None and unit.kind == FRAME:
                stats.arrived()
            frames.put(unit)

        def hand_over(unit: Optional[Unit]) -> None:  # from FFmpeg's pipe thread
            try:
                loop.call_soon_threadsafe(take, unit)
            except RuntimeError:  # the loop is closed: the session is long over
                pass

        self._ffmpeg = FFmpegSource(cmd, hand_over)
        self._ffmpeg.start()

    async def _next_source(self) -> None:
        """FFmpeg ended before its first frame: the next way of capturing and encoding, on the
        session as it is (the TV is waiting for its first frame)."""
        failed = self._ffmpeg
        assert failed is not None
        await asyncio.get_running_loop().run_in_executor(None, self._stop_ffmpeg, failed)
        backend, encoder = self._using
        _LOGGER.warning("%s with %s failed, trying the next option. %s", backend, encoder, failed.errors())
        self._start_source()

    async def _set_up(self) -> None:
        """Connect and pair-verify, GET /info, SETUP, the video connection, RECORD, /feedback."""
        s = self.settings
        negotiator = self._negotiator = Negotiator(
            self.target, timing_port=0, audio_port=0, steps=self.report.steps)
        negotiator.on_reply = self._on_setup_reply
        channel = await negotiator.connect()
        # The TV's timing requests and its (silent) audio control port, on ports a firewall rule names.
        transport, self._responder = await bind_udp(NtpResponder, channel.local_ip)
        self._transports.append(transport)
        negotiator.timing_port = self.report.timing_port = self._responder.port
        transport, _ = await bind_udp(asyncio.DatagramProtocol, channel.local_ip, taken=(negotiator.timing_port,))
        self._transports.append(transport)
        negotiator.audio_port = transport.get_extra_info("sockname")[1]
        _LOGGER.info("Timing (NTP) on UDP port %d, audio control on UDP port %d (allow inbound UDP %d-%d)",
                     negotiator.timing_port, negotiator.audio_port, TIMING_PORTS.start, TIMING_PORTS.stop - 1)
        info_answered = True
        try:
            self.report.info = await channel.info()
        except MirrorError as ex:
            # Unanswered, the connection is out of step: SETUP then goes on a new one (establish).
            info_answered = ex.code is not None
            _LOGGER.warning("%s", ex)

        plan = self.profile.plan()
        _LOGGER.info("Setting up mirroring to %s (%s): %s", self.target.name,
                     "saved settings" if self.saved else "first time", plan.describe())
        est = self.report.established = await negotiator.establish(plan)
        for note in est.fallbacks:
            _LOGGER.info("Fallback: %s", note)
        channel = negotiator.channel
        assert channel is not None
        if info_answered:
            await self._info_after_setup(est, channel)
        limit = canvas_limit(s, self.max_canvas)
        self.report.canvas = choose_canvas(self.report.info, self.target, limit)
        self.report.fps = choose_fps(s.framerate, self.report.info)

        self._send_buffer = send_buffer(s.video_bitrate_kbps, self.report.fps)
        try:
            self._data = await DataChannel.open(channel.remote_ip, est.data_port, buffer=self._send_buffer)
        except (OSError, asyncio.TimeoutError) as ex:
            self.report.data_channel = f"refused ({answer_of(ex)})"
            self.report.steps["video connection"] = answer_of(ex)
            raise data_refused(est.data_port, answer_of(ex), self.report.steps) from ex
        self.report.data_channel = "open"
        self.report.steps["video connection"] = "ok"
        self._cipher = VideoCipher(video_key(channel.derive, est.video_id))
        await negotiator.record(est)
        self._record_ns = now_ns()
        self._feedback = Feedback(channel)
        self._feedback.start()

    def _on_setup_reply(self, reply: dict) -> None:
        """As soon as a SETUP answer names them: probe the TV's timing port, open its event channel."""
        channel = self._negotiator.channel if self._negotiator is not None else None
        if channel is None:
            return
        port = reply.get("timingPort")
        if isinstance(port, int) and port > 0 and self._responder is not None and not self._timing_probed:
            # From our timing port to the one the TV asks from: a firewall that blocks inbound UDP
            # then lets its requests through, as answers to these.
            self._timing_probed = True
            self._responder.probe_receiver(channel.remote_ip, port)
        port = reply.get("eventPort")
        if isinstance(port, int) and port > 0 and self._events is None:
            self._events = asyncio.ensure_future(open_event_channel(channel, port))

    async def _info_after_setup(self, est: Established, channel: ControlChannel) -> None:
        """Some TVs only say how big their screen is once the session exists: in the answer to
        a control-first SETUP (combinedGetInfoWithControlSetup), or to GET /info after it."""
        if self.report.info is not None and self.report.info.displays:
            return
        for reply in est.replies:
            if isinstance(reply.get("info"), dict):
                info = ReceiverInfo.parse(reply["info"])
                if info.displays:
                    _LOGGER.info("%s (from SETUP): %s", self.target.name, info.summary())
                    self.report.info = info
                    return
        try:
            info = await channel.info()
        except MirrorError as ex:
            _LOGGER.info("GET /info after SETUP: %s", ex)
            return
        if info.displays:
            self.report.info = info

    async def _supervise(self) -> str:
        assert self._data is not None and self._feedback is not None and self._responder is not None
        est = self.report.established
        assert est is not None
        pump = asyncio.ensure_future(self._pump())
        waits = {
            "stop": asyncio.ensure_future(self._stop.wait()),
            "lost": asyncio.ensure_future(self._feedback.lost.wait()),
            "closed": asyncio.ensure_future(self._data.closed.wait()),
        }
        started = now_ns()
        try:
            while True:
                done, _ = await asyncio.wait({pump, *waits.values()}, timeout=SUPERVISE_SECONDS)
                now = now_ns()
                if waits["stop"] in done:
                    self._check_screen()
                    self._stopping = True
                    self._set(State.STOPPING)
                    return "Stopped"
                if pump in done:
                    try:
                        pump.result()  # raises what ended it
                    except _VideoClosed as ex:
                        return await self._video_closed(str(ex))
                    return "Stopped"
                if waits["closed"] in done:
                    return await self._video_closed(self._data.close_reason)
                if waits["lost"] in done:
                    return Text("mirror_tv_ended", "The TV ended screen mirroring")
                if self.seconds is not None and now - started >= self.seconds * NS:
                    self._stopping = True
                    return Text("mirror_time_up", "Mirrored for {seconds} s, as asked", seconds=f"{self.seconds:g}")
                if est.plan.timing == NTP:
                    self._check_timing(now)
                self._check_frames(now)
                if self._stats.due(now):
                    self._stats.log_window(now, self._responder.requests)
        finally:
            pump.cancel()
            for wait in waits.values():
                wait.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass

    def _check_timing(self, now: int) -> None:
        """The TV must ask for the time soon after RECORD; a long pause later is logged."""
        responder = self._responder
        assert responder is not None
        if not responder.requests:
            if now - self._record_ns > NTP_GRACE_SECONDS * NS:
                raise no_timing(responder.port, self.report.steps)
            return
        quiet = now - (responder.last_request_ns or now) > NTP_QUIET_SECONDS * NS
        if quiet and not self._timing_quiet:
            _LOGGER.warning("The TV has not asked for the time for %d s (after %d times): if a firewall or a network "
                            "change now keeps its requests out, its picture drifts", NTP_QUIET_SECONDS,
                            responder.requests)
        elif self._timing_quiet and not quiet:
            _LOGGER.info("The TV asks for the time again")
        self._timing_quiet = quiet

    def _check_frames(self, now: int) -> None:
        """FFmpeg must give a first frame, and then keep giving them."""
        assert self._frames is not None and self._ffmpeg is not None
        last = self._frames.last_frame_ns
        if last is None and now - self._ffmpeg.started_ns > FIRST_FRAME_SECONDS * NS:
            raise self._no_picture(FIRST_FRAME_SECONDS)
        if last is not None and now - last > FRAME_GAP_SECONDS * NS:
            raise self._no_picture(FRAME_GAP_SECONDS)

    def _no_picture(self, seconds: int) -> MirrorError:
        assert self._ffmpeg is not None
        errors = self._ffmpeg.errors()
        self.report.steps["picture"] = f"none for {seconds} s"
        return MirrorError(Text(
            "mirror_no_picture", "No picture came from the screen capture or the encoder ({encoder}) for {seconds} s."
            "{errors}", encoder=ffmpeg.ENCODER_LABELS.get(self.report.encoder, self.report.encoder), seconds=seconds,
            errors=f"\n{errors}" if errors else "",
        ), step="encoder", steps=self.report.steps)

    async def _video_closed(self, reason: str) -> str:
        """The TV closed the video connection. If it closes the control connection as well, as
        when someone ends mirroring on the TV, the session is over; if it did so before the
        second keyframe, or kept the control connection, it did not take the video."""
        assert self._feedback is not None
        channel = self._negotiator.channel if self._negotiator is not None else None
        def control_closed() -> bool:
            return channel is None or channel.closed or self._feedback.lost.is_set()

        waited = 0.0
        while not control_closed() and waited < TV_ENDED_SECONDS:
            await asyncio.sleep(0.05)
            waited += 0.05
        if control_closed() and self._stats.keyframes > 1:
            self._stopping = True
            self.report.data_channel = "closed by the TV, with the control connection"
            return Text("mirror_tv_ended", "The TV ended screen mirroring")
        raise self._data_closed(reason)

    def _data_closed(self, reason: str = "") -> MirrorError:
        assert self._data is not None
        self._dropped_at_once = self._stats.keyframes <= 1
        self.report.data_closed_after = self._stats.frames
        self.report.data_channel = self._data.close_reason or reason or "closed by the TV"
        self.report.steps["video"] = f"closed after {self._stats.frames} frames"
        return data_closed(self._stats.frames, self.report.steps)

    def _stalled(self) -> MirrorError:
        self.report.data_channel = f"stalled (nothing taken for {VIDEO_STALL_SECONDS} s)"
        self.report.steps["video"] = f"stalled after {self._stats.frames} frames"
        return MirrorError(Text(
            "mirror_video_stalled", "The TV took none of the video for {seconds} s. Check the network between this "
            "PC and the TV (the Wi-Fi signal, or use a cable), then try again.", seconds=VIDEO_STALL_SECONDS,
        ), step="video", steps=self.report.steps)

    def _drop_reason(self, idr: bool, waiting: bool, late: bool) -> str:
        """Why the frame at hand is not to be sent ("" to send it). Dropping a frame costs the
        frames after it up to the next keyframe, so a frame goes unless it cannot help: a newer
        keyframe is already waiting, the TV takes the video slower than it comes, or the frame
        is past its lead with newer ones waiting behind it."""
        assert self._frames is not None and self._data is not None
        frames = self._frames
        if idr:
            return "a newer keyframe is waiting" if frames.keyframe_waiting() else ""
        if waiting:
            return "waiting for a keyframe"
        if frames.frames > MAX_BACKLOG_FRAMES:
            return f"{frames.frames} frames are waiting to be sent"
        if self._data.buffered > self._send_buffer:
            return f"{self._data.buffered // 1024} KB are still waiting for the network"
        if late and frames.frames:
            return "frames are older than the lead when they come to be sent"
        return ""

    async def _drained(self) -> None:
        """Wait for the video connection to take what was written, down to its buffers. A TV
        that takes none of it for VIDEO_STALL_SECONDS has stalled; one that takes it slowly
        has not (Windows only shows a send done when all of it went)."""
        assert self._data is not None
        data = self._data
        left = data.buffered
        while True:
            try:
                return await asyncio.wait_for(data.drain(), VIDEO_STALL_SECONDS)
            except asyncio.TimeoutError:
                if data.buffered >= left:
                    raise self._stalled() from None
                left = data.buffered

    async def _pump(self) -> None:
        """FFmpeg's frames to the TV, as they come."""
        assert self._data is not None and self._cipher is not None and self._ffmpeg is not None
        assert self._frames is not None
        est = self.report.established
        assert est is not None
        frames = self._frames
        lead_ns = est.plan.lead_ms * 1_000_000
        mapper = PtsMapper()
        trailer = self.profile.trailer
        params: Optional[tuple[bytes, bytes]] = None
        avcc = b""
        picture = (0, 0)
        length_size = 4
        send_codec = True
        wait_for_idr = True
        slow_logged = 0
        while True:
            unit = await frames.get()
            if unit is None:
                if self._stopping:
                    return
                self._check_screen()
                if frames.last_frame_ns is None and self._sources:  # no frame at all from this way
                    await self._next_source()
                    continue
                if self._xscreen is not None and await self._xscreen.lost_within():
                    raise vdisplay.VddError("lost")  # ddagrab lost its output before the driver said so
                raise MirrorError(ended_text(self._ffmpeg), step="encoder")
            taken = now_ns()
            trimmed = frames.take_trimmed()
            if trimmed:
                if not wait_for_idr:
                    _LOGGER.info("The TV took no video for a while: %d frames were dropped, and frames up to the "
                                 "next keyframe will be", trimmed)
                wait_for_idr = True
                self._stats.drop(trimmed)
            if unit.kind == CONFIG:
                config = parse_avcc(unit.data)
                length_size = config.length_size
                if config.sps and config.pps and (config.sps[0], config.pps[0]) != params:
                    params = (config.sps[0], config.pps[0])
                    send_codec = True
                continue
            if not unit.data:
                continue
            nals, sps, pps = frame_nals(split_avcc(unit.data, length_size))
            if sps and pps and (sps[0], pps[0]) != params:
                params = (sps[0], pps[0])  # new parameter sets in the stream: a new codec packet
                send_codec = True
            if not nals or params is None:
                continue
            idr = is_idr(nals)
            taken_at = mapper.map(unit.pts_ms, unit.arrived_ns)
            why = self._drop_reason(idr, wait_for_idr or send_codec, taken - taken_at > lead_ns)
            if why:
                if not wait_for_idr:
                    _LOGGER.info("Dropping frames up to the next keyframe: %s", why)
                wait_for_idr = True
                self._stats.drop()
                continue
            wait_for_idr = False
            timestamp = to_ntp(taken_at + lead_ns)
            packets = []
            if send_codec:
                avcc = avcc_record(params[0], params[1], trailer)
                info = parse_sps(params[0])
                picture = (info.width, info.height)
                if self.report.picture != picture:
                    _LOGGER.info("Sending %dx%d H.264 (profile %d, level %.1f), avcC %s", info.width, info.height,
                                 info.profile_idc, info.level_idc / 10, avcc.hex())
                self.report.picture = picture
                packets.append(codec_packet(avcc, timestamp, *picture))
                self.report.codec_packets += 1
                send_codec = False
            payload = join_avcc(nals)
            packets.append(self._cipher.video_packet(payload, timestamp, idr))
            packed = now_ns()
            data = b"".join(packets)
            try:
                self._data.write(data)
                await self._drained()
            except ConnectionError as ex:
                raise _VideoClosed(f"closed by the TV ({ex})") from ex
            sent = now_ns()
            if sent - packed > lead_ns and sent - slow_logged > LOG_SECONDS * NS:
                slow_logged = sent
                _LOGGER.warning("The network took %.0f ms to take a %d KB frame, more than the lead (%d ms)",
                                (sent - packed) / 1e6, len(data) // 1024, est.plan.lead_ms)
            self._stats.add(len(data), idr, buffered=self._data.buffered, pipe=max(0, unit.arrived_ns - taken_at),
                            behind=mapper.behind(unit.pts_ms, unit.arrived_ns), queue=taken - unit.arrived_ns,
                            pack=packed - taken, send=sent - packed, age=sent - taken_at)
            self.report.frames_sent = self._stats.frames
            if self._stats.frames == 1:
                self.report.first_frame_ms = (sent - self._ffmpeg.started_ns) / 1e6
                if self.source == TEST_PATTERN:
                    what = Text("mirror_what_test", "the test pattern")
                elif self.virtual_device:
                    what = Text("mirror_what_extended", "the extended screen")
                else:
                    what = Text("mirror_what_screen", "this PC's screen")
                self._set(State.CASTING, Text(
                    "mirror_casting", "Mirroring {what} to {device} ({width}x{height}, {fps} fps)",
                    what=what, device=self.target.name, width=picture[0], height=picture[1], fps=self.report.fps,
                ))

    async def _release(self) -> None:
        """Stop what the session started and tear it down on the TV. Each part is taken before
        it is stopped, so that this can run again (a retry, then the end) without doing twice."""
        loop = asyncio.get_running_loop()
        self._stopping = True
        feedback, self._feedback = self._feedback, None
        if feedback is not None:
            await feedback.stop()
            self.report.feedback = dict(feedback.answers)
        negotiator, self._negotiator = self._negotiator, None
        est = self.report.established
        # TEARDOWN while FFmpeg stops, so that the TV leaves mirroring at once.
        jobs = []
        if self._ffmpeg is not None:
            jobs.append(loop.run_in_executor(None, self._stop_ffmpeg, self._ffmpeg))
        if negotiator is not None and est is not None:
            jobs.append(negotiator.teardown(est.uri))
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        data, self._data = self._data, None
        if data is not None:
            await data.close()
        events, self._events = self._events, None
        if events is not None:
            if not events.done():
                events.cancel()
            elif not events.cancelled() and events.exception() is None and events.result() is not None:
                events.result().close()
        if negotiator is not None:
            negotiator.close()
        responder, self._responder = self._responder, None
        if responder is not None:
            self.report.ntp_requests = responder.requests
            self._stats.log_last(responder.requests)
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        self.report.latency = self._stats.summary()
        self.report.frames_sent = self._stats.frames

    @staticmethod
    def _stop_ffmpeg(source: FFmpegSource) -> None:
        try:
            source.stop()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Stopping FFmpeg failed", exc_info=True)

    async def _cleanup(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self._release()
        finally:
            if self._xscreen is not None:
                # Only once FFmpeg is gone (_release): ddagrab losing its output would turn a stop into an error.
                await self._xscreen.stop()
                self._xscreen = None
                self.virtual_device = ""
        _LOGGER.info("Mirroring ended: %d frames sent, %d dropped; the TV asked for the time %d times; "
                     "/feedback answers %s; steps %s", self._stats.frames, self._stats.dropped,
                     self.report.ntp_requests, self.report.feedback or "none", self.report.steps)
        est = self.report.established
        # Remembered only once the TV has taken frames and the time: what was asked for works.
        if (self.remember and est is not None and self.error is None and self._stats.frames
                and self.report.ntp_requests and self.target.identifier):
            profile = profiles.MirrorProfile.of(est.plan, self.profile.trailer)
            info = self.report.info.summary() if self.report.info else ""
            try:
                await loop.run_in_executor(None, lambda: profiles.save(
                    self.target.identifier, profile, name=self.target.name, info=info))
            except OSError as ex:
                _LOGGER.warning("Could not save how to mirror to %s: %s", self.target.name, ex)
