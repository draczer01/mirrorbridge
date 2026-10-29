"""Google Cast screen mirroring against a receiver in the same process (tests/fake_cast_tv.py):
the control connection, LAUNCH, OFFER and ANSWER, the video decrypted and decoded by FFmpeg,
lost packets sent again, and the ways a receiver says no or goes away."""

import asyncio
import struct
import subprocess
import sys

import pytest

from fake_cast_tv import AUDIO_RECEIVER_SSRC, RECEIVER_SSRC, TRANSPORT, FakeCastTV
from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.googlecast import CastDevice, CastMirrorError, GoogleCastMirror
from mirrorbridge.googlecast import session as gc_session
from mirrorbridge.googlecast.audio import AudioTimer, OggReader, has_opus, opus_samples
from mirrorbridge.googlecast.channel import NS_RECEIVER, NS_WEBRTC, decode_message, encode_message
from mirrorbridge.googlecast.offer import AnswerError, VideoOffer, parse_answer
from mirrorbridge.googlecast.rtp import (CAST, LAST_PACKET, PT_PSFB, PT_RR, FrameCipher, Packetizer, expand,
                                         parse_rtcp, sender_report)
from mirrorbridge.googlecast.sender import Clock
from mirrorbridge.mirror.clock import NS
from mirrorbridge.mirror import TEST_PATTERN
from mirrorbridge.session import State

SETTINGS = Settings(encoder="libx264", framerate=30, video_bitrate_kbps=1500, output_height=360)


def _ffmpeg() -> str:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264", mirror=True):
        pytest.skip("this FFmpeg has no libx264")
    return ff


