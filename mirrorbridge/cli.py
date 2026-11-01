"""Command line interface. `mirrorbridge` with no arguments opens the GUI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import getpass
import logging
import math
import os
import statistics
import sys

from . import (APP_NAME, CLI_NAME, __version__, airplay_patch, audio, ffmpeg, googlecast, mirror, probe, speakers,
               vdisplay, winmonitors)
from .config import Settings, app_data_dir, credentials_file
from .devices import Device, DeviceManager
from .mirror import profiles as mirror_profiles
from .mirror import sound as mirror_sound
from .session import CastSession, State

_LOGGER = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

# mirror --sound-trial: the ways of sending the sound to try on a TV, in this order, each one
# change from the default (as MIRRORBRIDGE_MIRROR_SOUND_* names it, and the knobs it sets).
SOUND_TRIAL = [
    ("default", {}),
    ("VOLUME=-20", {"volume_db": -20.0}),
    ("IS_MEDIA=1", {"is_media": True}),
    ("SHAPE=minmax", {"shape": "minmax"}),
    ("SHAPE=apple", {"shape": "apple"}),
    ("CODEC=alac-sized", {"codec": "alac-sized"}),
    ("CODEC=pcm", {"codec": "pcm"}),
    ("CODEC=pcmle", {"codec": "pcmle"}),
    ("SHAPE=media", {"shape": "media"}),
    ("SYNC_FLAGS=7", {"sync_flags": 7}),
    ("HOLD=0", {"hold": False}),
    ("REDUNDANT=2", {"redundant": 2}),
]
SOUND_TRIAL_SECONDS = 12  # each try, unless --seconds says otherwise
SOUND_CHECK_SECONDS = 10  # doctor --sound records this long
SOUND_RATE_TOLERANCE = 0.01  # and the capture must give 44100 frames a second within this


_QT_APP = None


def _ask_in_dialogs() -> bool:
    """MirrorBridge.exe with its output redirected has no keyboard to read: ask in dialogs."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False)) and sys.stdin is None


def _dialog_prompt(text: str, secret: bool = False, choices: list[str] | None = None) -> str:
    from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit

    global _QT_APP  # pylint: disable=global-statement
    _QT_APP = QApplication.instance() or QApplication([])  # kept alive for later prompts
    if choices:
        value, ok = QInputDialog.getItem(None, APP_NAME, text, choices, 0, False)
        value = str(choices.index(value) + 1) if ok else ""
    else:
        mode = QLineEdit.Password if secret else QLineEdit.Normal
        value, ok = QInputDialog.getText(None, APP_NAME, text, mode)
    if not ok:
        raise KeyboardInterrupt
    return value.strip()


async def _prompt(text: str, secret: bool = False, choices: list[str] | None = None) -> str:
    if _ask_in_dialogs():
        return _dialog_prompt(text, secret, choices)  # Qt has to run on the main thread
    ask = getpass.getpass if secret else input
    return (await asyncio.get_running_loop().run_in_executor(None, ask, text)).strip()


def _cli_words(message: str, device: Device) -> str:
    """A cast's errors name menu items in English; name the options that do the same here."""
    paths = {
        "Device > Forget pairing": f'forget -d "{device.name}"',
        "Device > Pair with PIN": f'pair -d "{device.name}"',
        "Quality > Delay > Smooth": "--latency smooth",
        "Quality > Encoder > x264 (CPU)": "--encoder libx264",
        "What to cast > Extended screen size > 720p": "--extend-mode 720p",
        "More options > Test this TV": f'probe -d "{device.name}"',
        "Show my screen should work": f'mirror -d "{device.name}" should work',
        "Show my screen did not work": f'mirror -d "{device.name}" did not work',
        "Choose Show my screen again": f'Run mirror -d "{device.name}" again',
        "Play a video file": f'cast -d "{device.name}" --file VIDEO',
    }
    for item, option in paths.items():
        message = message.replace(item, option)
    return message


def _print_devices(devices: list[Device], verbose: bool = False) -> None:
    if not devices:
        print("No AirPlay devices found. Try --host <ip> if mDNS is blocked on your network.")
        return
    for i, dev in enumerate(devices, 1):
        print(f"{i:>2}. {dev.name:<28} {dev.address:<16} {dev.model:<22} {dev.status}")
        if verbose:  # what the receiver advertises, for bug reports
            print(f"    {dev.details()}")


async def _choose(manager: DeviceManager, args) -> Device:
    devices = await manager.scan(timeout=args.timeout, hosts=args.host)
    if args.device:
        wanted = args.device.lower()
        for dev in devices:
            if wanted in (dev.name.lower(), dev.address, dev.identifier.lower()):
                return dev
        raise SystemExit(f"Device '{args.device}' not found")
    _print_devices(devices)
    if not devices:
        raise SystemExit(1)
    if len(devices) == 1:
        return devices[0]
    choice = await _prompt(
        "Select a device by number: ", choices=[f"{d.name}  ({d.address}, {d.status})" for d in devices]
    )
    try:
        return devices[int(choice) - 1]
    except (ValueError, IndexError):
        raise SystemExit("Invalid selection") from None


