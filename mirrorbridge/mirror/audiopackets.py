"""What goes over the screen-audio (type 96) stream: the codec frames, the encrypted RTP packets,
the syncs and the answers to the TV's resend requests.

Each packet carries 352 samples of 44.1 kHz 16-bit stereo, as the SETUP announced it:

* ALAC "verbatim" frames (the format's uncompressed escape), built here bit by bit: a 23-bit
  header (element tag 001 for a channel pair, instance 0, 12 zero bits, has_size, 2 bits of
  shift, escape 1), with has_size a 32-bit sample count, then the samples L0 R0 L1 R1 ... as
  16-bit two's complement, the END tag 111 and zero bits to a whole byte: 1412 bytes, or 1416
  with the count. FFmpeg's ALAC decoder and Apple's take them bit for bit.
* or the samples as they are (PCM, big-endian as pyatv sends them, or little-endian as a test).

The data packet, sent over UDP to the TV's dataPort:

    [0]     0x80
    [1]     0x60, or 0xE0 with the marker bit (the first packet, and the first after a re-anchor)
    [2:4]   seq, the packet's number (16 bits, big-endian)
    [4:8]   RTP time: the number of its first sample (32 bits)
    [8:12]  SSRC (0, as Apple's own mirroring sender)
    [12:]   what the AudioCipher seals: with the SETUP's shk, ChaCha20-Poly1305 with the nonce
            4 zero bytes and LE64(packet number), [4:12] of the header as the additional data,
            then the 16-byte tag and the nonce's last 8 bytes, as shairport-sync opens it

A sync (from our control port to the TV's, in the clear, 20 bytes) ties a sample to a time:
0x90 (the first, and after a re-anchor) or 0x80, 0xD4, two bytes of flags, the sample the TV plays
at the time that follows (field1), that time on the session clock in NTP form with the 1900
epoch, and the RTP time of the next packet to go (field3).

The TV asks for lost packets with 0x80 0xD5, any 2 bytes, the first seq and a count; each one
we still hold is sent again behind 0x80 0xD6 and its own seq, and one we no longer hold is
answered with the 8 bytes iOS sends then: 80 D6 00 01, the seq, 00 00.

Nothing here touches a socket or the event loop (sound.py does).
"""

from __future__ import annotations

import array
import math
import os
import struct
from dataclasses import dataclass
from typing import Optional, Protocol

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SR, SPF, CHANNELS = 44100, 352, 2
FRAME_BYTES = SPF * 4  # 1408 bytes of s16 BE stereo per packet
SPF_NS = SPF * 1_000_000_000 // SR  # 7_981_859
MASK32 = 0xFFFFFFFF
HEADER_SIZE = 12

RING_SIZE = 256  # packets kept for resends (about 2 s)
RESEND_REPEAT_NS = 20_000_000  # a packet is sent again at most once in this time

# ALAC's header for a channel pair, verbatim: tag 001, instance 0, 12 zero bits, has_size 0,
# shift 00, escape 1 (23 bits); ALAC_SIZED_HEADER has has_size 1.
ALAC_HEADER = (1 << 20) | 1
ALAC_SIZED_HEADER = (1 << 20) | (1 << 3) | 1
ALAC_END = 7

SYNC = 0xD4
RESEND_REQUEST = 0x55
RESEND_ANSWER = 0xD6
SYNC_FLAGS = 0x0004  # Apple's mirroring sender; owntone and pyatv send 0x0007


def ms_to_samples(ms: float) -> int:
    """Milliseconds as a whole number of samples at 44.1 kHz, rounded down."""
    return math.floor(ms * SR / 1000 + 1e-9)


def alac_verbatim(be: bytes) -> bytes:
    """352 frames of s16 BE stereo (1408 bytes) as a 1412-byte ALAC frame without its size."""
    if len(be) != FRAME_BYTES:
        raise ValueError(f"an ALAC frame without its size holds {FRAME_BYTES} bytes, not {len(be)}")
    v = (ALAC_HEADER << (len(be) * 8)) | int.from_bytes(be, "big")
    return (((v << 3) | ALAC_END) << 6).to_bytes(len(be) + 4, "big")


