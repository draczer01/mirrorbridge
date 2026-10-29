"""The capture chain (mirrorbridge.mirror.capture): the sizes worked out for the graphics card,
the filters of each chain, which one is tried first, gfxcapture's input, and what FFmpeg says
when Windows takes the screen capture away (the lines are as FFmpeg 7.1 and 8.1 write them)."""

import random
import subprocess

import pytest

from mirrorbridge import ffmpeg, winmonitors
from mirrorbridge.mirror import capture, flv

CANVAS = (1920, 1080)
MAIN = ffmpeg.Monitor(0, 0, 0, 2560, 1440, True, 0, device=r"\\.\DISPLAY1")
CASE = ffmpeg.Monitor(2, 2560, 0, 1024, 600, False, 2, device=r"\\.\DISPLAY3")
# Humberto's PC as EnumDisplayMonitors lists it: the case screen first (gfxcapture's monitor_idx 0),
# then the main monitor, then the 1024x600 one; ddagrab numbers them otherwise.
HANDLES = [winmonitors.MonitorHandle(0x10003, 0, r"\\.\DISPLAY2", -682, 0),
           winmonitors.MonitorHandle(0x10001, 1, r"\\.\DISPLAY1", 0, 0),
           winmonitors.MonitorHandle(0x2A0B5F, 2, r"\\.\DISPLAY3", 2560, 0)]
# What x264 wrote for a 640x360 test pattern (as in test_mirror.py)
SPS_640x360 = bytes.fromhex("6764001eacb405017fcb8088000003000800000301e478b175")


@pytest.mark.parametrize("size, canvas, fitted", [
    ((2560, 1440), CANVAS, (1920, 1080)),
    ((1024, 600), CANVAS, (1024, 600)),  # never made bigger
    ((1920, 1080), CANVAS, (1920, 1080)),
    ((682, 2560), CANVAS, (286, 1080)),  # 287.7 wide: rounded down, not up to 288
    ((2560, 682), CANVAS, (1920, 510)),  # the case screen as ddagrab gives it (sideways)
    ((1919, 1080), CANVAS, (1918, 1080)),  # an odd side is made even by going down
    ((1921, 1081), CANVAS, (1918, 1080)),
    ((3840, 2160), (1280, 720), (1280, 720)),
    ((2560, 1440), (1919, 1079), (1918, 1078)),  # within an odd canvas too
    ((1, 1), CANVAS, (2, 2)),
])
def test_sizes_for_the_graphics_card_fit_the_tvs_screen_and_are_even(size, canvas, fitted):
    assert capture.fit_even(*size, canvas) == fitted


def test_a_fitted_size_never_goes_past_the_canvas_or_the_capture():
    rng = random.Random(7)
    for _ in range(2000):
        width, height = rng.randint(64, 8000), rng.randint(64, 8000)
        canvas = (rng.randint(320, 3840), rng.randint(240, 2160))
        out_w, out_h = capture.fit_even(width, height, canvas)
        assert out_w % 2 == out_h % 2 == 0
        assert out_w <= min(width, canvas[0]) and out_h <= min(height, canvas[1])
        # The shape is kept to within a pixel of rounding on each side.
        assert abs(out_w * height - out_h * width) <= 2 * (width + height), (width, height, canvas)


def test_capture_size_and_scaling():
    region = ffmpeg.Region(100, 200, 1280, 720)
    assert capture.capture_size("ddagrab", MAIN, region) == (1280, 720)  # a window: its rectangle
    assert capture.capture_size("ddagrab", MAIN, None) == (2560, 1440)
    assert capture.capture_size("gdigrab", None, None) is None and capture.capture_size("test", MAIN, None) is None
    assert capture.needs_scaling((2560, 1440), CANVAS) and not capture.needs_scaling((1024, 600), CANVAS)
    assert capture.needs_scaling((1919, 1080), CANVAS) and capture.needs_scaling(None, CANVAS)


