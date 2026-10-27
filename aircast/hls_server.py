"""Tiny HTTP server that hands the live HLS playlist and segments to the Apple TV."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
}


_TARGET_RE = re.compile(rb"#EXT-X-TARGETDURATION:(\d+)")


def fix_target_duration(playlist: bytes) -> bytes:
    """FFmpeg writes TARGETDURATION:0 for sub-second segments; Apple players need >= 1."""
    return _TARGET_RE.sub(lambda m: b"#EXT-X-TARGETDURATION:" + (m.group(1) if int(m.group(1)) >= 1 else b"1"), playlist)


@dataclass
class Activity:
    """What one client (or all of them together) fetched, as time.monotonic() values."""

    requests: int = 0
    user_agent: str = ""
    first_playlist_at: float | None = None
    first_segment_at: float | None = None
    last_request_at: float = 0.0


class HlsServer:
    """Serves files from one directory; nothing outside it is reachable."""

    def __init__(self, root: Path, host: str, port: int) -> None:
        self.root = root.resolve()
        self.host = host
        self.port = port
        self.requests = 0
        self.last_request_at = 0.0  # time.monotonic() of the receiver's latest fetch
        self._clients: dict[str, Activity] = {}
        self._runner: web.AppRunner | None = None

    def url(self, name: str = "stream.m3u8") -> str:
        return f"http://{self.host}:{self.port}/{name}"

    @property
    def playlist_url(self) -> str:
        return self.url()

    def activity(self, ip: str | None = None) -> Activity:
        """Fetches by one client IP, or by every client when ip is None."""
        if ip is not None:
            return self._clients.get(ip, Activity())
        clients = list(self._clients.values())

        def first(attr: str) -> float | None:
            return min((t for t in (getattr(c, attr) for c in clients) if t is not None), default=None)

        return Activity(
            requests=sum(c.requests for c in clients),
            user_agent=", ".join(sorted({c.user_agent for c in clients if c.user_agent})),
            first_playlist_at=first("first_playlist_at"),
            first_segment_at=first("first_segment_at"),
            last_request_at=max((c.last_request_at for c in clients), default=0.0),
        )

    def _record(self, request: web.Request, name: str) -> None:
        now = time.monotonic()
        self.requests += 1
        self.last_request_at = now
        ip = request.remote or "?"
        client = self._clients.get(ip)
        if client is None:
            # The User-Agent names the TV's video player (AppleCoreMedia, ExoPlayer...).
            client = self._clients[ip] = Activity(user_agent=request.headers.get("User-Agent", ""))
            _LOGGER.info("%s started fetching the stream (User-Agent: %s)", ip, client.user_agent or "none")
        client.requests += 1
        client.last_request_at = now
        if name.endswith(".m3u8"):
            if client.first_playlist_at is None:
                client.first_playlist_at = now
                _LOGGER.info("%s fetched its first playlist, %s", ip, name)
        elif client.first_segment_at is None:
            client.first_segment_at = now
            after = f"{now - client.first_playlist_at:.1f} s after its first playlist" if client.first_playlist_at else "before any playlist"
            _LOGGER.info("%s fetched its first video segment, %s, %s", ip, name, after)
        _LOGGER.debug("%s GET %s", ip, name)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = (self.root / name).resolve()
        if path.parent != self.root or path.suffix not in _CONTENT_TYPES or not path.is_file():
            raise web.HTTPNotFound()
        self._record(request, name)
        try:
            body = path.read_bytes()
        except OSError as ex:  # segment deleted between check and read
            raise web.HTTPNotFound() from ex
        if path.suffix == ".m3u8":
            body = fix_target_duration(body)
        return web.Response(
            body=body,
            content_type=_CONTENT_TYPES[path.suffix],
            headers={"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"},
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/{name}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # On Windows SO_REUSEADDR would let a second AirCast bind the same port silently,
        # so the "port busy" fallback in session.py would never run.
        site = web.TCPSite(self._runner, self.host, self.port, reuse_address=os.name == "posix")
        await site.start()
        if self.port == 0:  # pick up the OS-assigned port
            self.port = site._server.sockets[0].getsockname()[1]  # pylint: disable=protected-access
        _LOGGER.info("Serving the stream from %s", self.url(""))

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
