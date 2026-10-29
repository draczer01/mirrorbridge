"""The pieces of screen mirroring's sound, against fixed vectors, FFmpeg's ALAC decoder, the way
shairport-sync opens a packet (cryptography's ChaCha20-Poly1305) and pyatv's own cipher
(tests/test_mirror_sound_loopback.py has whole sessions)."""

import hashlib
import random
import struct
import subprocess

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from pyatv.support.chacha20 import Chacha20Cipher8byteNonce

from fake_mirror_tv import parse_alac
from mirrorbridge import ffmpeg
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
    assert ap.resend_missing(0xBD0D).hex() == "80d60001bd0d0000" and ap.resend_missing(0x1_0001).hex() == "80d6000100010000"


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
