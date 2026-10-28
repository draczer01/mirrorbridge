import asyncio
import importlib.util
import logging
import plistlib
import time

import pytest
from pyatv import exceptions
from pyatv.auth.hap_pairing import parse_credentials
from pyatv.auth.hap_session import HAPSession
from pyatv.auth.hap_srp import hkdf_expand
from pyatv.auth.server_auth import CLIENT_CREDENTIALS
from pyatv.protocols.airplay.player import AirPlayPlayer
from pyatv.protocols.airplay.server_auth import BaseAirPlayServerAuth
from pyatv.protocols.raop.protocols import StreamContext
from pyatv.protocols.raop.protocols import airplayv2 as _airplayv2
from pyatv.protocols.raop.protocols.airplayv2 import EVENTS_READ_INFO, EVENTS_SALT, EVENTS_WRITE_INFO
from pyatv.support.http import HttpResponse, format_response, http_connect, parse_request, parse_response
from pyatv.support.rtsp import RtspSession

from mirrorbridge import airplay_patch, airplay_queue
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


async def test_player_stops_the_keepalive_when_the_cast_ends(monkeypatch, caplog):
    """pyatv never calls teardown(): its feedback task would ping the TV forever."""
    airplay_patch.apply()
    monkeypatch.setattr(airplay_patch, "BLIND_POLL_SECONDS", 0.05)
    caplog.set_level(logging.INFO, logger="mirrorbridge.airplay_patch")

    def no_playback_info(method, uri, protocol):
        return (501 if uri == "/playback-info" else 200), None, True

    async with MockReceiver(no_playback_info) as tv:
        v2 = await _connect(tv)
        player = AirPlayPlayer(v2.rtsp, v2)
        task = asyncio.ensure_future(airplay_patch._player_play_url(player, URL))
        deadline = time.monotonic() + 5
        # Until the player has its 501: closing the connection under a request still waiting
        # for its answer would leave that request to its own 10 s time-out.
        while "does not report playback status" not in caplog.text:
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


# -- The play queue (AirPlay video v2), for TVs that have no POST /play --------------------------

TCL_FEATURES = 0x38BCF46 << 32 | 0x7F8AD0  # what the TCL 55QM7L advertises: "0x7F8AD0,0x38BCF46"
APPLE_TV_FEATURES = 0x3C155FDE << 32 | 0x4A7FDFD5  # an Apple TV 4K (AppleTV6,2)
STREAM_ID = 3
CREDENTIALS = parse_credentials(CLIENT_CREDENTIALS)  # what pyatv's own receiver tests pair with
TV_AGENT = "AppleCoreMedia/1.0.0 (TCL 55QM7L)"
PLAYING = (
    {"type": "notification", "name": "currentItemChanged", "reason": "ReasonAddToPlayQueue"},
    {"type": "playbackState", "name": "loading"},
    {"type": "playbackState", "name": "playing"},
)
BACK = ({"type": "sendMediaRemoteCommand", "value": "pbpr"}, {"type": "sendMediaRemoteCommand", "value": "pbal"})
HANG_UP = "hang up"  # in what a QueueTV says: it closes its event channel
XML = "xml"  # (XML, message): said as an XML plist, with no Content-Type
COMMAND_NAMES = {"insertPlayQueueItem": "insert", "setRate": "rate"}


@pytest.fixture(autouse=True)
def no_remembered_routes():
    airplay_patch._ROUTES.clear()
    yield
    airplay_patch._ROUTES.clear()


def _bplist(value):
    return plistlib.dumps(value, fmt=plistlib.FMT_BINARY)


class _Verify(BaseAirPlayServerAuth):
    """pyatv's receiver side of pair-verify, which hands over the keys it agreed on."""

    def __init__(self):
        super().__init__("QueueTV")
        self.agreed = None  # (output key, input key); its own `keys` are its identity

    def enable_encryption(self, output_key, input_key):
        self.agreed = (output_key, input_key)


