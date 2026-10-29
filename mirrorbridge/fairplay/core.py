# MirrorBridge's FairPlay SAP: a Python port of doubletake's Go implementation
# (https://github.com/omarroth/doubletake, internal/airplay/fpsap.go, fairplay_sap.go,
# fairplay_message.go, fairplay_crypto.go and fairplay_md5.go). Copyright the doubletake
# authors; this module is free software under the GNU Lesser General Public License version 3
# (LICENSE and COPYING.GPL in this folder).
"""The FairPlay SAP handshake a sender does before mirroring to an Apple TV or a Mac.

    m1 = session.message1()          POST /fp-setup  ->  m2 (142 bytes)
    m3 = session.exchange_m3(m2)     POST /fp-setup  ->  m4 (32 bytes)
    session.confirm_m4(m4)
    ekey = session.wrap_key(raw_key) the 72-byte "ekey" a SETUP can carry

Everything here works on bytes. Go's byte arithmetic wraps at 256; Python's does not, so
values are cut to a byte (`_b`) wherever the wrap changes the result: before a division, a
right shift, a modulo or an index, and when they are stored.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Callable, Optional, Sequence

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import _tables as T

M2_SIZE = 142
M3_SIZE = 164
M4_SIZE = 32
EKEY_SIZE = 72
MODES = 4


class FairPlayError(ValueError):
    """A FairPlay message that is not what the handshake expects."""


def _b(value: int) -> int:
    return value & 0xFF


def _rotl8(value: int, count: int) -> int:
    value, count = _b(value), count & 7
    return _b(value << count | value >> (8 - count))


def _rotl32(value: int, count: int) -> int:
    value &= 0xFFFFFFFF
    return (value << count | value >> (32 - count)) & 0xFFFFFFFF


# -- the SAP hash (fairplay_sap.go): FairPlay's own, not a standard hash -----------------------

def _rotate_or_zero(value: int, count: int) -> int:
    count = _b(count)
    return 0 if count == 0 else _rotl8(value, count)


def _wide_seed(value: int, count: int) -> int:
    value, count = _b(value), _b(count)
    if count == 0:
        return T.SAP_SEED[0]
    right = value >> (8 - count) if count <= 8 else 0
    return T.SAP_SEED[(value << count | right) % len(T.SAP_SEED)]


def _majority(a: int, b: int, c: int) -> int:
    a, b, c = _b(a), _b(b), _b(c)
    return a ^ ((a ^ b) & (a ^ c))


def _select(mask: int, if_set: int, if_clear: int) -> int:
    mask, if_set, if_clear = _b(mask), _b(if_set), _b(if_clear)
    return if_clear ^ ((if_set ^ if_clear) & mask)


def _square(value: int) -> int:
    value = _b(value)
    return _b(value * value)


def _cube(value: int) -> int:
    value = _b(value)
    return _b(value * value * value)


def _nonlinear_circuit(hash_: list[int], matrix: list[int], aux: list[int], work: list[int]) -> None:
    """fairplay_sap.go's nonlinearCircuit, one statement for one. Go gives &, &^, << and >> the
    precedence of *, and | and ^ that of +, so every such expression is bracketed here."""
    # pylint: disable=too-many-statements,invalid-name
    seed = T.SAP_SEED

    def hi(i: int) -> int:
        return hash_[_b(i) % 20]

    def si(i: int) -> int:
        return seed[_b(i) % 21]

    def h(i: int) -> int:
        return hi(work[i])

    def m(i: int) -> int:
        return matrix[work[i] % 35]

    def s(i: int) -> int:
        return si(work[i])

    def ma(i: int) -> int:
        return matrix[aux[i] % 35]

    rz, maj, sel, sq, cube, b = _rotate_or_zero, _majority, _select, _square, _cube, _b

    matrix[12] = b(0x14 + (sel(92, work[64], work[99] // 3) & _wide_seed(s(206), 4)))
    work[4] = b(2 * sq(work[99] // 5))
    work[153] = b(work[153] ^ (sq(m(203)) * work[190]))
    hash_[3] = b(0x13 ^ ((s(205) >> 1) & 0x10))
    work[33] = b(work[33] - (s(36) & ~9))
    aux[5] = b(((m(67) & ~2) | 1 | ((h(181) >> 6) & 2) | (hash_[3] & 0x10)) - 15)
    matrix[12] = 0x07
    work[2] = b(work[2] - 64)
    hash_[19] = s(58)
    aux[4] = b(92 - m(32))
    aux[9] = b(m(15) + 0x9E)
    work[34] = b(work[34] + si(aux[9]) // 5)
    hash_[19] = b(hash_[19] + (0xE6 ^ ((hi(aux[9]) >> 1) & 0x66)))
    work[15] = b(work[15] ^ (3 * rz(work[72], b(-s(190)) & 7) - 9 * s(126)))
    hash_[15] = b(hash_[15] ^ cube(m(181)))
    matrix[4] = b(matrix[4] ^ (work[202] // 3))
    matrix[1] = b(matrix[1] + cube(maj(92 - hi(aux[4]), ~work[105], 0xC6)))
    hash_[19] = b(hash_[19] ^ ((224 | (s(92) & 27)) * m(41) // 3))
    work[140] = b(work[140] + rz(92, b(-work[5]) & 7))
    matrix[12] = b(matrix[12] + maj(~work[4] ^ m(12), work[182], 192))
    work[36] = b(work[36] + 125)
    work[124] = _rotl8(maj(maj(work[138], hash_[15], 74), h(43), 95), 4)
    aux_hash = hi(aux[9])
    aux[1] = b(0x4C & ~(aux_hash & b(s(68) << 1)))
    aux[2] = b(222 - maj((work[177] + s(79)) >> 1, 3 * work[148] // 5, matrix[1]))
    matrix[16] = b(matrix[16] + (((ma(4) & ~0x60) | aux_hash | 8) - (_rotl8(work[33], 2) | 128)))
    hash_[14] = b(hash_[14] ^ ma(2))
    work[19] = b(work[19] + maj(rz(si(h(201)), b(m(112) << 1) & 6),
                                ((h(208) & ~0x7C) | (h(164) & 0x7C)) // 5, 37))
    matrix[8] = b(rz(140, b(-sq(s(45))) & 7) ^ aux[4])
    work[190] = 56
    work[53] = b(~((h(83) | 204) // 5))
    hash_[13] = b(hash_[13] + h(41))
    hash_[10] = maj(ma(4), work[2], aux[2]) // 15
    aux[3] = b(92 - sq(0x28 | (ma(1) & (0x12 | (s(2) & 4)))))
    seed_bits = si(aux[4])
    matrix[13] = b(matrix[13] ^ seed_bits)
    aux[6] = b(92 + sq(maj(m(179) - 38, aux[2], 177)))
    expansion_bits = maj(aux[3] + (aux[4] & 74), ~seed_bits, 121)
    work[47] = b(work[47] ^ (m(89) + maj(expansion_bits ^ 0xA6, aux[4], 4)))
    aux[7] = b(seed_bits // 3 - ma(9) - (0x14 | (work[151] & ((aux[4] & 0x88) | 0x62)) | (aux[4] & 0x22)))
    expanded_selector = b(expansion_bits ^ ((aux[4] & 0xCA) >> 1) ^ 75)
    aux[9] = b(aux[9] + (0x80 | (maj(aux[7], work[151], 0x20) & 0x64) | (seed_bits & 0x44) | (ma(9) & 0x1B)))
    matrix[33] = b(matrix[33] ^ work[26])
    matrix[30] = b(b(aux[9] // 3 - ((aux[4] & ~8) | 0x13)) ^ h(122))
    work[22] = b((m(90) & 0x1B) | 0x44)
    wide = sel(71, matrix[expanded_selector % 35], si(aux[5]))
    matrix[18] = b(matrix[18] + ((wide * wide * wide) >> 1))
    matrix[5] = b(matrix[5] - s(92))
    matrix[18] = b(matrix[18] ^ (sel(aux[3], ma(3), sel(16, m(183), work[41]))
                                 * sel(expanded_selector, h(59), work[17])))
    matrix[22] = b(maj(sel(hash_[14] | 28, (work[7] & 28) | 0x82, h(93)),
                       rz(ma(4), rz(work[11], b(-m(28)) & 7) & 7), matrix[33]) + 74)
    hash_[15] = b(hash_[15] - maj(maj(aux[3], aux[4], 214), si(h(39) ^ 217), aux[6]))

    hash9 = hi(aux[9])
    indexed_hash = hi(b(aux[4] // 3 - (aux[9] | work[22])) ^ aux[6]
                      ^ (((m(57) | hash9) & (0x52 | (aux[9] & 0x0D))) | (((m(57) & hash9) | aux[9]) & 0x20)))
    aux[6] = b(sq(sq(h(99))) | ma(9))
    aux[1] = b(aux[1] + rz(h(151) | s(202), h(50) & 7)
               + maj(h(4), (sel(matrix[16], indexed_hash, m(138)) + sel(17, work[33], s(39))) // 5, 147))
    aux[0] = sel(hash_[10] & 7, ma(6) & h(209), sel(0x47, rz(s(127), ma(6) & 7), b(si(ma(5)) << 1)))
    selected_square = sel(198, sq(m(14)), h(145) ^ aux[0])
    seed9 = si(aux[9])
    hash3 = hi(aux[3])
    matrix[2] = b(matrix[2] + ((b(hash3 << 1) & ((work[25] & 0x96) | (seed9 & 8))) | (seed9 & 0x40)))
    matrix[14] = b(matrix[14] - sel(34, work[97], ma(3) & (aux[0] ^ m(100))))
    work[23] = b(work[23] ^ b(maj(maj(s(17), hash3, aux[0]), work[50] // 3, 0x76) << 1))
    hash_[17] = 115
    hash_[13] = b(((maj(hi(aux[7]), work[10], 82) >> 1) & 0x68) | (h(39) & 0x17))
    matrix[33] = b(matrix[33] - (work[113] & 9))
    matrix[28] = b(matrix[28] - ((aux[3] & ~0x20) | ((work[110] >> 1) & 0x20)))
    work[95] = si(aux[3])
    hash_[15] = b(maj(work[95] - 48, ~work[184], 189) & cube(maj(aux[7], si(aux[1]), 0xAA)))
    matrix[22] = b(matrix[22] + work[183])
    aux[4] = b(aux[4] ^ (3 * s(1)))
    aux[5] = b(aux[5] + 198 * maj(s(178), ma(1), 209) * h(13) * (s(26) >> 1))
    aux[8] = sel(10, ma(3), ma(9))
    matrix[18] = b(matrix[18] - sel(hash_[15], aux[5] // 15, cube(hi(aux[6]) | 81)))
    aux[1] = b(aux[1] + si(hi(aux[1])) // 3 - h(160))
    hash_[16] = b(147 - maj(aux[0], maj(s(69), work[172], aux[2] - selected_square + 77), 0xC2 | (aux[0] & 5)))
    hash_[3] = b(hash_[3] - _wide_seed(maj(s(155), work[105], 141), maj(s(168), h(29), 6) & 7))
    work[5] = b(rz(0x38, b(-(h(61) // 5)) & 7) ^ (b(~ma(8)) // 5))
    work[198] = b(work[198] + work[3])
    wide = 162 | ma(9)
    work[164] = b(work[164] + wide * wide // 5)
    aux[2] = b(maj(rz(139, b(-aux[5]) & 6), hi(aux[3]), 12) | sel(95, cube(seed9), hi(aux[7])))
    matrix[12] = b(matrix[12] + (16 | ((work[103] | 60) & (aux[2] | (work[103] & 32)))) // 3)
    work[143] = b(work[143] - (0x12 | (sel(aux[9], sel(matrix[8], work[35], aux[7]), aux[8] // 3)
                                       & (0x4D | ((work[172] >> 1) & 0x20)))))
    matrix[29] = 162
    hash_[15] = b(hash_[15] + maj(m(149) ^ sq(work[43]), sel(95, h(125), si(aux[1])) >> 1, 115))
    aux[9] = b(aux[9] - hi(aux[7]))
    hash_[7] = b(hash_[7] - sq(rz(ma(5), b(-m(17) * (m(17) & 1)))))
    matrix[8] = b(matrix[8] + cube(s(202)) - work[184])
    hash_[16] = b(m(102) << 1) & 0x84
    aux[6] = b(aux[6] ^ (si(aux[7]) >> 1))
    hash_[7] = b(hash_[7] - (h(191) - sel(177, si(si(aux[1])), b(s(80) << 1))))
    hash_[6] = h(119)
    hash_[12] = b((hi(aux[8]) ^ b(m(71) + m(15))) & maj((work[118] & ~0x2C) | 2, sq(hi(aux[9])), 27))
    digest_index = sel(0xA9, b(s(57) * 231), maj(work[32], ma(1), 23)) // 5
    seed_sample = si(aux[6])
    aux[5] = b(maj((seed_sample & 0x1C) | (h(82) & 0xA2) | (si(digest_index) & 0x41),
                   maj(cube(hi(aux[7])), work[82], 92), 192) ^ digest_index)
    matrix[25] = b(matrix[25] ^ (2 * hi(aux[9]) * work[5] - (rz(aux[4], seed_sample & 7) & b(aux[3] + 110))))


def sap_hash(block: bytes) -> bytes:
    """fairplaySAPHash of one 64-byte block: 16 bytes."""
    if len(block) < 64:
        raise FairPlayError("the SAP hash takes 64-byte blocks")
    hash_, matrix, aux = list(T.SAP_HASH), list(T.SAP_MATRIX), [0] * 10
    work = [block[(i & 63) ^ 3] for i in range(210)]
    for i in range(840):  # Go's uint32 index wraps below zero, which changes the first pass
        x = work[((i - 155) & 0xFFFFFFFF) % 210]
        y = work[((i - 57) & 0xFFFFFFFF) % 210]
        z = work[((i - 13) & 0xFFFFFFFF) % 210]
        w = work[i % 210]
        work[i % 210] = _b(_rotl8(y, 5) + (_rotl8(z, 3) ^ w) - _rotl8(x, 7))
    _nonlinear_circuit(hash_, matrix, aux, work)
    out = [0] * 16
    out[0:3] = aux[0:3]
    out[4:11] = aux[3:10]
    out = [_b(v + 0xE1) for v in out]
    out[3], out[11] = 0x3D, 0x3C
    out[10] ^= aux[3] ^ 133
    for i, value in enumerate(work):
        if i < len(matrix):
            value ^= matrix[i]
        if i < len(hash_):
            value ^= hash_[i]
        out[i & 15] ^= value
    for i in range(256):
        out[i & 15] ^= _rotl8(out[(i - 7) & 15], 1) ^ _rotl8(out[(i - 5) & 15], 6) ^ _rotl8(out[(i - 1) & 15], 5)
    return bytes(out)


# -- FairPlay's modified MD5 compression (fairplay_md5.go) -------------------------------------

SWAP, CYCLE, KDF = 0, 1, 2  # how the message schedule is changed after round 31

_MD5_SHIFT = (7, 12, 17, 22) * 4 + (5, 9, 14, 20) * 4 + (4, 11, 16, 23) * 4 + (6, 10, 15, 21) * 4
_MD5_CONSTANT = tuple(int(abs(__import__("math").sin(i + 1)) * 2 ** 32) & 0xFFFFFFFF for i in range(64))


def md5_compress(state: Sequence[int], block: bytes, mutation: int) -> tuple[int, int, int, int]:
    """One compression of a 64-byte block. The words are read big-endian, and after round 31 the
    message schedule is changed in one of three ways, so hashlib's MD5 cannot do this."""
    message = list(struct.unpack(">16I", block[:64]))
    a, b, c, d = state
    mask = 0xFFFFFFFF
    for rnd in range(64):
        if rnd < 16:
            f, word = (b & c) | (~b & d), rnd
        elif rnd < 32:
            f, word = (d & b) | (~d & c), (5 * rnd + 1) & 15
        elif rnd < 48:
            f, word = b ^ c ^ d, (3 * rnd + 5) & 15
        else:
            f, word = c ^ (b | (~d & mask)), (7 * rnd) & 15
        a, b, c, d = d, (b + _rotl32((a + f + _MD5_CONSTANT[rnd] + message[word]) & mask, _MD5_SHIFT[rnd])) & mask, b, c
        if rnd == 31:
            _mutate(message, a, b, c, d, mutation)
    return ((state[0] + a) & mask, (state[1] + b) & mask, (state[2] + c) & mask, (state[3] + d) & mask)


