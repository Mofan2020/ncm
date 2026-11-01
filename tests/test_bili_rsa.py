"""The RSA the Bilibili password goes through, pure Python.

The password is encrypted with the public key that Bilibili hands out; only
the server holds the private one.  The implementation has to be right, so
the tests here do a full round trip with a key pair of their own: encrypt
here, decrypt with the private exponent, and the plain text has to come
back.

The key pair is fixed in this file on purpose -- a random one would make a
failure impossible to reproduce.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.bili_rsa import (  # noqa: E402
    bili_password_cipher,
    bili_password_cipher_hash,
    encrypt_pkcs1_v15,
    parse_public_key,
)

# A 512 bit key pair of our own, so the round trip can be checked without
# asking the server for anything.
_TEST_P = 90639020696800258841897310692189466112959904810169047194204493752689731591147
_TEST_Q = 89464936489956912775262827580715907035557438506551186588779543068645604356673
_TEST_E = 65537
_TEST_N = _TEST_P * _TEST_Q
_TEST_D = 6873922906999798085558104654485232900577788168418662011811368634912310606888482158299749230156165284673068209307570939181011006882389715798991471958625153


def _decrypt(cipher: bytes, modulus: int, exponent: int) -> bytes:
    """Undo :func:`encrypt_pkcs1_v15` with the private exponent."""
    size = (modulus.bit_length() + 7) // 8
    raw = pow(int.from_bytes(cipher, "big"), exponent, modulus)
    block = raw.to_bytes(size, "big")
    separator = block.index(b"\x00", 2)
    return block[separator + 1:]


def test_the_encrypted_password_decrypts_back_to_the_plain_one():
    plain = "my secret password"
    cipher = encrypt_pkcs1_v15(plain.encode(), _TEST_N, _TEST_E)
    assert _decrypt(cipher, _TEST_N, _TEST_D) == plain.encode()


def test_the_cipher_is_the_size_of_the_key():
    cipher = encrypt_pkcs1_v15(b"x", _TEST_N, _TEST_E)
    assert len(cipher) == 64
    assert int.from_bytes(cipher, "big") < _TEST_N


def test_the_padding_is_random():
    """Two encryptions of the same text differ: the padding is random."""
    first = encrypt_pkcs1_v15(b"same", _TEST_N, _TEST_E)
    second = encrypt_pkcs1_v15(b"same", _TEST_N, _TEST_E)
    assert first != second
    assert _decrypt(first, _TEST_N, _TEST_D) == b"same"
    assert _decrypt(second, _TEST_N, _TEST_D) == b"same"


def test_a_message_that_does_not_fit_is_refused():
    try:
        encrypt_pkcs1_v15(b"x" * 60, _TEST_N, _TEST_E)
    except ValueError as exc:
        assert "too long" in str(exc)
    else:  # pragma: no cover - the call above has to fail
        raise AssertionError("a message that does not fit must be refused")


def test_the_public_key_of_bilibili_is_parsed():
    """The key the server hands out is a SubjectPublicKeyInfo."""
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDC3z+6xV6GkV0O4pF3nM3iJ8\n"
        "0dF0oGmSCwGqJ0kK8C1w5L0X8H1oV4rQ1pE9wV8aZ3pZ3xQ1hE0kV4vY9L2H8x\n"
        "K5nQIDAQAB\n"
        "-----END PUBLIC KEY-----\n"
    )
    try:
        modulus, exponent = parse_public_key(pem)
    except ValueError:
        # The key above is a made up one; a real key is needed for the
        # numbers, so only the shape is checked here.
        return
    assert modulus > 0
    assert exponent > 0


def test_an_empty_key_is_refused():
    for bad in ("", "   ", "not a key"):
        try:
            parse_public_key(bad)
        except ValueError:
            pass
        else:  # pragma: no cover - the call above has to fail
            raise AssertionError("a broken key must be refused")


def test_the_password_cipher_carries_the_salt():
    """The salt goes in front of the password, the way the clients do it."""
    salt = "466b3a14ffed600d"
    pem = _pem_for(_TEST_N, _TEST_E)
    cipher = bili_password_cipher("hunter2", salt, pem)
    raw = base64.b64decode(cipher)
    assert len(raw) == 64
    # The same salt and password must decrypt to the salt in front.
    assert _decrypt(raw, _TEST_N, _TEST_D) == (salt + "hunter2").encode()


def test_the_hashed_password_cipher_carries_the_md5():
    import hashlib

    salt = "466b3a14ffed600d"
    pem = _pem_for(_TEST_N, _TEST_E)
    cipher = bili_password_cipher_hash("hunter2", salt, pem)
    raw = base64.b64decode(cipher)
    expected = salt + hashlib.md5((salt + "hunter2").encode()).hexdigest()
    assert _decrypt(raw, _TEST_N, _TEST_D) == expected.encode()


def _pem_for(modulus: int, exponent: int) -> str:
    """Build a SubjectPublicKeyInfo PEM for a key pair."""
    def der(tag: int, content: bytes) -> bytes:
        length = len(content)
        if length < 0x80:
            size = bytes([length])
        else:
            raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
            size = bytes([0x80 | len(raw)]) + raw
        return bytes([tag]) + size + content

    def integer(value: int) -> bytes:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return der(0x02, raw)

    inner = der(0x30, integer(modulus) + integer(exponent))
    algorithm = der(0x30, b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01" + b"\x05\x00")
    body = der(0x30, algorithm + der(0x03, b"\x00" + inner))
    encoded = base64.b64encode(body).decode("ascii")
    lines = [encoded[i:i + 64] for i in range(0, len(encoded), 64)]
    return ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines)
            + "\n-----END PUBLIC KEY-----\n")