class QueueTV:
    """A TV with the play queue and no POST /play, as the TCL 55QM7L: on real sockets,
    pair-verified and HAP-encrypted, with an event channel on which it says how playback goes.

    `refuse` maps a request's name (play, setup, record, setup130, insert, feedback, teardown...)
    to the status it gets; /play gets 404 unless told otherwise. `late` maps a name to the
    seconds the TV takes to answer it, once. `said` is what the TV says on the event channel
    once it has taken an item; with `fetch`, its player first fetches the item's URL, the
    playlists in it and a segment. With cseq=False, its answers carry no CSeq.
    """

    def __init__(self, *, refuse=None, late=None, said=PLAYING + BACK, cseq=True, fetch=False):
        self.refuse = {"play": 404, **(refuse or {})}
        self.late = dict(late or {})
        self.said = list(said)
        self.cseq = cseq
        self.fetch = fetch
        self.requests = []  # (name, method, uri, protocol, headers, body, encrypted) as each arrived
        self.commands = []  # what each /command said, unwrapped
        self.overlaps = []  # names of requests that came while another one was still unanswered
        self.connections = 0
        self.answers = []  # how the sender answered each event: (status, CSeq)
        self.fetched = []  # what the TV's player fetched: (path, status)
        self.port = self.event_port = None
        self._servers = []
        self._events = None  # (writer, HAPSession) of the open event channel
        self._shared = None  # the latest pair-verify's shared secret
        self._next_event = 1
        self._tasks = set()

    async def __aenter__(self):
        control = await asyncio.start_server(self._control, "127.0.0.1", 0)
        events = await asyncio.start_server(self._event_channel, "127.0.0.1", 0)
        self._servers = [control, events]
        self.port = control.sockets[0].getsockname()[1]
        self.event_port = events.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._events is not None:
            self._events[0].close()
        for server in self._servers:
            server.close()

    def names(self, *, feedback=False):
        return [r[0] for r in self.requests if feedback or r[0] != "feedback"]

    def first(self, name):
        return next(r for r in self.requests if r[0] == name)

    # -- the control connection

    async def _control(self, reader, writer):
        self.connections += 1
        session, verify = HAPSession(), _Verify()
        state = {"unanswered": 0, "remote control": False}
        pending = asyncio.Queue()
        answering = asyncio.ensure_future(self._answer_in_turn(pending, writer, session, state))
        buf = b""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                buf += session.decrypt(data)
                while True:
                    request, buf = parse_request(buf)
                    if request is None:
                        break
                    name = self._name(request)
                    self.requests.append((name, request.method, request.path, f"{request.protocol}/{request.version}",
                                          dict(request.headers), request.body, session.chacha20 is not None))
                    if name == "pair-verify":  # in the clear, and before anything else is read
                        writer.write(format_response(verify.handle_request(request)))
                        if verify.agreed is not None:
                            session.enable(*verify.agreed)
                            self._shared = verify.shared_key
                        continue
                    if state["unanswered"]:
                        self.overlaps.append(name)
                    state["unanswered"] += 1
                    pending.put_nowait((name, request))
        except ConnectionError:
            pass
        finally:
            answering.cancel()
            writer.close()

    async def _answer_in_turn(self, pending, writer, session, state):
        while True:
            name, request = await pending.get()
            delay = self.late.pop(name, 0)
            if delay:
                await asyncio.sleep(delay)
            code, body, then = self._respond(name, request, state)
            payload = _bplist(body) if body is not None else b""
            head = (f"{request.protocol}/{request.version} {code} {'OK' if code < 300 else 'Error'}\r\n"
                    f"Server: AirTunes/377.40.00\r\nContent-Length: {len(payload)}\r\n")
            if payload:
                head += "Content-Type: application/x-apple-binary-plist\r\n"
            if self.cseq and "CSeq" in request.headers:
                head += f"CSeq: {request.headers['CSeq']}\r\n"
            state["unanswered"] -= 1
            if writer.is_closing():
                return
            writer.write(session.encrypt(head.encode() + b"\r\n" + payload))
            if then is not None:
                task = asyncio.ensure_future(then)
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def _name(self, request):
        if request.path == "/pair-verify":
            return "pair-verify"
        if request.method == "SETUP":
            return "setup130" if "streams" in plistlib.loads(request.body) else "setup"
        if request.path == "/command":
            command = plistlib.loads(plistlib.loads(request.body)["params"]["data"])
            self.commands.append(command)
            what = command.get("type")
            return command["property"] if what == "setProperty" else COMMAND_NAMES.get(what, what)
        return {"RECORD": "record", "TEARDOWN": "teardown"}.get(request.method) or request.path.strip("/")

    def _respond(self, name, request, state):
        """(status, plist body or None, what to do once answered or None)"""
        if name in self.refuse:
            return self.refuse[name], None, None
        if name == "setup":
            return 200, {"eventPort": self.event_port, "timingPort": 0}, None
        if name == "setup130":
            state["remote control"] = True
            return 200, {"streams": [{"type": 130, "streamID": STREAM_ID}]}, None
        if name == "info":
            return 200, {"psi": "PSI-OF-THE-TV", "features": TCL_FEATURES}, None
        if request.path == "/command":
            if not state["remote control"] or request.headers.get("X-Apple-StreamID") != str(STREAM_ID):
                return 500, None, None
            command = self.commands[-1]
            if name == "insert":
                return 200, None, self._play(command["item"])
            if name == "playbackInfo":
                return 200, None, self.say({"kind": "response", "type": "playbackInfo",
                                            "messageID": command["messageID"],
                                            "info": {"playbackState": "playing", "rate": 1.0}})
        return 200, None, None

    async def _play(self, item):
        if self.fetch:
            await self._fetch(item["Content-Location"])
        for message in self.said:
            if message == HANG_UP:
                self._events[0].close()
                continue
            if isinstance(message, dict) and message.get("type") == "notification":
                message = {**message, "item": {"uuid": item["uuid"]}}  # notifications name the item
            await self.say(message)

    async def _fetch(self, url):
        """As a player would: the playlist, the playlist in it, and the first segment."""
        import aiohttp

        try:
            async with aiohttp.ClientSession(headers={"User-Agent": TV_AGENT}) as http:
                for _ in range(3):
                    async with http.get(url) as resp:
                        self.fetched.append((resp.url.path, resp.status))
                        text = await resp.text(errors="replace") if url.endswith(".m3u8") else ""
                    entries = [line for line in text.splitlines() if line and not line.startswith("#")]
                    if not entries:
                        return
                    url = str(resp.url.join(aiohttp.client.URL(entries[0])))
        except aiohttp.ClientError as ex:
            self.fetched.append((url, repr(ex)))

    # -- the event channel

    async def _event_channel(self, reader, writer):
        # The TV writes with the key the sender reads with, and the other way round.
        keys = [hkdf_expand(EVENTS_SALT, info, self._shared) for info in (EVENTS_WRITE_INFO, EVENTS_READ_INFO)]
        session = HAPSession()
        session.enable(*keys)
        channel = self._events = (writer, session)
        buf = b""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                buf += session.decrypt(data)
                while True:
                    answer, buf = parse_response(buf)
                    if answer is None:
                        break
                    self.answers.append((answer.code, answer.headers.get("CSeq")))
        except ConnectionError:
            pass
        finally:
            if self._events is channel:
                self._events = None
            writer.close()

    async def say(self, message):
        """What these TVs say on the event channel: POST /command RTSP/1.0 {params: {data}}."""
        while self._events is None:
            await asyncio.sleep(0.01)
        writer, session = self._events
        head = f"POST /command RTSP/1.0\r\nCSeq: {self._next_event}\r\nServer: AirTunes/377.40.00\r\n"
        if isinstance(message, tuple):  # (XML, message): pyatv reads a body with no Content-Type as text
            body = plistlib.dumps({"params": {"data": _bplist(message[1])}}, fmt=plistlib.FMT_XML)
        else:
            body = _bplist({"params": {"data": _bplist(message)}})
            head += "Content-Type: application/x-apple-binary-plist\r\n"
        self._next_event += 1
        writer.write(session.encrypt(head.encode() + f"Content-Length: {len(body)}\r\n\r\n".encode() + body))
        await writer.drain()


