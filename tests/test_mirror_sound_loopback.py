"""The PC's sound in whole screen mirroring sessions with the fake TV (tests/fake_mirror_tv.py),
a TCL 55QM7L by default: the test tone on time, the TV's answers and resend requests, the sound
off and why, the video's restarts and retries, the TV closing the session when the sound starts,
PTP, muting, and the sound failing or stalling while the picture goes on as it would without it."""

import asyncio
import dataclasses
import itertools
import logging
import os
import re
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field

import pytest

from fake_mirror_tv import TCL_CONTROL_FIRST, FakeMirrorTV
from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.mirror import TEST_PATTERN, MirrorError, MirrorSession, probe_mirroring, profiles, ptp, sound
from mirrorbridge.mirror import session as mirror_session
from mirrorbridge.mirror.audiopackets import SPF, SR, ChaChaShk, ms_to_samples
from mirrorbridge.mirror.clock import NS, from_ntp
from mirrorbridge.mirror.h264 import TRAILER_ISO
from mirrorbridge.mirror.negotiation import FEEDBACK_SECONDS, MEDIA_FIRST, NTP, PTP
from mirrorbridge.mirror.sound import SoundKnobs
from mirrorbridge.session import State

SETTINGS = Settings(encoder="libx264", framerate=30, video_bitrate_kbps=1500, output_height=360)
# The desktop path, with FFmpeg's test source standing in for the screen.
DESKTOP_SETTINGS = dataclasses.replace(SETTINGS, capture_backend="test")
M = 0xFFFFFFFF
MS = NS // 1000
FEEDERS = ("mirrorbridge-audio", "mirror-tone")
KNOBS = ("MIRRORBRIDGE_NO_AUDIO", "MIRRORBRIDGE_TIMING", "MIRRORBRIDGE_LEAD_MS", "MIRRORBRIDGE_PTP_LOCK_S",
         "MIRRORBRIDGE_PTP_NO_FALLBACK")


def _ffmpeg() -> str:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264", mirror=True):
        pytest.skip("this FFmpeg has no libx264")
    return ff