def test_the_chains_and_their_filters():
    native = capture.chain_for(capture.GPU, (1024, 600), CANVAS)
    assert native.native and native.size_out == (1024, 600) and native.label == "GPU native"
    assert capture.gpu_filters(native) == [] and capture.output_args(native) == [
        "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1"]
    assert native.colour == capture.NATIVE_COLOUR and native.colour.label == "BT.601 limited range"
    scaled = capture.chain_for(capture.GPU, (2560, 1440), CANVAS)
    assert not scaled.native and scaled.size_out == (1920, 1080) and scaled.label == "GPU scale_d3d11"
    assert capture.gpu_filters(scaled) == [
        "scale_d3d11=width=1920:height=1080:format=nv12",
        "setparams=range=pc:colorspace=bt709:color_primaries=bt709:color_trc=bt709"]
    assert capture.output_args(scaled) == []
    assert scaled.colour.label == "BT.709 full range, not measured here"
    # Odd: the GPU scales it to an even size rather than handing NVENC an odd one.
    odd = capture.chain_for(capture.GPU, (1919, 1080), CANVAS)
    assert capture.gpu_filters(odd)[0] == "scale_d3d11=width=1918:height=1080:format=nv12"
    cpu = capture.chain_for(capture.CPU, (2560, 1440), CANVAS)
    assert cpu.size_out is None and cpu.label == "CPU swscale" and capture.output_args(cpu) == []
    assert cpu.colour.label == "BT.709 limited range"
    # gfxcapture: scaled by Windows into fit_even's size, RGB into NVENC as at the screen's own size,
    # repeating its last frame on a still screen (an FFmpeg that can) or giving frames only on changes.
    gfx = capture.chain_for(capture.GFX, (2560, 1440), CANVAS, repeats=True)
    assert gfx.size_out == (1920, 1080) and not gfx.native and gfx.label == "GPU gfxcapture with repeats"
    assert gfx.repeats and not gfx.changes_only
    assert gfx.colour == capture.NATIVE_COLOUR and capture.output_args(gfx) == ["-enc_time_base", "1:1000",
                                                                               *capture.NATIVE_OUTPUT]
    changes = capture.chain_for(capture.GFX, (2560, 1440), CANVAS)
    assert changes.label == "GPU gfxcapture without repeats" and changes.changes_only
    assert capture.output_args(changes) == capture.output_args(gfx) and changes.size_out == gfx.size_out
    assert capture.chain_for(capture.GFX, (682, 2560), CANVAS).size_out == (286, 1080)
    # repeats is gfxcapture's alone
    assert not capture.chain_for(capture.GPU, (2560, 1440), CANVAS, repeats=True).repeats
    assert not capture.chain_for(capture.CPU, (2560, 1440), CANVAS, repeats=True).changes_only
    for kind in (capture.GPU, capture.GFX):
        with pytest.raises(ValueError):
            capture.chain_for(kind, None, CANVAS)
    assert [capture.chain_label(c, b) for c, b in ((native, "ddagrab"), (scaled, "ddagrab"), (gfx, "ddagrab"),
                                                   (changes, "ddagrab"), (cpu, "ddagrab"), (None, "ddagrab"),
                                                   (cpu, "gdigrab"), (None, "test"))] == [
        "GPU native", "GPU scale_d3d11", "GPU gfxcapture with repeats", "GPU gfxcapture without repeats",
        "CPU swscale", "CPU swscale", "gdigrab", "test"]
    assert [capture.capture_name("ddagrab", kind) for kind in (capture.GPU, capture.GFX, capture.CPU)] == [
        "ddagrab", "gfxcapture", "ddagrab"]
    # What the tray says the encoder gets its frames through, as a session starts: the chain too
    # where it can be the graphics card's (NVENC with ddagrab), gfxcapture's saying whether it repeats.
    assert [capture.via(b, c, "h264_nvenc") for c, b in ((native, "ddagrab"), (gfx, "ddagrab"), (changes, "ddagrab"),
                                                         (cpu, "ddagrab"), (cpu, "gdigrab"), (None, "ddagrab"))] == [
        "ddagrab on the graphics card", "gfxcapture on the graphics card, repeating frames",
        "gfxcapture on the graphics card, frames only on changes", "ddagrab and the processor", "gdigrab", "ddagrab"]
    assert [capture.via("ddagrab", c, "h264_nvenc").key for c in (native, gfx, changes, cpu)] == [
        "via_gpu", "via_gfx_repeats", "via_gfx_changes", "via_cpu"]
    assert [capture.via("ddagrab", cpu, encoder) for encoder in ("libx264", "h264_qsv")] == ["ddagrab"] * 2
    # The VBV: one frame, but four for gfxcapture without repeats (a still screen's last frame is all it gets)
    assert [capture.vbv_frames(c) for c in (native, gfx, changes, cpu, None)] == [1, 1, 4, 1, 1]


def test_the_cpu_chain_writes_and_tags_bt709_limited_range():
    assert capture.cpu_filters(["hwdownload", "format=bgra"], 1920, 1080, "h264_nvenc") == [
        "hwdownload", "format=bgra",
        "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        ":flags=bilinear:out_color_matrix=bt709:out_range=tv",
        "format=nv12",
        "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"]
    x264 = capture.cpu_filters([], 640, 360, "libx264")
    assert x264[0].startswith("scale=w='min(640,iw)':h='min(360,ih)'") and x264[1:] == [
        "format=yuv420p", "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"]


@pytest.fixture()
def windows(monkeypatch):
    """Windows, with an FFmpeg that has the filters in `filters` (8.1 has gfxcapture and scale_d3d11,
    7.1 neither) and the filters' options named "filter:option" there (gfxcapture:dup_frames: an
    FFmpeg after 9.1, whose gfxcapture repeats frames), and Humberto's displays as
    EnumDisplayMonitors lists them."""
    filters = {"ddagrab", "scale_d3d11", "gfxcapture", "gfxcapture:dup_frames"}
    monkeypatch.setattr(capture.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg, "has_filter", lambda ff, name: name in filters)
    monkeypatch.setattr(ffmpeg, "filter_options", lambda ff, name: frozenset(
        found.split(":")[1] for found in filters if found.startswith(name + ":")))
    monkeypatch.setattr(capture, "BROKEN", set())
    monkeypatch.setattr(winmonitors, "monitor_handles", lambda: list(HANDLES))
    return filters


