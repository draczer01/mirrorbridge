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

The samples come from audio.LoopbackFeeder (the PC's sound, WASAPI loopback at 44.1 kHz in
blocks of 352) or from ToneFeeder (a beep at each whole second, for tests and for the TV's
first try). AudioSender packs, seals and sends the packets and the syncs, keeps the last 256
for the TV's resend requests, which come in to AudioControl, our audio control port. SoundKnobs
holds the test switches (MIRRORBRIDGE_MIRROR_SOUND_*), read once per session. MirrorSound puts it
together for one session (session.py starts it once the TV took the stream, and stops it before
the session says it stopped); whatever goes wrong with it stops the sound, never the video.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import struct
import threading
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping, Optional

from .. import audio as system_audio
from .audiopackets import (FORMATS, FRAME_BYTES, RING_SIZE, SPF, SPF_NS, SR, SYNC_FLAGS, AudioCipher, AudioFormat,
                           PacketRing, parse_resend_request, resend_answer, resend_missing, rtp_header, sync_packet)
from .clock import NS, now_ns, to_ntp

if TYPE_CHECKING:
    from .negotiation import AudioSetup

_LOGGER = logging.getLogger(__name__)

SOUND_BY_DEFAULT = False  # stage 1: the sound is on only when asked for (MIRRORBRIDGE_MIRROR_SOUND=1, mirror --sound)
SYNC_INTERVAL_S = 1.0  # a sync this often
WINDOW_NS = 2 * NS  # the capture base is the smallest offset of this long
STEP_WINDOW_NS = 250_000_000  # every block this long late by JUMP_NS: samples were lost, the base moves
JUMP_NS = 30_000_000  # off by this much: fill or cut instead of single samples
FILL_MAX_NS = 200_000_000  # behind by more than this: re-anchor the line instead of filling
TOL_START_NS = 3_000_000  # single-sample corrections start when the stream is this far off the line
TOL_STOP_NS = 1_000_000  # and stop when it is back within this
MICRO_SMALL_NS = 10_000_000  # below this, at most one sample in 352; above, two
KEEP_TICK_S = 0.010  # the keeper looks this often
KEEP_TRIGGER_NS = 100_000_000  # no samples for this long: the keeper sends zeros
KEEP_TARGET_NS = 10_000_000  # up to this far behind now
MIN_GAP_MS = 20  # the smallest sync gap the lead and the delay may make
MAX_PENDING_BLOCKS = 64  # blocks on their way to the event loop; more are dropped, and filled
CAPTURE_BUFFER_FRAMES = 4410  # WASAPI's buffer for the loopback recorder: 100 ms
SEND_BUFFER_LIMIT = 64 * 1024  # bytes waiting in the data socket's buffer: past it, packets are dropped
SEND_ERRORS_PER_S = 50  # the network refusing the sound this often in a second, for SEND_ERROR_SECONDS
SEND_ERROR_SECONDS = 5  # seconds in a row, stops it (a Wi-Fi reconnect takes less, and the picture rides it out)
RESEND_MAX = RING_SIZE  # packets one request may ask for (each at most once in audiopackets.RESEND_REPEAT_NS)
RESEND_PER_S = 2 * SR // SPF  # answers to resend requests a second at most, in all: twice the stream's own packets
RESEND_BURST = RESEND_MAX  # and at once, after a quiet while: a whole ring's worth
RESEND_LOG_NS = NS  # a resend request in the log at most this often (a TV may ask with each packet it gets)
LATE_MARGIN_NS = 10_000_000  # a packet sent with less time than this before it is heard is late
SOUND_DROP_SECONDS = 5  # a TV that closes the session this soon after the first sound packet
SOUND_ENDED_SECONDS = 2  # but this soon when it ended mirroring as its remote does: later, a person more likely did
SOUND_OK_SECONDS = 60  # sound sent this long without the TV ending the session: it takes it
TONE_HZ = 880  # the test tone: a 100 ms beep (TONE_SAMPLES) at each whole second, at -12 dBFS (TONE_LEVEL)
TONE_SAMPLES = 4410
TONE_LEVEL = 8191
TONE_RAMP = 220  # faded in and out over 5 ms
TONE_ORIGIN_NS = 0  # whole seconds from this time of the session clock (set_tone_origin)

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

    def sync_point(self, n_sent: int, gap: int, first_index: int = 0) -> tuple[int, int, int]:
        """(field1, NTP time, field3) of a sync sent when n_sent packets have gone: the next
        packet's first sample, its line time, and the sample heard then (gap samples before), as
        RTP times of a stream whose first packet was number first_index (a test switch)."""
        s_n = n_sent * SPF
        rtp = first_index * SPF + s_n
        return (rtp - gap) & 0xFFFFFFFF, to_ntp(self.line_ns(s_n), epoch=True), rtp & 0xFFFFFFFF


