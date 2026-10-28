import asyncio
import importlib.util
import logging
import plistlib
import time

import pytest
from pyatv import exceptions
from pyatv.protocols.airplay.player import AirPlayPlayer
from pyatv.protocols.raop.protocols import StreamContext
from pyatv.protocols.raop.protocols import airplayv2 as _airplayv2
from pyatv.support.http import HttpResponse, http_connect
from pyatv.support.rtsp import RtspSession

from mirrorbridge import airplay_patch
from mirrorbridge.airplay_patch import CastContext, ReceiverRefusedVideo

URL = "http://192.168.1.32:7878/master.m3u8"


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
        if isinstance(body, Exception):
            raise body
        return body if isinstance(body, FakeResp) else FakeResp(body)


class FakePlayer:
    def __init__(self, bodies):
        self.rtsp = type("R", (), {"connection": FakeConn(bodies)})()


@pytest.fixture
def fast_sleep(monkeypatch):
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(airplay_patch.asyncio, "sleep", no_sleep)


@pytest.mark.usefixtures("fast_sleep")
async def test_slow_start_then_stop_on_tv():
    bodies = [{"readyToPlay": False}] * 10 + [{"duration": float("nan"), "rate": 1.0}] * 3 + [{}] * 4
    player = FakePlayer(bodies)
    await airplay_patch._wait_for_live_stream(player)
    assert player.rtsp.connection.bodies == []


@pytest.mark.usefixtures("fast_sleep")
async def test_live_stream_without_duration_counts_as_playing():
    player = FakePlayer([{"readyToPlay": True, "rate": 1.0}] * 50)
    await airplay_patch._wait_for_live_stream(player)  # ends only when connection closes


@pytest.mark.usefixtures("fast_sleep")
async def test_receiver_error_raises():
    with pytest.raises(exceptions.PlaybackError):
        await airplay_patch._wait_for_live_stream(FakePlayer([{"error": {"code": -1, "domain": "x"}}]))


@pytest.mark.usefixtures("fast_sleep")
async def test_never_starts_raises():
    with pytest.raises(exceptions.PlaybackError, match="never started"):
        await airplay_patch._wait_for_live_stream(FakePlayer([{}] * 40))


@pytest.mark.usefixtures("fast_sleep")
async def test_empty_playback_info_is_neither_playing_nor_stopped():
    """Some TVs answer 200 with no body: that must not end the cast, nor fail its start."""
    bodies = [FakeResp(None)] * 40 + [{"rate": 1.0}] + [FakeResp(None)] * 40 + [{}] * 4
    player = FakePlayer(bodies)
    await airplay_patch._wait_for_live_stream(player)
    assert player.rtsp.connection.bodies == []  # ended by the 4 idle answers, not before


def _blind_player(monkeypatch, bodies):
    """A player whose TV closes the connection after 3 polls of watching it blind."""
    player = FakePlayer(bodies)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if sleeps.count(airplay_patch.BLIND_POLL_SECONDS) == 3:
            player.rtsp.connection.transport = None  # the TV closes the connection

    monkeypatch.setattr(airplay_patch.asyncio, "sleep", sleep)
    return player, sleeps


@pytest.mark.parametrize("code", [404, 405, 501])
async def test_receiver_without_playback_info_is_kept_open_until_it_disconnects(monkeypatch, code):
    """Non-Apple TVs may answer /playback-info with 501; the cast must go on."""
    player, sleeps = _blind_player(monkeypatch, [FakeResp(None, code=code)])
    await airplay_patch._wait_for_live_stream(player)
    assert sleeps == [airplay_patch.BLIND_POLL_SECONDS] * 3


async def test_playback_info_that_keeps_failing_is_given_up_after_three_tries(monkeypatch):
    """tvOS 18+ answers 500 every time; a 403 or a timeout must not end the cast either."""
    bodies = [FakeResp(None, code=500), exceptions.AuthenticationError("not authenticated"),
              FakeResp({"rate": 1.0}), FakeResp(None, code=500), TimeoutError("no response"),
              FakeResp(None, code=500)]
    player, sleeps = _blind_player(monkeypatch, bodies)
    await airplay_patch._wait_for_live_stream(player)
    assert player.rtsp.connection.bodies == []  # a good answer restarts the count
    assert sleeps == [1] * 5 + [airplay_patch.BLIND_POLL_SECONDS] * 3


# -- AirPlayV2.play_url against receivers on a real socket ---------------------


