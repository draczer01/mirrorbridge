"""Setting a mirroring session up with the TV: SETUP, RECORD, /feedback and TEARDOWN.

A session is set up with SETUP requests on the encrypted control connection, each a binary
plist:

* the session itself: deviceID, sessionUUID, isScreenMirroringSession, how the TV gets the time
  (timingProtocol "NTP" and the port of our responder, clock.py), and who is sending;
* a screen-audio stream (type 96), which some TVs only get ready with, and which carries the
  PC's sound when that is asked for (sound.py): its format (ALAC unless the TV lists only PCM),
  its latencies, the key of its AudioCipher (a random shk) and our audio control port; the TV
  answers with its own data and control ports for it (AudioSetup);
* the screen-video stream (type 110) on a URI of its own; the answer names the TCP port the
  video goes to (dataPort).

TVs differ in the order they take these in. Apple's receivers take the session alone first
("control-first", with updateSessionRequest and combinedGetInfoWithControlSetup), TVs of other
brands may refuse that and want the session fields on the first stream's SETUP ("media-first").
A refusal of the first shape (400, 405, 406, 415, 455, 501) is followed once by the other one,
on a fresh pair-verify: a TV may close the connection right after saying no.

PTP timing (timingPeerInfo) can be asked for too, to learn whether the TV would give a PTP clock.
Some TVs advertise PTP but answer without a timingPeerInfo.ClockID, so unless MirrorBridge runs
its own PTP clock (ptp_clock_id, the experiment of ptp.py), a PTP session is torn down and set
up again over NTP, once, on a fresh pair-verify. With our clock running, an answer without a
ClockID is kept: the TV's clock follows ours once SETPEERS has named the two of us
(set_peers()), and ptp_fallback() goes over to NTP if it does not. A later SETUP of such a
session that the TV refuses gets NTP in the same way.

Each answer is handed to `on_reply` as it comes, so that the TV's timing port is probed and its
event channel opened at once, not only once every SETUP is done (a TV may wait for them before
it answers the next one). After SETUP: the video connection, RECORD (unless the TV said
skipRecord), then POST /feedback every 2 seconds for as long as the session lasts (TVs end a
session that hears nothing for about 15 s), and TEARDOWN at the end or when setting up fails
half-way.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import plistlib
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..airplay_patch import format_steps
from ..text import Text
from .audiopackets import ALAC_44100, AudioCipher, AudioFormat, ChaChaShk, ms_to_samples
from .control import (CONTROL_READ, CONTROL_SALT, CONTROL_WRITE, SENDER, SOURCE_VERSION, ControlChannel, Target,
                      decode_plist, sender_id)
from .errors import ChannelClosed, MirrorError, NoAnswer, answer_of, refused

_LOGGER = logging.getLogger(__name__)

CONTROL_FIRST = "control-first"
MEDIA_FIRST = "media-first"
ORDERINGS = (CONTROL_FIRST, MEDIA_FIRST)
NTP = "ntp"
PTP = "ptp"

# What a TV answers to a request of a shape it does not take; worth one try of the other shape.
SHAPE_REFUSED = frozenset({400, 405, 406, 415, 455, 501})

SETUP_SECONDS = 30  # some TVs take 20 s to answer a SETUP
RECORD_SECONDS = 15
TEARDOWN_SECONDS = 3
try:  # experiment: MIRRORBRIDGE_FEEDBACK_S, to tell a hitch every 2 s apart from POST /feedback
    FEEDBACK_INTERVAL = min(30.0, max(0.5, float(os.environ["MIRRORBRIDGE_FEEDBACK_S"])))
except (KeyError, ValueError):
    FEEDBACK_INTERVAL = 2.0
FEEDBACK_SECONDS = 5
DEFAULT_LEAD_MS = 100  # how long after capture the TV shows a frame (75 is enough on a cable)


def audio_from_env(audio: bool) -> bool:
    """MIRRORBRIDGE_NO_AUDIO=1, for an experiment: SETUP without the audio (type 96) stream."""
    return False if os.environ.get("MIRRORBRIDGE_NO_AUDIO") == "1" else audio


def timing_from_env(timing: str) -> str:
    """MIRRORBRIDGE_TIMING=ptp (or ntp), for an experiment, or `timing` when unset."""
    value = os.environ.get("MIRRORBRIDGE_TIMING", "").strip().lower()
    return value if value in (NTP, PTP) else timing


def lead_from_env(lead_ms: int) -> int:
    """MIRRORBRIDGE_LEAD_MS, for an experiment on Wi-Fi (20-1000 ms), or `lead_ms` when unset."""
    try:
        return min(1000, max(20, int(os.environ["MIRRORBRIDGE_LEAD_MS"])))
    except (KeyError, ValueError):
        return lead_ms

PEER_LIST_CHANGED = "/peer-list-changed"  # SETPEERS's Content-Type (SETPEERSX is for feature bit 52)

# Who is sending (and SOURCE_VERSION, in control.py, as in the User-Agent).
SENDER_MODEL = SENDER.get("model", "Windows")
SENDER_OS_BUILD = SENDER.get("osBuildVersion", "10.0")

# The screen-audio stream: ALAC, 352 samples a packet, 44.1 kHz 16-bit stereo, as it was set up
# before it carried sound (audiopackets.py has the other formats).
AUDIO_STREAM = 96
VIDEO_STREAM = 110
ALAC = 2
ALAC_FORMAT = 0x40000
SAMPLES_PER_PACKET = 352
SAMPLE_RATE = 44100
TIMESTAMP_INFO = ("SubSu", "BePxT", "AfPxT", "BefEn", "EmEnc")
# The latencies (latencyMin, latencyMax) the type-96 SETUP asks for, in samples, by
# MIRRORBRIDGE_MIRROR_SOUND_SHAPE (None: the sync gap). "screen" is the dict TCL's TVs take;
# "apple" what iOS asks for when it mirrors; "media" owntone's for AirPlay 2 speakers, with
# isMedia and without usingScreen.
AUDIO_SHAPES = {"screen": (0, None), "minmax": (None, None), "apple": (3750, 3750), "media": (11025, 88200)}
# The keys whose bytes a SETUP's DEBUG line leaves out (_redacted), wherever they are in it.
SECRET_KEYS = frozenset({"shk", "shiv", "ekey", "eiv"})


@dataclass
class Plan:
    """What to ask the TV for. A device's working plan is saved (profiles.py)."""

    ordering: str = CONTROL_FIRST
    audio: bool = True  # the type-96 stream (with the PC's sound only when that is asked for)
    timing: str = NTP
    lead_ms: int = DEFAULT_LEAD_MS
    fallback: bool = True  # the one-shot fallbacks (the other ordering; NTP after PTP)

    def describe(self) -> str:
        audio = "with" if self.audio else "without"
        return f"{self.ordering}, {audio} the audio stream, {self.timing.upper()} timing"


