"""AirPlay video sent from a URL to TVs that only have the play queue ("AirPlay video v2").

POST /play, which pyatv sends and Apple TVs take, belongs to Apple's first AirPlay video API
(feature bit 0). TVs built on Apple's AirPlay SDK for TVs (LG, Samsung, TCL...) advertise
video v2 (bit 49) and the play queue (bit 33) instead, and answer /play with 404. They are
handed the URL on the same encrypted RTSP connection, in this order:

1. pair-verify, then a SETUP that starts the session and names the event port;
2. the event channel: an encrypted connection to that port, on which the TV reports playback;
3. RECORD (without it, an LG TV plays but never says so);
4. a second SETUP, of a type-130 remote control stream, whose answer gives a stream ID;
5. POST /command HTTP/1.1 for that stream: insertPlayQueueItem with the URL, two
   setProperty, and setRate 1. Each command is a binary plist {params: {data: <bplist>}}.

A TV drops the connection when two requests overlap on it (pyatv's keep-alive next to a
/command, for one), so here every request, the keep-alive included, waits for the answer to
the one before, also when that request gave up waiting for it: the next one is only sent
once that late answer has come. Answers are matched to their requests by CSeq: pyatv's
HttpConnection gives each answer to the oldest request still waiting, so one answer that
came after its request gave up would be taken for the answer to every request after it.

The TV's messages on the event channel (playbackState, notification, sendMediaRemoteCommand,
errors, and answers to playbackInfo) come framed like requests and are each answered 200 OK.

What is sent here follows the public notes of airkast (Apache-2.0), which plays URLs this way
on an LG CX, and of the open pyatv pull requests #2846 and #2899 (MIT): the requests, their
order, the plist keys and the identifiers, not their code.
"""

from __future__ import annotations

import asyncio
import logging
import plistlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from pyatv import exceptions
from pyatv.auth.hap_channel import setup_channel
from pyatv.auth.hap_pairing import HapCredentials
from pyatv.protocols.airplay.auth import verify_connection
from pyatv.protocols.airplay.channels import EventChannel
from pyatv.protocols.raop.protocols.airplayv2 import EVENTS_READ_INFO, EVENTS_SALT, EVENTS_WRITE_INFO
from pyatv.support.http import HttpConnection, HttpRequest, HttpResponse, parse_response
from pyatv.support.rtsp import BPLIST_CONTENT_TYPE, USER_AGENT, RtspSession

from . import APP_NAME
from .text import Text

_LOGGER = logging.getLogger(__name__)

# The remote control stream that takes /command (SETUP type 130, controlType 1). pyatv's own
# type-130 stream, for its remote control, is another one (controlType 2, its own socket).
STREAM_TYPE_RCS = 130
RCS_CLIENT_TYPE = "A6B27562-B43A-4F2D-B75F-82391E250194"
COMMAND_USER_AGENT = "AirPlay/870.14.1"  # what Apple's senders send /command with
SENDER_ID = "AA:BB:CC:DD:EE:FF"  # the deviceID pyatv sends in every SETUP, which these TVs take

REQUEST_TIMEOUT = 10  # what pyatv's HttpConnection waits by default
# Some receivers hold the first SETUP while they ask whether to allow a new sender. A cast's
# own "the TV never answered" clock only starts once this SETUP is answered (CastContext).
SETUP_TIMEOUT = 60
FEEDBACK_INTERVAL = 2.0  # pyatv's keep-alive: POST /feedback every 2 s
FEEDBACK_TIMEOUT = 5
FEEDBACK_MISSES = 2  # unanswered keep-alives in a row, with the TV silent meanwhile, that end a cast
CLOSE_TIMEOUT = 3  # each request of a stop: a TV that does not answer must not hold it up
INFO_INTERVAL = 10  # asks the TV what it is playing, for the log
INFO_TIMEOUT = 3
START_TIMEOUT = 30  # a video file the TV took must be said to play within this (a live stream: the cast session)
EVENT_CHANNEL_TRIES = 5  # pyatv's: some receivers open the event port a moment after SETUP
LOGGED_DETAIL = 400  # characters of a message from the TV that the log shows at INFO

