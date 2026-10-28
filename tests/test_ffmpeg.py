import shutil
import subprocess
import time

import pytest

from aircast import audio, ffmpeg
from aircast.config import Settings

FF = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FF is None, reason="ffmpeg not installed")


def _ffprobe() -> str:
    """ffprobe next to ffmpeg (only the file name changes, not folders like C:\\ffmpeg)."""
    from pathlib import Path

    beside = Path(FF).with_name(Path(FF).name.replace("ffmpeg", "ffprobe"))
    found = str(beside) if beside.exists() else shutil.which("ffprobe")
    if not found:
        pytest.skip("ffprobe not installed")
    return found


def _cmd(tmp_path, **kw):
    settings = Settings(**{k: v for k, v in kw.items() if k in Settings.__dataclass_fields__})
    return ffmpeg.build_command(
        "ffmpeg", settings, tmp_path,
        encoder=kw.get("encoder", "libx264"), backend=kw.get("backend", "test"),
        monitor=kw.get("monitor"), audio_port=kw.get("audio_port"), for_apple=kw.get("for_apple", True),
    )


def test_ddagrab_uses_monitor_and_hwdownload(tmp_path):
    mon = ffmpeg.Monitor(1, 1920, 0, 2560, 1440, dxgi_output=1)
    cmd = _cmd(tmp_path, backend="ddagrab", encoder="h264_nvenc", monitor=mon)
    assert "ddagrab=output_idx=1:framerate=30:draw_mouse=1" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("hwdownload,format=bgra,scale=")
    assert vf.endswith("format=nv12")
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"


def test_gdigrab_window_and_monitor_offsets(tmp_path):
    cmd = _cmd(tmp_path, backend="gdigrab", window_title="Notepad")
    assert "title=Notepad" in cmd
    mon = ffmpeg.Monitor(1, -1280, 0, 1280, 1024)
    cmd = _cmd(tmp_path, backend="gdigrab", monitor=mon)
    assert cmd[cmd.index("-offset_x") + 1] == "-1280"
    assert cmd[cmd.index("-video_size") + 1] == "1280x1024"


def test_latency_controls_segment_and_gop(tmp_path):
    cmd = _cmd(tmp_path, latency_mode="smooth", framerate=60)
    assert cmd[cmd.index("-hls_time") + 1] == "2.0"
    assert cmd[cmd.index("-g") + 1] == "120"
    cmd = _cmd(tmp_path, latency_mode="low", framerate=30)
    assert cmd[cmd.index("-g") + 1] == "15"


def test_apple_tv_stream_is_unchanged(tmp_path):
    """Apple TVs get exactly the HLS output they always had."""
    for mode, seg in (("low", "0.5"), ("balanced", "1.0"), ("smooth", "2.0")):
        cmd = _cmd(tmp_path, latency_mode=mode)
        assert cmd[cmd.index("-f", cmd.index("-map")):] == [
            "-f", "hls", "-hls_time", seg, "-hls_list_size", "6",
            "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
            "-hls_allow_cache", "0", "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(tmp_path / "seg_%05d.ts"), str(tmp_path / "stream.m3u8"),
        ]
        assert cmd[cmd.index("-force_key_frames") + 1] == f"expr:gte(t,n_forced*{seg})"


def test_other_tvs_get_two_second_segments_a_long_window_and_a_master_playlist(tmp_path):
    cmd = _cmd(tmp_path, latency_mode="low", framerate=30, for_apple=False)
    assert cmd[cmd.index("-hls_time") + 1] == "2.0"
    assert cmd[cmd.index("-g") + 1] == "60"
    assert cmd[cmd.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2.0)"
    assert cmd[cmd.index("-hls_list_size") + 1] == "6"  # 6 x 2 s = 12 s
    assert cmd[cmd.index("-hls_delete_threshold") + 1] == "6"
    assert "+program_date_time" in cmd[cmd.index("-hls_flags") + 1]
    assert cmd[cmd.index("-master_pl_name") + 1] == "master.m3u8"
    assert cmd[-1] == str(tmp_path / "stream.m3u8")
    assert ffmpeg.segment_seconds(Settings(latency_mode="smooth"), for_apple=False) == 2.0