@dataclass
class AudioOffer:
    """What the type-96 stream asks for. The defaults are the stream as it was before it carried
    sound, as it still is when the sound is not wanted: ALAC, latencies 0 and the lead."""

    format: AudioFormat = ALAC_44100
    shape: str = "screen"  # a key of AUDIO_SHAPES
    gap: Optional[int] = None  # the sync gap, in samples (None: the lead)
    lat_min: Optional[int] = None  # in samples, in place of the shape's
    lat_max: Optional[int] = None
    is_media: bool = False  # isMedia
    redundant: int = 0  # redundantAudio: each packet sent this many times more
    ports: Optional[tuple[int, int]] = None  # (data, control) in place of the TV's answer
    # A new cipher for each attempt (its key goes into that attempt's SETUP): FairPlay TVs would
    # get one of their own here.
    cipher_factory: Callable[[Optional[ControlChannel]], AudioCipher] = ChaChaShk.for_channel


@dataclass
class AudioSetup:
    """The type-96 stream as the TV took it: where its packets and syncs go, and how they are
    sealed."""

    stream_id: int  # its streamConnectionID
    format: AudioFormat
    cipher: AudioCipher
    gap: int  # the sync gap, in samples
    lat_min: int
    lat_max: int
    data_port: int  # the TV's, for the packets
    control_port: int  # the TV's, for the syncs and the resend answers
    answer: dict  # the TV's streams[] entry for it
    redundant: int = 0


