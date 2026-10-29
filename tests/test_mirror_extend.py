"""Mirror screen with Extend screen on: the virtual monitor a cast adds (mirrorbridge.extended),
mirrored to a TV in the same process (tests/fake_mirror_tv.py), and the mirror command's options.

The driver and Windows' list of displays are fakes that log, in order, the monitor being added and
removed, each FFmpeg command and each FFmpeg being stopped. FFmpeg itself runs, with its test
source standing in for the screen, so the TV takes real frames; what is checked of each command
is what it would have captured (ddagrab's output of the virtual monitor, or gdigrab's rectangle of
it) and how it fits that into the TV's screen.
"""

import asyncio
import threading

import pytest

from fake_mirror_tv import CREDENTIALS, FakeMirrorTV
from mirrorbridge import extended, ffmpeg, vdisplay
from mirrorbridge.config import Settings
from mirrorbridge.mirror import TEST_PATTERN, MirrorError, MirrorSession
from mirrorbridge.mirror import session as mirror_session
from mirrorbridge.session import State
from mirrorbridge.vdisplay import DriverStatus, VddError

VIRTUAL = r"\\.\DISPLAY9"
MAIN = ffmpeg.Monitor(0, 0, 0, 1920, 1080, True, 0, device=r"\\.\DISPLAY1")
# Extend screen at 4K, while What to cast also names a window and Quality > Resolution says 360p:
# neither applies to the virtual screen, which is sent whole, fitted into the TV's screen.
SETTINGS = Settings(extend_screen=True, extend_mode="4k", encoder="libx264", framerate=30, video_bitrate_kbps=1500,
                    output_height=360, window_title="Notepad", capture_audio=False)


def _ffmpeg() -> None:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264", mirror=True):
        pytest.skip("this FFmpeg has no libx264")


@pytest.fixture(autouse=True)
def app_data(tmp_path, monkeypatch):
    """%APPDATA% (~/.config off Windows) in tmp_path: no saved settings or mirroring.json."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture
async def tv():
    tv = await FakeMirrorTV().start()
    yield tv
    await tv.stop()


class Driver:
    """The virtual display driver and Windows' displays, as a session sees them."""

    def __init__(self) -> None:
        self.events: list = []  # ("start", w, h, hz), ("ffmpeg", backend, device), "stop ffmpeg", "stop screen"
        self.screens: list = []  # every virtual screen object made
        self.commands: list = []  # the FFmpeg commands the session asked for (before the test source stands in)
        self.size = None  # the virtual monitor's, while it is on the desktop
        self.refuse = None  # a DriverStatus start() fails with
        self.ddagrab_fails = False  # FFmpeg with ddagrab ends at once, as when DXGI says no
        self.frames = 0  # FFmpeg ends after this many frames (0: it runs until stopped)

    def monitors(self) -> list:
        """ffmpeg.list_monitors(): the PC's display, and to its right the virtual one while it is there."""
        if self.size is None:
            return [MAIN]
        return [MAIN, ffmpeg.Monitor(1, 1920, 0, *self.size, False, 1, device=VIRTUAL)]

    def screen_class(self) -> type:
        driver = self

        class Screen(vdisplay.VirtualScreen):
            name = "fake"

            def __init__(self, on_lost=None) -> None:
                super().__init__(on_lost)
                driver.screens.append(self)

            @classmethod
            def status(cls) -> DriverStatus:
                return driver.refuse or DriverStatus.OK

            def start(self, width: int, height: int, hz: int) -> str:
                driver.events.append(("start", width, height, hz))
                if driver.refuse is not None:
                    raise VddError(f"driver:{driver.refuse.value}")
                driver.size = (width, height)
                return VIRTUAL

            def stop(self) -> None:
                driver.events.append("stop screen")
                driver.size = None

        return Screen

    def started(self) -> list:
        return [event for event in self.events if isinstance(event, tuple) and event[0] == "start"]

    def ffmpegs(self) -> list:
        return [event[1:] for event in self.events if isinstance(event, tuple) and event[0] == "ffmpeg"]