class MockReceiver:
    """An AirPlay receiver that answers each request with respond(method, uri, protocol).

    respond returns (code, plist body or None, whether to echo CSeq), or None to hang up.
    pyatv's pairing and event channel are not part of this: MockV2 skips them.
    """

    def __init__(self, respond):
        self.respond = respond
        self.raw = []  # each request exactly as it arrived
        self.requests = []  # (method, uri, protocol)
        self.port = None
        self._server = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc):
        self._server.close()

    def sent(self, *, feedback=False):
        return [r for r in self.requests if feedback or r[1] != "/feedback"]

    async def _handle(self, reader, writer):
        buf = b""
        while True:
            data = await reader.read(65536)
            if not data:
                break
            buf += data
            while b"\r\n\r\n" in buf:
                head, rest = buf.split(b"\r\n\r\n", 1)
                lines = head.decode().split("\r\n")
                headers = dict(line.split(": ", 1) for line in lines[1:])
                size = int(headers.get("Content-Length", 0))
                if len(rest) < size:
                    break
                self.raw.append(head + b"\r\n\r\n" + rest[:size])
                buf = rest[size:]
                method, uri, protocol = lines[0].split(" ")
                self.requests.append((method, uri, protocol))
                answer = self.respond(method, uri, protocol)
                if answer is None:
                    writer.close()
                    return
                code, body, echo = answer
                payload = plistlib.dumps(body, fmt=plistlib.FMT_BINARY) if body is not None else b""
                out = f"{protocol} {code} {'OK' if code < 300 else 'Error'}\r\nContent-Length: {len(payload)}\r\n"
                if payload:
                    out += "Content-Type: application/x-apple-binary-plist\r\n"
                if echo and "CSeq" in headers:
                    out += f"CSeq: {headers['CSeq']}\r\n"
                writer.write(out.encode() + b"\r\n" + payload)
                await writer.drain()
        writer.close()


def apple_tv(method, uri, protocol):
    return 200, None, True


def put_refused_everywhere(method, uri, protocol):
    """PUT is 501 whatever the protocol line (the TCL 55QM7L may well do this)."""
    return (501 if method == "PUT" else 200), None, True


def uxplay_like(method, uri, protocol):
    """Routes by protocol line, like UxPlay: PUT and /rate only over HTTP/1.1."""
    if protocol == "RTSP/1.0" and (method == "PUT" or uri.startswith("/rate")):
        return 501, None, True
    return 200, None, True


def errors_without_cseq(method, uri, protocol):
    """Error replies carry no CSeq, so pyatv's exchange() would wait 4 s for each."""
    if method == "PUT" or (uri.startswith("/rate") and protocol == "RTSP/1.0"):
        return 501, None, False
    return 200, None, True


def _v2_class(base):
    class MockV2(base):
        feedback = True

        async def _setup_base(self, timing_server_port):  # no pair-verify, no event channel
            await self.rtsp.setup(body={"timingPort": timing_server_port})

        async def start_feedback(self):
            if self.feedback:
                await super().start_feedback()

    return MockV2


MockV2 = _v2_class(_airplayv2.AirPlayV2)


