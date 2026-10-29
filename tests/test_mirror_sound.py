"""The pieces of screen mirroring's sound, against fixed vectors, FFmpeg's ALAC decoder, the way
shairport-sync opens a packet (cryptography's ChaCha20-Poly1305) and pyatv's own cipher
(tests/test_mirror_sound_loopback.py has whole sessions)."""

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import plistlib
import random
import re
import struct
import subprocess
import sys
import time
import types
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from pyatv.support.chacha20 import Chacha20Cipher8byteNonce

from fake_mirror_tv import parse_alac
from mirrorbridge import audio, ffmpeg
from mirrorbridge.mirror import audiopackets as ap
from mirrorbridge.mirror import sound
from mirrorbridge.mirror.clock import NS, from_ntp, to_ntp

R = 44100


def _ramp() -> bytes:
    """352 frames whose samples step by 91 from -32000: [i][ch] = (2i + ch) * 91 - 32000."""
    return b"".join(struct.pack(">h", (2 * i + ch) * 91 - 32000) for i in range(352) for ch in range(2))


def _random_frames(count: int, seed: int = 7) -> list[bytes]:
    rnd = random.Random(seed)
    return [bytes(rnd.getrandbits(8) for _ in range(1408)) for _ in range(count)]


def _caf(packets: list[bytes], frame_length: int) -> bytes:
    """A CAF file around ALAC packets, with UxPlay's ALAC cookie (352 samples, 16 bits, stereo,
    44100 Hz) but `frame_length` in it, for FFmpeg to decode."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return kind + struct.pack(">q", len(body)) + body

    def varint(n: int) -> bytes:
        out = [n & 0x7F]
        n >>= 7
        while n:
            out.append(0x80 | (n & 0x7F))
            n >>= 7
        return bytes(reversed(out))

    desc = struct.pack(">d4sIIIII", float(R), b"alac", 1, 0, frame_length, 2, 0)
    kuki = struct.pack(">IBBBBBBHIII", frame_length, 0, 16, 40, 10, 14, 2, 255, 0, 0, R)
    pakt = struct.pack(">qqii", len(packets), 352 * len(packets), 0, 0) + b"".join(varint(len(p)) for p in packets)
    data = struct.pack(">I", 0) + b"".join(packets)
    return (b"caff" + struct.pack(">HH", 1, 0) + chunk(b"desc", desc) + chunk(b"kuki", kuki) + chunk(b"pakt", pakt)
            + chunk(b"data", data))


def _ffmpeg_decode(tmp_path, packets: list[bytes], frame_length: int = 352) -> tuple[bytes, str]:
    """What FFmpeg decodes the packets to, as s16 BE, and what it said."""
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    path = tmp_path / f"alac{frame_length}.caf"
    path.write_bytes(_caf(packets, frame_length))
    done = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "s16be", "-"],
                          capture_output=True, timeout=60, check=False)
    return done.stdout, done.stderr.decode(errors="replace")


def _shairport_open(key: bytes, datagram: bytes) -> bytes:
    """Open a data packet as shairport-sync does: the nonce is 4 zero bytes and the last 8, the
    additional data [4:12], the ciphertext and tag what lies between."""
    return ChaCha20Poly1305(key).decrypt(b"\0" * 4 + datagram[-8:], datagram[12:-8], datagram[4:12])


# -- codec frames ---------------------------------------------------------------------------------


def test_alac_verbatim_frames_are_the_pinned_bytes():
    silence = ap.alac_verbatim(bytes(1408))
    assert len(silence) == 1412 and silence[:4].hex() == "20000200" and silence[-2:].hex() == "01c0"
    assert hashlib.sha256(silence).hexdigest() == "aa325f59b937af2b039bb6d807bcf4cbc5faca82f9b3b67954078bf98f7baed8"
    ramp = ap.alac_verbatim(_ramp())
    assert ramp[:8].hex() == "200003060106b707" and ramp[-4:].hex() == "14f9cbc0"
    assert hashlib.sha256(ramp).hexdigest() == "b7854e68394045e3a15cf1d80e392e012958b85d126d2e20c1dd07c153988a5c"
    for frame in _random_frames(5) + [bytes(1408), _ramp()]:
        assert parse_alac(ap.alac_verbatim(frame)) == frame
    with pytest.raises(ValueError):
        ap.alac_verbatim(bytes(1404))


def test_the_fake_tvs_alac_parser_refuses_what_is_not_a_verbatim_frame():
    good = bytearray(ap.alac_verbatim(_ramp()))
    for broken in (good[:-1], good + b"\0", bytes([0x40]) + good[1:], good[:-1] + bytes([good[-1] | 1]),
                   good[:-1] + bytes([good[-1] & 0x3F])):
        with pytest.raises(ValueError):
            parse_alac(bytes(broken))


def test_ffmpeg_decodes_the_verbatim_frames_bit_for_bit(tmp_path):
    frames = _random_frames(10) + [_ramp(), bytes(1408)]
    out, said = _ffmpeg_decode(tmp_path, [ap.alac_verbatim(f) for f in frames])
    assert out == b"".join(frames), said


def test_alac_frames_with_their_size_are_the_pinned_bytes(tmp_path):
    silence = ap.alac_verbatim_sized(bytes(1408))
    assert len(silence) == 1416 and silence[:8].hex() == "200012000002c000" and silence[-4:].hex() == "000001c0"
    assert hashlib.sha256(silence).hexdigest() == "5fdafd44490bae2f665582add14e18f65512921363f640699cd08d2589e7d3cf"
    ramp = ap.alac_verbatim_sized(_ramp())
    assert ramp[:8].hex() == "200012000002c106" and ramp[-4:].hex() == "14f9cbc0"
    assert hashlib.sha256(ramp).hexdigest() == "af1559b90a7103d9d301d0466ae5fdfcfeb6a3ebc8eab7d137009ddc1e6adf2c"
    assert parse_alac(ramp) == _ramp() and parse_alac(ap.alac_verbatim_sized(_ramp()[:40])) == _ramp()[:40]
    frames = _random_frames(10)
    # A decoder takes the count whatever frame length its cookie names; without it, not.
    for frame_length in (352, 4096):
        out, said = _ffmpeg_decode(tmp_path, [ap.alac_verbatim_sized(f) for f in frames], frame_length)
        assert out == b"".join(frames), (frame_length, said)
    out, said = _ffmpeg_decode(tmp_path, [ap.alac_verbatim(f) for f in frames], 4096)
    assert out != b"".join(frames) and said


def test_pcm_big_and_little_endian():
    assert ap.PCM_44100.payload(_ramp())[:8].hex() == "8300835b83b68411"
    assert ap.PCM_44100_LE.payload(_ramp())[:8].hex() == "00835b83b6831184"
    assert ap.swap16(ap.swap16(_ramp())) == _ramp()
    assert ap.ALAC_44100.payload(_ramp()) == ap.alac_verbatim(_ramp())
    assert ap.ALAC_44100_SIZED.payload(_ramp()) == ap.alac_verbatim_sized(_ramp())
    assert [(f.ct, f.audio_format) for f in ap.FORMATS.values()] == [(2, 0x40000), (2, 0x40000), (1, 0x800),
                                                                       (1, 0x800)]


def test_ms_to_samples_rounds_down():
    assert [ap.ms_to_samples(ms) for ms in (100, 140, 50, 300, 0.03, 0, 20)] == [4410, 6174, 2205, 13230, 1, 0, 882]


# -- RTP, ChaCha20-Poly1305, syncs, resends ------------------------------------------------------------


def test_rtp_headers():
    assert ap.rtp_header(0, marker=True).hex() == "80e0000000000000" "00000000"
    assert ap.rtp_header(1).hex() == "8060000100000160" "00000000"
    assert ap.rtp_header(65535)[2:4].hex() == "ffff"
    assert ap.rtp_header(65536)[2:8].hex() == "0000" "01600000"
    assert ap.rtp_header(12201612)[4:8].hex() == "00000080"
    assert ap.rtp_header(5, marker=True)[1] == 0xE0 and ap.rtp_header(5)[1] == 0x60
    assert ap.rtp_header(2, ssrc=0x1_2345_6789)[8:12].hex() == "23456789"


@pytest.mark.parametrize("index,header,tag,trailer,digest", [
    (0, "80e000000000000000000000", "7e2a83f7de3c05f2866d356e780c85da", "0000000000000000",
     "33cf362e1235d8ce2455b2969948e0864bd1755d69b261c350de403948b94e3e"),
    (1, "806000010000016000000000", "7fc6b940bd9e307327c5ceb01b46d022", "0100000000000000",
     "6b47dd00c9e1057214aa675c6cd2a5640c1a3216f35682d0ea96bd64aba9810d"),
    (65536, "806000000160000000000000", "32a69664649ab3f017058c60640e09e1", "0000010000000000",
     "5879ea271a95e3cf4c5633d5988af25123bd370ea9d610754ab1a0944937ded7"),
])
def test_sealed_packets_are_the_pinned_bytes_and_open_as_shairport_opens_them(index, header, tag, trailer, digest):
    key = bytes(range(32))
    silence = ap.alac_verbatim(bytes(1408))
    head = ap.rtp_header(index, marker=index == 0)
    packet = head + ap.ChaChaShk(key).seal(index, head, silence)
    assert len(packet) == 1448 and packet[:12].hex() == header
    assert packet[-24:-8].hex() == tag and packet[-8:].hex() == trailer
    assert hashlib.sha256(packet).hexdigest() == digest
    assert _shairport_open(key, packet) == silence


def test_chacha_shk_is_pyatvs_cipher_in_counter_order():
    key = bytes(range(32))
    ours = ap.ChaChaShk(key)
    theirs = Chacha20Cipher8byteNonce(key, key)  # as pyatv's AirPlay 2 sender uses it
    for index, frame in enumerate(_random_frames(4)):
        payload = ap.alac_verbatim(frame)
        head = ap.rtp_header(index)
        nonce = theirs.out_nonce
        assert ours.seal(index, head, payload) == theirs.encrypt(payload, aad=head[4:12]) + nonce[-8:]


def test_chacha_shk_layouts_other_than_shairports_do_not_open():
    key = bytes(range(32))
    payload = ap.alac_verbatim(_ramp())
    head = ap.rtp_header(5)
    nonce = struct.pack("<LQ", 0, 5)
    aead = ChaCha20Poly1305(key)
    whole_header = head + aead.encrypt(nonce, payload, head) + nonce[4:]
    zeros_at_the_end = head + aead.encrypt(nonce[4:] + bytes(4), payload, head[4:12]) + nonce[4:]
    for bad in (whole_header, zeros_at_the_end):
        with pytest.raises(InvalidTag):
            _shairport_open(key, bad)
    assert _shairport_open(key, head + ap.ChaChaShk(key).seal(5, head, payload)) == payload


def test_chacha_shk_key_and_fields():
    with pytest.raises(ValueError):
        ap.ChaChaShk(bytes(16))
    cipher = ap.ChaChaShk(bytes(range(32)))
    assert cipher.stream_fields() == {"shk": bytes(range(32))} and cipher.session_fields() == {}
    assert cipher.overhead == 24 and cipher.name == "ChaCha20-Poly1305 (shk)"
    one, two = ap.ChaChaShk.for_channel(None), ap.ChaChaShk.for_channel(object())
    assert len(one.key) == 32 and one.key != two.key


def _sync_on_line(anchor: int, n_sent: int, gap: int, first: bool = False, flags: int = 4) -> bytes:
    s = n_sent * 352
    return ap.sync_packet(first, s - gap, to_ntp(anchor + s * NS // R, epoch=True), s, flags)


def test_sync_packets_are_the_pinned_bytes():
    anchor = 5_123_456_789
    assert _sync_on_line(anchor, 0, 4410, first=True).hex() == "90d40004ffffeec683aa7e851f9add3700000000"
    assert _sync_on_line(anchor, 877, 4410).hex() == "80d400040004a4a683aa7e8c1fa0cef10004b5e0"
    assert _sync_on_line(anchor, 12201612, 4410).hex() == "80d40004ffffef4683abfaf4acce341100000080"  # RTP wraps
    assert _sync_on_line(anchor, 877, 4410, flags=7).hex() == "80d400070004a4a683aa7e8c1fa0cef10004b5e0"


def test_resend_requests_and_answers():
    assert ap.parse_resend_request(bytes.fromhex("80d50001fffe0003")) == (0xFFFE, 3)
    assert ap.parse_resend_request(bytes.fromhex("00550001001e0002")) == (30, 2)  # the marker bit is ignored
    assert ap.parse_resend_request(bytes.fromhex("80d50001fffe00")) is None
    assert ap.parse_resend_request(bytes.fromhex("80d40001fffe0003")) is None
    packet = ap.rtp_header(1) + bytes(1436)
    answer = ap.resend_answer(packet)
    assert len(answer) == 1452 and answer[:8].hex() == "80d6000180600001" and answer[4:] == packet
    assert ap.resend_missing(0xBD0D).hex() == "80d60001bd0d0000"
    assert ap.resend_missing(0x1_0001).hex() == "80d6000100010000"


def test_packet_ring_finds_packets_by_seq_across_the_wrap():
    ring = ap.PacketRing()
    for index in range(65300, 65600):
        ring.put(index, b"p%d" % index)
    newest = 65599
    assert ring.get(0xFFFE, newest) == b"p65534" and ring.get(0, newest) == b"p65536"
    assert ring.get(63, newest) == b"p65599" and ring.get(65344, newest) == b"p65344"
    assert ring.get(65343, newest) is None  # 256 before the newest: its slot holds 65599
    assert ring.get(64, newest) is None  # seq 64 was packet 64, long gone (65600 is not sent yet)
    assert ring.by_index(65599) == b"p65599" and ring.by_index(65343) is None and ring.by_index(-1) is None
    ring.put(65600, b"new")  # in 65344's slot
    assert ring.by_index(65344) is None and ring.get(64, 65600) == b"new"
    assert ring.get(0, 3) is None and ring.get(5, 3) is None  # nothing that far back


def test_packet_ring_sends_a_packet_again_at_most_once_in_20_ms():
    ring = ap.PacketRing()
    ring.put(7, b"x")
    assert ring.mark_resent(7, 7, 1_000_000_000)
    assert not ring.mark_resent(7, 7, 1_000_000_000 + 19_000_000)
    assert ring.mark_resent(7, 7, 1_000_000_000 + 25_000_000)
    assert not ring.mark_resent(8, 8, 0)  # not held
    ring.put(7 + 256, b"y")
    assert ring.mark_resent(7 + 256, 7 + 256, 1_000_000_000 + 26_000_000)  # a new packet in the slot


# -- choosing the format -------------------------------------------------------------------------------


def test_the_format_follows_the_tvs_screen_stream_formats():
    assert ap.choose_format(None) == (ap.ALAC_44100, "ALAC: the TV did not list its screen sound formats")
    assert ap.choose_format(0x40000)[0] is ap.ALAC_44100
    assert ap.choose_format(0x1C0000)[0] is ap.ALAC_44100 and ap.choose_format(0x1440800)[0] is ap.ALAC_44100
    fmt, why = ap.choose_format(0x800)
    assert fmt is ap.PCM_44100 and "PCM" in why
    fmt, why = ap.choose_format(0x1000000)
    assert fmt is ap.ALAC_44100 and why == "screenStream 0x1000000 lists neither ALAC nor PCM: sending ALAC anyway"
    fmt, why = ap.choose_format(0x40000, "pcmle")
    assert fmt is ap.PCM_44100_LE and "MIRRORBRIDGE_MIRROR_SOUND_CODEC=pcmle" in why
    assert ap.choose_format(0x800, "alac-sized")[0] is ap.ALAC_44100_SIZED
    assert ap.choose_format(0x800, "")[0] is ap.PCM_44100


def test_sync_points_lie_on_one_line():
    line = sound.SampleLine()
    line.observe(352, 5_123_456_789 + 352 * NS // R, fresh=True)
    line.start(0)
    assert line.A == 5_123_456_789
    assert ap.sync_packet(True, *line.sync_point(0, 4410)).hex() == "90d40004ffffeec683aa7e851f9add3700000000"
    assert ap.sync_packet(False, *line.sync_point(877, 4410)).hex() == "80d400040004a4a683aa7e8c1fa0cef10004b5e0"
    rnd = random.Random(11)
    for _ in range(200):
        n_sent = rnd.randrange(12_000_000)  # before the RTP time wraps (27 h)
        field1, ntp, field3 = line.sync_point(n_sent, 4410)
        exact = (from_ntp(ntp, epoch=True) - line.A) * R / NS
        assert abs(exact - field3) <= 1 and (field3 - field1) & 0xFFFFFFFF == 4410 and field3 % 352 == 0


# -- the sample line ---------------------------------------------------------------------------------


def _wasapi_blocks(seconds: float, ppm: float = 0.0, jitter_ms: float = 0.0, seed: int = 1, stall_at: float = -1,
                   stall_ms: float = 0.0, gap_at: float = -1, gap_s: float = 0.0):
    """(n, arrived_ns, fresh) as soundcard's record(352) gives them: the engine has 441 samples
    (10 ms) ready every 10 ms of its own clock (44100 * (1 + ppm/1e6) samples a second of the
    PC's), the thread wakes up to jitter_ms late, a stall holds it up and loses what overflowed
    the 100 ms buffer, and a gap (a new output device) gives nothing for gap_s, then a fresh
    recorder."""
    rnd = random.Random(seed)
    rate = R * (1 + ppm / 1e6)
    t0 = 7 * NS
    pending = k = skipped = 0
    stalled_until = -1
    fresh = True
    gap_done = False
    while True:
        t_packet = t0 + int(k * 441 / rate * NS)
        if t_packet - t0 > seconds * NS:
            return
        k += 1
        if 0 <= gap_at and not gap_done and t_packet - t0 >= gap_at * NS:
            if t_packet - t0 < (gap_at + gap_s) * NS:
                pending = 0
                continue
            gap_done = fresh = True
        wake = t_packet + int(rnd.random() * jitter_ms * 1e6)
        if 0 <= stall_at and stalled_until < 0 and t_packet - t0 >= stall_at * NS:
            stalled_until = t_packet + int(stall_ms * 1e6)
        if stalled_until > 0 and wake < stalled_until:
            skipped += 1
            continue
        ready = 441
        if stalled_until > 0:
            held = (skipped + 1) * 441
            ready = held - max(0, held - 4410)
            stalled_until = 0
        pending += ready
        while pending >= 352:
            pending -= 352
            yield 352, wake, fresh
            fresh = False


def _drive(blocks, hold_ns: int = NS):
    """Feed the blocks to a SampleLine as the session does: watched while the sound is held (the
    first video frame has not gone yet; 1 s here), then anchored, corrected and put into the
    stream, with the keeper's tick every 10 ms of the blocks' clock. Returns the line, the
    totals, (time, line error) of each block and (time, lag) at each tick."""
    line = sound.SampleLine()
    totals = dict(filled=0, cut=0, inserted=0, deleted=0, kept=0, reanchors=0)
    errors, lags = [], []
    start = tick = None
    tick_ns = int(sound.KEEP_TICK_S * NS)
    for n, arrived, fresh in blocks:
        while tick is not None and tick <= arrived:
            lags.append((tick, tick - line.line_ns(line.s)))
            z = line.keep(tick)
            line.advance(z)
            totals["kept"] += z
            tick += tick_ns
        c0 = line.observe(n, arrived, fresh)
        start = arrived if start is None else start
        if arrived - start < hold_ns:
            continue
        if line.A is None:
            line.start(c0)
            tick = arrived + tick_ns
        corr = line.correction(c0, n)
        errors.append((arrived - start, line.error_ns))
        out = len(sound.apply(bytes(4 * n), corr)) // 4
        totals["filled"] += corr.fill
        totals["cut"] += corr.cut
        totals["inserted" if corr.micro > 0 else "deleted"] += abs(corr.micro)
        totals["reanchors"] += corr.reanchored
        line.advance(out)
        if out:
            line.fed_real()
    return line, totals, errors, lags


def _worst(errors, after_s: float = 0.0) -> float:
    return max(abs(e) for t, e in errors if t >= after_s * NS) / 1e6


def test_an_exact_source_needs_no_correction():
    """The test tone: each block stamped with its ideal time."""
    blocks = ((352, 3 * NS + (k + 1) * 352 * NS // R, k == 0) for k in range(R * 60 // 352))
    line, totals, errors, lags = _drive(blocks)
    assert totals == dict(filled=0, cut=0, inserted=0, deleted=0, kept=0, reanchors=0)
    assert _worst(errors) < 0.001 and line.steps == 0  # the integer ns of the stamps


@pytest.mark.parametrize("ppm,seed", [(300, 2), (-300, 3)])
def test_a_card_whose_clock_drifts_is_followed_by_single_samples(ppm, seed):
    line, totals, errors, _ = _drive(_wasapi_blocks(120, ppm=ppm, jitter_ms=12, seed=seed))
    assert _worst(errors, after_s=12) <= 6
    drift = abs(ppm) / 1e6 * 119 * R  # 119 s of stream after the 1 s held
    made, other = (totals["deleted"], totals["inserted"]) if ppm > 0 else (totals["inserted"], totals["deleted"])
    assert abs(made - other - drift) <= 0.2 * drift, totals
    assert totals["filled"] == totals["cut"] == totals["kept"] == totals["reanchors"] == 0


def test_idle_zeros_cut_short_are_followed_too():
    """When nothing plays, soundcard gives int(samplerate * elapsed) zeros a chunk: up to about
    570 ppm short."""
    _, totals, errors, _ = _drive(_wasapi_blocks(120, ppm=-570, jitter_ms=12, seed=6))
    assert _worst(errors, after_s=12) <= 6 and totals["filled"] == totals["cut"] == 0


def test_a_short_stall_that_loses_nothing_is_ridden_out():
    _, totals, errors, lags = _drive(_wasapi_blocks(40, jitter_ms=5, seed=5, stall_at=20, stall_ms=80))
    assert totals["filled"] == totals["cut"] == totals["kept"] == 0
    assert max(lag for _, lag in lags) < sound.KEEP_TRIGGER_NS


def test_a_stall_that_loses_samples_is_kept_on_time_and_the_base_moves_once():
    line, totals, errors, lags = _drive(_wasapi_blocks(40, jitter_ms=5, seed=4, stall_at=20, stall_ms=400))
    assert line.steps == 1 and totals["reanchors"] == totals["filled"] == 0
    assert max(lag for _, lag in lags) <= 110 * 1_000_000  # the keeper: zeros on time through the stall
    assert totals["kept"] > 0.3 * R and totals["cut"] > 0  # the late blocks are cut until the base moves
    assert _worst(errors, after_s=21.4) < 3


def test_a_new_recorder_after_half_a_second_without_blocks():
    line, totals, errors, lags = _drive(_wasapi_blocks(30, jitter_ms=5, seed=5, gap_at=20, gap_s=0.5))
    assert 0.35 * R <= totals["kept"] <= 0.55 * R and totals["filled"] == 0 and totals["cut"] <= 352
    assert max(lag for _, lag in lags) <= 110 * 1_000_000 and line.steps == 0


def _started(blocks: int = 50) -> tuple[sound.SampleLine, int]:
    """A line fed `blocks` exact blocks; returns it and the next block's arrival."""
    line = sound.SampleLine()
    for k in range(blocks):
        c0 = line.observe(352, NS + (k + 1) * 352 * NS // R, k == 0)
        if line.A is None:
            line.start(c0)
        assert line.correction(c0, 352) == sound.Correction()
        line.advance(352)
    return line, NS + (blocks + 1) * 352 * NS // R


def test_samples_dropped_on_the_way_are_filled():
    line, arrived = _started()
    line.skip(1760)
    corr = line.correction(line.observe(352, arrived + 1760 * NS // R, False), 352)
    assert abs(corr.fill - 1760) <= 1 and corr.cut == corr.micro == 0 and not corr.reanchored


def test_a_line_far_behind_is_anchored_again():
    line, arrived = _started()
    line.skip(15_000)  # 340 ms
    c0 = line.observe(352, arrived + 15_000 * NS // R, False)
    corr = line.correction(c0, 352)
    assert corr == sound.Correction(reanchored=True) and line.reanchors == 1
    assert line.correction(c0, 352) == sound.Correction()  # on the new line
    line.advance(352)
    n_sent = line.s // 352
    field1, ntp, field3 = line.sync_point(n_sent, 4410)
    assert field3 == line.s and (field3 - field1) & 0xFFFFFFFF == 4410
    assert abs(from_ntp(ntp, epoch=True) - (line.t_cap(c0) + 352 * NS // R)) <= 1


def test_apply_cuts_then_corrects_then_fills():
    frames = [_ramp()[4 * i:4 * i + 4] for i in range(352)]
    more = sound.apply(_ramp(), sound.Correction(micro=2))
    assert len(more) == 354 * 4
    assert more == b"".join(frames[:118] + frames[117:235] + frames[234:])  # 117 and 234 twice
    fewer = sound.apply(_ramp(), sound.Correction(micro=-2))
    assert fewer == b"".join(frames[:117] + frames[118:234] + frames[235:])
    both = sound.apply(_ramp(), sound.Correction(cut=2, micro=1, fill=3))
    assert both == bytes(12) + b"".join(frames[2:178] + frames[177:])  # the 175th of the 350 left, twice
    assert sound.apply(_ramp(), sound.Correction(cut=352, micro=-1)) == b""
    assert sound.apply(_ramp(), sound.Correction()) == _ramp()


def test_floats_become_s16_big_endian_clipped():
    assert sound.floats_to_s16be([1.0, -1.0, 0.5, 1.5, -1.5]).hex() == "7fff80014000" "7fff8000"
    import numpy as np

    assert sound.floats_to_s16be(np.zeros((352, 2), dtype=np.float32)) == bytes(1408)


# -- the test switches ---------------------------------------------------------------------------------


def test_sound_knobs_default_to_unset():
    assert sound.SoundKnobs.from_env({}) == sound.SoundKnobs()
    knobs = sound.SoundKnobs()
    assert knobs.enable is None and knobs.shape == "screen" and knobs.hold and knobs.sync_flags == 4
    assert knobs.replace(tone=True).tone and not knobs.tone


def test_every_sound_knob_from_the_environment():
    env = {"MIRRORBRIDGE_MIRROR_SOUND": "1", "MIRRORBRIDGE_MIRROR_SOUND_TONE": "1",
           "MIRRORBRIDGE_MIRROR_SOUND_DELAY_MS": "-40.5", "MIRRORBRIDGE_MIRROR_SOUND_GAP_MS": "50",
           "MIRRORBRIDGE_MIRROR_SOUND_CODEC": "PCMLE", "MIRRORBRIDGE_MIRROR_SOUND_SHAPE": "media",
           "MIRRORBRIDGE_MIRROR_SOUND_LAT_MIN_MS": "0.03", "MIRRORBRIDGE_MIRROR_SOUND_LAT_MAX_MS": "300",
           "MIRRORBRIDGE_MIRROR_SOUND_IS_MEDIA": "1", "MIRRORBRIDGE_MIRROR_SOUND_REDUNDANT": "2",
           "MIRRORBRIDGE_MIRROR_SOUND_SYNC_FLAGS": "7", "MIRRORBRIDGE_MIRROR_SOUND_SSRC": "id",
           "MIRRORBRIDGE_MIRROR_SOUND_VOLUME": "-20", "MIRRORBRIDGE_MIRROR_SOUND_HOLD": "0",
           "MIRRORBRIDGE_MIRROR_SOUND_PORTS": "7001, 7002", "MIRRORBRIDGE_MIRROR_SOUND_MUTE": "1"}
    assert sound.SoundKnobs.from_env(env) == sound.SoundKnobs(
        enable=True, tone=True, delay_ms=-40.5, gap_ms=50.0, codec="pcmle", shape="media", lat_min_ms=0.03,
        lat_max_ms=300.0, is_media=True, redundant=2, sync_flags=7, ssrc="id", volume_db=-20.0, hold=False,
        ports=(7001, 7002), mute=True)
    assert sound.SoundKnobs.from_env({"MIRRORBRIDGE_MIRROR_SOUND": "0"}).enable is False
    assert sound.SoundKnobs.from_env({"MIRRORBRIDGE_MIRROR_SOUND_VOLUME": "-144"}).volume_db == -144


def test_sound_knobs_out_of_range_and_invalid(caplog):
    clamped = {"MIRRORBRIDGE_MIRROR_SOUND_DELAY_MS": "900", "MIRRORBRIDGE_MIRROR_SOUND_GAP_MS": "5",
               "MIRRORBRIDGE_MIRROR_SOUND_LAT_MAX_MS": "-1", "MIRRORBRIDGE_MIRROR_SOUND_REDUNDANT": "5",
               "MIRRORBRIDGE_MIRROR_SOUND_VOLUME": "-60"}
    with caplog.at_level("INFO", logger="mirrorbridge.mirror.sound"):
        knobs = sound.SoundKnobs.from_env(clamped)
    assert (knobs.delay_ms, knobs.gap_ms, knobs.lat_max_ms, knobs.redundant, knobs.volume_db) == (500, 20, 0, 2, -30)
    assert "MIRRORBRIDGE_MIRROR_SOUND_DELAY_MS=900 is out of -500..500: using 500" in caplog.messages
    assert sound.SoundKnobs.from_env({"MIRRORBRIDGE_MIRROR_SOUND_VOLUME": "-500"}).volume_db == -144
    invalid = {"MIRRORBRIDGE_MIRROR_SOUND": "yes", "MIRRORBRIDGE_MIRROR_SOUND_TONE": "2",
               "MIRRORBRIDGE_MIRROR_SOUND_DELAY_MS": "soon", "MIRRORBRIDGE_MIRROR_SOUND_GAP_MS": "nan",
               "MIRRORBRIDGE_MIRROR_SOUND_CODEC": "aac", "MIRRORBRIDGE_MIRROR_SOUND_SHAPE": "square",
               "MIRRORBRIDGE_MIRROR_SOUND_REDUNDANT": "1.5", "MIRRORBRIDGE_MIRROR_SOUND_SYNC_FLAGS": "5",
               "MIRRORBRIDGE_MIRROR_SOUND_SSRC": "1", "MIRRORBRIDGE_MIRROR_SOUND_VOLUME": "inf",
               "MIRRORBRIDGE_MIRROR_SOUND_PORTS": "7001", "MIRRORBRIDGE_MIRROR_SOUND_HOLD": "",
               "MIRRORBRIDGE_MIRROR_SOUND_MUTE": "on"}
    caplog.clear()
    with caplog.at_level("INFO", logger="mirrorbridge.mirror.sound"):
        assert sound.SoundKnobs.from_env(invalid) == sound.SoundKnobs()
    assert len(caplog.records) == 12 and all(r.levelname == "INFO" for r in caplog.records)
    assert "MIRRORBRIDGE_MIRROR_SOUND_CODEC=aac is not alac, alac-sized, pcm, pcmle: left at unset" in caplog.messages
    assert sound.SoundKnobs.from_env({"MIRRORBRIDGE_MIRROR_SOUND_PORTS": "0,7002"}).ports is None


def test_sound_knobs_describe_names_every_variable():
    text = sound.SoundKnobs(enable=True, delay_ms=40.0, ports=(1, 2)).describe()
    for name in ("", "_TONE", "_DELAY_MS", "_GAP_MS", "_CODEC", "_SHAPE", "_LAT_MIN_MS", "_LAT_MAX_MS", "_IS_MEDIA",
                 "_REDUNDANT", "_SYNC_FLAGS", "_SSRC", "_VOLUME", "_HOLD", "_PORTS", "_MUTE"):
        assert f"MIRRORBRIDGE_MIRROR_SOUND{name}=" in text
    assert text.startswith("MIRRORBRIDGE_MIRROR_SOUND=1, MIRRORBRIDGE_MIRROR_SOUND_TONE=0, ")
    assert "_DELAY_MS=40," in text and "_GAP_MS=unset," in text and "_PORTS=1,2," in text and "_HOLD=1," in text


# -- capture: the loopback recorder's sink, the test tone ------------------------------------------------


class _FakeSoundcard(types.ModuleType):
    """soundcard as LoopbackFeeder uses it: the default speaker changes after `switch_after`
    records, record number `empty_at` comes back empty and number `fail_at` raises (a device
    unplugged). With `pace`, the records come at that many frames a second, as a device's do."""

    def __init__(self, switch_after: int = 20, empty_at: int = 10, fail_at: int = 30, pace: float = 0) -> None:
        super().__init__("soundcard")
        self.records = 0
        self.switch_after, self.empty_at, self.fail_at = switch_after, empty_at, fail_at
        self.pace = pace
        self.started: float = 0.0
        self.opened: list[tuple[str, dict]] = []
        self.closed = 0

    def default_speaker(self):
        which = "B" if self.records >= self.switch_after else "A"
        return SimpleNamespace(id=f"{{speaker-{which}}}", name=f"Speakers {which}")

    def get_microphone(self, ident: str, include_loopback: bool = False):
        assert include_loopback
        card = self

        class Recorder:
            def __enter__(self):
                card.opened.append((ident, kwargs))
                return self

            def __exit__(self, *exc):
                card.closed += 1

            def record(self, numframes):
                import numpy as np

                card.records += 1
                if card.records == card.fail_at:
                    raise RuntimeError("the device was unplugged")
                if card.pace:  # each record when its frames are all there, from the first on
                    card.started = card.started or time.perf_counter()
                    time.sleep(max(0.0, card.started + card.records * numframes / card.pace - time.perf_counter()))
                else:
                    time.sleep(0.001)
                if card.records == card.empty_at:
                    return np.zeros((0, 2), dtype=np.float32)
                return np.full((numframes, 2), card.records / 1000, dtype=np.float32)

        kwargs: dict = {}

        def recorder(**kw):
            kwargs.update(kw)
            return Recorder()

        return SimpleNamespace(recorder=recorder)


def test_loopback_feeder_hands_blocks_to_a_sink(monkeypatch):
    card = _FakeSoundcard()
    monkeypatch.setitem(sys.modules, "soundcard", card)
    monkeypatch.setattr(audio, "SPEAKER_CHECK_SECONDS", 0)
    got = []

    def sink(arr, arrived, fresh):
        got.append((arr.copy(), arrived, fresh, feeder.speaker_id))
        if len(got) == 40:
            raise RuntimeError("the sound stopped")

    feeder = audio.LoopbackFeeder(sink=sink, samplerate=44100, block_frames=352, buffer_frames=4410,
                                  name="mirrorbridge-audio")
    feeder.start()
    feeder.join(10)
    assert not feeder.is_alive() and feeder.error is None  # a sink that raises ends the thread
    assert [a.shape for a, _, _, _ in got] == [(352, 2)] * 40 and all(a.dtype == "float32" for a, _, _, _ in got)
    assert [i for i, (_, _, fresh, _) in enumerate(got) if fresh] == [0, 20, 29]  # each recorder opened
    assert [opened for opened, _ in card.opened] == ["{speaker-A}", "{speaker-B}", "{speaker-B}"]
    assert card.opened[0][1] == dict(samplerate=44100, channels=2, blocksize=4410) and card.closed == 3
    assert [s for _, _, _, s in got[:20]] == ["{speaker-A}"] * 20 and got[20][3] == "{speaker-B}"
    assert feeder.speaker_name == "Speakers B"
    assert not got[9][0].any() and got[8][0][0, 0] == pytest.approx(0.009)  # the empty record: zeros
    stamps = [arrived for _, arrived, _, _ in got]
    assert stamps == sorted(stamps)
    assert got[29][1] - got[28][1] >= 0.45 * NS  # half a second before reopening after the failure


def test_loopback_feeder_sink_with_a_source_and_without_a_port_or_sink():
    import numpy as np

    got = []

    def sink(arr, arrived, fresh):
        got.append((arr, fresh))
        if len(got) == 5:
            feeder.stop()

    feeder = audio.LoopbackFeeder(source=lambda frames: np.full(frames * 2, 0.25, dtype="<f4").tobytes(), sink=sink,
                                  block_frames=352)
    feeder.run()
    assert [(a.shape, fresh) for a, fresh in got] == [((352, 2), True)] + [((352, 2), False)] * 4
    assert feeder.name == "mirrorbridge-audio"
    with pytest.raises(ValueError):
        audio.LoopbackFeeder()


def test_tone_feeder_beeps_at_whole_seconds():
    clock = [5_700_000_123]
    blocks = []

    def sleep(seconds):
        clock[0] += max(1, round(seconds * NS))

    def on_block(pcm, n, stamp, fresh):
        blocks.append((pcm, n, stamp, fresh, clock[0]))
        if len(blocks) == R * 3 // 352:
            tone.stop()

    tone = sound.ToneFeeder(on_block, clock=lambda: clock[0], sleep=sleep)
    assert tone.name == "mirror-tone"
    tone.run()
    t0 = 5_700_000_123
    for k, (_, n, stamp, _, now) in enumerate(blocks):
        assert n == 352 and stamp == t0 + (k + 1) * 352 * NS // R <= now
    assert [fresh for *_, fresh, _ in blocks] == [True] + [False] * (len(blocks) - 1)
    pcm = b"".join(block[0] for block in blocks)
    beep = sound.beep()
    starts = [c for c in range(1, len(pcm) // 4)
              if pcm[4 * c:4 * c + 4] != bytes(4) and pcm[max(0, 4 * c - 8820):4 * c] == bytes(min(8820, 4 * c))]
    onsets = [c - 3 for c in starts]  # the fade-in makes the beep's first three samples 0
    assert len(onsets) == 3
    for second, c in zip((6, 7, 8), onsets):
        assert abs(t0 + c * NS / R - second * NS) <= NS / R  # at the whole second, to a sample
        assert pcm[4 * c:4 * (c + 4410)] == beep
    silent = bytearray(pcm)
    for c in onsets:
        silent[4 * c:4 * (c + 4410)] = bytes(4 * 4410)
    assert not any(silent)


def test_the_beep_and_where_it_starts():
    beep = sound.beep()
    left = [struct.unpack_from(">h", beep, 4 * j)[0] for j in range(4410)]
    assert len(beep) == 4410 * 4 and all(beep[4 * j:4 * j + 2] == beep[4 * j + 2:4 * j + 4] for j in range(4410))
    assert left[:3] == [0, 0, 0] and left[3] == 1 and left[-1] == 0
    assert max(map(abs, left[:220])) < max(map(abs, left[220:4190])) == 8191  # faded in and out over 220
    assert max(map(abs, left[:110])) < 4100 and max(map(abs, left[-110:])) < 4100
    assert left[100] == round(8191 * math.sin(2 * math.pi * 880 * 100 / R) * 0.5 * (1 - math.cos(math.pi * 100 / 220)))
    # sample 0 at 0.99999 s: the beep starts at the first sample at or after 1 s
    t0 = 999_990_000
    block = sound.tone_block(0, 1000, t0, 0)
    first = -((-(NS - t0) * R) // NS)
    assert first == 1 and t0 + first * NS // R >= NS > t0
    assert block[:4] == bytes(4) and block[4:] == beep[:999 * 4]
    assert sound.tone_block(0, 352, NS, 0)[:4 * 352] == beep[:4 * 352]  # sample 0 at a whole second
    assert sound.tone_block(0, 352, NS + 50_000_000, 0) == bytes(1408)  # began before the tone did
    assert sound.tone_block(4410 - 100, 352, NS, 0) == beep[4 * 4310:] + bytes(4 * 252)


def test_the_tone_origin_moves_the_beeps(monkeypatch):
    monkeypatch.setattr(sound, "TONE_ORIGIN_NS", 0)
    sound.set_tone_origin(250_000_000)
    assert sound.TONE_ORIGIN_NS == 250_000_000
    got = []
    clock = [NS]
    tone = sound.ToneFeeder(lambda pcm, *rest: (got.append(pcm), len(got) == 200 and tone.stop()),
                            clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + round(s * NS) + 1))
    tone.run()
    pcm = b"".join(got)
    first = next(i for i in range(0, len(pcm), 4) if pcm[i:i + 4] != bytes(4)) // 4 - 3
    assert first == math.ceil(0.25 * R)


async def test_tone_feeder_runs_as_its_own_thread():
    got = []
    tone = sound.ToneFeeder(lambda pcm, n, stamp, fresh: got.append(stamp))
    tone.start()
    await asyncio.sleep(0.2)
    tone.stop()
    tone.join(2)
    assert not tone.is_alive() and len(got) >= 10 and got == sorted(got)
    assert all(b - a == 352 * NS // R or b - a == 352 * NS // R + 1 for a, b in zip(got, got[1:]))


# -- the sender: packets, syncs, resends ------------------------------------------------------------------


def _recording_sender(start_index: int = 0, packets: int = 0, **kw):
    """An AudioSender whose sends are recorded: (sender, data sent, control datagrams sent, clock)."""
    data, control, clock = [], [], [NS]
    sender = sound.AudioSender(ap.ALAC_44100, ap.ChaChaShk(bytes(range(32))), ssrc=0, tv_ip="192.168.1.20",
                               send_data=lambda packet: data.append(packet) or True,
                               send_control=lambda packet, addr: control.append((packet, addr)),
                               control_addr=("192.168.1.20", 6002), start_index=start_index, clock=lambda: clock[0],
                               **kw)
    sender.push(_ramp() * packets)
    return sender, data, control, clock


def test_the_sender_answers_resend_requests_across_the_seq_wrap():
    sender, data, control, _ = _recording_sender(65500, 40)
    tv = ("192.168.1.20", 6002)
    sender.on_control(bytes.fromhex("80d50001fffe0003"), tv)
    assert [(len(p), p[:2].hex(), p[2:4].hex(), addr) for p, addr in control] == [
        (1452, "80d6", "fffe", tv), (1452, "80d6", "ffff", tv), (1452, "80d6", "0000", tv)]
    assert [p[4:] for p, _ in control] == [data[34], data[35], data[36]]  # packets 65534, 65535, 65536
    assert _shairport_open(bytes(range(32)), control[2][0][4:]) == ap.alac_verbatim(_ramp())
    assert sender.stats.resend_requests == 1 and sender.stats.asked == 3 and sender.stats.resent == 3
    control.clear()
    sender.on_control(bytes.fromhex("80d500010010" "0002"), tv)  # seqs 16 and 17: packets 65552 and 65553, not sent
    assert [p.hex() for p, _ in control] == ["80d6000100100000", "80d6000100110000"] and sender.stats.missed == 2


def test_the_sender_ignores_what_it_cannot_answer():
    sender, data, control, clock = _recording_sender(0, 300)
    tv = ("192.168.1.20", 6002)
    sender.on_control(bytes.fromhex("80d5000100100000"), tv)  # count 0
    sender.on_control(bytes.fromhex("80d4000400000000ffffeec600000000"), tv)  # a sync, 0x54
    assert control == [] and sender.stats.unknown_control == 2 and sender.stats.resend_requests == 0
    sender.on_control(bytes.fromhex("80d5000100100002"), ("192.168.1.21", 6002))
    assert control == [] and sender.stats.foreign_control == 1
    sender.on_control(struct.pack(">BBHHH", 0x80, 0xD5, 1, 44, 300), tv)  # more than the ring holds
    assert len(control) == 256 and sender.stats.asked == 256 and sender.stats.resent == 256
    assert control[0][0][2:4] == struct.pack(">H", 44) and control[-1][0][2:4] == struct.pack(">H", 299)


def test_the_sender_sends_a_packet_again_at_most_once_in_20_ms():
    sender, data, control, clock = _recording_sender(0, 10)
    tv = ("192.168.1.20", 6002)
    request = bytes.fromhex("80d5000100030002")
    sender.on_control(request, tv)
    clock[0] += 15_000_000
    sender.on_control(request, tv)
    assert len(control) == 2 and sender.stats.deduped == 2
    clock[0] += 10_000_000  # 25 ms after the first
    sender.on_control(request, tv)
    assert len(control) == 4 and sender.stats.resent == 4 and control[2][0][4:] == data[3]


def test_the_sender_packs_whole_packets_and_marks_the_first():
    sender, data, control, _ = _recording_sender()
    assert sender.push(_ramp()[:1000]) == 0 and sender.carry == 250 and data == []
    assert sender.push(_ramp()[1000:] + _ramp()) == 2 and sender.carry == 0 and sender.index == 2
    sender.mark_next()
    sender.push(_ramp())
    assert [p[1] for p in data] == [0xE0, 0x60, 0xE0] and [p[2:4].hex() for p in data] == ["0000", "0001", "0002"]
    assert all(len(p) == 1448 and _shairport_open(bytes(range(32)), p) == ap.alac_verbatim(_ramp()) for p in data)
    assert sender.stats.packets == 3 and sender.stats.bytes == 3 * 1448
    line = sound.SampleLine()
    line.observe(352, 5_123_456_789 + 352 * NS // R, True)
    line.start(0)
    sent = sender.send_sync(line.sync_point(0, 4410), first=True)
    assert sent.hex() == "90d40004ffffeec683aa7e851f9add3700000000" and control == [(sent, ("192.168.1.20", 6002))]
    assert sender.stats.syncs == 1


def test_the_sender_repeats_packets_and_counts_the_seq_wrap(caplog):
    sender, data, _, _ = _recording_sender(65534, 0, redundant=2)
    with caplog.at_level("INFO", logger="mirrorbridge.mirror.sound"):
        sender.push(_ramp() * 4)
    assert [struct.unpack_from(">H", p, 2)[0] for p in data] == [65534, 65534, 65535, 65534, 65535, 0, 65535, 0, 1]
    assert sender.stats.repeats == 5 and sender.stats.seq_wraps == 1
    assert "Sound: seq wrapped to 0 at packet 65536 (0.0 s)" in caplog.messages


def test_packets_past_the_send_buffer_limit_are_dropped():
    class Transport:
        size = 70_000
        sent = []

        def is_closing(self):
            return False

        def get_write_buffer_size(self):
            return self.size

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    transport = Transport()
    send = sound.datagram_sender(transport, ("192.168.1.20", 6001))
    sender = sound.AudioSender(ap.PCM_44100, ap.ChaChaShk(bytes(32)), ssrc=0, tv_ip="192.168.1.20", send_data=send,
                               send_control=lambda packet, addr: None, control_addr=("192.168.1.20", 6002))
    sender.push(_ramp() * 2)
    assert transport.sent == [] and sender.stats.send_dropped == 2 and sender.stats.packets == 2
    transport.size = 0
    sender.push(_ramp())
    assert len(transport.sent) == 1 and len(transport.sent[0][0]) == 1444 and sender.stats.send_dropped == 2
    assert sound.SoundStats(packets=3).as_dict()["packets"] == 3 and "headroom_min_ms" in sound.SoundStats().as_dict()


async def _udp_sender(audio, **kw):
    """An AudioSender on real UDP sockets to the fake TV's ports: (sender, its control port,
    the data transport)."""
    loop = asyncio.get_running_loop()
    _, control = await loop.create_datagram_endpoint(sound.AudioControl, local_addr=("127.0.0.1", 0))
    data, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
    sender = sound.AudioSender(ap.ALAC_44100, ap.ChaChaShk(audio.stream["shk"]), ssrc=0, tv_ip="127.0.0.1",
                               send_data=sound.datagram_sender(data, ("127.0.0.1", audio.data_port)),
                               send_control=control.send, control_addr=("127.0.0.1", audio.control_port), **kw)
    control.attach(sender)
    return sender, control, data


async def _paced_push(sender, data, pcm: bytes) -> None:
    """Push as a stream would, not all at once: Windows' event loop sends one datagram at a
    time (under Wine about one in 0.4 ms) and holds the rest, which datagram_sender drops past
    64 KiB; and on Linux the receiving socket's buffer must not overflow."""
    sender.push(pcm)
    await asyncio.sleep(0.001)
    while data.get_write_buffer_size() > 8 * 1448:
        await asyncio.sleep(0.001)


async def _sent(audio, data, last: int) -> None:
    """Until the data transport has sent everything and packet `last` came (3 s at most)."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (data.get_write_buffer_size() or last not in audio.packets):
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)  # for what is still on its way


async def _send_frames(sender, data, audio, frames: list[bytes]) -> None:
    line = sound.SampleLine()
    line.observe(352, time.perf_counter_ns(), True)
    line.start(0)
    sender.send_sync(line.sync_point(0, 4410), first=True)
    for frame in frames:
        await _paced_push(sender, data, frame)
    await _sent(audio, data, len(frames) - 1)


@pytest.mark.parametrize("profile", [{}, {"audio_lose": (30, 31)}, {"redundant": 2}])
async def test_the_sender_over_udp_to_the_fake_tvs_audio_ports(profile):
    from fake_mirror_tv import AudioReceiver, TvProfile

    redundant = profile.pop("redundant", 0)
    receiver = AudioReceiver(TvProfile(**profile), {"type": 96, "ct": 2, "spf": 352, "shk": bytes(range(32))})
    audio = await receiver.open()
    sender, control, data = await _udp_sender(audio, redundant=redundant)
    frames = _random_frames(200, seed=3)
    port = control.port
    try:
        await _send_frames(sender, data, audio, frames)
    finally:
        control.close()
        data.close()
        receiver.close()
    assert audio.errors == [] and sorted(audio.packets) == list(range(200))
    assert audio.pcm() == (b"".join(frames), [])
    packets = [audio.packets[i] for i in range(200)]
    assert [(p.seq, p.rtp, p.ssrc, p.marker) for p in packets] == [(i, 352 * i, 0, i == 0) for i in range(200)]
    assert [p.nonce8 for p in packets] == [struct.pack("<Q", i) for i in range(200)]
    assert len(audio.syncs) == 1 and audio.syncs[0].first and audio.syncs[0].field3 == 0
    assert audio.control_peer == ("127.0.0.1", port)
    if profile.get("audio_lose"):
        assert audio.requests == [(30, 2)] and audio.packets[30].resent and audio.packets[31].resent
        assert sender.stats.resend_requests == 1 and sender.stats.resent == 2 and sender.stats.missed == 0
        assert not any(p.resent for i, p in audio.packets.items() if i not in (30, 31))
    else:
        assert audio.requests == [] and sender.stats.resent == 0
    copies = [p.copies for p in packets]
    assert copies == ([3] * 198 + [2, 1] if redundant else [1] * 200)
    assert sender.stats.repeats == (397 if redundant else 0) and sender.stats.send_dropped == 0


# -- the fake TV's audio stream ----------------------------------------------------------------------------


@pytest.mark.parametrize("changes,keys", [
    ({}, {"type", "dataPort", "controlPort", "streamID"}),
    ({"audio_ports": False}, {"type", "streamID"}),
    ({"audio_control_port": False}, {"type", "dataPort", "streamID"}),
    ({"audio_answer_type": False, "refuse": ()}, {"dataPort", "controlPort", "streamID"}),
])
async def test_the_fake_tv_gives_the_audio_stream_ports_of_its_own(changes, keys):
    from fake_mirror_tv import FakeMirrorTV, TvProfile
    from mirrorbridge.mirror.control import decode_plist
    from mirrorbridge.mirror.negotiation import CONTROL_FIRST, MEDIA_FIRST, Negotiator, Plan

    profile = dataclasses.replace(TvProfile(screen_formats=0x800, record_headers={"Audio-Latency": "11025"},
                                            feedback_body={"streams": []}), **changes)
    tv = await FakeMirrorTV(profile).start()
    negotiator = Negotiator(tv.target(), timing_port=7880, audio_port=7881)
    try:
        await negotiator.connect()
        assert (await negotiator.channel.info()).screen_formats == 0x800
        est = await negotiator.establish(Plan())
        control_first = est.plan.ordering == CONTROL_FIRST
        assert est.plan.ordering == (MEDIA_FIRST if profile.refuse else CONTROL_FIRST)
        answer = est.replies[1 if control_first else 0]["streams"][0]
        assert set(answer) == keys and len(tv.audios) == 1
        audio = tv.audios[0]
        assert answer.get("dataPort", audio.data_port) == audio.data_port > 0
        assert answer.get("controlPort", audio.control_port) == audio.control_port > 0
        offered = next(s for setup in tv.setups for s in setup.get("streams", []) if s["type"] == 96)
        assert audio.stream == offered and audio.session["sessionUUID"] == est.uuid and "streams" not in audio.session
        record = await negotiator.channel.request("RECORD", est.uri, headers={"Session": est.uuid})
        assert record.headers["Audio-Latency"] == "11025"
        feedback = await negotiator.channel.request("POST", "/feedback")
        assert decode_plist(feedback) == {"streams": []}
        await negotiator.teardown(est.uri)
    finally:
        negotiator.close()
        await tv.stop()
    assert audio.datagrams == 0 and audio.packets == {}


async def test_the_tone_through_the_line_and_the_sender_is_heard_at_whole_seconds():
    """The pieces in a row, as the session will put them: the tone's blocks on the sample line,
    the sender to the fake TV over UDP, and the beeps found where rule A hears them: their
    whole second plus the gap."""
    from fake_mirror_tv import AudioReceiver, TvProfile

    clock = [5_400_000_000]
    blocks = []
    tone = sound.ToneFeeder(lambda pcm, n, stamp, fresh: (blocks.append((pcm, n, stamp, fresh)),
                                                          len(blocks) == R * 5 // 2 // 352 and tone.stop()),
                            clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + round(s * NS) + 1))
    tone.run()
    receiver = AudioReceiver(TvProfile(), {"type": 96, "ct": 2, "spf": 352, "shk": bytes(range(32))})
    audio = await receiver.open()
    sender, control, data = await _udp_sender(audio)
    line = sound.SampleLine()
    gap = 4410
    try:
        for k, (pcm, n, stamp, fresh) in enumerate(blocks):
            c0 = line.observe(n, stamp, fresh)
            if line.A is None:
                line.start(c0)
                sender.send_sync(line.sync_point(0, gap), first=True)
            if k == len(blocks) // 2:
                sender.send_sync(line.sync_point(sender.index, gap), first=False)
            corr = line.correction(c0, n)
            assert corr == sound.Correction()
            out = sound.apply(pcm, corr)
            line.advance(len(out) // 4)
            await _paced_push(sender, data, out)
        await _sent(audio, data, sender.index - 1)
    finally:
        control.close()
        data.close()
        receiver.close()
    assert audio.errors == [] and len(audio.syncs) == 2 and audio.pcm()[1] == []
    onsets = audio.onsets()
    assert len(onsets) == 2  # at 6 s and 7 s of the clock
    for second, onset in zip((6, 7), onsets):
        heard = audio.heard_ns(onset)
        assert abs(heard - (second * NS + gap * NS // R)) < 100_000  # the fade-in's 3 silent samples: 68 us
        assert abs(audio.heard_ns(onset, rule="D") - heard - 11025 * NS / R) <= 1


# -- the type-96 SETUP, the TV's answer, /info and the saved verdict -----------------------------------------


class _StubChannel:
    """A control connection that answers each request with the next plist of `answers`."""

    local_ip, remote_ip = "192.168.1.10", "192.168.1.20"
    closed = out_of_step = False

    def __init__(self, answers=(), headers=None) -> None:
        self.answers = list(answers)
        self.headers = headers or {}
        self.requests = []

    @staticmethod
    def derive(salt, write, read):
        return bytes(range(32)), bytes(range(32, 64))

    async def request(self, method, uri, *, body=None, headers=None, content_type=None, timeout=10):
        from pyatv.support.http import HttpResponse

        self.requests.append((method, uri, body, content_type))
        answer = self.answers.pop(0) if self.answers else {}
        data = plistlib.dumps(answer, fmt=plistlib.FMT_BINARY) if answer else b""
        return HttpResponse("RTSP", "1.0", 200, "OK", self.headers, data)


class _SealedOtherwise:
    """An AudioCipher whose key goes into the session's fields, not the stream's (as FairPlay's
    would): what the seam must carry."""

    name, overhead = "a test cipher", 0
    made = []

    def __init__(self, channel) -> None:
        self.channel = channel
        _SealedOtherwise.made.append(self)

    def stream_fields(self):
        return {}

    def session_fields(self):
        return {"ekey": bytes(72), "eiv": bytes(16)}

    def seal(self, index, header, payload):
        return payload


def _negotiator(offer=None, answers=(), headers=None):
    from mirrorbridge.mirror.control import Target
    from mirrorbridge.mirror.negotiation import Negotiator

    negotiator = Negotiator(Target("TV", "192.168.1.20"), timing_port=7880, audio_port=7881, audio=offer)
    negotiator.channel = _StubChannel(answers, headers)
    return negotiator


def _todays_audio_stream(stream_id: int, lead_ms: int = 100) -> dict:
    """The type-96 dict as it was before it carried sound, but for its random shk."""
    return {"audioFormat": 0x40000, "audioMode": "default", "controlPort": 7881, "ct": 2,
            "latencyMax": lead_ms * 44100 // 1000, "latencyMin": 0, "spf": 352, "sr": 44100,
            "streamConnectionID": stream_id, "type": 96, "usingScreen": True}


@pytest.mark.parametrize("lead", [100, 60, 200, 300])
def test_the_audio_stream_is_as_it_was_by_default(lead):
    from mirrorbridge.mirror.negotiation import AudioOffer, Plan

    for offer in (None, AudioOffer(ap.ALAC_44100, "screen", ap.ms_to_samples(lead))):  # sound off; on, default knobs
        stream = _negotiator(offer)._audio_stream(77, Plan(lead_ms=lead), ap.ChaChaShk(bytes(range(32))))
        assert stream.pop("shk") == bytes(range(32)) and stream == _todays_audio_stream(77, lead)


@pytest.mark.parametrize("changes,latencies,keys", [
    ({"shape": "minmax"}, (4410, 4410), {"usingScreen": True}),
    ({"shape": "apple"}, (3750, 3750), {"usingScreen": True}),
    ({"shape": "media"}, (11025, 88200), {"isMedia": True, "supportsDynamicStreamID": False}),
    ({"lat_min": 1, "lat_max": 6615}, (1, 6615), {"usingScreen": True}),
    ({"shape": "apple", "lat_max": 9000}, (3750, 9000), {"usingScreen": True}),
    ({"is_media": True}, (0, 4410), {"usingScreen": True, "isMedia": True}),
    ({"redundant": 2}, (0, 4410), {"usingScreen": True, "redundantAudio": 2}),
    ({"gap": 6174}, (0, 6174), {"usingScreen": True}),
    ({"gap": 2205, "shape": "minmax"}, (2205, 2205), {"usingScreen": True}),
])
def test_the_audio_stream_takes_the_shapes_and_the_overrides(changes, latencies, keys):
    from mirrorbridge.mirror.negotiation import AudioOffer, Plan

    stream = _negotiator(AudioOffer(**changes))._audio_stream(77, Plan(), ap.ChaChaShk(bytes(32)))
    assert (stream["latencyMin"], stream["latencyMax"]) == latencies
    rest = {key: value for key, value in stream.items() if key not in _todays_audio_stream(77) and key != "shk"}
    assert rest == {key: value for key, value in keys.items() if key != "usingScreen"}
    assert ("usingScreen" in stream) == ("usingScreen" in keys)


def test_the_audio_stream_names_the_format_and_the_ciphers_key():
    from mirrorbridge.mirror.negotiation import AudioOffer, Plan

    stream = _negotiator(AudioOffer(ap.PCM_44100))._audio_stream(77, Plan(), ap.ChaChaShk(bytes(32)))
    assert (stream["ct"], stream["audioFormat"], stream["spf"], stream["sr"]) == (1, 0x800, 352, 44100)
    stream = _negotiator(AudioOffer(ap.ALAC_44100_SIZED))._audio_stream(77, Plan(), _SealedOtherwise(None))
    assert (stream["ct"], stream["audioFormat"]) == (2, 0x40000) and "shk" not in stream


def _answers(ordering: str, audio: dict, video_port: int = 7100) -> list[dict]:
    session = {"eventPort": 7010, "timingPort": 7011}
    video = {"type": 110, "dataPort": video_port, "streamID": 2}
    if ordering == "control-first":
        return [session, {"streams": [audio]}, {"streams": [video]}]
    return [{**session, "streams": [audio]}, {"streams": [video]}]


@pytest.mark.parametrize("ordering", ["control-first", "media-first"])
@pytest.mark.parametrize("answer,ports,note", [
    ({"type": 96, "dataPort": 6001, "controlPort": 6002, "streamID": 1}, (6001, 6002), ""),
    ({"dataPort": 6001, "controlPort": 6002, "streamID": 1}, (6001, 6002), ""),  # untyped
    ({"type": 96, "streamID": 1}, None, "the TV named no port for the sound"),
    ({"type": 96, "dataPort": 6001, "streamID": 1}, None, "the TV named no control port for the sound"),
    ({"type": 96, "dataPort": True, "controlPort": 6002}, None, "the TV named no port for the sound"),
])
async def test_the_tvs_answer_to_the_audio_stream(ordering, answer, ports, note, caplog):
    from mirrorbridge.mirror.negotiation import AudioOffer, Plan

    offer = AudioOffer(ap.PCM_44100, "minmax", 4410, redundant=1)
    negotiator = _negotiator(offer, _answers(ordering, answer))
    with caplog.at_level("INFO", logger="mirrorbridge.mirror.negotiation"):
        est = await negotiator._attempt(Plan(ordering=ordering))  # pylint: disable=protected-access
    assert est.data_port == 7100 and est.audio_note == note
    assert f"Sound: the TV answered the audio stream with {dict(sorted(answer.items()))}" in caplog.messages
    if ports is None:
        assert est.audio is None
        return
    sent = next(s for _, _, body, _ in negotiator.channel.requests for s in body.get("streams", []) if s["type"] == 96)
    setup = est.audio
    assert (setup.data_port, setup.control_port) == ports and setup.answer == answer
    assert setup.stream_id == sent["streamConnectionID"] and setup.cipher.stream_fields() == {"shk": sent["shk"]}
    assert (setup.format, setup.gap, setup.lat_min, setup.lat_max, setup.redundant) == (ap.PCM_44100, 4410, 4410,
                                                                                         4410, 1)


async def test_the_audio_answer_found_elsewhere_or_not_at_all():
    from mirrorbridge.mirror.negotiation import AudioOffer, Plan, audio_answer

    typed = {"type": 96, "dataPort": 6001, "controlPort": 6002}
    # Typed 96 in another answer than the audio SETUP's: taken. Untyped elsewhere: not.
    assert audio_answer([{"streams": [typed]}, {}, {}], 1) == (typed, (6001, 6002), "")
    assert audio_answer([{"streams": [{"dataPort": 1, "controlPort": 2}]}, {}, {}], 1) == (
        None, None, "the TV did not answer the audio stream")
    assert audio_answer([{"streams": [{"type": 110, "dataPort": 1}]}], 0)[2] == "the TV did not answer the audio stream"
    # MIRRORBRIDGE_MIRROR_SOUND_PORTS stands in for what the TV named, or did not.
    assert audio_answer([{"streams": [{"type": 96}]}], 0, (5001, 5002)) == ({"type": 96}, (5001, 5002), "")
    assert audio_answer([{}], 0, (5001, 5002)) == ({}, (5001, 5002), "")
    negotiator = _negotiator(AudioOffer(ports=(5001, 5002)), _answers("media-first", {"type": 96}))
    est = await negotiator._attempt(Plan(ordering="media-first"))  # pylint: disable=protected-access
    assert (est.audio.data_port, est.audio.control_port) == (5001, 5002)
    negotiator = _negotiator(None, [{"eventPort": 7010}, {"streams": [{"type": 110, "dataPort": 7100}]}])
    est = await negotiator._attempt(Plan(audio=False))  # pylint: disable=protected-access
    assert est.audio is None and est.audio_note == "no audio stream in this TV's plan"


@pytest.mark.parametrize("ordering", ["control-first", "media-first"])
async def test_each_attempt_gets_a_cipher_of_its_own_whose_fields_go_where_it_says(ordering):
    """The seam FairPlay's cipher would use: made for each attempt with the channel, its session
    fields in the first SETUP body, its stream fields (none) in the type-96 dict."""
    from mirrorbridge.mirror.negotiation import AudioOffer, Plan

    _SealedOtherwise.made.clear()
    answer = {"type": 96, "dataPort": 6001, "controlPort": 6002}
    negotiator = _negotiator(AudioOffer(cipher_factory=_SealedOtherwise), _answers(ordering, answer) * 2)
    first = await negotiator._attempt(Plan(ordering=ordering))  # pylint: disable=protected-access
    second = await negotiator._attempt(Plan(ordering=ordering))  # pylint: disable=protected-access
    assert [cipher.channel for cipher in _SealedOtherwise.made] == [negotiator.channel] * 2
    assert first.audio.cipher is _SealedOtherwise.made[0] and second.audio.cipher is _SealedOtherwise.made[1]
    bodies = [body for _, _, body, _ in negotiator.channel.requests]
    count = len(bodies) // 2
    for at in (0, count):
        assert bodies[at]["ekey"] == bytes(72) and bodies[at]["eiv"] == bytes(16) and "sessionUUID" in bodies[at]
        assert not any("ekey" in body for body in bodies[at + 1:at + count])
    audio = [s for body in bodies for s in body.get("streams", []) if s["type"] == 96]
    assert len(audio) == 2 and not any("shk" in s for s in audio)
    # ChaChaShk: a new key each time, and nothing in the session's fields.
    negotiator = _negotiator(None, _answers(ordering, answer) * 2)
    keys = [(await negotiator._attempt(Plan(ordering=ordering))).audio.cipher.key  # pylint: disable=protected-access
            for _ in range(2)]
    assert keys[0] != keys[1] and all(len(key) == 32 for key in keys)
    assert not any(key in body for _, _, body, _ in negotiator.channel.requests for key in ("ekey", "eiv"))


async def test_the_setup_log_leaves_every_key_out(caplog):
    from mirrorbridge.mirror.negotiation import AudioOffer, Plan

    secrets = []

    class Keyed(_SealedOtherwise):
        def __init__(self, channel):
            super().__init__(channel)
            self.key = bytes(range(100, 132))
            secrets.extend([self.key, b"\x11" * 72, b"\x22" * 16])

        def stream_fields(self):
            return {"shk": self.key}

        def session_fields(self):
            return {"ekey": b"\x11" * 72, "eiv": b"\x22" * 16}

    for ordering in ("media-first", "control-first"):
        negotiator = _negotiator(AudioOffer(cipher_factory=Keyed), _answers(ordering, {"type": 96}))
        with caplog.at_level("DEBUG", logger="mirrorbridge.mirror.negotiation"):
            await negotiator._attempt(Plan(ordering=ordering))  # pylint: disable=protected-access
    secrets.extend(negotiator.channel.derive("", "", ""))
    assert "<32 bytes>" in caplog.text and "<72 bytes>" in caplog.text and "<16 bytes>" in caplog.text
    for secret in secrets:
        for shown in (repr(secret), secret.hex(), repr(secret[:16])):
            assert shown not in caplog.text


async def test_record_keeps_its_answer_and_says_the_tvs_audio_latency(caplog):
    from mirrorbridge.mirror.negotiation import Established, Plan

    est = Established(Plan(), "UUID", "rtsp://tv/1", "rtsp://tv/2", 2, 7100)
    negotiator = _negotiator(headers={"CSeq": "4", "Audio-Latency": "11025"})
    with caplog.at_level("DEBUG", logger="mirrorbridge.mirror.negotiation"):
        await negotiator.record(est)
    assert est.record_headers == {"CSeq": "4", "Audio-Latency": "11025"}
    assert "RECORD answered 200; Audio-Latency: 11025" in caplog.messages
    caplog.clear()
    negotiator = _negotiator(headers={"CSeq": "4"})
    with caplog.at_level("INFO", logger="mirrorbridge.mirror.negotiation"):
        await negotiator.record(est)
    assert "RECORD answered 200" in caplog.messages and est.record_headers == {"CSeq": "4"}


async def test_set_volume_sends_owntones_parameter():
    from mirrorbridge.mirror.negotiation import Established, Plan

    est = Established(Plan(), "UUID", "rtsp://tv/1", "rtsp://tv/2", 2, 7100)
    negotiator = _negotiator()
    assert await negotiator.set_volume(est, -20) == 200
    assert negotiator.channel.requests == [("SET_PARAMETER", "rtsp://tv/1", b"volume: -20.000000\r\n",
                                            "text/parameters")]
    assert negotiator.steps["volume"] == 200


def test_get_info_keeps_what_says_how_the_tv_takes_the_sound():
    from mirrorbridge.mirror.control import ReceiverInfo

    info = ReceiverInfo.parse({
        "supportedFormats": {"screenStream": 21235712, "audioStream": 21235712},
        "audioLatencies": [{"inputLatencyMicros": 0, "outputLatencyMicros": 400000, "type": 100}],
        "initialVolume": -20, "volumeControlType": 4,
    })
    assert (info.screen_formats, info.audio_formats, info.initial_volume, info.volume_control_type) == (
        0x1440800, 0x1440800, -20.0, 4)
    assert info.audio_latencies == [{"inputLatencyMicros": 0, "outputLatencyMicros": 400000, "type": 100}]
    summary = info.summary()
    assert ("screenStream formats: 0x1440800; audioStream formats: 0x1440800; audioLatencies: "
            "[{'inputLatencyMicros': 0, 'outputLatencyMicros': 400000, 'type': 100}]; initialVolume: -20; "
            "volumeControlType: 4; eventPort: none") in summary
    bare = ReceiverInfo.parse({"supportedFormats": {"screenStream": 0x1C0000}, "initialVolume": True,
                               "audioLatencies": "none", "volumeControlType": "4"})
    assert (bare.audio_formats, bare.audio_latencies, bare.initial_volume, bare.volume_control_type) == (
        None, None, None, None)
    assert "audioStream" not in bare.summary() and "initialVolume" not in bare.summary()


def test_the_saved_profile_keeps_what_is_known_of_the_sound(tmp_path):
    from mirrorbridge.mirror import profiles

    path = tmp_path / "mirroring.json"
    assert profiles.MirrorProfile().sound == "" and profiles.MirrorProfile().sound_note == ""
    assert profiles.set_sound("tcl", "heard", path=path) is None and not path.exists()  # nothing saved for it yet
    profiles.save("tcl", profiles.MirrorProfile("media-first", lead_ms=150), name="TCL", info="displays: 1920x1080",
                  path=path)
    assert profiles.set_sound("tcl", "off", "2026-10-29: closed the session when the sound started", path) == path
    saved = profiles.load("tcl", path)
    assert (saved.ordering, saved.lead_ms, saved.sound, saved.sound_note) == (
        "media-first", 150, "off", "2026-10-29: closed the session when the sound started")
    data = json.loads(path.read_text())
    assert data["tcl"]["name"] == "TCL" and data["tcl"]["info"] == "displays: 1920x1080"
    profiles.set_sound("tcl", "heard", path=path)
    assert (profiles.load("tcl", path).sound, profiles.load("tcl", path).sound_note) == ("heard", "")
    with pytest.raises(ValueError):
        profiles.set_sound("tcl", "loud", path=path)
    # With nothing saved, what the session that heard it used is saved with it.
    used = profiles.MirrorProfile("media-first", checked="2026-10-29 12:00")
    assert profiles.set_sound("roku", "heard", path=path, profile=used, name="Roku") == path
    assert profiles.load("roku", path) == dataclasses.replace(used, sound="heard")
    assert json.loads(path.read_text())["roku"]["name"] == "Roku" and profiles.load("tcl", path).sound == "heard"
    for odd in ("loud", None, 3):
        assert profiles.MirrorProfile.from_dict({"sound": odd, "sound_note": 5}).sound == ""
    assert profiles.MirrorProfile.from_dict({"sound_note": 5}).sound_note == ""
    # What an older MirrorBridge wrote, without them, loads with nothing known.
    assert profiles.MirrorProfile.from_dict({"ordering": "media-first"}).sound == ""


# -- the command line ---------------------------------------------------------------------------------


def _cli_sessions(monkeypatch) -> list:
    """The mirror command's sessions, as it makes them (each logs a DEBUG line and gives a notice),
    for the fake TCL it is told it chose."""
    from fake_mirror_tv import CREDENTIALS
    from pyatv.conf import AppleTV, ManualService
    from pyatv.const import Protocol

    from mirrorbridge import cli, mirror
    from mirrorbridge.devices import Device
    from mirrorbridge.text import Text

    made = []

    class Session:
        def __init__(self, target, settings, on_status, **kw) -> None:
            self.target, self.settings, self.kw = target, settings, kw
            made.append(self)

        async def run(self) -> None:
            logging.getLogger("mirrorbridge.mirror.session").debug("A line for the log file")
            self.kw["on_notice"](Text("mirror_sound_not_taken", "This TV does not take the PC's sound with Mirror "
                                      "screen; for a video with sound, use Play a video file."))

    async def choose(_manager, _args):
        conf = AppleTV("127.0.0.1", "TCL 55QM7L")
        conf.add_service(ManualService("fake-tv", Protocol.AirPlay, 7000, {"features": "0x7F8AD0,0x38BCF46"},
                                       credentials=CREDENTIALS))
        return Device(conf)

    async def paired(_manager, _device, force=False):
        return None

    monkeypatch.setattr(mirror, "MirrorSession", Session)
    monkeypatch.setattr(cli, "_choose", choose)
    monkeypatch.setattr(cli, "_pair_if_needed", paired)
    return made


def test_the_mirror_commands_sound_options_are_for_one_run(tmp_path, monkeypatch, capsys):
    from mirrorbridge import cli
    from mirrorbridge.config import Settings

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MIRRORBRIDGE_MIRROR_SOUND_SHAPE", "apple")
    Settings().save()
    saved = Settings.load()
    made = _cli_sessions(monkeypatch)
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    log = tmp_path / "step1.log"
    assert cli.main(["mirror", "-d", "TCL", "--sound", "--beep", "--sound-delay", "40", "--no-audio",
                     "--keep-pc-sound", "--log", str(log)]) == 0
    (session,) = made
    assert session.kw["sound"] is True and session.kw["source"] == "desktop"
    assert session.kw["sound_knobs"] == sound.SoundKnobs(tone=True, delay_ms=40.0, shape="apple")
    assert not session.settings.capture_audio and not session.settings.mute_pc_audio
    assert Settings.load() == saved  # for this run only
    assert ('[note] This TV does not take the PC\'s sound with Mirror screen; for a video with sound, use cast -d '
            '"TCL 55QM7L" --file VIDEO.') in capsys.readouterr().out
    # --log: every DEBUG line in the file, and nothing of it left behind.
    text = log.read_text(encoding="utf-8")
    assert "DEBUG   mirrorbridge.mirror.session: A line for the log file" in text
    assert f"DEBUG   mirrorbridge.cli: MirrorBridge {cli.__version__}: mirror -d TCL --sound --beep" in text
    assert root.level == level and root.handlers == handlers
    assert cli.main(["mirror", "-d", "TCL", "--log", str(log)]) == 0
    assert log.read_text(encoding="utf-8").startswith(text)  # added to
    # Without them, the sound as the settings and the switches say.
    made.clear()
    assert cli.main(["mirror", "-d", "TCL", "--test-pattern"]) == 0
    (session,) = made
    assert session.kw["sound"] is None and session.kw["sound_knobs"] == sound.SoundKnobs(shape="apple")
    assert session.settings.capture_audio and session.settings.mute_pc_audio and session.kw["source"] == "test"
    with pytest.raises(SystemExit):
        cli.main(["mirror", "-d", "TCL", "--log", str(tmp_path / "no folder" / "step1.log")])


@pytest.mark.parametrize("value", ["600", "-500.5", "nan", "soon"])
def test_the_sound_delay_is_from_minus_500_to_500_ms(value, capsys):
    from mirrorbridge import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["mirror", "--sound-delay", value])
    assert "--sound-delay" in capsys.readouterr().err
    assert cli.build_parser().parse_args(["mirror", "--sound-delay", "-500"]).sound_delay == -500.0


def test_the_sound_options_are_in_the_help(capsys):
    from mirrorbridge import cli

    for command in ("mirror", "doctor"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([command, "--help"])
    out = " ".join(capsys.readouterr().out.split())
    for option in ("--sound send this PC's sound with the picture", "--beep", "--sound-delay MS", "--no-audio",
                   "--keep-pc-sound", "--sound-trial", "--log PATH", "--sound check only this PC's sound"):
        assert option in out


@pytest.mark.parametrize("rate, code", [(44100, 0), (48000, 1)])  # 48000: the frames do not come at 44.1 kHz
def test_doctor_sound_records_as_a_session_would_and_says_how_it_went(monkeypatch, capsys, rate, code):
    from mirrorbridge import cli

    card = _FakeSoundcard(switch_after=10**9, empty_at=10**9, fail_at=10**9, pace=rate)
    monkeypatch.setitem(sys.modules, "soundcard", card)
    monkeypatch.setattr(cli, "SOUND_CHECK_SECONDS", 2)
    asked = []

    async def prompt(text, secret=False, choices=None):
        asked.append(text)
        return ""

    class Speakers:
        calls: list = []

        def __init__(self) -> None:
            self.error = ""

        def mute(self) -> bool:
            Speakers.calls.append("mute")
            return True

        def restore(self) -> None:
            Speakers.calls.append("restore")

    monkeypatch.setattr(cli, "_prompt", prompt)
    monkeypatch.setattr(cli.speakers, "MutedSpeakers", Speakers)
    assert cli.main(["doctor", "--sound"]) == code
    out = capsys.readouterr().out
    assert asked == ["Play some sound on this PC (music or a video), then press Enter "]
    assert card.opened[0] == ("{speaker-A}", dict(samplerate=44100, channels=2, blocksize=4410))
    assert "Speaker:   Speakers A\n" in out and "Level:     peak " in out
    blocks, frames, measured = (int(x) for x in re.search(r"Blocks:    (\d+), (\d+) frames, (\d+) frames/s "
                                                          r"\(44100 asked\)", out).groups())
    assert frames == 352 * blocks and measured == pytest.approx(rate, rel=0.005)
    assert re.search(r"Arrival:   [\d.]+ ms late \(median\), [\d.]+ ms \(99%\)", out)
    assert "Line:      error " in out and "Mute:      muted, and on again 0.5 s later" in out
    assert Speakers.calls == ["mute", "restore"]
    assert ("Result:    OK" in out) == (code == 0)


def test_doctor_sounds_figures_are_the_lines():
    from mirrorbridge import cli

    exact = [(352, 7 * NS + (k + 1) * 352 * NS // R, k == 0) for k in range(500)]
    figures = cli._line_figures(exact)  # pylint: disable=protected-access
    assert figures["rate"] == pytest.approx(R, rel=1e-6) and figures["frames"] == 500 * 352
    assert figures["opened"] == 1 and figures["late_p99"] == 0 and figures["error_max"] == 0
    assert (figures["inserted"], figures["deleted"], figures["filled_ms"], figures["kept_ms"]) == (0, 0, 0, 0)
    # A card 1% fast: the line takes samples out to stay on time.
    fast = [(n, 7 * NS + (arrived - 7 * NS) * 100 // 101, fresh) for n, arrived, fresh in exact]
    figures = cli._line_figures(fast)  # pylint: disable=protected-access
    assert figures["rate"] == pytest.approx(R * 1.01, rel=1e-4) and figures["deleted"] > 0
    assert cli._line_figures([])["rate"] == 0  # pylint: disable=protected-access
