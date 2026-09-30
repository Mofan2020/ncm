"""AES-128-ECB, pure Python (no native dependencies).

Why not ``cryptography``/``pycryptodome``?  The eapi signer only needs to
encrypt a few hundred bytes per request, but a native crypto extension drags an
OpenSSL linkage into the PyInstaller bundle: on the Intel macOS runner the
bundled ``libssl.3.dylib`` shadowed the one the wheel expected and the frozen app
died with ``Symbol not found: _SSL_get0_group_name``.  A ~120 line, dependency
free implementation that is verified against FIPS-197 test vectors (see
``tests/test_aes.py``) is the more robust choice for this project.

Only encryption is needed, and only in ECB mode with a fixed key.
"""
from __future__ import annotations

__all__ = [
    "aes_ecb_decrypt",
    "aes_ecb_encrypt",
    "decrypt_block",
    "encrypt_block",
    "expand_key",
    "pkcs7_pad",
    "pkcs7_unpad",
]

# FIPS-197 S-box.
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)

BLOCK_SIZE = 16
KEY_SIZE = 16


def _xtime(value: int) -> int:
    """Multiply by x (0x02) in GF(2^8)."""
    value <<= 1
    return (value ^ 0x1B) & 0xFF if value & 0x100 else value


def expand_key(key: bytes) -> list[list[int]]:
    """Return the 44 key-schedule words (AES-128) as lists of 4 bytes."""
    if len(key) != KEY_SIZE:
        raise ValueError(f"AES-128 needs a {KEY_SIZE} byte key, got {len(key)}")
    words = [list(key[index * 4:index * 4 + 4]) for index in range(4)]
    for index in range(4, 44):
        temp = list(words[index - 1])
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[byte] for byte in temp]
            temp[0] ^= _RCON[index // 4 - 1]
        previous = words[index - 4]
        words.append([previous[position] ^ temp[position] for position in range(4)])
    return words


def _add_round_key(state: list[list[int]], round_key: list[list[int]]) -> None:
    for column in range(4):
        for row in range(4):
            state[column][row] ^= round_key[column][row]


def _sub_bytes(state: list[list[int]]) -> None:
    for column in range(4):
        state[column] = [_SBOX[byte] for byte in state[column]]


def _shift_rows(state: list[list[int]]) -> None:
    for row in range(1, 4):
        values = [state[column][row] for column in range(4)]
        values = values[row:] + values[:row]
        for column in range(4):
            state[column][row] = values[column]


def _mix_columns(state: list[list[int]]) -> None:
    for column in range(4):
        a0, a1, a2, a3 = state[column]
        total = a0 ^ a1 ^ a2 ^ a3
        state[column] = [
            a0 ^ total ^ _xtime(a0 ^ a1),
            a1 ^ total ^ _xtime(a1 ^ a2),
            a2 ^ total ^ _xtime(a2 ^ a3),
            a3 ^ total ^ _xtime(a3 ^ a0),
        ]


def encrypt_block(key: bytes | list[list[int]], block: bytes) -> bytes:
    """Encrypt exactly one 16-byte block (``key`` may be a key schedule)."""
    if len(block) != BLOCK_SIZE:
        raise ValueError(f"AES operates on {BLOCK_SIZE} byte blocks, got {len(block)}")
    schedule = key if isinstance(key, list) else expand_key(key)
    state = [list(block[column * 4:column * 4 + 4]) for column in range(4)]
    _add_round_key(state, schedule[:4])
    for round_index in range(1, 10):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, schedule[round_index * 4:round_index * 4 + 4])
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, schedule[40:44])
    return bytes(byte for column in state for byte in column)


def pkcs7_pad(data: bytes) -> bytes:
    """Pad to a multiple of 16 bytes (always adds 1..16 bytes)."""
    padding = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return data + bytes([padding]) * padding


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """Pad ``data`` with PKCS#7 and encrypt it with AES-128-ECB."""
    schedule = expand_key(key)
    padded = pkcs7_pad(data)
    return b"".join(
        encrypt_block(schedule, padded[offset:offset + BLOCK_SIZE])
        for offset in range(0, len(padded), BLOCK_SIZE)
    )


# --------------------------------------------------------------- decryption
# Only used to verify our own signer (round-trip tests); the eapi protocol never
# needs to decrypt anything.

#: Inverse S-box, derived from the table above so there is one source of truth.
_INV_SBOX = bytes(
    next(index for index, value in enumerate(_SBOX) if value == byte) for byte in range(256)
)


def _gmul(left: int, right: int) -> int:
    """Multiply two bytes in GF(2^8) (slow but obviously correct)."""
    result = 0
    while right:
        if right & 1:
            result ^= left
        left = _xtime(left)
        right >>= 1
    return result


def _inv_sub_bytes(state: list[list[int]]) -> None:
    for column in range(4):
        state[column] = [_INV_SBOX[byte] for byte in state[column]]


def _inv_shift_rows(state: list[list[int]]) -> None:
    for row in range(1, 4):
        values = [state[column][row] for column in range(4)]
        values = values[-row:] + values[:-row]
        for column in range(4):
            state[column][row] = values[column]


def _inv_mix_columns(state: list[list[int]]) -> None:
    for column in range(4):
        a0, a1, a2, a3 = state[column]
        state[column] = [
            _gmul(a0, 0x0E) ^ _gmul(a1, 0x0B) ^ _gmul(a2, 0x0D) ^ _gmul(a3, 0x09),
            _gmul(a0, 0x09) ^ _gmul(a1, 0x0E) ^ _gmul(a2, 0x0B) ^ _gmul(a3, 0x0D),
            _gmul(a0, 0x0D) ^ _gmul(a1, 0x09) ^ _gmul(a2, 0x0E) ^ _gmul(a3, 0x0B),
            _gmul(a0, 0x0B) ^ _gmul(a1, 0x0D) ^ _gmul(a2, 0x09) ^ _gmul(a3, 0x0E),
        ]


def decrypt_block(key: bytes | list[list[int]], block: bytes) -> bytes:
    """Decrypt exactly one 16-byte block (``key`` may be a key schedule)."""
    if len(block) != BLOCK_SIZE:
        raise ValueError(f"AES operates on {BLOCK_SIZE} byte blocks, got {len(block)}")
    schedule = key if isinstance(key, list) else expand_key(key)
    state = [list(block[column * 4:column * 4 + 4]) for column in range(4)]
    _add_round_key(state, schedule[40:44])
    for round_index in range(9, 0, -1):
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, schedule[round_index * 4:round_index * 4 + 4])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, schedule[:4])
    return bytes(byte for column in state for byte in column)


def pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding, validating it."""
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("padded data must be a non-empty multiple of 16 bytes")
    padding = data[-1]
    if not 1 <= padding <= BLOCK_SIZE or data[-padding:] != bytes([padding]) * padding:
        raise ValueError("invalid PKCS#7 padding")
    return data[:-padding]


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """Reverse :func:`aes_ecb_encrypt` (PKCS#7 padding removed)."""
    schedule = expand_key(key)
    plain = b"".join(
        decrypt_block(schedule, data[offset:offset + BLOCK_SIZE])
        for offset in range(0, len(data), BLOCK_SIZE)
    )
    return pkcs7_unpad(plain)
