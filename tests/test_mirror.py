"""Screen mirroring's pieces, against fixed vectors, an independent implementation of the
cryptography, and FFmpeg's own output (tests/test_mirror_loopback.py has whole sessions)."""

import asyncio
import json
import logging
import random
import socket
import struct
import subprocess
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pyatv.auth.hap_srp import hkdf_expand

from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.mirror import capture, clock, flv, h264, packets, profiles, source, stats
from mirrorbridge.mirror.control import ReceiverInfo, Target, parse_features, sender_id
from mirrorbridge.mirror.datachannel import DataChannel
from mirrorbridge.mirror.errors import no_timing, refused
from mirrorbridge.mirror.negotiation import CONTROL_FIRST, MEDIA_FIRST, PTP, Plan
from mirrorbridge.mirror import session as mirror_session
from mirrorbridge.mirror.session import (MirrorSession, catch_up_bytes, canvas_limit, choose_canvas, choose_fps,
                                         choose_mirror_fps, keyframes_on_demand, keyframes_text, latency_lead,
                                         lead_override, mirror_latency, on_arrival, send_buffer)
from mirrorbridge.mirror.source import DESKTOP, TEST_PATTERN, FrameQueue, Unit, build_command

# What x264 wrote for a 640x360 test pattern (High, level 3.0), and the record the TV got for it.
SPS_640x360 = bytes.fromhex("6764001eacb405017fcb8088000003000800000301e478b175")
PPS_640x360 = bytes.fromhex("68ef3cb0")
AVCC_640x360 = bytes.fromhex("0164001effe100196764001eacb405017fcb8088000003000800000301e478b17501000468ef3cb0")


def _ffmpeg() -> str:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264"):
        pytest.skip("this FFmpeg has no libx264")
    return ff


def _ffmpeg_flv(size: str = "96x54", frames: int = 10, extra: tuple[str, ...] = ()) -> bytes:
    """FLV with H.264 in it, as the mirroring source makes it (High profile, no B-frames)."""
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30",
           "-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-profile:v", "high", "-bf", "0",
           "-g", "5", *extra, "-flvflags", "no_duration_filesize+no_metadata", "-f", "flv", "pipe:1"]
    return subprocess.run(cmd, check=True, capture_output=True, timeout=60).stdout


# -- avcC and SPS ----------------------------------------------------------------------------------


def test_sps_of_a_cropped_640x360_picture():
    sps = h264.parse_sps(SPS_640x360)
    assert (sps.width, sps.height) == (640, 360)  # 23 rows of macroblocks, 8 lines cropped
    assert (sps.profile_idc, sps.level_idc, sps.chroma_format_idc) == (100, 30, 1)
    assert (sps.bit_depth_luma, sps.bit_depth_chroma) == (8, 8)


def test_avcc_with_each_trailer():
    iso = h264.avcc_record(SPS_640x360, PPS_640x360, h264.TRAILER_ISO)
    fixed = h264.avcc_record(SPS_640x360, PPS_640x360, h264.TRAILER_FIXED)
    assert iso == AVCC_640x360 + bytes.fromhex("fdf8f800")
    assert fixed == AVCC_640x360 + bytes.fromhex("02000000")
    config = h264.parse_avcc(fixed)
    assert config.sps == [SPS_640x360] and config.pps == [PPS_640x360] and config.length_size == 4
    with pytest.raises(ValueError):
        h264.avcc_record(SPS_640x360, PPS_640x360, "00")


def test_avcc_is_ffmpegs_own_for_a_high_profile_stream():
    tags = flv.FlvReader().feed(_ffmpeg_flv())
    record = tags[0].data
    assert tags[0].kind == flv.CONFIG
    config = h264.parse_avcc(record)
    sps = h264.parse_sps(config.sps[0])
    assert (sps.width, sps.height, sps.profile_idc) == (96, 54, 100)
    assert h264.avcc_record(config.sps[0], config.pps[0], h264.TRAILER_ISO) == record
    assert h264.avcc_record(config.sps[0], config.pps[0], h264.TRAILER_FIXED)[:-4] == record[:-4]


def test_nal_units_in_and_out_of_frames():
    sei, idr = b"\x06\x05\x01\x80", b"\x65\x88\x84\x21"
    data = h264.join_avcc([sei, SPS_640x360, PPS_640x360, idr])
    nals = h264.split_avcc(data)
    keep, sps, pps = h264.frame_nals(nals)
    assert keep == [idr] and sps == [SPS_640x360] and pps == [PPS_640x360] and h264.is_idr(keep)
    assert h264.split_annexb(h264.to_annexb(nals)) == nals
    with pytest.raises(ValueError):
        h264.split_avcc(data[:-1])


# -- FLV ----------------------------------------------------------------------------------------------


def test_flv_reader_on_ffmpeg_output_in_any_chunks():
    data = _ffmpeg_flv(frames=12)
    rng = random.Random(7)
    reader, tags, pos = flv.FlvReader(), [], 0
    while pos < len(data):
        step = rng.randint(1, 700)
        tags += reader.feed(data[pos:pos + step])
        pos += step
    assert [t.kind for t in tags][:2] == [flv.CONFIG, flv.FRAME]
    frames = [t for t in tags if t.kind == flv.FRAME]
    assert len(frames) == 12
    assert [t.keyframe for t in frames] == [i % 5 == 0 for i in range(12)]
    assert [t.pts_ms for t in frames] == sorted(t.pts_ms for t in frames)
    assert frames[1].pts_ms - frames[0].pts_ms in (33, 34)
    for tag in frames:
        nals = h264.split_avcc(tag.data)
        assert h264.is_idr(nals) == tag.keyframe


def test_flv_reader_composition_time_negative_timestamps_and_errors():
    reader = flv.FlvReader()
    data = flv.write_header() + flv.write_video_tag(flv.CONFIG, 0, AVCC_640x360)
    data += flv.write_video_tag(flv.FRAME, 40, h264.join_avcc([b"\x65\x00"]), keyframe=True, cts_ms=-7)
    data += flv.write_video_tag(flv.FRAME, (1 << 32) - 5, h264.join_avcc([b"\x41\x00"]))
    tags = reader.feed(data)
    assert [(t.kind, t.dts_ms, t.pts_ms, t.keyframe) for t in tags] == [
        (flv.CONFIG, 0, 0, True), (flv.FRAME, 40, 33, True), (flv.FRAME, -5, -5, False)]
    with pytest.raises(flv.FlvError):
        flv.FlvReader().feed(b"MP4" + bytes(20))
    with pytest.raises(flv.FlvError):
        flv.FlvReader().feed(flv.write_header() + bytes([9, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0x12, 0]) + bytes(4))


def test_flv_reader_enhanced_avc_tags():
    body = bytes([0x80 | 0x10 | 1]) + b"avc1" + (-3 & 0xFFFFFF).to_bytes(3, "big") + b"\x00\x00\x00\x02\x65\x00"
    tag = bytes([9]) + len(body).to_bytes(3, "big") + (100).to_bytes(3, "big") + bytes(4) + body
    tags = flv.FlvReader().feed(flv.write_header() + tag + struct.pack(">I", 11 + len(body)))
    assert [(t.kind, t.pts_ms, t.keyframe) for t in tags] == [(flv.FRAME, 97, True)]
    assert tags[0].data == b"\x00\x00\x00\x02\x65\x00"


