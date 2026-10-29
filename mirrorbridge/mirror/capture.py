"""How the screen gets to the encoder (the capture chain), and what FFmpeg says when Windows
takes the screen capture away. Nothing here runs FFmpeg but has_filter and gfx_repeats; the
session decides.

Three chains, the first that can be used tried first (choose_chain), source.build_command making
the command for each:

- GPU, for NVIDIA NVENC with ddagrab, when the screen already fits the TV's screen and both
  sides are even: ddagrab's frames go straight into NVENC, with no filter at all, and never
  leave the graphics card. NVENC turns them into YUV itself, BT.601 limited range, and says so
  in the stream (NATIVE_COLOUR); NATIVE_OUTPUT makes the stream also say BT.709 primaries and
  transfer, as for the other chains, instead of the sRGB transfer the screen's frames come
  tagged with. (scale_d3d11, FFmpeg 8.0 and later, could scale them there too, but it fails on
  Humberto's graphics card: GPU_SCALED_ENABLED.)
- GFX, for NVENC when the screen is bigger than the TV's or of an odd size, with an FFmpeg that
  has gfxcapture (8.1 and later): Windows Graphics Capture scales the display on the graphics
  card into the size asked for, keeping its shape (gfx_input), and NVENC converts its RGB as for
  GPU. It gives a frame only when something on the screen changes, stamped with when it did,
  up to gfx_rate a second. An FFmpeg whose gfxcapture can repeat the last frame (dup_frames,
  after FFmpeg 9.1: gfx_repeats) is asked to, as ddagrab does by itself, and gfx_filters keeps
  the frames to the frame rate sent. Without it a still screen gives no frames for as long as
  it stays so (the session allows for that), and none come to sharpen the one that changed it,
  which is blurred through a one-frame VBV (a larger one helps only in part:
  GFX_STILL_VBV_FRAMES). A still slide would stay blurred until something moves, so Scaling
  "auto" takes GFX only with repeats, and "gfx" (the Debug menu's test) takes it either way.
- CPU, for every other encoder and capture, and as the fallback: the frames are copied to
  memory and scaled by swscale into BT.709 limited range, and tagged so (until now the pixels
  were BT.601 and the stream said nothing, which receivers take as BT.709 for HD).

A GPU or GFX chain that fails on a display (FFmpeg ends before its first frame, or the picture
is not the size asked for, sps_size and size_problem: a rotated screen, a size Windows gave in
other units) is not tried again on it for the rest of the run (BROKEN).

Windows takes the capture away now and then: a display or its driver changing, the secure
desktop of a UAC prompt or the lock screen, signing out. ddagrab then ends FFmpeg, with one of
the messages capture_lost knows; gfxcapture with one too, or, the display gone, by ending its
video, which FFmpeg takes as the end of its input. The session can start FFmpeg again.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from typing import Optional

from .. import ffmpeg
from ..text import Text
from . import h264

_LOGGER = logging.getLogger(__name__)

CPU, GPU, GFX = "cpu", "gpu", "gfx"
GPU_ENCODERS = ("h264_nvenc",)  # encoders the graphics card chains feed; Quick Sync, AMF and MF are not tried yet
# scale_d3d11 when the screen is bigger than the TV's (if this FFmpeg has it). Off: on Humberto's RTX 4080
# SUPER (driver 621.01) it fails with FFmpeg 8.1.1 and 9.0.2 alike, with hwupload in front too
# ("[AVHWFramesContext] Could not create the texture (80070057)", then "Failed to configure output pad on
# Parsed_scale_d3d11", 2026-10-29), so gfxcapture scales instead. Kept for a card or driver it works on.
GPU_SCALED_ENABLED = False
GFX_ENABLED = True  # gfxcapture when the screen is bigger than the TV's (if this FFmpeg has it)
GFX_REPEATS = "dup_frames"  # gfxcapture's option to repeat the last frame (FFmpeg after 9.1: gfx_repeats)
GFX_EXTRA_FPS = 10  # gfxcapture's max_framerate above the frame rate sent, with repeats (gfx_rate)
# Without repeats nothing keeps the frames to the frame rate (gfx_filters), so max_framerate is the
# frame rate and this much more: Windows hands a frame over no sooner than 1/max_framerate after the
# last one, and at exactly 60 a 60 Hz screen's refreshes would be right at that edge, where one a
# hair early would wait for the next. At 62 (16.1 ms) a 60, 120 or 240 Hz screen gives 60 frames a
# second; one whose refresh is not a multiple of 60 gives fewer: 50 at 200 Hz (every fourth
# refresh), 48 at 144 Hz and 55 at 165 Hz (every third).
GFX_SPARE_FPS = 2
# The VBV, in frames, of the GFX chain without repeats. Through the one-frame VBV of the other chains
# (ffmpeg.MIRROR_VBV_FRAMES) a frame that changes most of the screen (a keyframe, a new slide) comes
# out blurred, and on a still screen no frames come after it to sharpen it. Measured with x264 and
# mirroring's rate control (CBR, -bufsize N frames, at a steady frame rate as NVENC counts them) as
# a stand-in for NVENC, at 8 Mbit/s and 60 fps, on two 1080p slides (lyrics on a photo; notes with
# a photo), PSNR of Y and SSIM of the frame that brings a new slide: 22.5-27.1 dB and 0.85-0.92
# with 1 frame, 28.4-33.0 dB with 2, 34.3-39.7 dB and 0.971-0.979 with 4, 37.3-41.4 dB with 8;
# what a one-frame VBV reaches 10 frames later (ddagrab's repeats) was 35.1-39.9 dB and
# 0.972-0.975. A keyframe went from 23.1-26.9 dB with 1 to 32.6-35.7 dB with 4, and no further
# with 8 (x264 gives a stream's first frame no more, whatever the VBV). It helps a frame that
# comes after others, as a slide after half a second of the mouse pointer moving (34.6 dB with 4,
# 23.1 with 1), not slides changed one after the other with nothing between them: the encoder's
# buffer fills by a frame's worth for each frame it gets, not with time, so those got 26.3-29.6
# dB with 4 (24.1-26.8 with 1). The cost: a frame may be up to 4 frames' worth (67 kB at 8 Mbit/s
# and 60 fps), which on a link no faster than the bitrate reaches the TV up to 3 frames later
# (50 ms at 60 fps, 100 ms at 30); on a 100 Mbit/s link, 4 ms.
GFX_STILL_VBV_FRAMES = 4
GFX_SCALE_MODE = "bilinear"  # gfxcapture's own default (bicubic is not measured)
# What gfxcapture logs when Windows ends its capture (the display gone): only as information, which
# FFmpeg does not show at -loglevel warning, and the video ends, so FFmpeg ends by itself, exit code 0.
GFX_CLOSED = "Capture item closed"


@dataclass(frozen=True)
class Colour:
    """What a chain's pixels are: range "tv" (limited) or "pc" (full), the YCbCr matrix as
    FFmpeg names it, and whether that is known (measured, or what the filter is told to write)
    rather than found by someone else on other hardware."""

    range: str
    matrix: str
    measured: bool = True

    @property
    def label(self) -> str:
        matrix = {"bt709": "BT.709", "bt470bg": "BT.601"}.get(self.matrix, self.matrix)
        found = "" if self.measured else ", not measured here"
        return f"{matrix} {'full' if self.range == 'pc' else 'limited'} range{found}"


CPU_COLOUR = Colour("tv", "bt709")  # swscale writes what it is told to
# NVENC's own conversion of the screen's RGB (seen on Humberto's PC with ddagrab; FFmpeg's nvenc.c
# tags any RGB input so, whatever range its frames say, as gfxcapture's say full)
NATIVE_COLOUR = Colour("tv", "bt470bg")
# The video processor's default: measured by a third party on an RTX 4060 with driver 591.74
# (github.com/mattpocock/course-video-manager, pull request 2229).
GPU_SCALED_COLOUR = Colour("pc", "bt709", measured=False)
# Not -color_primaries and -color_trc: FFmpeg 7.1 and 8.1 give the encoder the frames' own tags in
# their place when it opens (fftools/ffmpeg_enc.c), and ddagrab and gfxcapture tag their frames
# sRGB. So the stream's header is changed instead.
NATIVE_OUTPUT = ("-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1")
# gfxcapture's frames have the times the screen changed at, on a link that says max_framerate a
# second: FFmpeg would give the encoder 1/max_framerate as its time base and move each frame onto
# that grid (a steady 60 a second came out 14, 15 and 28 ms apart: judder). In ms, as FLV has them.
GFX_OUTPUT = ("-enc_time_base", "1:1000")

# (ffmpeg, display_key, GPU or GFX) where that chain failed, for the rest of the run
BROKEN: set[tuple[str, str, str]] = set()


@dataclass(frozen=True)
class Chain:
    """One way from the capture to the encoder: kind GPU, GFX or CPU, the capture's size (None
    when not known: gdigrab by window title, the test pattern), the size encoded (None for CPU:
    swscale fits it itself), the colours of what is encoded, and for GFX whether gfxcapture
    repeats the last frame on a still screen (gfx_repeats)."""

    kind: str
    size_in: Optional[tuple[int, int]] = None
    size_out: Optional[tuple[int, int]] = None
    colour: Optional[Colour] = None
    repeats: bool = False

    @property
    def native(self) -> bool:
        """GPU at the capture's own size: no filter at all."""
        return self.kind == GPU and self.size_in is not None and self.size_in == self.size_out

    @property
    def changes_only(self) -> bool:
        """gfxcapture without repeats: frames only as the screen changes, none while it is still."""
        return self.kind == GFX and not self.repeats

    @property
    def label(self) -> str:
        if self.kind == GFX:
            return f"GPU gfxcapture {'with' if self.repeats else 'without'} repeats"
        if self.kind != GPU:
            return "CPU swscale"
        return "GPU native" if self.native else "GPU scale_d3d11"


