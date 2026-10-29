"""How the screen gets to the encoder (the capture chain), and what FFmpeg says when Windows
takes the screen capture away. Nothing here runs FFmpeg but has_filter; the session decides.

Two chains, the GPU one first when it can be used (gpu_possible), source.build_command making
the command for either:

- GPU, for NVIDIA NVENC with ddagrab only: the frames never leave the graphics card.
  - At the screen's own size (it already fits the TV's screen and both sides are even),
    ddagrab's frames go straight into NVENC, with no filter at all. NVENC turns them into YUV
    itself, BT.601 limited range, and says so in the stream (NATIVE_COLOUR); NATIVE_OUTPUT
    makes the stream also say BT.709 primaries and transfer, as for the other chains, instead
    of the sRGB transfer the screen's frames come tagged with.
  - Bigger than the TV's screen, or of an odd size, scale_d3d11 (FFmpeg 8.0 and later) scales
    them on the graphics card into NV12. It cannot be told which colours to write, so the
    frames are what the driver's video processor writes, tagged as GPU_SCALED_COLOUR: BT.709
    full range, as measured on an RTX 4060 (not yet on Humberto's PC). GPU_SCALED_ENABLED
    turns this one off.
- CPU, for every other encoder and capture, and as the fallback: the frames are copied to
  memory and scaled by swscale into BT.709 limited range, and tagged so (until now the pixels
  were BT.601 and the stream said nothing, which receivers take as BT.709 for HD).

A GPU chain that fails on a display (FFmpeg ends before its first frame, or the picture is not
the size asked for, sps_size and size_problem: a rotated screen, a size Windows gave in other
units) is not tried again on it for the rest of the run (BROKEN).

Windows takes the capture away now and then: a display or its driver changing, the secure
desktop of a UAC prompt or the lock screen, signing out. ddagrab then ends FFmpeg, with one of
the messages capture_lost knows; the session can start FFmpeg again.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Optional

from .. import ffmpeg
from . import h264

CPU, GPU = "cpu", "gpu"
GPU_ENCODERS = ("h264_nvenc",)  # encoders the GPU chain feeds; Quick Sync, AMF and MF are not tried yet
GPU_SCALED_ENABLED = True  # scale_d3d11 when the screen is bigger than the TV's (if this FFmpeg has it)


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
NATIVE_COLOUR = Colour("tv", "bt470bg")  # NVENC's own conversion of the screen's RGB (seen on Humberto's PC)
# The video processor's default: measured by a third party on an RTX 4060 with driver 591.74
# (github.com/mattpocock/course-video-manager, pull request 2229).
GPU_SCALED_COLOUR = Colour("pc", "bt709", measured=False)
# Not -color_primaries and -color_trc: FFmpeg 7.1 and 8.1 give the encoder the frames' own tags in
# their place when it opens (fftools/ffmpeg_enc.c), and ddagrab tags its frames sRGB. So the
# stream's header is changed instead.
NATIVE_OUTPUT = ("-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1")

# (ffmpeg, display_key) where the GPU chain failed, for the rest of the run
BROKEN: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class Chain:
    """One way from the capture to the encoder: kind GPU or CPU, the capture's size (None when
    not known: gdigrab by window title, the test pattern), the size encoded (None for CPU:
    swscale fits it itself) and the colours of what is encoded."""

    kind: str
    size_in: Optional[tuple[int, int]] = None
    size_out: Optional[tuple[int, int]] = None
    colour: Optional[Colour] = None

    @property
    def native(self) -> bool:
        """GPU at the capture's own size: no filter at all."""
        return self.kind == GPU and self.size_in is not None and self.size_in == self.size_out

    @property
    def label(self) -> str:
        if self.kind != GPU:
            return "CPU swscale"
        return "GPU native" if self.native else "GPU scale_d3d11"


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


def mark_broken(ff: str, monitor: "ffmpeg.Monitor | None") -> None:
    """The GPU chain failed on this display: the CPU one from now on, until the app restarts."""
    BROKEN.add((ff, display_key(monitor)))