@pytest.fixture
def driver(monkeypatch):
    d = Driver()
    screen = d.screen_class()
    monkeypatch.setattr(vdisplay, "available", lambda: True)
    monkeypatch.setattr(vdisplay, "backend", lambda name=vdisplay.DEFAULT_BACKEND: screen)
    monkeypatch.setattr(ffmpeg, "list_monitors", d.monitors)
    # As on Windows, where Desktop Duplication reaches the virtual monitor (output 1 of adapter 0).
    monkeypatch.setattr(ffmpeg, "pick_capture_backend", lambda ff, settings, monitor=None, region=None: "ddagrab")
    build = mirror_session.build_command

    def command(ff, settings, *, source, backend="", monitor=None, region=None, **kw):
        d.commands.append(build(ff, settings, source=source, backend=backend, monitor=monitor, region=region, **kw))
        d.events.append(("ffmpeg", backend, monitor.device if monitor else None))
        if backend == "ddagrab" and d.ddagrab_fails:
            return [ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "nullsrc", "-vf", "ddagrab_refused",
                    "-f", "flv", "pipe:1"]
        # FFmpeg's 1080p test source stands in for the screen, fitted into the TV's screen as the screen would be.
        cmd = build(ff, settings, source=source, backend="test", monitor=monitor, region=region, **kw)
        if d.frames:
            cmd = cmd[:-3] + ["-frames:v", str(d.frames)] + cmd[-3:]
        return cmd

    monkeypatch.setattr(mirror_session, "build_command", command)
    stop_ffmpeg = MirrorSession._stop_ffmpeg

    def stop(source) -> None:
        d.events.append("stop ffmpeg")
        stop_ffmpeg(source)

    monkeypatch.setattr(MirrorSession, "_stop_ffmpeg", staticmethod(stop))
    return d


def _option(cmd: list, name: str) -> str:
    return cmd[cmd.index(name) + 1]


def _watch(states: list, casting: asyncio.Event, session_of=lambda: None, devices=None):
    def status(state, message) -> None:
        states.append((state, str(message)))
        if state == State.CASTING:
            if devices is not None:
                devices.append(session_of().virtual_device)
            casting.set()

    return status


# -- the session ------------------------------------------------------------------------------------------


async def test_extend_screen_mirrors_the_virtual_screen_and_removes_it_at_the_end(tv, driver):
    _ffmpeg()
    tv.reconfigure(width=1280, height=720, max_fps=25)
    states, devices, casting = [], [], asyncio.Event()
    session = MirrorSession(tv.target(), SETTINGS, _watch(states, casting, lambda: session, devices), seconds=1)
    await asyncio.wait_for(session.run(), 60)

    assert driver.started() == [("start", 1920, 1080, 60)]  # the Extended screen size, 4K, but 1080p at most
    assert driver.ffmpegs() == [("ddagrab", VIRTUAL)]  # that monitor, not display 1 or the window
    cmd = driver.commands[0]
    assert _option(cmd, "-i").startswith("ddagrab=output_idx=1:framerate=25:")  # the virtual monitor's DXGI output
    # Fitted into the TV's screen; Quality > Resolution (360p) does not apply, as for a cast.
    assert _option(cmd, "-vf").startswith("hwdownload,format=bgra,scale=w='min(1280,iw)':h='min(720,ih)'")
    assert session.report.canvas == session.report.picture == (1280, 720) and tv.videos[0].frames
    order = [state for state, _ in states]
    assert order.index(State.EXTENDING) < order.index(State.CONNECTING) < order.index(State.CASTING)
    assert (State.CASTING, "Mirroring the extended screen to TCL 55QM7L (1280x720, 25 fps)") in states
    assert devices == [VIRTUAL] and session.settings.window_title == ""
    assert session.settings.video_bitrate_kbps == 1500  # not a 4K cast's 20 Mbps: the TV gets 1080p at most
    # Removed once, after the last FFmpeg: Desktop Duplication losing its output would be an error.
    assert driver.events[-2:] == ["stop ffmpeg", "stop screen"] and driver.events.count("stop screen") == 1
    assert len(driver.screens) == 1 and session.virtual_device == ""
    assert states[-1] == (State.STOPPED, "Mirrored for 1 s, as asked") and session.error is None


