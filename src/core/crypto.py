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

The module has **no native dependencies**: AES-128-ECB lives in
:mod:`src.core.aes`, and the rest is the standard library.  Shipping a native
crypto extension would drag OpenSSL into the PyInstaller bundle, which broke the
Intel macOS build (see ``docs/notes.md``).

The client identity
-------------------

The risk control looks at **who** is talking, so the app states the same
client in every place the server can look.  The shape below is taken from the
official desktop client, reverse engineered with IDA Professional 9.5
(``docs/notes.md`` section 22 lists the addresses and the pseudocode).

The eapi body is::

    {
      ...business parameters...,
      "os": "OSX",          <- forced on, upper case
      "verifyId": 1,        <- forced on
      "deviceId": "<CoSDK device id>",   <- forced on, a *different* id
      "header": {
        "deviceId": "<system device id>",
        "osver": "...",
        "appver": "3.1.9",
        "buildver": "3364",
        "mobilename": "...",
        "os": "osx",
        "brand": "Apple",
        "requestId": "<a fresh UUID per request>"
      }
    }

Two details are easy to get wrong and are called out by the binary itself:

* the business ``os`` is **``OSX``** while the header's is **``osx``** -- two
  different spellings on purpose;
* the body ``deviceId`` (from the CoSDK) and the header ``deviceId`` (from the
  system info helper) are **two different values**.  The official client keeps
  them apart, so we do too.

The old identity was ``os=pc`` with ``appver=8.9.70`` -- a Windows platform
carrying an Android version number.  Nothing broke because of it, but it is a
contradiction the risk engine can see, and there is no reason to keep it.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import uuid
from typing import Any

from src.core.aes import aes_ecb_encrypt

__all__ = [
    "EAPI_KEY",
    "EAPI_SEPARATOR",
    "build_client_header",
    "build_client_identity",
    "encode_type_for_level",
    "sign_eapi",
    "eapi_body",
]

EAPI_KEY = b"e82ckenh8dichen8"
EAPI_SEPARATOR = "-36cd479b6b5-"

#: The single place the eapi URL prefix is spelled out (enforced by
#: ``tests/test_api.py::test_no_weapi_signing_anywhere``).
EAPI_PREFIX = "/eapi"

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

#: The client this app presents itself as.  These are the official desktop
#: client's values: the app version and build come from the app bundle, and
#: the platform and brand are constants in the binary.
CLIENT_OS = "osx"
CLIENT_BRAND = "Apple"
CLIENT_APPVER = "3.1.9"
CLIENT_BUILDVER = "3364"

#: The ``os`` the **business** JSON carries.  Different case from the
#: header's ``os`` on purpose -- the official client does exactly this.
CLIENT_BODY_OS = "OSX"

#: The desktop client's user agent.  The official client hardcodes the system
#: version and the Chrome version too, so the string is the same on every
#: machine; we keep that property.
CLIENT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    f"NeteaseMusicDesktop/{CLIENT_APPVER}.{CLIENT_BUILDVER}"
)


def _stable_device_id() -> str:
    """The **system** device id -- the one that goes into the header.

    The official client builds it as ``<hardwareUUID>|<random UUID>`` and
    persists it, so the same install always looks like the same machine.
    Ours is stable for the lifetime of the process, which is as far as we can
    go without writing to the user's disk from the crypto layer.
    """
    cached = getattr(_stable_device_id, "_cached", None)
    if cached is None:
        cached = f"{platform.node()}|{uuid.uuid4()}"
        _stable_device_id._cached = cached  # type: ignore[attr-defined]
    return cached


def _co_device_id() -> str:
    """The **CoSDK** device id -- the one that goes into the body.

    A different value from :func:`_stable_device_id` on purpose: the official
    client keeps the two apart, and the report warns explicitly against
    mixing them up.
    """
    cached = getattr(_co_device_id, "_cached", None)
    if cached is None:
        cached = os.urandom(16).hex()
        _co_device_id._cached = cached  # type: ignore[attr-defined]
    return cached


def _system_version() -> str:
    """The running macOS version, as the header's ``osver``."""
    version = platform.mac_ver()[0]
    return version or "10.15.7"


def _hardware_model() -> str:
    """The hardware model, as the header's ``mobilename``.

    The official client reads the machine's model identifier (``hw.model``
    on macOS, e.g. ``Mac14,7``), not the CPU architecture.  ``arm64`` would
    be a strange thing to call a machine, so we ask the system the same way
    the client does -- and fall back to what Python knows.
    """
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.model"],
            capture_output=True, text=True, timeout=2.0,
        )
        model = result.stdout.strip()
        if result.returncode == 0 and model:
            return model
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.machine() or "Mac"


def build_client_header(
    request_id: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build the ``header`` block that accompanies every eapi payload.

    The keys are the ones the official client puts there
    (``+[YYYSystemInfoHelper systemInfo]``), plus the ``requestId`` that
    ``+[YYYRequestSecureHelper _secureParamStringWithURI:params:]`` adds to a
    copy of that dictionary for every single request.
    """
    header: dict[str, Any] = {
        "deviceId": _stable_device_id(),
        "osver": _system_version(),
        "appver": CLIENT_APPVER,
        "buildver": CLIENT_BUILDVER,
        "mobilename": _hardware_model(),
        "os": CLIENT_OS,
        "brand": CLIENT_BRAND,
    }
    # The request id has to be **new** on every request: re-using one makes
    # the server answer ``code 400 参数错误`` (observed when hammering the
    # endpoint during development).
    header["requestId"] = request_id or str(uuid.uuid4()).upper()
    header.update(overrides)
    return header


def build_client_identity(header: dict[str, Any] | None = None) -> dict[str, str]:
    """The cookie fields that state the same client as the eapi header.

    The official client repeats the header's fields in the ``Cookie`` header,
    so the server sees the same answer twice -- once in the body it can verify
    cryptographically, once in the cookie.  A request whose cookie says
    something different from its header is a request that looks forged.

    The login ticket (``MUSIC_U``) and the anonymous ticket (``MUSIC_A``)
    are **not** part of this: the session already carries them, and putting
    them here as well would send them twice.
    """
    identity = dict(header if header is not None else build_client_header())
    identity.pop("MUSIC_U", None)
    identity.pop("MUSIC_A", None)
    return {str(k): str(v) for k, v in identity.items() if v is not None}


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
    # AES-ECB has no IV; PKCS7 padding is applied inside aes_ecb_encrypt().
    return aes_ecb_encrypt(message, EAPI_KEY).hex().upper()


def eapi_body(
    path: str,
    data: dict[str, Any] | None = None,
    *,
    header: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the form body for ``POST /eapi<path>``.

    On top of the business parameters the official client forces three fields
    onto the JSON before it is encrypted (``+[CloudMusicCoSDK
    internalGenerateSerial:jsonDict:type:]``): the platform, a ``verifyId``
    and the CoSDK device id.  They are part of what gets signed, so they have
    to be in here, not added afterwards.
    """
    payload: dict[str, Any] = dict(data or {})
    payload["os"] = CLIENT_BODY_OS
    payload["verifyId"] = 1
    payload["deviceId"] = _co_device_id()
    payload["header"] = build_client_header() if header is None else header
    return {"params": sign_eapi(path, payload)}
