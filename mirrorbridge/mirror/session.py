"""One screen mirroring session: the desktop (or a test pattern) on the TV, a fraction of a second
behind, the way an iPhone or a Mac mirrors.

    pick the encoder -> connect and pair-verify -> GET /info (how big the TV's screen is)
    -> SETUP (negotiation.py; the TV's timing port is probed and its event channel opened as
    soon as an answer names them) -> video connection -> RECORD -> /feedback
    -> FFmpeg -> frames, each stamped with when to show it and encrypted -> TEARDOWN

It speaks to the tray and the command line as a cast does (session.CastSession): run() until
the session ends, stop() from anywhere on the loop, and on_status(State, message) on the way,
the messages and errors being Texts. It works from a copy of the settings it was given: what
is changed in the menus while it runs is for the next session.

Delay comes first, but a dropped frame costs every frame after it up to the next keyframe:
until then, the picture on the TV is frozen. So a network that holds the video up for a moment
(Wi-Fi does, for a few hundred ms now and then) is ridden out: the frames that waited are sent
late, with the times they were taken for, and the TV catches up. The TV is given CATCH_UP_MS
for it: frames go late as long as they have been past their lead for no longer than that and
the video connection holds no more than about CATCH_UP_MS of the bitrate (catch_up_bytes). Only
past that, when the TV (or the network) takes the video slower than it comes, are frames
dropped up to the next keyframe, and the drop ends on a keyframe that is on time, so that the
TV is back to real time. A TV that takes nothing for VIDEO_STALL_SECONDS ends the session. So
does FFmpeg giving no picture (FIRST_FRAME_SECONDS, FRAME_GAP_SECONDS), but not gfxcapture's
without repeats once it gave a frame: it gives one only when the screen changes, so a screen
that stays as it is is no fault (FFmpeg ending is still seen to); setting up has START_SECONDS
in all.

Keyframes come only when needed (ffmpeg.mirror_encoder_args): a keyframe made through the
small buffer that keeps the delay down is a blur on text, so FFmpeg makes one when it starts
and none after. When frames were dropped (or the TV took none for a while, FrameQueue), FFmpeg
is started again (_restart): its first frame is a keyframe. That is at most once every
KEYFRAME_RESTART_SECONDS, without waiting for another frame (gfxcapture without repeats gives
none while the screen stays as it is), and the TV is told nothing: the frames' times go on from
where they were (PtsMapper.restart) and the same picture settings need no new codec packet.

FFmpeg is also started again when Windows took the screen capture away (a display or its driver
changing, a UAC prompt, the lock screen: capture.capture_lost), after CAPTURE_RETRY_DELAYS,
with the display looked up again; a capture that does not come back within
CAPTURE_LOST_SECONDS ends the session with its own error. FFmpeg ending before its first frame
for another reason (the driver refuses ddagrab, the GPU encoder is out of sessions) goes on
the next way, as a cast does: the graphics card's chain (capture.py: NVENC with ddagrab, or with
gfxcapture when the screen must be scaled and it repeats frames, or Scaling says so), then the
processor's, then GDI capture, then the CPU encoder. A graphics card chain that ends so (but
for Windows keeping the screen from it, which is no fault of the chain), or makes a picture of
the wrong size (its first SPS), gives way to the processor's on that display for the rest of
the run; one at the display's own size whose display comes back at a size to be scaled is
chosen again (_rechoose_chain).

Mirror screen settings: Latency (MIRROR_LATENCIES, the TV's wait for each frame, the lead:
Normal is the one saved for the TV) and Frame rate (choose_mirror_fps: Automatic is 60 with a
hardware encoder and a capture that keeps up, else 30, and 30 once the session falls back to
x264). More options > Debug has test switches for the sessions after they are set (lead_override,
keyframes_on_demand, on_arrival, Settings.mirror_scaler): the TV's wait in place of the
Latency's, which is never saved; a keyframe every 10 or 2 seconds (every 2 s, FFmpeg is not
started again for one); the processor's chain in place of the graphics card's, or gfxcapture
to scale even without repeats; and codec packets that ask the TV to show each frame as it
comes, not at its time
(packets.codec_packet). Test this TV and `mirror --probe` leave them out.

A TV that drops the video connection within AT_ONCE_SECONDS of the first frame of a first
session (nothing saved for it yet) may not take the avcC ending it was sent: the session is set
up once more with the other one (h264.TRAILERS), and what works is saved.

With Extend screen on, the desktop is the virtual monitor a cast would add (extended.py): it is
added before the TV is asked, captured as any display is and fitted into the TV's screen, and
kept for the whole session, through the retry above and FFmpeg's restarts, so the windows on
it stay there. It goes when the session ends, however it ends; if it goes away first (sleep),
the session ends with VddError("lost"), as a cast does.

PTP timing is an experiment (mirror --timing ptp, MIRRORBRIDGE_TIMING=ptp; ptp.py), for this run
only: never saved. Our PTP clock starts on UDP 319 and 320 before SETUP (NTP if they cannot be
had), the TV is added as its peer and named in SETPEERS after the SETUPs, and the video
connection opens only once the TV follows our clock (its first Delay_Req, within
MIRRORBRIDGE_PTP_LOCK_S) and that time is over without it announcing a better clock, so that the
codec packet and the first frame come after it. If a SETUP or SETPEERS is refused or the TV does
not follow, the session is set up again over NTP (unless MIRRORBRIDGE_PTP_NO_FALLBACK=1). The NTP
responder stays bound either way.

The PC's sound (sound.py) goes over the type-96 stream the session sets up anyway, for now only
when asked for (MIRRORBRIDGE_MIRROR_SOUND=1, mirror --sound; never for Test this TV, nor with PTP
timing): held until the first frame has gone, then sent with the picture's lead, the test tone
in its place for the test pattern. Nothing the sound does may stop or slow the picture: whatever
goes wrong stops the sound alone, and the person is told once (on_notice). A TV that closes the
session within SOUND_DROP_SECONDS of the first sound packet (SOUND_ENDED_SECONDS when it ends
mirroring as its remote does) gets it set up once more without sound (but for a run that asked
for the sound), and mirroring.json keeps that for the next ones, unless the TV closed that one
as soon.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import audio as system_audio
from .. import extended, ffmpeg, vdisplay
from ..config import Settings
from ..session import State, StatusCallback
from ..speakers import MutedSpeakers
from ..text import Text
from . import capture, negotiation, profiles, ptp, sound


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_switch_interval() -> Optional[float]:
    """MIRRORBRIDGE_SWITCH_INTERVAL_MS: experiment, Python's thread switch interval for the
    session (sys.setswitchinterval, 5 ms by default), so that PTP's threads wait less for the GIL."""
    try:
        return min(100.0, max(0.1, float(os.environ["MIRRORBRIDGE_SWITCH_INTERVAL_MS"]))) / 1000
    except (KeyError, ValueError):
        return None


def _env_slew_ns() -> Optional[int]:
    """MIRRORBRIDGE_PTS_SLEW_US: experiment, how far frame times may move per frame (us)."""
    try:
        return max(0, int(float(os.environ["MIRRORBRIDGE_PTS_SLEW_US"]) * 1000))
    except (KeyError, ValueError):
        return None
from .clock import NS, TIMING_PORTS, NtpResponder, PtsMapper, bind_udp, now_ns, to_ntp
from .control import FEATURE_1080P, ControlChannel, ReceiverInfo, Target, open_event_channel
from .datachannel import HEARTBEAT_IDLE_ONLY, DataChannel
from .errors import MirrorError, answer_of, data_closed, data_refused, needs_fairplay, no_timing, refused
from .flv import CONFIG, FRAME
from .h264 import TRAILERS, avcc_record, frame_nals, is_idr, join_avcc, parse_avcc, parse_sps, split_avcc
from .audiopackets import SPF, SR, choose_format, ms_to_samples
from .negotiation import NTP, PTP, AudioOffer, Established, Feedback, Negotiator
from .packets import ON_ARRIVAL, VideoCipher, codec_packet, video_key
from .sound import FAILED, AudioControl, MirrorSound, SoundKnobs
from .source import (DESKTOP, STDERR_LINES, TEST_PATTERN, FFmpegSource, FrameQueue, Unit, build_command,
                     ended_text)
from .stats import LOG_SECONDS, StageTimes

_LOGGER = logging.getLogger(__name__)

MAX_CANVAS = (1920, 1080)  # never more, whatever the TV's screen: 4K costs more delay than it is worth
FALLBACK_CANVAS = (1280, 720)  # a TV that gives no displays[] and no 1080p feature bit
NTP_GRACE_SECONDS = 10  # from RECORD until the TV must have asked for the time
NTP_QUIET_SECONDS = 15  # then, a pause this long in its requests is worth a warning
CATCH_UP_MS = 1000  # how long frames may be past their lead; beyond that, drop up to the next keyframe
QUEUE_SECONDS = 2  # frames kept at most while the TV takes none (source.FrameQueue)
SEND_BUFFER_FRAMES = 2  # the video connection's send buffers, in average frames
MIN_SEND_BUFFER = 16 * 1024
MAX_SEND_BUFFER = 256 * 1024
VIDEO_STALL_SECONDS = 5  # the TV taking none of the video this long ends the session
FIRST_FRAME_SECONDS = 15  # from starting FFmpeg to its first frame
FRAME_GAP_SECONDS = 10  # no frame from FFmpeg this long, once they came
START_SECONDS = 90  # connecting, pair-verify, GET /info, SETUP, the video connection and RECORD
TV_ENDED_SECONDS = 1.0  # after the video connection closes, for the control connection to close too
# The TV closing the video within this of the first frame did not take it (_at_once): in time,
# whatever the keyframes (it was the second keyframe when there was one every 2 s).
AT_ONCE_SECONDS = 2
SUPERVISE_SECONDS = 0.25
KEYFRAME_RESTART_SECONDS = 2.0  # FFmpeg started again for a keyframe at most this often
CAPTURE_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 3.0)  # a lost screen capture: FFmpeg again after each
CAPTURE_STABLE_SECONDS = 5.0  # an FFmpeg that gave frames this long: a capture lost after it is a new loss
CAPTURE_LOST_SECONDS = 10  # from the capture lost to a frame again, at most
# Mirror screen settings > Latency: the TV's wait for each frame (the lead), in ms; 0 is the one
# saved for the TV (profiles.MirrorProfile.lead_ms, 100 ms unless a TV needed more).
MIRROR_LATENCIES = {"low": 60, "normal": 0, "smooth": 200}
FAST_FPS, SLOW_FPS = 60, 30  # Frame rate Automatic (choose_mirror_fps)
FAST_CAPTURES = ("ddagrab", TEST_PATTERN)  # captures that keep up with a hardware encoder at FAST_FPS


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
    """`wanted` (30 if none), or less if the TV says it takes less."""
    most = info.displays[0].max_fps if info is not None and info.displays and info.displays[0].max_fps else 60
    return max(1, min(wanted or 30, most))