def alac_verbatim_sized(be: bytes) -> bytes:
    """Frames of s16 BE stereo as an ALAC frame that says how many there are (has_size, a 32-bit
    count): 1416 bytes for 352, which a decoder takes whatever frame length its cookie names."""
    if len(be) % 4:
        raise ValueError("s16 stereo comes in frames of 4 bytes")
    n = len(be) // 4
    v = (((ALAC_SIZED_HEADER << 32) | n) << (n * 32)) | int.from_bytes(be, "big")
    return (((v << 3) | ALAC_END) << 6).to_bytes(len(be) + 8, "big")


def swap16(be: bytes) -> bytes:
    """Each 16-bit sample with its two bytes the other way round (big- to little-endian)."""
    samples = array.array("h")
    samples.frombytes(be)
    samples.byteswap()
    return samples.tobytes()


@dataclass(frozen=True)
class AudioFormat:
    """How the stream's samples are packed, and how the type-96 SETUP names it (ct, audioFormat)."""

    name: str
    ct: int
    audio_format: int
    sized: bool = False  # ALAC with has_size
    little_endian: bool = False  # PCM
    sr: int = SR
    spf: int = SPF

    def payload(self, be: bytes) -> bytes:
        """One packet's 1408 bytes of s16 BE stereo as this format sends them."""
        if self.ct == 2:
            return alac_verbatim_sized(be) if self.sized else alac_verbatim(be)
        return swap16(be) if self.little_endian else bytes(be)


ALAC_44100 = AudioFormat("ALAC", 2, 0x40000)
ALAC_44100_SIZED = AudioFormat("ALAC with its size", 2, 0x40000, sized=True)
PCM_44100 = AudioFormat("PCM", 1, 0x800)
PCM_44100_LE = AudioFormat("PCM little-endian", 1, 0x800, little_endian=True)
FORMATS = {"alac": ALAC_44100, "alac-sized": ALAC_44100_SIZED, "pcm": PCM_44100, "pcmle": PCM_44100_LE}

# Bits of GET /info's supportedFormats.screenStream (owntone's table): the formats a stream
# with usingScreen may use.
SCREEN_ALAC_BIT, SCREEN_PCM_BIT = 0x40000, 0x800


def choose_format(screen_formats: Optional[int], knob: str = "") -> tuple[AudioFormat, str]:
    """The format to send and why: the one `knob` names (MIRRORBRIDGE_MIRROR_SOUND_CODEC), else
    ALAC if the TV's screenStream lists it, PCM if it lists only that, and ALAC otherwise."""
    if knob in FORMATS:
        return FORMATS[knob], f"{FORMATS[knob].name}, as MIRRORBRIDGE_MIRROR_SOUND_CODEC={knob} asks"
    if screen_formats is None:
        return ALAC_44100, "ALAC: the TV did not list its screen sound formats"
    if screen_formats & SCREEN_ALAC_BIT:
        return ALAC_44100, f"ALAC: screenStream {screen_formats:#x} lists it"
    if screen_formats & SCREEN_PCM_BIT:
        return PCM_44100, f"PCM: screenStream {screen_formats:#x} lists PCM but not ALAC"
    return ALAC_44100, f"screenStream {screen_formats:#x} lists neither ALAC nor PCM: sending ALAC anyway"


def rtp_header(index: int, ssrc: int = 0, marker: bool = False, spf: int = SPF) -> bytes:
    """The 12-byte header of packet number `index` (seq and RTP time wrap as they must)."""
    return struct.pack(">BBHII", 0x80, 0xE0 if marker else 0x60, index & 0xFFFF, (index * spf) & MASK32,
                       ssrc & MASK32)


def sync_packet(first: bool, field1: int, ntp: int, field3: int, flags: int = SYNC_FLAGS) -> bytes:
    """A 20-byte sync: sample `field1` is played at `ntp` (session clock, NTP form with the 1900
    epoch); `field3` is the RTP time of the next packet to go."""
    return struct.pack(">BBHIQI", 0x90 if first else 0x80, SYNC, flags, field1 & MASK32, ntp, field3 & MASK32)