class QueueAtv:
    """What manager.connect() returns, as far as playing a URL goes: pyatv's AirPlayStream.play_url
    (a connection of its own, AirPlayV2 and its player), against `tv`."""

    def __init__(self, tv):
        self.tv = tv
        self.stream = self
        self.closed = False

    async def play_url(self, url, **_kwargs):
        connection = await http_connect("127.0.0.1", self.tv.port)
        try:
            context = StreamContext()
            context.credentials = CREDENTIALS
            v2 = _airplayv2.AirPlayV2(context, RtspSession(connection))
            await AirPlayPlayer(v2.rtsp, v2).play_url(url, 0)
        finally:
            connection.close()

    def close(self):
        self.closed = True


class MockAtv:
    """The same for a MockReceiver, with _play_url_v2 only, as the byte-for-byte tests above."""

    def __init__(self, receiver):
        self.receiver = receiver
        self.stream = self

    async def play_url(self, url, **_kwargs):
        v2 = await _connect(self.receiver, feedback=False)
        try:
            self.response = await airplay_patch._play_url_v2(v2, 1234, url)
        finally:
            v2.teardown()
            v2.rtsp.connection.close()


async def _cast_to(atv, cast, url=URL, timeout=10):
    airplay_patch.apply()
    return await asyncio.wait_for(asyncio.ensure_future(airplay_patch.play_url(atv, url, cast)), timeout)


def _queue_tasks():
    return [t for t in asyncio.all_tasks() if not t.done() and "QueueSession" in repr(t.get_coro())]


