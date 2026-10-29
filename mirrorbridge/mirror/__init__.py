"""Screen mirroring (experimental): the PC's screen on an AirPlay 2 TV of another brand, a
fraction of a second behind, as an iPhone mirrors it, instead of a cast's few seconds.

A cast (session.py) sends HLS that the TV buffers; mirroring sends each H.264 frame as soon as
FFmpeg has encoded it, over a connection of its own, with the time the TV should show it.
It works with TVs that pair the HomeKit way, such as TCL, Roku, Samsung and LG TVs, and with
Apple TVs and Macs (feature bit 14), which first want the FairPlay handshake (..fairplay).

    control.py      the encrypted RTSP connection (pair-verify) and GET /info
    negotiation.py  SETUP in the shapes TVs take, RECORD, /feedback, TEARDOWN
    clock.py        the NTP responder the TV asks for the time, and the frame clock
    source.py       FFmpeg: the desktop or a test pattern, H.264 as FLV into a pipe
    capture.py      the desktop on the graphics card or by the processor; a lost capture
    flv.py, h264.py reading FFmpeg's output; the avcC record the TV gets
    packets.py      the 128-byte packet header and the video's encryption
    datachannel.py  the TCP connection the video goes over
    stats.py        where each frame's time goes, logged every few seconds
    session.py      MirrorSession: all of it, for the tray and the command line
    probe.py        probe_mirroring(): what a TV takes, step by step
    profiles.py     what worked, saved per device
"""

from .control import ReceiverInfo, Target
from .errors import MirrorError
from .probe import ALL_VARIANTS, ProbeResult, probe_mirroring
from .profiles import MirrorProfile
from .session import MirrorSession, SessionReport
from .source import DESKTOP, SOURCES, TEST_PATTERN

__all__ = [
    "ALL_VARIANTS",
    "DESKTOP",
    "MirrorError",
    "MirrorProfile",
    "MirrorSession",
    "ProbeResult",
    "ReceiverInfo",
    "SOURCES",
    "SessionReport",
    "TEST_PATTERN",
    "Target",
    "probe_mirroring",
]
