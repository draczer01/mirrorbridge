"""Locate FFmpeg, probe hardware encoders and build the capture -> HLS command line."""

from __future__ import annotations

import functools
import logging
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import CHOICES, Settings
from .text import Text

_LOGGER = logging.getLogger(__name__)

# Preferred order: GPU encoders first (NVIDIA, Intel, AMD, Windows Media Foundation),
# CPU x264 last. The first one that actually encodes a test frame wins.
ENCODER_PREFERENCE = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "libx264"]

ENCODER_LABELS = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_qsv": "Intel Quick Sync",
    "h264_amf": "AMD AMF",
    "h264_mf": "Windows Media Foundation",
    "libx264": "x264 (CPU)",
}

# Segment length (seconds) per latency mode. Apple TV buffers ~3 segments before
# it starts playing, so this is the main latency knob.
LATENCY_SEGMENT_SECONDS = {"low": 0.5, "balanced": 1.0, "smooth": 2.0}

# AirPlay TVs from other brands (LG, Samsung, TCL...) play the stream with their own
# HLS players, which are not known to cope with sub-second segments or a 3-second
# playlist. They get what Apple's HLS rules for AirPlay 2 TVs ask for: segments of at
# least 2 s, a playlist of at least 12 s whose segments stay on disk a while after
# they leave it, PROGRAM-DATE-TIME, and a master playlist that declares CODECS,
# BANDWIDTH and RESOLUTION (FFmpeg's own), plus FRAME-RATE and AVERAGE-BANDWIDTH
# (added by hls_server, since FFmpeg does not write them while the stream is live).
# Apple TVs keep the stream they have always been sent.
OTHER_TV_MIN_SEGMENT_SECONDS = 2.0
OTHER_TV_WINDOW_SECONDS = 12
MEDIA_PLAYLIST = "stream.m3u8"
MASTER_PLAYLIST = "master.m3u8"


def segment_seconds(settings: Settings, for_apple: bool = True) -> float:
    seg = LATENCY_SEGMENT_SECONDS.get(settings.latency_mode, 1.0)
    return seg if for_apple else max(seg, OTHER_TV_MIN_SEGMENT_SECONDS)


def playlist_size(settings: Settings, for_apple: bool = True) -> int:
    """How many segments the live playlist lists."""
    if for_apple:
        return 6
    return max(6, math.ceil(OTHER_TV_WINDOW_SECONDS / segment_seconds(settings, for_apple)))


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


class FFmpegNotFound(RuntimeError):
    """Raised when no usable ffmpeg binary can be found."""


def _windows_ffmpeg_candidates(exe: str) -> list[Path]:
    """Places winget, Chocolatey and Scoop put ffmpeg.exe.

    An app started before FFmpeg was installed keeps the old PATH (that is why
    'winget install Gyan.FFmpeg' can say "already installed" while MirrorBridge still
    cannot see it), so these folders are searched directly.
    """
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    found: list[Path] = [local / "Microsoft" / "WinGet" / "Links" / exe]
    packages = local / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        found += sorted(packages.glob(f"*FFmpeg*/*/bin/{exe}"), reverse=True)
    program_data = Path(os.environ.get("ProgramData", "C:/ProgramData"))
    found += [
        program_data / "chocolatey" / "bin" / exe,
        Path.home() / "scoop" / "shims" / exe,
        Path("C:/ffmpeg/bin") / exe,
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg" / "bin" / exe,
    ]
    return found


def _registry_path_dirs() -> list[Path]:
    """PATH as currently stored in the registry (fresher than our inherited one)."""
    try:
        import winreg  # type: ignore
    except ImportError:
        return []
    dirs: list[Path] = []
    for hive, key in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
        except OSError:
            continue
        dirs += [Path(os.path.expandvars(p)) for p in str(value).split(";") if p.strip()]
    return dirs


def bundled_dir() -> Path:
    """Folder holding files shipped with the app (PyInstaller bundle or install dir)."""
    return Path(getattr(sys, "_MEIPASS", Path(sys.argv[0]).resolve().parent))


