"""B站私信：会话列表、某个会话的消息、发消息（文字 + 图片）。

私信接口不在 ``api.bilibili.com`` 上，而在 ``api.vc.bilibili.com``：

* ``GET  /session_svr/v1/session_svr/get_sessions``      会话列表
* ``POST /session_svr/v1/session_svr/fetch_session_msgs``  某个会话的消息
* ``POST /session_svr/v1/session_svr/send_msg``            发消息

这三个接口都要登录态（``SESSDATA``），两个写接口还要 ``csrf``（cookie 里的
``bili_jct``），少了会回 ``-111``。``buvid3`` 也要带上，缺了容易被风控。

图片私信是另一套：图片要先上传到 ``customerservice.bilibili.com``，拿到
``image_url`` 之后才能作为一条 ``msg_type=2`` 的消息发出去。

* ``POST /x/custom/msg_svr/v1/upload``  上传图片（multipart）
* 发消息时 ``content`` = ``{"url":…, "width":…, "height":…, …}``

回答里的 ``content`` 是一个 **JSON 字符串**（例如 ``{"content": "你好"}``），
不解析就只会看到一串花括号，所以这里统一解一次；解不出来就把原文当文字，
不丢消息。

所有方法都不抛异常：失败时返回 ``{"success": False, "message": "给用户看的中文"}``。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

__all__ = ["BiliIm", "BILI_IM_BASE", "BILI_IM_UPLOAD_BASE"]

log = logging.getLogger(__name__)

#: 私信接口的宿主。它和 ``api.bilibili.com`` 是两套网关，路由前缀也不同。
BILI_IM_BASE = "https://api.vc.bilibili.com"

#: 私信图片上传的宿主。又是另一套网关：``api.vc.bilibili.com`` 上没有
#: 这个路由（404），网页端的私信应用是把它放在客服域名下的。
BILI_IM_UPLOAD_BASE = "https://customerservice.bilibili.com"

#: 站内私信。
MSG_TYPE_TEXT = 1

#: 图片私信。网页端的私信应用里 ``EN_MSG_TYPE_PIC = 2``。
MSG_TYPE_IMAGE = 2

#: 上传图片时 B 站给的字段名（multipart 的文件部分）。
_UPLOAD_FILE_FIELD = "file"

#: 图片私信 ``content`` 里图片地址的字段名。网页端发的时候写的是 ``url``。
_IMAGE_URL_KEY = "url"

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
    parsed = _parse_content_obj(raw)
    if isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, str):
            return content
        if content is not None:
            return json.dumps(content, ensure_ascii=False)
        # 系统通知（登录提醒一类）没有 ``content``，正文在 ``text`` 里，
        # 标题在 ``title`` 里。只认 content 的话，这类消息会显示成一串 JSON。
        text_body = parsed.get("text")
        if isinstance(text_body, str) and text_body.strip():
            title = parsed.get("title")
            if isinstance(title, str) and title.strip():
                return f"{title.strip()}\n{text_body.strip()}"
            return text_body.strip()
        return str(raw)
    if isinstance(parsed, str):
        return parsed
    return str(raw) if raw is not None else ""


def _parse_content_obj(raw: Any) -> Any:
    """把 ``content`` 解成对象。解不出来返回 ``None``。

    图片私信的 ``content`` 解出来是一个带 ``url`` 的对象，文字私信是一个带
    ``content`` 的对象。两者都要能拿到，所以这一步只负责解 JSON。
    """
    if raw is None:
        return None
    if isinstance(raw, dict | list):
        return raw
    text = str(raw)
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _image_info(parsed: Any) -> dict[str, Any]:
    """从一条消息的 ``content`` 里取出图片信息。

    图片私信的 ``content`` 解出来是 ``{"url": …, "width": …, "height": …,
    "size": …, "original": 1}``。字段名是从 B 站私信应用自己的代码里读出来
    的：发的时候写 ``url``，宽度写 ``width``，高度写 ``height``。

    取不到 ``url`` 就返回空字典——那不是图片消息（或者是另一种卡片），
    界面上就按普通消息处理。
    """
    if not isinstance(parsed, dict):
        return {}
    url = parsed.get(_IMAGE_URL_KEY)
    if not isinstance(url, str) or not url.strip():
        return {}
    return {
        "url": url.strip(),
        "width": _as_int(parsed.get("width")),
        "height": _as_int(parsed.get("height")),
        "size": _as_int(parsed.get("size")),
    }


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
        #: 已经查到的对方资料（mid -> {name, face}），进程内缓存。
        self._cards_cache: dict[str, dict[str, Any]] = {}
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

    def _lock(self) -> Any:
        """The request lock of the client, so both halves of the app share one.

        ``requests.Session`` is not thread safe; the client exposes its lock
        and every module on the same client has to take it around a request.
        A contextlib.nullcontext fallback keeps a fake client in the tests
        working without one.
        """
        import contextlib

        getter = getattr(self._client, "request_lock", None)
        if getter is None:
            return contextlib.nullcontext()
        return getter

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """一次 GET，返回解析后的包封；失败返回 ``None``。"""
        try:
            with self._lock():
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
            with self._lock():
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

    def _cards(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """对方的资料：一次问一批，而不是一个会话问一次。

        ``get_sessions`` 只给「关注过的人」带上 ``account_info``（名字 + 头像）；
        没关注、没聊过的会话在这一项上是空的，界面于是只剩一串 uid。
        ``/account/v1/user/cards?uids=1,2,3`` 一次能把 50 个补齐，网页端的私信
        列表也是这么做的。
        """
        wanted = [item for item in dict.fromkeys(ids) if item and item not in self._cards_cache]
        if not wanted:
            return {item: self._cards_cache[item] for item in ids if item in self._cards_cache}
        for start in range(0, len(wanted), 50):
            batch = wanted[start:start + 50]
            body = self._get("/account/v1/user/cards", {"uids": ",".join(batch)})
            if not body or body.get("code") != 0:
                continue
            for entry in body.get("data") or []:
                if not isinstance(entry, dict):
                    continue
                mid = _as_text(entry.get("mid"))
                if not mid:
                    continue
                self._cards_cache[mid] = {
                    "name": _as_text(entry.get("name")),
                    "face": _as_text(entry.get("face")),
                }
        return {item: self._cards_cache[item] for item in ids if item in self._cards_cache}

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
            last_parsed = _parse_content_obj(last.get("content"))
            last_image = _image_info(last_parsed)
            items.append({
                # 会话由「聊天对象 + 会话类型」确定。
                "session_id": f"{talker_id}_{session_type}",
                "talker_id": talker_id,
                "name": name,
                "face": _as_text(info.get("pic_url")),
                # 最后一条是图片时，列表里给个占位文字，不能显示成一串 JSON。
                "last_message": ("[图片]" if last_image
                                 else _parse_content(last.get("content"))),
                "unread": _as_int(entry.get("unread_count")),
                "last_time": _as_int(last.get("timestamp")),
                "msg_type": _as_int(last.get("msg_type")),
                "image_url": last_image.get("url", ""),
            })

        # 名字还是 uid、或者没有头像的，一次批量补齐。
        missing = [item["talker_id"] for item in items
                   if not item["name"] or item["name"] == item["talker_id"] or not item["face"]]
        if missing:
            cards = self._cards(missing)
            for item in items:
                card = cards.get(item["talker_id"]) or {}
                if card.get("name"):
                    item["name"] = card["name"]
                if card.get("face"):
                    item["face"] = card["face"]

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
        params = {
            "session_id": session_id,
            "talker_id": talker_id,
            "session_type": 1,
            "size": max(1, int(size or 20)),
            "sender_type": 0,
            "build": 0,
            "mobi_app": "web",
            "platform": "web",
        }
        # 网页端读消息走的是 ``svr_sync`` 网关上的 GET；``session_svr`` 那条
        # 只在会话列表上有，读消息是 404（实测 2026-11-01）。GET 不行时再退回
        # 老的 POST 路由，免得哪天两边都换了。
        body = self._get("/svr_sync/v1/svr_sync/fetch_session_msgs", params)
        if body is None:
            legacy = dict(params)
            legacy.pop("sender_type", None)
            legacy["csrf"] = self._csrf()
            body = self._post("/session_svr/v1/session_svr/fetch_session_msgs", legacy)
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
            msg_type = _as_int(entry.get("msg_type"))
            parsed = _parse_content_obj(entry.get("content"))
            image = _image_info(parsed)
            if image:
                # 图片消息没有文字正文，content 里就是图片本身。
                content = ""
            else:
                content = _parse_content(entry.get("content"))
            items.append({
                "sender_id": sender_id,
                "is_self": bool(mid) and sender_id == mid,
                "content": content,
                "timestamp": _as_int(entry.get("timestamp")),
                "msg_type": msg_type,
                "msg_key": _as_text(entry.get("msg_key")),
                # 图片消息带这三项；文字消息是空字典，界面上按文字处理。
                "image_url": image.get("url", ""),
                "image_width": image.get("width", 0),
                "image_height": image.get("height", 0),
                "image_size": image.get("size", 0),
            })
        return {"success": True, "items": items, "count": len(items), "message": ""}

    # -------------------------------------------------------------------- writing
    def _post_multipart(self, path: str, fields: dict[str, Any],
                        file_path: str) -> dict[str, Any] | None:
        """上传一个文件（multipart/form-data）。失败返回 ``None``。

        私信的图片走的是客服域名上的 ``/x/custom/msg_svr/v1/upload``，
        文件部分的字段名是 ``file``，另外还要带 ``filename`` / ``mid`` /
        ``build`` / ``mobi_app`` 四个普通字段。
        """
        path_obj = Path(file_path)
        try:
            handle = path_obj.open("rb")
        except OSError as exc:
            log.warning("bili im upload: cannot open %s: %s", file_path, exc)
            return None
        headers = self._headers()
        headers.pop("Content-Type", None)   # requests 要自己带 multipart 的 boundary
        # 上一次请求的状态码不能留到这一次：网络故障时它会被误读成「路没了」。
        self._last_status = 0
        try:
            with handle, self._lock():
                files = {_UPLOAD_FILE_FIELD: (path_obj.name, handle,
                                              "application/octet-stream")}
                response = self._client.session.post(
                    BILI_IM_UPLOAD_BASE + path, data=fields, files=files,
                    headers=headers, timeout=(10, 120),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("bili im upload failed (%s): %s", path, exc)
            return None
        return self._parse(response, path)

    def upload_image(self, image_path: str) -> dict[str, Any]:
        """上传一张图片，返回它的地址和尺寸。

        图片私信是两步：先上传拿到 ``image_url``，再把它作为一条
        ``msg_type=2`` 的消息发出去。这一步只做前半段。

        失败时返回 ``{"success": False, "message": "给用户看的中文"}``。
        """
        if not self._logged_in():
            return {"success": False, "message": "请先登录 B站账号"}
        path = str(image_path or "").strip()
        if not path:
            return {"success": False, "message": "没有选择图片"}
        if not os.path.isfile(path):
            return {"success": False, "message": "找不到这个图片文件"}
        csrf = self._csrf()
        if not csrf:
            return {"success": False, "message": "缺少 csrf，请重新登录 B站账号"}
        fields = {
            "filename": os.path.basename(path),
            "mid": self._uid(),
            "build": "0",
            "mobi_app": "web",
            "csrf": csrf,
        }
        body = self._post_multipart("/x/custom/msg_svr/v1/upload", fields, path)
        if body is None:
            if self._last_status == 404:
                return {"success": False, "message": "B站现在没有开放私信图片上传接口"}
            return {"success": False, "message": "网络请求失败，图片没有上传成功"}
        code = body.get("code")
        if code != 0:
            return {"success": False,
                    "message": _error_text(code, "图片没有上传成功")}
        data = body.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        url = _as_text(data.get("image_url"))
        if not url:
            return {"success": False, "message": "图片上传成功，但没有拿到图片地址"}
        return {
            "success": True,
            "message": "",
            "url": url,
            "width": _as_int(data.get("image_width")),
            "height": _as_int(data.get("image_height")),
            "size": _as_int(data.get("img_size")),
        }

    def send_image(self, talker_id: str, image_path: str,
                   session_id: str = "") -> dict[str, Any]:
        """发一张图片私信：先上传，再按 ``msg_type=2`` 发出去。

        图片消息的 ``content`` 是一个 JSON 字符串，里面是图片的地址和尺寸。
        """
        if not self._logged_in():
            return {"success": False, "message": "请先登录 B站账号"}
        talker_id = _as_text(talker_id).strip()
        if not talker_id:
            return {"success": False, "message": "对方 id 是空的"}

        uploaded = self.upload_image(image_path)
        if not uploaded.get("success"):
            return {"success": False,
                    "message": str(uploaded.get("message") or "图片没有上传成功")}

        payload = {
            _IMAGE_URL_KEY: str(uploaded.get("url") or ""),
            "width": int(uploaded.get("width") or 0),
            "height": int(uploaded.get("height") or 0),
            "size": int(uploaded.get("size") or 0),
            "original": 1,
        }
        sent = self._send(talker_id, MSG_TYPE_IMAGE, payload, session_id)
        if not sent.get("success"):
            return sent
        return {"success": True, "message": "",
                "image_url": str(uploaded.get("url") or "")}

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
        return self._send(talker_id, MSG_TYPE_TEXT, {"content": text}, session_id)

    def _send(self, talker_id: str, msg_type: int, payload: dict[str, Any],
              session_id: str = "") -> dict[str, Any]:
        """发消息的共同部分：拼表单、POST、把错误码翻成中文。"""
        csrf = self._csrf()
        if not csrf:
            return {"success": False, "message": "缺少 csrf，请重新登录 B站账号"}
        cookies = self._cookies()
        data = {
            "talker_id": talker_id,
            "session_type": 1,
            "msg_type": msg_type,
            "group_id": "",
            "uid": self._uid(),
            "content": json.dumps(payload, ensure_ascii=False),
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