def _decoded_frames(ff: str, annexb: bytes, tmp_path) -> int:
    path = tmp_path / "video.h264"
    path.write_bytes(annexb)
    out = subprocess.run([ff, "-hide_banner", "-v", "error", "-f", "h264", "-i", str(path), "-f", "null", "-"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    probe = subprocess.run([ff, "-hide_banner", "-f", "h264", "-i", str(path), "-f", "null", "-"],
                           capture_output=True, text=True, timeout=60)
    frames = [line for line in probe.stderr.replace("\r", "\n").splitlines() if line.startswith("frame=")]
    assert frames, probe.stderr
    return int(frames[-1].split("frame=")[1].split()[0])


async def _mirror(tv: FakeCastTV, seconds: float = 3, **kw) -> tuple[GoogleCastMirror, list]:
    states = []
    kw.setdefault("codec", "h264")  # the frames these tests check are H.264
    kw.setdefault("audio", False)
    session = GoogleCastMirror(CastDevice("Sala familiar", tv.host, tv.port), SETTINGS,
                               lambda state, message: states.append((state, message)), source=TEST_PATTERN,
                               seconds=seconds, **kw)
    await asyncio.wait_for(session.run(), 60)
    return session, states


# -- pieces ---------------------------------------------------------------------------------------


def test_cast_message_round_trip():
    raw = encode_message("sender-0", "receiver-0", NS_RECEIVER, '{"type":"PING"}')
    assert struct.unpack(">I", raw[:4])[0] == len(raw) - 4
    message = decode_message(raw[4:])
    assert message == {"source": "sender-0", "destination": "receiver-0", "namespace": NS_RECEIVER,
                       "payload": '{"type":"PING"}'}


def test_cipher_matches_chrome_nonce():
    cipher = FrameCipher(bytes(16), bytes(range(16)))
    # The frame id, big-endian, XORed into bytes 8 to 11 of the IV mask.
    assert cipher.nonce(0x01020304) == bytes(range(8)) + bytes([8 ^ 1, 9 ^ 2, 10 ^ 3, 11 ^ 4]) + bytes(range(12, 16))
    data = bytes(range(256)) * 10
    assert cipher.apply(7, cipher.apply(7, data)) == data
    assert cipher.apply(7, data) != cipher.apply(8, data)


def test_packets_split_evenly_with_cast_header():
    packetizer = Packetizer(ssrc=600000, payload_type=96, sequence=65535, max_payload=1000)
    packets = packetizer.packets(frame_id=258, rtp_time=90000, data=b"x" * 2500, keyframe=False)
    assert len(packets) == 3
    assert [len(p) - 19 for p in packets] == [834, 834, 832]
    first, last = packets[0], packets[-1]
    assert first[1] == 96 and last[1] == 96 | 0x80  # the marker on the last packet only
    assert struct.unpack_from(">H", first, 2)[0] == 65535 and struct.unpack_from(">H", last, 2)[0] == 1
    byte0, frame8, packet_id, last_id, ref = struct.unpack_from(">BBHHB", last, 12)
    assert (byte0, frame8, packet_id, last_id, ref) == (0x40, 2, 2, 2, 1)
    key = packetizer.packets(frame_id=5, rtp_time=0, data=b"k", keyframe=True)[0]
    assert key[12] == 0xC0 and key[18] == 5  # a keyframe refers to itself


def test_rtcp_feedback_and_report():
    body = struct.pack(">II4sBBH", RECEIVER_SSRC, 1, CAST, 9, 2, 180)
    body += struct.pack(">BHB", 10, 3, 0b101) + struct.pack(">BHB", 11, LAST_PACKET, 0)
    rr = struct.pack(">BBHI", 0x81, PT_RR, 7, RECEIVER_SSRC) + struct.pack(">IIIIII", 1, 0x05000007, 0, 0, 0, 0)
    psfb = struct.pack(">BBH", 0x80 | 15, PT_PSFB, len(body) // 4) + body
    pli = struct.pack(">BBHII", 0x81, PT_PSFB, 2, RECEIVER_SSRC, 1)
    rtcp = parse_rtcp(rr + psfb + pli)
    assert rtcp.ssrc == RECEIVER_SSRC and rtcp.pli
    assert rtcp.report["lost"] == 7 and rtcp.report["fraction_lost"] == 5
    assert rtcp.feedback.ack == 9 and rtcp.feedback.target_delay_ms == 180
    assert rtcp.feedback.losses == [(10, 3), (10, 4), (10, 6), (11, LAST_PACKET)]
    assert expand(2, 255) == 258 and expand(250, 260) == 250 and expand(0xFF, 0) == -1
    sr = sender_report(5, 1 << 40, 123, 4, 5)
    assert len(sr) == 28 and sr[1] == 200


def test_offer_and_answer():
    offer = VideoOffer(width=1280, height=720, fps=30, kbps=4000, delay_ms=200)
    stream = offer.message()["offer"]["supportedStreams"][0]
    assert stream["codecName"] == "h264" and stream["rtpPayloadType"] == 96 and stream["targetDelay"] == 200
    assert stream["timeBase"] == "1/90000" and stream["maxBitRate"] == 4000000 and len(stream["aesKey"]) == 32
    answer = parse_answer({"type": "ANSWER", "result": "ok", "answer": {
        "udpPort": 50000, "sendIndexes": [0], "ssrcs": [77],
        "constraints": {"video": {"maxDimensions": {"width": 1920, "height": 1080, "frameRate": "60000/1000"},
                                  "maxBitRate": 10000000, "maxDelay": 400}}}})
    assert (answer.udp_port, answer.receiver_ssrc, answer.max_fps, answer.max_kbps) == (50000, 77, 60.0, 10000)
    with pytest.raises(AnswerError, match="refused"):
        parse_answer({"type": "ANSWER", "result": "error", "error": {"code": 1, "description": "no codec"}})
    with pytest.raises(AnswerError, match="none of the video"):
        parse_answer({"type": "ANSWER", "result": "ok", "answer": {"udpPort": 1, "sendIndexes": [], "ssrcs": []}})


def test_delay_from_environment(monkeypatch):
    monkeypatch.setenv(gc_session.DELAY_ENV, "120")
    assert gc_session.choose_delay(None) == 120
    assert gc_session.choose_delay(80) == 80
    assert gc_session.choose_delay(5) == gc_session.MIN_DELAY_MS
    monkeypatch.setenv(gc_session.DELAY_ENV, "nonsense")
    assert gc_session.choose_delay(None) == gc_session.DEFAULT_DELAY_MS


# -- against the fake receiver ----------------------------------------------------------------------


@pytest.fixture
async def tv():
    tv = await FakeCastTV().start()
    yield tv
    await tv.stop()


async def test_mirrors_a_test_pattern(tv, tmp_path):
    ff = _ffmpeg()
    session, states = await _mirror(tv, seconds=3)
    assert State.CASTING in [state for state, _ in states]
    assert states[-1][0] is State.STOPPED and "Time limit" in states[-1][1]
    assert tv.kinds(NS_RECEIVER)[:1] == ["LAUNCH"] and "STOP" in tv.kinds(NS_RECEIVER) and tv.stopped
    assert tv.offer is not None and tv.offer["offer"]["castMode"] == "mirroring"
    assert tv.payload_types == {96}
    assert tv.sender_reports, "no RTCP sender report"
    assert tv.frames and tv.frames[0][1], "the first frame must be a keyframe"
    assert [frame_id for frame_id, _key, _data in tv.frames] == list(range(len(tv.frames)))
    assert tv.frames[0][2].startswith(b"\x00\x00\x00\x01") and tv.frames[0][2][4] & 0x1F == 7  # SPS first
    assert len(tv.frames) >= 60
    assert _decoded_frames(ff, tv.annexb(), tmp_path) == len(tv.frames)
    assert session.canvas == (640, 360)
    assert session.frames_shown == len(tv.frames)  # from the receiver's event log
    assert session.frames_reported


async def test_lost_packets_are_sent_again(tmp_path):
    ff = _ffmpeg()
    tv = await FakeCastTV(drop_every=4).start()
    tv.show_log = False  # a receiver without an event log: nothing to say what it showed
    try:
        session, _states = await _mirror(tv, seconds=3)
    finally:
        await tv.stop()
    assert tv.dropped > 10 and tv.resent_got >= tv.dropped - 3
    assert session._sender is None  # closed at the end
    assert len(tv.frames) >= 60
    assert session.frames_shown == 0 and not session.frames_reported
    assert _decoded_frames(ff, tv.annexb(), tmp_path) == len(tv.frames)


async def test_answer_limits_the_picture():
    _ffmpeg()
    tv = await FakeCastTV(constraints={"video": {"maxDimensions": {"width": 320, "height": 180,
                                                                   "frameRate": "15"}}}).start()
    try:
        session, _states = await _mirror(tv, seconds=1.5)
    finally:
        await tv.stop()
    assert session.canvas == (320, 180) and session.fps == 15 and session.picture == (320, 180)


async def test_receiver_that_cannot_launch():
    _ffmpeg()
    tv = await FakeCastTV(launch=False).start()
    try:
        with pytest.raises(CastMirrorError, match="did not start Cast screen mirroring"):
            await _mirror(tv)
    finally:
        await tv.stop()


async def test_receiver_that_refuses_the_offer():
    _ffmpeg()
    tv = await FakeCastTV(answer={"type": "ANSWER", "result": "error",
                                  "error": {"code": 10, "description": "unsupported codec"}}).start()
    try:
        with pytest.raises(CastMirrorError, match="unsupported codec"):
            await _mirror(tv)
    finally:
        await tv.stop()
    assert "OFFER" in tv.kinds(NS_WEBRTC)


async def test_tv_ends_mirroring(tv):
    _ffmpeg()

    async def end_soon():
        while not tv.frames:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        tv.end_app()

    ender = asyncio.ensure_future(end_soon())
    session, states = await _mirror(tv, seconds=20)
    await ender
    assert states[-1][0] is State.STOPPED and "ended screen mirroring" in states[-1][1]
    assert not tv.stopped  # it was the TV that stopped it: no STOP for an app that is gone


async def test_silent_receiver_ends_the_session(tv, monkeypatch):
    _ffmpeg()
    tv.silent = True
    monkeypatch.setattr(gc_session, "RTCP_SECONDS", 1.5)
    with pytest.raises(CastMirrorError, match="stopped answering"):
        await _mirror(tv, seconds=20)


async def test_stop_while_mirroring(tv):
    _ffmpeg()
    session = GoogleCastMirror(CastDevice("TV", tv.host, tv.port), SETTINGS, source=TEST_PATTERN)
    task = asyncio.ensure_future(session.run())
    while not tv.frames:
        await asyncio.sleep(0.05)
    await session.stop()
    await asyncio.wait_for(task, 20)
    assert session.state is State.STOPPED and tv.stopped
    assert any(dest == TRANSPORT for _ns, dest, _p in tv.messages)


async def test_mirrors_vp8(tv, tmp_path):
    ff = _ffmpeg()
    session, _states = await _mirror(tv, seconds=2.5, codec="vp8")
    stream = tv.offer["offer"]["supportedStreams"][0]
    assert stream["codecName"] == "vp8" and session.encoder == "libvpx"
    assert tv.frames and tv.frames[0][1] and not tv.frames[0][2][0] & 1  # a VP8 keyframe first
    ivf = bytearray(b"DKIF" + struct.pack("<HHIHHIIII", 0, 32, 0x30385056, 640, 360, 30, 1, len(tv.frames), 0))
    for index, (_id, _key, data) in enumerate(tv.frames):
        ivf += struct.pack("<IQ", len(data), index) + data
    path = tmp_path / "video.ivf"
    path.write_bytes(bytes(ivf))
    out = subprocess.run([ff, "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and not out.stderr, out.stderr
    assert len(tv.frames) >= 45 and session.frames_shown == len(tv.frames)


def test_receiver_event_log():
    items = struct.pack(">IIhHhHhH", 3000, 2 << 24, 0, 13 << 12, -20, 12 << 12, 7, 14 << 12)
    body = struct.pack(">I4s", RECEIVER_SSRC, CAST) + items
    rtcp = parse_rtcp(struct.pack(">BBH", 0x82, 204, len(body) // 4) + body)
    assert rtcp.log and rtcp.decoded == 1 and rtcp.played == [-20]


def test_codec_from_environment(monkeypatch):
    monkeypatch.setenv(gc_session.CODEC_ENV, "VP8")
    assert gc_session.choose_codec(None) == "vp8"
    assert gc_session.choose_codec("h264") == "h264"
    monkeypatch.setenv(gc_session.CODEC_ENV, "av1")
    assert gc_session.choose_codec(None) == "vp8"
    monkeypatch.delenv(gc_session.CODEC_ENV)
    assert gc_session.choose_codec(None) == "vp8"


# -- sound ----------------------------------------------------------------------------------------


def test_offer_and_answer_with_sound():
    offer = VideoOffer(width=1280, height=720, fps=30, kbps=4000, delay_ms=400, audio=True).message()
    video, sound = offer["offer"]["supportedStreams"]
    assert sound["type"] == "audio_source" and sound["codecName"] == "opus" and sound["rtpPayloadType"] == 127
    assert sound["timeBase"] == "1/48000" and sound["channels"] == 2 and sound["bitRate"] == 102000
    assert sound["aesKey"] == video["aesKey"] and 1 <= sound["ssrc"] <= 500000 and sound["index"] != video["index"]
    answer = parse_answer({"type": "ANSWER", "result": "ok", "answer": {
        "udpPort": 5000, "sendIndexes": [0, 1], "ssrcs": [11, 12], "constraints": {"audio": {"maxBitRate": 64000}}}})
    assert answer.receiver_ssrc == 11 and answer.audio_receiver_ssrc == 12 and answer.audio_max_bps == 64000
    alone = parse_answer({"type": "ANSWER", "answer": {"udpPort": 5000, "sendIndexes": [0], "ssrcs": [11]}})
    assert alone.audio_receiver_ssrc is None


def _page(packets: list, granule: int = 0) -> bytes:
    lacing, data = b"", b""
    for packet in packets:
        lacing += bytes([255] * (len(packet) // 255) + [len(packet) % 255])
        data += packet
    return b"OggS" + struct.pack("<BBQIIIB", 0, 0, granule, 1, 0, 0, len(lacing)) + lacing + data


def test_ogg_and_opus_frames():
    head = _page([b"OpusHead" + bytes(11)]) + _page([b"OpusTags" + bytes(8)])
    long = bytes([0xF8]) + bytes(300)  # CELT fullband 20 ms, one frame, longer than a lacing value
    stream = head + _page([bytes([0xF0, 1, 2]), long])
    reader = OggReader()
    packets = reader.feed(stream[:40]) + reader.feed(stream[40:])
    assert packets == [bytes([0xF0, 1, 2]), long]
    assert [opus_samples(p) for p in packets] == [480, 960]
    assert opus_samples(bytes([0xF3, 3])) == 3 * 480  # code 3: the count in the next byte


def test_audio_timer_follows_the_clock():
    clock = Clock()
    timer = AudioTimer(clock)
    start = clock.start_ns + NS
    first = timer.stamp(start, 480)
    assert first == clock.rtp(start - 25 * NS // 1000, 48000)
    assert timer.stamp(start + 10 * NS // 1000, 480) == first + 480  # on time: counted on
    later = timer.stamp(start + NS, 480)  # a second of silence: started again
    assert timer.restarts == 1 and later == clock.rtp(start + NS - 25 * NS // 1000, 48000)
    # Half a second of sound held up and let go at once: left out until the count is on time.
    t, stamps = start + NS, []
    for _ in range(50):
        stamps.append(timer.stamp(t, 480))
    assert stamps[:4].count(None) == 0 and stamps.count(None) == 46
    t += 10 * NS // 1000
    assert timer.stamp(t, 480) is not None


def test_audio_lead_from_environment(monkeypatch):
    monkeypatch.setenv("MIRRORBRIDGE_CAST_AUDIO_LEAD_MS", "300")
    clock = Clock()
    timer = AudioTimer(clock)
    assert timer.lead_ms == 300
    first = timer.stamp(clock.start_ns + NS, 480)
    assert first == clock.rtp(clock.start_ns + NS - 300 * NS // 1000, 48000)
    assert timer.stamp(clock.start_ns + NS + 10 * NS // 1000, 480) == first + 480 and timer.restarts == 0
    monkeypatch.setenv("MIRRORBRIDGE_CAST_AUDIO_LEAD_MS", "x")
    assert AudioTimer(clock).lead_ms == 25


def _need_opus() -> str:
    ff = _ffmpeg()
    if not has_opus(ff):
        pytest.skip("this FFmpeg has no libopus")
    return ff


async def test_mirrors_sound(tv):
    _need_opus()
    session, _states = await _mirror(tv, seconds=3, audio=True)
    sound = tv.audio
    assert sound is not None and sound.receiver_ssrc == AUDIO_RECEIVER_SSRC
    assert tv.payload_types == {96, 127}
    delays = {st["type"]: st["targetDelay"] for st in tv.offer["offer"]["supportedStreams"]}
    assert delays == {"video_source": 400, "audio_source": 100}  # the TCL plays sound ~0.4 s late
    assert len(sound.frames) >= 200  # 100 a second
    assert all(opus_samples(data) == 480 for _id, _key, data in sound.frames)  # decrypted Opus, 10 ms each
    steps = [b - a for a, b in zip(sound.rtp_times, sound.rtp_times[1:])]
    assert steps.count(480) >= len(steps) - 2
    assert tv.audio_reports, "no sender report for the sound"
    # The sound starts with the picture: the first frames of each are timed alike.
    video = tv.video
    assert abs(sound.rtp_times[0] / 48000 - video.rtp_times[0] / 90000) < 0.3
    assert not session.sound  # over


async def test_receiver_without_sound():
    _need_opus()
    tv = await FakeCastTV().start()
    tv.take_audio = False
    try:
        session, _states = await _mirror(tv, seconds=2, audio=True)
    finally:
        await tv.stop()
    assert tv.audio is None and tv.payload_types == {96} and len(tv.frames) >= 30
    assert session.frames_shown == len(tv.frames)


def test_speakers_are_muted_and_put_back(monkeypatch):
    from mirrorbridge import speakers

    class FakeVolume:
        state = {"muted": False, "released": 0}

        def muted(self):
            return self.state["muted"]

        def set_muted(self, muted):
            self.state["muted"] = muted

        def release(self):
            self.state["released"] += 1

    monkeypatch.setattr(speakers.sys, "platform", "win32")
    monkeypatch.setattr(speakers, "_Volume", FakeVolume)
    muted = speakers.MutedSpeakers()
    assert muted.mute() and FakeVolume.state["muted"]
    assert not muted.mute()  # once
    muted.restore()
    assert not FakeVolume.state["muted"] and FakeVolume.state["released"] == 1
    FakeVolume.state["muted"] = True  # muted by the person already: left alone
    again = speakers.MutedSpeakers()
    assert not again.mute()
    again.restore()
    assert FakeVolume.state["muted"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' audio endpoints")
def test_speakers_through_com_do_not_crash():
    # A CI machine may have no speakers (then nothing is muted); either way the COM calls must
    # neither crash nor leave anything muted.
    from mirrorbridge import speakers

    muted = speakers.MutedSpeakers()
    did = muted.mute()
    muted.restore()
    if did:
        volume = speakers._Volume()  # pylint: disable=protected-access
        assert not volume.muted()
        volume.release()
