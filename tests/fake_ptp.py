"""A TV's PTP clock inside the test process, for MirrorBridge's PTP grandmaster (ptp.py) to talk to.

Once begun (as a TV's clock starts on SETPEERS), it is one of:

* "slave": a clock that follows the sender's, end to end. It announces itself once at priority
  248 (an SDK receiver's default), then sends a Delay_Req after each Sync and Follow_Up pair to
  where the Sync came from, and works out from the Delay_Resp how far its clock is from the
  sender's and how long the way is. Both sides use time.perf_counter_ns(), so on loopback the
  offset it finds is pure error.
* "grandmaster": a clock that insists on being the best: it announces priority1 64 every 250 ms
  to where the sender's general messages come from, and never asks for the time.
* "silent": it hears everything and says nothing.

It records what came, from where, and when, either way. Like fake_mirror_tv.py it reads and
writes the wire on its own (struct offsets), not with mirrorbridge.mirror.ptp, so that the two
sides check each other. Its sockets are on ephemeral ports of 127.0.0.1: tests never bind 319
or 320.
"""

from __future__ import annotations

import select
import socket
import statistics
import struct
import threading
import time
from typing import Callable, Optional

SYNC, DELAY_REQ, FOLLOW_UP, DELAY_RESP, ANNOUNCE, SIGNALING = 0x0, 0x1, 0x8, 0x9, 0xB, 0xC
HEADER = struct.Struct(">BBHBBHqI8sHHBb")
NS = 1_000_000_000
CLOCK_ID = 0x1122334455660008
PORT_NUMBER = 0x8001


def stamp(ns: int) -> bytes:
    seconds, rest = divmod(ns, NS)
    return struct.pack(">HII", seconds >> 32, seconds & 0xFFFFFFFF, rest)


def read_stamp(data: bytes, offset: int = 34) -> int:
    high, low, rest = struct.unpack_from(">HII", data, offset)
    return ((high << 32) | low) * NS + rest


def header(kind: int, length: int, sequence: int, control: int, log_interval: int, flags: int = 0x0400) -> bytes:
    return HEADER.pack(0x10 | kind, 2, length, 0, 0, flags, 0, 0, CLOCK_ID.to_bytes(8, "big"), PORT_NUMBER,
                       sequence, control, log_interval)


def announce(sequence: int, priority1: int, priority2: int, log_interval: int) -> bytes:
    return (header(ANNOUNCE, 64, sequence, 5, log_interval, 0x0408) + stamp(0)
            + struct.pack(">hBBBBHB8sHB", 37, 0, priority1, 248, 0xFE, 0xFFFF, priority2, CLOCK_ID.to_bytes(8, "big"),
                          0, 0xA0))