@pytest.mark.parametrize("chosen, added", [("4k", "1080p"), ("1080p", "1080p"), ("720p", "720p")])
def test_mirror_screen_adds_the_virtual_screen_at_1080p_at_most(chosen, added):
    # Mirroring sends 1080p at most: a 4K screen would only be shrunk again on every frame.
    assert mirror_session.extend_mode(chosen) == added


@pytest.mark.parametrize("ending", ["stop", "tv"])
async def test_the_virtual_screen_goes_when_the_session_is_stopped_or_the_tv_ends_it(tv, driver, ending):
    _ffmpeg()
    tv.reconfigure(width=640, height=360)
    states, casting = [], asyncio.Event()
    session = MirrorSession(tv.target(), SETTINGS, _watch(states, casting), remember=False)
    task = asyncio.ensure_future(session.run())
    await asyncio.wait_for(casting.wait(), 30)
    assert driver.size == (1920, 1080) and session.virtual_device == VIRTUAL
    if ending == "stop":
        await session.stop()
    else:
        await asyncio.sleep(2.5)  # past the second keyframe
        tv.end_mirroring()
    await asyncio.wait_for(task, 30)
    assert session.error is None and states[-1] == (
        State.STOPPED, "Stopped" if ending == "stop" else "The TV ended screen mirroring")
    assert driver.events[-2:] == ["stop ffmpeg", "stop screen"] and driver.events.count("stop screen") == 1
    assert driver.size is None and session.virtual_device == ""


async def test_a_virtual_screen_that_goes_away_ends_the_session_with_its_own_error(tv, driver):
    _ffmpeg()
    tv.reconfigure(width=640, height=360)
    states, casting = [], asyncio.Event()
    session = MirrorSession(tv.target(), SETTINGS, _watch(states, casting), remember=False)
    task = asyncio.ensure_future(session.run())
    await asyncio.wait_for(casting.wait(), 30)
    threading.Thread(target=driver.screens[0].on_lost).start()  # as the driver's keep-alive thread would
    with pytest.raises(VddError) as caught:
        await asyncio.wait_for(task, 30)
    assert caught.value.code == "lost" and session.error is caught.value and session.state == State.FAILED
    assert State.STOPPING not in [state for state, _ in states]  # not taken for a stop
    assert driver.events[-2:] == ["stop ffmpeg", "stop screen"] and tv.teardowns == 1


@pytest.mark.parametrize("lost", [True, False])
async def test_ffmpeg_ending_with_the_virtual_screen_is_the_screens_error(tv, driver, monkeypatch, lost):
    """Desktop Duplication can end FFmpeg before the driver notices the monitor went away (sleep):
    the session waits a moment for the driver before it blames FFmpeg, as a cast does."""
    _ffmpeg()
    monkeypatch.setattr(extended, "LOST_SECONDS", 3 if lost else 0.5)
    tv.reconfigure(width=640, height=360)
    driver.frames = 15
    session = MirrorSession(tv.target(), SETTINGS, remember=False)
    task = asyncio.ensure_future(session.run())
    while not (session._ffmpeg and session._ffmpeg.proc and session._ffmpeg.proc.poll() is not None):  # pylint: disable=protected-access
        assert not task.done()
        await asyncio.sleep(0.02)
    if lost:  # the driver notices a second later
        threading.Timer(1.0, driver.screens[0].on_lost).start()
    with pytest.raises((VddError, MirrorError)) as caught:
        await asyncio.wait_for(task, 30)
    if lost:
        assert isinstance(caught.value, VddError) and caught.value.code == "lost"
    else:
        assert isinstance(caught.value, MirrorError) and caught.value.args[0].key == "encoder_stopped"
    assert driver.events[-1] == "stop screen" and driver.events.count("stop screen") == 1
    assert len(driver.ffmpegs()) == 1  # FFmpeg said nothing of a lost capture: it is not started again


