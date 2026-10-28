"""Experimental PTP timing for screen mirroring, with MirrorBridge as the TV's grandmaster clock.

Under NTP timing (clock.py) the TV asks the PC for the time every couple of seconds. Under PTP,
as AirPlay 2 uses it (IEEE 1588 over UDP, unicast and two-step, event messages on port 319 and
general ones on 320), the PC announces itself as the best clock around and sends the time 8
times a second (Sync, then Follow_Up with the time the Sync left); a TV that follows it asks how
long the way is (Delay_Req, answered at once with Delay_Resp and the time the request came).
PTP time here is the session clock itself (clock.now_ns()) as 48 bits of seconds and 32 of
nanoseconds, the same instant clock.to_ntp() puts in a video header, so frame timestamps stay
as they are: only the timeline a frame is on ([40:48] of its header, packets.py) becomes our
ClockIdentity.

This is a prototype, to learn whether a TV mirrors differently under PTP (mirror --timing ptp
or MIRRORBRIDGE_TIMING=ptp, session.py). Its log is the point: what the TV sends and from which
ports, whether it follows our clock or wants to be the grandmaster itself, and how steadily our
side keeps time (summary()).

The messages are laid out as OwnTone's libairptp lays them out (MIT, Copyright (c) 2026
OwnTone): the same sizes, flags, port number and TLVs. Only one program on a PC can own UDP 319
and 320, so there is one PtpNode per process, which sessions share (acquire, release), each
adding its TV as a peer. It runs in threads of its own, not on the session's event loop: a
Delay_Req is stamped the moment recvfrom returns it, and the Syncs leave on time however busy
the loop is.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import math
import os
import secrets
import socket
import struct
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional

from .clock import NS, now_ns
from .stats import percentile

_LOGGER = logging.getLogger(__name__)

EVENT_PORT = 319
GENERAL_PORT = 320
PORTS = (EVENT_PORT, GENERAL_PORT)  # ours (tests use others: Linux wants root below 1024)
PEER_PORTS = (EVENT_PORT, GENERAL_PORT)  # the TV's

# Message types: the low nibble of byte 0.
SYNC, DELAY_REQ, PDELAY_REQ, PDELAY_RESP = 0x0, 0x1, 0x2, 0x3
FOLLOW_UP, DELAY_RESP, PDELAY_RESP_FOLLOW_UP, ANNOUNCE, SIGNALING, MANAGEMENT = 0x8, 0x9, 0xA, 0xB, 0xC, 0xD
NAMES = {SYNC: "Sync", DELAY_REQ: "Delay_Req", PDELAY_REQ: "Pdelay_Req", PDELAY_RESP: "Pdelay_Resp",
         FOLLOW_UP: "Follow_Up", DELAY_RESP: "Delay_Resp", PDELAY_RESP_FOLLOW_UP: "Pdelay_Resp_Follow_Up",
         ANNOUNCE: "Announce", SIGNALING: "Signaling", MANAGEMENT: "Management"}
UNREADABLE = -1  # a packet too short for a header, counted under this
_CONTROL = {SYNC: 0, DELAY_REQ: 1, FOLLOW_UP: 2, DELAY_RESP: 3}  # controlField; 5 for every other type

TIMESCALE, TWO_STEP, UNICAST = 1 << 3, 1 << 9, 1 << 10
EVENT_FLAGS = UNICAST | TWO_STEP | TIMESCALE  # 0x0608, as iOS sends Sync and Delay_Resp
GENERAL_FLAGS = UNICAST | TIMESCALE  # 0x0408

TRANSPORT_SPECIFIC = 1  # the high nibble of byte 0, as receivers expect it
VERSION = 2
DOMAIN = 0
PORT_NUMBER = 0x8005  # the port number of our sourcePortIdentity, the same as iOS's
HEADER = struct.Struct(">BBHBBHqI8sHHBb")  # 34 bytes
TIMESTAMP = struct.Struct(">HII")  # 48 bits of seconds, 32 of nanoseconds
TLV = struct.Struct(">HH")  # type, length
ORG_EXTENSION, PATH_TRACE = 0x0003, 0x0008
IEEE_8021 = bytes.fromhex("0080c2")
APPLE = bytes.fromhex("000d93")
SIZES = {SYNC: 44, DELAY_REQ: 44, PDELAY_REQ: 54, PDELAY_RESP: 54, FOLLOW_UP: 96, DELAY_RESP: 54,
         PDELAY_RESP_FOLLOW_UP: 54, ANNOUNCE: 76, SIGNALING: 106}

SYNC_NS = NS // 8  # Sync and Follow_Up every 125 ms (logMessageInterval -3), as iOS and Apple TVs
SYNC_LOG_INTERVAL = -3
SIGNALING_NS = NS
SIGNALING_LOG_INTERVAL = -128
LATE_NS = 5_000_000  # a Sync leaving this much after its time counts as late
RECEIVE_TIMEOUT = 0.1  # how long a receiving thread blocks before it looks whether to stop
RECEIVE_BYTES = 2048
KEEP = 20000  # samples kept for the totals (40 minutes of Delay_Req at 8 a second)

IN_USE, BLOCKED, FAILED = "in use", "blocked", "failed"


def announce_ms_from_env() -> int:
    """MIRRORBRIDGE_PTP_ANNOUNCE_MS, for an experiment: how often to Announce (125-8000 ms).
    1000 unless set, as OwnTone and nqptp; Apple TVs and HomePods announce every 250."""
    try:
        return min(8000, max(125, int(os.environ["MIRRORBRIDGE_PTP_ANNOUNCE_MS"])))
    except (KeyError, ValueError):
        return 1000


def signaling_from_env() -> str:
    """MIRRORBRIDGE_PTP_SIGNALING: "off" sends no Signaling, "apple" the values seen from Apple
    devices; anything else OwnTone's ("owntone")."""
    value = os.environ.get("MIRRORBRIDGE_PTP_SIGNALING", "").strip().lower()
    return value if value in ("off", "apple") else "owntone"


