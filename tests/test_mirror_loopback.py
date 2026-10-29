"""Screen mirroring to a TV in the same process (tests/fake_mirror_tv.py), a TCL 55QM7L by
default: pairing, SETUP in its shapes and the fallbacks, the clock, the event channel, video
decrypted and decoded by FFmpeg, the probe, and the ways a TV says no, is slow, or goes away."""

import asyncio
import dataclasses
import functools
import logging
import statistics
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from fake_mirror_tv import CREDENTIALS, PIN, FakeMirrorTV
from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.mirror import TEST_PATTERN, MirrorError, MirrorSession, capture, probe_mirroring, profiles, stats
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


def _noisy_pattern(monkeypatch) -> None:
    """The test pattern with noise on it: frames as big as a busy screen's, so that the buffers
    on the way hide less of a TV that reads slowly or stops for a while."""
    real_build_command = mirror_session.build_command

    def noisy(ff, settings, **kw):
        cmd = real_build_command(ff, settings, **kw)
        at = next(n for n, arg in enumerate(cmd) if arg.startswith("testsrc="))
        cmd[at] += ",noise=alls=100:allf=t"
        return cmd

    monkeypatch.setattr(mirror_session, "build_command", noisy)


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
    keyframe every 10 s and frames shown as they come; mirroring.json keeps the TV's own lead."""
    _ffmpeg()
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, lead_ms=150))
    settings = dataclasses.replace(SETTINGS, mirror_lead_ms=300, mirror_keyframes=10, mirror_on_arrival=True)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=2.5)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    assert ("Mirroring with lead 300 ms (Debug TV wait), keyframes every 10 s and when needed, frames shown on "
            "arrival") in caplog.text
    assert tv.video_streams[0]["latencyMs"] == 300 and tv.setups[0]["streams"][0]["latencyMax"] == 300 * 44100 // 1000
    assert session.report.established.plan.lead_ms == 300
    assert session._stats.lead_ms == 300  # pylint: disable=protected-access
    (video,) = tv.videos
    ahead = [from_ntp(stamp) - arrived for (stamp, _, _), arrived in zip(video.frames, video.arrived)]
    assert max(ahead) <= 300 * NS // 1000 and statistics.median(ahead) > 240 * NS // 1000
    assert video.errors == [] and video.codec_subtypes == [b"\x14\x01"]
    assert [n for n, (_, idr, _) in enumerate(video.frames) if idr] == [0]  # not at 60, as every 2 s
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


@pytest.mark.parametrize("keyframe_seconds", [2, 10])
async def test_ending_mirroring_on_the_tv_stops_the_session(tv, keyframe_seconds):
    """2.5 s in, past AT_ONCE_SECONDS: with a keyframe every 10 s (More options > Debug) too,
    though only one has gone, it is the TV ending mirroring, not one that did not take the video
    and is set up again with the other avcC ending."""
    _ffmpeg()
    states = []
    casting = asyncio.Event()

    def status(state, message):
        states.append((state, str(message)))
        if state == State.CASTING:
            casting.set()

    settings = dataclasses.replace(SETTINGS, mirror_keyframes=keyframe_seconds)
    session = MirrorSession(tv.target(), settings, status, source=TEST_PATTERN, remember=False)
    task = asyncio.ensure_future(session.run())
    await asyncio.wait_for(casting.wait(), 30)
    await asyncio.sleep(2.5)
    tv.end_mirroring()
    await asyncio.wait_for(task, 30)
    assert session.state == State.STOPPED and session.error is None
    assert states[-1] == (State.STOPPED, "The TV ended screen mirroring")
    assert len(tv.videos) == 1 and not session.report.retried


async def test_frames_the_tv_cannot_take_in_time_are_dropped_up_to_a_keyframe(tv, monkeypatch, tmp_path):
    ff = _ffmpeg()
    # As if the network never took the video in time: every P-frame finds too much still waiting.
    monkeypatch.setattr(mirror_session, "catch_up_bytes", lambda kbps, fps: -1)
    settings = dataclasses.replace(SETTINGS, mirror_keyframes=2)  # FFmpeg's own keyframes, every 2 s
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=2.5, remember=False)
    await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    assert session.report.latency["dropped"] > 40 and len(video.frames) >= 2
    assert all(idr for _, idr, _ in video.frames)  # only keyframes got through, each a fresh start
    assert _decode(ff, bytes(video.annexb), tmp_path)[0] == len(video.frames)


