"""Screen mirroring to a TV in the same process (tests/fake_mirror_tv.py), a TCL 55QM7L by
default: pairing, SETUP in its shapes and the fallbacks, the clock, the event channel, video
decrypted and decoded by FFmpeg, the probe, and the ways a TV says no, is slow, or goes away."""

import asyncio
import dataclasses
import logging
import statistics
import subprocess
import sys
import uuid

import pytest

from fake_mirror_tv import CREDENTIALS, PIN, FakeMirrorTV
from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.mirror import TEST_PATTERN, MirrorError, MirrorSession, probe_mirroring, profiles
from mirrorbridge.mirror import session as mirror_session
from mirrorbridge.mirror.clock import NS, from_ntp
from mirrorbridge.mirror.control import USER_AGENT, ControlChannel, Target, open_event_channel
from mirrorbridge.mirror.h264 import TRAILER_FIXED, TRAILER_ISO
from mirrorbridge.mirror.negotiation import CONTROL_FIRST, MEDIA_FIRST, NTP, PTP, Negotiator, Plan
from mirrorbridge.session import State
from mirrorbridge.text import Text

SETTINGS = Settings(encoder="libx264", framerate=30, video_bitrate_kbps=1500, output_height=360)
SMALL_BUFFERS = pytest.mark.skipif(sys.platform == "win32", reason="Windows holds sends in buffers of its own that "
                                   "the test cannot keep small (Wine leaves out SO_SNDBUF), so a TV that reads slowly "
                                   "or stops for a moment shows only after the session, or not at all")


def _ffmpeg() -> str:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264", mirror=True):
        pytest.skip("this FFmpeg has no libx264")
    return ff


@pytest.fixture(autouse=True)
def app_data(tmp_path, monkeypatch):
    """%APPDATA% (~/.config off Windows) in tmp_path, so mirroring.json starts empty."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "MirrorBridge"


@pytest.fixture
async def tv():
    tv = await FakeMirrorTV().start()
    yield tv
    await tv.stop()


async def _negotiator(tv, **kw) -> Negotiator:
    negotiator = Negotiator(tv.target(), timing_port=7880, audio_port=7881, **kw)
    await negotiator.connect()
    return negotiator


def _device(tv, credentials=None):
    """The fake TV as discovery would find it (devices.Device), with or without a saved pairing."""
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import Protocol

    from mirrorbridge.devices import Device

    conf = AppleTV("127.0.0.1", tv.profile.name)
    conf.add_service(ManualService("fake-tv", Protocol.AirPlay, tv.port,
                                   {"features": "0x7F8AD0,0x38BCF46", "srcvers": "377.40.00", "model": "55QM7L",
                                    "manufacturer": "TCL"}, credentials=credentials))
    return Device(conf)


def _decode(ff: str, stream: bytes, tmp_path) -> tuple[int, str]:
    """How many frames FFmpeg decodes from an H.264 stream, and their size."""
    path = tmp_path / "received.h264"
    path.write_bytes(stream)
    out = subprocess.run([ff, "-hide_banner", "-v", "error", "-f", "h264", "-i", str(path), "-f", "framecrc", "-"],
                         capture_output=True, text=True, timeout=60, check=True)
    assert out.stderr == ""
    lines = out.stdout.splitlines()
    size = next(line.split(":")[1].strip() for line in lines if line.startswith("#dimensions"))
    return sum(1 for line in lines if line and not line.startswith("#")), size


# -- pairing and the control connection -----------------------------------------------------------------


async def test_pin_pairing_then_pair_verify_and_encrypted_info(tv):
    from pyatv.storage.memory_storage import MemoryStorage

    from mirrorbridge.devices import DeviceManager

    device = _device(tv)
    manager = DeviceManager(asyncio.get_running_loop(), storage=MemoryStorage())

    async def pin(*_):
        return str(PIN)

    await manager.pair(device, pin)
    target = Target.from_device(device)
    assert target.credentials and not target.needs_fairplay and target.srcvers == "377.40.00"
    channel = await ControlChannel.open(target)
    try:
        info = await channel.info()
    finally:
        channel.close()
    assert tv.info_encrypted is True
    assert info.displays[0].width == 1920 and info.displays[0].max_fps == 60
    assert info.features == tv.profile.features and info.screen_formats == 0x1C0000


async def test_a_tv_that_dropped_the_pairing_says_so_in_m4(tv):
    tv.reconfigure(m4_error=True)
    with pytest.raises(MirrorError) as caught:
        await ControlChannel.open(tv.target())
    assert caught.value.args[0].key == "mirror_verify_refused"
    assert "M4 error 2" in str(caught.value) and caught.value.step == "pair-verify"


async def test_not_paired(tv):
    with pytest.raises(MirrorError) as caught:
        await ControlChannel.open(tv.target(credentials=None))
    assert caught.value.args[0].key == "mirror_not_paired" and caught.value.step == "pair-verify"
    await asyncio.sleep(0.05)
    assert all(conn.closed for conn in tv.connections)


async def test_a_tv_that_is_not_there():
    with pytest.raises(MirrorError) as caught:
        await ControlChannel.open(Target("TV", "127.0.0.1", 9, credentials=CREDENTIALS))
    assert caught.value.args[0].key == "mirror_connect_failed"


@pytest.mark.parametrize("change, reason", [
    ({"m2_error": True}, "M2 error 2"),
    ({"m2_garbage": True}, "M2 could not be decrypted"),
])
async def test_pair_verify_answers_that_say_no_are_texts(tv, change, reason):
    tv.reconfigure(**change)
    with pytest.raises(MirrorError) as caught:
        await ControlChannel.open(tv.target())
    assert caught.value.args[0].key == "mirror_verify_refused" and reason in str(caught.value)
    assert isinstance(caught.value.args[0].values["reason"], str)


async def test_stopping_during_pair_verify_closes_the_connection(tv):
    tv.reconfigure(verify_delay=5)
    opening = asyncio.ensure_future(ControlChannel.open(tv.target()))
    await asyncio.sleep(0.3)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    await asyncio.sleep(0.1)
    assert len(tv.connections) == 1 and tv.connections[0].closed


async def test_a_late_answer_to_get_info_is_not_taken_for_a_setups(tv):
    """pyatv hands answers to requests in order: an answer that comes after its request gave
    up would be taken for the next one's. SETUP then goes on a new connection."""
    tv.reconfigure(info_delay=0.5)
    negotiator = await _negotiator(tv)
    with pytest.raises(MirrorError) as caught:
        await negotiator.channel.info(timeout=0.1)
    assert caught.value.args[0].key == "mirror_no_info" and negotiator.channel.out_of_step
    est = await negotiator.establish(Plan())
    try:
        await asyncio.sleep(0.6)  # the /info answer has come by now, on the old connection
        # the stale connection, control-first (refused) and media-first, each on its own
        assert est.plan.ordering == MEDIA_FIRST and est.data_port and len(tv.connections) == 3
        assert tv.connections[0].closed and not tv.connections[-1].closed
        assert [s.get("type") for r in est.replies for s in r.get("streams", [])] == [96, 110]
        assert not negotiator.channel.out_of_step
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