@dataclass
class Established:
    """A session the TV accepted."""

    plan: Plan  # as it worked: the ordering and timing really used
    uuid: str
    uri: str  # the session's URI: RECORD and TEARDOWN go there
    video_uri: str
    video_id: int  # the video stream's streamConnectionID (its key's salt)
    data_port: int
    event_port: Optional[int] = None
    receiver_timing_port: Optional[int] = None  # the TV's own timing port, where its requests come from
    skip_record: bool = False
    ptp_clock_id: Optional[int] = None  # what a PTP SETUP answered, if one was tried
    ptp_answered: bool = False
    timeline_id: int = 0  # [40:48] of each frame: 0 under NTP, our ClockID under PTP
    ptp_addresses: list[str] = field(default_factory=list)  # the TV's timingPeerInfo.Addresses, under PTP
    fallbacks: list[str] = field(default_factory=list)
    replies: list[dict] = field(default_factory=list)
    audio: Optional[AudioSetup] = None  # the type-96 stream, if the TV took it with its ports
    audio_note: str = ""  # why there is none
    record_headers: dict = field(default_factory=dict)  # RECORD's answer (Audio-Latency)


class _ShapeRefused(Exception):
    def __init__(self, code: int, step: str) -> None:
        super().__init__(f"{step}: {code}")
        self.code = code
        self.step = step


class _PtpTried(Exception):
    """The TV answered a PTP SETUP (with or without a ClockID); the session was torn down.
    `ordering` is the one it answered in. `why`: a session kept with our clock failed at a
    later step (what the TV said)."""

    def __init__(self, clock_id: Optional[int], ordering: str, why: str = "") -> None:
        super().__init__(why or f"ClockID {clock_id}")
        self.clock_id = clock_id
        self.ordering = ordering
        self.why = why


def _stream_id() -> int:
    return secrets.randbits(63)


def _first(replies: list[dict], key: str) -> Any:
    for reply in replies:
        value = reply.get(key)
        if value not in (None, 0, ""):
            return value
    return None


def _clock_id(reply: dict) -> Optional[int]:
    peer = reply.get("timingPeerInfo")
    if isinstance(peer, dict) and isinstance(peer.get("ClockID"), int):
        return peer["ClockID"]
    return None