# -- packets and encryption ----------------------------------------------------------------------------


def test_codec_header_layout():
    header = packets.Header(packets.CODEC, 44, 0x0000000500000000, width=1920.0, height=1080.0).pack()
    assert len(header) == 128
    assert header[:8] == bytes.fromhex("2c000000 01 00 1601")
    assert header[8:16] == bytes.fromhex("0000000005000000")
    for offset in (16, 40, 56):
        assert header[offset:offset + 8] == bytes.fromhex("0000f044 00008744")  # 1920.0f, 1080.0f
    parsed = packets.Header.parse(header)
    assert (parsed.kind, parsed.size, parsed.timestamp, parsed.width, parsed.height) == (1, 44, 5 << 32, 1920, 1080)


def test_codec_packets_can_ask_for_frames_on_arrival():
    """More options > Debug > Show frames on arrival clears RespectTimestamps (0x02 of [6]), and
    nothing else."""
    at_their_time = packets.codec_packet(AVCC_640x360, 5 << 32, 640, 360)
    on_arrival = packets.codec_packet(AVCC_640x360, 5 << 32, 640, 360, on_arrival=True)
    assert at_their_time[6:8] == bytes.fromhex("1601") and on_arrival[6:8] == bytes.fromhex("1401")
    assert on_arrival[:6] + on_arrival[8:] == at_their_time[:6] + at_their_time[8:]
    assert packets.Header.parse(on_arrival).subtype == bytes.fromhex("1401")


def test_frame_and_heartbeat_headers():
    header = packets.Header(packets.VIDEO, 1016, 0x123456789ABCDEF0, idr=True).pack()
    assert header[:8] == bytes.fromhex("f8030000 00 10 0000")
    assert header[8:16] == struct.pack("<Q", 0x123456789ABCDEF0)
    assert header[16:] == bytes(112)  # timeline 0 at [40:48]
    parsed = packets.Header.parse(header)
    assert parsed.idr and parsed.timeline == 0 and parsed.size == 1016
    assert packets.heartbeat_packet(0)[:8] == bytes.fromhex("00000000 02 00 1e00")
    with pytest.raises(ValueError):
        packets.Header.parse(header[:100])


def test_nonce_counts_frames_little_endian():
    assert packets.nonce(0) == bytes(12)
    assert packets.nonce(1) == bytes.fromhex("00000000 0100000000000000")
    assert packets.nonce(0x0102030405) == bytes.fromhex("00000000 0504030201000000")
    cipher = packets.VideoCipher(bytes(32))
    assert cipher.next_nonce == packets.nonce(0)
    cipher.video_packet(b"x", 0, False)
    cipher.video_packet(b"y", 0, False)
    assert cipher.next_nonce == packets.nonce(2)


def _derive(shared: bytes):
    return lambda salt, out_info, in_info: (hkdf_expand(salt, out_info, shared), hkdf_expand(salt, in_info, shared))


def test_video_key_is_hkdf_sha512_of_the_pair_verify_secret():
    shared = bytes(range(32))
    stream_id = 7316947103129214234
    expected = HKDF(hashes.SHA512(), 32, f"DataStream-Salt{stream_id}".encode(),
                    b"DataStream-Output-Encryption-Key").derive(shared)
    assert packets.video_key(_derive(shared), stream_id) == expected
    assert packets.video_key(_derive(shared), stream_id + 1) != expected


def test_frames_are_chacha20_poly1305_with_the_header_as_aad():
    key = bytes(range(100, 132))
    cipher, decipher = packets.VideoCipher(key), packets.VideoDecipher(key)
    for counter, payload in enumerate([b"\x00\x00\x00\x02\x65\x88", b"\x00\x00\x00\x03\x41\x9a\x00" * 50]):
        packet = cipher.video_packet(payload, 1000 + counter, idr=counter == 0)
        header, body = packet[:128], packet[128:]
        assert packets.Header.parse(header).size == len(payload) + 16 == len(body)
        assert body == ChaCha20Poly1305(key).encrypt(packets.nonce(counter), payload, header)
        assert decipher.open(header, body) == payload


# -- the clock -------------------------------------------------------------------------------------------


def test_ntp_values():
    assert clock.to_ntp(1_500_000_000) == (1 << 32) | (1 << 31)
    assert clock.to_ntp(0, epoch=True) == clock.NTP_EPOCH_SECONDS << 32
    for ns in (0, 1, 999_999_999, 123_456_789_012_345):
        assert abs(clock.from_ntp(clock.to_ntp(ns)) - ns) <= 1
        assert abs(clock.from_ntp(clock.to_ntp(ns, True), True) - ns) <= 1


def test_ntp_reply_echoes_the_request():
    request = struct.pack(">BBHI", 0x80, 0xD2, 0x1234, 0xA1B2C3D4) + bytes(16) + bytes.fromhex("0102030405060708")
    reply = clock.ntp_reply(request, 5 * clock.NS, 5 * clock.NS + 250_000_000)
    assert reply[:8] == bytes.fromhex("80d31234a1b2c3d4") and len(reply) == 32  # its sequence number comes back
    assert reply[8:16] == bytes.fromhex("0102030405060708")
    received, sent = struct.unpack(">QQ", reply[16:])
    assert received == (clock.NTP_EPOCH_SECONDS + 5) << 32
    assert sent == ((clock.NTP_EPOCH_SECONDS + 5) << 32) | (1 << 30)
    assert clock.ntp_reply(b"\x80\xd3" + bytes(30), 0) is None
    assert clock.ntp_reply(request[:20], 0) is None