def test_audio_input_or_silence(tmp_path):
    assert any("anullsrc" in a for a in _cmd(tmp_path))
    cmd = _cmd(tmp_path, audio_port=5555)
    assert "tcp://127.0.0.1:5555?listen=1" in cmd
    assert not any("anullsrc" in a for a in cmd)


def test_native_resolution_only_rounds_to_even(tmp_path):
    vf = (c := _cmd(tmp_path, output_height=0))[c.index("-vf") + 1]
    assert "scale='trunc(iw/2)*2':'trunc(ih/2)*2'" in vf  # H.264 4:2:0 needs even sizes


def test_scaled_height_is_even_and_never_upscaled(tmp_path):
    vf = (c := _cmd(tmp_path, output_height=1080))[c.index("-vf") + 1]
    assert "scale=-2:'trunc(min(1080,ih)/2)*2'" in vf


def test_window_region_crops_ddagrab_relative_to_its_display(tmp_path):
    from aircast.ffmpeg import Monitor, Region, build_command
    from aircast.config import Settings

    mon = Monitor(1, left=1920, top=0, width=2560, height=1440, dxgi_output=1)
    cmd = build_command("ffmpeg", Settings(window_title="Video"), tmp_path, encoder="libx264",
                        backend="ddagrab", monitor=mon, region=Region(2020, 50, 1280, 720))
    src = cmd[cmd.index("lavfi") + 2]
    assert "output_idx=1" in src and "offset_x=100:offset_y=50:video_size=1280x720" in src


def test_window_region_with_gdigrab_uses_desktop_coordinates(tmp_path):
    from aircast.ffmpeg import Monitor, Region, build_command
    from aircast.config import Settings

    mon = Monitor(0, 0, 0, 1920, 1080)
    cmd = build_command("ffmpeg", Settings(window_title="Video"), tmp_path, encoder="libx264",
                        backend="gdigrab", monitor=mon, region=Region(10, 20, 800, 600))
    assert cmd[cmd.index("-offset_x") + 1] == "10" and cmd[cmd.index("-video_size") + 1] == "800x600"
    assert cmd[cmd.index("-i", cmd.index("gdigrab")) + 1] == "desktop"


def test_window_region_picks_display_and_clips_to_even(monkeypatch):
    import aircast.ffmpeg as ff
    from aircast import winmonitors

    monkeypatch.setattr(ff.sys, "platform", "win32")
    left, right = ff.Monitor(0, 0, 0, 1920, 1080, True), ff.Monitor(1, 1920, 0, 1920, 1080)
    # Mostly on the right display, hanging 101 px off its bottom edge.
    monkeypatch.setattr(winmonitors, "window_rect", lambda _t: (1800, 200, 1001, 981))
    mon, region = ff.window_region("App", [left, right])
    assert mon is right
    assert (region.left, region.top, region.width, region.height) == (1920, 200, 880, 880)
    monkeypatch.setattr(winmonitors, "window_rect", lambda _t: None)
    with pytest.raises(RuntimeError, match="not on screen"):
        ff.window_region("App", [left, right])


def test_window_without_region_falls_back_to_gdigrab(monkeypatch):
    import aircast.ffmpeg as ff
    from aircast.config import Settings

    monkeypatch.setattr(ff.sys, "platform", "win32")
    monkeypatch.setattr(ff, "has_filter", lambda *_a: True)
    mon = ff.Monitor(0, 0, 0, 1920, 1080)
    assert ff.pick_capture_backend("ffmpeg", Settings(), mon) == "ddagrab"
    assert ff.pick_capture_backend("ffmpeg", Settings(window_title="X"), mon) == "gdigrab"
    assert ff.pick_capture_backend("ffmpeg", Settings(window_title="X"), mon, ff.Region(0, 0, 64, 64)) == "ddagrab"
    other_gpu = ff.Monitor(1, 1920, 0, 1920, 1080, dxgi_output=None)
    assert ff.pick_capture_backend("ffmpeg", Settings(capture_backend="ddagrab"), other_gpu) == "gdigrab"


