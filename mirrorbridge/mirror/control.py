"""The encrypted RTSP connection that sets up and holds a mirroring session.

AirPlay 2 TVs of other brands (TCL, Roku, Samsung, LG...) pair the HomeKit way: once with the
PIN the TV shows (devices.DeviceManager.pair, through pyatv), then on every connection with
pair-verify from the saved pairing. Pair-verify agrees on a secret that keys everything after
it: this connection (ChaCha20-Poly1305 in frames of at most 1024 bytes, pyatv's HAPSession), the
TV's event channel and the video (packets.py).

pyatv (MIT) does the cryptography. What it does not do is check the TV's pair-verify answers
for an error (M2, M4): a TV that has dropped the pairing says so there, and pyatv would then go
on to send requests the TV cannot read, which it never answers. pair_verify() here checks them
for a saved pairing. pyatv's requests also time out after 4 to 10 s, and a TV can take longer
than that to answer a mirroring SETUP, so each request here names its own timeout.

pyatv hands answers to requests in the order they come, not by CSeq: an answer that comes after
its request timed out would be taken for the next request's. So a channel that has had a
request go unanswered (or an answer with another request's CSeq) is `out_of_step`, and a
session is set up on a new connection rather than on that one (negotiation.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import plistlib
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from cryptography.exceptions import InvalidTag
from pyatv import exceptions
from pyatv.auth.hap_pairing import AuthenticationType, HapCredentials, parse_credentials
from pyatv.auth.hap_session import HAPSession
from pyatv.auth.hap_srp import SRPAuthHandler
from pyatv.auth.hap_tlv8 import ErrorCode, TlvValue, read_tlv, write_tlv
from pyatv.protocols.airplay.auth.hap_transient import AirPlayHapTransientPairVerifyProcedure
from pyatv.protocols.airplay.channels import EventChannel
from pyatv.support.http import HttpConnection, HttpResponse, http_connect, parse_request
from pyatv.support.rtsp import BPLIST_CONTENT_TYPE

from ..text import Text
from .errors import ChannelClosed, MirrorError, NoAnswer, answer_of
from .packets import KeyDeriver

_LOGGER = logging.getLogger(__name__)

CONNECT_SECONDS = 10
VERIFY_SECONDS = 15
REQUEST_SECONDS = 10
INFO_SECONDS = 10

# Who is sending: the version of AirPlay MirrorBridge's requests say they come from, in the
# User-Agent of every request on the control connection and as the SETUP's sourceVersion, the
# same in both (a receiver may go by either). It is the version senders on HomeKit-paired
# sessions give.
# Experiment: MIRRORBRIDGE_SENDER=iphone says MirrorBridge is what AirParrot says it is (an iPhone
# 13 on iOS 16.5, AirPlay 690.7.1), to see whether a TV treats an Apple sender differently;
# MIRRORBRIDGE_SOURCE_VERSION sets the version alone.
SENDERS = {
    "iphone": {"model": "iPhone14,3", "osBuildVersion": "20F66", "osName": "iPhone OS", "osVersion": "16.5",
               "sourceVersion": "690.7.1"},
}
SENDER = SENDERS.get(os.environ.get("MIRRORBRIDGE_SENDER", "").strip().lower(), {})
SOURCE_VERSION = os.environ.get("MIRRORBRIDGE_SOURCE_VERSION", "").strip() or SENDER.get("sourceVersion", "980.71.1")
USER_AGENT = f"AirPlay/{SOURCE_VERSION}"

# Pair-verify goes out as pyatv's HomeKit pairing sends it ("X-Apple-HKP: 3"): TVs of other
# brands want type 3 (type 5, Apple TV's screen-capture pairing, fails there).
_PAIR_HEADERS = {
    "User-Agent": "AirPlay/320.20",
    "Connection": "keep-alive",
    "X-Apple-HKP": 3,
    "Content-Type": "application/octet-stream",
}

CONTROL_SALT = "Control-Salt"
CONTROL_WRITE = "Control-Write-Encryption-Key"
CONTROL_READ = "Control-Read-Encryption-Key"
EVENTS_SALT = "Events-Salt"
EVENTS_WRITE = "Events-Write-Encryption-Key"
EVENTS_READ = "Events-Read-Encryption-Key"

# AirPlay feature bits (the "features" TXT value and /info) that decide how to mirror
FEATURE_SCREEN = 7
FEATURE_FAIRPLAY = 14
FEATURE_1080P = 28
FEATURE_PTP = 41


@dataclass
class Target:
    """The receiver to mirror to, as discovery (or the user) found it."""

    name: str
    address: str
    port: int = 7000
    credentials: Optional[str] = None  # pyatv's pairing string, as devices.DeviceManager saved it
    features: Optional[int] = None
    srcvers: str = ""
    identifier: str = ""  # what its saved mirroring settings are kept under (profiles.py)

    @classmethod
    def from_device(cls, device) -> "Target":
        """A devices.Device, with the pairing MirrorBridge saved for it."""
        svc = device.airplay
        props = svc.properties if svc else {}
        return cls(device.name, device.address, svc.port if svc and svc.port else 7000,
                   svc.credentials if svc else None, device.features, props.get("srcvers", ""), device.identifier)

    def has_feature(self, bit: int) -> bool:
        return self.features is not None and bool(self.features >> bit & 1)

    @property
    def offers_mirroring(self) -> bool:
        """The receiver says it takes screen mirroring (bit 7); one that says nothing may."""
        return self.features is None or self.has_feature(FEATURE_SCREEN)

    @property
    def needs_fairplay(self) -> bool:
        """Apple TVs and Macs (bit 14): their mirroring needs FairPlay, which MirrorBridge lacks."""
        return self.has_feature(FEATURE_FAIRPLAY)


@dataclass
class Display:
    width: int
    height: int
    max_fps: Optional[int] = None


@dataclass
class ReceiverInfo:
    """What the TV says about itself in GET /info, read after pair-verify."""

    displays: list[Display] = field(default_factory=list)
    max_width: Optional[int] = None  # widthPixelsMax / heightPixelsMax, where given
    max_height: Optional[int] = None
    screen_formats: Optional[int] = None  # supportedFormats.screenStream
    event_port: Optional[int] = None
    timing_peer: Optional[dict] = None  # timingPeerInfo
    features: Optional[int] = None
    model: str = ""
    source_version: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, plist: Mapping[str, Any]) -> "ReceiverInfo":
        info = cls(raw=dict(plist))
        for item in plist.get("displays") or []:
            if not isinstance(item, dict):
                continue
            width = _number(item, "widthPixels", "width")
            height = _number(item, "heightPixels", "height")
            if width and height:
                info.displays.append(Display(width, height, _number(item, "maxFPS", "refreshRate")))
            info.max_width = info.max_width or _number(item, "widthPixelsMax")
            info.max_height = info.max_height or _number(item, "heightPixelsMax")
        info.max_width = _number(plist, "widthPixelsMax") or info.max_width
        info.max_height = _number(plist, "heightPixelsMax") or info.max_height
        formats = plist.get("supportedFormats")
        if isinstance(formats, dict) and isinstance(formats.get("screenStream"), int):
            info.screen_formats = formats["screenStream"]
        info.event_port = _number(plist, "eventPort")
        peer = plist.get("timingPeerInfo")
        info.timing_peer = peer if isinstance(peer, dict) else None
        info.features = parse_features(plist.get("features"))
        info.model = str(plist.get("model", ""))
        info.source_version = str(plist.get("sourceVersion", ""))
        return info

    def summary(self) -> str:
        displays = ", ".join(f"{d.width}x{d.height}" + (f" up to {d.max_fps} fps" if d.max_fps else "")
                             for d in self.displays) or "none"
        parts = [f"displays: {displays}"]
        if self.max_width and self.max_height:
            parts.append(f"max {self.max_width}x{self.max_height}")
        parts.append(f"screenStream formats: {hex(self.screen_formats) if self.screen_formats is not None else 'none'}")
        parts.append(f"eventPort: {self.event_port if self.event_port is not None else 'none'}")
        parts.append(f"timingPeerInfo: {self.timing_peer if self.timing_peer else 'none'}")
        if self.features is not None:
            parts.append(f"features: {hex(self.features)}")
        if self.model or self.source_version:
            parts.append(f"model {self.model or '?'}, sourceVersion {self.source_version or '?'}")
        return "; ".join(parts)


def _number(source: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return None


def parse_features(value: Any) -> Optional[int]:
    """Feature bits from /info: an int, or text such as "0x7F8AD0,0x38BCF46" (low word first)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        low, _, high = value.strip().partition(",")
        try:
            return (int(high, 16) << 32 if high else 0) | int(low, 16)
        except ValueError:
            return None
    return None