def _choose(**kw):
    args = {"encoder": "h264_nvenc", "backend": "ddagrab", "monitor": MAIN, "region": None, "canvas": CANVAS,
            "scaler": "auto"}
    return capture.choose_chain("ffmpeg.exe", **{**args, **kw})


SCALES = "from 2560x1440 to 1920x1080"
REPEATS = "repeating the last frame while the screen is still"
NO_REPEATS = ("this FFmpeg's gfxcapture has no dup_frames (FFmpeg after 9.1 has it), so a still screen would get "
              "no frames to sharpen the one that changed it")


def test_which_chain_is_tried_first(windows, monkeypatch):
    """The graphics card's at the screen's own size, gfxcapture when it must be scaled (and repeats
    frames), else the processor's; scale_d3d11 (which fails on Humberto's graphics card) only when
    turned on."""
    assert not capture.GPU_SCALED_ENABLED and capture.GFX_ENABLED
    assert _choose() == (capture.GFX, f"gfxcapture scales {SCALES}, {REPEATS}")
    assert _choose(monitor=CASE) == (capture.GPU, "1024x600 needs no scaling")
    assert _choose(region=ffmpeg.Region(0, 0, 1280, 720)) == (capture.GPU, "1280x720 needs no scaling")
    # A window bigger than the TV's screen: gfxcapture takes whole displays here, so the processor.
    assert _choose(region=ffmpeg.Region(0, 0, 2560, 1400)) == (capture.CPU, (
        "scaling from 2560x1400 to 1920x1050: gfxcapture is not used for a window; scale_d3d11 is turned off "
        "(capture.GPU_SCALED_ENABLED)"))
    assert _choose(scaler="nonsense")[0] == capture.GFX  # a hand-edited settings.json: automatic
    assert _choose(scaler="cpu") == (capture.CPU, "the Debug menu's Scaling is set to the processor")
    assert _choose(monitor=CASE, scaler="cpu")[0] == capture.CPU
    assert _choose(encoder="libx264") == (capture.CPU, "x264 (CPU) does not take frames from the graphics card yet")
    assert _choose(encoder="h264_qsv")[1] == "Intel Quick Sync does not take frames from the graphics card yet"
    assert _choose(backend="gdigrab") == (capture.CPU, "the capture is gdigrab, not ddagrab")
    assert _choose(backend="test") == (capture.CPU, "the capture is test, not ddagrab")
    assert _choose(monitor=None) == (capture.CPU, "the size of the capture is not known")
    # FFmpeg 7.1: no gfxcapture (nor scale_d3d11), so only a screen that needs no scaling.
    windows.discard("gfxcapture")
    assert _choose() == (capture.CPU, f"scaling {SCALES}: this FFmpeg has no gfxcapture (FFmpeg 8.1 and later have "
                                      "it); scale_d3d11 is turned off (capture.GPU_SCALED_ENABLED)")
    assert _choose(monitor=CASE)[0] == capture.GPU
    # scale_d3d11 turned on: in gfxcapture's place only where gfxcapture cannot be used.
    monkeypatch.setattr(capture, "GPU_SCALED_ENABLED", True)
    assert _choose() == (capture.GPU, f"scale_d3d11 {SCALES} (this FFmpeg has no gfxcapture (FFmpeg 8.1 and later "
                                      "have it))")
    windows.add("gfxcapture")
    assert _choose()[0] == capture.GFX
    windows.discard("gfxcapture:dup_frames")  # FFmpeg 8.1.1: no repeats
    assert _choose() == (capture.GPU, f"scale_d3d11 {SCALES} ({NO_REPEATS})")
    windows.add("gfxcapture:dup_frames")
    monkeypatch.setattr(capture, "GPU_SCALED_ENABLED", False)
    monkeypatch.setattr(capture, "GFX_ENABLED", False)
    assert _choose() == (capture.CPU, f"scaling {SCALES}: gfxcapture is turned off (capture.GFX_ENABLED); "
                                      "scale_d3d11 is turned off (capture.GPU_SCALED_ENABLED)")
    monkeypatch.setattr(capture, "GFX_ENABLED", True)
    # A display Windows does not list (gone since the list of displays was made)
    gone = ffmpeg.Monitor(1, 5000, 0, 2560, 1440, False, 1, device=r"\\.\DISPLAY9")
    assert _choose(monitor=gone)[1] == (rf"scaling {SCALES}: Windows does not list \\.\DISPLAY9 for gfxcapture; "
                                        "scale_d3d11 is turned off (capture.GPU_SCALED_ENABLED)")
    monkeypatch.setattr(capture.sys, "platform", "linux")
    assert _choose(monitor=CASE) == (capture.CPU, "the graphics card chain is for Windows only")