# -- packets on the wire ------------------------------------------------------------------------------


@dataclass
class SoundStats:
    """What one session's sound did, for the log and the session's report. Blocks are counted
    as blocks, everything from skipped to kept in samples."""

    packets: int = 0
    bytes: int = 0
    repeats: int = 0  # packets sent again for redundancy
    syncs: int = 0
    held: int = 0  # blocks captured while waiting for the first video frame
    overflow: int = 0  # blocks dropped on the way to the event loop
    skipped: int = 0
    filled: int = 0
    cut: int = 0
    inserted: int = 0
    deleted: int = 0
    kept: int = 0  # the keeper's zeros
    steps: int = 0
    reanchors: int = 0
    resend_requests: int = 0
    asked: int = 0  # packets asked for again
    resent: int = 0
    deduped: int = 0  # asked for again within 20 ms of being answered: not answered again
    missed: int = 0  # no longer held: the 8-byte answer
    limited: int = 0  # not answered: past RESEND_PER_S, or the control port's buffer full
    unknown_control: int = 0
    foreign_control: int = 0  # from another address than the TV's
    send_errors: int = 0
    send_dropped: int = 0  # not sent: the socket's buffer was full (a packet, or a resend answer)
    late: int = 0
    headroom_min_ms: Optional[float] = None
    error_ms: float = 0.0  # the last line error
    error_min_ms: Optional[float] = None
    error_max_ms: Optional[float] = None
    seq_wraps: int = 0

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def datagram_sender(transport, addr: tuple, limit: int = SEND_BUFFER_LIMIT) -> Callable[[bytes], bool]:
    """send_data for an AudioSender: sendto() on an unconnected transport, or False without
    sending when more than `limit` bytes already wait in its buffer (asyncio would hold them
    without end) or it is closing."""
    def send(data: bytes) -> bool:
        if transport.is_closing() or transport.get_write_buffer_size() > limit:
            return False
        transport.sendto(data, addr)
        return True
    return send


