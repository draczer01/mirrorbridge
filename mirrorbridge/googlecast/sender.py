"""The video's UDP side: frames out as RTP, the receiver's RTCP in, lost packets sent again.

Each frame is encrypted (rtp.FrameCipher), cut into packets and sent at once, and kept until
the receiver says it has it (Cast feedback's ack) or it is older than KEEP_SECONDS. The
receiver asks for what it misses in the same feedback, and those packets go again, each at
most once every RESEND_GAP_NS. A sender report every rtp.RTCP_SECONDS ties the frames' RTP
times to our clock: the receiver shows each frame its target delay after the time it was
taken for, so the frames' RTP times come from that same clock (rtp_time).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from ..mirror.clock import NS, now_ns
from .rtp import (ALL_PACKETS, LAST_PACKET, RTCP_SECONDS, VIDEO_CLOCK, FrameCipher, Packetizer, expand, is_rtcp,
                  parse_rtcp, sender_report)

_LOGGER = logging.getLogger(__name__)

NTP_EPOCH = 2208988800  # 1900 to 1970
KEEP_SECONDS = 3  # frames kept for sending again
RESEND_GAP_NS = 20_000_000  # the same packet at most once in this long
SEND_BUFFER = 4 << 20  # a 1080p keyframe is 100 KB or more, sent in one go


@dataclass
class _Frame:
    frame_id: int
    packets: list[bytes]
    keyframe: bool
    taken_ns: int
    resent: dict = field(default_factory=dict)  # packet id -> when it last went again


@dataclass
class SenderStats:
    frames: int = 0
    keyframes: int = 0
    packets: int = 0
    bytes: int = 0
    resent: int = 0
    plis: int = 0
    feedbacks: int = 0
    reports: int = 0
    lost_reported: int = 0  # the receiver's cumulative lost count, from its last report
    target_delay_ms: int = 0  # what the receiver's feedback says it uses
    decoded: int = 0  # frames the receiver says it decoded (its event log)
    played: int = 0  # ...and showed
    played_late: int = 0  # ...after their time
    logs: int = 0  # event logs received: without any, decoded and played say nothing
    spare_ms: Optional[int] = None  # the last frame shown: ms before its time (negative: late)


class VideoSender(asyncio.DatagramProtocol):
    def __init__(self, cipher: FrameCipher, packetizer: Packetizer, receiver_ssrc: int) -> None:
        self.cipher = cipher
        self.packetizer = packetizer
        self.receiver_ssrc = receiver_ssrc
        self.stats = SenderStats()
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.next_frame_id = 0
        self.acked = -1  # the last frame the receiver has whole (and all before it)
        self.last_rtcp_ns: Optional[int] = None
        self.pli_pending = False
        self.error: Optional[Exception] = None
        self._frames: OrderedDict[int, _Frame] = OrderedDict()
        self._clock_ns = now_ns()  # RTP time 0 and the NTP time it is
        self._clock_wall = time.time_ns()
        self._reports: Optional[asyncio.Task] = None

    # -- clocks --------------------------------------------------------------------------------

    def rtp_time(self, ns: int) -> int:
        return (ns - self._clock_ns) * VIDEO_CLOCK // NS

    def ntp_time(self, ns: int) -> int:
        wall = self._clock_wall + ns - self._clock_ns
        seconds, rest = divmod(wall, NS)
        return ((seconds + NTP_EPOCH) & 0xFFFFFFFF) << 32 | (rest << 32) // NS

    # -- asyncio ---------------------------------------------------------------------------------

    def connection_made(self, transport) -> None:
        self.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)
            except OSError:
                pass
        self._reports = asyncio.ensure_future(self._report_loop())

    def connection_lost(self, exc) -> None:
        if self._reports is not None:
            self._reports.cancel()

    def error_received(self, exc) -> None:
        # Windows reports an ICMP "port unreachable" for an earlier packet here: the receiver
        # is not listening (any more).
        _LOGGER.debug("Video socket: %s", exc)
        self.error = exc

    def datagram_received(self, data: bytes, addr) -> None:
        if not is_rtcp(data):
            return
        self.last_rtcp_ns = now_ns()
        rtcp = parse_rtcp(data)
        if rtcp.report is not None:
            self.stats.lost_reported = rtcp.report["lost"]
        if rtcp.pli:
            self.stats.plis += 1
            self.pli_pending = True
        self.stats.logs += rtcp.log
        self.stats.decoded += rtcp.decoded
        for spare in rtcp.played:
            self.stats.played += 1
            self.stats.played_late += spare < 0
            self.stats.spare_ms = spare
        if rtcp.feedback is not None:
            self.stats.feedbacks += 1
            self._on_feedback(rtcp.feedback)

    def close(self) -> None:
        if self._reports is not None:
            self._reports.cancel()
        if self.transport is not None:
            self.transport.close()

    # -- sending ---------------------------------------------------------------------------------

    def send_frame(self, data: bytes, keyframe: bool, taken_ns: int) -> int:
        """Encrypt, packetize and send one frame (Annex B H.264) taken at `taken_ns`; returns its id."""
        frame_id = self.next_frame_id
        self.next_frame_id += 1
        packets = self.packetizer.packets(frame_id, self.rtp_time(taken_ns), self.cipher.apply(frame_id, data),
                                          keyframe)
        self._frames[frame_id] = _Frame(frame_id, packets, keyframe, taken_ns)
        self._prune(taken_ns)
        for packet in packets:
            self._send(packet)
        self.stats.frames += 1
        self.stats.keyframes += keyframe
        self.stats.packets += len(packets)
        self.stats.bytes += len(data)
        return frame_id

    def in_flight(self) -> int:
        """Frames sent that the receiver does not have yet."""
        return self.next_frame_id - 1 - self.acked

    def oldest_unacked_ns(self) -> Optional[int]:
        frame = self._frames.get(self.acked + 1)
        return frame.taken_ns if frame is not None else None

    def send_report(self) -> None:
        now = now_ns()
        self._send(sender_report(self.packetizer.ssrc, self.ntp_time(now), self.rtp_time(now),
                                 self.packetizer.packets_sent, self.packetizer.octets_sent))
        self.stats.reports += 1

    def _send(self, packet: bytes) -> None:
        if self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(packet)

    async def _report_loop(self) -> None:
        while True:
            self.send_report()
            await asyncio.sleep(RTCP_SECONDS)

    def _prune(self, now: int) -> None:
        while self._frames:
            first_id, first = next(iter(self._frames.items()))
            if first_id <= self.acked or now - first.taken_ns > KEEP_SECONDS * NS:
                self._frames.popitem(last=False)
            else:
                break

    # -- what the receiver says ------------------------------------------------------------------

    def _on_feedback(self, feedback) -> None:
        latest = self.next_frame_id - 1
        ack = expand(feedback.ack, latest)
        if ack > latest:
            ack -= 256
        if ack > self.acked:
            self.acked = ack
            self._prune(now_ns())
        self.stats.target_delay_ms = feedback.target_delay_ms
        later = {expand(frame, ack) for frame in feedback.received_later}
        now = now_ns()
        for truncated, packet_id in feedback.losses:
            frame = self._frames.get(expand(truncated, ack))
            if frame is None or frame.frame_id in later:
                continue
            if packet_id == ALL_PACKETS:
                wanted = range(len(frame.packets))
            elif packet_id == LAST_PACKET:
                wanted = range(len(frame.packets) - 1, len(frame.packets))
            else:
                wanted = range(packet_id, packet_id + 1) if packet_id < len(frame.packets) else range(0)
            for index in wanted:
                if now - frame.resent.get(index, 0) < RESEND_GAP_NS:
                    continue
                frame.resent[index] = now
                self._send(frame.packets[index])
                self.stats.resent += 1