# -- SETUP ------------------------------------------------------------------------------------------------


async def test_tcl_refuses_control_first_and_takes_media_first(tv):
    negotiator = await _negotiator(tv)
    answers = []
    negotiator.on_reply = answers.append
    est = await negotiator.establish(Plan())
    try:
        assert est.plan.ordering == MEDIA_FIRST and est.plan.timing == NTP
        assert negotiator.steps["setup control-first"] == 400 and negotiator.steps["setup media-first"] == 200
        assert "control-first SETUP refused with 400" in est.fallbacks[0]
        assert est.data_port and est.event_port and est.receiver_timing_port
        assert answers == est.replies and answers[0]["timingPort"] == est.receiver_timing_port  # as they came
        assert len(tv.connections) == 2  # the other ordering on a fresh pair-verify
        assert tv.user_agents == {USER_AGENT} and USER_AGENT.endswith(tv.setups[-2]["sourceVersion"])
        first, video = tv.setups[-2:]
        assert first["isScreenMirroringSession"] and first["timingProtocol"] == "NTP"
        assert first["timingPort"] == 7880 and "timingPeerInfo" not in first
        audio = first["streams"][0]
        assert audio["type"] == 96 and audio["ct"] == 2 and audio["controlPort"] == 7881 and len(audio["shk"]) == 32
        assert video["streams"][0]["type"] == 110 and video["streams"][0]["streamConnectionID"] == est.video_id
        assert "sessionUUID" not in video
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()
    assert tv.teardowns == 1


async def test_control_first_receiver_with_a_media_first_profile_falls_back(tv):
    tv.reconfigure(refuse=(MEDIA_FIRST,), audio_first=False, event_port=False)
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan(MEDIA_FIRST, audio=False))
    try:
        assert est.plan.ordering == CONTROL_FIRST and tv.orderings == [CONTROL_FIRST]
        session, video = tv.setups[-2:]
        assert "streams" not in session and session["combinedGetInfoWithControlSetup"] is True
        assert [s["type"] for s in video["streams"]] == [110] and est.event_port is None
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


async def test_a_tv_that_closes_the_connection_after_saying_no(tv):
    tv.reconfigure(close_after_refusal=True)
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan())
    try:
        assert est.plan.ordering == MEDIA_FIRST and tv.codes("SETUP") == [400, 200, 200]
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