def is_broken(ff: str, monitor: "ffmpeg.Monitor | None") -> bool:
    return (ff, display_key(monitor)) in BROKEN


def gpu_possible(ff: str, *, encoder: str, backend: str, monitor: "ffmpeg.Monitor | None",
                 region: "ffmpeg.Region | None", canvas: tuple[int, int], scaler: str) -> tuple[bool, str]:
    """Whether the GPU chain can be used, and why (or why not), in words for the log."""
    size_in = capture_size(backend, monitor, region)
    if scaler == "cpu":
        return False, "the Debug menu's Scaling is set to the processor"
    if sys.platform != "win32":
        return False, "the graphics card chain is for Windows only"
    if backend != "ddagrab":
        return False, f"the capture is {backend or 'not of a screen'}, not ddagrab"
    if encoder not in GPU_ENCODERS:
        return False, f"{ffmpeg.ENCODER_LABELS.get(encoder, encoder)} does not take frames from the graphics card yet"
    if size_in is None:
        return False, "the size of the capture is not known"
    if is_broken(ff, monitor):
        return False, f"it failed on {display_key(monitor)} earlier in this run"
    if not needs_scaling(size_in, canvas):
        return True, f"{size_in[0]}x{size_in[1]} needs no scaling"
    if not GPU_SCALED_ENABLED:
        return False, "scaling on the graphics card is turned off (capture.GPU_SCALED_ENABLED)"
    if not ffmpeg.has_filter(ff, "scale_d3d11"):
        return False, "this FFmpeg has no scale_d3d11 (FFmpeg 8.0 and later have it)"
    width, height = fit_even(*size_in, canvas)
    return True, f"scale_d3d11 from {size_in[0]}x{size_in[1]} to {width}x{height}"


def chain_for(kind: str, size_in: Optional[tuple[int, int]], canvas: tuple[int, int]) -> Chain:
    """The chain of that kind for a capture of size_in and the TV's canvas. GPU needs size_in."""
    if kind == GPU:
        if size_in is None:
            raise ValueError("the GPU chain needs the size of the capture")
        size_out = fit_even(*size_in, canvas)
        native = size_out == tuple(size_in)
        return Chain(GPU, tuple(size_in), size_out, NATIVE_COLOUR if native else GPU_SCALED_COLOUR)
    return Chain(CPU, tuple(size_in) if size_in else None, None, CPU_COLOUR)


def chain_label(chain: Optional[Chain], backend: str) -> str:
    """For the log: GPU native, GPU scale_d3d11, CPU swscale, gdigrab or test."""
    if backend in ("test", "gdigrab"):
        return backend
    return (chain or Chain(CPU)).label


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
    """Output options a chain needs after the encoder's (the GPU chain at its own size)."""
    return list(NATIVE_OUTPUT) if chain is not None and chain.native else []


def cpu_filters(prefilters: list[str], width: int, height: int, encoder: str) -> list[str]:
    """The CPU chain's -vf: the capture's own filters (ddagrab's frames brought to memory), then
    swscale fitting it into width x height (never up, even sides; bilinear: a quarter less CPU
    than bicubic for a 1440p desktop on a 1080p TV, every frame) into BT.709 limited range, and
    the frames tagged as that."""
    return list(prefilters) + [
        f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease"
        ":force_divisible_by=2:flags=bilinear:out_color_matrix=bt709:out_range=tv",
        "format=yuv420p" if encoder == "libx264" else "format=nv12",
        "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709",
    ]


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
]), re.IGNORECASE)
_LOG_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+")  # [Parsed_ddagrab_0 @ 000001d2...]


def capture_lost(stderr: str) -> Optional[str]:
    """The first line of FFmpeg's stderr that says the screen capture was lost, without FFmpeg's
    [filter @ address] prefix; None if none does (an encoder error, or just "Generic error")."""
    for line in stderr.splitlines():
        if _CAPTURE_LOST.search(line):
            return _LOG_PREFIX.sub("", line.strip()).strip()
    return None