async def _console_pin(device_provides_pin: bool, our_pin: str | None) -> str | None:
    if device_provides_pin:
        return await _prompt("Enter the PIN shown on the TV: ")
    print(f"Enter this PIN on the receiver: {our_pin}")
    if _ask_in_dialogs():
        await _prompt(f"Enter this PIN on the receiver: {our_pin}\nThen press OK.")
    return our_pin


async def _pair_if_needed(manager: DeviceManager, device: Device, force: bool = False) -> None:
    if device.needs_password:
        pwd = await _prompt("AirPlay password: ", secret=True)
        await manager.set_password(device, pwd)
    if force or device.needs_pairing:
        print(f"Pairing with {device.name}. A PIN should appear on the TV...")
        await manager.pair(device, _console_pin)
        print("Paired. Credentials saved to", credentials_file())


async def cmd_scan(args) -> int:
    manager = DeviceManager(asyncio.get_running_loop())
    _print_devices(await manager.scan(timeout=args.timeout, hosts=args.host), args.verbose)
    return 0


async def cmd_pair(args) -> int:
    manager = DeviceManager(asyncio.get_running_loop())
    device = await _choose(manager, args)
    await _pair_if_needed(manager, device, force=True)
    return 0


async def cmd_forget(args) -> int:
    manager = DeviceManager(asyncio.get_running_loop())
    device = await _choose(manager, args)
    await manager.forget(device)
    print(f"Forgot pairing for {device.name}")
    return 0


def _what_to_send(settings: Settings, args) -> None:
    """--monitor, --window, --extend and --extend-mode over the app's What to cast choice, alike
    for cast and mirror: without any of the first three, what the app has selected is sent."""
    if args.window_title is not None:  # --window "" clears a window picked in the app
        settings.window_title = args.window_title.strip()
    if args.monitor is not None:
        settings.monitor_index = args.monitor - 1
        if args.window_title is None:
            settings.window_title = ""  # --monitor means a whole display
    if args.extend:
        settings.extend_screen = True
    elif args.monitor is not None or args.window_title is not None:
        settings.extend_screen = False  # a display or a window, not the virtual screen
    if args.extend_mode:
        settings.extend_mode = args.extend_mode


async def cmd_cast(args) -> int:
    settings = Settings.load()
    for key in ("framerate", "encoder", "capture_backend", "latency_mode", "ffmpeg_path"):
        value = getattr(args, key, None)
        if value not in (None, ""):
            setattr(settings, key, value)
    _what_to_send(settings, args)
    if args.bitrate:
        settings.video_bitrate_kbps = args.bitrate
    if args.height is not None:
        settings.output_height = args.height
    if args.no_audio:
        settings.capture_audio = False

    manager = DeviceManager(asyncio.get_running_loop())
    device = await _choose(manager, args)
    if not device.can_cast_video:
        print(f"{device.name}: {device.status}")
        return 1
    await _pair_if_needed(manager, device)

    if args.file:
        airplay_patch.apply()
        atv = await manager.connect(device)
        try:
            print(f"Playing {args.file} on {device.name} (Ctrl+C to stop)")
            await airplay_patch.play_url(atv, args.file, _file_cast(device))
        finally:
            atv.close()
        return 0

    def status(state: State, message: str) -> None:
        message = _cli_words(message, device)
        print(f"[{state.value}] {message}" if message else f"[{state.value}]")

    session = CastSession(manager, device, settings, status)
    print("Press Ctrl+C to stop casting.")
    try:
        await session.run()
    except Exception:  # pylint: disable=broad-except
        return 1
    return 0


def _file_cast(device: Device) -> airplay_patch.CastContext:
    """What playing a video file (or a URL) on `device` hands the patched pyatv code."""
    return airplay_patch.CastContext(apple=device.is_apple, live=False, password=device.requires_password,
                                     device=device.identifier, features=device.features)


async def cmd_probe(args) -> int:
    args.device = args.device or args.name
    settings = Settings.load()
    manager = DeviceManager(asyncio.get_running_loop())
    device = await _choose(manager, args)
    if not device.can_cast_video:
        print(f"{device.name}: {device.status}")
        return 1
    await _pair_if_needed(manager, device)

    def progress(number: int, total: int, name: str) -> None:
        print(f"Test {number} of {total}: {name}...")

    skipped = probe.mirroring_skipped(device)
    if skipped is None:
        print(f"Testing {device.name}. Watch the TV: it may show Apple's test video, then MirrorBridge's test "
              f"pattern, and during the last test a moving test pattern, mirrored from this PC. If the TV asks "
              f"whether to allow MirrorBridge, choose Allow. This takes about three minutes.")
    else:
        print(f"Testing {device.name}. Watch the TV: it may show Apple's test video, then MirrorBridge's test "
              f"pattern. If the TV asks whether to allow MirrorBridge, choose Allow. This takes about two minutes. "
              f"The fourth test, screen mirroring, is skipped: {_cli_words(skipped, device)}.")
    report = await probe.run(manager, device, settings, seconds=args.seconds, public_url=args.url,
                             progress=progress, watch=print)
    print()
    for line in report.lines():
        print(_cli_words(line, device))
    print()
    print(_cli_words(report.conclusion(), device))
    print(report.where())
    return 0 if report.worked else 1


