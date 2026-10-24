"""The session must not carry the fake PC-client identity cookies.

NetEase's risk control refuses to add a track to the account's favourites
when the session claims to be the PC client but was not issued to it: the
like endpoint answers ``code 524 当前环境异常，已取消喜欢``.

Measured on 2026-10-24: eight tracks that always failed with the identity
cookies present, each tried from a clean "not liked" state, all answered 200
without them.
"""
from __future__ import annotations

from src.auth import login as login_module

IDENTITY_COOKIES = ("os", "appver", "channel")


def _manager(monkeypatch, tmp_path, monkey_home):
    """A LoginManager with a clean settings directory."""
    login_module.LoginManager._instance = None
    return login_module.LoginManager()


def test_the_session_carries_no_identity_cookies(tmp_path, monkeypatch, isolated_home):
    manager = _manager(monkeypatch, tmp_path, isolated_home)

    names = set(manager.get_session().cookies.get_dict())

    for name in IDENTITY_COOKIES:
        assert name not in names, f"{name} must not be sent: it makes the " \
                                  "like endpoint refuse the track"


def test_a_saved_cookie_string_does_not_bring_them_back(tmp_path, monkeypatch, isolated_home):
    """A session saved by an older build still has them in it."""
    manager = _manager(monkeypatch, tmp_path, isolated_home)

    manager.apply_cookie_string(
        "os=pc; appver=8.9.70; channel=netease; _ntes_nuid=abc; "
        "MUSIC_U=token; __csrf=csrf")

    cookies = manager.get_session().cookies.get_dict()

    for name in IDENTITY_COOKIES:
        assert name not in cookies
    assert cookies.get("MUSIC_U") == "token"
    assert cookies.get("_ntes_nuid") == "abc"


def test_the_saved_cookie_string_does_not_write_them_back(tmp_path, monkeypatch, isolated_home):
    manager = _manager(monkeypatch, tmp_path, isolated_home)
    manager.apply_cookie_string("os=pc; appver=8.9.70; MUSIC_U=token")

    saved = manager._cookie_string()

    for name in IDENTITY_COOKIES:
        assert name not in saved
    assert "MUSIC_U=token" in saved


def test_the_client_cookie_table_is_empty(tmp_path, monkeypatch, isolated_home):
    """The constant documents the decision; keep it and the code in step."""
    manager = _manager(monkeypatch, tmp_path, isolated_home)

    assert manager.CLIENT_COOKIES == {}
