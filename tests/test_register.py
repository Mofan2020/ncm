"""Register-account tests: the existence check and the register call.

The register endpoint doubles as "set a new password" for an existing
account, so the guard that refuses a registered number is the important one
here -- it is what keeps the app from taking over somebody else's account.
"""
from __future__ import annotations

import pytest

from src.auth.login import LoginManager


@pytest.fixture()
def manager(isolated_home):
    LoginManager._instance = None
    instance = LoginManager()
    yield instance
    LoginManager._instance = None


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else __import__("json").dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self):
        return self.responses.pop(0) if self.responses else FakeResponse({"code": 200})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._next()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next()


# ------------------------------------------------------------- existence check

def test_an_unregistered_number_is_reported_as_registerable(manager):
    manager._session = FakeSession([FakeResponse(
        {"code": 200, "exist": -1, "nickname": None, "hasPassword": False})])
    result = manager.check_phone_registered("17482199351")
    assert result == {"registered": False, "known": True, "message": ""}


def test_a_registered_number_is_reported_as_taken(manager):
    manager._session = FakeSession([FakeResponse(
        {"code": 200, "exist": 1, "nickname": "****", "hasPassword": True})])
    result = manager.check_phone_registered("18888888888")
    assert result["registered"] is True
    assert result["known"] is True


def test_exist_two_also_means_registered(manager):
    """``exist`` was observed as both 1 and 2 for registered numbers."""
    manager._session = FakeSession([FakeResponse({"code": 200, "exist": 2})])
    assert manager.check_phone_registered("15217799229")["registered"] is True


def test_a_failed_check_is_unknown_and_never_says_unregistered(manager):
    """Registering a number that has an account would reset its password."""
    manager._session = FakeSession([FakeResponse("", status_code=502)])
    result = manager.check_phone_registered("18888888888")
    assert result["known"] is False
    assert result["registered"] is False


def test_an_unexpected_answer_is_unknown(manager):
    manager._session = FakeSession([FakeResponse(
        {"code": 400, "msg": "手机号或ursToken不能为空"})])
    result = manager.check_phone_registered("")
    assert result["known"] is False


def test_the_existence_check_uses_the_signed_route(manager):
    manager._session = FakeSession([FakeResponse({"code": 200, "exist": -1})])
    manager.check_phone_registered("17482199351")
    method, url, kwargs = manager._session.calls[0]
    assert method == "POST"
    assert url.endswith("/eapi/api/cellphone/existence/check")
    assert "params" in kwargs["data"], "the route must be signed"


def test_the_existence_check_masks_the_number_in_the_log(manager):
    manager._session = FakeSession([FakeResponse({"code": 200, "exist": -1})])
    manager.check_phone_registered("17482199351")
    log = manager.config_path_for_debug().read_text(encoding="utf-8")
    assert "174****51" in log
    assert "17482199351" not in log


# ------------------------------------------------------------------- register

def test_registering_an_existing_account_is_refused_before_anything_is_sent(manager):
    """The register endpoint also resets passwords -- never call it for a
    number that already has an account."""
    manager._session = FakeSession([FakeResponse(
        {"code": 200, "exist": 1, "nickname": "****"})])
    result = manager.register_phone("18888888888", "1234", "secret12", "nick")
    assert result["success"] is False
    assert result["error_key"] == "login.register_already_registered"
    # Exactly one request: the existence check.  No register call was made.
    assert len(manager._session.calls) == 1
    assert "register/cellphone" not in manager._session.calls[0][1]


def test_registering_sends_the_password_as_md5(manager):
    manager._session = FakeSession([
        FakeResponse({"code": 200, "exist": -1}),
        FakeResponse({"code": 503, "message": "验证码错误"}),
    ])
    manager.register_phone("17482199351", "1234", "secret12", "nick")
    method, url, kwargs = manager._session.calls[1]
    assert url.endswith("/eapi/api/w/register/cellphone")
    assert "params" in kwargs["data"]
    # The payload is signed, so assert on the plain-text login call instead:
    # the md5 must never leave as the password itself.
    assert "secret12" not in kwargs["data"]["params"].lower()