KIND_WORDS = {GPU: "the graphics card's", GFX: "the graphics card's, through Windows Graphics Capture",
              CPU: "the processor's"}  # for the log


def fit_even(width: int, height: int, canvas: tuple[int, int]) -> tuple[int, int]:
    """width x height fitted into canvas keeping its shape, never made bigger, each side then
    rounded down to an even number (NV12 needs even sides; rounding up could go past an odd
    canvas). 2560x1440 in 1920x1080 is 1920x1080, 682x2560 is 286x1080, 1919x1080 is 1918x1080."""
    width, height = max(1, width), max(1, height)
    most_w, most_h = canvas
    if width > most_w or height > most_h:
        if width * most_h >= height * most_w:  # the width is what limits it
            width, height = most_w, height * most_w // width
        else:
            width, height = width * most_h // height, most_h
    return max(2, width // 2 * 2), max(2, height // 2 * 2)


def capture_size(backend: str, monitor: "ffmpeg.Monitor | None",
                 region: "ffmpeg.Region | None") -> Optional[tuple[int, int]]:
    """How big the captured picture is: the window's rectangle, else the display's; None for
    the test pattern or when there is neither."""
    if backend == "test":
        return None
    if region is not None:
        return region.width, region.height
    if monitor is not None:
        return monitor.width, monitor.height
    return None


def needs_scaling(size_in: Optional[tuple[int, int]], canvas: tuple[int, int]) -> bool:
    """Whether the capture has to be scaled for the TV (an unknown size has to be, to be sure)."""
    return size_in is None or fit_even(*size_in, canvas) != tuple(size_in)


def display_key(monitor: "ffmpeg.Monitor | None") -> str:
    if monitor is None:
        return ""
    return monitor.device or f"output {monitor.dxgi_output}"


def mark_broken(ff: str, monitor: "ffmpeg.Monitor | None", kind: str = GPU) -> None:
    """That graphics card chain (GPU or GFX) failed on this display: not again there until the
    app restarts."""
    BROKEN.add((ff, display_key(monitor), kind))


def is_broken(ff: str, monitor: "ffmpeg.Monitor | None", kind: str = GPU) -> bool:
    return (ff, display_key(monitor), kind) in BROKEN


def capture_name(backend: str, kind: str) -> str:
    """What captures the screen, for the log and the tray: gfxcapture for GFX (its backend is
    ddagrab's, the capture of the chains after it), else the backend."""
    return "gfxcapture" if kind == GFX else backend


def choose_chain(ff: str, *, encoder: str, backend: str, monitor: "ffmpeg.Monitor | None",
                 region: "ffmpeg.Region | None", canvas: tuple[int, int], scaler: str) -> tuple[str, str]:
    """The chain to try first, GPU, GFX or CPU, and why, in words for the log: GPU when the
    screen needs no scaling, else GFX (with `scaler` "gfx" whether gfxcapture repeats frames or
    not, otherwise only if it does), else scale_d3d11 (when it is turned on), else CPU. Any
    `scaler` but "cpu" and "gfx" is "auto"."""
    size_in = capture_size(backend, monitor, region)
    if scaler == "cpu":
        return CPU, "the Debug menu's Scaling is set to the processor"
    if sys.platform != "win32":
        return CPU, "the graphics card chain is for Windows only"
    if backend != "ddagrab":
        return CPU, f"the capture is {backend or 'not of a screen'}, not ddagrab"
    if encoder not in GPU_ENCODERS:
        return CPU, f"{ffmpeg.ENCODER_LABELS.get(encoder, encoder)} does not take frames from the graphics card yet"
    if size_in is None:
        return CPU, "the size of the capture is not known"
    if not needs_scaling(size_in, canvas):
        if is_broken(ff, monitor, GPU):
            return CPU, f"it failed on {display_key(monitor)} earlier in this run"
        return GPU, f"{size_in[0]}x{size_in[1]} needs no scaling"
    width, height = fit_even(*size_in, canvas)
    scaled = f"from {size_in[0]}x{size_in[1]} to {width}x{height}"
    gfx_why = gfx_unusable(ff, monitor, region, scaler)
    if not gfx_why:
        if gfx_repeats(ff):
            return GFX, f"gfxcapture scales {scaled}, repeating the last frame while the screen is still"
        return GFX, (f"gfxcapture scales {scaled}, as the Debug menu's Scaling says, without repeats "
                     f"(this FFmpeg's gfxcapture has no {GFX_REPEATS}): frames only as the screen changes")
    d3d11_why = d3d11_unusable(ff, monitor)
    if not d3d11_why:
        return GPU, f"scale_d3d11 {scaled} ({gfx_why})"
    return CPU, f"scaling {scaled}: {gfx_why}; {d3d11_why}"


def gfx_unusable(ff: str, monitor: "ffmpeg.Monitor | None", region: "ffmpeg.Region | None",
                 scaler: str = "auto") -> str:
    """Why gfxcapture cannot scale this display ("" if it can): for `scaler` "auto", also an
    FFmpeg whose gfxcapture cannot repeat frames."""
    if not GFX_ENABLED:
        return "gfxcapture is turned off (capture.GFX_ENABLED)"
    if region is not None:
        return "gfxcapture is not used for a window"
    if is_broken(ff, monitor, GFX):
        return f"gfxcapture failed on {display_key(monitor)} earlier in this run"
    if not ffmpeg.has_filter(ff, "gfxcapture"):
        return "this FFmpeg has no gfxcapture (FFmpeg 8.1 and later have it)"
    if scaler != "gfx" and not gfx_repeats(ff):
        return (f"this FFmpeg's gfxcapture has no {GFX_REPEATS} (FFmpeg after 9.1 has it), so a still screen "
                "would get no frames to sharpen the one that changed it")
    if not gfx_monitor(monitor):
        return f"Windows does not list {display_key(monitor) or 'the display'} for gfxcapture"
    return ""


def d3d11_unusable(ff: str, monitor: "ffmpeg.Monitor | None") -> str:
    """Why scale_d3d11 cannot scale ddagrab's frames ("" if it can)."""
    if not GPU_SCALED_ENABLED:
        return "scale_d3d11 is turned off (capture.GPU_SCALED_ENABLED)"
    if is_broken(ff, monitor, GPU):
        return f"scale_d3d11 failed on {display_key(monitor)} earlier in this run"
    if not ffmpeg.has_filter(ff, "scale_d3d11"):
        return "this FFmpeg has no scale_d3d11 (FFmpeg 8.0 and later have it)"
    return ""


def chain_for(kind: str, size_in: Optional[tuple[int, int]], canvas: tuple[int, int],
              repeats: bool = False) -> Chain:
    """The chain of that kind for a capture of size_in and the TV's canvas (GFX: repeating
    frames or not). GPU and GFX need size_in."""
    if kind in (GPU, GFX):
        if size_in is None:
            raise ValueError("the graphics card chains need the size of the capture")
        size_out = fit_even(*size_in, canvas)
        if kind == GFX:  # RGB into NVENC, as at the screen's own size
            return Chain(GFX, tuple(size_in), size_out, NATIVE_COLOUR, repeats)
        native = size_out == tuple(size_in)
        return Chain(GPU, tuple(size_in), size_out, NATIVE_COLOUR if native else GPU_SCALED_COLOUR)
    return Chain(CPU, tuple(size_in) if size_in else None, None, CPU_COLOUR)


def chain_label(chain: Optional[Chain], backend: str) -> str:
    """For the log: GPU native, GPU gfxcapture with or without repeats, GPU scale_d3d11, CPU
    swscale, gdigrab or test."""
    if backend in ("test", "gdigrab"):
        return backend
    return (chain or Chain(CPU)).label


def via(backend: str, chain: Optional[Chain], encoder: str) -> str:
    """What the encoder gets its frames through, for "{encoder} via {backend}" in the tray as a
    session starts: the capture, and where the chain can be the graphics card's (ddagrab into
    GPU_ENCODERS) the chain too, gfxcapture's saying whether it repeats frames."""
    if chain is None or backend != "ddagrab" or encoder not in GPU_ENCODERS:
        return backend
    if chain.kind == GFX:
        if chain.repeats:
            return Text("via_gfx_repeats", "gfxcapture on the graphics card, repeating frames")
        return Text("via_gfx_changes", "gfxcapture on the graphics card, frames only on changes")
    if chain.kind == GPU:
        return Text("via_gpu", "{capture} on the graphics card", capture=backend)
    return Text("via_cpu", "{capture} and the processor", capture=backend)


def vbv_frames(chain: Optional[Chain]) -> float:
    """The encoder's VBV, in frames: GFX_STILL_VBV_FRAMES for gfxcapture without repeats, else
    the one frame of mirroring (ffmpeg.MIRROR_VBV_FRAMES)."""
    return GFX_STILL_VBV_FRAMES if chain is not None and chain.changes_only else ffmpeg.MIRROR_VBV_FRAMES


def gpu_filters(chain: Chain) -> list[str]:
    """The GPU chain's -vf: none at the capture's own size, else scale_d3d11 into NV12 and the
    colours it writes. The size is fit_even's (chain_for): scale_d3d11 keeps no shape of its
    own, it stretches to exactly what it is given."""
    if chain.native:
        return []
    assert chain.size_out is not None
    width, height = chain.size_out
    colour = chain.colour or GPU_SCALED_COLOUR
    return [f"scale_d3d11=width={width}:height={height}:format=nv12",
            f"setparams=range={colour.range}:colorspace={colour.matrix}:color_primaries=bt709:color_trc=bt709"]


def output_args(chain: Optional[Chain]) -> list[str]:
    """Output options a chain needs after the encoder's (RGB into NVENC: GPU at its own size, GFX;
    and GFX's times as they came, GFX_OUTPUT)."""
    if chain is None or not (chain.native or chain.kind == GFX):
        return []
    return (list(GFX_OUTPUT) if chain.kind == GFX else []) + list(NATIVE_OUTPUT)


def cpu_filters(prefilters: list[str], width: int, height: int, encoder: str, bt709: bool = True) -> list[str]:
    """The CPU chain's -vf: the capture's own filters (ddagrab's frames brought to memory), then
    swscale fitting it into width x height (never up, even sides; bilinear: a quarter less CPU
    than bicubic for a 1440p desktop on a 1080p TV, every frame) into BT.709 limited range, and
    the frames tagged as that. Without bt709, swscale's own choice and nothing said, as before
    (BT.601, limited: what Google Cast's VP8 is taken as, which can say nothing else)."""
    scale = (f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease"
             ":force_divisible_by=2:flags=bilinear")
    pixels = "format=yuv420p" if encoder == "libx264" else "format=nv12"
    if not bt709:
        return list(prefilters) + [scale, pixels]
    return list(prefilters) + [
        scale + ":out_color_matrix=bt709:out_range=tv", pixels,
        "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709",
    ]


# -- gfxcapture ------------------------------------------------------------------------------------


def gfx_repeats(ff: str) -> bool:
    """Whether this FFmpeg's gfxcapture can repeat the last frame while the screen is still, as
    ddagrab does (GFX_REPEATS: FFmpeg after 9.1; not 8.1.1). Asked once for each FFmpeg
    (ffmpeg.filter_options)."""
    return GFX_REPEATS in ffmpeg.filter_options(ff, "gfxcapture")


def gfx_rate(fps: int, repeats: bool) -> int:
    """gfxcapture's max_framerate when `fps` are sent. Windows hands a frame over at a refresh of
    the screen at least 1/max_framerate after the last one: on Humberto's 200 Hz screen 60
    (16.7 ms) gave every fourth refresh, 50 fps, and 70 (14.3 ms) every third, 66 fps. With
    repeats, GFX_EXTRA_FPS more, which gfx_filters keeps to `fps` (on a 144 Hz screen 70 is still
    every third refresh: 48 fps); without, GFX_SPARE_FPS more, and nothing else."""
    return fps + (GFX_EXTRA_FPS if repeats else GFX_SPARE_FPS)


def gfx_kbps(kbps: int, fps: int, repeats: bool) -> int:
    """The bitrate NVENC is given with gfxcapture. It is told gfx_rate frames a second (the
    capture's own rate: FFmpeg has no other to give it for frames that come as the screen
    changes) and gives each frame its bitrate's share of that, so it gets as many more kbit/s
    as make each frame's share what it is at `fps`, and the VBV (bufsize) the same."""
    return round(kbps * gfx_rate(fps, repeats) / max(1, fps))


def gfx_monitor(monitor: "ffmpeg.Monitor | None") -> str:
    """gfxcapture's option for the display: hmonitor= its HMONITOR, found by its device name, else
    monitor_idx= its place in EnumDisplayMonitors' order, found by where it is (not ddagrab's
    output_idx: on Humberto's PC gfxcapture's 0 is the case screen, ddagrab's the main monitor);
    "" if Windows lists no such display. Looked up again for each FFmpeg: a display's handle
    changes when it comes back."""
    if monitor is None or sys.platform != "win32":
        return ""
    from .. import winmonitors  # pylint: disable=import-outside-toplevel

    try:
        listed = winmonitors.monitor_handles()
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Listing the displays' handles failed", exc_info=True)
        return ""
    if monitor.device:
        for found in listed:
            if found.device.lower() == monitor.device.lower():
                return f"hmonitor={found.handle}"
    for found in listed:
        if (found.left, found.top) == (monitor.left, monitor.top):
            return f"monitor_idx={found.index}"
    return ""


def gfx_input(target: str, size: tuple[int, int], fps: int, cursor: bool, repeats: bool) -> list[str]:
    """gfxcapture's input: the display `target` names (gfx_monitor; with none FFmpeg ends at once,
    saying so), scaled on the graphics card into `size` keeping its shape (fit_even's, so with no
    bars), up to gfx_rate(fps, repeats) frames a second, repeating the last one while the screen
    is still (`repeats`: an FFmpeg that can, gfx_repeats) or not, with the mouse pointer or not."""
    options = [target, f"width={size[0]}", f"height={size[1]}", "resize_mode=scale_aspect",
               f"scale_mode={GFX_SCALE_MODE}", f"max_framerate={gfx_rate(fps, repeats)}",
               f"{GFX_REPEATS}=1" if repeats else "", f"capture_cursor={int(bool(cursor))}"]
    return ["-f", "lavfi", "-i", "gfxcapture=" + ":".join(option for option in options if option)]


def gfx_filters(fps: int, repeats: bool) -> list[str]:
    """gfxcapture's -vf with repeats: at most one frame in each 1/fps of a second (the first that
    comes in it), each as soon as it comes; without, none (gfx_rate keeps to about fps).

    Not the fps filter: that holds each frame until the next one comes, to know how many times
    to repeat it, so on a screen that changes now and then each change would show only with the
    next one, and the time between them would come at once, as repeats of the frame before (seen
    with a source like gfxcapture's: a frame 1.5 s late, then 84 repeats in 70 ms).

    The slots are half of one off the first frame: gfxcapture's times start at 0 with it, so on a
    screen refreshed at the frame rate (60 Hz) the next ones would come at the slots' edges, and
    a few microseconds early would put one in the slot of the frame before, which loses it (half
    of a 60 a second, in a test with times 0.05 ms either side of the edges).

    A frame that comes in the slot of the one before is left out (on a 200 Hz screen, gfxcapture
    gives 66 a second for 60: one in ten). If it was the last change for a while, its first
    repeat is not: gfxcapture repeats the last frame when none came for 1.5 of its intervals
    (FFmpeg's vsrc_gfxcapture_winrt.cpp: 21 ms at 70 a second, then every 14 ms), stamped later
    than that frame by at least as much, so in a later slot (1.5/gfx_rate is more than 1/fps
    above 20 fps), and the screen as it ended reaches the TV about 21 ms late, not never.
    Without repeats nothing would come after it, and the TV would show the frame before until
    the screen changes again: so then no select."""
    if not repeats:
        return []
    return [f"select='isnan(prev_selected_t)+gt(floor(t*{fps}+0.5),floor(prev_selected_t*{fps}+0.5))'"]


# -- the picture made ------------------------------------------------------------------------------


def sps_size(sps: bytes) -> Optional[tuple[int, int]]:
    """The size of the picture an SPS NAL unit (with its header byte) describes; None if it
    cannot be read."""
    try:
        info = h264.parse_sps(sps)
    except (ValueError, IndexError):
        return None
    return info.width, info.height


def size_problem(made: tuple[int, int], chain: Chain, canvas: tuple[int, int]) -> str:
    """What is wrong with the size of the picture a chain made (from its first SPS): not what
    was asked for, or bigger than the TV's screen. "" if nothing."""
    if chain.size_out is not None and tuple(made) != tuple(chain.size_out):
        return f"the encoder made {made[0]}x{made[1]}, not the {chain.size_out[0]}x{chain.size_out[1]} asked for"
    if made[0] > canvas[0] or made[1] > canvas[1]:
        return f"the encoder made {made[0]}x{made[1]}, bigger than the TV's {canvas[0]}x{canvas[1]}"
    return ""


# -- the capture lost ------------------------------------------------------------------------------

# What FFmpeg says when Windows takes the screen capture away, any letter case.
_CAPTURE_LOST = re.compile("|".join([
    # ddagrab, while it runs: 887a0026 access lost (a display or its driver changing, signing
    # out), 887a0005 and 887a0007 the graphics card removed or reset
    r"AcquireNextFrame failed: [0-9a-f]+",
    r"Output parameters changed!",  # a new resolution or format
    # ddagrab, starting: the secure desktop (UAC, the lock screen), and a display that is going
    # or coming back
    r"Desktop duplication access denied",
    r"Too many open duplication sessions",
    r"Failed to enumerate DXGI output",
    r"Failed getting output description",
    r"Selected output not supported",
    r"Failed duplicating output",
    r"Requested output format unavailable",
    r"Failed to capture image",  # gdigrab
    # gfxcapture (vsrc_gfxcapture_winrt.cpp), while it runs: a DXGI error in any of its calls, as
    # "<call> failed: 0x887A0005" (the graphics card removed; 0006 hung, 0007 reset, 0020 its
    # driver's error, 0026 access lost); starting: a display that is going or coming back (its
    # handle no longer good, or not listed)
    r"failed: 0x887A00[0-9a-f]{2}",
    r"Failed to setup graphics capture for monitor",
    r"Failed to start graphics capture session",
    r"Failed to find capture source",
]), re.IGNORECASE)
_LOG_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+")  # [Parsed_ddagrab_0 @ 000001d2...]


def capture_lost(stderr: str, *, kind: str = CPU, returncode: Optional[int] = None) -> Optional[str]:
    """The first line of FFmpeg's stderr that says the screen capture was lost, without FFmpeg's
    [filter @ address] prefix; None if none does (an encoder error, or just "Generic error").
    For gfxcapture (kind GFX), an FFmpeg that ended by itself (`returncode` 0) with no such line
    lost it too: GFX_CLOSED."""
    for line in stderr.splitlines():
        if _CAPTURE_LOST.search(line):
            return _LOG_PREFIX.sub("", line.strip()).strip()
    if kind == GFX and returncode == 0:
        return f"{GFX_CLOSED} (FFmpeg ended by itself)"
    return None