def _mutate(message: list[int], a: int, b: int, c: int, d: int, mutation: int) -> None:
    def swap(i: int, j: int) -> None:
        message[i], message[j] = message[j], message[i]

    if mutation in (SWAP, CYCLE):
        indices = [a & 15, b & 15, c & 15, d & 15, (a >> 4) & 15, (b >> 4) & 15, (c >> 4) & 15, (d >> 4) & 15]
        if mutation == SWAP:
            for i, j in enumerate(indices):
                swap(i, j)
        else:
            first = message[indices[0]]
            for i in range(len(indices) - 1):
                message[indices[i]] = message[indices[i + 1]]
            message[indices[-1]] = first
    elif mutation == KDF:
        swap(a & 15, b & 15)
        swap(c & 15, d & 15)
        for shift in (4, 8, 12):
            swap((a >> shift) & 15, (b >> shift) & 15)
    else:
        raise ValueError(f"unknown mutation {mutation}")


def _words_le(data: bytes) -> tuple[int, int, int, int]:
    return struct.unpack("<4I", data[:16])


def _bytes_be(words: Sequence[int]) -> bytes:
    return struct.pack(">4I", *words)


# -- the message cipher (fairplay_message.go): AES rounds with FairPlay's own round keys ------

_INV_SBOX = T.INVERSE_SBOX
_SBOX = bytes(_INV_SBOX.index(i) for i in range(256))