def sender_id(credentials: Optional[str]) -> str:
    """This PC's deviceID/macAddress for the TV: a locally administered MAC address that stays
    the same for one pairing, so the TV sees the same sender every time."""
    digest = hashlib.sha256((credentials or "MirrorBridge").encode()).digest()
    return ":".join(f"{b:02X}" for b in bytes([0x02]) + digest[:5])


def connect_failed(target: Target, answer: str) -> MirrorError:
    return MirrorError(Text(
        "mirror_connect_failed", "Could not connect to {device} ({address}:{port}): {error}. Check that the TV is "
        "on and on the same network as this PC.",
        device=target.name, address=target.address, port=target.port, error=answer,
    ), step="connect", steps={"connect": answer})


def _tlv(resp: HttpResponse) -> dict:
    body = resp.body
    if isinstance(body, str):
        body = body.encode("utf-8")  # pyatv decodes bodies without an application/* type
    if not isinstance(body, bytes):
        raise ValueError("pair-verify answer is not TLV8")
    return read_tlv(body) if body else {}


def _pair_verify_refused(target: Target, reason: str) -> MirrorError:
    return MirrorError(Text(
        "mirror_verify_refused", "{device} did not accept MirrorBridge's pairing (pair-verify: {reason}). Use "
        "Device > Forget pairing, then cast again and type the new PIN the TV shows.",
        device=target.name, reason=reason,
    ), step="pair-verify", steps={"pair-verify": reason})


