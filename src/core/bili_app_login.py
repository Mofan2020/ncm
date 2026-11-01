"""The Bilibili app login route, and the signature it wants.

The web login route (``/x/passport-login/web/login``) cannot be used from a
desktop client: the server checks the geetest result and answers ``-105``
（验证码错误）for every request that does not carry a slider the user really
solved.  There is no way around that from the outside.

The app route (``/x/passport-login/oauth2/login``) is the one the phone app
uses, and it does not check the slider: with a correct signature and the four
geetest fields left empty it goes straight to the credential check.  Measured
on 2026-11-01 with a wrong password:

* correct signature          -> ``-629``（用户名或密码错误）
* missing ``appkey``         -> ``-3``（签名错误）
* wrong ``sign``             -> ``-3``（签名错误）
* web route, no slider       -> ``-105``（验证码错误）

So the password login becomes possible without a browser and without a
captcha -- which is what the desktop client needs.

The signature is the documented one: every parameter sorted by name, url
encoded, plus the app secret, hashed with MD5.
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

__all__ = [
    "APPKEY",
    "APPSEC",
    "app_sign",
    "app_login_params",
    "app_sms_params",
]

#: The app key of the Android client.  It is a public constant of the app:
#: every third party client (biliup included) uses the same one.
APPKEY = "783bbb7264451d82"

#: The secret that goes with :data:`APPKEY`.  It never leaves this file and
#: is not a user credential -- it is the same in every install.
APPSEC = "2653583c8873dea268ab9386918b1d65"

#: The client version the app sends.  The server does not check it, but a
#: request without it is answered ``-400``.
_BUILD = "6310300"

#: The device id the app sends.  It only has to look like one.
_BUVID = "XY" + "0" * 28


def app_sign(params: dict[str, Any]) -> str:
    """Sign the parameters the way the app does.

    Every parameter is sorted by name, url encoded and hashed together with
    the app secret.  The ``appkey`` has to be part of the sorted list: a
    signature that leaves it out is answered ``-3``.
    """
    query = urllib.parse.urlencode(sorted(params.items()))
    return hashlib.md5((query + APPSEC).encode("utf-8")).hexdigest()


def app_login_params(
    username: str,
    password_cipher: str,
    *,
    ts: int | None = None,
) -> dict[str, Any]:
    """The parameters of one password login.

    ``password_cipher`` is the encrypted password -- the caller has to ask
    the server for the public key first (see
    :meth:`src.core.bilibili.BiliClient.password_key`).

    The four geetest fields are sent empty on purpose: the app route does
    not look at them, and sending a made up value is answered ``-105``.
    """
    moment = int(time.time()) if ts is None else int(ts)
    params: dict[str, Any] = {
        "actionKey": "appkey",
        "appkey": APPKEY,
        "build": _BUILD,
        "buvid": _BUVID,
        "captcha": "",
        "challenge": "",
        "channel": "bili",
        "device": "phone",
        "mobi_app": "android",
        "password": str(password_cipher or ""),
        "permission": "ALL",
        "platform": "android",
        "seccode": "",
        "subid": "1",
        "ts": str(moment),
        "username": str(username or ""),
        "validate": "",
    }
    params["sign"] = app_sign(params)
    return params


def app_sms_params(
    tel: str,
    cid: str = "86",
    *,
    ts: int | None = None,
) -> dict[str, Any]:
    """The parameters of one "send me a code" request."""
    moment = int(time.time()) if ts is None else int(ts)
    params: dict[str, Any] = {
        "actionKey": "appkey",
        "appkey": APPKEY,
        "build": _BUILD,
        "buvid": _BUVID,
        "channel": "bili",
        "cid": str(cid or "86"),
        "device": "phone",
        "mobi_app": "android",
        "platform": "android",
        "tel": str(tel or ""),
        "ts": str(moment),
    }
    params["sign"] = app_sign(params)
    return params


def app_sms_login_params(
    tel: str,
    code: str,
    captcha_key: str,
    cid: str = "86",
    *,
    ts: int | None = None,
) -> dict[str, Any]:
    """The parameters of one "log me in with the code" request."""
    moment = int(time.time()) if ts is None else int(ts)
    params: dict[str, Any] = {
        "actionKey": "appkey",
        "appkey": APPKEY,
        "build": _BUILD,
        "buvid": _BUVID,
        "captcha_key": str(captcha_key or ""),
        "channel": "bili",
        "cid": str(cid or "86"),
        "code": str(code or ""),
        "device": "phone",
        "mobi_app": "android",
        "platform": "android",
        "tel": str(tel or ""),
        "ts": str(moment),
    }
    params["sign"] = app_sign(params)
    return params