def _gf_mul(a: int, b: int) -> int:
    product = 0
    while b:
        if b & 1:
            product ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return product


_MUL = {n: bytes(_gf_mul(x, n) for x in range(256)) for n in (2, 3, 9, 11, 13, 14)}


def _shift_rows(state: list[int], inverse: bool) -> list[int]:
    out = [0] * 16
    for row in range(4):
        for column in range(4):
            source = (column - row + 4) & 3 if inverse else (column + row) & 3
            out[4 * column + row] = state[4 * source + row]
    return out


def _mix_columns(state: list[int], inverse: bool) -> list[int]:
    m2, m3, m9, m11, m13, m14 = (_MUL[n] for n in (2, 3, 9, 11, 13, 14))
    out = [0] * 16
    for column in range(4):
        o = column * 4
        a, b, c, d = state[o:o + 4]
        if inverse:
            out[o] = m14[a] ^ m11[b] ^ m13[c] ^ m9[d]
            out[o + 1] = m9[a] ^ m14[b] ^ m11[c] ^ m13[d]
            out[o + 2] = m13[a] ^ m9[b] ^ m14[c] ^ m11[d]
            out[o + 3] = m11[a] ^ m13[b] ^ m9[c] ^ m14[d]
        else:
            out[o] = m2[a] ^ m3[b] ^ c ^ d
            out[o + 1] = a ^ m2[b] ^ m3[c] ^ d
            out[o + 2] = a ^ b ^ m2[c] ^ m3[d]
            out[o + 3] = m3[a] ^ b ^ c ^ m2[d]
    return out


