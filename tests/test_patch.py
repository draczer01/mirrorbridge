import plistlib

import pytest
from pyatv import exceptions

from aircast import airplay_patch


class FakeResp:
    def __init__(self, body):
        self.body = plistlib.dumps(body, fmt=plistlib.FMT_BINARY) if body is not None else b""
        self.headers = {"Content-Type": "application/x-apple-binary-plist"}


class FakeConn:
    def __init__(self, bodies):
        self.bodies = list(bodies)

    async def get(self, _path):
        if not self.bodies:
            raise exceptions.ConnectionLostError("closed")
        return FakeResp(self.bodies.pop(0))


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