async def _verify_hap(conn: HttpConnection, credentials: HapCredentials, target: Target) -> KeyDeriver:
    """Pair-verify (M1-M4) with a saved HomeKit pairing; the TV's M4 status is checked."""
    srp = SRPAuthHandler()
    _, public = srp.initialize()
    resp = await conn.post("/pair-verify", headers=_PAIR_HEADERS, allow_error=True,
                           body=write_tlv({TlvValue.SeqNo: b"\x01", TlvValue.PublicKey: public}))
    if resp.code != 200:
        raise _pair_verify_refused(target, f"HTTP {resp.code} to M1")
    m2 = _tlv(resp)
    if TlvValue.Error in m2:
        raise _pair_verify_refused(target, f"M2 error {m2[TlvValue.Error][0] if m2[TlvValue.Error] else 0}")
    try:
        # Checks that the TV is the one that was paired (its Ed25519 signature) and signs ours.
        m3 = srp.verify1(credentials, m2[TlvValue.PublicKey], m2[TlvValue.EncryptedData])
    except KeyError as ex:
        raise _pair_verify_refused(target, "M2 without a key") from ex
    except (InvalidTag, ValueError) as ex:  # sealed with another key, or not a key at all
        raise _pair_verify_refused(target, "M2 could not be decrypted") from ex
    except exceptions.AuthenticationError as ex:
        raise _pair_verify_refused(target, f"the TV's signature: {ex}") from ex
    resp = await conn.post("/pair-verify", headers=_PAIR_HEADERS, allow_error=True,
                           body=write_tlv({TlvValue.SeqNo: b"\x03", TlvValue.EncryptedData: m3}))
    if resp.code != 200:
        raise _pair_verify_refused(target, f"HTTP {resp.code} to M3")
    m4 = _tlv(resp)
    if TlvValue.Error in m4:
        code = m4[TlvValue.Error][0] if m4[TlvValue.Error] else 0
        try:
            name = ErrorCode(code).name
        except ValueError:
            name = "unknown"
        raise _pair_verify_refused(target, f"M4 error {code} ({name})")
    return srp.verify2


async def pair_verify(conn: HttpConnection, target: Target) -> KeyDeriver:
    """Pair-verify on `conn` with the target's saved pairing; returns the key derivation."""
    try:
        credentials = parse_credentials(target.credentials)
    except (ValueError, exceptions.InvalidCredentialsError) as ex:
        raise _pair_verify_refused(target, "unreadable saved pairing") from ex
    if credentials.type == AuthenticationType.HAP:
        return await _verify_hap(conn, credentials, target)
    if credentials.type == AuthenticationType.Transient:
        # pyatv's own procedure, which checks neither answer for an error: a TV that says no
        # shows up as an answer it cannot read, or one that does not decrypt.
        procedure = AirPlayHapTransientPairVerifyProcedure(conn, SRPAuthHandler())
        try:
            await procedure.verify_credentials()
        except (KeyError, InvalidTag, exceptions.InvalidResponseError) as ex:
            raise _pair_verify_refused(target, f"transient pairing: {type(ex).__name__}") from ex
        return procedure.encryption_keys
    raise MirrorError(Text(
        "mirror_not_paired", "MirrorBridge is not paired with {device}. Use Device > Pair with PIN, then try again.",
        device=target.name,
    ), step="pair-verify", steps={"pair-verify": "not paired"})


