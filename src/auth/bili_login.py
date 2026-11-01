"""B站账号会话。

网易云的登录态放在 ``settings.yaml`` 里，B站另存一个文件：两边的 cookie
格式、有效期和刷新方式都不一样，混在一起只会在升级时互相拖累。文件放在
配置目录下的 ``bilibili.json``，内容只有三样：

.. code-block:: json

    {
      "cookie": "SESSDATA=...; bili_jct=...",
      "mid": "123456",
      "uname": "某人",
      "saved_at": "2026-10-31 21:00:00"
    }

这个模块只管「会话在不在、cookie 是什么、账号是谁」，登录的具体流程在
:mod:`src.core.bilibili`，界面层的翻译在 :mod:`src.gui.bilibili_bridge`。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.bilibili import BiliClient

__all__ = ["BiliSession", "get_bili_session"]

log = logging.getLogger(__name__)

#: The file name inside the config directory.
SESSION_FILE = "bilibili.json"


class BiliSession:
    """The Bilibili session: one client, one remembered cookie."""

    def __init__(self, client: BiliClient | None = None,
                 path: str | Path | None = None) -> None:
        self._client = client if client is not None else BiliClient()
        self._path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._account: dict[str, Any] = {}
        self._cookie = ""
        self.load()

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> Path:
        """Where the session is stored."""
        if self._path is not None:
            return self._path
        from src.config.settings import get_settings

        return get_settings().config_path.parent / SESSION_FILE

    # ----------------------------------------------------------------- access
    @property
    def client(self) -> BiliClient:
        """The core client that carries the cookie."""
        return self._client

    @property
    def cookie(self) -> str:
        return self._cookie

    @property
    def account(self) -> dict[str, Any]:
        """The last known account information (may be empty)."""
        return dict(self._account)

    @property
    def is_logged_in(self) -> bool:
        return bool(self._cookie)

    # ------------------------------------------------------------------- I/O
    def load(self) -> bool:
        """Restore the remembered cookie.  Returns whether one was found."""
        path = self.path
        try:
            if not path.is_file():
                return False
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("cannot read the bilibili session (%s)", exc)
            return False
        if not isinstance(data, dict):
            return False
        cookie = str(data.get("cookie") or "")
        if not cookie:
            return False
        with self._lock:
            self._cookie = cookie
            self._account = {
                "mid": str(data.get("mid") or ""),
                "uname": str(data.get("uname") or ""),
                "face": str(data.get("face") or ""),
            }
        self._client.set_cookie(cookie)
        return True

    def save(self, cookie: str, account: dict[str, Any] | None = None) -> bool:
        """Remember ``cookie`` on disk.  Never raises."""
        cookie = str(cookie or "").strip()
        if not cookie:
            return self.clear()
        account = account or {}
        with self._lock:
            self._cookie = cookie
            self._account = {
                "mid": str(account.get("mid") or ""),
                "uname": str(account.get("uname") or ""),
                "face": str(account.get("face") or ""),
            }
        self._client.set_cookie(cookie)
        payload = {
            "cookie": cookie,
            "mid": self._account["mid"],
            "uname": self._account["uname"],
            "face": self._account["face"],
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file first: a half written session file
            # would log the user out on the next start.
            handle, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                                 prefix=".bilibili-", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
            return True
        except OSError as exc:
            log.warning("cannot save the bilibili session (%s)", exc)
            return False

    def clear(self) -> bool:
        """Forget the session."""
        with self._lock:
            self._cookie = ""
            self._account = {}
        try:
            self._client.set_cookie("")
        except Exception:  # pragma: no cover - the client does not raise
            pass
        try:
            path = self.path
            if path.is_file():
                path.unlink()
        except OSError as exc:
            log.warning("cannot remove the bilibili session (%s)", exc)
            return False
        return True

    # ---------------------------------------------------------------- account
    def account_info(self, refresh: bool = False) -> dict[str, Any]:
        """The account information, from the server when asked to refresh."""
        with self._lock:
            cached = dict(self._account)
            has_cookie = bool(self._cookie)
        # 只有真正拿到了「是谁」才算有缓存：登录那一刻写下的 cookie 旁边
        # mid/uname/face 都是空的，拿这份空缓存当答案，界面就会一直显示
        # 一个没名字没头像的账号，直到手动点刷新。
        if not refresh and cached.get("mid"):
            return {
                "logged_in": True,
                "mid": cached.get("mid", ""),
                "uname": cached.get("uname", ""),
                "face": cached.get("face", ""),
                "level": 0,
                "vip_status": 0,
                "vip_label": "",
                "coins": 0,
                "stale": True,
            }
        if not has_cookie:
            return {
                "logged_in": False, "mid": "", "uname": "", "face": "",
                "level": 0, "vip_status": 0, "vip_label": "", "coins": 0,
                "stale": False,
            }
        info = self._client.account_info()
        if info.get("logged_in"):
            with self._lock:
                self._account = {
                    "mid": str(info.get("mid") or ""),
                    "uname": str(info.get("uname") or ""),
                    "face": str(info.get("face") or ""),
                }
            # The account name changes; keep the file in step with it.
            self.save(self._cookie, self._account)
        return {**info, "stale": False}

    def apply_to_media(self, media: Any) -> None:
        """Give the media server the cookie it needs for CDN requests."""
        setter = getattr(media, "set_bili_cookie", None)
        if setter is None:
            return
        try:
            setter(self._cookie)
        except Exception:  # pragma: no cover - the server does not raise
            log.debug("cannot hand the bilibili cookie to the media server")


_INSTANCE: BiliSession | None = None
_INSTANCE_LOCK = threading.Lock()


def get_bili_session() -> BiliSession:
    """The shared session (singleton)."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = BiliSession()
    return _INSTANCE