def _xor(state: Sequence[int], key: bytes) -> list[int]:
    return [x ^ k for x, k in zip(state, key)]


def _decrypt_block(block: bytes, mode: int) -> list[int]:
    state = _xor(block, T.MESSAGE_ROUND_KEY_10)
    for rnd in range(9, 0, -1):
        state = [_INV_SBOX[x] for x in _shift_rows(state, True)]
        state = _mix_columns(_xor(state, T.MESSAGE_MIDDLE_KEYS[mode][rnd - 1]), True)
    state = [_INV_SBOX[x] for x in _shift_rows(state, True)]
    return _xor(state, T.MESSAGE_ROUND_KEY_0)


def _encrypt_block(block: Sequence[int], mode: int) -> bytes:
    state = _shift_rows([_SBOX[x] for x in _xor(block, T.MESSAGE_ROUND_KEY_0)], False)
    for rnd in range(9):
        state = _xor(_mix_columns(state, False), T.MESSAGE_MIDDLE_KEYS[mode][rnd])
        state = _shift_rows([_SBOX[x] for x in state], False)
    return bytes(_xor(state, T.MESSAGE_ROUND_KEY_10))


def decrypt_message(message: bytes) -> bytes:
    """The 128-byte body at [16:144] of an FPLY message, decrypted in the mode at [12]
    (CBC over eight blocks)."""
    mode = message[12]
    if mode >= MODES:
        raise FairPlayError(f"unsupported FairPlay mode {mode}")
    out = bytearray(128)
    for block in range(8):
        start = 16 + block * 16
        chain = T.MESSAGE_IV[mode] if block == 0 else message[start - 16:start]
        plain = _decrypt_block(message[start:start + 16], mode)
        out[block * 16:block * 16 + 16] = bytes(p ^ c for p, c in zip(plain, chain))
    return bytes(out)