class ControlChannel:
    """The RTSP connection, encrypted after pair-verify. One request at a time: a TV is asked
    the next thing only once it has answered the last (/feedback included)."""

    def __init__(self, target: Target, conn: HttpConnection, derive: KeyDeriver) -> None:
        self.target = target
        self.conn = conn
        self.derive = derive
        self.cseq = 0
        self.out_of_step = False  # a late answer may still come (see the module's docstring)
        self.dacp_id = f"{int.from_bytes(hashlib.sha256(target.address.encode()).digest()[:8], 'big'):X}"
        self.active_remote = int(time.time()) & 0xFFFFFFFF
        self._lock = asyncio.Lock()
        self.local_ip = conn.local_ip
        self.remote_ip = conn.remote_ip

    @classmethod
    async def open(cls, target: Target, connect_seconds: float = CONNECT_SECONDS,
                   verify_seconds: float = VERIFY_SECONDS) -> "ControlChannel":
        """Connect to the TV and pair-verify; raises MirrorError with the reason."""
        try:
            conn = await asyncio.wait_for(http_connect(target.address, target.port), connect_seconds)
        except (OSError, asyncio.TimeoutError) as ex:
            raise connect_failed(target, answer_of(ex)) from ex
        try:
            derive = await asyncio.wait_for(pair_verify(conn, target), verify_seconds)
        except MirrorError:
            conn.close()
            raise
        except (asyncio.TimeoutError, TimeoutError, OSError, RuntimeError, ValueError, KeyError, InvalidTag,
                exceptions.ConnectionLostError, exceptions.AuthenticationError, exceptions.ProtocolError,
                exceptions.InvalidResponseError) as ex:
            conn.close()
            _LOGGER.debug("Pair-verify with %s failed", target.name, exc_info=True)
            if isinstance(ex, (OSError, TimeoutError, asyncio.TimeoutError)):
                reason = answer_of(ex)
            else:
                reason = str(ex) or type(ex).__name__
            raise _pair_verify_refused(target, reason) from ex
        except BaseException:
            # Cancelled (Stop, or a time limit around this): the TV is not left holding a
            # half-done pair-verify, which some TVs count as the one sender they allow.
            conn.close()
            raise
        session = HAPSession()
        session.enable(*derive(CONTROL_SALT, CONTROL_WRITE, CONTROL_READ))
        conn.receive_processor = session.decrypt
        conn.send_processor = session.encrypt
        _LOGGER.info("Pair-verify with %s (%s:%s) done; the control connection is encrypted",
                     target.name, target.address, target.port)
        return cls(target, conn, derive)

    @property
    def closed(self) -> bool:
        return self.conn.transport is None

    async def request(
        self,
        method: str,
        uri: str,
        *,
        body: Optional[Any] = None,
        headers: Optional[Mapping[str, object]] = None,
        content_type: Optional[str] = None,
        timeout: float = REQUEST_SECONDS,
    ) -> HttpResponse:
        """Send one RTSP request and return the TV's answer, whatever its status.

        A dict body goes as a binary plist; bytes go as they are, with `content_type` (SETPEERS's
        plist array). Raises NoAnswer if the TV does not answer within `timeout`, ChannelClosed
        if the connection is gone.
        """
        async with self._lock:
            if self.closed:
                raise ChannelClosed(f"{method} {uri}: not connected")
            cseq = self.cseq
            hdrs: dict = {
                "CSeq": cseq,
                "DACP-ID": self.dacp_id,
                "Active-Remote": self.active_remote,
                "Client-Instance": self.dacp_id,
            }
            self.cseq += 1
            hdrs.update(headers or {})
            data = body
            if isinstance(body, dict):
                hdrs["Content-Type"] = BPLIST_CONTENT_TYPE
                data = plistlib.dumps(body, fmt=plistlib.FMT_BINARY)  # pylint: disable=no-member
            elif isinstance(body, bytearray):
                data = bytes(body)
            try:
                resp = await self.conn.send_and_receive(
                    method, uri, protocol="RTSP/1.0", user_agent=USER_AGENT, content_type=content_type,
                    headers=hdrs, body=data, allow_error=True, timeout=timeout,
                )
            except exceptions.AuthenticationError:  # a 403 raises even with allow_error
                return HttpResponse("RTSP", "1.0", 403, "Forbidden", {}, b"")
            except TimeoutError as ex:
                self.out_of_step = True
                raise NoAnswer(f"no answer to {method} {uri} within {timeout:g} s") from ex
            except (RuntimeError, exceptions.ConnectionLostError) as ex:
                raise ChannelClosed(f"{method} {uri}: {ex}") from ex
            answered = str(resp.headers.get("CSeq", cseq)).strip()
            if answered != str(cseq) and not self.out_of_step:
                _LOGGER.debug("Answer with CSeq %s to %s %s (CSeq %d): the connection is out of step",
                              answered, method, uri, cseq)
                self.out_of_step = True
            return resp

    async def info(self, timeout: Optional[float] = None) -> ReceiverInfo:
        """GET /info over the encrypted connection (within INFO_SECONDS unless `timeout` says
        otherwise); raises MirrorError if the TV does not give it."""
        timeout = INFO_SECONDS if timeout is None else timeout
        def no_info(answer: Any, code: Optional[int] = None) -> MirrorError:
            return MirrorError(Text(
                "mirror_no_info", "{device} did not say what it can show (GET /info: {answer}).",
                device=self.target.name, answer=answer,
            ), step="info", code=code, steps={"info": answer})

        try:
            resp = await self.request("GET", "/info", timeout=timeout)
        except (NoAnswer, ChannelClosed) as ex:
            raise no_info(answer_of(ex)) from ex
        if resp.code != 200:
            raise no_info(resp.code, resp.code)
        plist = decode_plist(resp)
        _LOGGER.debug("GET /info from %s: %s", self.target.name, plist)
        info = ReceiverInfo.parse(plist)
        _LOGGER.info("%s: %s", self.target.name, info.summary())
        return info

    def close(self) -> None:
        self.conn.close()