def pristine_airplayv2():
    """pyatv's own airplayv2 module loaded afresh: apply() changes the imported one."""
    spec = importlib.util.spec_from_file_location("pristine_airplayv2", _airplayv2.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HEADERS = _airplayv2.HEADERS  # the same X-Apple-Session-ID as ours
    return module


async def _connect(receiver, cls=MockV2, feedback=True):
    conn = await http_connect("127.0.0.1", receiver.port)
    rtsp = RtspSession(conn)
    # Fixed instead of random, so that two sessions' requests can be compared byte for byte.
    rtsp.dacp_id, rtsp.active_remote, rtsp.session_id = "MIRRORBRIDGE", 1, 1
    v2 = cls(StreamContext(), rtsp)
    v2.uuid = "u"
    v2.feedback = feedback
    return v2


async def _play(v2, cast):
    async def run():  # a task of its own, as airplay_patch.play_url runs
        airplay_patch._CAST.set(cast)
        try:
            return await airplay_patch._play_url_v2(v2, 1234, URL)
        finally:
            v2.teardown()
            v2.rtsp.connection.close()

    return await asyncio.ensure_future(run())


@pytest.mark.parametrize("apple", [True, None])
async def test_apple_tv_gets_exactly_the_requests_pyatv_sends(apple):
    """Apple TVs (and receivers that do not say what they are) see no change at all:
    the same requests, in the same order, with the same protocol line and bytes."""
    pristine = _v2_class(pristine_airplayv2().AirPlayV2)
    async with MockReceiver(apple_tv) as tv:
        v2 = await _connect(tv, pristine, feedback=False)
        resp = await v2.play_url(1234, URL)
        assert resp.code == 200
        v2.rtsp.connection.close()
        theirs = list(tv.raw)

    cast = CastContext(apple=apple, live=True)
    async with MockReceiver(apple_tv) as tv:
        resp = await _play(await _connect(tv, feedback=False), cast)
        assert resp.code == 200
        assert tv.raw == theirs
        assert [r[:2] for r in tv.requests] == [
            ("SETUP", tv.requests[0][1]), ("RECORD", tv.requests[1][1]), ("POST", "/play"),
            ("PUT", "/setProperty?isInterestedInDateRange"), ("PUT", "/setProperty?actionAtItemEnd"),
            ("POST", "/rate?value=1.000000"), ("PUT", "/setProperty?forwardEndTime"),
            ("PUT", "/setProperty?reverseEndTime"),
        ]
    assert cast.accepted_at is not None
    assert cast.steps == {"play": 200, "isInterestedInDateRange": 200, "actionAtItemEnd": 200,
                          "rate": 200, "forwardEndTime": 200, "reverseEndTime": 200}


async def test_put_refused_on_every_protocol_still_plays(caplog):
    """The 55QM7L case: POST /play works, PUT /setProperty is 501 Not Implemented."""
    cast = CastContext(apple=False, live=True)
    async with MockReceiver(put_refused_everywhere) as tv:
        with caplog.at_level(logging.INFO, logger="mirrorbridge.airplay_patch"):
            resp = await _play(await _connect(tv), cast)
        sent = tv.sent()
    assert resp.code == 200
    assert sent[2:] == [
        ("POST", "/play", "HTTP/1.1"),
        ("PUT", "/setProperty?isInterestedInDateRange", "RTSP/1.0"),
        ("PUT", "/setProperty?isInterestedInDateRange", "HTTP/1.1"),
        ("PUT", "/setProperty?actionAtItemEnd", "RTSP/1.0"),
        ("PUT", "/setProperty?actionAtItemEnd", "HTTP/1.1"),
        ("POST", "/rate?value=1.000000", "RTSP/1.0"),  # playback is still started
    ]
    assert cast.steps == {"play": 200, "isInterestedInDateRange": "501/HTTP:501",
                          "actionAtItemEnd": "501/HTTP:501", "rate": 200,
                          "forwardEndTime": "skipped", "reverseEndTime": "skipped"}
    # One line in mirrorbridge.log answers "what did the TV say to each request?"
    assert "Receiver answered /play with HTTP 200 OK" in caplog.text
    assert ("AirPlay video requests: play=200 isInterestedInDateRange=501/HTTP:501 "
            "actionAtItemEnd=501/HTTP:501 rate=200 forwardEndTime=skipped reverseEndTime=skipped") in caplog.text


async def test_receiver_that_wants_http_for_these_requests_gets_them_on_retry():
    cast = CastContext(apple=False, live=True)
    async with MockReceiver(uxplay_like) as tv:
        await _play(await _connect(tv), cast)
    assert cast.steps["isInterestedInDateRange"] == "501/HTTP:200"
    assert cast.steps["rate"] == "501/HTTP:200"


async def test_error_replies_without_cseq_cost_no_waiting():
    cast = CastContext(apple=False, live=True)
    async with MockReceiver(errors_without_cseq) as tv:
        started = time.monotonic()
        resp = await _play(await _connect(tv), cast)
        elapsed = time.monotonic() - started
    assert resp.code == 200
    assert elapsed < 2  # exchange() would have spent 4 s on each of 3 requests
    assert cast.steps["rate"] == "501/HTTP:200"


async def test_video_file_on_another_brand_still_gets_the_end_times():
    cast = CastContext(apple=False, live=False)
    async with MockReceiver(put_refused_everywhere) as tv:
        await _play(await _connect(tv), cast)
    assert cast.steps["forwardEndTime"] == cast.steps["reverseEndTime"] == "501/HTTP:501"


async def test_play_500_is_asked_again_on_the_same_session(monkeypatch):
    """pyatv's own retry would pair-verify and SETUP again on the encrypted connection."""
    monkeypatch.setattr(airplay_patch, "PLAY_RETRY_SECONDS", 0)
    answers = iter([500, 200])

    def flaky(method, uri, protocol):
        return (next(answers) if uri == "/play" else 200), None, True

    async with MockReceiver(flaky) as tv:
        cast = CastContext(apple=False, live=True)
        resp = await _play(await _connect(tv), cast)
        assert resp.code == 200 and cast.steps["play"] == "500/200"
        assert [r[0] for r in tv.sent()].count("SETUP") == 1

    def broken(method, uri, protocol):
        return (500 if uri == "/play" else 200), None, True

    async with MockReceiver(broken) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=r"play=500/500"):
            await _play(await _connect(tv), CastContext(apple=False, live=True))
        assert [r[1] for r in tv.sent()].count("/play") == 2
        assert [r[0] for r in tv.sent()].count("SETUP") == 1