async def test_a_drop_starts_ffmpeg_again_for_a_keyframe(tv, monkeypatch, tmp_path, caplog):
    """Keyframes only when needed (the default): frames dropped ask for one, and FFmpeg is started
    again for it, no more often than every KEYFRAME_RESTART_SECONDS; the TV's times go on."""
    ff = _ffmpeg()
    monkeypatch.setattr(mirror_session, "catch_up_bytes", lambda kbps, fps: -1)  # every P-frame is dropped
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=3, remember=False)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    restarts = session.report.restarts
    assert 1 <= len(restarts) <= 3 / mirror_session.KEYFRAME_RESTART_SECONDS + 1
    assert all(why.startswith("a keyframe: ") and "still waiting for the network" in why for why in restarts)
    # Only keyframes got through: the first, and that of each FFmpeg started again (but maybe the last).
    assert len(restarts) <= len(video.frames) <= len(restarts) + 1 and all(idr for _, idr, _ in video.frames)
    assert video.errors == [] and len(video.codec_packets) == 1  # the same picture: no new codec packet
    assert _decode(ff, bytes(video.annexb), tmp_path)[0] == len(video.frames)
    assert "its first keyframe went" in caplog.text


@SMALL_BUFFERS
async def test_a_tv_that_takes_no_video_for_a_while_gets_one_keyframe(tv, tmp_path, caplog, monkeypatch):
    """The TV reads none of the video for 2.5 s: the frame queue makes way for newer frames
    (FrameQueue), and the frames after them wait for a keyframe. FFmpeg is started again once
    for it, and only once: then the TV goes on with that FFmpeg's frames, at their times."""
    ff = _ffmpeg()
    _noisy_pattern(monkeypatch)
    tv.reconfigure(pause_after=20, pause_seconds=2.5, video_buffer=4096)
    settings = dataclasses.replace(SETTINGS, video_bitrate_kbps=4000)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=6, remember=False)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    assert video.errors == [] and video.paused_at == 20 and session.report.latency["dropped"] > 0
    assert "The TV took no video for a while" in caplog.text
    (why,) = session.report.restarts
    assert why.startswith("a keyframe: the TV took no video for a while")
    assert session._keyframe_wanted == ""  # pylint: disable=protected-access
    idrs = [n for n, (_, idr, _) in enumerate(video.frames) if idr]
    assert idrs[0] == 0 and len(idrs) == 2 and idrs[1] > 20 and len(video.frames) > idrs[1] + 30
    assert _decode(ff, bytes(video.annexb), tmp_path)[0] == len(video.frames)


@SMALL_BUFFERS
async def test_a_network_that_holds_the_video_up_for_a_moment_is_ridden_out(tv, tmp_path, caplog, monkeypatch):
    """The TV reads none of the video for 0.6 s, as Wi-Fi held a TCL's up now and then: the
    frames that waited go late, with the times they were taken for, and the TV goes on with
    them, P-frames, instead of every frame up to the next keyframe being dropped (2 s of a
    frozen picture)."""
    ff = _ffmpeg()
    _noisy_pattern(monkeypatch)
    tv.reconfigure(pause_after=15, pause_seconds=0.6, video_buffer=4096)
    settings = dataclasses.replace(SETTINGS, video_bitrate_kbps=4000, mirror_keyframes=2)
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
async def test_a_network_that_holds_the_video_up_for_longer_than_the_tv_is_given_gets_a_fresh_start(
        tv, tmp_path, caplog, monkeypatch):
    """The TV reads none of the video for 1.8 s, more than CATCH_UP_MS past the lead: frames are
    dropped up to a keyframe, and not to the one that waited through the pause (already late,
    with frames behind it), so that the TV is back to real time, not left behind."""
    ff = _ffmpeg()
    _noisy_pattern(monkeypatch)
    tv.reconfigure(pause_after=15, pause_seconds=1.8, video_buffer=4096)
    settings = dataclasses.replace(SETTINGS, video_bitrate_kbps=4000)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=5, remember=False)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    assert video.errors == [] and video.paused_at == 15 and session.report.latency["dropped"] > 0
    assert "the TV is given to catch up" in caplog.text
    # Where frames were dropped, the TV's times jump: the frame after the jump is a keyframe,
    # and it came on time.
    shown = [from_ntp(stamp) for stamp, _, _ in video.frames]
    gap = max(range(1, len(shown)), key=lambda n: shown[n] - shown[n - 1])
    assert shown[gap] - shown[gap - 1] > NS and video.frames[gap][1]
    assert video.arrived[gap] <= shown[gap]
    assert _decode(ff, bytes(video.annexb), tmp_path)[0] == len(video.frames)


