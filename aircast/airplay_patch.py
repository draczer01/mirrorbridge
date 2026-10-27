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
  forever after a cast), retries a "500" from /play by pairing again on a connection
  that is already encrypted, and reports any refusal as AuthenticationError
  ("status code: N"). The player is wrapped so that it cleans up after itself, and
  refusals become ReceiverRefusedVideo with a message a person can act on.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import plistlib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from pyatv import exceptions
from pyatv.protocols.airplay import player as _player
from pyatv.protocols.raop.protocols import airplayv2 as _airplayv2
from pyatv.support.http import HttpResponse, decode_bplist_from_body
from pyatv.support.rtsp import BPLIST_CONTENT_TYPE, USER_AGENT, RtspSession, get_digest_payload

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


@dataclass
class CastContext:
    """What AirCast knows about a cast that pyatv's play_url is not told."""

    apple: Optional[bool] = None  # receiver made by Apple; None = unknown
    live: bool = False  # an endless live stream rather than a file
    accepted_at: Optional[float] = None  # time.monotonic() when the TV accepted the video
    steps: dict = field(default_factory=dict)  # each request's answer, for logs and errors


_CAST: contextvars.ContextVar[Optional[CastContext]] = contextvars.ContextVar("aircast_cast", default=None)


async def play_url(atv, url: str, cast: CastContext) -> None:
    """atv.stream.play_url(url), with `cast` handed to the patched pyatv code.

    Run it as a task of its own: the context variable then reaches the tasks pyatv
    starts for this playback and nothing else.
    """
    _CAST.set(cast)
    await atv.stream.play_url(url)


def _cast() -> CastContext:
    cast = _CAST.get()
    if cast is None:  # a plain atv.stream.play_url (a video file): pyatv's own defaults
        cast = CastContext()
        _CAST.set(cast)
    return cast


class ReceiverRefusedVideo(exceptions.PlaybackError):
    """The receiver turned the video down; `code` is its HTTP status (None: no answer)."""

    def __init__(self, message: str, code: Optional[int] = None, steps: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.steps = dict(steps or {})


_REFUSALS = {
    401: "The TV asks for an AirPlay password (HTTP 401). Check the password in the TV's "
    "AirPlay settings, then use Forget pairing and cast again to enter it.",
    403: "The TV no longer accepts AirCast's pairing (HTTP 403). Use Forget pairing, then "
    "cast again and type the new PIN the TV shows.",
    453: "The TV is busy with another AirPlay sender (HTTP 453). Stop AirPlay on the other "
    "phone, tablet or computer, then try again.",
    470: "AirPlay access is restricted on the TV (HTTP 470). In the TV's AirPlay settings, "
    "allow everyone on the same network, then pair again.",
}


def format_steps(steps: dict) -> str:
    return " ".join(f"{name}={answer}" for name, answer in steps.items()) or "nothing sent"


def refusal(code: Optional[int], steps: dict) -> ReceiverRefusedVideo:
    """A clear error for a TV that said no, instead of pyatv's "status code: N"."""
    if code in _REFUSALS:
        return ReceiverRefusedVideo(_REFUSALS[code], code, steps)
    return ReceiverRefusedVideo(
        f"This TV accepts AirPlay pairing but refused video sent from a URL ({format_steps(steps)}). "
        "It may only take screen mirroring and audio from Apple devices.",
        code, steps,
    )


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
    """AirPlayV2.play_url, tolerant of receivers that skip Apple's optional requests."""
    cast = _cast()
    steps = cast.steps
    try:
        await self._setup_base(timing_server_port)  # pylint: disable=protected-access
    except exceptions.HttpError as ex:  # pair-verify or SETUP refused
        steps["setup"] = ex.status_code
        raise refusal(ex.status_code, steps) from ex
    await self.start_feedback()
    try:
        await self.rtsp.record()
    except exceptions.HttpError as ex:
        steps["record"] = ex.status_code
        raise refusal(ex.status_code, steps) from ex

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
    if not _answered(resp):
        raise refusal(resp.code if resp is not None else None, steps)
    cast.accepted_at = time.monotonic()

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


_pyatv_player_play_url = _player.AirPlayPlayer.play_url


async def _player_play_url(self, url: str, position: float = 0) -> None:
    """AirPlayPlayer.play_url that always tears its session down and names a refusal."""
    try:
        await _pyatv_player_play_url(self, url, position)
    except exceptions.AuthenticationError as ex:
        # pyatv's name both for any 4xx/5xx answer to an AirPlay 1 /play and for a
        # pairing the TV no longer accepts.
        match = re.fullmatch(r"status code: (\d+)", str(ex))
        steps = _cast().steps
        if match:
            steps.setdefault("play", int(match.group(1)))
        raise refusal(int(match.group(1)) if match else 403, steps) from ex
    except exceptions.HttpError as ex:
        if ex.status_code in _REFUSALS:
            raise refusal(ex.status_code, _cast().steps) from ex
        raise
    finally:
        # pyatv never does this: its feedback task would ping the TV every 2 s for as
        # long as AirCast runs, and the event channel would stay open.
        self.stream_protocol.teardown()


async def _wait_for_live_stream(self) -> None:  # replaces AirPlayPlayer._wait_for_media_to_end
    cast = _cast()
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
            raise exceptions.PlaybackError(
                f"Receiver error {err.get('code', '?')} ({err.get('domain', '?')})"
            )

        playing = "duration" in parsed or parsed.get("readyToPlay") or parsed.get("rate", 0) > 0
        if playing:
            if not started:
                _LOGGER.info("The TV started playing the stream")
            started = True
            idle_polls = 0
        elif started:
            idle_polls += 1
            if idle_polls >= STOPPED_POLLS_BEFORE_END:
                _LOGGER.info("Playback stopped on the receiver")
                return
        else:
            waited += 1
            if waited > STARTUP_GRACE_SECONDS:
                raise exceptions.PlaybackError(
                    "The receiver never started playing. Check that Windows Firewall "
                    "allows AirCast and that both devices are on the same network."
                )
        await asyncio.sleep(1)


async def _keep_alive_blind(self) -> None:
    """Hold the AirPlay session open until the receiver drops the connection.

    Nothing is sent here: pyatv's feedback task already pings an AirPlay 2 receiver
    every 2 seconds, and pyatv clears the transport when the connection closes.
    """
    while self.rtsp.connection.transport is not None:
        await asyncio.sleep(BLIND_POLL_SECONDS)
    _LOGGER.info("AirPlay connection closed by receiver")


_applied = False


def apply() -> None:
    global _applied  # pylint: disable=global-statement
    if _applied:
        return
    if not hasattr(_player.AirPlayPlayer, "_wait_for_media_to_end") or not all(
        hasattr(_airplayv2.AirPlayV2, name) for name in ("_setup_base", "start_feedback", "teardown")
    ):
        _LOGGER.warning("pyatv internals changed; AirPlay playback patches not applied")
        return
    _player.AirPlayPlayer._wait_for_media_to_end = _wait_for_live_stream  # pylint: disable=protected-access
    _player.AirPlayPlayer.play_url = _player_play_url
    _airplayv2.AirPlayV2.play_url = _play_url_v2
    _applied = True