@needs_ffmpeg
def test_libx264_is_detected():
    assert ffmpeg.encoder_works(FF, "libx264")
    assert ffmpeg.pick_encoder(FF, "auto") in ffmpeg.ENCODER_PREFERENCE


def _wait_segments(path, n, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text().count("#EXTINF") >= n:
            return path.read_text()
        time.sleep(0.2)
    raise AssertionError("no HLS segments produced")


@needs_ffmpeg
def test_pipeline_produces_live_hls_with_audio(tmp_path):
    """Run the real command with the test source and PCM pushed over TCP like system audio."""
    import math
    import struct

    port = audio.free_port()
    settings = Settings(latency_mode="balanced", output_height=720)
    cmd = ffmpeg.build_command(FF, settings, tmp_path, encoder="libx264", backend="test", audio_port=port)
    proc = ffmpeg.popen(cmd)
    phase = [0]

    def tone(frames):
        out = bytearray()
        for _ in range(frames):
            v = 0.2 * math.sin(phase[0])
            phase[0] += 2 * math.pi * 440 / audio.SAMPLE_RATE
            out += struct.pack("<ff", v, v)
        time.sleep(frames / audio.SAMPLE_RATE)
        return bytes(out)

    feeder = audio.LoopbackFeeder(port, source=tone)
    feeder.start()
    try:
        text = _wait_segments(tmp_path / "stream.m3u8", 3)
        assert "#EXT-X-ENDLIST" not in text  # live playlist
        seg = sorted(tmp_path.glob("seg_*.ts"))[0]
        probe = subprocess.run(
            [_ffprobe(), "-v", "error", "-show_entries", "stream=codec_name,height",
             "-of", "csv=p=0", str(seg)], capture_output=True, text=True, check=True,
        ).stdout
        assert "h264,720" in probe and "aac" in probe
    finally:
        feeder.stop()
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(10)
    assert feeder.error is None


@needs_ffmpeg
def test_stream_for_other_tvs_declares_codecs_and_program_date_time(tmp_path):
    """FFmpeg's master playlist must carry what Apple's rules for AirPlay 2 TVs ask for."""
    import re

    from aircast.hls_server import complete_variants, fix_target_duration

    settings = Settings(latency_mode="low", output_height=360)
    cmd = ffmpeg.build_command(FF, settings, tmp_path, encoder="libx264", backend="test", for_apple=False)
    proc = ffmpeg.popen(cmd)
    try:
        media = _wait_segments(tmp_path / "stream.m3u8", 2)
        master = (tmp_path / "master.m3u8").read_text()
    finally:
        proc.stdin.write(b"q")
        proc.stdin.flush()
        proc.wait(10)
    variant = next(line for line in master.splitlines() if line.startswith("#EXT-X-STREAM-INF"))
    assert re.search(r'CODECS="avc1\.64[0-9a-f]{4},mp4a\.40\.2"', variant), variant
    assert re.search(r"BANDWIDTH=\d+", variant) and "RESOLUTION=640x360" in variant
    # FFmpeg leaves these out while the stream is live; HlsServer adds them as it serves it.
    assert "FRAME-RATE=" not in variant
    average = (settings.video_bitrate_kbps + settings.audio_bitrate_kbps) * 1000
    served = complete_variants(variant.encode(), settings.framerate, average).decode()
    peak = int(re.search(r"[:,]BANDWIDTH=(\d+)", served).group(1))
    assert served.endswith(f",FRAME-RATE=30.000,AVERAGE-BANDWIDTH={average}") and average <= peak
    assert "#EXT-X-PROGRAM-DATE-TIME:" in media
    assert b"#EXT-X-TARGETDURATION:2\n" in fix_target_duration(media.encode())