class AudioSender:
    """Makes the stream's packets from s16 BE samples, seals them (the AudioCipher), keeps the
    last RING_SIZE and sends them, sends the syncs it is given, and answers the TV's resend
    requests. It knows no socket and no event loop: send_data sends a data packet (False: it
    was dropped), send_control a datagram from our control port (a sync), and send_answer an
    answer to a resend request from it (False: it was dropped; send_control's by default)."""

    def __init__(self, fmt: AudioFormat, cipher: AudioCipher, *, ssrc: int, tv_ip: str,
                 send_data: Callable[[bytes], bool], send_control: Callable[[bytes, tuple], None],
                 control_addr: tuple[str, int], redundant: int = 0, sync_flags: int = SYNC_FLAGS,
                 start_index: int = 0, clock: Callable[[], int] = now_ns, stats: Optional[SoundStats] = None,
                 send_answer: Optional[Callable[[bytes, tuple], bool]] = None) -> None:
        self.fmt = fmt
        self.cipher = cipher
        self.ssrc = ssrc
        self.tv_ip = tv_ip
        self._send_data = send_data
        self._send_control = send_control
        self._send_answer = send_answer or (lambda data, addr: send_control(data, addr) is not False)
        self.control_addr = control_addr
        self.redundant = redundant
        self.sync_flags = sync_flags
        self.start_index = start_index  # a test switch: the first packet's number (65 500 for the seq wrap)
        self.index = start_index  # the next packet's number
        self._clock = clock
        self.stats = stats if stats is not None else SoundStats()
        self.ring = PacketRing()
        self._carry = b""  # samples short of a whole packet
        self._marker = True
        # Resend answers: how many may go now (RESEND_PER_S, up to RESEND_BURST) and when that
        # was worked out; when each seq no longer held was last answered; the log's last line.
        self._allowance = float(RESEND_BURST)
        self._allowance_ns: Optional[int] = None
        self._gone: dict[int, int] = {}
        self._logged_ns: Optional[int] = None
        self._unlogged = 0

    @property
    def carry(self) -> int:
        """Samples waiting for the next packet."""
        return len(self._carry) // 4

    def push(self, pcm_be: bytes) -> int:
        """Put samples into the stream; every whole packet's worth goes at once. Returns the
        packets made."""
        data = self._carry + pcm_be if self._carry else pcm_be
        made = 0
        while len(data) - made * FRAME_BYTES >= FRAME_BYTES:
            self._packet(data[made * FRAME_BYTES:(made + 1) * FRAME_BYTES])
            made += 1
        self._carry = data[made * FRAME_BYTES:]
        return made

    def mark_next(self) -> None:
        """Set the marker bit on the next packet (the first after a re-anchor)."""
        self._marker = True

    def _packet(self, chunk: bytes) -> None:
        index = self.index
        header = rtp_header(index, self.ssrc, self._marker, self.fmt.spf)
        self._marker = False
        packet = header + self.cipher.seal(index, header, self.fmt.payload(chunk))
        self.ring.put(index, packet)
        for earlier in range(max(self.start_index, index - self.redundant), index):
            again = self.ring.by_index(earlier)
            if again is not None:
                self._send(again)
                self.stats.repeats += 1
        self._send(packet)
        self.index += 1
        self.stats.packets += 1
        self.stats.bytes += len(packet)
        if index > self.start_index and index & 0xFFFF == 0:
            self.stats.seq_wraps += 1
            _LOGGER.info("Sound: seq wrapped to 0 at packet %d (%.1f s)", index,
                         (index - self.start_index) * SPF / SR)

    def _send(self, packet: bytes) -> None:
        if self._send_data(packet) is False:
            self.stats.send_dropped += 1

    def send_sync(self, point: tuple[int, int, int], first: bool) -> bytes:
        """Send a sync with (field1, NTP time, field3) from SampleLine.sync_point."""
        packet = sync_packet(first, *point, flags=self.sync_flags)
        self._send_control(packet, self.control_addr)
        self.stats.syncs += 1
        return packet

    def on_control(self, data: bytes, addr: tuple) -> None:
        """A datagram on our control port: from the TV, a request for lost packets. Each one
        still held goes again, each one gone gets iOS's 8-byte answer; neither again within
        20 ms of the last time, and RESEND_PER_S answers a second at most in all: a TV that
        asks for a gap again with each packet it gets, and never gets it (the answers lost too),
        is not to flood the network the picture goes over, nor the event loop it is sent from."""
        if addr[0] != self.tv_ip:
            self.stats.foreign_control += 1
            _LOGGER.debug("Sound control port: %d bytes from %s, not the TV", len(data), addr[0])
            return
        request = parse_resend_request(data)
        if request is None or request[1] == 0:
            self.stats.unknown_control += 1
            _LOGGER.debug("Sound control port: %d bytes from the TV that ask for nothing: %s", len(data),
                          data[:8].hex())
            return
        first, count = request[0], min(request[1], RESEND_MAX)
        self.stats.resend_requests += 1
        self.stats.asked += count
        newest = self.index - 1
        now = self._clock()
        self._allow(now)
        repeat_ns = self.ring.repeat_ns
        resent = gone = repeated = limited = 0
        for i in range(count):
            seq = (first + i) & 0xFFFF
            packet = self.ring.get(seq, newest)
            if packet is None:
                answered = self._gone.get(seq)
                if answered is not None and now - answered < repeat_ns:
                    repeated += 1
                    continue
            elif not self.ring.due(seq, newest, now):
                repeated += 1
                continue
            if self._allowance < 1:
                limited = count - i
                break
            if not self._send_answer(resend_answer(packet) if packet is not None else resend_missing(seq), addr):
                self.stats.send_dropped += 1
                limited = count - i
                break
            self._allowance -= 1
            if packet is None:
                self._gone[seq] = now
                gone += 1
            else:
                self.ring.mark_resent(seq, newest, now)
                resent += 1
        if len(self._gone) > 2 * RESEND_MAX:
            self._gone = {seq: at for seq, at in self._gone.items() if now - at < repeat_ns}
        self.stats.resent += resent
        self.stats.missed += gone
        self.stats.deduped += repeated
        self.stats.limited += limited
        self._unlogged += 1
        if self._logged_ns is None or now - self._logged_ns >= RESEND_LOG_NS:
            _LOGGER.debug("The TV asked for %d packets again from seq %d: %d sent, %d gone, %d answered just before, "
                          "%d over the limit%s", count, first, resent, gone, repeated, limited,
                          f" ({self._unlogged} requests since the last line)" if self._unlogged > 1 else "")
            self._logged_ns, self._unlogged = now, 0

    def _allow(self, now: int) -> None:
        """The resend answers that may go now: RESEND_PER_S more for each second since the last
        request, up to RESEND_BURST."""
        if self._allowance_ns is not None:
            self._allowance = min(float(RESEND_BURST),
                                  self._allowance + (now - self._allowance_ns) * RESEND_PER_S / NS)
        self._allowance_ns = now


