"""Adapt pyatv's AirPlay video playback to live streams and to non-Apple receivers.

Two pyatv 0.18 behaviours get in the way of casting a screen:

* AirPlayPlayer polls /playback-info and gives up if no "duration" shows up within
  ~5 seconds. A live screen stream can take longer to start and may report no
  duration at all. The replacement wait is patient while starting and only ends when
  the receiver reports an error, the connection drops, or playback stops on the TV.
  Receivers that do not implement /playback-info at all are left to the session,
  which watches the TV's requests for video segments instead.

* AirPlayV2.play_url follows POST /play with a series of PUT /setProperty requests
  that Apple TVs accept but many AirPlay 2 TVs from other brands answer with
  "501 Not Implemented", and pyatv treats that as fatal. The replacement sends the
  same requests but only POST /play has to succeed; the extras are best effort.
"""

from __future__ import annotations

import asyncio
import logging
import plistlib

from pyatv import exceptions
from pyatv.protocols.airplay import player as _player
from pyatv.protocols.raop.protocols import airplayv2 as _airplayv2
from pyatv.support.http import decode_bplist_from_body

_LOGGER = logging.getLogger(__name__)

STARTUP_GRACE_SECONDS = 30
STOPPED_POLLS_BEFORE_END = 4
BLIND_POLL_SECONDS = 2


class ReceiverRefusedVideo(exceptions.PlaybackError):
    """The receiver answered POST /play with an error: it does not take video URLs."""


async def _wait_for_live_stream(self) -> None:  # replaces AirPlayPlayer._wait_for_media_to_end
    started = False
    waited = 0
    idle_polls = 0
    while True:
        try:
            resp = await self.rtsp.connection.get("/playback-info", allow_error=True)
            status = resp.code
        except (RuntimeError, exceptions.ConnectionLostError):
            _LOGGER.info("AirPlay connection closed by receiver")
            return
        except (TimeoutError, exceptions.AuthenticationError) as ex:
            resp, status = None, str(ex)

        if resp is None or not 200 <= resp.code < 300:
            # The TV does not report playback state. Keep the session open; the cast
            # session notices the end when the TV stops fetching video segments.
            _LOGGER.info(
                "The receiver does not report playback status (%s); "
                "watching its video requests instead", status,
            )
            await _keep_alive_blind(self)
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


async def _keep_alive_blind(self) -> None:
    """Hold the AirPlay session open until the receiver drops the connection.

    Nothing is sent here: pyatv's feedback task already pings an AirPlay 2 receiver
    every 2 seconds, and pyatv clears the transport when the connection closes.
    """
    while self.rtsp.connection.transport is not None:
        await asyncio.sleep(BLIND_POLL_SECONDS)
    _LOGGER.info("AirPlay connection closed by receiver")


async def _optional(self, method: str, uri: str, body=None) -> None:
    """Send a request the receiver may not implement; log instead of failing."""
    try:
        resp = await self.rtsp.exchange(method, uri=uri, body=body, allow_error=True)
    except (exceptions.HttpError, exceptions.AuthenticationError, TimeoutError) as ex:
        _LOGGER.debug("%s %s refused: %s", method, uri, ex)
        return
    if not 200 <= resp.code < 300:
        _LOGGER.debug("%s %s not supported by the receiver (HTTP %s)", method, uri, resp.code)


async def _play_url_v2(self, timing_server_port: int, url: str, position: float = 0.0):
    """AirPlayV2.play_url, tolerant of receivers that skip the optional requests."""
    await self._setup_base(timing_server_port)  # pylint: disable=protected-access
    await self.start_feedback()
    await self.rtsp.record()

    body = {
        "Content-Location": url,
        "Start-Position-Seconds": position,
        "uuid": self.uuid,
        "streamType": 1,
        "mediaType": "file",
        "mightSupportStorePastisKeyRequests": True,
        "playbackRestrictions": 0,
        "secureConnectionMs": 22,
        "volume": 1.0,
        "infoMs": 122,
        "connectMs": 18,
        "authMs": 0,
        "bonjourMs": 0,
        "referenceRestrictions": 3,
        "SenderMACAddress": "AA:BB:CC:DD:EE:FF",
        "model": "iPhone14,3",
        "postAuthMs": 0,
        "clientBundleID": "dev.pyatv.GPU",
        "clientProcName": "dev.pyatv.GPU",
        "osBuildVersion": "20G1116",
        "rate": 1.0,
    }
    resp = await self.rtsp.connection.post(
        "/play",
        headers=_airplayv2.HEADERS,
        body=plistlib.dumps(body, fmt=plistlib.FMT_BINARY),  # pylint: disable=no-member
        allow_error=True,
    )
    _LOGGER.info("Receiver answered /play with HTTP %s", resp.code)
    if resp.code == 500:
        return resp  # AirPlayPlayer retries "Internal Server Error"
    if not 200 <= resp.code < 300:
        raise ReceiverRefusedVideo(
            f"The TV refused to play video sent by URL (HTTP {resp.code} {resp.message}). "
            "Some AirPlay TVs from brands other than Apple only accept music and photos, "
            "or screen mirroring, from non-Apple devices."
        )

    # Apple TVs want these; other receivers often answer 501 Not Implemented. Only
    # /rate matters (playback may start paused without it), and only where supported.
    await _optional(self, "PUT", "/setProperty?isInterestedInDateRange", {"value": True})
    await _optional(self, "PUT", "/setProperty?actionAtItemEnd", {"value": 0})
    await _optional(self, "POST", "/rate?value=1.000000")
    end_time = {"value": {"flags": 0, "value": 0, "epoch": 0, "timescale": 0}}
    await _optional(self, "PUT", "/setProperty?forwardEndTime", end_time)
    await _optional(self, "PUT", "/setProperty?reverseEndTime", end_time)
    return resp


_applied = False


def apply() -> None:
    global _applied  # pylint: disable=global-statement
    if _applied:
        return
    if not hasattr(_player.AirPlayPlayer, "_wait_for_media_to_end") or not hasattr(
        _airplayv2.AirPlayV2, "_setup_base"
    ):
        _LOGGER.warning("pyatv internals changed; AirPlay playback patches not applied")
        return
    _player.AirPlayPlayer._wait_for_media_to_end = _wait_for_live_stream  # pylint: disable=protected-access
    _airplayv2.AirPlayV2.play_url = _play_url_v2
    _applied = True
