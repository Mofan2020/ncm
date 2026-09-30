"""Login manager supporting QR code, phone verification, and cookie import."""
import os
import json
import time
import base64
import hashlib
import secrets
import threading
import requests
from enum import Enum
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from src.config import get_settings
from src.i18n import t


class LoginMethod(Enum):
    """Login method types."""
    QRCODE = "qrcode"
    PHONE = "phone"
    COOKIE = "cookie"


class LoginStatus(Enum):
    """Login status."""
    IDLE = "idle"
    PENDING = "pending"      # QR code waiting for scan
    SCANNED = "scanned"      # QR code scanned, waiting for confirmation
    LOGGING_IN = "logging_in"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class UserInfo:
    """User information after login."""
    user_id: str = ""
    nickname: str = ""
    avatar_url: str = ""
    vip_type: int = 0
    cookie: str = ""
    profile: Dict[str, Any] = field(default_factory=dict)


class LoginManager:
    """Manages authentication with NetEase Cloud Music."""
    
    _instance: Optional['LoginManager'] = None
    _lock = threading.Lock()
    
    # API endpoints
    QRCODE_KEY_URL = "https://music.163.com/api/login/qrcode/unikey"
    QRCODE_CHECK_URL = "https://music.163.com/api/login/qrcode/client/login"
    PHONE_SEND_CODE_URL = "https://music.163.com/api/sent/verificationcode"
    PHONE_LOGIN_URL = "https://music.163.com/api/login/cellphone"
    LOGIN_STATUS_URL = "https://music.163.com/api/login/status"
    LOGOUT_URL = "https://music.163.com/api/logout"
    USER_DETAIL_URL = "https://music.163.com/api/v1/user/detail/"
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self._status = LoginStatus.IDLE
        self._user_info: Optional[UserInfo] = None
        self._callbacks: Dict[str, list] = {
            'status_change': [],
            'qrcode_update': [],
            'login_success': [],
            'login_failed': []
        }
        
        # QR code polling
        self._qrcode_key: str = ""
        self._polling_thread: Optional[threading.Thread] = None
        _stop_polling = threading.Event()
        self._stop_polling = _stop_polling
        
        # Session with cookies
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15',
            'Referer': 'https://music.163.com/',
            'Content-Type': 'application/x-www-form-urlencoded',
        })
        
        # Load saved cookie
        self._load_saved_cookie()
    
    def _load_saved_cookie(self) -> None:
        """Load saved cookie from settings."""
        settings = get_settings()
        saved_cookie = settings.auth.saved_cookie
        if saved_cookie:
            try:
                self._session.cookies.update(self._parse_cookie_string(saved_cookie))
                # Verify cookie is still valid
                if self._check_login_status():
                    return
            except Exception:
                pass
            # Cookie invalid, clear it
            settings.update_auth(saved_cookie="")
    
    def _parse_cookie_string(self, cookie_str: str) -> Dict[str, str]:
        """Parse cookie string to dict."""
        cookies = {}
        for item in cookie_str.split(';'):
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                cookies[key.strip()] = value.strip()
        return cookies
    
    def _cookie_to_string(self) -> str:
        """Convert session cookies to string."""
        return '; '.join([f"{k}={v}" for k, v in self._session.cookies.get_dict().items()])
    
    def _save_cookie(self) -> None:
        """Save cookie to settings."""
        settings = get_settings()
        cookie_str = self._cookie_to_string()
        if cookie_str:
            settings.update_auth(saved_cookie=cookie_str)
    
    def _clear_cookie(self) -> None:
        """Clear saved cookie."""
        self._session.cookies.clear()
        settings = get_settings()
        settings.update_auth(saved_cookie="")
    
    # Callback management
    def on(self, event: str, callback: Callable) -> None:
        """Register event callback."""
        if event in self._callbacks:
            self._callbacks[event].append(callback)
    
    def off(self, event: str, callback: Callable) -> None:
        """Unregister event callback."""
        if event in self._callbacks and callback in self._callbacks[event]:
            self._callbacks[event].remove(callback)
    
    def _emit(self, event: str, *args, **kwargs) -> None:
        """Emit event to callbacks."""
        for callback in self._callbacks.get(event, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                print(f"Callback error: {e}")
    
    def _set_status(self, status: LoginStatus) -> None:
        """Update login status."""
        self._status = status
        self._emit('status_change', status)
    
    @property
    def status(self) -> LoginStatus:
        return self._status
    
    @property
    def user_info(self) -> Optional[UserInfo]:
        return self._user_info
    
    @property
    def is_logged_in(self) -> bool:
        return self._user_info is not None and self._check_login_status()
    
    def get_session(self) -> requests.Session:
        """Get authenticated session."""
        return self._session
    
    def get_cookies(self) -> Dict[str, str]:
        """Get current cookies as dict."""
        return self._session.cookies.get_dict()
    
    # QR Code Login
    def login_qrcode(self) -> bool:
        """Start QR code login flow."""
        self._stop_polling.clear()
        self._set_status(LoginStatus.PENDING)
        
        # Get QR code key
        try:
            resp = self._session.get(self.QRCODE_KEY_URL, params={'type': 1, 'ts': int(time.time() * 1000)})
            data = resp.json()
            if data.get('code') != 200:
                self._set_status(LoginStatus.FAILED)
                self._emit('login_failed', t('login.login_failed'))
                return False
            self._qrcode_key = data['unikey']
        except Exception as e:
            self._set_status(LoginStatus.FAILED)
            self._emit('login_failed', str(e))
            return False
        
        # Generate QR code URL
        qrcode_url = f"https://music.163.com/login?codekey={self._qrcode_key}"
        self._emit('qrcode_update', qrcode_url)
        
        # Start polling thread
        self._polling_thread = threading.Thread(target=self._poll_qrcode, daemon=True)
        self._polling_thread.start()
        return True
    
    def _poll_qrcode(self) -> None:
        """Poll for QR code scan status."""
        while not self._stop_polling.is_set():
            try:
                resp = self._session.get(
                    self.QRCODE_CHECK_URL,
                    params={'key': self._qrcode_key, 'type': 1, 'ts': int(time.time() * 1000)}
                )
                data = resp.json()
                code = data.get('code')
                
                if code == 800:
                    # Waiting for scan
                    if self._status != LoginStatus.PENDING:
                        self._set_status(LoginStatus.PENDING)
                elif code == 801:
                    # Scanned, waiting for confirmation
                    if self._status != LoginStatus.SCANNED:
                        self._set_status(LoginStatus.SCANNED)
                elif code == 802:
                    # Confirmed, login successful
                    self._stop_polling.set()
                    self._handle_login_success(data.get('cookie', ''))
                    return
                elif code == 803:
                    # Expired
                    self._stop_polling.set()
                    self._set_status(LoginStatus.EXPIRED)
                    self._emit('login_failed', t('login.qrcode_expired'))
                    return
                    
            except Exception as e:
                print(f"QR code poll error: {e}")
            
            time.sleep(2)
    
    def refresh_qrcode(self) -> bool:
        """Refresh QR code."""
        self.cancel_login()
        return self.login_qrcode()
    
    def cancel_login(self) -> None:
        """Cancel ongoing login."""
        self._stop_polling.set()
        if self._polling_thread and self._polling_thread.is_alive():
            self._polling_thread.join(timeout=1)
        self._set_status(LoginStatus.IDLE)
    
    # Phone Verification Login
    def send_phone_code(self, phone: str, ctcode: str = "86") -> bool:
        """Send verification code to phone."""
        try:
            resp = self._session.post(
                self.PHONE_SEND_CODE_URL,
                data={'phone': phone, 'ctcode': ctcode},
                headers={'Referer': 'https://music.163.com/'}
            )
            data = resp.json()
            return data.get('code') == 200
        except Exception as e:
            print(f"Send phone code error: {e}")
            return False
    
    def login_phone(self, phone: str, code: str, ctcode: str = "86") -> bool:
        """Login with phone and verification code."""
        self._set_status(LoginStatus.LOGGING_IN)
        try:
            resp = self._session.post(
                self.PHONE_LOGIN_URL,
                data={'phone': phone, 'captcha': code, 'ctcode': ctcode, 'rememberLogin': 'true'},
                headers={'Referer': 'https://music.163.com/'}
            )
            data = resp.json()
            if data.get('code') == 200:
                self._handle_login_success(self._cookie_to_string())
                return True
            else:
                self._set_status(LoginStatus.FAILED)
                self._emit('login_failed', data.get('msg', t('login.login_failed')))
                return False
        except Exception as e:
            self._set_status(LoginStatus.FAILED)
            self._emit('login_failed', str(e))
            return False
    
    # Cookie Import
    def login_cookie(self, cookie_str: str) -> bool:
        """Login with imported cookie."""
        self._set_status(LoginStatus.LOGGING_IN)
        try:
            cookies = self._parse_cookie_string(cookie_str)
            self._session.cookies.update(cookies)
            
            if self._check_login_status():
                self._handle_login_success(cookie_str)
                return True
            else:
                self._set_status(LoginStatus.FAILED)
                self._emit('login_failed', t('login.cookie_invalid'))
                return False
        except Exception as e:
            self._set_status(LoginStatus.FAILED)
            self._emit('login_failed', str(e))
            return False
    
    def _check_login_status(self) -> bool:
        """Check if current session is logged in."""
        try:
            resp = self._session.get(self.LOGIN_STATUS_URL)
            data = resp.json()
            return data.get('code') == 200 and data.get('data', {}).get('account') is not None
        except Exception:
            return False
    
    def _handle_login_success(self, cookie_str: str) -> None:
        """Handle successful login."""
        self._set_status(LoginStatus.SUCCESS)
        
        # Get user info
        try:
            resp = self._session.get(self.LOGIN_STATUS_URL)
            data = resp.json()
            account = data.get('data', {}).get('account', {})
            profile = data.get('data', {}).get('profile', {})
            
            self._user_info = UserInfo(
                user_id=str(account.get('id', '')),
                nickname=account.get('userName', profile.get('nickname', '')),
                avatar_url=profile.get('avatarUrl', ''),
                vip_type=profile.get('vipType', 0),
                cookie=cookie_str,
                profile=profile
            )
            
            # Save cookie if remember login
            settings = get_settings()
            if settings.auth.remember_login:
                self._save_cookie()
            
            self._emit('login_success', self._user_info)
        except Exception as e:
            print(f"Get user info error: {e}")
            self._emit('login_success', self._user_info)
    
    def logout(self) -> None:
        """Logout current user."""
        try:
            self._session.post(self.LOGOUT_URL)
        except Exception:
            pass
        self._clear_cookie()
        self._user_info = None
        self._set_status(LoginStatus.IDLE)
    
    def get_qrcode_image(self, qrcode_url: str, size: int = 256) -> Optional[bytes]:
        """Generate QR code image from URL."""
        try:
            import qrcode
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            from io import BytesIO
            buffer = BytesIO()
            img.save(buffer, format='PNG')
            return buffer.getvalue()
        except Exception as e:
            print(f"Generate QR code error: {e}")
            return None


def get_login_manager() -> LoginManager:
    """Get global login manager instance."""
    return LoginManager()