def _printer(device: Device):
    """What a mirroring session says (on_status), on the command line."""

    def status(state: State, message: str) -> None:
        message = _cli_words(message, device)
        print(f"[{state.value}] {message}" if message else f"[{state.value}]")

    return status


def _noticer(device: Device):
    """What the person should know of the sound (a session's on_notice), on the command line."""
    return lambda text: print(f"[note] {_cli_words(str(text), device)}")


async def cmd_mirror(args) -> int:
    """Screen mirroring (experimental), or with --probe, how the TV takes it."""
    settings = Settings.load()
    _what_to_send(settings, args)
    if args.no_audio:
        settings.capture_audio = False
    if args.keep_pc_sound:
        settings.mute_pc_audio = False
    # This run only, in place of Mirror screen settings > Frame rate: settings.json is not written.
    fps = args.framerate or int(os.environ.get("MIRRORBRIDGE_FPS", "0") or 0)  # MIRRORBRIDGE_FPS: experiment
    manager = DeviceManager(asyncio.get_running_loop())
    device = await _choose(manager, args)
    if device.airplay is None:
        print(f"{device.name}: {device.status}")
        return 1
    await _pair_if_needed(manager, device)
    target = mirror.Target.from_device(device)

    if args.probe:
        print(f"Finding out how {device.name} takes screen mirroring...")
        result = await mirror.probe_mirroring(target, settings, seconds=args.seconds or 10,
                                              send_test_pattern=not args.no_video)
        print()
        for line in result.lines():
            print(_cli_words(line, device))
        return 0 if result.worked else 1

    # The sound, for this run (off unless asked for, for now): the test switches of
    # MIRRORBRIDGE_MIRROR_SOUND_*, with --beep and --sound-delay over them.
    knobs = mirror_sound.SoundKnobs.from_env()
    if args.beep or args.sound_trial:
        knobs = knobs.replace(tone=True)
    if args.sound_delay is not None:
        knobs = knobs.replace(delay_ms=args.sound_delay)
    if args.sound_trial:
        return await _sound_trial(target, settings, device, args, knobs, fps if fps > 0 else None)

    source = mirror.TEST_PATTERN if args.test_pattern else mirror.DESKTOP
    session = mirror.MirrorSession(target, settings, _printer(device), source=source, seconds=args.seconds,
                                   timing=args.timing, fps=fps if fps > 0 else None,
                                   sound=True if args.sound or args.beep else None, sound_knobs=knobs,
                                   on_notice=_noticer(device))
    print("Press Ctrl+C to stop mirroring.")
    try:
        await session.run()
    except Exception:  # pylint: disable=broad-except
        return 1
    return 0


async def _sound_trial(target, settings: Settings, device: Device, args, base: "mirror_sound.SoundKnobs",
                       fps) -> int:
    """mirror --sound-trial: the test pattern with its beep, sent the ways SOUND_TRIAL lists, one
    after the other, until the person hears it on the TV. 0 once they did. Heard the default
    way, it is kept for the TV (mirroring.json), so that the PC is muted while the TV plays the
    sound from then on."""
    seconds = args.seconds or SOUND_TRIAL_SECONDS
    if not args.no_audio:  # Include computer sound off in the app: the trial sends only its beep
        settings = dataclasses.replace(settings, capture_audio=True)
    print(f"Sound trial on {device.name}: up to {len(SOUND_TRIAL)} tries of {seconds:g} s, each with the test "
          f"pattern and a beep every second. Listen to the TV, and after each try say whether it beeped.")
    warned = False
    for number, (name, changes) in enumerate(SOUND_TRIAL, 1):
        knobs = base.replace(**changes)
        if knobs.codec in ("pcm", "pcmle") and not warned:
            warned = True
            await _prompt("Turn the TV volume down to about 10 now (raw PCM in the wrong byte order is loud noise), "
                          "then press Enter ")
        print(f"\nTry {number} of {len(SOUND_TRIAL)}: {name}")
        session = mirror.MirrorSession(target, settings, _printer(device), source=mirror.TEST_PATTERN,
                                       seconds=seconds, remember=False, timing=args.timing, fps=fps, sound=True,
                                       sound_knobs=knobs, on_notice=_noticer(device))
        try:
            await session.run()
        except Exception:  # pylint: disable=broad-except
            pass  # the session said why ([Failed]); some beeps may have played before
        answer = await _heard()
        report = session.report
        stats = report.sound_stats
        _LOGGER.info("Sound trial %d (%s; %s): %s; %s; resends %d of %d asked (%d gone)", number, name,
                     knobs.describe(), answer, report.sound, stats.get("resent", 0), stats.get("asked", 0),
                     stats.get("missed", 0))
        if answer == "q":
            print("Sound trial stopped.")
            return 1
        if answer == "y":
            print(f"{device.name} beeped with try {number} ({name}).")
            if number == 1:
                _keep_heard(target, session, knobs)
            return 0
    print(f"{device.name} did not beep with any of them. Please send the log (--log) with a bug report.")
    return 1


