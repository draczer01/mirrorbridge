"""A screen mirroring receiver inside the test process, for MirrorBridge's sender to talk to.

It pairs and verifies with pyatv's own server side (MIT: the PIN is pyatv's 1111, and
pyatv.auth.server_auth.CLIENT_CREDENTIALS verifies without pairing), then plays a TV of
another brand: GET /info, SETUP in the shapes its TvProfile takes, the event channel, its own
NTP requests to the sender's timing port, RECORD, /feedback, TEARDOWN, and a video connection
whose frames it decrypts and turns back into an H.264 stream FFmpeg can decode. Its TvProfile
also makes it slow, forgetful or stubborn in the ways real TVs can be.

It reads the wire on its own (struct offsets, HKDF with pyatv's hkdf_expand), not with
mirrorbridge.mirror.packets, so that the two sides check each other.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import plistlib
import socket
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from pyatv.auth.hap_channel import AbstractHAPChannel
from pyatv.auth.hap_session import HAPSession
from pyatv.auth.hap_srp import hkdf_expand
from pyatv.auth.hap_tlv8 import ErrorCode, TlvValue, read_tlv, write_tlv
from pyatv.auth.server_auth import CLIENT_CREDENTIALS, PIN_CODE
from pyatv.protocols.airplay.server_auth import AirPlayServerAuth
from pyatv.support.chacha20 import Chacha20Cipher8byteNonce
from pyatv.support.http import BasicHttpServer, HttpRequest, HttpResponse, http_server, parse_response

from mirrorbridge.mirror.control import Target

_LOGGER = logging.getLogger(__name__)

PIN = PIN_CODE
CREDENTIALS = CLIENT_CREDENTIALS
NTP_EPOCH = 2208988800


@dataclass
class TvProfile:
    """How the fake TV behaves. The defaults are a TCL 55QM7L (Google TV, AirPlay 2)."""

    name: str = "TCL 55QM7L"
    model: str = "55QM7L"
    features: int = (0x38BCF46 << 32) | 0x7F8AD0  # bit 14 clear (no FairPlay), bit 41 (PTP) set
    srcvers: str = "377.40.00"
    width: int = 1920
    height: int = 1080
    max_fps: int = 60
    refuse: tuple[str, ...] = ("control-first",)  # SETUP orderings answered with 400
    audio_first: bool = True  # a video stream before a type-96 audio stream: 400
    ptp_clock_id: Optional[int] = None  # PTP asked for: a timingPeerInfo with this ClockID (None: without)
    event_port: bool = True
    timing_port: bool = True  # name its own timing port in the SETUP answer
    send_ntp: bool = True  # ask the sender for the time
    ntp_interval: float = 0.2
    drop_video_after: Optional[int] = None  # close the video connection after this many frames
    want_trailer: Optional[bytes] = None  # close it at a codec packet whose avcC does not end so
    refuse_video_connection: bool = False
    m2_error: bool = False  # pair-verify M2 carries an error
    m2_garbage: bool = False  # pair-verify M2 does not decrypt
    m4_error: bool = False  # pair-verify M4 carries an error (a TV that dropped the pairing)
    verify_delay: float = 0.0  # before answering pair-verify M1
    info_delay: float = 0.0  # before answering GET /info
    displays_before_setup: bool = True  # False: GET /info names no displays until a session exists
    setup_delay: float = 0.0  # before answering each SETUP
    close_after_refusal: bool = False  # close the connection right after refusing a SETUP ordering
    skip_record: bool = False  # say skipRecord in the SETUP answer
    event_port_delay: float = 0.0  # name the event port this long before listening on it
    video_buffer: Optional[int] = None  # SO_RCVBUF of the video connection (None: the system's)
    read_kbps: Optional[float] = None  # read the video no faster than this
    stall_after: Optional[int] = None  # stop reading the video after this many frames, keeping it open
    pause_after: Optional[int] = None  # once, after this many frames, read no video for pause_seconds
    pause_seconds: float = 0.6  # (as Wi-Fi holds the video up for a moment)


TCL = TvProfile()


@dataclass
class VideoReceived:
    """What came over one video connection."""

    stream_id: int
    codec_packets: list[tuple[int, float, float, bytes]] = field(default_factory=list)  # (ts, w, h, avcC)
    frames: list[tuple[int, bool, list[int]]] = field(default_factory=list)  # (ts, IDR flag, NAL types)
    arrived: list[int] = field(default_factory=list)  # when each frame came, time.perf_counter_ns()
    codec_subtypes: list[bytes] = field(default_factory=list)  # [6:8] of each codec packet
    paused_at: Optional[int] = None  # frames that had come when the TV paused (TvProfile.pause_after)
    heartbeats: int = 0
    annexb: bytearray = field(default_factory=bytearray)
    errors: list[str] = field(default_factory=list)
    closed_by_us: bool = False


def _avcc_params(record: bytes) -> tuple[list[bytes], list[bytes], bytes]:
    """SPS, PPS and what follows them in an avcC record."""
    pos = 6
    count = record[5] & 0x1F
    sps = []
    for _ in range(count):
        size = struct.unpack_from(">H", record, pos)[0]
        sps.append(record[pos + 2:pos + 2 + size])
        pos += 2 + size
    count = record[pos]
    pos += 1
    pps = []
    for _ in range(count):
        size = struct.unpack_from(">H", record, pos)[0]
        pps.append(record[pos + 2:pos + 2 + size])
        pos += 2 + size
    return sps, pps, record[pos:]


class _EventSender(AbstractHAPChannel):
    """The TV's end of the event channel: says something once and expects a 200."""

    def __init__(self, tv: "FakeMirrorTV", output_key: bytes, input_key: bytes) -> None:
        super().__init__(output_key, input_key)
        self.tv = tv

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        body = plistlib.dumps({"type": "updateInfo", "value": {"screenStreamState": "ready"}},
                              fmt=plistlib.FMT_BINARY)
        self.send(b"POST /command RTSP/1.0\r\nCSeq: 1\r\nContent-Type: application/x-apple-binary-plist\r\n"
                  + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    def handle_received(self) -> None:
        response, rest = parse_response(self.buffer)
        if response is not None:
            self.buffer = rest
            self.tv.event_answers.append(response.code)


class _NtpClient(asyncio.DatagramProtocol):
    """The TV asking the sender for the time, and checking the answers."""

    def __init__(self, tv: "FakeMirrorTV") -> None:
        self.tv = tv
        self.transport = None
        self.sent: dict[bytes, int] = {}

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) >= 2 and data[1] == 0xD2:
            self.tv.ntp_probes += 1  # the sender's requests to our timing port
            return
        if len(data) != 32 or data[0] != 0x80 or data[1] != 0xD3:
            self.tv.ntp_bad.append(f"not an answer: {data.hex()}")
            return
        origin = data[8:16]
        if origin not in self.sent:
            self.tv.ntp_bad.append(f"origin {origin.hex()} is not one we sent")
            return
        received, transmitted = struct.unpack_from(">QQ", data, 16)
        if received >> 32 < NTP_EPOCH or transmitted < received:
            self.tv.ntp_bad.append(f"times {received:#x} {transmitted:#x}")
            return
        self.tv.ntp_answers.append((received, transmitted))

    def ask(self, addr) -> None:
        stamp = struct.pack(">Q", time.monotonic_ns() & 0xFFFFFFFFFFFFFFFF)
        self.sent[stamp] = time.monotonic_ns()
        self.transport.sendto(struct.pack(">BBHI", 0x80, 0xD2, 7, 0) + bytes(16) + stamp, addr)


