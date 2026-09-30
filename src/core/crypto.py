"""eapi request signing for NetEase Cloud Music.

The official desktop / mobile clients talk to ``/eapi/*`` with a signed payload::

    message = "{path}-36cd479b6b5-{json}-36cd479b6b5-{md5(nobody{path}use{json}md5forencrypt)}"
    params  = HEX_UPPER(AES-128-ECB-PKCS7(message, key="e82ckenh8dichen8"))

``params`` is then POSTed as ``application/x-www-form-urlencoded``.

Verified live on 2026-09-30 (mainland-CN network):

* ``/eapi/*``   works -- this is the channel the app uses.
* ``/weapi/*``  is blackholed on that network: *every* request (including
  deliberately malformed payloads) answers ``HTTP 200`` with an empty body, so
  the classic AES-CBC + RSA weapi scheme cannot be used at all.  We therefore
  never call weapi and rely on eapi + the still-served legacy ``/api/*`` routes
  (see :mod:`src.core.api`).

The module is intentionally dependency-light: it uses ``cryptography`` (already a
runtime requirement) and nothing else.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "EAPI_KEY",
    "EAPI_SEPARATOR",
    "build_client_header",
    "encode_type_for_level",
    "sign_eapi",
    "eapi_body",
]

EAPI_KEY = b"e82ckenh8dichen8"
EAPI_SEPARATOR = "-36cd479b6b5-"

#: AES block size in bits (PKCS7 pads to this).
_BLOCK_BITS = 128

#: Quality level -> container the API should hand back.
_LEVEL_ENCODE_TYPE = {
    "standard": "mp3",
    "higher": "mp3",
    "exhigh": "mp3",
    "lossless": "flac",
    "hires": "flac",
    "jyeffect": "flac",
    "sky": "flac",
    "dolby": "flac",
}

#: Client fingerprint sent along with every signed request.  Values mirror the
#: official PC client closely enough to be accepted; ``deviceId`` only has to be
#: a stable-ish opaque string.
_DEFAULT_OS = "pc"
_DEFAULT_APPVER = "8.9.70"


def _stable_device_id() -> str:
    """Return a per-process device id (kept stable for the session)."""
    cached = getattr(_stable_device_id, "_cached", None)
    if cached is None:
        cached = os.urandom(16).hex()
        _stable_device_id._cached = cached  # type: ignore[attr-defined]
    return cached


def build_client_header(request_id: str | None = None, **overrides: Any) -> dict[str, Any]:
    """Build the ``header`` block that accompanies every eapi payload."""
    header: dict[str, Any] = {
        "os": _DEFAULT_OS,
        "appver": _DEFAULT_APPVER,
        "osver": "",
        "deviceId": _stable_device_id(),
        "requestId": request_id or str(int(time.time() * 1000)),
        "clientSign": "",
    }
    header.update(overrides)
    return header


def encode_type_for_level(level: str) -> str:
    """Map an audio level to the ``encodeType`` the API expects."""
    return _LEVEL_ENCODE_TYPE.get(level, "mp3")


def sign_eapi(path: str, payload: dict[str, Any]) -> str:
    """Return the hex-encoded ``params`` value for an eapi request.

    ``path`` must be the API path as the server sees it, i.e. including the
    leading ``/api`` prefix (``/api/song/enhance/player/url/v1``), *not* the
    ``/eapi`` URL prefix used for the HTTP call.
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(f"nobody{path}use{text}md5forencrypt".encode()).hexdigest()
    message = f"{path}{EAPI_SEPARATOR}{text}{EAPI_SEPARATOR}{digest}".encode()
    # AES-ECB has no IV and no built-in padding -- PKCS7 is applied by hand.
    padder = padding.PKCS7(_BLOCK_BITS).padder()
    padded = padder.update(message) + padder.finalize()
    encryptor = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return binascii.hexlify(encrypted).decode("ascii").upper()


def eapi_body(
    path: str,
    data: dict[str, Any] | None = None,
    *,
    header: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the form body for ``POST /eapi<path>``.

    A fresh ``requestId`` is generated for every call: re-using one across
    requests makes the server start answering ``code 400 参数错误`` (observed
    when hammering the endpoint during development).
    """
    payload: dict[str, Any] = dict(data or {})
    payload["header"] = build_client_header() if header is None else header
    return {"params": sign_eapi(path, payload)}