def lock_seconds_from_env() -> float:
    """MIRRORBRIDGE_PTP_LOCK_S: how long the TV has to follow our clock (0.5-30 s, 3 unless set)."""
    try:
        return min(30.0, max(0.5, float(os.environ["MIRRORBRIDGE_PTP_LOCK_S"])))
    except (KeyError, ValueError):
        return 3.0


# -- identity and time ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockIdentity:
    """Our PTP clockIdentity: 8 random bytes, new each time MirrorBridge starts (QPC starts again
    from 0 when the PC does, and a TV may hold on to a grandmaster it saw for minutes). The top
    bit is clear, so that the SETUP's plist writes the int in 8 bytes (plistlib writes 2^63
    and up in 16). It goes in PTP messages as `wire` (big-endian), in SETUP as int(), and in
    video headers as that int, little-endian (packets.py)."""

    value: int

    @classmethod
    def new(cls) -> "ClockIdentity":
        while True:
            value = secrets.randbits(63)
            if value:  # timeline 0 is NTP's
                return cls(value)

    @property
    def wire(self) -> bytes:
        return self.value.to_bytes(8, "big")

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return f"{self.value:016x}"


CLOCK_IDENTITY = ClockIdentity.new()


def timestamp(ns: int) -> bytes:
    """`ns` of the session clock as a PTP timestamp (10 bytes)."""
    seconds, rest = divmod(max(0, ns), NS)
    return TIMESTAMP.pack(seconds >> 32 & 0xFFFF, seconds & 0xFFFFFFFF, rest)


def read_timestamp(data: bytes, offset: int) -> int:
    high, low, nanoseconds = TIMESTAMP.unpack_from(data, offset)
    return ((high << 32) | low) * NS + nanoseconds


# -- the messages ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    kind: int
    length: int
    flags: int
    correction: int
    clock_id: int
    port_number: int
    sequence: int
    control: int
    log_interval: int
    transport: int = TRANSPORT_SPECIFIC
    version: int = VERSION
    domain: int = DOMAIN
    port_identity: bytes = b""  # clockIdentity and portNumber as they came (10 bytes)


def header(kind: int, length: int, identity: ClockIdentity, sequence: int, log_interval: int, flags: int) -> bytes:
    return HEADER.pack(TRANSPORT_SPECIFIC << 4 | kind, VERSION, length, DOMAIN, 0, flags, 0, 0, identity.wire,
                       PORT_NUMBER, sequence & 0xFFFF, _CONTROL.get(kind, 5), log_interval)


def parse_header(data: bytes) -> Header:
    if len(data) < HEADER.size:
        raise ValueError(f"a PTP header is {HEADER.size} bytes, not {len(data)}")
    (first, version, length, domain, _, flags, correction, _, clock, port, sequence, control,
     log_interval) = HEADER.unpack_from(data)
    return Header(first & 0x0F, length, flags, correction, int.from_bytes(clock, "big"), port, sequence, control,
                  log_interval, first >> 4, version & 0x0F, domain, bytes(data[20:30]))


def _tlv(kind: int, value: bytes) -> bytes:
    return TLV.pack(kind, len(value)) + value


def tlvs(data: bytes, offset: int) -> list[tuple[int, bytes]]:
    """The TLVs from `offset` on, as (type, value), as far as they are whole."""
    found = []
    while offset + TLV.size <= len(data):
        kind, length = TLV.unpack_from(data, offset)
        offset += TLV.size
        if offset + length > len(data):
            break
        found.append((kind, bytes(data[offset:offset + length])))
        offset += length
    return found


def sync(identity: ClockIdentity, sequence: int) -> bytes:
    """Two-step: originTimestamp 0, as libairptp and Apple devices send it; Follow_Up has the time."""
    return header(SYNC, 44, identity, sequence, SYNC_LOG_INTERVAL, EVENT_FLAGS) + timestamp(0)