@pytest.mark.parametrize("scaler", ["auto", "gfx", "cpu"])
@pytest.mark.parametrize("fits", [True, False])
@pytest.mark.parametrize("repeats", [True, False])
@pytest.mark.parametrize("broken", [None, capture.GPU, capture.GFX])
def test_the_debug_menus_scaling_and_gfxcaptures_repeats(windows, scaler, fits, repeats, broken):
    """Scaling "auto": the graphics card at the screen's own size; to scale, gfxcapture only if it
    repeats frames (else a still slide stays as blurred as its first frame), else the processor.
    "gfx" (a test): gfxcapture to scale, repeats or not. "cpu": always the processor. A chain that
    failed on the display earlier in the run is not tried there again, whatever Scaling says."""
    if not repeats:
        windows.discard("gfxcapture:dup_frames")
    monitor = CASE if fits else MAIN
    if broken:
        capture.mark_broken("ffmpeg.exe", monitor, broken)
    if scaler == "cpu":
        expected = capture.CPU
    elif fits:
        expected = capture.CPU if broken == capture.GPU else capture.GPU
    elif broken == capture.GFX or (scaler == "auto" and not repeats):
        expected = capture.CPU
    else:
        expected = capture.GFX
    kind, why = _choose(monitor=monitor, scaler=scaler)
    assert kind == expected, why
    if kind == capture.GFX:
        assert why == (f"gfxcapture scales {SCALES}, {REPEATS}" if repeats else
                       f"gfxcapture scales {SCALES}, as the Debug menu's Scaling says, without repeats (this "
                       "FFmpeg's gfxcapture has no dup_frames): frames only as the screen changes")
    elif kind == capture.CPU and scaler == "auto" and not fits and not broken:
        assert why == f"scaling {SCALES}: {NO_REPEATS}; scale_d3d11 is turned off (capture.GPU_SCALED_ENABLED)"


def test_a_chain_that_failed_on_a_display_is_not_tried_there_again(windows):
    """For the rest of the run, each graphics card chain on its own: gfxcapture failing on the main
    monitor leaves ddagrab's frames for a display that needs no scaling, and the other way round."""
    capture.mark_broken("ffmpeg.exe", MAIN, capture.GFX)
    assert capture.is_broken("ffmpeg.exe", MAIN, capture.GFX) and not capture.is_broken("ffmpeg.exe", MAIN)
    assert _choose() == (capture.CPU, (rf"scaling {SCALES}: gfxcapture failed on \\.\DISPLAY1 earlier in this "
                                       "run; scale_d3d11 is turned off (capture.GPU_SCALED_ENABLED)"))
    assert _choose(monitor=CASE)[0] == capture.GPU
    assert capture.choose_chain("other\\ffmpeg.exe", encoder="h264_nvenc", backend="ddagrab", monitor=MAIN,
                                region=None, canvas=CANVAS, scaler="auto")[0] == capture.GFX
    capture.mark_broken("ffmpeg.exe", CASE)  # the graphics card chain at the screen's own size
    assert capture.is_broken("ffmpeg.exe", CASE, capture.GPU) and not capture.is_broken("ffmpeg.exe", CASE, capture.GFX)
    assert _choose(monitor=CASE) == (capture.CPU, r"it failed on \\.\DISPLAY3 earlier in this run")
    # A smaller TV: the case screen now needs scaling, and gfxcapture has not failed on it.
    assert _choose(monitor=CASE, canvas=(960, 540))[0] == capture.GFX


def test_gfxcapture_is_given_the_display_by_its_handle(windows, monkeypatch):
    """hmonitor= the HMONITOR whose device name is the display's, whatever ddagrab's output index;
    monitor_idx= its place in EnumDisplayMonitors' order when only its position matches."""
    assert capture.gfx_monitor(MAIN) == f"hmonitor={0x10001}"
    assert capture.gfx_monitor(CASE) == f"hmonitor={0x2A0B5F}"
    assert capture.gfx_monitor(ffmpeg.Monitor(0, 0, 0, 2560, 1440, True, 0, device=r"\\.\display1")) == (
        f"hmonitor={0x10001}")  # any letter case
    nameless = ffmpeg.Monitor(1, -682, 0, 682, 2560, False, 1)  # no device name: found by where it is
    assert capture.gfx_monitor(nameless) == "monitor_idx=0"
    assert capture.gfx_monitor(ffmpeg.Monitor(1, 9000, 0, 800, 600, device=r"\\.\DISPLAY9")) == ""
    assert capture.gfx_monitor(None) == ""

    def fails():
        raise OSError("EnumDisplayMonitors")

    monkeypatch.setattr(winmonitors, "monitor_handles", fails)
    assert capture.gfx_monitor(MAIN) == ""
    monkeypatch.setattr(capture.sys, "platform", "linux")
    monkeypatch.setattr(winmonitors, "monitor_handles", lambda: list(HANDLES))
    assert capture.gfx_monitor(MAIN) == ""


def _bufsize(args: list[str]) -> str:
    return args[args.index("-bufsize") + 1]