def _feeders() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name in FEEDERS and t.is_alive()]


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    """mirroring.json in tmp_path, no sound or experiment knobs from outside, and no sound
    thread left behind."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in [name for name in os.environ if name.startswith(sound.ENV)] + list(KNOBS):
        monkeypatch.delenv(name, raising=False)
    yield
    threads = [t for t in threading.enumerate() if t.name in FEEDERS]
    for thread in threads:
        thread.join(2.0)
    assert not _feeders(), f"the test left sound threads running: {_feeders()}"


@pytest.fixture
async def tv():
    tv = await FakeMirrorTV().start()
    yield tv
    await tv.stop()


@pytest.fixture
def sent(monkeypatch):
    """When each sound datagram left, data and control (time.perf_counter_ns())."""
    times: list[int] = []
    real_sender, real_send = sound.datagram_sender, sound.AudioControl.send

    def sender(transport, addr, limit=sound.SEND_BUFFER_LIMIT):
        send = real_sender(transport, addr, limit)

        def counted(data: bytes) -> bool:
            times.append(time.perf_counter_ns())
            return send(data)

        return counted

    def control_send(self, data, addr):
        times.append(time.perf_counter_ns())
        real_send(self, data, addr)

    monkeypatch.setattr(sound, "datagram_sender", sender)
    monkeypatch.setattr(sound.AudioControl, "send", control_send)
    return times


def _sounds(monkeypatch, **changes) -> list:
    """Each MirrorSound the sessions make, made with `changes` (start_index, make_feeder)."""
    made = []

    class Recorded(sound.MirrorSound):
        def __init__(self, *args, **kw) -> None:
            super().__init__(*args, **{**kw, **changes})
            made.append(self)

    monkeypatch.setattr(mirror_session, "MirrorSound", Recorded)
    return made


class Speakers:
    """MutedSpeakers as a recorder: what was asked of it, and when."""

    made: list = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.muted = False
        self.error = ""
        Speakers.made.append(self)

    def mute(self) -> bool:
        self.calls.append(("mute", time.perf_counter_ns()))
        if self.muted:
            return False
        self.muted = True
        return True

    def restore(self) -> None:
        self.calls.append(("restore", time.perf_counter_ns()))
        self.muted = False

    def asked(self, what: str) -> list[int]:
        return [at for name, at in self.calls if name == what]


@pytest.fixture
def speakers(monkeypatch):
    Speakers.made = []
    monkeypatch.setattr(mirror_session, "MutedSpeakers", Speakers)
    return Speakers.made


@dataclass
class Run:
    """One session with the fake TV, with what it told on the way."""

    session: MirrorSession = None  # type: ignore[assignment]
    states: list = field(default_factory=list)  # (State, message, time.perf_counter_ns())
    notices: list = field(default_factory=list)
    error: BaseException = None  # type: ignore[assignment]

    @property
    def report(self):
        return self.session.report

    @property
    def keys(self) -> list[str]:
        return [text.key for text in self.notices]

    @property
    def last(self) -> tuple:
        return self.states[-1][:2]

    def at(self, state: State) -> int:
        return next(at for s, _, at in self.states if s == state)


def _session(tv, seconds: float = 3, *, settings: Settings = SETTINGS, on_state=None, **kw) -> Run:
    _ffmpeg()
    kw.setdefault("source", TEST_PATTERN)
    kw.setdefault("sound", True)
    run = Run()

    def status(state, message):
        run.states.append((state, str(message), time.perf_counter_ns()))
        if on_state is not None:
            on_state(state)

    run.session = MirrorSession(tv.target(), settings, status, seconds=seconds, on_notice=run.notices.append, **kw)
    return run


async def _mirror(tv, seconds: float = 3, *, fails: bool = False, **kw) -> Run:
    run = _session(tv, seconds, **kw)
    try:
        await asyncio.wait_for(run.session.run(), 60)
    except MirrorError as ex:
        if not fails:
            raise
        run.error = ex
    return run


def _streamed(tv):
    """The one type-96 stream that carried sound; nothing came over the others."""
    streams = [audio for audio in tv.audios if audio.datagrams]
    assert len(streams) == 1, [audio.datagrams for audio in tv.audios]
    (audio,) = streams
    assert audio.errors == [] and audio.packets
    return audio


def _contiguous(audio, first: int = 0) -> list:
    """The packets in order, with none missing from `first` on."""
    numbers = sorted(audio.packets)
    assert numbers == list(range(first, first + len(numbers)))
    return [audio.packets[n] for n in numbers]


def _ahead(video) -> list[int]:
    """How long before its time each frame came (ns)."""
    return [from_ntp(stamp) - arrived for (stamp, _, _), arrived in zip(video.frames, video.arrived)]


def _off_by(heard: int, origin: int) -> float:
    """How far (ms) `heard` is from the nearest whole second after `origin`."""
    off = (heard - origin) % NS
    return min(off, NS - off) / 1e6


# -- the tone on time ---------------------------------------------------------------------------------


async def test_the_test_tone_reaches_the_tv_on_its_line_and_in_time(tv, sent, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    run = await _mirror(tv, 3)
    returned = time.perf_counter_ns()
    report, session = run.report, run.session
    assert run.last == (State.STOPPED, "Mirrored for 3 s, as asked") and run.keys == []
    assert report.sound.startswith("sent: ") and report.steps["sound"] == report.sound
    audio = _streamed(tv)
    stats = report.sound_stats
    gap = ms_to_samples(100)  # the TCL's lead, and no delay

    # One stream from packet 0, in RTP time, no SSRC, the marker on the first only.
    packets = _contiguous(audio)
    assert len(packets) == stats["packets"] >= 250
    assert all(p.rtp == n * SPF & M and p.ssrc == 0 and p.marker == (n == 0) for n, p in enumerate(packets))
    assert not any(p.resent for p in packets) and audio.requests == []

    # A sync to start with, then one a second, all on one line, gap samples before the next packet.
    syncs = audio.syncs
    assert syncs[0].first and syncs[0].field3 == 0 and not any(s.first for s in syncs[1:]) and len(syncs) >= 3
    assert all((s.field3 - s.field1) & M == gap and s.field3 % SPF == 0 and s.flags == 4 for s in syncs)
    assert all(abs((b.arrived_ns - a.arrived_ns) / NS - 1.0) <= 0.15 for a, b in zip(syncs[1:], syncs[2:]))
    first = from_ntp(syncs[0].ntp, True)
    for s in syncs:
        line = from_ntp(s.ntp, True) - first
        assert abs(line - s.field3 * NS / SR) <= NS / SR, "a sync off the line"
        assert 0 <= s.arrived_ns - from_ntp(s.ntp, True) <= 60 * MS

    # Each packet comes before it is heard (rule A), most of the lead before.
    headroom = [audio.heard_ns(p.rtp) - p.arrived_ns for p in packets]
    assert max(headroom) <= 101 * MS and statistics.median(headroom) > 40 * MS

    # The beeps are heard at the whole seconds, the lead after they were made.
    onsets = audio.onsets()
    exact = stats["inserted"] == stats["deleted"] == stats["overflow"] == 0
    assert len(onsets) >= 2
    for onset in onsets:
        assert _off_by(audio.heard_ns(onset), 100 * MS) <= (0.5 if exact else 8), (onset, stats)

    # Held until the first frame went, then sent; nothing once run() is over, no thread left.
    assert stats["held"] > 0 and audio.first_ns >= session._first_sent_ns  # pylint: disable=protected-access
    assert max(sent) < returned and _feeders() == []
    count = len(sent)
    await asyncio.sleep(0.3)
    assert len(sent) == count
    assert "Sound: ALAC to 127.0.0.1:" in caplog.text and "Mirroring sound: sent: " in caplog.text
    assert "sound on (MIRRORBRIDGE_MIRROR_SOUND, mirror --sound)" in caplog.text


@pytest.mark.parametrize("typed", [True, False])
async def test_a_tv_that_takes_the_session_first_answers_the_audio_stream_in_the_second_setup(typed):
    """Control-first: the type-96 stream's answer is in the second SETUP's, with or without its
    "type"."""
    tv = await FakeMirrorTV(dataclasses.replace(TCL_CONTROL_FIRST, audio_answer_type=typed)).start()
    try:
        run = await _mirror(tv, 1.5)
    finally:
        await tv.stop()
    assert tv.orderings == ["control-first"] and run.report.sound.startswith("sent: ")
    audio = _streamed(tv)
    setup = run.report.established.audio
    assert (setup.data_port, setup.control_port) == (audio.data_port, audio.control_port)
    assert ("type" in setup.answer) == typed
    _contiguous(audio)


async def test_packets_the_tv_asks_for_again_are_sent_again(tv):
    tv.reconfigure(audio_lose=(30, 31))
    run = await _mirror(tv, 2)
    audio = _streamed(tv)
    _contiguous(audio)
    assert audio.requests == [(30, 2)] and audio.packets[30].resent and audio.packets[31].resent
    assert audio.missing == [] and audio.pcm()[1] == []
    assert run.report.sound_stats["resent"] == run.report.sound_stats["asked"] == 2


@pytest.mark.parametrize("change, why", [
    ({"audio_ports": False}, "the TV named no port for the sound"),
    ({"audio_control_port": False}, "the TV named no control port for the sound"),
])
async def test_a_tv_that_names_no_port_for_the_sound_gets_the_picture_alone(tv, change, why):
    tv.reconfigure(**change)
    run = await _mirror(tv, 2)
    report = run.report
    assert report.sound == report.steps["sound"] == f"off: {why}"
    assert all(audio.datagrams == 0 for audio in tv.audios) and len(tv.audios) == 1
    assert run.last == (State.STOPPED, "Mirrored for 2 s, as asked") and run.keys == ["mirror_sound_not_taken"]
    assert str(run.notices[0]).startswith("This TV does not take the PC's sound with Mirror screen")
    (video,) = tv.videos
    assert video.errors == [] and len(video.frames) == report.frames_sent >= 40 and report.steps["record"] == 200
    assert report.sound_stats == {} and run.session._sound is None  # pylint: disable=protected-access


# -- the sound off ----------------------------------------------------------------------------------


async def test_without_the_opt_in_the_sound_is_off_and_the_audio_stream_as_it_was(tv, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    run = await _mirror(tv, 1.5, sound=None)
    assert run.report.sound == "off: not turned on yet (mirror --sound, or MIRRORBRIDGE_MIRROR_SOUND=1)"
    assert run.keys == [] and all(audio.datagrams == 0 for audio in tv.audios)
    stream = dict(tv.audios[-1].stream)
    assert len(stream.pop("shk")) == 32 and isinstance(stream.pop("controlPort"), int)
    assert isinstance(stream.pop("streamConnectionID"), int)
    assert stream == {"audioFormat": 0x40000, "audioMode": "default", "ct": 2, "latencyMax": 4410,
                      "latencyMin": 0, "spf": 352, "sr": 44100, "type": 96, "usingScreen": True}
    assert "Sound knobs: MIRRORBRIDGE_MIRROR_SOUND=unset" in caplog.text


@pytest.mark.parametrize("settings, env, why", [
    (dataclasses.replace(SETTINGS, capture_audio=False), None, "Include computer sound is off"),
    (SETTINGS, "0", "MIRRORBRIDGE_MIRROR_SOUND=0"),
])
async def test_the_sound_asked_for_stays_off_when_the_settings_or_the_switch_say_so(tv, monkeypatch, settings,
                                                                                    env, why):
    if env is not None:
        monkeypatch.setenv(sound.ENV, env)
    run = await _mirror(tv, 1, settings=settings)
    assert run.report.sound == f"off: {why}" and all(audio.datagrams == 0 for audio in tv.audios)


async def test_test_this_tv_sends_no_sound(tv, monkeypatch):
    _ffmpeg()
    monkeypatch.setenv(sound.ENV, "1")
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=1), 120)
    assert result.worked and tv.audios and all(audio.datagrams == 0 for audio in tv.audios)
    (check,) = result.streams
    assert check.report.sound == "off: off for this session"


@pytest.mark.parametrize("verdict", [profiles.SOUND_HEARD, profiles.SOUND_OFF])
async def test_test_this_tv_keeps_what_is_known_of_the_sound(tv, verdict):
    """The probe saves the way that worked, and what was known of the sound with it: muting
    (heard) and the sound left out (off) go on as before."""
    _ffmpeg()
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, sound=verdict, sound_note="2026-10-29: as saved"),
                  name="TCL")
    result = await asyncio.wait_for(probe_mirroring(tv.target(), SETTINGS, seconds=1), 120)
    assert result.worked and result.saved and result.profile.sound == verdict
    saved = profiles.load("fake-tv")
    assert (saved.sound, saved.sound_note) == (verdict, "2026-10-29: as saved") and saved.checked


async def test_no_audio_stream_means_no_sound_and_the_saved_profile_keeps_its_audio_stream(tv, monkeypatch):
    monkeypatch.setenv("MIRRORBRIDGE_NO_AUDIO", "1")
    tv.reconfigure(audio_first=False)
    run = await _mirror(tv, 1.5)
    assert run.report.sound == "off: no audio stream in this TV's plan (MIRRORBRIDGE_NO_AUDIO=1)"
    assert tv.audios == [] and run.keys == []
    assert profiles.load("fake-tv").audio is True


async def test_the_environment_turns_the_sound_on_without_forcing_it(tv, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    monkeypatch.setenv(sound.ENV, "1")
    run = await _mirror(tv, 1.5, sound=None)
    assert run.report.sound.startswith("sent: ") and _streamed(tv).packets
    assert run.session._sound_forced is False  # pylint: disable=protected-access
    assert "Sound knobs: MIRRORBRIDGE_MIRROR_SOUND=1" in caplog.text


# -- the video's own ways ---------------------------------------------------------------------------


async def test_the_sound_goes_on_while_ffmpeg_is_started_again_for_keyframes(tv, monkeypatch):
    _ffmpeg()
    monkeypatch.setattr(mirror_session, "catch_up_bytes", lambda kbps, fps: -1)  # every P-frame is dropped
    run = await _mirror(tv, 3, remember=False)
    assert run.report.restarts and run.report.sound.startswith("sent: ")
    audio = _streamed(tv)
    packets = _contiguous(audio)
    span = (packets[-1].arrived_ns - packets[0].arrived_ns) / NS
    assert len(packets) >= 0.9 * span * SR / SPF and span > 2
    # No pause in the sound while FFmpeg started again.
    assert max(b.arrived_ns - a.arrived_ns for a, b in zip(packets, packets[1:])) < 200 * MS


async def test_setting_up_again_for_the_other_avcc_ending_starts_the_sound_afresh(tv):
    """Not waiting for the first frame (MIRRORBRIDGE_MIRROR_SOUND_HOLD=0), so that the first
    session sends sound before the TV drops its video."""
    tv.reconfigure(want_trailer=bytes.fromhex("fdf8f800"))
    run = await _mirror(tv, 1.5, sound_knobs=SoundKnobs(hold=False))
    assert run.session.state == State.STOPPED and run.report.trailer == TRAILER_ISO
    assert "avcC ending 02000000" in run.report.retried and run.keys == []
    streams = [audio for audio in tv.audios if audio.packets]
    assert len(streams) == 2 and streams[0].stream["shk"] != streams[1].stream["shk"]
    for audio in streams:
        assert audio.errors == [] and min(audio.packets) == 0 and audio.syncs[0].field3 == 0
        _contiguous(audio)
    assert run.report.sound.startswith("sent: ")


# -- what goes wrong with the sound stops the sound alone -------------------------------------------


def _picture_went_on(tv, run: Run, seconds: float) -> None:
    """The session ended as asked, with the frames of all its time, the last near the end."""
    assert run.last == (State.STOPPED, f"Mirrored for {seconds:g} s, as asked") and run.session.error is None
    report = run.report
    (video,) = tv.videos
    assert video.errors == [] and len(video.frames) == report.frames_sent >= 0.8 * 30 * seconds
    assert run.at(State.STOPPED) - video.arrived[-1] < 0.5 * NS


def _when_running(made: list, blocks: int):
    """Whether the sound has been sending for about `blocks` blocks (of the feeder's count)."""
    started = []

    def due(feeder) -> bool:
        if not made or made[0].state != sound.RUNNING:
            return False
        if not started:
            started.append(feeder.blocks)
        return feeder.blocks - started[0] >= blocks

    return due


async def test_a_cipher_that_breaks_stops_the_sound_and_the_picture_goes_on(tv, monkeypatch, caplog):
    real, count = ChaChaShk.seal, itertools.count()

    def seal(self, index, header, payload):
        if next(count) >= 50:
            raise RuntimeError("the cipher broke")
        return real(self, index, header, payload)

    monkeypatch.setattr(ChaChaShk, "seal", seal)
    run = await _mirror(tv, 3)
    _picture_went_on(tv, run, 3)
    assert run.report.sound == "stopped: RuntimeError('the cipher broke')"
    assert run.keys == ["mirror_sound_stopped"] and str(run.notices[0]) == "The sound stopped; the picture goes on."
    audio = _streamed(tv)
    assert len(_contiguous(audio)) == 50 and run.report.sound_stats["packets"] == 50
    assert "The sound stopped (RuntimeError('the cipher broke')); the picture goes on" in caplog.text


async def test_a_sound_that_cannot_start_leaves_the_picture_alone(tv, monkeypatch):
    def broken(*_args, **_kw):
        raise OSError("no sender today")

    monkeypatch.setattr(sound, "AudioSender", broken)
    run = await _mirror(tv, 2)
    _picture_went_on(tv, run, 2)
    assert run.report.sound == "stopped: it did not start (OSError('no sender today'))"
    assert run.keys == ["mirror_sound_stopped"] and all(audio.datagrams == 0 for audio in tv.audios)


async def test_a_capture_that_ends_stops_the_sound_and_the_picture_goes_on(tv, monkeypatch):
    class Gone(sound.ToneFeeder):
        def run(self) -> None:
            return  # as a recorder that failed at once

    monkeypatch.setattr(sound, "ToneFeeder", Gone)
    run = await _mirror(tv, 2)
    _picture_went_on(tv, run, 2)
    assert run.report.sound == "stopped: the sound capture ended" and run.keys == ["mirror_sound_stopped"]


async def test_a_capture_that_stalls_for_good_is_kept_going_with_silence(tv, monkeypatch):
    """No block after the first 50 sent: the keeper's zeros go on at the stream's pace, the
    picture as ever, and the session ends on time."""
    stall = _when_running(_sounds(monkeypatch), 50)

    class Stalled(sound.ToneFeeder):
        def __init__(self, on_block, **kw) -> None:
            def block(*args):
                if stall(self):
                    self._stop_event.wait()  # until the sound is stopped
                    raise sound._Halted  # pylint: disable=protected-access
                on_block(*args)

            super().__init__(block, **kw)

    monkeypatch.setattr(sound, "ToneFeeder", Stalled)
    run = await _mirror(tv, 3)
    _picture_went_on(tv, run, 3)
    assert run.report.sound.startswith("sent: ") and run.keys == []
    stats = run.report.sound_stats
    assert stats["kept"] > SR and stats["packets"] >= 250
    packets = _contiguous(_streamed(tv))
    assert max(b.arrived_ns - a.arrived_ns for a, b in zip(packets, packets[1:])) < 150 * MS


async def test_a_tv_that_never_asks_for_what_it_lost_changes_nothing(tv):
    tv.reconfigure(audio_lose=(30,), audio_resend=False)
    run = await _mirror(tv, 2)
    _picture_went_on(tv, run, 2)
    audio = _streamed(tv)
    assert audio.requests == [] and audio.pcm()[1] == [30] and run.report.sound_stats["resent"] == 0
    assert run.report.sound.startswith("sent: ") and run.keys == []


async def test_a_capture_that_pauses_is_kept_going_by_the_keeper(tv, monkeypatch):
    """The tone stops for 1 s (WASAPI sends nothing while nothing plays): the keeper's zeros keep
    the stream at its pace, without a missing packet."""
    paused = []
    pause = _when_running(_sounds(monkeypatch), 60)

    class Pausing(sound.ToneFeeder):
        def __init__(self, on_block, **kw) -> None:
            def block(*args):
                if not paused and pause(self):
                    began = time.perf_counter_ns()
                    if self._stop_event.wait(1.0):
                        raise sound._Halted  # pylint: disable=protected-access
                    paused.append((began, time.perf_counter_ns()))
                    self.blocks += SR // SPF  # the blocks of that second never come
                    return
                on_block(*args)

            super().__init__(block, **kw)

    monkeypatch.setattr(sound, "ToneFeeder", Pausing)
    run = await _mirror(tv, 3)
    assert paused and run.report.sound.startswith("sent: ")
    packets = _contiguous(_streamed(tv))
    (began, ended), = paused
    during = [p for p in packets if began + 200 * MS <= p.arrived_ns <= ended]
    assert len(during) >= 100 * (ended - began - 200 * MS) / NS
    assert run.report.sound_stats["kept"] > 0


# -- the end and the stops --------------------------------------------------------------------------


async def test_the_tv_ending_mirroring_stops_the_sound_before_the_session_says_so(tv, monkeypatch, sent, speakers,
                                                                                 caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    order = []
    real_stop = sound.MirrorSound.stop

    def stop(self):
        order.append("sound stopped")
        real_stop(self)

    monkeypatch.setattr(sound.MirrorSound, "stop", stop)
    casting = asyncio.Event()

    def on_state(state):
        order.append(state)
        if state == State.CASTING:
            casting.set()

    run = _session(tv, None, on_state=on_state, remember=False)
    task = asyncio.ensure_future(run.session.run())
    await asyncio.wait_for(casting.wait(), 30)
    await asyncio.sleep(2.5)
    tv.end_mirroring()
    await asyncio.wait_for(task, 30)
    assert run.last == (State.STOPPED, "The TV ended screen mirroring") and not run.report.retried
    assert order.index("sound stopped") < order.index(State.STOPPED)
    assert run.keys == [] and "The sound stopped" not in caplog.text
    (recorder,) = speakers
    assert recorder.asked("mute") == [] and recorder.asked("restore")
    assert recorder.asked("restore")[0] < run.at(State.STOPPED) and not recorder.muted
    assert max(sent) < run.at(State.STOPPED)
    count = len(sent)
    await asyncio.sleep(0.3)
    assert len(sent) == count
    assert run.report.sound.startswith("sent: ")
    assert "The TV closed the session 2." in caplog.text  # asked for this run: no retry, only a line


async def test_stopping_while_connecting_leaves_no_sound_behind(tv, monkeypatch):
    made = _sounds(monkeypatch)

    def on_state(state):
        if state == State.CONNECTING:
            asyncio.ensure_future(run.session.stop())

    run = _session(tv, 5, on_state=on_state)
    task = asyncio.ensure_future(run.session.run())  # stop() cancels this task, not the test's
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    assert run.last == (State.STOPPED, "Cancelled") and made == [] and _feeders() == []
    assert all(audio.datagrams == 0 for audio in tv.audios)


@pytest.mark.parametrize("started", [False, True])
async def test_stopping_while_the_sound_starts_leaves_nothing_behind(tv, monkeypatch, started):
    """stop() before the sound's socket is open, or once its feeder runs (the session still
    being set up): no thread, no socket, nothing sent."""
    made = _sounds(monkeypatch)
    real_start = sound.MirrorSound.start

    async def start(self):
        if started:
            await real_start(self)
        asyncio.ensure_future(run.session.stop())
        await asyncio.sleep(0)
        await real_start(self)

    monkeypatch.setattr(sound.MirrorSound, "start", start)
    run = _session(tv, 5)
    task = asyncio.ensure_future(run.session.run())  # stop() cancels this task, not the test's
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    assert run.last == (State.STOPPED, "Cancelled") and _feeders() == []
    (made_sound,) = made
    assert made_sound.state == sound.STOPPED and made_sound._transport is None  # pylint: disable=protected-access
    assert made_sound.control.sender is None and (made_sound.feeder is not None) == started
    assert made_sound.control.transport is None or made_sound.control.transport.is_closing()
    assert run.report.sound == "held: the picture never went"
    assert all(audio.datagrams == 0 for audio in tv.audios)


# -- PTP ----------------------------------------------------------------------------------------------


@pytest.fixture
def ptp_tv(tv, monkeypatch):
    monkeypatch.setattr(ptp, "PORTS", (0, 0))
    monkeypatch.setattr(ptp, "PEER_PORTS", tv.ptp_ports())
    yield tv
    assert ptp.PtpNode._instance is None  # pylint: disable=protected-access


async def test_a_kept_ptp_session_has_no_sound_yet(ptp_tv):
    ptp_tv.reconfigure(ptp="slave")
    run = await _mirror(ptp_tv, 1, timing="ptp")
    assert run.report.established.plan.timing == PTP
    assert run.report.sound == "off: no sound with PTP timing yet" and run.keys == []
    assert all(audio.datagrams == 0 for audio in ptp_tv.audios)


async def test_ptp_given_up_for_ntp_has_the_sound_with_the_last_setups_key(ptp_tv, monkeypatch):
    monkeypatch.setenv("MIRRORBRIDGE_PTP_LOCK_S", "1")
    ptp_tv.reconfigure(ptp="silent")
    run = await _mirror(ptp_tv, 1.5, timing="ptp")
    assert run.report.established.plan.timing == NTP and run.report.sound.startswith("sent: ")
    assert len(ptp_tv.audios) >= 2 and _streamed(ptp_tv) is ptp_tv.audios[-1]
    assert run.report.established.audio.cipher.stream_fields()["shk"] == ptp_tv.audios[-1].stream["shk"]


# -- A/B: the picture with the sound and without ------------------------------------------------------


async def test_the_sound_does_not_change_the_pictures_timing(tv):
    runs = []
    for wanted in (None, True):
        tv.videos.clear()
        runs.append((await _mirror(tv, 3, sound=wanted, remember=False), tv.videos[0]))
    (off, off_video), (on, on_video) = runs
    assert off.report.sound.startswith("off: ") and on.report.sound.startswith("sent: ")
    assert abs(statistics.median(_ahead(on_video)) - statistics.median(_ahead(off_video))) < 10 * MS
    # A busy machine may make a frame late or drop one either way.
    for count in ("late", "dropped"):
        assert on.report.latency[count] <= off.report.latency[count] + 2, (count, off.report.latency,
                                                                           on.report.latency)


# -- the wire's edges -------------------------------------------------------------------------------


async def test_the_sequence_number_wraps_on_the_wire_and_resends_cross_it(tv, monkeypatch):
    _sounds(monkeypatch, start_index=65_500)
    tv.reconfigure(audio_lose=(65_534, 65_535, 65_536))
    run = await _mirror(tv, 2)
    audio = _streamed(tv)
    packets = _contiguous(audio, 65_500)
    assert len(packets) > 100 and run.report.sound_stats["seq_wraps"] == 1
    assert all(p.rtp == (65_500 + n) * SPF & M for n, p in enumerate(packets))
    assert [int.from_bytes(p.nonce8, "little") for p in packets] == list(range(65_500, 65_500 + len(packets)))
    assert audio.requests == [(65_534, 3)] and all(audio.packets[n].resent for n in (65_534, 65_535, 65_536))
    assert run.report.sound_stats["resent"] == 3 and audio.missing == []
    assert audio.syncs[0].field3 == 65_500 * SPF & M and packets[0].marker


@pytest.mark.parametrize("settings, env, gap, latencies", [
    (dataclasses.replace(SETTINGS, mirror_lead_ms=300), {}, 13230, (0, 13230)),  # More options > Debug
    (SETTINGS, {"DELAY_MS": "40"}, 6174, (0, 6174)),
    (SETTINGS, {"GAP_MS": "50"}, 2205, (0, 2205)),
    (SETTINGS, {"DELAY_MS": "40", "SHAPE": "minmax"}, 6174, (6174, 6174)),
])
async def test_the_gap_follows_the_lead_and_the_knobs(tv, monkeypatch, settings, env, gap, latencies):
    for name, value in env.items():
        monkeypatch.setenv(f"{sound.ENV}_{name}", value)
    run = await _mirror(tv, 1.5, settings=settings, remember=False)
    audio = _streamed(tv)
    assert all((s.field3 - s.field1) & M == gap for s in audio.syncs) and run.report.established.audio.gap == gap
    assert (audio.stream["latencyMin"], audio.stream["latencyMax"]) == latencies


async def test_the_tvs_audio_latency_and_feedback_are_kept_and_logged(tv, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    body = {"latency": 11025, "streams": [{"type": 96}]}
    tv.reconfigure(record_headers={"Audio-Latency": "11025"}, feedback_body=body)
    run = await _mirror(tv, 2.5)
    assert run.report.audio_latency == "11025" and run.report.feedback_body == body
    line = next(line for line in caplog.text.splitlines() if "Mirroring sound: sent: " in line)
    assert f"Audio-Latency 11025; last /feedback {body}" in line and "ALAC, gap 4410, latencies 0/4410" in line
    assert "RECORD answered 200; Audio-Latency: 11025" in caplog.text


async def test_a_volume_the_tv_does_not_answer_holds_nothing_up(tv, caplog):
    """MIRRORBRIDGE_MIRROR_SOUND_VOLUME (the sound trial's try 2) on a TV that never answers
    SET_PARAMETER: the picture and the sound go all the same, and the request is given up when
    the session ends."""
    caplog.set_level(logging.INFO, "mirrorbridge")
    tv.reconfigure(answer_volume=False)
    run = await _mirror(tv, 2, sound_knobs=SoundKnobs(tone=True, volume_db=-20.0))
    assert run.last == (State.STOPPED, "Mirrored for 2 s, as asked") and run.report.sound.startswith("sent: ")
    ((body, asked),) = tv.parameters
    assert body == b"volume: -20.000000\r\n"
    assert tv.videos[0].arrived[0] - asked < FEEDBACK_SECONDS * NS  # not after the request's time ran out
    assert _streamed(tv).packets and "volume" not in run.report.steps and "SET_PARAMETER volume" not in caplog.text
    assert tv.teardowns == 1


async def test_a_volume_the_tv_answers_is_logged(tv, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    run = await _mirror(tv, 1.5, sound_knobs=SoundKnobs(tone=True, volume_db=-20.0))
    assert run.report.steps["volume"] == 200 and "SET_PARAMETER volume -20 dB: 200" in caplog.text
    assert len(tv.parameters) == 1


# -- muting this PC -----------------------------------------------------------------------------------


def _desktop_sound(monkeypatch) -> list:
    """The desktop's sound from a sink-mode LoopbackFeeder of silence at the stream's pace, with
    Windows' sound output found."""
    from mirrorbridge import audio as system_audio

    def silence(frames: int) -> bytes:
        time.sleep(frames / SR)
        return bytes(8 * frames)

    def feeder(sink):
        return system_audio.LoopbackFeeder(sink=sink, source=silence, samplerate=SR, block_frames=SPF,
                                           name="mirrorbridge-audio")

    monkeypatch.setattr(mirror_session.system_audio, "loopback_available", lambda: (True, "fake speakers"))
    return _sounds(monkeypatch, make_feeder=feeder)


@pytest.mark.parametrize("saved, settings, knobs, muted", [
    ("", DESKTOP_SETTINGS, SoundKnobs(), False),  # not known to play the sound: never muted
    ("heard", DESKTOP_SETTINGS, SoundKnobs(), True),
    ("heard", dataclasses.replace(DESKTOP_SETTINGS, mute_pc_audio=False), SoundKnobs(), False),
    ("heard", DESKTOP_SETTINGS, SoundKnobs(tone=True), False),  # the tone is not the PC's sound
    ("", DESKTOP_SETTINGS, SoundKnobs(mute=True), True),
])
async def test_this_pc_is_muted_only_when_the_tv_is_known_to_play_its_sound(tv, monkeypatch, speakers, saved,
                                                                            settings, knobs, muted):
    made = _desktop_sound(monkeypatch)
    if saved:
        profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, sound=saved))
    run = await _mirror(tv, 1.5, settings=settings, source=mirror_session.DESKTOP, sound_knobs=knobs,
                        remember=False)
    assert run.report.sound.startswith("sent: ") and _streamed(tv).packets
    (recorder,) = speakers
    (made_sound,) = made
    assert made_sound.tone == knobs.tone
    if muted:
        (mute,) = recorder.asked("mute")
        assert mute >= made_sound.first_packet_ns and recorder.asked("restore")[0] > mute
        assert recorder.asked("restore")[0] < run.at(State.STOPPED)
    else:
        assert recorder.asked("mute") == []
    assert not recorder.muted and run.keys == []


async def test_a_new_output_device_is_muted_in_place_of_the_old_one(tv, monkeypatch):
    """The default output changes while the PC is muted (headphones plugged in): the recorder
    follows it, the old one is given back and the new one muted, and that one is given back
    before the session says it stopped."""
    from mirrorbridge import audio as system_audio
    from test_mirror_sound import _FakeSoundcard

    card = _FakeSoundcard(switch_after=10**9, empty_at=10**9, fail_at=10**9, pace=SR)
    monkeypatch.setitem(sys.modules, "soundcard", card)
    monkeypatch.setattr(system_audio, "SPEAKER_CHECK_SECONDS", 0)
    monkeypatch.setattr(mirror_session.system_audio, "loopback_available", lambda: (True, "Speakers A"))
    made = _sounds(monkeypatch)
    endpoints = []  # (what was asked, of which endpoint, when)

    class Endpoints(Speakers):
        """Mutes the default output of the moment, and gives back the one it muted."""

        endpoint = ""

        def mute(self) -> bool:
            if self.muted:
                return False
            self.muted, self.endpoint = True, card.default_speaker().id
            endpoints.append(("mute", self.endpoint, time.perf_counter_ns()))
            if len(endpoints) == 1:
                card.switch_after = card.records + 40  # the new device comes about 0.3 s later
            return True

        def restore(self) -> None:
            if self.muted:
                endpoints.append(("restore", self.endpoint, time.perf_counter_ns()))
            self.muted = False

    monkeypatch.setattr(mirror_session, "MutedSpeakers", Endpoints)
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, sound=profiles.SOUND_HEARD))
    run = await _mirror(tv, 2.5, settings=DESKTOP_SETTINGS, source=mirror_session.DESKTOP, remember=False)
    assert run.report.sound.startswith("sent: ") and run.keys == []
    assert [(what, endpoint) for what, endpoint, _ in endpoints] == [
        ("mute", "{speaker-A}"), ("restore", "{speaker-A}"), ("mute", "{speaker-B}"), ("restore", "{speaker-B}")]
    assert endpoints[0][2] >= made[0].first_packet_ns and endpoints[-1][2] < run.at(State.STOPPED)
    assert [opened for opened, _ in card.opened] == ["{speaker-A}", "{speaker-B}"]
    assert _contiguous(_streamed(tv))  # the sound went on through the change


