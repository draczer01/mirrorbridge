"""Screen mirroring to a TV in the same process (tests/fake_mirror_tv.py), a TCL 55QM7L by
default: pairing, SETUP in its shapes and the fallbacks, the clock, the event channel, video
decrypted and decoded by FFmpeg, the probe, and the ways a TV says no."""

import asyncio
import subprocess

import pytest

from fake_mirror_tv import CREDENTIALS, PIN, FakeMirrorTV
from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.mirror import TEST_PATTERN, MirrorError, MirrorSession, probe_mirroring, profiles
from mirrorbridge.mirror import session as mirror_session
from mirrorbridge.mirror.clock import NS, from_ntp
from mirrorbridge.mirror.control import ControlChannel, Target
from mirrorbridge.mirror.h264 import TRAILER_FIXED, TRAILER_ISO
from mirrorbridge.mirror.negotiation import CONTROL_FIRST, MEDIA_FIRST, NTP, PTP, Negotiator, Plan
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


async def test_not_paired():
    with pytest.raises(MirrorError) as caught:
        await ControlChannel.open(Target("TV", "127.0.0.1", 9, credentials=None))
    assert caught.value.args[0].key in ("mirror_not_paired", "mirror_connect_failed")


# -- SETUP ------------------------------------------------------------------------------------------------


async def test_tcl_refuses_control_first_and_takes_media_first(tv):
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan())
    try:
        assert est.plan.ordering == MEDIA_FIRST and est.plan.timing == NTP
        assert negotiator.steps["setup control-first"] == 400 and negotiator.steps["setup media-first"] == 200
        assert "control-first SETUP refused with 400" in est.fallbacks[0]
        assert est.data_port and est.event_port and est.receiver_timing_port
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
        assert ptp["timingProtocol"] == "PTP" and ptp["timingPeerInfo"]["Addresses"] and "timingPort" not in ptp
        assert ntp["timingProtocol"] == "NTP"
        assert tv.teardowns == 1 and len(tv.connections) == 2  # torn down, then a new pair-verify
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
    assert (width, height) == (640.0, 360.0) and avcc.endswith(bytes.fromhex("fdf8f800"))
    assert ts == video.frames[0][0] and video.frames[0][1]  # the codec packet goes with the first IDR
    # Each frame is to be shown its lead (100 ms) after it was taken: a little less after it arrived.
    for (stamp, _, _), arrived in zip(video.frames, video.arrived):
        assert 40 * NS // 1000 < from_ntp(stamp) - arrived <= 100 * NS // 1000
    frames, size = _decode(ff, bytes(video.annexb), tmp_path)
    assert frames == len(video.frames) and size == "640x360"

    assert report.latency["frames"] == report.frames_sent and "send" in report.latency
    saved = profiles.load("fake-tv")
    assert saved is not None and saved.ordering == MEDIA_FIRST and saved.trailer == TRAILER_ISO
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
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, trailer=TRAILER_FIXED))
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=1, remember=False)
    await asyncio.wait_for(session.run(), 60)
    assert tv.codes("SETUP") == [200, 200] and tv.orderings == [MEDIA_FIRST]
    assert tv.videos[0].codec_packets[0][3].endswith(bytes.fromhex("02000000"))


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
    assert tv.teardowns == 1 and profiles.load("fake-tv") is None


# -- the probe -----------------------------------------------------------------------------------------------


async def test_probe_finds_how_tcl_mirrors_and_saves_it(tv):
    _ffmpeg()
    lines = []
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=2, progress=lines.append), 120)
    assert result.worked and result.pair_verify == "ok" and not result.fairplay
    assert result.info is not None and result.info.displays[0].width == 1920
    assert [a.worked for a in result.setups] == [False, True, False, False, True]
    assert [a.plan.describe() for a in result.setups][:4] == [
        "control-first, with the audio stream, NTP timing", "media-first, with the audio stream, NTP timing",
        "control-first, without the audio stream, NTP timing", "media-first, without the audio stream, NTP timing"]
    assert result.setups[0].steps == {"pair-verify": "ok", "setup control-first": 400}
    assert result.ptp == "answered without timingPeerInfo.ClockID" and result.ntp_after_ptp is True
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


async def test_probe_tries_the_other_avcc_ending_when_the_tv_drops_the_video(tv):
    _ffmpeg()
    tv.reconfigure(want_trailer=bytes.fromhex("02000000"))
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=1, variants=("trailer",)), 120)
    assert [(c.trailer, c.worked) for c in result.streams] == [(TRAILER_ISO, False), (TRAILER_FIXED, True)]
    assert result.streams[0].report.data_closed_after in (0, 1)  # the codec packet goes with the first frame
    assert result.worked and result.profile.trailer == TRAILER_FIXED
    assert len(result.setups) == 1 and result.ptp == "not tried"


async def test_probe_reports_a_tv_that_keeps_dropping_the_video(tv):
    _ffmpeg()
    tv.reconfigure(drop_video_after=5)
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=2, variants=("trailer",)), 120)
    assert not result.worked and result.saved is None and profiles.load("fake-tv") is None
    assert [c.trailer for c in result.streams] == [TRAILER_ISO, TRAILER_FIXED]
    assert all(c.report.data_closed_after is not None for c in result.streams)
    assert result.data_channel_accepted and not result.data_channel_kept
    assert result.conclusion().key == "mirror_data_closed"
    assert tv.teardowns == len(tv.orderings)


async def test_probe_without_video_holds_the_video_connection_open(tv):
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, send_test_pattern=False,
                                                    variants=()), 60)
    (check,) = result.streams
    assert check.worked and check.report.data_channel == "open" and check.report.frames_sent == 0
    assert result.worked and tv.records == 1 and tv.teardowns == len(tv.orderings) == 2


async def test_probe_of_a_tv_that_refuses_the_video_connection(tv):
    tv.reconfigure(refuse_video_connection=True)
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, send_test_pattern=False,
                                                    variants=()), 60)
    assert not result.worked and result.streams[0].report.data_channel == "refused (connection refused)"
    assert not result.data_channel_accepted and tv.teardowns == len(tv.orderings) == 2


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
    args = cli.build_parser().parse_args(["mirror", "-d", "TCL", "--test-pattern"])
    assert await cli.cmd_mirror(args) == 1
    assert 'Use forget -d "TCL 55QM7L", then cast again' in capsys.readouterr().out