def _free_udp_ports(size: int = 10) -> range:
    """A run of UDP ports free right now, for the tests of bind_udp. Not TIMING_PORTS: other
    tests (or test processes, with pytest -n) mirroring at the same time hold some of those."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            start = probe.getsockname()[1]
        ports = range(start, start + size)
        if ports.stop > 65536:
            continue
        try:
            for port in ports:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        return ports
    pytest.skip("no run of free UDP ports")


async def test_ntp_responder_over_udp():
    loop = asyncio.get_running_loop()
    ports = _free_udp_ports()
    transport, responder = await clock.bind_udp(clock.NtpResponder, "127.0.0.1", ports)
    answers: asyncio.Queue = asyncio.Queue()

    class Asker(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            answers.put_nowait(data)

    asker, _ = await loop.create_datagram_endpoint(Asker, local_addr=("127.0.0.1", 0))
    try:
        assert responder.port in ports
        before = clock.now_ns()
        request = clock.ntp_request(before)
        asker.sendto(request, ("127.0.0.1", responder.port))
        reply = await asyncio.wait_for(answers.get(), 5)
        assert reply[1] == clock.NTP_REPLY and reply[8:16] == request[24:32]
        received = clock.from_ntp(struct.unpack(">Q", reply[16:24])[0], epoch=True)
        assert before <= received <= clock.now_ns()
        assert responder.requests == 1 and responder.arrived.is_set()
        # Its own probe requests to a receiver's port, and the answers to them, are not counted.
        responder.probe_receiver("127.0.0.1", asker.get_extra_info("sockname")[1], count=1)
        probe = await asyncio.wait_for(answers.get(), 5)
        assert probe[1] == clock.NTP_REQUEST
        asker.sendto(clock.ntp_reply(probe, clock.now_ns()), ("127.0.0.1", responder.port))
        await asyncio.sleep(0.05)
        assert responder.requests == 1
    finally:
        asker.close()
        transport.close()


async def test_bind_udp_takes_the_next_free_port_of_the_range():
    assert clock.bind_udp.__defaults__[0] == clock.TIMING_PORTS == range(7880, 7890)  # the firewall rule's
    ports = _free_udp_ports()
    first, _ = await clock.bind_udp(asyncio.DatagramProtocol, "127.0.0.1", ports)
    second, _ = await clock.bind_udp(asyncio.DatagramProtocol, "127.0.0.1", ports)
    try:
        bound = [first.get_extra_info("sockname")[1], second.get_extra_info("sockname")[1]]
        assert all(p in ports for p in bound) and bound[0] != bound[1]
    finally:
        first.close()
        second.close()


def test_pts_mapper_takes_the_fastest_frame_of_the_window():
    mapper = clock.PtsMapper(window_ns=2 * clock.NS)
    base = 10 * clock.NS
    ms = 1_000_000
    # Frames every 33 ms that arrive 40 ms after they were taken, plus 0-15 ms of jitter.
    jitter = [5, 0, 12, 3, 15, 1, 7]
    mapped = [mapper.map(i * 33, base + i * 33 * ms + 40 * ms + j * ms) for i, j in enumerate(jitter)]
    assert mapped == [base + i * 33 * ms + 40 * ms + (5 if i == 0 else 0) * ms for i in range(len(jitter))]
    assert mapper.offset_ns == base + 40 * ms
    # The delay grows for good (a slower encoder): after the window the new delay is taken up.
    later = [mapper.map(i * 33, base + i * 33 * ms + 60 * ms) for i in range(7, 7 + 80)]
    assert later[-1] == base + 86 * 33 * ms + 60 * ms
    assert all(b > a for a, b in zip(mapped + later, (mapped + later)[1:]))


def test_pts_mapper_sees_a_delay_that_stays():
    mapper, ms = clock.PtsMapper(window_ns=2 * clock.NS), 1_000_000
    for i in range(10):
        mapper.map(i * 33, i * 33 * ms + 40 * ms)
    assert mapper.behind(10 * 33, 10 * 33 * ms + 40 * ms) == 0
    # FFmpeg starts holding frames back: 300 ms more, for good. The window takes it in as normal...
    for i in range(10, 200):
        taken = mapper.map(i * 33, i * 33 * ms + 340 * ms)
    assert i * 33 * ms + 340 * ms - taken == 0
    # ...but measured against the session's best, the frame is 300 ms behind.
    assert mapper.behind(i * 33, i * 33 * ms + 340 * ms) == 300 * ms


def test_pts_mapper_never_goes_backwards():
    mapper = clock.PtsMapper()
    first = mapper.map(100, 1_000_000_000)
    assert mapper.map(100, 1_000_000_000) == first + 1_000
    assert mapper.map(90, 990_000_000) > first


def test_pts_mapper_starts_again_with_a_new_ffmpeg():
    """FFmpeg started again 5 s in: its pts are back at 0, its frames come 50 ms after they were
    taken. The times follow the new frames at once and keep going up."""
    mapper, ms, base = clock.PtsMapper(), 1_000_000, 10 * clock.NS
    old = [mapper.map(i * 33, base + i * 33 * ms + 40 * ms) for i in range(150)]
    assert mapper.behind(150 * 33, base + 150 * 33 * ms + 40 * ms) == 0
    mapper.restart()
    assert mapper.offset_ns is None and mapper.lowest is None
    start = base + 5 * clock.NS
    new = [mapper.map(i * 16, start + i * 16 * ms + 50 * ms) for i in range(60)]
    assert new == [start + i * 16 * ms + 50 * ms for i in range(60)]  # within a frame: exactly
    assert all(b > a for a, b in zip(old + new, (old + new)[1:]))
    # Measured against the new FFmpeg's best: against the old one's, from its own origin, 5 s.
    assert mapper.behind(60 * 16, start + 60 * 16 * ms + 50 * ms) == 0
    # A restart while the old frames still had later times: the new ones never go back before them.
    mapper.restart()
    assert mapper.map(0, start) > new[-1]


# -- frames on their way -------------------------------------------------------------------------------


def _unit(kind: str = flv.FRAME, keyframe: bool = False, n: int = 0) -> Unit:
    return Unit(kind, n * 33, keyframe, bytes([n]), n)


async def test_frame_queue_keeps_the_newest_frames_and_the_avcc():
    queue = FrameQueue(limit=3)
    queue.put(_unit(flv.CONFIG))
    for n in range(1, 6):
        queue.put(_unit(keyframe=n == 4, n=n))
    queue.put(None)
    # Frames 1 and 2 made way; the avcC and the end are kept.
    assert queue.frames == 3 and queue.take_trimmed() == 2 and queue.take_trimmed() == 0
    assert queue.keyframe_waiting() and queue.last_frame_ns == 5
    got = [await queue.get() for _ in range(5)]
    assert [u.kind if u else None for u in got] == [flv.CONFIG, flv.FRAME, flv.FRAME, flv.FRAME, None]
    assert [u.pts_ms for u in got[1:4]] == [99, 132, 165] and queue.frames == 0 and not queue.keyframe_waiting()


async def test_frame_queue_forgets_an_old_ffmpegs_frames():
    queue = FrameQueue(limit=3)
    queue.put(_unit(flv.CONFIG))
    for n in range(1, 6):
        queue.put(_unit(keyframe=n == 4, n=n))
    queue.put(None)  # that FFmpeg ended
    queue.clear()
    assert queue.frames == 0 and queue.take_trimmed() == 0 and not queue.keyframe_waiting()
    assert queue.last_frame_ns == 5  # a gap in the frames still counts from the last one that came
    queue.put(_unit(keyframe=True, n=7))
    assert (await queue.get()).data == bytes([7]) and queue.frames == 0


def test_video_send_buffers_hold_about_two_frames():
    assert send_buffer(8000, 30) == 2 * 8000 * 1000 // 8 // 30
    assert send_buffer(500, 60) == 16 * 1024  # never less
    assert send_buffer(100000, 10) == 256 * 1024  # nor more


def test_a_second_of_the_bitrate_may_wait_for_the_network_before_frames_are_dropped():
    assert catch_up_bytes(8000, 24) == 1_000_000  # 8 Mbit/s for CATCH_UP_MS (1 s)
    assert catch_up_bytes(100, 30) == send_buffer(100, 30)  # never less than the send buffers


MS = 1_000_000
CATCH_UP = mirror_session.CATCH_UP_MS * MS


class _Connection:
    buffered = 0  # what asyncio still holds of the video connection


def _sender(*waiting: bool, buffered: int = 0) -> MirrorSession:
    """A session about to send a frame, with frames waiting behind it (True: a keyframe)."""
    session = MirrorSession(Target("TV", "1.2.3.4"), Settings(), profile=profiles.MirrorProfile())
    # pylint: disable=protected-access
    session._frames = FrameQueue(limit=60)
    for n, keyframe in enumerate(waiting):
        session._frames.put(_unit(keyframe=keyframe, n=n))
    session._data = _Connection()
    session._data.buffered = buffered
    session._catch_up = catch_up_bytes(8000, 30)
    return session


async def test_an_ffmpeg_that_ended_is_seen_to_before_a_keyframe_that_was_asked_for(monkeypatch):
    """The end of FFmpeg's frames (None) goes to _ended, which says what next, even while a
    keyframe is waiting to be asked for: _restart is not called for it, nor for an FFmpeg
    that is no more."""
    # pylint: disable=protected-access
    session = _sender()
    session._frames.put(None)
    session._cipher, session._ffmpeg = object(), object()
    session.report.established = SimpleNamespace(plan=SimpleNamespace(lead_ms=100))
    session._keyframe_wanted, session._last_restart_ns = "frames were dropped", 0
    calls = []

    async def ended() -> bool:
        calls.append("ended")
        return False  # the session ends

    async def restart(why, **kw) -> None:
        calls.append(f"restart for {why}")

    monkeypatch.setattr(session, "_ended", ended)
    monkeypatch.setattr(session, "_restart", restart)
    await asyncio.wait_for(session._pump(), 5)
    assert calls == ["ended"]


def test_frames_the_network_held_up_go_late_for_as_long_as_the_tv_is_given_to_catch_up():
    """_drop_reason(idr, waiting, late_ns, behind_ns, fresh) for P-frames while sending."""
    # pylint: disable=protected-access
    reason = _sender(False, False)._drop_reason
    assert reason(False, False, -50 * MS, -50 * MS, False) == ""  # on time
    assert reason(False, False, CATCH_UP - MS, CATCH_UP - MS, False) == ""  # late, within CATCH_UP_MS
    # This frame alone, or the frames since the first that went late, past their lead for longer.
    assert reason(False, False, CATCH_UP + MS, CATCH_UP + MS, False) == (
        "frames have been past their lead for 1001 ms, more than the 1000 ms the TV is given to catch up")
    assert "for 1100 ms" in reason(False, False, 200 * MS, CATCH_UP + 100 * MS, False)
    assert reason(False, False, -MS, CATCH_UP + 100 * MS, False) == ""  # on time again: the TV caught up
    # More than about CATCH_UP_MS of the bitrate still in asyncio's buffer.
    most = catch_up_bytes(8000, 30)
    assert _sender(buffered=most)._drop_reason(False, False, 0, 0, False) == ""
    assert _sender(buffered=most + 1)._drop_reason(False, False, 0, 0, False) == (
        "976 KB are still waiting for the network")


def test_a_drop_ends_on_a_keyframe_that_brings_the_tv_back_to_real_time():
    """Keyframes, and frames waiting for one (waiting=True)."""
    # pylint: disable=protected-access
    # While sending, a keyframe goes, unless a newer one waits or the TV has been behind too long.
    assert _sender(False)._drop_reason(True, False, 500 * MS, 500 * MS, False) == ""
    assert _sender(False, True)._drop_reason(True, False, 0, 0, False) == "a newer keyframe is waiting"
    assert "catch up" in _sender(False)._drop_reason(True, False, CATCH_UP + MS, CATCH_UP + MS, False)
    # Dropping, P-frames wait for a keyframe, and one on time, or the last frame that waited, ends it.
    assert _sender()._drop_reason(False, True, -50 * MS, 0, True) == "waiting for a keyframe"
    assert _sender(False, False)._drop_reason(True, True, -MS, 0, False) == ""
    assert _sender()._drop_reason(True, True, 500 * MS, 500 * MS, False) == ""
    # Not one that waited and is past its lead with frames behind it: the TV would stay behind ...
    assert _sender(False, False)._drop_reason(True, True, 500 * MS, 500 * MS, False) == (
        "the keyframe is 500 ms past its lead, with frames behind it")
    assert _sender(False, True)._drop_reason(True, True, -MS, 0, False) == "a newer keyframe is waiting"
    # ... unless FFmpeg gave it once the drop began: no drop waits longer than for that one.
    assert _sender(False, False)._drop_reason(True, True, 500 * MS, 500 * MS, True) == ""


def test_the_tv_dropping_the_video_at_once_is_a_matter_of_time_not_of_keyframes():
    """Within AT_ONCE_SECONDS of the first frame, whatever the Debug menu's keyframe interval."""
    # pylint: disable=protected-access
    session = MirrorSession(Target("TV", "1.2.3.4"), Settings(mirror_keyframes=10),
                            profile=profiles.MirrorProfile())
    assert session._at_once(10 * clock.NS)  # no frame went yet
    session._first_sent_ns = 10 * clock.NS
    assert session._at_once(10 * clock.NS + 1_900 * MS)
    assert not session._at_once(10 * clock.NS + 2_100 * MS)


