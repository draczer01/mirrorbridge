"""What goes over the mirroring data connection: 128-byte headers, codec packets, encrypted frames.

Every message is a 128-byte little-endian header, sent in the clear, then its payload:

    [0:4]   payload size (with the 16-byte tag of an encrypted frame)
    [4]     type: 0 video frame, 1 codec packet (avcC), 2 heartbeat, 5 streaming report
    [5]     0x10 on a frame that is an IDR
    [6:8]   16 01 on an H.264 codec packet, 1e 00 on a heartbeat, 00 00 on a frame
    [8:16]  when to show it: the session clock in NTP form, without the 1900 epoch (clock.py)
    [16:24] codec packet: width and height, float32 (again at [40:48] and [56:64])
    [40:48] frame: the timeline it is on (0 for NTP timing)

The codec packet goes out before the first IDR, and again whenever the parameter sets change,
with the timestamp of the IDR that follows it (receivers drop one whose time differs).

Frames are encrypted with ChaCha20-Poly1305 on receivers paired the HomeKit way (TVs of other
brands): the key is HKDF-SHA512 of the pair-verify secret with salt "DataStream-Salt" followed by
the video stream's streamConnectionID in decimal and info "DataStream-Output-Encryption-Key";
the nonce is 4 zero bytes and a little-endian 64-bit count of frames (0, 1, 2...); the whole
header is the additional data, and the 16-byte tag follows the ciphertext. Codec packets and
heartbeats stay in the clear.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Callable, Optional

from pyatv.support.chacha20 import Chacha20Cipher8byteNonce

HEADER_SIZE = 128
TAG_SIZE = 16

VIDEO, CODEC, HEARTBEAT, REPORT = 0, 1, 2, 5
IDR_FLAG = 0x10
_SUBTYPES = {VIDEO: b"\x00\x00", CODEC: b"\x16\x01", HEARTBEAT: b"\x1e\x00", REPORT: b"\x00\x00"}
_SIZE_OFFSETS = (16, 40, 56)

DATASTREAM_SALT = "DataStream-Salt"  # followed by the streamConnectionID in decimal
DATASTREAM_OUTPUT_INFO = "DataStream-Output-Encryption-Key"
DATASTREAM_INPUT_INFO = "DataStream-Input-Encryption-Key"

# (salt, output info, input info) -> (output key, input key), from pair-verify (control.py)
KeyDeriver = Callable[[str, str, str], tuple[bytes, bytes]]


@dataclass
class Header:
    kind: int
    size: int = 0
    timestamp: int = 0  # NTP form, no epoch
    idr: bool = False
    width: float = 0.0
    height: float = 0.0
    timeline: int = 0
    subtype: bytes = b""

    def pack(self) -> bytes:
        out = bytearray(HEADER_SIZE)
        struct.pack_into("<IBB", out, 0, self.size, self.kind, IDR_FLAG if self.idr else 0)
        out[6:8] = self.subtype or _SUBTYPES.get(self.kind, b"\x00\x00")
        struct.pack_into("<Q", out, 8, self.timestamp)
        if self.kind == CODEC:
            for offset in _SIZE_OFFSETS:
                struct.pack_into("<ff", out, offset, self.width, self.height)
        elif self.kind == VIDEO:
            struct.pack_into("<Q", out, 40, self.timeline)
        return bytes(out)

    @classmethod
    def parse(cls, data: bytes) -> "Header":
        if len(data) < HEADER_SIZE:
            raise ValueError("a mirroring header is 128 bytes")
        size, kind, flags = struct.unpack_from("<IBB", data, 0)
        timestamp = struct.unpack_from("<Q", data, 8)[0]
        width = height = 0.0
        timeline = 0
        if kind == CODEC:
            width, height = struct.unpack_from("<ff", data, 16)
        elif kind == VIDEO:
            timeline = struct.unpack_from("<Q", data, 40)[0]
        return cls(kind, size, timestamp, bool(flags & IDR_FLAG), width, height, timeline, bytes(data[6:8]))


# Experiment: MIRRORBRIDGE_ON_ARRIVAL=1 sends codec packets with 14 01 at [6:8], without 0x02 of [6]
# (RespectTimestamps), so that the TV shows each frame as it arrives, not at its timestamp.
ON_ARRIVAL = os.environ.get("MIRRORBRIDGE_ON_ARRIVAL") == "1"
_CODEC_ON_ARRIVAL = b"\x14\x01"


def codec_packet(avcc: bytes, timestamp: int, width: int, height: int, on_arrival: Optional[bool] = None) -> bytes:
    subtype = _CODEC_ON_ARRIVAL if (ON_ARRIVAL if on_arrival is None else on_arrival) else b""
    return Header(CODEC, len(avcc), timestamp, width=float(width), height=float(height),
                  subtype=subtype).pack() + avcc


def heartbeat_packet(timestamp: int) -> bytes:
    return Header(HEARTBEAT, 0, timestamp).pack()


def video_key(derive: KeyDeriver, stream_connection_id: int) -> bytes:
    """The ChaCha20-Poly1305 key of the video stream with this streamConnectionID."""
    key, _ = derive(f"{DATASTREAM_SALT}{stream_connection_id}", DATASTREAM_OUTPUT_INFO, DATASTREAM_INPUT_INFO)
    return key


def nonce(counter: int) -> bytes:
    """The 12-byte nonce of the frame with this number: 4 zero bytes, then LE64(counter)."""
    return struct.pack("<LQ", 0, counter)


class VideoCipher:
    """Encrypts frames in order, counting them for the nonce (pyatv's ChaCha20, MIT)."""

    def __init__(self, key: bytes) -> None:
        self._cipher = Chacha20Cipher8byteNonce(key, key)
        self.frames = 0

    @property
    def next_nonce(self) -> bytes:
        return self._cipher.out_nonce

    def video_packet(self, payload: bytes, timestamp: int, idr: bool, timeline: int = 0) -> bytes:
        """Header and encrypted frame, ready to send: the header (with the tag counted in its
        size) is the additional data."""
        header = Header(VIDEO, len(payload) + TAG_SIZE, timestamp, idr, timeline=timeline).pack()
        self.frames += 1
        return header + self._cipher.encrypt(payload, aad=header)


class VideoDecipher:
    """The receiver's side of VideoCipher (for tests and the loopback receiver)."""

    def __init__(self, key: bytes) -> None:
        self._cipher = Chacha20Cipher8byteNonce(key, key)

    def open(self, header: bytes, payload: bytes) -> bytes:
        return self._cipher.decrypt(payload, aad=header[:HEADER_SIZE])
