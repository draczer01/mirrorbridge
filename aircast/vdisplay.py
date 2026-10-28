"""Extend screen: a temporary virtual monitor that the TV shows as a second screen.

Windows has no built-in way for an app to add a monitor, so AirCast drives the Parsec Virtual
Display Driver: a Microsoft-signed IddCx (UMDF) driver that the user installs once, with one
UAC prompt, from Parsec's official installer. After that a normal (asInvoker) process controls
it with IOCTLs. The driver removes all its monitors about 1 s after the last keep-alive ping, so
a crashed or killed AirCast leaves nothing behind.

Driver facts (nomi-san/parsec-vdd core/parsec-vdd.h, app/Vdd/Core.cs, docs/PARSEC_VDD_RE.md, and
the driver's own mm.inf):
* root device Root\\Parsec\\VDA in the Display class, device interface
  {00b41627-04c4-429e-a26e-0265cf50c8fa}; the INF gives Everyone read/write access to it;
* every IOCTL takes a 32-byte zeroed input buffer and returns a DWORD: ADD -> monitor index
  0..15, REMOVE with in[1] = index, UPDATE = keep-alive ping, VERSION -> major << 16 | minor;
* a new monitor starts at 1920x1080@60. Its device ID contains "PSCCDD0" and "UID<256 + index>",
  and its source name (\\\\.\\DISPLAYn) is the one EnumDisplayMonitors and DXGI use.

The driver sits behind VirtualScreen (status / start / stop), so another driver (Amyuni's
usbmmidd, for example) can be added without touching the cast session. The DLLs load on
first use, so this module imports on any OS.
"""

from __future__ import annotations

import abc
import atexit
import ctypes
import enum
import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from . import __version__
from .config import app_data_dir
from .winmonitors import _DEVMODEW, _GUID

_LOGGER = logging.getLogger(__name__)


def _guid(d1: int, d2: int, d3: int, d4: tuple[int, ...]) -> _GUID:
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


# -- Parsec Virtual Display Driver --------------------------------------------

VDD_INTERFACE_GUID = _guid(0x00B41627, 0x04C4, 0x429E, (0xA2, 0x6E, 0x02, 0x65, 0xCF, 0x50, 0xC8, 0xFA))
GUID_DEVCLASS_DISPLAY = _guid(0x4D36E968, 0xE325, 0x11CE, (0xBF, 0xC1, 0x08, 0x00, 0x2B, 0xE1, 0x03, 0x18))
VDD_HARDWARE_ID = "Root\\Parsec\\VDA"
VDD_MONITOR_ID = "PSCCDD0"  # EDID manufacturer/product code in the monitor's device ID
VDD_UID_BASE = 0x100  # the monitor's UID in its device ID is 0x100 + driver index
VDD_MAX_DISPLAYS = 16  # per adapter

IOCTL_VDD_ADD = 0x0022E004  # CTL_CODE(0x22, 0x801, METHOD_BUFFERED, FILE_READ_ACCESS | FILE_WRITE_ACCESS)
IOCTL_VDD_REMOVE = 0x0022A008  # CTL_CODE(0x22, 0x802, METHOD_BUFFERED, FILE_WRITE_ACCESS)
IOCTL_VDD_UPDATE = 0x0022A00C  # CTL_CODE(0x22, 0x803, METHOD_BUFFERED, FILE_WRITE_ACCESS)
IOCTL_VDD_VERSION = 0x0022E010  # CTL_CODE(0x22, 0x804, METHOD_BUFFERED, FILE_READ_ACCESS | FILE_WRITE_ACCESS)
VDD_IN_BUFFER_SIZE = 32
PING_INTERVAL_S = 0.1  # ParsecDisplay pings every 100 ms; the driver gives up after ~1 s of silence
PRESENCE_INTERVAL_S = 1.0  # how often a cast checks that Windows still has its monitor
EXTEND_AFTER_S = 2.0  # how long Windows gets to put a new monitor on the desktop by itself

# Monitors' device interface; its paths are the monitor IDs EnumDisplayDevicesW reports.
GUID_DEVINTERFACE_MONITOR = _guid(0xE6F07B5F, 0xEE97, 0x4A90, (0xB0, 0x76, 0x33, 0xF5, 0x7B, 0xF4, 0xEA, 0xA7))

# Parsec's official installer (signed by Parsec Cloud, Inc.), pinned to the winget manifest of
# Parsec.ParsecVDD 0.45.0.0. Parsec's licence does not allow bundling it, so it is downloaded.
VDD_INSTALLER_URL = "https://builds.parsec.app/vdd/parsec-vdd-0.45.0.0.exe"
VDD_INSTALLER_SHA256 = "e23332448fdaf5aa017cb308db5ef6855fac526a7ded05d80c039404126d5362"
VDD_INSTALLER_ARGS = "/S"  # NSIS silent install
VDD_INSTALLER_MAX_BYTES = 8 * 1024 * 1024  # the real file is about 500 KB
VDD_WINGET_ID = "Parsec.ParsecVDD"
PARSEC_TERMS_URL = "https://parsec.app/terms"

# The sizes the user can pick: (width, height, refresh rate). 720p is AirParrot's "force 720p"
# (bigger text on the TV and a lighter stream); 4K is a preset of the driver.
EXTEND_MODES = {"1080p": (1920, 1080, 60), "720p": (1280, 720, 60), "4k": (3840, 2160, 60)}

STATE_FILE = "vdd_state.json"  # monitors this user's AirCast added and has not removed yet

