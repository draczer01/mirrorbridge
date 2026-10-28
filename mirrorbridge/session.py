"""One casting session: capture -> encode -> HLS -> HTTP -> Apple TV."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from ipaddress import IPv4Address
from pathlib import Path
from typing import Callable, Optional

from . import airplay_patch, audio, ffmpeg, vdisplay
from .config import Settings
from .devices import Device, DeviceManager
from .hls_server import Activity, HlsServer
from .text import Text

_LOGGER = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "Idle"
    STARTING = "Starting encoder"
    EXTENDING = "Creating virtual screen"
    BUFFERING = "Buffering"
    CONNECTING = "Connecting to receiver"
    CASTING = "Casting"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    FAILED = "Failed"


StatusCallback = Callable[[State, str], None]

PLAYLIST_TIMEOUT = 20  # seconds from the TV accepting the video until it fetches the playlist
SEGMENT_TIMEOUT = 15  # seconds from its first playlist until it fetches a video segment
NO_REQUEST_TIMEOUT = 45  # seconds for the TV to answer and fetch anything at all
IDLE_REQUEST_TIMEOUT = 20  # a playing TV refetches the live playlist every 0.5-2 s
EXTEND_4K_MIN_KBPS = 20000  # a 4K desktop at the usual 8 Mbps is a smeary mess
SUPERVISE_SECONDS = 0.5


class StreamNotFetched(RuntimeError):
    """The TV accepted the video but never downloaded the playlist."""


class StreamRejected(RuntimeError):
    """The TV downloaded the playlist but none of the video in it."""


def local_ip_reaching(remote: str) -> str:
    """IP of the local interface that routes to the receiver (skips VPN/Hyper-V adapters)."""
    try:
        from pyatv.support.net import get_local_address_reaching

        addr = get_local_address_reaching(IPv4Address(remote))
        if addr:
            return str(addr)
    except Exception:  # pylint: disable=broad-except
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((remote, 7000))
        return sock.getsockname()[0]


class CastSession:
    def __init__(
        self,
        manager: DeviceManager,
        device: Device,
        settings: Settings,
        on_status: Optional[StatusCallback] = None,
        *,
        work_dir: Path | None = None,
        route: str | None = None,
    ) -> None:
        self.manager = manager
        self.device = device
        self.settings = settings
        self.on_status = on_status or (lambda s, m: None)
        self.state = State.IDLE
        self.encoder = ""
        self.backend = ""
        self.stream_url = ""
        self.for_apple = True  # Apple TVs get the stream they always had; see ffmpeg.OTHER_TV_*
        self.no_codecs = False  # FFmpeg's master playlist declares no CODECS (see _log_master_playlist)
        self._work_dir = work_dir
        self._own_work_dir = work_dir is None
        self.route = route  # how the TV is handed the stream (airplay_patch.PLAY or QUEUE); None = its best
        self._proc: subprocess.Popen | None = None
        self.encoder_returncode: int | None = None
        self._stderr_tail: deque[str] = deque(maxlen=30)
        self._feeder: audio.LoopbackFeeder | None = None
        self._server: HlsServer | None = None
        self._atv = None
        self._cast: airplay_patch.CastContext | None = None
        self._play_task: asyncio.Task | None = None
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._starting = False  # stop() may cancel outright only while starting up
        self._xscreen: vdisplay.VirtualScreen | None = None
        self._lost = False  # the virtual screen went away under the cast
        self.virtual_device = ""  # \\.\DISPLAYn of the virtual screen while there is one
        self.error: Exception | None = None  # why the cast failed
        self.finished = threading.Event()

    # -- helpers -----------------------------------------------------------

    def _set(self, state: State, message: str = "") -> None:
        self.state = state
        _LOGGER.info("%s%s", state.value, f": {message}" if message else "")
        try:
            self.on_status(state, message)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Status callback failed")

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                _LOGGER.debug("ffmpeg: %s", line)

    def ffmpeg_errors(self) -> str:
        return "\n".join(list(self._stderr_tail)[-8:])

    async def _wait_for_segments(
        self, playlist: Path, count: int = 3, timeout: float = 25, master: Path | None = None
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(Text("ffmpeg_exited", "FFmpeg exited (code {code}):\n{errors}",
                                        code=self._proc.returncode, errors=self.ffmpeg_errors()))
            if playlist.exists() and (master is None or master.exists()):
                text = playlist.read_text(encoding="utf-8", errors="replace")
                if text.count("#EXTINF") >= count:
                    return
            await asyncio.sleep(0.2)
        raise RuntimeError(Text("ffmpeg_no_video", "FFmpeg produced no video within {seconds}s:\n{errors}",
                                seconds=round(timeout), errors=self.ffmpeg_errors()))

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        """Start casting and return when the cast ends (stop(), TV stop, or error)."""
        self._task = asyncio.current_task()
        try:
            if self._stop_requested.is_set():
                self._set(State.STOPPED, "Cancelled")
                return
            self._starting = True
            try:
                await self._start()
            finally:
                self._starting = False
            await self._supervise()
            if self.state != State.FAILED:
                self._set(State.STOPPED, "Cast ended")
        except asyncio.CancelledError:
            self._set(State.STOPPED, "Cancelled")
            raise
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Cast failed", exc_info=True)
            self.error = ex
            self._set(State.FAILED, str(ex))
            raise
        finally:
            try:
                await asyncio.shield(self._cleanup())
            finally:
                self.finished.set()

    async def stop(self) -> None:
        """Ask the cast to end. While it is still starting, startup is abandoned at once."""
        self._stop_requested.set()
        if self._starting and self._task and not self._task.done():
            self._task.cancel()

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        s = self.settings
        self._set(State.STARTING, Text("checking_ffmpeg", "Checking FFmpeg and encoders"))

        ff = ffmpeg.find_ffmpeg(s.ffmpeg_path)
        _LOGGER.info("FFmpeg: %s", ff)
        region = None
        if s.extend_screen and s.capture_backend != "test" and vdisplay.available():
            monitor = await self._create_virtual_screen()
            s = self.settings  # now without the display and window choices
        else:
            monitors = ffmpeg.list_monitors()
            monitor = monitors[max(0, min(s.monitor_index, len(monitors) - 1))] if monitors else None
            if s.window_title and s.capture_backend != "test":
                # Capture the window's rectangle of the screen rather than the window itself:
                # GDI window capture is black for GPU-drawn apps (browsers, players, games).
                monitor, region = ffmpeg.window_region(s.window_title, monitors)
                _LOGGER.info("Window %r is at %s on %s", s.window_title, region, monitor.label)
        # Apple TVs keep today's stream; other brands, and receivers that do not say
        # what they are, get the longer segments and master playlist of ffmpeg.OTHER_TV_*.
        self.for_apple = self.device.is_apple is True
        kind = {True: "an Apple TV", False: "a TV of another brand", None: "a receiver of unknown make"}
        _LOGGER.info("Streaming for %s: %.1f s segments", kind[self.device.is_apple],
                     ffmpeg.segment_seconds(s, self.for_apple))
        encoder = await loop.run_in_executor(None, ffmpeg.pick_encoder, ff, s.encoder)
        backend = await loop.run_in_executor(None, ffmpeg.pick_capture_backend, ff, s, monitor, region)

        if self._work_dir is None:
            self._work_dir = Path(tempfile.mkdtemp(prefix="mirrorbridge-"))
        self._work_dir.mkdir(parents=True, exist_ok=True)

        # If the first choice dies (driver refuses ddagrab, GPU encoder out of sessions),
        # fall back to GDI capture and then to the CPU encoder instead of failing.
        attempts = [(backend, encoder)]
        if backend == "ddagrab":
            attempts.append(("gdigrab", encoder))
        if encoder != "libx264":
            attempts.append((attempts[-1][0], "libx264"))
        for number, (self.backend, self.encoder) in enumerate(attempts, 1):
            try:
                await self._launch_encoder(ff, monitor, region)
                break
            except RuntimeError as ex:
                if self._lost:
                    raise vdisplay.VddError("lost") from ex
                if number == len(attempts):
                    raise
                _LOGGER.warning(
                    "%s with %s failed, trying the next option. %s", self.backend, self.encoder, ex
                )
                await self._stop_encoder()
                for leftover in self._work_dir.glob("*"):
                    leftover.unlink(missing_ok=True)

        host = s.bind_address or local_ip_reaching(self.device.address)
        # Other TVs get the master playlist; FFmpeg's own lacks these two of Apple's rules.
        variant = {} if self.for_apple else {
            "frame_rate": s.framerate,
            "average_bandwidth": (s.video_bitrate_kbps + s.audio_bitrate_kbps) * 1000,
        }
        self._server = HlsServer(self._work_dir, host, s.http_port, **variant)
        try:
            await self._server.start()
        except OSError:
            _LOGGER.warning("Port %s busy, using a random port", s.http_port)
            self._server = HlsServer(self._work_dir, host, 0, **variant)
            await self._server.start()
        if self.for_apple:
            self.stream_url = self._server.url(ffmpeg.MEDIA_PLAYLIST)
        else:
            self.stream_url = self._server.url(ffmpeg.MASTER_PLAYLIST)
            self._log_master_playlist()

        self._set(State.CONNECTING, self.device.name)
        airplay_patch.apply()
        self._atv = await self.manager.connect(self.device)
        self._cast = airplay_patch.CastContext(
            apple=self.device.is_apple, live=True, password=self.device.requires_password,
            device=self.device.identifier, features=self.device.features, route=self.route,
        )
        self._play_task = asyncio.ensure_future(airplay_patch.play_url(self._atv, self.stream_url, self._cast))

    def _log_master_playlist(self) -> None:
        served = self._server.rewrite_playlist((self._work_dir / ffmpeg.MASTER_PLAYLIST).read_bytes())
        text = served.decode("utf-8", "replace")
        variant = next((line for line in text.splitlines() if line.startswith("#EXT-X-STREAM-INF")), "")
        _LOGGER.info("Master playlist: %s", variant or "no variant")
        # FFmpeg takes CODECS from the encoder's header at startup, which some hardware
        # encoders (Media Foundation with some Intel drivers) do not give.
        self.no_codecs = "CODECS=" not in variant
        if self.no_codecs:
            _LOGGER.warning("FFmpeg declared no CODECS for %s; some TVs refuse such a stream", self.encoder)

    async def _create_virtual_screen(self) -> "ffmpeg.Monitor":
        """Add the virtual monitor Windows extends the desktop onto; return it as a display."""
        loop = asyncio.get_running_loop()
        s = self.settings
        self._set(State.EXTENDING)

        def lost() -> None:  # called from the driver's keep-alive or presence-check thread
            self._lost = True
            try:
                loop.call_soon_threadsafe(self._stop_requested.set)
            except RuntimeError:  # the loop is already closed
                pass

        self._xscreen = vdisplay.backend()(on_lost=lost)
        width, height, hz = vdisplay.EXTEND_MODES.get(s.extend_mode, vdisplay.EXTEND_MODES["1080p"])
        name = await loop.run_in_executor(None, self._xscreen.start, width, height, hz)
        self.virtual_device = name
        for _ in range(10):  # the display list can lag a moment behind the driver
            monitors = await loop.run_in_executor(None, ffmpeg.list_monitors)  # DXGI is slow right after a mode change
            monitor = next((m for m in monitors if m.device.lower() == name.lower()), None)
            if monitor is not None:
                break
            await asyncio.sleep(0.2)
        else:
            raise vdisplay.VddError("not_attached")
        if monitor.dxgi_output is None:
            _LOGGER.info("The virtual screen is drawn by another GPU (DXGI adapter %s)", monitor.dxgi_adapter)
        # The virtual screen is already the size the TV should get, so send it as it is. The
        # display and window choices do not apply to this cast.
        changes: dict = {"window_title": "", "output_height": 0}
        if monitor.height >= 2160:
            changes["video_bitrate_kbps"] = max(s.video_bitrate_kbps, EXTEND_4K_MIN_KBPS)
        self.settings = dataclasses.replace(s, **changes)
        _LOGGER.info("Casting virtual screen %s: %dx%d", name, monitor.width, monitor.height)
        return monitor

    async def _launch_encoder(
        self, ff: str, monitor: "ffmpeg.Monitor | None", region: "ffmpeg.Region | None" = None
    ) -> None:
        s = self.settings
        audio_port = None
        if s.capture_audio and self.backend != "test":
            ok, info = audio.loopback_available()
            if ok:
                audio_port = audio.free_port()
            else:
                _LOGGER.warning("%s - casting video with a silent audio track", info)

        cmd = ffmpeg.build_command(
            ff, s, self._work_dir, encoder=self.encoder, backend=self.backend,
            monitor=monitor, region=region, audio_port=audio_port, for_apple=self.for_apple,
        )
        label = ffmpeg.ENCODER_LABELS.get(self.encoder, self.encoder)
        self._set(State.STARTING, Text("encoder_via", "{encoder} via {backend}", encoder=label, backend=self.backend))
        self._stderr_tail.clear()
        self._proc = ffmpeg.popen(cmd)
        threading.Thread(
            target=self._drain_stderr, args=(self._proc,), name="mirrorbridge-ffmpeg-log", daemon=True
        ).start()
        if audio_port is not None:
            self._feeder = audio.LoopbackFeeder(audio_port)
            self._feeder.start()

        self._set(State.BUFFERING)
        playlist = self._work_dir / ffmpeg.MEDIA_PLAYLIST
        if self.for_apple:
            await self._wait_for_segments(playlist)
        else:
            # Other TVs are handed a playlist that already holds its whole window (Apple's
            # rule for AirPlay 2 TVs: a live playlist needs at least 6 segments). They then
            # start as far behind the live edge as with a short one, only later.
            size = ffmpeg.playlist_size(s, self.for_apple)
            await self._wait_for_segments(
                playlist, count=size, timeout=25 + size * ffmpeg.segment_seconds(s, self.for_apple),
                master=self._work_dir / ffmpeg.MASTER_PLAYLIST,
            )

    async def _stop_encoder(self) -> None:
        if self._feeder:
            self._feeder.stop()
            self._feeder = None
        proc = self._proc  # cleared only once FFmpeg is really gone, so cleanup can retry
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except asyncio.CancelledError:
                proc.kill()  # stopped while waiting: never leave a hung FFmpeg behind
                raise
        if proc:
            self.encoder_returncode = proc.returncode
        self._proc = None

    @property
    def cast(self) -> "airplay_patch.CastContext | None":
        """What the TV answered and said during this cast (None until it is connected)."""
        return self._cast

    def tv_activity(self) -> Activity:
        """What the TV fetched. Only its own address counts: a player opened on the PC or
        a phone must neither hide a TV that cannot reach MirrorBridge nor keep a cast going."""
        if self._server is None:
            return Activity()
        return self._server.activity(self.device.address)

    def clients(self) -> dict[str, Activity]:
        """Everything that reached the stream's server so far, by IP, the TV included."""
        return self._server.clients() if self._server else {}

    def _other_clients(self) -> str:
        """Who else fetched the stream, for an error that says the TV did not."""
        others = [f"{ip} ({c.user_agent or 'no User-Agent'})"
                  for ip, c in self.clients().items() if ip != self.device.address and c.requests]
        if not others:
            return ""
        return Text("tv_other_clients", " ({clients} did download it, but that is not the TV's address, "
                    "{address}.)", clients=", ".join(others), address=self.device.address)

    def _check_startup(self, tv: Activity, now: float, started_at: float) -> None:
        """Fail with the likely cause when the TV took the video but does not play it."""
        accepted = self._cast.accepted_at if self._cast else None
        if tv.first_playlist_at is None:
            if accepted is not None and now - accepted > PLAYLIST_TIMEOUT:
                if tv.missing:
                    raise StreamNotFetched(Text(
                        "tv_wrong_path", "The TV reached MirrorBridge but asked for {path}, which MirrorBridge "
                        "does not serve, instead of the stream at {url}. Please report this TV, with "
                        "mirrorbridge.log.", path=tv.missing_path, url=self.stream_url,
                    ))
                port = self._server.port if self._server else self.settings.http_port
                raise StreamNotFetched(Text(
                    "tv_not_fetched", "The TV accepted the video but never downloaded it from {url}. Allow "
                    "MirrorBridge (or inbound TCP port {port}) in Windows Firewall on private networks, and "
                    "check that the PC and the TV are on the same network.{others}",
                    url=self.stream_url, port=port, others=self._other_clients(),
                ))
            if accepted is None and now - started_at > NO_REQUEST_TIMEOUT:
                raise RuntimeError(Text(
                    "tv_no_request", "The TV did not start the video within {seconds} s and never asked for "
                    "it at {url}. Allow MirrorBridge in Windows Firewall on private networks, and check that "
                    "the PC and the TV are on the same network.{others}",
                    seconds=NO_REQUEST_TIMEOUT, url=self.stream_url, others=self._other_clients(),
                ))
        elif tv.first_segment_at is None and now - tv.first_playlist_at > SEGMENT_TIMEOUT:
            smooth = ffmpeg.LATENCY_SEGMENT_SECONDS["smooth"]
            if self.no_codecs and self.encoder != "libx264":
                label = ffmpeg.ENCODER_LABELS.get(self.encoder, self.encoder)
                hint = Text("hint_x264", "FFmpeg could not tell the TV which codecs {encoder} produces: "
                            "choose Quality > Encoder > x264 (CPU).", encoder=label)
            elif ffmpeg.segment_seconds(self.settings, self.for_apple) < smooth:
                hint = Text("hint_smooth", "Try Quality > Delay > Smooth.")
            elif self.virtual_device:
                # The TV gets the virtual screen's own size: Quality > Resolution does not apply.
                if self.settings.extend_mode == "720p":
                    hint = Text("hint_30fps", "Try 30 frames per second.")
                else:
                    hint = Text("hint_720p", "Try What to cast > Extended screen size > 720p, or 30 frames per second.")
            else:
                hint = Text("hint_lower", "Try a lower resolution or 30 frames per second.")
            asked = (Text("tv_also_asked", " (it also asked for {path}, which MirrorBridge does not have)",
                          path=tv.missing_path) if tv.missing else "")
            raise StreamRejected(Text(
                "tv_rejected", "The TV downloaded the playlist but none of the video{asked}, so it probably "
                "does not accept this video format. {hint}", asked=asked, hint=hint,
            ))

    async def _supervise(self) -> None:
        stop_wait = asyncio.ensure_future(self._stop_requested.wait())
        announced = False
        started_at = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({self._play_task, stop_wait}, timeout=SUPERVISE_SECONDS)
                if stop_wait in done:
                    if self._lost:
                        raise vdisplay.VddError("lost")
                    self._set(State.STOPPING)
                    return
                if self._play_task in done:
                    exc = self._play_task.exception()
                    if exc:
                        raise exc
                    return  # stopped from the TV
                if self._proc and self._proc.poll() is not None:
                    if self._xscreen and await self._screen_lost():
                        raise vdisplay.VddError("lost")
                    raise RuntimeError(Text("encoder_stopped", "Encoder stopped unexpectedly:\n{errors}",
                                            errors=self.ffmpeg_errors()))
                # The TV's own video requests are the one signal every receiver gives,
                # including those that do not report playback status over AirPlay.
                tv = self.tv_activity()
                now = time.monotonic()
                if not announced and tv.requests >= 1:
                    announced = True
                    accepted = self._cast.accepted_at if self._cast else None
                    if accepted is not None and tv.first_playlist_at is not None:
                        _LOGGER.info("The TV fetched the playlist %.1f s after accepting the video",
                                     max(0.0, tv.first_playlist_at - accepted))
                    self._set(State.CASTING, f"{self.device.name} - {self.stream_url}")
                self._check_startup(tv, now, started_at)
                if announced and now - tv.last_request_at > IDLE_REQUEST_TIMEOUT:
                    _LOGGER.info("The TV stopped requesting video; ending the cast")
                    return
        finally:
            stop_wait.cancel()

    async def _screen_lost(self, wait: float = 2.5) -> bool:
        """After FFmpeg died: did the virtual screen vanish under it? Its keep-alive notices
        within about 2 s (sleep and resume, for example)."""
        deadline = time.monotonic() + wait
        while not self._lost and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return self._lost

    async def _cleanup(self) -> None:
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
            try:
                await self._play_task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
        if self._atv:
            try:
                self._atv.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self._atv = None
        await self._stop_encoder()
        if self._xscreen:
            # Only once FFmpeg is gone: ddagrab losing its output would turn a stop into an error.
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._xscreen.stop)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.warning("Could not remove the virtual screen", exc_info=True)
            self._xscreen = None
            self.virtual_device = ""
        if self._server:
            await self._server.close()
            self._server = None
        if self._own_work_dir and self._work_dir:
            shutil.rmtree(self._work_dir, ignore_errors=True)