@SMALL_BUFFERS
@pytest.mark.parametrize("keyframes", [2, 0])
async def test_a_network_slower_than_the_video_does_not_leave_the_tv_behind(tv, tmp_path, caplog, monkeypatch,
                                                                            keyframes):
    """The TV reads about three quarters of the bitrate (about 2.4 Mbit/s of noise at 4 Mbit/s):
    the frames fall behind a little more every second. Once they have been past their lead for
    CATCH_UP_MS, frames are dropped up to a keyframe; none waits until it alone is a whole
    CATCH_UP_MS late, so none comes to the TV that late. With a keyframe every 2 s, the drop ends
    on one that is on time. With keyframes only when needed, FFmpeg is started again for one at
    once, which can still find the network behind (by a few hundred ms here): the TV is back to
    real time once it caught up."""
    ff = _ffmpeg()
    _noisy_pattern(monkeypatch)
    tv.reconfigure(read_kbps=1800, video_buffer=4096)
    settings = dataclasses.replace(SETTINGS, video_bitrate_kbps=4000, mirror_keyframes=keyframes)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=7, remember=False)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    (video,) = tv.videos
    assert video.errors == [] and session.report.latency["dropped"] > 0
    assert "frames have been past their lead for" in caplog.text
    shown = [from_ntp(stamp) for stamp, _, _ in video.frames]
    past = [arrived - at for at, arrived in zip(shown, video.arrived)]
    assert max(past) < mirror_session.CATCH_UP_MS * NS // 1000
    gaps = [n for n in range(1, len(shown)) if shown[n] - shown[n - 1] > 200 * NS // 1000]
    assert gaps and all(video.frames[n][1] for n in gaps)
    if keyframes:
        assert past[gaps[0]] <= 0 and session.report.restarts == []
    else:  # each drop ended on the keyframe of an FFmpeg started for it, never two within KEYFRAME_RESTART_SECONDS
        assert len(gaps) <= len(session.report.restarts) <= 7 / mirror_session.KEYFRAME_RESTART_SECONDS + 1
    assert _decode(ff, bytes(video.annexb), tmp_path)[0] == len(video.frames)


@SMALL_BUFFERS
async def test_a_slow_tv_gets_fewer_frames_not_later_ones(tv):
    """The video connection's buffers are kept to about two frames (session.send_buffer), so
    that a TV that reads slowly pushes back at once; once frames have been past their lead for
    more than CATCH_UP_MS, they are dropped rather than queued."""
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
    rates = []

    def nvenc_fails(ff, settings, **kw):
        rates.append((kw["encoder"], kw["fps"]))
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
    # Frame rate Automatic: 60 with the graphics card's encoder, 30 with x264, and what goes with it.
    # pylint: disable=protected-access
    assert rates == [("h264_nvenc", 60), ("libx264", 30)] and session.report.fps == session._stats.fps == 30
    assert session._frames.limit == 30 * mirror_session.QUEUE_SECONDS
    assert session._send_buffer == mirror_session.send_buffer(SETTINGS.video_bitrate_kbps, 30)
    assert session._catch_up == mirror_session.catch_up_bytes(SETTINGS.video_bitrate_kbps, 30)


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


# -- FFmpeg started again: the screen capture lost, the graphics card's picture ------------------------------

SCREEN = ffmpeg.Monitor(0, 0, 0, 1920, 1080, True, 0, device=r"\\.\DISPLAY1")
# As ddagrab ends FFmpeg (vsrc_ddagrab.c): Windows took the capture away (DXGI_ERROR_ACCESS_LOST), and
# a display that is not there yet when FFmpeg starts again
ACCESS_LOST = "[Parsed_ddagrab_0 @ 000001d2c4a0e2c0] AcquireNextFrame failed: 887a0026"
NOT_YET = "[Parsed_ddagrab_0 @ 000001d2c4a0e2c0] Failed to enumerate DXGI output 0"
_ENDS = ("import subprocess, sys\n"
         "if int(sys.argv[1]):\n"
         "    subprocess.run(sys.argv[4:])\n"
         "sys.stderr.write(sys.argv[2] + '\\n')\n"
         "if sys.argv[3] == 'stay':\n"
         "    sys.stderr.flush()\n"
         "    sys.stdin.buffer.read(1)\n"
         "sys.exit(0 if sys.argv[3] == 'stay' else int(sys.argv[3]))\n")


class Screen:
    """This PC's display as ddagrab captures it, FFmpeg's 1080p test source standing in for it.
    `ends` says how each FFmpeg in turn ends: after (frames, with this line on stderr), as when
    Windows takes the capture away (0 frames: at once), with exit code 1 or a third item: another
    exit code, or "stay" for an FFmpeg that then gives no more frames until it is stopped (a still
    screen with gfxcapture); None, or none left, runs until stopped."""

    def __init__(self, monkeypatch, ends=(), size=(1920, 1080)) -> None:
        self.ends = list(ends)
        self.commands: list = []  # (backend, chain kind, the command the session asked for)
        self.size = size  # what the stand-in makes for the graphics card chain
        monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [SCREEN])
        monkeypatch.setattr(ffmpeg, "pick_capture_backend", lambda ff, settings, monitor=None, region=None: "ddagrab")
        monkeypatch.setattr(capture, "BROKEN", set())
        build = mirror_session.build_command

        def command(ff, settings, *, source, backend="", width, height, chain=None, **kw):
            asked = build(ff, settings, source=source, backend=backend, width=width, height=height, chain=chain, **kw)
            self.commands.append((backend, chain.kind if chain else None, asked))
            if chain is not None and chain.kind in (capture.GPU, capture.GFX):
                width, height = self.size
            cmd = build(ff, settings, source=source, backend="test", width=width, height=height, chain=chain, **kw)
            end = self.ends.pop(0) if self.ends else None
            if end is None:
                return cmd
            frames, line, code = (*end, 1)[:3]
            if frames:
                cmd = cmd[:-3] + ["-frames:v", str(frames)] + cmd[-3:]
            return [sys.executable, "-c", _ENDS, str(frames), line, str(code), *cmd]

        monkeypatch.setattr(mirror_session, "build_command", command)

    def ways(self) -> list:
        return [(backend, kind) for backend, kind, _ in self.commands]