async def test_receiver_that_refuses_everything():
    def refuses(method, uri, protocol):
        if method in ("SETUP", "RECORD") or uri == "/feedback":
            return 200, None, True
        return 501, None, False

    async with MockReceiver(refuses) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=r"refused video sent from a URL \(play=501\)") as err:
            await _play(await _connect(tv), CastContext(apple=False, live=True))
        assert not any(r[0] == "PUT" for r in tv.sent())
    assert err.value.code == 501 and err.value.steps == {"play": 501}


@pytest.mark.parametrize("code, words", [
    (401, "password .*which MirrorBridge cannot send"), (403, "Forget pairing"),
    (453, "busy with another AirPlay sender"), (470, "restricted"),
])
async def test_refusals_are_explained(code, words):
    def refuses(method, uri, protocol):
        return (code if uri == "/play" else 200), None, True

    async with MockReceiver(refuses) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=words) as err:
            await _play(await _connect(tv), CastContext(apple=False, live=True))
    assert err.value.code == code
    assert "status code" not in str(err.value)


def test_menu_items_in_errors_are_named_as_the_gui_and_the_command_line_name_them():
    """The errors are in English, but a Spanish menu has no "Forget pairing" to click."""
    from types import SimpleNamespace

    from mirrorbridge import cli
    from mirrorbridge.gui import i18n

    errors = (airplay_patch.refusal(403, {"play": 403}), airplay_patch.setup_failed({"setup": "not paired"}))
    message = " ".join(str(e) for e in errors)
    assert " ".join(i18n.say(e.args[0]) for e in errors) == message  # English
    i18n.set_language("es")
    try:
        spanish = " ".join(i18n.say(e.args[0]) for e in errors)
    finally:
        i18n.set_language("en")
    assert spanish == (
        "La TV ya no acepta el emparejamiento de MirrorBridge (HTTP 403). Usa Dispositivo > Olvidar "
        "emparejamiento, vuelve a transmitir y escribe el PIN nuevo que muestra la TV. La TV no terminó de "
        "preparar la sesión de AirPlay (setup=not paired). La TV pide que MirrorBridge se empareje primero: "
        "usa Dispositivo > Emparejar con PIN y vuelve a transmitir."
    )
    options = cli._cli_words(message, SimpleNamespace(name="Sala familiar"))
    for words in ('forget -d "Sala familiar"', 'pair -d "Sala familiar"'):
        assert words in options
    assert " > " not in options


@pytest.mark.parametrize("error", [
    airplay_patch.refusal(401, {}), airplay_patch.refusal(453, {}), airplay_patch.refusal(470, {}),
    airplay_patch.refusal(None, {"play": "no answer"}), airplay_patch.refusal(500, {"play": 500}, apple=True),
    airplay_patch.refusal(501, {"play": 501}, apple=False),
    airplay_patch.credentials_refused({"pair-verify": "not authenticated"}, password=True),
    airplay_patch.setup_failed({"setup": "no answer"}),
])
def test_every_refusal_is_in_spanish_for_the_gui(error):
    from mirrorbridge.gui import i18n

    assert i18n.say(error.args[0]) == str(error)
    i18n.set_language("es")
    try:
        spanish = i18n.say(error.args[0])
    finally:
        i18n.set_language("en")
    assert spanish.startswith(("La TV", "El Apple TV", "El acceso", "Esta TV")), spanish
    assert not any(words in spanish for words in ("The TV", " the ", "Device >", "Forget")), spanish


