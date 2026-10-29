"""Adapt pyatv's AirPlay video playback to live streams and to non-Apple receivers.

Three pyatv 0.18 behaviours get in the way of casting a screen:

* AirPlayPlayer polls /playback-info and gives up if no "duration" shows up within
  ~5 seconds. A live screen stream can take longer to start and may report no
  duration at all. The replacement wait is patient while starting and only ends when
  the receiver reports an error, the connection drops, or playback stops on the TV.
  Receivers that do not implement /playback-info (or keep failing it) are left to the
  session, which watches the TV's requests for video segments instead.

* AirPlayV2.play_url follows POST /play with PUT /setProperty and POST /rate requests
  that Apple TVs accept but many AirPlay 2 TVs from other brands answer with
  "501 Not Implemented", and pyatv treats that as fatal. The replacement sends the
  same requests, in the same order and with the same protocol line, but only
  POST /play has to succeed. A request the TV says it does not know is asked once
  more over HTTP/1.1, and the two that only trim a video file are left out of a live
  stream to a TV that is not an Apple one.

* pyatv never tears the playback session down (its keep-alive task pings the TV
  forever after a cast, and its timing server's UDP socket stays open unless play_url
  returns normally), retries a "500" from /play by pairing again on a connection that
  is already encrypted, and reports any refusal as AuthenticationError
  ("status code: N") or with its own wording ("no response to SETUP ..."). The player
  is wrapped so that it cleans up after itself, and refusals and failed set-ups become
  ReceiverRefusedVideo with a message a person can act on.

* POST /play is the first AirPlay video API (feature bit 0), which TVs built on Apple's
  AirPlay SDK for TVs do not have: they answer it with 404. A receiver without bit 0 that
  advertises the play queue (bit 33) or AirPlay video v2 (bit 49) is handed the URL through
  the play queue instead (airplay_queue.py), and so, once, on a new connection, is any
  receiver that answers /play with 404. Receivers with bit 0, Apple TVs among them, get
  exactly what pyatv sends. Which way worked for each receiver is remembered while
  MirrorBridge runs; a way that was asked for (Test this TV tries each) is not.

Messages are Texts (text.py) that name MirrorBridge's menu items in English ("Device > Forget
pairing"); the GUI shows them in the user's language and the command line names its own
options instead (gui.i18n.say, cli.py).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import plistlib
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

from pyatv import exceptions
from pyatv.protocols.airplay import player as _player
from pyatv.protocols.raop.protocols import TimingServer
from pyatv.protocols.raop.protocols import airplayv2 as _airplayv2
from pyatv.support.http import HttpResponse, decode_bplist_from_body, http_connect
from pyatv.support.rtsp import BPLIST_CONTENT_TYPE, USER_AGENT, RtspSession, get_digest_payload

from .airplay_queue import QueueOptions, QueueRefused, QueueSession, pyatv_fits
from .text import Text

_LOGGER = logging.getLogger(__name__)

STARTUP_GRACE_SECONDS = 30
STOPPED_POLLS_BEFORE_END = 4
FAILED_POLLS_BEFORE_BLIND = 3  # tvOS 18+ reportedly answers /playback-info with 500 every time
BLIND_POLL_SECONDS = 2
PLAY_RETRY_SECONDS = 1
REQUEST_TIMEOUT = 10  # what HttpConnection waits by default

# "I do not know this request". Such a request is worth sending again over HTTP/1.1;
# any other answer is the TV's real one.
NOT_IMPLEMENTED = (404, 405, 501)

# The ways a video URL is handed over, and the feature bits that say which a receiver has.
PLAY = "play"  # POST /play: AirPlay video v1, what pyatv sends
QUEUE = "queue"  # the play queue: AirPlay video v2 (airplay_queue.py)
ROUTE_NAMES = {PLAY: "POST /play (AirPlay video v1)", QUEUE: "the play queue (AirPlay video v2)"}
VIDEO_V1, PLAY_QUEUE, VIDEO_V2 = 1 << 0, 1 << 33, 1 << 49


@dataclass
class CastContext:
    """What MirrorBridge knows about a cast that pyatv's play_url is not told."""

    apple: Optional[bool] = None  # receiver made by Apple; None = unknown
    live: bool = False  # an endless live stream rather than a file
    password: bool = False  # the receiver advertises an AirPlay password
    accepted_at: Optional[float] = None  # time.monotonic() when the TV accepted the video
    played: bool = False  # the TV said it was playing (/playback-info; the play queue says it in `queue`)
    steps: dict = field(default_factory=dict)  # each request's answer, for logs and errors
    device: str = ""  # the receiver's id: the way that worked for it is remembered under it
    features: Optional[int] = None  # the AirPlay feature bits it advertises; None = none
    route: Optional[str] = None  # PLAY or QUEUE; None = PLAY until play_url chooses (choose_route)
    remember: bool = True  # remember the way that worked; play_url turns it off for a route asked for
    fallback: bool = True  # after a 404 to /play, try the play queue once
    queue: Optional[QueueSession] = None  # the play queue session, on that route

    def waited_since(self, started: float) -> Optional[float]:
        """When the TV's silence counts from, in a cast that asked it to play at `started`:
        then, or on the play queue, once the TV answered its first SETUP. None until it has:
        a TV may hold that SETUP while it asks whether to allow MirrorBridge, and the play
        queue gives it airplay_queue.SETUP_TIMEOUT for that."""
        if self.queue is None:
            return started
        return None if self.queue.set_up_at is None else max(started, self.queue.set_up_at)