def test_gfxcapture_input_and_filters():
    """Scaled by Windows into the size asked for, keeping its shape. With repeats (an FFmpeg whose
    gfxcapture has dup_frames), at most 10 frames a second more than are sent, which select keeps
    to the frame rate without holding any back; without, at most 2 more, and no select."""
    assert (capture.gfx_rate(60, True), capture.gfx_rate(30, True)) == (70, 40)
    assert (capture.gfx_rate(60, False), capture.gfx_rate(30, False)) == (62, 32)
    assert capture.gfx_input("hmonitor=65537", (1920, 1080), 60, True, True) == [
        "-f", "lavfi", "-i", "gfxcapture=hmonitor=65537:width=1920:height=1080:resize_mode=scale_aspect"
        ":scale_mode=bilinear:max_framerate=70:dup_frames=1:capture_cursor=1"]
    assert capture.gfx_input("hmonitor=65537", (1920, 1080), 60, True, False) == [
        "-f", "lavfi", "-i", "gfxcapture=hmonitor=65537:width=1920:height=1080:resize_mode=scale_aspect"
        ":scale_mode=bilinear:max_framerate=62:capture_cursor=1"]
    assert capture.gfx_input("monitor_idx=0", (286, 1080), 30, False, True)[-1] == (
        "gfxcapture=monitor_idx=0:width=286:height=1080:resize_mode=scale_aspect:scale_mode=bilinear"
        ":max_framerate=40:dup_frames=1:capture_cursor=0")
    # No display: no option for it, and FFmpeg ends at once ("No capture source specified").
    assert capture.gfx_input("", (1920, 1080), 60, True, True)[-1].startswith("gfxcapture=width=1920:")
    assert capture.gfx_filters(60, True) == [
        "select='isnan(prev_selected_t)+gt(floor(t*60+0.5),floor(prev_selected_t*60+0.5))'"]
    assert capture.gfx_filters(60, False) == []
    # gfxcapture's first repeat comes 1.5 of its intervals after the last frame, so in a later slot
    # of select's than the frame before that one: the last frame of a burst that select left out
    # still reaches the TV, in its first repeat.
    for fps in (30, 60):
        assert 1.5 / capture.gfx_rate(fps, True) > 1 / fps
    # NVENC is told 70 (or 62) fps: as much more bitrate gives each frame its share at 60, and the
    # same VBV: one frame with repeats, GFX_STILL_VBV_FRAMES without.
    assert capture.gfx_kbps(8000, 60, True) == 9333 and capture.gfx_kbps(8000, 30, True) == 10667
    assert capture.gfx_kbps(8000, 60, False) == 8267 and capture.gfx_kbps(8000, 30, False) == 8533
    one_frame = _bufsize(ffmpeg.mirror_encoder_args("h264_nvenc", 8000, 60))
    assert _bufsize(ffmpeg.mirror_encoder_args("h264_nvenc", 9333, 70)) == "133k" == one_frame
    assert capture.GFX_STILL_VBV_FRAMES == 4
    assert _bufsize(ffmpeg.mirror_encoder_args("h264_nvenc", 8267, 62, vbv_frames=4)) == "533k" == _bufsize(
        ffmpeg.mirror_encoder_args("h264_nvenc", 8000, 60, vbv_frames=4))


GFX_OPTIONS = """Filter gfxcapture
  Capture graphics/screen content as a video source
    Inputs:
        none (source filter)
    Outputs:
       #0: default (video)
gfxcapture AVOptions:
   window_title      <string>     ..FV....... ECMAScript regular expression to match against the window title.
   monitor_idx       <int>        ..FV....... index of the monitor to capture (from -1 to INT_MAX) (default -1)
     window          -1           ..FV....... derive from selected window
   capture_cursor    <boolean>    ..FV....... capture mouse cursor (default true)
   max_framerate     <video_rate> ..FV....... set maximum capture frame rate (default "60")
   hmonitor          <uint64>     ..FV....... pre-existing HMONITOR handle (from 0 to UINT64_MAX) (default 0)
{dup}   resize_mode       <int>        ..FV....... capture source resize behavior (from 0 to 2) (default crop)
     crop            0            ..FV....... crop or add black bars into frame
     scale_aspect    2            ..FV....... scale source to fit initial size while preserving aspect ratio
   scale_mode        <int>        ..FV....... scaling algorithm (from 0 to 2) (default bilinear)
     bilinear        1            ..FV....... use bilinear scaling
"""
DUP_FRAMES = ("   dup_frames        <boolean>    ..FV....... repeat the last frame if the source delivers none in "
              "time (default true)\n")


