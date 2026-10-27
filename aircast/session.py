"""One casting session: capture -> encode -> HLS -> HTTP -> Apple TV."""

from __future__ import annotations

import asyncio
import enum
import logging
import shutil
import socket
import subprocess
import tempfile
import threading
from collections import deque
from ipaddress import IPv4Address
from pathlib import Path
from typing import Callable, Optional

from . import airplay_patch, audio, ffmpeg
from .config import Settings
from .devices import Device, DeviceManager
from .hls_server import HlsServer

_LOGGER = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "Idle"
    STARTING = "Starting encoder"
    BUFFERING = "Buffering"
    CONNECTING = "Connecting to receiver"
    CASTING = "Casting"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    FAILED = "Failed"


StatusCallback = Callable[[State, str], None]


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
    ) -> None:
        self.manager = manager
        self.device = device
        self.settings = settings
        self.on_status = on_status or (lambda s, m: None)
        self.state = State.IDLE
        self.encoder = ""
        self.backend = ""
        self.stream_url = ""
        self._work_dir = work_dir
        self._own_work_dir = work_dir is None
        self._proc: subprocess.Popen | None = None
        self.encoder_returncode: int | None = None
        self._stderr_tail: deque[str] = deque(maxlen=30)
        self._feeder: audio.LoopbackFeeder | None = None
        self._server: HlsServer | None = None
        self._atv = None
        self._play_task: asyncio.Task | None = None
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._starting = False  # stop() may cancel outright only while starting up
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

    async def _wait_for_segments(self, playlist: Path, count: int = 3, timeout: float = 25) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(f"FFmpeg exited (code {self._proc.returncode}):\n{self.ffmpeg_errors()}")
            if playlist.exists():
                text = playlist.read_text(encoding="utf-8", errors="replace")
                if text.count("#EXTINF") >= count:
                    return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"FFmpeg produced no video within {timeout:.0f}s:\n{self.ffmpeg_errors()}")

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
        self._set(State.STARTING, "Checking FFmpeg and encoders")

        ff = ffmpeg.find_ffmpeg(s.ffmpeg_path)
        _LOGGER.info("FFmpeg: %s", ff)
        monitors = ffmpeg.list_monitors()
        monitor = monitors[max(0, min(s.monitor_index, len(monitors) - 1))] if monitors else None
        region = None
        if s.window_title and s.capture_backend != "test":
            # Capture the window's rectangle of the screen rather than the window itself:
            # GDI window capture is black for GPU-drawn apps (browsers, players, games).
            monitor, region = ffmpeg.window_region(s.window_title, monitors)
            _LOGGER.info("Window %r is at %s on %s", s.window_title, region, monitor.label)
        encoder = await loop.run_in_executor(None, ffmpeg.pick_encoder, ff, s.encoder)
        backend = await loop.run_in_executor(None, ffmpeg.pick_capture_backend, ff, s, monitor, region)

        if self._work_dir is None:
            self._work_dir = Path(tempfile.mkdtemp(prefix="aircast-"))
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
                if number == len(attempts):
                    raise
                _LOGGER.warning(
                    "%s with %s failed, trying the next option. %s", self.backend, self.encoder, ex
                )
                await self._stop_encoder()
                for leftover in self._work_dir.glob("*"):
                    leftover.unlink(missing_ok=True)

        host = s.bind_address or local_ip_reaching(self.device.address)
        self._server = HlsServer(self._work_dir, host, s.http_port)
        try:
            await self._server.start()
        except OSError:
            _LOGGER.warning("Port %s busy, using a random port", s.http_port)
            self._server = HlsServer(self._work_dir, host, 0)
            await self._server.start()
        self.stream_url = self._server.playlist_url

        self._set(State.CONNECTING, self.device.name)
        airplay_patch.apply()
        self._atv = await self.manager.connect(self.device)
        self._play_task = asyncio.ensure_future(self._atv.stream.play_url(self.stream_url))

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
            monitor=monitor, region=region, audio_port=audio_port,
        )
        self._set(State.STARTING, f"{ffmpeg.ENCODER_LABELS.get(self.encoder, self.encoder)} via {self.backend}")
        self._stderr_tail.clear()
        self._proc = ffmpeg.popen(cmd)
        threading.Thread(
            target=self._drain_stderr, args=(self._proc,), name="aircast-ffmpeg-log", daemon=True
        ).start()
        if audio_port is not None:
            self._feeder = audio.LoopbackFeeder(audio_port)
            self._feeder.start()

        self._set(State.BUFFERING)
        await self._wait_for_segments(self._work_dir / "stream.m3u8")

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

    async def _supervise(self) -> None:
        stop_wait = asyncio.ensure_future(self._stop_requested.wait())
        announced = False
        try:
            while True:
                done, _ = await asyncio.wait({self._play_task, stop_wait}, timeout=0.5)
                if stop_wait in done:
                    self._set(State.STOPPING)
                    return
                if self._play_task in done:
                    exc = self._play_task.exception()
                    if exc:
                        raise exc
                    return  # stopped from the TV
                if self._proc and self._proc.poll() is not None:
                    raise RuntimeError(f"Encoder stopped unexpectedly:\n{self.ffmpeg_errors()}")
                if not announced and self._server and self._server.requests >= 1:
                    announced = True
                    self._set(State.CASTING, f"{self.device.name} - {self.stream_url}")
        finally:
            stop_wait.cancel()

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
        if self._server:
            await self._server.close()
            self._server = None
        if self._own_work_dir and self._work_dir:
            shutil.rmtree(self._work_dir, ignore_errors=True)
