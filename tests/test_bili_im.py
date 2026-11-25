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
        #: multipart 上传：记下 ``(url, 普通字段, 文件字段名)``。
        self.uploads: list[tuple[str, dict, dict]] = []
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
             timeout=None, files: dict | None = None) -> FakeResponse:
        if files is not None:
            # 上传：文件部分只记字段名，不读文件内容。
            self.uploads.append((url, dict(data or {}),
                                 {name: value[0] for name, value in files.items()}))
            return self._reply(url)
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
        "/svr_sync/v1/svr_sync/fetch_session_msgs": MESSAGES_PAYLOAD})
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

    # 读消息走 svr_sync 网关上的 GET（session_svr 那条是 404）
    url, params = session.gets[0]
    assert url.endswith("/svr_sync/v1/svr_sync/fetch_session_msgs")
    assert params["session_id"] == "900"
    assert params["talker_id"] == "123"
    assert params["session_type"] == 1


def test_messages_fall_back_to_the_legacy_route() -> None:
    """新路由没有回答时，退回老的 POST 路由。"""
    session = FakeSession({
        "/session_svr/v1/session_svr/fetch_session_msgs": MESSAGES_PAYLOAD})
    original = session._reply

    def reply(url: str):
        if url.endswith("/svr_sync/v1/svr_sync/fetch_session_msgs"):
            return FakeResponse({}, status=404)
        return original(url)

    session._reply = reply
    result = BiliIm(FakeClient(session)).messages("900", "123")
    assert result["success"] is True
    assert result["count"] == 3
    assert session.posts, "老路由应该被试一次"
    url, form = session.posts[0]
    assert url.endswith("/session_svr/v1/session_svr/fetch_session_msgs")
    assert form["csrf"] == "csrf-token"


def test_conversations_fill_in_missing_names_and_faces() -> None:
    """会话列表没带对方资料时，用 user/cards 一次问一批补齐。"""
    payload = {
        "code": 0,
        "data": {
            "session_list": [{
                "talker_id": 777,
                "session_type": 1,
                "unread_count": 0,
                "last_msg": {"msg_type": 1, "content": "hi"},
            }],
        },
    }
    session = FakeSession({
        "/session_svr/v1/session_svr/get_sessions": payload,
        "/account/v1/user/cards": {
            "code": 0,
            "data": [{"mid": 777, "name": "路人甲",
                      "face": "https://example.invalid/777.jpg"}],
        },
    })
    result = BiliIm(FakeClient(session)).conversations()
    assert result["success"] is True
    assert result["items"][0]["name"] == "路人甲"
    assert result["items"][0]["face"] == "https://example.invalid/777.jpg"
    urls = [url for url, _ in session.gets]
    assert any(url.endswith("/account/v1/user/cards") for url in urls)


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
        "/svr_sync/v1/svr_sync/fetch_session_msgs": {"code": -111, "message": "csrf 校验失败"},
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


# --------------------------------------------------------------------- 图片私信
#: 图片消息的 content：地址 + 尺寸 + 体积，JSON 字符串。
IMAGE_CONTENT = json.dumps({
    "url": "https://i0.hdslb.com/bfs/im/abc.jpg",
    "width": 800,
    "height": 600,
    "size": 20480,
    "original": 1,
})

IMAGE_MESSAGES_PAYLOAD = {
    "code": 0,
    "message": "0",
    "data": {
        "messages": [
            {
                "msg_type": 2,
                "msg_key": "pic-1",
                "sender_uid": 123,
                "content": IMAGE_CONTENT,
                "timestamp": 1700000400,
                "talker_id": 42,
                "session_id": 900,
            },
            {
                "msg_type": 1,
                "msg_key": "txt-1",
                "sender_uid": 42,
                "content": json.dumps({"content": "收到"}),
                "timestamp": 1700000500,
                "talker_id": 123,
                "session_id": 900,
            },
        ],
    },
}


def test_messages_read_a_picture_message() -> None:
    """图片消息要带出地址和尺寸，正文不显示成 JSON。"""
    session = FakeSession({
        "/svr_sync/v1/svr_sync/fetch_session_msgs": IMAGE_MESSAGES_PAYLOAD})
    result = BiliIm(FakeClient(session)).messages("900", "123")

    assert result["success"] is True
    picture, text = result["items"]
    assert picture["msg_type"] == 2
    assert picture["image_url"] == "https://i0.hdslb.com/bfs/im/abc.jpg"
    assert picture["image_width"] == 800
    assert picture["image_height"] == 600
    assert picture["image_size"] == 20480
    # 图片消息没有文字正文，不能把 JSON 显示给用户
    assert picture["content"] == ""
    # 文字消息不受影响
    assert text["content"] == "收到"
    assert text["image_url"] == ""


def test_conversations_show_a_placeholder_for_a_picture() -> None:
    """会话列表里最后一条是图片时，显示 [图片] 而不是一串 JSON。"""
    payload = {
        "code": 0,
        "data": {
            "session_list": [{
                "talker_id": 123,
                "session_type": 1,
                "unread_count": 0,
                "account_info": {"name": "小明"},
                "last_msg": {
                    "sender_uid": 123,
                    "msg_type": 2,
                    "msg_key": "pic-1",
                    "content": IMAGE_CONTENT,
                    "timestamp": 1700000400,
                },
            }],
        },
    }
    session = FakeSession({"/session_svr/v1/session_svr/get_sessions": payload})
    result = BiliIm(FakeClient(session)).conversations()
    assert result["success"] is True
    item = result["items"][0]
    assert item["last_message"] == "[图片]"
    assert item["msg_type"] == 2
    assert item["image_url"] == "https://i0.hdslb.com/bfs/im/abc.jpg"