def find_ffmpeg(configured: str = "") -> str:
    """Find ffmpeg: setting, bundled copy, PATH, package managers, then imageio-ffmpeg."""
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    base = bundled_dir()
    candidates: list[Path] = [Path(configured)] if configured else []
    candidates += [base / "ffmpeg" / exe, base / exe, base / "ffmpeg" / "bin" / exe]
    for path in candidates:
        if path.is_file():
            return str(path)
    found = shutil.which("ffmpeg")
    if found:
        return found
    if sys.platform == "win32":
        for path in _windows_ffmpeg_candidates(exe) + [d / exe for d in _registry_path_dirs()]:
            if path.is_file():
                return str(path)
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pylint: disable=broad-except
        pass
    raise FFmpegNotFound(Text(
        "ffmpeg_not_found", "FFmpeg was not found. Use the MirrorBridge.exe build (FFmpeg included), run "
        "'pip install imageio-ffmpeg', or put ffmpeg.exe next to MirrorBridge."
    ))


def _run(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
        check=False,
    )


@functools.lru_cache(maxsize=None)
def ffmpeg_version(ffmpeg: str) -> str:
    out = _run([ffmpeg, "-hide_banner", "-version"]).stdout
    return out.splitlines()[0] if out else "unknown"


@functools.lru_cache(maxsize=None)
def has_filter(ffmpeg: str, name: str) -> bool:
    out = _run([ffmpeg, "-hide_banner", "-filters"]).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


@functools.lru_cache(maxsize=None)
def has_input_format(ffmpeg: str, name: str) -> bool:
    out = _run([ffmpeg, "-hide_banner", "-devices"]).stdout
    return any(name in line.split()[1:2] for line in out.splitlines())


_WORKING_ENCODERS: set[tuple[str, str, bool, int | None]] = set()
_WARNED: set[tuple[str, str, str]] = set()  # (ffmpeg, encoder, why): each one is a warning once a run
# What FFmpeg says when the NVIDIA driver is older than its NVENC needs (570 for FFmpeg 8.1.1).
_DRIVER_LINE = re.compile(r"minimum required Nvidia driver|required nvenc API version", re.IGNORECASE)