async def _heard() -> str:
    """The person's answer after a try of the sound trial: "y", "n" or "q" (stop)."""
    if _ask_in_dialogs():
        answer = await _prompt("Did you hear one beep a second?", choices=["Yes", "No", "Stop the trial"])
        return {"1": "y", "2": "n"}.get(answer, "q")
    while True:
        answer = (await _prompt("Did you hear one beep a second? [y/n/q] ")).lower()
        if answer in ("y", "yes", "s", "si", "sí"):  # and in Spanish
            return "y"
        if answer in ("n", "no"):
            return "n"
        if answer in ("q", "quit"):
            return "q"


def _keep_heard(target, session, knobs: "mirror_sound.SoundKnobs") -> None:
    """The TV played the default way's sound: mirroring.json keeps that it does (and, for a TV with
    nothing saved yet, how this session mirrored to it). Not when MIRRORBRIDGE_MIRROR_SOUND_* test
    switches changed what the TV got: what is kept must be what a session sends."""
    if knobs.replace(enable=None, tone=False, delay_ms=0.0, mute=False) != mirror_sound.SoundKnobs():
        print("Not kept for the TV: MIRRORBRIDGE_MIRROR_SOUND_* test switches were set.")
        return
    est = session.report.established
    profile = None
    if est is not None:  # with the TV's own lead, as a session saves it
        plan = dataclasses.replace(est.plan, lead_ms=session.profile.lead_ms)
        profile = mirror_profiles.MirrorProfile.of(plan, session.report.trailer)
    try:
        kept = mirror_profiles.set_sound(target.identifier, mirror_profiles.SOUND_HEARD, profile=profile,
                                         name=target.name)
    except OSError as ex:
        print(f"Could not keep that for the TV: {ex}")
        return
    if kept is not None:
        print(f"Kept for {target.name}: it plays the PC's sound. From now on mirroring with the sound mutes this PC "
              f"while the TV plays it (--keep-pc-sound: not).")


async def cmd_googlecast(args) -> int:
    """Google Cast screen mirroring (experimental), or with --list, the Cast receivers."""
    if args.list or not args.device:
        devices = await googlecast.scan(args.timeout, args.host)
        if not devices:
            print("No Google Cast receivers found. Name one by its IP address: googlecast -d 192.168.1.33")
            return 1
        for device in devices:
            print(f"  {device.describe()}{'' if device.has_video else '  (no screen)'}")
        if args.list:
            return 0
        print('\nChoose one with -d "name" or -d IP.')
        return 1
    settings = Settings.load()
    _what_to_send(settings, args)
    if args.bitrate:
        settings.video_bitrate_kbps = args.bitrate
    if args.framerate:
        settings.framerate = args.framerate
    if args.encoder:
        settings.encoder = args.encoder
    if args.keep_pc_sound:
        settings.mute_pc_audio = False
    device = await googlecast.find(args.device, args.timeout)
    if device is None:
        print(f"No Google Cast receiver called {args.device!r}. Try googlecast --list, or its IP address.")
        return 1

    def status(state: State, message: str) -> None:
        print(f"[{state.value}] {message}" if message else f"[{state.value}]")

    source = mirror.TEST_PATTERN if args.test_pattern else mirror.DESKTOP
    session = googlecast.GoogleCastMirror(device, settings, status, source=source, seconds=args.seconds,
                                          delay_ms=args.delay, codec=args.codec, audio=not args.no_audio)
    print(f"Mirroring to {device.describe()}. Press Ctrl+C to stop.")
    try:
        await session.run()
    except Exception:  # pylint: disable=broad-except
        return 1
    return 0


async def cmd_doctor(args) -> int:
    if args.sound:
        return await _sound_check()
    settings = Settings.load()
    print(f"{APP_NAME} {__version__}  (data folder: {app_data_dir()})")
    try:
        ff = ffmpeg.find_ffmpeg(settings.ffmpeg_path)
    except ffmpeg.FFmpegNotFound as ex:
        print("FFmpeg:   MISSING -", ex)
        return 1
    print("FFmpeg:  ", ff)
    print("          ", ffmpeg.ffmpeg_version(ff))
    print("ddagrab:  ", "yes" if ffmpeg.has_filter(ff, "ddagrab") else "no (falls back to gdigrab)")
    print("gdigrab:  ", "yes" if ffmpeg.has_input_format(ff, "gdigrab") else "no")
    encs = ffmpeg.available_encoders(ff)
    print("Encoders: ", ", ".join(f"{e} ({ffmpeg.ENCODER_LABELS[e]})" for e in encs) or "NONE")
    ok, info = audio.loopback_available()
    print("Audio:    ", info if ok else f"NO - {info}")
    for mon in ffmpeg.list_monitors():
        print("Display:  ", mon.label)
    print("Extend:   ", _vdd_summary())
    manager = DeviceManager(asyncio.get_running_loop())
    print("\nScanning network...")
    _print_devices(await manager.scan(timeout=args.timeout, hosts=args.host), args.verbose)
    return 0


