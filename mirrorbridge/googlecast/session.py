"""One Google Cast screen mirroring session: the desktop (or a test pattern) on a Chromecast or a
TV with Chromecast built-in, as Chrome mirrors a tab, a fraction of a second behind.

    pick the encoder -> TLS to port 8009 -> LAUNCH the mirroring receiver (APP_ID)
    -> connect to it -> OFFER (VP8 or H.264, Opus, the AES key, the delay) -> ANSWER (the
    UDP port) -> FFmpeg -> each frame encrypted, in RTP packets -> lost packets again -> STOP

It speaks to the tray and the command line as the other sessions do (session.CastSession,
mirror.MirrorSession): run() until the session ends, stop() from anywhere on the loop, and
on_status(State, message) on the way, the messages and errors being Texts.

The receiver shows each frame `delay_ms` after it was taken (the OFFER's targetDelay), and
asks again for packets it misses until then. When it falls further behind than that by
CATCH_UP_MS (a network that holds the video up for a moment), frames are dropped up to the
next keyframe, which it can show without the ones before it, so it is back to real time.
FFmpeg is started as for AirPlay mirroring (mirror.source), with the same fallbacks when it
ends before its first frame: GDI capture, then the CPU encoder. With Extend screen on, the
virtual screen is what is sent, as for AirPlay mirroring.

The sound (googlecast.audio) is a second stream on the same socket, from a second FFmpeg:
the PC's audio, or a beep with the test pattern. A PC without system audio capture, or a
receiver that takes no audio, gets the picture alone, and so does a session whose sound
FFmpeg ends: the picture goes on. While the TV gets the PC's sound, the PC's own speakers
are muted (settings.mute_pc_audio, speakers.MutedSpeakers), and put back at the end.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import threading
from typing import Optional

from .. import audio as system_audio
from .. import extended, ffmpeg, vdisplay
from ..speakers import MutedSpeakers
from ..config import Settings
from ..mirror.clock import NS, now_ns
from ..mirror.flv import CONFIG, FRAME
from ..mirror.h264 import frame_nals, is_idr, parse_avcc, parse_sps, split_avcc, to_annexb
from ..mirror.session import FALLBACK_CANVAS, MAX_CANVAS, canvas_limit, extend_mode
from ..mirror.source import DESKTOP, TEST_PATTERN, FFmpegSource, FrameQueue, Unit, build_command, ended_text
from ..session import State, StatusCallback
from ..text import Text
from .channel import NS_RECEIVER, NS_WEBRTC, CastChannel, ChannelError
from .discovery import CastDevice
from .audio import AudioTimer, OpusSource, choose_lead, has_opus, opus_command, opus_samples
from .offer import AUDIO_BPS, AUDIO_PAYLOAD_TYPE, VIDEO_PAYLOAD_TYPE, AnswerError, VideoOffer, parse_answer
from .rtp import AUDIO_CLOCK, VIDEO_CLOCK, FrameCipher, Packetizer
from .sender import CastSocket, Clock, StreamSender
from .vp8 import ENCODER as VP8_ENCODER
from .vp8 import IvfSource, vp8_command

_LOGGER = logging.getLogger(__name__)

APP_ID = "0F5096E8"  # Chrome's audio and video mirroring receiver
DEFAULT_DELAY_MS = 400  # as Chrome asks; the TCL closed the connection after 2 s at 200
MIN_DELAY_MS, MAX_DELAY_MS = 50, 2000
CATCH_UP_MS = 500  # how far past its delay the receiver may fall before frames are dropped
QUEUE_SECONDS = 2
LAUNCH_SECONDS = 20
LAUNCH_POLLS = 5  # GET_STATUS after a LAUNCH answered without the app
ANSWER_SECONDS = 10
FIRST_FRAME_SECONDS = 15
FRAME_GAP_SECONDS = 10
RTCP_SECONDS = 10  # from the first frame (or the last RTCP) until the receiver must have said something
STATS_SECONDS = 5
SUPERVISE_SECONDS = 0.25
STOP_SECONDS = 3
AUDIO_QUEUE = 100  # Opus frames (a second) waiting to be sent, at most
DELAY_ENV = "MIRRORBRIDGE_CAST_DELAY_MS"  # a test value for the delay, from a .bat
CODEC_ENV = "MIRRORBRIDGE_CAST_CODEC"  # h264 or vp8, from a .bat
AUDIO_DELAY_ENV = "MIRRORBRIDGE_CAST_AUDIO_DELAY_MS"  # a test value for the sound's own delay
# The sound's own targetDelay. The TCL shows the picture at once whatever its delay, but plays
# the sound at its delay plus about 0.4 s of its own: 100 ms brings it nearest the picture.
AUDIO_DELAY_MS = 100
H264, VP8 = "h264", "vp8"
CODECS = (H264, VP8)


class CastMirrorError(Exception):
    """A Text for the person, as the other sessions' errors are."""