async def test_a_mute_that_fails_is_told_once(tv, monkeypatch, speakers):
    _desktop_sound(monkeypatch)

    def fails(self):
        self.error = "no endpoint"
        return False

    monkeypatch.setattr(Speakers, "mute", fails)
    run = await _mirror(tv, 1.5, settings=DESKTOP_SETTINGS, source=mirror_session.DESKTOP,
                        sound_knobs=SoundKnobs(mute=True), remember=False)
    assert run.keys == ["mirror_sound_not_muted"] and run.report.sound.startswith("sent: ")


async def test_windows_sound_output_is_looked_for_off_the_loop_and_its_absence_told(tv, monkeypatch):
    asked = []

    def available():
        asked.append(threading.get_ident())
        return False, "system audio capture unavailable: no speakers"

    monkeypatch.setattr(mirror_session.system_audio, "loopback_available", available)
    run = await _mirror(tv, 1, settings=DESKTOP_SETTINGS, source=mirror_session.DESKTOP, remember=False)
    assert asked and threading.get_ident() not in asked
    assert run.report.sound == "off: system audio capture unavailable: no speakers"
    assert run.keys == ["mirror_sound_no_output"] and all(audio.datagrams == 0 for audio in tv.audios)


# -- the TV that closes the session when the sound starts ----------------------------------------------