class _Handler(AirPlayServerAuth):
    """One RTSP connection's requests."""

    def __init__(self, tv: "FakeMirrorTV", conn: "_Connection") -> None:
        super().__init__(tv.profile.name)
        self.tv = tv
        self.conn = conn
        self.mirror: dict = {}
        self.audio = False
        self.tasks: list[asyncio.Task] = []
        self.servers: list[asyncio.AbstractServer] = []
        self.ntp: Optional[_NtpClient] = None
        self.add_route("SETUP", ".*", self.handle_setup)
        self.add_route("RECORD", ".*", self.handle_record)
        self.add_route("POST", "^/feedback$", self.handle_feedback)
        self.add_route("TEARDOWN", ".*", self.handle_teardown)

    def enable_encryption(self, output_key: bytes, input_key: bytes) -> None:
        self.conn.pending_keys = (output_key, input_key)  # after the M4 answer has gone out

    def handle_pair_verify(self, request: HttpRequest):
        body = request.body if isinstance(request.body, bytes) else str(request.body).encode()
        if self.tv.profile.verify_delay and read_tlv(body).get(TlvValue.SeqNo) == b"\x01":
            return asyncio.ensure_future(self._late(self.tv.profile.verify_delay, super().handle_pair_verify, request))
        return super().handle_pair_verify(request)

    def handle_info(self, request: HttpRequest):
        if self.tv.profile.info_delay:
            return asyncio.ensure_future(self._late(self.tv.profile.info_delay, self._info, request))
        return self._info(request)

    @staticmethod
    async def _late(seconds: float, handler, request: HttpRequest) -> HttpResponse:
        await asyncio.sleep(seconds)
        return handler(request)

    def _m1_verify(self, pairing_data):
        if self.tv.profile.m2_error:
            return write_tlv({TlvValue.SeqNo: b"\x02", TlvValue.Error: bytes([ErrorCode.Authentication])})
        answer = super()._m1_verify(pairing_data)
        if self.tv.profile.m2_garbage:
            tlv = read_tlv(answer)
            tlv[TlvValue.EncryptedData] = bytes(b ^ 0x55 for b in tlv[TlvValue.EncryptedData])
            answer = write_tlv(tlv)
        return answer

    def _m3_verify(self, pairing_data):
        if self.tv.profile.m4_error:
            return write_tlv({TlvValue.SeqNo: b"\x04", TlvValue.Error: bytes([ErrorCode.Authentication])})
        return super()._m3_verify(pairing_data)

    def _answer(self, request: HttpRequest, code: int, body: Optional[dict] = None, **headers) -> HttpResponse:
        self.tv.requests.append((request.method, request.path, code))
        hdrs = {"CSeq": request.headers.get("CSeq", "0"), **headers}
        data = b""
        if body is not None:
            data = plistlib.dumps(body, fmt=plistlib.FMT_BINARY)
            hdrs["Content-Type"] = "application/x-apple-binary-plist"
        return HttpResponse("RTSP", "1.0", code, "OK" if code == 200 else "Bad Request", hdrs, data)

    def _info(self, request: HttpRequest) -> HttpResponse:
        p = self.tv.profile
        self.tv.info_encrypted = self.conn.hap.chacha20 is not None
        self.tv.info_requests += 1
        info = {
            "name": p.name,
            "model": p.model,
            "sourceVersion": p.srcvers,
            "features": p.features,
            "supportedFormats": {"screenStream": 0x1C0000, "audioStream": 0x1FFFFFC},
            "statusFlags": 4,
            "deviceID": "AA:BB:CC:DD:EE:FF",
        }
        if p.displays_before_setup or self.mirror:
            info["displays"] = [{"uuid": self.tv.display_id, "widthPixels": p.width, "heightPixels": p.height,
                                 "width": p.width, "height": p.height, "maxFPS": p.max_fps, "features": 30,
                                 "rotation": False}]
        return self._answer(request, 200, info)

    def handle_setup(self, request: HttpRequest):
        return asyncio.ensure_future(self._setup(request))

    async def _setup(self, request: HttpRequest) -> HttpResponse:
        p = self.tv.profile
        if p.setup_delay:
            await asyncio.sleep(p.setup_delay)
        body = plistlib.loads(request.body)
        self.tv.setups.append(body)
        self.tv.user_agents.add(request.headers.get("User-Agent", ""))
        streams = body.get("streams") or []
        reply: dict = {}
        types = [stream.get("type") for stream in streams]
        if "sessionUUID" not in body and not self.mirror:
            return self._answer(request, 455)
        if p.audio_first and 110 in types and not self.audio and 96 not in types[:types.index(110)]:
            return self._answer(request, 400)
        if "sessionUUID" in body:
            ordering = "media-first" if streams else "control-first"
            if ordering in p.refuse or not body.get("isScreenMirroringSession"):
                if p.close_after_refusal:  # a moment after the answer has gone
                    asyncio.get_running_loop().call_later(0.05, self.conn.transport.close)
                return self._answer(request, 400)
            self.mirror = body
            self.tv.orderings.append(ordering)
            if body.get("timingProtocol") == "PTP":
                peer = {"ID": "fe80::1", "Addresses": ["127.0.0.1"], "SupportsClockPortMatchingOverride": True}
                if p.ptp_clock_id is not None:
                    peer["ClockID"] = p.ptp_clock_id
                reply["timingPeerInfo"] = peer
            else:
                loop = asyncio.get_running_loop()
                _, self.ntp = await loop.create_datagram_endpoint(lambda: _NtpClient(self.tv),
                                                                  local_addr=("127.0.0.1", 0))
                if p.timing_port:
                    reply["timingPort"] = self.ntp.transport.get_extra_info("sockname")[1]
                if p.send_ntp:
                    self.tasks.append(asyncio.ensure_future(self._ask_time(body["timingPort"])))
            if p.skip_record:
                reply["skipRecord"] = True
            if p.event_port:
                port = _free_port()
                reply["eventPort"] = port
                if p.event_port_delay:
                    self.tasks.append(asyncio.ensure_future(self._events_later(port, p.event_port_delay)))
                else:
                    await self._events(port)
        answered = []
        for stream in streams:
            if stream.get("type") == 96:
                self.audio = True
                answered.append({"type": 96, "dataPort": 7001, "controlPort": 7002, "streamID": 1})
            elif stream.get("type") == 110:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if p.video_buffer:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, p.video_buffer)
                sock.bind(("127.0.0.1", 0))
                server = await asyncio.start_server(
                    lambda r, w, sid=stream["streamConnectionID"]: self._video(r, w, sid), sock=sock,
                    limit=p.video_buffer or 2 ** 16)  # what the reader holds besides the socket's buffer
                if p.refuse_video_connection:
                    port = server.sockets[0].getsockname()[1]
                    server.close()
                else:
                    self.servers.append(server)
                    port = server.sockets[0].getsockname()[1]
                self.tv.video_streams.append(stream)
                answered.append({"type": 110, "dataPort": port, "streamID": 2})
        if answered:
            reply["streams"] = answered
        return self._answer(request, 200, reply)

    async def _events(self, port: int) -> None:
        server = await asyncio.get_running_loop().create_server(
            lambda: _EventSender(self.tv, *self._keys("Events-Salt", "Events-Write-Encryption-Key",
                                                      "Events-Read-Encryption-Key")), "127.0.0.1", port)
        self.servers.append(server)

    async def _events_later(self, port: int, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self._events(port)

    def _keys(self, salt: str, output_info: str, input_info: str) -> tuple[bytes, bytes]:
        return hkdf_expand(salt, output_info, self.shared_key), hkdf_expand(salt, input_info, self.shared_key)

    async def _ask_time(self, port: int) -> None:
        addr = (self.conn.peer, port)
        while self.ntp is not None:
            self.ntp.ask(addr)
            await asyncio.sleep(self.tv.profile.ntp_interval)

    async def _video(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stream_id: int) -> None:
        received = VideoReceived(stream_id)
        self.tv.videos.append(received)
        key = hkdf_expand(f"DataStream-Salt{stream_id}", "DataStream-Output-Encryption-Key", self.shared_key)
        cipher = Chacha20Cipher8byteNonce(key, key)
        params = b""
        self.tv.video_writers.append(writer)
        p = self.tv.profile
        began = time.perf_counter()
        read = 0
        try:
            while True:
                if p.stall_after is not None and len(received.frames) >= p.stall_after:
                    await asyncio.sleep(3600)  # stuck, with the connection open
                if p.pause_after is not None and received.paused_at is None and len(received.frames) >= p.pause_after:
                    received.paused_at = len(received.frames)
                    await asyncio.sleep(p.pause_seconds)  # held up, then reading at full speed again
                header = await reader.readexactly(128)
                size, kind, flags = struct.unpack_from("<IBB", header)
                timestamp = struct.unpack_from("<Q", header, 8)[0]
                payload = await reader.readexactly(size) if size else b""
                arrived = time.perf_counter_ns()
                read += 128 + size
                if p.read_kbps:  # no faster than that
                    await asyncio.sleep(max(0.0, began + read * 8 / (p.read_kbps * 1000) - time.perf_counter()))
                if kind == 1:
                    width, height = struct.unpack_from("<ff", header, 16)
                    # 0x14: frames shown as they come (RespectTimestamps, bit 0x02, cleared)
                    if header[6:8] not in (b"\x16\x01", b"\x14\x01") or (
                            struct.unpack_from("<ff", header, 40) != (width, height)):
                        received.errors.append(f"codec header {header[:64].hex()}")
                    received.codec_subtypes.append(bytes(header[6:8]))
                    sps, pps, trailer = _avcc_params(payload)
                    params = b"".join(b"\x00\x00\x00\x01" + nal for nal in sps + pps)
                    received.codec_packets.append((timestamp, width, height, payload))
                    want = self.tv.profile.want_trailer
                    if want is not None and trailer != want:
                        received.closed_by_us = True
                        return
                elif kind == 0:
                    plain = cipher.decrypt(payload, aad=header)
                    types, pos = [], 0
                    out = bytearray()
                    while pos < len(plain):
                        size = struct.unpack_from(">I", plain, pos)[0]
                        nal = plain[pos + 4:pos + 4 + size]
                        pos += 4 + size
                        types.append(nal[0] & 0x1F)
                        out += b"\x00\x00\x00\x01" + nal
                    idr = bool(flags & 0x10)
                    if idr != (5 in types):
                        received.errors.append(f"IDR flag {idr} on NAL units {types}")
                    if received.frames and timestamp <= received.frames[-1][0]:
                        received.errors.append(f"timestamp {timestamp:#x} after {received.frames[-1][0]:#x}")
                    received.frames.append((timestamp, idr, types))
                    received.arrived.append(arrived)
                    received.annexb += (params if idr else b"") + out
                    drop = self.tv.profile.drop_video_after
                    if drop is not None and len(received.frames) >= drop:
                        received.closed_by_us = True
                        writer.close()
                        return
                elif kind == 2:
                    received.heartbeats += 1
                else:
                    received.errors.append(f"packet type {kind}")
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as ex:  # pylint: disable=broad-except
            received.errors.append(repr(ex))
        finally:
            writer.close()

    def handle_record(self, request: HttpRequest):
        if request.headers.get("Session") != self.mirror.get("sessionUUID"):
            return self._answer(request, 454)
        self.tv.records += 1
        return self._answer(request, 200, None, **{"Audio-Latency": "0"})

    def handle_feedback(self, request: HttpRequest):
        return self._answer(request, 200)  # with nothing in it, as TCL's TVs answer

    def handle_teardown(self, request: HttpRequest):
        self.tv.teardowns += 1
        self.close()
        return self._answer(request, 200)

    def close(self) -> None:
        self.mirror = {}
        self.audio = False
        for task in self.tasks:
            task.cancel()
        self.tasks.clear()
        for server in self.servers:
            server.close()
        self.servers.clear()
        if self.ntp is not None:
            self.ntp.transport.close()
            self.ntp = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Connection(BasicHttpServer):
    def __init__(self, tv: "FakeMirrorTV") -> None:
        self.tv = tv
        self.hap = HAPSession()
        self.pending_keys: Optional[tuple[bytes, bytes]] = None
        self.peer = "127.0.0.1"
        self.closed = False
        super().__init__(_Handler(tv, self))

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        self.peer = transport.get_extra_info("peername")[0]
        self.tv.connections.append(self)

    def connection_lost(self, exc) -> None:
        self.closed = True
        self.handler.close()

    def process_received(self, data: bytes) -> bytes:
        return self.hap.decrypt(data)

    def process_sent(self, data: bytes) -> bytes:
        return self.hap.encrypt(data)

    def _send_response(self, resp: HttpResponse) -> None:
        super()._send_response(resp)
        if self.pending_keys is not None:
            self.hap.enable(*self.pending_keys)
            self.pending_keys = None


class FakeMirrorTV:
    def __init__(self, profile: TvProfile = TCL) -> None:
        self.profile = profile
        self.port = 0
        self.server: Optional[asyncio.AbstractServer] = None
        self.connections: list[_Connection] = []
        self.requests: list[tuple[str, str, int]] = []  # (method, path, status) of every answered request
        self.setups: list[dict] = []
        self.orderings: list[str] = []
        self.video_streams: list[dict] = []
        self.videos: list[VideoReceived] = []
        self.event_answers: list[int] = []
        self.ntp_answers: list[tuple[int, int]] = []
        self.ntp_bad: list[str] = []
        self.ntp_probes = 0
        self.records = 0
        self.teardowns = 0
        self.info_encrypted: Optional[bool] = None
        self.info_requests = 0
        self.user_agents: set[str] = set()
        self.video_writers: list[asyncio.StreamWriter] = []
        self.display_id = str(uuid.uuid4()).upper()  # a display's own UUID, as each TV makes one up

    async def start(self) -> "FakeMirrorTV":
        self.server, self.port = await http_server(lambda: _Connection(self), "127.0.0.1")
        return self

    async def stop(self) -> None:
        for conn in self.connections:
            conn.handler.close()
            if conn.transport is not None:
                conn.transport.close()
        if self.server is not None:
            self.server.close()

    def end_mirroring(self) -> None:
        """Someone ends mirroring on the TV: it closes its control and video connections at once."""
        for conn in self.connections:
            if conn.transport is not None:
                conn.transport.close()
        for writer in self.video_writers:
            writer.close()

    def reconfigure(self, **changes) -> None:
        self.profile = dataclasses.replace(self.profile, **changes)

    def target(self, credentials: str = CREDENTIALS, identifier: str = "fake-tv") -> Target:
        return Target(self.profile.name, "127.0.0.1", self.port, credentials, self.profile.features,
                      self.profile.srcvers, identifier)

    def codes(self, method: str) -> list[int]:
        return [code for m, _, code in self.requests if m == method]