@pytest.mark.parametrize("features, route", [
    (TCL_FEATURES, airplay_patch.QUEUE),
    (APPLE_TV_FEATURES, airplay_patch.PLAY),  # bit 0, with bits 33 and 49 too
    (1 << 49, airplay_patch.QUEUE),
    (1 << 33, airplay_patch.QUEUE),
    (None, airplay_patch.PLAY),  # it does not say: /play, and the play queue after a 404
    (1 << 9, airplay_patch.PLAY),  # audio only, as far as it says
])
def test_the_route_follows_the_feature_bits(features, route):
    assert airplay_patch.choose_route(CastContext(features=features))[0] == route


def test_a_route_that_worked_or_was_asked_for_comes_first():
    airplay_patch._remember(CastContext(device="TCL"), airplay_patch.PLAY)
    assert airplay_patch.choose_route(CastContext(device="TCL", features=TCL_FEATURES)) == (
        airplay_patch.PLAY, "it worked with this receiver before")
    assert airplay_patch.choose_route(CastContext(device="TCL", route=airplay_patch.QUEUE)) == (
        airplay_patch.QUEUE, "asked for")


async def test_a_route_that_was_asked_for_is_not_remembered():
    """Test this TV tries the play queue on an Apple TV too: later casts still get pyatv's /play."""
    asked = CastContext(apple=False, live=False, device="AppleTV", features=APPLE_TV_FEATURES,
                        route=airplay_patch.QUEUE, fallback=False)
    async with QueueTV() as tv:
        await _cast_to(QueueAtv(tv), asked)
    assert asked.route == airplay_patch.QUEUE and asked.accepted_at is not None
    assert not airplay_patch._ROUTES
    assert airplay_patch.choose_route(CastContext(device="AppleTV", features=APPLE_TV_FEATURES))[0] == \
        airplay_patch.PLAY


@pytest.mark.parametrize("features", [APPLE_TV_FEATURES, None])
async def test_receivers_with_airplay_video_v1_get_exactly_the_requests_pyatv_sends(features, caplog):
    """Apple TVs, and receivers that do not say, see no change: the same bytes as pyatv's own."""
    pristine = _v2_class(pristine_airplayv2().AirPlayV2)
    async with MockReceiver(apple_tv) as tv:
        v2 = await _connect(tv, pristine, feedback=False)
        assert (await v2.play_url(1234, URL)).code == 200
        v2.rtsp.connection.close()
        theirs = list(tv.raw)

    cast = CastContext(apple=True, live=True, device="AppleTV", features=features)
    async with MockReceiver(apple_tv) as tv:
        with caplog.at_level(logging.INFO, logger="mirrorbridge.airplay_patch"):
            await _cast_to(MockAtv(tv), cast)
        assert tv.raw == theirs
    assert cast.route == airplay_patch.PLAY and cast.queue is None
    assert airplay_patch._ROUTES == {"AppleTV": airplay_patch.PLAY}
    assert "Sending the video by POST /play (AirPlay video v1)" in caplog.text

    # After Test this TV sent this receiver a video through the play queue, the same again
    probe_like = CastContext(apple=True, live=False, device="AppleTV", features=features, route=airplay_patch.QUEUE,
                             fallback=False)
    async with QueueTV() as tv:
        await _cast_to(QueueAtv(tv), probe_like)
    again = CastContext(apple=True, live=True, device="AppleTV", features=features)
    async with MockReceiver(apple_tv) as tv:
        await _cast_to(MockAtv(tv), again)
        assert tv.raw == theirs
    assert again.route == airplay_patch.PLAY


