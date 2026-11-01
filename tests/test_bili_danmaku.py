"""B站弹幕模块的测试（不联网，传输层是假的）。

自己拼 protobuf 字节来验证解析器，免得测试依赖线上返回的二进制。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.bili_danmaku import (  # noqa: E402
    BiliDanmaku,
    _parse_dm_elems,
    _read_varint,
    mask_allows,
)

# ---------------------------------------------------------------------------
# 手工拼 protobuf
# ---------------------------------------------------------------------------


def enc_varint(value: int) -> bytes:
    """把一个整数编成 varint（低位在前）。"""
    out = bytearray()
    value = int(value)
    if value < 0:  # 负数按 64 位补码
        value += 1 << 64
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    """field ``number``，wire type 0（varint）。"""
    return enc_varint((number << 3) | 0) + enc_varint(value)


def field_bytes(number: int, payload: bytes) -> bytes:
    """field ``number``，wire type 2（length-delimited）。"""
    return enc_varint((number << 3) | 2) + enc_varint(len(payload)) + payload


def field_fixed64(number: int) -> bytes:
    """field ``number``，wire type 1（64 位定长）——解析器要能跳过它。"""
    return enc_varint((number << 3) | 1) + b"\x00" * 8


def dm_elem(id_: int = 1, progress: int = 0, mode: int = 1, fontsize: int = 25,
            color: int = 16777215, mid_hash: str = "", content: str = "",
            ctime: int = 0, pool: int = 0, action: str = "",
            id_str: str = "") -> bytes:
    """拼一条 ``DmElem``。"""
    body = b""
    body += field_varint(1, id_)
    body += field_varint(2, progress)
    body += field_varint(3, mode)
    body += field_varint(4, fontsize)
    body += field_varint(5, color)
    if mid_hash:
        body += field_bytes(6, mid_hash.encode("utf-8"))
    body += field_bytes(7, content.encode("utf-8"))
    body += field_varint(8, ctime)
    if action:
        body += field_bytes(10, action.encode("utf-8"))
    body += field_varint(11, pool)
    if id_str:
        body += field_bytes(12, id_str.encode("utf-8"))
    return body


def dm_message(*elems: bytes, extra: bytes = b"") -> bytes:
    """顶层消息：repeated DmElem elems = 1。

    ``extra`` 是顶层追加的原始字节，用来放解析器不认识的字段。
    """
    return b"".join(field_bytes(1, elem) for elem in elems) + extra


# ---------------------------------------------------------------------------
# 假的传输层
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload=None, content=b"", status_code=200, headers=None):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.headers = dict(headers or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """按 URL 片段回答请求；片段对应的值也可以是一个异常。

    路由的值如果是列表，就按顺序每次取一个（用完以后重复最后一个），
    这样分段请求能给出不同的回答。
    """

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default
        self.calls: list[dict] = []

    def _route(self, url):
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, list):
                    return payload.pop(0) if len(payload) > 1 else payload[0]
                return payload
        if isinstance(self.default, Exception):
            raise self.default
        if self.default is not None:
            return self.default
        return FakeResponse({"code": -404, "message": "no route"}, status_code=404)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": headers, "method": "GET"})
        return self._route(url)

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(data or {}),
                           "headers": headers, "method": "POST"})
        return self._route(url)


class FakeClient:
    """只实现弹幕模块用到的那几个方法。"""

    def __init__(self, session, cookie=None, video=None):
        self.session = session
        self._cookie = dict(cookie or {})
        self._video = dict(video or {})
        self._buvid_calls = 0

    def _headers(self):
        return {"User-Agent": "test", "Referer": "https://www.bilibili.com"}

    def cookie_dict(self):
        return dict(self._cookie)

    def cookie_header(self):
        return "; ".join(f"{name}={value}" for name, value in self._cookie.items())

    def csrf(self):
        return str(self._cookie.get("bili_jct") or "")

    def _ensure_buvid(self):
        self._buvid_calls += 1
        self._cookie.setdefault("buvid3", "B3-TEST")

    def video(self, bvid):
        return dict(self._video)


# ---------------------------------------------------------------------------
# varint 与 protobuf 解析
# ---------------------------------------------------------------------------


def test_read_varint_handles_multi_byte_values():
    assert _read_varint(b"\x01", 0) == (1, 1)
    assert _read_varint(b"\xac\x02", 0) == (300, 2)
    value, pos = _read_varint(b"\xff\xff\xff\xff\x0f", 0)
    assert value == 0xFFFFFFFF
    assert pos == 5


def test_read_varint_rejects_truncated_buffer():
    with pytest.raises(ValueError):
        _read_varint(b"\x80", 0)


def test_parse_dm_elems_reads_every_field():
    raw = dm_message(
        dm_elem(id_=12345, progress=4560, mode=4, fontsize=18, color=16711680,
                mid_hash="abc", content="前排", ctime=1700000000, pool=0,
                action="", id_str="12345"),
    )
    elems = _parse_dm_elems(raw)
    assert len(elems) == 1
    elem = elems[0]
    assert elem["id"] == 12345
    assert elem["progress"] == 4560
    assert elem["content"] == "前排"
    assert elem["color"] == 16711680
    assert elem["mode"] == 4
    assert elem["fontsize"] == 18
    assert elem["midHash"] == "abc"
    assert elem["ctime"] == 1700000000


def test_parse_dm_elems_reads_several_elems_and_skips_unknown_fields():
    raw = dm_message(
        dm_elem(id_=1, content="第一条"),
        dm_elem(id_=2, content="第二条"),
        extra=field_fixed64(9),  # 顶层未知字段，解析器要能跳过
    )
    elems = _parse_dm_elems(raw)
    assert [elem["content"] for elem in elems] == ["第一条", "第二条"]
    assert [elem["id"] for elem in elems] == [1, 2]


def test_parse_dm_elems_survives_garbage():
    assert _parse_dm_elems(b"\xff\xff\xff") == []


# ---------------------------------------------------------------------------
# danmaku()
# ---------------------------------------------------------------------------


def test_danmaku_requests_every_segment_and_merges_them():
    seg0 = FakeResponse(content=dm_message(dm_elem(id_=1, progress=1000,
                                                    content="第一段")))
    seg1 = FakeResponse(content=dm_message(dm_elem(id_=2, progress=2000,
                                                    content="第二段")))
    view = FakeResponse({"code": 0, "data": {"segs": 2,
                                             "dm_mask": {"area": {"all": False}}}})
    session = FakeSession({"/x/v2/dm/web/seg.so": [seg0, seg1],
                           "/x/v2/dm/web/view": view})
    client = FakeClient(session, video={"cid": 999})
    dm = BiliDanmaku(client)

    payload = dm.danmaku("BV1xx411c7mD")

    assert payload["success"] is True
    assert payload["count"] == 2
    assert [item["text"] for item in payload["items"]] == ["第一段", "第二段"]
    assert payload["items"][0]["time"] == 1.0
    assert payload["mask"] == {"area": {"all": False}}

    seg_calls = [call for call in session.calls if "seg.so" in call["url"]]
    assert [call["params"]["segment_index"] for call in seg_calls] == [0, 1]
    assert all(call["params"]["oid"] == 999 for call in seg_calls)
    assert all(call["params"]["type"] == 1 for call in seg_calls)


def test_danmaku_uses_the_given_cid_and_segment_count():
    seg = FakeResponse(content=dm_message(dm_elem(id_=1, content="只有一段")))
    session = FakeSession({"/x/v2/dm/web/seg.so": seg})
    client = FakeClient(session)
    dm = BiliDanmaku(client)

    payload = dm.danmaku("BV1xx411c7mD", cid=555, segments=1)

    assert payload["success"] is True
    assert payload["count"] == 1
    assert not [call for call in session.calls if "dm/web/view" in call["url"]]
    assert session.calls[0]["params"]["oid"] == 555


def test_danmaku_carries_the_user_mask_even_without_danmaku():
    view = FakeResponse({"code": 0, "data": {"segs": 1, "dm_mask": {"color": [1]}}})
    empty = FakeResponse(content=b"")
    session = FakeSession({"/x/v2/dm/web/seg.so": empty, "/x/v2/dm/web/view": view})
    client = FakeClient(session, video={"cid": 7})
    dm = BiliDanmaku(client)

    payload = dm.danmaku("BV1xx411c7mD")

    assert payload["success"] is True
    assert payload["items"] == []
    assert payload["mask"] == {"color": [1]}


def test_danmaku_reports_failure_without_a_cid():
    session = FakeSession()
    dm = BiliDanmaku(FakeClient(session, video={}))
    payload = dm.danmaku("BV1xx411c7mD")
    assert payload["success"] is False
    assert payload["items"] == []


def test_danmaku_swallows_transport_errors():
    session = FakeSession(default=RuntimeError("network down"))
    dm = BiliDanmaku(FakeClient(session, video={"cid": 3}))
    payload = dm.danmaku("BV1xx411c7mD")
    assert payload["success"] is False
    assert payload["count"] == 0


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


def test_send_posts_the_expected_form():
    session = FakeSession({"/x/v2/dm/post": FakeResponse({"code": 0, "message": "",
                                                         "data": {"dmid": "77"}})})
    client = FakeClient(session, cookie={"bili_jct": "CSRF", "buvid3": "B3-X",
                                         "SESSDATA": "S"})
    dm = BiliDanmaku(client)

    result = dm.send("BV1xx411c7mD", "你好", cid=999)

    assert result["success"] is True
    assert result["dm_id"] == "77"
    post = [call for call in session.calls if call["method"] == "POST"][0]
    form = post["params"]
    assert form["message"] == "你好"
    assert form["csrf"] == "CSRF"
    assert form["oid"] == 999
    assert form["bvid"] == "BV1xx411c7mD"
    assert form["buvid3"] == "B3-X"
    assert form["type"] == 1
    assert form["pool"] == 0
    assert "ct" in form and "rnd" in form


def test_send_prefixes_the_at_mark():
    session = FakeSession({"/x/v2/dm/post": FakeResponse({"code": 0, "data": {}})})
    dm = BiliDanmaku(FakeClient(session, cookie={"bili_jct": "CSRF"}))
    dm.send("BV1xx411c7mD", "你好", cid=1, at="123,小明")
    form = [call for call in session.calls if call["method"] == "POST"][0]["params"]
    assert form["message"].startswith("at=123,小明")
    assert "你好" in form["message"]


def test_send_fetches_the_buvid_when_the_cookie_has_none():
    session = FakeSession({"/x/v2/dm/post": FakeResponse({"code": 0, "data": {}})})
    client = FakeClient(session, cookie={"bili_jct": "CSRF"})
    dm = BiliDanmaku(client)
    dm.send("BV1xx411c7mD", "你好", cid=1)
    assert client._buvid_calls == 1
    form = [call for call in session.calls if call["method"] == "POST"][0]["params"]
    assert form["buvid3"] == "B3-TEST"


def test_send_returns_failure_instead_of_raising():
    session = FakeSession({"/x/v2/dm/post": FakeResponse(
        {"code": -352, "message": "risk control"})})
    dm = BiliDanmaku(FakeClient(session, cookie={"bili_jct": "CSRF"}))
    result = dm.send("BV1xx411c7mD", "你好", cid=1)
    assert result["success"] is False
    assert "风控" in result["message"]


def test_send_does_not_retry_after_a_risk_control_answer():
    session = FakeSession({"/x/v2/dm/post": FakeResponse(
        {"code": -401, "message": "账号未登录"})})
    dm = BiliDanmaku(FakeClient(session, cookie={"bili_jct": "CSRF"}))
    dm.send("BV1xx411c7mD", "你好", cid=1)
    posts = [call for call in session.calls if call["method"] == "POST"]
    assert len(posts) == 1


def test_send_swallows_transport_errors():
    session = FakeSession(default=RuntimeError("network down"))
    dm = BiliDanmaku(FakeClient(session, cookie={"bili_jct": "CSRF"}))
    result = dm.send("BV1xx411c7mD", "你好", cid=1)
    assert result["success"] is False
    assert result["dm_id"] == ""


def test_send_rejects_empty_input():
    dm = BiliDanmaku(FakeClient(FakeSession()))
    assert dm.send("", "你好", cid=1)["success"] is False
    assert dm.send("BV1xx411c7mD", "  ", cid=1)["success"] is False


# ---------------------------------------------------------------------------
# filter_items()
# ---------------------------------------------------------------------------


ITEMS = [
    {"id": "1", "time": 1.0, "mode": 1, "size": 25, "color": 16777215,
     "text": "第一", "uid_hash": "", "time_stamp": 0, "pool": 0, "action": ""},
    {"id": "2", "time": 5.0, "mode": 4, "size": 18, "color": 16711680,
     "text": "Good Song", "uid_hash": "", "time_stamp": 0, "pool": 0, "action": ""},
    {"id": "3", "time": 9.0, "mode": 5, "size": 25, "color": 255,
     "text": "第三", "uid_hash": "", "time_stamp": 0, "pool": 0, "action": ""},
]


def test_filter_by_modes():
    result = BiliDanmaku.filter_items(ITEMS, modes=[4, 5])
    assert [item["id"] for item in result] == ["2", "3"]


def test_filter_by_keywords_is_case_insensitive():
    result = BiliDanmaku.filter_items(ITEMS, keywords=["good"])
    assert [item["id"] for item in result] == ["2"]


def test_filter_by_time_range():
    assert [item["id"] for item in BiliDanmaku.filter_items(ITEMS, min_time=5.0)] \
        == ["2", "3"]
    assert [item["id"] for item in BiliDanmaku.filter_items(ITEMS, max_time=5.0)] \
        == ["1", "2"]


def test_filter_by_color():
    result = BiliDanmaku.filter_items(ITEMS, colors=[16711680, 255])
    assert [item["id"] for item in result] == ["2", "3"]


def test_filter_keeps_the_original_order_and_handles_no_condition():
    assert BiliDanmaku.filter_items(ITEMS) == ITEMS
    assert BiliDanmaku.filter_items([], modes=[1]) == []


def test_filter_combines_conditions():
    result = BiliDanmaku.filter_items(ITEMS, modes=[4, 5], max_time=6.0)
    assert [item["id"] for item in result] == ["2"]


# ---------------------------------------------------------------------------
# mask_allows()
# ---------------------------------------------------------------------------


def test_mask_allows_keeps_everything_without_a_rule():
    assert mask_allows({}, ITEMS[0]) is True
    assert mask_allows({"area": {"all": False}}, ITEMS[0]) is True


def test_mask_allows_hides_a_blocked_color():
    assert mask_allows({"color": [16711680]}, ITEMS[1]) is False
    assert mask_allows({"color": [16711680]}, ITEMS[0]) is True


def test_mask_allows_hides_a_blocked_mode():
    assert mask_allows({"mode": [4]}, ITEMS[1]) is False
    assert mask_allows({"mode": [4]}, ITEMS[2]) is True


# ---------------------------------------------------------------------------
# visible()
# ---------------------------------------------------------------------------


def test_visible_reads_and_filters():
    seg = FakeResponse(content=dm_message(
        dm_elem(id_=1, progress=1000, mode=1, content="第一"),
        dm_elem(id_=2, progress=5000, mode=4, content="第二"),
    ))
    session = FakeSession({"/x/v2/dm/web/seg.so": seg})
    dm = BiliDanmaku(FakeClient(session, video={"cid": 42}))
    items = dm.visible("BV1xx411c7mD", modes=[4])
    assert [item["text"] for item in items] == ["第二"]


def test_visible_returns_an_empty_list_on_failure():
    session = FakeSession(default=RuntimeError("network down"))
    dm = BiliDanmaku(FakeClient(session, video={"cid": 42}))
    assert dm.visible("BV1xx411c7mD") == []