def encrypt_message(mode: int, plaintext: bytes) -> bytes:
    """The inverse of decrypt_message: a 128-byte body in `mode`."""
    if mode >= MODES:
        raise FairPlayError(f"unsupported FairPlay mode {mode}")
    if len(plaintext) != 128:
        raise FairPlayError("a FairPlay message body is 128 bytes")
    out = bytearray()
    chain = T.MESSAGE_IV[mode]
    for block in range(8):
        chain = _encrypt_block(_xor(plaintext[block * 16:block * 16 + 16], chain), mode)
        out += chain
    return bytes(out)


def _decrypt_body(mode: int, payload: bytes) -> bytes:
    message = bytearray(144)
    message[12] = mode
    message[16:144] = payload
    return decrypt_message(bytes(message))


# -- the white-box exchange (fpsap.go) ---------------------------------------------------------

def _padded_digest_input(prefix: bytes, first: bytes, second: bytes, suffix: bytes) -> bytes:
    data = bytearray(320)
    body = prefix + first + second + suffix
    data[:len(body)] = body
    data[len(body)] = 0x80
    struct.pack_into("<Q", data, 312, len(body) * 8)
    return bytes(data)


def descriptor(m3_sap: bytes, m2_sap: bytes) -> bytes:
    """fpsapDescriptorForSAP: the 20-byte seed of the white box from both SAPs (also the
    ekey's MAC key)."""
    padded = _padded_digest_input(T.DESCRIPTOR_PREFIX, m3_sap, m2_sap, T.DESCRIPTOR_SUFFIX)
    state = list(_words_le(T.INITIAL_SESSION_KEY))
    first_final = state
    for offset in range(0, len(padded), 64):
        block = padded[offset:offset + 64]
        add = _words_le(sap_hash(block))
        state = [(s + a) & 0xFFFFFFFF for s, a in zip(state, add)]
        state = list(md5_compress(state, block, CYCLE))
        if offset == len(padded) - 64:
            first_final = state
            state = list(md5_compress(state, block, CYCLE))
    return struct.pack(">I", first_final[0]) + _bytes_be(state)


