"""One screen mirroring session: the desktop (or a test pattern) on the TV, a fraction of a second
behind, the way an iPhone or a Mac mirrors.

    pick the encoder -> connect and pair-verify -> GET /info (how big the TV's screen is)
    -> SETUP (negotiation.py) -> event channel -> video connection -> RECORD -> /feedback
    -> FFmpeg -> frames, each stamped with when to show it and encrypted -> TEARDOWN

It speaks to the tray and the command line as a cast does (session.CastSession): run() until
the session ends, stop() from anywhere on the loop, and on_status(State, message) on the way,
the messages and errors being Texts.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from .. import ffmpeg
from ..config import Settings
from ..session import State, StatusCallback
from ..text import Text
from . import profiles
from .clock import NS, TIMING_PORTS, NtpResponder, PtsMapper, bind_udp, now_ns, to_ntp
from .control import FEATURE_1080P, ReceiverInfo, Target, open_event_channel
from .datachannel import DataChannel
from .errors import MirrorError, answer_of, needs_fairplay, no_timing
from .flv import CONFIG
from .h264 import avcc_record, frame_nals, is_idr, join_avcc, parse_avcc, parse_sps, split_avcc
from .negotiation import NTP, Established, Feedback, Negotiator
from .packets import VideoCipher, codec_packet, video_key
from .source import DESKTOP, TEST_PATTERN, FFmpegSource, Unit, build_command, ended_text
from .stats import StageTimes

_LOGGER = logging.getLogger(__name__)

MAX_CANVAS = (1920, 1080)  # never more, whatever the TV's screen: 4K costs more delay than it is worth
FALLBACK_CANVAS = (1280, 720)  # a TV that gives no displays[] and no 1080p feature bit
NTP_GRACE_SECONDS = 10  # from RECORD until the TV must have asked for the time
MAX_BACKLOG_FRAMES = 3  # frames waiting to be sent; beyond that, drop up to the next keyframe
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


def choose_fps(wanted: int, info: Optional[ReceiverInfo]) -> int:
    most = info.displays[0].max_fps if info is not None and info.displays and info.displays[0].max_fps else 60
    return max(1, min(wanted or 30, most))


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
    error: Optional[BaseException] = None


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
        self.saved = saved is not None
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
        self._events: Optional[asyncio.BaseTransport] = None
        self._data: Optional[DataChannel] = None
        self._feedback: Optional[Feedback] = None
        self._ffmpeg: Optional[FFmpegSource] = None
        self._cipher: Optional[VideoCipher] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stats = StageTimes()
        self._record_ns = 0
        self._stopping = False

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
            self._starting = True
            try:
                await self._start()
            finally:
                self._starting = False
            ended = await self._supervise()
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

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        s = self.settings
        if self.target.needs_fairplay:
            raise needs_fairplay(self.target.name)
        self._set(State.STARTING, Text("checking_ffmpeg", "Checking FFmpeg and encoders"))
        ff = ffmpeg.find_ffmpeg(s.ffmpeg_path)
        encoder = await loop.run_in_executor(None, ffmpeg.pick_encoder, ff, s.encoder, True)
        self.report.encoder = encoder
        backend, monitor, region = TEST_PATTERN, None, None
        if self.source == DESKTOP:
            if s.extend_screen:
                _LOGGER.info("Screen mirroring shows a display or a window; the virtual screen is not used")
            monitors = ffmpeg.list_monitors()
            monitor = monitors[max(0, min(s.monitor_index, len(monitors) - 1))] if monitors else None
            if s.window_title and s.capture_backend != "test":
                monitor, region = ffmpeg.window_region(s.window_title, monitors)
                _LOGGER.info("Window %r is at %s on %s", s.window_title, region, monitor.label)
            backend = await loop.run_in_executor(None, ffmpeg.pick_capture_backend, ff, s, monitor, region)

        self._set(State.CONNECTING, self.target.name)
        negotiator = self._negotiator = Negotiator(
            self.target, timing_port=0, audio_port=0, steps=self.report.steps)
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
        try:
            self.report.info = await channel.info()
        except MirrorError as ex:
            _LOGGER.warning("%s", ex)
        limit = canvas_limit(s, self.max_canvas)
        width, height = self.report.canvas = choose_canvas(self.report.info, self.target, limit)
        fps = self.report.fps = choose_fps(s.framerate, self.report.info)

        plan = self.profile.plan()
        _LOGGER.info("Setting up mirroring to %s (%s): %s", self.target.name,
                     "saved settings" if self.saved else "first time", plan.describe())
        est = self.report.established = await negotiator.establish(plan)
        for note in est.fallbacks:
            _LOGGER.info("Fallback: %s", note)
        channel = negotiator.channel
        assert channel is not None
        if est.receiver_timing_port:
            # From our timing port to the one the TV asks from: a firewall that blocks inbound UDP
            # then lets its requests through, as answers to these.
            self._responder.probe_receiver(channel.remote_ip, est.receiver_timing_port)
        if est.event_port:
            self._events = await open_event_channel(channel, est.event_port)
        try:
            self._data = await DataChannel.open(channel.remote_ip, est.data_port)
        except (OSError, asyncio.TimeoutError) as ex:
            self.report.data_channel = f"refused ({answer_of(ex)})"
            self.report.steps["video connection"] = answer_of(ex)
            raise MirrorError(Text(
                "mirror_data_refused", "The TV did not take the video connection (TCP port {port}: {answer}).",
                port=est.data_port, answer=answer_of(ex),
            ), step="video connection", steps=self.report.steps) from ex
        self.report.data_channel = "open"
        self.report.steps["video connection"] = "ok"
        self._cipher = VideoCipher(video_key(channel.derive, est.video_id))
        await negotiator.record(est)
        self._record_ns = now_ns()
        self._feedback = Feedback(channel)
        self._feedback.start()

        cmd = build_command(ff, s, source=self.source, encoder=encoder, width=width, height=height, fps=fps,
                            backend=backend, monitor=monitor, region=region)
        label = ffmpeg.ENCODER_LABELS.get(encoder, encoder)
        self._set(State.STARTING, Text("encoder_via", "{encoder} via {backend}", encoder=label,
                                       backend="testsrc" if self.source == TEST_PATTERN else backend))
        self._stats = StageTimes(kbps=s.video_bitrate_kbps, fps=fps, lead_ms=est.plan.lead_ms)

        def hand_over(unit: Optional[Unit]) -> None:  # from FFmpeg's pipe thread
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, unit)
            except RuntimeError:  # the loop is closed: the session is long over
                pass

        self._ffmpeg = FFmpegSource(cmd, hand_over)
        self._ffmpeg.start()

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
                    self._stopping = True
                    self._set(State.STOPPING)
                    return "Stopped"
                if pump in done:
                    pump.result()  # raises what ended it
                if waits["closed"] in done:
                    raise self._data_closed()
                if waits["lost"] in done:
                    return Text("mirror_tv_ended", "The TV ended screen mirroring")
                if self.seconds is not None and now - started >= self.seconds * NS:
                    self._stopping = True
                    return Text("mirror_time_up", "Mirrored for {seconds} s, as asked", seconds=f"{self.seconds:g}")
                if (est.plan.timing == NTP and self._responder.requests == 0
                        and now - self._record_ns > NTP_GRACE_SECONDS * NS):
                    raise no_timing(self._responder.port, self.report.steps)
                if self._stats.due(now):
                    self._stats.log_window(now)
        finally:
            pump.cancel()
            for wait in waits.values():
                wait.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass

    def _data_closed(self, reason: str = "") -> MirrorError:
        assert self._data is not None
        self.report.data_closed_after = self._stats.frames
        self.report.data_channel = self._data.close_reason or reason or "closed by the TV"
        self.report.steps["video"] = f"closed after {self._stats.frames} frames"
        return MirrorError(Text(
            "mirror_data_closed", "The TV closed the video connection after {frames} frames. It may not take "
            "this video: please report this TV, with mirrorbridge.log.", frames=self._stats.frames,
        ), step="video", steps=self.report.steps)

    async def _pump(self) -> None:
        """FFmpeg's frames to the TV, as they come."""
        assert self._data is not None and self._cipher is not None and self._ffmpeg is not None
        est = self.report.established
        assert est is not None
        lead_ns = est.plan.lead_ms * 1_000_000
        mapper = PtsMapper()
        trailer = self.profile.trailer
        params: Optional[tuple[bytes, bytes]] = None
        avcc = b""
        picture = (0, 0)
        length_size = 4
        send_codec = True
        wait_for_idr = True
        while True:
            unit = await self._queue.get()
            if unit is None:
                if self._stopping:
                    return
                raise MirrorError(ended_text(self._ffmpeg), step="encoder")
            taken = now_ns()
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
            if not idr and (wait_for_idr or send_codec or self._queue.qsize() > MAX_BACKLOG_FRAMES):
                if not wait_for_idr:
                    _LOGGER.info("The TV takes the video slower than it comes (%d frames waiting); dropping "
                                 "frames up to the next keyframe", self._queue.qsize())
                wait_for_idr = True
                self._stats.drop()
                continue
            wait_for_idr = False
            taken_at = mapper.map(unit.pts_ms, unit.arrived_ns)
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
                await self._data.drain()
            except ConnectionError as ex:
                raise self._data_closed(f"closed by the TV ({ex})") from ex
            sent = now_ns()
            self._stats.add(len(data), idr, pipe=max(0, unit.arrived_ns - taken_at), queue=taken - unit.arrived_ns,
                            pack=packed - taken, send=sent - packed, age=sent - taken_at)
            self.report.frames_sent = self._stats.frames
            if self._stats.frames == 1:
                self.report.first_frame_ms = (sent - self._ffmpeg.started_ns) / 1e6
                what = (Text("mirror_what_test", "the test pattern") if self.source == TEST_PATTERN
                        else Text("mirror_what_screen", "this PC's screen"))
                self._set(State.CASTING, Text(
                    "mirror_casting", "Mirroring {what} to {device} ({width}x{height}, {fps} fps)",
                    what=what, device=self.target.name, width=picture[0], height=picture[1], fps=self.report.fps,
                ))

    async def _cleanup(self) -> None:
        loop = asyncio.get_running_loop()
        self._stopping = True
        if self._feedback is not None:
            await self._feedback.stop()
            self.report.feedback = dict(self._feedback.answers)
        if self._ffmpeg is not None:
            try:
                await loop.run_in_executor(None, self._ffmpeg.stop)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("Stopping FFmpeg failed", exc_info=True)
        if self._data is not None:
            await self._data.close()
        if self._events is not None:
            self._events.close()
            self._events = None
        negotiator = self._negotiator
        if negotiator is not None:
            if self.report.established is not None:
                await negotiator.teardown(self.report.established.uri)
            negotiator.close()
        if self._responder is not None:
            self.report.ntp_requests = self._responder.requests
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        if self._stats.frames:
            self._stats.log_window()
        self.report.latency = self._stats.summary()
        self.report.frames_sent = self._stats.frames
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
