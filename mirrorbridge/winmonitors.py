"""Win32 helpers: displays (real resolution + DXGI index), windows, child-process lifetime.

Resolution comes from the display mode (EnumDisplaySettings), which is in physical
pixels whatever the DPI scaling, so a 4K panel at 150% shows as 3840x2160, not
2560x1440. The DXGI adapter and output index are what FFmpeg's ddagrab filter needs;
ddagrab only sees outputs of the default adapter (adapter 0), so displays drawn by
another GPU (a virtual screen on a laptop's integrated GPU, for example) are captured
with gdigrab instead.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator, Optional

_LOGGER = logging.getLogger(__name__)

ENUM_CURRENT_SETTINGS = -1
MONITORINFOF_PRIMARY = 1
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14


@contextlib.contextmanager
def physical_pixels() -> Iterator[None]:
    """Make this thread per-monitor DPI aware so every coordinate is in physical pixels.

    Thread-scoped (Windows 10 1607+), so it neither fights Qt's own process setting nor
    changes anything for the rest of the app. On older systems it is a no-op.
    """
    try:
        set_ctx = ctypes.windll.user32.SetThreadDpiAwarenessContext
    except AttributeError:
        yield
        return
    set_ctx.restype = ctypes.c_void_p
    set_ctx.argtypes = [ctypes.c_void_p]
    previous = set_ctx(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    try:
        yield
    finally:
        if previous:
            set_ctx(previous)


@dataclass
class WinDisplay:
    device: str  # e.g. \\.\DISPLAY1
    left: int
    top: int
    width: int
    height: int
    primary: bool
    dxgi_output: Optional[int]  # output index on its DXGI adapter, None if DXGI does not list it
    dxgi_adapter: Optional[int] = None  # 0 = the default adapter, the only one ddagrab captures


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _DEVMODEW(ctypes.Structure):
    # Display flavour of the DEVMODEW unions; sizeof == 220 like the real struct.
    _fields_ = [
        ("dmDeviceName", wintypes.WCHAR * 32),
        ("dmSpecVersion", wintypes.WORD),
        ("dmDriverVersion", wintypes.WORD),
        ("dmSize", wintypes.WORD),
        ("dmDriverExtra", wintypes.WORD),
        ("dmFields", wintypes.DWORD),
        ("dmPositionX", wintypes.LONG),
        ("dmPositionY", wintypes.LONG),
        ("dmDisplayOrientation", wintypes.DWORD),
        ("dmDisplayFixedOutput", wintypes.DWORD),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", wintypes.WCHAR * 32),
        ("dmLogPixels", wintypes.WORD),
        ("dmBitsPerPel", wintypes.DWORD),
        ("dmPelsWidth", wintypes.DWORD),
        ("dmPelsHeight", wintypes.DWORD),
        ("dmDisplayFlags", wintypes.DWORD),
        ("dmDisplayFrequency", wintypes.DWORD),
        ("dmICMMethod", wintypes.DWORD),
        ("dmICMIntent", wintypes.DWORD),
        ("dmMediaType", wintypes.DWORD),
        ("dmDitherType", wintypes.DWORD),
        ("dmReserved1", wintypes.DWORD),
        ("dmReserved2", wintypes.DWORD),
        ("dmPanningWidth", wintypes.DWORD),
        ("dmPanningHeight", wintypes.DWORD),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# {770aae78-f26f-4dba-a829-253c83d1b387}
_IID_IDXGIFactory1 = _GUID(
    0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
)


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", wintypes.WCHAR * 32),
        ("DesktopCoordinates", wintypes.RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", wintypes.UINT),
        ("Monitor", wintypes.HMONITOR),
    ]


def _com_method(obj: ctypes.c_void_p, index: int, restype, *argtypes):
    """Fetch method `index` from a COM object's vtable."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        _com_method(obj, 2, wintypes.ULONG)(obj)  # IUnknown::Release