def choose_mirror_fps(wanted: object, info: Optional[ReceiverInfo], *, encoder: str,
                      backend: str) -> tuple[int, str]:
    """Mirror screen settings > Frame rate, and why, in words for the log: 30 or 60 as chosen;
    Automatic (0, or anything else) 60 with a hardware encoder and a capture that keeps up with it
    (FAST_CAPTURES: ddagrab, the test pattern), else 30 (x264, gdigrab). Never more than the TV
    takes (choose_fps)."""
    if wanted in (SLOW_FPS, FAST_FPS) and not isinstance(wanted, bool):
        want, why = int(wanted), "as chosen"
    elif encoder == "libx264":
        want, why = SLOW_FPS, "automatic: the CPU encoder"
    elif backend not in FAST_CAPTURES:
        want, why = SLOW_FPS, f"automatic: {backend or 'this'} capture"
    else:
        want, why = FAST_FPS, f"automatic: {ffmpeg.ENCODER_LABELS.get(encoder, encoder)} with {backend}"
    fps = choose_fps(want, info)
    return fps, why + (f", the TV takes {fps} at most" if fps < want else "")


def send_buffer(kbps: int, fps: int) -> int:
    """The video connection's send buffers (the kernel's and asyncio's): about
    SEND_BUFFER_FRAMES average frames, so that a slow network pushes back at once."""
    frame = kbps * 1000 // 8 // max(1, fps)
    return max(MIN_SEND_BUFFER, min(MAX_SEND_BUFFER, SEND_BUFFER_FRAMES * frame))


