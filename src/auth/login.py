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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
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

#: The plain route and the signed ``eapi`` route are polled alternately: both
#: answer (verified live), and a second channel means one misbehaving CDN node
#: cannot strand the login in ``scanned``.
_QR_CHANNELS = ("api", "eapi")

#: Throttle answers from the SMS endpoint ("操作过于频繁") -- never retried,
#: because retrying is what turns a one-minute cooldown into a long lockout.
_THROTTLE_CODES = (429, 503, 509)
_THROTTLE_WORDS = ("频繁", "过快", "稍后", "too often", "too many", "frequent")


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

    #: Seconds between QR polls (the phone needs a moment to confirm).
    QR_POLL_INTERVAL = 1.5

    #: Identity cookies the official PC client always carries.  Some login
    #: endpoints only hand out a session cookie when the caller looks like a PC
    #: client; they are harmless for the endpoints that ignore them.
    CLIENT_COOKIES = {"os": "pc", "appver": "8.9.70", "channel": "netease"}

    #: Cap for ``login-debug.log`` before it is rotated away.
    DEBUG_LOG_MAX = 256 * 1024

    #: A fresh login cookie is not always accepted by the account endpoint on
    #: the very first try (server-side propagation), so verification is retried.
    VERIFY_ATTEMPTS = 3
    VERIFY_RETRY_DELAY = 0.8

    #: Diagnostics land next to ``settings.yaml`` so a user can find them.
    DEBUG_LOG_NAME = "login-debug.log"

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
            "status_message": [],
        }

        self._qrcode_key = ""
        self._polling_thread: threading.Thread | None = None
        self._stop_polling = threading.Event()
        self._poll_lock = threading.Lock()

        self._status_checked_at = 0.0
        self._last_qr_code: int | None = None
        self._last_qr_message = ""
        self.logger = logging.getLogger("ncm.login")

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Referer": "https://music.163.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        self._install_client_identity()

        self.load_saved_cookie()

    def _install_client_identity(self) -> None:
        """Pretend to be the PC client (cookies only; the API still acks us)."""
        for name, value in self.CLIENT_COOKIES.items():
            self._session.cookies.set(name, value, domain=".music.163.com")
        if not self._session.cookies.get("_ntes_nuid"):
            self._session.cookies.set("_ntes_nuid", uuid.uuid4().hex, domain=".music.163.com")

    # ------------------------------------------------------------ diagnostics
    def config_path_for_debug(self) -> Path:
        """Where :meth:`_debug_log` writes (and where a user should look)."""
        return get_settings().config_path.with_name(self.DEBUG_LOG_NAME)

    def _debug_log(self, message: str) -> None:
        """Append one line to ``login-debug.log`` next to ``settings.yaml``.

        Nothing here may raise: a logging failure must never break a login.
        """
        try:
            path = self.config_path_for_debug()
            if path.exists() and path.stat().st_size > self.DEBUG_LOG_MAX:
                path.unlink()
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {message}\n")
        except Exception:
            pass

    @staticmethod
    def _mask_phone(phone: str) -> str:
        phone = str(phone or "")
        if len(phone) < 7:
            return "***"
        return f"{phone[:3]}****{phone[-2:]}"

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
    def last_qr_code(self) -> int | None:
        """Most recent QR poll code (``801`` / ``802`` / ``803`` ...), for the UI."""
        return self._last_qr_code

    @property
    def last_qr_message(self) -> str:
        """Server text that came with :attr:`last_qr_code`."""
        return self._last_qr_message

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
        # ``profile.nickname`` is the name the user chose; ``account.userName`` is
        # the privacy-masked form (``1_********477``), which tells nobody who they
        # are in the header.  Fall back to it, never lead with it.
        nickname = profile.get("nickname") or account.get("userName") or ""
        self._user_info = UserInfo(
            user_id=str(account.get("id", "")),
            nickname=nickname,
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
    def login_qrcode(self, force: bool = False) -> bool:
        """Start the QR-code login flow and begin polling.

        A code that is still on screen is reused (unless ``force``): minting a
        new unikey would invalidate a scan the user is confirming on the phone,
        which looks exactly like a login that never completes.
        """
        if not force and self.has_pending_qrcode():
            return self.resend_qrcode()

        self._stop_polling.clear()
        self._set_status(LoginStatus.PENDING)

        try:
            resp = self._session.get(self.QRCODE_KEY_URL,
                                     params={"type": 1, "timestamp": int(time.time() * 1000)},
                                     timeout=(5, 15))
            data = resp.json()
        except Exception as exc:
            self._debug_log(f"qrcode: unikey request failed: {exc}")
            self._fail(f"QR key request failed: {exc}")
            return False

        if data.get("code") != 200 or not data.get("unikey"):
            self._debug_log(f"qrcode: unikey refused: {data}")
            self._fail(data.get("message") or "QR key unavailable")
            return False

        self._qrcode_key = data["unikey"]
        qrcode_url = f"{self.BASE_URL}/login?codekey={self._qrcode_key}"
        self._last_qr_code = None
        self._last_qr_message = ""
        self._debug_log(f"qrcode: unikey={self._qrcode_key}")
        self._emit("qrcode_update", qrcode_url)

        with self._poll_lock:
            self._polling_thread = threading.Thread(target=self._poll_qrcode, daemon=True)
            self._polling_thread.start()
        return True

    def _poll_qrcode(self) -> None:
        attempt = 0
        while not self._stop_polling.is_set():
            channel = _QR_CHANNELS[attempt % len(_QR_CHANNELS)]
            attempt += 1

            data = self._poll_once(channel)
            if data is None:
                if self._stop_polling.wait(2.0):
                    return
                continue

            code = self._qr_code_of(data)
            message = str(data.get("message") or data.get("msg") or "")
            cookie = self._qr_cookie_of(data)

            if (code, message) != (self._last_qr_code, self._last_qr_message):
                # Log every change (never on every poll -- that would spin the
                # log while the QR just sits there waiting for a scan).
                self._last_qr_code, self._last_qr_message = code, message
                self._debug_log(f"poll {channel}: code={code} msg={message!r} "
                                f"cookie={'yes' if cookie else 'no'}")
                if code not in _QR_STATUS:
                    # An unknown answer used to be swallowed by a debug log and
                    # left the UI spinning on its last text.  Show it instead.
                    self._emit("status_message", message or f"code {code}")

            # Success is code 803 -- but some builds hand the cookie over with a
            # plain 200, and a nested ``data.code`` is possible too.
            if code == 803 or (code == 200 and cookie):
                self._stop_polling.set()
                if cookie:
                    self.apply_cookie_string(cookie)
                self._debug_log(f"qrcode authorised via {channel} (code={code})")
                self._handle_login_success(cookie)
                return

            if code == 800:
                self._stop_polling.set()
                self._last_qr_code, self._last_qr_message = code, message
                self._set_status(LoginStatus.EXPIRED)
                self._emit("login_failed", "qrcode_expired")
                return

            status = _QR_STATUS.get(code)
            if status is not None:
                self._set_status(status)

            if self._stop_polling.wait(self.QR_POLL_INTERVAL):
                return

    def _poll_once(self, channel: str) -> dict[str, Any] | None:
        """Poll the QR state on the plain (``api``) or signed (``eapi``) route."""
        try:
            if channel == "eapi":
                # Imported here on purpose: ``src.core`` imports ``src.auth``, so a
                # module-level import would create a cycle and break every entry
                # point that imports this module first (see tests/test_imports.py).
                from src.core.crypto import EAPI_PREFIX, eapi_body

                body = eapi_body("/api/login/qrcode/client/login",
                                 {"key": self._qrcode_key, "type": 1})
                url = f"{self.BASE_URL}{EAPI_PREFIX}/api/login/qrcode/client/login"
                resp = self._session.post(url, data=body, timeout=(5, 15))
            else:
                resp = self._session.get(
                    self.QRCODE_CHECK_URL,
                    params={"key": self._qrcode_key, "type": 1,
                            "timestamp": int(time.time() * 1000)},
                    timeout=(5, 15),
                )
            if resp.status_code != 200 or not (resp.text or "").strip():
                self._debug_log(f"poll {channel}: HTTP {resp.status_code} empty response")
                return None
            payload = resp.json()
        except Exception as exc:
            self.logger.warning("QR poll error on %s: %s", channel, exc)
            self._debug_log(f"poll {channel}: error {exc}")
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _qr_code_of(data: dict[str, Any]) -> int | None:
        """The poll code, tolerating a nested ``data`` object."""
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        for candidate in (data.get("code"), nested.get("code")):
            if isinstance(candidate, bool):
                continue
            if isinstance(candidate, int):
                return candidate
        return None

    @staticmethod
    def _qr_cookie_of(data: dict[str, Any]) -> str:
        """Extract a session cookie from any shape NetEase has used."""
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        for source in (data, nested):
            for key_name in ("cookie", "cookies"):
                value = source.get(key_name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict) and value:
                    return "; ".join(f"{k}={v}" for k, v in value.items())
        return ""

    def refresh_qrcode(self) -> bool:
        self.cancel_login()
        return self.login_qrcode(force=True)

    def has_pending_qrcode(self) -> bool:
        """True while a QR code is on screen and still resolvable."""
        return bool(self._qrcode_key) and self._status in (LoginStatus.PENDING,
                                                           LoginStatus.SCANNED)

    def resend_qrcode(self) -> bool:
        """Re-emit the QR currently on screen -- **without** a new unikey.

        Reopening the login panel used to mint a fresh unikey; a scan the user
        was confirming on the phone then pointed at a key nobody polled, which
        looks exactly like "scanned but never finishes".
        """
        if not self._qrcode_key:
            return False
        self._emit("qrcode_update", f"{self.BASE_URL}/login?codekey={self._qrcode_key}")
        return True

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
        """Request an SMS captcha.

        Returns ``{'success', 'message', 'error_key', 'throttled'}``.

        Measurements (2026-09-30): the endpoint acks *anything* -- a bogus number
        also gets ``{"code":200,"data":true}`` -- so the ``data`` flag is what
        decides whether the request was accepted, and a throttle answer carries
        code 429/503 or a "频繁" style message.  Throttle answers are **never
        retried**: each extra request extends NetEase's cooldown for the number.
        """
        masked = self._mask_phone(phone)
        try:
            resp = self._session.post(
                self.SMS_CODE_URL,
                data={"cellphone": phone, "ctcode": ctcode,
                      "timestamp": int(time.time() * 1000)},
                timeout=(5, 15),
            )
            data = resp.json()
        except Exception as exc:
            self._debug_log(f"sms {masked}: request failed: {exc}")
            return {"success": False, "message": str(exc),
                    "error_key": "login.code_send_failed"}

        code = data.get("code")
        message = str(data.get("message") or data.get("msg") or "")
        accepted = data.get("data")
        self._debug_log(f"sms {masked}: code={code} data={accepted!r} msg={message!r}")

        if code == 200 and accepted is not False:
            return {"success": True, "message": "sent", "error_key": None}

        throttled = code in _THROTTLE_CODES or any(word in message for word in _THROTTLE_WORDS)
        return {
            "success": False,
            "message": message or f"code {code}",
            "error_key": "login.code_too_frequent" if throttled else "login.code_send_failed",
            "throttled": throttled,
        }

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
        """Verify the fresh session, retrying briefly before giving up.

        The retry matters: a QR authorisation can hand over a cookie that the
        account endpoint only honours a moment later, and without the retry that
        turned a *successful* login into a failure while the UI still showed
        "scanned, confirm on your phone".
        """
        verified = False
        for attempt in range(self.VERIFY_ATTEMPTS):
            if self.verify_login(force=True):
                verified = True
                break
            if attempt < self.VERIFY_ATTEMPTS - 1:
                time.sleep(self.VERIFY_RETRY_DELAY)

        if not verified:
            self._debug_log(f"login: the session was not confirmed after "
                            f"{self.VERIFY_ATTEMPTS} tries "
                            f"(cookie={'yes' if cookie_str else 'no'}, "
                            f"session cookies={len(self.get_cookies())})")
            self._fail("login_status_check_failed")
            return
        self._status_checked_at = time.time()
        if get_settings().auth.remember_login:
            self._save_cookie()
        self._debug_log(f"login: success as {self._user_info.user_id if self._user_info else '?'}")
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