def decode_plist(resp: HttpResponse) -> dict:
    """The binary plist in an answer, or {} for an empty or unreadable one."""
    body = resp.body
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not body:
        return {}
    try:
        value = plistlib.loads(body)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Unreadable plist in answer: %r", body[:200])
        return {}
    return value if isinstance(value, dict) else {}


class _LoggingEventChannel(EventChannel):
    """pyatv's event channel (answers every request with 200), with what the TV says logged."""

    def handle_received(self) -> None:
        try:
            request, _ = parse_request(bytes(self.buffer))
        except ValueError:
            request = None
        if request is not None:
            body = request.body
            if isinstance(body, bytes) and body.startswith(b"bplist"):
                try:
                    body = plistlib.loads(body)
                except Exception:  # pylint: disable=broad-except
                    pass
            _LOGGER.info("The TV says on the event channel: %s %s %s", request.method, request.path, body or "")
        super().handle_received()

    def connection_lost(self, exc) -> None:
        _LOGGER.debug("Event channel closed: %s", exc)


async def open_event_channel(channel: ControlChannel, port: int, seconds: float = 3.0,
                             attempt_seconds: float = 1.5) -> Optional[asyncio.BaseTransport]:
    """Connect to the TV's event channel (keys reversed: the TV writes with the read key).

    Some receivers open it a moment after naming its port, so a refused connection is tried
    again every half second, for `seconds` in all (a connection that is not answered gets
    `attempt_seconds`). The session is set up meanwhile, without waiting for this. None if it
    never opens: the session goes on without it.
    """
    out_key, in_key = channel.derive(EVENTS_SALT, EVENTS_READ, EVENTS_WRITE)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            transport, _ = await asyncio.wait_for(loop.create_connection(
                lambda: _LoggingEventChannel(out_key, in_key), channel.remote_ip, port),
                max(0.1, min(attempt_seconds, deadline - loop.time())))
            _LOGGER.info("Event channel open (port %d)", port)
            return transport
        except (OSError, asyncio.TimeoutError) as ex:
            _LOGGER.debug("Event channel, attempt %d: %s", attempt, ex)
        if loop.time() + 0.5 >= deadline:
            break
        await asyncio.sleep(0.5)
    _LOGGER.warning("Could not open the TV's event channel on port %d; going on without it", port)
    return None
