"""Mute this PC's speakers while the TV plays its sound, and put them back after.

The system audio a mirror sends is recorded with WASAPI loopback, which records the mix
before the speakers' own mute, so the TV keeps the sound while the PC is silent: no echo of
the same sound a fraction of a second apart. Muting is what the volume icon does
(IAudioEndpointVolume::SetMute on the default output), through COM with ctypes, as
pycaw would. Speakers the person had muted already are left as they are.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from typing import Optional

_LOGGER = logging.getLogger(__name__)

CLSID_MM_DEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMM_DEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
CLSCTX_ALL = 0x17
E_RENDER, E_CONSOLE = 0, 0
# Places in the COM interfaces' method tables (IUnknown's three first)
RELEASE = 2
GET_DEFAULT_AUDIO_ENDPOINT = 4  # IMMDeviceEnumerator
ACTIVATE = 3  # IMMDevice
SET_MUTE, GET_MUTE = 14, 15  # IAudioEndpointVolume


class _Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text: str) -> "_Guid":
        guid = cls()
        ctypes.memmove(guid.data, uuid.UUID(text).bytes_le, 16)
        return guid


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])


def _check(hresult: int, what: str) -> None:
    if hresult < 0:
        raise OSError(f"{what} failed (0x{hresult & 0xFFFFFFFF:08X})")


class _Volume:
    """The default output's IAudioEndpointVolume."""

    def __init__(self) -> None:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)  # S_FALSE (already) is fine
        enumerator, device, volume = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        _check(ole32.CoCreateInstance(ctypes.byref(_Guid.of(CLSID_MM_DEVICE_ENUMERATOR)), None, CLSCTX_ALL,
                                      ctypes.byref(_Guid.of(IID_IMM_DEVICE_ENUMERATOR)), ctypes.byref(enumerator)),
               "CoCreateInstance(MMDeviceEnumerator)")
        try:
            _check(_method(enumerator, GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_int, ctypes.c_int,
                           ctypes.POINTER(ctypes.c_void_p))(enumerator, E_RENDER, E_CONSOLE, ctypes.byref(device)),
                   "GetDefaultAudioEndpoint")
            try:
                _check(_method(device, ACTIVATE, ctypes.POINTER(_Guid), ctypes.c_ulong, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p))(device, ctypes.byref(_Guid.of(IID_IAUDIO_ENDPOINT_VOLUME)),
                                                                CLSCTX_ALL, None, ctypes.byref(volume)),
                       "Activate(IAudioEndpointVolume)")
            finally:
                _method(device, RELEASE)(device)
        finally:
            _method(enumerator, RELEASE)(enumerator)
        self._volume = volume

    def muted(self) -> bool:
        state = ctypes.c_int()
        _check(_method(self._volume, GET_MUTE, ctypes.POINTER(ctypes.c_int))(self._volume, ctypes.byref(state)),
               "GetMute")
        return bool(state.value)

    def set_muted(self, muted: bool) -> None:
        _check(_method(self._volume, SET_MUTE, ctypes.c_int, ctypes.c_void_p)(self._volume, int(muted), None),
               "SetMute")

    def release(self) -> None:
        if self._volume:
            _method(self._volume, RELEASE)(self._volume)
            self._volume = ctypes.c_void_p()


class MutedSpeakers:
    """mute() at the start, restore() at the end (both safe to call when nothing was done)."""

    def __init__(self) -> None:
        self._volume: Optional[_Volume] = None

    def mute(self) -> bool:
        """Mute the default output if it is not muted; True if this did it."""
        if sys.platform != "win32" or self._volume is not None:
            return False
        try:
            volume = _Volume()
            if volume.muted():
                volume.release()
                return False
            volume.set_muted(True)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Could not mute this PC's speakers: %s", ex)
            return False
        self._volume = volume
        _LOGGER.info("This PC's speakers are muted while the TV plays the sound")
        return True

    def restore(self) -> None:
        volume, self._volume = self._volume, None
        if volume is None:
            return
        try:
            volume.set_muted(False)
            _LOGGER.info("This PC's speakers are back on")
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Could not turn this PC's speakers back on: %s", ex)
        finally:
            volume.release()
