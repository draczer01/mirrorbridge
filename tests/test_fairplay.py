"""FairPlay SAP (mirrorbridge/fairplay): the vectors doubletake's own tests check its Go code with
(internal/airplay/fairplay_vectors_test.go, fpsap_test.go, fairplay_key_test.go), some of them
from the C playfair reference."""

import hashlib
import hmac
import struct

import pytest

from mirrorbridge.fairplay import FairPlayError, FairPlaySession, stream_key
from mirrorbridge.fairplay import _tables as T
from mirrorbridge.fairplay import core

REFERENCE_LOCAL_SAP = bytes.fromhex(
    "0001e4e3dd688293e6fa66b95ba41768e587c65f750218ff1be21543d573cefb"
    "087bd36e0c6363c3c8242f4abcfa6d660b801032015405eb4ab04dda7aeff38f"
    "fb36f4cfa48f0b5d92ae363f68b45925bbe6413ab6bdc4968f548d21e67d20f"
    "1912b6820e53f1013cde29df7350a9b9fa7c51320aea62d2949786c87642e34ba")
REFERENCE_RECEIVER_SAP = bytes.fromhex(
    "0001cc342a5e5b1a6773c20e21b8224df862481864ef810aae2e3703c8819c23"
    "539de5f5d749bc5b7a266c496283ce7f03937ae1f616de0c15ff338ccaffb09e"
    "aabbe40f5d5f558fb97f1731f8f7da60a0ec6579c33ea98312c3b67135a6694f"
    "f82305d9ba5c615fa254d2b1834583cee42d4426c835a7a5f6c8421c0da3f1c7")


def _entropy(data: bytes):
    """An entropy source that hands out `data` in order."""
    stream = bytearray(data)

    def take(n: int) -> bytes:
        assert len(stream) >= n
        out = bytes(stream[:n])
        del stream[:n]
        return out
    return take


def test_table_data_checksum():
    digest = hashlib.sha256()
    written = 0

    def write(data: bytes) -> None:
        nonlocal written
        digest.update(data)
        written += len(data)

    write(T.FIRST_INPUT_MASK)
    for rounds, mix, final in ((T.FIRST_ROUNDS, T.FIRST_MIX, T.FIRST_FINAL),
                               (T.SECOND_ROUNDS, T.SECOND_MIX, T.SECOND_FINAL)):
        for ref in rounds:
            write(bytes(core._sub(ref, v) for v in range(256)))
        for row in range(4):
            write(bytes(core._mix_lookup(mix[row * 4 + out], v) for v in range(256) for out in range(4)))
        for ref in final:
            write(bytes(core._sub(ref, v) for v in range(256)))
    assert written == 90128
    assert digest.hexdigest() == "28d0986abebe30458348dfa2957aa1d52d6f3ad5a9468c5d8a9c4139b7ca2b43"


def test_primitive_vectors():
    block = bytes((i * 3 + 1) & 0xFF for i in range(64))
    words = core.md5_compress(struct.unpack("<4I", bytes(range(16))), block, core.KDF)
    assert struct.pack("<4I", *words).hex() == "f6f728cb5a4397b675664f9291b859aa"
    assert core.sap_hash(block).hex() == "75498a4e218773030e9cdf04f0c49367"


def test_sap_hash_corpus():
    """64 pseudo-random blocks, checked against the C reference as one SHA-256."""
    corpus = hashlib.sha256()
    state = 0x6A09E667F3BCC909
    mask = (1 << 64) - 1
    for _ in range(64):
        block = bytearray(64)
        for i in range(64):
            state ^= (state << 13) & mask
            state ^= state >> 7
            state ^= (state << 17) & mask
            block[i] = state & 0xFF
        corpus.update(core.sap_hash(bytes(block)))
    assert corpus.hexdigest() == "36ad2a7920076af59452d9f0c91e3b7d1aebc53f9143bd6819e39119d4535c92"


