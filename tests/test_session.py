"""How a cast decides that the TV took the video but is not playing it."""

import asyncio
from types import SimpleNamespace

import pytest

from aircast import session as session_module
from aircast.airplay_patch import CastContext
from aircast.config import Settings
from aircast.hls_server import Activity
from aircast.session import CastSession, StreamNotFetched, StreamRejected


class FakeServer:
    port = 7878

    def __init__(self, tv: Activity):
        self.tv = tv

    def activity(self, ip=None):
        return self.tv if ip in (None, "192.168.1.40") else Activity()


@pytest.fixture
def clock(monkeypatch):
    """session.time.monotonic() jumps 5 s per call (asyncio's own clock is untouched)."""
    now = [1000.0]

    def monotonic():
        now[0] += 5
        return now[0]

    monkeypatch.setattr(session_module, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(session_module, "SUPERVISE_SECONDS", 0.001)
    return now


def _session(tv: Activity, accepted_at=None, for_apple=False, **settings):
    device = SimpleNamespace(name="Sala familiar", address="192.168.1.40", is_apple=None)
    session = CastSession(None, device, Settings(**settings))
    session.for_apple = for_apple
    session.stream_url = "http://192.168.1.32:7878/master.m3u8"
    session._server = FakeServer(tv)
    session._cast = CastContext(live=True, accepted_at=accepted_at)
    session._play_task = asyncio.get_running_loop().create_future()  # the TV keeps the session open
    return session


async def test_tv_that_never_downloads_the_stream_gets_the_firewall_hint(clock):
    with pytest.raises(StreamNotFetched, match="Windows Firewall") as err:
        await _session(Activity(), accepted_at=clock[0])._supervise()
    assert "7878" in str(err.value)


@pytest.mark.parametrize("for_apple, latency, hint", [
    (True, "low", "Smooth"),
    (False, "low", "lower resolution"),  # other TVs already get Smooth's 2 s segments
])
async def test_tv_that_downloads_only_the_playlist_gets_the_format_hint(clock, for_apple, latency, hint):
    tv = Activity(requests=3, first_playlist_at=clock[0], last_request_at=10**9)
    with pytest.raises(StreamRejected, match=hint):
        await _session(tv, accepted_at=clock[0], for_apple=for_apple, latency_mode=latency)._supervise()


async def test_tv_that_plays_then_stops_fetching_ends_the_cast(clock):
    tv = Activity(requests=9, first_playlist_at=clock[0], first_segment_at=clock[0], last_request_at=clock[0])
    states = []
    session = _session(tv, accepted_at=clock[0])
    session.on_status = lambda state, _m: states.append(state)
    await session._supervise()  # returns: the TV went quiet for longer than IDLE_REQUEST_TIMEOUT
    assert states == [session_module.State.CASTING]


async def test_tv_that_never_answers_gets_an_error_too(clock):
    with pytest.raises(RuntimeError, match="did not start the video"):
        await _session(Activity())._supervise()
