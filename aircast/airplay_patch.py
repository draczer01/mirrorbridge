"""Keep pyatv's AirPlay video session alive for a live (endless) HLS stream.

pyatv's AirPlayPlayer polls /playback-info and gives up if no "duration" shows up
within ~5 seconds. A live screen stream can take longer to start and may report no
duration at all, which would close the connection and stop the cast. This replaces
that wait with one that is patient while starting and only ends when the receiver
reports an error, the connection drops, or the user stops playback on the TV.
"""

from __future__ import annotations

import asyncio
import logging

from pyatv import exceptions
from pyatv.protocols.airplay import player as _player
from pyatv.support.http import decode_bplist_from_body

_LOGGER = logging.getLogger(__name__)

STARTUP_GRACE_SECONDS = 30
STOPPED_POLLS_BEFORE_END = 4


async def _wait_for_live_stream(self) -> None:  # replaces AirPlayPlayer._wait_for_media_to_end
    started = False
    waited = 0
    idle_polls = 0
    while True:
        try:
            resp = await self.rtsp.connection.get("/playback-info")
        except (RuntimeError, exceptions.ConnectionLostError):
            _LOGGER.info("AirPlay connection closed by receiver")
            return

        parsed = decode_bplist_from_body(resp) if resp.body else {}
        if "error" in parsed:
            err = parsed["error"]
            raise exceptions.PlaybackError(
                f"Receiver error {err.get('code', '?')} ({err.get('domain', '?')})"
            )

        playing = "duration" in parsed or parsed.get("readyToPlay") or parsed.get("rate", 0) > 0
        if playing:
            if not started:
                _LOGGER.info("Apple TV started playing the stream")
            started = True
            idle_polls = 0
        elif started:
            idle_polls += 1
            if idle_polls >= STOPPED_POLLS_BEFORE_END:
                _LOGGER.info("Playback stopped on the receiver")
                return
        else:
            waited += 1
            if waited > STARTUP_GRACE_SECONDS:
                raise exceptions.PlaybackError(
                    "The receiver never started playing. Check that Windows Firewall "
                    "allows AirCast and that both devices are on the same network."
                )
        await asyncio.sleep(1)


_applied = False


def apply() -> None:
    global _applied  # pylint: disable=global-statement
    if _applied:
        return
    if not hasattr(_player.AirPlayPlayer, "_wait_for_media_to_end"):
        _LOGGER.warning("pyatv internals changed; live-stream keepalive patch not applied")
        return
    _player.AirPlayPlayer._wait_for_media_to_end = _wait_for_live_stream  # pylint: disable=protected-access
    _applied = True
