"""KeepAwake: the power request that keeps the PC and its screen awake while it is on a TV."""

import logging
import sys

import pytest

from mirrorbridge import awake
from mirrorbridge.awake import (ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED, INVALID_HANDLE_VALUE,
                                POWER_REQUEST_DISPLAY_REQUIRED, POWER_REQUEST_SYSTEM_REQUIRED, KeepAwake)

HANDLE = 0x1234


def _wine() -> bool:
    """Whether this "Windows" is Wine, whose PowerCreateRequest is a stub that always fails."""
    if sys.platform != "win32":
        return False
    import ctypes  # pylint: disable=import-outside-toplevel
    return hasattr(ctypes.WinDLL("ntdll"), "wine_get_version")


class FakeKernel32:
    """kernel32's power calls, as Windows answers them (or fails one of them)."""

    def __init__(self, create=HANDLE, refuse=()):
        self.calls = []
        self.create, self.refuse = create, set(refuse)

    def SetThreadExecutionState(self, flags):  # pylint: disable=invalid-name
        self.calls.append(("SetThreadExecutionState", flags))
        return 0x80000000

    def PowerCreateRequest(self, context):  # pylint: disable=invalid-name
        reason = context._obj  # pylint: disable=protected-access
        self.calls.append(("PowerCreateRequest", reason.Version, reason.Flags, reason.Reason.SimpleReasonString))
        return self.create

    def PowerSetRequest(self, handle, kind):  # pylint: disable=invalid-name
        self.calls.append(("PowerSetRequest", handle, kind))
        return kind not in self.refuse

    def PowerClearRequest(self, handle, kind):  # pylint: disable=invalid-name
        self.calls.append(("PowerClearRequest", handle, kind))
        return True

    def CloseHandle(self, handle):  # pylint: disable=invalid-name
        self.calls.append(("CloseHandle", handle))
        return True


def test_the_pc_and_its_screen_stay_awake_until_stopped(caplog):
    kernel32 = FakeKernel32()
    keep = KeepAwake("MirrorBridge is showing this screen on TCL", kernel32=kernel32)
    with caplog.at_level(logging.INFO, logger=awake.__name__):
        assert keep.start()
        assert not keep.start()  # once is enough
        keep.stop()
        keep.stop()  # and once only
    assert kernel32.calls == [
        ("SetThreadExecutionState", ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED),  # a screen that just went off
        ("PowerCreateRequest", 0, 1, "MirrorBridge is showing this screen on TCL"),  # powercfg /requests says why
        ("PowerSetRequest", HANDLE, POWER_REQUEST_SYSTEM_REQUIRED),
        ("PowerSetRequest", HANDLE, POWER_REQUEST_DISPLAY_REQUIRED),
        ("PowerClearRequest", HANDLE, POWER_REQUEST_SYSTEM_REQUIRED),
        ("PowerClearRequest", HANDLE, POWER_REQUEST_DISPLAY_REQUIRED),
        ("CloseHandle", HANDLE),
    ]
    assert "Keeping this PC and its screen awake while mirroring" in caplog.text
    assert "This PC may sleep again" in caplog.text


def test_a_test_pattern_keeps_only_the_pc_awake():
    kernel32 = FakeKernel32()
    keep = KeepAwake("test", display=False, kernel32=kernel32)
    assert keep.start()
    keep.stop()
    assert [call[0] for call in kernel32.calls] == [
        "PowerCreateRequest", "PowerSetRequest", "PowerClearRequest", "CloseHandle"]
    assert ("PowerSetRequest", HANDLE, POWER_REQUEST_SYSTEM_REQUIRED) in kernel32.calls


@pytest.mark.parametrize("create", [INVALID_HANDLE_VALUE, None])
def test_a_request_windows_does_not_make_is_only_a_warning(caplog, create):
    kernel32 = FakeKernel32(create=create)
    keep = KeepAwake("test", kernel32=kernel32)
    assert not keep.start()
    keep.stop()
    assert "Could not keep this PC awake while mirroring: PowerCreateRequest failed" in caplog.text
    assert [call[0] for call in kernel32.calls] == ["SetThreadExecutionState", "PowerCreateRequest"]


def test_a_request_windows_refuses_in_part_is_taken_back(caplog):
    """The screen refused after the PC was kept awake: what was set is cleared, and the handle closed."""
    kernel32 = FakeKernel32(refuse={POWER_REQUEST_DISPLAY_REQUIRED})
    keep = KeepAwake("test", kernel32=kernel32)
    assert not keep.start()
    assert "PowerSetRequest(0) failed" in caplog.text
    assert kernel32.calls[-2:] == [("PowerClearRequest", HANDLE, POWER_REQUEST_SYSTEM_REQUIRED),
                                   ("CloseHandle", HANDLE)]
    keep.stop()
    assert kernel32.calls[-1] == ("CloseHandle", HANDLE)  # nothing more


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has the power requests")
def test_nothing_is_asked_off_windows():
    keep = KeepAwake("test")
    assert not keep.start()
    keep.stop()


@pytest.mark.skipif(sys.platform != "win32" or _wine(), reason="Windows' own power requests (Wine has none)")
def test_windows_takes_the_request_and_lets_it_go():
    keep = KeepAwake("MirrorBridge test")
    assert keep.start()
    keep.stop()