def catch_up_bytes(kbps: int, fps: int) -> int:
    """How much video may wait in asyncio's send buffer before frames are dropped: about
    CATCH_UP_MS of the bitrate, and never less than the send buffers."""
    return max(send_buffer(kbps, fps), kbps * 1000 // 8 * CATCH_UP_MS // 1000)


def lead_override(settings: Settings) -> int:
    """More options > Debug > TV wait: the lead to ask for in place of the one saved for the TV,
    within what a saved one may be; 0 for the saved one."""
    lead = settings.mirror_lead_ms
    if not isinstance(lead, int) or isinstance(lead, bool) or lead <= 0:
        return 0
    return max(profiles.MIN_LEAD_MS, min(profiles.MAX_LEAD_MS, lead))


def mirror_latency(settings: Settings) -> str:
    """Mirror screen settings > Latency: "low", "normal" or "smooth" (normal for anything else)."""
    level = settings.mirror_latency
    return level if isinstance(level, str) and level in MIRROR_LATENCIES else "normal"


def latency_lead(settings: Settings, saved_ms: int) -> tuple[int, str]:
    """The lead to ask the TV for, and why, in words for the log: More options > Debug > TV wait
    if it is set (lead_override), else the Latency's (Normal: `saved_ms`, the one saved for the TV)."""
    debug = lead_override(settings)
    if debug:
        return debug, "Debug TV wait"
    level = mirror_latency(settings)
    if MIRROR_LATENCIES[level]:
        return MIRROR_LATENCIES[level], f"Latency {level}"
    return saved_ms, f"Latency {level}, the TV's own"


def keyframes_on_demand(settings: Settings) -> bool:
    """Whether FFmpeg is started again for a keyframe once frames were dropped: with keyframes
    only when needed (More options > Debug > Keyframes, the default) and every 10 s. Every 2 s is
    as before: the next one ends a drop soon enough."""
    return ffmpeg.mirror_keyframe_seconds(settings.mirror_keyframes) != ffmpeg.MIRROR_KEYFRAME_SECONDS


def keyframes_text(encoder: str, settings: Settings) -> str:
    """The keyframes `encoder` makes with these settings, in words for the log."""
    seconds = ffmpeg.mirror_keyframe_interval(encoder, ffmpeg.mirror_keyframe_seconds(settings.mirror_keyframes))
    if not seconds:
        return "only when needed"
    return f"every {seconds} s" + (" and when needed" if keyframes_on_demand(settings) else "")


def on_arrival(settings: Settings) -> bool:
    """More options > Debug > Show frames on arrival: on only if settings.json says true (not
    "false", 1 or null from a hand edit)."""
    return settings.mirror_on_arrival is True


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
    keyframes: str = ""  # how often FFmpeg made one, in words (keyframes_text)
    trailer: str = ""
    data_channel: str = "not opened"  # "open", "refused (...)", "closed by the TV"
    frames_sent: int = 0
    codec_packets: int = 0
    data_closed_after: Optional[int] = None  # frames sent when the TV closed the video connection
    timing_port: int = 0  # our NTP responder's UDP port
    ntp_requests: int = 0
    ptp: str = ""  # what our PTP clock did, when it ran (ptp.PtpNode.totals())
    feedback: dict = field(default_factory=dict)
    latency: dict = field(default_factory=dict)
    first_frame_ms: Optional[float] = None  # from starting FFmpeg to the first frame sent
    restarts: list = field(default_factory=list)  # why FFmpeg was started again while the TV waited
    retried: str = ""  # why the session was set up a second time: the other avcC ending, or without sound
    # The PC's sound: "off: ..." (why not), "sent: ...", "held: ..." or "stopped: ..." at the end
    # (MirrorSound.final_text), and its figures (sound.SoundStats.as_dict()).
    sound: str = "not set up"
    sound_stats: dict = field(default_factory=dict)
    audio_latency: str = ""  # RECORD's Audio-Latency, if the TV said it
    feedback_body: Optional[dict] = None  # the last plist /feedback was answered with
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
        timing: Optional[str] = None,
        fps: Optional[int] = None,
        sound: Optional[bool] = None,
        sound_knobs: Optional[SoundKnobs] = None,
        on_notice: Optional[Callable[[Text], None]] = None,
    ) -> None:
        self.target = target
        self.settings = dataclasses.replace(settings)  # a copy: menu changes are for the next session
        self.on_status = on_status or (lambda s, m: None)
        self.source = source
        saved = profile or profiles.load(target.identifier)
        self.profile = saved or profiles.MirrorProfile()
        self.saved = saved is not None  # given or saved: what is asked for is known to work
        self.seconds = seconds
        self.remember = remember
        self.max_canvas = max_canvas
        self.timing = timing  # NTP or PTP for this run only (mirror --timing); None: MIRRORBRIDGE_TIMING, else saved
        self.fps_asked = fps  # for this run only (mirror --fps, MIRRORBRIDGE_FPS); None: Mirror screen settings
        self.state = State.IDLE
        self.report = SessionReport(trailer=self.profile.trailer)
        self.error: Optional[Exception] = None
        self.finished = threading.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._starting = False
        self._negotiator: Optional[Negotiator] = None
        self._responder: Optional[NtpResponder] = None
        self._ptp: Optional[ptp.PtpNode] = None  # our PTP clock, while a PTP session has it
        self._ptp_peer = ""  # the TV's address on it
        self._timeline = 0  # [40:48] of each frame
        self._transports: list[asyncio.BaseTransport] = []
        self._events: Optional[asyncio.Task] = None  # opening the event channel, then its transport
        self._data: Optional[DataChannel] = None
        self._send_buffer = MIN_SEND_BUFFER
        self._catch_up = MIN_SEND_BUFFER  # catch_up_bytes
        self._feedback: Optional[Feedback] = None
        self._ffmpeg: Optional[FFmpegSource] = None
        self._capture: tuple = ()  # FFmpeg, the display and the window's region, for each way below
        # The ways of capturing and encoding still to try: (capture backend, encoder, capture.GPU or CPU)
        self._sources: list[tuple[str, str, str]] = []
        self._using = ("", "", capture.CPU)  # the way FFmpeg runs now
        self._chain: Optional[capture.Chain] = None  # the desktop's way to the encoder now
        self._auto_fps = False  # Frame rate Automatic: lower for a slower way (_start_source)
        self._lead_why = ""  # where the lead came from (latency_lead)
        self._generation = 0  # FFmpegs this session started, for the log
        self._cipher: Optional[VideoCipher] = None
        self._frames: Optional[FrameQueue] = None
        self._stats = StageTimes()
        self._record_ns = 0
        self._stopping = False
        self._timing_probed = False
        self._timing_quiet = False
        self._dropped_at_once = False  # the TV closed the video at once (_at_once)
        self._first_sent_ns: Optional[int] = None  # when the first frame went to the TV
        self._retried = False
        self._xscreen: Optional[extended.ExtendedScreen] = None  # Extend screen's virtual monitor
        self.virtual_device = ""  # \\.\DISPLAYn of the virtual screen while there is one
        # The PC's sound (sound.py): True for this run (mirror --sound), False not at all (the
        # probe), None as the settings and MIRRORBRIDGE_MIRROR_SOUND say (_sound_wanted).
        self.sound_asked = sound
        self._sound_forced = sound is True
        self._knobs = sound_knobs or SoundKnobs.from_env()
        self.on_notice = on_notice or (lambda text: None)  # what the person should know of the sound, once
        self._sound: Optional[MirrorSound] = None
        self._audio_control: Optional[AudioControl] = None  # our audio control port, bound before SETUP
        self._volume: Optional[asyncio.Future] = None  # SET_PARAMETER volume (MIRRORBRIDGE_MIRROR_SOUND_VOLUME)
        self._speakers = MutedSpeakers()
        self._muted = False
        self._loopback: tuple[bool, str] = (False, "not checked")  # Windows has a sound output to record (_start)
        self._sound_on = False  # this attempt's _sound_wanted, and why not
        self._sound_why = ""
        self._sound_retried = False  # set up again without sound: the TV closed the session when it started
        self._sound_blamed = False  # and it did not drop the video without the sound too
        self._sound_first_ns: Optional[int] = None  # when the first sound packet went
        self._closed_ns = 0  # when the TV closed the session (its video or control connection)
        self._noticed: set[str] = set()
        self._reset_restarts()

    def _reset_restarts(self) -> None:
        """FFmpeg's restarts start afresh with each set-up (also the second, for the other avcC ending)."""
        self._accepting = 0  # the FFmpeg (generation) whose frames are taken; 0 while one is restarted
        self._gen_frames = 0  # frames from it
        self._gen_started_ns = 0
        self._worked: tuple = ()  # the last way (_using) that gave frames
        self._keyframe_wanted = ""  # why a keyframe is needed ("": none is)
        self._last_restart_ns = 0
        self._capture_retries = 0
        self._recovering_since = 0  # when the screen capture was lost, until a frame comes again (0: not lost)
        self._lost = ""  # what FFmpeg said when it was lost
        self._first_keyframe: Optional[tuple[int, str, int]] = None  # (generation, why, old one stopped) to log
        self._size_checked = 0  # the last generation whose picture size was checked (_wrong_size)

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
        switch, before = _env_switch_interval(), sys.getswitchinterval()
        if switch is not None:
            sys.setswitchinterval(switch)
        try:
            if self._stop.is_set():
                self._set(State.STOPPED, "Cancelled")
                return
            try:
                ended = await self._attempts()
            except MirrorError:
                # If the virtual screen went away (sleep), say that, on either attempt: the TV
                # not answering or FFmpeg ending afterwards came from it.
                self._check_screen()
                raise
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
                if switch is not None:
                    sys.setswitchinterval(before)
                self.finished.set()

    async def stop(self) -> None:
        """End the session. While it is still being set up, setting up is abandoned at once."""
        self._stop.set()
        if self._starting and self._task and not self._task.done():
            self._task.cancel()

    async def _attempts(self) -> str:
        """One session; once more without sound if the TV closed it when the sound started (and
        the sound was not asked for this run: _sound_dropped); and once more with the other avcC
        ending if the TV dropped the video at once on a first one. Each retry at most once."""
        while True:
            try:
                ended = await self._attempt()
            except MirrorError as ex:
                if self._sound_dropped():
                    await self._retry_without_sound()
                else:
                    other = self._other_trailer(ex)
                    if other is None:
                        raise
                    if self._sound_retried:  # it dropped the video without the sound as well
                        self._sound_blamed = False
                    await self._retry_with(other)
                if self._stop.is_set():
                    self._check_screen()
                    return "Stopped"
                continue
            if self._sound_dropped(ended=True):  # it ended the session itself: "The TV ended screen mirroring"
                await self._retry_without_sound()
                if self._stop.is_set():
                    self._check_screen()
                    return "Stopped"
                continue
            if self._sound_retried and self._closed_soon():
                # Ended as soon without the sound (the person ending it, say): not the sound's doing.
                _LOGGER.info("The TV ended the session without sound as well: the sound is not left out for it")
                self._sound_blamed = False
            return ended

    async def _attempt(self) -> str:
        self._starting = True
        try:
            await self._start()
        except BaseException:
            self._halt_sound()  # before run() says how it ended
            raise
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

    def _closed_after_sound(self) -> Optional[float]:
        """Seconds from the first sound packet to the TV closing the session, if that was within
        SOUND_DROP_SECONDS; None otherwise."""
        first, closed = self._sound_first_ns, self._closed_ns
        if not first or not closed or not 0 <= closed - first < sound.SOUND_DROP_SECONDS * NS:
            return None
        return (closed - first) / NS

    def _closed_soon(self) -> bool:
        """Whether the TV closed this session before its first frame or within SOUND_DROP_SECONDS
        of it."""
        first, closed = self._first_sent_ns, self._closed_ns
        return bool(closed) and (first is None or closed - first < sound.SOUND_DROP_SECONDS * NS)

    def _sound_dropped(self, ended: bool = False) -> bool:
        """Whether the TV closed the session when the sound started, and the session is to be set
        up again without sound: not when the sound was asked for this run (mirror --sound), nor
        when someone heard it on this TV, nor a second time. `ended`: the TV ended mirroring
        (both its connections closed, as its remote closes them); that counts only within
        SOUND_ENDED_SECONDS, since later a person ending it is the likelier, and no TV is to be
        set up again just after someone left it."""
        after = self._closed_after_sound()
        if after is None or self._stop.is_set():
            return False
        if self._sound_forced or self._sound_retried or self.profile.sound == profiles.SOUND_HEARD:
            _LOGGER.info("The TV closed the session %.1f s after the first sound packet", after)
            return False
        if ended and after >= sound.SOUND_ENDED_SECONDS:
            _LOGGER.info("The TV ended mirroring %.1f s after the first sound packet: taken for someone ending it, "
                         "not for the sound", after)
            return False
        return True

    async def _retry_without_sound(self) -> None:
        after = self._closed_after_sound() or 0.0
        _LOGGER.info("The TV closed the session %.1f s after the first sound packet: setting up again without sound",
                     after)
        text = Text("mirror_retry_no_sound", "The TV stopped when the sound started: going on without sound")
        self._set(State.STARTING, text)
        self._notice(text)
        await asyncio.shield(self._release())
        self._sound_retried = self._sound_blamed = True
        self.report = SessionReport(trailer=self.profile.trailer,
                                    retried="the TV closed the session when the sound started")

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        s = self.settings
        if self.target.needs_fairplay:
            raise needs_fairplay(self.target.name)
        self._stopping = self._timing_probed = self._timing_quiet = self._dropped_at_once = False
        self._first_sent_ns = self._sound_first_ns = None
        self._closed_ns = 0
        self._reset_restarts()
        self._set(State.STARTING, Text("checking_ffmpeg", "Checking FFmpeg and encoders"))
        ff = ffmpeg.find_ffmpeg(s.ffmpeg_path)
        encoder = await loop.run_in_executor(None, ffmpeg.pick_encoder, ff, s.encoder, True,
                                             ffmpeg.mirror_keyframe_seconds(s.mirror_keyframes))
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
        self._capture = (ff, monitor, region)
        self._using = (backend, encoder, capture.CPU)  # the first way, for the frame rate (_set_up)
        if self.source == DESKTOP and not self._knobs.tone and self._sound_wanted(output=False)[0]:
            # Whether Windows has a sound output to record: soundcard's first import and COM take
            # a while (2.5 s under Wine), so off the loop, before the TV waits for anything.
            self._loopback = await loop.run_in_executor(None, system_audio.loopback_available)
            found, what = self._loopback
            _LOGGER.info("Sound to record: %s", what if found else f"none ({what})")

        self._set(State.CONNECTING, self.target.name)
        try:
            await asyncio.wait_for(self._set_up(), START_SECONDS)
        except asyncio.TimeoutError as ex:
            _LOGGER.info("Setting up took more than %d s", START_SECONDS)
            raise refused(None, self.report.steps, "setup") from ex
        est = self.report.established
        assert est is not None and self.report.canvas is not None
        self._stats = StageTimes(kbps=s.video_bitrate_kbps, fps=self.report.fps, lead_ms=est.plan.lead_ms)
        if self._ptp is not None:
            self._ptp.start_window()  # its figures a second, as the stats line's, not counting setting up
        self._frames = FrameQueue(self.report.fps * QUEUE_SECONDS)
        # If the first choice dies before its first frame (driver refuses ddagrab, GPU encoder out
        # of sessions), _pump goes on with the processor's chain, GDI capture, then the CPU encoder.
        self._sources = [(backend, encoder, capture.CPU)]
        if self.source == DESKTOP:
            kind, why = await loop.run_in_executor(None, functools.partial(
                capture.choose_chain, ff, encoder=encoder, backend=backend, monitor=monitor, region=region,
                canvas=self.report.canvas, scaler=s.mirror_scaler))
            _LOGGER.info("Capture chain: %s (%s)", capture.KIND_WORDS[kind], why)
            if kind != capture.CPU:
                self._sources.insert(0, (backend, encoder, kind))
        if backend == "ddagrab":
            self._sources.append(("gdigrab", encoder, capture.CPU))
        if encoder != "libx264":
            self._sources.append((self._sources[-1][0], "libx264", capture.CPU))
        self._start_source("start")

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
            self.virtual_device = await self._xscreen.add(mode)  # out of What to cast now
            monitor = await self._xscreen.find()
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

    def _start_source(self, why: str) -> None:
        """FFmpeg with the next way of capturing and encoding (`why`, for the log)."""
        backend, encoder, kind = self._using = self._sources.pop(0)
        label = ffmpeg.ENCODER_LABELS.get(encoder, encoder)
        if self._auto_fps:  # Frame rate Automatic: 30 for x264 and gdigrab
            fps, fps_why = choose_mirror_fps(0, self.report.info, encoder=encoder, backend=backend)
            if fps < self.report.fps:
                self._use_fps(fps, fps_why)
        self.report.encoder = encoder
        if self._first_sent_ns is None:  # the TV is still waiting for its first frame
            way = "testsrc" if self.source == TEST_PATTERN else capture.via(backend, self._chain_for(backend, kind),
                                                                            encoder)
            self._set(State.STARTING, Text("encoder_via", "{encoder} via {backend}", encoder=label, backend=way))
        self._launch(why)

    def _chain_for(self, backend: str, kind: str) -> Optional[capture.Chain]:
        """The desktop's chain of that kind for the display as it is now (the processor's when its
        size is not known; gfxcapture repeating frames if this FFmpeg can); None for the test
        pattern."""
        if self.source != DESKTOP:
            return None
        ff, monitor, region = self._capture
        size_in = capture.capture_size(backend, monitor, region)
        if size_in is None:
            kind = capture.CPU
        return capture.chain_for(kind, size_in, self.report.canvas or FALLBACK_CANVAS,
                                 repeats=kind == capture.GFX and capture.gfx_repeats(ff))

    def _use_fps(self, fps: int, why: str) -> None:
        """Another frame rate for the FFmpegs from now on, and what depends on it."""
        kbps = self.settings.video_bitrate_kbps
        _LOGGER.info("Mirroring at %d fps from now on (%s)", fps, why)
        self.report.fps = fps
        self._stats.set_rate(kbps, fps)
        if self._frames is not None:
            self._frames.limit = max(2, fps * QUEUE_SECONDS)
        self._send_buffer = send_buffer(kbps, fps)
        self._catch_up = catch_up_bytes(kbps, fps)
        if self._data is not None:
            self._data.resize(self._send_buffer)

    def _launch(self, why: str) -> None:
        """Start FFmpeg the way _using says, its frames into the frame queue: a new generation,
        whose frames alone are taken from now on."""
        loop = asyncio.get_running_loop()
        s = self.settings
        ff, monitor, region = self._capture
        backend, encoder, kind = self._using
        canvas = self.report.canvas or FALLBACK_CANVAS
        size_in = capture.capture_size(backend, monitor, region) if self.source == DESKTOP else None
        chain = self._chain = self._chain_for(backend, kind)
        changes_only = chain is not None and chain.changes_only and backend == "ddagrab"
        gfx = chain is not None and chain.kind == capture.GFX and backend == "ddagrab"
        self._stats.variable_rate = changes_only  # gfxcapture without repeats: frames only as the screen changes
        self._stats.paced_by_screen = gfx  # gfxcapture: frames at the screen's refreshes, fewer at 200 Hz
        self.report.keyframes = keyframes_text(encoder, s)
        fps = self.report.fps
        cmd = build_command(ff, s, source=self.source, encoder=encoder, width=canvas[0], height=canvas[1], fps=fps,
                            backend=backend, monitor=monitor, region=region, chain=chain,
                            extra=s.mirror_encoder_extra,
                            keyframe_seconds=ffmpeg.mirror_keyframe_seconds(s.mirror_keyframes), bt709=True)
        self._generation += 1
        gen = self._accepting = self._generation
        self._gen_frames = 0
        frames, stats = self._frames, self._stats
        assert frames is not None
        # gfxcapture that repeats frames may begin with black ones (capture.gfx_black_start): their times
        first_times: Optional[list[int]] = [] if gfx and chain is not None and chain.repeats else None

        def take(unit: Optional[Unit]) -> None:
            if gen != self._accepting:  # from an FFmpeg stopped for a newer one
                return
            if unit is not None and unit.kind == FRAME:
                if not self._gen_frames:
                    self._worked = self._using
                    self._recovering_since = 0  # the capture is back
                self._gen_frames += 1
                stats.arrived()
                if first_times is not None and len(first_times) < capture.GFX_BLACK_FRAMES:
                    first_times.append(unit.pts_ms)
                    if len(first_times) == capture.GFX_BLACK_FRAMES:
                        self._black_start(gen, first_times, capture.gfx_rate(fps))
            frames.put(unit)

        def hand_over(unit: Optional[Unit]) -> None:  # from FFmpeg's pipe thread
            try:
                loop.call_soon_threadsafe(take, unit)
            except RuntimeError:  # the loop is closed: the session is long over
                pass

        est = self.report.established
        size = f"{size_in[0]}x{size_in[1]}" if size_in else "testsrc" if self.source == TEST_PATTERN else "?"
        made = chain.size_out if chain is not None and chain.size_out else canvas
        vbv_frames = capture.vbv_frames(chain)
        vbv = round(s.video_bitrate_kbps * vbv_frames / max(1, fps)) * 1000 // 8
        rate, gfx_said = f"VBV {vbv} bytes", ""
        if vbv_frames != ffmpeg.MIRROR_VBV_FRAMES:
            rate += f", {vbv_frames:g} frames"
        if chain is not None and chain.kind == capture.GFX and backend == "ddagrab":
            # what NVENC is told (source.build_command), and the frames it gets
            top = capture.gfx_rate(fps)
            rate += (f"; NVENC told {capture.gfx_kbps(s.video_bitrate_kbps, fps)} kbit/s at {top} "
                     "fps, the same share a frame")
            gfx_said = (f", gfxcapture's frames as the screen changes and repeats of the last while it does not, up "
                        f"to {top} a second, each as it comes" if chain.repeats else
                        f", frames only as the screen changes, without repeats (gfxcapture up to {top} a second)")
        _LOGGER.info("FFmpeg %d (%s): %s, %s -> %s%dx%d, %s at %s%d fps, %d kbit/s (%s)%s, keyframes %s; "
                     "lead %d ms (%s)",
                     gen, why, capture.chain_label(chain, backend), size,
                     "" if chain is not None and chain.size_out else "within ", *made,
                     ffmpeg.ENCODER_LABELS.get(encoder, encoder), "up to " if changes_only else "", fps,
                     s.video_bitrate_kbps, rate, gfx_said, self.report.keyframes, est.plan.lead_ms if est else 0,
                     self._lead_why)
        _LOGGER.info("FFmpeg %d: %s", gen, subprocess.list2cmdline(cmd))
        self._ffmpeg = FFmpegSource(cmd, hand_over)
        self._gen_started_ns = now_ns()
        self._ffmpeg.start()

    @staticmethod
    def _black_start(gen: int, times_ms: list[int], rate: int) -> None:
        """Say so in the log when an FFmpeg's gfxcapture most likely began with black frames: the
        TV then begins with a black keyframe, and the picture comes after it as an ordinary frame
        (blurred at first, as every frame that changes the whole screen is). Not yet seen on real
        hardware: this is how to tell."""
        blacks = capture.gfx_black_start(times_ms, rate)
        if blacks:
            _LOGGER.info("FFmpeg %d: its first %s most likely black (frames at %s ms: the picture %d ms after, "
                         "sooner than gfxcapture repeats a frame), as gfxcapture sends while Windows gives no "
                         "picture yet: the TV began with a black keyframe", gen,
                         "frame was" if blacks == 1 else f"{blacks} frames were",
                         ", ".join(str(t) for t in times_ms[:blacks + 1]), times_ms[blacks] - times_ms[blacks - 1])

    async def _next_source(self) -> None:
        """FFmpeg ended before its first frame: the next way of capturing and encoding, on the
        session as it is (the TV is waiting for a frame)."""
        failed = self._ffmpeg
        assert failed is not None and self._frames is not None
        await asyncio.get_running_loop().run_in_executor(None, self._stop_ffmpeg, failed)
        backend, encoder, kind = self._using
        said = failed.errors(STDERR_LINES)
        if kind in (capture.GPU, capture.GFX):
            ff, monitor, _region = self._capture
            lost = capture.capture_lost(said, kind=kind, returncode=failed.returncode)
            if lost:  # Windows kept the screen from it (a UAC prompt, the lock screen): not the chain's fault
                _LOGGER.warning("%s with %s on the graphics card could not capture %s (%s), trying the processor's "
                                "chain. FFmpeg said:\n%s", capture.capture_name(backend, kind), encoder,
                                capture.display_key(monitor), lost, said)
            else:
                capture.mark_broken(ff, monitor, kind)
                _LOGGER.warning("%s with %s on the graphics card failed on %s (not tried there again until "
                                "MirrorBridge quits), trying the processor's chain. FFmpeg said:\n%s",
                                capture.capture_name(backend, kind), encoder, capture.display_key(monitor), said)
        else:
            _LOGGER.warning("%s with %s failed, trying the next option. FFmpeg said:\n%s", backend, encoder, said)
        self._frames.clear()
        self._stats.restarted()
        self._start_source(f"after {backend} with {encoder} failed")

    async def _restart(self, why: str, *, refresh: bool = False, next_source: bool = False) -> None:
        """Stop FFmpeg and start it again (`why`, for the log), the TV's session going on: for a
        keyframe (the first frame of an FFmpeg is one), after the screen capture was lost (with
        the display looked up again: refresh), or the next way (next_source). The old FFmpeg is
        gone before the new one starts, so that the two never hold the encoder or the screen
        capture at once, and none of its frames are sent."""
        loop = asyncio.get_running_loop()
        assert self._frames is not None
        old = self._ffmpeg
        self._accepting = 0
        self._keyframe_wanted = ""  # the new FFmpeg's first frame is a keyframe
        self._frames.clear()
        stopped = now_ns()
        if old is not None:
            await loop.run_in_executor(None, self._kill_ffmpeg, old)
        if refresh:
            await self._refresh_capture()
            await self._rechoose_chain()
        if self._stopping:
            return
        self._last_restart_ns = now_ns()
        self._stats.restarted()
        self.report.restarts.append(why)
        self._first_keyframe = (self._generation + 1, why, stopped)
        if next_source and self._sources:
            self._start_source(why)
        else:
            self._launch(why)

    async def _refresh_capture(self) -> None:
        """The display again, after the screen capture was lost: it may have another size now, or
        another output (the virtual screen's is looked up as Extend screen found it). Kept as it
        was if it is not found."""
        loop = asyncio.get_running_loop()
        ff, monitor, region = self._capture
        if self.source != DESKTOP or monitor is None:
            return
        found, window = monitor, region
        try:
            if self._xscreen is not None:
                found = await self._xscreen.find()
            else:
                monitors = await loop.run_in_executor(None, ffmpeg.list_monitors)
                found = next((m for m in monitors if (m.device.lower() == monitor.device.lower() if monitor.device
                                                      else m.index == monitor.index)), monitor)
                if region is not None and self.settings.window_title:
                    found, window = ffmpeg.window_region(self.settings.window_title, monitors)
        except (vdisplay.VddError, RuntimeError, OSError) as ex:
            _LOGGER.info("Capturing the display as it was: %s", ex)
            return
        if (found, window) != (monitor, region):
            _LOGGER.info("The display is now %s (%s)%s", found.label, found.device or found.index,
                         f", the window at {window}" if window != region else "")
        self._capture = (ff, found, window)

    async def _rechoose_chain(self) -> None:
        """The graphics card chain at the display's own size (GPU) for a display that came back
        at a size that has to be scaled now: the chain is chosen again as at the start
        (capture.choose_chain: gfxcapture where Scaling and this FFmpeg let it, else the
        processor's, never scale_d3d11 while it is off), the ways after it staying as they
        were."""
        backend, encoder, kind = self._using
        ff, monitor, region = self._capture
        canvas = self.report.canvas or FALLBACK_CANVAS
        if kind != capture.GPU or not capture.needs_scaling(capture.capture_size(backend, monitor, region), canvas):
            return
        chosen, why = await asyncio.get_running_loop().run_in_executor(None, functools.partial(
            capture.choose_chain, ff, encoder=encoder, backend=backend, monitor=monitor, region=region,
            canvas=canvas, scaler=self.settings.mirror_scaler))
        if chosen == kind:
            return
        _LOGGER.info("Capture chain from now on: %s (%s)", capture.KIND_WORDS[chosen], why)
        self._using = (backend, encoder, chosen)
        if self._sources and self._sources[0] == self._using:
            self._sources.pop(0)  # the processor's chain is the way now, not the next one

    def _want_keyframe(self, why: str) -> None:
        """A keyframe is needed (`why`, for the log): FFmpeg is started again for one (_pump)."""
        if not self._keyframe_wanted and not self._stopping:
            self._keyframe_wanted = why

    def _keyframe_due_in(self) -> Optional[float]:
        """Seconds until FFmpeg may be started again for the keyframe asked for (0: now), at most
        once every KEYFRAME_RESTART_SECONDS; None if none is asked for."""
        if not self._keyframe_wanted or self._stopping:
            return None
        return max(0.0, KEYFRAME_RESTART_SECONDS - (now_ns() - self._last_restart_ns) / NS)

    async def _set_up(self) -> None:
        """Connect and pair-verify, GET /info, SETUP, the video connection, RECORD, /feedback."""
        s = self.settings
        negotiator = self._negotiator = Negotiator(
            self.target, timing_port=0, audio_port=0, steps=self.report.steps)
        negotiator.on_reply = self._on_setup_reply
        negotiator.on_restart = self._close_events  # the event channel was the old session's
        channel = await negotiator.connect()
        # The TV's timing requests and its audio control port (the sound's syncs, its resend
        # requests), on ports a firewall rule names.
        transport, self._responder = await bind_udp(NtpResponder, channel.local_ip)
        self._transports.append(transport)
        negotiator.timing_port = self.report.timing_port = self._responder.port
        transport, self._audio_control = await bind_udp(AudioControl, channel.local_ip,
                                                        taken=(negotiator.timing_port,))
        self._transports.append(transport)
        negotiator.audio_port = self._audio_control.port
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
        plan.lead_ms, self._lead_why = latency_lead(s, self.profile.lead_ms)
        if "MIRRORBRIDGE_LEAD_MS" in os.environ:
            plan.lead_ms, self._lead_why = negotiation.lead_from_env(plan.lead_ms), "MIRRORBRIDGE_LEAD_MS"
        plan.audio = negotiation.audio_from_env(plan.audio)
        plan.timing = self.timing or negotiation.timing_from_env(plan.timing)
        shown = "on arrival" if on_arrival(s) or ON_ARRIVAL else "at their time"
        _LOGGER.info("Mirroring with lead %d ms (%s), keyframes %s, frames shown %s", plan.lead_ms, self._lead_why,
                     keyframes_text(self.report.encoder, s), shown)
        self._sound_on, self._sound_why = self._sound_wanted()
        if not self._sound_on and self.source == DESKTOP and self._sound_wanted(output=False)[0]:
            self._notice(Text("mirror_sound_no_output", "No sound: Windows has no sound output to record. Turn on "
                              "speakers or headphones in Sound settings."))
        negotiator.audio = self._audio_offer(plan, self._sound_on)
        # The video's own rule (each frame at its time) is the sound's too: "on arrival" breaks it.
        _LOGGER.info("Sound knobs: %s; frames shown %s", self._knobs.describe(), shown)
        _LOGGER.info("Experiment knobs: /feedback every %g s (MIRRORBRIDGE_FEEDBACK_S), time requests %s "
                     "(MIRRORBRIDGE_NTP_STOP_S), heartbeat %s (MIRRORBRIDGE_HEARTBEAT_IDLE_ONLY), audio stream %s "
                     "(MIRRORBRIDGE_NO_AUDIO), frame rate %s (mirror --fps, MIRRORBRIDGE_FPS), pattern noise %s "
                     "(MIRRORBRIDGE_PATTERN_NOISE), sender %s with AirPlay %s (MIRRORBRIDGE_SENDER, "
                     "MIRRORBRIDGE_SOURCE_VERSION), H.264 profile %s (MIRRORBRIDGE_H264_PROFILE), thread switch "
                     "interval %g ms (MIRRORBRIDGE_SWITCH_INTERVAL_MS), "
                     "%s timing (mirror --timing, MIRRORBRIDGE_TIMING)%s, sound %s (MIRRORBRIDGE_MIRROR_SOUND, mirror "
                     "--sound)",
                     negotiation.FEEDBACK_INTERVAL,
                     f"answered for {_env_float('MIRRORBRIDGE_NTP_STOP_S', 0):g} s only"
                     if "MIRRORBRIDGE_NTP_STOP_S" in os.environ else "always answered",
                     "only when idle" if HEARTBEAT_IDLE_ONLY else "every second",
                     "set up" if plan.audio else "left out",
                     f"{self.fps_asked} asked" if self.fps_asked else "as Mirror screen settings say",
                     os.environ.get("MIRRORBRIDGE_PATTERN_NOISE") or "off", negotiation.SENDER_MODEL,
                     negotiation.SOURCE_VERSION, ffmpeg.MIRROR_PROFILE, sys.getswitchinterval() * 1000,
                     plan.timing.upper(), self._ptp_knobs(plan), "on" if self._sound_on else f"off: {self._sound_why}")
        if self.report.info is not None and "PTPInfo" in self.report.info.raw:
            _LOGGER.info("The TV's GET /info has PTPInfo: %r", self.report.info.raw["PTPInfo"])
        elif plan.timing == PTP:  # a TV without it may have no PTP clock at all
            _LOGGER.info("The TV's GET /info has no PTPInfo" if self.report.info is not None
                         else "The TV did not answer GET /info: whether it has PTPInfo is not known")
        if plan.timing == PTP:
            plan.timing = self._start_ptp(channel.local_ip)
        negotiator.ptp_clock_id = int(self._ptp.identity) if self._ptp is not None else None
        _LOGGER.info("Setting up mirroring to %s (%s): %s", self.target.name,
                     "saved settings" if self.saved else "first time", plan.describe())
        est = self.report.established = await negotiator.establish(plan)
        if est.plan.timing == PTP:
            est = self.report.established = await self._follow_us(negotiator, est)
        if est.plan.timing != PTP:
            await self._stop_ptp()  # the TV gave a ClockID of its own, or did not follow ours
        self._timeline = est.timeline_id if est.plan.timing == PTP else 0
        if self._timeline and os.environ.get("MIRRORBRIDGE_PTP_TIMELINE") == "0":
            _LOGGER.info("Frames go on timeline 0, not our PTP clock's (MIRRORBRIDGE_PTP_TIMELINE=0)")
            self._timeline = 0
        for note in est.fallbacks:
            _LOGGER.info("Fallback: %s", note)
        channel = negotiator.channel
        assert channel is not None
        if info_answered:
            await self._info_after_setup(est, channel)
        limit = canvas_limit(s, self.max_canvas)
        self.report.canvas = choose_canvas(self.report.info, self.target, limit)
        backend, encoder, _kind = self._using
        if self.fps_asked:  # mirror --fps: in place of Mirror screen settings > Frame rate
            self.report.fps = choose_fps(self.fps_asked, self.report.info)
            fps_why = "mirror --fps or MIRRORBRIDGE_FPS" + (
                f", the TV takes {self.report.fps} at most" if self.report.fps < self.fps_asked else "")
            self._auto_fps = False
        else:
            self.report.fps, fps_why = choose_mirror_fps(s.mirror_framerate, self.report.info, encoder=encoder,
                                                         backend=backend)
            self._auto_fps = s.mirror_framerate not in (SLOW_FPS, FAST_FPS)
        _LOGGER.info("Mirroring into %dx%d at %d fps (%s)", *self.report.canvas, self.report.fps, fps_why)

        self._send_buffer = send_buffer(s.video_bitrate_kbps, self.report.fps)
        self._catch_up = catch_up_bytes(s.video_bitrate_kbps, self.report.fps)
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
        self.report.audio_latency = next((str(value) for key, value in est.record_headers.items()
                                          if key.lower() == "audio-latency"), "")
        self._feedback = Feedback(channel)
        self._feedback.start()
        if self._sound_on and est.audio is not None and self._knobs.volume_db is not None:
            # After the first /feedback, and never waited for: a TV that does not answer it must
            # not hold the picture. Its answer is logged; it is never fatal.
            self._volume = asyncio.ensure_future(negotiator.set_volume(est, self._knobs.volume_db))
        await self._start_sound(est, channel)

    @staticmethod
    def _ptp_knobs(plan: negotiation.Plan) -> str:
        if plan.timing != PTP:
            return ""
        return (f": the TV must follow our clock within {ptp.lock_seconds_from_env():g} s (MIRRORBRIDGE_PTP_LOCK_S) "
                f"{'or the session is kept anyway' if os.environ.get('MIRRORBRIDGE_PTP_NO_FALLBACK') == '1' else 'or NTP'}"
                f" (MIRRORBRIDGE_PTP_NO_FALLBACK), Announce every {ptp.announce_ms_from_env()} ms "
                f"(MIRRORBRIDGE_PTP_ANNOUNCE_MS), Signaling {ptp.signaling_from_env()} (MIRRORBRIDGE_PTP_SIGNALING), "
                f"frames on timeline {'0' if os.environ.get('MIRRORBRIDGE_PTP_TIMELINE') == '0' else 'our ClockID'} "
                f"(MIRRORBRIDGE_PTP_TIMELINE)")

    def _start_ptp(self, local_ip: str) -> str:
        """Our PTP clock on UDP 319 and 320, for a PTP session: PTP, or NTP if they cannot be had."""
        try:
            self._ptp = ptp.PtpNode.acquire(local_ip)
        except ptp.PtpUnavailable as ex:
            _LOGGER.warning("PTP timing not possible: %s: NTP timing", ex.explain())
            self.report.steps["ptp"] = ex.kind
            return NTP
        return PTP

    async def _follow_us(self, negotiator: Negotiator, est: Established) -> Established:
        """The TV took a PTP session: send it our clock, name the two of us in SETPEERS, and wait
        for it to follow; otherwise the session again, over NTP."""
        node, channel = self._ptp, negotiator.channel
        assert node is not None and channel is not None
        tv_ip = channel.remote_ip
        if est.ptp_addresses and tv_ip not in est.ptp_addresses:
            _LOGGER.info("PTP: the TV names %s, not %s, in its timingPeerInfo; sending to %s", est.ptp_addresses,
                         tv_ip, tv_ip)
        node.add_peer(tv_ip)
        self._ptp_peer = tv_ip
        answer = await negotiator.set_peers(est, [tv_ip, channel.local_ip])
        if answer != 200:
            why = f"SETPEERS answered {answer}"
        else:
            why = await node.wait_ready(tv_ip, ptp.lock_seconds_from_env())
        if not why:
            return est
        if os.environ.get("MIRRORBRIDGE_PTP_NO_FALLBACK") == "1":
            _LOGGER.warning("PTP: %s. Keeping PTP timing anyway (MIRRORBRIDGE_PTP_NO_FALLBACK=1): the TV may show "
                            "nothing, or a picture that freezes", why)
            return est
        await self._stop_ptp()
        return await negotiator.ptp_fallback(est, why)  # its own event channel too (on_restart)

    async def _stop_ptp(self) -> None:
        """The TV off our PTP clock, and our use of it ended (the last one stops it)."""
        node, self._ptp = self._ptp, None
        if node is None:
            return
        self.report.ptp = node.totals()
        if self._ptp_peer:
            node.remove_peer(self._ptp_peer)
            self._ptp_peer = ""
        await asyncio.get_running_loop().run_in_executor(None, node.release)

    def _close_events(self) -> None:
        events, self._events = self._events, None
        if events is not None:
            if not events.done():
                events.cancel()
            elif not events.cancelled() and events.exception() is None and events.result() is not None:
                events.result().close()

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

    # -- the sound ---------------------------------------------------------

    def _sound_wanted(self, output: bool = True) -> tuple[bool, str]:
        """Whether this session sends the PC's sound, and why not: for now only when asked for
        (sound.SOUND_BY_DEFAULT), never for the probe, never with Include computer sound off,
        and not once the TV closed the session when it started (_retry_without_sound).
        `output`: also that Windows has a sound output to record (_loopback, from _start)."""
        knobs = self._knobs
        if self.sound_asked is False:
            return False, "off for this session"
        if not self.settings.capture_audio:
            return False, "Include computer sound is off"
        if knobs.enable is False:
            return False, f"{sound.ENV}=0"
        forced = self.sound_asked is True  # mirror --sound, --beep, --sound-trial
        if not (forced or knobs.enable or sound.SOUND_BY_DEFAULT):
            return False, f"not turned on yet (mirror --sound, or {sound.ENV}=1)"
        if not forced and self.profile.sound == profiles.SOUND_OFF:
            return False, (f"this TV closed the session when the sound started "
                           f"({self.profile.sound_note or 'as saved for it'})")
        if self._sound_retried:  # and its type-96 stream as it always was
            return False, "going on without sound: the TV closed the session when it started"
        if output and self.source == DESKTOP and not knobs.tone and not self._loopback[0]:
            return False, self._loopback[1]
        return True, ""

    def _audio_offer(self, plan: negotiation.Plan, wanted: bool) -> AudioOffer:
        """What the type-96 stream asks for: as it always was when the sound is not wanted (every
        knob left out); else the format the TV's screenStream calls for, and the gap of the lead
        and the delay, with the knobs' latencies."""
        if not wanted:
            return AudioOffer()
        knobs = self._knobs
        info = self.report.info
        fmt, why = choose_format(info.screen_formats if info is not None else None, knobs.codec)
        _LOGGER.info("Sound format: %s", why)
        self.report.steps["sound"] = why
        gap_ms = knobs.gap_ms if knobs.gap_ms is not None else max(sound.MIN_GAP_MS, plan.lead_ms + knobs.delay_ms)
        return AudioOffer(fmt, knobs.shape, ms_to_samples(gap_ms),
                          None if knobs.lat_min_ms is None else ms_to_samples(knobs.lat_min_ms),
                          None if knobs.lat_max_ms is None else ms_to_samples(knobs.lat_max_ms),
                          knobs.is_media, knobs.redundant, knobs.ports)

    async def _start_sound(self, est: Established, channel: ControlChannel) -> None:
        """The PC's sound to the TV, if it is wanted and the TV took the type-96 stream with its
        ports; otherwise why not, in the report. Never stops the session."""
        off, refused = self._sound_why, False
        if not off:
            if est.audio is None:  # no type-96 stream, or the TV did not name its ports for it
                off, refused = est.audio_note or "no audio stream in this TV's plan", est.plan.audio
                if not est.plan.audio and not negotiation.audio_from_env(True):
                    off += " (MIRRORBRIDGE_NO_AUDIO=1)"
            elif est.plan.timing == PTP:
                off = "no sound with PTP timing yet"
        if off:
            self.report.sound = self.report.steps["sound"] = f"off: {off}"
            _LOGGER.info("Sound: %s", off)
            if refused:
                self._notice(Text("mirror_sound_not_taken", "This TV does not take the PC's sound with Mirror "
                                  "screen; for a video with sound, use Play a video file."))
            return
        assert est.audio is not None and self._audio_control is not None
        # Kept before anything is awaited: a stop meanwhile must find it to stop it. The test
        # pattern has the test tone for its sound, as mirror --beep the screen.
        self._sound = MirrorSound(est.audio, knobs=self._knobs, control=self._audio_control,
                                  local_ip=channel.local_ip, tv_ip=channel.remote_ip,
                                  tone=self._knobs.tone or self.source != DESKTOP,
                                  on_first_packet=self._sound_started, on_failed=self._sound_failed,
                                  on_device_changed=self._device_changed)
        self.report.sound = f"on: {est.audio.format.name}"
        try:
            await self._sound.start()
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("The sound did not start", exc_info=True)
            self._sound.fail(f"it did not start ({ex!r})")

    def _sound_started(self) -> None:
        """The first sound packet went: when (_sound_dropped), and the PC muted if the TV is known
        to play the sound (or the knob says so), so that it does not echo."""
        self._sound_first_ns = now_ns()
        first = self._first_sent_ns
        _LOGGER.info("Sound: first packet %s", f"{(self._sound_first_ns - first) / 1e6:.0f} ms after the first frame"
                     if first is not None else "before the first frame")
        if (self.source == DESKTOP and not self._knobs.tone and self.settings.mute_pc_audio
                and (self.profile.sound == profiles.SOUND_HEARD or self._knobs.mute)):
            self._mute()

    def _mute(self) -> None:
        if self._speakers.mute():
            self._muted = True
        elif getattr(self._speakers, "error", ""):  # not when they were muted already
            self._notice(Text("mirror_sound_not_muted", "Could not mute this PC: turn its volume down to avoid an "
                              "echo."))

    def _device_changed(self) -> None:
        """The sound comes from another output device now: that one is muted instead."""
        if self._muted:
            self._restore_speakers()
            self._mute()

    def _restore_speakers(self) -> None:
        self._muted = False
        try:
            self._speakers.restore()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Could not give the speakers back")

    def _halt_sound(self) -> None:
        """Stop the sound at once and give the speakers back (synchronous, and as often as asked):
        before the session says it stopped, and when it is released."""
        if self._sound is not None:
            try:
                self._sound.stop()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Stopping the sound failed")
        self._restore_speakers()

    def _sound_failed(self, reason: str) -> None:
        """The sound stopped for `reason` (MirrorSound.fail); the picture goes on. Said to the
        person only while the session itself goes on: when the TV ends it, the sound's errors
        are only its end."""
        self._restore_speakers()
        channel = self._negotiator.channel if self._negotiator is not None else None
        ending = (self._stopping or self._feedback is None or self._feedback.lost.is_set()
                  or (self._data is not None and self._data.closed.is_set()) or channel is None or channel.closed)
        if ending:
            self.report.sound = self.report.steps["sound"] = f"stopped: the session ended ({reason})"
            _LOGGER.debug("The sound stopped as the session ended (%s)", reason)
            return
        _LOGGER.warning("The sound stopped (%s); the picture goes on", reason)
        self.report.sound = self.report.steps["sound"] = f"stopped: {reason}"
        self._notice(Text("mirror_sound_stopped", "The sound stopped; the picture goes on."))

    def _check_sound(self, now: int, log: bool) -> None:
        """The supervisor's look at the sound (and its log line every LOG_SECONDS): whatever it
        finds stops the sound, not the session."""
        if self._sound is None:
            return
        try:
            self._sound.check(now)
            if log:
                self._sound.log_window(now)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Checking the sound failed")

    def _notice(self, text: Text) -> None:
        """Tell the person (on_notice), once a session for each kind of notice."""
        if text.key in self._noticed:
            return
        self._noticed.add(text.key)
        _LOGGER.info("Notice: %s", text)
        try:
            self.on_notice(text)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Notice callback failed")

    def _sound_verdict(self) -> tuple[str, str]:
        """What mirroring.json keeps of this TV and the sound (MirrorProfile.sound and its note):
        "off" once it closed the session when the sound started, and kept it without (not when
        it closed that one as soon: _attempts); nothing known again once a session asked for the
        sound sent it for SOUND_OK_SECONDS without that; else as it was."""
        if self._sound_retried and self._sound_blamed:
            return profiles.SOUND_OFF, f"{time.strftime('%Y-%m-%d')}: closed the session when the sound started"
        if (self.profile.sound == profiles.SOUND_OFF and self._sound_forced
                and self.report.sound_stats.get("packets", 0) >= sound.SOUND_OK_SECONDS * (SR // SPF)):
            _LOGGER.info("The TV took the sound for %d s: it is no longer left out", sound.SOUND_OK_SECONDS)
            return profiles.SOUND_UNKNOWN, ""
        return self.profile.sound, self.profile.sound_note

    def _log_sound(self) -> None:
        """The sound's line at the end, after the session's."""
        report = self.report
        stats = report.sound_stats
        setup = report.established.audio if report.established is not None else None
        if not stats or setup is None:
            _LOGGER.info("Mirroring sound: %s", report.sound)
            return

        def ms(field: str) -> str:
            return f"{stats[field] * 1000 / SR:.0f} ms"

        headroom = stats["headroom_min_ms"]
        _LOGGER.info("Mirroring sound: %s; %d packets (%.1f MB), %s, gap %d, latencies %d/%d; resent %d of %d "
                     "asked (%d gone, %d over the limit); filled %s, kept %s, cut %s; corrections +%d/-%d; base steps "
                     "%d; re-anchored %d; held %d blocks; overflows %d; headroom min %s ms; Audio-Latency %s; last "
                     "/feedback %s", report.sound,
                     stats["packets"], stats["bytes"] / 1e6, setup.format.name, setup.gap, setup.lat_min, setup.lat_max,
                     stats["resent"], stats["asked"], stats["missed"], stats["limited"], ms("filled"), ms("kept"),
                     ms("cut"),
                     stats["inserted"], stats["deleted"], stats["steps"], stats["reanchors"], stats["held"],
                     stats["overflow"], f"{headroom:.0f}" if headroom is not None else "-",
                     report.audio_latency or "none", report.feedback_body or "none")

    async def _probe_loop_lag(self) -> None:
        """Experiment: how late a 10 ms sleep wakes on the session's loop, which also answers the
        TV's timing requests (so a late loop stamps them late)."""
        while True:
            asked = now_ns()
            await asyncio.sleep(0.010)
            self._stats.loop_lag(max(0, now_ns() - asked - 10_000_000))

    async def _supervise(self) -> str:
        assert self._data is not None and self._feedback is not None and self._responder is not None
        est = self.report.established
        assert est is not None
        pump = asyncio.ensure_future(self._pump())
        lag_probe = asyncio.ensure_future(self._probe_loop_lag())
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
                        self._closed_ns = now
                        return await self._video_closed(str(ex))
                    return "Stopped"
                if waits["closed"] in done:
                    self._closed_ns = now
                    return await self._video_closed(self._data.close_reason)
                if waits["lost"] in done:
                    self._closed_ns = now
                    return Text("mirror_tv_ended", "The TV ended screen mirroring")
                if self.seconds is not None and now - started >= self.seconds * NS:
                    self._stopping = True
                    return Text("mirror_time_up", "Mirrored for {seconds} s, as asked", seconds=f"{self.seconds:g}")
                if est.plan.timing == NTP:
                    self._check_timing(now)
                self._check_frames(now)
                due = self._stats.due(now)
                if due:
                    self._stats.log_window(now, self._responder.requests, self._ptp.summary() if self._ptp else "")
                self._check_sound(now, due)
        finally:
            self._halt_sound()  # before run() says the session stopped
            pump.cancel()
            lag_probe.cancel()
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
        """FFmpeg must give a first frame, and then keep giving them; gfxcapture's without repeats
        gives them only as the screen changes, so once it gave one, a gap is no fault (its end is
        _ended's). A screen capture that was lost must come back within CAPTURE_LOST_SECONDS (the
        FFmpegs started meanwhile give none)."""
        assert self._frames is not None and self._ffmpeg is not None
        if self._recovering_since:
            if now - self._recovering_since > CAPTURE_LOST_SECONDS * NS:
                _LOGGER.error("The screen capture did not come back within %g s", CAPTURE_LOST_SECONDS)
                raise self._capture_lost()
            return
        last = self._frames.last_frame_ns
        if last is None and now - self._ffmpeg.started_ns > FIRST_FRAME_SECONDS * NS:
            raise self._no_picture(FIRST_FRAME_SECONDS)
        if last is not None and now - last > FRAME_GAP_SECONDS * NS:
            # A screen that stays as it is; if FFmpeg ended meanwhile, its end goes to _ended,
            # which says why (a lost capture, the virtual screen gone), not this.
            if self._stats.variable_rate and self._gen_frames:
                return
            raise self._no_picture(FRAME_GAP_SECONDS)

    def _no_picture(self, seconds: int) -> MirrorError:
        assert self._ffmpeg is not None
        errors = self._ffmpeg.errors()
        _LOGGER.error("No picture from FFmpeg %d for %d s. FFmpeg said:\n%s", self._accepting, seconds,
                      self._ffmpeg.errors(STDERR_LINES))
        self.report.steps["picture"] = f"none for {seconds} s"
        return MirrorError(Text(
            "mirror_no_picture", "No picture came from the screen capture or the encoder ({encoder}) for {seconds} s."
            "{errors}", encoder=ffmpeg.ENCODER_LABELS.get(self.report.encoder, self.report.encoder), seconds=seconds,
            errors=f"\n{errors}" if errors else "",
        ), step="encoder", steps=self.report.steps)

    def _capture_lost(self) -> MirrorError:
        reason = self._lost or "no picture"
        self.report.steps["picture"] = f"capture lost ({reason})"
        return MirrorError(Text(
            "mirror_capture_lost", "Windows stopped the screen capture and it did not come back ({reason}). Choose "
            "Show my screen again.", reason=reason,
        ), step="capture", steps=self.report.steps)

    async def _video_closed(self, reason: str) -> str:
        """The TV closed the video connection. If it closes the control connection as well, as
        when someone ends mirroring on the TV, the session is over; if it did so at once
        (_at_once), or kept the control connection, it did not take the video."""
        assert self._feedback is not None
        at_once = self._at_once(now_ns())
        channel = self._negotiator.channel if self._negotiator is not None else None
        def control_closed() -> bool:
            return channel is None or channel.closed or self._feedback.lost.is_set()

        waited = 0.0
        while not control_closed() and waited < TV_ENDED_SECONDS:
            await asyncio.sleep(0.05)
            waited += 0.05
        if control_closed() and not at_once:
            self._stopping = True
            self.report.data_channel = "closed by the TV, with the control connection"
            return Text("mirror_tv_ended", "The TV ended screen mirroring")
        raise self._data_closed(reason, at_once)

    def _at_once(self, closed_ns: int) -> bool:
        """Whether the TV closed the video before it could have shown much of it: before the
        first frame went, or within AT_ONCE_SECONDS of it. Measured in time, not in keyframes,
        so that the Debug menu's keyframe interval does not move it."""
        first = self._first_sent_ns
        return first is None or closed_ns - first < AT_ONCE_SECONDS * NS

    def _data_closed(self, reason: str = "", at_once: bool = False) -> MirrorError:
        assert self._data is not None
        self._dropped_at_once = at_once
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

    def _drop_reason(self, idr: bool, waiting: bool, late_ns: int, behind_ns: int, fresh: bool) -> str:
        """Why the frame at hand is not to be sent ("" to send it). `late_ns` is how far past its
        lead it is now; `behind_ns` how long the frames have been past theirs (since the first of
        those sent since one was on time, or this one's own `late_ns`); `fresh` whether FFmpeg
        gave it after frames were last dropped.

        Dropping a frame costs the frames after it up to the next keyframe (the first of FFmpeg
        started again, _restart), a frozen picture until then, so a frame goes unless it cannot
        help: a newer keyframe is already waiting, or the network has not caught up within the
        CATCH_UP_MS the TV is given (more than about CATCH_UP_MS of the video still waiting to go,
        or frames past their lead for longer than that). A short hold-up of the network leaves
        frames waiting, a little late: they still go, and the TV catches up. One that stays slower
        than the video does not keep the TV behind for good: a drop ends on a keyframe that is on
        time, not on one of the frames that waited, already late with more behind it, or on the
        first keyframe FFmpeg gives once the drop began (so that it never waits for one long)."""
        assert self._frames is not None and self._data is not None
        frames = self._frames
        if idr and frames.keyframe_waiting():
            return "a newer keyframe is waiting"
        if waiting:
            if not idr:
                return "waiting for a keyframe"
            if late_ns > 0 and frames.frames and not fresh:
                return f"the keyframe is {late_ns // 1_000_000} ms past its lead, with frames behind it"
            return ""
        if self._data.buffered > self._catch_up:
            return f"{self._data.buffered // 1024} KB are still waiting for the network"
        if late_ns > 0 and behind_ns > CATCH_UP_MS * 1_000_000:
            return (f"frames have been past their lead for {behind_ns // 1_000_000} ms, more than the "
                    f"{CATCH_UP_MS} ms the TV is given to catch up")
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
        """FFmpeg's frames to the TV, as they come. A frame the network held up past its lead
        goes late, with its own timestamp (the TV catches up), unless _drop_reason says it
        cannot help. Frames dropped ask for a keyframe (keyframes_on_demand): FFmpeg is started
        again for one, and the frames that follow are those of the new FFmpeg."""
        assert self._data is not None and self._cipher is not None and self._ffmpeg is not None
        assert self._frames is not None
        est = self.report.established
        assert est is not None
        frames = self._frames
        lead_ns = est.plan.lead_ms * 1_000_000
        shown_on_arrival = on_arrival(self.settings) or ON_ARRIVAL
        on_demand = keyframes_on_demand(self.settings)
        mapper = PtsMapper(window_ns=round(_env_float("MIRRORBRIDGE_PTS_WINDOW_S", 2.0) * NS),
                           slew_ns=_env_slew_ns())
        _LOGGER.info("Experiment knobs: frame times from a %g s window, %s", mapper.window_ns / NS,
                     f"moving at most {mapper.slew_ns / 1000:g} us a frame" if mapper.slew_ns is not None
                     else "jumping to its smallest offset")
        last_offset: Optional[int] = None
        trailer = self.profile.trailer
        params: Optional[tuple[bytes, bytes]] = None
        avcc = b""
        picture = (0, 0)
        length_size = 4
        send_codec = True
        wait_for_idr = True
        slow_logged = ridden_logged = 0
        late_frames = held_ns = 0  # frames sent past their lead since the last on time, and their sends
        behind_since = 0  # when the first frame past its lead since the last on time was due (0: none is)
        dropped_at = 0  # when frames were last dropped: what FFmpeg gives after that is fresh

        def restarted() -> None:
            """Another FFmpeg: its pts start from 0 again (its offset is no step of the
            capture's), and it starts with a keyframe."""
            nonlocal wait_for_idr, behind_since, late_frames, held_ns, dropped_at, last_offset
            mapper.restart()
            last_offset = None
            wait_for_idr = True
            behind_since = late_frames = held_ns = 0
            dropped_at = now_ns()

        while True:
            # A keyframe asked for is not left waiting for a frame: gfxcapture without repeats
            # gives none while the screen stays as it is. Nor is it got for an FFmpeg that has
            # ended: that end goes to _ended first (a lost capture, the next way), with the frames
            # before it.
            due_in = self._keyframe_due_in()
            if due_in == 0 and not frames.ended() and self._ffmpeg.running():
                _LOGGER.info("Starting FFmpeg again for a keyframe: %s", self._keyframe_wanted)
                await self._restart(f"a keyframe: {self._keyframe_wanted}")
                restarted()
                continue
            if not due_in:
                unit = await frames.get()
            else:
                try:
                    unit = await asyncio.wait_for(frames.get(), due_in)  # FrameQueue.get takes nothing if cancelled
                except asyncio.TimeoutError:
                    continue  # no frame came meanwhile: the keyframe is due now
            if unit is None:
                if self._stopping or not await self._ended():
                    return
                restarted()
                continue
            taken = now_ns()
            trimmed = frames.take_trimmed()
            if trimmed:
                if not wait_for_idr:
                    _LOGGER.info("The TV took no video for a while: %d frames were dropped, and frames up to the "
                                 "next keyframe will be", trimmed)
                wait_for_idr = True
                dropped_at = taken
                behind_since = late_frames = held_ns = 0
                self._stats.drop(trimmed)
                if on_demand and not frames.keyframe_waiting():
                    self._want_keyframe(f"the TV took no video for a while ({trimmed} frames dropped)")
            if unit.kind == CONFIG:
                config = parse_avcc(unit.data)
                length_size = config.length_size
                wrong = self._wrong_size(config.sps[0]) if config.sps else ""
                if wrong:
                    await self._restart(f"the graphics card's picture: {wrong}", next_source=True)
                    restarted()
                    continue
                if config.sps and config.pps and (config.sps[0], config.pps[0]) != params:
                    params = (config.sps[0], config.pps[0])
                    send_codec = True
                continue
            if not unit.data:
                continue
            nals, sps, pps = frame_nals(split_avcc(unit.data, length_size))
            wrong = self._wrong_size(sps[0]) if sps else ""
            if wrong:
                await self._restart(f"the graphics card's picture: {wrong}", next_source=True)
                restarted()
                continue
            if sps and pps and (sps[0], pps[0]) != params:
                params = (sps[0], pps[0])  # new parameter sets in the stream: a new codec packet
                send_codec = True
            if not nals or params is None:
                continue
            idr = is_idr(nals)
            taken_at = mapper.map(unit.pts_ms, unit.arrived_ns)
            used = mapper._used  # pylint: disable=protected-access
            if last_offset is not None and used is not None and abs(used - last_offset) > 5_000_000:
                _LOGGER.info("Frame times stepped %+.1f ms (the capture's offset changed)",
                             (used - last_offset) / 1e6)
            last_offset = used
            due = taken_at + lead_ns  # when the TV is to show it
            why = self._drop_reason(idr, wait_for_idr or send_codec, taken - due,
                                    taken - (behind_since or due), unit.arrived_ns > dropped_at)
            if why:
                if not wait_for_idr:
                    _LOGGER.info("Dropping frames up to the next keyframe: %s", why)
                    dropped_at = taken
                # A drop that begins asks for a keyframe, unless one is waiting; so does a keyframe
                # dropped, one that waited too long (with a keyframe every 10 s, the next is far off).
                if on_demand and (idr or not wait_for_idr) and not frames.keyframe_waiting():
                    self._want_keyframe(why)
                wait_for_idr = True
                behind_since = late_frames = held_ns = 0
                self._stats.drop()
                continue
            wait_for_idr = False
            behind_since = (behind_since or due) if taken > due else 0
            timestamp = to_ntp(due)
            packets = []
            if send_codec:
                avcc = avcc_record(params[0], params[1], trailer)
                info = parse_sps(params[0])
                picture = (info.width, info.height)
                if self.report.picture != picture:
                    _LOGGER.info("Sending %dx%d H.264 (profile %d, level %.1f), avcC %s", info.width, info.height,
                                 info.profile_idc, info.level_idc / 10, avcc.hex())
                self.report.picture = picture
                packets.append(codec_packet(avcc, timestamp, *picture, on_arrival=shown_on_arrival))
                self.report.codec_packets += 1
                send_codec = False
            payload = join_avcc(nals)
            packets.append(self._cipher.video_packet(payload, timestamp, idr, timeline=self._timeline))
            packed = now_ns()
            data = b"".join(packets)
            try:
                self._data.write(data)
                await self._drained()
            except ConnectionError as ex:
                raise _VideoClosed(f"closed by the TV ({ex})") from ex
            sent = now_ns()
            if idr:
                self._keyframe_sent(sent)
            if sent - packed > lead_ns and sent - slow_logged > LOG_SECONDS * NS:
                slow_logged = sent
                _LOGGER.warning("The network took %.0f ms to take a %d KB frame, more than the lead (%d ms)",
                                (sent - packed) / 1e6, len(data) // 1024, est.plan.lead_ms)
            late = sent - taken_at > lead_ns
            if late:
                late_frames += 1
                held_ns += sent - packed
            elif late_frames:  # on time again: the TV has caught up
                # Only when the network made them late (not FFmpeg), and once per stall at most.
                if held_ns > lead_ns and sent - ridden_logged > LOG_SECONDS * NS:
                    ridden_logged = sent
                    _LOGGER.info("The network held up for %.0f ms: sent %d frames late instead of dropping them",
                                 held_ns / 1e6, late_frames)
                late_frames = held_ns = 0
            self._stats.add(len(data), idr, buffered=self._data.buffered, late=late,
                            pipe=max(0, unit.arrived_ns - taken_at),
                            behind=mapper.behind(unit.pts_ms, unit.arrived_ns), queue=taken - unit.arrived_ns,
                            pack=packed - taken, send=sent - packed, age=sent - taken_at)
            self.report.frames_sent = self._stats.frames
            if self._stats.frames == 1:
                self._first_sent_ns = sent
                if self._sound is not None:
                    self._sound.go()  # the sound starts with the picture: the next block anchors its line
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

    def _keyframe_sent(self, sent: int) -> None:
        """A keyframe went to the TV: none is needed any more, and the first of an FFmpeg started
        again says how long the picture stood still."""
        self._keyframe_wanted = ""
        first = self._first_keyframe
        if first is not None and first[0] == self._accepting:
            self._first_keyframe = None
            _LOGGER.info("FFmpeg %d (%s): its first keyframe went %.0f ms after the one before was stopped",
                         first[0], first[1], (sent - first[2]) / 1e6)

    def _wrong_size(self, sps: bytes) -> str:
        """What is wrong with the size of the picture the graphics card chain makes, from the
        first SPS of each of its FFmpegs ("" if nothing, or if it is not that chain): a size the
        encoder was not asked for, or one bigger than the TV's screen, as from a rotated display.
        That chain is not used on the display again in this run."""
        chain = self._chain
        if chain is None or chain.kind not in (capture.GPU, capture.GFX) or self._size_checked == self._accepting:
            return ""
        self._size_checked = self._accepting
        made = capture.sps_size(sps)
        if made is None:
            return ""
        problem = capture.size_problem(made, chain, self.report.canvas or FALLBACK_CANVAS)
        if problem and not self._sources:  # nothing to go on with (cannot be: the processor's chain is next)
            _LOGGER.warning("The graphics card chain: %s", problem)
            return ""
        if problem:
            ff, monitor, _region = self._capture
            capture.mark_broken(ff, monitor, chain.kind)
            _LOGGER.warning("The graphics card chain on %s: %s. The processor's chain takes over, and is used for "
                            "that display from now on", capture.display_key(monitor), problem)
        return problem

    async def _ended(self) -> bool:
        """FFmpeg ended (its pipe did) while the session went on: True if another FFmpeg runs in
        its place now, False if the session is stopping; else raises why the session ends.

        A screen capture that was lost (capture.capture_lost, once it had worked) is started again
        after each of CAPTURE_RETRY_DELAYS, and while it is, a ddagrab that ends before its first
        frame counts as one more try whatever it said (a display that is still changing), never as
        a reason to go on with GDI capture. An FFmpeg that ends before its first frame otherwise
        gives way to the next way of capturing and encoding (_next_source)."""
        loop = asyncio.get_running_loop()
        self._check_screen()
        failed = self._ffmpeg
        assert failed is not None
        await loop.run_in_executor(None, self._kill_ffmpeg, failed)  # reaped, and all of its stderr read
        if self._stopping:
            return False
        said = failed.errors(STDERR_LINES)
        backend, _encoder, kind = self._using
        count, now = self._gen_frames, now_ns()
        ran = (now - failed.started_ns) / NS
        lost = capture.capture_lost(said, kind=kind, returncode=failed.returncode)
        recovering = bool(self._recovering_since)
        if (lost and (count or recovering or self._using == self._worked)) or (
                recovering and not count and backend == "ddagrab"):
            if count and now - self._gen_started_ns > CAPTURE_STABLE_SECONDS * NS:
                self._capture_retries = 0  # it had come back for a while: this is another loss
            if not recovering:  # what it said first is what the session says if the capture does not come back
                self._recovering_since, self._lost = now, lost or ""
            tries = len(CAPTURE_RETRY_DELAYS)
            if self._capture_retries >= tries:
                _LOGGER.error("FFmpeg %d stopped after %d frames (%.1f s, exit code %s): the screen capture did not "
                              "come back after %d tries. FFmpeg said:\n%s", self._accepting, count, ran,
                              failed.returncode, tries, said)
                raise self._capture_lost()
            delay = CAPTURE_RETRY_DELAYS[self._capture_retries]
            self._capture_retries += 1
            _LOGGER.warning("FFmpeg %d stopped after %d frames (%.1f s, exit code %s): %s. Starting it again in "
                            "%.2f s (try %d of %d). FFmpeg said:\n%s", self._accepting, count, ran, failed.returncode,
                            f"the screen capture was lost ({lost})" if lost else "the screen capture is not back yet",
                            delay, self._capture_retries, tries, said)
            await asyncio.sleep(delay)
            if self._stopping:
                return False
            self._check_screen()
            await self._restart(f"the screen capture lost, try {self._capture_retries}", refresh=True)
            return True
        if not count and self._sources:  # this way gives nothing: the next one
            await self._next_source()
            return True
        if self._xscreen is not None and await self._xscreen.lost_within():
            raise vdisplay.VddError("lost")  # ddagrab lost its output before the driver said so
        _LOGGER.error("FFmpeg %d stopped after %d frames (%.1f s, exit code %s). FFmpeg said:\n%s", self._accepting,
                      count, ran, failed.returncode, said)
        raise MirrorError(ended_text(failed), step="encoder")

    async def _release(self) -> None:
        """Stop what the session started and tear it down on the TV. Each part is taken before
        it is stopped, so that this can run again (a retry, then the end) without doing twice."""
        try:
            await self._release_session()
        finally:
            await self._stop_ptp()  # however the rest went: no thread or port left behind

    async def _release_session(self) -> None:
        loop = asyncio.get_running_loop()
        self._stopping = True
        self._halt_sound()  # again, for a session that failed while it was set up
        sound_, self._sound = self._sound, None
        volume, self._volume = self._volume, None
        if volume is not None:
            volume.cancel()  # if still unanswered: TEARDOWN is not to wait behind it
            try:
                await volume
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
        feedback, self._feedback = self._feedback, None
        if feedback is not None:
            await feedback.stop()
            self.report.feedback = dict(feedback.answers)
            self.report.feedback_body = feedback.last_body
        negotiator, self._negotiator = self._negotiator, None
        est = self.report.established
        # TEARDOWN while FFmpeg stops, so that the TV leaves mirroring at once.
        jobs = []
        if self._ffmpeg is not None:
            jobs.append(loop.run_in_executor(None, self._stop_ffmpeg, self._ffmpeg))
        if negotiator is not None and est is not None:
            jobs.append(negotiator.teardown(est.uri))
        if sound_ is not None and sound_.feeder is not None and sound_.feeder.is_alive():
            jobs.append(loop.run_in_executor(None, sound_.feeder.join, 1.0))  # told to end by _halt_sound
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        if sound_ is not None:
            self.report.sound_stats = sound_.stats.as_dict()
            if sound_.state != FAILED:  # a failure is as _sound_failed said it
                self.report.sound = sound_.final_text()
            self.report.steps["sound"] = self.report.sound
        data, self._data = self._data, None
        if data is not None:
            await data.close()
        self._close_events()
        if negotiator is not None:
            negotiator.close()
        responder, self._responder = self._responder, None
        if responder is not None:
            self.report.ntp_requests = responder.requests
            self._stats.log_last(responder.requests, self._ptp.summary() if self._ptp else "")
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        self._audio_control = None
        self.report.latency = self._stats.summary()
        self.report.frames_sent = self._stats.frames

    @staticmethod
    def _stop_ffmpeg(source: FFmpegSource) -> None:
        try:
            source.stop()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Stopping FFmpeg failed", exc_info=True)

    @staticmethod
    def _kill_ffmpeg(source: FFmpegSource) -> None:
        """End an FFmpeg at once (another starts in its place), or reap one that ended."""
        try:
            source.stop(kill=True)
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
        restarts = self.report.restarts
        _LOGGER.info("Mirroring ended: %d frames sent (%d late), %d dropped; FFmpeg started again %d times%s; the TV "
                     "asked for the time %d times; /feedback answers %s; steps %s%s", self._stats.frames,
                     self._stats.late, self._stats.dropped, len(restarts),
                     f" ({'; '.join(restarts[:10])}{'...' if len(restarts) > 10 else ''})" if restarts else "",
                     self.report.ntp_requests, self.report.feedback or "none", self.report.steps,
                     f"; PTP: {self.report.ptp}" if self.report.ptp else "")
        self._log_sound()
        est = self.report.established
        # Remembered only once the TV has taken frames and the time: what was asked for works.
        # Not a PTP session (an experiment), nor a profile with PTP in it.
        if (self.remember and est is not None and self.error is None and self._stats.frames
                and self.report.ntp_requests and self.target.identifier and est.plan.timing == NTP):
            # With the TV's own lead: a test value from the Debug menu or MIRRORBRIDGE_LEAD_MS is never saved.
            plan = dataclasses.replace(est.plan, lead_ms=self.profile.lead_ms)
            profile = profiles.MirrorProfile.of(plan, self.profile.trailer)
            if negotiation.audio_from_env(True) is False:  # nor a plan without audio (MIRRORBRIDGE_NO_AUDIO)
                profile.audio = self.profile.audio
            profile.sound, profile.sound_note = self._sound_verdict()
            info = self.report.info.summary() if self.report.info else ""
            try:
                await loop.run_in_executor(None, lambda: profiles.save(
                    self.target.identifier, profile, name=self.target.name, info=info))
            except OSError as ex:
                _LOGGER.warning("Could not save how to mirror to %s: %s", self.target.name, ex)