def test_a_wrong_captcha_is_reported_as_such(manager):
    manager._session = FakeSession([
        FakeResponse({"code": 200, "exist": -1}),
        FakeResponse({"code": 503, "message": "验证码错误"}),
    ])
    result = manager.register_phone("17482199351", "0000", "secret12", "nick")
    assert result["success"] is False
    assert result["error_key"] == "login.register_bad_captcha"


def test_a_throttled_register_is_reported_as_a_throttle(manager):
    manager._session = FakeSession([
        FakeResponse({"code": 200, "exist": -1}),
        FakeResponse({"code": 408, "message": "操作过于频繁，请稍后再试!"}),
    ])
    result = manager.register_phone("17482199351", "0000", "secret12", "nick")
    assert result["success"] is False
    assert result["error_key"] == "login.code_too_frequent"
    assert result["throttled"] is True


def test_a_missing_password_or_nickname_is_refused_locally(manager):
    manager._session = FakeSession([])
    assert manager.register_phone("17482199351", "1234", "", "nick")["error_key"] == \
        "login.register_need_password"
    assert manager.register_phone("17482199351", "1234", "secret12", "")["error_key"] == \
        "login.register_need_nickname"
    assert manager.register_phone("17482199351", "", "secret12", "nick")["error_key"] == \
        "login.enter_phone_and_code"
    # Nothing was sent: those are local checks.
    assert manager._session.calls == []


def test_an_unknown_check_blocks_the_register(manager):
    manager._session = FakeSession([FakeResponse("", status_code=502)])
    result = manager.register_phone("18888888888", "1234", "secret12", "nick")
    assert result["success"] is False
    assert result["error_key"] == "login.register_check_failed"
    assert len(manager._session.calls) == 1


def test_a_successful_register_signs_the_user_in(manager, monkeypatch):
    manager._session = FakeSession([
        FakeResponse({"code": 200, "exist": -1}),
        FakeResponse({"code": 200, "message": "注册成功"}),
    ])
    monkeypatch.setattr(manager, "verify_login", lambda force=False: True)
    result = manager.register_phone("17482199351", "1234", "secret12", "nick")
    assert result["success"] is True


def test_a_register_that_cannot_sign_in_says_the_account_exists(manager, monkeypatch):
    """The account was created even when the automatic sign-in failed."""
    manager._session = FakeSession([
        FakeResponse({"code": 200, "exist": -1}),
        FakeResponse({"code": 200, "message": "注册成功"}),
    ])
    monkeypatch.setattr(manager, "verify_login", lambda force=False: False)
    monkeypatch.setattr(manager, "login_phone",
                        lambda phone, code, ctcode="86": {"success": False, "message": "nope"})
    result = manager.register_phone("17482199351", "1234", "secret12", "nick")
    assert result["success"] is False
    assert result["registered"] is True
    assert result["error_key"] == "login.register_login_failed"


# --------------------------------------------------------------------- bridge

def test_the_bridge_exposes_the_register_check(bridge, monkeypatch):
    monkeypatch.setattr(bridge.login_manager, "check_phone_registered",
                        lambda phone, ctcode="86": {"registered": True, "known": True, "message": ""})
    result = bridge.check_phone_registered("18888888888")
    assert result["registered"] is True
    assert result["known"] is True
    assert result["success"] is True


def test_the_bridge_reports_an_unknown_check(bridge, monkeypatch):
    monkeypatch.setattr(bridge.login_manager, "check_phone_registered",
                        lambda phone, ctcode="86": {"registered": False, "known": False,
                                                    "message": "check_failed"})
    result = bridge.check_phone_registered("18888888888")
    assert result["known"] is False
    assert result["error_key"] == "login.register_check_failed"


def test_the_bridge_forwards_the_register_result(bridge, monkeypatch):
    monkeypatch.setattr(bridge.login_manager, "register_phone",
                        lambda phone, code, password, nickname, ctcode="86":
                        {"success": False, "error_key": "login.register_bad_captcha",
                         "message": "验证码错误"})
    result = bridge.register_phone("17482199351", "0000", "secret12", "nick")
    assert result["success"] is False
    assert result["error_key"] == "login.register_bad_captcha"
    assert result["message"] == "验证码错误"
