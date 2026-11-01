"""B站私信的单元测试：假 session，不打真网络，也不碰真实账号。"""
from __future__ import annotations

import json

from src.core.bili_im import BiliIm


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """记录请求，按 URL 给回预置的回答。"""

    def __init__(self, responses: dict | None = None) -> None:
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.raise_on_request = False

    def _reply(self, url: str) -> FakeResponse:
        if self.raise_on_request:
            raise RuntimeError("network is down")
        for suffix, payload in self.responses.items():
            if url.endswith(suffix):
                return FakeResponse(payload)
        return FakeResponse({"code": -404, "message": "not found"})

    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            timeout=None) -> FakeResponse:
        self.gets.append((url, dict(params or {})))
        return self._reply(url)

    def post(self, url: str, data: dict | None = None, headers: dict | None = None,
             timeout=None) -> FakeResponse:
        self.posts.append((url, dict(data or {})))
        return self._reply(url)


class FakeClient:
    """假 B站客户端。"""

    def __init__(self, session: FakeSession, signed_in: bool = True,
                 mid: str = "42") -> None:
        self.session = session
        self.is_logged_in = signed_in
        self._mid = mid

    def csrf(self) -> str:
        return "csrf-token"

    def cookie_dict(self) -> dict:
        return {"SESSDATA": "abc", "bili_jct": "csrf-token",
                "DedeUserID": self._mid, "buvid3": "BUVID3-123"}

    def bili_headers(self) -> dict:
        return {"User-Agent": "UA", "Referer": "https://www.bilibili.com",
                "Cookie": "SESSDATA=abc"}

    def account_info(self) -> dict:
        return {"logged_in": self.is_logged_in, "mid": self._mid, "uname": "某人"}


# --------------------------------------------------------------------- 会话列表
#: 会话列表现在的形状：一个会话一个对象，对方的资料和最后一条消息
#: 都挂在对象里面（``/session_svr/v1/session_svr/get_sessions``）。
CONVERSATIONS_PAYLOAD = {
    "code": 0,
    "message": "0",
    "data": {
        "session_list": [
            {
                "talker_id": 123,
                "session_type": 1,
                "unread_count": 3,
                "account_info": {
                    "name": "小明",
                    "pic_url": "https://example.invalid/ming.jpg",
                },
                "last_msg": {
                    "sender_uid": 123,
                    "msg_type": 1,
                    "msg_key": "k1",
                    "content": json.dumps({"content": "在吗"}),
                    "timestamp": 1700000000,
                },
            },
            {
                "talker_id": 456,
                "session_type": 1,
                "unread_count": 0,
                "account_info": {
                    "name": "小红",
                    "pic_url": "https://example.invalid/hong.jpg",
                },
                "last_msg": {
                    "sender_uid": 456,
                    "msg_type": 1,
                    "msg_key": "k2",
                    "content": json.dumps({"content": "你好"}),
                    "timestamp": 1700000009,
                },
            },
        ],
    },
}


def test_conversations_normalizes_names_faces_and_unread() -> None:
    session = FakeSession({
        "/session_svr/v1/session_svr/get_sessions": CONVERSATIONS_PAYLOAD,
    })
    result = BiliIm(FakeClient(session)).conversations()

    assert result["success"] is True
    assert result["count"] == 2
    first, second = result["items"]
    assert first["session_id"] == "123_1"
    assert first["talker_id"] == "123"
    assert first["name"] == "小明"
    assert first["face"] == "https://example.invalid/ming.jpg"
    assert first["last_message"] == "在吗"
    assert first["unread"] == 3
    assert first["last_time"] == 1700000000
    assert first["msg_type"] == 1
    assert second["name"] == "小红"
    assert second["unread"] == 0
    # 请求参数照接口约定发
    url, params = session.gets[0]
    assert url.endswith("/session_svr/v1/session_svr/get_sessions")
    assert params["session_type"] == 1
    assert params["size"] == 20


def test_conversation_without_friend_entry_falls_back_to_talker_id() -> None:
    payload = {
        "code": 0,
        "data": {
            "session_list": [{
                "talker_id": 777,
                "session_type": 1,
                "unread_count": 1,
                "last_msg": {"msg_type": 1, "content": "hi"},
            }],
        },
    }
    session = FakeSession({"/session_svr/v1/session_svr/get_sessions": payload})
    result = BiliIm(FakeClient(session)).conversations()
    assert result["success"] is True
    assert result["items"][0]["name"] == "777"
    assert result["items"][0]["face"] == ""
    # 不是 JSON 的 content 也要有内容
    assert result["items"][0]["last_message"] == "hi"


