"""Reads the H.264 that FFmpeg writes as FLV to a pipe.

FLV rather than raw H.264 (Annex-B): every tag says how long it is, so a frame is complete the
moment its last byte arrives, whereas an Annex-B frame only ends when the next one starts
(measured with x264 at 1080p30 on 4 cores: a median 13 ms from a frame going into FFmpeg to it
being complete in the pipe with FLV, 45 ms with Annex-B). A tag also carries the frame's
timestamp in ms, a keyframe flag and, at the start, FFmpeg's avcC.

Layout (Adobe FLV 10.1, E.4): a 9-byte header "FLV" 01 flags offset, then for each tag
PreviousTagSize (u32), type (8 audio, 9 video, 18 script), data size (u24), timestamp (u24 plus
an upper byte), stream id (u24) and the data. A video tag's data starts with frame type and
codec (7 = AVC), AVCPacketType (0 avcC, 1 NAL units, 2 end of sequence) and a composition time
offset (SI24, ms). The enhanced form (bit 7 of the first byte set, FourCC "avc1") is read too.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

TAG_VIDEO = 9
CODEC_AVC = 7
CONFIG, FRAME, END = "config", "frame", "end"
_ENHANCED_KINDS = {0: CONFIG, 1: FRAME, 2: END, 3: FRAME}  # 3: frames without a composition time


class FlvError(ValueError):
    """What FFmpeg wrote is not FLV with H.264 in it."""


@dataclass
class VideoTag:
    kind: str  # CONFIG (data is avcC), FRAME (NAL units with lengths) or END
    dts_ms: int
    pts_ms: int
    keyframe: bool
    data: bytes


class FlvReader:
    """Feed it the pipe's bytes as they come; it returns each video tag once it is complete."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._header_done = False
        self.tags = 0

    def feed(self, chunk: bytes) -> list[VideoTag]:
        self._buf += chunk
        out: list[VideoTag] = []
        buf = self._buf
        pos = 0
        if not self._header_done:
            if len(buf) < 9:
                return out
            if buf[:3] != b"FLV":
                raise FlvError("the stream does not start with an FLV header")
            pos = struct.unpack_from(">I", buf, 5)[0] + 4  # header, then PreviousTagSize0
            if len(buf) < pos:
                return out
            self._header_done = True
        while len(buf) - pos >= 11:
            kind = buf[pos] & 0x1F
            size = int.from_bytes(buf[pos + 1:pos + 4], "big")
            if len(buf) - pos < 11 + size + 4:
                break
            timestamp = int.from_bytes(buf[pos + 4:pos + 7], "big") | (buf[pos + 7] << 24)
            if timestamp & 0x80000000:
                timestamp -= 1 << 32
            data = bytes(buf[pos + 11:pos + 11 + size])
            pos += 11 + size + 4
            self.tags += 1
            if kind == TAG_VIDEO and data:
                tag = _video_tag(data, timestamp)
                if tag is not None:
                    out.append(tag)
        if pos:
            del buf[:pos]
        return out


def _si24(data: bytes) -> int:
    value = int.from_bytes(data, "big")
    return value - (1 << 24) if value & 0x800000 else value


def _video_tag(data: bytes, timestamp: int) -> Optional[VideoTag]:
    first = data[0]
    if first & 0x80:  # enhanced RTMP/FLV: FourCC codec, packet type in the low bits
        keyframe = (first >> 4) & 0x07 == 1
        packet = first & 0x0F
        if data[1:5] != b"avc1":
            raise FlvError(f"FFmpeg sent {bytes(data[1:5])!r} video, not H.264")
        kind = _ENHANCED_KINDS.get(packet)
        if kind is None:
            return None  # metadata and other packet types
        if packet == 1:
            return VideoTag(kind, timestamp, timestamp + _si24(data[5:8]), keyframe, data[8:])
        return VideoTag(kind, timestamp, timestamp, keyframe, data[5:])
    if first & 0x0F != CODEC_AVC:
        raise FlvError(f"FFmpeg sent FLV video codec {first & 0x0F}, not H.264")
    if len(data) < 5:
        return None
    keyframe = first >> 4 == 1
    kind = {0: CONFIG, 1: FRAME, 2: END}.get(data[1])
    if kind is None:
        return None
    return VideoTag(kind, timestamp, timestamp + _si24(data[2:5]), keyframe, data[5:])


def write_header() -> bytes:
    """An FLV header with video only (for tests and tools)."""
    return b"FLV\x01\x01" + struct.pack(">I", 9) + struct.pack(">I", 0)


def write_video_tag(kind: str, timestamp_ms: int, data: bytes, keyframe: bool = False, cts_ms: int = 0) -> bytes:
    """One AVC video tag and its PreviousTagSize (for tests and tools)."""
    packet = {CONFIG: 0, FRAME: 1, END: 2}[kind]
    body = bytes([(0x10 if keyframe or kind == CONFIG else 0x20) | CODEC_AVC, packet])
    body += (cts_ms & 0xFFFFFF).to_bytes(3, "big") + data
    ts = timestamp_ms & 0xFFFFFFFF
    header = bytes([TAG_VIDEO]) + len(body).to_bytes(3, "big") + (ts & 0xFFFFFF).to_bytes(3, "big")
    header += bytes([ts >> 24]) + b"\x00\x00\x00"
    return header + body + struct.pack(">I", 11 + len(body))