@pytest.mark.parametrize("mode,decrypted,aes_key", [
    (0, "b66a3295ffa6b56e02ed1b3d67fef74b90fe148570de65e6773669126a4905d8405644cae0b2f5ed6109c099c7aea7398dac8d623fbd69b87242b374d98f89502bb5a63e29c46a8ed0e98466966191ec1e6c8675087fde21337db1c8fab4c21db824026335f6fc37e2e5b6f53357d06994bd383d6029a0aff654fb1521bcdde4", "f7dd1ccb9e745f7951a6e325d73a1f5f"),
    (1, "0f95c6ddc8987eda18577da2db074e7c04715af8b3914a73be1b3d6c111953017ee0a39dfcab3e0d57f2f9fbd59c5e18101788c2ab8e3cbb403bcb48b53f3e5bf74f949e79fa5ca679df4bfcb33a69b1442675d03f948fe5bd0c5ffb64b73a5ab58f46d6baae097b599624147c2487991163ecffc4d966240f9526346a10fdb0", "b44ad891396f097aa309bc132f5b8889"),
    (2, "40f18751b44d733e0aa0416401a7d3f40375fad3ce56900602578bca14660909820e6ef3a5e943cafef5370f72c52177d9b82278b414811201a3d99202bedcca26a4d1ad08bc2669f4bae6ca54b8a120d0425edb6082f51f5aecdb547bfdb319099c9ea2729ae6a1c4480827ce9991e273843cf1c7d74ebbebc2657659bcea9f", "d38cd8efecdb20f333273c4312d9b236"),
    (3, "70a3c30edf0e1dfa1785ce4336ed547062672a47f714a0c1f89a83d95691103dfe5cf653d4cb8299793faf33fd0d4482ef5333b41ab094a90e1baf996bcf4989783f6918397fbacddaf00a2b97556dd8099841578bc5eb1444912b47298eaf356fdd6701bb3f64e725a80eb4c6f3556195de35c93e7cc703bdd24351468e9847", "769e2fe4c5ad7fbe6fd6772d00f529f4"),
])
def test_message_vectors(mode, decrypted, aes_key):
    message = bytearray(164)
    message[12] = mode
    for i in range(16, 144):
        message[i] = (i * 5 + 7) & 0xFF
    assert core.decrypt_message(bytes(message)).hex() == decrypted
    assert core.encrypt_message(mode, bytes.fromhex(decrypted)) == bytes(message[16:144])
    assert core.wrapping_key(REFERENCE_RECEIVER_SAP, bytes(message)).hex() == aes_key


def test_key_unwrap_vector():
    m3 = bytearray(164)
    m3[12] = 3
    for i in range(16, 144):
        m3[i] = (i * 5 + 7) & 0xFF
    ekey = bytes((i * 7 + 3) & 0xFF for i in range(72))
    assert core.unwrap_key(REFERENCE_RECEIVER_SAP, bytes(m3), ekey).hex() == "903e5be94732428e9965afb262b193a4"


CAPTURED_M2 = bytes.fromhex(
    "46504c59030102000000008202034a114c26b77d4e2eec2c8f89fdb653b5b32d3576bc176816d110a14c3f53c08dbb93"
    "6183bfdfe0a4f3c12e85216003b46f738c40c54da6c436d29d1b342d63c7b314309ae79a33bb1787709ef077cbfe4190"
    "117a3423e270fd1a2eac44da1a7934f59dc681d1b70783f228c4d077c2d495f5285c3bf8df586fc2ebfe17fb5b65")


def _sparse(index: int) -> bytes:
    payload = bytearray(128)
    payload[index] = 0x42
    return bytes(payload)


@pytest.mark.parametrize("payload,want", [
    (bytes(128), "6f627565f3e77f5b5ede91beee7baf92e4241e0b"),
    (b"\xff" * 128, "dc2cc74f2ed55484f59f95b96082f0f5c017dd17"),
    (CAPTURED_M2[14:142], "4b911e48af23d8406368aeafbb61bfcd569e3e55"),
    (_sparse(0), "9bfb9556b8659c2ac94b7ef9e587d71e159ea624"),
    (_sparse(63), "150d9fa4eb456e73ba48de5779c5c996b16b3b23"),
    (_sparse(64), "a167db30424ff8890d085c0f1c92b2c5cc06fc45"),
    (_sparse(127), "d246ec5e7adc8118994b8df77146529486ac7caf"),
], ids=["zeros", "ff", "captured-m2", "42-at-0", "42-at-63", "42-at-64", "42-at-127"])
def test_exchange_golden_vectors(payload, want):
    assert core.exchange(REFERENCE_LOCAL_SAP, core._decrypt_body(3, payload)).hex() == want