# -- Win32 ------------------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_NO_BUFFERING = 0x20000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_ACCESS_DENIED = 5
ERROR_IO_PENDING = 997
ERROR_CANCELLED = 1223
WAIT_TIMEOUT = 258
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_PRIORITY_HIGHEST = 2

CR_SUCCESS = 0
CR_BUFFER_SMALL = 0x1A
CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0
DIGCF_PRESENT = 0x2
SPDRP_HARDWAREID = 0x1
DN_DRIVER_LOADED = 0x00000002
DN_STARTED = 0x00000008
DN_HAS_PROBLEM = 0x00000400
CM_PROB_NEED_RESTART = 0x0E
CM_PROB_DISABLED = 0x16
CM_PROB_HARDWARE_DISABLED = 0x1D
CM_PROB_DISABLED_SERVICE = 0x20
CM_PROB_FAILED_POST_START = 0x2B

EDD_GET_DEVICE_INTERFACE_NAME = 0x1
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1  # on a source (\\.\DISPLAYn)
DISPLAY_DEVICE_ACTIVE = 0x1  # on a monitor
DISPLAY_DEVICE_ATTACHED = 0x2  # on a monitor
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000
CDS_UPDATEREGISTRY = 0x1
DISP_CHANGE_SUCCESSFUL = 0
SDC_TOPOLOGY_EXTEND = 0x00000004
SDC_APPLY = 0x00000080

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SW_HIDE = 0
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
_CREATE_NEW_CONSOLE = 0x00000010

# IOCTL buffers of requests that never completed (see VddDevice.ioctl). Deliberately never freed.
_LEAKED: list = []


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]  # sizeof == 32 on x64


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]  # sizeof == 32 on x64


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]  # sizeof == 840


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]  # sizeof == 112 on x64


# -- errors ----------------------------------------------------------------------


class DriverStatus(enum.Enum):
    OK = "ok"
    NOT_INSTALLED = "not_installed"
    DISABLED = "disabled"
    RESTART_REQUIRED = "restart_required"
    DRIVER_ERROR = "driver_error"
    UNKNOWN_PROBLEM = "unknown_problem"
    INACCESSIBLE = "inaccessible"


_BROKEN = "The virtual screen driver is not responding. Reinstall it with 'aircast vdd install'."
_MESSAGES = {
    "driver:not_installed": "The Parsec Virtual Display Driver is not installed. Choose Extend screen "
                            "in the app, or run 'aircast vdd install'.",
    "driver:disabled": "The virtual screen is turned off in Windows. Enable it in Device Manager > "
                       "Display adapters > Parsec Virtual Display Adapter.",
    "driver:restart_required": "Restart Windows to use the virtual screen.",
    "driver:driver_error": _BROKEN,
    "driver:unknown_problem": _BROKEN,
    "driver:inaccessible": _BROKEN,
    "add_failed": "Windows did not allow another virtual screen.",
    "not_attached": 'Windows did not turn on the virtual screen. Press Win+P, choose "Extend" and try again.',
    "lost": "The virtual screen closed (for example, when the PC went to sleep). Start casting again.",
    "download": "Could not download the virtual screen driver",
    "mismatch": "The downloaded file is not the expected driver installer, so it was not run.",
    "install": "Could not run the virtual screen driver installer",
    "cancelled": "Cancelled",
    "unsupported": "Virtual screens need Windows 10 or 11",
}


class VddError(RuntimeError):
    """A virtual screen problem. `code` says which one, so the app can explain it in the user's
    language: "driver:<DriverStatus value>", add_failed, not_attached, lost, download, mismatch,
    install, cancelled or unsupported."""

    def __init__(self, code: str, detail: str = "") -> None:
        message = _MESSAGES.get(code, code)
        if detail:
            message = f"{message} ({detail})" if message.endswith(".") else f"{message}: {detail}"
        super().__init__(message)
        self.code = code
        self.detail = detail

    @property
    def status(self) -> Optional[DriverStatus]:
        """The driver status behind a "driver:..." error, else None."""
        prefix, _, value = self.code.partition(":")
        return DriverStatus(value) if prefix == "driver" else None


# -- DLL bindings (loaded on first use, so the module imports on any OS) -------------

_api: Optional[SimpleNamespace] = None
_api_lock = threading.Lock()


def available() -> bool:
    """Whether this OS can have a virtual screen at all (the driver may still be missing)."""
    return sys.platform == "win32"


def _dlls() -> SimpleNamespace:
    global _api  # pylint: disable=global-statement
    with _api_lock:
        if _api is None:
            if not available():
                raise VddError("unsupported")
            _api = _bind()
        return _api


