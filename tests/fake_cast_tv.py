"""A Google Cast receiver in the test's own process, as a TV with Chromecast built-in answers.

The Cast control connection (TLS on a free port, a self-signed certificate) takes CONNECT,
PING, LAUNCH, GET_STATUS and STOP, and on the mirroring app's transport an OFFER, which it
answers with the UDP port of its video socket. That socket puts the RTP packets back into
frames, decrypts them with the OFFER's key and keeps them (Annex B, for FFmpeg to decode),
and sends Cast feedback as a receiver does: the last frame it has whole, and the packets it
misses. `drop_every` throws away every n-th packet the first time it comes, so that the
sender has to send them again.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
import struct
import tempfile
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mirrorbridge.googlecast.channel import (NS_CONNECTION, NS_HEARTBEAT, NS_RECEIVER, NS_WEBRTC, PLATFORM,
                                             decode_message, encode_message)
from mirrorbridge.googlecast.rtp import ALL_PACKETS, CAST, PT_APP, PT_PSFB, PT_RR, PT_SR, FrameCipher, expand
from mirrorbridge.googlecast.session import APP_ID

TRANSPORT = "web-5"
SESSION = "8c9a6f9e-0000-4000-8000-000000000001"
RECEIVER_SSRC = 31415


def _certificate(folder: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-chromecast")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2)).sign(key, hashes.SHA256()))
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class _Video(asyncio.DatagramProtocol):
    def __init__(self, tv: "FakeCastTV") -> None:
        self.tv = tv
        self.transport = None
        self.sender_addr = None
        self.parts: dict[int, dict[int, bytes]] = {}  # frame id -> packet id -> payload
        self.last_packet: dict[int, int] = {}
        self.keyframes: dict[int, bool] = {}
        self.seen: set[tuple[int, int]] = set()
        self.count = 0
        self.newest = -1
        self.logged = -1

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.sender_addr = addr
        if len(data) >= 8 and 194 <= data[1] <= 210:
            if data[1] == PT_SR:
                self.tv.sender_reports.append(struct.unpack_from(">IQI", data, 4))
            return
        if len(data) < 19 or data[0] != 0x80:
            return
        payload_type = data[1] & 0x7F
        self.tv.payload_types.add(payload_type)
        byte0, frame8, packet_id, last_id, _ref = struct.unpack_from(">BBHHB", data, 12)
        frame_id = expand(frame8, max(self.newest, 0))
        self.count += 1
        if self.tv.drop_every and self.count % self.tv.drop_every == 0 and (frame_id, packet_id) not in self.seen:
            self.seen.add((frame_id, packet_id))
            self.tv.dropped += 1
            return
        if (frame_id, packet_id) in self.seen and (frame_id, packet_id) not in self.parts.get(frame_id, {}):
            self.tv.resent_got += 1
        self.seen.add((frame_id, packet_id))
        if frame_id <= self.tv.acked:
            return
        self.newest = max(self.newest, frame_id)
        self.parts.setdefault(frame_id, {})[packet_id] = data[19:]
        self.last_packet[frame_id] = last_id
        self.keyframes[frame_id] = bool(byte0 & 0x80)
        self._complete()
        self.feedback()

    def _complete(self) -> None:
        tv = self.tv
        while True:
            frame_id = tv.acked + 1
            parts = self.parts.get(frame_id)
            if parts is None or len(parts) != self.last_packet[frame_id] + 1:
                return
            data = b"".join(parts[i] for i in range(len(parts)))
            tv.frames.append((frame_id, self.keyframes[frame_id], tv.cipher.apply(frame_id, data)))
            del self.parts[frame_id]
            tv.acked = frame_id

    def feedback(self) -> None:
        if self.transport is None or self.sender_addr is None or self.tv.silent:
            return
        tv = self.tv
        losses = []
        for frame_id in range(tv.acked + 1, self.newest + 1):
            got = self.parts.get(frame_id)
            if not got:
                losses.append((frame_id & 0xFF, ALL_PACKETS, 0))
                continue
            for packet_id in range(self.last_packet[frame_id] + 1):
                if packet_id not in got and (packet_id < max(got) or frame_id < self.newest):
                    losses.append((frame_id & 0xFF, packet_id, 0))
        body = struct.pack(">II4sBBH", RECEIVER_SSRC, tv.offer_ssrc, CAST, tv.acked & 0xFF, len(losses),
                           tv.delay_ms)
        body += b"".join(struct.pack(">BHB", *loss) for loss in losses)
        body += b"\x00" * (-len(body) % 4)
        report = struct.pack(">BBHI", 0x80, PT_RR, 1, RECEIVER_SSRC)
        psfb = struct.pack(">BBH", 0x80 | 15, PT_PSFB, len(body) // 4) + body
        app = b""
        if tv.show_log and tv.acked > self.logged:
            # The receiver's event log: the frames completed since the last one, decoded and shown
            # with 50 ms to spare.
            items = b"".join(struct.pack(">IIhHhH", frame_id * 3000, 1 << 24, 0, 13 << 12, 50, 12 << 12)
                             for frame_id in range(self.logged + 1, tv.acked + 1))
            self.logged = tv.acked
            app_body = struct.pack(">I4s", RECEIVER_SSRC, CAST) + items
            app = struct.pack(">BBH", 0x80 | 2, PT_APP, len(app_body) // 4) + app_body
        self.transport.sendto(report + psfb + app, self.sender_addr)


class FakeCastTV:
    def __init__(self, *, drop_every: int = 0, launch: bool = True, answer: Optional[dict] = None,
                 constraints: Optional[dict] = None) -> None:
        self.drop_every = drop_every
        self.launch = launch
        self.answer_override = answer
        self.constraints = constraints
        self.host = "127.0.0.1"
        self.port = 0
        self.messages: list[tuple[str, str, dict]] = []
        self.offer: Optional[dict] = None
        self.offer_ssrc = 0
        self.delay_ms = 0
        self.cipher: Optional[FrameCipher] = None
        self.frames: list[tuple[int, bool, bytes]] = []
        self.sender_reports: list = []
        self.payload_types: set[int] = set()
        self.acked = -1
        self.dropped = 0
        self.resent_got = 0
        self.stopped = False
        self.silent = False  # send no RTCP at all
        self.show_log = True  # send the receiver's event log (frames decoded and shown)
        self._server = None
        self._video = None
        self._video_transport = None
        self._writers: list[asyncio.StreamWriter] = []
        self._tmp = tempfile.TemporaryDirectory()

    async def start(self) -> "FakeCastTV":
        cert, key = _certificate(Path(self._tmp.name))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._server = await asyncio.start_server(self._serve, self.host, 0, ssl=context)
        self.port = self._server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        self._video_transport, self._video = await loop.create_datagram_endpoint(
            lambda: _Video(self), local_addr=(self.host, 0))
        return self

    @property
    def udp_port(self) -> int:
        return self._video_transport.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
        if self._video_transport is not None:
            self._video_transport.close()
        self._tmp.cleanup()

    def kinds(self, namespace: str) -> list[str]:
        return [p.get("type") for ns, _src, p in self.messages if ns == namespace]

    def annexb(self) -> bytes:
        return b"".join(data for _id, _key, data in self.frames)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                size = struct.unpack(">I", await reader.readexactly(4))[0]
                message = decode_message(await reader.readexactly(size))
                payload = json.loads(message["payload"])
                self.messages.append((message["namespace"], message["destination"], payload))
                for namespace, destination, answer in self._answer(message["namespace"], message["destination"],
                                                                    payload):
                    writer.write(encode_message(destination, message["source"], namespace, json.dumps(answer)))
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass

    def _status(self, running: bool) -> dict:
        apps = [{"appId": APP_ID, "displayName": "Chrome Mirroring", "sessionId": SESSION, "transportId": TRANSPORT,
                 "namespaces": [{"name": NS_WEBRTC}]}] if running else []
        return {"type": "RECEIVER_STATUS", "status": {"applications": apps, "volume": {"level": 1.0}}}

    def _answer(self, namespace: str, destination: str, payload: dict):
        kind = payload.get("type")
        if namespace == NS_HEARTBEAT and kind == "PING":
            yield NS_HEARTBEAT, destination, {"type": "PONG"}
        elif namespace == NS_RECEIVER and kind == "LAUNCH":
            if not self.launch:
                yield NS_RECEIVER, PLATFORM, {"type": "LAUNCH_ERROR", "reason": "NOT_FOUND",
                                              "requestId": payload["requestId"]}
                return
            yield NS_RECEIVER, PLATFORM, {**self._status(True), "requestId": payload["requestId"]}
        elif namespace == NS_RECEIVER and kind == "GET_STATUS":
            yield NS_RECEIVER, PLATFORM, {**self._status(self.launch and not self.stopped),
                                          "requestId": payload["requestId"]}
        elif namespace == NS_RECEIVER and kind == "STOP":
            self.stopped = True
            yield NS_RECEIVER, PLATFORM, {**self._status(False), "requestId": payload["requestId"]}
        elif namespace == NS_WEBRTC and kind == "OFFER" and destination == TRANSPORT:
            self.offer = payload
            stream = payload["offer"]["supportedStreams"][0]
            self.offer_ssrc = stream["ssrc"]
            self.delay_ms = stream["targetDelay"]
            self.cipher = FrameCipher(bytes.fromhex(stream["aesKey"]), bytes.fromhex(stream["aesIvMask"]))
            answer = self.answer_override or {
                "type": "ANSWER", "seqNum": payload["seqNum"], "result": "ok",
                "answer": {"castMode": "mirroring", "udpPort": self.udp_port, "sendIndexes": [stream["index"]],
                           "ssrcs": [RECEIVER_SSRC], "receiverRtcpEventLog": [0],
                           "constraints": self.constraints or {}}}
            yield NS_WEBRTC, TRANSPORT, {**answer, "seqNum": payload["seqNum"]}
        elif namespace == NS_CONNECTION:
            return

    def end_app(self) -> None:
        """The TV's own remote stops mirroring: a RECEIVER_STATUS without the app."""
        for writer in self._writers:
            writer.write(encode_message(PLATFORM, "*", NS_RECEIVER, json.dumps(self._status(False))))