_CAST: contextvars.ContextVar[Optional[CastContext]] = contextvars.ContextVar("mirrorbridge_cast", default=None)

# The way each receiver took a video, by its id, for as long as MirrorBridge runs.
_ROUTES: dict[str, str] = {}


def choose_route(cast: CastContext) -> tuple[str, str]:
    """How to hand this receiver the URL (PLAY or QUEUE), and why, for the log."""
    if cast.route:
        return cast.route, "asked for"
    if cast.device in _ROUTES:
        return _ROUTES[cast.device], "it worked with this receiver before"
    features = cast.features
    if features is None:
        return PLAY, "the receiver advertises no features (the play queue follows if /play answers 404)"
    if features & VIDEO_V1:
        return PLAY, "the receiver advertises AirPlay video v1 (feature bit 0)"
    if features & (PLAY_QUEUE | VIDEO_V2):
        return QUEUE, ("the receiver advertises the play queue (bit 33) or AirPlay video v2 (bit 49), "
                       "and not AirPlay video v1 (bit 0)")
    return PLAY, "the receiver advertises neither AirPlay video v1 nor v2"


def _remember(cast: CastContext, route: str) -> None:
    if cast.remember and cast.device and _ROUTES.get(cast.device) != route:
        _LOGGER.info("This receiver takes video by %s; MirrorBridge uses that from now on", ROUTE_NAMES[route])
        _ROUTES[cast.device] = route


async def play_url(atv, url: str, cast: CastContext) -> None:
    """atv.stream.play_url(url), the way that suits the receiver, with `cast` handed to the
    patched pyatv code.

    Run it as a task of its own: the context variable then reaches the tasks pyatv
    starts for this playback and nothing else.
    """
    _CAST.set(cast)
    if cast.route:  # asked for (Test this TV): whether it works says nothing about the receiver's own way
        cast.remember = False
    cast.route, why = choose_route(cast)
    _LOGGER.info("Sending the video by %s: %s", ROUTE_NAMES[cast.route], why)
    await atv.stream.play_url(url)


def _cast() -> CastContext:
    cast = _CAST.get()
    if cast is None:  # a plain atv.stream.play_url (a video file): pyatv's own defaults
        cast = CastContext()
        _CAST.set(cast)
    return cast