def test_the_debug_menus_tv_wait_stays_within_what_a_profile_takes():
    assert lead_override(Settings()) == 0  # automatic: the TV's own
    assert lead_override(Settings(mirror_lead_ms=60)) == 60 and lead_override(Settings(mirror_lead_ms=300)) == 300
    assert lead_override(Settings(mirror_lead_ms=5)) == 20 and lead_override(Settings(mirror_lead_ms=5000)) == 1000
    assert lead_override(Settings(mirror_lead_ms="fast")) == 0 and lead_override(Settings(mirror_lead_ms=-1)) == 0
    assert lead_override(Settings(mirror_lead_ms=True)) == 0


def test_the_latency_asks_the_tv_for_a_lead_of_its_own_and_the_debug_menu_overrides_it():
    """Mirror screen settings > Latency: Low 60 ms, Normal the TV's (saved, or 100 ms), Smooth
    200 ms; More options > Debug > TV wait, when it is set, wins."""
    assert [latency_lead(Settings(mirror_latency=level), 150) for level in ("low", "normal", "smooth")] == [
        (60, "Latency low"), (150, "Latency normal, the TV's own"), (200, "Latency smooth")]
    assert latency_lead(Settings(), profiles.MirrorProfile().lead_ms) == (100, "Latency normal, the TV's own")
    assert latency_lead(Settings(mirror_latency="smooth", mirror_lead_ms=300), 150) == (300, "Debug TV wait")
    assert latency_lead(Settings(mirror_latency="low", mirror_lead_ms=5000), 150) == (1000, "Debug TV wait")
    # A settings.json edited by hand: Normal.
    assert [mirror_latency(Settings(mirror_latency=v)) for v in ("low", "smooth", "fast", "", None, 1)] == [
        "low", "smooth", "normal", "normal", "normal", "normal"]
    assert Settings().mirror_latency == "normal"