def _masks(seed: bytes) -> list[bytes]:
    masks = []
    for i in range(9):
        block = bytearray(64)
        block[:20] = seed
        block[20] = i
        block[21:36] = T.MASK_SUFFIX
        block[36] = 0x80
        struct.pack_into("<I", block, 56, 0x320)
        masks.append(_bytes_be(md5_compress(T.MASK_STATE, bytes(block), SWAP)))
    return masks


def _digest32(left: bytes, right: bytes) -> bytes:
    block = bytearray(64)
    block[:16] = left
    block[16:32] = right
    block[32] = 0x80
    struct.pack_into("<I", block, 56, 0x100)
    return _bytes_be(md5_compress(T.DIGEST32_STATE, bytes(block), SWAP))


def _sub(lookup: tuple[int, int, int], value: int) -> int:
    base, in_xor, out_xor = lookup
    return T.SUBSTITUTION_BASES[base][value ^ in_xor] ^ out_xor


def _mix_lookup(lookup: tuple[int, int, int], value: int) -> int:
    base, in_xor, out_xor = lookup
    return T.MIX_BASES[base][value ^ in_xor] ^ out_xor


def _network_mix(mix: Sequence[tuple[int, int, int]], substituted: Sequence[int]) -> list[int]:
    state = [0] * 16
    for word in range(4):
        offset = word * 4
        for out_byte in range(4):
            mixed = 0
            for in_byte in range(4):
                mixed ^= _mix_lookup(mix[in_byte * 4 + out_byte], substituted[offset + in_byte])
            state[offset + out_byte] = mixed
    return state