async def test_setup_refused_is_explained():
    def busy(method, uri, protocol):
        return (453 if method == "SETUP" else 200), None, True

    async with MockReceiver(busy) as tv:
        with pytest.raises(ReceiverRefusedVideo, match="busy") as err:
            await _play(await _connect(tv), CastContext())
    assert err.value.steps == {"setup": 453}


@pytest.mark.parametrize("password", [False, True])
async def test_setup_401_is_not_reported_as_a_403(password):
    """pyatv raises the same AuthenticationError for a 401 and a 403: name neither."""
    def wants_password(method, uri, protocol):
        return (401 if method == "SETUP" else 200), None, True

    async with MockReceiver(wants_password) as tv:
        with pytest.raises(ReceiverRefusedVideo, match="rejected MirrorBridge's credentials") as err:
            await _play(await _connect(tv), CastContext(apple=False, password=password))
    assert err.value.steps == {"setup": "not authenticated"} and err.value.code is None
    assert "403" not in str(err.value) and "Device > Forget pairing" in str(err.value)
    assert ("AirPlay password" in str(err.value)) is password  # only when the TV advertises one


async def test_tv_that_hangs_up_on_setup_gets_the_pairing_hint():
    def hangs_up(method, uri, protocol):
        return None if method == "SETUP" else (200, None, True)

    async with MockReceiver(hangs_up) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=r"did not finish setting up .*\(setup=connection lost\)"):
            await _play(await _connect(tv), CastContext(apple=False))
        assert ("POST", "/play") not in [r[:2] for r in tv.sent()]


def _setup_raising(error):
    class FailingV2(MockV2):
        async def _setup_base(self, timing_server_port):
            raise error

    return FailingV2


@pytest.mark.parametrize("error, answer, words", [
    # pyatv ignores the TV's last pair-verify answer: a TV that dropped the pairing never
    # answers the encrypted SETUP that follows.
    (TimeoutError("no response to SETUP rtsp://192.168.1.40/1 (RTSP/1.0)"), "no answer", "Forget pairing"),
    (exceptions.ConnectionLostError("connection was lost"), "connection lost", "Forget pairing"),
    (ConnectionRefusedError(111, "Connect call failed"), "connection refused", "Forget pairing"),  # event channel
    (exceptions.InvalidResponseError("got unexpected response"), "unreadable answer", "Forget pairing"),
    (KeyError(6), "unreadable answer", "Forget pairing"),  # a pair-verify TLV error
    (exceptions.NotSupportedError("encryption keys not supported"), "not paired", "Device > Pair with PIN"),
    (exceptions.AuthenticationError("incorrect device response"), "not authenticated", "Forget pairing"),
])
async def test_failed_session_setup_is_explained(error, answer, words):
    async with MockReceiver(apple_tv) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=words) as err:
            await _play(await _connect(tv, _setup_raising(error)), CastContext(apple=False))
    assert err.value.steps == {"setup": answer}
    assert str(error) not in str(err.value)  # never pyatv's own wording alone


async def test_play_without_an_answer_and_an_apple_tv_failing_are_not_blamed_on_the_brand(monkeypatch):
    monkeypatch.setattr(airplay_patch, "PLAY_RETRY_SECONDS", 0)
    silent = airplay_patch.refusal(None, {"play": "no answer"})
    assert "did not answer the request to play" in str(silent) and "Apple devices" not in str(silent)

    def broken(method, uri, protocol):
        return (500 if uri == "/play" else 200), None, True

    async with MockReceiver(broken) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=r"Apple TV could not start the video \(play=500/500\)") as err:
            await _play(await _connect(tv), CastContext(apple=True, live=True))
    assert "Apple devices" not in str(err.value)


class FakeStreamProtocol:
    """An AirPlay 1 receiver answering /play with `code`, as AirPlayV1.play_url returns it."""

    def __init__(self, code):
        self.code = code
        self.torn_down = False

    async def play_url(self, _port, _url, _position):
        return HttpResponse("HTTP", "1.1", self.code, "Not Found", {}, b"")

    def teardown(self):
        self.torn_down = True


async def test_player_names_the_refusal_and_cleans_up():
    """pyatv says AuthenticationError('status code: 404'); MirrorBridge says what happened."""
    async with MockReceiver(apple_tv) as tv:
        conn = await http_connect("127.0.0.1", tv.port)
        protocol = FakeStreamProtocol(404)
        player = AirPlayPlayer(RtspSession(conn), protocol)
        with pytest.raises(ReceiverRefusedVideo, match=r"\(play=404\)"):
            await airplay_patch._player_play_url(player, URL)
        conn.close()
    assert protocol.torn_down