def follow_up(identity: ClockIdentity, sequence: int, sent_ns: int) -> bytes:
    """When the Sync of `sequence` left, with 802.1AS's Follow_Up information TLV (all zeros:
    no rate or phase change known) and Apple's TLV with our clock identity."""
    return (header(FOLLOW_UP, 96, identity, sequence, SYNC_LOG_INTERVAL, GENERAL_FLAGS) + timestamp(sent_ns)
            + _tlv(ORG_EXTENSION, IEEE_8021 + b"\x00\x00\x01" + bytes(22))
            + _tlv(ORG_EXTENSION, APPLE + b"\x00\x00\x04" + identity.wire + b"\x00\x00"))


def delay_resp(identity: ClockIdentity, request: Header, received_ns: int) -> bytes:
    """The answer to a Delay_Req: when it came, for whom (its sourcePortIdentity) and its sequenceId."""
    return (header(DELAY_RESP, 54, identity, request.sequence, SYNC_LOG_INTERVAL, EVENT_FLAGS)
            + timestamp(received_ns) + request.port_identity)


def pdelay_resp(identity: ClockIdentity, request: Header, received_ns: int) -> bytes:
    return (header(PDELAY_RESP, 54, identity, request.sequence, SYNC_LOG_INTERVAL, EVENT_FLAGS)
            + timestamp(received_ns) + request.port_identity)


def pdelay_resp_follow_up(identity: ClockIdentity, request: Header, sent_ns: int) -> bytes:
    return (header(PDELAY_RESP_FOLLOW_UP, 54, identity, request.sequence, SYNC_LOG_INTERVAL, GENERAL_FLAGS)
            + timestamp(sent_ns) + request.port_identity)


@dataclass(frozen=True)
class Dataset:
    """What an Announce says about its grandmaster, the part that decides who is the best clock."""

    priority1: int
    clock_class: int
    accuracy: int
    variance: int  # offsetScaledLogVariance
    priority2: int
    identity: int
    steps_removed: int = 0
    time_source: int = 0
    utc_offset: int = 0

    def key(self) -> tuple[int, ...]:
        return self.priority1, self.clock_class, self.accuracy, self.variance, self.priority2, self.identity

    def describe(self) -> str:
        return (f"grandmaster {self.identity:016x}, priority1 {self.priority1}, class {self.clock_class}, "
                f"accuracy {self.accuracy:#04x}, variance {self.variance:#06x}, priority2 {self.priority2}, "
                f"steps removed {self.steps_removed}, time source {self.time_source:#04x}")


def our_dataset(identity: ClockIdentity) -> Dataset:
    """As libairptp announces itself, to be the best clock: priority 128, class 6 (GPS),
    accuracy 0x21 (100 ns), Apple's variance 0x436A, time source GPS; UTC offset 37 s as Apple
    devices send it."""
    return Dataset(128, 6, 0x21, 0x436A, 128, identity.value, 0, 0x20, 37)


def announce(identity: ClockIdentity, sequence: int, log_interval: int = 0,
             dataset: Optional[Dataset] = None) -> bytes:
    """Our Announce, with the path-trace TLV (our clock identity again) that iOS adds."""
    ds = dataset or our_dataset(identity)
    return (header(ANNOUNCE, 76, identity, sequence, log_interval, GENERAL_FLAGS) + timestamp(0)
            + struct.pack(">hBBBBHB8sHB", ds.utc_offset, 0, ds.priority1, ds.clock_class, ds.accuracy, ds.variance,
                          ds.priority2, ds.identity.to_bytes(8, "big"), ds.steps_removed, ds.time_source)
            + _tlv(PATH_TRACE, identity.wire))


def parse_announce(data: bytes) -> Dataset:
    if len(data) < 64:
        raise ValueError(f"an Announce is at least 64 bytes, not {len(data)}")
    (utc_offset, _, priority1, clock_class, accuracy, variance, priority2, identity, steps,
     source) = struct.unpack_from(">hBBBBHB8sHB", data, 44)
    return Dataset(priority1, clock_class, accuracy, variance, priority2, int.from_bytes(identity, "big"), steps,
                   source, utc_offset)


def compare(theirs: Dataset, ours: Dataset) -> int:
    """IEEE 1588's comparison of two grandmasters: priority1, clockClass, clockAccuracy,
    offsetScaledLogVariance, priority2, then clockIdentity, the lower winning. < 0 when
    `theirs` is the better one, 0 when it is the same grandmaster, > 0 when ours is better."""
    if theirs.identity == ours.identity:
        return 0
    return -1 if theirs.key() < ours.key() else 1


def verdict(theirs: Dataset, ours: Dataset) -> str:
    result = compare(theirs, ours)
    if result < 0:
        return "better than ours: it wants to be the grandmaster"
    if result == 0:
        return "our own clock, passed on"
    return "not as good as ours: it should follow our clock"


# Signaling's two Apple TLVs (subtypes 1 and 5). Nobody knows what they mean: OwnTone sends
# 00 00 03 01 and zeros, Apple devices 00 04 03 01 / 00 10 03 01 ending in 20 6f 2f.
_SIGNALING_VALUES = {
    "owntone": (b"\x00\x00\x03\x01" + bytes(12), b"\x00\x00\x03\x01" + bytes(22)),
    "apple": (b"\x00\x04\x03\x01" + bytes(9) + b"\x20\x6f\x2f", b"\x00\x10\x03\x01" + bytes(19) + b"\x20\x6f\x2f"),
}