def _first_network(masks: list[bytes]) -> bytes:
    state = [x ^ k for x, k in zip(T.FIXED_BLOCK, T.FIRST_INPUT_MASK)]
    for bank in range(9):
        substituted = [0] * 16
        for output, inp in enumerate(T.FIRST_POSITION_MAP):
            substituted[output] = _sub(T.FIRST_ROUNDS[bank * 16 + inp], state[inp])
        state = _network_mix(T.FIRST_MIX, substituted)
        state = [x ^ k for x, k in zip(state, masks[bank])]
    out = [0] * 16
    for output, inp in enumerate(T.FIRST_POSITION_MAP):
        out[output] = _sub(T.FIRST_FINAL[inp], state[inp])
    return bytes(out)


def _second_network(state_bytes: bytes, masks: list[bytes]) -> bytes:
    state = list(state_bytes)
    for bank in range(8, -1, -1):
        substituted = [0] * 16
        for output, inp in enumerate(T.SECOND_POSITION_MAP):
            substituted[output] = _sub(T.SECOND_ROUNDS[bank * 16 + output], state[inp]) ^ masks[bank][output]
        state = _network_mix(T.SECOND_MIX, substituted)
    out = [0] * 16
    for output, inp in enumerate(T.SECOND_POSITION_MAP):
        out[output] = _sub(T.SECOND_FINAL[output], state[inp]) ^ T.SECOND_OUTPUT_MASK[output]
    return bytes(out)


def exchange(m3_sap: bytes, m2_sap: bytes) -> bytes:
    """fpsapExchangeForSAP: the 20 bytes that end m3 (and that m4 repeats)."""
    masks = _masks(descriptor(m3_sap, m2_sap))
    intermediate = _first_network(masks)
    left = _digest32(intermediate, T.FIXED_BLOCK)
    output = _second_network(left, masks)
    return output[:4] + _digest32(left, output)


# -- records, key wrapping (fairplay_crypto.go) ------------------------------------------------

def _record(kind: int, payload_length: int) -> bytearray:
    record = bytearray(12 + payload_length)
    record[:8] = b"FPLY" + bytes((3, 1, kind, 0))
    struct.pack_into(">I", record, 8, payload_length)
    return record


def _check_record(record: bytes, kind: int, payload_length: int) -> None:
    if len(record) != 12 + payload_length:
        raise FairPlayError(f"length {len(record)}, want {12 + payload_length}")
    if record[:4] != b"FPLY":
        raise FairPlayError(f"magic {record[:4].hex()}, want FPLY")
    if tuple(record[4:8]) != (3, 1, kind, 0):
        raise FairPlayError(f"version/type {record[4:8].hex()}")
    if struct.unpack_from(">I", record, 8)[0] != payload_length:
        raise FairPlayError("declared payload length")


def wrapping_key(receiver_sap: bytes, m3: bytes) -> bytes:
    """deriveFairPlayWrappingKey: the AES key the ekey wraps the raw key with."""
    material = _padded_digest_input(T.KDF_PREFIX, decrypt_message(m3), receiver_sap, T.KDF_SUFFIX)
    state = list(_words_le(T.INITIAL_SESSION_KEY))
    for offset in range(0, len(material), 64):
        block = material[offset:offset + 64]
        modified = md5_compress(state, block, KDF)
        hashed = _words_le(sap_hash(block))
        state = [(x + y) & 0xFFFFFFFF for x, y in zip(modified, hashed)]
    return _bytes_be(state)