# --------------------------------------------------------------------- 消息列表
MESSAGES_PAYLOAD = {
    "code": 0,
    "message": "0",
    "data": {
        "messages": [
            {
                "msg_type": 1,
                "msg_key": "key-1",
                "sender_uid": 42,
                "content": json.dumps({"content": "我发的"}),
                "timestamp": 1700000100,
                "talker_id": 123,
                "session_id": 900,
            },
            {
                "msg_type": 1,
                "msg_key": "key-2",
                "sender_uid": 123,
                "content": json.dumps({"content": "对方发的"}),
                "timestamp": 1700000200,
                "talker_id": 123,
                "session_id": 900,
            },
            {
                "msg_type": 10,
                "msg_key": "key-3",
                "sender_uid": 123,
                "content": "不是 JSON 的内容",
                "timestamp": 1700000300,
                "talker_id": 123,
                "session_id": 900,
            },
        ],
    },
}


def test_messages_parses_json_content_and_marks_own_messages() -> None:
    session = FakeSession({
        "/session_svr/v1/session_svr/fetch_session_msgs": MESSAGES_PAYLOAD})
    result = BiliIm(FakeClient(session)).messages("900", "123")

    assert result["success"] is True
    assert result["count"] == 3
    mine, theirs, raw = result["items"]
    assert mine["content"] == "我发的"
    assert mine["is_self"] is True
    assert mine["sender_id"] == "42"
    assert mine["timestamp"] == 1700000100
    assert mine["msg_type"] == 1
    assert mine["msg_key"] == "key-1"
    assert theirs["content"] == "对方发的"
    assert theirs["is_self"] is False
    # 不是 JSON 的 content 不炸，原文当文字，msg_type 保留
    assert raw["content"] == "不是 JSON 的内容"
    assert raw["msg_type"] == 10
    assert raw["msg_key"] == "key-3"

    url, form = session.posts[0]
    assert url.endswith("/session_svr/v1/session_svr/fetch_session_msgs")
    assert form["session_id"] == "900"
    assert form["talker_id"] == "123"
    assert form["session_type"] == 1
    assert form["csrf"] == "csrf-token"


# --------------------------------------------------------------------- 发消息
def test_send_posts_json_content_and_required_fields() -> None:
    session = FakeSession({
        "/session_svr/v1/session_svr/send_msg": {"code": 0, "message": "0", "data": {}},
    })
    result = BiliIm(FakeClient(session)).send("123", "晚上好")

    assert result["success"] is True
    assert result["message"] == ""
    url, form = session.posts[0]
    assert url.endswith("/session_svr/v1/session_svr/send_msg")
    assert form["talker_id"] == "123"
    assert form["csrf"] == "csrf-token"
    assert form["msg_type"] == 1
    assert form["uid"] == "42"
    assert form["buvid"] == "BUVID3-123"
    assert form["client_key"]
    # content 必须是 JSON 字符串
    assert isinstance(form["content"], str)
    assert json.loads(form["content"]) == {"content": "晚上好"}


def test_send_rejects_empty_text() -> None:
    session = FakeSession()
    result = BiliIm(FakeClient(session)).send("123", "   ")
    assert result["success"] is False
    assert session.posts == []


# --------------------------------------------------------------------- 失败路径
def test_not_logged_in_fails_without_request() -> None:
    session = FakeSession()
    im = BiliIm(FakeClient(session, signed_in=False))
    for result in (im.conversations(), im.messages("900", "123"),
                   im.send("123", "在吗")):
        assert result["success"] is False
        assert "登录" in result["message"]
    assert session.gets == [] and session.posts == []


def test_error_codes_are_translated() -> None:
    session = FakeSession({
        "/session_svr/v1/session_svr/get_sessions": {"code": -101, "message": "账号未登录"},
        "/session_svr/v1/session_svr/fetch_session_msgs": {"code": -111, "message": "csrf 校验失败"},
        "/session_svr/v1/session_svr/send_msg": {"code": -352, "message": "风控校验失败"},
    })
    im = BiliIm(FakeClient(session))
    assert "登录" in im.conversations()["message"]
    assert "登录" in im.messages("900", "123")["message"]
    assert "风控" in im.send("123", "在吗")["message"]


def test_network_exception_returns_failure_instead_of_raising() -> None:
    session = FakeSession()
    session.raise_on_request = True
    im = BiliIm(FakeClient(session))
    for result in (im.conversations(), im.messages("900", "123"),
                   im.send("123", "在吗")):
        assert result["success"] is False
        assert result["message"]