@pytest.mark.parametrize("live", [True, False])
async def test_a_tv_with_only_the_play_queue_gets_the_url_through_it(live, caplog):
    cast = CastContext(apple=False, live=live, device="TCL", features=TCL_FEATURES)
    async with QueueTV() as tv:
        with caplog.at_level(logging.INFO, logger="mirrorbridge"):
            await _cast_to(QueueAtv(tv), cast)
        assert not _queue_tasks()  # the keep-alive is gone with the cast
    assert tv.connections == 1 and tv.names() == [
        "pair-verify", "pair-verify", "setup", "record", "setup130", "insert", "isInterestedInDateRange",
        "actionAtItemEnd", "rate", "stop", "teardown",
    ]
    assert all(r[6] for r in tv.requests if r[0] != "pair-verify")  # HAP-encrypted, every one
    # A TV that asks whether to allow a new sender names it from this header, SETUP's included
    assert {r[4].get("X-Apple-Client-Name") for r in tv.requests if r[0] != "pair-verify"} == {"MirrorBridge"}
    assert tv.overlaps == []
    assert cast.route == airplay_patch.QUEUE and cast.accepted_at is not None
    assert airplay_patch._ROUTES == {"TCL": airplay_patch.QUEUE}
    assert cast.steps == {"setup": 200, "record": 200, "setup130": 200, "insert": 200,
                          "isInterestedInDateRange": 200, "actionAtItemEnd": 200, "rate": 200}

    setup = plistlib.loads(tv.first("setup")[5])
    assert setup["timingProtocol"] == "NTP" and isinstance(setup["timingPort"], int)
    assert setup["deviceID"] == "AA:BB:CC:DD:EE:FF" and "sessionCorrelationUUID" not in setup
    (stream,) = plistlib.loads(tv.first("setup130")[5])["streams"]
    assert stream == {"type": 130, "controlType": 1, "clientTypeUUID": "A6B27562-B43A-4F2D-B75F-82391E250194",
                      "channelID": "AA:BB:CC:DD:EE:FF-RCS-1", "clientUUID": stream["clientUUID"].upper()}
    _, method, uri, protocol, headers, _, _ = tv.first("insert")
    assert (method, uri, protocol) == ("POST", "/command", "HTTP/1.1")
    assert headers["User-Agent"] == "AirPlay/870.14.1" and headers["X-Apple-StreamID"] == str(STREAM_ID)
    assert headers["Content-Type"] == "application/x-apple-binary-plist" and headers["X-Apple-ProtocolVersion"] == "1"
    assert headers["X-Apple-Session-ID"] == headers["X-Apple-Session-ID"].upper()
    insert, date_range, at_end, rate, stop = tv.commands
    item = insert["item"]
    assert insert["type"] == "insertPlayQueueItem" and item["Content-Location"] == URL and item["mediaType"] == "file"
    assert "Start-Position-Seconds" not in item
    if live:  # the TV's player picks where a live stream starts
        assert "Start-Position" not in item
    else:
        assert item["Start-Position"] == {"value": 0, "timescale": 1000, "flags": 1, "epoch": 0}
    assert date_range == {"type": "setProperty", "property": "isInterestedInDateRange", "value": True,
                          "item": {"uuid": item["uuid"]}}
    assert at_end == {"type": "setProperty", "property": "actionAtItemEnd", "value": 1}
    assert rate == {"type": "setRate", "rate": 1.0} and stop == {"type": "stop"}

    assert tv.answers == [(200, str(n)) for n in range(1, 6)]  # every event answered, CSeq echoed
    assert cast.queue.events == {"notification/currentItemChanged": 1, "playbackState/loading": 1,
                                 "playbackState/playing": 1, "sendMediaRemoteCommand/pbpr": 1,
                                 "sendMediaRemoteCommand/pbal": 1}
    assert cast.queue.taken.is_set() and cast.queue.played.is_set()
    for line in ("Sending the video by the play queue (AirPlay video v2): the receiver advertises the play queue",
                 "AirPlay video requests: setup=200 record=200 setup130=200 insert=200 isInterestedInDateRange=200 "
                 "actionAtItemEnd=200 rate=200",
                 "The TV says playbackState/playing", "The cast ended: BACK was pressed on the TV's remote",
                 "Ended the AirPlay video session: stop=200 teardown=200"):
        assert line in caplog.text


async def test_a_receiver_that_answers_play_with_404_gets_the_play_queue_on_a_new_connection(caplog):
    """No features to go by (a TV typed in by hand): /play first, then once the play queue."""
    cast = CastContext(apple=False, live=True, device="LG")
    async with QueueTV() as tv:
        with caplog.at_level(logging.INFO, logger="mirrorbridge.airplay_patch"):
            await _cast_to(QueueAtv(tv), cast)
        assert tv.connections == 2
        assert tv.names() == [
            "pair-verify", "pair-verify", "setup", "record", "play",  # pyatv's own, on the first connection
            "pair-verify", "pair-verify", "setup", "record", "setup130", "insert", "isInterestedInDateRange",
            "actionAtItemEnd", "rate", "stop", "teardown",
        ]
        assert cast.steps["play"] == 404 and cast.steps["insert"] == 200 and cast.accepted_at is not None
        assert "The receiver has no POST /play (HTTP 404); trying the play queue (AirPlay video v2) on a new " \
               "connection" in caplog.text
        assert airplay_patch._ROUTES == {"LG": airplay_patch.QUEUE}

        again = CastContext(apple=False, live=True, device="LG")  # the next cast goes straight there
        sent = len(tv.requests)
        with caplog.at_level(logging.INFO, logger="mirrorbridge.airplay_patch"):
            await _cast_to(QueueAtv(tv), again)
        assert "play" not in [r[0] for r in tv.requests[sent:]] and again.route == airplay_patch.QUEUE
        assert "it worked with this receiver before" in caplog.text