async def test_setting_up_again_for_the_other_avcc_ending_keeps_the_virtual_screen(tv, driver):
    """The windows on the virtual screen stay there: it is not removed and added again between
    the two sessions the TV is asked for."""
    _ffmpeg()
    tv.reconfigure(width=640, height=360, want_trailer=bytes.fromhex("fdf8f800"))
    session = MirrorSession(tv.target(), SETTINGS, seconds=1)
    await asyncio.wait_for(session.run(), 60)
    assert session.error is None and session.report.retried and len(tv.videos) == 2 and tv.teardowns == 2
    assert driver.events == [("start", 1920, 1080, 60), ("ffmpeg", "ddagrab", VIRTUAL), "stop ffmpeg",
                             ("ffmpeg", "ddagrab", VIRTUAL), "stop ffmpeg", "stop screen"]
    assert len(driver.screens) == 1 and session.virtual_device == ""


async def test_gdi_capture_after_ddagrab_takes_the_same_virtual_screen(tv, driver):
    _ffmpeg()
    tv.reconfigure(width=640, height=360)
    driver.ddagrab_fails = True
    states = []
    session = MirrorSession(tv.target(), SETTINGS, lambda s, m: states.append((s, str(m))), remember=False)
    task = asyncio.ensure_future(session.run())

    async def frames_came() -> None:  # from the second FFmpeg, which takes a while to start under Wine
        while not (tv.videos and len(tv.videos[0].frames) >= 5):
            assert not task.done()
            await asyncio.sleep(0.05)

    await asyncio.wait_for(frames_came(), 30)
    await session.stop()
    await asyncio.wait_for(task, 30)
    assert session.error is None and tv.records == 1
    assert driver.events == [("start", 1920, 1080, 60), ("ffmpeg", "ddagrab", VIRTUAL), "stop ffmpeg",
                             ("ffmpeg", "gdigrab", VIRTUAL), "stop ffmpeg", "stop screen"]
    gdi = driver.commands[1]
    # The virtual monitor's rectangle of the desktop, not the window still named in the settings.
    at = gdi.index("gdigrab")
    assert gdi[at:at + 13] == ["gdigrab", "-framerate", "30", "-draw_mouse", "1", "-offset_x", "1920", "-offset_y",
                               "0", "-video_size", "1920x1080", "-i", "desktop"]
    assert _option(gdi, "-vf").startswith("scale=w='min(640,iw)':h='min(360,ih)'")
    starting = [message for state, message in states if state == State.STARTING]
    assert starting[-2:] == ["x264 (CPU) via ddagrab", "x264 (CPU) via gdigrab"]