async def test_setup_waits_as_long_as_it_is_told(tv):
    """A TV may take longer than pyatv's 4-10 s to answer SETUP: Negotiator.setup_seconds
    (SETUP_SECONDS, 30 s) is what counts, shortened here."""
    tv.reconfigure(setup_delay=0.4, refuse=())
    negotiator = await _negotiator(tv, setup_seconds=0.1)
    try:
        with pytest.raises(MirrorError) as caught:
            await negotiator.establish(Plan(fallback=False))
        assert caught.value.args[0].key == "mirror_no_answer" and "no answer" in str(caught.value)
    finally:
        negotiator.close()
    negotiator = await _negotiator(tv, setup_seconds=2)
    est = await negotiator.establish(Plan(fallback=False))
    try:
        assert est.plan.ordering == CONTROL_FIRST and est.data_port
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


async def test_skip_record_is_honoured(tv):
    tv.reconfigure(skip_record=True)
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan())
    try:
        assert est.skip_record
        await negotiator.record(est)
        assert negotiator.steps["record"] == "skipped" and tv.records == 0
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


async def test_an_event_channel_that_opens_late(tv):
    tv.reconfigure(event_port_delay=1.0)
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan())
    try:
        transport = await open_event_channel(negotiator.channel, est.event_port)
        assert transport is not None
        await asyncio.sleep(0.2)
        assert tv.event_answers == [200]
        transport.close()
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


async def test_without_fallback_a_refused_shape_is_an_error(tv):
    negotiator = await _negotiator(tv)
    try:
        with pytest.raises(MirrorError) as caught:
            await negotiator.establish(Plan(CONTROL_FIRST, fallback=False))
    finally:
        negotiator.close()
    assert caught.value.code == 400 and caught.value.args[0].key == "mirror_refused"
    assert "setup control-first=400" in str(caught.value)


async def test_tcl_needs_the_audio_stream(tv):
    negotiator = await _negotiator(tv)
    try:
        with pytest.raises(MirrorError):
            await negotiator.establish(Plan(MEDIA_FIRST, audio=False, fallback=False))
    finally:
        negotiator.close()
    assert tv.codes("SETUP") == [400]


@pytest.mark.parametrize("clock_id", [None, 0x1234ABCD])
async def test_ptp_answer_is_torn_down_and_ntp_set_up_on_a_fresh_pair_verify(tv, clock_id):
    tv.reconfigure(ptp_clock_id=clock_id)
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan(MEDIA_FIRST, timing=PTP))
    try:
        assert est.plan.timing == NTP and est.ptp_answered and est.ptp_clock_id == clock_id
        assert ("without timingPeerInfo.ClockID" if clock_id is None else "ClockID 0x1234abcd") in est.fallbacks[0]
        ptp, ntp = tv.setups[0], tv.setups[1]
        assert ptp["timingProtocol"] == "PTP" and "timingPort" not in ptp
        peer = ptp["timingPeerInfo"]
        assert peer["Addresses"] == ["127.0.0.1"] and uuid.UUID(peer["ID"]) and ptp["timingPeerList"] == [peer]
        assert peer["SupportsClockPortMatchingOverride"] is True and peer["DeviceType"] == 0
        assert ntp["timingProtocol"] == "NTP"
        assert tv.teardowns == 1 and len(tv.connections) == 2  # torn down, then a new pair-verify
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


async def test_ntp_after_ptp_starts_from_the_ordering_the_tv_took(tv):
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan(CONTROL_FIRST, timing=PTP))
    try:
        assert est.plan == Plan(MEDIA_FIRST, timing=NTP)
        # control-first PTP refused, media-first PTP answered; then media-first NTP at once
        assert tv.codes("SETUP") == [400, 200, 200, 200] and tv.orderings == [MEDIA_FIRST, MEDIA_FIRST]
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


async def test_ptp_without_fallback_says_ptp_is_missing(tv):
    negotiator = await _negotiator(tv)
    try:
        with pytest.raises(MirrorError) as caught:
            await negotiator.establish(Plan(MEDIA_FIRST, timing=PTP, fallback=False))
    finally:
        negotiator.close()
    assert caught.value.args[0].key == "mirror_no_ptp" and tv.teardowns == 1


# -- whole sessions --------------------------------------------------------------------------------------


