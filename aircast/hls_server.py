"""Tiny HTTP server that hands the live HLS playlist and segments to the Apple TV."""

from __future__ import annotations

import logging
import os
import re
import time
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


class HlsServer:
    """Serves files from one directory; nothing outside it is reachable."""

    def __init__(self, root: Path, host: str, port: int) -> None:
        self.root = root.resolve()
        self.host = host
        self.port = port
        self.requests = 0
        self.last_request_at = 0.0  # time.monotonic() of the receiver's latest fetch
        self._runner: web.AppRunner | None = None

    @property
    def playlist_url(self) -> str:
        return f"http://{self.host}:{self.port}/stream.m3u8"

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = (self.root / name).resolve()
        if path.parent != self.root or path.suffix not in _CONTENT_TYPES or not path.is_file():
            raise web.HTTPNotFound()
        self.requests += 1
        self.last_request_at = time.monotonic()
        _LOGGER.debug("%s GET %s", request.remote, name)
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
        _LOGGER.info("Serving stream at %s", self.playlist_url)

    async def close(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