async def test_without_the_fallback_a_404_to_play_is_a_refusal():
    cast = CastContext(apple=False, live=True, device="LG", fallback=False)
    async with QueueTV() as tv:
        with pytest.raises(ReceiverRefusedVideo, match=r"refused video sent from a URL \(play=404\)"):
            await _cast_to(QueueAtv(tv), cast)
        assert tv.connections == 1
    assert not airplay_patch._ROUTES


@pytest.mark.parametrize("refuse, words", [
    ({"setup130": 501}, r"The TV turned down the video sent from a URL \(setup=200 record=200 setup130=501\)\. "
                        r"Use More options > Test this TV"),
    ({"insert": 400}, r"turned down the video sent from a URL \(.*setup130=200 insert=400\)"),
    ({"insert": 403}, r"no longer accepts MirrorBridge's pairing \(HTTP 403\)"),
    ({"record": 470}, r"\(HTTP 470\)"),
])
async def test_a_tv_that_turns_the_play_queue_down_is_explained(refuse, words):
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(refuse=refuse) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=words):
            await _cast_to(QueueAtv(tv), cast)
        assert tv.names()[-1] == "teardown" and "stop" not in tv.names()  # nothing was playing
        assert not _queue_tasks()
    assert not airplay_patch._ROUTES


async def test_a_tv_that_does_not_answer_the_play_queue_setup_gets_the_setup_hint(monkeypatch):
    monkeypatch.setattr(airplay_queue, "REQUEST_TIMEOUT", 0.2)
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(late={"setup130": 5}) as tv:
        with pytest.raises(ReceiverRefusedVideo, match=r"did not finish setting up the AirPlay session "
                                                        r"\(setup=200 record=200 setup130=no answer\)"):
            await _cast_to(QueueAtv(tv), cast)


async def test_the_keep_alive_and_the_tvs_status_never_overlap_a_command(monkeypatch, caplog):
    """A TV drops the connection when two requests overlap: pyatv's keep-alive runs on its own."""
    monkeypatch.setattr(airplay_queue, "FEEDBACK_INTERVAL", 0.005)
    monkeypatch.setattr(airplay_queue, "INFO_INTERVAL", 0.01)
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=PLAYING) as tv:  # it plays until the cast is stopped
        caplog.set_level(logging.INFO, logger="mirrorbridge.airplay_queue")
        task = asyncio.ensure_future(_cast_to(QueueAtv(tv), cast))
        deadline = time.monotonic() + 10
        while tv.names(feedback=True).count("feedback") < 20 or tv.names().count("playbackInfo") < 5:
            assert not task.done() and time.monotonic() < deadline, tv.names(feedback=True)
            await asyncio.sleep(0.02)
        task.cancel()  # Stop casting
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tv.overlaps == []
        assert tv.names()[-2:] == ["stop", "teardown"]
        assert not _queue_tasks()
    # What the TV answered on the event channel, once at INFO
    assert caplog.text.count("The TV is playing: {'info': {'playbackState': 'playing', 'rate': 1.0}, "
                             "'type': 'playbackInfo'}") == 1


@pytest.mark.parametrize("cseq", [True, False])
async def test_a_late_answer_is_not_taken_for_the_next_one(monkeypatch, cseq):
    """pyatv's HttpConnection hands each answer to the oldest request still waiting."""
    monkeypatch.setattr(airplay_queue, "REQUEST_TIMEOUT", 0.4)
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(refuse={"isInterestedInDateRange": 501}, late={"isInterestedInDateRange": 0.6},
                       cseq=cseq) as tv:
        await _cast_to(QueueAtv(tv), cast)
        assert tv.overlaps == []  # the next request waited for that late answer
    assert cast.steps["isInterestedInDateRange"] == "no answer"
    assert cast.steps["actionAtItemEnd"] == cast.steps["rate"] == 200  # their own answers, not the late 501