async def test_two_seconds_of_test_pattern_reach_the_tv_and_decode(tv, tmp_path, app_data):
    ff = _ffmpeg()
    states = []
    session = MirrorSession(tv.target(), SETTINGS, lambda s, m: states.append((s, str(m))),
                            source=TEST_PATTERN, seconds=2)
    await asyncio.wait_for(session.run(), 60)
    report = session.report

    assert [s for s, _ in states][-1] == State.STOPPED and states[-1][1] == "Mirrored for 2 s, as asked"
    assert (State.CASTING, "Mirroring the test pattern to TCL 55QM7L (640x360, 30 fps)") in states
    assert report.steps["setup control-first"] == 400 and report.steps["record"] == 200
    assert report.steps["teardown"] == 200 and tv.records == 1 and tv.teardowns == 1
    assert report.canvas == report.picture == (640, 360) and report.data_channel == "open"
    # The TV asked for the time, got good answers, and our probe reached its timing port.
    assert len(tv.ntp_answers) >= 3 and tv.ntp_bad == [] and tv.ntp_probes >= 1
    assert report.ntp_requests >= len(tv.ntp_answers)
    assert tv.event_answers == [200] and report.feedback.get("200", 0) >= 1
    assert all(code == 200 for code in tv.codes("POST"))

    (video,) = tv.videos
    assert video.errors == [] and video.heartbeats >= 1 and not video.closed_by_us
    assert len(video.frames) == report.frames_sent >= 40
    ts, width, height, avcc = video.codec_packets[0]
    assert (width, height) == (640.0, 360.0) and avcc.endswith(bytes.fromhex("02000000"))
    assert ts == video.frames[0][0] and video.frames[0][1]  # the codec packet goes with the first IDR
    # Each frame is to be shown its lead (100 ms) after it was taken, so a little less after it
    # arrived: never more, and most frames arrive well before (a busy machine may delay a few).
    ahead = [from_ntp(stamp) - arrived for (stamp, _, _), arrived in zip(video.frames, video.arrived)]
    assert max(ahead) <= 100 * NS // 1000 and statistics.median(ahead) > 40 * NS // 1000
    assert sum(1 for a in ahead if a > 40 * NS // 1000) >= 0.9 * len(ahead)
    # Paced from the first frame: no burst of frames stamped a few ms apart at the start.
    assert statistics.median(b[0] - a[0] for a, b in zip(video.frames[:10], video.frames[1:11])) > (20 << 32) // 1000
    frames, size = _decode(ff, bytes(video.annexb), tmp_path)
    assert frames == len(video.frames) and size == "640x360"

    assert report.latency["frames"] == report.frames_sent and "send" in report.latency and "behind" in report.latency
    saved = profiles.load("fake-tv")
    assert saved is not None and saved.ordering == MEDIA_FIRST and saved.trailer == TRAILER_FIXED
    assert (app_data / "mirroring.json").is_file()


async def test_desktop_capture_is_scaled_to_the_tv(tv):
    """The desktop path (ffmpeg.py's capture input, then fitted into the TV's screen), with
    FFmpeg's 1080p test source standing in for the screen."""
    _ffmpeg()
    tv.reconfigure(width=1280, height=720, max_fps=25)
    settings = Settings(encoder="libx264", capture_backend="test", framerate=60, video_bitrate_kbps=1500,
                        output_height=0)
    states = []
    session = MirrorSession(tv.target(), settings, lambda s, m: states.append(str(m)), seconds=1)
    await asyncio.wait_for(session.run(), 60)
    assert "Mirroring this PC's screen to TCL 55QM7L (1280x720, 25 fps)" in states
    assert session.report.picture == (1280, 720) and tv.videos[0].codec_packets[0][1:3] == (1280.0, 720.0)


async def test_saved_profile_is_asked_for_first(tv):
    _ffmpeg()
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, trailer=TRAILER_ISO))
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=1, remember=False)
    await asyncio.wait_for(session.run(), 60)
    assert tv.codes("SETUP") == [200, 200] and tv.orderings == [MEDIA_FIRST]
    assert tv.videos[0].codec_packets[0][3].endswith(bytes.fromhex("fdf8f800"))


async def test_the_debug_switches_reach_the_tv_and_a_test_lead_is_not_saved(tv, caplog):
    """More options > Debug: a TV wait of 300 ms (in SETUP, the frames' times and the stats), a
    keyframe every second and frames shown as they come; mirroring.json keeps the TV's own lead."""
    _ffmpeg()
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, lead_ms=150))
    settings = dataclasses.replace(SETTINGS, mirror_lead_ms=300, mirror_keyframe_seconds=1, mirror_on_arrival=True)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=2.5)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    assert "Mirroring with lead 300 ms (test value), keyframe every 1 s, frames shown on arrival" in caplog.text
    assert tv.video_streams[0]["latencyMs"] == 300 and tv.setups[0]["streams"][0]["latencyMax"] == 300 * 44100 // 1000
    assert session.report.established.plan.lead_ms == 300
    assert session._stats.lead_ms == 300  # pylint: disable=protected-access
    (video,) = tv.videos
    ahead = [from_ntp(stamp) - arrived for (stamp, _, _), arrived in zip(video.frames, video.arrived)]
    assert max(ahead) <= 300 * NS // 1000 and statistics.median(ahead) > 240 * NS // 1000
    assert video.errors == [] and video.codec_subtypes == [b"\x14\x01"]
    assert [n for n, (_, idr, _) in enumerate(video.frames) if idr][:3] == [0, 30, 60]
    assert profiles.load("fake-tv").lead_ms == 150  # the TV's own, not the test value