def signaling(identity: ClockIdentity, sequence: int, variant: str = "owntone", target: bytes = bytes(10)) -> bytes:
    first, second = _SIGNALING_VALUES.get(variant, _SIGNALING_VALUES["owntone"])
    return (header(SIGNALING, 106, identity, sequence, SIGNALING_LOG_INTERVAL, GENERAL_FLAGS) + target
            + _tlv(ORG_EXTENSION, APPLE + b"\x00\x00\x01" + first)
            + _tlv(ORG_EXTENSION, APPLE + b"\x00\x00\x05" + second))


def describe(data: bytes) -> str:
    """A message's key fields, for the log."""
    try:
        head = parse_header(data)
    except ValueError as ex:
        return str(ex)
    parts = [f"{len(data)} bytes", f"flags {head.flags:#06x}", f"clock {head.clock_id:016x} port {head.port_number:#06x}",
             f"sequence {head.sequence}", f"interval {head.log_interval}", f"control {head.control}"]
    if (head.transport, head.version, head.domain) != (TRANSPORT_SPECIFIC, VERSION, DOMAIN):
        parts.append(f"transportSpecific {head.transport}, version {head.version}, domain {head.domain}")
    if head.correction:
        parts.append(f"correction {head.correction}")
    try:
        if head.kind == ANNOUNCE:
            parts.append(parse_announce(data).describe())
        elif head.kind in (SYNC, DELAY_REQ, FOLLOW_UP, DELAY_RESP, PDELAY_REQ, PDELAY_RESP, PDELAY_RESP_FOLLOW_UP):
            parts.append(f"time {read_timestamp(data, 34) / NS:.6f} s")
        if head.kind in (DELAY_RESP, PDELAY_RESP, PDELAY_RESP_FOLLOW_UP) and len(data) >= 54:
            parts.append(f"for {data[44:54].hex()}")
        start = {FOLLOW_UP: 44, SIGNALING: 44, ANNOUNCE: 64}.get(head.kind)
        if start is not None:
            for kind, value in tlvs(data, start):
                parts.append(f"TLV {kind:#06x} {value.hex()}")
    except (struct.error, ValueError):
        parts.append("cut short")
    return ", ".join(parts)


# -- the node --------------------------------------------------------------------------------------


class PtpUnavailable(Exception):
    """UDP 319 or 320 cannot be had: `kind` is IN_USE (another program has it), BLOCKED (not
    allowed: a Hyper-V or WinNAT excluded range on Windows, no root on Linux) or FAILED."""

    def __init__(self, port: int, kind: str, detail: str) -> None:
        super().__init__(f"UDP {port}: {kind} ({detail})")
        self.port = port
        self.kind = kind
        self.detail = detail

    def explain(self) -> str:
        if self.kind == IN_USE:
            return f"UDP {self.port} is in use by another program ({self.detail})"
        if self.kind == BLOCKED:
            return (f"UDP {self.port} may not be used ({self.detail}; on Windows a reserved port range, see "
                    f"netsh interface ipv4 show excludedportrange protocol=udp)")
        return f"UDP {self.port} could not be opened ({self.detail})"


def bind_failure(ex: OSError) -> str:
    """IN_USE, BLOCKED or FAILED, for why binding failed."""
    winerror = getattr(ex, "winerror", None)
    if ex.errno == errno.EADDRINUSE or winerror == 10048:  # WSAEADDRINUSE
        return IN_USE
    if isinstance(ex, PermissionError) or ex.errno == errno.EACCES or winerror == 10013:  # WSAEACCES
        return BLOCKED
    return FAILED


