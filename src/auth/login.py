"""Login manager: QR code, SMS captcha / phone, and cookie import.

Flow notes (verified live on 2026-09-30 -- see ``docs/notes.md``):

* ``/api/login/qrcode/unikey`` + ``/api/login/qrcode/client/login`` need **no**
  encryption and work as-is.  The QR payload is
  ``https://music.163.com/login?codekey=<unikey>``.
* The poll codes are ``800 = expired``, ``801 = waiting for scan``,
  ``802 = scanned, waiting for confirmation``, ``803 = authorised``.  (An
  earlier revision of this project had them shifted by one, which made QR login
  impossible.)
* The SMS endpoint takes **``cellphone``** (not ``phone``) and phone login goes
  through ``/api/w/login/cellphone``; ``/api/login/cellphone`` answers
  ``401 无权限访问. ENC`` (it wants an encrypted payload we no longer have a
  working channel for) and ``/api/sent/verificationcode`` no longer exists.
* ``/api/login/status`` no longer exists either -- the current user is read from
  ``/api/w/nuser/account/get``.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import requests

from src.config import get_settings

__all__ = [
    "LoginMethod",
    "LoginStatus",
    "UserInfo",
    "LoginManager",
    "get_login_manager",
]


class LoginMethod(Enum):
    """Login method types."""

    QRCODE = "qrcode"
    PHONE = "phone"
    COOKIE = "cookie"


class LoginStatus(Enum):
    """Login status."""

    IDLE = "idle"
    PENDING = "pending"      # QR code shown, waiting for the scan
    SCANNED = "scanned"      # scanned, waiting for confirmation on the phone
    LOGGING_IN = "logging_in"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class UserInfo:
    """Authenticated user."""

    user_id: str = ""
    nickname: str = ""
    avatar_url: str = ""
    vip_type: int = 0
    cookie: str = ""
    profile: dict[str, Any] = field(default_factory=dict)


#: QR poll response codes -> our status enum.
_QR_STATUS = {
    800: LoginStatus.EXPIRED,
    801: LoginStatus.PENDING,
    802: LoginStatus.SCANNED,
    803: LoginStatus.SUCCESS,
}


class LoginManager:
    """Manages authentication with NetEase Cloud Music (singleton)."""

    _instance: LoginManager | None = None
    _lock = threading.Lock()

    BASE_URL = "https://music.163.com"
    QRCODE_KEY_URL = f"{BASE_URL}/api/login/qrcode/unikey"
    QRCODE_CHECK_URL = f"{BASE_URL}/api/login/qrcode/client/login"
    SMS_CODE_URL = f"{BASE_URL}/api/sms/captcha/sent"
    PHONE_LOGIN_URL = f"{BASE_URL}/api/w/login/cellphone"
    LOGOUT_URL = f"{BASE_URL}/api/logout"
    ACCOUNT_URL = f"{BASE_URL}/api/w/nuser/account/get"

    USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15")

    #: How long a positive login check is trusted before re-verifying.
    STATUS_TTL = 60.0

    def __new__(cls) -> LoginManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._status = LoginStatus.IDLE
        self._user_info: UserInfo | None = None
        self._callbacks: dict[str, list] = {
            "status_change": [],
            "qrcode_update": [],
            "login_success": [],
            "login_failed": [],
        }

        self._qrcode_key = ""
        self._polling_thread: threading.Thread | None = None
        self._stop_polling = threading.Event()
        self._poll_lock = threading.Lock()

        self._status_checked_at = 0.0
        self.logger = logging.getLogger("ncm.login")

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Referer": "https://music.163.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        })

        self.load_saved_cookie()

    # ------------------------------------------------------------- cookie I/O
    @staticmethod
    def parse_cookie_string(cookie_str: str) -> dict[str, str]:
        """Parse a ``k=v; k2=v2`` cookie header into a dict."""
        cookies: dict[str, str] = {}
        for item in (cookie_str or "").split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
        return cookies

    def _cookie_string(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._session.cookies.get_dict().items())

    def apply_cookie_string(self, cookie_str: str) -> None:
        """Merge a cookie header into the session."""
        for key, value in self.parse_cookie_string(cookie_str).items():
            self._session.cookies.set(key, value, domain=".music.163.com")

    def _save_cookie(self) -> None:
        cookie_str = self._cookie_string()
        if cookie_str:
            get_settings().update_auth(saved_cookie=cookie_str)

    def _clear_cookie(self) -> None:
        self._session.cookies.clear()
        get_settings().update_auth(saved_cookie="")

    def load_saved_cookie(self) -> bool:
        """Restore a remembered session and report whether it is still valid."""
        saved = get_settings().auth.saved_cookie
        if not saved:
            return False
        self.apply_cookie_string(saved)
        ok, account, profile = self._fetch_account()
        if ok:
            self._set_user_from_account(account, profile)
            self._set_status(LoginStatus.SUCCESS)
            return True
        if account is None and profile is None:
            # Server answered properly but the cookie is dead -> drop it.
            self._clear_cookie()
        return False

    # --------------------------------------------------------------- callbacks
    def on(self, event: str, callback: Callable) -> None:
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    def off(self, event: str, callback: Callable) -> None:
        if event in self._callbacks and callback in self._callbacks[event]:
            self._callbacks[event].remove(callback)

    def _emit(self, event: str, *args, **kwargs) -> None:
        for callback in list(self._callbacks.get(event, [])):
            try:
                callback(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.warning("callback %s failed: %s", event, exc)

    # ------------------------------------------------------------------ state
    def _set_status(self, status: LoginStatus) -> None:
        if status == self._status:
            return
        self._status = status
        self._emit("status_change", status)

    @property
    def status(self) -> LoginStatus:
        return self._status

    @property
    def user_info(self) -> UserInfo | None:
        return self._user_info

    @property
    def is_logged_in(self) -> bool:
        """Whether a user session is currently held.

        Purely local -- it never performs a network round trip, so it is safe to
        call on every request.  Use :meth:`verify_login` to re-check online.
        """
        return self._user_info is not None

    def get_session(self) -> requests.Session:
        return self._session

    def get_cookies(self) -> dict[str, str]:
        return self._session.cookies.get_dict()

    def invalidate(self) -> None:
        """Forget the cached login state (called when an API call says so)."""
        self._status_checked_at = 0.0

    def verify_login(self, force: bool = False) -> bool:
        """Re-check the session online at most once per :attr:`STATUS_TTL`."""
        if not force and self._user_info is not None:
            if time.time() - self._status_checked_at < self.STATUS_TTL:
                return True

        ok, account, profile = self._fetch_account()
        self._status_checked_at = time.time()
        if ok:
            self._set_user_from_account(account, profile)
            self._set_status(LoginStatus.SUCCESS)
            return True
        if account is None and profile is None and self._user_info is not None:
            # Session expired server-side.
            self._user_info = None
            self._clear_cookie()
            self._set_status(LoginStatus.IDLE)
        return False

    def _set_user_from_account(self, account: dict[str, Any], profile: dict[str, Any]) -> None:
        self._user_info = UserInfo(
            user_id=str(account.get("id", "")),
            nickname=account.get("userName") or profile.get("nickname", ""),
            avatar_url=profile.get("avatarUrl", ""),
            vip_type=profile.get("vipType", 0) or 0,
            cookie=self._cookie_string(),
            profile=profile,
        )

    def _fetch_account(self) -> tuple[bool, dict[str, Any], dict[str, Any]]:
        """Query the current account. Returns ``(logged_in, account, profile)``."""
        try:
            resp = self._session.get(self.ACCOUNT_URL, params={"timestamp": int(time.time() * 1000)},
                                     timeout=(5, 15))
            if resp.status_code != 200 or not resp.text.strip():
                return False, {}, {}
            data = resp.json()
        except Exception as exc:
            self.logger.warning("account query failed: %s", exc)
            return False, {}, {}

        if data.get("code") != 200:
            self.logger.warning("account query returned code %s", data.get("code"))
            return False, {}, {}
        account = data.get("account") or {}
        profile = data.get("profile") or {}
        if not account:
            return False, {}, {}
        return True, account, profile

    # ---------------------------------------------------------- QR code login
    def login_qrcode(self) -> bool:
        """Start the QR-code login flow and begin polling."""
        self._stop_polling.clear()
        self._set_status(LoginStatus.PENDING)

        try:
            resp = self._session.get(self.QRCODE_KEY_URL,
                                     params={"type": 1, "timestamp": int(time.time() * 1000)},
                                     timeout=(5, 15))
            data = resp.json()
        except Exception as exc:
            self._fail(f"QR key request failed: {exc}")
            return False

        if data.get("code") != 200 or not data.get("unikey"):
            self._fail(data.get("message") or "QR key unavailable")
            return False

        self._qrcode_key = data["unikey"]
        qrcode_url = f"{self.BASE_URL}/login?codekey={self._qrcode_key}"
        self._emit("qrcode_update", qrcode_url)

        with self._poll_lock:
            self._polling_thread = threading.Thread(target=self._poll_qrcode, daemon=True)
            self._polling_thread.start()
        return True

    def _poll_qrcode(self) -> None:
        while not self._stop_polling.is_set():
            try:
                resp = self._session.get(
                    self.QRCODE_CHECK_URL,
                    params={"key": self._qrcode_key, "type": 1, "timestamp": int(time.time() * 1000)},
                    timeout=(5, 15),
                )
                data = resp.json()
            except Exception as exc:
                self.logger.warning("QR poll error: %s", exc)
                if self._stop_polling.wait(2.0):
                    return
                continue

            code = data.get("code")
            status = _QR_STATUS.get(code)

            if status is LoginStatus.SUCCESS:
                self._stop_polling.set()
                cookie = data.get("cookie") or ""
                if not cookie and isinstance(data.get("cookies"), dict):
                    cookie = "; ".join(f"{k}={v}" for k, v in data["cookies"].items())
                if cookie:
                    self.apply_cookie_string(cookie)
                self._handle_login_success(cookie)
                return

            if status is LoginStatus.EXPIRED:
                self._stop_polling.set()
                self._set_status(LoginStatus.EXPIRED)
                self._emit("login_failed", "qrcode_expired")
                return

            if status is not None:
                self._set_status(status)
            elif code is not None:
                self.logger.debug("unknown QR code %s", code)

            if self._stop_polling.wait(2.0):
                return

    def refresh_qrcode(self) -> bool:
        self.cancel_login()
        return self.login_qrcode()

    def cancel_login(self) -> None:
        self._stop_polling.set()
        thread = self._polling_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._polling_thread = None
        if self._status in (LoginStatus.PENDING, LoginStatus.SCANNED, LoginStatus.LOGGING_IN):
            self._set_status(LoginStatus.IDLE)

    # ------------------------------------------------------- SMS / phone login
    def send_phone_code(self, phone: str, ctcode: str = "86") -> dict[str, Any]:
        """Request an SMS captcha. Returns ``{'success': bool, 'message': str}``."""
        try:
            resp = self._session.post(
                self.SMS_CODE_URL,
                data={"cellphone": phone, "ctcode": ctcode},
                timeout=(5, 15),
            )
            data = resp.json()
        except Exception as exc:
            return {"success": False, "message": str(exc)}

        if data.get("code") == 200:
            return {"success": True, "message": "sent"}
        return {"success": False, "message": data.get("message") or data.get("msg") or "unknown error"}

    def login_phone(self, phone: str, code: str, ctcode: str = "86") -> dict[str, Any]:
        """Log in with a phone number + SMS captcha."""
        self._set_status(LoginStatus.LOGGING_IN)
        try:
            resp = self._session.post(
                self.PHONE_LOGIN_URL,
                data={
                    "phone": phone,
                    "countrycode": ctcode,
                    "captcha": code,
                    "rememberLogin": "true",
                },
                timeout=(5, 15),
            )
            data = resp.json()
        except Exception as exc:
            self._fail(str(exc))
            return {"success": False, "message": str(exc)}

        if data.get("code") == 200:
            self._handle_login_success(self._cookie_string())
            return {"success": True, "message": "ok"}

        message = data.get("message") or data.get("msg") or f"code {data.get('code')}"
        self._fail(message)
        return {"success": False, "message": message}

    # --------------------------------------------------------- cookie import
    def login_cookie(self, cookie_str: str) -> dict[str, Any]:
        """Log in by importing a browser cookie header."""
        self._set_status(LoginStatus.LOGGING_IN)
        self.apply_cookie_string(cookie_str)
        ok, account, profile = self._fetch_account()
        if ok:
            self._set_user_from_account(account, profile)
            self._handle_login_success(self._cookie_string())
            return {"success": True, "message": "ok"}

        self._fail("cookie_invalid")
        return {"success": False, "message": "cookie_invalid"}

    def _handle_login_success(self, cookie_str: str) -> None:
        if not self.verify_login(force=True):
            self._fail("login_status_check_failed")
            return
        self._status_checked_at = time.time()
        if get_settings().auth.remember_login:
            self._save_cookie()
        self._set_status(LoginStatus.SUCCESS)
        self._emit("login_success", self._user_info)

    def _fail(self, message: str) -> None:
        self._set_status(LoginStatus.FAILED)
        self._emit("login_failed", message)

    def logout(self) -> None:
        """Drop the session both locally and on the server."""
        try:
            self._session.post(self.LOGOUT_URL, data={"csrf_token": ""}, timeout=(5, 15))
        except Exception as exc:
            self.logger.info("logout request failed (ignored): %s", exc)
        self.cancel_login()
        self._clear_cookie()
        self._user_info = None
        self._status_checked_at = 0.0
        self._set_status(LoginStatus.IDLE)

    # ------------------------------------------------------------------ image
    def get_qrcode_image(self, qrcode_url: str, size: int = 256) -> bytes | None:
        """Render ``qrcode_url`` as a PNG (used by the GUI)."""
        try:
            from io import BytesIO

            import qrcode

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=2,
            )
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            if size:
                image = image.resize((size, size))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as exc:
            self.logger.warning("QR render failed: %s", exc)
            return None


def get_login_manager() -> LoginManager:
    """Return the process-wide login manager."""
    return LoginManager()