@pytest.mark.parametrize("payload,want", [
    (bytes(128), "7e38958ffe4ed433743919fe7eb16376afa4eb9e"),
    (b"\x01" + bytes(127), "ea46797d726c6a9be43ffa72385ff97ce1c54f1b"),
])
def test_descriptor(payload, want):
    assert core.descriptor(REFERENCE_LOCAL_SAP, core._decrypt_body(3, payload)).hex() == want


def test_session_exchange():
    m2 = bytearray(142)
    m2[:12] = b"FPLY" + bytes((3, 1, 2, 0)) + struct.pack(">I", 130)
    m2[12], m2[13] = 2, 3
    entropy = bytes(range(1, 127))
    session = FairPlaySession(_entropy(entropy + b"\x5a" * 16))
    assert session.message1().hex() == "46504c590301010000000004020003bb"
    m3 = session.exchange_m3(bytes(m2))
    assert len(m3) == 164 and m3[:4] == b"FPLY"
    assert core.decrypt_message(m3) == b"\x00\x01" + entropy
    receiver_sap = core._decrypt_body(3, bytes(m2[14:]))
    assert session.remote_sap == receiver_sap
    assert m3[144:] == core.exchange(b"\x00\x01" + entropy, receiver_sap)
    raw = bytes(range(16))
    ekey = session.wrap_key(raw)
    assert core.unwrap_key(receiver_sap, m3, ekey) == raw
    with pytest.raises(FairPlayError):
        session.exchange_m3(bytes(141))
    for mode in (4, 0xFF):
        bad = bytearray(m2)
        bad[13] = mode
        with pytest.raises(FairPlayError):
            session.exchange_m3(bytes(bad))
    other = FairPlaySession(_entropy(b"\xa5" * 126)).exchange_m3(bytes(m2))
    assert other[16:144] != m3[16:144]


@pytest.mark.parametrize("mode", range(4))
def test_session_uses_receiver_selected_mode(mode):
    receiver_sap = bytes([0, 1] + [(i * 7 + 3) & 0xFF for i in range(2, 128)])
    m2 = bytearray(142)
    m2[:12] = b"FPLY" + bytes((3, 1, 2, 0)) + struct.pack(">I", 130)
    m2[12], m2[13] = 2, mode
    m2[14:142] = core.encrypt_message(mode, receiver_sap)
    entropy = bytes(range(1, 127))
    session = FairPlaySession(_entropy(entropy))
    m3 = session.exchange_m3(bytes(m2))
    assert m3[12] == mode
    assert core.decrypt_message(m3) == b"\x00\x01" + entropy
    assert session.remote_sap == receiver_sap
    assert m3[144:] == core.exchange(b"\x00\x01" + entropy, receiver_sap)


def test_m4_confirms_m3():
    m2 = bytearray(142)
    m2[:12] = b"FPLY" + bytes((3, 1, 2, 0)) + struct.pack(">I", 130)
    m2[12], m2[13] = 2, 1
    session = FairPlaySession()
    with pytest.raises(FairPlayError):
        session.confirm_m4(bytes(32))
    with pytest.raises(FairPlayError):
        session.wrap_key(bytes(16))
    m3 = session.exchange_m3(bytes(m2))
    m4 = bytearray(b"FPLY" + bytes((3, 1, 4, 0)) + struct.pack(">I", 20) + m3[144:])
    session.confirm_m4(bytes(m4))
    m4[31] ^= 1
    with pytest.raises(FairPlayError):
        session.confirm_m4(bytes(m4))


def _test_m3(mode: int) -> bytes:
    m3 = bytearray(164)
    m3[:12] = b"FPLY" + bytes((3, 1, 3, 0)) + struct.pack(">I", 152)
    m3[12] = mode
    for i in range(13, 164):
        m3[i] = (i * 7 + 3) & 0xFF
    return bytes(m3)