def test_a_session_works_from_a_copy_of_the_settings():
    """What the menus change while it runs is for the next session (a restart of FFmpeg takes
    the copy, as the first one did)."""
    settings = Settings(video_bitrate_kbps=1500, mirror_keyframes=0, mirror_latency="low")
    session = MirrorSession(Target("TV", "1.2.3.4"), settings, profile=profiles.MirrorProfile())
    settings.video_bitrate_kbps, settings.mirror_keyframes, settings.mirror_latency = 9000, 2, "smooth"
    assert session.settings is not settings and session.settings == Settings(
        video_bitrate_kbps=1500, mirror_keyframes=0, mirror_latency="low")


def test_an_older_settings_json_loads_with_mirror_screens_new_settings(tmp_path, caplog):
    """The old keyframe switch (every 2 s was its default) is dropped, not carried over: the new
    one starts at its own default, only when needed. Hand edits of the new ones give their defaults."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mirror_keyframe_seconds": 2, "mirror_encoder_extra": None, "mirror_lead_ms": 150,
                                "framerate": 60}), encoding="utf-8")
    settings = Settings.load(path)
    assert (settings.mirror_keyframes, settings.mirror_encoder_extra) == (0, "")
    assert (settings.mirror_lead_ms, settings.framerate) == (150, 60)  # the others as they were
    assert (settings.mirror_latency, settings.mirror_framerate, settings.mirror_scaler) == ("normal", 0, "auto")
    settings.save(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "mirror_keyframe_seconds" not in saved and saved["mirror_keyframes"] == 0
    with caplog.at_level(logging.INFO, logger="mirrorbridge.config"):
        for junk in ({"mirror_keyframes": 5}, {"mirror_keyframes": "10"}, {"mirror_keyframes": True},
                     {"mirror_keyframes": 10.0}, {"mirror_latency": "fast"}, {"mirror_latency": None},
                     {"mirror_framerate": 24}, {"mirror_framerate": "60"}, {"mirror_scaler": "gpu"},
                     {"mirror_encoder_extra": 8}, {"mirror_encoder_extra": ["-ldkfs", "8"]}):
            path.write_text(json.dumps(junk), encoding="utf-8")
            assert Settings.load(path) == Settings(), junk
    assert "Ignoring mirror_keyframes=5" in caplog.text
    chosen = {"mirror_keyframes": 10, "mirror_latency": "smooth", "mirror_framerate": 60, "mirror_scaler": "cpu",
              "mirror_encoder_extra": "-ldkfs 8"}
    path.write_text(json.dumps(chosen), encoding="utf-8")
    settings = Settings.load(path)
    assert {key: getattr(settings, key) for key in chosen} == chosen


def test_frames_are_shown_on_arrival_only_when_settings_json_says_true():
    assert on_arrival(Settings(mirror_on_arrival=True)) and not on_arrival(Settings())
    for edited in ("false", "true", 1, None):  # by hand: the default
        assert not on_arrival(Settings(mirror_on_arrival=edited))


# -- what is asked, and of whom ------------------------------------------------------------------------


def test_features_and_fairplay():
    tcl = parse_features("0x7F8AD0,0x38BCF46")
    assert tcl == 0x38BCF46007F8AD0
    assert not Target("TCL", "1.2.3.4", features=tcl).needs_fairplay
    assert Target("TCL", "1.2.3.4", features=tcl).has_feature(41)  # PTP
    assert Target("Apple TV", "1.2.3.5", features=parse_features("0x4A7FDFD5,0xBC157FDE")).needs_fairplay
    assert parse_features(0x1234) == 0x1234 and parse_features("junk") is None and parse_features(True) is None


def test_receiver_info_and_canvas():
    info = ReceiverInfo.parse({
        "displays": [{"widthPixels": 3840, "heightPixels": 2160, "maxFPS": 60}],
        "supportedFormats": {"screenStream": 0x1C0000}, "features": "0x7F8AD0,0x38BCF46",
        "timingPeerInfo": {"ID": "x"}, "eventPort": 7010,
    })
    assert info.displays[0].width == 3840 and info.screen_formats == 0x1C0000 and info.event_port == 7010
    assert "3840x2160 up to 60 fps" in info.summary()
    target = Target("TV", "1.2.3.4")
    assert choose_canvas(info, target) == (1920, 1080)
    assert choose_canvas(info, target, canvas_limit(Settings(output_height=720))) == (1280, 720)
    assert choose_canvas(info, target, canvas_limit(Settings(output_height=0))) == (1920, 1080)
    assert choose_canvas(None, target) == (1280, 720)
    assert choose_canvas(None, Target("TV", "1.2.3.4", features=1 << 28)) == (1920, 1080)
    small = ReceiverInfo.parse({"displays": [{"width": 1366, "height": 768, "refreshRate": 50}]})
    assert choose_canvas(small, target) == (1366, 768) and choose_fps(60, small) == 50
    assert choose_fps(30, None) == 30


def test_frame_rate_automatic_is_60_with_a_hardware_encoder_that_is_given_frames_fast_enough():
    """Mirror screen settings > Frame rate: Automatic (0, or anything a hand edit left) is 60 for
    NVENC, AMF, Quick Sync or Media Foundation with ddagrab or the test pattern, else 30; 30 and
    60 are as chosen; never more than the TV says it takes."""
    tv = ReceiverInfo.parse({"displays": [{"widthPixels": 1920, "heightPixels": 1080, "maxFPS": 60}]})
    slow_tv = ReceiverInfo.parse({"displays": [{"width": 1366, "height": 768, "refreshRate": 50}]})

    def fps(wanted, encoder, backend, info=tv):
        return choose_mirror_fps(wanted, info, encoder=encoder, backend=backend)

    assert fps(0, "h264_nvenc", "ddagrab") == (60, "automatic: NVIDIA NVENC with ddagrab")
    assert fps(0, "h264_amf", TEST_PATTERN)[0] == fps(0, "h264_qsv", "ddagrab")[0] == 60
    assert fps(0, "h264_nvenc", "ddagrab", None)[0] == 60  # a TV that says nothing
    assert fps(0, "libx264", "ddagrab") == (30, "automatic: the CPU encoder")
    assert fps(0, "h264_nvenc", "gdigrab") == (30, "automatic: gdigrab capture")
    assert fps(0, "h264_nvenc", "ddagrab", slow_tv) == (50, "automatic: NVIDIA NVENC with ddagrab, the TV takes "
                                                           "50 at most")
    assert fps(30, "h264_nvenc", "ddagrab") == (30, "as chosen")
    assert fps(60, "libx264", "gdigrab") == (60, "as chosen")
    assert fps(60, "libx264", "ddagrab", slow_tv) == (50, "as chosen, the TV takes 50 at most")
    assert [fps(v, "h264_nvenc", "ddagrab")[0] for v in (24, "60", None, True)] == [60, 60, 60, 60]  # Automatic
    assert Settings().mirror_framerate == 0


def test_sender_id_is_a_stable_local_mac():
    first = sender_id("aa:bb")
    assert first == sender_id("aa:bb") != sender_id("cc:dd")
    assert first.startswith("02:") and len(first.split(":")) == 6


def test_plans_and_profiles(tmp_path):
    path = tmp_path / "mirroring.json"
    assert profiles.load("tv", path) is None
    profile = profiles.MirrorProfile.of(Plan(MEDIA_FIRST, audio=True), h264.TRAILER_FIXED)
    profiles.save("tv", profile, name="TCL", info="displays: 1920x1080", path=path)
    assert profiles.load("tv", path) == profile
    assert profile.plan().ordering == MEDIA_FIRST and profile.plan(fallback=False).fallback is False
    # A file edited by hand: what makes no sense is replaced by the defaults; PTP never sticks.
    raw = '{"tv": {"profile": {"ordering": "sideways", "timing": "ptp", "trailer": "ff", "lead_ms": 5, "x": 1}}}'
    path.write_text(raw)
    assert profiles.load("tv", path) == profiles.MirrorProfile()
    path.write_text("not json")
    assert profiles.load("tv", path) is None
    described = Plan(CONTROL_FIRST, audio=False, timing=PTP).describe()
    assert described == "control-first, without the audio stream, PTP timing"


def test_refusals_say_what_to_do():
    steps = {"pair-verify": "ok", "setup media-first": 401}
    assert refused(401, steps, "setup media-first").args[0].key == "mirror_password"
    assert refused(403, steps, "x").args[0].key == "tv_pairing_refused"
    assert refused(453, steps, "x").args[0].key == "tv_busy"
    error = refused(None, {"setup video": "no answer"}, "setup video")
    assert error.args[0].key == "mirror_no_answer" and "setup video=no answer" in str(error)
    assert refused(500, steps, "x").args[0].key == "mirror_refused" and refused(500, steps, "x").code == 500
    error = no_timing(7880, {})
    assert "7880-7889" in str(error) and "Windows Firewall" in str(error)


# -- FFmpeg ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("encoder, expected", [
    ("libx264", ["-tune", "zerolatency"]),
    ("h264_nvenc", ["-delay", "0"]),
    ("h264_qsv", ["-async_depth", "1"]),
    ("h264_amf", ["-usage", "ultralowlatency"]),
    ("h264_mf", ["-scenario", "display_remoting"]),
])
def test_mirror_encoder_args_hold_nothing_back(encoder, expected):
    args = ffmpeg.mirror_encoder_args(encoder, 6000, 60)
    joined = " ".join(args)
    assert " ".join(expected) in joined
    assert args[args.index("-bf") + 1] == "0" and args[args.index("-g") + 1] == "120"
    assert args[args.index("-bufsize") + 1] == "100k"  # one frame's share: keyframes stay small
    assert args[args.index("-maxrate") + 1] == "6000k"


def _option(args: list[str], name: str):
    return args[args.index(name) + 1] if name in args else None


@pytest.mark.parametrize("encoder", ffmpeg.ENCODER_PREFERENCE)
def test_a_keyframe_every_2_s_every_10_s_or_only_when_needed(encoder):
    def args(seconds):
        return ffmpeg.mirror_encoder_args(encoder, 6000, 30, keyframe_seconds=seconds)

    every_2, every_10, needed = args(2), args(10), args(0)
    assert every_2 == ffmpeg.mirror_encoder_args(encoder, 6000, 30)  # a caller that does not say: as before
    assert (_option(every_2, "-g"), _option(every_2, "-force_key_frames")) == ("60", "expr:gte(t,n_forced*2)")
    assert (_option(every_10, "-g"), _option(every_10, "-force_key_frames")) == ("300", "expr:gte(t,n_forced*10)")
    if encoder in ("h264_nvenc", "libx264"):
        # The first keyframe only (the session starts FFmpeg again for another).
        assert _option(needed, "-g") == "2147483647" and "-force_key_frames" not in needed
    else:
        assert needed == every_10  # not tried that way on real hardware: one every 10 s
    for each in (every_2, every_10, needed):
        assert _option(each, "-bf") == "0" and _option(each, "-bufsize") == "200k"  # one frame's share, still
        if encoder == "h264_nvenc":
            assert _option(each, "-forced-idr") == "1"  # the forced keyframes are IDRs
    if encoder == "libx264":
        assert [_option(a, "-x264-params") for a in (every_2, every_10, needed)] == [
            "keyint=60:min-keyint=60:scenecut=0", "keyint=300:min-keyint=300:scenecut=0", "keyint=infinite:scenecut=0"]
    # Anything but a whole number of seconds is the default of 2 s.
    assert args(-1) == args(None) == args(True) == args(2.5) == every_2
    assert ffmpeg.mirror_keyframe_interval(encoder, 0) == (0 if encoder in ffmpeg.NO_PERIODIC_ENCODERS else 10)


def test_the_debug_menus_keyframes_setting():
    """0 (only when needed, the default), 10 or 2; anything else from a settings.json edited by hand is 0."""
    values = (0, 2, 10, 1, 5, "2", None, 10.0, True, False, -10)
    assert [ffmpeg.mirror_keyframe_seconds(v) for v in values] == [0, 2, 10, 0, 0, 0, 0, 10, 0, 0, 0]
    assert ffmpeg.MIRROR_KEYFRAME_CHOICES == (0, 10, 2) and Settings().mirror_keyframes == 0
    # A session starts FFmpeg again for a keyframe after a drop, but not with one every 2 s (as before).
    assert [keyframes_on_demand(Settings(mirror_keyframes=v)) for v in (0, 10, 2, 7)] == [True, True, False, True]
    assert [keyframes_text("libx264", Settings(mirror_keyframes=v)) for v in (0, 10, 2)] == [
        "only when needed", "every 10 s and when needed", "every 2 s"]
    assert keyframes_text("h264_qsv", Settings()) == "every 10 s and when needed"  # not tried without


def test_ffmpeg_for_mirroring_takes_the_keyframes_it_is_given(monkeypatch):
    monkeypatch.setattr(source, "_version", lambda ff: (7, 1))
    cmd = build_command("ffmpeg", Settings(), source=TEST_PATTERN, encoder="libx264", width=640, height=360, fps=30)
    assert _option(cmd, "-g") == "60" and "keyint=60:min-keyint=60:scenecut=0" in cmd  # the default, 2 s
    # Not the Debug menu's setting: the session says (Google Cast mirroring never does).
    cmd = build_command("ffmpeg", Settings(mirror_keyframes=10), source=TEST_PATTERN, encoder="h264_nvenc",
                        width=640, height=360, fps=30)
    assert _option(cmd, "-g") == "60" and "expr:gte(t,n_forced*2)" in cmd
    cmd = build_command("ffmpeg", Settings(), source=TEST_PATTERN, encoder="h264_nvenc", width=640, height=360,
                        fps=30, keyframe_seconds=10)
    assert _option(cmd, "-g") == "300" and "expr:gte(t,n_forced*10)" in cmd
    cmd = build_command("ffmpeg", Settings(), source=TEST_PATTERN, encoder="h264_nvenc", width=640, height=360,
                        fps=60, keyframe_seconds=0)
    assert _option(cmd, "-g") == "2147483647" and "-force_key_frames" not in cmd


async def test_google_cast_mirroring_keeps_a_keyframe_every_2_s(monkeypatch):
    """Google Cast mirroring starts FFmpeg as Mirror screen does (build_command) but never again for
    a keyframe: it keeps one every 2 s whatever the Debug menu says, and the CPU chain."""
    from mirrorbridge.googlecast import session as cast_session
    from mirrorbridge.googlecast.discovery import CastDevice

    monkeypatch.setattr(source, "_version", lambda ff: (7, 1))
    started = []

    class Started:
        def __init__(self, cmd, on_unit):
            started.append(cmd)

        def start(self):
            pass

    monkeypatch.setattr(cast_session, "FFmpegSource", Started)
    monitor = ffmpeg.Monitor(0, 0, 0, 2560, 1440, True, 0, device=r"\\.\DISPLAY1")
    for what, backend, capture_monitor in ((TEST_PATTERN, "test", None), (DESKTOP, "ddagrab", monitor)):
        cast = cast_session.GoogleCastMirror(CastDevice("TV", "192.0.2.1"), Settings(mirror_keyframes=0), source=what)
        # pylint: disable=protected-access
        cast._capture, cast._sources = ("ffmpeg", capture_monitor, None), [(backend, "h264_nvenc")]
        cast._frames, cast.canvas, cast.fps = FrameQueue(60), (1920, 1080), 30
        cast._start_source()
        cmd = started[-1]
        assert _option(cmd, "-force_key_frames") == "expr:gte(t,n_forced*2)" and _option(cmd, "-g") == str(30 * 2)
        assert "-bsf:v" not in cmd
    assert _option(started[-1], "-vf").startswith("hwdownload,format=bgra,scale=")


def test_mirror_command_test_pattern_and_desktop(monkeypatch):
    settings = Settings(video_bitrate_kbps=4000)
    monkeypatch.setattr(source, "_version", lambda ff: (7, 1))
    cmd = build_command("ffmpeg", settings, source=TEST_PATTERN, encoder="libx264", width=640, height=360, fps=30)
    assert "testsrc=size=640x360:rate=30" in cmd and cmd[-3:] == ["-f", "flv", "pipe:1"]
    assert cmd[cmd.index("-vf") + 1] == "format=yuv420p" and "-an" in cmd
    # FFmpeg holds at most 2 captured frames, and paces the test pattern from its first frame.
    assert cmd[cmd.index("-thread_queue_size") + 1] == "2"
    assert cmd[cmd.index("-re"):cmd.index("-re") + 3] == ["-re", "-readrate_initial_burst", "0.001"]
    monkeypatch.setattr(source, "_version", lambda ff: (6, 0))
    cmd = build_command("ffmpeg", settings, source=TEST_PATTERN, encoder="libx264", width=640, height=360, fps=30)
    assert "-readrate_initial_burst" not in cmd  # an FFmpeg that does not know it
    mon = ffmpeg.Monitor(1, 0, 0, 2560, 1440, dxgi_output=0)
    cmd = build_command("ffmpeg", settings, source=DESKTOP, encoder="h264_nvenc", width=1920, height=1080, fps=60,
                        backend="ddagrab", monitor=mon)
    assert "ddagrab=output_idx=0:framerate=60:draw_mouse=1" in cmd
    # The CPU chain: to memory, scaled into BT.709 limited range (bilinear), tagged as that.
    assert cmd[cmd.index("-vf") + 1] == (
        "hwdownload,format=bgra,scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease"
        ":force_divisible_by=2:flags=bilinear:out_color_matrix=bt709:out_range=tv,format=nv12,"
        "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709")
    gdi = build_command("ffmpeg", settings, source=DESKTOP, encoder="libx264", width=1280, height=720, fps=30,
                        backend="gdigrab", monitor=mon)
    assert _option(gdi, "-vf") == (
        "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        ":flags=bilinear:out_color_matrix=bt709:out_range=tv,format=yuv420p,"
        "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709")
    assert "-bsf:v" not in cmd + gdi


def test_mirror_command_on_the_graphics_card(monkeypatch):
    """NVENC with ddagrab: no filter at the screen's own size, scale_d3d11 when it must be smaller;
    neither brings the frames to memory."""
    monkeypatch.setattr(source, "_version", lambda ff: (8, 1))
    big = ffmpeg.Monitor(0, 0, 0, 2560, 1440, True, 0, device=r"\\.\DISPLAY1")
    small = ffmpeg.Monitor(2, 2560, 0, 1024, 600, False, 2, device=r"\\.\DISPLAY3")

    def command(monitor, **kw):
        chain = capture.chain_for(capture.GPU, (monitor.width, monitor.height), (1920, 1080))
        return build_command("ffmpeg", Settings(), source=DESKTOP, encoder="h264_nvenc", width=1920, height=1080,
                             fps=60, backend="ddagrab", monitor=monitor, chain=chain, **kw)

    native = command(small)
    assert "ddagrab=output_idx=2:framerate=60:draw_mouse=1" in native
    assert "-vf" not in native and not any("hwdownload" in a for a in native)
    # The stream says BT.709 primaries and transfer as the other chains do (NVENC's own BT.601 matrix stays).
    assert native[native.index("-bsf:v"):native.index("-bsf:v") + 2] == list(capture.NATIVE_OUTPUT)
    assert native.index("-bsf:v") > native.index("-c:v") and native[-3:] == ["-f", "flv", "pipe:1"]
    scaled = command(big)
    assert _option(scaled, "-vf") == ("scale_d3d11=width=1920:height=1080:format=nv12,"
                                      "setparams=range=pc:colorspace=bt709:color_primaries=bt709:color_trc=bt709")
    assert "-bsf:v" not in scaled and not any("hwdownload" in a for a in scaled)
    # Options typed into settings.json come after the encoder's; ones that do not parse are left out.
    extra = command(small, extra="-ldkfs 8 -spatial-aq 0")
    assert extra[extra.index("-ldkfs"):extra.index("-ldkfs") + 4] == ["-ldkfs", "8", "-spatial-aq", "0"]
    assert extra.index("-ldkfs") > extra.index("-bsf:v") and extra[-3:] == ["-f", "flv", "pipe:1"]
    assert command(small, extra='-x264opts "open') == native == command(small, extra=None) == command(small, extra=" ")
    # A GPU chain is only ddagrab's: gdigrab gets the CPU one.
    gdi = build_command("ffmpeg", Settings(), source=DESKTOP, encoder="h264_nvenc", width=1920, height=1080, fps=60,
                        backend="gdigrab", monitor=big, chain=capture.chain_for(capture.GPU, (2560, 1440), (1920, 1080)))
    assert _option(gdi, "-vf").startswith("scale=w='min(1920,iw)'")


def test_ffmpeg_versions_are_read_from_its_banner(monkeypatch):
    for banner, version in (("ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023", (6, 1)),
                            ("ffmpeg version 7.1-essentials_build-www.gyan.dev Copyright", (7, 1)),
                            ("ffmpeg version n7.0.2 Copyright", (7, 0)),
                            ("ffmpeg version N-117000-g1234abcd Copyright", (99, 0)),
                            ("unknown", (0, 0))):
        monkeypatch.setattr(ffmpeg, "ffmpeg_version", lambda ff, banner=banner: banner)
        assert source._version("ffmpeg") == version  # pylint: disable=protected-access

    def missing(ff):
        raise FileNotFoundError(ff)

    monkeypatch.setattr(ffmpeg, "ffmpeg_version", missing)
    assert source._version("nowhere/ffmpeg") == (0, 0)  # pylint: disable=protected-access


@pytest.mark.parametrize("seconds, keyframes", [(2, [0, 60, 120]), (0, [0])])
def test_mirror_encoder_args_run_with_libx264(seconds, keyframes):
    ff = _ffmpeg()
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
           "-frames:v", "130", "-pix_fmt", "yuv420p",
           *ffmpeg.mirror_encoder_args("libx264", 1000, 30, keyframe_seconds=seconds), "-f", "flv", "pipe:1"]
    tags = flv.FlvReader().feed(subprocess.run(cmd, check=True, capture_output=True, timeout=60).stdout)
    frames = [t for t in tags if t.kind == flv.FRAME]
    assert len(frames) == 130
    assert [i for i, t in enumerate(frames) if t.keyframe] == keyframes  # every 2 s or the first only, no scene cuts
    assert all(b.dts_ms == b.pts_ms for b in frames)  # no B-frames: nothing reordered


def test_stage_times_percentiles_and_summary(caplog):
    caplog.set_level("INFO")
    times = stats.StageTimes(kbps=8000, fps=30, lead_ms=100)
    for i in range(1, 101):
        times.add(1000, keyframe=i == 1, buffered=i * 1024, pipe=i * 1_000_000, send=500_000)
    times.add(400_000, keyframe=True, pipe=0, send=0)  # far over 8 frames' share of 8 Mbit/s
    times.drop()
    times.add(1000, keyframe=False, late=True, age=250_000_000)  # a frame the network held up past its lead
    assert stats.percentile([float(i) for i in range(1, 101)], 0.5) == 51.0
    times.log_window(ntp_requests=4)
    assert "pipe 50.0/95.0" in caplog.text and "dropped 1, late 1" in caplog.text
    assert "up to 100 KB waiting" in caplog.text
    summary = times.summary()
    assert summary["frames"] == 102 and summary["keyframes"] == 2 and summary["send"] == (0.5, 0.5)
    assert summary["dropped"] == 1 and summary["late"] == 1
    assert times.big_keyframes == 1 and summary["largest_keyframe"] == 400_000
    times.log_window(ntp_requests=9)
    assert "the TV asked for the time 5 times" in caplog.text


def test_stage_times_warn_when_ffmpeg_cannot_keep_up(caplog):
    caplog.set_level("INFO")
    times = stats.StageTimes(kbps=8000, fps=60, lead_ms=100)
    start = clock.now_ns()
    for i in range(50):  # 25 frames a second for 2 s, of the 60 asked for, more and more behind
        times.add(1000, keyframe=False, behind=i * 10_000_000)
    times.log_window(start + 2 * clock.NS)
    assert "of the 60 asked for: the screen capture or the encoder cannot keep up" in caplog.text
    assert times.summary()["slow_windows"] == 1 and "behind 240.0/470.0" in caplog.text


def test_stage_times_count_what_ffmpeg_delivers_not_what_the_tv_takes(caplog, monkeypatch):
    caplog.set_level("INFO")
    now = [0]
    monkeypatch.setattr(stats, "now_ns", lambda: now[0])
    times = stats.StageTimes(fps=30)
    for i in range(60):  # 30 frames a second for 2 s, of which a slow TV takes one in ten
        now[0] = i * clock.NS // 30
        times.arrived()
        if i % 10 == 0:
            times.add(1000, keyframe=True)
    now[0] += 3 * clock.NS  # the session then takes its time to stop
    times.log_last()
    assert "Mirroring: 30.5 fps (of 30)" in caplog.text and "cannot keep up" not in caplog.text
    assert times.frames == 6 and times.slow_windows == 0


def test_stage_times_do_not_warn_of_a_slow_ffmpeg_for_a_window_it_was_started_again_in(caplog):
    """The picture stands still while another FFmpeg starts: that is not the capture or the
    encoder failing to keep up. The window after it is counted again."""
    caplog.set_level("INFO")
    times = stats.StageTimes(kbps=8000, fps=60, lead_ms=100)
    start = clock.now_ns()
    for i in range(50):  # 25 frames a second for 2 s, of the 60 asked for
        if i == 25:
            times.restarted()
        times.add(1000, keyframe=False)
    times.log_window(start + 2 * clock.NS)
    assert "cannot keep up" not in caplog.text and "; FFmpeg started again 1 times" in caplog.text
    for i in range(50):
        times.add(1000, keyframe=False)
    times.log_window(start + 4 * clock.NS)
    assert "of the 60 asked for: the screen capture or the encoder cannot keep up" in caplog.text
    assert times.summary()["slow_windows"] == 1 and times.summary()["restarts"] == 1
    times.set_rate(1500, 30)  # the x264 FFmpeg that came after a hardware encoder's
    assert (times.fps, times.frame_budget) == (30, 1500 * 1000 // 8 // 30)


async def test_the_video_connection_closes_cleanly_once_its_buffer_went_out():
    """A close with video still buffered sends it, and then asyncio's transport lets go of its
    loop: the connection must not be aborted after that (it raised AttributeError)."""
    received = bytearray()
    tv_done = asyncio.Event()

    async def tv(reader, writer):
        try:
            while chunk := await reader.read(65536):
                received.extend(chunk)
        except ConnectionError:
            pass  # dropped: the count below says how much came
        finally:
            writer.close()
            tv_done.set()

    # Through a 16 KB send buffer even loopback is slow (2 MB took 0.3-0.7 s on Linux, and longer
    # than the 1 s close on Windows, where the old test then hung in Server.wait_closed), so the
    # close gets 10 s here, and every wait has a limit.
    server = await asyncio.start_server(tv, "127.0.0.1", 0)
    try:
        data = await DataChannel.open("127.0.0.1", server.sockets[0].getsockname()[1], buffer=16384)
        # Two writes: Windows takes a whole first write into the kernel at once, whatever the send
        # buffer, but not a second one before the loop has run, so that one waits in asyncio.
        data.write(b"x" * 150_000)
        data.write(b"x" * 150_000)
        assert data.buffered > 0
        try:
            await asyncio.wait_for(data.close(seconds=10), 20)
        except asyncio.TimeoutError:
            pytest.fail(f"close() did not end ({len(received)} bytes came, {data.buffered} still buffered)")
        try:
            await asyncio.wait_for(tv_done.wait(), 10)
        except asyncio.TimeoutError:
            pytest.fail(f"the TV's side never saw the connection end ({len(received)} bytes came)")
    finally:
        server.close()
    assert len(received) == 300_000
