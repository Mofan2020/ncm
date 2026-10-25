"""Tests for the eapi request signing."""
from __future__ import annotations

import binascii
import json
import re
import time

from src.core.crypto import (
    EAPI_KEY,
    EAPI_SEPARATOR,
    build_client_header,
    eapi_body,
    encode_type_for_level,
    sign_eapi,
)


def _decode(params: str) -> bytes:
    """Decrypt our own signed payload with the same (dependency-free) AES."""
    from src.core.aes import aes_ecb_decrypt

    raw = binascii.unhexlify(params)
    assert len(raw) % 16 == 0, "AES block size"
    return aes_ecb_decrypt(raw, EAPI_KEY)


def test_signature_layout_and_padding():
    path = "/api/song/enhance/player/url/v1"
    payload = {"ids": "[1]", "level": "standard"}

    params = sign_eapi(path, payload)
    assert re.fullmatch(r"[0-9A-F]+", params), "params must be upper-case hex"
    assert len(params) % 32 == 0

    plain = _decode(params).decode("utf-8")
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    assert plain.startswith(f"{path}{EAPI_SEPARATOR}{text}{EAPI_SEPARATOR}")
    digest = plain.rsplit(EAPI_SEPARATOR, 1)[1]
    assert re.fullmatch(r"[0-9a-f]{32}", digest)


def test_signature_is_deterministic_for_same_payload():
    assert sign_eapi("/api/x", {"a": 1}) == sign_eapi("/api/x", {"a": 1})


def test_non_ascii_payload_roundtrip():
    payload = {"keyword": "热歌榜", "ids": "[347230]"}
    plain = _decode(sign_eapi("/api/search", payload)).decode("utf-8")
    assert "热歌榜" in plain, "payload must not be escaped"


def test_encode_type_mapping():
    assert encode_type_for_level("standard") == "mp3"
    assert encode_type_for_level("exhigh") == "mp3"
    assert encode_type_for_level("lossless") == "flac"
    assert encode_type_for_level("hires") == "flac"
    assert encode_type_for_level("nonsense") == "mp3"


def test_client_header_defaults_and_overrides():
    """The header states the official desktop client.

    The field set is the one the official client builds
    (``+[YYYSystemInfoHelper systemInfo]``), plus the per-request
    ``requestId`` that the request layer adds.
    """
    header = build_client_header()
    assert set(header) >= {
        "os", "appver", "buildver", "osver", "mobilename", "brand",
        "deviceId", "requestId",
    }
    assert header["os"] == "osx"
    assert header["brand"] == "Apple"
    # The request id is a fresh UUID, not a timestamp.
    assert len(header["requestId"]) == 36 and header["requestId"].count("-") == 4
    assert build_client_header(appver="1.2.3")["appver"] == "1.2.3"


def test_the_header_and_the_body_state_the_same_client():
    """The business ``os`` and the header's ``os`` differ on purpose.

    The official client writes ``OSX`` in the business JSON and ``osx`` in
    the header -- two different spellings, both correct.
    """
    body = eapi_body("/api/x", {"a": 1})
    payload = json.loads(_decode(body["params"]).decode("utf-8").split(EAPI_SEPARATOR)[1])
    assert payload["os"] == "OSX"
    assert payload["verifyId"] == 1
    assert payload["deviceId"]
    assert payload["header"]["os"] == "osx"


def test_the_two_device_ids_are_different():
    """The body device id and the header device id are two values.

    The official client keeps them apart (the body one comes from the
    CoSDK, the header one from the system info helper) and the reverse
    engineering report warns explicitly against mixing them up.
    """
    body = eapi_body("/api/x", {"a": 1})
    payload = json.loads(_decode(body["params"]).decode("utf-8").split(EAPI_SEPARATOR)[1])
    assert payload["deviceId"] != payload["header"]["deviceId"]


def test_request_id_is_fresh_per_call():
    first = eapi_body("/api/x", {"a": 1})
    time.sleep(0.002)
    second = eapi_body("/api/x", {"a": 1})
    assert first["params"] != second["params"], "a re-used requestId is rejected by the API"


def test_eapi_body_contains_header_and_params():
    body = eapi_body("/api/x", {"a": 1})
    assert set(body) == {"params"}
    payload = json.loads(_decode(body["params"]).decode("utf-8").split(EAPI_SEPARATOR)[1])
    assert payload["a"] == 1 and "header" in payload