@pytest.mark.parametrize("mode", range(4))
def test_key_wrap_round_trip(mode):
    m3 = _test_m3(mode)
    receiver_sap = bytes([0, 1] + [((i * 11) & 0xFF) ^ mode for i in range(2, 128)])
    raw = bytes((0x10, 0x21, 0x32, 0x43, 0x54, 0x65, 0x76, 0x87, 0x98, 0xA9, 0xBA, 0xCB, 0xDC, 0xED, 0xFE, mode))
    mask = bytes(range(1, 17))
    ekey = core.wrap_key(receiver_sap, m3, raw, mask)
    assert ekey[:16].hex() == "46504c59010201000000003c00000000"
    assert struct.unpack(">I", ekey[32:36])[0] == 16
    assert ekey[16:32] == mask
    mac = hmac.new(core.descriptor(core.decrypt_message(m3), receiver_sap), ekey[:36] + raw, hashlib.sha1).digest()
    assert ekey[36:56] == mac
    assert core.unwrap_key(receiver_sap, m3, ekey) == raw


def test_key_wrap_rejects_invalid_input():
    with pytest.raises(FairPlayError):
        core.wrap_key(REFERENCE_RECEIVER_SAP, bytes(143), bytes(16), bytes(16))
    m3 = bytearray(_test_m3(3))
    m3[12] = 4
    with pytest.raises(FairPlayError):
        core.wrap_key(REFERENCE_RECEIVER_SAP, bytes(m3), bytes(16), bytes(16))


def test_alternate_mac_vector():
    m2 = bytes.fromhex(
        "46504c5903010200000000820201cf32a25714b2524f8aa0ad7af164e37bcf4424e200047efc0ad67afcd95ded1c2730"
        "bb591b962ed63a9c4ded88ba8fc78de64d91ccfd5c7b56da88e31f5cceafc7431995a01665a54e1939d25b94db64b9e4"
        "5d8d063e1e6af07e9656162b0efa404275ea5a44d9591c7256b9fbe6513898b80227721988571650942ad946688a")
    m3 = bytes.fromhex(
        "46504c590301030000000098018f1a9c5b9228300aafe0b41f28b66a62a6cd62bf84eb623273dead10b1f034a8d56812"
        "6faa133f6ad5ab91acda3839817b4d9530b679fee43ac9e950f6e7aaf1381bd2d3d5198a03bf5648890d19234270a358"
        "3e4651893be09c6c75463c42e544fec9abc9f7722a2cc254364365ef91ded76b8c00f9674b08920fb9401e4be6d52a33"
        "f2f9ed6fadb672be45c3cde5ad94f3fea5b32ee4")
    receiver_sap = core._decrypt_body(m2[13], m2[14:])
    assert receiver_sap == REFERENCE_RECEIVER_SAP
    ekey = core.wrap_key(receiver_sap, m3, bytes(range(16)), bytes.fromhex("c853e777b9b65e7652d768d97c974f15"))
    assert ekey[:56].hex() == ("46504c59010201000000003c00000000c853e777b9b65e7652d768d97c974f15000000102a53a000"
                               "8888fe26bfb1e1f825f38f50d6730059")
    assert core.descriptor(core.decrypt_message(m3), receiver_sap).hex() == "2fd95dc2c23122bc77c57b983a9188c4760db322"


def test_key_decrypt_vector():
    m3 = bytes.fromhex(
        "46504c590301030000000098018f1a9c7d0af257b31f21f5c2d2bc814c032d457835ad0b06250574bbc7ab4a58cca6ee"
        "ad2c911d7f3e1e7ed4c058955dff3d5ceef014387a985bdb34995015e3dfbdacc56047cb926e093b13e9fdb5e1eee317"
        "c018bbc87fc5453c7671647da686da3d564875d03f8aea9d60092de06110bc7be0c16f391c369c75344ae47f33acfcf1"
        "0e63a9b58bfce215e96001c49e4be967c5067f2a")
    ekey = bytes.fromhex(
        "46504c59010201000000003c0000000088e4f82c8178c18b4751ac24b27c0c2a00000010c899dc6965c1081de6a9d966"
        "e2ba3e34548cdbc651c322db18dc22f58fe154a60aecee18")
    assert core.unwrap_key(REFERENCE_RECEIVER_SAP, m3, ekey).hex() == "8e1214398d46d72e7b1b8e32f80c8bf0"


def test_stream_key():
    raw, secret = bytes(range(16)), bytes(range(32))
    assert stream_key(raw, None) == raw
    assert stream_key(raw, secret) == hashlib.sha512(raw + secret).digest()[:16]