async def test_a_first_session_tries_the_other_avcc_ending_once(tv):
    _ffmpeg()
    tv.reconfigure(want_trailer=bytes.fromhex("fdf8f800"))
    states = []
    session = MirrorSession(tv.target(), SETTINGS, lambda s, m: states.append((s, str(m))), source=TEST_PATTERN,
                            seconds=1)
    await asyncio.wait_for(session.run(), 60)
    assert session.state == State.STOPPED and session.error is None
    assert (State.STARTING, "The TV dropped the video at once: trying again with the other ending of the H.264 "
            "header") in states
    first, second = tv.videos
    assert first.closed_by_us and first.codec_packets[0][3].endswith(bytes.fromhex("02000000"))
    assert not second.closed_by_us and len(second.frames) >= 20 and tv.teardowns == 2
    assert session.report.trailer == TRAILER_ISO and "avcC ending 02000000" in session.report.retried
    assert profiles.load("fake-tv").trailer == TRAILER_ISO


async def test_a_saved_avcc_ending_is_not_changed_behind_the_users_back(tv):
    _ffmpeg()
    tv.reconfigure(want_trailer=bytes.fromhex("fdf8f800"))
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, trailer=TRAILER_FIXED))
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=5)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_data_closed" and len(tv.videos) == 1


async def test_a_tv_that_names_its_screen_only_once_the_session_exists(tv):
    _ffmpeg()
    tv.reconfigure(displays_before_setup=False, width=480, height=270)
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=1, remember=False)
    await asyncio.wait_for(session.run(), 60)
    assert tv.info_requests == 2 and session.report.canvas == session.report.picture == (480, 270)


async def test_stop_while_mirroring_tears_down(tv):
    _ffmpeg()
    casting = asyncio.Event()
    session = MirrorSession(tv.target(), SETTINGS, lambda s, m: s == State.CASTING and casting.set(),
                            source=TEST_PATTERN)
    task = asyncio.ensure_future(session.run())
    await asyncio.wait_for(casting.wait(), 30)
    await session.stop()
    await asyncio.wait_for(task, 30)
    assert session.state == State.STOPPED and tv.teardowns == 1
    assert session._ffmpeg.returncode is not None  # pylint: disable=protected-access


async def test_a_tv_that_never_asks_for_the_time_points_at_the_firewall(tv, monkeypatch):
    _ffmpeg()
    tv.reconfigure(send_ntp=False)
    monkeypatch.setattr(mirror_session, "NTP_GRACE_SECONDS", 0.5)
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=10)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_no_timing" and "Windows Firewall" in str(caught.value)
    assert session.state == State.FAILED and tv.teardowns == 1
    assert profiles.load("fake-tv") is None


async def test_a_tv_that_drops_the_video_fails_the_session(tv):
    _ffmpeg()
    tv.reconfigure(drop_video_after=3)
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=10)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_data_closed"
    assert session.report.data_closed_after is not None and session.report.data_closed_after >= 2
    # Dropped at once both times: the other avcC ending was tried too, then nothing is saved.
    assert len(tv.videos) == 2 and tv.teardowns == 2 and profiles.load("fake-tv") is None


async def test_ending_mirroring_on_the_tv_stops_the_session(tv):
    _ffmpeg()
    states = []
    casting = asyncio.Event()

    def status(state, message):
        states.append((state, str(message)))
        if state == State.CASTING:
            casting.set()

    session = MirrorSession(tv.target(), SETTINGS, status, source=TEST_PATTERN, remember=False)
    task = asyncio.ensure_future(session.run())
    await asyncio.wait_for(casting.wait(), 30)
    await asyncio.sleep(2.5)  # past the second keyframe
    tv.end_mirroring()
    await asyncio.wait_for(task, 30)
    assert session.state == State.STOPPED and session.error is None
    assert states[-1] == (State.STOPPED, "The TV ended screen mirroring")


async def test_frames_the_tv_cannot_take_in_time_are_dropped_up_to_a_keyframe(tv, monkeypatch, tmp_path):
    ff = _ffmpeg()
    monkeypatch.setattr(mirror_session, "CATCH_UP_MS", -10_000)  # as if every frame were far past its lead
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=2.5, remember=False)
    await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    assert session.report.latency["dropped"] > 40 and len(video.frames) >= 2
    assert all(idr for _, idr, _ in video.frames)  # only keyframes got through, each a fresh start
    assert _decode(ff, bytes(video.annexb), tmp_path)[0] == len(video.frames)