async def test_a_tv_that_closes_the_session_when_the_sound_starts_is_mirrored_without_it(tv, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    monkeypatch.setenv(sound.ENV, "1")
    tv.reconfigure(close_on_audio=0.3)
    run = await _mirror(tv, 3, sound=None)
    report = run.report
    assert run.last == (State.STOPPED, "Mirrored for 3 s, as asked") and run.session.error is None
    assert report.retried == "the TV closed the session when the sound started"
    assert run.keys == ["mirror_retry_no_sound"]
    assert (State.STARTING, "The TV stopped when the sound started: going on without sound") in [
        state[:2] for state in run.states]
    assert report.sound == "off: going on without sound: the TV closed the session when it started"
    first, second = tv.audios
    assert first.packets and second.datagrams == 0 and len(tv.videos) == 2 and len(tv.videos[1].frames) >= 60
    assert "usingScreen" in second.stream and second.stream["latencyMax"] == 4410  # as it always was
    saved = profiles.load("fake-tv")
    assert saved.sound == profiles.SOUND_OFF and saved.sound_note.endswith(": closed the session when the sound "
                                                                           "started")
    assert "after the first sound packet: setting up again without sound" in caplog.text

    # The next session leaves the sound out; one that asks for it sends it, and goes down with it.
    tv.audios.clear()
    run = await _mirror(tv, 1.5, sound=None)
    assert run.report.sound.startswith("off: this TV closed the session when the sound started (")
    assert all(audio.datagrams == 0 for audio in tv.audios) and not run.report.retried
    tv.audios.clear()
    tv.videos.clear()
    run = await _mirror(tv, 3, fails=True)
    assert tv.audios[-1].packets and len(tv.videos) == 1 and not run.report.retried
    if run.error is not None:
        assert run.error.args[0].key == "mirror_data_closed"
    else:
        assert run.last == (State.STOPPED, "The TV ended screen mirroring")
    assert profiles.load("fake-tv").sound == profiles.SOUND_OFF


async def test_someone_ending_mirroring_on_the_tv_is_not_taken_for_the_sound(tv, monkeypatch, caplog):
    """Back on the TV's remote 2.5 s in (the sound turned on, not asked for this run): the TV
    ends mirroring as it would for the sound, but too late for that to be the likelier; the
    session is not set up again on the TV just left, and the sound is not left out for it."""
    caplog.set_level(logging.INFO, "mirrorbridge")
    monkeypatch.setenv(sound.ENV, "1")
    casting = asyncio.Event()
    run = _session(tv, None, on_state=lambda state: state == State.CASTING and casting.set(), sound=None)
    task = asyncio.ensure_future(run.session.run())
    await asyncio.wait_for(casting.wait(), 30)
    await asyncio.sleep(2.5)
    tv.end_mirroring()
    await asyncio.wait_for(task, 30)
    assert run.last == (State.STOPPED, "The TV ended screen mirroring") and not run.report.retried
    assert len(tv.videos) == 1 and run.keys == [] and run.report.sound.startswith("sent: ")
    assert profiles.load("fake-tv").sound == profiles.SOUND_UNKNOWN
    assert re.search(r"The TV ended mirroring 2\.\d s after the first sound packet: taken for someone ending it",
                     caplog.text)


async def test_a_tv_that_ends_the_session_without_sound_as_soon_is_not_kept_from_the_sound(tv, monkeypatch,
                                                                                          caplog):
    """The TV closed the session when the sound started, and then the one without sound as soon
    (someone pressing Back twice, say): the sound is not what it took against, nothing is kept."""
    caplog.set_level(logging.INFO, "mirrorbridge")
    monkeypatch.setenv(sound.ENV, "1")
    tv.reconfigure(close_on_audio=0.3)
    loop = asyncio.get_running_loop()
    casting = []

    def on_state(state):
        if state == State.CASTING:
            casting.append(state)
            if len(casting) == 2:
                loop.call_later(2.5, tv.end_mirroring)  # later than at once, sooner than SOUND_DROP_SECONDS

    run = await _mirror(tv, 10, sound=None, on_state=on_state)
    assert run.report.retried == "the TV closed the session when the sound started" and len(casting) == 2
    assert run.last == (State.STOPPED, "The TV ended screen mirroring") and run.keys == ["mirror_retry_no_sound"]
    saved = profiles.load("fake-tv")
    assert saved is not None and saved.sound == profiles.SOUND_UNKNOWN
    assert "The TV ended the session without sound as well" in caplog.text


async def test_a_tv_left_without_sound_has_it_again_once_a_run_that_asked_sent_it_long_enough(tv, monkeypatch):
    monkeypatch.setattr(sound, "SOUND_OK_SECONDS", 1)
    profiles.save("fake-tv", profiles.MirrorProfile(MEDIA_FIRST, sound=profiles.SOUND_OFF, sound_note="before"))
    run = await _mirror(tv, 2.5)  # mirror --sound
    assert run.report.sound_stats["packets"] >= SR // SPF and not run.report.retried
    assert profiles.load("fake-tv").sound == profiles.SOUND_UNKNOWN


@dataclass
class SimpleTarget:
    """Enough of a Target for a session that is never run."""

    name: str = "TCL"
    identifier: str = "fake-tv"
    needs_fairplay: bool = False


@pytest.mark.parametrize("forced, packets, retried, blamed, saved, verdict", [
    (True, 60 * 125, False, False, "off", ""),  # 60 s sent by a run that asked: known again
    (True, 60 * 125 - 1, False, False, "off", "off"),
    (False, 60 * 125, False, False, "off", "off"),  # not asked for: it was never sent
    (False, 0, True, True, "", "off"),
    (False, 0, True, False, "", ""),  # closed as soon without the sound
    (False, 10, False, False, "heard", "heard"),
])
def test_the_sound_verdict_kept_for_the_tv(forced, packets, retried, blamed, saved, verdict):
    session = MirrorSession(SimpleTarget(), SETTINGS, sound=True if forced else None,
                            profile=profiles.MirrorProfile(sound=saved, sound_note="before" if saved else ""))
    session.report.sound_stats = {"packets": packets}
    session._sound_retried, session._sound_blamed = retried, blamed  # pylint: disable=protected-access
    got, note = session._sound_verdict()  # pylint: disable=protected-access
    assert got == verdict
    if retried and blamed:
        assert re.fullmatch(r"\d{4}-\d\d-\d\d: closed the session when the sound started", note)
    else:
        assert note == ("before" if verdict and verdict == saved else "")


# -- mirror --sound-trial ------------------------------------------------------------------------------


def _command_line(tv, monkeypatch, answers: list[str]) -> list[str]:
    """The mirror command with the fake TV for its device, and `answers` for its questions, in
    turn. Returns the questions it asked."""
    from fake_mirror_tv import CREDENTIALS
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import Protocol

    from mirrorbridge import cli
    from mirrorbridge.devices import Device

    _ffmpeg()
    SETTINGS.save()
    conf = AppleTV("127.0.0.1", tv.profile.name)
    conf.add_service(ManualService("fake-tv", Protocol.AirPlay, tv.port, {"features": "0x7F8AD0,0x38BCF46",
                                                                          "srcvers": "377.40.00"},
                                   credentials=CREDENTIALS))
    device = Device(conf)
    asked: list[str] = []
    left = list(answers)

    async def choose(_manager, _args):
        return device

    async def prompt(text, secret=False, choices=None):
        asked.append(text)
        return left.pop(0)

    monkeypatch.setattr(cli, "_choose", choose)
    monkeypatch.setattr(cli, "_prompt", prompt)
    return asked


HEARD = "Did you hear one beep a second? [y/n/q] "


@pytest.mark.parametrize("answers, questions, tries, code", [
    (["y"], 1, 1, 0),  # heard the default way: kept for the TV
    (["n", "y"], 2, 2, 0),  # heard with VOLUME=-20: not kept, since a session does not send that
    (["maybe", "q"], 2, 1, 1),  # asked again, then stopped
])
async def test_the_sound_trial_tries_each_way_until_the_tv_beeps(tv, monkeypatch, caplog, capsys, answers,
                                                                 questions, tries, code):
    from mirrorbridge import cli

    caplog.set_level(logging.INFO, "mirrorbridge")
    asked = _command_line(tv, monkeypatch, answers)
    args = cli.build_parser().parse_args(["mirror", "-d", "TCL", "--sound-trial", "--seconds", "1.5"])
    assert await cli.cmd_mirror(args) == code
    assert asked == [HEARD] * questions
    streams = [audio for audio in tv.audios if audio.datagrams]
    assert len(streams) == tries and all(audio.packets and audio.errors == [] for audio in streams)
    assert [(int(number), name) for number, name in re.findall(r"Sound trial (\d+) \(([^;]+); ", caplog.text)] == [
        (1, "default"), (2, "VOLUME=-20")][:tries]
    assert "MIRRORBRIDGE_MIRROR_SOUND_TONE=1" in caplog.text and "; sent: " in caplog.text
    assert ("SET_PARAMETER volume -20 dB" in caplog.text) == (tries == 2)
    out = capsys.readouterr().out
    saved = profiles.load("fake-tv")  # the trial's sessions save nothing of their own
    if answers == ["y"]:
        assert "TCL 55QM7L beeped with try 1 (default)." in out and "Kept for TCL 55QM7L" in out
        assert saved.sound == profiles.SOUND_HEARD and saved.ordering == MEDIA_FIRST
    else:
        assert saved is None
    assert ("Sound trial stopped." in out) == (code == 1)


async def test_the_sound_trial_has_the_tv_turned_down_before_raw_pcm(tv, monkeypatch, caplog):
    from mirrorbridge import cli

    caplog.set_level(logging.INFO, "mirrorbridge")
    monkeypatch.setattr(cli, "SOUND_TRIAL", [("default", {}), ("CODEC=pcm", {"codec": "pcm"}),
                                             ("CODEC=pcmle", {"codec": "pcmle"})])
    asked = _command_line(tv, monkeypatch, ["n", "", "n", "n"])
    args = cli.build_parser().parse_args(["mirror", "-d", "TCL", "--sound-trial", "--seconds", "1"])
    assert await cli.cmd_mirror(args) == 1
    assert asked == [HEARD, "Turn the TV volume down to about 10 now (raw PCM in the wrong byte order is loud "
                            "noise), then press Enter ", HEARD, HEARD]
    first, pcm, pcmle = [audio for audio in tv.audios if audio.datagrams]
    assert (first.stream["ct"], pcm.stream["ct"], pcmle.stream["ct"]) == (2, 1, 1)
    assert pcm.packets and pcm.errors == [] and pcmle.packets and pcmle.errors == []
    assert profiles.load("fake-tv") is None