class FakePtp:
    """Two sockets (event, general) and a thread that reads both; see the module's docstring."""

    def __init__(self, ip: str = "127.0.0.1") -> None:
        self.ip = ip
        self.mode = ""  # "" until begin()
        self.event = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.event.bind((ip, 0))
        self.general = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.general.bind((ip, 0))
        self.ports = (self.event.getsockname()[1], self.general.getsockname()[1])
        self.received: list[tuple[int, int, tuple, int, bytes]] = []  # (type, our port, from, when, message)
        self.sent_delay_reqs: dict[int, int] = {}  # sequenceId -> t3
        self.syncs: dict[int, tuple[int, tuple]] = {}  # sequenceId -> (t2, from)
        self.follow_ups: dict[int, int] = {}  # sequenceId -> t1
        self.offsets: list[int] = []  # ns, ((t2 - t1) - (t4 - t3)) / 2
        self.delays: list[int] = []  # ns, ((t2 - t1) + (t4 - t3)) / 2
        self.errors: list[str] = []
        self.locked_ns: Optional[int] = None  # when it sent its first Delay_Req
        self.master_general: Optional[tuple] = None  # where the sender's general messages come from
        self._sequence = 0
        self._changed = threading.Condition()
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._receive, daemon=True, name="fake-ptp")]
        self._threads[0].start()

    # -- control ------------------------------------------------------------------------------------

    def begin(self, mode: str) -> None:
        """The TV's clock starts (SETPEERS named the sender): `mode` is slave, grandmaster or silent."""
        if self.mode:
            return
        self.mode = mode
        if mode == "slave":
            self._threads.append(threading.Thread(target=self._announce, args=(248, 248, 0, 1), daemon=True))
        elif mode == "grandmaster":
            self._threads.append(threading.Thread(target=self._announce, args=(64, 64, -2, None), daemon=True))
        for thread in self._threads[1:]:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(2)
        self.event.close()
        self.general.close()

    def wait_for(self, done: Callable[["FakePtp"], bool], timeout: float) -> bool:
        """Block until done(self) (checked after each message), `timeout` s at most."""
        end = time.monotonic() + timeout
        with self._changed:
            while not done(self):
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._changed.wait(min(left, 0.1))
        return True

    # -- what came ----------------------------------------------------------------------------------

    def of_type(self, kind: int) -> list[tuple[int, int, tuple, int, bytes]]:
        return [entry for entry in list(self.received) if entry[0] == kind]

    def rate(self, kind: int, seconds: float) -> float:
        """Messages of `kind` a second, over the first `seconds` from the first of them."""
        times = [when for _, _, _, when, _ in self.of_type(kind)]
        if not times:
            return 0.0
        return sum(1 for when in times if when - times[0] < seconds * NS) / seconds

    def last_after(self, kind: int, seconds: float) -> bool:
        """Whether a message of `kind` came at least `seconds` after the first one."""
        times = [when for _, _, _, when, _ in self.of_type(kind)]
        return bool(times) and times[-1] - times[0] >= seconds * NS

    @property
    def median_offset_ms(self) -> float:
        return statistics.median(self.offsets) / 1e6

    # -- the threads --------------------------------------------------------------------------------

    def _receive(self) -> None:
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([self.event, self.general], [], [], 0.05)
            except (OSError, ValueError):
                return
            for sock in readable:
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError:
                    continue
                now = time.perf_counter_ns()
                try:
                    self._handle(sock, data, addr, now)
                except Exception as ex:  # pylint: disable=broad-except
                    self.errors.append(repr(ex))
                with self._changed:
                    self._changed.notify_all()

    def _handle(self, sock: socket.socket, data: bytes, addr, now: int) -> None:
        if len(data) < 34:
            self.errors.append(f"short packet {data.hex()}")
            return
        kind = data[0] & 0x0F
        sequence = struct.unpack_from(">H", data, 30)[0]
        self.received.append((kind, sock.getsockname()[1], addr, now, data))
        if sock is self.general and kind in (ANNOUNCE, FOLLOW_UP, SIGNALING):
            self.master_general = addr
        if self.mode != "slave":
            return
        if kind == SYNC:
            self.syncs[sequence] = (now, addr)
        elif kind == FOLLOW_UP:
            self.follow_ups[sequence] = read_stamp(data)
            if sequence in self.syncs:
                self._delay_req(sequence, self.syncs[sequence][1])
        elif kind == DELAY_RESP:
            self._delay_resp(data, sequence)

    def _delay_req(self, sequence: int, master_event: tuple) -> None:
        sent = time.perf_counter_ns()  # t3, just before it leaves
        self.event.sendto(header(DELAY_REQ, 44, sequence, 1, 0x7F) + stamp(sent), master_event)
        self.sent_delay_reqs[sequence] = sent
        if self.locked_ns is None:
            self.locked_ns = sent

    def _delay_resp(self, data: bytes, sequence: int) -> None:
        if data[44:54] != CLOCK_ID.to_bytes(8, "big") + struct.pack(">H", PORT_NUMBER):
            self.errors.append(f"Delay_Resp for {data[44:54].hex()}")
            return
        if sequence not in self.sent_delay_reqs or sequence not in self.follow_ups:
            self.errors.append(f"Delay_Resp {sequence} to no Delay_Req")
            return
        t1, (t2, _), t3, t4 = (self.follow_ups[sequence], self.syncs[sequence], self.sent_delay_reqs[sequence],
                               read_stamp(data))
        if not t3 <= t4 <= time.perf_counter_ns():
            self.errors.append(f"Delay_Resp {sequence}: received {t4}, sent at {t3}")
        self.offsets.append(((t2 - t1) - (t4 - t3)) // 2)
        self.delays.append(((t2 - t1) + (t4 - t3)) // 2)

    def _announce(self, priority1: int, priority2: int, log_interval: int, count: Optional[int]) -> None:
        """Announce to where the sender's general messages come from, every 2^log_interval s,
        `count` times (None: until stopped)."""
        sent = 0
        while not self._stop.is_set() and (count is None or sent < count):
            target = self.master_general
            if target is not None:
                self.general.sendto(announce(self._sequence, priority1, priority2, log_interval), target)
                self._sequence = (self._sequence + 1) & 0xFFFF
                sent += 1
            self._stop.wait(2.0 ** log_interval if target is not None else 0.02)
