"""RSA encryption for the Bilibili password, pure Python (no native dependencies).

Why not ``cryptography``/``pycryptodome``?  The frozen builds link no OpenSSL
of their own -- the same reason :mod:`src.core.aes` exists.  Only the
*public* key operation is needed here (the server holds the private key), so
this is a few dozen lines: parse the PEM, then a textbook ``m^e mod n`` with
PKCS#1 v1.5 padding.

The key Bilibili hands out is 1024 bit, so the padding has 128 bytes of room
for the salt and the password -- enough for any real password.

The implementation is checked against a known answer in
``tests/test_bili_rsa.py``.
"""
from __future__ import annotations

import base64
import hashlib
import os

__all__ = [
    "bili_password_cipher",
    "encrypt_pkcs1_v15",
    "parse_public_key",
]


def _der_length(data: bytes, offset: int) -> tuple[int, int]:
    """Read one DER length field; return ``(value, next_offset)``."""
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    count = first & 0x7F
    if count == 0 or count > 4:
        raise ValueError("unsupported DER length")
    value = int.from_bytes(data[offset:offset + count], "big")
    return value, offset + count


def _der_sequence(data: bytes, offset: int) -> tuple[list[int], int]:
    """Read a DER SEQUENCE of INTEGERs; return ``(numbers, next_offset)``."""
    if data[offset] != 0x30:
        raise ValueError("not a DER sequence")
    length, offset = _der_length(data, offset + 1)
    end = offset + length
    numbers: list[int] = []
    while offset < end:
        if data[offset] != 0x02:
            raise ValueError("not a DER integer")
        size, offset = _der_length(data, offset + 1)
        numbers.append(int.from_bytes(data[offset:offset + size], "big"))
        offset += size
    return numbers, end


def _der_element(data: bytes, offset: int) -> tuple[bytes, int]:
    """Read one DER element of any tag; return ``(content, next_offset)``."""
    length, offset = _der_length(data, offset + 1)
    return data[offset:offset + length], offset + length


def parse_public_key(pem: str) -> tuple[int, int]:
    """Return ``(modulus, exponent)`` from a PEM encoded public key."""
    text = (pem or "").strip()
    if not text:
        raise ValueError("empty public key")
    body = []
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-----BEGIN"):
            inside = True
            continue
        if line.startswith("-----END"):
            break
        if inside and line:
            body.append(line)
    if not body:
        raise ValueError("no key body")
    raw = base64.b64decode("".join(body))
    # SubjectPublicKeyInfo: SEQUENCE { AlgorithmIdentifier, BIT STRING }.
    # The BIT STRING carries the real key: SEQUENCE { modulus, exponent }.
    # A bare PKCS#1 key (SEQUENCE { modulus, exponent }) is accepted too.
    inner: list[int] = []
    if raw[0] == 0x30:
        length, offset = _der_length(raw, 1)
        end = offset + length
        while offset < end:
            tag = raw[offset]
            content, offset = _der_element(raw, offset)
            if tag == 0x03:
                # BIT STRING: the first byte counts the unused bits.
                inner, _ = _der_sequence(content[1:], 0)
                break
        else:
            inner, _ = _der_sequence(raw, 0)
    else:
        inner, _ = _der_sequence(raw, 0)
    if len(inner) < 2:
        raise ValueError("not an RSA public key")
    modulus, exponent = inner[0], inner[1]
    if modulus <= 0 or exponent <= 0:
        raise ValueError("broken public key")
    return modulus, exponent


def encrypt_pkcs1_v15(message: bytes, modulus: int, exponent: int) -> bytes:
    """Encrypt one block with RSA/ECB/PKCS1Padding (type 2)."""
    size = (modulus.bit_length() + 7) // 8
    if len(message) > size - 11:
        raise ValueError("message too long for this key")
    # 0x00 0x02 <random non-zero bytes> 0x00 <message>
    padding = bytearray()
    while len(padding) < size - len(message) - 3:
        byte = os.urandom(1)
        # The padding bytes must not be zero: zero marks the end of the pad.
        if byte != b"\x00":
            padding.extend(byte)
    block = b"\x00\x02" + bytes(padding) + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), exponent, modulus)
    return cipher.to_bytes(size, "big")


def bili_password_cipher(password: str, hash_salt: str, pem: str) -> str:
    """Encrypt a password the way the Bilibili clients do.

    The salt from ``/x/passport-login/web/key`` is prepended to the password,
    the result is encrypted with RSA/ECB/PKCS1Padding and handed over as
    base64.
    """
    plain = (str(hash_salt or "") + str(password or "")).encode("utf-8")
    modulus, exponent = parse_public_key(pem)
    return base64.b64encode(
        encrypt_pkcs1_v15(plain, modulus, exponent)
    ).decode("ascii")


def bili_password_cipher_hash(password: str, hash_salt: str, pem: str) -> str:
    """Same as :func:`bili_password_cipher` but salted with the MD5 hash.

    Some of the routes (the app login among them) want the salt to be the
    hex MD5 of the salt plus the password instead of the raw salt.
    """
    mixed = hashlib.md5(
        (str(hash_salt or "") + str(password or "")).encode("utf-8")
    ).hexdigest()
    plain = (str(hash_salt or "") + mixed).encode("utf-8")
    modulus, exponent = parse_public_key(pem)
    return base64.b64encode(
        encrypt_pkcs1_v15(plain, modulus, exponent)
    ).decode("ascii")
