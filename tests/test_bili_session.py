"""B站会话与播放地址的单元测试：假 session，不打真网络。"""
from __future__ import annotations

from src.auth.bili_login import BiliSession
from src.core.bilibili import BiliClient, _ordered_bili_urls


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """按路由给回预置的回答。"""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = responses or {}
        self.cookies: dict[str, str] = {}

    def get(self, url: str, params=None, headers=None, timeout=None):
        for suffix, payload in self.responses.items():
            if url.endswith(suffix):
                return FakeResponse(payload)
        return FakeResponse({"code": -404, "message": "not found"})

    def post(self, url: str, data=None, headers=None, timeout=None):
        return self.get(url)

    def clear(self) -> None:
        self.cookies.clear()


class FakeClient:
    """只给 ``BiliSession`` 用得到的那几样。"""

    def __init__(self, responses: dict | None = None) -> None:
        self.session = FakeSession(responses)
        self.cookie = ""

    def set_cookie(self, cookie: str) -> None:
        self.cookie = cookie

    def account_info(self) -> dict:
        for suffix, payload in self.session.responses.items():
            if suffix.endswith("/x/web-interface/nav"):
                data = payload.get("data") or {}
                return {
                    "logged_in": bool(data.get("isLogin")),
                    "mid": str(data.get("mid") or ""),
                    "uname": str(data.get("uname") or ""),
                    "face": str(data.get("face") or ""),
                }
        return {"logged_in": False, "mid": "", "uname": "", "face": ""}


# --------------------------------------------------------------------- 账号缓存
def test_an_empty_cache_is_not_an_account(isolated_home, tmp_path):
    """登录那一刻存下的 cookie 旁边没有名字和头像，不能当成账号回答。"""
    client = FakeClient({
        "/x/web-interface/nav": {
            "code": 0,
            "data": {"isLogin": True, "mid": 42, "uname": "某人",
                     "face": "https://example.invalid/face.jpg"},
        },
    })
    session = BiliSession(client, path=tmp_path / "bilibili.json")
    session._cookie = "SESSDATA=abc"
    session._account = {"mid": "", "uname": "", "face": ""}

    info = session.account_info()

    assert info["logged_in"] is True
    assert info["mid"] == "42"
    assert info["uname"] == "某人"
    assert info["face"] == "https://example.invalid/face.jpg"


def test_a_known_account_is_answered_from_the_cache(isolated_home, tmp_path):
    client = FakeClient()
    session = BiliSession(client, path=tmp_path / "bilibili.json")
    session._cookie = "SESSDATA=abc"
    session._account = {"mid": "42", "uname": "某人", "face": "https://example.invalid/f.jpg"}

    info = session.account_info()

    assert info["mid"] == "42"
    assert info["stale"] is True
    # 缓存够用时不该去问服务器
    assert client.session.responses == {}


# --------------------------------------------------------------------- CDN 主机
def test_the_fast_cdn_host_comes_first():
    """mcdn 节点实测最慢，官方 upos 镜像最快。"""
    urls = [
        "https://xy61x164x142x12xy.mcdn.bilivideo.cn:8082/v1/resource/x.m4s",
        "https://upos-sz-mirrorbd.bilivideo.com/v1/resource/x.m4s",
        "https://mv0bs24m.edge.mountaintoys.cn:4483/v1/resource/x.m4s",
    ]

    ordered = _ordered_bili_urls(urls)

    assert ordered[0].startswith("https://upos-sz-mirrorbd")
    assert ordered[-1].startswith("https://xy61x164x142x12xy.mcdn")
    assert len(ordered) == 3


def test_duplicate_urls_are_removed():
    ordered = _ordered_bili_urls(["https://a.bilivideo.com/x.m4s",
                                  "https://a.bilivideo.com/x.m4s", ""])

    assert ordered == ["https://a.bilivideo.com/x.m4s"]


def test_the_highest_bitrate_wins_for_one_resolution():
    """同一个画质给好几条，得挑码率最高的那条。"""
    client = BiliClient(cookie="SESSDATA=abc")
    entries = [
        {"id": 80, "height": 1080, "bandwidth": 749418,
         "baseUrl": "https://upos-sz-mirrorbd.bilivideo.com/low.m4s"},
        {"id": 80, "height": 1080, "bandwidth": 2629444,
         "baseUrl": "https://upos-sz-mirrorbd.bilivideo.com/high.m4s"},
        {"id": 64, "height": 720, "bandwidth": 358814,
         "baseUrl": "https://upos-sz-mirrorbd.bilivideo.com/small.m4s"},
    ]

    chosen = client._pick_video(entries, 80)

    assert chosen is not None
    assert chosen["bandwidth"] == 2629444
