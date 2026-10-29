"""The PC's sound in a mirroring session: captured samples put on the session clock, packed into
the type-96 stream (audiopackets.py) and sent to the TV on time.

The TV shows a video frame its lead after the frame was taken; the sound taken at the same
moment must be heard then too. Everything here hangs off one straight line on the session clock,
the sample line: stream sample s is heard at line_ns(s) + G, where G (the sync gap, in samples)
is the video lead plus MIRRORBRIDGE_MIRROR_SOUND_DELAY_MS, and line_ns(s) is about when sample s
was captured. The syncs tell the TV that line (a sample and its time, once a second), the RTP
time and seq of the packets count on without a jump, and what the capture does is made to fit
the line instead:

* the capture time of each block is its arrival less its length, at best: the smallest such
  offset of the last 2 s (as clock.PtsMapper does for video frames). A capture that falls
  behind for good (samples lost in a stall) moves that base once it has been late for 250 ms;
* the stream is kept on the line by inserting or deleting single samples (at most 2 in 352)
  while it is 3 ms off, by filling silence or cutting samples when it is 30 ms off, and by
  re-anchoring the line itself (a 0x90 sync, the marker bit) when it is 200 ms behind;
* the keeper sends zeros, on time, whenever no block came for 100 ms (a device change, a
  stall), so that the TV never runs dry and late blocks are not sent in a burst.

SoundKnobs holds the test switches (MIRRORBRIDGE_MIRROR_SOUND_*), read once per session.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
from collections import deque
from dataclasses import dataclass
from typing import Mapping, Optional

from .audiopackets import FORMATS, SPF, SPF_NS, SR
from .clock import NS, to_ntp

_LOGGER = logging.getLogger(__name__)

SOUND_BY_DEFAULT = False  # stage 1: the sound is on only when asked for (MIRRORBRIDGE_MIRROR_SOUND=1, mirror --sound)
SYNC_INTERVAL_S = 1.0
WINDOW_NS = 2 * NS  # the capture base is the smallest offset of this long
STEP_WINDOW_NS = 250_000_000  # every block this long late by JUMP_NS: samples were lost, the base moves
JUMP_NS = 30_000_000  # off by this much: fill or cut instead of single samples
FILL_MAX_NS = 200_000_000  # behind by more than this: re-anchor the line instead of filling
TOL_START_NS = 3_000_000  # single-sample corrections start when the stream is this far off the line
TOL_STOP_NS = 1_000_000  # and stop when it is back within this
MICRO_SMALL_NS = 10_000_000  # below this, at most one sample in 352; above, two
KEEP_TICK_S = 0.010
KEEP_TRIGGER_NS = 100_000_000  # no samples for this long: the keeper sends zeros
KEEP_TARGET_NS = 10_000_000  # up to this far behind now
MIN_GAP_MS = 20
MAX_PENDING_BLOCKS = 64
CAPTURE_BUFFER_FRAMES = 4410  # WASAPI's buffer for the loopback recorder: 100 ms
SEND_BUFFER_LIMIT = 64 * 1024
SEND_ERRORS_PER_S = 50
LATE_MARGIN_NS = 10_000_000
SOUND_DROP_SECONDS = 5
SOUND_OK_SECONDS = 60
TONE_HZ = 880
TONE_SAMPLES = 4410
TONE_LEVEL = 8191
TONE_RAMP = 220
TONE_ORIGIN_NS = 0

ENV = "MIRRORBRIDGE_MIRROR_SOUND"
SHAPES = ("screen", "minmax", "apple", "media")  # the latencies the type-96 SETUP asks for


def _flag(name: str, text: str) -> bool:
    if text not in ("0", "1"):
        raise ValueError
    return text == "1"


def _choice(choices: dict):
    def parse(name: str, text: str):
        if text.lower() not in choices:
            raise ValueError
        return choices[text.lower()]
    return parse


def _clamped(name: str, text: str, value: float, low: float, high: float) -> float:
    if low <= value <= high:
        return value
    used = min(high, max(low, value))
    _LOGGER.info("%s=%s is out of %g..%g: using %g", name, text, low, high, used)
    return used


def _number(low: float, high: float):
    def parse(name: str, text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError
        return _clamped(name, text, value, low, high)
    return parse


def _whole(low: int, high: int):
    def parse(name: str, text: str) -> int:
        return int(_clamped(name, text, int(text), low, high))
    return parse


def _volume(name: str, text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return -144.0 if value <= -144 else _clamped(name, text, value, -30, 0)


def _ports(name: str, text: str) -> tuple[int, int]:
    data, control = (int(part) for part in text.split(","))
    if not (0 < data < 65536 and 0 < control < 65536):
        raise ValueError
    return data, control


# Each knob: its environment variable, what it takes, and how it is read.
_KNOBS = {
    "enable": (ENV, "1 or 0", _choice({"1": True, "0": False})),
    "tone": (ENV + "_TONE", "0 or 1", _flag),
    "delay_ms": (ENV + "_DELAY_MS", "a number of ms from -500 to 500", _number(-500, 500)),
    "gap_ms": (ENV + "_GAP_MS", "a number of ms from 20 to 2000", _number(MIN_GAP_MS, 2000)),
    "codec": (ENV + "_CODEC", ", ".join(FORMATS), _choice({name: name for name in FORMATS})),
    "shape": (ENV + "_SHAPE", ", ".join(SHAPES), _choice({name: name for name in SHAPES})),
    "lat_min_ms": (ENV + "_LAT_MIN_MS", "a number of ms from 0 to 2000", _number(0, 2000)),
    "lat_max_ms": (ENV + "_LAT_MAX_MS", "a number of ms from 0 to 2000", _number(0, 2000)),
    "is_media": (ENV + "_IS_MEDIA", "0 or 1", _flag),
    "redundant": (ENV + "_REDUNDANT", "0, 1 or 2", _whole(0, 2)),
    "sync_flags": (ENV + "_SYNC_FLAGS", "4 or 7", _choice({"4": 4, "7": 7})),
    "ssrc": (ENV + "_SSRC", "0 or id", _choice({"0": "0", "id": "id"})),
    "volume_db": (ENV + "_VOLUME", "-144 (mute) or a number of dB from -30 to 0", _volume),
    "hold": (ENV + "_HOLD", "0 or 1", _flag),
    "ports": (ENV + "_PORTS", "two port numbers, data,control", _ports),
    "mute": (ENV + "_MUTE", "0 or 1", _flag),
}


def _shown(value) -> str:
    if value is None or value == "":
        return "unset"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, tuple):
        return ",".join(str(part) for part in value)
    return str(value)


@dataclass(frozen=True)
class SoundKnobs:
    """The sound's test switches, from MIRRORBRIDGE_MIRROR_SOUND and MIRRORBRIDGE_MIRROR_SOUND_*.
    None and "" mean unset: the session decides."""

    enable: Optional[bool] = None  # 1: on (stage 1's opt-in), 0: off
    tone: bool = False  # the beep instead of the PC's sound
    delay_ms: float = 0.0  # the sound later (+) or earlier (-) than the picture
    gap_ms: Optional[float] = None  # the sync gap itself, instead of the lead and delay_ms
    codec: str = ""  # a key of audiopackets.FORMATS ("": from the TV's screenStream)
    shape: str = "screen"  # the latencies SETUP asks for (SHAPES)
    lat_min_ms: Optional[float] = None
    lat_max_ms: Optional[float] = None
    is_media: bool = False
    redundant: int = 0  # each packet sent this many times more, with the ones after it
    sync_flags: int = 4
    ssrc: str = "0"  # "id": the low 32 bits of the stream's streamConnectionID
    volume_db: Optional[float] = None  # SET_PARAMETER volume after RECORD
    hold: bool = True  # wait for the first video frame
    ports: Optional[tuple[int, int]] = None  # (data, control) instead of the TV's answer
    mute: bool = False  # mute the PC even if the TV's sound is not confirmed

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "SoundKnobs":
        """The knobs set in `env`. A value a knob does not take leaves it at its default, with a
        line in the log; a number out of its range is taken as the nearest it allows."""
        values = {}
        for field, (name, takes, parse) in _KNOBS.items():
            text = env.get(name, "").strip()
            if not text:
                continue
            try:
                values[field] = parse(name, text)
            except ValueError:
                _LOGGER.info("%s=%s is not %s: left at %s", name, text, takes, _shown(getattr(cls, field)))
        return cls(**values)

    def replace(self, **changes) -> "SoundKnobs":
        return dataclasses.replace(self, **changes)

    def describe(self) -> str:
        """Every knob with its environment variable, for the log."""
        return ", ".join(f"{name}={_shown(getattr(self, field))}" for field, (name, _, _) in _KNOBS.items())


@dataclass
class Correction:
    """What to do to one block so that the stream stays on the line (in samples)."""

    fill: int = 0  # zeros before it
    cut: int = 0  # dropped from its start
    micro: int = 0  # single samples repeated (+) or left out (-) through it
    reanchored: bool = False  # the line itself moved instead


def apply(pcm_be: bytes, corr: Correction) -> bytes:
    """A block of s16 BE stereo frames with the correction made: the cut first, then the single
    samples (spread evenly through what is left), then the zeros in front."""
    out = pcm_be[4 * corr.cut:] if corr.cut else pcm_be
    k = abs(corr.micro)
    n = len(out) // 4
    if k and n:
        positions = sorted({j * n // (k + 1) for j in range(1, k + 1)})
        parts = []
        start = 0
        for i in positions:
            if corr.micro > 0:
                parts.append(out[4 * start:4 * (i + 1)])
                parts.append(out[4 * i:4 * (i + 1)])
                start = i + 1
            else:
                parts.append(out[4 * start:4 * i])
                start = i + 1
        parts.append(out[4 * start:])
        out = b"".join(parts)
    if corr.fill > 0:
        out = bytes(4 * corr.fill) + out
    return out


def floats_to_s16be(arr) -> bytes:
    """soundcard's float32 samples as s16 BE, clipped."""
    import numpy as np  # type: ignore

    return np.clip(np.rint(np.asarray(arr, dtype=np.float32) * 32767.0), -32768, 32767).astype(">i2").tobytes()