def test_whether_gfxcapture_repeats_frames_is_asked_of_each_ffmpeg_once(monkeypatch):
    """`ffmpeg -h filter=gfxcapture` lists dup_frames after FFmpeg 9.1, not in 8.1.1, and 7.1 has no
    gfxcapture at all; each FFmpeg is asked once, and one that cannot be asked counts as without."""
    answers = {"ffmpeg-9.2.exe": GFX_OPTIONS.format(dup=DUP_FRAMES), "ffmpeg-8.1.1.exe": GFX_OPTIONS.format(dup=""),
               "ffmpeg-7.1.exe": "Unknown filter 'gfxcapture'.\n"}
    asked = []

    def run(args, timeout=15):
        asked.append(args[0])
        if args[0] not in answers:
            raise FileNotFoundError(args[0])
        assert args[1:] == ["-hide_banner", "-h", "filter=gfxcapture"]
        return subprocess.CompletedProcess(args, 0, answers[args[0]], "")

    monkeypatch.setattr(ffmpeg, "_run", run)
    ffmpeg.filter_options.cache_clear()
    try:
        assert ffmpeg.filter_options("ffmpeg-8.1.1.exe", "gfxcapture") == {
            "window_title", "monitor_idx", "capture_cursor", "max_framerate", "hmonitor", "resize_mode", "scale_mode"}
        assert [capture.gfx_repeats(ff) for ff in ("ffmpeg-9.2.exe", "ffmpeg-8.1.1.exe", "ffmpeg-7.1.exe",
                                                   "gone.exe")] == [True, False, False, False]
        assert [capture.gfx_repeats(ff) for ff in ("ffmpeg-9.2.exe", "ffmpeg-8.1.1.exe", "ffmpeg-7.1.exe",
                                                   "gone.exe")] == [True, False, False, False]
        assert sorted(asked) == ["ffmpeg-7.1.exe", "ffmpeg-8.1.1.exe", "ffmpeg-9.2.exe", "gone.exe"]
    finally:
        ffmpeg.filter_options.cache_clear()


def _gfx_pts(ff: str, source: str, tmp_path, repeats: bool = True) -> list[int]:
    """The frame times (ms) FFmpeg writes for a source like gfxcapture's (`source`: its times,
    on a link that says 70 frames a second, as gfxcapture's does for 60) through the GFX chain's
    filters (with repeats, or without) and output options, into FLV."""
    made = tmp_path / "vfr.flv"
    output = capture.output_args(capture.chain_for(capture.GFX, (1920, 1080), CANVAS, repeats))
    filters = capture.gfx_filters(60, repeats)
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source, "-t", "2",
                    *(["-vf", ",".join(filters)] if filters else []), "-c:v", "libx264", "-preset", "ultrafast",
                    *output, "-f", "flv", str(made)], check=True, capture_output=True, timeout=60)
    return [t.pts_ms for t in flv.FlvReader().feed(made.read_bytes()) if t.kind == flv.FRAME]


def test_gfxcapture_is_kept_to_the_frame_rate_without_the_fps_filters_repeats(tmp_path):
    """gfx_filters (with repeats) on a source like gfxcapture's on a 200 Hz screen (a frame every
    15 ms while the screen changes, none while it does not): 60 a second at most, each at its own
    time, and nothing while the screen is still (the fps filter fills that time with repeats of
    the frame before, all at once when the next frame comes, and holds each frame back until
    then)."""
    ff = _ffmpeg()
    pts = _gfx_pts(ff, "testsrc=size=160x90:rate=70,settb=1/10000000,setpts=N*150000,"
                       "select='lt(mod(t\\,1)\\,0.5)'", tmp_path)  # 0.5 s changing, 0.5 s still
    assert len(pts) == len(set(pts)) and 55 <= len(pts) <= 61  # two half seconds of at most 30 frames
    assert not [p for p in pts if 500 <= p < 1000 or p >= 1500]  # none while the screen is still
    assert {b - a for a, b in zip(pts, pts[1:]) if b - a < 400} <= {15, 30}  # on the screen's 15 ms


def test_gfxcapture_on_a_60_hz_screen_keeps_every_frame_at_its_time(tmp_path):
    """A screen refreshed 60 times a second, its frames' times a little either side of where
    gfx_filters' slots would be if they began with the first frame: all 60 a second, 16 or 17 ms
    apart as they came, not moved onto the 1/70 s that the link says (14, 15 and 28 ms apart:
    judder) nor one in two lost."""
    ff = _ffmpeg()
    pts = _gfx_pts(ff, "testsrc=size=160x90:rate=70,settb=1/10000000,"
                       "setpts=N*166667+if(mod(N\\,2)\\,-500\\,500)", tmp_path)
    assert len(pts) == 120 and {b - a for a, b in zip(pts, pts[1:])} == {16, 17}