def test_upload_image_posts_multipart_and_returns_the_url(tmp_path) -> None:
    """上传走客服域名上的 multipart 接口，字段名是 file。"""
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n not really a png")
    session = FakeSession({
        "/x/custom/msg_svr/v1/upload": {
            "code": 0,
            "message": "0",
            "data": {
                "image_url": "https://i0.hdslb.com/bfs/im/up.jpg",
                "image_width": 1024,
                "image_height": 768,
                "img_size": 40960,
            },
        },
    })
    result = BiliIm(FakeClient(session)).upload_image(str(picture))

    assert result["success"] is True
    assert result["url"] == "https://i0.hdslb.com/bfs/im/up.jpg"
    assert result["width"] == 1024
    assert result["height"] == 768
    assert result["size"] == 40960

    url, fields, files = session.uploads[0]
    assert url == ("https://customerservice.bilibili.com"
                   "/x/custom/msg_svr/v1/upload")
    assert "file" in files
    assert files["file"] == "shot.png"
    assert fields["filename"] == "shot.png"
    assert fields["mid"] == "42"
    assert fields["csrf"] == "csrf-token"


def test_send_image_uploads_first_then_sends_a_picture_message(tmp_path) -> None:
    """发图片是两步：先上传，再以 msg_type=2 发出去。"""
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = FakeSession({
        "/x/custom/msg_svr/v1/upload": {
            "code": 0,
            "data": {
                "image_url": "https://i0.hdslb.com/bfs/im/up.jpg",
                "image_width": 1024,
                "image_height": 768,
                "img_size": 40960,
            },
        },
        "/session_svr/v1/session_svr/send_msg": {"code": 0, "data": {}},
    })
    result = BiliIm(FakeClient(session)).send_image("123", str(picture))

    assert result["success"] is True
    assert result["image_url"] == "https://i0.hdslb.com/bfs/im/up.jpg"
    # 上传在前，发消息在后
    assert len(session.uploads) == 1
    assert len(session.posts) == 1

    url, form = session.posts[0]
    assert url.endswith("/session_svr/v1/session_svr/send_msg")
    assert form["talker_id"] == "123"
    assert form["msg_type"] == 2
    content = json.loads(form["content"])
    assert content["url"] == "https://i0.hdslb.com/bfs/im/up.jpg"
    assert content["width"] == 1024
    assert content["height"] == 768
    assert content["size"] == 40960
    assert content["original"] == 1


def test_send_image_does_not_send_when_the_upload_fails(tmp_path) -> None:
    """上传失败就不能再发消息，否则对方收到一条空图片。"""
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = FakeSession({
        "/x/custom/msg_svr/v1/upload": {"code": -400, "message": "文件不合法"},
        "/session_svr/v1/session_svr/send_msg": {"code": 0, "data": {}},
    })
    result = BiliIm(FakeClient(session)).send_image("123", str(picture))

    assert result["success"] is False
    assert result["message"]
    assert session.posts == [], "上传失败之后不应该再发消息"


def test_send_image_reports_a_failed_send(tmp_path) -> None:
    """上传成功但发送失败时，必须报失败，不能当成发出去了。"""
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = FakeSession({
        "/x/custom/msg_svr/v1/upload": {
            "code": 0,
            "data": {"image_url": "https://i0.hdslb.com/bfs/im/up.jpg",
                     "image_width": 10, "image_height": 10, "img_size": 10},
        },
        "/session_svr/v1/session_svr/send_msg": {"code": -352, "message": "风控"},
    })
    result = BiliIm(FakeClient(session)).send_image("123", str(picture))
    assert result["success"] is False
    assert result["message"]


def test_upload_image_rejects_a_missing_file() -> None:
    session = FakeSession()
    result = BiliIm(FakeClient(session)).upload_image("/tmp/没有这个文件.png")
    assert result["success"] is False
    assert "找不到" in result["message"]
    assert session.uploads == []


def test_upload_image_needs_a_login() -> None:
    session = FakeSession()
    im = BiliIm(FakeClient(session, signed_in=False))
    result = im.upload_image("/tmp/whatever.png")
    assert result["success"] is False
    assert "登录" in result["message"]
    assert session.uploads == []


def test_picture_message_without_a_url_is_not_an_image() -> None:
    """content 里有 url 才算图片；别的卡片消息按普通消息处理。"""
    payload = {
        "code": 0,
        "data": {
            "messages": [{
                "msg_type": 2,
                "sender_uid": 123,
                "content": json.dumps({"title": "一个卡片", "text": "正文"}),
                "timestamp": 1700000400,
            }],
        },
    }
    session = FakeSession({
        "/svr_sync/v1/svr_sync/fetch_session_msgs": payload})
    result = BiliIm(FakeClient(session)).messages("900", "123")
    item = result["items"][0]
    assert item["image_url"] == ""
    # 卡片消息的正文照旧给出来
    assert "正文" in item["content"]
