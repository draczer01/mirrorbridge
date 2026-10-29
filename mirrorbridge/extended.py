"""Extend screen for a session: the virtual monitor that a cast (session.py) or screen mirroring
(mirror/session.py) sends, so that the TV works as a second screen.

vdisplay.py drives the driver; this is what a session does with it, written once so that both
kinds of session do it alike:

* add the monitor at the Extended screen size and find it among the displays FFmpeg captures
  (ddagrab's output index, or its rectangle of the desktop for gdigrab);
* keep it for the whole session: a session that starts FFmpeg again (another way of capturing
  or encoding, or mirroring set up once more for the TV) captures the same monitor, since
  removing and adding it would move the windows on it back to the PC's own screen;
* say when it goes away under the session (sleep, a display change): `lost` turns true at once,
  on the driver's thread, and `on_lost` is called on the session's loop to end it;
* remove it when the session ends, however it ends. The session calls stop() once FFmpeg is
  gone: Desktop Duplication losing its output would turn a stop into an error.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Callable, Optional

from . import ffmpeg, vdisplay
from .config import Settings

_LOGGER = logging.getLogger(__name__)

LOOKUPS = 10  # the display list can lag a moment behind the driver
LOOKUP_SECONDS = 0.2
LOST_SECONDS = 2.5  # the driver's keep-alive notices a monitor that went away within about 2 s


def wanted(settings: Settings) -> bool:
    """Whether a session that sends the desktop sends the virtual screen: Extend screen is on,
    the capture is a real one (not FFmpeg's test source), and this Windows can have one."""
    return settings.extend_screen and settings.capture_backend != "test" and vdisplay.available()


def screen_settings(settings: Settings) -> Settings:
    """`settings` for sending the virtual screen. It is already the size the TV should get (the
    Extended screen size), so the display and window choices and Quality > Resolution do not
    apply; a window left chosen would otherwise make gdigrab capture that window."""
    return dataclasses.replace(settings, window_title="", output_height=0)


class ExtendedScreen:
    """One virtual monitor for the life of one session."""

    def __init__(self, on_lost: Callable[[], None]) -> None:
        self.on_lost = on_lost  # called on the session's loop if the monitor goes away
        self.lost = False  # the monitor went away under the session
        self.device = ""  # \\.\DISPLAYn while there is one
        self.monitor: Optional[ffmpeg.Monitor] = None  # as FFmpeg captures it
        self._screen: Optional[vdisplay.VirtualScreen] = None

    async def add(self, mode: str) -> str:
        """Add the monitor Windows extends the desktop onto, at the size of `mode` (a key of
        vdisplay.EXTEND_MODES), and return its device name (\\\\.\\DISPLAYn). The session makes
        that its virtual_device at once, before find(): Windows can list the new monitor while
        find() still waits for DXGI, and the tray's What to cast menu must already leave it out.
        Raises VddError when the driver is missing or broken ("driver:...")."""
        loop = asyncio.get_running_loop()

        def lost() -> None:  # called from the driver's keep-alive or presence-check thread
            self.lost = True
            try:
                loop.call_soon_threadsafe(self.on_lost)
            except RuntimeError:  # the loop is already closed
                pass

        # Kept before it starts, so that stop() can end a start that is still running.
        self._screen = vdisplay.backend()(on_lost=lost)
        width, height, hz = vdisplay.EXTEND_MODES.get(mode, vdisplay.EXTEND_MODES["1080p"])
        self.device = await loop.run_in_executor(None, self._screen.start, width, height, hz)
        return self.device

    async def find(self) -> ffmpeg.Monitor:
        """The monitor add() put on the desktop, as FFmpeg captures it. Raises
        VddError("not_attached") when Windows does not put it there."""
        loop = asyncio.get_running_loop()
        name = self.device
        for _ in range(LOOKUPS):
            monitors = await loop.run_in_executor(None, ffmpeg.list_monitors)  # DXGI is slow right after a mode change
            monitor = next((m for m in monitors if m.device.lower() == name.lower()), None)
            if monitor is not None:
                break
            await asyncio.sleep(LOOKUP_SECONDS)
        else:
            raise vdisplay.VddError("not_attached")
        if monitor.dxgi_output is None:
            _LOGGER.info("The virtual screen is drawn by another GPU (DXGI adapter %s)", monitor.dxgi_adapter)
        self.monitor = monitor
        return monitor

    async def lost_within(self, seconds: Optional[float] = None) -> bool:
        """After FFmpeg died: did the monitor go away under it? (Sleep and resume, for example:
        the driver's keep-alive notices within about 2 s, LOST_SECONDS.)"""
        deadline = time.monotonic() + (LOST_SECONDS if seconds is None else seconds)
        while not self.lost and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return self.lost

    async def stop(self) -> None:
        """Remove the monitor. Safe at any time, and more than once; a start still running
        gives up."""
        screen, self._screen = self._screen, None
        self.device, self.monitor = "", None
        if screen is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, screen.stop)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("Could not remove the virtual screen", exc_info=True)
