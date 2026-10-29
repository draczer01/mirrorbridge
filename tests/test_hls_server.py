import logging

import aiohttp

from mirrorbridge.hls_server import HlsServer


async def test_serves_playlist_and_blocks_other_files(tmp_path):
    (tmp_path / "stream.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "seg_00001.ts").write_bytes(b"\x47" * 188)
    (tmp_path / "secret.txt").write_text("nope")
    server = HlsServer(tmp_path, "127.0.0.1", 0)
    await server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(server.playlist_url) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith("application/vnd.apple.mpegurl")
                assert "no-cache" in resp.headers["Cache-Control"]
            async with http.get(f"{base}/seg_00001.ts") as resp:
                assert resp.status == 200 and resp.content_type == "video/mp2t"
            for bad in ("secret.txt", "missing.ts", "..%2Fsecret.txt"):
                async with http.get(f"{base}/{bad}") as resp:
                    assert resp.status == 404
    finally:
        await server.close()
    assert server.requests == 2


def test_sub_second_target_duration_is_raised_to_one():
    from mirrorbridge.hls_server import fix_target_duration

    assert b"#EXT-X-TARGETDURATION:1\n" in fix_target_duration(b"#EXTM3U\n#EXT-X-TARGETDURATION:0\n")
    assert b"#EXT-X-TARGETDURATION:2\n" in fix_target_duration(b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n")


async def test_served_playlist_has_valid_target_duration(tmp_path):
    (tmp_path / "stream.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:0\n#EXTINF:0.5,\nseg_00000.ts\n")
    server = HlsServer(tmp_path, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(server.playlist_url) as resp:
                assert "#EXT-X-TARGETDURATION:1" in await resp.text()
    finally:
        await server.close()


async def test_records_each_client_and_its_first_playlist_and_segment(tmp_path, caplog):
    (tmp_path / "master.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "seg_00001.ts").write_bytes(b"\x47" * 188)
    server = HlsServer(tmp_path, "127.0.0.1", 0)
    await server.start()
    try:
        assert server.activity().requests == 0 and server.activity().first_playlist_at is None
        async with aiohttp.ClientSession(headers={"User-Agent": "AppleCoreMedia/1.0.0"}) as http:
            with caplog.at_level(logging.INFO, logger="mirrorbridge.hls_server"):
                async with http.get(server.url("master.m3u8")) as resp:
                    assert resp.status == 200
                async with http.get(server.url("seg_00001.ts")) as resp:
                    assert resp.status == 200
    finally:
        await server.close()
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0] == "127.0.0.1 started fetching the stream (User-Agent: AppleCoreMedia/1.0.0)"
    assert lines[1] == "127.0.0.1 fetched its first playlist, master.m3u8"
    assert lines[2].startswith("127.0.0.1 fetched its first video segment, seg_00001.ts, ")
    assert lines[2].endswith(" s after its first playlist") and len(lines) == 3
    tv = server.activity("127.0.0.1")
    assert tv.requests == 2 and tv.user_agent == "AppleCoreMedia/1.0.0"
    assert tv.first_playlist_at <= tv.first_segment_at <= tv.last_request_at
    assert server.activity("192.168.1.40").requests == 0
    assert server.activity() == tv  # only one client
    assert server.url("master.m3u8").endswith(f":{server.port}/master.m3u8")


async def test_requests_it_cannot_serve_are_logged_and_counted_but_are_not_fetches(tmp_path, caplog):
    """A TV that reaches MirrorBridge but asks for the wrong thing must not look like a firewall problem."""
    (tmp_path / "stream.m3u8").write_text("#EXTM3U\n")
    server = HlsServer(tmp_path, "127.0.0.1", 0)
    await server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "ExoPlayerLib/2.18"}) as http:
            with caplog.at_level(logging.INFO, logger="mirrorbridge.hls_server"):
                for method, path, status in (("GET", "/live/stream.m3u8", 404), ("GET", "/live/stream.m3u8", 404),
                                             ("GET", "/seg_00009.ts", 404), ("POST", "/stream.m3u8", 405)):
                    async with http.request(method, base + path) as resp:
                        assert resp.status == status
    finally:
        await server.close()
    tv = server.activity("127.0.0.1")
    assert tv.requests == 0 and tv.first_playlist_at is None  # nothing was fetched
    assert tv.missing == 4 and tv.missing_path == "/live/stream.m3u8"
    agent = "User-Agent: ExoPlayerLib/2.18"
    assert [r.getMessage() for r in caplog.records] == [  # once per request, with the player that asked
        f"127.0.0.1 asked for GET /live/stream.m3u8, which MirrorBridge does not serve (HTTP 404, {agent})",
        f"127.0.0.1 asked for GET /seg_00009.ts, which MirrorBridge does not serve (HTTP 404, {agent})",
        f"127.0.0.1 asked for POST /stream.m3u8, which MirrorBridge does not serve (HTTP 405, {agent})",
    ]


async def test_a_player_in_a_web_page_may_fetch_the_stream(tmp_path, caplog):
    """CORS: some TVs play AirPlay video in a web page, which asks first and must read errors too."""
    (tmp_path / "stream.m3u8").write_text("#EXTM3U\n")
    server = HlsServer(tmp_path, "127.0.0.1", 0)
    await server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "WebKit"}) as http:
            with caplog.at_level(logging.INFO, logger="mirrorbridge.hls_server"):
                for _ in range(2):
                    async with http.options(base + "/stream.m3u8", headers={
                        "Origin": "https://receiver.example", "Access-Control-Request-Method": "GET",
                    }) as resp:
                        assert resp.status == 204
                        assert resp.headers["Access-Control-Allow-Origin"] == "*"
                        assert "GET" in resp.headers["Access-Control-Allow-Methods"]
            async with http.get(base + "/stream.m3u8") as resp:
                assert resp.status == 200 and resp.headers["Access-Control-Allow-Origin"] == "*"
            async with http.head(base + "/stream.m3u8") as resp:
                assert resp.status == 200
            async with http.get(base + "/seg_00001.ts") as resp:
                assert resp.status == 404 and resp.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        await server.close()
    tv = server.activity("127.0.0.1")
    assert tv.missing == 1 and tv.requests == 2  # the preflights are neither fetches nor refusals
    assert tv.preflights == 2 and server.activity().preflights == 2  # but they show that the TV reached the PC
    assert [r.getMessage() for r in caplog.records] == [  # once per client
        "127.0.0.1 asked whether it may fetch /stream.m3u8 from a web page (OPTIONS, User-Agent: WebKit)",
    ]


