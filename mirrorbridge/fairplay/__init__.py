# MirrorBridge's FairPlay SAP: a Python port of doubletake's Go implementation
# (https://github.com/omarroth/doubletake). Copyright the doubletake authors; this package is
# free software under the GNU Lesser General Public License version 3 (LICENSE and COPYING.GPL
# in this folder). The rest of MirrorBridge is MIT.
"""FairPlay SAP, the handshake Apple TVs and Macs ask of a sender before screen mirroring.

A receiver that advertises feature bit 14 wants POST /fp-setup twice before its mirroring
SETUP: m1 (16 bytes) answered by m2 (142), then m3 (164) answered by m4 (32), which repeats the
last 20 bytes of m3. The sender then wraps a random 16-byte key into a 72-byte "ekey" that the
receiver can open with what m3 told it; that key (mixed with the pair-verify secret on a
HomeKit-paired receiver, stream_key()) is the video stream's shk. core.py has the algorithms,
_tables.py their constant data.
"""

from .core import FairPlayError, FairPlaySession, stream_key

__all__ = ["FairPlayError", "FairPlaySession", "stream_key"]