async def test_a_failed_set_up_removes_the_virtual_screen(tv, driver, monkeypatch):
    _ffmpeg()
    tv.reconfigure(setup_delay=2)
    monkeypatch.setattr(mirror_session, "START_SECONDS", 1)
    session = MirrorSession(tv.target(), SETTINGS)
    with pytest.raises(MirrorError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.args[0].key == "mirror_no_answer" and session.state == State.FAILED
    assert driver.events == [("start", 1920, 1080, 60), "stop screen"] and session.virtual_device == ""


@pytest.mark.parametrize("when", ["setting up", "between the attempts", "setting up again"])
async def test_a_virtual_screen_lost_while_the_tv_is_set_up_is_what_the_session_says(tv, driver, monkeypatch,
                                                                                     when):
    """The PC sleeping while the TV is asked (the first time, or again with the other avcC ending)
    leaves the TV unanswered too: the session ends with the virtual screen's error, not with the
    TV's, on either attempt, and removes the screen once."""
    _ffmpeg()
    monkeypatch.setattr(mirror_session, "START_SECONDS", 5)
    if when == "setting up":
        tv.reconfigure(width=640, height=360, setup_delay=10)  # the SETUP is never answered in time
    else:  # the TV drops the video at once, so the session is set up once more
        tv.reconfigure(width=640, height=360, want_trailer=bytes.fromhex("fdf8f800"))
    connecting = 0

    def lose() -> None:  # on the driver's own thread, as its keep-alive would
        thread = threading.Thread(target=driver.screens[0].on_lost)
        thread.start()
        thread.join()

    def status(state, message) -> None:
        nonlocal connecting
        if state == State.CONNECTING:
            connecting += 1
            if (when, connecting) in (("setting up", 1), ("setting up again", 2)):
                lose()
        elif state == State.STARTING and getattr(message, "key", "") == "mirror_retry_trailer":
            if when == "between the attempts":
                lose()
            else:
                tv.reconfigure(setup_delay=10)  # the second SETUP is never answered in time

    session = MirrorSession(tv.target(), SETTINGS, status)
    with pytest.raises(VddError) as caught:
        await asyncio.wait_for(session.run(), 60)
    assert caught.value.code == "lost" and session.error is caught.value and session.state == State.FAILED
    assert connecting == (2 if when == "setting up again" else 1)
    assert len(tv.videos) == (0 if when == "setting up" else 1)
    assert driver.started() == [("start", 1920, 1080, 60)] and len(driver.screens) == 1
    assert driver.events[-1] == "stop screen" and driver.events.count("stop screen") == 1
    assert session.virtual_device == ""


async def test_the_virtual_screen_is_the_sessions_as_soon_as_the_driver_adds_it(tv, driver, monkeypatch):
    """Windows can list the new monitor before Desktop Duplication shows it to FFmpeg: the tray
    must already know it is the session's, or What to cast would offer it as one more display."""
    _ffmpeg()
    tv.reconfigure(width=640, height=360)
    seen = []

    def monitors() -> list:  # DXGI a moment behind the driver: two lookups without the new monitor
        seen.append(session.virtual_device)
        return [MAIN] if len(seen) <= 2 else driver.monitors()

    monkeypatch.setattr(ffmpeg, "list_monitors", monitors)
    session = MirrorSession(tv.target(), SETTINGS, seconds=1, remember=False)
    await asyncio.wait_for(session.run(), 60)
    assert seen == [VIRTUAL] * 3 and session.error is None
    assert driver.ffmpegs() == [("ddagrab", VIRTUAL)] and session.virtual_device == ""


async def test_stopping_while_the_tv_is_set_up_removes_the_virtual_screen(tv, driver):
    _ffmpeg()
    tv.reconfigure(setup_delay=10)
    states = []
    session = MirrorSession(tv.target(), SETTINGS, lambda s, m: states.append(s))
    task = asyncio.ensure_future(session.run())
    while State.CONNECTING not in states:
        assert not task.done()
        await asyncio.sleep(0.01)
    await session.stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert driver.events == [("start", 1920, 1080, 60), "stop screen"] and states[-1] == State.STOPPED
    assert session.finished.is_set() and session.virtual_device == ""


async def test_a_missing_driver_ends_the_session_before_the_tv_is_asked(tv, driver):
    _ffmpeg()
    driver.refuse = DriverStatus.NOT_INSTALLED
    session = MirrorSession(tv.target(), SETTINGS)
    with pytest.raises(VddError) as caught:
        await asyncio.wait_for(session.run(), 30)
    assert caught.value.status is DriverStatus.NOT_INSTALLED and session.state == State.FAILED
    assert tv.connections == [] and driver.ffmpegs() == [] and driver.events[-1] == "stop screen"


async def test_the_probe_and_the_test_pattern_never_add_a_virtual_screen(tv, driver):
    """Test this TV (and mirror --probe, which runs the same probe_mirroring) mirrors a test
    pattern, as does mirror --test-pattern: neither adds a monitor, whatever What to cast says."""
    from mirrorbridge import probe
    from mirrorbridge.devices import Device
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import Protocol

    _ffmpeg()
    tv.reconfigure(width=640, height=360)
    conf = AppleTV("127.0.0.1", tv.profile.name)
    conf.add_service(ManualService("fake-tv", Protocol.AirPlay, tv.port,
                                   {"features": "0x7F8AD0,0x38BCF46", "srcvers": "377.40.00", "model": "55QM7L",
                                    "manufacturer": "TCL"}, credentials=CREDENTIALS))
    tested = await asyncio.wait_for(probe._mirroring(Device(conf), SETTINGS, 1), 120)  # pylint: disable=protected-access
    assert tested.accepted and tested.error is None
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=1, remember=False)
    await asyncio.wait_for(session.run(), 60)
    assert session.error is None and session.virtual_device == ""
    assert driver.screens == [] and driver.started() == []
    assert {backend for backend, _device in driver.ffmpegs()} == {TEST_PATTERN}


