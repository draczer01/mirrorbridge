"""The sound for Cast mirroring: the PC's audio (or a beep with the test pattern) as Opus.

As Chrome sends it: Opus at 48 kHz in stereo, one frame every 10 ms, each frame a Cast
frame of its own. A second FFmpeg encodes it (libopus, low delay) from the system audio
that audio.LoopbackFeeder records (WASAPI loopback, raw float32 over TCP, as for the HLS
cast), and writes Ogg to the pipe, one page per frame, which OggReader takes apart.

The frames' RTP times count the samples (AUDIO_CLOCK a second) from when the first one
came, less LEAD_MS, the time it took to record and encode it; so the sound is shown with the
picture, both being timed by the session's Clock. AudioTimer keeps that count on the clock:
the sound card's clock is not quite the PC's, the system audio stops while nothing plays,
and sound held up on the way (FFmpeg starting, a busy moment) comes in a burst. When the
frames come later than the count says (a gap, or a slower sound card), the count jumps
ahead; when they come earlier, frames are left out until it is on time again, so a burst
does not leave the sound behind the picture for good.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Optional

from .. import ffmpeg
from ..mirror.clock import NS, now_ns
from ..mirror.flv import FRAME
from ..mirror.source import READ_SIZE, FFmpegSource, Unit
from .offer import AUDIO_CHANNELS
from .rtp import AUDIO_CLOCK
from .sender import Clock

_LOGGER = logging.getLogger(__name__)

ENCODER = "libopus"
FRAME_MS = 10
LEAD_MS = 25  # recorded (10 ms blocks), encoded (10 ms frames, 2.5 ms look-ahead), piped
LEAD_ENV = "MIRRORBRIDGE_CAST_AUDIO_LEAD_MS"  # a test value for LEAD_MS, from a .bat
JUMP_MS = 200  # a frame this late: the sound stopped for a while, start the count again
DRIFT_MS = 40  # the count may be this far off before it is put right
WINDOW_SECONDS = 2  # ...judged by the earliest frame in this long
TEST_TONE = "sine=frequency=440:beep_factor=2:sample_rate=48000"  # a tone, and a beep each second


@functools.lru_cache(maxsize=None)
def has_opus(ff: str) -> bool:
    out = ffmpeg._run([ff, "-hide_banner", "-encoders"]).stdout  # pylint: disable=protected-access
    return any(line.split()[1:2] == [ENCODER] for line in out.splitlines() if line.strip())


def opus_command(ff: str, bps: int, port: Optional[int] = None) -> list[str]:
    """FFmpeg reading float32 PCM from 127.0.0.1:port (or the test tone), writing Opus in Ogg."""
    if port is None:
        source = ["-re", "-f", "lavfi", "-i", TEST_TONE]
    else:
        source = ["-thread_queue_size", "1024", "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer",
                  "-f", "f32le", "-ar", str(AUDIO_CLOCK), "-ac",
                  str(AUDIO_CHANNELS), "-i", f"tcp://127.0.0.1:{port}?listen=1"]
    return [ff, "-hide_banner", "-nostats", "-loglevel", "warning", *source,
            "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_CLOCK), "-c:a", ENCODER, "-application", "lowdelay",
            "-frame_duration", str(FRAME_MS), "-b:a", str(bps), "-vbr", "constrained",
            "-flush_packets", "1", "-page_duration", str(FRAME_MS * 1000), "-f", "ogg", "pipe:1"]


class OggReader:
    """Ogg pages in, the Opus packets in them out (not OpusHead and OpusTags)."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._packet = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        packets = []
        while len(self._buffer) >= 27:
            if self._buffer[:4] != b"OggS":
                raise ValueError("not Ogg")
            count = self._buffer[26]
            if len(self._buffer) < 27 + count:
                break
            lacing = self._buffer[27:27 + count]
            size = 27 + count + sum(lacing)
            if len(self._buffer) < size:
                break
            pos = 27 + count
            for length in lacing:
                self._packet += self._buffer[pos:pos + length]
                pos += length
                if length < 255:
                    packet, self._packet = bytes(self._packet), bytearray()
                    if packet and not packet.startswith((b"OpusHead", b"OpusTags")):
                        packets.append(packet)
            del self._buffer[:size]
        return packets


