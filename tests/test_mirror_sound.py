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
from mirrorbridge.mirror.clock import NS, to_ntp

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
