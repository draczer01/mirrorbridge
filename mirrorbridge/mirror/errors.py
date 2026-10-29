"""Why screen mirroring failed, in words a person can act on (Texts, as in airplay_patch).

Each step of a session notes what the TV answered (`steps`: name -> HTTP status, or "no answer",
"connection lost"...), for the log and for the message.
"""

from __future__ import annotations

from typing import Optional

from ..airplay_patch import format_steps
from ..text import Text
from .clock import TIMING_PORTS


class MirrorError(RuntimeError):
    """Mirroring failed; the message is a Text. `code` is the TV's status code if it gave one."""

    def __init__(self, message: str, *, step: str = "", code: Optional[int] = None,
                 steps: Optional[dict] = None) -> None:
        super().__init__(message)
        self.step = step
        self.code = code
        self.steps = dict(steps or {})


class NoAnswer(TimeoutError):
    """The TV did not answer a request in time."""


class ChannelClosed(ConnectionError):
    """The TV closed the control connection (or it was never open)."""


def answer_of(ex: BaseException) -> str:
    """A step's answer, for a step that raised instead of being answered."""
    if isinstance(ex, TimeoutError):
        return "no answer"
    if isinstance(ex, ConnectionRefusedError):
        return "connection refused"
    if isinstance(ex, (ConnectionError, EOFError)):
        return "connection lost"
    if isinstance(ex, OSError):
        return "connection failed"
    return "unreadable answer"


def message_of(ex: BaseException) -> str:
    """The Text a MirrorError carries, or the exception in words."""
    message = ex.args[0] if ex.args else ""
    if isinstance(message, str) and message:
        return message
    return str(ex) or type(ex).__name__


def needs_fairplay(device: str) -> MirrorError:
    """An Apple TV or a Mac: their screen mirroring is FairPlay's."""
    return MirrorError(Text(
        "mirror_fairplay", "{device} needs FairPlay for screen mirroring (an Apple TV or a Mac), which MirrorBridge "
        "does not have yet. Cast to it as usual instead.", device=device,
    ), step="classify")


def fairplay_failed(device: str, step: str, answer: str, code: Optional[int] = None) -> MirrorError:
    """The FairPlay handshake an Apple TV or a Mac wants before mirroring did not go through."""
    return MirrorError(Text(
        "mirror_fairplay_failed", "{device} did not complete the FairPlay handshake that screen mirroring to an "
        "Apple TV needs ({step}: {answer}). Please report this TV, with mirrorbridge.log; casting still works.",
        device=device, step=step, answer=answer,
    ), step=step, code=code, steps={step: answer})


def no_timing(port: int, steps: dict) -> MirrorError:
    """The TV never asked for the time: almost always a firewall in the way of its requests."""
    return MirrorError(Text(
        "mirror_no_timing", "The TV never asked MirrorBridge for the time (UDP port {port}), so it cannot show the "
        "picture. Allow MirrorBridge, or inbound UDP ports {ports}, in Windows Firewall on private networks, then "
        "try again.", port=port, ports=f"{TIMING_PORTS.start}-{TIMING_PORTS.stop - 1}",
    ), step="timing", steps=steps)


def data_refused(port: int, answer: str, steps: dict) -> MirrorError:
    """The TV did not take the video connection it named."""
    return MirrorError(Text(
        "mirror_data_refused", "The TV did not take the video connection (TCP port {port}: {answer}).",
        port=port, answer=answer,
    ), step="video connection", steps=steps)


def data_closed(frames: int, steps: dict) -> MirrorError:
    """The TV closed the video connection while the session went on."""
    return MirrorError(Text(
        "mirror_data_closed", "The TV closed the video connection after {frames} frames. It may not take "
        "this video: please report this TV, with mirrorbridge.log.", frames=frames,
    ), step="video", steps=steps)


def refused(code: Optional[int], steps: dict, step: str) -> MirrorError:
    """The TV said no to a step of setting up the session (or never answered it)."""
    if code == 401:
        text = Text("mirror_password", "The TV asks for an AirPlay password (HTTP 401), which MirrorBridge does "
                    "not send for screen mirroring yet. Turn off the AirPlay password in the TV's AirPlay "
                    "settings, then try again.")
    elif code == 403:
        text = Text("tv_pairing_refused", "The TV no longer accepts MirrorBridge's pairing (HTTP 403). Use "
                    "Device > Forget pairing, then cast again and type the new PIN the TV shows.")
    elif code == 453:
        text = Text("tv_busy", "The TV is busy with another AirPlay sender (HTTP 453). Stop AirPlay on the "
                    "other phone, tablet or computer, then try again.")
    elif code == 470:
        text = Text("tv_restricted", "AirPlay access is restricted on the TV (HTTP 470). In the TV's AirPlay "
                    "settings, allow everyone on the same network, then pair again.")
    elif code is None:
        text = Text("mirror_no_answer", "The TV stopped answering while screen mirroring was set up ({steps}). "
                    "Check that the PC and the TV are on the same network, then try again.",
                    steps=format_steps(steps))
    else:
        text = Text("mirror_refused", "The TV turned down screen mirroring ({steps}). Please report this TV, "
                    "with mirrorbridge.log.", steps=format_steps(steps))
    return MirrorError(text, step=step, code=code, steps=steps)
