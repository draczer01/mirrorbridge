"""The best way to show the screen on each TV, chosen for the person.

A TV can be reached in more than one way: AirPlay screen mirroring, Google Cast screen mirroring
(Chromecast built-in), or the older AirPlay cast (HLS, a few seconds behind). The person sees
one TV and presses one button; this module decides the way.

    Screen        one TV, as AirPlay discovery and Google Cast discovery each found it,
                  joined by its IP address
    routes()      the ways a Screen takes, by what it says it can do
    RouteHistory  what worked and what failed with each TV, kept in routes.json, so the way
                  that worked last time is tried first and one that failed goes last
    plan()        the ways to try, best first: the controller starts the first and, when it
                  fails before the picture reaches the TV, the next one, without asking

The order with no history is the lowest delay first: AirPlay mirroring (proven on TCL, Roku,
Samsung and LG TVs), then Google Cast mirroring, then the AirPlay cast, which works with the most
receivers (Apple TVs included, until MirrorBridge has FairPlay).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .config import app_data_dir

if TYPE_CHECKING:
    from .devices import Device
    from .googlecast import CastDevice

_LOGGER = logging.getLogger(__name__)

AIRPLAY_MIRROR = "airplay_mirror"
CAST_MIRROR = "googlecast_mirror"
AIRPLAY_CAST = "airplay_cast"
ORDER = [AIRPLAY_MIRROR, CAST_MIRROR, AIRPLAY_CAST]  # lowest delay first, with no history
MIRRORING = {AIRPLAY_MIRROR, CAST_MIRROR}
# Ways whose session can say it is sending while the TV shows nothing: Google Cast mirroring
# reached CASTING on Humberto's TCL with the screen black (2026-10-29). Their CASTING is counted,
# but never makes them the way to try first.
UNCONFIRMED = {CAST_MIRROR}
FILE_NAME = "routes.json"
CAST_PREFIX = "cast:"


@dataclass
class Screen:
    """One TV, however it was found."""

    airplay: Optional["Device"] = None
    cast: Optional["CastDevice"] = None

    @property
    def identifier(self) -> str:
        """The AirPlay identifier when there is one (what settings and pairings already use)."""
        if self.airplay is not None:
            return self.airplay.identifier
        assert self.cast is not None
        return CAST_PREFIX + (self.cast.identifier or self.cast.host)

    @property
    def name(self) -> str:
        if self.airplay is not None:
            return self.airplay.name
        name = self.cast.name
        if _serial(name) or name == self.cast.host:  # a receiver with no name of its own (izzi's TV box)
            return f"{self.cast.model or 'Google Cast'} ({self.cast.host})"
        return name

    @property
    def host(self) -> str:
        return self.airplay.address if self.airplay is not None else self.cast.host

    @property
    def model(self) -> str:
        if self.airplay is not None:
            return self.airplay.model
        return self.cast.model or "Google Cast"

    @property
    def shows_video(self) -> bool:
        """It has a screen: not a speaker."""
        return bool(routes(self))

    def answers_to(self, identifier: str) -> bool:
        """`identifier` (a saved last_device_id) names this TV, by either of its ways."""
        if identifier == self.identifier:
            return True
        return self.cast is not None and identifier == CAST_PREFIX + (self.cast.identifier or self.cast.host)


def _serial(name: str) -> bool:
    """A name that is only a serial number, such as "1001B026D756D451"."""
    return len(name) >= 12 and all(ch in "0123456789abcdefABCDEF-" for ch in name)


def merge(devices: list["Device"], casts: list["CastDevice"]) -> list[Screen]:
    """The TVs, each once: a TV found by AirPlay and by Google Cast (the same IP address) is one."""
    screens = [Screen(airplay=device) for device in devices]
    by_host = {screen.host: screen for screen in screens}
    for cast in casts:
        screen = by_host.get(cast.host)
        if screen is not None and screen.cast is None:
            screen.cast = cast
        else:
            screens.append(Screen(cast=cast))
    return sorted(screens, key=lambda s: (not s.shows_video, s.name.lower()))


def airplay_mirrors(device: "Device") -> bool:
    """AirPlay screen mirroring: the receiver says it mirrors and does not need FairPlay."""
    from . import mirror  # pylint: disable=import-outside-toplevel

    if not device.can_cast_video:
        return False
    target = mirror.Target.from_device(device)
    return target.offers_mirroring and not target.needs_fairplay


def routes(screen: Screen) -> list[str]:
    """The ways `screen` takes, in ORDER."""
    found = []
    if screen.airplay is not None and airplay_mirrors(screen.airplay):
        found.append(AIRPLAY_MIRROR)
    if screen.cast is not None and screen.cast.has_video:
        found.append(CAST_MIRROR)
    if screen.airplay is not None and screen.airplay.can_cast_video:
        found.append(AIRPLAY_CAST)
    return found


class RouteHistory:
    """What each way did with each TV: {identifier: {"worked": route, route: {"ok", "failed",
    "last", "error", "at"}}}, in routes.json next to settings.json."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or app_data_dir() / FILE_NAME
        try:
            self.data: dict = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def _route(self, identifier: str, route: str) -> dict:
        tv = self.data.setdefault(identifier, {})
        return tv.setdefault(route, {"ok": 0, "failed": 0, "last": ""})

    def worked(self, identifier: str, route: str, confirmed: bool = False) -> None:
        """A way that reached the TV; an UNCONFIRMED way becomes the one to try first only when
        `confirmed` (the TV said it showed frames)."""
        entry = self._route(identifier, route)
        entry.update(ok=entry["ok"] + 1, last="ok", at=int(time.time()))
        entry.pop("error", None)
        if route not in UNCONFIRMED or confirmed:
            self.data[identifier]["worked"] = route
        self._save()

    def failed(self, identifier: str, route: str, error: str = "", lasting: bool = True) -> None:
        """A way that did not work. Only a `lasting` failure (the TV does not take it) sends it
        last next time; a passing one (the TV off, the network) is only noted."""
        entry = self._route(identifier, route)
        entry.update(failed=entry["failed"] + 1, error=error[:300], at=int(time.time()))
        if not lasting:
            self._save()
            return
        entry["last"] = "failed"
        if self.data[identifier].get("worked") == route:
            del self.data[identifier]["worked"]
        self._save()

    def last_worked(self, identifier: str) -> str:
        return self.data.get(identifier, {}).get("worked", "")

    def last(self, identifier: str, route: str) -> str:
        return self.data.get(identifier, {}).get(route, {}).get("last", "")

    def forget(self, identifier: str) -> None:
        if self.data.pop(identifier, None) is not None:
            self._save()

    def _save(self) -> None:
        try:
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as ex:
            _LOGGER.warning("Could not save %s: %s", self.path, ex)


