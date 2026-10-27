import plistlib

import pytest
from pyatv import exceptions

from aircast import airplay_patch


class FakeResp:
    def __init__(self, body, code=200):
        self.body = plistlib.dumps(body, fmt=plistlib.FMT_BINARY) if body is not None else b""
        self.headers = {"Content-Type": "application/x-apple-binary-plist"}
        self.code = code
        self.message = "OK" if code == 200 else "Not Implemented"


class FakeConn:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.transport = object()

    async def get(self, _path, allow_error=False):
        if not self.bodies:
            raise exceptions.ConnectionLostError("closed")
        body = self.bodies.pop(0)
        return body if isinstance(body, FakeResp) else FakeResp(body)


class FakePlayer:
    def __init__(self, bodies):
        self.rtsp = type("R", (), {"connection": FakeConn(bodies)})()


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(airplay_patch.asyncio, "sleep", no_sleep)


async def test_slow_start_then_stop_on_tv():
    bodies = [{"readyToPlay": False}] * 10 + [{"duration": float("nan"), "rate": 1.0}] * 3 + [{}] * 4
    player = FakePlayer(bodies)
    await airplay_patch._wait_for_live_stream(player)
    assert player.rtsp.connection.bodies == []


async def test_live_stream_without_duration_counts_as_playing():
    player = FakePlayer([{"readyToPlay": True, "rate": 1.0}] * 50)
    await airplay_patch._wait_for_live_stream(player)  # ends only when connection closes


async def test_receiver_error_raises():
    with pytest.raises(exceptions.PlaybackError):
        await airplay_patch._wait_for_live_stream(FakePlayer([{"error": {"code": -1, "domain": "x"}}]))


async def test_never_starts_raises():
    with pytest.raises(exceptions.PlaybackError, match="never started"):
        await airplay_patch._wait_for_live_stream(FakePlayer([{}] * 40))


async def test_receiver_without_playback_info_is_kept_open_until_it_disconnects(monkeypatch):
    """Non-Apple TVs may answer /playback-info with 501; the cast must go on."""
    player = FakePlayer([FakeResp(None, code=501)])
    polls = []

    async def sleep(_s):
        polls.append(1)
        if len(polls) == 3:
            player.rtsp.connection.transport = None  # the TV closes the connection

    monkeypatch.setattr(airplay_patch.asyncio, "sleep", sleep)
    await airplay_patch._wait_for_live_stream(player)
    assert len(polls) == 3


class FakeRtsp:
    """Records the requests AirPlayV2.play_url makes; /setProperty answers 501."""

    def __init__(self, play_code=200):
        self.sent = []
        rtsp = self

        class Conn:
            async def post(self, path, headers=None, body=None, allow_error=False):
                rtsp.sent.append(("POST", path))
                return FakeResp(None, code=play_code)

        self.connection = Conn()

    async def record(self):
        self.sent.append(("RECORD", None))

    async def exchange(self, method, uri=None, body=None, allow_error=False, **_kw):
        self.sent.append((method, uri))
        if uri.startswith("/setProperty"):
            if not allow_error:
                raise exceptions.HttpError("RTSP/1.0 method PUT failed with code 501", 501)
            return FakeResp(None, code=501)
        return FakeResp(None)


class FakeV2:
    def __init__(self, play_code=200):
        self.rtsp = FakeRtsp(play_code)
        self.uuid = "u"

    async def _setup_base(self, _port):
        self.rtsp.sent.append(("SETUP", None))

    async def start_feedback(self):
        pass


async def test_play_url_survives_receivers_without_set_property():
    """The 55QM7L case: POST /play works, PUT /setProperty is 501 Not Implemented."""
    v2 = FakeV2()
    resp = await airplay_patch._play_url_v2(v2, 1234, "http://pc:7878/stream.m3u8")
    assert resp.code == 200
    assert ("POST", "/rate?value=1.000000") in v2.rtsp.sent  # playback is still started
    assert v2.rtsp.sent[:3] == [("SETUP", None), ("RECORD", None), ("POST", "/play")]


async def test_play_url_reports_a_tv_that_refuses_video():
    with pytest.raises(airplay_patch.ReceiverRefusedVideo, match="HTTP 501"):
        await airplay_patch._play_url_v2(FakeV2(play_code=501), 1234, "http://pc/stream.m3u8")


def test_patches_target_existing_pyatv_methods():
    from pyatv.protocols.airplay.player import AirPlayPlayer
    from pyatv.protocols.raop.protocols.airplayv2 import AirPlayV2

    assert hasattr(AirPlayPlayer, "_wait_for_media_to_end")
    assert hasattr(AirPlayV2, "_setup_base") and hasattr(AirPlayV2, "start_feedback")