def _redacted(value: Any) -> Any:
    """`value` for the log: the bytes of every key in SECRET_KEYS, at any depth, as "<N bytes>"."""
    if isinstance(value, dict):
        return {key: f"<{len(item)} bytes>" if key in SECRET_KEYS and isinstance(item, (bytes, bytearray))
                else _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536


def audio_answer(replies: list[dict], index: int, ports: Optional[tuple[int, int]] = None
                 ) -> tuple[Optional[dict], Optional[tuple[int, int]], str]:
    """The TV's answer to the type-96 stream: (its streams[] entry, (dataPort, controlPort), "")
    or, when the sound cannot go, (the entry or None, None, why). The entry is the one typed 96
    in any answer, else the first not typed 110 in the audio SETUP's own answer (replies[index]),
    as a TV may leave the type out. `ports` (MIRRORBRIDGE_MIRROR_SOUND_PORTS) stand in for the
    ones it names. Without a control port there is no sound: the syncs would go to the data port,
    where a receiver takes anything for audio."""
    entry = next((stream for reply in replies for stream in reply.get("streams") or []
                  if isinstance(stream, dict) and stream.get("type") == AUDIO_STREAM), None)
    if entry is None and 0 <= index < len(replies):
        entry = next((stream for stream in replies[index].get("streams") or []
                      if isinstance(stream, dict) and stream.get("type") != VIDEO_STREAM), None)
    if ports is not None:
        return entry if entry is not None else {}, ports, ""
    if entry is None:
        return None, None, "the TV did not answer the audio stream"
    if not _port(entry.get("dataPort")):
        return entry, None, "the TV named no port for the sound"
    if not _port(entry.get("controlPort")):
        return entry, None, "the TV named no control port for the sound"
    return entry, (entry["dataPort"], entry["controlPort"]), ""


class Negotiator:
    """Opens the control connection and sets a session up on it, with the one-shot fallbacks.

    `steps` collects each request's answer (for the log, errors and the probe's report).
    """

    def __init__(
        self,
        target: Target,
        *,
        timing_port: int,
        audio_port: int,
        name: str = "MirrorBridge",
        setup_seconds: float = SETUP_SECONDS,
        steps: Optional[dict] = None,
        audio: Optional[AudioOffer] = None,
    ) -> None:
        self.target = target
        self.timing_port = timing_port
        self.audio_port = audio_port
        self.audio = audio or AudioOffer()  # what the type-96 stream asks for (the session sets it)
        self.name = name
        self.setup_seconds = setup_seconds
        self.device_id = sender_id(target.credentials)
        # For PTP's timingPeerInfo: an ID of its own for this PC, the same for one pairing.
        self.clock_peer_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"mirrorbridge.{self.device_id}")).upper()
        self.steps: dict = steps if steps is not None else {}
        self.channel: Optional[ControlChannel] = None
        self.on_reply: Optional[Callable[[dict], None]] = None  # each accepted SETUP's answer
        # A session whose answers went to on_reply is gone, and one is set up again (NTP after PTP).
        self.on_restart: Optional[Callable[[], None]] = None
        # Our PTP ClockID while our own PTP clock runs (ptp.py): a PTP SETUP is then kept, not
        # only tried. None: a PTP SETUP only finds out what the TV answers (the probe).
        self.ptp_clock_id: Optional[int] = None

    async def connect(self) -> ControlChannel:
        """A new control connection with a fresh pair-verify."""
        if self.channel is not None:
            self.channel.close()
        self.channel = None
        try:
            self.channel = await ControlChannel.open(self.target)
        except MirrorError as ex:
            self.steps.update(ex.steps)
            ex.steps = dict(self.steps)
            raise
        self.steps["pair-verify"] = "ok"
        return self.channel

    async def establish(self, plan: Plan) -> Established:
        """Set the session up as `plan` says: on the open connection, or a new one if it is
        closed or out of step (a request on it went unanswered)."""
        if self.channel is None or self.channel.closed or self.channel.out_of_step:
            await self.connect()
        try:
            return await self._set_up(plan)
        except _PtpTried as tried:
            if tried.why:
                note = f"PTP timing did not work ({tried.why}): NTP on a fresh pair-verify"
            elif tried.clock_id is None:
                note = "PTP SETUP answered without timingPeerInfo.ClockID: NTP on a fresh pair-verify"
            else:
                note = f"PTP SETUP answered with ClockID {tried.clock_id:#x}, which MirrorBridge cannot use yet: NTP"
            _LOGGER.info("%s", note)
            if not plan.fallback:
                raise MirrorError(Text(
                    "mirror_no_ptp", "The TV wants PTP timing, which MirrorBridge does not have yet."
                ), step="setup", steps=self.steps) from tried
            return await self._ntp_instead(plan, tried.ordering, note, tried.clock_id)

    async def ptp_fallback(self, est: Established, why: str) -> Established:
        """The TV took a PTP session but its clock did not follow ours (`why`): TEARDOWN, then
        NTP on a fresh pair-verify, in the ordering it took."""
        await self.teardown(est.uri)
        note = f"PTP timing did not work ({why}): NTP on a fresh pair-verify"
        _LOGGER.info("%s", note)
        ntp = await self._ntp_instead(est.plan, est.plan.ordering, note, None)
        ntp.fallbacks = est.fallbacks + ntp.fallbacks
        return ntp

    async def _ntp_instead(self, plan: Plan, ordering: str, note: str, clock_id: Optional[int]) -> Established:
        if self.on_restart is not None:
            try:
                self.on_restart()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Restart callback failed")
        await self.connect()
        # The ordering the TV answered PTP in, so that it is not refused first again.
        est = await self._set_up(dataclasses.replace(plan, timing=NTP, ordering=ordering))
        est.fallbacks.insert(0, note)
        est.ptp_answered = True
        est.ptp_clock_id = clock_id
        return est

    async def _set_up(self, plan: Plan) -> Established:
        orderings = [plan.ordering]
        if plan.fallback:
            orderings.append(MEDIA_FIRST if plan.ordering == CONTROL_FIRST else CONTROL_FIRST)
        fallbacks: list[str] = []
        for index, ordering in enumerate(orderings):
            try:
                est = await self._attempt(dataclasses.replace(plan, ordering=ordering))
            except _ShapeRefused as ex:
                if index + 1 == len(orderings):
                    raise refused(ex.code, self.steps, ex.step) from ex
                note = f"{ordering} SETUP refused with {ex.code}: trying {orderings[index + 1]}"
                _LOGGER.info("%s", note)
                fallbacks.append(note)
                await self.connect()
                continue
            est.fallbacks = fallbacks + est.fallbacks
            return est
        raise AssertionError("unreachable")

    # -- the requests ---------------------------------------------------------------------------

    def _session_fields(self, plan: Plan, session_uuid: str, cipher: Optional[AudioCipher] = None) -> dict:
        """The session's own fields (in the order plistlib writes them: by name), with what the
        audio's cipher puts there (nothing for ChaChaShk)."""
        assert self.channel is not None
        fields: dict = {
            "deviceID": self.device_id,
            "isScreenMirroringSession": True,
            "macAddress": self.device_id,
            "model": SENDER_MODEL,
            "name": self.name,
            "osBuildVersion": SENDER_OS_BUILD,
            "sessionUUID": session_uuid,
            "sourceVersion": SOURCE_VERSION,
            "timingProtocol": "PTP" if plan.timing == PTP else "NTP",
        }
        fields.update({key: SENDER[key] for key in ("osName", "osVersion") if key in SENDER})  # experiment
        if plan.timing == PTP and self.ptp_clock_id is not None:
            # Our own grandmaster clock, as OwnTone describes its own (no ClockPorts, so no
            # port matching to override).
            peer = {"Addresses": [self.channel.local_ip], "ClockID": self.ptp_clock_id, "DeviceType": 0,
                    "ID": self.clock_peer_id, "SupportsClockPortMatchingOverride": False}
            fields["timingPeerInfo"] = peer
            fields["timingPeerList"] = [peer]
        elif plan.timing == PTP:
            # This PC as a PTP clock peer, as a sender that has one describes itself.
            peer = {"Addresses": [self.channel.local_ip], "DeviceType": 0, "ID": self.clock_peer_id,
                    "SupportsClockPortMatchingOverride": True}
            fields["timingPeerInfo"] = peer
            fields["timingPeerList"] = [peer]
        else:
            fields["timingPort"] = self.timing_port
        if cipher is not None:
            fields.update(cipher.session_fields())
        return fields

    def _latencies(self, plan: Plan) -> tuple[int, int, int]:
        """The sync gap, latencyMin and latencyMax (samples) self.audio asks for: the shape's,
        then the overrides; the gap is the lead unless the offer names one."""
        offer = self.audio
        gap = offer.gap if offer.gap is not None else ms_to_samples(plan.lead_ms)
        low, high = AUDIO_SHAPES.get(offer.shape, AUDIO_SHAPES["screen"])
        low = offer.lat_min if offer.lat_min is not None else gap if low is None else low
        high = offer.lat_max if offer.lat_max is not None else gap if high is None else high
        return gap, low, high

    def _audio_stream(self, stream_id: int, plan: Plan, cipher: AudioCipher) -> dict:
        """The screen-audio stream as self.audio asks for it, with the key `cipher` gives the TV.
        By default it is the stream as it was before it carried sound: ALAC, latencies 0 and the
        lead, usingScreen."""
        offer = self.audio
        fmt = offer.format
        _gap, low, high = self._latencies(plan)
        stream = {
            "audioFormat": fmt.audio_format,
            "audioMode": "default",
            "controlPort": self.audio_port,
            "ct": fmt.ct,
            "latencyMax": high,
            "latencyMin": low,
            **cipher.stream_fields(),
            "spf": fmt.spf,
            "sr": fmt.sr,
            "streamConnectionID": stream_id,
            "type": AUDIO_STREAM,
        }
        if offer.shape == "media":
            stream.update(isMedia=True, supportsDynamicStreamID=False)
        else:
            stream["usingScreen"] = True
        if offer.is_media:
            stream["isMedia"] = True
        if offer.redundant:
            stream["redundantAudio"] = offer.redundant
        return stream

    def _video_stream(self, stream_id: int, plan: Plan) -> dict:
        assert self.channel is not None
        write_key, read_key = self.channel.derive(CONTROL_SALT, CONTROL_WRITE, CONTROL_READ)
        return {
            "latencyMs": plan.lead_ms,
            # The video key comes from pair-verify (packets.video_key); TVs paired the HomeKit way
            # get these as well, as other senders send them.
            "shiv": read_key[:16],
            "shk": write_key[:16],
            "streamConnectionID": stream_id,
            "timestampInfo": [{"name": name} for name in TIMESTAMP_INFO],
            "type": VIDEO_STREAM,
        }

    async def _setup(self, step: str, uri: str, body: dict, first: bool = False) -> dict:
        assert self.channel is not None
        _LOGGER.debug("SETUP %s (%s): %s", uri, step, _redacted(body))
        try:
            resp = await self.channel.request("SETUP", uri, body=body, timeout=self.setup_seconds)
        except (NoAnswer, ChannelClosed) as ex:
            self.steps[step] = answer_of(ex)
            raise refused(None, self.steps, step) from ex
        self.steps[step] = resp.code
        if 200 <= resp.code < 300:
            reply = decode_plist(resp)
            _LOGGER.info("SETUP (%s) answered %d: %s", step, resp.code, reply)
            return reply
        _LOGGER.info("SETUP (%s) refused: %d %s", step, resp.code, resp.message)
        if first and resp.code in SHAPE_REFUSED:
            raise _ShapeRefused(resp.code, step)
        raise refused(resp.code, self.steps, step)

    async def _attempt(self, plan: Plan) -> Established:
        host = f"{self.target.address}:{self.target.port}"
        session_uuid = str(uuid.uuid4()).upper()
        audio_id, video_id = _stream_id(), _stream_id()
        audio_uri, video_uri = f"rtsp://{host}/{audio_id}", f"rtsp://{host}/{video_id}"
        cipher = self.audio.cipher_factory(self.channel)  # a new key for each attempt
        session = self._session_fields(plan, session_uuid, cipher)
        video = self._video_stream(video_id, plan)
        audio = self._audio_stream(audio_id, plan, cipher) if plan.audio else None
        uri = audio_uri if audio or plan.ordering == CONTROL_FIRST else video_uri
        replies: list[dict] = []
        first_step = f"setup {plan.ordering}"
        if plan.ordering == CONTROL_FIRST:
            body = {**session, "updateSessionRequest": False, "combinedGetInfoWithControlSetup": True}
        else:
            body = {**session, "streams": [audio or video]}
        replies.append(await self._setup(first_step, uri, body, first=True))
        addresses: list[str] = []
        kept = False  # a PTP session with our clock, which a refusal from here on turns into NTP
        try:
            if plan.timing == PTP:
                clock = _clock_id(replies[0])
                self.steps["ptp clock"] = f"{clock:#x}" if clock is not None else "none"
                if self.ptp_clock_id is None or clock is not None:
                    await self.teardown(uri)
                    raise _PtpTried(clock, plan.ordering)
                kept = plan.fallback
                peer = replies[0].get("timingPeerInfo")
                _LOGGER.info("PTP SETUP answered without a ClockID, kept: the TV's timingPeerInfo is %s", peer)
                if isinstance(peer, dict) and isinstance(peer.get("Addresses"), list):
                    addresses = [str(address) for address in peer["Addresses"]]
            self._answered(replies[-1])
            if audio and plan.ordering == CONTROL_FIRST:
                replies.append(await self._setup("setup audio", uri, {"streams": [audio]}))
                self._answered(replies[-1])
            if audio or plan.ordering == CONTROL_FIRST:
                replies.append(await self._setup("setup video", video_uri, {"streams": [video]}))
                self._answered(replies[-1])
            data_port = None
            for reply in replies:
                for stream in reply.get("streams") or []:
                    if isinstance(stream, dict) and stream.get("type") == VIDEO_STREAM and stream.get("dataPort"):
                        data_port = int(stream["dataPort"])
            if not data_port:
                self.steps["data port"] = "none"
                raise MirrorError(Text(
                    "mirror_no_data_port", "The TV accepted screen mirroring but named no port for the video "
                    "({steps}). Please report this TV, with mirrorbridge.log.",
                    steps=format_steps(self.steps),
                ), step="setup video", steps=self.steps)
        except _PtpTried:
            raise
        except BaseException as ex:
            await self.teardown(uri)
            if kept and isinstance(ex, MirrorError):
                # Under PTP, which a TV may not get on with (its stream setups may wait for a
                # clock that only starts with SETPEERS): NTP, as when its clock does not follow.
                if ex.code is not None:
                    why = f"{ex.step} answered {ex.code}"
                elif self.steps.get("data port") == "none":
                    why = "the SETUPs named no port for the video"
                else:
                    why = f"{ex.step}: {self.steps.get(ex.step, 'no answer')}"
                raise _PtpTried(None, plan.ordering, why) from ex
            raise
        timing_port = _first(replies, "timingPort")
        sound, note = self._audio_setup(replies, plan, audio_id, cipher) if audio else (
            None, "no audio stream in this TV's plan")
        return Established(
            plan=plan, uuid=session_uuid, uri=uri, video_uri=video_uri, video_id=video_id, data_port=data_port,
            event_port=_first(replies, "eventPort"),
            receiver_timing_port=int(timing_port) if isinstance(timing_port, int) else None,
            skip_record=bool(_first(replies, "skipRecord")), replies=replies,
            timeline_id=(self.ptp_clock_id or 0) if plan.timing == PTP else 0, ptp_addresses=addresses,
            audio=sound, audio_note=note,
        )

    def _audio_setup(self, replies: list[dict], plan: Plan, stream_id: int,
                     cipher: AudioCipher) -> tuple[Optional[AudioSetup], str]:
        """The type-96 stream as the TV took it (audio_answer), or None and why not. Never raises:
        the video goes on either way."""
        index = 1 if plan.ordering == CONTROL_FIRST else 0  # the audio SETUP's answer
        entry, ports, note = audio_answer(replies, index, self.audio.ports)
        if entry:
            _LOGGER.info("Sound: the TV answered the audio stream with %s", entry)
        if ports is None:
            return None, note
        gap, low, high = self._latencies(plan)
        return AudioSetup(stream_id, self.audio.format, cipher, gap, low, high, ports[0], ports[1], dict(entry),
                          self.audio.redundant), ""

    def _answered(self, reply: dict) -> None:
        if self.on_reply is not None:
            try:
                self.on_reply(reply)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("SETUP answer callback failed")

    async def set_peers(self, est: Established, peers: list[str]) -> Any:
        """SETPEERS: the session's clock peers (the TV's address and ours), a binary-plist array,
        which starts the TV's PTP clock. The TV's answer: 200 when it took them."""
        assert self.channel is not None
        body = plistlib.dumps(peers, fmt=plistlib.FMT_BINARY)  # pylint: disable=no-member
        try:
            resp = await self.channel.request("SETPEERS", est.uri, body=body, content_type=PEER_LIST_CHANGED,
                                              timeout=RECORD_SECONDS)
            answer: Any = resp.code
        except (NoAnswer, ChannelClosed) as ex:
            answer = answer_of(ex)
        self.steps["setpeers"] = answer
        _LOGGER.info("SETPEERS %s: %s", peers, answer)
        return answer

    async def record(self, est: Established) -> None:
        """RECORD: the session starts (unless the TV said it starts without)."""
        if est.skip_record:
            self.steps["record"] = "skipped"
            return
        assert self.channel is not None
        headers = {"Session": est.uuid, "Range": "npt=0-", "RTP-Info": "seq=0;rtptime=0"}
        try:
            resp = await self.channel.request("RECORD", est.uri, headers=headers, timeout=RECORD_SECONDS)
        except (NoAnswer, ChannelClosed) as ex:
            self.steps["record"] = answer_of(ex)
            raise refused(None, self.steps, "record") from ex
        self.steps["record"] = resp.code
        est.record_headers = dict(resp.headers or {})
        latency = (resp.headers or {}).get("Audio-Latency")  # the TV's own delay, in samples (UxPlay: 11025)
        _LOGGER.info("RECORD answered %d%s", resp.code, f"; Audio-Latency: {latency}" if latency is not None else "")
        _LOGGER.debug("RECORD's answer: %s", est.record_headers)
        if not 200 <= resp.code < 300:
            raise refused(resp.code, self.steps, "record")

    async def set_volume(self, est: Established, db: float, timeout: float = FEEDBACK_SECONDS) -> Any:
        """SET_PARAMETER volume on the session, as owntone sends it (dB: -30 to 0, -144 mutes):
        the TV's answer code, or what went wrong."""
        assert self.channel is not None
        body = f"volume: {db:.6f}\r\n".encode()
        try:
            resp = await self.channel.request("SET_PARAMETER", est.uri, body=body, content_type="text/parameters",
                                              timeout=timeout)
            answer: Any = resp.code
        except (NoAnswer, ChannelClosed) as ex:
            answer = answer_of(ex)
        self.steps["volume"] = answer
        _LOGGER.info("SET_PARAMETER volume %g dB: %s", db, answer)
        return answer

    async def teardown(self, uri: str) -> None:
        """TEARDOWN, as a courtesy: whatever the TV answers (or not), the session is over."""
        channel = self.channel
        if channel is None or channel.closed:
            return
        try:
            resp = await channel.request("TEARDOWN", uri, timeout=TEARDOWN_SECONDS)
            self.steps["teardown"] = resp.code
        except (NoAnswer, ChannelClosed) as ex:
            self.steps["teardown"] = answer_of(ex)
        _LOGGER.debug("TEARDOWN %s: %s", uri, self.steps["teardown"])

    def close(self) -> None:
        if self.channel is not None:
            self.channel.close()


