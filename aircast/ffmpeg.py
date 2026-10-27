"""Locate FFmpeg, probe hardware encoders and build the capture -> HLS command line."""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

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

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


class FFmpegNotFound(RuntimeError):
    """Raised when no usable ffmpeg binary can be found."""


def _windows_ffmpeg_candidates(exe: str) -> list[Path]:
    """Places winget, Chocolatey and Scoop put ffmpeg.exe.

    An app started before FFmpeg was installed keeps the old PATH (that is why
    'winget install Gyan.FFmpeg' can say "already installed" while AirCast still
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
    raise FFmpegNotFound(
        "FFmpeg was not found. Use the AirCast.exe build (FFmpeg included), run "
        "'pip install imageio-ffmpeg', or put ffmpeg.exe next to AirCast."
    )


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


_WORKING_ENCODERS: set[tuple[str, str]] = set()


def encoder_works(ffmpeg: str, encoder: str) -> bool:
    """Encode a few real frames; listing in `-encoders` does not mean the GPU exists.

    Only successes are cached: a GPU encoder that is busy (OBS, ShadowPlay) or slow to
    start right after logon gets probed again on the next cast.
    """
    if (ffmpeg, encoder) in _WORKING_ENCODERS:
        return True
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
        "-frames:v", "5", "-pix_fmt", "nv12" if encoder != "libx264" else "yuv420p",
        # Same options as a real cast, so an option the driver rejects shows up here.
        *_encoder_args(encoder, Settings(), gop=30), "-f", "null", "-",
    ]
    try:
        result = _run(args, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        _LOGGER.debug("Encoder %s unusable: %s", encoder, result.stderr.strip()[-300:])
        return False
    _WORKING_ENCODERS.add((ffmpeg, encoder))
    return True


def available_encoders(ffmpeg: str) -> list[str]:
    return [enc for enc in ENCODER_PREFERENCE if encoder_works(ffmpeg, enc)]


def pick_encoder(ffmpeg: str, wanted: str) -> str:
    if wanted != "auto":
        if encoder_works(ffmpeg, wanted):
            return wanted
        _LOGGER.warning("Encoder %s does not work on this machine, auto-selecting", wanted)
    for enc in ENCODER_PREFERENCE:
        if encoder_works(ffmpeg, enc):
            return enc
    raise RuntimeError("No working H.264 encoder found in this FFmpeg build")


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
        raise RuntimeError(f'The window "{title}" is not on screen. Restore it if it is minimized, or pick another one.')
    left, top, width, height = rect
    right, bottom = left + width, top + height

    def overlap(m: Monitor) -> int:
        w = min(right, m.left + m.width) - max(left, m.left)
        h = min(bottom, m.top + m.height) - max(top, m.top)
        return max(0, w) * max(0, h)

    best = max(monitors, key=overlap, default=None)
    if best is None or overlap(best) == 0:
        raise RuntimeError(f'The window "{title}" is off screen.')
    x0, y0 = max(left, best.left), max(top, best.top)
    x1, y1 = min(right, best.left + best.width), min(bottom, best.top + best.height)
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if w < 64 or h < 64:
        raise RuntimeError(f'The window "{title}" is too small to cast.')
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
) -> list[str]:
    """Build the ffmpeg command: capture -> scale -> H.264 + AAC -> live HLS in out_dir.

    If audio_port is given, ffmpeg listens on 127.0.0.1:audio_port for raw float32 PCM
    (system audio captured by aircast.audio). Otherwise a silent track is added, since
    some Apple TV versions refuse to start an HLS stream without audio.
    """
    fps = settings.framerate
    seg = LATENCY_SEGMENT_SECONDS.get(settings.latency_mode, 1.0)
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
    cmd += [
        "-f", "hls",
        "-hls_time", str(seg),
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
        "-hls_allow_cache", "0",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
        str(out_dir / "stream.m3u8"),
    ]
    return cmd


def popen(cmd: list[str]) -> subprocess.Popen:
    _LOGGER.debug("Starting: %s", subprocess.list2cmdline(cmd))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,  # 'q' on stdin = graceful stop
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
        env={**os.environ, "AV_LOG_FORCE_NOCOLOR": "1"},
    )
    if sys.platform == "win32":
        from . import winmonitors

        try:
            if not winmonitors.kill_with_this_process(int(proc._handle)):  # pylint: disable=protected-access
                _LOGGER.debug("Could not tie FFmpeg to AirCast's lifetime")
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Job object setup failed", exc_info=True)
    return proc