def _bind() -> SimpleNamespace:
    # Own WinDLL instances, so these prototypes never clash with ctypes.windll users elsewhere.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    cfg = ctypes.WinDLL("cfgmgr32")
    sapi = ctypes.WinDLL("setupapi", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    sh32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    dword_p = ctypes.POINTER(wintypes.DWORD)
    ulong_p = ctypes.POINTER(wintypes.ULONG)

    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, dword_p, ctypes.POINTER(OVERLAPPED)]
    k32.GetOverlappedResultEx.restype = wintypes.BOOL
    k32.GetOverlappedResultEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), dword_p,
                                          wintypes.DWORD, wintypes.BOOL]
    k32.CancelIoEx.restype = wintypes.BOOL
    k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, dword_p]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetCurrentThread.restype = wintypes.HANDLE
    k32.GetCurrentThread.argtypes = []
    k32.SetThreadPriority.restype = wintypes.BOOL
    k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    k32.GetTickCount64.restype = ctypes.c_uint64
    k32.GetTickCount64.argtypes = []

    cfg.CM_Get_Device_Interface_List_SizeW.restype = wintypes.ULONG
    cfg.CM_Get_Device_Interface_List_SizeW.argtypes = [ulong_p, ctypes.POINTER(_GUID), wintypes.LPCWSTR,
                                                       wintypes.ULONG]
    cfg.CM_Get_Device_Interface_ListW.restype = wintypes.ULONG
    cfg.CM_Get_Device_Interface_ListW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, ctypes.c_void_p,
                                                  wintypes.ULONG, wintypes.ULONG]
    cfg.CM_Get_DevNode_Status.restype = wintypes.ULONG
    cfg.CM_Get_DevNode_Status.argtypes = [ulong_p, ulong_p, wintypes.DWORD, wintypes.ULONG]

    sapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    sapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    sapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    sapi.SetupDiEnumDeviceInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    sapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    sapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA),
                                                       wintypes.DWORD, dword_p, ctypes.c_void_p,
                                                       wintypes.DWORD, dword_p]
    sapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    sapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

    u32.EnumDisplayDevicesW.restype = wintypes.BOOL
    u32.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW),
                                        wintypes.DWORD]
    u32.EnumDisplaySettingsW.restype = wintypes.BOOL
    u32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW)]
    u32.ChangeDisplaySettingsExW.restype = wintypes.LONG
    u32.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_DEVMODEW), wintypes.HWND,
                                             wintypes.DWORD, ctypes.c_void_p]
    u32.SetDisplayConfig.restype = wintypes.LONG
    u32.SetDisplayConfig.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32]

    sh32.ShellExecuteExW.restype = wintypes.BOOL
    sh32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]

    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoUninitialize.restype = None
    ole32.CoUninitialize.argtypes = []
    return SimpleNamespace(kernel32=k32, cfgmgr32=cfg, setupapi=sapi, user32=u32, shell32=sh32, ole32=ole32)


# -- driver detection ------------------------------------------------------------------


def interface_paths(guid: _GUID = VDD_INTERFACE_GUID) -> list[str]:
    """Device interface paths of present devices that expose `guid`."""
    cfg = _dlls().cfgmgr32
    for _ in range(3):  # the list can grow between the two calls
        size = wintypes.ULONG(0)
        if cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(guid), None,
                                                  CM_GET_DEVICE_INTERFACE_LIST_PRESENT) != CR_SUCCESS:
            return []
        if size.value <= 1:
            return []
        buf = ctypes.create_unicode_buffer(size.value)
        rc = cfg.CM_Get_Device_Interface_ListW(ctypes.byref(guid), None, buf, size.value,
                                               CM_GET_DEVICE_INTERFACE_LIST_PRESENT)
        if rc == CR_BUFFER_SMALL:
            continue
        if rc != CR_SUCCESS:
            return []
        raw = ctypes.wstring_at(ctypes.addressof(buf), size.value)
        return [p for p in raw.split("\0") if p]
    return []


def driver_status() -> DriverStatus:
    """Find Root\\Parsec\\VDA among the Display-class devices and read its devnode status
    (the same mapping as parsec-vdd's QueryDeviceStatus)."""
    api = _dlls()
    sapi, cfg = api.setupapi, api.cfgmgr32
    devs = sapi.SetupDiGetClassDevsW(ctypes.byref(GUID_DEVCLASS_DISPLAY), None, None, DIGCF_PRESENT)
    if devs in (None, INVALID_HANDLE_VALUE):
        return DriverStatus.INACCESSIBLE
    try:
        index = 0
        while True:
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not sapi.SetupDiEnumDeviceInfo(devs, index, ctypes.byref(info)):
                return DriverStatus.NOT_INSTALLED
            index += 1
            buf = ctypes.create_unicode_buffer(1024)
            regtype, needed = wintypes.DWORD(), wintypes.DWORD()
            if not sapi.SetupDiGetDeviceRegistryPropertyW(devs, ctypes.byref(info), SPDRP_HARDWAREID,
                                                          ctypes.byref(regtype), buf, ctypes.sizeof(buf),
                                                          ctypes.byref(needed)):
                continue
            ids = ctypes.wstring_at(ctypes.addressof(buf), len(buf)).split("\0")  # REG_MULTI_SZ
            if not any(i.lower() == VDD_HARDWARE_ID.lower() for i in ids if i):
                continue
            status, problem = wintypes.ULONG(), wintypes.ULONG()
            if cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), info.DevInst, 0) != CR_SUCCESS:
                return DriverStatus.NOT_INSTALLED
            if status.value & DN_HAS_PROBLEM:
                return {
                    CM_PROB_NEED_RESTART: DriverStatus.RESTART_REQUIRED,
                    CM_PROB_DISABLED: DriverStatus.DISABLED,
                    CM_PROB_HARDWARE_DISABLED: DriverStatus.DISABLED,
                    CM_PROB_DISABLED_SERVICE: DriverStatus.DISABLED,
                    CM_PROB_FAILED_POST_START: DriverStatus.DRIVER_ERROR,
                }.get(problem.value, DriverStatus.UNKNOWN_PROBLEM)
            if status.value & (DN_DRIVER_LOADED | DN_STARTED):
                return DriverStatus.OK
            return DriverStatus.UNKNOWN_PROBLEM
    finally:
        sapi.SetupDiDestroyDeviceInfoList(devs)


