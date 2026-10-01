"""Login-flow tests: QR state machine, SMS throttling, diagnostics.

Everything runs against stubbed HTTP -- the real endpoints are covered by the
opt-in live tests in ``test_live_api.py``.
"""
from __future__ import annotations

import json

import pytest

from src.auth.login import LoginManager, LoginStatus


@pytest.fixture()
def manager(isolated_home):
    """A fresh LoginManager (the class is a singleton, so reset it)."""
    LoginManager._instance = None
    instance = LoginManager()
    yield instance
    LoginManager._instance = None


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict | list) else (payload or "")

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records calls and replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self):
        return self.responses.pop(0) if self.responses else FakeResponse({"code": 801})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._next()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next()


# ------------------------------------------------------------------ parsing

def test_qr_code_of_accepts_plain_and_nested_codes():
    assert LoginManager._qr_code_of({"code": 802}) == 802
    assert LoginManager._qr_code_of({"data": {"code": 803}}) == 803
    assert LoginManager._qr_code_of({"code": True}) is None      # bool is not a code
    assert LoginManager._qr_code_of({"message": "x"}) is None


def test_qr_cookie_of_handles_every_shape_netease_has_used():
    assert LoginManager._qr_cookie_of({"cookie": "MUSIC_U=1"}) == "MUSIC_U=1"
    assert LoginManager._qr_cookie_of({"cookies": {"MUSIC_U": "2"}}) == "MUSIC_U=2"
    assert LoginManager._qr_cookie_of({"data": {"cookie": "MUSIC_U=3"}}) == "MUSIC_U=3"
    assert LoginManager._qr_cookie_of({"cookie": "   "}) == ""
    assert LoginManager._qr_cookie_of({}) == ""


# ------------------------------------------------------- polling channels

def test_poll_once_uses_the_right_request_per_channel(manager):
    manager._qrcode_key = "KEY"
    session = FakeSession([FakeResponse({"code": 801}), FakeResponse({"code": 801})])
    manager._session = session

    assert manager._poll_once("api") == {"code": 801}
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == LoginManager.QRCODE_CHECK_URL
    assert kwargs["params"]["key"] == "KEY"
    assert kwargs["params"]["type"] == 1
    assert "timestamp" in kwargs["params"], "the plain route needs a cache buster"

    assert manager._poll_once("eapi") == {"code": 801}
    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url.endswith("/eapi/api/login/qrcode/client/login")
    assert "params" in kwargs["data"], "the eapi route must be signed"


def test_poll_once_survives_an_empty_or_broken_answer(manager):
    manager._qrcode_key = "KEY"
    manager._session = FakeSession([FakeResponse("", status_code=200),
                                    FakeResponse("not json"),
                                    FakeResponse({"code": 801}, status_code=502)])
    assert manager._poll_once("api") is None
    assert manager._poll_once("api") is None
    assert manager._poll_once("api") is None


# ------------------------------------------------- the reported stuck-at-802 bug

def test_scanned_never_means_logged_in(manager, monkeypatch):
    """Regression: 802 must not be mistaken for success, and must not spin."""
    monkeypatch.setattr(manager, "QR_POLL_INTERVAL", 0.0)
    seen = {"n": 0}

    def fake_poll(channel):
        seen["n"] += 1
        if seen["n"] >= 3:
            manager._stop_polling.set()          # end the loop for the test
        return {"code": 802, "message": "已扫码，等待确认"}

    monkeypatch.setattr(manager, "_poll_once", fake_poll)
    monkeypatch.setattr(manager, "_handle_login_success",
                        lambda cookie: pytest.fail("802 is not a login"))
    manager._poll_qrcode()

    assert manager.status is LoginStatus.SCANNED
    assert manager.last_qr_code == 802
    assert manager.last_qr_message == "已扫码，等待确认"
    assert seen["n"] >= 3, "the poll must keep trying, not give up on 802"
    log = (manager.config_path_for_debug()).read_text(encoding="utf-8")
    assert "code=802" in log