async def _until(condition, task, seconds: float = 30) -> None:
    async def wait() -> None:
        while not condition():
            assert not task.done(), task.exception() if task.done() and not task.cancelled() else ""
            await asyncio.sleep(0.05)

    await asyncio.wait_for(wait(), seconds)


@pytest.mark.parametrize("then", [NOT_YET, "[Parsed_ddagrab_0 @ 0000] Something FFmpeg 9 says about DXGI"])
async def test_a_lost_screen_capture_is_started_again_with_the_settings_it_began_with(tv, monkeypatch, tmp_path,
                                                                                     then):
    """ddagrab says Windows took the capture away (a display driver changing, a UAC prompt) after
    15 frames; the next FFmpeg ends before its first frame, as while the display still changes
    (whatever it says: never GDI capture, which is next); the one after that runs. The TV gets
    the frames of the first and the last, on one timeline, and a change in the menus meanwhile
    is for the next session."""
    ff = _ffmpeg()
    screen = Screen(monkeypatch, ends=[(15, ACCESS_LOST), (0, then)])
    monkeypatch.setattr(mirror_session, "CAPTURE_RETRY_DELAYS", (0.05,) * 5)
    settings = dataclasses.replace(SETTINGS)
    states = []

    def status(state, message) -> None:
        states.append(state)
        if state == State.CASTING:  # the menus, while it runs
            settings.video_bitrate_kbps, settings.mirror_keyframes, settings.mirror_scaler = 9000, 2, "cpu"

    session = MirrorSession(tv.target(), settings, status, remember=False)
    task = asyncio.ensure_future(session.run())
    await _until(lambda: tv.videos and len(tv.videos[0].frames) >= 45, task)
    await session.stop()
    await asyncio.wait_for(task, 30)
    assert session.error is None and states.count(State.CASTING) == 1 and tv.records == 1
    assert screen.ways() == [("ddagrab", capture.CPU)] * 3  # gdigrab came next, and was never needed
    assert screen.commands[0][2] == screen.commands[2][2] and "1500k" in screen.commands[2][2]
    assert session.report.restarts == ["the screen capture lost, try 1", "the screen capture lost, try 2"]
    (video,) = tv.videos
    assert video.errors == [] and [n for n, (_, idr, _) in enumerate(video.frames) if idr] == [0, 15]
    assert _decode(ff, bytes(video.annexb), tmp_path) == (len(video.frames), "640x360")


@pytest.mark.parametrize("ends", ["tries", "time"])
async def test_a_screen_capture_that_does_not_come_back_ends_the_session_with_its_own_error(tv, monkeypatch, ends):
    """After CAPTURE_RETRY_DELAYS, or CAPTURE_LOST_SECONDS from the loss, whichever comes first
    (here while the session waits 30 s to start the third); not "No picture for 10 s"
    (FRAME_GAP_SECONDS), which waits meanwhile."""
    _ffmpeg()
    screen = Screen(monkeypatch, ends=[(10, ACCESS_LOST)] + [(0, NOT_YET)] * 5)
    monkeypatch.setattr(mirror_session, "CAPTURE_RETRY_DELAYS", (0.02,) * 5 if ends == "tries" else (0.02, 30.0))
    monkeypatch.setattr(mirror_session, "CAPTURE_LOST_SECONDS", 30 if ends == "tries" else 1.5)
    monkeypatch.setattr(mirror_session, "FRAME_GAP_SECONDS", 0.5)
    session = MirrorSession(tv.target(), SETTINGS, remember=False)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_capture_lost" and caught.value.step == "capture"
    assert str(caught.value) == ("Windows stopped the screen capture and it did not come back (AcquireNextFrame "
                                 "failed: 887a0026). Choose Show my screen again.")
    assert screen.ways() == [("ddagrab", capture.CPU)] * (6 if ends == "tries" else 2)
    assert session.state == State.FAILED and tv.teardowns == 1


