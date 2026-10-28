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
_VARIANT_RE = re.compile(rb"^#EXT-X-STREAM-INF:[^\r\n]*", re.M)
_BANDWIDTH_RE = re.compile(rb"[:,]BANDWIDTH=(\d+)")
MISSING_LOG_LIMIT = 50  # distinct refused requests logged at INFO; the rest only at DEBUG


def fix_target_duration(playlist: bytes) -> bytes:
    """FFmpeg writes TARGETDURATION:0 for sub-second segments; Apple players need >= 1."""
    return _TARGET_RE.sub(lambda m: b"#EXT-X-TARGETDURATION:" + (m.group(1) if int(m.group(1)) >= 1 else b"1"), playlist)


def complete_variants(playlist: bytes, frame_rate: float, average_bandwidth: int) -> bytes:
    """Add FRAME-RATE and AVERAGE-BANDWIDTH to a master playlist's variants.

    Apple's HLS rules for AirPlay 2 TVs ask for both. FFmpeg never writes FRAME-RATE,
    and writes AVERAGE-BANDWIDTH only when the stream ends. The average is never put
    above the peak BANDWIDTH that FFmpeg declared.
    """
    def complete(match: re.Match) -> bytes:
        line = match.group(0)
        if b"FRAME-RATE=" not in line:
            line += b",FRAME-RATE=%.3f" % frame_rate
        if b"AVERAGE-BANDWIDTH=" not in line:
            peak = _BANDWIDTH_RE.search(line)
            average = min(average_bandwidth, int(peak.group(1))) if peak else average_bandwidth
            line += b",AVERAGE-BANDWIDTH=%d" % average
        return line

    return _VARIANT_RE.sub(complete, playlist)


@dataclass
class Activity:
    """What one client (or all of them together) fetched, as time.monotonic() values."""

    requests: int = 0  # files served
    user_agent: str = ""
    first_playlist_at: float | None = None
    first_segment_at: float | None = None
    last_request_at: float = 0.0
    missing: int = 0  # requests answered 404/405: the client reached MirrorBridge, but asked for something else
    missing_path: str = ""  # the first of them


class HlsServer:
    """Serves files from one directory; nothing outside it is reachable.

    With frame_rate and average_bandwidth, a master playlist is served with them added
    to its variants (see complete_variants).
    """

    def __init__(
        self, root: Path, host: str, port: int, *, frame_rate: float = 0, average_bandwidth: int = 0
    ) -> None:
        self.root = root.resolve()
        self.host = host
        self.port = port
        self.frame_rate = frame_rate
        self.average_bandwidth = average_bandwidth
        self.requests = 0
        self.last_request_at = 0.0  # time.monotonic() of the receiver's latest fetch
        self._clients: dict[str, Activity] = {}
        self._missing_logged: set[tuple[str, str]] = set()
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
            missing=sum(c.missing for c in clients),
            missing_path=next((c.missing_path for c in clients if c.missing_path), ""),
        )

    def clients(self) -> dict[str, Activity]:
        """Every client that reached the server, by IP."""
        return dict(self._clients)

    def _client(self, request: web.Request) -> tuple[str, Activity]:
        ip = request.remote or "?"
        client = self._clients.get(ip)
        if client is None:
            # The User-Agent names the TV's video player (AppleCoreMedia, ExoPlayer...).
            client = self._clients[ip] = Activity(user_agent=request.headers.get("User-Agent", ""))
        return ip, client

    def rewrite_playlist(self, body: bytes) -> bytes:
        """A playlist as it is served."""
        body = fix_target_duration(body)
        if self.frame_rate and self.average_bandwidth:
            body = complete_variants(body, self.frame_rate, self.average_bandwidth)
        return body

    def _record(self, request: web.Request, name: str) -> None:
        now = time.monotonic()
        self.requests += 1
        self.last_request_at = now
        ip, client = self._client(request)
        if not client.requests:
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

    def _record_missing(self, request: web.Request, status: int) -> None:
        """A request the server could not answer. It still shows that the TV reached the PC."""
        ip, client = self._client(request)
        client.missing += 1
        client.missing_path = client.missing_path or request.raw_path
        key = (ip, f"{request.method} {request.raw_path}")
        if key not in self._missing_logged and len(self._missing_logged) < MISSING_LOG_LIMIT:
            self._missing_logged.add(key)
            _LOGGER.info("%s asked for %s, which MirrorBridge does not serve (HTTP %s, User-Agent: %s)",
                         ip, key[1], status, client.user_agent or "none")
        else:
            _LOGGER.debug("%s %s: HTTP %s", ip, key[1], status)

    @web.middleware
    async def _note_missing(self, request: web.Request, handler) -> web.StreamResponse:
        # Also sees what aiohttp refuses before any handler runs (other paths and methods).
        try:
            return await handler(request)
        except web.HTTPException as ex:
            if ex.status in (404, 405):
                self._record_missing(request, ex.status)
            raise

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
            body = self.rewrite_playlist(body)
        return web.Response(
            body=body,
            content_type=_CONTENT_TYPES[path.suffix],
            headers={"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"},
        )

    async def start(self) -> None:
        app = web.Application(middlewares=[self._note_missing])
        app.router.add_get("/{name}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # On Windows SO_REUSEADDR would let a second MirrorBridge bind the same port silently,
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