class ReceiverRefusedVideo(exceptions.PlaybackError):
    """The receiver turned the video down; `code` is its HTTP status, None when it gave
    none (no answer, or a failure that pyatv reports without one)."""

    def __init__(self, message: str, code: Optional[int] = None, steps: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.steps = dict(steps or {})


_REFUSALS = {
    # pyatv sends no password with a video it plays from a URL, whatever MirrorBridge stores.
    401: Text("tv_password", "The TV asks for an AirPlay password (HTTP 401), which MirrorBridge cannot "
              "send with video it plays from a URL. Turn off the AirPlay password in the TV's AirPlay "
              "settings, then try again."),
    403: Text("tv_pairing_refused", "The TV no longer accepts MirrorBridge's pairing (HTTP 403). Use "
              "Device > Forget pairing, then cast again and type the new PIN the TV shows."),
    453: Text("tv_busy", "The TV is busy with another AirPlay sender (HTTP 453). Stop AirPlay on the "
              "other phone, tablet or computer, then try again."),
    470: Text("tv_restricted", "AirPlay access is restricted on the TV (HTTP 470). In the TV's AirPlay "
              "settings, allow everyone on the same network, then pair again."),
}


def format_steps(steps: dict) -> str:
    return " ".join(f"{name}={answer}" for name, answer in steps.items()) or "nothing sent"


def refusal(code: Optional[int], steps: dict, apple: Optional[bool] = None) -> ReceiverRefusedVideo:
    """A clear error for a TV that said no, instead of pyatv's "status code: N"."""
    if code in _REFUSALS:
        return ReceiverRefusedVideo(_REFUSALS[code], code, steps)
    if code is None:
        text = Text("tv_no_answer", "The TV did not answer the request to play the video ({steps}). "
                    "Check that the PC and the TV are on the same network, then try again.",
                    steps=format_steps(steps))
    elif apple:
        text = Text("apple_tv_failed", "The Apple TV could not start the video ({steps}). Try again, "
                    "or restart the Apple TV.", steps=format_steps(steps))
    else:
        text = Text("tv_no_url_video", "This TV accepts AirPlay pairing but refused video sent from a "
                    "URL ({steps}). It may only take screen mirroring and audio from Apple devices.",
                    steps=format_steps(steps))
    return ReceiverRefusedVideo(text, code, steps)


def credentials_refused(steps: dict, password: bool = False) -> ReceiverRefusedVideo:
    """pyatv says "not authenticated" for a 401 and a 403 alike, and for a failed pair-verify."""
    also = Text("tv_also_password", " The TV also asks for an AirPlay password, which MirrorBridge "
                "cannot send with video it plays from a URL: turn it off in the TV's AirPlay settings.")
    text = Text("tv_credentials", "The TV rejected MirrorBridge's credentials ({steps}). Use Device > "
                "Forget pairing, then cast again and type the new PIN the TV shows.{password}",
                steps=format_steps(steps), password=also if password else "")
    return ReceiverRefusedVideo(text, None, steps)


def setup_failed(steps: dict) -> ReceiverRefusedVideo:
    """The TV stopped answering, hung up or answered nonsense while the session was set up.

    pyatv does not check the TV's last pair-verify answer, so a TV that dropped MirrorBridge's
    pairing still gets an encrypted SETUP, which it cannot read: it never answers.
    """
    if "not paired" in steps.values():
        advice = Text("tv_pair_first", "The TV wants MirrorBridge to pair first: use Device > Pair "
                      "with PIN, then cast again.")
    else:
        advice = Text("tv_pairing_dropped", "Most often the TV has dropped MirrorBridge's pairing: use "
                      "Device > Forget pairing, then cast again and type the new PIN the TV shows.")
    return ReceiverRefusedVideo(
        Text("tv_setup_failed", "The TV did not finish setting up the AirPlay session ({steps}). {advice}",
             steps=format_steps(steps), advice=advice),
        None, steps,
    )


def queue_refused(ex: QueueRefused, steps: dict, apple: Optional[bool] = None) -> ReceiverRefusedVideo:
    """A clear error for a TV that turned down a step of the play queue."""
    if ex.code is None:
        # Silence while the session is set up is most often a dropped pairing; after that,
        # the TV did not answer the video itself.
        return setup_failed(steps) if ex.step in ("setup", "record", "setup130") else refusal(None, steps)
    if ex.code in _REFUSALS:
        return refusal(ex.code, steps)
    if apple:
        return refusal(ex.code, steps, apple)
    return ReceiverRefusedVideo(
        Text("tv_queue_refused", "The TV turned down the video sent from a URL ({steps}). Use More options > "
             "Test this TV to see which way of sending video it takes, and send the test's report with your bug "
             "report.", steps=format_steps(steps)),
        ex.code, steps,
    )


# What pair-verify, SETUP, the event channel and RECORD raise when the TV does not
# play along. NotSupportedError (a RuntimeError) comes from pyatv's "no credentials"
# verifier, which has no keys for the event channel.
_SESSION_FAILURES = (
    TimeoutError, OSError, RuntimeError, ValueError, KeyError,
    exceptions.ConnectionLostError, exceptions.InvalidResponseError,
)
_SESSION_ERRORS = (exceptions.HttpError, exceptions.AuthenticationError, *_SESSION_FAILURES)


def _failure(ex: BaseException) -> str:
    """A step's answer, for a step that raised instead of being answered."""
    if isinstance(ex, TimeoutError):
        return "no answer"
    if isinstance(ex, ConnectionRefusedError):
        return "connection refused"
    if isinstance(ex, exceptions.NotSupportedError):
        return "not paired"
    if isinstance(ex, (exceptions.ConnectionLostError, ConnectionError, RuntimeError)):
        return "connection lost"
    if isinstance(ex, OSError):
        return "connection failed"
    return "unreadable answer"


def _session_error(step: str, ex: BaseException, cast: CastContext) -> ReceiverRefusedVideo:
    """What setting up the session raised, as an error a person can act on."""
    steps = cast.steps
    if isinstance(ex, exceptions.HttpError):  # refused, with a status code
        steps[step] = ex.status_code
        return refusal(ex.status_code, steps, cast.apple)
    if isinstance(ex, exceptions.AuthenticationError):
        steps[step] = "not authenticated"
        return credentials_refused(steps, cast.password)
    _LOGGER.info("AirPlay %s failed: %r", step, ex)
    steps[step] = _failure(ex)
    return setup_failed(steps)


def _code(resp: Optional[HttpResponse]) -> object:
    return resp.code if resp is not None else "no answer"


def _answered(resp: Optional[HttpResponse]) -> bool:
    return resp is not None and 200 <= resp.code < 300


async def _send(
    rtsp: RtspSession, method: str, uri: str, body: Optional[dict] = None, protocol: str = "RTSP/1.0"
) -> Optional[HttpResponse]:
    """Send one request with exactly the headers rtsp.exchange() would add; never raise.

    exchange() then waits up to 4 s for a reply that carries the same CSeq, so a TV
    whose error replies leave CSeq out would cost 4 s and a TimeoutError per request.
    The connection itself pairs replies with requests in order, which is all one
    request needs. Returns None if the TV did not answer.
    """
    cseq = rtsp.cseq
    rtsp.cseq += 1
    headers: dict = {
        "CSeq": cseq,
        "DACP-ID": rtsp.dacp_id,
        "Active-Remote": rtsp.active_remote,
        "Client-Instance": rtsp.dacp_id,
    }
    if rtsp.digest_info:
        headers["Authorization"] = get_digest_payload(method, uri, *rtsp.digest_info)
    data = None
    if body is not None:
        headers["Content-Type"] = BPLIST_CONTENT_TYPE
        data = plistlib.dumps(body, fmt=plistlib.FMT_BINARY)  # pylint: disable=no-member
    try:
        return await rtsp.connection.send_and_receive(
            method, uri, protocol=protocol, user_agent=USER_AGENT, headers=headers, body=data,
            allow_error=True, timeout=REQUEST_TIMEOUT,
        )
    except exceptions.AuthenticationError:  # a 403 raises even with allow_error
        return HttpResponse(*protocol.split("/"), 403, "Forbidden", {}, b"")
    except (TimeoutError, RuntimeError, exceptions.ConnectionLostError) as ex:
        _LOGGER.debug("%s %s %s: %s", method, uri, protocol, ex)
        return None


async def _optional(self, name: str, method: str, uri: str, body: Optional[dict] = None) -> None:
    """Send a request the TV may not implement and note its answer under `name`.

    It goes out as pyatv sends it (RTSP/1.0, what Apple TVs expect). If the TV says it
    does not know the request, it is asked once more over HTTP/1.1. Whatever the TV
    answers, the cast goes on.
    """
    steps = _cast().steps
    resp = await _send(self.rtsp, method, uri, body)
    steps[name] = _code(resp)
    if resp is not None and resp.code in NOT_IMPLEMENTED:
        retry = await _send(self.rtsp, method, uri, body, protocol="HTTP/1.1")
        steps[name] = f"{resp.code}/HTTP:{_code(retry)}"


def _body_text(resp: HttpResponse) -> str:
    if not resp.body:
        return ""
    try:
        return f": {decode_bplist_from_body(resp)}"
    except Exception:  # pylint: disable=broad-except
        return f": {resp.body[:200]!r}"


async def _post_play(self, body: dict) -> Optional[HttpResponse]:
    """POST /play exactly as pyatv sends it. None if the TV did not answer."""
    try:
        resp = await self.rtsp.connection.post(
            "/play",
            headers=_airplayv2.HEADERS,
            body=plistlib.dumps(body, fmt=plistlib.FMT_BINARY),  # pylint: disable=no-member
            allow_error=True,
        )
    except exceptions.AuthenticationError:  # a 403 raises even with allow_error
        resp = HttpResponse("HTTP", "1.1", 403, "Forbidden", {}, b"")
    except (TimeoutError, RuntimeError, exceptions.ConnectionLostError) as ex:
        _LOGGER.info("Receiver did not answer /play: %s", ex)
        return None
    _LOGGER.info("Receiver answered /play with HTTP %s %s%s", resp.code, resp.message, _body_text(resp))
    return resp


async def _play_url_v2(self, timing_server_port: int, url: str, position: float = 0.0):
    """AirPlayV2.play_url, tolerant of receivers that skip Apple's optional requests, and by
    the play queue where the receiver has no /play."""
    cast = _cast()
    if cast.route == QUEUE:
        return await _play_queue(self, self.rtsp, timing_server_port, url, position)
    steps = cast.steps
    try:
        await self._setup_base(timing_server_port)  # pair-verify, SETUP, event channel
    except _SESSION_ERRORS as ex:
        raise _session_error("setup", ex, cast) from ex
    await self.start_feedback()
    try:
        await self.rtsp.record()
    except _SESSION_ERRORS as ex:
        raise _session_error("record", ex, cast) from ex

    body = {
        "Content-Location": url,
        "Start-Position-Seconds": position,
        "uuid": self.uuid,
        "streamType": 1,
        "mediaType": "file",
        "mightSupportStorePastisKeyRequests": True,
        "playbackRestrictions": 0,
        "secureConnectionMs": 22,
        "volume": 1.0,
        "infoMs": 122,
        "connectMs": 18,
        "authMs": 0,
        "bonjourMs": 0,
        "referenceRestrictions": 3,
        "SenderMACAddress": "AA:BB:CC:DD:EE:FF",
        "model": "iPhone14,3",
        "postAuthMs": 0,
        "clientBundleID": "dev.pyatv.GPU",
        "clientProcName": "dev.pyatv.GPU",
        "osBuildVersion": "20G1116",
        "rate": 1.0,
    }
    resp = await _post_play(self, body)
    steps["play"] = _code(resp)
    if resp is not None and resp.code == 500:
        # AirPlayPlayer would retry "Internal Server Error" by running all of the above
        # again on this connection, which is already encrypted. Ask for /play once more.
        await asyncio.sleep(PLAY_RETRY_SECONDS)
        resp = await _post_play(self, body)
        steps["play"] = f"500/{_code(resp)}"
    if resp is not None and resp.code == 404 and cast.fallback:
        return await _fall_back_to_queue(self, timing_server_port, url, position)
    if not _answered(resp):
        raise refusal(resp.code if resp is not None else None, steps, cast.apple)
    cast.accepted_at = time.monotonic()
    _remember(cast, PLAY)

    # Apple TVs want these; other receivers often answer 501 Not Implemented. Only
    # /rate matters (playback may start paused without it), and only where supported.
    await _optional(self, "isInterestedInDateRange", "PUT", "/setProperty?isInterestedInDateRange", {"value": True})
    await _optional(self, "actionAtItemEnd", "PUT", "/setProperty?actionAtItemEnd", {"value": 0})
    await _optional(self, "rate", "POST", "/rate?value=1.000000")
    if cast.live and cast.apple is False:
        # Where a video file ends: a live stream has no end to trim, and these are the
        # requests other brands' TVs refuse. Apple TVs still get them, as always.
        steps["forwardEndTime"] = steps["reverseEndTime"] = "skipped"
    else:
        end_time = {"value": {"flags": 0, "value": 0, "epoch": 0, "timescale": 0}}
        await _optional(self, "forwardEndTime", "PUT", "/setProperty?forwardEndTime", end_time)
        await _optional(self, "reverseEndTime", "PUT", "/setProperty?reverseEndTime", end_time)
    _LOGGER.info("AirPlay video requests: %s", format_steps(steps))
    return resp


async def _play_queue(self, rtsp: RtspSession, timing_server_port: int, url: str, position: float) -> HttpResponse:
    """Hand the URL over through the play queue on `rtsp`, and answer as POST /play would.

    The player then waits in _wait_for_live_stream, and its play_url closes the session.
    Requests go out one at a time, so pyatv's own keep-alive is never started.
    """
    cast = _cast()
    cast.route = QUEUE
    # A live stream gets no Start-Position: 0 would start the TV at the oldest segment it can
    # still fetch, seconds further behind than its player's own choice.
    options = QueueOptions(apple=bool(cast.apple), start=None if cast.live else position, live=cast.live)
    queue = cast.queue = QueueSession(rtsp, self.context.credentials, cast.steps, options)
    try:
        await queue.open(timing_server_port)
        await queue.load(url)
    except QueueRefused as ex:
        _LOGGER.info("AirPlay video requests: %s", format_steps(cast.steps))
        raise queue_refused(ex, cast.steps, cast.apple) from ex
    except _SESSION_ERRORS as ex:
        raise _session_error(queue.step or "setup", ex, cast) from ex
    cast.accepted_at = time.monotonic()
    _remember(cast, QUEUE)
    _LOGGER.info("AirPlay video requests: %s", format_steps(cast.steps))
    return HttpResponse("HTTP", "1.1", 200, "OK", {}, b"")


async def _fall_back_to_queue(self, timing_server_port: int, url: str, position: float) -> HttpResponse:
    """/play answered 404: try the play queue, once, on a connection of its own."""
    connection = self.rtsp.connection
    if connection.transport is None:  # the TV hung up with its 404: nowhere to connect again
        cast = _cast()
        raise refusal(404, cast.steps, cast.apple)
    address = connection.remote_ip
    port = connection.transport.get_extra_info("peername")[1]
    _LOGGER.info("The receiver has no POST /play (HTTP 404); trying %s on a new connection", ROUTE_NAMES[QUEUE])
    self.teardown()  # pyatv's keep-alive and event channel
    connection.close()  # the TV ends the session that came with it
    try:
        fresh = await http_connect(address, port)
    except OSError as ex:
        raise _session_error("connect", ex, _cast()) from ex
    return await _play_queue(self, RtspSession(fresh), timing_server_port, url, position)


_pyatv_player_play_url = _player.AirPlayPlayer.play_url


async def _player_play_url(self, url: str, position: float = 0) -> None:
    """AirPlayPlayer.play_url that always tears its session down and names a refusal."""
    cast = _cast()
    try:
        await _pyatv_player_play_url(self, url, position)
    except exceptions.AuthenticationError as ex:
        # pyatv's name both for any 4xx/5xx answer to an AirPlay 1 /play and for
        # credentials the TV turned down (a 401 or a 403 alike, or a failed pair-verify).
        match = re.fullmatch(r"status code: (\d+)", str(ex))
        if match is None:
            cast.steps.setdefault("pair-verify", "not authenticated")
            raise credentials_refused(cast.steps, cast.password) from ex
        cast.steps.setdefault("play", int(match.group(1)))
        raise refusal(int(match.group(1)), cast.steps, cast.apple) from ex
    except exceptions.HttpError as ex:
        if ex.status_code in _REFUSALS:
            raise refusal(ex.status_code, cast.steps, cast.apple) from ex
        raise
    except exceptions.PlaybackError as ex:
        if str(ex) != "Max retries exceeded":
            raise
        # An AirPlay 1 receiver answered every /play with 500 (AirPlay 2 ones never
        # get here: _play_url_v2 asks again itself, then refuses).
        cast.steps.setdefault("play", "/".join(["500"] * _player.PLAY_RETRIES))
        raise refusal(500, cast.steps, cast.apple) from ex
    finally:
        # pyatv never does this: its feedback task would ping the TV every 2 s for as
        # long as MirrorBridge runs, and the event channel would stay open.
        self.stream_protocol.teardown()
        if cast.queue is not None:
            # Stop the video on the TV and end its session, also when the cast is stopped
            # (cancelled): in a task of its own, so a second cancel does not cut it short.
            await asyncio.shield(asyncio.ensure_future(cast.queue.close()))


async def _wait_for_live_stream(self) -> None:  # replaces AirPlayPlayer._wait_for_media_to_end
    cast = _cast()
    if cast.queue is not None:
        # The TV says on the event channel when the video ends; /playback-info is not there.
        await cast.queue.wait()
        return
    if cast.accepted_at is None:  # AirPlay 1: the player only gets here once /play succeeded
        cast.accepted_at = time.monotonic()
    started = False
    waited = 0
    idle_polls = 0
    failures = 0
    while True:
        try:
            resp = await self.rtsp.connection.get("/playback-info", allow_error=True)
            status = resp.code
        except (RuntimeError, exceptions.ConnectionLostError):
            _LOGGER.info("AirPlay connection closed by receiver")
            return
        except exceptions.AuthenticationError:  # a 403 raises even with allow_error
            resp, status = None, 403
        except (TimeoutError, exceptions.HttpError) as ex:
            resp, status = None, str(ex)

        if resp is not None and resp.code in NOT_IMPLEMENTED:
            # The TV does not report playback state. Keep the session open; the cast
            # session notices the end when the TV stops fetching video segments.
            _LOGGER.info(
                "The receiver does not report playback status (HTTP %s); "
                "watching its video requests instead", status,
            )
            await _keep_alive_blind(self)
            return
        if not _answered(resp):
            failures += 1
            if failures >= FAILED_POLLS_BEFORE_BLIND:
                _LOGGER.info(
                    "The receiver keeps failing to report playback status (%s); "
                    "watching its video requests instead", status,
                )
                await _keep_alive_blind(self)
                return
            await asyncio.sleep(1)
            continue
        failures = 0

        try:
            parsed = decode_bplist_from_body(resp) if resp.body else None
        except Exception:  # pylint: disable=broad-except
            parsed = None
        if not isinstance(parsed, dict):
            # An empty (or unreadable) answer says nothing either way: neither count it
            # as playing nor as stopped.
            await asyncio.sleep(1)
            continue

        if "error" in parsed:
            err = parsed["error"]
            raise exceptions.PlaybackError(Text(
                "tv_error", "Receiver error {code} ({domain})", code=err.get("code", "?"),
                domain=err.get("domain", "?"),
            ))

        playing = "duration" in parsed or parsed.get("readyToPlay") or parsed.get("rate", 0) > 0
        if playing:
            if not started:
                _LOGGER.info("The TV started playing the stream")
            started = cast.played = True
            idle_polls = 0
        elif started:
            idle_polls += 1
            if idle_polls >= STOPPED_POLLS_BEFORE_END:
                _LOGGER.info("Playback stopped on the receiver")
                return
        else:
            waited += 1
            if waited > STARTUP_GRACE_SECONDS:
                raise exceptions.PlaybackError(Text(
                    "tv_never_played", "The receiver never started playing. Check that Windows "
                    "Firewall allows MirrorBridge and that both devices are on the same network."
                ))
        await asyncio.sleep(1)


async def _keep_alive_blind(self) -> None:
    """Hold the AirPlay session open until the receiver drops the connection.

    Nothing is sent here: pyatv's feedback task already pings an AirPlay 2 receiver
    every 2 seconds, and pyatv clears the transport when the connection closes.
    """
    while self.rtsp.connection.transport is not None:
        await asyncio.sleep(BLIND_POLL_SECONDS)
    _LOGGER.info("AirPlay connection closed by receiver")


@asynccontextmanager
async def _timing_server(rtsp: RtspSession):  # replaces player.timing_server
    """pyatv's timing server, closed however play_url ends.

    pyatv closes it only when play_url returns, so every cast that is stopped (play_url
    is cancelled) or refused would leave a UDP socket open for as long as MirrorBridge runs.
    """
    _, server = await asyncio.get_running_loop().create_datagram_endpoint(
        TimingServer, local_addr=(rtsp.connection.local_ip, 0)
    )
    try:
        yield server
    finally:
        server.close()


_applied = False


def apply() -> None:
    global _applied  # pylint: disable=global-statement
    if _applied:
        return
    if not (
        hasattr(_player, "timing_server")
        and hasattr(_player.AirPlayPlayer, "_wait_for_media_to_end")
        and all(hasattr(_airplayv2.AirPlayV2, name) for name in ("_setup_base", "start_feedback", "teardown"))
        and pyatv_fits()
    ):
        _LOGGER.warning("pyatv internals changed; AirPlay playback patches not applied")
        return
    _player.timing_server = _timing_server
    _player.AirPlayPlayer._wait_for_media_to_end = _wait_for_live_stream  # pylint: disable=protected-access
    _player.AirPlayPlayer.play_url = _player_play_url
    _airplayv2.AirPlayV2.play_url = _play_url_v2
    _applied = True