def encoder_works(ffmpeg: str, encoder: str, mirror: bool = False, keyframes: int = 2) -> bool:
    """Encode a few real frames; listing in `-encoders` does not mean the GPU exists.

    Only successes are cached: a GPU encoder that is busy (OBS, ShadowPlay) or slow to
    start right after logon gets probed again on the next cast. mirror=True tries the
    options of screen mirroring (mirror_encoder_args, with a keyframe every `keyframes`
    seconds, MIRROR_KEYFRAME_SECONDS unless told, 0 for the first only) instead of those of a
    cast. A failure is a warning in the log (once a run for each reason), with FFmpeg's line
    about the NVIDIA driver if it wrote one: with a driver too old for this FFmpeg, NVENC is
    lost without another word.
    """
    key = (ffmpeg, encoder, mirror, keyframes if mirror else None)
    if key in _WORKING_ENCODERS:
        return True
    # Same options as a real cast, so an option the driver rejects shows up here.
    options = (mirror_encoder_args(encoder, 8000, 30, keyframe_seconds=keyframes) if mirror
               else _encoder_args(encoder, Settings(), gop=30))
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
        "-frames:v", "5", "-pix_fmt", "nv12" if encoder != "libx264" else "yuv420p",
        *options, "-f", "null", "-",
    ]
    try:
        result = _run(args, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as ex:
        _unusable(ffmpeg, encoder, f"FFmpeg did not answer: {ex}")
        return False
    if result.returncode != 0:
        stderr = result.stderr.strip()
        tail = stderr[-300:]
        driver = [line.strip() for line in stderr.splitlines() if _DRIVER_LINE.search(line) and line not in tail]
        _unusable(ffmpeg, encoder, "\n".join(driver + [tail]).strip() or f"exit code {result.returncode}")
        return False
    _WORKING_ENCODERS.add(key)
    return True


def _unusable(ffmpeg: str, encoder: str, why: str) -> None:
    key = (ffmpeg, encoder, why)
    log = _LOGGER.debug if key in _WARNED else _LOGGER.warning
    _WARNED.add(key)
    log("Encoder %s does not work here: %s", ENCODER_LABELS.get(encoder, encoder), why)


def available_encoders(ffmpeg: str) -> list[str]:
    return [enc for enc in ENCODER_PREFERENCE if encoder_works(ffmpeg, enc)]


def pick_encoder(ffmpeg: str, wanted: str, mirror: bool = False, keyframes: int = 2) -> str:
    """`wanted` if it works, else the first of ENCODER_PREFERENCE that does (mirror and
    keyframes as for encoder_works)."""
    if wanted != "auto":
        if encoder_works(ffmpeg, wanted, mirror, keyframes):
            return wanted
        _LOGGER.warning("Encoder %s does not work on this machine, auto-selecting", wanted)
    for enc in ENCODER_PREFERENCE:
        if encoder_works(ffmpeg, enc, mirror, keyframes):
            return enc
    raise RuntimeError(Text("no_encoder", "No working H.264 encoder found in this FFmpeg build"))


def pick_capture_backend(
    ffmpeg: str, settings: Settings, monitor: "Monitor | None" = None, region: "Region | None" = None
) -> str:
    """ddagrab (Desktop Duplication, GPU) when it can see the display, else gdigrab."""
    wanted = settings.capture_backend
    if wanted in ("gdigrab", "test"):
        return wanted
    if sys.platform != "win32":
        return "test" if wanted == "auto" else wanted
    unreachable = monitor is not None and monitor.dxgi_output is None
    if settings.window_title and region is None:
        reason = "this window can only be captured with gdigrab"
    elif unreachable:
        reason = "this display is wired to a GPU that ddagrab cannot reach"
    elif not has_filter(ffmpeg, "ddagrab"):
        reason = "this FFmpeg has no ddagrab"
    else:
        return "ddagrab"
    if wanted == "ddagrab":
        _LOGGER.warning("Using gdigrab instead of ddagrab: %s", reason)
    elif unreachable:
        _LOGGER.info("Using gdigrab for %s: %s (DXGI adapter %s)", monitor.device or monitor.label,
                     reason, monitor.dxgi_adapter)
    return "gdigrab"


@dataclass
class Region:
    """A rectangle of the desktop in physical pixels (always even-sized)."""

    left: int
    top: int
    width: int
    height: int


@dataclass
class Monitor:
    index: int
    left: int
    top: int
    width: int
    height: int
    primary: bool = False
    dxgi_output: int | None = 0  # ddagrab output_idx; None = use gdigrab
    device: str = ""  # \\.\DISPLAYn on Windows
    dxgi_adapter: int | None = 0  # GPU that draws it; ddagrab reaches only adapter 0

    @property
    def label(self) -> str:
        tag = " (primary)" if self.primary else ""
        return f"Display {self.index + 1}: {self.width}x{self.height}{tag}"


def list_monitors() -> list[Monitor]:
    """Displays with their physical resolution, primary first."""
    if sys.platform == "win32":
        try:
            from .winmonitors import list_displays

            displays = list_displays()
            if displays:
                # `-f lavfi -i ddagrab` always opens the default adapter, so a display drawn
                # by another GPU gets no output index and is captured with gdigrab.
                return [
                    Monitor(i, d.left, d.top, d.width, d.height, d.primary,
                            d.dxgi_output if d.dxgi_adapter == 0 else None,
                            device=d.device, dxgi_adapter=d.dxgi_adapter)
                    for i, d in enumerate(displays)
                ]
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Display enumeration failed", exc_info=True)
    try:
        import mss  # type: ignore

        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            mons = sct.monitors[1:]
    except Exception:  # pylint: disable=broad-except
        return [Monitor(0, 0, 0, 1920, 1080, True)]
    return [
        Monitor(i, m["left"], m["top"], m["width"], m["height"], m["left"] == 0 and m["top"] == 0, i)
        for i, m in enumerate(mons)
    ]


def list_windows() -> list[str]:
    """Titles of windows a person would want to cast (Windows only), sorted."""
    if sys.platform != "win32":
        return []
    from . import winmonitors

    titles = {w.title for w in winmonitors.list_windows()}
    return sorted(titles, key=str.lower)


def window_region(title: str, monitors: list[Monitor]) -> tuple[Monitor, Region]:
    """Where the window is on screen: the display holding most of it, and its rectangle
    clipped to that display, in desktop coordinates with even width and height."""
    from . import winmonitors

    rect = winmonitors.window_rect(title) if sys.platform == "win32" else None
    if rect is None:
        raise RuntimeError(Text("window_hidden", 'The window "{title}" is not on screen. Restore it if it is '
                                "minimized, or pick another one.", title=title))
    left, top, width, height = rect
    right, bottom = left + width, top + height

    def overlap(m: Monitor) -> int:
        w = min(right, m.left + m.width) - max(left, m.left)
        h = min(bottom, m.top + m.height) - max(top, m.top)
        return max(0, w) * max(0, h)

    best = max(monitors, key=overlap, default=None)
    if best is None or overlap(best) == 0:
        raise RuntimeError(Text("window_off_screen", 'The window "{title}" is off screen.', title=title))
    x0, y0 = max(left, best.left), max(top, best.top)
    x1, y1 = min(right, best.left + best.width), min(bottom, best.top + best.height)
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if w < 64 or h < 64:
        raise RuntimeError(Text("window_too_small", 'The window "{title}" is too small to cast.', title=title))
    return best, Region(x0, y0, w, h)


def _video_input(
    backend: str, settings: Settings, monitor: Monitor | None, region: Region | None = None
) -> tuple[list[str], list[str]]:
    """Return (input args, filters that must run first) for the chosen capture backend."""
    fps = settings.framerate
    cursor = "1" if settings.show_cursor else "0"
    if backend == "ddagrab":
        idx = monitor.dxgi_output if monitor and monitor.dxgi_output is not None else 0
        src = f"ddagrab=output_idx={idx}:framerate={fps}:draw_mouse={cursor}"
        if region and monitor:  # a window: crop its rectangle out of that display
            src += (f":offset_x={region.left - monitor.left}:offset_y={region.top - monitor.top}"
                    f":video_size={region.width}x{region.height}")
        # ddagrab yields GPU (D3D11) frames; bring them to system memory for scaling.
        return ["-f", "lavfi", "-i", src], ["hwdownload", "format=bgra"]
    if backend == "gdigrab":
        args = ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", cursor]
        area = region or monitor
        if region is None and settings.window_title:
            return args + ["-i", f"title={settings.window_title}"], []
        if area:
            args += [
                "-offset_x", str(area.left), "-offset_y", str(area.top),
                "-video_size", f"{area.width}x{area.height}",
            ]
        return args + ["-i", "desktop"], []
    if backend == "test":
        return ["-re", "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate={fps}"], []
    raise ValueError(f"Unknown capture backend: {backend}")


def _encoder_args(encoder: str, settings: Settings, gop: int) -> list[str]:
    kbps = settings.video_bitrate_kbps
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps // 2}k"]
    common = ["-g", str(gop), "-bf", "0"]
    if encoder == "h264_nvenc":
        specific = ["-preset", "p4", "-tune", "ll", "-rc", "cbr", "-profile:v", "high", "-zerolatency", "1"]
    elif encoder == "h264_qsv":
        specific = ["-preset", "veryfast", "-look_ahead", "0", "-profile:v", "high"]
    elif encoder == "h264_amf":
        specific = ["-usage", "lowlatency", "-quality", "speed", "-rc", "cbr", "-profile:v", "high"]
    elif encoder == "h264_mf":
        specific = ["-rate_control", "cbr", "-scenario", "display_remoting", "-hw_encoding", "1"]
    else:
        specific = ["-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "high", "-x264-params", f"keyint={gop}:scenecut=0"]
    return ["-c:v", encoder] + specific + rate + common


# Screen mirroring (mirrorbridge.mirror) shows each frame about 100 ms after it was captured, so
# the encoder may hold none back: no B-frames, no lookahead, no frames in flight, and CBR with a
# VBV of about one frame (which also keeps a keyframe from arriving as one big burst).
#
# Through a VBV that small a keyframe comes out far worse than the P-frames before it (q 39
# against q 7 on dense text at 8 Mbit/s and 60 fps, on Humberto's PC), and the picture takes a
# second or two to sharpen again: a keyframe every 2 s was a pulse on text every 2 s. So by
# default (MIRROR_KEYFRAMES_DEFAULT, keyframe_seconds 0) FFmpeg makes a keyframe only when it
# starts, as an iPhone does, and the session starts FFmpeg again when it needs another one
# (frames it dropped cost every frame up to the next keyframe). NVENC and x264 then make no
# other; Quick Sync, AMF and Media Foundation, not tried that way on real hardware, still make
# one every UNTESTED_KEYFRAME_SECONDS. The Debug menu (MIRROR_KEYFRAME_CHOICES) also has a
# keyframe every 10 s and every 2 s, to compare. A caller that does not say gets one every
# MIRROR_KEYFRAME_SECONDS, as before (Google Cast mirroring, the encoder probe).
MIRROR_KEYFRAME_SECONDS = 2
MIRROR_KEYFRAMES_DEFAULT = 0
MIRROR_KEYFRAME_CHOICES = CHOICES["mirror_keyframes"]  # 0 (only when needed), 10, 2
UNTESTED_KEYFRAME_SECONDS = 10
NO_PERIODIC_ENCODERS = ("h264_nvenc", "libx264")
NO_PERIODIC_GOP = 2**31 - 1  # -g: in effect never
MIRROR_VBV_FRAMES = 1.0


def mirror_keyframe_seconds(seconds: object) -> int:
    """The Debug menu's keyframe setting as a session takes it: `seconds` if it is one of
    MIRROR_KEYFRAME_CHOICES, else MIRROR_KEYFRAMES_DEFAULT (a settings.json edited by hand)."""
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds in MIRROR_KEYFRAME_CHOICES:
        return int(seconds)
    return MIRROR_KEYFRAMES_DEFAULT


def mirror_keyframe_interval(encoder: str, keyframe_seconds: int = MIRROR_KEYFRAME_SECONDS) -> int:
    """Seconds between the keyframes `encoder` makes when asked for one every keyframe_seconds
    (0: none after the first, but see UNTESTED_KEYFRAME_SECONDS); anything but a whole number of
    seconds is MIRROR_KEYFRAME_SECONDS."""
    if not isinstance(keyframe_seconds, int) or isinstance(keyframe_seconds, bool) or keyframe_seconds < 0:
        return MIRROR_KEYFRAME_SECONDS
    if keyframe_seconds == 0 and encoder not in NO_PERIODIC_ENCODERS:
        return UNTESTED_KEYFRAME_SECONDS
    return keyframe_seconds


def mirror_encoder_args(encoder: str, kbps: int, fps: int, vbv_frames: float = MIRROR_VBV_FRAMES,
                        keyframe_seconds: int = MIRROR_KEYFRAME_SECONDS) -> list[str]:
    """The encoder's options for mirroring at kbps and fps, with a keyframe every
    keyframe_seconds (0: at the start only; mirror_keyframe_interval)."""
    seconds = mirror_keyframe_interval(encoder, keyframe_seconds)
    bufsize = max(1, round(kbps * vbv_frames / max(1, fps)))
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{bufsize}k"]
    if seconds:
        gop = max(1, round(fps * seconds))
        common = ["-g", str(gop), "-bf", "0", "-force_key_frames", f"expr:gte(t,n_forced*{seconds})"]
        keyint = f"keyint={gop}:min-keyint={gop}:scenecut=0"
    else:
        common = ["-g", str(NO_PERIODIC_GOP), "-bf", "0"]
        keyint = "keyint=infinite:scenecut=0"
    if encoder == "h264_nvenc":
        # -delay 0: NVENC otherwise keeps about 3 frames in flight (50 ms at 60 fps).
        specific = ["-preset", "p4", "-tune", "ll", "-rc", "cbr", "-profile:v", "high", "-zerolatency", "1",
                    "-delay", "0", "-forced-idr", "1"]
    elif encoder == "h264_qsv":
        # -async_depth 1: Quick Sync works 4 frames ahead by default.
        specific = ["-preset", "veryfast", "-look_ahead", "0", "-async_depth", "1", "-low_delay_brc", "1",
                    "-forced_idr", "1", "-profile:v", "high"]
    elif encoder == "h264_amf":
        specific = ["-usage", "ultralowlatency", "-quality", "speed", "-rc", "cbr", "-latency", "1",
                    "-enforce_hrd", "1", "-profile:v", "high"]
    elif encoder == "h264_mf":
        specific = ["-rate_control", "cbr", "-scenario", "display_remoting", "-hw_encoding", "1"]
    else:
        specific = ["-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "high", "-x264-params", keyint]
    return ["-c:v", encoder] + specific + rate + common


def build_command(
    ffmpeg: str,
    settings: Settings,
    out_dir: Path,
    *,
    encoder: str,
    backend: str,
    monitor: Monitor | None = None,
    region: Region | None = None,
    audio_port: int | None = None,
    audio_rate: int = 48000,
    audio_channels: int = 2,
    for_apple: bool = True,
) -> list[str]:
    """Build the ffmpeg command: capture -> scale -> H.264 + AAC -> live HLS in out_dir.

    If audio_port is given, ffmpeg listens on 127.0.0.1:audio_port for raw float32 PCM
    (system audio captured by mirrorbridge.audio). Otherwise a silent track is added, since
    some Apple TV versions refuse to start an HLS stream without audio.
    for_apple=False writes the stream for TVs of other brands (see OTHER_TV_*), with
    MASTER_PLAYLIST next to the media playlist.
    """
    fps = settings.framerate
    seg = segment_seconds(settings, for_apple)
    gop = max(1, round(fps * seg))

    video_in, pre_filters = _video_input(backend, settings, monitor, region)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
           "-thread_queue_size", "512"] + video_in

    if audio_port is not None:
        cmd += [
            "-thread_queue_size", "1024", "-use_wallclock_as_timestamps", "1",
            "-f", "f32le", "-ar", str(audio_rate), "-ac", str(audio_channels),
            "-i", f"tcp://127.0.0.1:{audio_port}?listen=1",
        ]
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={audio_rate}:cl=stereo"]

    filters = list(pre_filters)
    # H.264 in 4:2:0 needs even width and height; windows often have odd sizes.
    if settings.output_height:
        filters.append(f"scale=-2:'trunc(min({settings.output_height},ih)/2)*2':flags=bicubic")
    else:
        filters.append("scale='trunc(iw/2)*2':'trunc(ih/2)*2'")
    filters.append("format=yuv420p" if encoder == "libx264" else "format=nv12")
    cmd += ["-vf", ",".join(filters)]

    cmd += _encoder_args(encoder, settings, gop)
    cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{seg})"]
    cmd += [
        "-c:a", "aac", "-b:a", f"{settings.audio_bitrate_kbps}k", "-ar", "48000", "-ac", "2",
        "-af", "aresample=async=1000",
        "-map", "0:v:0", "-map", "1:a:0",
    ]
    flags = "delete_segments+independent_segments+omit_endlist+temp_file"
    cmd += ["-f", "hls", "-hls_time", str(seg)]
    size = playlist_size(settings, for_apple)
    if for_apple:
        cmd += ["-hls_list_size", str(size), "-hls_flags", flags]
    else:
        cmd += [
            "-hls_list_size", str(size),
            "-hls_delete_threshold", str(size),
            "-hls_flags", flags + "+program_date_time",
            # FFmpeg takes CODECS from the encoder's SPS and the AAC config.
            "-master_pl_name", MASTER_PLAYLIST,
        ]
    cmd += [
        "-hls_allow_cache", "0",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
        str(out_dir / MEDIA_PLAYLIST),
    ]
    return cmd


def popen(cmd: list[str], stdout: int = subprocess.DEVNULL) -> subprocess.Popen:
    """Start FFmpeg; stdout=subprocess.PIPE to read what it writes to pipe:1."""
    _LOGGER.debug("Starting: %s", subprocess.list2cmdline(cmd))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,  # 'q' on stdin = graceful stop
        stdout=stdout,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
        env={**os.environ, "AV_LOG_FORCE_NOCOLOR": "1"},
    )
    if sys.platform == "win32":
        from . import winmonitors

        try:
            if not winmonitors.kill_with_this_process(int(proc._handle)):  # pylint: disable=protected-access
                _LOGGER.debug("Could not tie FFmpeg to MirrorBridge's lifetime")
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Job object setup failed", exc_info=True)
    return proc
