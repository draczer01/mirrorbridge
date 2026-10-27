import aiohttp

from aircast.hls_server import HlsServer


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
    from aircast.hls_server import fix_target_duration

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


async def test_records_each_client_and_its_first_playlist_and_segment(tmp_path):
    (tmp_path / "master.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "seg_00001.ts").write_bytes(b"\x47" * 188)
    server = HlsServer(tmp_path, "127.0.0.1", 0)
    await server.start()
    try:
        assert server.activity().requests == 0 and server.activity().first_playlist_at is None
        async with aiohttp.ClientSession(headers={"User-Agent": "AppleCoreMedia/1.0.0"}) as http:
            async with http.get(server.url("master.m3u8")) as resp:
                assert resp.status == 200
            async with http.get(server.url("seg_00001.ts")) as resp:
                assert resp.status == 200
    finally:
        await server.close()
    tv = server.activity("127.0.0.1")
    assert tv.requests == 2 and tv.user_agent == "AppleCoreMedia/1.0.0"
    assert tv.first_playlist_at <= tv.first_segment_at <= tv.last_request_at
    assert server.activity("192.168.1.40").requests == 0
    assert server.activity() == tv  # only one client
    assert server.url("master.m3u8").endswith(f":{server.port}/master.m3u8")