@SMALL_BUFFERS
async def test_a_network_that_holds_the_video_up_for_a_moment_is_ridden_out(tv, tmp_path, caplog, monkeypatch):
    """The TV reads none of the video for 0.6 s, as Wi-Fi held a TCL's up now and then: the
    frames that waited go late, with the times they were taken for, and the TV goes on with
    them, P-frames, instead of every frame up to the next keyframe being dropped (2 s of a
    frozen picture)."""
    ff = _ffmpeg()
    real_build_command = mirror_session.build_command

    def noisy(ff, settings, **kw):  # frames as big as a busy screen's: the buffers on the way hide less
        cmd = real_build_command(ff, settings, **kw)
        at = next(n for n, arg in enumerate(cmd) if arg.startswith("testsrc="))
        cmd[at] += ",noise=alls=100:allf=t"
        return cmd

    monkeypatch.setattr(mirror_session, "build_command", noisy)
    tv.reconfigure(pause_after=15, pause_seconds=0.6, video_buffer=4096)
    settings = dataclasses.replace(SETTINGS, video_bitrate_kbps=4000)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=3, remember=False)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    latency = session.report.latency
    assert video.errors == [] and video.paused_at == 15
    assert latency["dropped"] == 0 and latency["late"] > 0 and len(video.frames) == latency["frames"] >= 61
    # Every frame up to the next keyframe (every 2 s) is a P-frame, those that waited too.
    assert [n for n, (_, idr, _) in enumerate(video.frames) if idr] == [0, 60]
    # Each keeps the time it was taken for, a frame after the one before, so those that waited
    # came past it.
    shown = [from_ntp(stamp) for stamp, _, _ in video.frames]
    assert all(20 * NS // 1000 < b - a < 50 * NS // 1000 for a, b in zip(shown[10:40], shown[11:40]))
    assert sum(1 for at, arrived in zip(shown, video.arrived) if arrived > at) >= 5
    assert _decode(ff, bytes(video.annexb), tmp_path)[0] == len(video.frames)
    assert "frames late instead of dropping them" in caplog.text and "Dropping frames" not in caplog.text


@SMALL_BUFFERS
async def test_a_slow_tv_gets_fewer_frames_not_later_ones(tv):
    """The video connection's buffers are kept to about two frames (session.send_buffer), so
    that a TV that reads slowly pushes back at once; once frames are more than CATCH_UP_MS past
    their lead, they are dropped rather than queued."""
    _ffmpeg()
    tv.reconfigure(read_kbps=60, video_buffer=8192)
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=8, remember=False)
    await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    assert session.report.latency["dropped"] > 0 and session.error is None
    assert video.errors == [] and video.frames and video.frames[0][1]


async def test_a_tv_that_stops_reading_the_video_ends_the_session(tv, monkeypatch):
    _ffmpeg()
    tv.reconfigure(stall_after=5, video_buffer=8192)
    monkeypatch.setattr(mirror_session, "VIDEO_STALL_SECONDS", 1.0)
    session = MirrorSession(tv.target(), Settings(encoder="libx264", framerate=30, video_bitrate_kbps=8000,
                                                  output_height=720), source=TEST_PATTERN)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_video_stalled" and session.state == State.FAILED
    assert session._frames.frames <= session._frames.limit  # pylint: disable=protected-access
    assert tv.teardowns == 1


