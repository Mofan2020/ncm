"""The signature the app login route wants.

The route answers ``-3``（签名错误）when the signature does not match, so
this has to be exactly right.  The rule is the documented one: every
parameter sorted by name, url encoded, hashed together with the app secret.

The values checked here are the ones the server was measured with on
2026-11-01: a correct signature reaches the credential check (``-629``), a
missing ``appkey`` or a wrong ``sign`` is answered ``-3``.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.bili_app_login import (  # noqa: E402
    APPKEY,
    APPSEC,
    app_login_params,
    app_sign,
    app_sms_login_params,
    app_sms_params,
)


def test_the_signature_is_the_md5_of_the_sorted_query():
    params = {"b": "2", "a": "1", "appkey": APPKEY}
    expected = hashlib.md5(
        ("a=1&appkey=" + APPKEY + "&b=2" + APPSEC).encode("utf-8")
    ).hexdigest()
    assert app_sign(params) == expected


def test_the_signature_needs_the_appkey():
    """A signature that leaves the app key out is answered -3."""
    without = app_sign({"username": "u"})
    with_key = app_sign({"username": "u", "appkey": APPKEY})
    assert without != with_key


def test_the_signature_follows_the_parameters():
    assert app_sign({"a": "1"}) != app_sign({"a": "2"})
    assert app_sign({"a": "1"}) != app_sign({"a": "1", "b": "2"})


def test_the_login_parameters_carry_the_app_key():
    params = app_login_params("user", "cipher", ts=1700000000)
    assert params["appkey"] == APPKEY
    assert params["username"] == "user"
    assert params["password"] == "cipher"
    assert params["sign"]
    assert params["ts"] == "1700000000"


def test_the_login_parameters_leave_the_captcha_fields_empty():
    """The app route does not check the slider; a made up value is -105."""
    params = app_login_params("user", "cipher")
    for name in ("captcha", "challenge", "validate", "seccode"):
        assert params[name] == ""


def test_the_sms_parameters_carry_the_app_key():
    params = app_sms_params("13800000000", "86", ts=1700000000)
    assert params["appkey"] == APPKEY
    assert params["tel"] == "13800000000"
    assert params["cid"] == "86"
    assert params["sign"]


def test_the_sms_login_parameters_carry_the_captcha_key():
    params = app_sms_login_params("13800000000", "123456", "key-1", "86")
    assert params["code"] == "123456"
    assert params["captcha_key"] == "key-1"
    assert params["sign"]


def test_the_signed_query_matches_the_signature():
    """What goes over the wire has to be what was signed.

    The signature is taken over the parameters *before* the signature
    itself is added, so it is left out of the query here too.
    """
    params = app_login_params("user", "cipher", ts=1700000000)
    without = {name: value for name, value in params.items() if name != "sign"}
    query = urllib.parse.urlencode(sorted(without.items()))
    expected = hashlib.md5((query + APPSEC).encode("utf-8")).hexdigest()
    assert params["sign"] == expected