FFMPEG_MASTER = (b"#EXTM3U\n#EXT-X-VERSION:3\n"
                 b'#EXT-X-STREAM-INF:BANDWIDTH=8976000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"\n'
                 b"stream.m3u8\n\n")


def test_master_playlist_gets_frame_rate_and_average_bandwidth():
    from mirrorbridge.hls_server import complete_variants

    done = complete_variants(FFMPEG_MASTER, 30, 8160000)
    assert done == FFMPEG_MASTER.replace(b'mp4a.40.2"\n', b'mp4a.40.2",FRAME-RATE=30.000,AVERAGE-BANDWIDTH=8160000\n')
    assert complete_variants(done, 60, 1) == done  # what is there already is kept
    assert b"AVERAGE-BANDWIDTH=8976000" in complete_variants(FFMPEG_MASTER, 30, 10**8)  # never above the peak
    media = b"#EXTM3U\n#EXTINF:2.0,\nseg_00001.ts\n"
    assert complete_variants(media, 30, 1) == media


async def test_served_master_playlist_is_completed_only_when_asked(tmp_path):
    (tmp_path / "master.m3u8").write_bytes(FFMPEG_MASTER)
    for kwargs, completed in (({}, False), ({"frame_rate": 30, "average_bandwidth": 8160000}, True)):
        server = HlsServer(tmp_path, "127.0.0.1", 0, **kwargs)
        await server.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(server.url("master.m3u8")) as resp:
                    body = await resp.read()
        finally:
            await server.close()
        assert (b",FRAME-RATE=30.000,AVERAGE-BANDWIDTH=8160000\n" in body) is completed