def choose_delay(wanted: Optional[int]) -> int:
    """The delay to ask for: `wanted`, the test value in DELAY_ENV, or DEFAULT_DELAY_MS."""
    if not wanted:
        try:
            wanted = int(os.environ.get(DELAY_ENV, "") or 0)
        except ValueError:
            wanted = 0
    return max(MIN_DELAY_MS, min(MAX_DELAY_MS, wanted or DEFAULT_DELAY_MS))


def choose_codec(wanted: Optional[str]) -> str:
    """The codec to send: `wanted`, the test value in CODEC_ENV, or VP8: the TCL's receiver takes
    H.264, says it decodes and shows every frame, and leaves the screen black."""
    wanted = (wanted or os.environ.get(CODEC_ENV, "") or VP8).strip().lower()
    return wanted if wanted in CODECS else VP8


def find_app(status: dict) -> Optional[dict]:
    """The mirroring receiver in a RECEIVER_STATUS, if it runs."""
    apps = ((status.get("status") or {}).get("applications")) or []
    return next((app for app in apps if isinstance(app, dict) and app.get("appId") == APP_ID), None)


class GoogleCastMirror:
    def __init__(
        self,
        device: CastDevice,
        settings: Settings,
        on_status: Optional[StatusCallback] = None,
        *,
        source: str = DESKTOP,
        seconds: Optional[float] = None,
        delay_ms: Optional[int] = None,
        codec: Optional[str] = None,
        audio: Optional[bool] = None,
        max_canvas: tuple[int, int] = MAX_CANVAS,
    ) -> None:
        self.device = device
        self.settings = settings
        self.on_status = on_status or (lambda s, m: None)
        self.source = source
        self.seconds = seconds
        self.delay_ms = choose_delay(delay_ms)
        self.codec = choose_codec(codec)
        self.max_canvas = max_canvas
        self.audio = settings.capture_audio if audio is None else audio  # wanted
        self.sound = False  # being sent
        self.state = State.IDLE
        self.error: Optional[Exception] = None
        self.finished = threading.Event()
        self.canvas: tuple[int, int] = FALLBACK_CANVAS
        self.fps = 30
        self.encoder = ""
        self.answer = None
        self.picture: Optional[tuple[int, int]] = None  # what the encoder really made (its SPS)
        self.virtual_device = ""  # \\.\DISPLAYn of the virtual screen while there is one
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._starting = False
        self._channel: Optional[CastChannel] = None
        self._app: Optional[dict] = None
        self._app_gone = False
        self._sender: Optional[StreamSender] = None
        self._audio_sender: Optional[StreamSender] = None
        self._socket: Optional[CastSocket] = None
        self._audio_bps = AUDIO_BPS
        self._audio_ffmpeg: Optional[OpusSource] = None
        self._feeder: Optional[system_audio.LoopbackFeeder] = None
        self._audio_task: Optional[asyncio.Task] = None
        self._timer: Optional[AudioTimer] = None
        self._speakers = MutedSpeakers()
        self._ffmpeg: Optional[FFmpegSource] = None
        self._frames: Optional[FrameQueue] = None
        self._capture: tuple = ()
        self._sources: list[tuple[str, str]] = []
        self._using = ("", "")
        self._sps: list[bytes] = []
        self._pps: list[bytes] = []
        self._length_size = 4
        self._first_sent_ns: Optional[int] = None
        self._dropped = 0
        self._shown = 0
        self._reported = False
        self._xscreen: Optional[extended.ExtendedScreen] = None

    @property
    def frames_shown(self) -> int:
        """Frames the receiver's own event log says it showed: with VP8, the proof the picture is
        on the TV (frames sent, even acknowledged, can still leave it black). With H.264 the TCL
        reports every frame shown on a black screen."""
        sender = self._sender
        return sender.stats.played if sender is not None else self._shown

    @property
    def frames_reported(self) -> bool:
        """Whether the receiver sends an event log at all: without one, frames_shown stays 0
        even with the picture on the TV."""
        sender = self._sender
        return sender.stats.logs > 0 if sender is not None else self._reported

    def _set(self, state: State, message: str = "") -> None:
        self.state = state
        _LOGGER.info("%s%s", state.value, f": {message}" if message else "")
        try:
            self.on_status(state, message)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Status callback failed")

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """Mirror until stop(), the time limit, the receiver ending it, or an error (raised)."""
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
            if self._xscreen is not None and self._xscreen.lost:
                ex = vdisplay.VddError("lost")
            _LOGGER.debug("Cast mirroring failed", exc_info=True)
            self.error = ex
            self._set(State.FAILED, str(ex))
            raise ex
        finally:
            try:
                await asyncio.shield(self._cleanup())
            finally:
                self.finished.set()

    async def stop(self) -> None:
        self._stop.set()
        if self._starting and self._task and not self._task.done():
            self._task.cancel()

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        s = self.settings
        self._set(State.STARTING, Text("checking_ffmpeg", "Checking FFmpeg and encoders"))
        ff = ffmpeg.find_ffmpeg(s.ffmpeg_path)
        if self.codec == VP8:
            encoder = VP8_ENCODER
        else:
            encoder = await loop.run_in_executor(None, ffmpeg.pick_encoder, ff, s.encoder, True)
        backend, monitor, region = TEST_PATTERN, None, None
        if self.source == DESKTOP:
            if extended.wanted(s):
                monitor = await self._virtual_screen()
                s = self.settings
            else:
                monitors = ffmpeg.list_monitors()
                monitor = monitors[max(0, min(s.monitor_index, len(monitors) - 1))] if monitors else None
                if s.window_title and s.capture_backend != "test":
                    monitor, region = ffmpeg.window_region(s.window_title, monitors)
            backend = await loop.run_in_executor(None, ffmpeg.pick_capture_backend, ff, s, monitor, region)
        if self.audio:
            self.audio = await loop.run_in_executor(None, self._audio_possible, ff)

        self._set(State.CONNECTING, self.device.name)
        await self._set_up()
        self._frames = FrameQueue(self.fps * QUEUE_SECONDS)
        self._capture = (ff, monitor, region)
        self._sources = [(backend, encoder)]
        if backend == "ddagrab":
            self._sources.append(("gdigrab", encoder))
        if encoder not in ("libx264", VP8_ENCODER):
            self._sources.append((self._sources[-1][0], "libx264"))
        self._start_source()
        if self._audio_sender is not None:
            self._start_audio()

    def _audio_delay(self) -> int:
        try:
            wanted = int(os.environ.get(AUDIO_DELAY_ENV, "") or 0)
        except ValueError:
            wanted = 0
        return max(MIN_DELAY_MS, min(MAX_DELAY_MS, self.delay_ms, wanted or AUDIO_DELAY_MS))

    def _audio_possible(self, ff: str) -> bool:
        if not has_opus(ff):
            _LOGGER.warning("This FFmpeg has no Opus encoder; mirroring without sound")
            return False
        if self.source == DESKTOP:
            ok, info = system_audio.loopback_available()
            if not ok:
                _LOGGER.warning("%s - mirroring without sound", info)
                return False
        return True

    async def _virtual_screen(self) -> ffmpeg.Monitor:
        self._set(State.EXTENDING)
        self._xscreen = extended.ExtendedScreen(on_lost=self._stop.set)
        self.virtual_device = await self._xscreen.add(extend_mode(self.settings.extend_mode))
        monitor = await self._xscreen.find()
        self.settings = extended.screen_settings(self.settings)
        _LOGGER.info("Mirroring virtual screen %s: %dx%d", monitor.device, monitor.width, monitor.height)
        return monitor

    async def _set_up(self) -> None:
        """TLS, LAUNCH, OFFER and ANSWER, and the UDP socket the video goes out of."""
        s = self.settings
        channel = self._channel = CastChannel(self.device.host, self.device.port)
        channel.on_message = self._on_message
        try:
            await channel.open()
        except ChannelError as ex:
            raise CastMirrorError(Text("gc_no_connection", "Could not connect to {device} over Google Cast "
                                       "({error}).", device=self.device.name, error=str(ex))) from ex
        try:
            status = await channel.request(NS_RECEIVER, {"type": "LAUNCH", "appId": APP_ID}, timeout=LAUNCH_SECONDS)
            app = find_app(status) if status.get("type") == "RECEIVER_STATUS" else None
            for _ in range(LAUNCH_POLLS):
                if app is not None or status.get("type") == "LAUNCH_ERROR":
                    break
                # Some receivers answer with the status of before, and start the app a moment later.
                await asyncio.sleep(1)
                status = await channel.request(NS_RECEIVER, {"type": "GET_STATUS"}, timeout=5)
                app = find_app(status)
        except ChannelError as ex:
            raise CastMirrorError(Text("gc_no_launch", "{device} did not start Cast screen mirroring ({error}).",
                                       device=self.device.name, error=str(ex))) from ex
        if app is None:
            reason = status.get("reason") or status.get("type") or "no answer"
            raise CastMirrorError(Text("gc_no_launch", "{device} did not start Cast screen mirroring ({error}).",
                                       device=self.device.name, error=str(reason)))
        self._app = app
        _LOGGER.info("Mirroring receiver started on %s: session %s, transport %s", self.device.name,
                     app.get("sessionId"), app.get("transportId"))
        channel.connect(app["transportId"])

        width, height = canvas_limit(s, self.max_canvas)
        offer = VideoOffer(width=width, height=height, fps=max(1, min(60, s.framerate or 30)),
                           kbps=s.video_bitrate_kbps, delay_ms=self.delay_ms, codec=self.codec, audio=self.audio,
                           audio_delay_ms=self._audio_delay())
        try:
            reply = await channel.request(NS_WEBRTC, offer.message(), app["transportId"], timeout=ANSWER_SECONDS,
                                          key="seqNum")
            answer = self.answer = parse_answer(reply)
        except (ChannelError, AnswerError) as ex:
            raise CastMirrorError(Text("gc_no_answer", "{device} did not take the video ({error}).",
                                       device=self.device.name, error=str(ex))) from ex
        _LOGGER.info("ANSWER: UDP port %d, receiver SSRC %d, limits %s", answer.udp_port, answer.receiver_ssrc,
                     answer.raw.get("constraints"))
        if answer.max_width and answer.max_height:
            scale = min(1.0, answer.max_width / width, answer.max_height / height)
            width, height = int(width * scale) // 2 * 2, int(height * scale) // 2 * 2
        self.canvas = (width, height)
        self.fps = max(1, min(offer.fps, int(answer.max_fps or offer.fps)))
        if answer.max_delay_ms and answer.max_delay_ms < self.delay_ms:
            self.delay_ms = answer.max_delay_ms
        if answer.max_kbps and answer.max_kbps < s.video_bitrate_kbps:
            _LOGGER.info("The receiver takes at most %d kbps", answer.max_kbps)
            self.settings = s = dataclasses.replace(s, video_bitrate_kbps=max(300, answer.max_kbps))
        _LOGGER.info("Sending %s %dx%d at %d fps, %d kbps, shown %d ms after capture", self.codec, width, height,
                     self.fps, s.video_bitrate_kbps, self.delay_ms)

        clock, cipher = Clock(), FrameCipher(offer.key, offer.iv_mask)
        sender = StreamSender(cipher, Packetizer(offer.ssrc, VIDEO_PAYLOAD_TYPE), answer.receiver_ssrc, clock,
                              VIDEO_CLOCK)
        streams = [sender]
        if self.audio and answer.audio_receiver_ssrc is None:
            _LOGGER.info("%s took no sound; mirroring the picture alone", self.device.name)
        elif self.audio:
            self._audio_bps = min(AUDIO_BPS, answer.audio_max_bps or AUDIO_BPS)
            self._audio_sender = StreamSender(cipher, Packetizer(offer.audio_ssrc, AUDIO_PAYLOAD_TYPE),
                                              answer.audio_receiver_ssrc, clock, AUDIO_CLOCK)
            streams.append(self._audio_sender)
            _LOGGER.info("Sending Opus at %d kbps, shown %d ms after capture, taken as recorded %d ms before it "
                         "comes", self._audio_bps // 1000, offer.audio_delay_ms or self.delay_ms, choose_lead(None))
        sock = CastSocket(streams)
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: sock, remote_addr=(self.device.host, answer.udp_port))
        self._socket, self._sender = sock, sender

    def _on_message(self, namespace: str, source: str, payload: dict) -> None:
        kind = payload.get("type")
        if namespace == NS_RECEIVER and kind == "RECEIVER_STATUS" and self._app is not None:
            app = find_app(payload)
            if app is None or app.get("sessionId") != self._app.get("sessionId"):
                _LOGGER.info("The mirroring receiver on %s is no longer running", self.device.name)
                self._app_gone = True
        elif kind == "CLOSE" and self._app is not None and source == self._app.get("transportId"):
            _LOGGER.info("The mirroring receiver closed the connection")
            self._app_gone = True
        elif namespace == NS_WEBRTC:
            _LOGGER.info("From the mirroring receiver: %s", payload)

    # -- FFmpeg --------------------------------------------------------------

    def _start_source(self) -> None:
        loop = asyncio.get_running_loop()
        ff, monitor, region = self._capture
        backend, encoder = self._using = self._sources.pop(0)
        width, height = self.canvas
        cmd = build_command(ff, self.settings, source=self.source, encoder="libx264" if encoder == VP8_ENCODER
                            else encoder, width=width, height=height, fps=self.fps, backend=backend, monitor=monitor,
                            region=region)
        if encoder == VP8_ENCODER:
            cmd = vp8_command(cmd, self.settings.video_bitrate_kbps, self.fps, self.settings.mirror_keyframe_seconds)
        self.encoder = encoder
        self._set(State.STARTING, Text("encoder_via", "{encoder} via {backend}",
                                       encoder=ffmpeg.ENCODER_LABELS.get(encoder, encoder),
                                       backend="testsrc" if self.source == TEST_PATTERN else backend))
        frames = self._frames
        assert frames is not None

        def hand_over(unit: Optional[Unit]) -> None:  # from FFmpeg's pipe thread
            try:
                loop.call_soon_threadsafe(frames.put, unit)
            except RuntimeError:
                pass

        self._ffmpeg = (IvfSource if encoder == VP8_ENCODER else FFmpegSource)(cmd, hand_over)
        self._ffmpeg.start()

    @staticmethod
    def _stop_ffmpeg(source: FFmpegSource) -> None:
        source.stop()

    def _start_audio(self) -> None:
        loop = asyncio.get_running_loop()
        port = system_audio.free_port() if self.source == DESKTOP else None
        queue: asyncio.Queue = asyncio.Queue()

        def hand_over(unit: Optional[Unit]) -> None:  # from FFmpeg's pipe thread
            def put() -> None:
                if unit is None or queue.qsize() < AUDIO_QUEUE:
                    queue.put_nowait(unit)
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass

        self._audio_ffmpeg = OpusSource(opus_command(self._capture[0], self._audio_bps, port), hand_over)
        self._audio_ffmpeg.start()
        if port is not None:
            self._feeder = system_audio.LoopbackFeeder(port)
            self._feeder.start()
        self._audio_task = asyncio.ensure_future(self._pump_audio(queue))

    async def _pump_audio(self, queue: asyncio.Queue) -> None:
        """The Opus frames to the receiver, from the first video frame on, until FFmpeg ends."""
        sender = self._audio_sender
        assert sender is not None
        timer = self._timer = AudioTimer(sender.clock)
        while True:
            unit = await queue.get()
            if unit is None:
                source = self._audio_ffmpeg
                _LOGGER.warning("The sound stopped; the picture goes on. %s", source.errors() if source else "")
                self.sound = False
                self._speakers.restore()
                return
            if self._first_sent_ns is None:
                continue
            rtp_time = timer.stamp(unit.arrived_ns, opus_samples(unit.data))
            if rtp_time is not None:
                sender.send_frame(unit.data, True, unit.arrived_ns, rtp_time=rtp_time)
                if not self.sound and self.source == DESKTOP and self.settings.mute_pc_audio:
                    self._speakers.mute()  # the TV has the sound: no echo from the PC
                self.sound = True

    # -- sending ---------------------------------------------------------------

    async def _pump(self) -> None:
        """FFmpeg's frames to the receiver, until FFmpeg ends (raised as an error)."""
        frames, sender = self._frames, self._sender
        assert frames is not None and sender is not None
        waiting_keyframe = True
        while True:
            unit = await frames.get()
            if unit is None:
                if self._first_sent_ns is None and self._sources:
                    failed = self._ffmpeg
                    assert failed is not None
                    await asyncio.get_running_loop().run_in_executor(None, self._stop_ffmpeg, failed)
                    _LOGGER.warning("%s with %s failed, trying the next option. %s", *self._using, failed.errors())
                    self._start_source()
                    continue
                assert self._ffmpeg is not None
                raise CastMirrorError(ended_text(self._ffmpeg))
            if unit.kind == CONFIG:
                config = parse_avcc(unit.data)
                self._sps, self._pps, self._length_size = config.sps, config.pps, config.length_size
                continue
            if unit.kind != FRAME:
                continue
            if self.codec == VP8:
                nals, keyframe = [], unit.keyframe
            else:
                nals, sps, pps = frame_nals(split_avcc(unit.data, self._length_size))
                self._sps, self._pps = sps or self._sps, pps or self._pps
                keyframe = unit.keyframe or is_idr(nals)
            if frames.take_trimmed():
                waiting_keyframe = True
            if not keyframe and self._behind(sender, unit.arrived_ns):
                waiting_keyframe = True
            if waiting_keyframe and not keyframe:
                self._dropped += 1
                continue
            if keyframe and not self._sps and self.codec == H264:
                continue  # no parameter sets yet: nothing the receiver could decode
            waiting_keyframe = False
            data = unit.data if self.codec == VP8 else to_annexb((self._sps + self._pps if keyframe else []) + nals)
            sender.send_frame(data, keyframe, unit.arrived_ns)
            if self._first_sent_ns is None:
                self._first_sent_ns = now_ns()
                if self._sps:
                    try:
                        info = parse_sps(self._sps[0])
                        self.picture = (info.width, info.height)
                    except ValueError:
                        pass
                self._set(State.CASTING, self.device.name)

    def _behind(self, sender: StreamSender, now: int) -> bool:
        """The receiver has fallen more than CATCH_UP_MS past its delay (once it says anything)."""
        oldest = sender.oldest_unacked_ns()
        if oldest is None or not sender.stats.feedbacks:
            return False
        return now - oldest > (self.delay_ms + CATCH_UP_MS) * NS // 1000

    async def _supervise(self) -> str:
        pump = asyncio.ensure_future(self._pump())
        started = now_ns()
        last_stats = started
        try:
            while True:
                if pump.done():
                    pump.result()  # raises what ended it
                    return "Stopped"
                if self._stop.is_set():
                    return "Stopped"
                now = now_ns()
                if self.seconds is not None and self._first_sent_ns is not None \
                        and now - self._first_sent_ns > self.seconds * NS:
                    return "Time limit reached"
                if self._channel is not None and self._channel.closed.is_set():
                    raise CastMirrorError(Text("gc_tv_closed", "{device} closed the Cast connection.",
                                               device=self.device.name))
                if self._app_gone:
                    return Text("gc_tv_ended", "{device} ended screen mirroring", device=self.device.name)
                self._check_frames(now, started)
                self._check_rtcp(now)
                if now - last_stats > STATS_SECONDS * NS:
                    last_stats = now
                    self._log_stats()
                await asyncio.sleep(SUPERVISE_SECONDS)
        finally:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass

    def _check_frames(self, now: int, started: int) -> None:
        frames = self._frames
        if frames is None:
            return
        if frames.last_frame_ns is None:
            if now - started > FIRST_FRAME_SECONDS * NS:
                raise CastMirrorError(Text("gc_no_picture", "FFmpeg gave no picture in {seconds} s.",
                                           seconds=FIRST_FRAME_SECONDS))
        elif now - frames.last_frame_ns > FRAME_GAP_SECONDS * NS:
            raise CastMirrorError(Text("gc_no_picture", "FFmpeg gave no picture in {seconds} s.",
                                       seconds=FRAME_GAP_SECONDS))

    def _check_rtcp(self, now: int) -> None:
        sender = self._sender
        if sender is None or self._first_sent_ns is None:
            return
        heard = sender.last_rtcp_ns or self._first_sent_ns
        if now - heard > RTCP_SECONDS * NS:
            raise CastMirrorError(Text("gc_no_rtcp", "{device} stopped answering the video ({seconds} s without a "
                                       "word). Check that the PC and the TV are on the same network.",
                                       device=self.device.name, seconds=RTCP_SECONDS))

    def _log_stats(self) -> None:
        sender = self._sender
        if sender is None:
            return
        st = sender.stats
        _LOGGER.info("Cast: %d frames (%d keyframes, %d dropped), %.1f MB, %d packets sent again, %d waiting for "
                     "the TV, %d lost by its count, %d keyframe requests, TV delay %d ms; the TV decoded %d and "
                     "showed %d (%d late, last with %s ms to spare)",
                     st.frames, st.keyframes, self._dropped, st.bytes / 1e6, st.resent, max(0, sender.in_flight()),
                     st.lost_reported, st.plis, st.target_delay_ms, st.decoded, st.played, st.played_late,
                     st.spare_ms)
        audio = self._audio_sender
        if audio is not None:
            st, timer = audio.stats, self._timer
            _LOGGER.info("Cast sound: %d frames, %.2f MB, %d packets sent again, %d waiting for the TV; the TV "
                         "played %d (%d late, last with %s ms to spare); count restarted %d times, %d frames left out",
                         st.frames, st.bytes / 1e6, st.resent, max(0, audio.in_flight()), st.played, st.played_late,
                         st.spare_ms, timer.restarts if timer else 0, timer.skipped if timer else 0)

    # -- the end -----------------------------------------------------------------

    async def _cleanup(self) -> None:
        loop = asyncio.get_running_loop()
        if self._audio_task is not None:
            self._audio_task.cancel()
        if self._feeder is not None:
            self._feeder.stop()
        for source in (self._ffmpeg, self._audio_ffmpeg):
            if source is not None:
                await loop.run_in_executor(None, self._stop_ffmpeg, source)
        self._ffmpeg = self._audio_ffmpeg = None
        if self._sender is not None:
            self._log_stats()
            self._shown = self._sender.stats.played
            self._reported = self._sender.stats.logs > 0
        if self._socket is not None:
            self._socket.close()
        self._socket = self._sender = self._audio_sender = None
        self.sound = False
        self._speakers.restore()
        channel, self._channel = self._channel, None
        if channel is not None:
            if self._app is not None and not channel.closed.is_set() and not self._app_gone:
                try:
                    await channel.request(NS_RECEIVER, {"type": "STOP", "sessionId": self._app.get("sessionId")},
                                          timeout=STOP_SECONDS)
                except ChannelError as ex:
                    _LOGGER.debug("STOP: %s", ex)
            await channel.close()
        if self._xscreen is not None:
            await self._xscreen.stop()
            self.virtual_device = ""