class AudioControl(asyncio.DatagramProtocol):
    """Our audio control port, bound before SETUP (the type-96 stream names it as controlPort):
    the syncs and the resend answers go out from it, and the TV's resend requests that come in
    go to the attached AudioSender. It stays unconnected, so that an ICMP error neither stops it
    reading (Windows' Proactor) nor fails the next send (Linux)."""

    def __init__(self) -> None:
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.sender: Optional[AudioSender] = None
        self.early = 0  # datagrams before a sender was attached
        self.errors = 0
        self.last_error: Optional[Exception] = None

    @property
    def port(self) -> int:
        assert self.transport is not None
        return self.transport.get_extra_info("sockname")[1]

    def connection_made(self, transport) -> None:
        self.transport = transport

    def attach(self, sender: AudioSender) -> None:
        self.sender = sender

    def detach(self) -> None:
        self.sender = None

    def datagram_received(self, data: bytes, addr) -> None:
        if self.sender is None:
            self.early += 1
            _LOGGER.debug("Sound control port: %d bytes from %s before the sound started", len(data), addr[0])
            return
        try:
            self.sender.on_control(data, addr)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Sound control port: could not answer %d bytes from %s", len(data), addr[0])

    def send(self, data: bytes, addr: tuple) -> None:
        if self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(data, addr)

    def answer(self, data: bytes, addr: tuple) -> bool:
        """send() for an answer to a resend request: False, and nothing sent, while more than
        SEND_BUFFER_LIMIT waits in the socket's buffer (Windows' event loop sends one datagram
        at a time, and holds the rest without end). The syncs never wait on this."""
        transport = self.transport
        if transport is None or transport.is_closing() or transport.get_write_buffer_size() > SEND_BUFFER_LIMIT:
            return False
        transport.sendto(data, addr)
        return True

    def error_received(self, exc) -> None:
        self.errors += 1
        self.last_error = exc
        (_LOGGER.warning if self.errors == 1 else _LOGGER.debug)(
            "Sound control port %s: %s", self.port if self.transport else "?", exc)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None


# -- the test tone -------------------------------------------------------------------------------------

_beep: Optional[bytes] = None


def beep() -> bytes:
    """The beep, TONE_SAMPLES frames of s16 BE stereo: TONE_HZ at TONE_LEVEL, faded in and out
    over TONE_RAMP samples (a raised cosine), the same on both channels."""
    global _beep  # pylint: disable=global-statement
    if _beep is None:
        out = bytearray()
        for j in range(TONE_SAMPLES):
            edge = min(j, TONE_SAMPLES - 1 - j)
            ramp = 0.5 * (1 - math.cos(math.pi * edge / TONE_RAMP)) if edge < TONE_RAMP else 1.0
            value = round(TONE_LEVEL * math.sin(2 * math.pi * TONE_HZ * j / SR) * ramp)
            out += struct.pack(">hh", value, value)
        _beep = bytes(out)
    return _beep


def set_tone_origin(ns: int) -> None:
    """Beep at the whole seconds after `ns` of the session clock (the latency meter's start)
    instead of the clock's own; for tones started after this."""
    global TONE_ORIGIN_NS  # pylint: disable=global-statement
    TONE_ORIGIN_NS = ns


