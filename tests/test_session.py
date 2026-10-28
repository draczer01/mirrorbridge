"""How a cast decides that the TV took the video but is not playing it, and when.

The timeouts are written out as numbers here (20 s, 15 s...), not imported, so that a
changed timeout fails a test.
"""

import asyncio
import io
from types import SimpleNamespace

import pytest

from mirrorbridge import ffmpeg
from mirrorbridge import session as session_module
from mirrorbridge.airplay_patch import CastContext
from mirrorbridge.config import Settings
from mirrorbridge.hls_server import Activity
from mirrorbridge.session import CastSession, State, StreamNotFetched, StreamRejected

T0 = 1000.0
STEP = 0.5  # fake seconds per supervision round
TV = "192.168.1.40"
PC = "192.168.1.32"  # a player on the PC itself (VLC, a browser)


@pytest.fixture
def clock(monkeypatch):
    """session.time.monotonic() moves on STEP s per call from T0 (asyncio's own clock is untouched)."""
    now = [T0]

    def monotonic():
        now[0] += STEP
        return now[0]

    monkeypatch.setattr(session_module, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(session_module, "SUPERVISE_SECONDS", 0.001)
    return now


class FakeServer:
    """What each client has fetched by now, from a script of (seconds after T0, ip, path).

    A path that starts with "404 " is a request the server could not answer. The TV
    accepts the video (answers POST /play) `accepted` seconds after T0; None = never.
    """

    port = 7878

    def __init__(self, clock, cast, fetches, accepted, agents):
        self.clock, self.cast, self.fetches, self.accepted, self.agents = clock, cast, fetches, accepted, agents

    def _activity(self, ip):
        seen = Activity(user_agent=self.agents.get(ip, ""))
        for t, who, path in self.fetches:
            at = T0 + t
            if (ip is not None and who != ip) or at > self.clock[0]:
                continue
            if path.startswith("404 "):
                seen.missing += 1
                seen.missing_path = seen.missing_path or path[4:]
                continue
            seen.requests += 1
            seen.last_request_at = at
            if path.endswith(".m3u8"):
                seen.first_playlist_at = seen.first_playlist_at or at
            else:
                seen.first_segment_at = seen.first_segment_at or at
        return seen

    def activity(self, ip=None):
        if self.accepted is not None and self.cast.accepted_at is None and self.clock[0] >= T0 + self.accepted:
            self.cast.accepted_at = T0 + self.accepted
        return self._activity(ip)

    def clients(self):
        return {who: self._activity(who) for _t, who, _p in self.fetches}


def fetching(ip, playlist_from, until, segments_from=None):
    """A player that fetches the playlist every second, and a segment too from segments_from."""
    fetches, t = [], playlist_from
    while t <= until:
        fetches.append((t, ip, "master.m3u8"))
        if segments_from is not None and t >= segments_from:
            fetches.append((t, ip, f"seg_{t:05.0f}.ts"))
        t += 1
    return fetches


def _session(clock, fetches=(), accepted=0.0, for_apple=False, agents=None, **settings):
    device = SimpleNamespace(name="Sala familiar", address=TV, is_apple=None)
    session = CastSession(None, device, Settings(**settings))
    session.for_apple = for_apple
    session.encoder = "libx264"
    session.stream_url = f"http://{PC}:7878/master.m3u8"
    session._cast = CastContext(live=True)
    session._server = FakeServer(clock, session._cast, list(fetches), accepted, agents or {})
    session._play_task = asyncio.get_running_loop().create_future()  # the TV keeps the session open
    return session


def since(clock, seconds):
    """Fake seconds from `seconds` after T0 until now."""
    return clock[0] - (T0 + seconds)


async def test_tv_that_never_downloads_the_stream_gets_the_firewall_hint(clock):
    session = _session(clock, accepted=3)
    with pytest.raises(StreamNotFetched, match="Windows Firewall") as err:
        await session._supervise()
    assert "7878" in str(err.value)
    assert 20 < since(clock, 3) <= 21  # 20 s after the TV accepted the video


async def test_a_slow_tv_is_given_its_time(clock):
    """POST /play answered after 40 s, the playlist fetched 15 s after that, the first
    segment 10 s after the playlist: slow, but it plays. The cast then ends 20 s after
    the TV stops fetching."""
    session = _session(clock, fetching(TV, 55, until=90, segments_from=65), accepted=40)
    states = []
    session.on_status = lambda state, _m: states.append((state, since(clock, 0)))
    await session._supervise()
    assert [state for state, _t in states] == [State.CASTING]
    assert 55 <= states[0][1] <= 56  # announced with the TV's first fetch
    assert 20 < since(clock, 90) <= 21


@pytest.mark.parametrize("for_apple, encoder, no_codecs, hint", [
    (True, "libx264", False, r"Try Quality > Delay > Smooth\."),
    (False, "libx264", False, "lower resolution"),  # other TVs already get Smooth's 2 s segments
    (False, "h264_mf", True, r"Media Foundation produces: choose Quality > Encoder > x264 \(CPU\)"),
])
async def test_tv_that_downloads_only_the_playlist_gets_the_format_hint(clock, for_apple, encoder, no_codecs, hint):
    session = _session(clock, fetching(TV, 3, until=60), accepted=1, for_apple=for_apple, latency_mode="low")
    session.encoder, session.no_codecs = encoder, no_codecs
    with pytest.raises(StreamRejected, match=hint):
        await session._supervise()
    assert 15 < since(clock, 3) <= 16  # 15 s after its first playlist


@pytest.mark.parametrize("extend_mode, hint", [
    ("1080p", r"format\. Try What to cast > Extended screen size > 720p, or 30 frames per second\.$"),
    ("720p", r"format\. Try 30 frames per second\.$"),
])
async def test_an_extended_cast_is_pointed_at_a_smaller_virtual_screen(clock, extend_mode, hint):
    """Quality > Resolution does not apply to the virtual screen, which is sent at its own size."""
    session = _session(clock, fetching(TV, 3, until=60), accepted=1, extend_screen=True, extend_mode=extend_mode)
    session.virtual_device = r"\\.\DISPLAY9"
    with pytest.raises(StreamRejected, match=hint):
        await session._supervise()


async def test_tv_that_never_answers_gets_an_error_too(clock):
    with pytest.raises(RuntimeError, match="did not start the video"):
        await _session(clock, accepted=None)._supervise()
    assert 45 < since(clock, STEP) <= 46  # 45 s after supervision started


async def test_only_the_tvs_own_fetches_count(clock):
    """A player on the PC fetching the stream neither hides a TV that cannot reach
    MirrorBridge nor counts as the TV casting."""
    agents = {PC: "VLC/3.0.20 LibVLC/3.0.20"}
    session = _session(clock, fetching(PC, 2, until=80, segments_from=2), accepted=1, agents=agents)
    states = []
    session.on_status = lambda state, _m: states.append(state)
    with pytest.raises(StreamNotFetched, match="Windows Firewall") as err:
        await session._supervise()
    assert states == []  # never "Casting"
    assert f"{PC} (VLC/3.0.20 LibVLC/3.0.20) did download it" in str(err.value)
    assert 20 < since(clock, 1) <= 21


async def test_tv_that_asks_for_something_else_is_not_sent_to_the_firewall(clock):
    session = _session(clock, [(2, TV, "404 /live/master.m3u8")], accepted=1)
    with pytest.raises(StreamNotFetched, match="reached MirrorBridge but asked for /live/master.m3u8") as err:
        await session._supervise()
    assert "Firewall" not in str(err.value)


async def test_tv_that_asks_for_a_missing_segment_says_so(clock):
    fetches = fetching(TV, 3, until=60) + [(4, TV, "404 /seg_00001.ts")]
    with pytest.raises(StreamRejected, match=r"also asked for /seg_00001\.ts, which MirrorBridge does not have"):
        await _session(clock, fetches, accepted=1)._supervise()


class FakeFFmpeg:
    stderr = io.BytesIO(b"")


@pytest.mark.parametrize("is_apple, count, timeout, master", [
    (True, 3, 25, False),  # Apple TVs: as soon as 3 segments exist, as always
    (False, 6, 37, True),  # others: the playlist's whole 12 s window, 6 segments of 2 s
    (None, 6, 37, True),
])
async def test_other_tvs_get_the_playlist_once_it_holds_12_seconds(monkeypatch, tmp_path, is_apple, count, timeout,
                                                                   master):
    waits = []

    async def wait_for_segments(_self, playlist, count=3, timeout=25, master=None):
        waits.append((playlist.name, count, timeout, master))

    monkeypatch.setattr(ffmpeg, "popen", lambda _cmd: FakeFFmpeg())
    monkeypatch.setattr(CastSession, "_wait_for_segments", wait_for_segments)
    device = SimpleNamespace(name="Sala familiar", address=TV, is_apple=is_apple)
    session = CastSession(None, device, Settings(latency_mode="low", capture_audio=False), work_dir=tmp_path)
    session.for_apple = is_apple is True
    session.backend, session.encoder = "test", "libx264"
    await session._launch_encoder("ffmpeg", None)
    assert waits == [("stream.m3u8", count, timeout, tmp_path / "master.m3u8" if master else None)]