async def test_an_encoder_that_gives_nothing_ends_the_session(tv, monkeypatch):
    _ffmpeg()
    monkeypatch.setattr(mirror_session, "build_command",
                        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(30)"])
    monkeypatch.setattr(mirror_session, "FIRST_FRAME_SECONDS", 1)
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_no_picture" and "for 1 s" in str(caught.value)
    assert session.state == State.FAILED and tv.teardowns == 1


async def test_a_gpu_encoder_that_will_not_start_gives_way_to_x264(tv, monkeypatch, caplog):
    """As a cast does when NVENC is out of sessions: the session the TV set up goes on, with the
    CPU encoder. (NVENC is made to fail here: on a PC with an NVIDIA GPU it would start.)"""
    _ffmpeg()
    real_build_command = mirror_session.build_command

    def nvenc_fails(ff, settings, **kw):
        if kw["encoder"] == "h264_nvenc":
            return [sys.executable, "-c", "import sys; sys.exit(1)"]
        return real_build_command(ff, settings, **kw)

    monkeypatch.setattr(ffmpeg, "pick_encoder", lambda *args: "h264_nvenc")
    monkeypatch.setattr(mirror_session, "build_command", nvenc_fails)
    states = []
    session = MirrorSession(tv.target(), SETTINGS, lambda s, m: states.append((s, str(m))), source=TEST_PATTERN,
                            seconds=1)
    with caplog.at_level(logging.WARNING, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    assert session.state == State.STOPPED and session.error is None
    starting = [message for state, message in states if state == State.STARTING]
    assert starting[-2:] == ["NVIDIA NVENC via testsrc", "x264 (CPU) via testsrc"]
    assert session.report.encoder == "libx264" and session.report.frames_sent > 0
    assert tv.records == 1 and tv.teardowns == 1  # one session, set up once
    assert "test with h264_nvenc failed, trying the next option" in caplog.text


async def test_when_every_way_of_encoding_fails_the_session_says_so(tv, monkeypatch):
    _ffmpeg()
    commands = []

    def failing(ff, settings, **kw):
        commands.append((kw["backend"], kw["encoder"]))
        return [sys.executable, "-c", "import sys; sys.exit(1)"]

    monkeypatch.setattr(ffmpeg, "pick_encoder", lambda *args: "h264_nvenc")
    monkeypatch.setattr(mirror_session, "build_command", failing)
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert commands == [(TEST_PATTERN, "h264_nvenc"), (TEST_PATTERN, "libx264")]
    assert caught.value.args[0].key == "encoder_stopped"
    assert session.state == State.FAILED and tv.records == 1 and tv.teardowns == 1


async def test_setting_up_has_a_time_limit(tv, monkeypatch):
    _ffmpeg()
    tv.reconfigure(setup_delay=2)
    monkeypatch.setattr(mirror_session, "START_SECONDS", 1)
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_no_answer" and session.state == State.FAILED


# -- the probe -----------------------------------------------------------------------------------------------


async def test_probe_finds_how_tcl_mirrors_and_saves_it(tv):
    _ffmpeg()
    lines = []
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=2, progress=lines.append), 120)
    assert result.worked and result.pair_verify == "ok" and not result.fairplay
    assert result.info is not None and result.info.displays[0].width == 1920
    # Until the TV takes one; then the video; then, for the report, the TCL wanting the audio
    # stream, PTP without a ClockID, and control-first without the audio stream.
    assert [(a.plan.describe(), a.worked) for a in result.setups] == [
        ("control-first, with the audio stream, NTP timing", False),
        ("media-first, with the audio stream, NTP timing", True),
        ("media-first, without the audio stream, NTP timing", False),
        ("media-first, with the audio stream, PTP timing", True),
        ("control-first, without the audio stream, NTP timing", False)]
    video = next(n for n, line in enumerate(lines) if " s of the test pattern: " in line)
    assert video < next(n for n, line in enumerate(lines) if "without the audio stream" in line)
    assert result.setups[0].steps == {"pair-verify": "ok", "setup control-first": 400}
    assert result.ptp == "answered without timingPeerInfo.ClockID" and result.ntp_after_ptp is True
    assert result.not_tried == [] and "Not tried" not in "\n".join(result.lines())
    (check,) = result.streams
    assert check.worked and check.report.frames_sent >= 40 and check.report.ntp_requests >= 3
    assert result.data_channel_accepted and result.data_channel_kept
    assert result.profile == profiles.load("fake-tv") and result.profile.ordering == MEDIA_FIRST
    assert result.saved is not None and result.saved.is_file()
    assert "Screen mirroring works with TCL 55QM7L" in result.lines()[-2] and lines
    # Every session it set up was torn down, and nothing is left open.
    assert tv.teardowns == len(tv.orderings) == 4
    await asyncio.sleep(0.1)
    assert all(conn.closed for conn in tv.connections)


async def test_probe_leaves_the_debug_switches_out(tv):
    _ffmpeg()
    settings = dataclasses.replace(SETTINGS, mirror_lead_ms=300, mirror_keyframe_seconds=5, mirror_on_arrival=True)
    result = await asyncio.wait_for(probe_mirroring(tv.target(), settings, seconds=2.5, variants=()), 60)
    assert result.worked and {stream["latencyMs"] for stream in tv.video_streams} == {100}
    video = tv.videos[-1]
    assert video.codec_subtypes == [b"\x16\x01"]
    assert [n for n, (_, idr, _) in enumerate(video.frames) if idr][:2] == [0, 60]  # every 2 s
    assert result.profile.lead_ms == profiles.load("fake-tv").lead_ms == 100


async def test_probe_tries_the_other_avcc_ending_when_the_tv_drops_the_video(tv):
    _ffmpeg()
    tv.reconfigure(want_trailer=bytes.fromhex("fdf8f800"))
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=1, variants=("trailer",)), 120)
    assert [(c.trailer, c.worked) for c in result.streams] == [(TRAILER_FIXED, False), (TRAILER_ISO, True)]
    # The TV closed the connection at the codec packet, which went with the first frame; how
    # many frames went before the sender saw it depends on the machine.
    first = tv.videos[0]
    assert first.closed_by_us and len(first.codec_packets) == 1 and first.frames == []
    assert result.streams[0].report.codec_packets == 1 and result.streams[0].report.data_closed_after <= 20
    assert result.worked and result.profile.trailer == TRAILER_ISO
    assert len(result.setups) == 1 and result.ptp == "not tried"