async def _sound_check() -> int:
    """doctor --sound: this PC's side of Mirror screen's sound, without a TV. SOUND_CHECK_SECONDS
    of what it plays, recorded as a session records it (WASAPI loopback at 44.1 kHz, blocks of
    one packet), and what the session's sample line would make of it. 0 when the blocks came at
    44100 frames a second (within SOUND_RATE_TOLERANCE)."""
    print(f"{APP_NAME} {__version__}: the sound check for Mirror screen")
    await _prompt("Play some sound on this PC (music or a video), then press Enter ")
    blocks: list[tuple[int, int, bool]] = []  # (frames, when it came, fresh)
    peak = [0.0]

    def sink(arr, arrived: int, fresh: bool) -> None:
        blocks.append((len(arr), arrived, fresh))
        if len(arr):
            peak[0] = max(peak[0], float(abs(arr).max()))

    feeder = audio.LoopbackFeeder(sink=sink, samplerate=mirror_sound.SR, block_frames=mirror_sound.SPF,
                                  buffer_frames=mirror_sound.CAPTURE_BUFFER_FRAMES, name="mirrorbridge-audio")
    print(f"Recording this PC's sound for {SOUND_CHECK_SECONDS:g} s...")
    feeder.start()
    await asyncio.sleep(SOUND_CHECK_SECONDS)
    feeder.stop()
    await asyncio.get_running_loop().run_in_executor(None, feeder.join, 2.0)
    got = list(blocks)
    figures = _line_figures(got)
    rate = figures["rate"]
    worked = bool(got) and abs(rate - mirror_sound.SR) <= mirror_sound.SR * SOUND_RATE_TOLERANCE
    level = f"peak {20 * math.log10(peak[0]):.1f} dBFS" if peak[0] > 0 else "silence (did anything play?)"
    lines = [f"Speaker:   {feeder.speaker_name or 'none'}"
             + (f" ({figures['opened']} recorders opened: the output changed)" if figures["opened"] > 1 else "")]
    if not got:
        lines.append(f"Blocks:    none{f' ({feeder.error})' if feeder.error else ''}: Windows has no sound output "
                     f"to record")
    else:
        lines += [
            f"Blocks:    {len(got)}, {figures['frames']} frames, {rate:.0f} frames/s ({mirror_sound.SR} asked)",
            f"Arrival:   {figures['late_p50']:.1f} ms late (median), {figures['late_p99']:.1f} ms (99%), against "
            f"the earliest of the last 2 s",
            f"Line:      error {figures['error_min']:+.1f}..{figures['error_max']:+.1f} ms, corrections "
            f"+{figures['inserted']}/-{figures['deleted']}, filled {figures['filled_ms']:.0f} ms, kept "
            f"{figures['kept_ms']:.0f} ms, cut {figures['cut_ms']:.0f} ms, base steps {figures['steps']}, re-anchored "
            f"{figures['reanchors']}",
            f"Level:     {level}",
        ]
    lines.append(f"Mute:      {await _mute_round_trip()}")
    lines.append(f"Result:    {'OK' if worked else 'NOT OK: the sound is not recorded at 44100 frames a second'}")
    for line in lines:
        print(line)
    _LOGGER.info("Sound check: %s", "; ".join(" ".join(line.split()) for line in lines))
    return 0 if worked else 1


