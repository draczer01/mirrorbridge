"""The capture chain (mirrorbridge.mirror.capture): the sizes worked out for the graphics card,
the filters of each chain, when the GPU one can be used, and what FFmpeg says when Windows takes
the screen capture away (the lines are as FFmpeg 7.1 and 8.1 write them)."""

import random
import subprocess

import pytest

from mirrorbridge import ffmpeg
from mirrorbridge.mirror import capture, flv

CANVAS = (1920, 1080)
MAIN = ffmpeg.Monitor(0, 0, 0, 2560, 1440, True, 0, device=r"\\.\DISPLAY1")
CASE = ffmpeg.Monitor(2, 2560, 0, 1024, 600, False, 2, device=r"\\.\DISPLAY3")
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
    with pytest.raises(ValueError):
        capture.chain_for(capture.GPU, None, CANVAS)
    assert [capture.chain_label(c, b) for c, b in ((native, "ddagrab"), (scaled, "ddagrab"), (cpu, "ddagrab"),
                                                   (None, "ddagrab"), (cpu, "gdigrab"), (None, "test"))] == [
        "GPU native", "GPU scale_d3d11", "CPU swscale", "CPU swscale", "gdigrab", "test"]


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
    """Windows, with an FFmpeg that has the filters in `filters` (8.1 has scale_d3d11, 7.1 not)."""
    filters = {"ddagrab", "scale_d3d11"}
    monkeypatch.setattr(capture.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg, "has_filter", lambda ff, name: name in filters)
    monkeypatch.setattr(capture, "BROKEN", set())
    return filters


def _possible(**kw):
    args = {"encoder": "h264_nvenc", "backend": "ddagrab", "monitor": MAIN, "region": None, "canvas": CANVAS,
            "scaler": "auto"}
    return capture.gpu_possible("ffmpeg.exe", **{**args, **kw})


def test_when_the_graphics_card_chain_can_be_used(windows, monkeypatch):
    assert _possible() == (True, "scale_d3d11 from 2560x1440 to 1920x1080")
    assert _possible(monitor=CASE) == (True, "1024x600 needs no scaling")
    assert _possible(region=ffmpeg.Region(0, 0, 1280, 720)) == (True, "1280x720 needs no scaling")
    assert _possible(scaler="nonsense")[0]  # a hand-edited settings.json: automatic
    assert _possible(scaler="cpu") == (False, "the Debug menu's Scaling is set to the processor")
    assert _possible(encoder="libx264") == (False, "x264 (CPU) does not take frames from the graphics card yet")
    assert _possible(encoder="h264_qsv")[1] == "Intel Quick Sync does not take frames from the graphics card yet"
    assert _possible(backend="gdigrab") == (False, "the capture is gdigrab, not ddagrab")
    assert _possible(monitor=None) == (False, "the size of the capture is not known")
    # FFmpeg 7.1: no scale_d3d11, so only a screen that needs no scaling.
    windows.discard("scale_d3d11")
    assert _possible() == (False, "this FFmpeg has no scale_d3d11 (FFmpeg 8.0 and later have it)")
    assert _possible(monitor=CASE)[0]
    windows.add("scale_d3d11")
    monkeypatch.setattr(capture, "GPU_SCALED_ENABLED", False)
    assert _possible() == (False, "scaling on the graphics card is turned off (capture.GPU_SCALED_ENABLED)")
    assert _possible(monitor=CASE)[0]
    monkeypatch.setattr(capture, "GPU_SCALED_ENABLED", True)
    # Once it failed on a display, not again there for the rest of the run; the others still can.
    capture.mark_broken("ffmpeg.exe", MAIN)
    assert capture.is_broken("ffmpeg.exe", MAIN) and not capture.is_broken("ffmpeg.exe", CASE)
    assert _possible() == (False, r"it failed on \\.\DISPLAY1 earlier in this run")
    assert _possible(monitor=CASE)[0] and not capture.is_broken("other\\ffmpeg.exe", MAIN)
    monkeypatch.setattr(capture.sys, "platform", "linux")
    assert _possible(monitor=CASE) == (False, "the graphics card chain is for Windows only")


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
    # Not the capture: the encoder, or only what follows a loss
    ("[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: out of memory (10): (no details)\n"
     "[vost#0:0/h264_nvenc @ 0x2] Error while opening encoder", None),
    ("[in#0/lavfi @ 0x1] Error during demuxing: Generic error in an external library", None),
    ("[h264_nvenc @ 0x1] The minimum required Nvidia driver for nvenc is 570.0 or newer", None),
    ("", None),
])
def test_ffmpeg_saying_the_screen_capture_was_lost(stderr, lost):
    assert capture.capture_lost(stderr) == lost


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
