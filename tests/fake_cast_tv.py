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
AUDIO_RECEIVER_SSRC = 27182


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


class _Stream:
    """One stream's frames as the receiver puts them together."""

    def __init__(self, receiver_ssrc: int, sender_ssrc: int, video: bool) -> None:
        self.receiver_ssrc = receiver_ssrc
        self.sender_ssrc = sender_ssrc
        self.video = video
        self.frames: list[tuple[int, bool, bytes]] = []
        self.rtp_times: list[int] = []
        self.acked = -1
        self.parts: dict[int, dict[int, bytes]] = {}  # frame id -> packet id -> payload
        self.last_packet: dict[int, int] = {}
        self.keyframes: dict[int, bool] = {}
        self.times: dict[int, int] = {}
        self.seen: set[tuple[int, int]] = set()
        self.count = 0
        self.newest = -1
        self.logged = -1


class _Video(asyncio.DatagramProtocol):
    def __init__(self, tv: "FakeCastTV") -> None:
        self.tv = tv
        self.transport = None
        self.sender_addr = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.sender_addr = addr
        tv = self.tv
        if len(data) >= 8 and 194 <= data[1] <= 210:
            if data[1] == PT_SR:
                ssrc = struct.unpack_from(">I", data, 4)[0]
                (tv.sender_reports if ssrc == tv.offer_ssrc else tv.audio_reports).append(
                    struct.unpack_from(">IQI", data, 4))
            return
        if len(data) < 19 or data[0] != 0x80:
            return
        payload_type = data[1] & 0x7F
        ssrc = struct.unpack_from(">I", data, 8)[0]
        stream = tv.streams.get(ssrc)
        if stream is None:
            return
        tv.payload_types.add(payload_type)
        rtp_time = struct.unpack_from(">I", data, 4)[0]
        byte0, frame8, packet_id, last_id, _ref = struct.unpack_from(">BBHHB", data, 12)
        frame_id = expand(frame8, max(stream.newest, 0))
        stream.count += 1
        if stream.video and tv.drop_every and stream.count % tv.drop_every == 0 \
                and (frame_id, packet_id) not in stream.seen:
            stream.seen.add((frame_id, packet_id))
            tv.dropped += 1
            return
        if (frame_id, packet_id) in stream.seen and (frame_id, packet_id) not in stream.parts.get(frame_id, {}):
            tv.resent_got += 1
        stream.seen.add((frame_id, packet_id))
        if frame_id <= stream.acked:
            return
        stream.newest = max(stream.newest, frame_id)
        stream.parts.setdefault(frame_id, {})[packet_id] = data[19:]
        stream.last_packet[frame_id] = last_id
        stream.keyframes[frame_id] = bool(byte0 & 0x80)
        stream.times[frame_id] = rtp_time
        self._complete(stream)
        self.feedback(stream)

    def _complete(self, stream: _Stream) -> None:
        while True:
            frame_id = stream.acked + 1
            parts = stream.parts.get(frame_id)
            if parts is None or len(parts) != stream.last_packet[frame_id] + 1:
                return
            data = b"".join(parts[i] for i in range(len(parts)))
            stream.frames.append((frame_id, stream.keyframes[frame_id], self.tv.cipher.apply(frame_id, data)))
            stream.rtp_times.append(stream.times.pop(frame_id))
            del stream.parts[frame_id]
            stream.acked = frame_id

    def feedback(self, stream: _Stream) -> None:
        if self.transport is None or self.sender_addr is None or self.tv.silent:
            return
        tv = self.tv
        losses = []
        for frame_id in range(stream.acked + 1, stream.newest + 1):
            got = stream.parts.get(frame_id)
            if not got:
                losses.append((frame_id & 0xFF, ALL_PACKETS, 0))
                continue
            for packet_id in range(stream.last_packet[frame_id] + 1):
                if packet_id not in got and (packet_id < max(got) or frame_id < stream.newest):
                    losses.append((frame_id & 0xFF, packet_id, 0))
        body = struct.pack(">II4sBBH", stream.receiver_ssrc, stream.sender_ssrc, CAST, stream.acked & 0xFF,
                           len(losses), tv.delay_ms)
        body += b"".join(struct.pack(">BHB", *loss) for loss in losses)
        body += b"\x00" * (-len(body) % 4)
        report = struct.pack(">BBHI", 0x80, PT_RR, 1, stream.receiver_ssrc)
        psfb = struct.pack(">BBH", 0x80 | 15, PT_PSFB, len(body) // 4) + body
        app = b""
        if tv.show_log and stream.acked > stream.logged:
            # The receiver's event log: the frames completed since the last one, decoded and shown
            # with 50 ms to spare.
            items = b"".join(struct.pack(">IIhHhH", frame_id * 3000, 1 << 24, 0, 13 << 12, 50, 12 << 12)
                             for frame_id in range(stream.logged + 1, stream.acked + 1))
            stream.logged = stream.acked
            app_body = struct.pack(">I4s", stream.receiver_ssrc, CAST) + items
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
        self.streams: dict[int, _Stream] = {}  # by the sender's SSRC
        self.sender_reports: list = []
        self.audio_reports: list = []
        self.payload_types: set[int] = set()
        self.take_audio = True
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

    def _stream(self, video: bool) -> Optional[_Stream]:
        return next((stream for stream in self.streams.values() if stream.video == video), None)

    @property
    def frames(self) -> list[tuple[int, bool, bytes]]:
        stream = self._stream(True)
        return stream.frames if stream else []

    @property
    def acked(self) -> int:
        stream = self._stream(True)
        return stream.acked if stream else -1

    @property
    def video(self) -> Optional[_Stream]:
        return self._stream(True)

    @property
    def audio(self) -> Optional[_Stream]:
        return self._stream(False)

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
            offered = payload["offer"]["supportedStreams"]
            stream = next(s for s in offered if s["type"] == "video_source")
            self.offer_ssrc = stream["ssrc"]
            self.delay_ms = stream["targetDelay"]
            self.cipher = FrameCipher(bytes.fromhex(stream["aesKey"]), bytes.fromhex(stream["aesIvMask"]))
            indexes, ssrcs = [stream["index"]], [RECEIVER_SSRC]
            self.streams = {stream["ssrc"]: _Stream(RECEIVER_SSRC, stream["ssrc"], True)}
            sound = next((s for s in offered if s["type"] == "audio_source"), None)
            if sound is not None and self.take_audio:
                indexes.append(sound["index"])
                ssrcs.append(AUDIO_RECEIVER_SSRC)
                self.streams[sound["ssrc"]] = _Stream(AUDIO_RECEIVER_SSRC, sound["ssrc"], False)
            answer = self.answer_override or {
                "type": "ANSWER", "seqNum": payload["seqNum"], "result": "ok",
                "answer": {"castMode": "mirroring", "udpPort": self.udp_port, "sendIndexes": indexes,
                           "ssrcs": ssrcs, "receiverRtcpEventLog": [0], "constraints": self.constraints or {}}}
            yield NS_WEBRTC, TRANSPORT, {**answer, "seqNum": payload["seqNum"]}
        elif namespace == NS_CONNECTION:
            return

    def end_app(self) -> None:
        """The TV's own remote stops mirroring: a RECEIVER_STATUS without the app."""
        for writer in self._writers:
            writer.write(encode_message(PLATFORM, "*", NS_RECEIVER, json.dumps(self._status(False))))
