"""AES-128-ECB tests: FIPS-197 vectors plus an equivalence check.

The cross-check against ``cryptography`` runs only when that package is
importable (it is installed locally, but deliberately *not* a runtime
dependency any more -- see ``src/core/aes.py``).
"""
from __future__ import annotations

import json

import pytest

from src.core.aes import BLOCK_SIZE, KEY_SIZE, aes_ecb_encrypt, encrypt_block, expand_key, pkcs7_pad
from src.core.crypto import EAPI_KEY, sign_eapi

# FIPS-197, appendix B/C.1
FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CIPHERTEXT = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


def test_fips_197_aes128_vector():
    assert encrypt_block(FIPS_KEY, FIPS_PLAINTEXT) == FIPS_CIPHERTEXT


def test_key_schedule_is_reusable_and_deterministic():
    schedule = expand_key(FIPS_KEY)
    assert len(schedule) == 44
    assert encrypt_block(schedule, FIPS_PLAINTEXT) == FIPS_CIPHERTEXT
    assert expand_key(FIPS_KEY) == schedule


def test_zero_key_matches_a_known_vector():
    # AES-128 with an all-zero key, all-zero block (independent reference).
    assert encrypt_block(bytes(16), bytes(16)).hex() == "66e94bd4ef8a2c3b884cfa59ca342b2e"


def test_key_and_block_size_are_validated():
    with pytest.raises(ValueError):
        expand_key(b"too short")
    with pytest.raises(ValueError):
        encrypt_block(FIPS_KEY, b"not 16 bytes")


@pytest.mark.parametrize("length", [0, 1, 15, 16, 17, 31, 32, 33, 64])
def test_pkcs7_padding(length):
    padded = pkcs7_pad(b"a" * length)
    assert len(padded) % BLOCK_SIZE == 0
    assert len(padded) > length
    assert padded[-1] == len(padded) - length
    assert padded[:length] == b"a" * length


def test_ecb_encrypt_output_is_block_aligned_hex():
    encrypted = aes_ecb_encrypt(b"hello world", EAPI_KEY)
    assert len(encrypted) % BLOCK_SIZE == 0
    assert encrypted != b"hello world"


def test_ecb_encrypt_is_deterministic():
    assert aes_ecb_encrypt(b"payload", EAPI_KEY) == aes_ecb_encrypt(b"payload", EAPI_KEY)


def test_key_size_constant():
    assert KEY_SIZE == 16
    assert len(EAPI_KEY) == KEY_SIZE


def test_sign_eapi_output_shape():
    params = sign_eapi("/api/song/enhance/player/url/v1", {"ids": json.dumps([1])})
    assert params == params.upper()
    assert len(params) % (BLOCK_SIZE * 2) == 0
    bytes.fromhex(params)  # must be valid hex


@pytest.mark.parametrize("payload", [
    {},
    {"a": 1},
    {"ids": json.dumps([1, 2, 3]), "level": "standard", "encodeType": "mp3"},
    {"长": "中文", "nested": {"list": [1, 2, 3]}},
    {"header": {"os": "pc", "appver": "8.9.70", "deviceId": "x" * 32}},
])
def test_matches_the_cryptography_reference(payload):
    """Same bytes as the well-tested library == our implementation is correct."""
    cryptography = pytest.importorskip("cryptography")
    import hashlib

    from cryptography.hazmat.primitives import padding as ref_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    path = "/api/test/endpoint"
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(f"nobody{path}use{text}md5forencrypt".encode()).hexdigest()
    message = f"{path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode()

    padder = ref_padding.PKCS7(128).padder()
    padded = padder.update(message) + padder.finalize()
    encryptor = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).encryptor()
    expected = (encryptor.update(padded) + encryptor.finalize()).hex().upper()

    assert sign_eapi(path, payload) == expected
    assert cryptography is not None


# --------------------------------------------------------------- decryption

def test_fips_197_decrypt_vector():
    from src.core.aes import decrypt_block

    assert decrypt_block(FIPS_KEY, FIPS_CIPHERTEXT) == FIPS_PLAINTEXT


def test_roundtrip_many_sizes_and_payloads():
    from src.core.aes import aes_ecb_decrypt

    for length in (0, 1, 15, 16, 17, 63, 64, 200):
        for data in (b"x" * length, bytes(range(256))[:length]):
            encrypted = aes_ecb_encrypt(data, EAPI_KEY)
            assert aes_ecb_decrypt(encrypted, EAPI_KEY) == data


def test_roundtrip_of_a_signed_payload():
    """The eapi signature must decrypt back to the exact signed message."""
    from src.core.aes import aes_ecb_decrypt

    path = "/api/song/enhance/player/url/v1"
    payload = {"ids": "[1,2]", "level": "hires", "讯息": "中文"}
    message = aes_ecb_decrypt(bytes.fromhex(sign_eapi(path, payload)), EAPI_KEY)
    text = message.decode("utf-8")
    assert text.startswith(f"{path}-36cd479b6b5-")
    assert text.endswith("-36cd479b6b5-" + __import__("hashlib").md5(
        f"nobody{path}use{text.split('-36cd479b6b5-')[1]}md5forencrypt".encode()).hexdigest())


def test_pkcs7_unpad_validates():
    from src.core.aes import pkcs7_unpad

    with pytest.raises(ValueError):
        pkcs7_unpad(b"")
    with pytest.raises(ValueError):
        pkcs7_unpad(b"not a multiple of 16")
    with pytest.raises(ValueError):
        pkcs7_unpad(b"a" * 15 + b"\x05")   # padding claims 5 bytes, only 1 present
    assert pkcs7_unpad(b"a" * 15 + b"\x01") == b"a" * 15


def test_decrypt_block_validates_the_block_size():
    from src.core.aes import decrypt_block

    with pytest.raises(ValueError):
        decrypt_block(FIPS_KEY, b"short")


def test_no_native_crypto_dependency():
    """The whole point of src/core/aes.py: no cryptography / OpenSSL in the build."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((root / "src").rglob("*.py")) + [root / "main.py"]:
        text = path.read_text(encoding="utf-8")
        if "import cryptography" in text or "from cryptography" in text:
            offenders.append(path.name)
    assert offenders == [], f"native crypto import found in {offenders}"
    spec = (root / "main.spec").read_text(encoding="utf-8")
    assert '"cryptography"' in spec, "the spec must keep excluding cryptography"
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptography" not in requirements
