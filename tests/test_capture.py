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
    # gfxcapture: scaled by Windows into fit_even's size, RGB into NVENC as at the screen's own size.
    gfx = capture.chain_for(capture.GFX, (2560, 1440), CANVAS)
    assert gfx.size_out == (1920, 1080) and not gfx.native and gfx.label == "GPU gfxcapture"
    assert gfx.colour == capture.NATIVE_COLOUR and capture.output_args(gfx) == list(capture.NATIVE_OUTPUT)
    assert capture.chain_for(capture.GFX, (682, 2560), CANVAS).size_out == (286, 1080)
    for kind in (capture.GPU, capture.GFX):
        with pytest.raises(ValueError):
            capture.chain_for(kind, None, CANVAS)
    assert [capture.chain_label(c, b) for c, b in ((native, "ddagrab"), (scaled, "ddagrab"), (gfx, "ddagrab"),
                                                   (cpu, "ddagrab"), (None, "ddagrab"), (cpu, "gdigrab"),
                                                   (None, "test"))] == [
        "GPU native", "GPU scale_d3d11", "GPU gfxcapture", "CPU swscale", "CPU swscale", "gdigrab", "test"]
    assert [capture.capture_name("ddagrab", kind) for kind in (capture.GPU, capture.GFX, capture.CPU)] == [
        "ddagrab", "gfxcapture", "ddagrab"]


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
    7.1 neither), and Humberto's displays as EnumDisplayMonitors lists them."""
    filters = {"ddagrab", "scale_d3d11", "gfxcapture"}
    monkeypatch.setattr(capture.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg, "has_filter", lambda ff, name: name in filters)
    monkeypatch.setattr(capture, "BROKEN", set())
    monkeypatch.setattr(winmonitors, "monitor_handles", lambda: list(HANDLES))
    return filters


def _choose(**kw):
    args = {"encoder": "h264_nvenc", "backend": "ddagrab", "monitor": MAIN, "region": None, "canvas": CANVAS,
            "scaler": "auto"}
    return capture.choose_chain("ffmpeg.exe", **{**args, **kw})


SCALES = "from 2560x1440 to 1920x1080"


def test_which_chain_is_tried_first(windows, monkeypatch):
    """The graphics card's at the screen's own size, gfxcapture when it must be scaled, else the
    processor's; scale_d3d11 (which fails on Humberto's graphics card) only when turned on."""
    assert not capture.GPU_SCALED_ENABLED and capture.GFX_ENABLED
    assert _choose() == (capture.GFX, f"gfxcapture scales {SCALES}")
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


def test_gfxcapture_input_and_filters():
    """Scaled by Windows into the size asked for, keeping its shape, at most 10 frames a second more
    than are sent, which select keeps to the frame rate without holding any back."""
    assert capture.gfx_rate(60) == 70 and capture.gfx_rate(30) == 40
    assert capture.gfx_input("hmonitor=65537", (1920, 1080), 60, True) == [
        "-f", "lavfi", "-i", "gfxcapture=hmonitor=65537:width=1920:height=1080:resize_mode=scale_aspect"
        ":scale_mode=bilinear:max_framerate=70:capture_cursor=1"]
    assert capture.gfx_input("monitor_idx=0", (286, 1080), 30, False)[-1] == (
        "gfxcapture=monitor_idx=0:width=286:height=1080:resize_mode=scale_aspect:scale_mode=bilinear"
        ":max_framerate=40:capture_cursor=0")
    # No display: no option for it, and FFmpeg ends at once ("No capture source specified").
    assert capture.gfx_input("", (1920, 1080), 60, True)[-1].startswith("gfxcapture=width=1920:")
    assert capture.gfx_filters(60) == ["select='isnan(prev_selected_t)+gt(floor(t*60),floor(prev_selected_t*60))'"]
    # NVENC is told 70 fps: as much more bitrate gives each frame its share at 60, and the same VBV.
    assert capture.gfx_kbps(8000, 60) == 9333 and capture.gfx_kbps(8000, 30) == 10667
    assert "-bufsize" in (args := ffmpeg.mirror_encoder_args("h264_nvenc", 9333, 70))
    assert args[args.index("-bufsize") + 1] == "133k" == ffmpeg.mirror_encoder_args("h264_nvenc", 8000, 60)[
        ffmpeg.mirror_encoder_args("h264_nvenc", 8000, 60).index("-bufsize") + 1]


def test_gfxcapture_is_kept_to_the_frame_rate_without_repeats(tmp_path):
    """gfx_filters on a source like gfxcapture's (a frame every 15 ms while the screen changes,
    none while it does not): 60 a second at most, each with its own time, and nothing while the
    screen is still (the fps filter fills that time with repeats of the frame before, all at once
    when the next frame comes, and holds each frame back until then)."""
    ff = _ffmpeg()
    made = tmp_path / "vfr.flv"
    source = ("testsrc=size=160x90:rate=200,settb=1/10000000,"
              "select='lt(mod(t\\,1)\\,0.5)*not(mod(n\\,3))'")  # 0.5 s changing, 0.5 s still
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source, "-t", "2",
                    "-vf", ",".join(capture.gfx_filters(60)), "-c:v", "libx264", "-preset", "ultrafast",
                    "-f", "flv", str(made)], check=True, capture_output=True, timeout=60)
    pts = [t.pts_ms for t in flv.FlvReader().feed(made.read_bytes()) if t.kind == flv.FRAME]
    assert len(pts) == len(set(pts)) and 55 <= len(pts) <= 61  # two half seconds of at most 30 frames
    assert not [p for p in pts if 500 <= p < 1000 or p >= 1500]  # none while the screen is still
    assert min(b - a for a, b in zip(pts, pts[1:])) >= 15


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
    trace = subprocess.run([ff, "-hide_banner", "-loglevel", "debug", "-i", str(made), "-c", "copy",
                            "-bsf:v", "trace_headers", "-f", "null", "-"], capture_output=True, text=True, timeout=60)
    fields = {}
    for line in trace.stderr.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-4] in ("colour_primaries", "transfer_characteristics", "matrix_coefficients"):
            fields.setdefault(parts[-4], int(parts[-1]))  # [trace_headers @ 0x..] 87  colour_primaries  00000001 = 1
    assert fields == {"colour_primaries": 1, "transfer_characteristics": 1, "matrix_coefficients": 5}
