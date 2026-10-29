"""VP8 for receivers that show nothing of H.264: Chrome's own mirroring codec when the PC has no
hardware H.264 encoder, so every Cast mirroring receiver takes it.

FFmpeg encodes it with libvpx in real-time mode (no lag, no alternate reference frames, error
resilient, a keyframe every few seconds) and writes IVF to the pipe: a 32-byte file header,
then each frame as its size (4 bytes, little-endian), its time (8 bytes) and the frame. A
frame whose first bit is 0 is a keyframe. Cast Streaming sends the frames as they are.
"""

from __future__ import annotations

import logging
import os
import struct

from .. import ffmpeg
from ..mirror.clock import now_ns
from ..mirror.flv import FRAME
from ..mirror.source import READ_SIZE, FFmpegSource, Unit

_LOGGER = logging.getLogger(__name__)

ENCODER = "libvpx"
IVF_HEADER = 32
FRAME_HEADER = 12


def vp8_command(h264_cmd: list[str], kbps: int, fps: int, keyframe_seconds: int) -> list[str]:
    """mirror.source.build_command's command (made for libx264), encoding VP8 into IVF instead."""
    cut = h264_cmd.index("-c:v")
    gop = max(1, round(fps * ffmpeg.mirror_keyframe_seconds(keyframe_seconds)))
    return h264_cmd[:cut] + [
        "-c:v", ENCODER, "-deadline", "realtime", "-cpu-used", "8", "-lag-in-frames", "0", "-auto-alt-ref", "0",
        "-error-resilient", "1", "-threads", str(max(1, min(8, (os.cpu_count() or 2) // 2))),
        "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-minrate", f"{kbps}k", "-bufsize", f"{max(1, kbps // 2)}k",
        "-g", str(gop), "-keyint_min", str(gop), "-flush_packets", "1", "-f", "ivf", "pipe:1",
    ]


class IvfReader:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._header = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        frames = []
        if not self._header:
            if len(self._buffer) < IVF_HEADER:
                return frames
            if self._buffer[:4] != b"DKIF":
                raise ValueError("not IVF")
            del self._buffer[:IVF_HEADER]
            self._header = True
        while len(self._buffer) >= FRAME_HEADER:
            size = struct.unpack_from("<I", self._buffer)[0]
            if len(self._buffer) < FRAME_HEADER + size:
                break
            frames.append(bytes(self._buffer[FRAME_HEADER:FRAME_HEADER + size]))
            del self._buffer[:FRAME_HEADER + size]
        return frames


def is_keyframe(frame: bytes) -> bool:
    return bool(frame) and not frame[0] & 1


class IvfSource(FFmpegSource):
    """FFmpegSource for VP8 in IVF: each frame a Unit(FRAME), keyframes marked."""

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        reader = IvfReader()
        fd = self.proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, READ_SIZE)
                if not chunk:
                    break
                arrived = now_ns()
                for frame in reader.feed(chunk):
                    if self.first_unit_ns is None:
                        self.first_unit_ns = arrived
                    self.on_unit(Unit(FRAME, 0, is_keyframe(frame), frame, arrived))
        except ValueError as ex:
            self.error = ex
            _LOGGER.error("FFmpeg's output: %s", ex)
        except OSError as ex:
            _LOGGER.debug("FFmpeg pipe: %s", ex)
        finally:
            self.on_unit(None)