async def test_a_late_keep_alive_is_not_overlapped_and_the_cast_goes_on(monkeypatch):
    """An LG left a /feedback unanswered for over 5 s and played on: the next request waits for it."""
    monkeypatch.setattr(airplay_queue, "FEEDBACK_INTERVAL", 0.05)
    monkeypatch.setattr(airplay_queue, "FEEDBACK_TIMEOUT", 0.3)
    monkeypatch.setattr(airplay_queue, "INFO_INTERVAL", 0.02)
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=PLAYING, late={"feedback": 0.6}) as tv:  # later than FEEDBACK_TIMEOUT
        task = asyncio.ensure_future(_cast_to(QueueAtv(tv), cast, timeout=30))
        deadline = time.monotonic() + 10
        while tv.names(feedback=True).count("feedback") < 4 or tv.names().count("playbackInfo") < 2:
            assert not task.done() and time.monotonic() < deadline, tv.names(feedback=True)
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tv.overlaps == []
        assert tv.names()[-2:] == ["stop", "teardown"]
    assert not cast.queue.ended.is_set()  # the TV was still there

ITEM_PLAYED_TO_END = {"type": "notification", "name": "itemPlayedToEnd"}


@pytest.mark.parametrize("said, why, live", [
    (PLAYING + BACK, "BACK was pressed on the TV's remote", True),
    (PLAYING + ({"type": "playbackState", "name": "stopped"},), "the TV stopped playing", True),
    (PLAYING + (ITEM_PLAYED_TO_END,), "the video ended on the TV", False),  # a video file
    (PLAYING + (HANG_UP,), "the TV closed the event channel", True),
])
async def test_the_cast_ends_when_the_tv_says_so_and_is_stopped_on_it(said, why, live, caplog):
    cast = CastContext(apple=False, live=live, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=said) as tv:
        with caplog.at_level(logging.INFO, logger="mirrorbridge.airplay_queue"):
            await _cast_to(QueueAtv(tv), cast)
        assert tv.names()[-2:] == ["stop", "teardown"]
    assert cast.queue.end_reason == why and f"The cast ended: {why}" in caplog.text


async def test_a_tv_that_ends_the_live_stream_as_a_file_is_explained(caplog):
    """A live stream has no end: a player that reaches one read the playlist once, as a file's."""
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=PLAYING + (ITEM_PLAYED_TO_END,)) as tv:
        with pytest.raises(exceptions.PlaybackError, match=r"The TV ended the live stream after \d+ s, as if it "
                                                           r"were a video file that had reached its end\. Use More "
                                                           r"options > Test this TV"):
            await _cast_to(QueueAtv(tv), cast)
        assert tv.names()[-2:] == ["stop", "teardown"]
    assert "The TV ended the live stream after" in caplog.text


@pytest.mark.parametrize("said, words", [
    ((HANG_UP,), "the TV closed the event channel"),
    ((), None),  # it takes the item and never says anything about it
])
async def test_a_video_file_the_tv_never_plays_is_explained(monkeypatch, said, words):
    """Video files have no cast session watching what the TV fetches."""
    monkeypatch.setattr(airplay_queue, "START_TIMEOUT", 0.2)
    monkeypatch.setattr(airplay_queue, "INFO_INTERVAL", 0.05)
    cast = CastContext(apple=False, live=False, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=said) as tv:
        with pytest.raises(exceptions.PlaybackError) as err:
            await _cast_to(QueueAtv(tv), cast)
        assert tv.names()[-2:] == ["stop", "teardown"]
    if words:
        assert str(err.value) == (f"The TV ended the AirPlay session before it played the video: {words}. Use More "
                                  "options > Test this TV, and send the test's report with your bug report.")
        from mirrorbridge.gui import i18n

        i18n.set_language("es")
        try:
            spanish = i18n.say(err.value.args[0])
        finally:
            i18n.set_language("en")
        assert spanish == ("La TV terminó la sesión de AirPlay antes de reproducir el video: la TV cerró el canal "
                           "de eventos. Usa Más opciones > Probar esta TV y manda el informe de la prueba con tu "
                           "reporte del problema.")
    else:
        assert str(err.value).startswith("The TV took the video but did not say within 0.2 s that it was playing")


async def test_the_clock_for_an_answer_starts_once_the_tv_answered_the_first_setup(monkeypatch):
    """The TV may hold that SETUP while it asks whether to allow MirrorBridge."""
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    assert cast.waited_since(5.0) == 5.0  # /play: from the request
    async with QueueTV(late={"setup": 0.3}) as tv:
        task = asyncio.ensure_future(_cast_to(QueueAtv(tv), cast))
        while cast.queue is None or "setup" not in tv.names():
            await asyncio.sleep(0.01)
        assert cast.waited_since(5.0) is None  # still asking
        await task
    assert cast.waited_since(5.0) == cast.queue.set_up_at > 5.0