def _udp_socket(ip: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Without it, another program's bind to 0.0.0.0:319 may not stop ours to the address
        # (Windows), and the conflict would go unseen. Never SO_REUSEADDR.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # pylint: disable=no-member
        sock.bind((ip, port))
        sock.settimeout(RECEIVE_TIMEOUT)
    except BaseException:
        sock.close()
        raise
    return sock


def _bind(ip: str, port: int) -> socket.socket:
    try:
        return _udp_socket(ip, port)
    except OSError as ex:
        raise PtpUnavailable(port, bind_failure(ex), f"{type(ex).__name__}: {ex}") from ex


@dataclass
class _Peer:
    ip: str
    event_port: int
    general_port: int
    added_ns: int = field(default_factory=now_ns)
    refs: int = 1
    locked: threading.Event = field(default_factory=threading.Event)
    lock_ns: Optional[int] = None
    waiters: list = field(default_factory=list)  # (loop, asyncio.Event) of wait_locked()
    next_announce: int = 0  # at the next tick, then every announce interval
    next_signaling: int = 0
    announce_sequence: int = 0
    signaling_sequence: int = 0
    last_sync_ns: Optional[int] = None  # when our last Sync to it left
    delay_req_ports: Counter = field(default_factory=Counter)  # its source ports, with how many came
    dataset: Optional[Dataset] = None  # what it announced last
    better_since: Optional[int] = None  # since when it announces a better grandmaster than ours
    better_last: int = 0
    better_hold_ns: int = 3 * NS  # after this without such an Announce, it is taken to have stopped


class _Counts:
    """Messages per type, sent and received, for a window and in all; Delay_Req lag; Sync cadence."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent: Counter = Counter()
        self.received: Counter = Counter()
        self.errors: Counter = Counter()
        self.lag: deque = deque(maxlen=KEEP)  # ms from our last Sync to a Delay_Req
        self.interval: deque = deque(maxlen=KEEP)  # ms between two Syncs
        self.late = 0
        self._window_lag: deque = deque(maxlen=KEEP)
        self._window_interval: deque = deque(maxlen=KEEP)
        self._start, self._sent, self._received, self._late = now_ns(), Counter(), Counter(), 0

    def add_lag(self, ns: int) -> None:
        with self.lock:
            self.lag.append(ns / 1e6)
            self._window_lag.append(ns / 1e6)

    def add_interval(self, ns: int, late: bool) -> None:
        with self.lock:
            self.interval.append(ns / 1e6)
            self._window_interval.append(ns / 1e6)
            self.late += late

    def window(self) -> tuple:
        """What came since the last call: (seconds, sent, received, lag, interval, late)."""
        with self.lock:
            now = now_ns()
            window = ((now - self._start) / NS, self.sent - self._sent, self.received - self._received,
                      list(self._window_lag), list(self._window_interval), self.late - self._late)
            self._start, self._sent, self._received, self._late = now, Counter(self.sent), Counter(self.received), \
                self.late
            self._window_lag.clear()
            self._window_interval.clear()
            return window


def _per_type(counts: Counter, seconds: Optional[float] = None) -> str:
    items = sorted(counts.items(), key=lambda item: item[0])
    if seconds is None:
        return ", ".join(f"{NAMES.get(kind, 'unreadable')} {count}" for kind, count in items) or "nothing"
    return ", ".join(f"{NAMES.get(kind, 'unreadable')} {count / seconds:.1f}" for kind, count in items) or "nothing"


def _spread(values: list) -> str:
    if not values:
        return "none"
    return f"{percentile(values, 0.5):.1f}/{percentile(values, 0.95):.1f}/{max(values):.1f}"


class PtpNode:
    """The PC's PTP grandmaster: UDP 319 and 320 on one IPv4 address, three threads, the TVs it
    sends to. One per process: acquire() and release() count who uses it."""

    _instance: Optional["PtpNode"] = None
    _instances_lock = threading.Lock()

    def __init__(self, local_ip: str, ports: tuple[int, int], identity: ClockIdentity = CLOCK_IDENTITY) -> None:
        self.local_ip = local_ip
        self.asked_ports = ports
        self.identity = identity
        self.dataset = our_dataset(identity)
        self.announce_ns = announce_ms_from_env() * 1_000_000
        self.announce_log_interval = round(math.log2(self.announce_ns / NS))
        self.signaling = signaling_from_env()
        self.refs = 0
        self._lock = threading.Lock()
        self._peers: dict[str, _Peer] = {}
        self._counts = _Counts()
        self._seen: set = set()  # (ip, port, type, our port) of the first message of each kind, logged
        self._errors_logged: set = set()
        self._stop = threading.Event()
        self._sync_sequence = 0
        self._event = _bind(local_ip, ports[0])
        try:
            self._general = _bind(local_ip, ports[1])
        except PtpUnavailable:
            self._event.close()
            raise
        self.ports = (self._event.getsockname()[1], self._general.getsockname()[1])
        self._threads = [
            threading.Thread(target=self._run, args=(self._receive_loop, self._event), name="ptp-event", daemon=True),
            threading.Thread(target=self._run, args=(self._receive_loop, self._general), name="ptp-general",
                             daemon=True),
            threading.Thread(target=self._run, args=(self._send_loop,), name="ptp-send", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        _LOGGER.info("PTP: our clock %s on %s, UDP %d and %d; Announce every %d ms, Signaling %s", identity,
                     local_ip, *self.ports, self.announce_ns // 1_000_000, self.signaling)

    # -- sharing -----------------------------------------------------------------------------------

    @classmethod
    def acquire(cls, local_ip: str, ports: Optional[tuple[int, int]] = None) -> "PtpNode":
        """The process's node, started on `local_ip` if it is not running; raises PtpUnavailable
        if UDP 319 or 320 cannot be had there (`ports`: PORTS unless given)."""
        ports = tuple(ports or PORTS)
        if ":" in local_ip:
            raise PtpUnavailable(ports[0], FAILED, f"IPv4 only, and this PC is {local_ip} to the TV")
        with cls._instances_lock:
            node = cls._instance
            if node is None:
                node = cls._instance = cls(local_ip, ports)
            elif node.local_ip != local_ip or node.asked_ports != ports:
                raise PtpUnavailable(ports[0], IN_USE, f"MirrorBridge's own PTP clock runs on {node.local_ip}")
            node.refs += 1
            return node

    def release(self) -> None:
        """One user less; the last one stops the threads and closes the sockets."""
        with self._instances_lock:
            self.refs -= 1
            if self.refs > 0:
                return
            if PtpNode._instance is self:
                PtpNode._instance = None
        self._stop.set()
        for thread in self._threads:
            thread.join(2.0)
            if thread.is_alive():
                _LOGGER.warning("PTP: thread %s did not stop", thread.name)
        self._event.close()
        self._general.close()
        _LOGGER.info("PTP: our clock stopped; in all: sent %s; received %s", _per_type(self._counts.sent),
                     _per_type(self._counts.received))

    @property
    def running(self) -> bool:
        return not self._stop.is_set() and all(thread.is_alive() for thread in self._threads)

    # -- peers -------------------------------------------------------------------------------------

    def add_peer(self, ip: str, ports: Optional[tuple[int, int]] = None) -> None:
        """Send our clock to `ip` (a TV): Announce and Signaling at the next tick, then Sync and
        Follow_Up every 125 ms (`ports`: its event and general ports, PEER_PORTS unless given)."""
        event_port, general_port = ports or PEER_PORTS
        with self._lock:
            peer = self._peers.get(ip)
            if peer is not None:
                peer.refs += 1
                return
            self._peers[ip] = _Peer(ip, event_port, general_port)
        _LOGGER.info("PTP: sending our clock to %s (UDP %d and %d)", ip, event_port, general_port)

    def remove_peer(self, ip: str) -> None:
        with self._lock:
            peer = self._peers.get(ip)
            if peer is None:
                return
            peer.refs -= 1
            if peer.refs > 0:
                return
            del self._peers[ip]
        _LOGGER.info("PTP: no longer sending to %s (%s)", ip, self._peer_state(peer))

    def peers(self) -> list[str]:
        with self._lock:
            return list(self._peers)

    def locked(self, ip: str) -> bool:
        """Whether `ip` follows our clock: it sent a Delay_Req or Pdelay_Req since it was added."""
        peer = self._peers.get(ip)
        return peer is not None and peer.locked.is_set()

    async def wait_locked(self, ip: str, timeout: float) -> bool:
        """Wait up to `timeout` s for `ip` to follow our clock (on the caller's event loop)."""
        loop = asyncio.get_running_loop()
        arrived = asyncio.Event()
        with self._lock:
            peer = self._peers.get(ip)
            if peer is None:
                return False
            if peer.locked.is_set():
                return True
            waiter = (loop, arrived)
            peer.waiters.append(waiter)
        try:
            await asyncio.wait_for(arrived.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._lock:
                if waiter in peer.waiters:
                    peer.waiters.remove(waiter)
        return peer.locked.is_set()

    def better_for_ns(self, ip: str) -> Optional[int]:
        """How long `ip` has been announcing a better grandmaster than ours, None if it does not
        (or stopped: nothing better from it for 3 of its Announce intervals, 1 s at least)."""
        peer = self._peers.get(ip)
        if peer is None or peer.better_since is None:
            return None
        now = now_ns()
        if now - peer.better_last > peer.better_hold_ns:
            return None
        return now - peer.better_since

    async def wait_ready(self, ip: str, seconds: float) -> str:
        """Wait for `ip` to follow our clock, `seconds` at most. "" if it does; otherwise why not,
        for the log: no Delay_Req in time, or a better grandmaster announced for `seconds`."""
        locked = await self.wait_locked(ip, seconds)
        while locked:
            better = self.better_for_ns(ip)
            if better is None:
                return ""
            if better >= seconds * NS:
                break
            await asyncio.sleep(0.1)  # it follows us and announces a better clock: which wins?
        peer = self._peers.get(ip)
        if peer is None:
            return "the TV was taken off"
        announced = (f"the TV announces a better clock than ours ({peer.dataset.describe()})"
                     if peer.dataset is not None and self.better_for_ns(ip) is not None else "")
        if locked:
            return f"{announced} for {seconds:g} s, although it sent Delay_Req"
        heard = self._heard_from(ip)
        if announced:
            return f"{announced} and sent no Delay_Req within {seconds:g} s"
        return f"no Delay_Req or Pdelay_Req from the TV within {seconds:g} s (it sent {heard})"

    def _heard_from(self, ip: str) -> str:
        kinds = sorted({kind for seen_ip, _, kind, _ in list(self._seen) if seen_ip == ip})  # the threads add to it
        return ", ".join(NAMES.get(kind, "unreadable") for kind in kinds) or "nothing on UDP 319-320"

    # -- threads -----------------------------------------------------------------------------------

    def _run(self, loop, *args) -> None:
        try:
            loop(*args)
        except BaseException:  # never silently
            _LOGGER.exception("PTP: thread %s stopped", threading.current_thread().name)
            raise

    def _recvfrom(self, sock: socket.socket):
        return sock.recvfrom(RECEIVE_BYTES)

    def _receive_loop(self, sock: socket.socket) -> None:
        local_port = sock.getsockname()[1]
        while not self._stop.is_set():
            try:
                data, addr = self._recvfrom(sock)
                received = now_ns()  # T4 of a Delay_Req: first, before anything else
            except (TimeoutError, socket.timeout):
                continue
            except ConnectionResetError as ex:  # Windows: an ICMP "port unreachable" for one of our sends
                self._error(f"receiving on UDP {local_port}", ex)
                continue
            except OSError as ex:
                if self._stop.is_set():
                    break
                self._error(f"receiving on UDP {local_port}", ex)
                time.sleep(0.01)
                continue
            try:
                self._handle(data, addr, received, local_port)
            except Exception as ex:  # pylint: disable=broad-except
                self._error(f"handling a packet from {addr[0]}", ex)

    def _handle(self, data: bytes, addr, received: int, local_port: int) -> None:
        ip, port = addr[0], addr[1]
        try:
            head = parse_header(data)
        except ValueError:
            self._count(self._counts.received, UNREADABLE)
            if (ip, port, UNREADABLE, local_port) not in self._seen:
                self._seen.add((ip, port, UNREADABLE, local_port))
                _LOGGER.info("PTP: an unreadable %d-byte packet on UDP %d from %s:%d", len(data), local_port, ip, port)
            return
        peer = self._peers.get(ip)
        if head.kind == DELAY_REQ and len(data) >= SIZES[DELAY_REQ]:
            # Answered at once, from our general port to the TV's, whatever port it asked from.
            self._send(self._general, delay_resp(self.identity, head, received),
                       (ip, peer.general_port if peer else GENERAL_PORT), DELAY_RESP)
        elif head.kind == PDELAY_REQ and len(data) >= SIZES[PDELAY_REQ]:
            sent = now_ns()
            self._send(self._event, pdelay_resp(self.identity, head, received),
                       (ip, peer.event_port if peer else EVENT_PORT), PDELAY_RESP)
            self._send(self._general, pdelay_resp_follow_up(self.identity, head, sent),
                       (ip, peer.general_port if peer else GENERAL_PORT), PDELAY_RESP_FOLLOW_UP)
        self._count(self._counts.received, head.kind)
        if (ip, port, head.kind, local_port) not in self._seen:
            self._seen.add((ip, port, head.kind, local_port))
            _LOGGER.info("PTP: first %s from %s:%d (on our UDP %d)%s: %s", NAMES.get(head.kind, f"type {head.kind}"),
                         ip, port, local_port, "" if peer else ", not a TV we send to", describe(data))
        if peer is None:
            return
        if head.kind in (DELAY_REQ, PDELAY_REQ):
            peer.delay_req_ports[port] += 1
            if peer.last_sync_ns is not None:
                self._counts.add_lag(received - peer.last_sync_ns)
            self._lock_peer(peer, head.kind, port, received)
        elif head.kind == ANNOUNCE:
            self._announced(peer, port, head, data, received)

    def _lock_peer(self, peer: _Peer, kind: int, port: int, received: int) -> None:
        with self._lock:
            if peer.locked.is_set():
                return
            peer.lock_ns = received
            peer.locked.set()
            waiters = list(peer.waiters)
        _LOGGER.info("PTP: %s follows our clock: its first %s came %.0f ms after it was added, from port %d",
                     peer.ip, NAMES[kind], (received - peer.added_ns) / 1e6, port)
        for loop, arrived in waiters:
            try:
                loop.call_soon_threadsafe(arrived.set)
            except RuntimeError:  # its loop is closed: nobody waits any more
                pass

    def _announced(self, peer: _Peer, port: int, head: Header, data: bytes, received: int) -> None:
        try:
            dataset = parse_announce(data)
        except ValueError:
            return
        if compare(dataset, self.dataset) < 0:
            if peer.better_since is None or received - peer.better_last > peer.better_hold_ns:
                peer.better_since = received
            peer.better_last = received
            peer.better_hold_ns = max(NS, round(3 * NS * 2.0 ** max(-7, min(7, head.log_interval))))
        else:
            peer.better_since = None
        if dataset != peer.dataset:
            _LOGGER.info("PTP: %s:%d announces %s (%s): %s", peer.ip, port, dataset.describe(),
                         "the first time" if peer.dataset is None else "a change", verdict(dataset, self.dataset))
            peer.dataset = dataset

    def _send_loop(self) -> None:
        tick = now_ns()
        last_sync: Optional[int] = None
        while not self._stop.is_set():
            wait = tick - now_ns()
            if wait > 0:
                time.sleep(wait / NS)
            if self._stop.is_set():
                break
            with self._lock:
                peers = list(self._peers.values())
            try:
                started = self._tick(peers)
            except Exception as ex:  # pylint: disable=broad-except
                self._error("sending", ex)
                started = None
            if started is not None and last_sync is not None:
                self._counts.add_interval(started - last_sync, started - tick > LATE_NS)
            last_sync = started
            tick += SYNC_NS
            behind = now_ns() - tick
            if behind > SYNC_NS:  # the thread was held up for more than a tick: no burst to catch up
                tick += behind // SYNC_NS * SYNC_NS

    def _tick(self, peers: list[_Peer]) -> Optional[int]:
        """Announce and Signaling to the peers they are due to, then Sync and Follow_Up to each.
        Returns when the first Sync left (None when there was nobody to send to)."""
        if not peers:
            return None
        now = now_ns()
        for peer in peers:
            if now >= peer.next_announce:
                self._send(self._general, announce(self.identity, peer.announce_sequence, self.announce_log_interval),
                           (peer.ip, peer.general_port), ANNOUNCE)
                peer.announce_sequence = (peer.announce_sequence + 1) & 0xFFFF
                peer.next_announce = max(peer.next_announce + self.announce_ns, now + self.announce_ns // 2)
            if self.signaling != "off" and now >= peer.next_signaling:
                self._send(self._general, signaling(self.identity, peer.signaling_sequence, self.signaling),
                           (peer.ip, peer.general_port), SIGNALING)
                peer.signaling_sequence = (peer.signaling_sequence + 1) & 0xFFFF
                peer.next_signaling = max(peer.next_signaling + SIGNALING_NS, now + SIGNALING_NS // 2)
        sequence = self._sync_sequence
        self._sync_sequence = (sequence + 1) & 0xFFFF
        message = sync(self.identity, sequence)
        first = None
        for peer in peers:
            sent = now_ns()  # T1: just before the Sync leaves
            self._send(self._event, message, (peer.ip, peer.event_port), SYNC)
            peer.last_sync_ns = sent
            first = sent if first is None else first
            self._send(self._general, follow_up(self.identity, sequence, sent), (peer.ip, peer.general_port), FOLLOW_UP)
        return first

    def _send(self, sock: socket.socket, data: bytes, addr: tuple[str, int], kind: int) -> None:
        try:
            sock.sendto(data, addr)
        except OSError as ex:  # ConnectionResetError among them: an ICMP answer to an earlier packet
            self._error(f"sending {NAMES[kind]} to {addr[0]}:{addr[1]}", ex)
            return
        self._count(self._counts.sent, kind)

    def _count(self, counts: Counter, kind: int) -> None:
        with self._counts.lock:
            counts[kind] += 1

    def _error(self, what: str, ex: BaseException) -> None:
        """Counted, and logged the first time for each kind of error: a thread goes on."""
        key = (what.split(" to ")[0], type(ex).__name__, getattr(ex, "errno", None))
        with self._counts.lock:
            self._counts.errors[key] += 1
        if key not in self._errors_logged:
            self._errors_logged.add(key)
            _LOGGER.warning("PTP: %s: %s: %s (going on; later ones are only counted)", what, type(ex).__name__, ex)

    # -- what happened -------------------------------------------------------------------------------

    def _peer_state(self, peer: _Peer) -> str:
        if peer.lock_ns is not None:
            ports = ", ".join(f"{port} ({count})" for port, count in sorted(peer.delay_req_ports.items()))
            state = (f"{peer.ip} follows our clock since {(peer.lock_ns - peer.added_ns) / NS:.2f} s after it was "
                     f"added, Delay_Req from port {ports}")
        else:
            state = f"{peer.ip} does not follow our clock (no Delay_Req), it sent {self._heard_from(peer.ip)}"
        if peer.dataset is not None:
            state += f"; it announces {peer.dataset.describe()}, {verdict(peer.dataset, self.dataset)}"
        return state

    def summary(self) -> str:
        """For the session's stats line: messages a second since the last call, Delay_Req's lag
        behind our Sync, how steadily the Syncs leave, then the totals and each TV's state."""
        seconds, sent, received, lag, interval, late = self._counts.window()
        seconds = max(1e-9, seconds)
        with self._lock:
            peers = list(self._peers.values())
        with self._counts.lock:
            totals = f"in all: sent {_per_type(self._counts.sent)}; received {_per_type(self._counts.received)}"
            errors = sum(self._counts.errors.values())
        parts = [
            f"PTP a second: sent {_per_type(sent, seconds)}; received {_per_type(received, seconds)}",
            f"Delay_Req after our Sync ms p50/p95/max {_spread(lag)}",
            f"Sync every ms p50/p95/max {_spread(interval)}, {late} more than {LATE_NS // 1_000_000} ms late",
            totals,
        ]
        if errors:
            parts.append(f"{errors} errors")
        parts += [self._peer_state(peer) for peer in peers]
        return "; ".join(parts)

    def totals(self) -> str:
        """The whole session's counts, Delay_Req's lag and the Sync cadence, for the end of the log."""
        with self._counts.lock:
            lag, interval = list(self._counts.lag), list(self._counts.interval)
            parts = [f"sent {_per_type(self._counts.sent)}", f"received {_per_type(self._counts.received)}",
                     f"Delay_Req after our Sync ms p50/p95/max {_spread(lag)}",
                     f"Sync every ms p50/p95/max {_spread(interval)}, {self._counts.late} more than "
                     f"{LATE_NS // 1_000_000} ms late"]
            errors = sum(self._counts.errors.values())
        if errors:
            parts.append(f"{errors} errors")
        with self._lock:
            parts += [self._peer_state(peer) for peer in self._peers.values()]
        return "; ".join(parts)