class Feedback:
    """POST /feedback every FEEDBACK_INTERVAL, the first one at once, on the control connection
    (one request at a time with the others). `lost` is set when the connection is gone."""

    def __init__(self, channel: ControlChannel) -> None:
        self.channel = channel
        self.sent = 0
        self.answers: dict[str, int] = {}  # answer -> how many times
        self.last_body: Optional[dict] = None  # the last plist a 2xx answer carried ({} for none)
        self.lost = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                resp = await self.channel.request("POST", "/feedback", timeout=FEEDBACK_SECONDS)
                answer = str(resp.code)
                failures = 0
                if 200 <= resp.code < 300:
                    body = decode_plist(resp)
                    if body != self.last_body:  # what a TV says there may tell how it takes the sound
                        _LOGGER.debug("POST /feedback answered %s", body)
                        self.last_body = body
            except ChannelClosed:
                _LOGGER.info("The TV closed the control connection")
                self.lost.set()
                return
            except NoAnswer:
                answer = "no answer"
                failures += 1
            self.sent += 1
            self.answers[answer] = self.answers.get(answer, 0) + 1
            if self.sent == 1 or answer not in ("200",):
                _LOGGER.debug("POST /feedback: %s", answer)
            if failures >= 3:
                _LOGGER.info("The TV stopped answering /feedback")
                self.lost.set()
                return
            await asyncio.sleep(FEEDBACK_INTERVAL)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
            self._task = None
