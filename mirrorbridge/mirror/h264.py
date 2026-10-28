"""H.264 as a mirroring receiver wants it: NAL units, the SPS fields that matter, and avcC.

A receiver gets the stream's parameter sets once, as an AVCDecoderConfigurationRecord (avcC,
ISO/IEC 14496-15) in a codec packet, and then each frame as one access unit of NAL units with
4-byte big-endian lengths ("AVCC" form), without start codes, access unit delimiters, SEI,
filler or parameter sets of its own.

Which ending of avcC a TV accepts is not known for every receiver: after the PPS, the ISO form
(what FFmpeg writes) has 4 more bytes for the High profiles, fd f8 f8 00 for 4:2:0 8-bit video
and nothing for the others; some senders end every record with 02 00 00 00 instead. Both are
here (TRAILERS), and a device's working one is saved (mirror.profiles).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# NAL unit types (H.264 table 7-1)
NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9
NAL_END_SEQUENCE = 10
NAL_END_STREAM = 11
NAL_FILLER = 12

# Kept out of a frame: the receiver has the parameter sets from the codec packet, and the rest
# only costs bytes (or confuses its parser).
_NOT_IN_FRAMES = {NAL_SEI, NAL_SPS, NAL_PPS, NAL_AUD, NAL_END_SEQUENCE, NAL_END_STREAM, NAL_FILLER}

TRAILER_ISO = "fdf8f800"  # ISO/IEC 14496-15, as FFmpeg writes it: High profiles only
TRAILER_FIXED = "02000000"  # appended to every record
TRAILERS = (TRAILER_ISO, TRAILER_FIXED)

# Profiles whose avcC carries chroma format and bit depths (ISO/IEC 14496-15 5.3.3.1.2)
_HIGH_PROFILES = {100, 110, 122, 144}
# Profiles whose SPS carries them (H.264 7.3.2.1.1)
_CHROMA_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


def nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F


def split_annexb(data: bytes) -> list[bytes]:
    """NAL units of an Annex-B stream (start codes 00 00 01 or 00 00 00 01)."""
    nals = []
    start = data.find(b"\x00\x00\x01")
    while start != -1:
        begin = start + 3
        nxt = data.find(b"\x00\x00\x01", begin)
        end = len(data) if nxt == -1 else nxt
        if nxt != -1 and data[end - 1] == 0:
            end -= 1  # the leading zero of a 4-byte start code
        nal = data[begin:end]
        if nxt == -1:
            nal = nal.rstrip(b"\x00") or nal  # trailing_zero_8bits
        if nal:
            nals.append(bytes(nal))
        start = nxt
    return nals


def split_avcc(data: bytes, length_size: int = 4) -> list[bytes]:
    """NAL units of length-prefixed ("AVCC") data. Raises ValueError if a length runs past the end."""
    nals = []
    pos = 0
    view = memoryview(data)
    while pos < len(data):
        if pos + length_size > len(data):
            raise ValueError("truncated NAL length")
        size = int.from_bytes(view[pos:pos + length_size], "big")
        pos += length_size
        if pos + size > len(data):
            raise ValueError("NAL unit runs past the end of the access unit")
        if size:
            nals.append(bytes(view[pos:pos + size]))
        pos += size
    return nals


def join_avcc(nals: list[bytes]) -> bytes:
    """NAL units with 4-byte big-endian lengths, as a mirroring frame carries them."""
    return b"".join(struct.pack(">I", len(nal)) + nal for nal in nals)


def to_annexb(nals: list[bytes]) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)


def frame_nals(nals: list[bytes]) -> tuple[list[bytes], list[bytes], list[bytes]]:
    """Split an access unit into (the NAL units to send, its SPSs, its PPSs)."""
    keep, sps, pps = [], [], []
    for nal in nals:
        kind = nal_type(nal)
        if kind == NAL_SPS:
            sps.append(nal)
        elif kind == NAL_PPS:
            pps.append(nal)
        elif kind not in _NOT_IN_FRAMES:
            keep.append(nal)
    return keep, sps, pps


def is_idr(nals: list[bytes]) -> bool:
    return any(nal_type(nal) == NAL_IDR for nal in nals)


# -- SPS ---------------------------------------------------------------------------------------


def _unescape(rbsp: bytes) -> bytes:
    """Remove emulation prevention bytes (00 00 03 -> 00 00)."""
    if b"\x00\x00\x03" not in rbsp:
        return rbsp
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _Bits:
    """Big-endian bit reader with Exp-Golomb codes (H.264 9.1)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self.pos >> 3
            if byte >= len(self.data):
                raise ValueError("SPS ends too early")
            value = (value << 1) | ((self.data[byte] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad Exp-Golomb code in SPS")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        code = self.ue()
        return (code + 1) // 2 if code & 1 else -(code // 2)


@dataclass
class Sps:
    """What a sequence parameter set says about the picture."""

    profile_idc: int
    constraints: int
    level_idc: int
    chroma_format_idc: int
    bit_depth_luma: int
    bit_depth_chroma: int
    width: int
    height: int


def parse_sps(nal: bytes) -> Sps:
    """Read an SPS NAL unit (with its header byte). Raises ValueError if it is not one."""
    if not nal or nal_type(nal) != NAL_SPS or len(nal) < 4:
        raise ValueError("not an SPS")
    bits = _Bits(_unescape(nal[1:]))
    profile, constraints, level = bits.u(8), bits.u(8), bits.u(8)
    bits.ue()  # seq_parameter_set_id
    chroma, separate_planes, depth_luma, depth_chroma = 1, 0, 8, 8
    if profile in _CHROMA_PROFILES:
        chroma = bits.ue()
        if chroma == 3:
            separate_planes = bits.u(1)
        depth_luma = bits.ue() + 8
        depth_chroma = bits.ue() + 8
        bits.u(1)  # qpprime_y_zero_transform_bypass_flag
        if bits.u(1):  # seq_scaling_matrix_present_flag
            for index in range(8 if chroma != 3 else 12):
                if bits.u(1):
                    last = following = 8
                    for _ in range(16 if index < 6 else 64):
                        if following:
                            following = (last + bits.se() + 256) % 256
                        last = following or last
    bits.ue()  # log2_max_frame_num_minus4
    poc_type = bits.ue()
    if poc_type == 0:
        bits.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif poc_type == 1:
        bits.u(1)
        bits.se()
        bits.se()
        for _ in range(bits.ue()):
            bits.se()
    bits.ue()  # max_num_ref_frames
    bits.u(1)  # gaps_in_frame_num_value_allowed_flag
    width_mbs = bits.ue() + 1
    height_units = bits.ue() + 1
    frame_mbs_only = bits.u(1)
    if not frame_mbs_only:
        bits.u(1)  # mb_adaptive_frame_field_flag
    bits.u(1)  # direct_8x8_inference_flag
    crop = (0, 0, 0, 0)
    if bits.u(1):  # frame_cropping_flag
        crop = (bits.ue(), bits.ue(), bits.ue(), bits.ue())
    if separate_planes or chroma == 0:
        unit_x, unit_y = 1, 2 - frame_mbs_only
    else:
        unit_x = 2 if chroma in (1, 2) else 1
        unit_y = (2 if chroma == 1 else 1) * (2 - frame_mbs_only)
    width = width_mbs * 16 - unit_x * (crop[0] + crop[1])
    height = (2 - frame_mbs_only) * height_units * 16 - unit_y * (crop[2] + crop[3])
    return Sps(profile, constraints, level, chroma, depth_luma, depth_chroma, width, height)


# -- avcC --------------------------------------------------------------------------------------


def avcc_record(sps: bytes, pps: bytes, trailer: str = TRAILER_ISO) -> bytes:
    """The AVCDecoderConfigurationRecord for one SPS and one PPS, with 4-byte NAL lengths."""
    if trailer not in TRAILERS:
        raise ValueError(f"unknown avcC trailer {trailer!r}")
    info = parse_sps(sps)
    record = bytes([1, info.profile_idc, info.constraints, info.level_idc, 0xFC | 3, 0xE0 | 1])
    record += struct.pack(">H", len(sps)) + sps + b"\x01" + struct.pack(">H", len(pps)) + pps
    if trailer == TRAILER_FIXED:
        record += bytes.fromhex(TRAILER_FIXED)
    elif info.profile_idc in _HIGH_PROFILES:
        record += bytes([0xFC | info.chroma_format_idc, 0xF8 | (info.bit_depth_luma - 8),
                         0xF8 | (info.bit_depth_chroma - 8), 0])
    return record


@dataclass
class AvcConfig:
    sps: list[bytes]
    pps: list[bytes]
    length_size: int


def parse_avcc(record: bytes) -> AvcConfig:
    """The parameter sets of an avcC record (FFmpeg's, in an FLV sequence header, or ours)."""
    if len(record) < 7 or record[0] != 1:
        raise ValueError("not an avcC record")
    length_size = (record[4] & 3) + 1
    pos = 5
    sets: list[list[bytes]] = []
    for count_mask in (0x1F, 0xFF):
        if pos >= len(record):
            raise ValueError("avcC record ends too early")
        count = record[pos] & count_mask
        pos += 1
        found = []
        for _ in range(count):
            size = struct.unpack_from(">H", record, pos)[0]
            pos += 2
            if pos + size > len(record):
                raise ValueError("avcC parameter set runs past the end")
            found.append(bytes(record[pos:pos + size]))
            pos += size
        sets.append(found)
    return AvcConfig(sets[0], sets[1], length_size)
