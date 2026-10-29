"""Finding out how a TV takes screen mirroring, in one go, for a bug report or before a session.

probe_mirroring() goes through what a session does, one step at a time, each within a time
limit of its own and each SETUP on a fresh pair-verified connection:

1. whether the TV needs FairPlay (an Apple TV or a Mac: nothing more is tried);
2. pair-verify and GET /info;
3. the SETUP shapes, the saved (or default) one first, until the TV takes one, each torn down
   at once;
4. a few seconds of the test pattern with the shape it took, through a whole session: the
   video connection, RECORD, /feedback, the TV asking for the time, frames; if the TV drops the
   video connection, again with the other way of ending the avcC record ("trailer"), then with
   the other ordering. Without the test pattern, the video connection is only held open for a
   moment, and the TV must still ask for the time. What worked is saved for the device
   (profiles.py) there and then, so the next session asks for it at once;
5. for the report, the rest of what `variants` names, in the time left: the shape that worked
   without (or with) the silent audio stream, PTP timing (does the TV give a ClockID), and the
   other ordering.

The test pattern comes as soon as the TV has taken a SETUP, so that a time limit in all (Test
this TV gives one) goes on what the person watches the TV for, and on what Mirror screen needs.
Whatever happens, every session the probe set up is torn down, also when that limit runs out.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .. import ffmpeg
from ..airplay_patch import format_steps
from ..config import Settings
from ..session import State
from ..text import Text
from . import profiles
from .clock import NtpResponder, bind_udp
from .control import SOURCE_VERSION, USER_AGENT, ReceiverInfo, Target, connect_failed
from .datachannel import DataChannel
from .errors import MirrorError, answer_of, data_closed, data_refused, message_of, needs_fairplay, no_timing, refused
from .h264 import TRAILERS
from .negotiation import CONTROL_FIRST, MEDIA_FIRST, NTP, PTP, Established, Negotiator, Plan
from .session import MirrorSession, SessionReport
from .source import TEST_PATTERN

_LOGGER = logging.getLogger(__name__)

ORDERING = "ordering"  # both SETUP orderings
AUDIO = "audio"  # with and without the type-96 audio stream
TIMING = "timing"  # a PTP SETUP as well, to see whether the TV gives a ClockID
TRAILER = "trailer"  # the other avcC ending, if the TV drops the video
ALL_VARIANTS = (ORDERING, AUDIO, TIMING, TRAILER)

INFO_STEP_SECONDS = 40  # connect, pair-verify and GET /info
SETUP_STEP_SECONDS = 90  # one SETUP shape, PTP and its NTP retry included
STREAM_SETUP_SECONDS = 75  # a stream check's time on top of the seconds it streams
DATA_HOLD_SECONDS = 2.0  # without the test pattern: how long the video connection is held open
TIMING_WAIT_SECONDS = 5.0  # and then how much longer the TV may take to ask for the time
MAX_STREAM_CHECKS = 3
FIT_MARGIN_SECONDS = 2.0  # with a time limit, a step for the report starts only with this to spare


@dataclass
class SetupAttempt:
    """One SETUP shape and what the TV said to it."""

    plan: Plan  # as asked; established.plan is what was used in the end
    worked: bool = False
    steps: dict = field(default_factory=dict)  # each request -> the TV's status code (or "no answer"...)
    established: Optional[Established] = None
    note: str = ""  # the fallbacks taken, or in short why it failed (English, for lines())
    error: Optional[str] = None  # why it failed, a Text

    def line(self) -> str:
        if self.worked and self.established is not None:
            est = self.established
            extra = [f"video data port {est.data_port}"]
            if est.event_port:
                extra.append(f"event port {est.event_port}")
            if est.receiver_timing_port:
                extra.append(f"the TV's timing port {est.receiver_timing_port}")
            if est.skip_record:
                extra.append("skipRecord")
            result = "ok (" + ", ".join(extra) + ")"
            if self.note:
                result += f" after: {self.note}"
        else:
            result = f"failed: {self.note}"
        return f"SETUP {self.plan.describe()}: {result} [{format_steps(self.steps)}]"


@dataclass
class StreamCheck:
    """A few seconds of the test pattern (or, without it, the video connection held open)."""

    plan: Plan
    trailer: str
    worked: bool = False
    report: Optional[SessionReport] = None
    note: str = ""  # in short why it failed (English, for lines())
    error: Optional[str] = None  # why it failed, a Text
    stopped: bool = False  # the probe's time limit ran out while it went

    def line(self) -> str:
        report = self.report
        head = f"Video ({self.plan.ordering}, avcC ending {self.trailer})"
        if report is None:
            return f"{head}: {self.note}"
        parts = [f"video connection {report.data_channel}"]
        if report.picture:
            parts.append(f"{report.picture[0]}x{report.picture[1]} at {report.fps} fps with {report.encoder}, "
                         f"keyframes {report.keyframes}")
        parts.append(f"{report.frames_sent} frames sent")
        if report.data_closed_after is not None:
            parts.append(f"the TV closed the video connection after {report.data_closed_after} frames")
        parts.append(f"the TV asked for the time {report.ntp_requests} times")
        if report.feedback:
            parts.append("/feedback " + ", ".join(f"{answer} x{count}" for answer, count in report.feedback.items()))
        if report.first_frame_ms is not None:
            parts.append(f"first frame {report.first_frame_ms:.0f} ms after starting FFmpeg")
        latency = {k: v for k, v in report.latency.items() if isinstance(v, tuple)}
        if latency:
            parts.append("ms p50/p95 " + ", ".join(f"{stage} {p50}/{p95}" for stage, (p50, p95) in latency.items()))
        verdict = "ok" if self.worked else self.note if self.stopped else f"failed: {self.note or self.error}"
        return f"{head}: {verdict}; " + "; ".join(parts) + f" [{format_steps(report.steps)}]"


@dataclass
class ProbeResult:
    """What the probe found out. `profile` is the combination that worked, None if none did."""

    target: str
    address: str
    fairplay: bool = False
    features: Optional[int] = None
    srcvers: str = ""
    sender: str = f"User-Agent {USER_AGENT}, sourceVersion {SOURCE_VERSION}"  # who MirrorBridge says it is
    pair_verify: str = "not tried"
    info: Optional[ReceiverInfo] = None
    setups: list[SetupAttempt] = field(default_factory=list)
    ptp: str = "not tried"  # "ClockID 0x...", "answered without a ClockID", "refused (...)"
    ntp_after_ptp: Optional[bool] = None  # whether NTP worked on a fresh pair-verify after PTP
    streams: list[StreamCheck] = field(default_factory=list)
    profile: Optional[profiles.MirrorProfile] = None
    saved: Optional[Path] = None
    error: Optional[str] = None  # why it did not work, a Text
    not_tried: list[str] = field(default_factory=list)  # what `variants` named, left out for lack of time
    seconds: float = 0.0

    @property
    def worked(self) -> bool:
        return self.profile is not None

    @property
    def session_accepted(self) -> bool:
        """The TV accepted a mirroring session (some SETUP shape, or a stream check's)."""
        return any(attempt.worked for attempt in self.setups) or any(
            check.report is not None and check.report.established is not None for check in self.streams)

    @property
    def data_channel_accepted(self) -> bool:
        return any(s.report is not None and s.report.data_channel != "not opened"
                   and not s.report.data_channel.startswith("refused") for s in self.streams)

    @property
    def data_channel_kept(self) -> bool:
        """The TV kept the video connection open while frames went (or while it was held open)."""
        return any(s.worked for s in self.streams)

    def conclusion(self) -> Text:
        if self.profile is not None:
            return Text("mirror_probe_works", "Screen mirroring works with {device}: {how}.",
                        device=self.target, how=self.profile.describe())
        if self.error:
            return self.error
        return Text("mirror_probe_failed", "Screen mirroring did not work with {device}. Please report this TV, "
                    "with mirrorbridge.log.", device=self.target)

    def lines(self) -> list[str]:
        """The report, in English, a line per step (for the command line and the log)."""
        out = [f"Screen mirroring probe: {self.target} ({self.address})"]
        if self.features is not None:
            out.append(f"Features: {self.features:#x}" + (f", srcvers {self.srcvers}" if self.srcvers else "")
                       + (" - FairPlay needed" if self.fairplay else " - no FairPlay needed"))
        out.append(f"Sent as: {self.sender}")
        out.append(f"Pair-verify: {self.pair_verify}")
        if self.info is not None:
            out.append(f"GET /info: {self.info.summary()}")
        out += [attempt.line() for attempt in self.setups]
        if self.ptp != "not tried":
            retry = {None: "", True: "; NTP on a fresh pair-verify: ok", False: "; NTP on a fresh pair-verify: failed"}
            out.append(f"PTP: {self.ptp}{retry[self.ntp_after_ptp]}")
        out += [check.line() for check in self.streams]
        if self.not_tried:
            out.append("Not tried, for lack of time: " + "; ".join(self.not_tried))
        if self.saved is not None:
            out.append(f"Saved to {self.saved}")
        out.append(str(self.conclusion()))
        out.append(f"({self.seconds:.1f} s)")
        return out


def _short(ex: MirrorError) -> str:
    """Why a step failed, in a few words (the whole message is in `error`)."""
    if ex.code is not None:
        return f"refused with {ex.code}"
    answer = ex.steps.get(ex.step)
    return f"{ex.step}: {answer}" if answer else message_of(ex)


class _Probe:
    def __init__(self, target: Target, settings: Settings, *, seconds: float, send_test_pattern: bool,
                 variants: Iterable[str], save: bool, progress: Callable[[str], None],
                 on_pattern: Callable[[], None], deadline: Optional[float]) -> None:
        self.target = target
        # Without the Debug menu's test switches, the Latency or options typed into settings.json:
        # what the probe finds and saves is the TV's own. With a keyframe every 2 s, as the probe
        # always sent: none of its few seconds goes on FFmpeg started again for one.
        defaults = Settings()
        self.settings = dataclasses.replace(settings, mirror_lead_ms=defaults.mirror_lead_ms,
                                            mirror_latency=defaults.mirror_latency,
                                            mirror_keyframes=ffmpeg.MIRROR_KEYFRAME_SECONDS,
                                            mirror_scaler=defaults.mirror_scaler,
                                            mirror_encoder_extra=defaults.mirror_encoder_extra,
                                            mirror_on_arrival=defaults.mirror_on_arrival)
        self.seconds = seconds
        self.send_test_pattern = send_test_pattern
        self.variants = set(variants)
        self.save = save
        self.progress = progress
        self.on_pattern = on_pattern
        self.deadline = deadline  # time.monotonic() when the time limit runs out, None without one
        self.start = profiles.load(target.identifier) or profiles.MirrorProfile()
        self.result = ProbeResult(target.name, f"{target.address}:{target.port}", fairplay=target.needs_fairplay,
                                  features=target.features, srcvers=target.srcvers)
        self.negotiator = Negotiator(target, timing_port=0, audio_port=0)
        self.transports: list[asyncio.BaseTransport] = []
        self.responder: Optional[NtpResponder] = None  # the timing port the SETUPs name
        self.local_ip = ""  # this PC's address towards the TV
        self.slowest = 0.0  # the longest a SETUP shape has taken so far, in s

    def say(self, line: str) -> None:
        _LOGGER.info("Probe: %s", line)
        try:
            self.progress(line)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Probe progress callback failed")

    def _pattern_shown(self) -> None:
        try:
            self.on_pattern()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Probe callback failed")

    async def run(self) -> ProbeResult:
        result = self.result
        if result.fairplay:
            result.error = message_of(needs_fairplay(self.target.name))
            return result
        try:
            if not await self._info():
                return result
            plans = self._plans()
            plan = await self._first(plans)
            if plan is None:
                return result
            plan = await self._streams(plan, plans)
            await self._rest(plan, plans)
        finally:
            self._close_udp()
            self.negotiator.close()
        return result

    def ran_out(self, limit: float) -> None:
        """The time limit stopped the probe: a stream check under way says how far it got."""
        result = self.result
        check = result.streams[-1] if result.streams else None
        if check is not None and not check.worked and check.error is None:
            check.stopped = True
            check.note = (f"stopped by the time limit after {check.report.frames_sent if check.report else 0} "
                          "frames")
            check.error = time_up(limit)
        if result.profile is None:
            result.error = time_up(limit)

    def _save(self) -> None:
        """What worked, for the device's next session (Mirror screen's too), as soon as it is known."""
        result = self.result
        if result.profile is None or not self.save or not self.target.identifier:
            return
        info = result.info.summary() if result.info else ""
        try:
            result.saved = profiles.save(self.target.identifier, result.profile, name=self.target.name, info=info)
        except OSError as ex:
            _LOGGER.warning("Could not save how to mirror to %s: %s", self.target.name, ex)

    async def _bind_udp(self) -> None:
        """The timing port (answered by an NtpResponder) and the audio control port the SETUPs name."""
        transport, responder = await bind_udp(NtpResponder, self.local_ip)
        self.transports.append(transport)
        self.responder = responder
        self.negotiator.timing_port = responder.port
        transport, _ = await bind_udp(asyncio.DatagramProtocol, self.local_ip, taken=(responder.port,))
        self.transports.append(transport)
        self.negotiator.audio_port = transport.get_extra_info("sockname")[1]

    def _close_udp(self) -> None:
        for transport in self.transports:
            transport.close()
        self.transports.clear()
        self.responder = None

    def _fits(self, setups: int) -> bool:
        """Whether `setups` more SETUP shapes fit in the time limit, at the pace the TV has answered."""
        return self.deadline is None or time.monotonic() + setups * self.slowest + FIT_MARGIN_SECONDS < self.deadline

    # -- 2. pair-verify and /info -----------------------------------------------------------------

    async def _info(self) -> bool:
        self.say("pair-verify and GET /info")
        try:
            channel = await asyncio.wait_for(self.negotiator.connect(), INFO_STEP_SECONDS)
            self.result.pair_verify = "ok"
            self.local_ip = channel.local_ip
            await self._bind_udp()
            self.result.info = await asyncio.wait_for(channel.info(), INFO_STEP_SECONDS)
        except MirrorError as ex:
            if self.result.pair_verify == "ok":  # no /info: SETUP may work all the same
                _LOGGER.warning("%s", ex)
                return True
            self.result.pair_verify = format_steps(ex.steps) if ex.steps else str(ex)
            self.result.error = message_of(ex)
            return False
        except asyncio.TimeoutError:
            if self.result.pair_verify == "ok":
                return True
            self.result.pair_verify = f"no answer within {INFO_STEP_SECONDS} s"
            self.result.error = message_of(connect_failed(self.target, "no answer"))
            return False
        finally:
            self.negotiator.close()
        return True

    # -- 3. SETUP shapes ---------------------------------------------------------------------------

    def _plans(self) -> list[Plan]:
        """The shapes to try: the saved (or default) one first, then the variants."""
        first = self.start.plan(fallback=ORDERING not in self.variants)
        plans = [first]
        if ORDERING in self.variants:
            other = MEDIA_FIRST if first.ordering == CONTROL_FIRST else CONTROL_FIRST
            plans.append(dataclasses.replace(first, ordering=other))
        if AUDIO in self.variants:
            plans += [dataclasses.replace(plan, audio=not plan.audio) for plan in list(plans)]
        return plans

    async def _attempt(self, plan: Plan) -> SetupAttempt:
        attempt = SetupAttempt(plan)
        if not self.transports:  # a stream check closed them
            await self._bind_udp()
        self.negotiator.steps = attempt.steps
        established = None
        started = time.monotonic()
        try:
            established = await asyncio.wait_for(self._establish(plan), SETUP_STEP_SECONDS)
        except MirrorError as ex:
            attempt.note = _short(ex)
            attempt.error = message_of(ex)
        except asyncio.TimeoutError:
            attempt.note = f"no answer within {SETUP_STEP_SECONDS} s"
            attempt.error = message_of(refused(None, attempt.steps, "setup"))
        else:
            attempt.worked = True
            attempt.established = established
            attempt.note = "; ".join(established.fallbacks)
        finally:
            if established is not None:
                await self.negotiator.teardown(established.uri)
            self.negotiator.close()
        self.slowest = max(self.slowest, time.monotonic() - started)
        self.result.setups.append(attempt)
        self.say(attempt.line())
        return attempt

    async def _establish(self, plan: Plan) -> Established:
        await self.negotiator.connect()
        return await self.negotiator.establish(plan)

    async def _first(self, plans: list[Plan]) -> Optional[Plan]:
        """The shapes in turn, each leaving `plans`, until the TV takes one: that shape as the
        TV took it (the ordering and timing used), None if it took none."""
        while plans:
            plan = plans.pop(0)
            self.say(f"SETUP {plan.describe()}")
            attempt = await self._attempt(plan)
            if attempt.worked and attempt.established is not None:
                return dataclasses.replace(attempt.established.plan, fallback=True)
            if attempt.steps.get("pair-verify") not in (None, "ok"):
                self.result.error = attempt.error  # the pairing is gone: nothing else will work
                return None
        failed = self.result.setups[-1] if self.result.setups else None
        self.result.error = failed.error if failed else None
        return None

    async def _rest(self, plan: Plan, plans: list[Plan]) -> None:
        """For the report, while time is left: `plan` (what the TV took) with the audio stream
        the other way round, then PTP, then the other ordering, as far as `variants` names them."""
        steps = [(other, False) for other in plans if other.ordering == plan.ordering]
        if TIMING in self.variants:
            steps.append((dataclasses.replace(plan, timing=PTP, fallback=True), True))
        steps += [(other, False) for other in plans if other.ordering != plan.ordering]
        for shape, ptp in steps:
            if not self._fits(2 if ptp else 1):  # PTP takes a second SETUP when the TV gives no ClockID
                self.say(f"SETUP {shape.describe()}: not tried, for lack of time")
                self.result.not_tried.append(f"SETUP {shape.describe()}")
                continue
            if ptp:
                await self._ptp(shape)
                continue
            self.say(f"SETUP {shape.describe()}")
            attempt = await self._attempt(shape)
            if attempt.steps.get("pair-verify") not in (None, "ok"):
                return  # the pairing is gone: nothing else will work

    async def _ptp(self, plan: Plan) -> None:
        self.say("SETUP with PTP timing, to see whether the TV gives a ClockID")
        attempt = await self._attempt(plan)
        clock = attempt.steps.get("ptp clock")
        if clock is None:
            self.result.ptp = f"refused ({format_steps(attempt.steps)})"
        elif clock == "none":
            self.result.ptp = "answered without timingPeerInfo.ClockID"
        else:
            self.result.ptp = f"answered with ClockID {clock}"
        if clock is not None:
            self.result.ntp_after_ptp = attempt.established is not None and attempt.established.plan.timing == NTP

    # -- 4. the video ------------------------------------------------------------------------------

    def _trailers(self) -> list[str]:
        """The avcC endings to try: the saved (or default) one, then, if `variants` names it, the other."""
        trailers = [self.start.trailer] + [t for t in TRAILERS if t != self.start.trailer]
        if TRAILER not in self.variants or not self.send_test_pattern:
            return trailers[:1]
        return trailers

    async def _streams(self, plan: Plan, plans: list[Plan]) -> Plan:
        """The video with `plan`; if the TV drops it, with the other avcC ending, then with the
        other ordering. Returns the shape that worked (saved there and then), else `plan`."""
        trailers = self._trailers()
        checks = [(plan, trailer) for trailer in trailers]
        while checks and len(self.result.streams) < MAX_STREAM_CHECKS:
            shape, trailer = checks.pop(0)
            check = StreamCheck(shape, trailer)
            self.result.streams.append(check)  # there already if the time limit stops it
            await (self._stream(check) if self.send_test_pattern else self._hold(check))
            self.say(check.line())
            if check.worked:
                self.result.profile = profiles.MirrorProfile.of(shape, trailer)
                self._save()
                return shape
            report = check.report
            dropped = report is not None and (report.data_closed_after is not None or report.data_channel != "open")
            if not dropped:
                # Not something another avcC ending or ordering changes (the time, the encoder...).
                self.result.error = check.error
                return plan
            if not checks:
                other = await self._other_ordering(shape, plans)
                if other is not None:
                    checks.append((other, trailers[0]))
        last = self.result.streams[-1] if self.result.streams else None
        self.result.error = last.error if last is not None else None
        return plan

    async def _other_ordering(self, plan: Plan, plans: list[Plan]) -> Optional[Plan]:
        """For a TV that drops the video: the other ordering, if `variants` names it, it is
        still to be tried, and the TV takes it (as the TV took it)."""
        other = next((shape for shape in plans if shape.ordering != plan.ordering and shape.audio == plan.audio), None)
        if ORDERING not in self.variants or other is None:
            return None
        plans.remove(other)
        self.say(f"SETUP {other.describe()}")
        attempt = await self._attempt(other)
        if attempt.worked and attempt.established is not None:
            return dataclasses.replace(attempt.established.plan, fallback=True)
        return None

    async def _stream(self, check: StreamCheck) -> None:
        """A whole session of the test pattern, `self.seconds` long."""
        plan, trailer = check.plan, check.trailer
        self._close_udp()  # the session binds its own ports, from the first of the range
        self.say(f"{self.seconds:g} s of the test pattern: {plan.describe()}, avcC ending {trailer}")
        profile = profiles.MirrorProfile(plan.ordering, plan.audio, NTP, trailer, plan.lead_ms)

        def status(state: State, _message: str) -> None:
            if state == State.CASTING:  # its first frame went to the TV: the time to look
                self._pattern_shown()

        # NTP whatever MIRRORBRIDGE_TIMING says: the probe's checks are made for it.
        session = MirrorSession(self.target, self.settings, status, source=TEST_PATTERN, profile=profile,
                                seconds=self.seconds, remember=False, timing=NTP)
        check.report = session.report
        try:
            await asyncio.wait_for(session.run(), self.seconds + STREAM_SETUP_SECONDS)
        except asyncio.TimeoutError:
            check.note = f"no end within {self.seconds + STREAM_SETUP_SECONDS:g} s"
            check.error = message_of(refused(None, session.report.steps, "video"))
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Probe stream failed", exc_info=True)
            check.error = message_of(ex)
        finally:
            check.report = session.report  # as it ended, also when the time limit stopped it
        report = check.report
        if check.error is not None:
            return
        if not report.frames_sent:
            check.note = "no frames went to the TV"
            check.error = Text("mirror_no_frames", "No video went to {device}: FFmpeg made no frame in {seconds} s.",
                               device=self.target.name, seconds=f"{self.seconds:g}")
        elif report.ntp_requests == 0 and report.established is not None and report.established.plan.timing == NTP:
            check.note = "the TV never asked for the time"
            check.error = message_of(no_timing(report.timing_port, report.steps))
        else:
            check.worked = True

    async def _hold(self, check: StreamCheck) -> None:
        """Without the test pattern: set up, open the video connection, hold it while the TV
        asks for the time, tear down."""
        plan = check.plan
        report = check.report = SessionReport(trailer=check.trailer)
        if not self.transports:
            await self._bind_udp()
        self.negotiator.steps = report.steps
        responder = self.responder
        asked = responder.requests if responder is not None else 0
        established = None
        try:
            established = report.established = await asyncio.wait_for(self._establish(plan), SETUP_STEP_SECONDS)
            channel = self.negotiator.channel
            assert channel is not None
            try:
                data = await DataChannel.open(channel.remote_ip, established.data_port)
            except (OSError, asyncio.TimeoutError) as ex:
                report.data_channel = f"refused ({answer_of(ex)})"
                check.note = f"the TV did not take the video connection ({answer_of(ex)})"
                check.error = message_of(data_refused(established.data_port, answer_of(ex), report.steps))
                return
            report.data_channel = "open"
            try:
                await self.negotiator.record(established)
                try:
                    await asyncio.wait_for(data.closed.wait(), DATA_HOLD_SECONDS)
                except asyncio.TimeoutError:
                    check.worked = await self._timing_seen(check, asked)
                else:
                    report.data_channel = data.close_reason or "closed by the TV"
                    report.data_closed_after = 0
                    check.note = f"the TV closed the video connection before any frame ({report.data_channel})"
                    check.error = message_of(data_closed(0, report.steps))
            finally:
                await data.close()
        except MirrorError as ex:
            check.error = message_of(ex)
        except asyncio.TimeoutError:
            check.note = f"no answer within {SETUP_STEP_SECONDS} s"
            check.error = message_of(refused(None, report.steps, "setup"))
        finally:
            if established is not None:
                await self.negotiator.teardown(established.uri)
            self.negotiator.close()

    async def _timing_seen(self, check: StreamCheck, asked: int) -> bool:
        """Whether the TV asked for the time during this check (waiting a little for it)."""
        responder, report = self.responder, check.report
        if (responder is None or report is None or report.established is None
                or report.established.plan.timing != NTP):
            return True
        report.timing_port = responder.port
        waited = 0.0
        while responder.requests == asked and waited < TIMING_WAIT_SECONDS:
            await asyncio.sleep(0.1)
            waited += 0.1
        report.ntp_requests = responder.requests - asked
        if report.ntp_requests:
            return True
        check.note = "the TV never asked for the time"
        check.error = message_of(no_timing(responder.port, report.steps))
        return False


def time_up(seconds: float) -> Text:
    return Text("mirror_probe_time_up", "The screen mirroring test was stopped after {seconds} s, before it found a "
                "way that works.", seconds=f"{seconds:g}")


async def probe_mirroring(
    target: Target,
    settings: Optional[Settings] = None,
    *,
    seconds: float = 10,
    send_test_pattern: bool = True,
    variants: Iterable[str] = ALL_VARIANTS,
    save: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    limit: Optional[float] = None,
    on_pattern: Optional[Callable[[], None]] = None,
) -> ProbeResult:
    """Find out how `target` takes screen mirroring; see the module's docstring.

    `variants` picks what is tried besides the saved (or default) way (ALL_VARIANTS: ORDERING,
    AUDIO, TIMING, TRAILER). Without `send_test_pattern` no video is sent: the video connection
    is only opened and held for a moment. `progress` gets a line per step as it goes, and
    `on_pattern` is called when the test pattern's first frame has gone to the TV: the moment
    to look at it. What worked is saved for the device when `save` (and the target has an
    identifier). With a `limit`, the probe takes at most that many seconds in all: the steps
    for the report only start if they fit, and when the limit runs out the step under way is
    stopped and torn down, and the result says what was found until then.
    """
    started = time.monotonic()
    probe = _Probe(target, settings or Settings.load(), seconds=seconds, send_test_pattern=send_test_pattern,
                   variants=variants, save=save, progress=progress or (lambda line: None),
                   on_pattern=on_pattern or (lambda: None), deadline=None if limit is None else started + limit)
    task = asyncio.ensure_future(probe.run())
    try:
        await asyncio.wait({task}, timeout=limit)
        if task.done():
            result = task.result()
        else:
            _LOGGER.info("Probe: stopped after %g s", limit)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)  # what was under way is torn down
            result = probe.result
            probe.ran_out(limit or 0)
    finally:
        if not task.done():  # the probe itself was cancelled
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        probe.result.seconds = time.monotonic() - started
    _LOGGER.info("Probe: %s", result.conclusion())
    _LOGGER.debug("Probe report:\n%s", "\n".join(result.lines()))
    return result
