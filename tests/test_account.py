"""The account panel: profile read-out and the nickname-edit guard rails."""
from __future__ import annotations

import pytest


@pytest.fixture()
def bridge(isolated_home):
    from src.gui.bridge import GuiBridge

    return GuiBridge()


def test_the_profile_is_empty_when_nobody_is_signed_in(bridge):
    from src.auth import get_login_manager

    manager = get_login_manager()
    manager._user_info = None
    assert manager.get_profile() == {}


def test_the_bridge_reports_the_logged_out_state(bridge):
    from src.auth import get_login_manager

    get_login_manager()._user_info = None
    result = bridge.get_user_profile()
    assert result["success"] is False
    assert result["logged_in"] is False
    assert result["error_key"] == "account.login_required"


def test_the_profile_shape_is_stable(bridge, monkeypatch):
    """The panel reads these keys; a missing one must not raise."""
    from src.auth import get_login_manager
    from src.auth.login import UserInfo

    manager = get_login_manager()
    manager._user_info = UserInfo(
        user_id="123", nickname="tester", avatar_url="http://x/a.png",
        vip_type=0, cookie="", profile={"signature": "hi", "level": 7},
    )
    profile = manager.get_profile()
    for key in ("userId", "nickname", "avatarUrl", "vipType"):
        assert key in profile, key
    assert profile["nickname"] == "tester"


def test_a_masked_username_never_becomes_the_display_name(bridge):
    """``account.userName`` is ``1_********477``; it tells nobody who they are."""
    from src.auth import get_login_manager
    from src.auth.login import UserInfo

    manager = get_login_manager()
    manager._user_info = UserInfo(
        user_id="1", nickname="real-name", avatar_url="", vip_type=0, cookie="",
        profile={"userName": "1_********477", "nickname": "real-name"},
    )
    assert manager.get_profile()["nickname"] == "real-name"


def test_an_empty_nickname_is_refused_before_any_request(bridge):
    from src.auth import get_login_manager

    manager = get_login_manager()
    manager._user_info = None
    result = manager.update_nickname("   ")
    assert result["success"] is False
    assert result["error_key"] == "account.nickname_empty"


def test_an_overlong_nickname_is_refused(bridge):
    from src.auth import get_login_manager

    manager = get_login_manager()
    manager._user_info = None
    result = manager.update_nickname("x" * 21)
    assert result["success"] is False
    assert result["error_key"] == "account.nickname_too_long"


def test_renaming_while_logged_out_does_not_hit_the_network(bridge, monkeypatch):
    from src.auth import get_login_manager

    manager = get_login_manager()
    manager._user_info = None
    calls = []
    monkeypatch.setattr(manager, "_post_eapi", lambda *a, **k: calls.append(a))
    result = manager.update_nickname("new name")
    assert result["error_key"] == "account.login_required"
    assert calls == [], "a logged-out rename must not send anything"


def test_the_unchanged_nickname_is_not_sent(bridge, monkeypatch):
    from src.auth import get_login_manager
    from src.auth.login import UserInfo

    manager = get_login_manager()
    manager._user_info = UserInfo(
        user_id="1", nickname="same", avatar_url="", vip_type=0, cookie="",
        profile={},
    )
    calls = []
    monkeypatch.setattr(manager, "_post_eapi", lambda *a, **k: calls.append(a))
    result = manager.update_nickname("same")
    assert result["success"] is True
    assert result.get("unchanged") is True
    assert calls == []