def parse_resend_request(data: bytes) -> Optional[tuple[int, int]]:
    """(first seq, count) of the TV's request for lost packets, or None if `data` is not one."""
    if len(data) < 8 or data[1] & 0x7F != RESEND_REQUEST:
        return None
    first, count = struct.unpack_from(">HH", data, 4)
    return first, count


def resend_answer(packet: bytes) -> bytes:
    """A packet sent again: 0x80 0xD6 and its own seq in front of it, as it went the first time."""
    return b"\x80\xd6" + packet[2:4] + packet


def resend_missing(seq: int) -> bytes:
    """The answer for a packet no longer held (iOS's)."""
    return b"\x80\xd6\x00\x01" + struct.pack(">H", seq & 0xFFFF) + b"\x00\x00"


class AudioCipher(Protocol):
    """What seals the packets, and what the TV must be told of its key in SETUP."""

    name: str  # for the log
    overhead: int  # bytes seal() adds to the payload

    def stream_fields(self) -> dict:
        """Fields for the type-96 stream's SETUP dict."""

    def session_fields(self) -> dict:
        """Fields for the session's first SETUP body."""

    def seal(self, index: int, header: bytes, payload: bytes) -> bytes:
        """What follows the 12-byte header of packet number `index`."""


class ChaChaShk:
    """ChaCha20-Poly1305 with the 32-byte key the type-96 SETUP gives the TV (shk)."""

    name = "ChaCha20-Poly1305 (shk)"
    overhead = 24  # the tag and the nonce's last 8 bytes

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"the shk is 32 bytes, not {len(key)}")
        self.key = bytes(key)
        self._aead = ChaCha20Poly1305(self.key)

    @classmethod
    def for_channel(cls, channel=None) -> "ChaChaShk":
        """A new random key for one SETUP. The control channel is not needed for it."""
        return cls(os.urandom(32))

    def stream_fields(self) -> dict:
        return {"shk": self.key}

    def session_fields(self) -> dict:
        return {}

    def seal(self, index: int, header: bytes, payload: bytes) -> bytes:
        nonce = struct.pack("<LQ", 0, index)
        return self._aead.encrypt(nonce, payload, header[4:12]) + nonce[4:]


class PacketRing:
    """The last RING_SIZE packets sent, by their number, for the TV's resend requests."""

    def __init__(self, size: int = RING_SIZE, repeat_ns: int = RESEND_REPEAT_NS) -> None:
        self.size = size
        self.repeat_ns = repeat_ns
        self._slots: list[Optional[list]] = [None] * size  # [index, packet, when last sent again]

    def put(self, index: int, packet: bytes) -> None:
        self._slots[index % self.size] = [index, packet, None]

    def _slot(self, index: int) -> Optional[list]:
        if index < 0:
            return None
        slot = self._slots[index % self.size]
        return slot if slot is not None and slot[0] == index else None

    def _find(self, seq: int, newest: int) -> Optional[list]:
        """The slot of the newest packet up to `newest` whose seq is `seq`, if still held."""
        index = newest - ((newest - seq) & 0xFFFF)
        if newest - index >= self.size:
            return None
        return self._slot(index)

    def get(self, seq: int, newest: int) -> Optional[bytes]:
        slot = self._find(seq, newest)
        return slot[1] if slot is not None else None

    def by_index(self, index: int) -> Optional[bytes]:
        slot = self._slot(index)
        return slot[1] if slot is not None else None

    def due(self, seq: int, newest: int, now: int) -> bool:
        """Whether the packet with this seq may go again at `now`: held, and not sent again less
        than repeat_ns ago (a TV that asks again at once)."""
        slot = self._find(seq, newest)
        return slot is not None and (slot[2] is None or now - slot[2] >= self.repeat_ns)

    def mark_resent(self, seq: int, newest: int, now: int) -> bool:
        """Note that the packet with this seq goes again at `now`. False if it may not (due)."""
        slot = self._find(seq, newest)
        if slot is None or (slot[2] is not None and now - slot[2] < self.repeat_ns):
            return False
        slot[2] = now
        return True