def driver_version() -> str:
    """Version the driver reports ("0.45"), or "" when it cannot be asked."""
    try:
        device = VddDevice.open()
    except VddError:
        return ""
    try:
        version = device.version()
    finally:
        device.close()
    return f"{version[0]}.{version[1]}" if version else ""


# -- device handle and IOCTLs ------------------------------------------------------------


def remove_payload(index: int) -> bytes:
    """REMOVE's input: the index as a 16-bit big-endian number, so in[1] holds it."""
    return (index & 0xFFFF).to_bytes(2, "big")


class VddDevice:
    """An open handle on the driver's adapter. Thread-safe: every call has its own OVERLAPPED."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def open(cls) -> "VddDevice":
        k32 = _dlls().kernel32
        for path in interface_paths():
            handle = k32.CreateFileW(
                path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING | FILE_FLAG_OVERLAPPED | FILE_FLAG_WRITE_THROUGH,
                None,
            )
            if handle and handle != INVALID_HANDLE_VALUE:
                device = cls(handle)
                device.update()
                return device
            _LOGGER.debug("CreateFileW(%s) failed: %s", path, ctypes.get_last_error())
        raise VddError(f"driver:{DriverStatus.DRIVER_ERROR.value}", "its device cannot be opened")

    def close(self) -> None:
        if self._handle:
            _dlls().kernel32.CloseHandle(self._handle)
            self._handle = 0

    def ioctl(self, code: int, payload: bytes = b"", timeout_ms: int = 1000) -> Optional[int]:
        """Send one IOCTL (32 bytes in, a DWORD out) and wait at most about timeout_ms + 2 s.
        Returns the DWORD output, or None."""
        k32 = _dlls().kernel32
        if not self._handle:
            return None
        inbuf = (ctypes.c_ubyte * VDD_IN_BUFFER_SIZE)()
        ctypes.memmove(inbuf, payload, min(len(payload), VDD_IN_BUFFER_SIZE))
        out = wintypes.DWORD(0)
        ov = OVERLAPPED()
        ov.hEvent = k32.CreateEventW(None, True, False, None)
        if not ov.hEvent:
            return None
        done = wintypes.DWORD(0)
        leak = False
        try:
            # Any error but ERROR_IO_PENDING is final: a request that fails at once was never
            # queued, so nothing writes into inbuf/out/ov later and the event is never set. The
            # driver's requests always pend; they fail at once when its device has gone away.
            if not k32.DeviceIoControl(self._handle, code, inbuf, VDD_IN_BUFFER_SIZE, ctypes.byref(out),
                                       ctypes.sizeof(out), None, ctypes.byref(ov)) \
                    and ctypes.get_last_error() != ERROR_IO_PENDING:
                return None
            if k32.GetOverlappedResultEx(self._handle, ctypes.byref(ov), ctypes.byref(done), timeout_ms, False):
                return out.value
            if ctypes.get_last_error() != WAIT_TIMEOUT:
                return None  # it completed, with an error
            # Never return while the kernel may still write into inbuf/out/ov, and never block
            # forever either: the reference code's GetOverlappedResult(bWait=TRUE) can hang.
            k32.CancelIoEx(self._handle, ctypes.byref(ov))
            if k32.GetOverlappedResultEx(self._handle, ctypes.byref(ov), ctypes.byref(done), 2000, False):
                return out.value  # it finished before the cancel took effect: a slow ADD still counts
            if ctypes.get_last_error() == WAIT_TIMEOUT:
                leak = True
                _LEAKED.append((inbuf, out, ov))  # keeps the memory valid for a late completion
            return None
        finally:
            if not leak:
                k32.CloseHandle(ov.hEvent)

    def version(self) -> Optional[tuple[int, int]]:
        value = self.ioctl(IOCTL_VDD_VERSION, timeout_ms=1000)
        return None if value is None else ((value >> 16) & 0xFFFF, value & 0xFFFF)

    def update(self) -> bool:
        return self.ioctl(IOCTL_VDD_UPDATE, timeout_ms=1000) is not None

    def add(self) -> int:
        index = self.ioctl(IOCTL_VDD_ADD, timeout_ms=5000)
        self.update()
        if index is None or index >= VDD_MAX_DISPLAYS:
            raise VddError("add_failed")
        return index

    def remove(self, index: int) -> bool:
        ok = self.ioctl(IOCTL_VDD_REMOVE, remove_payload(index), timeout_ms=1000) is not None
        self.update()
        return ok


def _raise_thread_priority() -> None:
    """A late ping costs the monitor, so the pinger runs above normal (as in ParsecDisplay)."""
    try:
        k32 = _dlls().kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)
    except (VddError, OSError):
        pass


class KeepAlive(threading.Thread):
    """Pings the driver every 100 ms while AirCast owns a monitor, and does nothing else: a ping
    that waits for anything (a display enumeration, ADD's 5 s) can cost the monitor.

    Calls on_lost once when the driver stops answering (5 failed pings in a row), then stops.
    """

    def __init__(self, device: VddDevice, on_lost: Optional[Callable[[], None]] = None) -> None:
        super().__init__(name="aircast-vdd-keepalive", daemon=True)
        self._device = device
        self._on_lost = on_lost
        self._stop_event = threading.Event()

    def run(self) -> None:
        _raise_thread_priority()
        failures = 0
        last = time.monotonic()
        while not self._stop_event.wait(PING_INTERVAL_S):
            now = time.monotonic()
            if now - last > 0.5:
                _LOGGER.warning("Virtual screen keep-alive ran %.0f ms late", (now - last) * 1000)
            last = now
            failures = 0 if self._device.update() else failures + 1
            if failures >= 5:
                _LOGGER.error("The virtual screen is gone (the driver stopped answering)")
                if self._on_lost and not self._stop_event.is_set():
                    self._on_lost()
                return

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=2)


class PresenceWatch(threading.Thread):
    """Checks about once a second that Windows still has the monitor (the watchdog removes it
    while the PC sleeps, for example) and calls on_lost once after two misses, then stops.

    A thread of its own: EnumDisplayDevicesW waits for any display mode change under way
    (a monitor waking up, HDR, Win+P), which can take seconds, and the pings must not wait.
    """

    def __init__(self, present: Callable[[], bool], on_lost: Callable[[], None]) -> None:
        super().__init__(name="aircast-vdd-presence", daemon=True)
        self._present = present
        self._on_lost = on_lost
        self._stop_event = threading.Event()

    def run(self) -> None:
        missing = 0
        while not self._stop_event.wait(PRESENCE_INTERVAL_S):
            missing = 0 if self._present() else missing + 1
            if missing >= 2:
                _LOGGER.error("The virtual screen is gone (Windows removed it)")
                if not self._stop_event.is_set():
                    self._on_lost()
                return

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=1)  # a check stuck in Windows ends on its own and reports nothing


# -- finding the new monitor, extending, setting the mode --------------------------------


@dataclass
class VddSource:
    device_name: str  # \\.\DISPLAYn, the name EnumDisplayMonitors and DXGI_OUTPUT_DESC use
    uid: int  # 0x100 + driver index
    attached_to_desktop: bool
    monitor_active: bool


_UID_RE = re.compile(r"UID(\d+)", re.IGNORECASE)


def parse_uid(device_id: str) -> Optional[int]:
    r"""The UID of a Parsec monitor from its device ID, e.g.
    \\?\DISPLAY#PSCCDD0#5&1a2b&0&UID257#{e6f07b5f-...} -> 257. None for any other monitor."""
    if VDD_MONITOR_ID not in device_id.upper():
        return None
    match = _UID_RE.search(device_id)
    return int(match.group(1)) if match else None


def vdd_sources() -> list[VddSource]:
    """Every Parsec virtual monitor Windows knows about right now (sources, then their monitors)."""
    u32 = _dlls().user32
    found: list[VddSource] = []
    i = 0
    while True:
        src = DISPLAY_DEVICEW()
        src.cb = ctypes.sizeof(src)
        if not u32.EnumDisplayDevicesW(None, i, ctypes.byref(src), 0):
            break
        i += 1
        j = 0
        while True:
            mon = DISPLAY_DEVICEW()
            mon.cb = ctypes.sizeof(mon)
            if not u32.EnumDisplayDevicesW(src.DeviceName, j, ctypes.byref(mon), EDD_GET_DEVICE_INTERFACE_NAME):
                break
            j += 1
            uid = parse_uid(mon.DeviceID)
            if uid is None or not mon.StateFlags & DISPLAY_DEVICE_ATTACHED:
                continue
            found.append(VddSource(
                device_name=src.DeviceName,
                uid=uid,
                attached_to_desktop=bool(src.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP),
                monitor_active=bool(mon.StateFlags & DISPLAY_DEVICE_ACTIVE),
            ))
    return found


def _present(uid: int) -> bool:
    return any(src.uid == uid for src in vdd_sources())


def _monitor_known(uid: int) -> bool:
    """Whether Windows has the monitor with this UID as a device yet, on the desktop or not."""
    return any(parse_uid(path) == uid for path in interface_paths(GUID_DEVINTERFACE_MONITOR))


Rect = tuple[int, int, int, int]  # left, top, right, bottom


def desktop_rects() -> dict[str, Rect]:
    """{\\\\.\\DISPLAYn: its rectangle} for every source that is part of the desktop."""
    u32 = _dlls().user32
    rects: dict[str, Rect] = {}
    i = 0
    while True:
        src = DISPLAY_DEVICEW()
        src.cb = ctypes.sizeof(src)
        if not u32.EnumDisplayDevicesW(None, i, ctypes.byref(src), 0):
            break
        i += 1
        if not src.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        dm = _DEVMODEW()
        dm.dmSize = ctypes.sizeof(dm)
        if u32.EnumDisplaySettingsW(src.DeviceName, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
            rects[src.DeviceName] = (dm.dmPositionX, dm.dmPositionY,
                                     dm.dmPositionX + dm.dmPelsWidth, dm.dmPositionY + dm.dmPelsHeight)
    return rects


def own_screen_on_desktop() -> bool:
    """Whether the desktop has a screen that is not a Parsec monitor (a headless PC may have none)."""
    parsec = {src.device_name for src in vdd_sources()}
    return any(name not in parsec for name in desktop_rects())


def is_extended(name: str, rects: dict[str, Rect], parsec: set[str], own_screen: bool = True) -> bool:
    """Whether source `name` is a screen of its own next to the PC's: on the desktop, not
    overlapping another screen (Duplicate shows both at the same spot), and not the only screen
    left when the PC had one of its own ("Second screen only").

    `parsec` is the source names of every Parsec monitor; `own_screen` is whether the desktop
    had a screen that is not one before the virtual monitor was added.
    """
    rect = rects.get(name)
    if rect is None:
        return False
    for other, (left, top, right, bottom) in rects.items():
        if other != name and left < rect[2] and rect[0] < right and top < rect[3] and rect[1] < bottom:
            return False
    return not own_screen or any(other not in parsec for other in rects)


def extend_desktop() -> bool:
    """Win+P "Extend": apply the extend topology from the display database."""
    rc = _dlls().user32.SetDisplayConfig(0, None, 0, None, SDC_TOPOLOGY_EXTEND | SDC_APPLY)
    if rc != 0:
        _LOGGER.warning("SetDisplayConfig(EXTEND) failed: %s", rc)
    return rc == 0


def wait_for_source(index: int, timeout: float = 10.0, cancel: Optional[threading.Event] = None,
                    own_screen: bool = True) -> str:
    """Wait until the monitor with driver index `index` extends the desktop; returns \\\\.\\DISPLAYn.

    `own_screen`: the desktop had a screen of its own before the monitor was added, so it must
    still have one (see is_extended).
    """
    uid = VDD_UID_BASE + index
    start = time.monotonic()
    extended = False
    while time.monotonic() - start < timeout:
        sources = vdd_sources()
        mine = next((src for src in sources if src.uid == uid), None)
        if mine is not None and mine.attached_to_desktop and is_extended(
                mine.device_name, desktop_rects(), {src.device_name for src in sources}, own_screen):
            return mine.device_name
        if not extended and time.monotonic() - start > EXTEND_AFTER_S and (mine is not None or _monitor_known(uid)):
            # Windows brings back the topology it saved for this set of monitors, so a Duplicate,
            # PC screen only or Second screen only picked once comes back on every cast. Apply
            # Win+P "Extend" once, and only once Windows has the new monitor: before that, Extend
            # would be applied to (and saved for) the user's own monitors alone.
            extended = True
            _LOGGER.info("The virtual screen does not extend the desktop yet; extending the desktop")
            extend_desktop()
        if cancel is not None and cancel.wait(0.1):
            raise VddError("cancelled")
        if cancel is None:
            time.sleep(0.1)
    raise VddError("not_attached")


def supported_modes(device_name: str) -> set[tuple[int, int, int]]:
    """(width, height, refresh rate) of every mode the display offers."""
    u32 = _dlls().user32
    modes = set()
    n = 0
    while True:
        dm = _DEVMODEW()
        dm.dmSize = ctypes.sizeof(dm)
        if not u32.EnumDisplaySettingsW(device_name, n, ctypes.byref(dm)):
            break
        modes.add((dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency))
        n += 1
    return modes


def pick_mode(wanted: tuple[int, int, int], offered: set[tuple[int, int, int]]) -> Optional[tuple[int, int, int]]:
    """The mode to switch to: `wanted`, else the same size at its fastest offered refresh rate,
    else None (keep the monitor's default 1920x1080@60)."""
    if wanted in offered:
        return wanted
    same_size = [m for m in offered if m[:2] == wanted[:2]]
    return max(same_size, key=lambda m: m[2]) if same_size else None


def set_mode(device_name: str, width: int, height: int, hz: int) -> bool:
    u32 = _dlls().user32
    dm = _DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    if not u32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return False
    if (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency) == (width, height, hz):
        return True
    dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency = width, height, hz
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
    rc = u32.ChangeDisplaySettingsExW(device_name, ctypes.byref(dm), None, CDS_UPDATEREGISTRY, None)
    if rc != DISP_CHANGE_SUCCESSFUL:
        _LOGGER.warning("ChangeDisplaySettingsExW(%s, %dx%d@%d) = %d", device_name, width, height, hz, rc)
    return rc == DISP_CHANGE_SUCCESSFUL


def _wait_until_gone(uid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while _present(uid) and time.monotonic() < deadline:
        time.sleep(0.1)


# -- monitors left behind ------------------------------------------------------------------
# The watchdog removes a crashed AirCast's monitor, unless another program (the Parsec app,
# ParsecDisplay) keeps pinging the driver. So every monitor is written down when it is added and
# crossed out when it is removed; whatever is still written down at the next start is removed.


def _state_path() -> Path:
    return app_data_dir() / STATE_FILE


def _read_state() -> list[dict]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("displays") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("index"), int)]


def _write_state(entries: list[dict]) -> None:
    path = _state_path()
    try:
        if entries:
            path.write_text(json.dumps({"displays": entries}, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError as ex:
        _LOGGER.warning("Could not update %s: %s", path, ex)


def _mine(entry: dict, index: int) -> bool:
    return entry.get("pid") == os.getpid() and entry.get("index") == index


def _remember(index: int) -> None:
    entries = [e for e in _read_state() if not _mine(e, index)]
    entries.append({"index": index, "uid": VDD_UID_BASE + index, "pid": os.getpid(), "time": time.time()})
    _write_state(entries)


def _forget(index: int) -> None:
    _write_state([e for e in _read_state() if not _mine(e, index)])


def split_state(
    entries: list[dict], attached_uids: set[int], alive: Callable[[object], bool], booted_at: float
) -> tuple[list[int], list[dict]]:
    """(indexes to remove now, entries to keep) for the monitors written in the state file.

    An entry whose AirCast is still running (the command line next to the app) is kept. Any other
    entry is dropped, and its monitor removed if one with that UID is still attached and the
    entry is from this boot (a monitor never outlives a restart).
    """
    remove: list[int] = []
    keep: list[dict] = []
    for entry in entries:
        if alive(entry.get("pid")):
            keep.append(entry)
        elif entry.get("uid") in attached_uids and float(entry.get("time") or 0) > booted_at:
            remove.append(entry["index"])
    return remove, keep


def _process_alive(pid: object) -> bool:
    """Whether another process with this ID is running (this one never counts)."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    k32 = _dlls().kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED  # exists, but is not ours to query
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _boot_time() -> float:
    return time.time() - _dlls().kernel32.GetTickCount64() / 1000


def _remove_stale(device: VddDevice) -> None:
    entries = _read_state()
    if not entries:
        return
    attached = {src.uid for src in vdd_sources()}
    stale, keep = split_state(entries, attached, _process_alive, _boot_time())
    for index in stale:
        _LOGGER.info("Removing virtual screen %s that an earlier AirCast left behind", index)
        device.remove(index)
    _write_state(keep)


def recover_stale() -> None:
    """At startup: remove virtual monitors an earlier AirCast added and never removed."""
    if not _read_state():
        return
    try:
        if driver_status() is DriverStatus.OK:
            device = VddDevice.open()
            try:
                _remove_stale(device)
            finally:
                device.close()
            return
    except VddError as ex:
        _LOGGER.debug("Virtual screen clean-up skipped: %s", ex)
    # Without a working driver no monitor can be left; keep only what a running AirCast owns.
    _write_state([e for e in _read_state() if _process_alive(e.get("pid"))])


# -- the object a cast session uses ------------------------------------------------------------


class VirtualScreen(abc.ABC):
    """What a cast needs from a virtual display driver: one monitor for the life of one cast."""

    name = ""

    def __init__(self, on_lost: Optional[Callable[[], None]] = None) -> None:
        self.on_lost = on_lost  # called from a background thread if the monitor goes away

    @classmethod
    @abc.abstractmethod
    def status(cls) -> DriverStatus:
        """Whether the driver is installed and working."""

    @classmethod
    def version(cls) -> str:
        return ""

    @abc.abstractmethod
    def start(self, width: int, height: int, hz: int) -> str:
        """Add the monitor, put it on the desktop at this mode (if offered) and return its
        source name, \\\\.\\DISPLAYn. Blocks for up to about 10 s; raises VddError."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the monitor. Safe at any time, from any thread, more than once."""


_ACTIVE: set["ParsecScreen"] = set()


class ParsecScreen(VirtualScreen):
    """A monitor of the Parsec Virtual Display Driver."""

    name = "parsec"

    def __init__(self, on_lost: Optional[Callable[[], None]] = None) -> None:
        super().__init__(on_lost)
        self._lock = threading.Lock()  # held by start(), so stop() waits for it to give up
        self._cancel = threading.Event()
        self._device: Optional[VddDevice] = None
        self._keepalive: Optional[KeepAlive] = None
        self._presence: Optional[PresenceWatch] = None
        self._lost_lock = threading.Lock()
        self._quiet = False  # on_lost was called, or the monitor is going away on purpose
        self.index: Optional[int] = None
        self.device_name = ""

    @classmethod
    def status(cls) -> DriverStatus:
        """Ready means started and with its device interface there: an adapter can start without
        registering it, and then it cannot be opened."""
        status = driver_status()
        if status is DriverStatus.OK and not interface_paths():
            return DriverStatus.DRIVER_ERROR
        return status

    @classmethod
    def version(cls) -> str:
        return driver_version()

    def _report_lost(self) -> None:
        """From the pinger or the presence check: tell the cast once, and never for a monitor
        that is being removed on purpose."""
        with self._lost_lock:
            if self._quiet:
                return
            self._quiet = True
        if self.on_lost:
            self.on_lost()

    def start(self, width: int = 1920, height: int = 1080, hz: int = 60) -> str:
        with self._lock:
            if self._cancel.is_set():
                raise VddError("cancelled")
            status = self.status()
            if status is not DriverStatus.OK:
                raise VddError(f"driver:{status.value}")
            with self._lost_lock:
                self._quiet = False
            try:
                self._device = VddDevice.open()
                version = self._device.version()
                _LOGGER.info("Parsec Virtual Display Driver %s", "%d.%d" % version if version else "(no version)")
                _remove_stale(self._device)
                own_screen = own_screen_on_desktop()  # before ADD: Extend has to keep it
                # Pings start before ADD: the driver drops its monitors ~1 s after the last one.
                self._keepalive = KeepAlive(self._device, self._report_lost)
                self._keepalive.start()
                self.index = self._device.add()
                _remember(self.index)
                self.device_name = wait_for_source(self.index, cancel=self._cancel, own_screen=own_screen)
                mode = pick_mode((width, height, hz), supported_modes(self.device_name))
                if mode is None:
                    _LOGGER.warning("The virtual screen offers no %dx%d mode; keeping its default", width, height)
                else:
                    set_mode(self.device_name, *mode)
                uid = VDD_UID_BASE + self.index
                self._presence = PresenceWatch(lambda: _present(uid), self._report_lost)
                self._presence.start()
                if self._cancel.wait(0.5):  # let the desktop settle before Desktop Duplication opens it
                    raise VddError("cancelled")
                _ACTIVE.add(self)
                _LOGGER.info("Virtual screen %s is %s", self.index, self.device_name)
                return self.device_name
            except BaseException:
                self._teardown()
                raise

    def stop(self) -> None:
        self._cancel.set()  # a start() still running gives up at its next step
        with self._lock:
            self._teardown()

    def _teardown(self) -> None:
        _ACTIVE.discard(self)
        with self._lost_lock:
            self._quiet = True  # its monitor is about to go on purpose
        presence, keepalive, device, index = self._presence, self._keepalive, self._device, self.index
        self._presence, self._keepalive, self._device, self.index = None, None, None, None
        if presence:
            presence.stop()
        if device and index is not None:
            if device.remove(index):
                _forget(index)
            else:  # left in the state file, so the next start removes it if it is still there
                _LOGGER.warning("Removing virtual screen %s failed; the driver drops it within about 1 s", index)
        if keepalive:
            keepalive.stop()
        if device:
            device.close()
        if index is not None:
            _wait_until_gone(VDD_UID_BASE + index, timeout=3)


BACKENDS: dict[str, type[VirtualScreen]] = {ParsecScreen.name: ParsecScreen}
DEFAULT_BACKEND = ParsecScreen.name


def backend(name: str = DEFAULT_BACKEND) -> type[VirtualScreen]:
    return BACKENDS[name]


def _stop_active() -> None:
    """Quit without a clean stop: remove the monitor now rather than when the watchdog fires."""
    for screen in list(_ACTIVE):
        try:
            screen.stop()
        except Exception:  # pylint: disable=broad-except
            pass


atexit.register(_stop_active)


# -- first-time install (one UAC prompt) --------------------------------------------------------


def downloads_dir() -> Path:
    """%LOCALAPPDATA%\\AirCast\\downloads: a downloaded installer does not need to roam."""
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / "AirCast" if local else app_data_dir()) / "downloads"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_installer(dest_dir: Optional[Path] = None,
                       progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Download Parsec's official installer and check it against the pinned SHA-256.

    A file that does not match is deleted and never run (VddError "mismatch"); a network or
    disk problem is VddError "download". `progress(done, total)` is called from this thread;
    total is 0 when the server does not say.
    """
    import urllib.request

    dest_dir = dest_dir or downloads_dir()
    target = dest_dir / VDD_INSTALLER_URL.rsplit("/", 1)[1]
    part = target.with_name(target.name + ".part")
    request = urllib.request.Request(VDD_INSTALLER_URL, headers={"User-Agent": f"AirCast/{__version__}"})
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_of(target) == VDD_INSTALLER_SHA256:
                return target
            target.unlink()  # changed since it was checked: never run it
        with urllib.request.urlopen(request, timeout=30) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while chunk := resp.read(65536):
                got += len(chunk)
                if got > VDD_INSTALLER_MAX_BYTES:
                    raise VddError("mismatch", "far larger than expected")
                out.write(chunk)
                if progress:
                    progress(got, total)
        if sha256_of(part) != VDD_INSTALLER_SHA256:
            raise VddError("mismatch")
        os.replace(part, target)
    except (OSError, http.client.HTTPException) as ex:
        part.unlink(missing_ok=True)
        raise VddError("download", str(ex)) from ex
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return target


def run_installer_elevated(installer: Path, hwnd: Optional[int] = None,
                           on_started: Optional[Callable[[], None]] = None, timeout_s: int = 180) -> bool:
    """Run the installer silently after the UAC prompt. False if the user said No.

    `hwnd` is a window of ours in the foreground, so Windows shows the prompt in front instead
    of flashing in the taskbar. `on_started` is called once the installer is running.
    """
    api = _dlls()
    k32 = api.kernel32
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.hwnd = hwnd
    sei.lpVerb = "runas"
    sei.lpFile = str(installer)
    sei.lpParameters = VDD_INSTALLER_ARGS
    sei.lpDirectory = str(installer.parent)
    sei.nShow = SW_HIDE
    # ShellExecuteEx may hand the work to COM shell extensions, so COM must be up on this thread.
    com = api.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    try:
        if not api.shell32.ShellExecuteExW(ctypes.byref(sei)):
            err = ctypes.get_last_error()
            if err == ERROR_CANCELLED:
                return False
            raise VddError("install", f"{ctypes.FormatError(err).strip()} ({err})")  # in Windows' language
    finally:
        if com >= 0:  # S_OK or S_FALSE; not RPC_E_CHANGED_MODE
            api.ole32.CoUninitialize()
    if on_started:
        on_started()
    if sei.hProcess:
        try:
            if k32.WaitForSingleObject(sei.hProcess, timeout_s * 1000) == WAIT_TIMEOUT:
                _LOGGER.warning("The driver installer is still running after %s s", timeout_s)
            code = wintypes.DWORD()
            k32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
            _LOGGER.info("Parsec VDD installer exit code %s", code.value)  # meaning not documented
        finally:
            k32.CloseHandle(sei.hProcess)
    return True


def wait_until_ready(timeout_s: float = 30.0) -> DriverStatus:
    """After installing: wait for the driver to start. Returns the last status seen."""
    deadline = time.monotonic() + timeout_s
    while True:
        status = driver_status()
        if status is DriverStatus.OK and interface_paths():
            return status
        if status in (DriverStatus.RESTART_REQUIRED, DriverStatus.DISABLED) or time.monotonic() > deadline:
            return DriverStatus.DRIVER_ERROR if status is DriverStatus.OK else status
        time.sleep(0.5)


def winget_path() -> str:
    """winget's App Execution Alias, by full path: a bare "winget" would run a winget.exe left
    next to AirCast.exe or in the current folder (often Downloads) first."""
    local = os.environ.get("LOCALAPPDATA")
    path = os.path.join(local, "Microsoft", "WindowsApps", "winget.exe") if local else ""
    if not path or not os.path.exists(path):
        raise FileNotFoundError("winget is not installed")
    return path


def install_with_winget() -> None:
    """For when the download is blocked: install the same package with winget, in a console
    the user can watch. Raises OSError when winget is missing."""
    subprocess.Popen(  # pylint: disable=consider-using-with
        [winget_path(), "install", "-e", "--id", VDD_WINGET_ID, "--silent",
         "--accept-package-agreements", "--accept-source-agreements"],
        creationflags=_CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
    )