# -- the mirror command ---------------------------------------------------------------------------------


@pytest.mark.parametrize("argv, saved, extend, mode, monitor, window", [
    (["--extend", "--extend-mode", "720p"], {}, True, "720p", 0, ""),
    ([], {"extend_screen": True, "extend_mode": "4k"}, True, "4k", 0, ""),  # the app's choice
    (["--monitor", "2"], {"extend_screen": True}, False, "1080p", 1, ""),  # a display, not the virtual screen
    (["--window", "PowerPoint"], {"extend_screen": True}, False, "1080p", 0, "PowerPoint"),
    (["--extend", "--window", "PowerPoint"], {}, True, "1080p", 0, "PowerPoint"),  # --extend wins, as on cast
], ids=["extend", "saved", "monitor", "window", "extend-and-window"])
async def test_the_mirror_command_chooses_what_to_send_as_cast_does(monkeypatch, argv, saved, extend, mode,
                                                                    monitor, window):
    from mirrorbridge import cli, mirror
    from mirrorbridge.devices import Device
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import PairingRequirement, Protocol

    Settings(**saved).save()
    conf = AppleTV("127.0.0.1", "TCL 55QM7L")
    conf.add_service(ManualService("fake-tv", Protocol.AirPlay, 7000, {"features": "0x7F8AD0,0x38BCF46",
                                                                       "model": "55QM7L", "manufacturer": "TCL"},
                                   credentials=CREDENTIALS, pairing_requirement=PairingRequirement.Mandatory))
    device = Device(conf)
    sessions = []

    class Session:
        def __init__(self, target, settings, on_status, *, source, seconds) -> None:
            sessions.append((target.name, settings, source))

        async def run(self) -> None:
            return None

    class Cast(Session):
        def __init__(self, _manager, device, settings, on_status) -> None:  # pylint: disable=super-init-not-called
            sessions.append((device.name, settings, "cast"))

    async def choose(_manager, args):
        assert args.device == "TCL"
        return device

    async def paired(_manager, _device, force=False):
        return None

    monkeypatch.setattr(mirror, "MirrorSession", Session)
    monkeypatch.setattr(cli, "CastSession", Cast)
    monkeypatch.setattr(cli, "_choose", choose)
    monkeypatch.setattr(cli, "_pair_if_needed", paired)
    assert await cli.cmd_mirror(cli.build_parser().parse_args(["mirror", "-d", "TCL", *argv])) == 0
    ((name, settings, source),) = sessions
    assert name == "TCL 55QM7L" and source == mirror.DESKTOP
    assert (settings.extend_screen, settings.extend_mode, settings.monitor_index, settings.window_title) == (
        extend, mode, monitor, window)
    # The cast command takes the same options the same way.
    assert await cli.cmd_cast(cli.build_parser().parse_args(["cast", "-d", "TCL", *argv])) == 0
    assert sessions[1:] == [("TCL 55QM7L", settings, "cast")]


def test_the_mirror_commands_help_names_extend(capsys):
    from mirrorbridge import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["mirror", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--extend mirror a new virtual screen, so the TV works as a second screen" in out
    assert "--extend-mode {1080p,720p,4k}" in out and "--monitor MONITOR" in out and "--window WINDOW_TITLE" in out