async def test_a_graphics_card_picture_of_the_wrong_size_gives_way_to_the_processor_for_good(
        tv, monkeypatch, tmp_path, caplog):
    """The graphics card chain at the display's own size (what Windows says it is) makes a bigger
    picture, as a rotated display or a size in other units would: none of it goes to the TV, the
    processor's chain takes over at once, and is the only one tried on that display afterwards."""
    ff = _ffmpeg()
    tv.reconfigure(width=1280, height=720)
    screen = Screen(monkeypatch, size=(1920, 1080))
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [dataclasses.replace(SCREEN, width=1280, height=720)])
    # As on Windows with NVENC (x264 encodes here), at the screen's own size: no scaling needed.
    monkeypatch.setattr(capture, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(capture, "GPU_ENCODERS", ("libx264",))
    settings = dataclasses.replace(SETTINGS, output_height=720)
    session = MirrorSession(tv.target(), settings, seconds=1.5, remember=False)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    assert session.error is None and screen.ways() == [("ddagrab", capture.GPU), ("ddagrab", capture.CPU)]
    assert session.report.restarts == [
        "the graphics card's picture: the encoder made 1920x1080, not the 1280x720 asked for"]
    assert capture.is_broken(ff, dataclasses.replace(SCREEN, width=1280, height=720))
    (video,) = tv.videos
    assert video.errors == [] and [packet[1:3] for packet in video.codec_packets] == [(1280.0, 720.0)]
    assert _decode(ff, bytes(video.annexb), tmp_path) == (len(video.frames), "1280x720")
    assert "Capture chain: the graphics card's (1280x720 needs no scaling)" in caplog.text
    session = MirrorSession(tv.target(), settings, seconds=0.5, remember=False)  # the next one, in the same run
    await asyncio.wait_for(session.run(), 60)
    assert session.error is None and screen.ways()[2:] == [("ddagrab", capture.CPU)]


def _gfxcapture(tv, monkeypatch) -> None:
    """As on Windows with NVENC and FFmpeg 8.1 (x264 encodes here, the test source stands in for
    gfxcapture): a 1080p display on a 720p TV, which gfxcapture scales."""
    tv.reconfigure(width=1280, height=720)
    monkeypatch.setattr(capture, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(capture, "GPU_ENCODERS", ("libx264",))
    monkeypatch.setattr(ffmpeg, "has_filter", lambda ff, name: name == "gfxcapture")
    monkeypatch.setattr(capture, "gfx_monitor", lambda monitor: "hmonitor=65537")


GFX_SETTINGS = dataclasses.replace(SETTINGS, output_height=720)


async def test_gfxcapture_that_fails_before_its_first_frame_gives_way_to_the_processor_for_good(
        tv, monkeypatch, caplog):
    """gfxcapture ends FFmpeg before its first frame (here as when it cannot get a graphics card
    device): the session goes on with ddagrab and the processor's chain, saying why with FFmpeg's
    words, and does not try gfxcapture on that display again in this run."""
    ff = _ffmpeg()
    _gfxcapture(tv, monkeypatch)
    screen = Screen(monkeypatch, ends=[(0, "[Parsed_gfxcapture_0 @ 0x1] Failed to create D3D11VA device.")],
                    size=(1280, 720))
    session = MirrorSession(tv.target(), GFX_SETTINGS, seconds=1, remember=False)
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    assert session.error is None and screen.ways() == [("ddagrab", capture.GFX), ("ddagrab", capture.CPU)]
    assert ("Capture chain: the graphics card's, through Windows Graphics Capture (gfxcapture scales from 1920x1080 "
            "to 1280x720)") in caplog.text
    assert "FFmpeg 1 (start): GPU gfxcapture, 1920x1080 -> 1280x720, x264 (CPU) at up to 30 fps" in caplog.text
    assert (r"gfxcapture with libx264 on the graphics card failed on \\.\DISPLAY1 (not tried there again until "
            "MirrorBridge quits), trying the processor's chain. FFmpeg said:\n[Parsed_gfxcapture_0 @ 0x1] Failed to "
            "create D3D11VA device.") in caplog.text
    assert capture.is_broken(ff, SCREEN, capture.GFX) and not capture.is_broken(ff, SCREEN, capture.GPU)
    assert tv.records == 1 and tv.videos[0].frames and tv.videos[0].errors == []
    caplog.clear()
    session = MirrorSession(tv.target(), GFX_SETTINGS, seconds=0.5, remember=False)  # the next one, in the same run
    with caplog.at_level(logging.INFO, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    assert session.error is None and screen.ways()[2:] == [("ddagrab", capture.CPU)]
    assert (r"Capture chain: the processor's (scaling from 1920x1080 to 1280x720: gfxcapture failed on \\.\DISPLAY1 "
            "earlier in this run; scale_d3d11 is turned off (capture.GPU_SCALED_ENABLED))") in caplog.text


@pytest.mark.parametrize("scaler, frames, ends", [("auto", 20, None), ("cpu", 20, "mirror_no_picture"),
                                                  ("auto", 0, "mirror_no_picture")])
async def test_a_still_screen_does_not_end_a_gfxcapture_session(tv, monkeypatch, caplog, scaler, frames, ends):
    """gfxcapture gives frames only as the screen changes: once its FFmpeg gave one, no frame for
    longer than FRAME_GAP_SECONDS is a screen that stays as it is while that FFmpeg runs, and the
    frame rate is no warning. Not so for an FFmpeg that never gave a frame, nor for ddagrab's,
    which gives frames whatever the screen does."""
    _ffmpeg()
    _gfxcapture(tv, monkeypatch)
    screen = Screen(monkeypatch, ends=[(frames, "", "stay")], size=(1280, 720))
    monkeypatch.setattr(mirror_session, "FRAME_GAP_SECONDS", 0.5)
    monkeypatch.setattr(mirror_session, "FIRST_FRAME_SECONDS", 1)
    monkeypatch.setattr(mirror_session, "StageTimes", functools.partial(stats.StageTimes, interval=1.0))
    settings = dataclasses.replace(GFX_SETTINGS, mirror_scaler=scaler)
    session = MirrorSession(tv.target(), settings, seconds=3, remember=False)
    with caplog.at_level(logging.INFO):
        if ends is None:
            await asyncio.wait_for(session.run(), 60)
        else:
            with pytest.raises(MirrorError) as caught:
                await asyncio.wait_for(session.run(), 60)
            assert caught.value.args[0].key == ends
    assert screen.ways() == [("ddagrab", capture.GFX if scaler == "auto" else capture.CPU)]
    if ends is None:
        assert session.error is None and session.report.frames_sent == frames and session.report.restarts == []
        assert "cannot keep up" not in caplog.text and "fps (of up to 30)" in caplog.text


async def test_gfxcapture_ending_its_video_is_a_lost_capture(tv, monkeypatch, caplog):
    """Windows closing gfxcapture's capture (its display gone, here back at once): FFmpeg ends by
    itself, saying nothing at -loglevel warning, and is started again as for a lost capture, with
    the display looked up again; not "Encoder stopped unexpectedly"."""
    _ffmpeg()
    _gfxcapture(tv, monkeypatch)
    screen = Screen(monkeypatch, ends=[(15, "", 0)], size=(1280, 720))
    monkeypatch.setattr(mirror_session, "CAPTURE_RETRY_DELAYS", (0.05,) * 5)
    session = MirrorSession(tv.target(), GFX_SETTINGS, remember=False)
    task = asyncio.ensure_future(session.run())
    with caplog.at_level(logging.WARNING, logger=mirror_session.__name__):
        await _until(lambda: tv.videos and len(tv.videos[0].frames) >= 30, task)
    await session.stop()
    await asyncio.wait_for(task, 30)
    assert session.error is None and screen.ways() == [("ddagrab", capture.GFX)] * 2
    assert session.report.restarts == ["the screen capture lost, try 1"]
    assert "the screen capture was lost (Capture item closed (FFmpeg ended by itself))" in caplog.text
    (video,) = tv.videos
    assert video.errors == [] and [n for n, (_, idr, _) in enumerate(video.frames) if idr] == [0, 15]


async def test_a_keyframe_asked_for_on_a_still_gfxcapture_screen_comes_without_another_frame(tv, monkeypatch):
    """gfxcapture: the last frame of a burst of changes is dropped (here as if the network held it),
    and the screen then stays as it is, so no frame comes after it: FFmpeg is started again for a
    keyframe all the same, and the TV gets the screen as it is now, not the frame before."""
    _ffmpeg()
    _gfxcapture(tv, monkeypatch)
    Screen(monkeypatch, size=(1280, 720))
    builds, stand_in = [], mirror_session.build_command

    def still_after_a_burst(ff, settings, **kw):
        cmd = stand_in(ff, settings, **kw)
        if not builds:  # the first FFmpeg: 0.7 s of changes (21 frames), then none
            cmd[cmd.index("-vf") + 1] += ",select='lt(t,0.7)'"
        builds.append(cmd)
        return cmd

    monkeypatch.setattr(mirror_session, "build_command", still_after_a_burst)
    monkeypatch.setattr(mirror_session, "FRAME_GAP_SECONDS", 0.5)
    real, calls = MirrorSession._drop_reason, []

    def drop(self, *args):
        calls.append(args)
        return "the network held it" if len(calls) == 21 else real(self, *args)

    monkeypatch.setattr(MirrorSession, "_drop_reason", drop)
    session = MirrorSession(tv.target(), GFX_SETTINGS, seconds=2.5, remember=False)
    await asyncio.wait_for(session.run(), 60)
    assert session.error is None and len(builds) == 2
    assert session.report.restarts == ["a keyframe: the network held it"]
    assert session._keyframe_wanted == ""  # pylint: disable=protected-access
    (video,) = tv.videos
    assert video.errors == [] and [n for n, (_, idr, _) in enumerate(video.frames) if idr] == [0, 20]


async def test_a_keyframe_that_waited_too_long_and_is_dropped_asks_for_another(tv, monkeypatch):
    """With a keyframe every 10 s (and Quick Sync, AMF and Media Foundation make one every 10 s
    by default), a keyframe dropped because it waited too long asks for a new one at once: the
    TV does not wait 10 s for the next."""
    _ffmpeg()
    real, keyframes = MirrorSession._drop_reason, []

    def drop(self, idr, *args):
        keyframes.extend([idr] if idr else [])
        if len(keyframes) == 1 and idr:
            return "the keyframe is 500 ms past its lead, with frames behind it"
        return real(self, idr, *args)

    monkeypatch.setattr(MirrorSession, "_drop_reason", drop)
    settings = dataclasses.replace(SETTINGS, mirror_keyframes=10)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=2, remember=False)
    await asyncio.wait_for(session.run(), 60)
    assert session.report.restarts == ["a keyframe: the keyframe is 500 ms past its lead, with frames behind it"]
    (video,) = tv.videos
    assert video.errors == [] and len(video.frames) > 20 and video.frames[0][1] and len(keyframes) == 2


@pytest.mark.parametrize("gfx", [False, True])
async def test_a_display_that_comes_back_at_a_size_to_scale_gets_its_chain_chosen_again(tv, monkeypatch, gfx):
    """The graphics card chain at the display's own size (1280x720 on a 720p TV); Windows takes the
    capture away and the display comes back at 1920x1080: FFmpeg is started again on the chain
    chosen for that size, gfxcapture (with an FFmpeg that has it) or the processor's, never on
    scale_d3d11, which is turned off (capture.GPU_SCALED_ENABLED)."""
    _ffmpeg()
    tv.reconfigure(width=1280, height=720)
    if gfx:
        _gfxcapture(tv, monkeypatch)
    small = dataclasses.replace(SCREEN, width=1280, height=720)
    screen = Screen(monkeypatch, ends=[(15, ACCESS_LOST)], size=(1280, 720))
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [SCREEN if screen.commands else small])
    monkeypatch.setattr(capture, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(capture, "GPU_ENCODERS", ("libx264",))
    monkeypatch.setattr(mirror_session, "CAPTURE_RETRY_DELAYS", (0.05,) * 5)
    session = MirrorSession(tv.target(), GFX_SETTINGS, remember=False)
    task = asyncio.ensure_future(session.run())
    await _until(lambda: tv.videos and len(tv.videos[0].frames) >= 30, task)
    await session.stop()
    await asyncio.wait_for(task, 30)
    assert session.error is None and session.report.restarts == ["the screen capture lost, try 1"]
    assert screen.ways() == [("ddagrab", capture.GPU), ("ddagrab", capture.GFX if gfx else capture.CPU)]
    assert not any("scale_d3d11" in arg for _, _, cmd in screen.commands for arg in cmd)
    (video,) = tv.videos
    assert video.errors == [] and [n for n, (_, idr, _) in enumerate(video.frames) if idr] == [0, 15]


async def test_a_graphics_card_chain_that_windows_kept_the_screen_from_is_tried_again_next_time(
        tv, monkeypatch, caplog):
    """The graphics card chain cannot start its capture because Windows keeps the screen from it
    (a UAC prompt or the lock screen as the session starts): the processor's chain goes on, as
    for any end before the first frame, but the graphics card's is not given up for the rest of
    the run: it was not its fault."""
    ff = _ffmpeg()
    tv.reconfigure(width=1280, height=720)
    small = dataclasses.replace(SCREEN, width=1280, height=720)
    screen = Screen(monkeypatch, ends=[(0, "[Parsed_ddagrab_0 @ 0x1] Desktop duplication access denied")],
                    size=(1280, 720))
    monkeypatch.setattr(ffmpeg, "list_monitors", lambda: [small])
    monkeypatch.setattr(capture, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(capture, "GPU_ENCODERS", ("libx264",))
    session = MirrorSession(tv.target(), GFX_SETTINGS, seconds=1, remember=False)
    with caplog.at_level(logging.WARNING, logger=mirror_session.__name__):
        await asyncio.wait_for(session.run(), 60)
    assert session.error is None and screen.ways() == [("ddagrab", capture.GPU), ("ddagrab", capture.CPU)]
    assert r"could not capture \\.\DISPLAY1 (Desktop duplication access denied)" in caplog.text
    assert not capture.is_broken(ff, small) and tv.videos[0].frames
    session = MirrorSession(tv.target(), GFX_SETTINGS, seconds=0.5, remember=False)  # the next one, in the same run
    await asyncio.wait_for(session.run(), 60)
    assert session.error is None and screen.ways()[2:] == [("ddagrab", capture.GPU)]


async def test_setting_up_again_for_the_other_avcc_ending_starts_restarts_afresh(tv):
    """What the first attempt left (a keyframe asked for, a capture being got back, its tries) is
    not the second's: it would start FFmpeg again at once, or end at once."""
    _ffmpeg()
    tv.reconfigure(want_trailer=bytes.fromhex("fdf8f800"))

    def status(state, message) -> None:
        if getattr(message, "key", "") == "mirror_retry_trailer":
            # pylint: disable=protected-access
            session._keyframe_wanted, session._last_restart_ns = "frames were dropped", 0
            session._recovering_since, session._capture_retries, session._lost = 1, 5, ACCESS_LOST

    session = MirrorSession(tv.target(), SETTINGS, status, source=TEST_PATTERN, seconds=1.5)
    await asyncio.wait_for(session.run(), 60)
    assert session.error is None and session.report.retried and session.report.restarts == []
    first, second = tv.videos
    assert first.closed_by_us and len(second.frames) >= 30 and second.errors == []
    assert [n for n, (_, idr, _) in enumerate(second.frames) if idr] == [0]


@pytest.mark.parametrize("latency, debug, lead", [("low", 0, 60), ("normal", 0, 150), ("smooth", 0, 200),
                                                  ("smooth", 300, 300)])
async def test_the_latency_asks_the_tv_for_its_lead_and_saves_none(tv, latency, debug, lead):
    """Mirror screen settings > Latency: Low 60 ms, Normal the TV's own (150 ms saved here), Smooth
    200 ms; More options > Debug > TV wait wins. What is saved stays the TV's own."""
    _ffmpeg()
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, lead_ms=150))
    settings = dataclasses.replace(SETTINGS, mirror_latency=latency, mirror_lead_ms=debug)
    session = MirrorSession(tv.target(), settings, source=TEST_PATTERN, seconds=1)
    await asyncio.wait_for(session.run(), 60)
    assert tv.video_streams[0]["latencyMs"] == lead == session._stats.lead_ms  # pylint: disable=protected-access
    (video,) = tv.videos
    ahead = [from_ntp(stamp) - arrived for (stamp, _, _), arrived in zip(video.frames, video.arrived)]
    assert max(ahead) <= lead * NS // 1000 and statistics.median(ahead) > (lead - 60) * NS // 1000
    assert profiles.load("fake-tv").lead_ms == 150


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
    """The TV's own lead, keyframes every 2 s, and the chain and encoder options as they come: Test
    this TV says what the TV does, not what a Debug menu or settings.json experiment does to it."""
    settings = dataclasses.replace(SETTINGS, mirror_lead_ms=300, mirror_keyframes=10, mirror_on_arrival=True,
                                   mirror_latency="low", mirror_scaler="cpu", mirror_encoder_extra="-bogus_option 1")
    result = await asyncio.wait_for(probe_mirroring(tv.target(), settings, seconds=2.5, variants=()), 60)
    assert result.worked and {stream["latencyMs"] for stream in tv.video_streams} == {100}
    video = tv.videos[-1]
    assert video.codec_subtypes == [b"\x16\x01"]
    assert [n for n, (_, idr, _) in enumerate(video.frames) if idr][:2] == [0, 60]  # every 2 s
    assert result.profile.lead_ms == profiles.load("fake-tv").lead_ms == 100
    (check,) = result.streams
    assert check.report.restarts == [] and "at 30 fps with libx264, keyframes every 2 s;" in check.line()


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
