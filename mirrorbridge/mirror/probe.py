"""Finding out how a TV takes screen mirroring, in one go, for a bug report or before a session.

probe_mirroring() goes through what a session does, one step at a time, each within a time
limit of its own and each SETUP on a fresh pair-verified connection:

1. whether the TV needs FairPlay (an Apple TV or a Mac: nothing more is tried);
2. pair-verify and GET /info;
3. the SETUP shapes that `variants` names (both orderings, with and without the silent audio
   stream, and PTP timing, to learn whether the TV gives a ClockID), each torn down at once;
4. a few seconds of the test pattern with the shape that worked, through a whole session: the
   video connection, RECORD, /feedback, the TV asking for the time, frames; if the TV drops the
   video connection, again with the other way of ending the avcC record ("trailer"). Without
   the test pattern, the video connection is only held open for a moment, and the TV must
   still ask for the time.

What worked is saved for the device (profiles.py), so the next session asks for it at once.
Whatever happens, every session the probe set up is torn down, also when the probe is given a
time limit in all (Test this TV gives it one) and runs out of it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..airplay_patch import format_steps
from ..config import Settings
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

    def line(self) -> str:
        report = self.report
        head = f"Video ({self.plan.ordering}, avcC ending {self.trailer})"
        if report is None:
            return f"{head}: {self.note}"
        parts = [f"video connection {report.data_channel}"]
        if report.picture:
            parts.append(f"{report.picture[0]}x{report.picture[1]} at {report.fps} fps with {report.encoder}")
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
        verdict = "ok" if self.worked else f"failed: {self.note or self.error}"
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
                 variants: Iterable[str], save: bool, progress: Callable[[str], None]) -> None:
        self.target = target
        self.settings = settings
        self.seconds = seconds
        self.send_test_pattern = send_test_pattern
        self.variants = set(variants)
        self.save = save
        self.progress = progress
        self.start = profiles.load(target.identifier) or profiles.MirrorProfile()
        self.result = ProbeResult(target.name, f"{target.address}:{target.port}", fairplay=target.needs_fairplay,
                                  features=target.features, srcvers=target.srcvers)
        self.negotiator = Negotiator(target, timing_port=0, audio_port=0)
        self.transports: list[asyncio.BaseTransport] = []
        self.responder: Optional[NtpResponder] = None  # the timing port the SETUPs name (without the test pattern)

    def say(self, line: str) -> None:
        _LOGGER.info("Probe: %s", line)
        try:
            self.progress(line)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Probe progress callback failed")

    async def run(self) -> ProbeResult:
        result = self.result
        if result.fairplay:
            result.error = message_of(needs_fairplay(self.target.name))
            return result
        try:
            if not await self._info():
                return result
            working = await self._setups()
            if not working:
                return result
            if self.send_test_pattern:
                self._close_udp()  # the sessions bind their own ports, from the first of the range
            await self._streams(working)
        finally:
            self._close_udp()
            self.negotiator.close()
        if result.profile is not None and self.save and self.target.identifier:
            info = result.info.summary() if result.info else ""
            try:
                result.saved = profiles.save(self.target.identifier, result.profile, name=self.target.name, info=info)
            except OSError as ex:
                _LOGGER.warning("Could not save how to mirror to %s: %s", self.target.name, ex)
        return result

    def _close_udp(self) -> None:
        for transport in self.transports:
            transport.close()
        self.transports.clear()
        self.responder = None

    # -- 2. pair-verify and /info -----------------------------------------------------------------

    async def _info(self) -> bool:
        self.say("pair-verify and GET /info")
        try:
            channel = await asyncio.wait_for(self.negotiator.connect(), INFO_STEP_SECONDS)
            self.result.pair_verify = "ok"
            transport, responder = await bind_udp(NtpResponder, channel.local_ip)
            self.transports.append(transport)
            self.responder = responder
            self.negotiator.timing_port = responder.port
            transport, _ = await bind_udp(asyncio.DatagramProtocol, channel.local_ip, taken=(responder.port,))
            self.transports.append(transport)
            self.negotiator.audio_port = transport.get_extra_info("sockname")[1]
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
        self.negotiator.steps = attempt.steps
        established = None
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
        self.say(attempt.line())
        return attempt

    async def _establish(self, plan: Plan) -> Established:
        await self.negotiator.connect()
        return await self.negotiator.establish(plan)

    async def _setups(self) -> list[Plan]:
        """Try the shapes; returns those that worked, the best first."""
        working: list[Plan] = []
        for plan in self._plans():
            self.say(f"SETUP {plan.describe()}")
            attempt = await self._attempt(plan)
            self.result.setups.append(attempt)
            if attempt.worked and attempt.established is not None:
                used = dataclasses.replace(attempt.established.plan, fallback=True)
                if used not in working:
                    working.append(used)
            elif attempt.steps.get("pair-verify") not in (None, "ok"):
                self.result.error = attempt.error  # the pairing is gone: nothing else will work
                break
        if TIMING in self.variants and working:
            await self._ptp(working[0])
        if not working and self.result.error is None:
            failed = self.result.setups[-1] if self.result.setups else None
            self.result.error = failed.error if failed else None
        return working

    async def _ptp(self, plan: Plan) -> None:
        self.say("SETUP with PTP timing, to see whether the TV gives a ClockID")
        attempt = await self._attempt(dataclasses.replace(plan, timing=PTP, fallback=True))
        self.result.setups.append(attempt)
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

    def _checks(self, working: list[Plan]) -> list[tuple[Plan, str]]:
        trailers = [self.start.trailer] + [t for t in TRAILERS if t != self.start.trailer]
        if TRAILER not in self.variants or not self.send_test_pattern:
            trailers = trailers[:1]
        checks = [(working[0], trailer) for trailer in trailers]
        checks += [(plan, trailers[0]) for plan in working[1:] if plan.ordering != working[0].ordering]
        return checks[:MAX_STREAM_CHECKS]

    async def _streams(self, working: list[Plan]) -> None:
        for plan, trailer in self._checks(working):
            check = await (self._stream(plan, trailer) if self.send_test_pattern else self._hold(plan, trailer))
            self.result.streams.append(check)
            self.say(check.line())
            if check.worked:
                profile = profiles.MirrorProfile.of(plan, trailer)
                self.result.profile = profile
                return
            report = check.report
            dropped = report is not None and (report.data_closed_after is not None or report.data_channel != "open")
            if not dropped:
                # Not something another avcC ending or ordering changes (the time, the encoder...).
                self.result.error = check.error
                return
        last = self.result.streams[-1] if self.result.streams else None
        self.result.error = last.error if last is not None else None

    async def _stream(self, plan: Plan, trailer: str) -> StreamCheck:
        """A whole session of the test pattern, `self.seconds` long."""
        check = StreamCheck(plan, trailer)
        self.say(f"{self.seconds:g} s of the test pattern: {plan.describe()}, avcC ending {trailer}")
        profile = profiles.MirrorProfile(plan.ordering, plan.audio, NTP, trailer, plan.lead_ms)
        session = MirrorSession(self.target, self.settings, source=TEST_PATTERN, profile=profile,
                                seconds=self.seconds, remember=False)
        check.report = session.report
        try:
            await asyncio.wait_for(session.run(), self.seconds + STREAM_SETUP_SECONDS)
        except asyncio.TimeoutError:
            check.note = f"no end within {self.seconds + STREAM_SETUP_SECONDS:g} s"
            check.error = message_of(refused(None, session.report.steps, "video"))
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("Probe stream failed", exc_info=True)
            check.error = message_of(ex)
        report = check.report = session.report  # a new one if the session was set up again
        if check.error is not None:
            return check
        if not report.frames_sent:
            check.note = "no frames went to the TV"
            check.error = Text("mirror_no_frames", "No video went to {device}: FFmpeg made no frame in {seconds} s.",
                               device=self.target.name, seconds=f"{self.seconds:g}")
        elif report.ntp_requests == 0 and report.established is not None and report.established.plan.timing == NTP:
            check.note = "the TV never asked for the time"
            check.error = message_of(no_timing(report.timing_port, report.steps))
        else:
            check.worked = True
        return check

    async def _hold(self, plan: Plan, trailer: str) -> StreamCheck:
        """Without the test pattern: set up, open the video connection, hold it while the TV
        asks for the time, tear down."""
        check = StreamCheck(plan, trailer, report=SessionReport(trailer=trailer))
        report = check.report
        assert report is not None
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
                return check
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
        return check

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
) -> ProbeResult:
    """Find out how `target` takes screen mirroring; see the module's docstring.

    `variants` picks what is tried besides the saved (or default) way (ALL_VARIANTS: ORDERING,
    AUDIO, TIMING, TRAILER). Without `send_test_pattern` no video is sent: the video connection
    is only opened and held for a moment. `progress` gets a line per step as it goes. What
    worked is saved for the device when `save` (and the target has an identifier). With a
    `limit`, the probe takes at most that many seconds in all: then the step under way is
    stopped and torn down, and the result says what was found until then.
    """
    started = time.monotonic()
    probe = _Probe(target, settings or Settings.load(), seconds=seconds, send_test_pattern=send_test_pattern,
                   variants=variants, save=save, progress=progress or (lambda line: None))
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
            if result.profile is None:
                result.error = time_up(limit or 0)
    finally:
        if not task.done():  # the probe itself was cancelled
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        probe.result.seconds = time.monotonic() - started
    _LOGGER.info("Probe: %s", result.conclusion())
    _LOGGER.debug("Probe report:\n%s", "\n".join(result.lines()))
    return result