def test_authorised_login_succeeds_and_stores_the_cookie(manager, monkeypatch):
    monkeypatch.setattr(manager, "_poll_once", lambda channel: {
        "code": 803, "message": "授权成功", "cookie": "MUSIC_U=abc"})
    monkeypatch.setattr(manager, "verify_login", lambda force=False: True)

    manager._qrcode_key = "KEY"
    manager._poll_qrcode()

    assert manager.get_cookies().get("MUSIC_U") == "abc"
    assert manager.status is LoginStatus.SUCCESS


def test_a_cookie_with_a_plain_200_also_counts_as_success(manager, monkeypatch):
    monkeypatch.setattr(manager, "_poll_once",
                        lambda channel: {"code": 200, "cookies": {"MUSIC_U": "xyz"}})
    monkeypatch.setattr(manager, "verify_login", lambda force=False: True)
    manager._qrcode_key = "KEY"
    manager._poll_qrcode()
    assert manager.get_cookies().get("MUSIC_U") == "xyz"
    assert manager.status is LoginStatus.SUCCESS


def test_authorisation_arriving_on_the_second_channel_is_not_missed(manager, monkeypatch):
    """The plain route keeps saying 802; eapi finally reports 803."""
    answers = [{"code": 802}, {"code": 803, "cookie": "MUSIC_U=late"}]
    monkeypatch.setattr(manager, "QR_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(manager, "_poll_once", lambda channel: answers.pop(0) if answers else {"code": 802})
    monkeypatch.setattr(manager, "verify_login", lambda force=False: True)
    manager._qrcode_key = "KEY"
    manager._poll_qrcode()
    assert manager.get_cookies().get("MUSIC_U") == "late"


def test_unexpected_code_is_surfaced_to_the_ui(manager, monkeypatch):
    messages = []
    manager.on("status_message", messages.append)
    monkeypatch.setattr(manager, "QR_POLL_INTERVAL", 0.0)
    state = {"n": 0}

    def fake_poll(channel):
        state["n"] += 1
        if state["n"] >= 2:
            manager._stop_polling.set()
        return {"code": 4042, "message": "风控校验"}

    monkeypatch.setattr(manager, "_poll_once", fake_poll)
    manager._qrcode_key = "KEY"
    manager._poll_qrcode()
    assert messages == ["风控校验"], "an unknown code must reach the UI, not just the log"


def test_expired_qr_reports_expiry(manager, monkeypatch):
    failures = []
    manager.on("login_failed", failures.append)
    monkeypatch.setattr(manager, "_poll_once", lambda channel: {"code": 800, "message": "二维码已过期"})
    manager._qrcode_key = "KEY"
    manager._poll_qrcode()
    assert manager.status is LoginStatus.EXPIRED
    assert failures == ["qrcode_expired"]


def test_a_new_unikey_is_only_requested_when_needed(manager, monkeypatch):
    # the polling thread must not drain the stubbed responses
    monkeypatch.setattr(manager, "_poll_qrcode", lambda: None)
    session = FakeSession([FakeResponse({"code": 200, "unikey": "KEY-1"}),
                           FakeResponse({"code": 200, "unikey": "KEY-2"})])
    manager._session = session
    assert manager.login_qrcode() is True
    assert manager._qrcode_key == "KEY-1"
    assert manager.has_pending_qrcode() is True

    # reopening the panel must NOT invalidate the displayed code
    manager.login_qrcode()
    assert manager._qrcode_key == "KEY-1"
    unikey_calls = [call for call in session.calls if call[1] == LoginManager.QRCODE_KEY_URL]
    assert len(unikey_calls) == 1, "a displayed QR code must be reused, not replaced"

    # an explicit refresh does mint a new one
    manager.refresh_qrcode()
    assert manager._qrcode_key == "KEY-2"


def test_resend_qrcode_reemits_without_a_new_key(manager):
    urls = []
    manager.on("qrcode_update", urls.append)
    manager._qrcode_key = "KEPT"
    assert manager.resend_qrcode() is True
    assert urls == ["https://music.163.com/login?codekey=KEPT"]
    manager._qrcode_key = ""
    assert manager.resend_qrcode() is False


def test_has_pending_qrcode_is_false_once_resolved(manager):
    manager._qrcode_key = "KEY"
    manager._set_status(LoginStatus.PENDING)
    assert manager.has_pending_qrcode() is True
    manager._set_status(LoginStatus.SCANNED)
    assert manager.has_pending_qrcode() is True
    manager._set_status(LoginStatus.SUCCESS)
    assert manager.has_pending_qrcode() is False
    manager._set_status(LoginStatus.EXPIRED)
    assert manager.has_pending_qrcode() is False


def test_poll_loop_alternates_both_channels(manager, monkeypatch):
    """Both routes are really polled -- one bad CDN node cannot strand us."""
    monkeypatch.setattr(manager, "QR_POLL_INTERVAL", 0.0)
    seen: list[str] = []

    def fake_poll(channel):
        seen.append(channel)
        if len(seen) >= 4:
            manager._stop_polling.set()
        return {"code": 801, "message": "等待扫码"}

    monkeypatch.setattr(manager, "_poll_once", fake_poll)
    manager._qrcode_key = "KEY"
    manager._poll_qrcode()
    assert seen == ["api", "eapi", "api", "eapi"]


def test_a_slow_account_endpoint_does_not_lose_a_valid_login(manager, monkeypatch):
    """The reported symptom: cookie arrives, the account check lags a moment."""
    monkeypatch.setattr(manager, "VERIFY_RETRY_DELAY", 0.0)
    answers = [False, True]
    monkeypatch.setattr(manager, "verify_login",
                        lambda force=False: answers.pop(0) if answers else True)
    manager._handle_login_success("MUSIC_U=abc")
    assert manager.status is LoginStatus.SUCCESS


def test_login_still_fails_when_the_session_is_really_dead(manager, monkeypatch):
    monkeypatch.setattr(manager, "VERIFY_RETRY_DELAY", 0.0)
    monkeypatch.setattr(manager, "verify_login", lambda force=False: False)
    failures = []
    manager.on("login_failed", failures.append)

    manager._handle_login_success("MUSIC_U=dead")

    assert manager.status is LoginStatus.FAILED
    assert failures == ["login_status_check_failed"]
    assert "not confirmed" in manager.config_path_for_debug().read_text(encoding="utf-8")


# ------------------------------------------------------------------ account

def test_the_header_shows_the_real_nickname_not_the_masked_one(manager):
    """`account.userName` is `1_********477`; only `profile.nickname` identifies."""
    manager._set_user_from_account(
        {"id": 9641982263, "userName": "1_********477"},
        {"nickname": "深空遗尘", "avatarUrl": "https://img", "vipType": 0},
    )
    assert manager.user_info.nickname == "深空遗尘"
    assert manager.user_info.user_id == "9641982263"


def test_a_profile_without_a_nickname_still_names_the_user(manager):
    manager._set_user_from_account({"id": 1, "userName": "1_****477"}, {})
    assert manager.user_info.nickname == "1_****477"


# --------------------------------------------------------------------- SMS

def _sms(manager, payload, status_code=200):
    manager._session = FakeSession([FakeResponse(payload, status_code=status_code)])
    return manager.send_phone_code("13800001111")


def test_sms_success_requires_a_truthy_data_flag(manager):
    assert _sms(manager, {"code": 200, "data": True})["success"] is True
    assert _sms(manager, {"code": 200})["success"] is True          # no flag at all
    assert _sms(manager, {"code": 200, "data": False})["success"] is False


def test_sms_throttling_is_reported_as_such(manager):
    result = _sms(manager, {"code": 503, "message": "操作过于频繁，请稍后再试"})
    assert result["success"] is False
    assert result["error_key"] == "login.code_too_frequent"
    assert result["throttled"] is True
    assert "频繁" in result["message"]


def test_sms_throttling_is_detected_from_the_message_alone(manager):
    result = _sms(manager, {"code": 200, "data": False, "msg": "发送太频繁了"})
    assert result["error_key"] == "login.code_too_frequent"
    assert result["throttled"] is True


def test_sms_is_never_retried(manager):
    """Retrying is what turns NetEase's one-minute cooldown into a lockout."""
    session = FakeSession([FakeResponse({"code": 503, "message": "操作过于频繁"})])
    manager._session = session
    manager.send_phone_code("13800001111")
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == LoginManager.SMS_CODE_URL
    assert kwargs["data"]["cellphone"] == "13800001111"
    assert kwargs["data"]["ctcode"] == "86"


def test_sms_failure_keeps_the_server_message(manager):
    result = _sms(manager, {"code": 400, "message": "手机号格式不正确"})
    assert result["success"] is False
    assert result["error_key"] == "login.code_send_failed"
    assert result["message"] == "手机号格式不正确"
    assert result["throttled"] is False


def test_sms_network_error_is_reported(manager):
    class Boom(FakeSession):
        def post(self, url, **kwargs):
            raise RuntimeError("connection reset")

    manager._session = Boom([])
    result = manager.send_phone_code("13800001111")
    assert result["success"] is False
    assert "connection reset" in result["message"]


# ------------------------------------------------------------ diagnostics

def test_debug_log_masks_the_phone_number(manager):
    _sms(manager, {"code": 200, "data": True})
    log = (manager.config_path_for_debug()).read_text(encoding="utf-8")
    assert "138****11" in log
    assert "13800001111" not in log


def test_debug_log_is_capped(manager, monkeypatch):
    monkeypatch.setattr(LoginManager, "DEBUG_LOG_MAX", 200)
    for _ in range(40):
        _sms(manager, {"code": 200, "data": True})
    assert manager.config_path_for_debug().stat().st_size <= 400


def test_client_identity_cookies_are_installed(manager):
    cookies = manager.get_cookies()
    assert cookies.get("os") == "pc"
    assert len(cookies.get("_ntes_nuid", "")) == 32


# ------------------------------------------------------------------ bridge

def test_bridge_maps_the_throttle_error_key(bridge, monkeypatch):
    monkeypatch.setattr(bridge.login_manager, "send_phone_code",
                        lambda phone, ctcode="86": {"success": False, "message": "操作过于频繁",
                                                    "error_key": "login.code_too_frequent",
                                                    "throttled": True})
    result = bridge.send_phone_code("13800001111")
    assert result["success"] is False
    assert result["error_key"] == "login.code_too_frequent"
    assert result["throttled"] is True
    assert result["message"] == "操作过于频繁"


def test_bridge_exposes_the_last_qr_code(bridge, monkeypatch):
    bridge.login_manager._last_qr_code = 802
    bridge.login_manager._last_qr_message = "已扫码，等待确认"
    status = bridge.get_login_status()
    assert status["qr_code"] == 802
    assert status["qr_message"] == "已扫码，等待确认"


def test_bridge_does_not_mint_a_new_qrcode_while_one_is_pending(bridge, monkeypatch):
    """Reopening the login panel must not throw away an in-flight scan."""
    bridge.login_manager._qrcode_key = "KEY"
    bridge.login_manager._set_status(LoginStatus.PENDING)
    http_calls: list[tuple] = []
    monkeypatch.setattr(bridge.login_manager, "_session",
                        type("S", (), {"get": lambda *args, **kwargs: http_calls.append(args)})())
    reemitted: list[str] = []
    monkeypatch.setattr(bridge.login_manager, "_emit",
                        lambda event, *args: reemitted.append(event))

    assert bridge.login_qrcode()["success"] is True
    assert http_calls == [], "no new unikey request while one is on screen"
    assert reemitted == ["qrcode_update"]


def test_bridge_forwards_the_qrcode_status_payload(bridge):
    """The JS reads status.code / status.message to explain unknown answers."""
    calls = []
    monkeypatch_calls = calls.append
    bridge._call_js = lambda name, payload=None: monkeypatch_calls((name, payload))
    bridge.login_manager._last_qr_code = 4042
    bridge.login_manager._last_qr_message = "风控校验"
    bridge._on_login_message("风控校验")
    assert calls == [("onLoginMessage", {"message": "风控校验"})]