def _aes_block(key: bytes, block: bytes, decrypt: bool = False) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    op = cipher.decryptor() if decrypt else cipher.encryptor()
    return op.update(block) + op.finalize()


def wrap_key(receiver_sap: bytes, m3: bytes, raw_key: bytes, mask: bytes) -> bytes:
    """The 72-byte ekey: header, mask, length, HMAC-SHA1, AES(raw_key XOR mask)."""
    _check_record(m3, 3, 152)
    if m3[12] >= MODES:
        raise FairPlayError(f"unsupported FairPlay mode {m3[12]}")
    if len(raw_key) != 16 or len(mask) != 16:
        raise FairPlayError("the raw key and its mask are 16 bytes")
    head = T.EKEY_HEADER + mask + struct.pack(">I", len(raw_key))
    masked = bytes(k ^ x for k, x in zip(raw_key, mask))
    wrapped = _aes_block(wrapping_key(receiver_sap, m3), masked)
    mac_key = descriptor(decrypt_message(m3), receiver_sap)
    mac = hmac.new(mac_key, head + raw_key, hashlib.sha1).digest()
    return head + mac + wrapped


def unwrap_key(receiver_sap: bytes, m3: bytes, ekey: bytes) -> bytes:
    """What the receiver does with an ekey (for tests)."""
    plain = _aes_block(wrapping_key(receiver_sap, m3), ekey[56:72], decrypt=True)
    return bytes(p ^ x for p, x in zip(plain, ekey[16:32]))


def stream_key(raw_key: bytes, shared_secret: Optional[bytes]) -> bytes:
    """The key a HomeKit-paired receiver takes from the ekey's raw key:
    SHA-512(raw key || pair-verify secret)[:16]; the raw key itself without a secret."""
    if not shared_secret:
        return raw_key
    return hashlib.sha512(raw_key + shared_secret).digest()[:16]


class FairPlaySession:
    """One handshake: m1, then m3 from the receiver's m2, its m4 checked, then the ekey."""

    def __init__(self, entropy: Callable[[int], bytes] = os.urandom) -> None:
        self._entropy = entropy
        self.local_sap = b"\x00\x01" + entropy(126)
        self.remote_sap = b""
        self.m3 = b""

    @staticmethod
    def message1() -> bytes:
        m1 = _record(1, len(T.M1_PAYLOAD))
        m1[12:] = T.M1_PAYLOAD
        return bytes(m1)

    def exchange_m3(self, m2: bytes) -> bytes:
        try:
            _check_record(m2, 2, 130)
        except FairPlayError as ex:
            raise FairPlayError(f"invalid m2: {ex}") from ex
        if m2[12] != 2:
            raise FairPlayError(f"invalid m2 payload marker {m2[12]}")
        mode = m2[13]
        if mode >= MODES:
            raise FairPlayError(f"m2 asks for mode {mode}, which FairPlay SAP does not have")
        m3 = _record(3, 152)
        m3[12] = mode
        m3[13:16] = T.M3_LABEL
        m3[16:144] = encrypt_message(mode, self.local_sap)
        self.remote_sap = _decrypt_body(mode, m2[14:142])
        m3[144:] = exchange(self.local_sap, self.remote_sap)
        self.m3 = bytes(m3)
        return self.m3

    def confirm_m4(self, m4: bytes) -> None:
        if not self.m3:
            raise FairPlayError("m4 before m3")
        try:
            _check_record(m4, 4, 20)
        except FairPlayError as ex:
            raise FairPlayError(f"invalid m4: {ex}") from ex
        if m4[12:] != self.m3[144:]:
            raise FairPlayError("m4 does not confirm m3")

    def wrap_key(self, raw_key: bytes) -> bytes:
        if not self.m3:
            raise FairPlayError("a key wrapped before m3")
        return wrap_key(self.remote_sap, self.m3, raw_key, self._entropy(16))