def tone_block(c0: int, n: int, t0: int, origin: int) -> bytes:
    """Samples c0 to c0 + n of a tone whose sample c is taken at t0 + c * NS // SR: a beep starts
    at the first sample at or after each whole second from `origin`; zeros elsewhere."""
    out = bytearray(4 * n)
    first = (t0 + c0 * NS // SR - origin) // NS - 1  # the second whose beep may still be playing
    last = (t0 + (c0 + n - 1) * NS // SR - origin) // NS
    sound = beep()
    for second in range(first, last + 1):
        start = -((-(origin + second * NS - t0) * SR) // NS)  # its first sample: rounded up
        if start < 0:
            continue  # it began before the tone did
        lo, hi = max(c0, start), min(c0 + n, start + TONE_SAMPLES)
        if lo < hi:
            out[4 * (lo - c0):4 * (hi - c0)] = sound[4 * (lo - start):4 * (hi - start)]
    return bytes(out)


class ToneFeeder(threading.Thread):
    """The test tone in place of the PC's sound: blocks of 352 samples, each handed to
    on_block(s16 BE bytes, 352, its time, fresh) when its last sample is due, stamped with that
    ideal time, so that the line needs no correction at all."""

    def __init__(self, on_block: Callable[[bytes, int, int, bool], None], *, clock: Callable[[], int] = now_ns,
                 sleep: Optional[Callable[[float], object]] = None) -> None:
        super().__init__(name="mirror-tone", daemon=True)
        self._on_block = on_block
        self._clock = clock
        self._stop_event = threading.Event()
        self._sleep = sleep if sleep is not None else self._stop_event.wait
        self.blocks = 0
        self.speaker_id = ""  # as LoopbackFeeder's: no device
        self.speaker_name = "the test tone"

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        origin = TONE_ORIGIN_NS
        t0 = self._clock()
        while not self._stop_event.is_set():
            due = t0 + (self.blocks + 1) * SPF * NS // SR
            wait = due - self._clock()
            if wait > 0:
                self._sleep(wait / NS)
                continue
            try:
                self._on_block(tone_block(self.blocks * SPF, SPF, t0, origin), SPF, due, self.blocks == 0)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("The test tone stopped: the sound took no more (%r)", ex)
                return
            self.blocks += 1


# -- one session's sound -------------------------------------------------------------------------------

NEW, HOLDING, RUNNING, STOPPED, FAILED = "new", "holding", "running", "stopped", "failed"


class _Halted(Exception):
    """The sound was stopped: the feeder's thread is to end."""


def _loopback_feeder(sink) -> threading.Thread:
    """The PC's sound: WASAPI loopback of the default output at 44.1 kHz, in blocks of one packet
    through a 100 ms buffer, each to `sink`."""
    return system_audio.LoopbackFeeder(sink=sink, samplerate=SR, block_frames=SPF,
                                       buffer_frames=CAPTURE_BUFFER_FRAMES, name="mirrorbridge-audio")


class _DataProtocol(asyncio.DatagramProtocol):
    """The data socket's end: it sends the packets to the TV's dataPort, reads nothing, and
    counts what the network refused."""

    def __init__(self, sound: "MirrorSound") -> None:
        self.sound = sound

    def datagram_received(self, data: bytes, addr) -> None:
        _LOGGER.debug("Sound data port: %d bytes from %s", len(data), addr[0])

    def error_received(self, exc) -> None:
        self.sound._send_error(exc)  # pylint: disable=protected-access


class MirrorSound:
    """One session's sound, from the moment the TV took the type-96 stream: the feeder's blocks
    put on the sample line, packed and sent (AudioSender) to the ports the TV named, with a
    sync a second and the keeper's zeros from a 10 ms ticker.

    It holds the samples back (they only move the line's base) until go(), when the first video
    frame has gone, unless MIRRORBRIDGE_MIRROR_SOUND_HOLD=0. Everything runs on the event loop
    but the feeder's sinks (_on_floats, _on_block), which only hand the blocks over, at most
    MAX_PENDING_BLOCKS at a time. Nothing here may stop or slow the video: whatever goes wrong
    stops the sound alone (fail, then on_failed)."""

    def __init__(self, setup: "AudioSetup", *, knobs: SoundKnobs, control: AudioControl, local_ip: str,
                 tv_ip: str, tone: bool, make_feeder: Optional[Callable[..., threading.Thread]] = None,
                 on_first_packet: Callable[[], None], on_failed: Callable[[str], None],
                 on_device_changed: Callable[[], None] = lambda: None, clock: Callable[[], int] = now_ns,
                 start_index: int = 0) -> None:
        self.setup = setup
        self.knobs = knobs
        self.control = control
        self.local_ip = local_ip
        self.tv_ip = tv_ip
        self.tone = tone
        self._make_feeder = make_feeder or _loopback_feeder
        self._on_first_packet = on_first_packet
        self._on_failed = on_failed
        self._on_device_changed = on_device_changed
        self._clock = clock
        self.start_index = start_index  # a test switch: the first packet's number
        self.gap = setup.gap
        self.state = NEW
        self.reason = ""  # why it failed
        self.stats = SoundStats()
        self.line = SampleLine()
        self.sender: Optional[AudioSender] = None
        self.feeder: Optional[threading.Thread] = None
        self.first_packet_ns: Optional[int] = None
        self.data_port = 0  # ours, the packets' source
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._ticker: Optional[asyncio.Future] = None
        self._halted = False  # read by the feeder's thread: it is to end
        # Blocks on their way to the loop, as two counters of one writer each (the feeder's
        # thread, the loop), so that neither loses the other's updates.
        self._queued = 0
        self._fed = 0
        self._dropped = 0  # samples dropped since the last block handed over (the feeder's)
        self._dropped_fresh = False
        self._speaker_id: Optional[str] = None
        self._went_ns: Optional[int] = None
        self._stopped_ns: Optional[int] = None
        self._data_errors = 0
        self._last_error: Optional[Exception] = None
        self._errors_since = 0  # the send errors' 1 s window: when it began, and the count then
        self._errors_at = 0
        self._refused_s = 0  # the windows in a row with SEND_ERRORS_PER_S errors or more
        self._window = (0, SoundStats(), 0)  # the log's window: when it began, the counts and send errors then
        self._window_error: Optional[tuple[float, float]] = None
        self._window_headroom: Optional[float] = None

    # -- starting and stopping (the loop) --------------------------------------------------------

    async def start(self) -> None:
        """Open the data socket, take the control port's requests, then start the feeder (last,
        once nothing can be cancelled any more). Safe to stop() at any point of it."""
        loop = self._loop = asyncio.get_running_loop()
        setup = self.setup
        transport, _ = await loop.create_datagram_endpoint(lambda: _DataProtocol(self),
                                                           local_addr=(self.local_ip, 0))
        if self.state != NEW:  # stopped meanwhile
            transport.close()
            return
        self._transport = transport
        self.data_port = transport.get_extra_info("sockname")[1]
        ssrc = setup.stream_id & 0xFFFFFFFF if self.knobs.ssrc == "id" else 0
        self.sender = AudioSender(setup.format, setup.cipher, ssrc=ssrc, tv_ip=self.tv_ip,
                                  send_data=datagram_sender(transport, (self.tv_ip, setup.data_port)),
                                  send_control=self.control.send, control_addr=(self.tv_ip, setup.control_port),
                                  redundant=setup.redundant, sync_flags=self.knobs.sync_flags,
                                  start_index=self.start_index, clock=self._clock, stats=self.stats,
                                  send_answer=self.control.answer)
        self.control.attach(self.sender)
        now = self._clock()
        self._errors_since, self._errors_at = now, self._errors()
        self._window = (now, dataclasses.replace(self.stats), self._errors_at)
        self.state = HOLDING
        _LOGGER.info("Sound: %s to %s:%d (control %s:%d), gap %d samples (%.0f ms), latencies %d/%d, %s, from UDP %d%s",
                     setup.format.name, self.tv_ip, setup.data_port, self.tv_ip, setup.control_port, self.gap,
                     self.gap * 1000 / SR, setup.lat_min, setup.lat_max, setup.cipher.name, self.data_port,
                     "" if self.knobs.hold else ", not waiting for the first video frame")
        self.feeder = ToneFeeder(self._on_block) if self.tone else self._make_feeder(sink=self._on_floats)
        self.feeder.start()
        if not self.knobs.hold:
            self.go()

    def go(self) -> None:
        """The first video frame went: the next block anchors the line, and the sound goes."""
        if self.state == HOLDING:
            self.state = RUNNING
            self._went_ns = self._clock()

    def stop(self) -> None:
        """Stop the sound at once (and as often as asked): the feeder is told to end, the ticker
        is cancelled, nothing more goes to the TV. The stats stay."""
        self._halted = True
        if self.state not in (STOPPED, FAILED):
            self.state = STOPPED
        if self._stopped_ns is None:
            self._stopped_ns = self._clock()
        feeder = self.feeder
        if feeder is not None:
            feeder.stop()  # type: ignore[attr-defined]
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()
        if self.sender is not None and self.control.sender is self.sender:
            self.control.detach()
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def fail(self, reason: str) -> None:
        """Stop the sound for `reason` and say so (on_failed); the video goes on."""
        if self.state in (STOPPED, FAILED):
            return
        self.reason = reason
        self.state = FAILED
        self.stop()
        try:
            self._on_failed(reason)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Sound failure callback failed")

    # -- the feeder's thread ----------------------------------------------------------------------

    def _on_floats(self, arr, arrived_ns: int, fresh: bool) -> None:
        """LoopbackFeeder's sink: its float32 frames as s16 BE."""
        if self._halted:
            raise _Halted
        self._on_block(floats_to_s16be(arr), len(arr), arrived_ns, fresh)

    def _on_block(self, pcm_be: bytes, n: int, arrived_ns: int, fresh: bool) -> None:
        """A block of n samples that came at arrived_ns (fresh: the first of a new recorder), on
        to the loop. Past MAX_PENDING_BLOCKS on the way it is dropped, and the line fills the gap
        it leaves. Raising ends the feeder."""
        if self._halted:
            raise _Halted
        if self._queued - self._fed >= MAX_PENDING_BLOCKS:
            self._dropped += n
            self._dropped_fresh = self._dropped_fresh or fresh
            self.stats.overflow += 1
            return
        dropped, dropped_fresh = self._dropped, self._dropped_fresh
        self._dropped, self._dropped_fresh = 0, False
        self._queued += 1
        assert self._loop is not None
        try:
            self._loop.call_soon_threadsafe(self._feed, pcm_be, n, arrived_ns, fresh or dropped_fresh, dropped)
        except RuntimeError as ex:  # the loop is closed: the session is long over
            raise _Halted from ex

    # -- the loop ----------------------------------------------------------------------------------

    def _feed(self, pcm_be: bytes, n: int, arrived_ns: int, fresh: bool, skipped: int) -> None:
        """One block on the line, corrected to fit it, into the stream."""
        try:
            self._fed += 1
            if self.state not in (HOLDING, RUNNING):
                return
            line = self.line
            if skipped and not fresh:
                line.skip(skipped)
                self.stats.skipped += skipped
            c0 = line.observe(n, arrived_ns, fresh)
            self.stats.steps = line.steps
            if fresh:
                self._speaker(getattr(self.feeder, "speaker_id", ""), getattr(self.feeder, "speaker_name", ""))
            if self.state == HOLDING:
                self.stats.held += 1
                return
            sender = self.sender
            assert sender is not None
            if line.A is None:
                line.start(c0)
                sender.send_sync(self._sync_point(), first=True)
                self._ticker = asyncio.ensure_future(self._tick())
            corr = line.correction(c0, n)
            self._error(line.error_ns)
            if corr.reanchored:
                self.stats.reanchors = line.reanchors
                _LOGGER.info("Sound: the capture was %.0f ms behind the line: anchored again", line.error_ns / 1e6)
                sender.mark_next()
                sender.send_sync(self._sync_point(), first=True)
            out = apply(pcm_be, corr)
            m = len(out) // 4
            cut = min(corr.cut, n)
            net = m - (n - cut) - corr.fill  # the single samples, as apply really made them
            self.stats.filled += corr.fill
            self.stats.cut += cut
            self.stats.inserted += max(0, net)
            self.stats.deleted += max(0, -net)
            line.advance(m)
            if m:
                line.fed_real()
            self._pushed(sender.push(out))
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("The sound failed on a block", exc_info=True)
            self.fail(repr(ex))

    def _speaker(self, speaker_id: str, name: str) -> None:
        """A new recorder: on another output device than before, the speakers may need muting
        again (on_device_changed)."""
        before, self._speaker_id = self._speaker_id, speaker_id
        if before is None or before == speaker_id:
            return
        _LOGGER.info("Sound: now from %s", name or speaker_id or "another device")
        try:
            self._on_device_changed()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Sound device callback failed")

    def _sync_point(self) -> tuple[int, int, int]:
        assert self.sender is not None
        return self.line.sync_point(self.sender.index - self.start_index, self.gap, self.start_index)

    def _error(self, error_ns: int) -> None:
        ms = error_ns / 1e6
        stats = self.stats
        stats.error_ms = ms
        stats.error_min_ms = ms if stats.error_min_ms is None else min(stats.error_min_ms, ms)
        stats.error_max_ms = ms if stats.error_max_ms is None else max(stats.error_max_ms, ms)
        low, high = self._window_error or (ms, ms)
        self._window_error = (min(low, ms), max(high, ms))

    def _pushed(self, made: int) -> None:
        """`made` packets just went: how long before they are heard (rule A's time, an
        indicator), and the first packet's callback."""
        if not made:
            return
        assert self.sender is not None
        now = self._clock()
        first = (self.sender.index - made - self.start_index) * SPF
        heard = self.line.line_ns(first) + self.gap * NS // SR
        for i in range(made):
            headroom = heard + i * SPF * NS // SR - now
            if headroom < LATE_MARGIN_NS:
                self.stats.late += 1
            ms = headroom / 1e6
            if self.stats.headroom_min_ms is None or ms < self.stats.headroom_min_ms:
                self.stats.headroom_min_ms = ms
            if self._window_headroom is None or ms < self._window_headroom:
                self._window_headroom = ms
        if self.first_packet_ns is None:
            self.first_packet_ns = now
            try:
                self._on_first_packet()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Sound first-packet callback failed")

    async def _tick(self) -> None:
        """Every KEEP_TICK_S, the keeper's zeros (a gap in the capture); every SYNC_INTERVAL_S, a
        sync. On deadlines of the session clock: asyncio's timers may fire early (by up to 15.6 ms
        on Windows) or late."""
        tick, interval = round(KEEP_TICK_S * NS), round(SYNC_INTERVAL_S * NS)
        start = self._clock()
        next_tick, next_sync = start + tick, start + interval
        while self.state == RUNNING:
            await asyncio.sleep(max(0, next_tick - self._clock()) / NS)
            now = self._clock()
            next_tick = next_tick + tick if next_tick + tick > now else now + tick
            try:
                sender = self.sender
                assert sender is not None
                zeros = self.line.keep(now)
                if zeros:
                    self.line.advance(zeros)
                    self.stats.kept += zeros
                    self._pushed(sender.push(bytes(4 * zeros)))
                if now >= next_sync:
                    sender.send_sync(self._sync_point(), first=False)
                    next_sync = next_sync + interval if next_sync + interval > now else now + interval
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("The sound's ticker failed", exc_info=True)
                self.fail(repr(ex))
                return

    def _send_error(self, exc: Exception) -> None:
        self._data_errors += 1
        self._last_error = exc
        (_LOGGER.warning if self._data_errors == 1 else _LOGGER.debug)("Sound data port %d: %s", self.data_port, exc)

    def _errors(self) -> int:
        return self._data_errors + self.control.errors

    # -- from the session's supervisor -----------------------------------------------------------

    @property
    def pending(self) -> int:
        """Blocks on their way to the loop."""
        return self._queued - self._fed

    def check(self, now_ns: int) -> None:
        """The feeder must still run, and the network must not have refused the sound
        SEND_ERRORS_PER_S times a second for SEND_ERROR_SECONDS in a row; otherwise the sound
        stops (fail). A shorter while (a Wi-Fi reconnect, the route gone for a moment) is
        ridden out as the picture's connection rides it out: the packets go on into the ring,
        on their line, and what the TV missed it may ask for again."""
        if self.state not in (HOLDING, RUNNING):
            return
        feeder = self.feeder
        if feeder is not None and not feeder.is_alive():
            error = getattr(feeder, "error", None)
            self.fail(f"the sound capture ended ({error!r})" if error else "the sound capture ended")
            return
        if now_ns - self._errors_since < NS:
            return
        errors = self._errors()
        refused, self._errors_since, self._errors_at = errors - self._errors_at, now_ns, errors
        last = self._last_error or self.control.last_error
        if refused < SEND_ERRORS_PER_S:
            if self._refused_s:
                _LOGGER.info("Sound: the network takes it again, after %d s", self._refused_s)
            self._refused_s = 0
            return
        self._refused_s += 1
        if self._refused_s == 1:
            _LOGGER.info("Sound: the network refused it %d times in 1 s (%s); going on", refused, last)
        if self._refused_s >= SEND_ERROR_SECONDS:
            self.fail(f"the network refused the sound {refused} times in 1 s, for {self._refused_s} s ({last})")

    def log_window(self, now_ns: int) -> None:
        """The sound's figures since the last call, for the log (with the session's every 5 s)."""
        if self.state == NEW:
            return
        began, before, errors_before = self._window
        stats = self.stats
        errors = self._errors()
        self._window = (now_ns, dataclasses.replace(stats), errors)
        error, self._window_error = self._window_error, None
        headroom, self._window_headroom = self._window_headroom, None
        if self._went_ns is None:
            _LOGGER.info("Sound: waiting for the first video frame (%d blocks held), queue %d", stats.held,
                         self.pending)
            return
        seconds = max(1e-9, (now_ns - began) / NS)
        packets = stats.packets - before.packets

        def ms(field: str) -> float:
            return (getattr(stats, field) - getattr(before, field)) * 1000 / SR

        low, high = error or (stats.error_ms, stats.error_ms)
        _LOGGER.info("Sound: %d packets (%.1f/s), line error %+.1f ms (%+.1f..%+.1f), corrections +%d/-%d, "
                     "filled %.0f ms, kept %.0f ms, cut %.0f ms, resends %d/%d asked (%d gone, %d repeated, %d over "
                     "the limit), headroom min %s ms (%d late), queue %d, send errors %d", packets, packets / seconds,
                     stats.error_ms, low, high, stats.inserted - before.inserted, stats.deleted - before.deleted,
                     ms("filled"), ms("kept"), ms("cut"), stats.resent - before.resent, stats.asked - before.asked,
                     stats.missed - before.missed, stats.deduped - before.deduped, stats.limited - before.limited,
                     f"{headroom:.0f}" if headroom is not None else "-", stats.late - before.late, self.pending,
                     errors - errors_before)

    def final_text(self) -> str:
        """How the sound ended, for the session's report."""
        if self.state == FAILED:
            return f"stopped: {self.reason}"
        if self._went_ns is None:
            return "held: the picture never went"
        if self.first_packet_ns is None:
            return "sent: 0 packets in 0.0 s"
        end = self._stopped_ns or self._clock()
        return f"sent: {self.stats.packets} packets in {(end - self.first_packet_ns) / NS:.1f} s"