@pytest.mark.parametrize("repeats", [True, False])
def test_the_last_change_of_a_burst_reaches_the_encoder(tmp_path, repeats):
    """A burst of changes on a 200 Hz screen (a frame every 15 ms), whose last frame (at 90 ms)
    comes in select's slot of the one before, then a still screen. With repeats, select leaves
    that frame out, but not gfxcapture's first repeat of it, 1.5 of its intervals later (at 70 a
    second, as FFmpeg's vsrc_gfxcapture_winrt.cpp stamps it), then one every 1/70 s: the screen as
    it ended comes 21 ms late. Without repeats nothing comes after it, so nothing is left out."""
    ff = _ffmpeg()
    times = "if(lt(N\\,7)\\,N*150000\\,900000+214286+(N-7)*142857)"  # in 100 ns, as gfxcapture's
    source = f"testsrc=size=160x90:rate=70:duration=1,settb=1/10000000,setpts={times}"
    if not repeats:
        source += ",select='lt(n\\,7)'"
    pts = _gfx_pts(ff, source, tmp_path, repeats)
    if repeats:
        assert pts[:8] == [0, 15, 30, 45, 60, 75, 111, 126] and 90 not in pts
    else:
        assert pts == [0, 15, 30, 45, 60, 75, 90]


def test_the_size_of_what_a_gpu_chain_made_is_checked():
    assert capture.sps_size(SPS_640x360) == (640, 360)
    assert capture.sps_size(b"\x67") is None and capture.sps_size(b"\x68\xef\x3c\xb0") is None
    native = capture.chain_for(capture.GPU, (1024, 600), CANVAS)
    assert capture.size_problem((1024, 600), native, CANVAS) == ""
    # A screen Windows turned: ddagrab gives it sideways, not as the list of displays said.
    assert capture.size_problem((600, 1024), native, CANVAS) == "the encoder made 600x1024, not the 1024x600 asked for"
    big = capture.Chain(capture.GPU, (2560, 1440), None)
    assert capture.size_problem((2560, 1440), big, CANVAS) == (
        "the encoder made 2560x1440, bigger than the TV's 1920x1080")


# Humberto's log, the two times FFmpeg stopped: a display driver removed, and signing out.
ACCESS_LOST = """[Parsed_ddagrab_0 @ 000001d2c8f0a2c0] AcquireNextFrame failed: 887a0026
[in#0/lavfi @ 000001d2c8e1b5c0] EOF timestamp not reliable
[in#0/lavfi @ 000001d2c8e1b5c0] Error during demuxing: Generic error in an external library
[vost#0:0/h264_nvenc @ 000001d2c8f9e100] Error submitting a packet to the muxer: Broken pipe"""


@pytest.mark.parametrize("stderr, lost", [
    (ACCESS_LOST, "AcquireNextFrame failed: 887a0026"),
    ("[Parsed_ddagrab_0 @ 0000020a] AcquireNextFrame failed: 887a0007", "AcquireNextFrame failed: 887a0007"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Output parameters changed!", "Output parameters changed!"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Desktop duplication access denied", "Desktop duplication access denied"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Too many open duplication sessions", "Too many open duplication sessions"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Failed to enumerate DXGI output 1", "Failed to enumerate DXGI output 1"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Failed getting output description", "Failed getting output description"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Selected output not supported", "Selected output not supported"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Failed duplicating output", "Failed duplicating output"),
    ("[Parsed_ddagrab_0 @ 0x55d0] Requested output format unavailable.", "Requested output format unavailable."),
    ("[gdigrab @ 000001] Failed to capture image (error 6)", "Failed to capture image (error 6)"),
    ("  acquirenextframe FAILED: 887A0026  ", "acquirenextframe FAILED: 887A0026"),  # any letter case
    # gfxcapture (vsrc_gfxcapture_winrt.cpp): a DXGI error in one of its calls, and a display going
    # or coming back as it starts
    ("[Parsed_gfxcapture_0 @ 0000016f] (wgctx->frame_pool->TryGetNextFrame(&capture_frame)) failed: 0x887A0005",
     "(wgctx->frame_pool->TryGetNextFrame(&capture_frame)) failed: 0x887A0005"),
    ("[Parsed_gfxcapture_0 @ 0x1] (dev->CreateRenderTargetView( reinterpret_cast<ID3D11Resource*>(frame->data[0]), "
     "&target_desc, &rtv)) failed: 0x887a0007",
     "(dev->CreateRenderTargetView( reinterpret_cast<ID3D11Resource*>(frame->data[0]), &target_desc, &rtv)) "
     "failed: 0x887a0007"),
    ("[Parsed_gfxcapture_0 @ 0x1] Failed to setup graphics capture for monitor (0x80070057)\n"
     "[Parsed_gfxcapture_0 @ 0x1] Failed to setup graphics capture",
     "Failed to setup graphics capture for monitor (0x80070057)"),
    ("[Parsed_gfxcapture_0 @ 0x1] Failed to start graphics capture session (0x80004005)",
     "Failed to start graphics capture session (0x80004005)"),
    ("[Parsed_gfxcapture_0 @ 0x1] No capture source specified\n[Parsed_gfxcapture_0 @ 0x1] Failed to find capture "
     "source", "Failed to find capture source"),
    # Not the capture: the encoder, or only what follows a loss
    ("[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: out of memory (10): (no details)\n"
     "[vost#0:0/h264_nvenc @ 0x2] Error while opening encoder", None),
    ("[in#0/lavfi @ 0x1] Error during demuxing: Generic error in an external library", None),
    ("[h264_nvenc @ 0x1] The minimum required Nvidia driver for nvenc is 570.0 or newer", None),
    ("[Parsed_gfxcapture_0 @ 0x1] Failed to create D3D11VA device.", None),
    ("[Parsed_gfxcapture_0 @ 0x1] (wgctx->frame_pool->TryGetNextFrame(&capture_frame)) failed: 0x80004005", None),
    ("", None),
])
def test_ffmpeg_saying_the_screen_capture_was_lost(stderr, lost):
    assert capture.capture_lost(stderr) == lost
    assert capture.capture_lost(stderr, kind=capture.GFX, returncode=1) == lost


def test_gfxcapture_ending_its_video_is_the_capture_lost():
    """Windows closing the capture (the display gone): gfxcapture says so only as information,
    which FFmpeg does not show at -loglevel warning, and ends its video, so FFmpeg ends by itself
    with exit code 0. Only gfxcapture's FFmpeg says it that way."""
    assert capture.capture_lost("", kind=capture.GFX, returncode=0) == "Capture item closed (FFmpeg ended by itself)"
    assert capture.capture_lost(ACCESS_LOST, kind=capture.GFX, returncode=0) == "AcquireNextFrame failed: 887a0026"
    assert capture.capture_lost("", kind=capture.GFX, returncode=1) is None
    assert capture.capture_lost("", kind=capture.GFX) is None
    assert capture.capture_lost("", kind=capture.GPU, returncode=0) is None
    assert capture.capture_lost("", kind=capture.CPU, returncode=0) is None


def _ffmpeg() -> str:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264"):
        pytest.skip("this FFmpeg has no libx264")
    return ff


def _vui(ff: str, made) -> dict:
    """The colour description in the SPS of the H.264 in `made` (trace_headers)."""
    trace = subprocess.run([ff, "-hide_banner", "-loglevel", "debug", "-i", str(made), "-c", "copy",
                            "-bsf:v", "trace_headers", "-f", "null", "-"], capture_output=True, text=True, timeout=60)
    fields = {}
    for line in trace.stderr.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-4] in ("colour_primaries", "transfer_characteristics", "matrix_coefficients",
                                             "video_full_range_flag"):
            fields.setdefault(parts[-4], int(parts[-1]))  # [trace_headers @ 0x..] 87  colour_primaries  00000001 = 1
    return fields