def _line_figures(blocks: list[tuple[int, int, bool]]) -> dict:
    """What a session's sample line (mirror.sound.SampleLine) makes of recorded blocks (frames,
    when each came in ns, fresh), each sent as it came, with the keeper looking at each block's
    time: the frame rate the recording really gave (fitted to the blocks' times), how late the
    blocks came against the line's base, and the corrections it would make."""
    line = mirror_sound.SampleLine()
    figures = dict(rate=0.0, frames=0, opened=0, late_p50=0.0, late_p99=0.0, error_min=0.0, error_max=0.0,
                   inserted=0, deleted=0, filled_ms=0.0, kept_ms=0.0, cut_ms=0.0, steps=0, reanchors=0)
    late, errors, times, counts = [], [], [], []
    filled = kept = cut = 0
    for n, arrived, fresh in blocks:
        figures["opened"] += int(fresh)
        figures["frames"] += n
        times.append(arrived / mirror_sound.NS)
        counts.append(figures["frames"])
        c0 = line.observe(n, arrived, fresh)
        late.append((arrived - line.t_cap(line.c)) / 1e6)
        if line.A is None:
            line.start(c0)
        else:
            zeros = line.keep(arrived)
            line.advance(zeros)
            kept += zeros
        corr = line.correction(c0, n)
        made = len(mirror_sound.apply(bytes(4 * n), corr)) // 4
        net = made - (n - min(corr.cut, n)) - corr.fill
        figures["inserted"] += max(0, net)
        figures["deleted"] += max(0, -net)
        filled += corr.fill
        cut += min(corr.cut, n)
        line.advance(made)
        if made:
            line.fed_real()
        errors.append(line.error_ns / 1e6)
    if len(times) > 1 and times[-1] > times[0]:
        figures["rate"] = statistics.linear_regression(times, counts).slope
    if late:
        ordered = sorted(late)
        figures["late_p50"] = ordered[(len(ordered) - 1) // 2]
        figures["late_p99"] = ordered[int(0.99 * (len(ordered) - 1))]
    if errors:
        figures["error_min"], figures["error_max"] = min(errors), max(errors)
    ms = 1000 / mirror_sound.SR
    figures.update(filled_ms=filled * ms, kept_ms=kept * ms, cut_ms=cut * ms, steps=line.steps,
                   reanchors=line.reanchors)
    return figures


async def _mute_round_trip() -> str:
    """Mute this PC's speakers as Mirror screen would, and turn them on again half a second later."""
    muted = speakers.MutedSpeakers()
    if muted.mute():
        await asyncio.sleep(0.5)
        muted.restore()
        return "muted, and on again 0.5 s later"
    if muted.error:
        return f"could not mute: {muted.error}"
    return "left as they were: muted already" if sys.platform == "win32" else "only on Windows"


_VDD_STATUS_TEXT = {
    vdisplay.DriverStatus.OK: "working",
    vdisplay.DriverStatus.NOT_INSTALLED: f"driver not installed ('{CLI_NAME} vdd install')",
    vdisplay.DriverStatus.DISABLED: "driver disabled in Device Manager (Display adapters > "
                                    "Parsec Virtual Display Adapter > Enable)",
    vdisplay.DriverStatus.RESTART_REQUIRED: "driver installed; restart Windows to finish",
    vdisplay.DriverStatus.DRIVER_ERROR: f"driver not responding (reinstall: '{CLI_NAME} vdd install')",
    vdisplay.DriverStatus.UNKNOWN_PROBLEM: f"driver has a problem (see Device Manager, or '{CLI_NAME} vdd install')",
    vdisplay.DriverStatus.INACCESSIBLE: "cannot check the driver",
}


def _vdd_summary(status: vdisplay.DriverStatus | None = None) -> str:
    if not vdisplay.available():
        return "needs Windows"
    status = status or vdisplay.backend().status()
    if status is not vdisplay.DriverStatus.OK:
        return _VDD_STATUS_TEXT[status]
    return f"Parsec Virtual Display Driver {vdisplay.backend().version() or '(unknown version)'}, working"


async def cmd_vdd(args) -> int:
    if not vdisplay.available():
        print("The virtual screen (cast --extend, mirror --extend) needs Windows 10 or 11.")
        return 1
    if args.vdd_command == "install":
        return await _vdd_install(args)
    status = vdisplay.backend().status()
    print("Extend:   ", _vdd_summary(status))
    dxgi = winmonitors.dxgi_map()
    for src in vdisplay.vdd_sources():
        where = dxgi.get(src.device_name)
        print(f"Screen:    {src.device_name} (UID {src.uid}"
              f"{', on the desktop' if src.attached_to_desktop else ', not on the desktop'}"
              f"{f', DXGI adapter {where[0]} output {where[1]}' if where else ''})")
    return 0 if status is vdisplay.DriverStatus.OK else 1


_WINGET_HINT = f"Install it with winget instead:  winget install -e --id {vdisplay.VDD_WINGET_ID}"


async def _vdd_install(args) -> int:
    status = vdisplay.backend().status()
    if status is vdisplay.DriverStatus.OK:
        print("The virtual display driver is already installed and working.")
        return 0
    if status is vdisplay.DriverStatus.RESTART_REQUIRED:
        print("The virtual display driver is installed. Restart Windows to finish installing it.")
        return 0
    about = (
        "MirrorBridge's Extend screen uses the free \"Parsec Virtual Display Driver\" from Parsec (Unity).\n"
        "It is downloaded from parsec.app (about 500 KB) and Windows asks for permission once.\n"
        f"By installing it you accept Parsec's terms: {vdisplay.PARSEC_TERMS_URL}"
    )
    print(about)
    if not args.yes:
        if _ask_in_dialogs():
            answer = await _prompt(f"{about}\n\nInstall it?", choices=["Install", "Not now"])
        else:
            answer = await _prompt("Install it? [y/N] ")
        if answer.lower() not in ("1", "y", "yes"):
            return 1

    def progress(done: int, total: int) -> None:
        print(f"\rDownloading... {done // 1024} KB" + (f" of {total // 1024} KB" if total else ""), end="", flush=True)

    try:
        installer = vdisplay.download_installer(progress=progress)
    except (vdisplay.VddError, OSError) as ex:
        print(f"\n{ex}\n{_WINGET_HINT}")
        if getattr(ex, "code", "") != "mismatch":  # a file that did not match is not offered again
            print(f"or download it yourself: {vdisplay.VDD_INSTALLER_URL}")
        return 1
    print(f"\nVerified {installer}. Windows will ask for permission...")
    try:
        started = vdisplay.run_installer_elevated(installer)
    except (vdisplay.VddError, OSError) as ex:
        print(f"{ex}\n{_WINGET_HINT}")
        return 1
    if not started:
        print("Not installed: the Windows permission was cancelled.")
        return 1
    print("Waiting for the driver to start...")
    status = vdisplay.wait_until_ready()
    print("Extend:   ", "ready" if status is vdisplay.DriverStatus.OK else _VDD_STATUS_TEXT[status])
    return 0 if status in (vdisplay.DriverStatus.OK, vdisplay.DriverStatus.RESTART_REQUIRED) else 1


def _sound_delay(text: str) -> float:
    value = float(text)
    if not -500 <= value <= 500:  # NaN too
        raise argparse.ArgumentTypeError(f"{text} is not from -500 to 500 ms")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CLI_NAME,
                                     description="Cast your Windows screen to Apple TV and other AirPlay TVs.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--host", action="append", help="receiver IP to scan directly (repeatable)")
        p.add_argument("--timeout", type=float, default=5, help="scan time in seconds")
        # Also accepted after the command ("scan -v"); SUPPRESS keeps a -v given before it.
        p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="debug logging, and what each receiver advertises")
        p.add_argument("--log", metavar="PATH", help="also write the debug log to this file (added to it), for a "
                                                     "bug report")
        return p

    def what_to_send(p, command: str) -> None:
        p.add_argument("--monitor", type=int, help="display number (1 = first)")
        p.add_argument("--window", dest="window_title", help="capture a single window by title")
        p.add_argument("--extend", action="store_true",
                       help=f"{command} a new virtual screen, so the TV works as a second screen (with none of "
                            f"--extend, --monitor or --window, {command} uses the app's 'What to cast' choice)")
        p.add_argument("--extend-mode", choices=list(vdisplay.EXTEND_MODES),
                       help="size of that screen (default: the app's choice, 1080p at first)")

    common(sub.add_parser("scan", help="list AirPlay receivers"))
    doctor = common(sub.add_parser("doctor", help="check FFmpeg, encoders, audio and network"))
    doctor.add_argument("--sound", action="store_true",
                        help=f"check only this PC's sound for Mirror screen: record {SOUND_CHECK_SECONDS} s of what "
                             f"it plays, as a session would, and mute its speakers for a moment")
    for name, text in (("pair", "pair with a receiver using the on-screen PIN"), ("forget", "remove saved pairing")):
        common(sub.add_parser(name, help=text)).add_argument("-d", "--device", help="name, IP or id")

    cast = common(sub.add_parser("cast", help="cast the screen (or a file) to a receiver"))
    cast.add_argument("-d", "--device", help="name, IP or id")
    cast.add_argument("--file", help="play a local video file or http(s) URL instead of the screen")
    what_to_send(cast, "cast")
    cast.add_argument("--fps", dest="framerate", type=int)
    cast.add_argument("--bitrate", type=int, help="video kbps")
    cast.add_argument("--height", type=int, help="output height, 0 = native")
    cast.add_argument("--encoder", choices=["auto", *ffmpeg.ENCODER_PREFERENCE])
    cast.add_argument("--capture", dest="capture_backend", choices=["auto", "ddagrab", "gdigrab", "test"])
    cast.add_argument("--latency", dest="latency_mode", choices=list(ffmpeg.LATENCY_SEGMENT_SECONDS))
    cast.add_argument("--no-audio", action="store_true")
    cast.add_argument("--ffmpeg", dest="ffmpeg_path")

    test = common(sub.add_parser("probe", help="test which ways of sending video a receiver takes, for a bug report"))
    test.add_argument("name", nargs="?", metavar="device", help="name, IP or id")
    test.add_argument("-d", "--device", help="name, IP or id")
    test.add_argument("--seconds", type=float, default=probe.DEFAULT_SECONDS,
                      help="how long each test watches the TV once it took the video (default "
                           f"{probe.DEFAULT_SECONDS}; the screen mirroring test, {probe.MIRROR_SECONDS} at most)")
    test.add_argument("--url", default=probe.PUBLIC_STREAM, help="the public HLS video of the first two tests")

    mirroring = common(sub.add_parser("mirror", help="mirror the screen with a fraction of a second of delay "
                                                     "(experimental; AirPlay 2 TVs of other brands)"))
    mirroring.add_argument("-d", "--device", help="name, IP or id")
    what_to_send(mirroring, "mirror")
    mirroring.add_argument("--probe", action="store_true",
                           help="find out how the TV takes screen mirroring and remember what works")
    mirroring.add_argument("--no-video", action="store_true", help="with --probe: send no test pattern")
    mirroring.add_argument("--test-pattern", action="store_true",
                           help="mirror a test pattern, not the screen (and no virtual screen)")
    mirroring.add_argument("--seconds", type=float, help="stop after this many seconds (--probe: 10)")
    mirroring.add_argument("--fps", dest="framerate", type=int,
                           help="frame rate for this run only (default: the app's choice)")
    mirroring.add_argument("--timing", choices=["ntp", "ptp"],
                           help="how the TV gets the time, for this run only (default: MIRRORBRIDGE_TIMING, else "
                                "NTP). ptp is an experiment: MirrorBridge as the TV's PTP clock, which needs inbound "
                                "UDP 319-320; NTP if the TV does not follow it")
    # The sound (experimental, off unless asked for), for this run only.
    mirroring.add_argument("--sound", action="store_true",
                           help="send this PC's sound with the picture (experimental; MIRRORBRIDGE_MIRROR_SOUND=1 "
                                "turns it on for every session)")
    mirroring.add_argument("--beep", action="store_true",
                           help="send a beep every second instead of this PC's sound (implies --sound)")
    mirroring.add_argument("--sound-delay", type=_sound_delay, metavar="MS",
                           help="the sound this many ms later (or earlier, below 0) than the picture, -500 to 500")
    mirroring.add_argument("--no-audio", action="store_true", help="no sound, as Include computer sound off")
    mirroring.add_argument("--keep-pc-sound", action="store_true",
                           help="do not mute this PC's speakers while the TV plays the sound")
    mirroring.add_argument("--sound-trial", action="store_true",
                           help="the test pattern with a beep, sent up to 12 ways (--seconds each, 12 by default) "
                                "until you hear it on the TV; heard the first way, the TV is known to play the sound")

    gcast = common(sub.add_parser("googlecast", help="mirror the screen to a Chromecast or a TV with Chromecast "
                                                     "built-in (experimental)"))
    gcast.add_argument("-d", "--device", help="name, id or IP address")
    gcast.add_argument("--list", action="store_true", help="list the Google Cast receivers on the network")
    what_to_send(gcast, "mirror")
    gcast.add_argument("--test-pattern", action="store_true", help="mirror a test pattern, not the screen")
    gcast.add_argument("--seconds", type=float, help="stop after this many seconds")
    gcast.add_argument("--delay", type=int, help="ms the TV shows each frame after it was taken "
                                                 f"(default {googlecast.DEFAULT_DELAY_MS})")
    gcast.add_argument("--codec", choices=["h264", "vp8"], help="video codec (default vp8)")
    gcast.add_argument("--no-audio", action="store_true", help="the picture alone (the test pattern beeps)")
    gcast.add_argument("--keep-pc-sound", action="store_true", help="do not mute this PC's speakers while the "
                                                                    "TV plays the sound")
    gcast.add_argument("--fps", dest="framerate", type=int)
    gcast.add_argument("--bitrate", type=int, help="video kbps")
    gcast.add_argument("--encoder", choices=["auto", *ffmpeg.ENCODER_PREFERENCE])

    vdd = sub.add_parser("vdd", help="the virtual display driver that --extend uses")
    vdd_sub = vdd.add_subparsers(dest="vdd_command", required=True, metavar="{status,install}")
    vdd_sub.add_parser("status", help="show whether the driver is installed and working")
    install = vdd_sub.add_parser("install", help="download Parsec's driver and install it (one UAC prompt)")
    install.add_argument("-y", "--yes", action="store_true", help="accept Parsec's terms without asking")

    gui = sub.add_parser("gui", help="start the tray app (default)")
    gui.add_argument("--open", action="store_true", help="also open the main window")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "gui"):
        from .gui.app import run_gui

        if sys.stderr is not None:  # the windowed exe has no console
            logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                                format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
        return run_gui(open_window=getattr(args, "open", False))

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT,
                        datefmt=LOG_DATE_FORMAT)
    if not args.verbose:
        logging.getLogger("pyatv").setLevel(logging.WARNING)

    handler = {"scan": cmd_scan, "pair": cmd_pair, "forget": cmd_forget, "cast": cmd_cast, "doctor": cmd_doctor,
               "probe": cmd_probe, "vdd": cmd_vdd, "mirror": cmd_mirror,
               "googlecast": cmd_googlecast}[args.command]
    try:
        with _log_file(getattr(args, "log", None), args.verbose):
            _LOGGER.debug("%s %s: %s", APP_NAME, __version__, " ".join(sys.argv[1:] if argv is None else argv))
            return asyncio.run(handler(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


@contextlib.contextmanager
def _log_file(path, verbose: bool):
    """--log PATH: every line of the log, down to DEBUG, also goes to PATH (added to what it has),
    while the console shows what it shows without it."""
    if not path:
        yield
        return
    try:
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as ex:
        raise SystemExit(f"Cannot write the log to {path}: {ex}") from ex
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root = logging.getLogger()
    # Put back afterwards: the root's level (DEBUG for the file), its handlers' (the console's
    # stays the root's of before), and without -v these at INFO, as in the app's own log, where
    # their DEBUG lines would bury the session's.
    noisy = [] if verbose else [logging.getLogger(name) for name in ("zeroconf", "aiohttp", "asyncio")]
    saved = [(item, item.level) for item in (root, *root.handlers, *noisy)]
    for other in root.handlers:
        other.setLevel(max(other.level, root.level))
    for logger in noisy:
        logger.setLevel(max(logger.level, logging.INFO))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
        for item, level in saved:
            item.setLevel(level)