def dxgi_map() -> dict[str, tuple[int, int]]:
    """{\\\\.\\DISPLAYn: (adapter index, output index)} over every DXGI adapter.

    A fresh factory on every call: a factory created before a monitor arrived (a virtual
    screen added a moment ago) does not list it.
    """
    result: dict[str, tuple[int, int]] = {}
    factory = ctypes.c_void_p()
    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError:
        return result
    create = dxgi.CreateDXGIFactory1
    create.restype = ctypes.c_long
    create.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    if create(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) != 0:
        return result
    try:
        # IDXGIFactory1::EnumAdapters1 is vtable slot 12
        enum_adapters1 = _com_method(factory, 12, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        adapter_index = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters1(factory, adapter_index, ctypes.byref(adapter)) != 0:
                break  # DXGI_ERROR_NOT_FOUND: no more adapters
            try:
                # IDXGIAdapter::EnumOutputs is slot 7; IDXGIOutput::GetDesc is slot 7
                enum_outputs = _com_method(adapter, 7, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
                output_index = 0
                while True:
                    output = ctypes.c_void_p()
                    if enum_outputs(adapter, output_index, ctypes.byref(output)) != 0:
                        break  # DXGI_ERROR_NOT_FOUND: no more outputs
                    try:
                        desc = _DXGI_OUTPUT_DESC()
                        get_desc = _com_method(output, 7, ctypes.c_long, ctypes.POINTER(_DXGI_OUTPUT_DESC))
                        if get_desc(output, ctypes.byref(desc)) == 0:
                            result.setdefault(desc.DeviceName, (adapter_index, output_index))
                    finally:
                        _release(output)
                    output_index += 1
            finally:
                _release(adapter)
            adapter_index += 1
    finally:
        _release(factory)
    return result


def list_displays() -> list[WinDisplay]:
    with physical_pixels():
        return _list_displays()


def _list_displays() -> list[WinDisplay]:
    user32 = ctypes.windll.user32
    handles: list[int] = []

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    def _collect(hmon, _hdc, _rect, _lparam):
        handles.append(hmon)
        return True

    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, monitor_enum_proc, wintypes.LPARAM]
    user32.EnumDisplayMonitors(None, None, monitor_enum_proc(_collect), 0)
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
    user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW)]

    try:
        dxgi = dxgi_map()
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("DXGI enumeration failed", exc_info=True)
        dxgi = {}

    displays: list[WinDisplay] = []
    for hmon in handles:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            continue
        rect = info.rcMonitor
        left, top = rect.left, rect.top
        width, height = rect.right - rect.left, rect.bottom - rect.top
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(mode)
        if user32.EnumDisplaySettingsW(info.szDevice, ENUM_CURRENT_SETTINGS & 0xFFFFFFFF, ctypes.byref(mode)):
            left, top = mode.dmPositionX, mode.dmPositionY
            width, height = mode.dmPelsWidth, mode.dmPelsHeight
        adapter, output = dxgi.get(info.szDevice, (None, None))
        displays.append(
            WinDisplay(
                device=info.szDevice,
                left=left,
                top=top,
                width=width,
                height=height,
                primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                dxgi_output=output,
                dxgi_adapter=adapter,
            )
        )
    # Primary first, then left-to-right, which is how people number their screens.
    displays.sort(key=lambda d: (not d.primary, d.left, d.top))
    return displays


@dataclass
class WinWindow:
    hwnd: int
    title: str


def list_windows() -> list[WinWindow]:
    """Top-level windows a person would want to cast, in Z order (front first).

    Skips minimized, cloaked (suspended UWP apps, other virtual desktops), tool and owned
    pop-up windows plus the desktop itself, which would all capture as black.
    """
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    gwl_exstyle, ws_ex_toolwindow, gw_owner = -20, 0x80, 4
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    found: list[WinWindow] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or user32.GetWindow(hwnd, gw_owner):
            return True
        if user32.GetWindowLongW(hwnd, gwl_exstyle) & ws_ex_toolwindow:
            return True
        cloaked = wintypes.DWORD(0)
        dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip():
                found.append(WinWindow(int(hwnd), buf.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def window_rect(title: str) -> Optional[tuple[int, int, int, int]]:
    """(left, top, width, height) in physical screen pixels of the frontmost window with
    this exact title, without the invisible resize border. None if it is not on screen."""
    with physical_pixels():
        for win in list_windows():
            if win.title != title:
                continue
            rect = wintypes.RECT()
            ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                win.hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
            ) == 0
            if not ok:
                ctypes.windll.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
                ok = bool(ctypes.windll.user32.GetWindowRect(win.hwnd, ctypes.byref(rect)))
            if ok and rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_job_handle: Optional[int] = None


def kill_with_this_process(process_handle: int) -> bool:
    """Put a child process in a job that Windows kills when MirrorBridge exits, even if
    MirrorBridge crashes or is ended from Task Manager, so FFmpeg never lingers."""
    global _job_handle  # pylint: disable=global-statement
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    if _job_handle is None:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            return False
        _job_handle = job  # deliberately never closed: closing it is what kills the children
    return bool(kernel32.AssignProcessToJobObject(_job_handle, process_handle))
