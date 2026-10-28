"""Screen mirroring's pieces, against fixed vectors, an independent implementation of the
cryptography, and FFmpeg's own output (tests/test_mirror_loopback.py has whole sessions)."""

import asyncio
import random
import struct
import subprocess

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pyatv.auth.hap_srp import hkdf_expand

from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.mirror import clock, flv, h264, packets, profiles, stats
from mirrorbridge.mirror.control import ReceiverInfo, Target, parse_features, sender_id
from mirrorbridge.mirror.errors import no_timing, refused
from mirrorbridge.mirror.negotiation import CONTROL_FIRST, MEDIA_FIRST, PTP, Plan
from mirrorbridge.mirror.session import canvas_limit, choose_canvas, choose_fps
from mirrorbridge.mirror.source import DESKTOP, TEST_PATTERN, build_command

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
    request = struct.pack(">BBHI", 0x80, 0xD2, 7, 0) + bytes(16) + bytes.fromhex("0102030405060708")
    reply = clock.ntp_reply(request, 5 * clock.NS, 5 * clock.NS + 250_000_000)
    assert reply[:4] == bytes.fromhex("80d30007") and len(reply) == 32
    assert reply[8:16] == bytes.fromhex("0102030405060708")
    received, sent = struct.unpack(">QQ", reply[16:])
    assert received == (clock.NTP_EPOCH_SECONDS + 5) << 32
    assert sent == ((clock.NTP_EPOCH_SECONDS + 5) << 32) | (1 << 30)
    assert clock.ntp_reply(b"\x80\xd3" + bytes(30), 0) is None
    assert clock.ntp_reply(request[:20], 0) is None


async def test_ntp_responder_over_udp():
    loop = asyncio.get_running_loop()
    transport, responder = await clock.bind_udp(clock.NtpResponder, "127.0.0.1")
    answers: asyncio.Queue = asyncio.Queue()

    class Asker(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            answers.put_nowait(data)

    asker, _ = await loop.create_datagram_endpoint(Asker, local_addr=("127.0.0.1", 0))
    try:
        assert responder.port in clock.TIMING_PORTS
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
    first, _ = await clock.bind_udp(asyncio.DatagramProtocol, "127.0.0.1")
    second, _ = await clock.bind_udp(asyncio.DatagramProtocol, "127.0.0.1")
    try:
        ports = [first.get_extra_info("sockname")[1], second.get_extra_info("sockname")[1]]
        assert all(p in clock.TIMING_PORTS for p in ports) and ports[0] != ports[1]
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


def test_pts_mapper_never_goes_backwards():
    mapper = clock.PtsMapper()
    first = mapper.map(100, 1_000_000_000)
    assert mapper.map(100, 1_000_000_000) == first + 1_000
    assert mapper.map(90, 990_000_000) > first


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


def test_mirror_command_test_pattern_and_desktop():
    settings = Settings(video_bitrate_kbps=4000)
    cmd = build_command("ffmpeg", settings, source=TEST_PATTERN, encoder="libx264", width=640, height=360, fps=30)
    assert "testsrc=size=640x360:rate=30" in cmd and cmd[-3:] == ["-f", "flv", "pipe:1"]
    assert cmd[cmd.index("-vf") + 1] == "format=yuv420p" and "-an" in cmd
    mon = ffmpeg.Monitor(1, 0, 0, 2560, 1440, dxgi_output=0)
    cmd = build_command("ffmpeg", settings, source=DESKTOP, encoder="h264_nvenc", width=1920, height=1080, fps=60,
                        backend="ddagrab", monitor=mon)
    assert "ddagrab=output_idx=0:framerate=60:draw_mouse=1" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease" in vf
    assert vf.endswith("format=nv12")


def test_mirror_encoder_args_run_with_libx264():
    ff = _ffmpeg()
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
           "-frames:v", "70", "-pix_fmt", "yuv420p", *ffmpeg.mirror_encoder_args("libx264", 1000, 30),
           "-f", "flv", "pipe:1"]
    tags = flv.FlvReader().feed(subprocess.run(cmd, check=True, capture_output=True, timeout=60).stdout)
    frames = [t for t in tags if t.kind == flv.FRAME]
    assert len(frames) == 70
    assert [i for i, t in enumerate(frames) if t.keyframe] == [0, 60]  # every 2 s, no scene cuts
    assert all(b.dts_ms == b.pts_ms for b in frames)  # no B-frames: nothing reordered


def test_stage_times_percentiles_and_summary(caplog):
    caplog.set_level("INFO")
    times = stats.StageTimes(kbps=8000, fps=30, lead_ms=100)
    for i in range(1, 101):
        times.add(1000, keyframe=i == 1, pipe=i * 1_000_000, send=500_000)
    times.add(400_000, keyframe=True, pipe=0, send=0)  # far over 8 frames' share of 8 Mbit/s
    times.drop()
    assert stats.percentile([float(i) for i in range(1, 101)], 0.5) == 51.0
    times.log_window()
    assert "pipe 50.0/95.0" in caplog.text and "dropped 1" in caplog.text
    summary = times.summary()
    assert summary["frames"] == 101 and summary["keyframes"] == 2 and summary["send"] == (0.5, 0.5)
    assert times.big_keyframes == 1 and summary["largest_keyframe"] == 400_000
