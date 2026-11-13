"""Pixiv 账号会话。

网易云的登录态放在 ``settings.yaml``，B站放在 ``bilibili.json``，Pixiv 再
单独一个 ``pixiv.json``：三家的 cookie 结构、有效期和刷新方式都不一样，
混在一个文件里只会在升级时互相拖累。

文件里只有四样：

.. code-block:: json

    {
      "cookie": "PHPSESSID=...; device_token=...",
      "id": "12345678",
      "name": "某人",
      "saved_at": "2026-11-12T21:00:00"
    }

``device_token`` 必须跟着一起存：pixiv 拿它认设备，少了它每次重启都会被
当成一台新机器，转头就把登录态踢掉重发验证。:meth:`PixivClient.cookie_from_webview`
已经把要留的那几个筛好了。

这个模块只管「会话在不在、cookie 是什么、账号是谁」；登录动作本身是界面
层开一个 webview 窗口让用户自己登，流程在 :mod:`src.gui.pixiv_bridge`。
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

from src.core.pixiv import PixivClient

__all__ = ["PixivSession", "get_pixiv_session", "PIXIV_SESSION_FILE"]

log = logging.getLogger(__name__)

#: 配置目录下的文件名。
PIXIV_SESSION_FILE = "pixiv.json"

#: 换账号时要报出去的字段，集中一处，免得两份 dict 慢慢长歪。
_ACCOUNT_KEYS = ("id", "name", "account", "avatar", "comment", "premium",
                 "mail_authorized", "following")


class PixivSession:
    """一个客户端 + 一份记住的 cookie。"""

    def __init__(self,                 client: Any = None,
                 path: str | Path | None = None) -> None:
        self._client = client if client is not None else PixivClient()
        self._path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._account: dict[str, Any] = {}
        self._cookie = ""
        self.load()

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> Path:
        """会话文件的位置。"""
        if self._path is not None:
            return self._path
        from src.config.settings import get_settings

        return get_settings().config_path.parent / PIXIV_SESSION_FILE

    # ----------------------------------------------------------------- access
    @property
    def client(self) -> PixivClient:
        return self._client

    @property
    def cookie(self) -> str:
        return self._cookie

    @property
    def account(self) -> dict[str, Any]:
        return dict(self._account)

    @property
    def is_logged_in(self) -> bool:
        """有 cookie 就算「存过」。能不能用只有 :meth:`account_info` 知道。"""
        return bool(self._cookie)

    # ------------------------------------------------------------------- I/O
    def load(self) -> bool:
        """把磁盘上的会话读回来。"""
        path = self.path
        try:
            if not path.is_file():
                return False
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("读取 pixiv 会话失败（%s）", exc)
            return False
        if not isinstance(data, dict):
            return False
        cookie = str(data.get("cookie") or "").strip()
        if not cookie:
            return False
        with self._lock:
            self._cookie = cookie
            self._account = {key: data.get(key) for key in _ACCOUNT_KEYS
                             if data.get(key) not in (None, "")}
        self._client.apply_cookie(cookie)
        return True

    def save(self, cookie: str, account: dict[str, Any] | None = None) -> bool:
        """记住 ``cookie``。永不抛。"""
        cookie = str(cookie or "").strip()
        if not cookie:
            return self.clear()
        account = account or {}
        with self._lock:
            self._cookie = cookie
            self._account = {key: account.get(key) for key in _ACCOUNT_KEYS
                             if account.get(key) not in (None, "")}
            snapshot = dict(self._account)
        self._client.apply_cookie(cookie)
        payload = {
            "cookie": cookie,
            **snapshot,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再换名：写到一半的会话文件会让下次启动直接掉登录。
            handle, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                                prefix=".pixiv-", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
            return True
        except OSError as exc:
            log.warning("保存 pixiv 会话失败（%s）", exc)
            return False

    def clear(self) -> bool:
        """忘掉会话。"""
        with self._lock:
            self._cookie = ""
            self._account = {}
        self._client.apply_cookie("")
        try:
            path = self.path
            if path.is_file():
                path.unlink()
        except OSError as exc:
            log.warning("删除 pixiv 会话失败（%s）", exc)
            return False
        return True

    # ---------------------------------------------------------------- account
    def account_info(self, refresh: bool = False) -> dict[str, Any]:
        """账号信息；``refresh`` 为真时回服务器问一次。

        只有真的拿到过「是谁」才算有缓存：登录那一刻写下的 cookie 旁边
        id / name 都是空的，把这份空缓存当答案，界面会一直显示一个没有
        名字没有头像的账号。

        cookie 失效时**不删 cookie**，只把 ``expired`` 标出来：用户可能
        只是断网了一次，删掉就得重新走一遍登录。
        """
        with self._lock:
            cached = dict(self._account)
            has_cookie = bool(self._cookie)
        if not has_cookie:
            return {"logged_in": False, "expired": False, "stale": False}
        if not refresh and cached.get("id"):
            return {**cached, "logged_in": True, "expired": False, "stale": True}
        live = self._client.refresh_self()
        if not live or not live.get("id"):
            # 拿不到身份：可能是 cookie 过期，也可能是网络挂了。两种都只
            # 报「这次没读到」，cookie 留着。
            return {
                **cached,
                "logged_in": bool(cached.get("id")),
                "expired": bool(cached.get("id")) or True,
                "stale": False,
                "error": self._client.last_error or "读不到登录状态。",
            }
        with self._lock:
            self._account = {key: live.get(key) for key in _ACCOUNT_KEYS
                             if live.get(key) not in (None, "")}
            snapshot = dict(self._account)
            cookie = self._cookie
        # 昵称和头像会变，顺手把文件同步一下。
        if cookie:
            self.save(cookie, snapshot)
        return {**snapshot, "logged_in": True, "expired": False, "stale": False}


_INSTANCE: PixivSession | None = None
_INSTANCE_LOCK = threading.Lock()


def get_pixiv_session() -> PixivSession:
    """共享的那个会话。"""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = PixivSession()
    return _INSTANCE
