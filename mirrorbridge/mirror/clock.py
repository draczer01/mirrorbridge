"""The one clock of a mirroring session, the NTP responder the TV syncs to, and FFmpeg's
timestamps put on that clock.

The TV asks the sender for the time (NTP request 0xd2, answered with 0xd3) and then shows each
video frame at the time its header names. Both must come from the same clock, and from one that
never jumps: pyatv's TimingServer answers with the wall clock, which Windows moves whenever it
syncs with a time server. Here everything uses time.perf_counter_ns(), the monotonic
QueryPerformanceCounter clock on Windows (boot-relative, like the TVs' own). NTP replies put it
in NTP form with the 1900 epoch added (NTP_EPOCH_SECONDS); video headers carry the same clock
in the same 32.32 form without it.

The TV sends its requests to the sender's timing port: inbound UDP, which Windows Firewall
blocks unless MirrorBridge is allowed. So the responder takes the first free port of a fixed,
small range (TIMING_PORTS) that a single firewall rule can name.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from collections import deque
from typing import Optional

_LOGGER = logging.getLogger(__name__)

NTP_EPOCH_SECONDS = 2208988800  # 1900-01-01 to 1970-01-01, added on the wire only
NS = 1_000_000_000

# UDP ports MirrorBridge's mirroring listens on: the NTP responder first, then the (silent)
# audio control port. Allow inbound UDP on these in Windows Firewall.
TIMING_PORTS = range(7880, 7890)

NTP_REQUEST = 0xD2
NTP_REPLY = 0xD3
NTP_PACKET = 32


def now_ns() -> int:
    """The session clock: monotonic nanoseconds (QueryPerformanceCounter on Windows)."""
    return time.perf_counter_ns()


def to_ntp(ns: int, epoch: bool = False) -> int:
    """`ns` of the session clock as a 64-bit NTP value (32 bits of seconds, 32 of fraction).
    epoch=True adds the 1900 epoch, as NTP packets carry it; video headers do not."""
    seconds, rest = divmod(ns, NS)
    if epoch:
        seconds += NTP_EPOCH_SECONDS
    return ((seconds & 0xFFFFFFFF) << 32) | ((rest << 32) // NS)


def from_ntp(value: int, epoch: bool = False) -> int:
    """A 64-bit NTP value back to nanoseconds of the session clock."""
    seconds = value >> 32
    if epoch:
        seconds -= NTP_EPOCH_SECONDS
    return seconds * NS + (((value & 0xFFFFFFFF) * NS + (1 << 31)) >> 32)


def ntp_reply(request: bytes, received_ns: int, sent_ns: Optional[int] = None) -> Optional[bytes]:
    """The 0xd3 answer to a 0xd2 timing request, or None if `request` is not one.

    [2:8] (the request's sequence number and what follows it) come back as they were; the
    request's transmit time [24:32] comes back as the origin time [8:16]; [16:24] is when it
    was received and [24:32] when the answer leaves, both on the session clock, 1900 epoch.
    """
    if len(request) < NTP_PACKET or request[1] != NTP_REQUEST:
        return None
    sent_ns = received_ns if sent_ns is None else sent_ns
    return bytes([0x80, NTP_REPLY]) + bytes(request[2:8]) + struct.pack(
        ">8sQQ", request[24:32], to_ntp(received_ns, True), to_ntp(sent_ns, True)
    )


def ntp_request(sent_ns: int) -> bytes:
    """A 0xd2 timing request, as a receiver sends it (and as we probe a receiver's own port)."""
    return struct.pack(">BBHIQQQ", 0x80, NTP_REQUEST, 7, 0, 0, 0, to_ntp(sent_ns, True))


def _ntp_stop_ns() -> Optional[int]:
    try:
        return int(float(os.environ["MIRRORBRIDGE_NTP_STOP_S"]) * NS)
    except (KeyError, ValueError):
        return None


class NtpResponder(asyncio.DatagramProtocol):
    """Answers the TV's timing requests and counts them, so a silent TV can be told apart."""

    def __init__(self) -> None:
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.requests = 0
        self.first_request_ns: Optional[int] = None
        self.last_request_ns: Optional[int] = None
        self._first_tv_minus_ours: Optional[int] = None
        # Experiment: MIRRORBRIDGE_NTP_STOP_S stops answering that long after the first request.
        self.stop_after_ns: Optional[int] = _ntp_stop_ns()
        self.stopped_answering = False
        self.peers: set[str] = set()
        self.arrived = asyncio.Event()

    @property
    def port(self) -> int:
        assert self.transport is not None
        return self.transport.get_extra_info("sockname")[1]

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        received = now_ns()
        if ntp_reply(data, received) is None:
            if len(data) >= 2 and data[1] == NTP_REPLY:
                return  # an answer to one of our own probes (probe_receiver)
            _LOGGER.debug("Ignoring a %d-byte packet on the timing port from %s", len(data), addr[0])
            return
        sent = now_ns()  # the answer's transmit time: when it really leaves
        silent = (self.stop_after_ns is not None and self.first_request_ns is not None
                  and received - self.first_request_ns >= self.stop_after_ns)
        if silent and not self.stopped_answering:
            self.stopped_answering = True
            _LOGGER.info("Not answering the TV's time requests any more (MIRRORBRIDGE_NTP_STOP_S test switch)")
        if self.transport is not None and not silent:
            self.transport.sendto(ntp_reply(data, received, sent), addr)
        self.requests += 1
        # Experiment: the TV's own transmit time against our receive time, from the first request
        # of the session. Its spread shows how much the TV's view of our clock can jitter.
        tv_minus_ours = from_ntp(struct.unpack(">Q", data[24:32])[0]) - received
        if self._first_tv_minus_ours is None:
            self._first_tv_minus_ours = tv_minus_ours
        _LOGGER.debug("NTP request %d, %s; tv_sent - our_received %+.2f ms vs the first; %s", self.requests,
                      f"{(received - self.last_request_ns) / 1e6:.0f} ms after the last"
                      if self.last_request_ns is not None else "the first",
                      (tv_minus_ours - self._first_tv_minus_ours) / 1e6,
                      "not answered (test switch)" if silent else f"answered {(sent - received) / 1e3:.0f} us after it came")
        self.last_request_ns = received
        if self.first_request_ns is None:
            self.first_request_ns = received
            _LOGGER.info("The TV asked for the time (NTP) from %s:%s", addr[0], addr[1])
        self.peers.add(addr[0])
        self.arrived.set()

    def probe_receiver(self, address: str, port: int, count: int = 3, spacing: float = 0.1) -> None:
        """Send `count` timing requests, `spacing` seconds apart, to the timing port the receiver
        named, from this responder's port: a stateful firewall then takes the receiver's
        requests from there as answers and lets them in. What the receiver answers is ignored."""
        loop = asyncio.get_running_loop()

        def send(left: int) -> None:
            if self.transport is None or left <= 0:
                return
            self.transport.sendto(ntp_request(now_ns()), (address, port))
            loop.call_later(spacing, send, left - 1)

        send(count)

    def error_received(self, exc) -> None:
        # Worth seeing: on Windows a socket error can leave the port deaf to the TV's requests.
        _LOGGER.warning("Timing port %s: %s", self.port if self.transport else "?", exc)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None


async def bind_udp(factory, host: str, ports: range = TIMING_PORTS, taken: tuple[int, ...] = ()):
    """Bind a UDP endpoint on the first free port of `ports` (one firewall rule covers them).

    If every one of them is taken, a random port is used, with a warning: the TV's requests
    may then be blocked by the firewall. Returns (transport, protocol).
    """
    loop = asyncio.get_running_loop()
    for port in ports:
        if port in taken:
            continue
        try:
            return await loop.create_datagram_endpoint(factory, local_addr=(host, port))
        except OSError:
            continue
    _LOGGER.warning("UDP ports %d-%d are all in use; using a random port, which Windows Firewall may block",
                    ports.start, ports.stop - 1)
    return await loop.create_datagram_endpoint(factory, local_addr=(host, 0))


class PtsMapper:
    """Puts FFmpeg's frame timestamps on the session clock.

    FFmpeg stamps each frame (pts, in ms for FLV) from an origin of its own; MirrorBridge sees
    when the frame came out of the pipe. arrival - pts is the frame's offset: the fixed delay
    of capture, encoding and the pipe, plus jitter that only ever makes it bigger. The smallest
    offset of the last `window_ns` is the delay without the jitter, so pts + that offset is
    when the frame was really taken, as steady as the capture itself. The result never goes
    backwards, even when a new smallest offset turns up.

    A delay that grows for good (FFmpeg holding frames back) becomes the window's new smallest
    offset within `window_ns`; `behind()` measures a frame against the smallest of the whole
    session instead, so that it still shows. A new FFmpeg in the same session starts its pts
    from another origin: `restart()`.
    """

    def __init__(self, window_ns: int = 2 * NS, slew_ns: Optional[int] = None) -> None:
        self.window_ns = window_ns
        # Experiment: how far the offset in use may move per frame towards the window's smallest,
        # instead of jumping to it when the smallest leaves the window (every window_ns or so).
        self.slew_ns = slew_ns
        self._offsets: deque[tuple[int, int]] = deque()  # (arrival, offset), offsets increasing
        self._last: Optional[int] = None
        self._used: Optional[int] = None  # the offset in use, with slew_ns
        self.lowest: Optional[int] = None  # the smallest offset of the session

    def restart(self) -> None:
        """A new FFmpeg, whose pts start from 0 again: forget the offsets, and the session's
        smallest, but not the last time given, so that the times that follow never go back."""
        self._offsets.clear()
        self.lowest = None

    def behind(self, pts_ms: int, arrival_ns: int) -> int:
        """How much later than at best (the session's smallest offset) this frame came, in ns."""
        offset = arrival_ns - pts_ms * 1_000_000
        return max(0, offset - self.lowest) if self.lowest is not None else 0

    def map(self, pts_ms: int, arrival_ns: int) -> int:
        """When the frame with this pts was taken, on the session clock (ns)."""
        offset = arrival_ns - pts_ms * 1_000_000
        if self.lowest is None or offset < self.lowest:
            self.lowest = offset
        while self._offsets and self._offsets[-1][1] >= offset:
            self._offsets.pop()
        self._offsets.append((arrival_ns, offset))
        while self._offsets[0][0] < arrival_ns - self.window_ns:
            self._offsets.popleft()
        target = self._offsets[0][1]
        if self.slew_ns is None or self._used is None:
            self._used = target
        else:
            self._used += max(-self.slew_ns, min(self.slew_ns, target - self._used))
        mapped = pts_ms * 1_000_000 + self._used
        if self._last is not None and mapped <= self._last:
            mapped = self._last + 1_000  # 1 us later than the frame before it
        self._last = mapped
        return mapped

    @property
    def offset_ns(self) -> Optional[int]:
        """The smallest offset of the window: arrival minus pts at best."""
        return self._offsets[0][1] if self._offsets else None
