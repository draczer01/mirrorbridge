"""The video of a mirroring session: FFmpeg encoding H.264 into a pipe, as FLV (flv.py).

Two sources: the desktop, captured as a cast captures it (ffmpeg.py: ddagrab or gdigrab, the
same display, window and mouse pointer choices), and a test pattern (FFmpeg's testsrc: a moving
gradient and a frame counter) that needs no screen at all. Either is scaled to fit the TV's
screen as GET /info gives it (never up), and encoded with ffmpeg.mirror_encoder_args: the
encoder MirrorBridge picks for casts, with every option that makes it hold frames back turned
off.

A thread of its own reads the pipe in large reads and hands each complete frame, with the time
its last byte arrived, to the asyncio loop.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from .. import ffmpeg
from ..config import Settings
from ..text import Text
from .clock import now_ns
from .flv import FlvError, FlvReader

_LOGGER = logging.getLogger(__name__)

DESKTOP = "desktop"
TEST_PATTERN = "test"
SOURCES = (DESKTOP, TEST_PATTERN)
READ_SIZE = 1 << 20
STOP_SECONDS = 5


@dataclass
class Unit:
    """One FLV video tag from FFmpeg: the avcC (flv.CONFIG) or a frame (flv.FRAME)."""

    kind: str
    pts_ms: int
    keyframe: bool
    data: bytes
    arrived_ns: int  # when its last byte came out of the pipe (clock.now_ns)


def build_command(
    ff: str,
    settings: Settings,
    *,
    source: str,
    encoder: str,
    width: int,
    height: int,
    fps: int,
    backend: str = "",
    monitor: "ffmpeg.Monitor | None" = None,
    region: "ffmpeg.Region | None" = None,
) -> list[str]:
    """FFmpeg: the source, fitted into width x height, encoded for mirroring, FLV to stdout."""
    if source == TEST_PATTERN:
        video_in = ["-re", "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}"]
        filters: list[str] = []
    else:
        capture = dataclasses.replace(settings, framerate=fps)
        video_in, filters = ffmpeg._video_input(backend, capture, monitor, region)  # pylint: disable=protected-access
        filters = list(filters) + [
            f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease"
            ":force_divisible_by=2:flags=bicubic",
        ]
    filters.append("format=yuv420p" if encoder == "libx264" else "format=nv12")
    cmd = [ff, "-hide_banner", "-nostats", "-loglevel", "warning", "-thread_queue_size", "64", *video_in]
    cmd += ["-vf", ",".join(filters), "-an"]
    cmd += ffmpeg.mirror_encoder_args(encoder, settings.video_bitrate_kbps, fps)
    cmd += ["-flush_packets", "1", "-flvflags", "no_duration_filesize+no_metadata", "-f", "flv", "pipe:1"]
    return cmd


class FFmpegSource:
    """Runs FFmpeg and hands each frame to `on_unit` (from the reader thread); None at the end."""

    def __init__(self, cmd: list[str], on_unit: Callable[[Optional[Unit]], None]) -> None:
        self.cmd = cmd
        self.on_unit = on_unit
        self.proc: Optional[subprocess.Popen] = None
        self.returncode: Optional[int] = None
        self.started_ns = 0
        self.first_unit_ns: Optional[int] = None
        self.error: Optional[Exception] = None
        self._stderr: deque[str] = deque(maxlen=30)

    def start(self) -> None:
        self.started_ns = now_ns()
        self.proc = ffmpeg.popen(self.cmd, stdout=subprocess.PIPE)
        threading.Thread(target=self._read, name="mirrorbridge-mirror-pipe", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name="mirrorbridge-mirror-log", daemon=True).start()

    def errors(self) -> str:
        return "\n".join(list(self._stderr)[-8:])

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        reader = FlvReader()
        fd = self.proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, READ_SIZE)
                if not chunk:
                    break
                arrived = now_ns()
                for tag in reader.feed(chunk):
                    if self.first_unit_ns is None:
                        self.first_unit_ns = arrived
                    self.on_unit(Unit(tag.kind, tag.pts_ms, tag.keyframe, tag.data, arrived))
        except FlvError as ex:
            self.error = ex
            _LOGGER.error("FFmpeg's output: %s", ex)
        except (OSError, ValueError) as ex:  # the pipe was closed under us (stop)
            _LOGGER.debug("FFmpeg pipe: %s", ex)
        finally:
            self.on_unit(None)

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)
                _LOGGER.debug("ffmpeg: %s", line)

    def stop(self) -> None:
        """Ask FFmpeg to quit ('q'), then kill it if it does not. Blocks up to STOP_SECONDS."""
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(STOP_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.returncode = proc.returncode


def ended_text(source: FFmpegSource) -> Text:
    """FFmpeg stopped while the session went on."""
    return Text("encoder_stopped", "Encoder stopped unexpectedly:\n{errors}",
                errors=source.errors() or str(source.error or ""))