async def test_probe_reports_a_tv_that_keeps_dropping_the_video(tv):
    _ffmpeg()
    tv.reconfigure(drop_video_after=5)
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=2, variants=("trailer",)), 120)
    assert not result.worked and result.saved is None and profiles.load("fake-tv") is None
    assert [c.trailer for c in result.streams] == [TRAILER_FIXED, TRAILER_ISO]
    assert all(c.report.data_closed_after is not None for c in result.streams)
    assert result.data_channel_accepted and not result.data_channel_kept
    assert isinstance(result.conclusion(), Text) and result.conclusion().key == "mirror_data_closed"
    assert tv.teardowns == len(tv.orderings)


async def test_probe_without_video_holds_the_video_connection_open(tv):
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, send_test_pattern=False,
                                                    variants=()), 60)
    (check,) = result.streams
    assert check.worked and check.report.data_channel == "open" and check.report.frames_sent == 0
    assert check.report.ntp_requests >= 1  # the TV asked for the time while it was held
    assert result.worked and tv.records == 1 and tv.teardowns == len(tv.orderings) == 2
    assert f"Sent as: User-Agent {USER_AGENT}" in "\n".join(result.lines())


async def test_probe_without_video_needs_the_tv_to_ask_for_the_time(tv, monkeypatch):
    from mirrorbridge.mirror import probe

    tv.reconfigure(send_ntp=False)
    monkeypatch.setattr(probe, "TIMING_WAIT_SECONDS", 0.5)
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, send_test_pattern=False,
                                                    variants=()), 60)
    assert not result.worked and result.saved is None and profiles.load("fake-tv") is None
    assert isinstance(result.conclusion(), Text) and result.conclusion().key == "mirror_no_timing"
    assert "the TV never asked for the time" in "\n".join(result.lines())


async def test_probe_of_a_tv_that_refuses_the_video_connection(tv):
    tv.reconfigure(refuse_video_connection=True)
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, send_test_pattern=False,
                                                    variants=()), 60)
    assert not result.worked and result.streams[0].report.data_channel == "refused (connection refused)"
    assert not result.data_channel_accepted and tv.teardowns == len(tv.orderings) == 2
    assert isinstance(result.conclusion(), Text) and result.conclusion().key == "mirror_data_refused"


async def test_probe_stops_at_a_dropped_pairing(tv):
    tv.reconfigure(m4_error=True)
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS), 60)
    assert not result.worked and "M4 error 2" in result.pair_verify
    assert result.conclusion().key == "mirror_verify_refused" and tv.codes("SETUP") == []


async def test_probe_of_an_apple_tv_sends_nothing(tv):
    target = tv.target()
    target.features = 0x4A7FDFD5 | (0xBC157FDE << 32)
    result = await probe_mirroring(target, SETTINGS)
    assert result.fairplay and result.conclusion().key == "mirror_fairplay"
    assert tv.connections == [] and tv.requests == []


# -- the command line ---------------------------------------------------------------------------------------


@pytest.fixture
def cli_device(tv, monkeypatch):
    from mirrorbridge import cli

    device = _device(tv, CREDENTIALS)

    async def choose(_manager, args):
        assert args.device == "TCL"
        return device

    monkeypatch.setattr(cli, "_choose", choose)
    return device


async def test_mirror_command_probes(tv, cli_device, capsys):
    from mirrorbridge import cli

    args = cli.build_parser().parse_args(["mirror", "-d", "TCL", "--probe", "--no-video"])
    assert await cli.cmd_mirror(args) == 0
    out = capsys.readouterr().out
    assert "Pair-verify: ok" in out and "PTP: answered without timingPeerInfo.ClockID" in out
    assert "Screen mirroring works with TCL 55QM7L: media-first" in out
    assert profiles.load(cli_device.identifier).ordering == MEDIA_FIRST


async def test_mirror_command_test_pattern(tv, cli_device, capsys):
    from mirrorbridge import cli

    _ffmpeg()
    SETTINGS.save()
    args = cli.build_parser().parse_args(["mirror", "-d", "TCL", "--test-pattern", "--seconds", "1"])
    assert await cli.cmd_mirror(args) == 0
    out = capsys.readouterr().out
    assert "[Casting] Mirroring the test pattern to TCL 55QM7L (640x360, 30 fps)" in out
    assert out.rstrip().endswith("Mirrored for 1 s, as asked") and tv.teardowns == 1


async def test_mirror_command_names_the_forget_command(tv, cli_device, capsys):
    from mirrorbridge import cli

    tv.reconfigure(m4_error=True)
    args = cli.build_parser().parse_args(["mirror", "-d", "TCL", "--probe", "--no-video"])  # no FFmpeg needed
    assert await cli.cmd_mirror(args) == 1
    assert 'Use forget -d "TCL 55QM7L", then cast again' in capsys.readouterr().out