def test_the_native_chains_stream_says_bt709_primaries_and_transfer(tmp_path):
    """NATIVE_OUTPUT rewrites the colour description in the SPS whatever the encoder; here x264
    is given frames tagged as ddagrab tags them (sRGB transfer)."""
    ff = _ffmpeg()
    made = tmp_path / "native.flv"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=30,format=nv12,setparams=color_trc=iec61966-2-1:color_primaries=bt709"
                    ":colorspace=bt470bg:range=tv", "-frames:v", "5", "-c:v", "libx264", *capture.NATIVE_OUTPUT,
                    "-f", "flv", str(made)], check=True, capture_output=True, timeout=60)
    tags = flv.FlvReader().feed(made.read_bytes())
    assert tags[0].kind == flv.CONFIG and sum(t.kind == flv.FRAME for t in tags) == 5
    fields = _vui(ff, made)
    assert {name: fields.get(name) for name in ("colour_primaries", "transfer_characteristics",
                                                "matrix_coefficients")} == {
        "colour_primaries": 1, "transfer_characteristics": 1, "matrix_coefficients": 5}


@pytest.mark.parametrize("bt709, red", [(True, (63, 102, 240)), (False, (82, 90, 240))])
def test_the_processors_chain_makes_and_says_bt709_limited_range(tmp_path, bt709, red):
    """cpu_filters on the screen's BGRA, as ddagrab's frames come to memory: pure red is BT.709's
    limited-range red, and the stream says BT.709 and limited range; without bt709 (Google Cast),
    swscale's own BT.601, and nothing said, as before."""
    ff = _ffmpeg()
    source = "color=c=red:size=64x64:rate=30,format=bgra"
    filters = ",".join(capture.cpu_filters(["format=bgra"], 64, 64, "libx264", bt709=bt709))
    raw = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source, "-frames:v", "1",
                          "-vf", filters, "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"],
                         check=True, capture_output=True, timeout=60).stdout
    y, u, v = raw[64 * 64 // 2], raw[64 * 64 + 16 * 32 + 16], raw[64 * 64 * 5 // 4 + 16 * 32 + 16]
    assert all(abs(got - want) <= 1 for got, want in zip((y, u, v), red)), (y, u, v)
    made = tmp_path / "cpu.flv"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source, "-frames:v", "5",
                    "-vf", filters, "-c:v", "libx264", "-f", "flv", str(made)], check=True, capture_output=True,
                   timeout=60)
    fields = _vui(ff, made)
    if bt709:
        assert fields == {"video_full_range_flag": 0, "colour_primaries": 1, "transfer_characteristics": 1,
                          "matrix_coefficients": 1}
    else:
        assert fields.get("matrix_coefficients", 2) == 2  # unspecified, or no colour description at all
