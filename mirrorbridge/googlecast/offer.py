"""OFFER and ANSWER: how a Cast Streaming session is agreed, in JSON on the webrtc namespace.

The sender offers its streams, each with an SSRC, a codec, the AES key and IV mask, the
delay it wants the receiver to show frames after (targetDelay) and the largest picture and
frame rate it will send; the receiver answers with the UDP port to send to, which streams
it takes (sendIndexes) and its own SSRCs for their RTCP, and limits it wants kept
(constraints). These are the fields Chrome's mirroring sends
(components/mirroring/service/session.cc), with Chrome's payload types: 96 for video,
whatever the codec, and 127 for audio, because some Android TV receivers take nothing else.
The audio (Opus, 48 kHz stereo, a frame every 10 ms) uses the video's AES key and IV mask,
as Chrome's does.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Optional

from .rtp import AUDIO_CLOCK, VIDEO_CLOCK

VIDEO_PAYLOAD_TYPE = 96
VIDEO_SSRC = range(500001, 1000001)  # Chrome's range for video SSRCs
AUDIO_PAYLOAD_TYPE = 127
AUDIO_SSRC = range(1, 500001)
VIDEO_INDEX, AUDIO_INDEX = 0, 1
AUDIO_CHANNELS = 2
AUDIO_BPS = 60 * 100 + AUDIO_CLOCK * AUDIO_CHANNELS  # Chrome's "auto" for 100 frames a second: 102 kbps


@dataclass
class VideoOffer:
    width: int
    height: int
    fps: int
    kbps: int
    delay_ms: int
    ssrc: int = field(default_factory=lambda: random.choice(VIDEO_SSRC))
    key: bytes = field(default_factory=lambda: os.urandom(16))
    iv_mask: bytes = field(default_factory=lambda: os.urandom(16))
    codec: str = "h264"
    audio: bool = False
    audio_ssrc: int = field(default_factory=lambda: random.choice(AUDIO_SSRC))
    audio_bps: int = AUDIO_BPS

    def message(self) -> dict:
        """The OFFER (seqNum is added when it is sent)."""
        stream = {
            "index": VIDEO_INDEX,
            "type": "video_source",
            "codecName": self.codec,
            "rtpProfile": "cast",
            "rtpPayloadType": VIDEO_PAYLOAD_TYPE,
            "ssrc": self.ssrc,
            "targetDelay": self.delay_ms,
            "aesKey": self.key.hex(),
            "aesIvMask": self.iv_mask.hex(),
            "timeBase": f"1/{VIDEO_CLOCK}",
            "receiverRtcpEventLog": True,
            "renderMode": "video",
            "maxFrameRate": f"{self.fps * 1000}/1000",
            "maxBitRate": self.kbps * 1000,
            "resolutions": [{"width": self.width, "height": self.height}],
        }
        streams = [stream]
        if self.audio:
            streams.append({
                "index": AUDIO_INDEX,
                "type": "audio_source",
                "codecName": "opus",
                "rtpProfile": "cast",
                "rtpPayloadType": AUDIO_PAYLOAD_TYPE,
                "ssrc": self.audio_ssrc,
                "targetDelay": self.delay_ms,
                "aesKey": self.key.hex(),
                "aesIvMask": self.iv_mask.hex(),
                "timeBase": f"1/{AUDIO_CLOCK}",
                "receiverRtcpEventLog": True,
                "bitRate": self.audio_bps,
                "sampleRate": AUDIO_CLOCK,
                "channels": AUDIO_CHANNELS,
            })
        return {"type": "OFFER", "offer": {"castMode": "mirroring", "receiverGetStatus": True,
                                           "supportedStreams": streams}}


@dataclass
class Answer:
    udp_port: int
    receiver_ssrc: int
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    max_fps: Optional[float] = None
    max_kbps: Optional[int] = None
    max_delay_ms: Optional[int] = None
    display: Optional[tuple[int, int]] = None
    audio_receiver_ssrc: Optional[int] = None  # None: the receiver took no audio
    audio_max_bps: Optional[int] = None
    raw: dict = field(default_factory=dict)


class AnswerError(ValueError):
    """The receiver turned the offer down, or answered something that is not an ANSWER."""


def _number(value) -> Optional[float]:
    """A JSON number, or a "30000/1001"-style rational, as Cast messages give frame rates."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        top, _, bottom = value.partition("/")
        try:
            return float(top) / float(bottom or 1)
        except (ValueError, ZeroDivisionError):
            return None
    return None


def parse_answer(message: dict, index: int = VIDEO_INDEX, audio_index: int = AUDIO_INDEX) -> Answer:
    if message.get("type") != "ANSWER":
        raise AnswerError(f"the receiver answered {message.get('type')!r}, not ANSWER")
    if message.get("result") not in (None, "ok"):
        error = message.get("error") or {}
        reason = error.get("description") or error or message.get("result")
        raise AnswerError(f"the receiver refused the stream: {reason}")
    answer = message.get("answer")
    if not isinstance(answer, dict):
        raise AnswerError("the ANSWER has no answer")
    indexes, ssrcs = answer.get("sendIndexes") or [], answer.get("ssrcs") or []
    if index not in indexes or len(indexes) != len(ssrcs):
        raise AnswerError(f"the receiver took none of the video (sendIndexes {indexes})")
    port = answer.get("udpPort")
    if not isinstance(port, int) or not 0 < port < 65536:
        raise AnswerError(f"the ANSWER names no UDP port ({port!r})")
    out = Answer(udp_port=port, receiver_ssrc=int(ssrcs[indexes.index(index)]), raw=answer)
    if audio_index in indexes:
        out.audio_receiver_ssrc = int(ssrcs[indexes.index(audio_index)])
    audio = (answer.get("constraints") or {}).get("audio") or {}
    if isinstance(audio.get("maxBitRate"), int) and audio["maxBitRate"] > 0:
        out.audio_max_bps = audio["maxBitRate"]
    video = (answer.get("constraints") or {}).get("video") or {}
    dims = video.get("maxDimensions") or {}
    if isinstance(dims.get("width"), int) and isinstance(dims.get("height"), int):
        out.max_width, out.max_height = dims["width"], dims["height"]
    out.max_fps = _number(dims.get("frameRate"))
    if isinstance(video.get("maxBitRate"), int) and video["maxBitRate"] > 0:
        out.max_kbps = video["maxBitRate"] // 1000
    if isinstance(video.get("maxDelay"), int) and video["maxDelay"] > 0:
        out.max_delay_ms = video["maxDelay"]
    shown = ((answer.get("display") or {}).get("dimensions")) or {}
    if isinstance(shown.get("width"), int) and isinstance(shown.get("height"), int):
        out.display = (shown["width"], shown["height"])
    return out
