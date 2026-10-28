"""Command line interface. `mirrorbridge` with no arguments opens the GUI."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from . import APP_NAME, CLI_NAME, __version__, airplay_patch, audio, ffmpeg, mirror, vdisplay, winmonitors
from .config import Settings, app_data_dir, credentials_file
from .devices import Device, DeviceManager
from .session import CastSession, State


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


async def cmd_cast(args) -> int:
    settings = Settings.load()
    for key in ("framerate", "encoder", "capture_backend", "latency_mode", "ffmpeg_path"):
        value = getattr(args, key, None)
        if value not in (None, ""):
            setattr(settings, key, value)
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
            await atv.stream.play_url(args.file)
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


async def cmd_mirror(args) -> int:
    """Screen mirroring (experimental), or with --probe, how the TV takes it."""
    settings = Settings.load()
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

    def status(state: State, message: str) -> None:
        message = _cli_words(message, device)
        print(f"[{state.value}] {message}" if message else f"[{state.value}]")

    source = mirror.TEST_PATTERN if args.test_pattern else mirror.DESKTOP
    session = mirror.MirrorSession(target, settings, status, source=source, seconds=args.seconds)
    print("Press Ctrl+C to stop mirroring.")
    try:
        await session.run()
    except Exception:  # pylint: disable=broad-except
        return 1
    return 0


async def cmd_doctor(args) -> int:
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
        print("The virtual screen (cast --extend) needs Windows 10 or 11.")
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
        return p

    common(sub.add_parser("scan", help="list AirPlay receivers"))
    common(sub.add_parser("doctor", help="check FFmpeg, encoders, audio and network"))
    for name, text in (("pair", "pair with a receiver using the on-screen PIN"), ("forget", "remove saved pairing")):
        common(sub.add_parser(name, help=text)).add_argument("-d", "--device", help="name, IP or id")

    cast = common(sub.add_parser("cast", help="cast the screen (or a file) to a receiver"))
    cast.add_argument("-d", "--device", help="name, IP or id")
    cast.add_argument("--file", help="play a local video file or http(s) URL instead of the screen")
    cast.add_argument("--monitor", type=int, help="display number (1 = first)")
    cast.add_argument("--window", dest="window_title", help="capture a single window by title")
    cast.add_argument("--fps", dest="framerate", type=int)
    cast.add_argument("--bitrate", type=int, help="video kbps")
    cast.add_argument("--height", type=int, help="output height, 0 = native")
    cast.add_argument("--encoder", choices=["auto", *ffmpeg.ENCODER_PREFERENCE])
    cast.add_argument("--capture", dest="capture_backend", choices=["auto", "ddagrab", "gdigrab", "test"])
    cast.add_argument("--latency", dest="latency_mode", choices=list(ffmpeg.LATENCY_SEGMENT_SECONDS))
    cast.add_argument("--no-audio", action="store_true")
    cast.add_argument("--ffmpeg", dest="ffmpeg_path")
    cast.add_argument("--extend", action="store_true",
                      help="cast a new virtual screen, so the TV works as a second screen (with none of "
                           "--extend, --monitor or --window, cast uses the app's 'What to cast' choice)")
    cast.add_argument("--extend-mode", choices=list(vdisplay.EXTEND_MODES),
                      help="size of that screen (default: the app's choice, 1080p at first)")

    mirroring = common(sub.add_parser("mirror", help="mirror the screen with a fraction of a second of delay "
                                                     "(experimental; AirPlay 2 TVs of other brands)"))
    mirroring.add_argument("-d", "--device", help="name, IP or id")
    mirroring.add_argument("--probe", action="store_true",
                           help="find out how the TV takes screen mirroring and remember what works")
    mirroring.add_argument("--no-video", action="store_true", help="with --probe: send no test pattern")
    mirroring.add_argument("--test-pattern", action="store_true", help="mirror a test pattern, not the screen")
    mirroring.add_argument("--seconds", type=float, help="stop after this many seconds (--probe: 10)")

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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("pyatv").setLevel(logging.WARNING)

    handler = {"scan": cmd_scan, "pair": cmd_pair, "forget": cmd_forget, "cast": cmd_cast, "doctor": cmd_doctor,
               "vdd": cmd_vdd, "mirror": cmd_mirror}[args.command]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
