"""The Cast control connection (CASTV2): TLS to port 8009, JSON messages in a small protobuf.

Every message is a 4-byte big-endian length and a CastMessage (cast_channel.proto): who sends
it, to whom, on which namespace, and a UTF-8 JSON payload. The sender opens a virtual
connection to "receiver-0" (the device) to launch an app, and another to the app's transportId
to talk to it. Both sides PING now and then; a PING is answered with a PONG.

The receiver's certificate is not checked: a TV's is signed by Google's Cast CA, which only
matters for device authentication, and nothing secret goes over this connection that the video
key (given in the OFFER) does not already protect against anyone but the TV.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import struct
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)

PORT = 8009
NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_WEBRTC = "urn:x-cast:com.google.cast.webrtc"  # OFFER and ANSWER of Cast Streaming
PLATFORM = "receiver-0"
SENDER = "sender-0"
HEARTBEAT_SECONDS = 5
CONNECT_SECONDS = 10
MAX_MESSAGE = 64 * 1024

# CastMessage fields (cast_channel.proto)
_VERSION, _SOURCE, _DESTINATION, _NAMESPACE, _PAYLOAD_TYPE, _PAYLOAD_UTF8, _PAYLOAD_BINARY = range(1, 8)


class ChannelError(Exception):
    """The connection failed or closed, or the receiver did not answer."""


# -- protobuf, as much of it as CastMessage needs ------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("bad varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def encode_message(source: str, destination: str, namespace: str, payload: str) -> bytes:
    """A CastMessage with a UTF-8 payload, with its 4-byte length in front."""
    body = bytearray()
    body += _varint(_VERSION << 3) + _varint(0)  # CASTV2_1_0
    for field, text in ((_SOURCE, source), (_DESTINATION, destination), (_NAMESPACE, namespace)):
        raw = text.encode()
        body += _varint(field << 3 | 2) + _varint(len(raw)) + raw
    body += _varint(_PAYLOAD_TYPE << 3) + _varint(0)  # STRING
    raw = payload.encode()
    body += _varint(_PAYLOAD_UTF8 << 3 | 2) + _varint(len(raw)) + raw
    return struct.pack(">I", len(body)) + bytes(body)


def decode_message(body: bytes) -> dict:
    """A CastMessage (without its length) as {"source", "destination", "namespace", "payload"};
    a binary payload comes as bytes. Raises ValueError if it is not one."""
    fields: dict[int, Any] = {}
    pos = 0
    while pos < len(body):
        key, pos = _read_varint(body, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            fields[field], pos = _read_varint(body, pos)
        elif wire == 2:
            size, pos = _read_varint(body, pos)
            if pos + size > len(body):
                raise ValueError("field runs past the end")
            fields[field] = bytes(body[pos:pos + size])
            pos += size
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            raise ValueError(f"wire type {wire}")
    if _NAMESPACE not in fields:
        raise ValueError("no namespace")
    payload = fields.get(_PAYLOAD_UTF8)
    return {
        "source": fields.get(_SOURCE, b"").decode("utf-8", "replace"),
        "destination": fields.get(_DESTINATION, b"").decode("utf-8", "replace"),
        "namespace": fields[_NAMESPACE].decode("utf-8", "replace"),
        "payload": payload.decode("utf-8", "replace") if payload is not None else fields.get(_PAYLOAD_BINARY, b""),
    }


# -- the connection ---------------------------------------------------------------------------


class CastChannel:
    """One TLS connection to a Cast receiver, with request/answer by requestId or seqNum.

    `on_message(namespace, source, payload_dict)` gets every JSON message no request was
    waiting for (RECEIVER_STATUS updates, CLOSE, the app's own messages).
    """

    def __init__(self, host: str, port: int = PORT, sender: str = SENDER) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.on_message: Optional[Callable[[str, str, dict], None]] = None
        self.on_closed: Optional[Callable[[], None]] = None
        self.local_ip = ""
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._tasks: list[asyncio.Task] = []
        self._waiting: dict[tuple[str, int], asyncio.Future] = {}
        self._next_id = 1
        self._connected: set[str] = set()
        self.closed = asyncio.Event()

    async def open(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=context), CONNECT_SECONDS)
        except (OSError, asyncio.TimeoutError, ssl.SSLError) as ex:
            raise ChannelError(f"could not connect to {self.host}:{self.port}: {ex or type(ex).__name__}") from ex
        sockname = self._writer.get_extra_info("sockname")
        self.local_ip = sockname[0] if sockname else ""
        self._tasks = [asyncio.ensure_future(self._read()), asyncio.ensure_future(self._heartbeat())]
        self.connect(PLATFORM)

    def next_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def send(self, namespace: str, payload: dict, destination: str = PLATFORM) -> None:
        if self._writer is None or self.closed.is_set():
            raise ChannelError("the Cast connection is closed")
        text = json.dumps(payload, separators=(",", ":"))
        if namespace != NS_HEARTBEAT:
            _LOGGER.debug("Cast -> %s %s: %s", destination, namespace.rsplit(".", 1)[-1], text[:2000])
        self._writer.write(encode_message(self.sender, destination, namespace, text))

    def connect(self, destination: str) -> None:
        """Open the virtual connection to `destination` (once)."""
        if destination in self._connected:
            return
        self._connected.add(destination)
        self.send(NS_CONNECTION, {"type": "CONNECT", "origin": {}, "userAgent": "MirrorBridge",
                                  "senderInfo": {"sdkType": 2, "version": "15.0", "browserVersion": "15.0",
                                                 "platform": 4, "systemVersion": "Windows", "connectionType": 1},
                                  "connType": 0}, destination)

    async def request(self, namespace: str, payload: dict, destination: str = PLATFORM,
                      timeout: float = 15, key: str = "requestId") -> dict:
        """Send `payload` with a new requestId (or seqNum, key="seqNum") and wait for the
        message that carries the same one back."""
        number = self.next_id()
        payload = {**payload, key: number}
        future = asyncio.get_running_loop().create_future()
        self._waiting[(key, number)] = future
        try:
            self.send(namespace, payload, destination)
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError as ex:
            raise ChannelError(f"no answer to {payload.get('type')} in {timeout:g} s") from ex
        finally:
            self._waiting.pop((key, number), None)

    async def _read(self) -> None:
        assert self._reader is not None
        try:
            while True:
                header = await self._reader.readexactly(4)
                size = struct.unpack(">I", header)[0]
                if size > MAX_MESSAGE:
                    raise ValueError(f"message of {size} bytes")
                self._dispatch(decode_message(await self._reader.readexactly(size)))
        except (asyncio.IncompleteReadError, OSError, ValueError, ssl.SSLError) as ex:
            _LOGGER.debug("Cast connection ended: %s", ex or type(ex).__name__)
        finally:
            self._closed()

    def _dispatch(self, message: dict) -> None:
        namespace, source, raw = message["namespace"], message["source"], message["payload"]
        if not isinstance(raw, str):
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Cast: not JSON on %s: %r", namespace, raw[:200])
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if namespace == NS_HEARTBEAT:
            if kind == "PING":
                self.send(NS_HEARTBEAT, {"type": "PONG"}, source)
            return
        _LOGGER.debug("Cast <- %s %s: %s", source, namespace.rsplit(".", 1)[-1], raw[:2000])
        for key in ("requestId", "seqNum"):
            number = payload.get(key)
            future = self._waiting.get((key, number)) if isinstance(number, int) else None
            if future is not None and not future.done():
                future.set_result(payload)
                return
        if namespace == NS_CONNECTION and kind == "CLOSE":
            self._connected.discard(source)
        if self.on_message is not None:
            try:
                self.on_message(namespace, source, payload)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Cast message handler failed")

    async def _heartbeat(self) -> None:
        while not self.closed.is_set():
            try:
                self.send(NS_HEARTBEAT, {"type": "PING"})
            except ChannelError:
                return
            await asyncio.sleep(HEARTBEAT_SECONDS)

    def _closed(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        for future in self._waiting.values():
            if not future.done():
                future.set_exception(ChannelError("the receiver closed the Cast connection"))
        if self.on_closed is not None:
            self.on_closed()

    async def close(self) -> None:
        for destination in list(self._connected):
            try:
                self.send(NS_CONNECTION, {"type": "CLOSE"}, destination)
            except ChannelError:
                break
        for task in self._tasks:
            task.cancel()
        writer, self._writer = self._writer, None
        self._closed()
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError, ssl.SSLError):
                pass