async def test_a_stop_before_the_tv_plays_still_ends_its_session():
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=()) as tv:  # it takes the item and says nothing
        task = asyncio.ensure_future(_cast_to(QueueAtv(tv), cast))
        while "rate" not in tv.names():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tv.names()[-2:] == ["stop", "teardown"] and not _queue_tasks()


async def test_the_tvs_messages_are_read_with_or_without_a_content_type():
    said = ((XML, PLAYING[2]), {"type": "notification", "name": "rateChanged", "rate": 1.0}) + BACK
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=said) as tv:
        await _cast_to(QueueAtv(tv), cast)
    assert tv.answers == [(200, "1"), (200, "2"), (200, "3"), (200, "4")]
    assert cast.queue.played.is_set() and "notification/rateChanged" in cast.queue.events


async def test_the_tvs_player_failing_is_explained(caplog):
    from mirrorbridge.gui import i18n

    error = {"type": "error", "error": {"domain": "CoreMediaErrorDomain", "code": -12927}}
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=PLAYING[:2] + (error,)) as tv:
        with pytest.raises(exceptions.PlaybackError) as err:
            await _cast_to(QueueAtv(tv), cast)
        assert tv.names()[-2:] == ["stop", "teardown"]
    assert str(err.value) == ("The TV's player could not play the video (error -12927, CoreMediaErrorDomain). Its "
                              "player does not take this video format. Please report this TV, with mirrorbridge.log.")
    i18n.set_language("es")
    try:
        spanish = i18n.say(err.value.args[0])
    finally:
        i18n.set_language("en")
    assert spanish == ("La TV no pudo reproducir el video: su reproductor dio el error -12927 (CoreMediaErrorDomain). "
                       "Su reproductor no acepta este formato de video. Avisa de esta TV y manda mirrorbridge.log.")


async def test_a_tv_that_stops_answering_ends_the_cast(monkeypatch, caplog):
    monkeypatch.setattr(airplay_queue, "FEEDBACK_INTERVAL", 0.01)
    monkeypatch.setattr(airplay_queue, "FEEDBACK_TIMEOUT", 0.05)
    monkeypatch.setattr(airplay_queue, "CLOSE_TIMEOUT", 0.05)
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=PLAYING, late={"feedback": 30}) as tv:
        with caplog.at_level(logging.INFO, logger="mirrorbridge.airplay_queue"):
            await _cast_to(QueueAtv(tv), cast)
        # Nothing more was sent while the TV owed that answer, the stop and TEARDOWN neither
        assert tv.names(feedback=True)[-1] == "feedback" and tv.names(feedback=True).count("feedback") == 1
        assert tv.overlaps == []
    assert cast.queue.end_reason == "the TV stopped answering (2 keep-alives in a row)"
    assert "Ended the AirPlay video session: stop=no answer teardown=not sent" in caplog.text


async def test_a_tv_that_still_talks_is_not_given_up_for_its_keep_alives(monkeypatch):
    monkeypatch.setattr(airplay_queue, "FEEDBACK_INTERVAL", 0.01)
    monkeypatch.setattr(airplay_queue, "FEEDBACK_TIMEOUT", 0.05)
    monkeypatch.setattr(airplay_queue, "CLOSE_TIMEOUT", 0.05)
    cast = CastContext(apple=False, live=True, device="TCL", features=TCL_FEATURES)
    async with QueueTV(said=PLAYING, late={"feedback": 30}) as tv:
        task = asyncio.ensure_future(_cast_to(QueueAtv(tv), cast))
        while "feedback" not in tv.names(feedback=True):
            await asyncio.sleep(0.01)
        for _ in range(20):  # 0.6 s of keep-alives unanswered, while the TV goes on saying how it plays
            await tv.say({"type": "notification", "name": "rateChanged", "rate": 1.0})
            await asyncio.sleep(0.03)
        assert not task.done()
        await asyncio.wait_for(task, 5)  # once it falls silent too
    assert cast.queue.end_reason == "the TV stopped answering (2 keep-alives in a row)"


@pytest.mark.parametrize("error", [
    airplay_patch.queue_refused(airplay_queue.QueueRefused("setup130", 501), {"setup130": 501}),
    airplay_patch.queue_refused(airplay_queue.QueueRefused("insert", None), {"insert": "no answer"}),
    airplay_patch.queue_refused(airplay_queue.QueueRefused("setup130", None), {"setup130": "no answer"}),
])
def test_play_queue_refusals_are_in_spanish_for_the_gui(error):
    test_every_refusal_is_in_spanish_for_the_gui(error)
    from mirrorbridge import cli
    from types import SimpleNamespace

    assert " > " not in cli._cli_words(str(error), SimpleNamespace(name="Sala familiar"))