# What tvOS wants in a play queue item and other TVs do without (an LG CX plays without any).
# playerLoggingID must be at most 6 characters: tvOS drops the connection otherwise.
_TVOS_ITEM_KEYS = {
    "playbackRestrictions": 0,
    "referenceRestrictions": 2,
    "supportsIntegratedTimeline": False,
    "snapTimeToPausePlayback": False,
    "clientBundleID": "app.mirrorbridge",
    "clientProcName": APP_NAME,
    "playerLoggingID": "MB",
    "playerItemLoggingID": "MB.1",
}

# CoreMedia errors a TV's player reports, with what to try.
_MEDIA_HINTS = {
    -17221: Text("tv_media_network", "Its player could not download the video: allow MirrorBridge in Windows "
                 "Firewall on private networks, and check that the PC and the TV are on the same network."),
    -12927: Text("tv_media_format", "Its player does not take this video format. Please report this TV, with "
                 "mirrorbridge.log."),
    -12911: Text("tv_media_decoder", "Its player could not decode the video. Try a lower resolution or 30 "
                 "frames per second."),
}

# Why a cast ended on the TV's side, for the log and for errors.
END_STOPPED = Text("queue_end_stopped", "the TV stopped playing")
END_PLAYED = Text("queue_end_played", "the video ended on the TV")
END_FAILED = Text("queue_end_failed", "the TV's player failed")
END_BACK = Text("queue_end_back", "BACK was pressed on the TV's remote")
END_EVENTS_CLOSED = Text("queue_end_events_closed", "the TV closed the event channel")
END_HUNG_UP = Text("queue_end_hung_up", "the TV closed the connection")


def pyatv_fits() -> bool:
    """pyatv still has the parts this module takes over (those of pyatv 0.18)."""
    return all(hasattr(HttpConnection, name) for name in ("data_received", "connection_lost", "close")) and all(
        hasattr(EventChannel, name) for name in ("handle_received", "parse_request", "format_response", "send")
    )


def bplist(value: Any) -> bytes:
    return plistlib.dumps(value, fmt=plistlib.FMT_BINARY)  # pylint: disable=no-member


def unwrap(body: Any) -> Optional[dict]:
    """The message in a body: the plist inside {params: {data: <bplist>}}, or the body's own
    plist when it has no such wrapper. None when the body holds neither."""
    if isinstance(body, str):  # pyatv hands over a body it could read as text as a str
        body = body.encode("utf-8")
    if not isinstance(body, bytes) or not body:
        return None
    try:
        outer = plistlib.loads(body)
    except Exception:  # pylint: disable=broad-except
        return None
    if not isinstance(outer, dict):
        return None
    params = outer.get("params")
    data = params.get("data") if isinstance(params, dict) else None
    if not isinstance(data, bytes):
        return outer
    try:
        inner = plistlib.loads(data)
    except Exception:  # pylint: disable=broad-except
        return None
    return inner if isinstance(inner, dict) else None


def cm_time(seconds: float) -> dict:
    """A CMTime, which Start-Position must be (an LG ignores Start-Position-Seconds)."""
    return {"value": int(round(seconds * 1000)), "timescale": 1000, "flags": 1, "epoch": 0}


def media_error(error: Any, host: str = "") -> Text:
    """What a TV's {type: error} message says, in words a person can act on. `host`: where
    the video came from, when that is not this PC (the firewall then has nothing to do with it)."""
    error = error if isinstance(error, dict) else {}
    code = error.get("code", "?")
    hint = _MEDIA_HINTS.get(code) or Text("tv_media_report", "Please report this TV, with mirrorbridge.log.")
    if code == -17221 and host:
        hint = Text("tv_media_unreachable", "Its player could not download the video from {host}.", host=host)
    return Text("tv_media_error", "The TV's player could not play the video (error {code}, {domain}). {hint}",
                code=code, domain=error.get("domain", "?"), hint=hint)


def with_probe_advice(problem: Text) -> Text:
    """`problem`, and what to do about it: Test this TV, whose report shows `problem` alone."""
    return Text("tv_probe_advice", "{problem} Use More options > Test this TV, and send the test's report with your "
                "bug report.", problem=problem)


def _answered(answer: Optional[HttpResponse]) -> bool:
    return answer is not None and 200 <= answer.code < 300