class SampleLine:
    """The stream's samples against the session clock (ns): when each captured block was really
    taken, where the stream stands against the line, and the keeper's zeros.

    c counts the samples captured since the recorder (re)opened, s the samples put into the
    stream since the line was anchored (A: the line time of stream sample 0).
    """

    def __init__(self) -> None:
        self.c = 0
        self._win: deque[tuple[int, int]] = deque()  # (arrived, offset), offsets increasing, over WINDOW_NS
        self._recent: deque[tuple[int, int]] = deque()  # the same over STEP_WINDOW_NS
        self._late_since: Optional[int] = None
        self.steps = 0
        self.A: Optional[int] = None
        self.s = 0
        self.correcting = False
        self.keeping = False
        self.reanchors = 0
        self.error_ns = 0  # the last block's capture time less its place on the line

    def observe(self, n: int, arrived_ns: int, fresh: bool) -> int:
        """A block of n samples came at arrived_ns (fresh: the first of a new recorder). Returns
        c0, the capture count of its first sample."""
        if fresh:
            self.c = 0
            self._win.clear()
            self._recent.clear()
            self._late_since = None
        c0 = self.c
        self.c += n
        off = arrived_ns - self.c * NS // SR
        for window, span in ((self._win, WINDOW_NS), (self._recent, STEP_WINDOW_NS)):
            while window and window[-1][1] >= off:
                window.pop()
            window.append((arrived_ns, off))
            while window[0][0] < arrived_ns - span:
                window.popleft()
        if off - self._win[0][1] > JUMP_NS:
            # Late by that much: jitter, or samples lost (a stall longer than the capture buffer).
            # Lost for good once every block of STEP_WINDOW_NS is that late: the base moves.
            if self._late_since is None:
                self._late_since = arrived_ns
            elif arrived_ns - self._late_since >= STEP_WINDOW_NS:
                self._win = deque(self._recent)
                self.steps += 1
                self._late_since = None
        else:
            self._late_since = None
        return c0

    def skip(self, n: int) -> None:
        """n samples were captured but never got here (dropped on the way): a gap to fill."""
        self.c += n

    def t_cap(self, c: int) -> int:
        """When captured sample c was taken."""
        return self._win[0][1] + c * NS // SR

    def line_ns(self, s: int) -> int:
        """The line time of stream sample s."""
        assert self.A is not None
        return self.A + s * NS // SR

    def start(self, c0: int) -> None:
        """Anchor the line at captured sample c0: it becomes stream sample 0."""
        self.A = self.t_cap(c0)
        self.s = 0

    def correction(self, c0: int, n: int) -> Correction:
        """What the block of n samples from c0 needs to stay on the line."""
        assert self.A is not None
        e = self.t_cap(c0) - self.line_ns(self.s)
        self.error_ns = e
        de = round(e * SR / NS)
        if e > FILL_MAX_NS:
            self.A = self.t_cap(c0) - self.s * NS // SR
            self.reanchors += 1
            self.correcting = False
            return Correction(reanchored=True)
        if e >= JUMP_NS:
            return Correction(fill=de)
        if e <= -JUMP_NS:
            return Correction(cut=min(n, -de))
        if abs(e) > TOL_START_NS:
            self.correcting = True
        elif abs(e) < TOL_STOP_NS:
            self.correcting = False
        if not self.correcting or de == 0:
            return Correction()
        per = max(1, round(n / SPF)) * (1 if abs(e) < MICRO_SMALL_NS else 2)
        k = min(abs(de), per)
        return Correction(micro=k if de > 0 else -k)

    def advance(self, m: int) -> None:
        """m samples went into the stream."""
        self.s += m

    def keep(self, now_ns: int) -> int:
        """How many zero samples to put into the stream now so that it is not behind: none until
        no samples came for KEEP_TRIGGER_NS, then up to KEEP_TARGET_NS behind now, to a whole
        packet, until a block with samples in it comes again (fed_real)."""
        if self.A is None:
            return 0
        lag = now_ns - self.line_ns(self.s)
        due = lag > KEEP_TARGET_NS + SPF_NS if self.keeping else lag > KEEP_TRIGGER_NS
        if not due:
            return 0
        self.keeping = True
        target = -((-(now_ns - KEEP_TARGET_NS - self.A) * SR) // NS)  # rounded up
        z = target - self.s
        z += (-(self.s + z)) % SPF
        return max(0, z)

    def fed_real(self) -> None:
        self.keeping = False

    def sync_point(self, n_sent: int, gap: int) -> tuple[int, int, int]:
        """(field1, NTP time, field3) of a sync sent when n_sent packets have gone: the next
        packet's first sample, its line time, and the sample heard then (gap samples before)."""
        s_n = n_sent * SPF
        return (s_n - gap) & 0xFFFFFFFF, to_ntp(self.line_ns(s_n), epoch=True), s_n & 0xFFFFFFFF