# Samples (at 48 kHz) of one Opus frame, by the configuration in the TOC byte's top 5 bits (RFC 6716)
_FRAME_SAMPLES = [480, 960, 1920, 2880] * 3 + [480, 960] * 2 + [120, 240, 480, 960] * 4


def opus_samples(packet: bytes) -> int:
    if not packet:
        return 0
    per_frame = _FRAME_SAMPLES[packet[0] >> 3]
    code = packet[0] & 3
    frames = 1 if code == 0 else 2 if code in (1, 2) else (packet[1] & 0x3F if len(packet) > 1 else 0)
    return per_frame * frames


def choose_lead(wanted: Optional[int]) -> int:
    """How long before it comes a frame is taken to have been recorded: `wanted`, the test value
    in LEAD_ENV, or LEAD_MS. More moves the sound earlier against the picture."""
    if wanted is None:
        try:
            wanted = int(os.environ.get(LEAD_ENV, "") or LEAD_MS)
        except ValueError:
            wanted = LEAD_MS
    return max(0, min(1000, wanted))


class AudioTimer:
    """RTP times for the frames as they come (see the module's notes); None: leave it out."""

    def __init__(self, clock: Clock, lead_ms: Optional[int] = None) -> None:
        self.clock = clock
        self.lead_ms = choose_lead(lead_ms)
        self.restarts = 0
        self.skipped = 0
        self._anchor: Optional[int] = None  # when the first sample of the count was recorded
        self._samples = 0
        self._window_start = 0
        self._window_min: Optional[int] = None

    def stamp(self, arrived_ns: int, samples: int) -> Optional[int]:
        lead = self.lead_ms * NS // 1000
        if self._anchor is not None and arrived_ns - self._expected() - lead > JUMP_MS * NS // 1000:
            self._anchor = None
            self.restarts += 1
        if self._anchor is None:
            self._anchor, self._samples = arrived_ns - lead, 0
            self._window_start, self._window_min = arrived_ns, None
        lag = arrived_ns - self._expected()
        if lag - lead < -DRIFT_MS * NS // 1000:
            # Recorded earlier than the count says (sound held up and then let go at once, or a
            # sound card faster than the PC): leave frames out until the count is back on time.
            self.skipped += 1
            return None
        self._window_min = lag if self._window_min is None else min(self._window_min, lag)
        if arrived_ns - self._window_start > WINDOW_SECONDS * NS:
            off = self._window_min - lead
            self._window_start, self._window_min = arrived_ns, None
            if off > DRIFT_MS * NS // 1000:  # a slower sound card: the count jumps ahead
                self._anchor += off
        rtp = self.clock.rtp(self._anchor, AUDIO_CLOCK) + self._samples
        self._samples += samples
        return rtp & 0xFFFFFFFF

    def _expected(self) -> int:
        assert self._anchor is not None
        return self._anchor + self._samples * NS // AUDIO_CLOCK


class OpusSource(FFmpegSource):
    """FFmpegSource for Opus in Ogg: each packet a Unit(FRAME)."""

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        reader = OggReader()
        fd = self.proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, READ_SIZE)
                if not chunk:
                    break
                arrived = now_ns()
                for packet in reader.feed(chunk):
                    if self.first_unit_ns is None:
                        self.first_unit_ns = arrived
                    self.on_unit(Unit(FRAME, 0, True, packet, arrived))
        except ValueError as ex:
            self.error = ex
            _LOGGER.error("FFmpeg's audio output: %s", ex)
        except OSError as ex:
            _LOGGER.debug("FFmpeg audio pipe: %s", ex)
        finally:
            self.on_unit(None)