def _plist(answer: HttpResponse) -> dict:
    return unwrap(answer.body) or {}


def _message(method: str, uri: str, protocol: str, user_agent: str, headers: dict, body: bytes) -> bytes:
    """A request as pyatv writes one: User-Agent, Content-Length, then the other headers."""
    lines = [f"{method} {uri} {protocol}", f"User-Agent: {user_agent}"]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    lines += [f"{name}: {value}" for name, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


class QueueRefused(Exception):
    """The TV turned down a step of the play queue (`code`: its HTTP status), or never answered
    it (None). The step's answer, in words, is in the session's steps."""

    def __init__(self, step: str, code: Optional[int]) -> None:
        super().__init__(f"{step}: {code if code is not None else 'no answer'}")
        self.step = step
        self.code = code


class SerialSession:
    """An RtspSession's requests, sent one at a time, each answer matched to its request.

    It takes over the answers of the session's (pair-verified) HttpConnection. A request that
    gave up waiting stays owed: the next request is only sent once its answer has come, and
    that answer is dropped (airkast's ControlConnection does the same). While it has not come,
    no request is sent at all.
    """

    def __init__(self, rtsp: RtspSession) -> None:
        self.rtsp = rtsp
        self.connection = rtsp.connection
        self.closed = asyncio.Event()  # the connection is gone
        self.answers = 0  # every answer the TV sent, late ones included: it is still there
        self._lock = asyncio.Lock()
        self._waiting: "OrderedDict[int, asyncio.Future]" = OrderedDict()  # by CSeq, oldest first
        self._owed: set[int] = set()  # those of them whose request gave up waiting
        self._buffer = b""
        lost = self.connection.connection_lost

        def connection_lost(exc: Optional[Exception]) -> None:
            self._lost()
            lost(exc)

        self.connection.data_received = self._data_received
        self.connection.connection_lost = connection_lost
        if self.connection.transport is None:
            self.closed.set()

    async def request(
        self,
        method: str,
        uri: str,
        body: Optional[Any] = None,
        *,
        protocol: str = "RTSP/1.0",
        user_agent: str = USER_AGENT,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Optional[HttpResponse]:
        """Send a request with the headers pyatv's RtspSession adds, and return its answer.

        None if the TV did not answer within `timeout` (REQUEST_TIMEOUT), still had not
        answered an earlier request within `timeout` (this one is then not sent), or the
        connection closed. Never raises for any of these. A body that is not bytes goes as a
        binary plist.
        """
        timeout = REQUEST_TIMEOUT if timeout is None else timeout
        async with self._lock:
            if not await self._settle(timeout):
                if not self.closed.is_set():
                    _LOGGER.debug("Not sending %s %s: the TV has still not answered CSeq %s", method, uri,
                                  ", ".join(map(str, sorted(self._owed))))
                return None
            transport = self.connection.transport
            if transport is None or self.closed.is_set():
                return None
            rtsp = self.rtsp
            cseq = rtsp.cseq
            rtsp.cseq += 1
            fields: dict = {
                "CSeq": cseq,
                "DACP-ID": rtsp.dacp_id,
                "Active-Remote": rtsp.active_remote,
                "Client-Instance": rtsp.dacp_id,
                # A receiver that asks whether to allow a new sender names it from this, not from SETUP's name.
                "X-Apple-Client-Name": APP_NAME,
                **(headers or {}),
            }
            data = b""
            if body is not None:
                data = body if isinstance(body, bytes) else bplist(body)
                fields["Content-Type"] = BPLIST_CONTENT_TYPE
            answer = asyncio.get_running_loop().create_future()
            self._waiting[cseq] = answer
            _LOGGER.debug("Sending %s %s %s (CSeq %s)", method, uri, protocol, cseq)
            transport.write(self.connection.send_processor(_message(method, uri, protocol, user_agent, fields, data)))
            try:
                return await asyncio.wait_for(asyncio.shield(answer), timeout)
            except (asyncio.TimeoutError, ConnectionError):
                _LOGGER.debug("No answer to %s %s (CSeq %s)", method, uri, cseq)
                return None
            finally:
                if not answer.done() and cseq in self._waiting:
                    self._owed.add(cseq)  # the next request waits for its answer, which is then dropped

    @property
    def owes(self) -> bool:
        """The TV has still not answered a request that gave up waiting."""
        return bool(self._owed)

    async def _settle(self, timeout: float) -> bool:
        """Wait up to `timeout` for the answers the TV still owes. False if one is still owed
        then, or the connection closed meanwhile: the next request must not be sent."""
        owed = [self._waiting[cseq] for cseq in self._owed if cseq in self._waiting]
        if owed:
            _LOGGER.debug("Waiting for the late answer to CSeq %s", ", ".join(map(str, sorted(self._owed))))
            await asyncio.wait(owed, timeout=timeout)
        return not self._owed and not self.closed.is_set()

    def _data_received(self, data: bytes) -> None:
        self._buffer += self.connection.receive_processor(data)
        while self._buffer:
            try:
                answer, self._buffer = parse_response(self._buffer)
            except ValueError as ex:  # not an answer at all
                _LOGGER.debug("Unreadable answer from the receiver: %s", ex)
                self._buffer = b""
                return
            if answer is None:  # the rest is still on its way
                return
            self._deliver(answer)

    def _deliver(self, answer: HttpResponse) -> None:
        self.answers += 1
        cseq = str(answer.headers.get("CSeq", "")).strip()
        if cseq.isdigit() and int(cseq) in self._waiting:
            key = int(cseq)
        elif self._waiting:  # no CSeq, or one never sent: answers come in order, so the oldest request's
            key = next(iter(self._waiting))
        else:
            _LOGGER.debug("Answer to no request: %s %s", answer.code, answer.message)
            return
        future = self._waiting.pop(key)
        if key in self._owed:
            self._owed.discard(key)
            _LOGGER.debug("Dropped the late answer to CSeq %s: %s %s", key, answer.code, answer.message)
        if not future.done():
            future.set_result(answer)

    def _lost(self) -> None:
        self.closed.set()
        for cseq, future in self._waiting.items():
            if future.done():
                continue
            if cseq in self._owed:
                future.cancel()  # nobody waits for its answer but _settle
            else:
                future.set_exception(ConnectionError("connection lost"))
        self._waiting.clear()
        self._owed.clear()


class QueueEventChannel(EventChannel):
    """pyatv's event channel, which answers every message the TV sends on it, reading them too."""

    def __init__(
        self,
        output_key: bytes,
        input_key: bytes,
        on_message: Callable[[dict], None],
        on_closed: Callable[[], None],
    ) -> None:
        super().__init__(output_key, input_key)
        self.on_message = on_message
        self.on_closed = on_closed

    def handle_received(self) -> None:
        while self.buffer:
            try:
                request, _, self.buffer = self.parse_request(self.buffer)
            except ValueError:
                _LOGGER.debug("Unreadable message on the event channel: %r", self.buffer[:100])
                self.buffer = b""
                return
            if request is None:  # the rest is still on its way
                return
            self._answer(request)
            message = unwrap(request.body)
            if message is None:
                _LOGGER.debug("Event channel: %s %s without a message", request.method, request.path)
                continue
            try:
                self.on_message(message)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Failed to handle a message from the TV: %s", message)

    def _answer(self, request: HttpRequest) -> None:
        """200 OK, as pyatv's EventChannel answers: the TV waits for it before it says more."""
        headers = {"Content-Length": "0", "Audio-Latency": "0"}
        for name in ("Server", "CSeq"):
            if name in request.headers:
                headers[name] = request.headers[name]
        self.send(self.format_response(HttpResponse(request.protocol, request.version, 200, "OK", headers, b"")))

    def connection_lost(self, exc: Optional[Exception]) -> None:
        super().connection_lost(exc)
        self.transport = None
        self.on_closed()


@dataclass
class QueueOptions:
    """How one receiver is handed the URL."""

    apple: bool = False  # an Apple TV (tvOS 26): PTP timing, its /info psi, and _TVOS_ITEM_KEYS
    media_type: str = "file"  # "file" or "streaming": an LG CX plays HLS sent as either
    start: Optional[float] = 0.0  # seconds into the video; None sends no Start-Position (live: the TV's own start)
    live: bool = False  # MirrorBridge's live stream: it has no end, and the cast session watches it start


class QueueSession:
    """One video played through the play queue, on the RTSP session pyatv opened for it.

    `steps` gets each request's answer by name (setup, record, setup130, insert...), for the
    log and for errors. open() and load() raise QueueRefused when the TV turns a step down,
    and what pyatv raises when pair-verify or the event channel fail: `step` then names the
    step under way. wait() returns when the video ends on the TV (or raises PlaybackError
    when it failed), and close() stops it.
    """

    def __init__(
        self,
        rtsp: RtspSession,
        credentials: HapCredentials,
        steps: dict,
        options: Optional[QueueOptions] = None,
    ) -> None:
        self.rtsp = rtsp
        self.credentials = credentials
        self.steps = steps
        self.options = options or QueueOptions()
        self.step = ""
        self.link: Optional[SerialSession] = None
        self.channel: Optional[QueueEventChannel] = None
        self.stream_id: Optional[int] = None
        self.item: Optional[str] = None  # the play queue item's uuid
        self.session_id = str(uuid4()).upper()  # X-Apple-Session-ID of every /command
        self.state: Optional[str] = None  # the TV's last playbackState
        self.events: dict[str, int] = {}  # what the TV said ("playbackState/playing"), in order, with counts
        self.error: Optional[dict] = None  # the TV's {type: error} message
        self.end_reason = ""  # why the TV ended the cast (a Text), if it did
        self.failure: Optional[Text] = None  # what wait() raises: the cast ended because something failed
        self.taken = asyncio.Event()  # the TV named the item: it took it
        self.played = asyncio.Event()  # the TV said it is playing
        self.ended = asyncio.Event()  # it said the video ended, failed, or hung up the event channel
        self.set_up_at: Optional[float] = None  # time.monotonic() when the TV answered the first SETUP
        self.loaded_at: Optional[float] = None  # ... and took the item
        self.heard = 0  # messages from the TV on the event channel, answers to playbackInfo included
        self._host = ""  # where the video comes from, when not from this PC
        self._replies: dict[int, asyncio.Future] = {}  # requests the TV answers on the event channel
        self._next_message = 1
        self._feedback_task: Optional[asyncio.Task] = None
        self._closed = False

    # -- requests ----------------------------------------------------------

    def _said(self, answer: Optional[HttpResponse]) -> object:
        """An answer for `steps`: its status, or why there is none."""
        if answer is not None:
            return answer.code
        return "connection lost" if self.link is None or self.link.closed.is_set() else "no answer"

    async def _step(
        self, name: str, method: str, uri: str, body: Optional[dict] = None, timeout: Optional[float] = None
    ) -> HttpResponse:
        answer = await self.link.request(method, uri, body, timeout=timeout)
        self.steps[name] = self._said(answer)
        if not _answered(answer):
            raise QueueRefused(name, answer.code if answer is not None else None)
        return answer

    async def command(self, name: str, payload: dict, timeout: Optional[float] = None) -> Optional[HttpResponse]:
        """POST /command with `payload`; its answer goes in steps under `name` (none if "")."""
        answer = await self.link.request(
            "POST", "/command", {"params": {"data": bplist(payload)}},
            protocol="HTTP/1.1", user_agent=COMMAND_USER_AGENT, timeout=timeout,
            headers={
                "X-Apple-ProtocolVersion": "1",
                "X-Apple-Session-ID": self.session_id,
                "X-Apple-StreamID": str(self.stream_id),
            },
        )
        if name:
            self.steps[name] = self._said(answer)
        return answer

    async def request(self, payload: dict, timeout: Optional[float] = None) -> Optional[dict]:
        """A command the TV answers on the event channel (playbackInfo): that answer, or None
        if none came within `timeout` (INFO_TIMEOUT)."""
        message_id = self._next_message
        self._next_message += 1
        reply = asyncio.get_running_loop().create_future()
        self._replies[message_id] = reply
        try:
            answer = await self.command("", {**payload, "kind": "request", "messageID": message_id})
            if not _answered(answer):
                return None
            return await asyncio.wait_for(reply, INFO_TIMEOUT if timeout is None else timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._replies.pop(message_id, None)

    # -- the session -------------------------------------------------------

    def _session_body(self, timing_port: int) -> dict:
        identity = {
            "deviceID": SENDER_ID,
            "macAddress": SENDER_ID,
            "model": "iPhone14,3",
            "name": APP_NAME,
            "osBuildVersion": "20F66",
            "osName": "iPhone OS",
            "osVersion": "16.5",
            "sourceVersion": "690.7.1",
        }
        if not self.options.apple:  # pyatv's own SETUP, which other brands' TVs already take
            return {
                **identity,
                "sessionUUID": str(uuid4()).upper(),
                "timingPort": timing_port,
                "timingProtocol": "NTP",
                "isMultiSelectAirPlay": True,
                "groupContainsGroupLeader": False,
                "senderSupportsRelay": False,
                "statsCollectionEnabled": False,
            }
        # tvOS 26 plays a video timed by NTP for some 20 seconds only, and says nothing about it.
        # PTP needs no clock of ours: the peer list is all it asks for.
        peer = {
            "ID": str(uuid4()).upper(),
            "Addresses": [self.rtsp.connection.local_ip],
            "DeviceType": 0,
            "SupportsClockPortMatchingOverride": True,
        }
        return {
            **identity,
            "timingProtocol": "PTP",
            "timingPeerInfo": peer,
            "timingPeerList": [peer],
            "sessionUUID": str(uuid4()).upper(),
            "sessionCorrelationUUID": str(uuid4()).upper(),
            "updateSessionRequest": False,
            "statsCollectionEnabled": False,
            "isMultiSelectAirPlay": False,
        }

    async def open(self, timing_port: int) -> None:
        """pair-verify, SETUP, the event channel, RECORD, and the remote control stream."""
        self.step = "pair-verify"
        verifier = await verify_connection(self.credentials, self.rtsp.connection)
        self.link = SerialSession(self.rtsp)

        self.step = "setup"
        answer = await self._step("setup", "SETUP", self.rtsp.uri, self._session_body(timing_port), SETUP_TIMEOUT)
        self.set_up_at = time.monotonic()
        event_port = _plist(answer).get("eventPort")
        if not event_port:
            self.steps["setup"] = f"{answer.code} without an event port"
            raise QueueRefused("setup", answer.code)

        self.step = "events"
        self.channel = await self._connect_events(verifier, int(event_port))

        # After the event channel: a TV that gets RECORD first plays, but reports nothing.
        self.step = "record"
        await self._step("record", "RECORD", self.rtsp.uri)

        sender = SENDER_ID
        if self.options.apple:  # tvOS names the channel after its own psi
            info = await self.link.request("GET", "/info")
            sender = (_plist(info).get("psi") if _answered(info) else None) or SENDER_ID
        self.step = "setup130"
        answer = await self._step("setup130", "SETUP", self.rtsp.uri, {"streams": [{
            "type": STREAM_TYPE_RCS,
            "controlType": 1,
            "clientTypeUUID": RCS_CLIENT_TYPE,
            "channelID": f"{sender}-RCS-1",
            "clientUUID": str(uuid4()).upper(),
        }]})
        streams = _plist(answer).get("streams")
        stream = streams[0] if isinstance(streams, list) and streams and isinstance(streams[0], dict) else {}
        self.stream_id = stream.get("streamID")
        if not isinstance(self.stream_id, int):
            # The TV took the stream without numbering it: the first stream is 1.
            _LOGGER.info("The TV gave the remote control stream no ID (%s); using 1", answer.body or "no body")
            self.steps["setup130"] = f"{answer.code} without a stream ID"
            self.stream_id = 1
        self.step = ""

    async def _connect_events(self, verifier, port: int) -> QueueEventChannel:
        tries = EVENT_CHANNEL_TRIES
        while True:
            try:
                _, channel = await setup_channel(
                    lambda output_key, input_key: QueueEventChannel(
                        output_key, input_key, self._on_message, self._on_events_closed),
                    verifier, self.rtsp.connection.remote_ip, port,
                    EVENTS_SALT, EVENTS_READ_INFO, EVENTS_WRITE_INFO,
                )
                return channel
            except ConnectionRefusedError:
                tries -= 1
                if not tries:
                    raise
                await asyncio.sleep(1)

    async def load(self, url: str) -> None:
        """Hand the TV the URL and start playback. Only the URL itself has to be taken."""
        self.step = "insert"
        host = urlsplit(url).hostname or ""
        self._host = "" if host == self.rtsp.connection.local_ip else host
        self.item = str(uuid4()).upper()
        item: dict = {"uuid": self.item, "mediaType": self.options.media_type, "Content-Location": url}
        if self.options.start is not None:
            item["Start-Position"] = cm_time(self.options.start)
        if self.options.apple:
            item.update(_TVOS_ITEM_KEYS, IsTLSEnabled=url.startswith("https:"))
        answer = await self.command("insert", {"type": "insertPlayQueueItem", "item": item})
        if not _answered(answer):
            if answer is not None:
                self.item = None  # turned down: there is nothing to stop
            raise QueueRefused("insert", answer.code if answer is not None else None)
        self.loaded_at = time.monotonic()
        # The rest only shapes playback; the TV may well start without them.
        await self.command("isInterestedInDateRange", {
            "type": "setProperty", "property": "isInterestedInDateRange", "value": True, "item": {"uuid": self.item},
        })
        await self.command("actionAtItemEnd", {"type": "setProperty", "property": "actionAtItemEnd", "value": 1})
        await self.command("rate", {"type": "setRate", "rate": 1.0})
        self.step = ""
        # Only now: the keep-alive must not be under way while the commands above are.
        self._feedback_task = asyncio.ensure_future(self._keep_alive())

    def _signs_of_life(self) -> int:
        """Everything the TV said so far: on the event channel, and answers, late ones included."""
        return self.heard + self.link.answers

    async def _keep_alive(self) -> None:
        """POST /feedback every 2 s, as pyatv's feedback task does, but in turn with the rest.

        A TV may leave one unanswered and play on (an LG did, for over 5 s). One that answers
        none of FEEDBACK_MISSES in a row, and gives no other sign of life meanwhile, is gone:
        the cast ends.
        """
        failed, misses = False, 0
        while not self.link.closed.is_set():
            heard = self._signs_of_life()
            await asyncio.sleep(FEEDBACK_INTERVAL)
            answer = await self.link.request("POST", "/feedback", timeout=FEEDBACK_TIMEOUT)
            if answer is None and self._signs_of_life() == heard:
                misses += 1
                if misses >= FEEDBACK_MISSES:
                    self._end(Text("queue_end_silent", "the TV stopped answering ({misses} keep-alives in a row)",
                                   misses=FEEDBACK_MISSES))
                    return
            else:
                misses = 0
            if not _answered(answer) and not failed:
                failed = True  # once: a TV that keeps playing does not need it said again
                _LOGGER.info("The TV answered the keep-alive with %s", self._said(answer))

    # -- what the TV says --------------------------------------------------

    def _on_message(self, message: dict) -> None:
        self.heard += 1
        kind = message.get("kind")
        if kind == "response":
            reply = self._replies.get(message.get("messageID"))
            if reply is not None and not reply.done():
                reply.set_result(message)
            _LOGGER.debug("The TV answered: %s", message)
            return
        what = message.get("type") or "?"
        name = message.get("name") or message.get("value")
        label = f"{what}/{name}" if isinstance(name, str) else str(what)
        if label in self.events:
            _LOGGER.debug("The TV says %s: %s", label, message)
        else:  # the first of each kind, for a bug report
            detail = repr(message)
            _LOGGER.info("The TV says %s: %s", label,
                         detail if len(detail) <= LOGGED_DETAIL else detail[:LOGGED_DETAIL] + "...")
        self.events[label] = self.events.get(label, 0) + 1

        item = message.get("item")
        uuid = item.get("uuid") if isinstance(item, dict) else message.get("uuid")
        if self.item is not None and uuid == self.item:
            self.taken.set()
        if what == "playbackState":
            self.state = name
            if name == "playing":
                self.played.set()
            elif name == "stopped" and self.played.is_set():
                self._end(END_STOPPED)
        elif what == "notification" and name == "itemPlayedToEnd":
            if self.options.live:
                # A live stream has no end: the TV's player read its playlist once, as a file's.
                seconds = round(time.monotonic() - (self.loaded_at or time.monotonic()))
                _LOGGER.warning("The TV ended the live stream after %d s, as if it were a video file", seconds)
                self._end(END_PLAYED, with_probe_advice(Text(
                    "tv_live_played_to_end", "The TV ended the live stream after {seconds} s, as if it were a "
                    "video file that had reached its end.", seconds=seconds)))
            else:
                self._end(END_PLAYED)
        elif what == "error":
            self.error = message.get("error") if isinstance(message.get("error"), dict) else {}
            self._end(END_FAILED, media_error(self.error, self._host))
        elif what == "sendMediaRemoteCommand" and name == "pbal":
            self._end(END_BACK)  # an LG sends pbpr, then pbal

    def _on_events_closed(self) -> None:
        if not self._closed:  # not when close() closes it
            self._end(END_EVENTS_CLOSED)

    def _end(self, reason: Text, failure: Optional[Text] = None) -> None:
        if not self.ended.is_set():
            self.end_reason = reason
            self.failure = failure
            self.ended.set()

    def ended_early(self) -> Text:
        """The error for a cast that the TV ended before it played the video."""
        return with_probe_advice(Text("tv_queue_ended", "The TV ended the AirPlay session before it played the "
                                      "video: {reason}.", reason=self.end_reason or END_HUNG_UP))

    async def wait(self) -> None:
        """Until the video ends on the TV: it stops after playing, reaches its end, BACK is
        pressed, or the TV hangs up. Raises PlaybackError when the TV's player fails, when it
        ends the live stream as a file, and, for a video file, when the TV says nothing about
        it within START_TIMEOUT or hangs up before it played it.

        Whether the TV fetches the live stream is for the cast session to watch (it sees the
        requests); here the TV is only asked, now and then, what it is playing, for the log.
        """
        waiters = [asyncio.ensure_future(event.wait()) for event in (self.ended, self.link.closed)]
        asked, answers = True, 0
        started = time.monotonic()

        def said_anything() -> bool:
            return self.taken.is_set() or self.played.is_set()

        try:
            while True:
                done, _ = await asyncio.wait(waiters, timeout=INFO_INTERVAL, return_when=asyncio.FIRST_COMPLETED)
                if done:
                    break
                if not self.options.live and not said_anything() and time.monotonic() - started > START_TIMEOUT:
                    raise exceptions.PlaybackError(with_probe_advice(Text(
                        "tv_queue_silent", "The TV took the video but did not say within {seconds} s that it was "
                        "playing it.", seconds=START_TIMEOUT)))
                if asked:
                    info = await self.request({"type": "playbackInfo"})
                    if info is None:
                        asked = answers > 0  # a TV that never answers it is not asked again
                        _LOGGER.debug("The TV did not answer playbackInfo")
                    else:
                        answers += 1
                        _LOGGER.log(logging.INFO if answers == 1 else logging.DEBUG, "The TV is playing: %s",
                                    {k: v for k, v in info.items() if k not in ("kind", "messageID")})
        finally:
            for waiter in waiters:
                waiter.cancel()
        self.end_reason = self.end_reason or END_HUNG_UP
        _LOGGER.info("The cast ended: %s", self.end_reason)
        if self.failure is not None:
            raise exceptions.PlaybackError(self.failure)
        if not self.options.live and not said_anything() and self.end_reason != END_BACK:
            raise exceptions.PlaybackError(self.ended_early())

    async def close(self) -> None:
        """Stop the video on the TV and end the session: once, never raising, in a few seconds."""
        if self._closed:
            return
        self._closed = True
        task, self._feedback_task = self._feedback_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.link is not None and not self.link.closed.is_set():
            said = {}
            if self.item is not None and self.stream_id is not None:
                said["stop"] = self._said(await self.command("", {"type": "stop"}, timeout=CLOSE_TIMEOUT))
            if self.link.owes:  # still no answer to an earlier request: closing the connection ends the session too
                said["teardown"] = "not sent"
            else:
                said["teardown"] = self._said(await self.link.request("TEARDOWN", self.rtsp.uri,
                                                                      timeout=CLOSE_TIMEOUT))
            _LOGGER.info("Ended the AirPlay video session: %s", " ".join(f"{k}={v}" for k, v in said.items()))
        for reply in self._replies.values():
            reply.cancel()
        if self.channel is not None:
            self.channel.close()
        self.rtsp.connection.close()
