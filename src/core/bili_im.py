"""B站私信：会话列表、某个会话的消息、发消息。

私信接口不在 ``api.bilibili.com`` 上，而在 ``api.vc.bilibili.com``：

* ``GET  /session_svr/v1/session_svr/get_sessions``      会话列表
* ``POST /session_svr/v1/session_svr/fetch_session_msgs``  某个会话的消息
* ``POST /session_svr/v1/session_svr/send_msg``            发消息

这三个接口都要登录态（``SESSDATA``），两个写接口还要 ``csrf``（cookie 里的
``bili_jct``），少了会回 ``-111``。``buvid3`` 也要带上，缺了容易被风控。

回答里的 ``content`` 是一个 **JSON 字符串**（例如 ``{"content": "你好"}``），
不解析就只会看到一串花括号，所以这里统一解一次；解不出来就把原文当文字，
不丢消息。

所有方法都不抛异常：失败时返回 ``{"success": False, "message": "给用户看的中文"}``。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

__all__ = ["BiliIm", "BILI_IM_BASE"]

log = logging.getLogger(__name__)

#: 私信接口的宿主。它和 ``api.bilibili.com`` 是两套网关，路由前缀也不同。
BILI_IM_BASE = "https://api.vc.bilibili.com"

#: 站内私信。
MSG_TYPE_TEXT = 1

#: 服务端错误码 -> 给用户看的中文短句。
_ERROR_TEXT: dict[int, str] = {
    -101: "账号未登录，请先登录 B站账号",
    -111: "登录状态已失效，请重新登录 B站账号",
    -352: "被 B站风控拦截了，请稍后再试",
    -403: "没有权限访问私信接口",
    -400: "请求参数有误",
}


def _error_text(code: Any, fallback: str = "") -> str:
    """把服务端的 ``code`` 翻成一句中文。"""
    try:
        number = int(code)
    except (TypeError, ValueError):
        return fallback or "请求失败"
    return _ERROR_TEXT.get(number, fallback or "请求失败")


def _parse_content(raw: Any) -> str:
    """把 ``content`` 字段里的 JSON 字符串解成文字。

    解不出来就用原文兜底：宁可给用户看一串花括号，也不要把一条消息弄丢。
    """
    if raw is None:
        return ""
    if isinstance(raw, dict | list):
        return json.dumps(raw, ensure_ascii=False)
    text = str(raw)
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, str):
            return content
        if content is not None:
            return json.dumps(content, ensure_ascii=False)
        return text
    if isinstance(parsed, str):
        return parsed
    return text


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


class BiliIm:
    """Bilibili 私信的读写访问。

    ``client`` 是一个 :class:`src.core.bilibili.BiliClient`；这里只用到它的
    ``session``、``csrf()``、``cookie_dict()``、``is_logged_in`` 和
    ``account_info()``。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._self_mid: str | None = None
        #: 上一次请求的状态码，用来区分「路没了」和「网络不通」。
        self._last_status = 0

    # ------------------------------------------------------------------ transport
    def _headers(self) -> dict[str, str]:
        """请求头。Cookie 一定要带上，否则服务端认为没有登录。"""
        client = self._client
        headers: dict[str, str] = {}
        getter = getattr(client, "bili_headers", None)
        if callable(getter):
            try:
                headers.update(getter() or {})
                return headers
            except Exception:  # noqa: BLE001 - 客户端理论上不抛，但别赌
                pass
        try:
            headers.update(client._headers() or {})  # noqa: SLF001 - 同包内可用
        except Exception:  # noqa: BLE001
            pass
        return headers

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """一次 GET，返回解析后的包封；失败返回 ``None``。"""
        try:
            response = self._client.session.get(
                BILI_IM_BASE + path, params=params, headers=self._headers(),
                timeout=(10, 30),
            )
        except Exception as exc:  # noqa: BLE001 - 上层不能看到异常
            log.warning("bili im request failed (%s): %s", path, exc)
            return None
        return self._parse(response, path)

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """一次表单 POST，返回解析后的包封；失败返回 ``None``。"""
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = self._client.session.post(
                BILI_IM_BASE + path, data=data, headers=headers, timeout=(10, 30),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("bili im write failed (%s): %s", path, exc)
            return None
        return self._parse(response, path)

    def _parse(self, response: Any, path: str) -> dict[str, Any] | None:
        status = getattr(response, "status_code", 200)
        # 记下来：404 说明这条路已经没有了，要跟网络故障分开说。
        self._last_status = status
        if status != 200:
            log.warning("bili im request %s answered HTTP %s", path, status)
            return None
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("bili im response %s is not JSON: %s", path, exc)
            return None
        return body if isinstance(body, dict) else None

    # -------------------------------------------------------------------- helpers
    def _logged_in(self) -> bool:
        try:
            return bool(getattr(self._client, "is_logged_in", False))
        except Exception:  # noqa: BLE001
            return False

    def _csrf(self) -> str:
        try:
            return str(self._client.csrf() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _cookies(self) -> dict[str, str]:
        try:
            return dict(self._client.cookie_dict() or {})
        except Exception:  # noqa: BLE001
            return {}

    def _mid(self) -> str:
        """当前登录账号的 mid；用来判断哪条消息是自己发的。"""
        if self._self_mid is not None:
            return self._self_mid
        mid = ""
        try:
            info = self._client.account_info() or {}
            if isinstance(info, dict):
                mid = _as_text(info.get("mid"))
        except Exception:  # noqa: BLE001
            mid = ""
        self._self_mid = mid
        return mid

    def _uid(self) -> str:
        """发消息时用的 ``uid``：cookie 里的 ``DedeUserID``，退回 mid。"""
        cookies = self._cookies()
        uid = _as_text(cookies.get("DedeUserID"))
        return uid or self._mid()

    # ------------------------------------------------------------------- reading
    def conversations(self, size: int = 20) -> dict[str, Any]:
        """会话列表。

        路由是 ``/session_svr/v1/session_svr/get_sessions``——注意中间那两段
        ``session_svr`` 是同一个词两遍，早先的 ``/session_svr/v1/session/...``
        整个前缀已经不存在了（回 404）。

        回答里 ``data.session_list`` 是一个会话一个对象：聊天对象、未读数、
        对方的资料（``account_info``）和最后一条消息（``last_msg``）。
        """
        if not self._logged_in():
            return {"success": False, "items": [], "count": 0,
                    "message": "请先登录 B站账号"}
        params = {
            "session_type": 1,
            "group_fold": 1,
            "unfollow_fold": 1,
            "up_fold": 1,
            "quote_fold": 1,
            "folder_id": 0,
            "size": max(1, int(size or 20)),
            "build": 0,
            "mobi_app": "web",
            "platform": "web",
        }
        body = self._get("/session_svr/v1/session_svr/get_sessions", params)
        if body is None:
            return {"success": False, "items": [], "count": 0,
                    "message": "网络请求失败，没有拿到私信会话列表"}
        code = body.get("code")
        if code != 0:
            return {"success": False, "items": [], "count": 0,
                    "message": _error_text(code, "拿私信会话列表失败")}
        data = body.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        items: list[dict[str, Any]] = []
        for entry in data.get("session_list") or []:
            if not isinstance(entry, dict):
                continue
            talker_id = _as_text(entry.get("talker_id"))
            session_type = _as_int(entry.get("session_type")) or 1
            info = entry.get("account_info")
            info = info if isinstance(info, dict) else {}
            last = entry.get("last_msg")
            last = last if isinstance(last, dict) else {}
            name = _as_text(info.get("name")) or talker_id
            items.append({
                # 会话由「聊天对象 + 会话类型」确定。
                "session_id": f"{talker_id}_{session_type}",
                "talker_id": talker_id,
                "name": name,
                "face": _as_text(info.get("pic_url")),
                "last_message": _parse_content(last.get("content")),
                "unread": _as_int(entry.get("unread_count")),
                "last_time": _as_int(last.get("timestamp")),
                "msg_type": _as_int(last.get("msg_type")),
            })

        return {"success": True, "items": items, "count": len(items), "message": ""}

    def messages(self, session_id: str, talker_id: str,
                 size: int = 20) -> dict[str, Any]:
        """某个会话里的消息。

        ``session_id`` 和 ``talker_id`` 两个都要给：服务端拿 ``session_id``
        找会话，拿 ``talker_id`` 认人。
        """
        if not self._logged_in():
            return {"success": False, "items": [], "count": 0,
                    "message": "请先登录 B站账号"}
        session_id = _as_text(session_id).strip()
        talker_id = _as_text(talker_id).strip()
        if not session_id:
            return {"success": False, "items": [], "count": 0,
                    "message": "会话 id 是空的"}
        data = {
            "session_id": session_id,
            "talker_id": talker_id,
            "session_type": 1,
            "size": max(1, int(size or 20)),
            "build": 0,
            "mobi_app": "web",
            "platform": "web",
            "csrf": self._csrf(),
        }
        body = self._post("/session_svr/v1/session_svr/fetch_session_msgs", data)
        if body is None:
            if self._last_status == 404:
                return {"success": False, "items": [], "count": 0,
                        "message": "B站现在没有开放消息记录接口，会话列表里能看到最后一条消息"}
            return {"success": False, "items": [], "count": 0,
                    "message": "网络请求失败，没有拿到消息"}
        code = body.get("code")
        if code != 0:
            return {"success": False, "items": [], "count": 0,
                    "message": _error_text(code, "拿消息失败")}
        payload = body.get("data") or {}
        if not isinstance(payload, dict):
            payload = {}
        mid = self._mid()
        items: list[dict[str, Any]] = []
        for entry in payload.get("messages") or []:
            if not isinstance(entry, dict):
                continue
            sender_id = _as_text(entry.get("sender_uid"))
            items.append({
                "sender_id": sender_id,
                "is_self": bool(mid) and sender_id == mid,
                "content": _parse_content(entry.get("content")),
                "timestamp": _as_int(entry.get("timestamp")),
                "msg_type": _as_int(entry.get("msg_type")),
                "msg_key": _as_text(entry.get("msg_key")),
            })
        return {"success": True, "items": items, "count": len(items), "message": ""}

    # -------------------------------------------------------------------- writing
    def send(self, talker_id: str, text: str,
             session_id: str = "") -> dict[str, Any]:
        """发一条站内私信。

        ``content`` 必须是 JSON 字符串（``{"content": "文字"}``），直接传裸文字
        会被服务端当成格式错误。``client_key`` 是一次性 id，用来防重放。
        """
        if not self._logged_in():
            return {"success": False, "message": "请先登录 B站账号"}
        talker_id = _as_text(talker_id).strip()
        text = _as_text(text).strip()
        if not talker_id:
            return {"success": False, "message": "对方 id 是空的"}
        if not text:
            return {"success": False, "message": "消息内容是空的"}
        csrf = self._csrf()
        if not csrf:
            return {"success": False, "message": "缺少 csrf，请重新登录 B站账号"}
        cookies = self._cookies()
        data = {
            "talker_id": talker_id,
            "session_type": 1,
            "msg_type": MSG_TYPE_TEXT,
            "group_id": "",
            "uid": self._uid(),
            "content": json.dumps({"content": text}, ensure_ascii=False),
            "client_key": uuid.uuid4().hex,
            "buvid": _as_text(cookies.get("buvid3")),
            "csrf": csrf,
            "mobi_app": "web",
            "platform": "web",
        }
        if session_id:
            data["session_id"] = _as_text(session_id)
        body = self._post("/session_svr/v1/session_svr/send_msg", data)
        if body is None:
            if self._last_status == 404:
                return {"success": False,
                        "message": "B站现在没有开放发私信的接口"}
            return {"success": False, "message": "网络请求失败，消息没有发出去"}
        code = body.get("code")
        if code != 0:
            return {"success": False,
                    "message": _error_text(code, "消息没有发出去")}
        return {"success": True, "message": ""}
