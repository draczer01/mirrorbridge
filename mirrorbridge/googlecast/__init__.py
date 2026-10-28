"""Google Cast screen mirroring (experimental): the PC's screen on a Chromecast, a Google TV or
any TV with Chromecast built-in, a fraction of a second behind, the way Chrome mirrors a tab.

It is Cast Streaming, the protocol of Chrome's mirroring: the receiver's own mirroring app is
started over the Cast control connection, the video is agreed on in an OFFER and an ANSWER,
and H.264 frames go over UDP, encrypted, as RTP, with the receiver asking for lost packets.

    channel.py    the Cast control connection (TLS, protobuf, PING/PONG)
    discovery.py  _googlecast._tcp receivers on mDNS, or one by its IP address
    offer.py      OFFER and ANSWER
    rtp.py        encryption, RTP packets, RTCP both ways
    sender.py     the UDP side: frames out, lost packets again, sender reports
    session.py    GoogleCastMirror: all of it, for the tray and the command line
"""

from .channel import ChannelError
from .discovery import CastDevice, find, scan
from .session import APP_ID, DEFAULT_DELAY_MS, CastMirrorError, GoogleCastMirror

__all__ = ["APP_ID", "CastDevice", "CastMirrorError", "ChannelError", "DEFAULT_DELAY_MS", "GoogleCastMirror",
           "find", "scan"]