def plan(screen: Screen, history: RouteHistory) -> list[str]:
    """The ways to try on `screen`, best first: the one that worked last time, then those that
    have not failed last time (lowest delay first), then those that did."""
    identifier = screen.identifier
    worked = history.last_worked(identifier)

    def rank(route: str) -> tuple:
        return (route != worked, history.last(identifier, route) == "failed", ORDER.index(route))

    return sorted(routes(screen), key=rank)


# Why a way failed, by the key of the Text its error carries
LASTING = {  # the TV does not take this way: try it last from now on
    "mirror_fairplay", "mirror_password", "mirror_refused", "mirror_no_data_port", "mirror_no_ptp",
    "mirror_data_refused", "mirror_data_closed", "mirror_no_timing", "gc_no_launch", "gc_no_answer",
    "tv_restricted", "tv_media_format", "tv_pairing_refused",
}
LOCAL = {  # this PC (FFmpeg, the capture): another way would fail the same, so none is tried
    "encoder_stopped", "ffmpeg_exited", "ffmpeg_no_video", "mirror_no_picture", "gc_no_picture",
}
PASSING, LASTING_FAILURE, LOCAL_FAILURE = "passing", "lasting", "local"


def error_key(exc: BaseException) -> str:
    """The key of the Text an error carries, or ""."""
    first = exc.args[0] if exc.args else None
    return getattr(first, "key", "") or ""


def classify(exc: BaseException) -> str:
    """LASTING_FAILURE (the TV does not take this way), LOCAL_FAILURE (this PC's side: the virtual
    screen driver, FFmpeg) or PASSING (the TV off, the network, a pairing to redo)."""
    from . import ffmpeg, vdisplay  # pylint: disable=import-outside-toplevel

    if isinstance(exc, (vdisplay.VddError, ffmpeg.FFmpegNotFound)):
        return LOCAL_FAILURE
    key = error_key(exc)
    if key in LOCAL:
        return LOCAL_FAILURE
    return LASTING_FAILURE if key in LASTING else PASSING