async def test_airplay1_play_500_every_time_is_explained(monkeypatch):
    """pyatv itself asks an AirPlay 1 receiver three times, then says "Max retries exceeded"."""
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    async with MockReceiver(apple_tv) as tv:
        conn = await http_connect("127.0.0.1", tv.port)
        player = AirPlayPlayer(RtspSession(conn), FakeStreamProtocol(500))
        with pytest.raises(ReceiverRefusedVideo, match=r"refused video sent from a URL \(play=500/500/500\)") as err:
            await airplay_patch._player_play_url(player, URL)
        conn.close()
    assert err.value.code == 500 and "Max retries" not in str(err.value)


async def test_airplay1_pair_verify_refused_is_not_reported_as_a_403():
    class RefusedVerify(FakeStreamProtocol):
        async def play_url(self, _port, _url, _position):
            raise exceptions.AuthenticationError("not authenticated")

    async with MockReceiver(apple_tv) as tv:
        conn = await http_connect("127.0.0.1", tv.port)
        player = AirPlayPlayer(RtspSession(conn), RefusedVerify(200))
        with pytest.raises(ReceiverRefusedVideo, match=r"credentials \(pair-verify=not authenticated\)") as err:
            await airplay_patch._player_play_url(player, URL)
        conn.close()
    assert "403" not in str(err.value)


@pytest.mark.parametrize("how", ["refused", "stopped"])
async def test_timing_server_is_closed_however_the_cast_ends(monkeypatch, how):
    """pyatv closes its timing server's UDP socket only when play_url returns."""
    airplay_patch.apply()
    servers = []

    class Recorded(airplay_patch.TimingServer):
        def connection_made(self, transport):
            super().connection_made(transport)
            servers.append(self)

    monkeypatch.setattr(airplay_patch, "TimingServer", Recorded)

    def respond(method, uri, protocol):
        if how == "refused" and uri == "/play":
            return 501, None, True
        return (501 if uri == "/playback-info" else 200), None, True

    async with MockReceiver(respond) as tv:
        v2 = await _connect(tv)
        player = AirPlayPlayer(v2.rtsp, v2)
        task = asyncio.ensure_future(airplay_patch._player_play_url(player, URL))
        if how == "refused":
            with pytest.raises(ReceiverRefusedVideo):
                await asyncio.wait_for(task, 5)
        else:  # Stop pressed: MirrorBridge cancels the play task
            while ("GET", "/playback-info", "HTTP/1.1") not in tv.requests:
                assert not task.done()
                await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        v2.rtsp.connection.close()
    assert len(servers) == 1 and servers[0].transport is None


async def test_player_stops_the_keepalive_when_the_cast_ends(monkeypatch):
    """pyatv never calls teardown(): its feedback task would ping the TV forever."""
    airplay_patch.apply()
    monkeypatch.setattr(airplay_patch, "BLIND_POLL_SECONDS", 0.05)

    def no_playback_info(method, uri, protocol):
        return (501 if uri == "/playback-info" else 200), None, True

    async with MockReceiver(no_playback_info) as tv:
        v2 = await _connect(tv)
        player = AirPlayPlayer(v2.rtsp, v2)
        task = asyncio.ensure_future(airplay_patch._player_play_url(player, URL))
        deadline = time.monotonic() + 5
        while ("GET", "/playback-info", "HTTP/1.1") not in tv.requests:
            if task.done():
                task.result()  # whatever ended the cast early, as the failure
            assert time.monotonic() < deadline, tv.requests
            await asyncio.sleep(0.02)
        assert v2._feedback_task is not None
        v2.rtsp.connection.close()  # the TV goes away
        await asyncio.wait_for(task, 5)
    assert v2._feedback_task is None


def test_patches_target_existing_pyatv_methods():
    from pyatv.protocols.airplay.player import AirPlayPlayer as Player
    from pyatv.protocols.raop.protocols.airplayv2 import AirPlayV2

    assert hasattr(Player, "_wait_for_media_to_end") and hasattr(Player, "play_url")
    assert hasattr(AirPlayV2, "_setup_base") and hasattr(AirPlayV2, "start_feedback")
    assert hasattr(AirPlayV2, "teardown")
