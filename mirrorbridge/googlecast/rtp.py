"""Cast Streaming on the wire: encrypted frames in RTP packets, and RTCP both ways.

As Chrome sends it (media/cast/net/rtp/rtp_packetizer.cc), every frame is encrypted whole
with AES-128-CTR, the counter starting at the session's IV mask XOR the 32-bit frame id
(big-endian, in bytes 8 to 11), then cut into packets of nearly equal size. Each packet has
the 12-byte RTP header (the marker bit on the frame's last packet, the frame's RTP time) and
the 7-byte Cast header:

    byte 0   0x80 if a keyframe | 0x40 (a referenced frame id follows) | extensions (0)
    byte 1   the frame id's low 8 bits
    2-3      this packet's id in the frame
    4-5      the frame's last packet id
    6        the low 8 bits of the frame it refers to (itself for a keyframe)

The receiver sends RTCP back to the address the packets came from: a receiver report, Cast
feedback (the last frame it has whole, and the packets it misses), now and then a PLI (it
wants a keyframe), its event log. The sender sends a sender report every RTCP_SECONDS,
which ties RTP times to its clock, so the receiver knows when to show each frame.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAX_PACKET = 1500 - 28  # an Ethernet MTU, less the IP and UDP headers (Chrome's limit)
RTP_HEADER = 12
CAST_HEADER = 7
MAX_PAYLOAD = MAX_PACKET - RTP_HEADER - CAST_HEADER - 4  # Chrome keeps room for one extension
VIDEO_CLOCK = 90000
RTCP_SECONDS = 0.5

KEYFRAME_BIT = 0x80
REFERENCE_BIT = 0x40
MARKER_BIT = 0x80

PT_SR, PT_RR, PT_APP, PT_RTPFB, PT_PSFB, PT_XR = 200, 201, 204, 205, 206, 207
CAST = b"CAST"
CST2 = b"CST2"
ALL_PACKETS = 0xFFFF  # a loss field: every packet of the frame
LAST_PACKET = 0xFFFE  # a loss field: the frame's last packet


def is_rtcp(packet: bytes) -> bool:
    return len(packet) >= 8 and 194 <= packet[1] <= 210


class FrameCipher:
    """AES-128-CTR per frame, as transport_encryption_handler.cc does it."""

    def __init__(self, key: bytes, iv_mask: bytes) -> None:
        if len(key) != 16 or len(iv_mask) != 16:
            raise ValueError("the AES key and IV mask are 16 bytes each")
        self.key = key
        self.iv_mask = iv_mask

    def nonce(self, frame_id: int) -> bytes:
        counter = bytearray(8) + struct.pack(">I", frame_id & 0xFFFFFFFF) + bytearray(4)
        return bytes(a ^ b for a, b in zip(counter, self.iv_mask))

    def apply(self, frame_id: int, data: bytes) -> bytes:
        """Encrypts (and, being CTR, decrypts)."""
        cipher = Cipher(algorithms.AES(self.key), modes.CTR(self.nonce(frame_id))).encryptor()
        return cipher.update(data) + cipher.finalize()


class Packetizer:
    """Frames to RTP packets for one stream (one SSRC)."""

    def __init__(self, ssrc: int, payload_type: int, sequence: int = 0, max_payload: int = MAX_PAYLOAD) -> None:
        self.ssrc = ssrc
        self.payload_type = payload_type
        self.sequence = sequence & 0xFFFF
        self.max_payload = max_payload
        self.packets_sent = 0
        self.octets_sent = 0

    def packets(self, frame_id: int, rtp_time: int, data: bytes, keyframe: bool,
                referenced: Optional[int] = None) -> list[bytes]:
        """The packets of one (already encrypted) frame."""
        if not data:
            return []
        if referenced is None:
            referenced = frame_id if keyframe else frame_id - 1
        count = -(-len(data) // self.max_payload)
        size = -(-len(data) // count)  # split evenly, as Chrome does
        out = []
        byte0 = REFERENCE_BIT | (KEYFRAME_BIT if keyframe else 0)
        for packet_id in range(count):
            chunk = data[packet_id * size:(packet_id + 1) * size]
            last = packet_id == count - 1
            header = struct.pack(">BBHII", 0x80, self.payload_type | (MARKER_BIT if last else 0), self.sequence,
                                 rtp_time & 0xFFFFFFFF, self.ssrc)
            cast = struct.pack(">BBHHB", byte0, frame_id & 0xFF, packet_id, count - 1, referenced & 0xFF)
            self.sequence = (self.sequence + 1) & 0xFFFF
            out.append(header + cast + chunk)
            self.packets_sent += 1
            self.octets_sent += len(chunk)
        return out


def sender_report(ssrc: int, ntp: int, rtp_time: int, packets: int, octets: int) -> bytes:
    """RTCP SR without report blocks: our clock (64-bit NTP) and the RTP time it is now."""
    return struct.pack(">BBHIQIII", 0x80, PT_SR, 6, ssrc, ntp & 0xFFFFFFFFFFFFFFFF, rtp_time & 0xFFFFFFFF,
                       packets & 0xFFFFFFFF, octets & 0xFFFFFFFF)


# -- what the receiver sends ------------------------------------------------------------------


@dataclass
class CastFeedback:
    """The receiver has every frame up to `ack` (8 bits, expanded by the sender), and misses
    `losses`: (8-bit frame id, packet id or ALL_PACKETS / LAST_PACKET) pairs."""

    ack: int
    target_delay_ms: int
    losses: list[tuple[int, int]] = field(default_factory=list)
    received_later: list[int] = field(default_factory=list)  # 8-bit ids past `ack` that came whole


@dataclass
class Rtcp:
    """What one compound RTCP packet from the receiver said."""

    ssrc: Optional[int] = None
    feedback: Optional[CastFeedback] = None
    pli: bool = False
    report: Optional[dict] = None  # fraction lost, cumulative lost, jitter, last SR, delay since it
    kinds: list[int] = field(default_factory=list)


def _cast_feedback(body: bytes) -> Optional[CastFeedback]:
    """Cast feedback after the two SSRCs: 'CAST', ack, loss count, target delay, losses, maybe CST2."""
    if len(body) < 8 or body[:4] != CAST:
        return None
    ack, count, delay = body[4], body[5], struct.unpack_from(">H", body, 6)[0]
    pos = 8
    losses = []
    for _ in range(count):
        if pos + 4 > len(body):
            break
        frame, packet, mask = body[pos], struct.unpack_from(">H", body, pos + 1)[0], body[pos + 3]
        pos += 4
        losses.append((frame, packet))
        if packet not in (ALL_PACKETS, LAST_PACKET):
            losses.extend((frame, packet + 1 + bit) for bit in range(8) if mask >> bit & 1)
    later = []
    if body[pos:pos + 4] == CST2 and pos + 6 <= len(body):
        bits = body[pos + 5]
        vector = body[pos + 6:pos + 6 + bits]
        later = [(ack + 2 + index * 8 + bit) & 0xFF for index, byte in enumerate(vector)
                 for bit in range(8) if byte >> bit & 1]
    return CastFeedback(ack, delay, losses, later)


def parse_rtcp(packet: bytes) -> Rtcp:
    """A compound RTCP packet from the receiver. Unknown parts are skipped; a broken one ends it."""
    out = Rtcp()
    pos = 0
    while pos + 4 <= len(packet):
        first, kind, words = struct.unpack_from(">BBH", packet, pos)
        size = (words + 1) * 4
        if first >> 6 != 2 or pos + size > len(packet):
            break
        count = first & 0x1F
        body = packet[pos + 4:pos + size]
        out.kinds.append(kind)
        if len(body) >= 4 and out.ssrc is None:
            out.ssrc = struct.unpack_from(">I", body)[0]
        if kind == PT_RR and count and len(body) >= 28:
            _media, lost, high, jitter, last_sr, since = struct.unpack_from(">IIIIII", body, 4)
            out.report = {"fraction_lost": lost >> 24, "lost": lost & 0xFFFFFF, "highest": high,
                          "jitter": jitter, "last_sr": last_sr, "since_sr": since}
        elif kind == PT_PSFB and count == 15 and len(body) >= 8:
            out.feedback = _cast_feedback(body[8:]) or out.feedback
        elif kind == PT_PSFB and count == 1:
            out.pli = True
        pos += size
    return out


def expand(truncated: int, near: int) -> int:
    """A full frame id from its low 8 bits: the one closest to `near`."""
    candidate = (near & ~0xFF) | (truncated & 0xFF)
    if candidate - near > 128:
        candidate -= 256
    elif near - candidate > 128:
        candidate += 256
    return candidate
