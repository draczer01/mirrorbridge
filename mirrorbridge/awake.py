"""Keep this PC and its screen awake while the screen is on a TV, and let them sleep again after.

Windows turns the screen off after some minutes without the keyboard or the mouse (10 on many
PCs), and puts the PC to sleep a while later. A screen that is off gives the screen capture no
picture: on Humberto's PC, with its screens off after 15 hours unused, Mirror screen got not one
frame from ddagrab and ended with "No picture came from the screen capture". A video player
keeps the screen on while it plays; a slide show, a PDF or a still desktop does not. So while a
session runs, MirrorBridge asks Windows to keep the screen on and the PC from sleeping, as a
video player does: a power request (PowerCreateRequest, PowerSetRequest), which `powercfg
/requests` lists with its reason. Starting it also resets the screen's idle timer
(SetThreadExecutionState with ES_DISPLAY_REQUIRED), as a key press would.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional

_LOGGER = logging.getLogger(__name__)

POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
POWER_REQUEST_DISPLAY_REQUIRED = 0
POWER_REQUEST_SYSTEM_REQUIRED = 1
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _Detailed(ctypes.Structure):
    _fields_ = [
        ("LocalizedReasonModule", wintypes.HMODULE),
        ("LocalizedReasonId", wintypes.ULONG),
        ("ReasonStringCount", wintypes.ULONG),
        ("ReasonStrings", ctypes.POINTER(wintypes.LPWSTR)),
    ]


class _Reason(ctypes.Union):
    _fields_ = [("Detailed", _Detailed), ("SimpleReasonString", wintypes.LPWSTR)]


class _ReasonContext(ctypes.Structure):
    """REASON_CONTEXT, with a plain string."""

    _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Reason", _Reason)]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
    kernel32.PowerCreateRequest.restype = wintypes.HANDLE
    kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    kernel32.PowerSetRequest.restype = wintypes.BOOL
    kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    kernel32.PowerClearRequest.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    kernel32.SetThreadExecutionState.restype = wintypes.DWORD
    return kernel32


def _last_error() -> int:
    """Windows' error code for the last failed call (0 off Windows, where a test stands in)."""
    return ctypes.get_last_error() if sys.platform == "win32" else 0


class KeepAwake:
    """start() when the screen goes to the TV, stop() when it ends (both safe to call when nothing
    was done, and stop() more than once). `display`: the screen too, not only the PC (a test
    pattern needs no screen)."""

    def __init__(self, reason: str, display: bool = True, kernel32=None) -> None:
        self.reason = reason
        self.display = display
        self._kernel32 = kernel32
        self._handle: Optional[int] = None
        self._set: list[int] = []
        self._context: Optional[_ReasonContext] = None  # kept alive with the request

    def start(self) -> bool:
        """Ask Windows to keep the PC (and the screen) awake; True if it took the request."""
        if self._handle is not None or (self._kernel32 is None and sys.platform != "win32"):
            return False
        try:
            kernel32 = self._kernel32 = self._kernel32 or _kernel32()
            if self.display:
                kernel32.SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
            context = _ReasonContext(POWER_REQUEST_CONTEXT_VERSION, POWER_REQUEST_CONTEXT_SIMPLE_STRING)
            context.Reason.SimpleReasonString = self.reason
            handle = kernel32.PowerCreateRequest(ctypes.byref(context))
            if not handle or handle == INVALID_HANDLE_VALUE:
                raise OSError(f"PowerCreateRequest failed ({_last_error()})")
            self._handle, self._context = handle, context
            kinds = [POWER_REQUEST_SYSTEM_REQUIRED] + ([POWER_REQUEST_DISPLAY_REQUIRED] if self.display else [])
            for kind in kinds:
                if not kernel32.PowerSetRequest(handle, kind):
                    raise OSError(f"PowerSetRequest({kind}) failed ({_last_error()})")
                self._set.append(kind)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Could not keep this PC awake while mirroring: %s", ex)
            self.stop()
            return False
        _LOGGER.info("Keeping this PC%s awake while mirroring", " and its screen" if self.display else "")
        return True

    def stop(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        kernel32 = self._kernel32
        try:
            for kind in self._set:
                kernel32.PowerClearRequest(handle, kind)
            kernel32.CloseHandle(handle)
            _LOGGER.info("This PC may sleep again")
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Could not let this PC sleep again: %s", ex)
        finally:
            self._set, self._context = [], None
