"""Exporting the session cookie so another device can sign in without a password.

The cookie is the account's credential: whoever holds it is logged in as that
account.  The export writes it with owner-only permissions and never leaves
it in a log.
"""
from __future__ import annotations

import logging

from src.gui import bridge as bridge_mod


class _FakeLogin:
    def __init__(self, cookie: str = "MUSIC_U=abc123; __csrf=def") -> None:
        self._cookie = cookie

    def cookie_string(self) -> str:
        return self._cookie


def _bridge(cookie: str = "MUSIC_U=abc123; __csrf=def"):
    bridge = bridge_mod.GuiBridge.__new__(bridge_mod.GuiBridge)
    bridge.logger = logging.getLogger("test.login_cookie")
    bridge.login_manager = _FakeLogin(cookie)
    return bridge


def test_the_export_writes_the_cookie_with_owner_only_permissions(tmp_path, monkeypatch):
    bridge = _bridge()
    target = tmp_path / "cookie.txt"
    monkeypatch.setattr(bridge, "_choose_cookie_save_path", lambda: target)

    result = bridge.export_login_cookie()

    assert result["success"] is True
    assert result["cookie"] == "MUSIC_U=abc123; __csrf=def"
    assert target.read_text(encoding="utf-8").strip() == "MUSIC_U=abc123; __csrf=def"
    assert oct(target.stat().st_mode)[-3:] == "600"


def test_a_cancelled_save_dialog_is_not_an_error(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_choose_cookie_save_path", lambda: None)

    result = bridge.export_login_cookie()

    assert result["cancelled"] is True


def test_the_copy_reports_whether_the_clipboard_took_the_cookie(monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "_copy_to_clipboard", lambda text: True)

    result = bridge.copy_login_cookie()

    assert result["success"] is True
    assert result["copied"] is True
    assert result["cookie"] == "MUSIC_U=abc123; __csrf=def"


def test_without_a_login_there_is_nothing_to_export():
    bridge = _bridge("")

    assert bridge.export_login_cookie()["success"] is False
    assert bridge.copy_login_cookie()["success"] is False


def test_the_cookie_never_reaches_the_log(caplog):
    bridge = _bridge()
    with caplog.at_level(logging.DEBUG):
        bridge.copy_login_cookie()
    assert "MUSIC_U" not in caplog.text
