"""The TCP connection the video goes over (the type-110 stream's dataPort).

Small socket buffers and no Nagle delay: a frame should leave the moment it is encrypted, and
a TV that falls behind should show up as a slow write here (which the sender answers by
dropping frames up to the next keyframe), not as seconds of video queued in the PC's kernel.
So the kernel's send buffer and asyncio's, which together hold what the TV has not yet taken,
are about two frames big (the session says how big). A heartbeat (a bare header of type 2) goes
every second once the first frame went. The TV sends nothing back; when it closes the
connection, `closed` is set.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

from .packets import heartbeat_packet

_LOGGER = logging.getLogger(__name__)

CONNECT_SECONDS = 5
SEND_BUFFER = 64 * 1024  # without a size from the session
HEARTBEAT_SECONDS = 1.0
CLOSE_SECONDS = 1.0  # to send what is still buffered when the session ends; then it is dropped


class DataChannel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.closed = asyncio.Event()
        self.close_reason = ""
        self.bytes_sent = 0
        self.heartbeats = 0
        self._started = False
        self._tasks = [asyncio.ensure_future(self._watch())]

    @classmethod
    async def open(cls, address: str, port: int, seconds: float = CONNECT_SECONDS,
                   buffer: int = SEND_BUFFER) -> "DataChannel":
        """Connect, with send buffers of `buffer` bytes; raises OSError (or
        asyncio.TimeoutError) if the TV does not take it."""
        reader, writer = await asyncio.wait_for(asyncio.open_connection(address, port), seconds)
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffer)
            except OSError as ex:
                _LOGGER.debug("Video connection socket options: %s", ex)
        writer.transport.set_write_buffer_limits(high=buffer)
        _LOGGER.info("Video connection to %s:%d open (send buffers %d KB)", address, port, buffer // 1024)
        return cls(reader, writer)

    async def _watch(self) -> None:
        try:
            while await self._reader.read(65536):
                pass
            self.close_reason = "closed by the TV"
        except (OSError, asyncio.IncompleteReadError) as ex:
            self.close_reason = f"reset by the TV ({ex})"
        except asyncio.CancelledError:
            raise
        self.closed.set()

    async def _heartbeat(self) -> None:
        while not self.closed.is_set():
            await asyncio.sleep(HEARTBEAT_SECONDS)
            self.write(heartbeat_packet(0))
            self.heartbeats += 1

    def write(self, data: bytes) -> None:
        """Queue one or more whole packets (a packet is never split between writes)."""
        if self.closed.is_set() or self._writer.is_closing():
            raise ConnectionResetError(self.close_reason or "video connection closed")
        self._writer.write(data)
        self.bytes_sent += len(data)
        if not self._started:
            self._started = True
            self._tasks.append(asyncio.ensure_future(self._heartbeat()))

    async def drain(self) -> None:
        await self._writer.drain()

    @property
    def buffered(self) -> int:
        """Bytes written but not yet handed to the kernel."""
        return self._writer.transport.get_write_buffer_size()

    async def close(self, seconds: float = CLOSE_SECONDS) -> None:
        """Close the connection: what is buffered gets `seconds` to go, then the connection is
        dropped (a TV that reads nothing would otherwise keep it, and its buffer, open)."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
        self._tasks.clear()
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), seconds)
        except asyncio.TimeoutError:
            # Only then: once a close has sent what was buffered, asyncio's transport has let go
            # of its loop, and abort() would raise.
            self._writer.transport.abort()  # what is left is dropped
        except (OSError, ConnectionError):
            pass

    def __repr__(self) -> str:
        peer: Optional[tuple] = self._writer.get_extra_info("peername")
        return f"DataChannel({peer})"
