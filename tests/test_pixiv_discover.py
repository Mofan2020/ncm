"""发现页/排行榜在「没登录」和「取不到」时到底给什么。

用户的原话是：不登录不应当获取不到作品；即便真取不到，也要给出错误信息，
便于排查。

这里钉三件事：
1. 推荐流（要会话）失败之后，还要去试公开的老接口，再不行拿排行榜兜底——
   游客也点得开发现页。
2. 三个来源都失败时，**每一次失败的原因都留在 ``last_error`` 里**，不是一句
   「没有作品」。
3. 失败和空结果**不进缓存**。一次 400 或者一次网络抖动把空列表写进缓存，那
   个页签会一直空到 TTL 到期，而界面上什么原因都看不到。
"""
from __future__ import annotations

import pytest

from src.core import pixiv as pixiv_mod
from src.core.pixiv import PixivClient
from src.gui.pixiv_bridge import PixivBridge


def _work(pid: str) -> dict:
    """一份最小可用的接口返回，够 normalize_work 认出来。"""
    return {
        "id": pid,
        "title": "t" + pid,
        "url": "https://i.pximg.net/x.jpg",
        "userId": "1",
        "userName": "u",
        "pageCount": 1,
    }


class _FakeClient(PixivClient):
    """按顺序给每个 URL 安排回答，同时记下打过哪些接口。"""

    def __init__(self, answers: dict[str, object]) -> None:
        super().__init__()
        self._answers = answers
        self.calls: list[str] = []

    def _ajax(self, path: str, params=None, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(path)
        answer = self._answers.get(path, (None, "没有安排回答"))
        body, error = answer if isinstance(answer, tuple) else (answer, "")
        self.last_error = error or ""
        return body

    def ranking(self, *, mode="daily", content="illust", page=1, date=""):  # noqa: ANN001, ANN201
        self.calls.append("ranking")
        body = self._answers.get("ranking")
        if body is None:
            self.last_error = "排行榜也挂了"
            return {"works": [], "has_more": False}
        return {"works": body, "has_more": False}


def test_a_guest_still_gets_works_from_the_public_route() -> None:
    """推荐流要会话、游客回 400，但老接口是公开的。"""
    client = _FakeClient({
        "/ajax/discovery/artworks": (None, "服务器回了 400。"),
        "/ajax/illust/discovery": {"illusts": [_work("1"), _work("2")]},
    })
    works = client.discovery()
    assert [w["id"] for w in works] == ["1", "2"]
    assert client.last_error == ""


def test_the_ranking_is_the_last_resort_and_says_so() -> None:
    client = _FakeClient({
        "/ajax/discovery/artworks": (None, "服务器回了 400。"),
        "/ajax/illust/discovery": (None, "服务器回了 500。"),
        "ranking": [{"id": "9", "title": "r", "thumb": "https://x/9.jpg"}],
    })
    works = client.discovery()
    assert [w["id"] for w in works] == ["9"]
    assert works[0].get("from_ranking") is True
    # 兜底也要留一句话：界面才能说清「这是排行榜，不是推荐流」。
    assert "排行榜" in client.last_error


def test_every_failed_source_is_named_when_nothing_works() -> None:
    """一条都拿不到时，原因要一条不落。"""
    client = _FakeClient({
        "/ajax/discovery/artworks": (None, "服务器回了 400。"),
        "/ajax/illust/discovery": (None, "服务器回了 500。"),
    })
    assert client.discovery() == []
    message = client.last_error
    assert "400" in message
    assert "500" in message
    assert "排行榜" in message
    assert len(client.calls) == 3, client.calls


@pytest.mark.parametrize("value, worth", [
    (None, False),
    ([], False),
    ({}, False),
    ({"works": []}, False),
    ({"works": [], "error": "挂了"}, False),
    ({"works": [{"id": "1"}]}, True),
    ([{"id": "1"}], True),
])
def test_only_real_content_goes_into_the_cache(value, worth) -> None:  # noqa: ANN001
    assert PixivBridge._worth_caching(value) is worth


class _CountingCache:
    def __init__(self) -> None:
        self.stored: list[object] = []

    def get(self, kind, key):  # noqa: ANN001, ANN201
        return None

    def set(self, kind, key, value, ttl):  # noqa: ANN001, ANN201
        self.stored.append(value)

    def discard(self, kind, key):  # noqa: ANN001
        return None

    def discard_prefix(self, kind, prefix):  # noqa: ANN001
        return None


def test_an_empty_answer_is_not_written_back() -> None:
    """一次失败不能把这个页签钉死到 TTL 到期。"""
    cache = _CountingCache()
    bridge = PixivBridge(cache=cache)
    assert bridge._cached("ranking", "daily|illust|1", lambda: {"works": []}) == {"works": []}
    assert cache.stored == []
    assert bridge._cached("ranking", "k", lambda: {"works": [{"id": "1"}]})
    assert cache.stored == [{"works": [{"id": "1"}]}]


def test_home_hands_the_error_to_the_interface() -> None:
    """发现页空着的时候，界面得能说出原因。"""
    class Session:
        is_logged_in = False

        def __init__(self) -> None:
            self.client = _FakeClient({
                "/ajax/discovery/artworks": (None, "服务器回了 400。"),
                "/ajax/illust/discovery": (None, "服务器回了 503。"),
            })

    cache = _CountingCache()
    bridge = PixivBridge(session=Session(), cache=cache)
    home = bridge.home()
    assert home["discover"] == []
    assert "400" in home["discover_error"]
    assert "503" in home["discover_error"]


def test_ranking_hands_its_error_to_the_interface() -> None:
    class Session:
        is_logged_in = False

        def __init__(self) -> None:
            self.client = _FakeClient({})

    bridge = PixivBridge(session=Session(), cache=_CountingCache())
    out = bridge.ranking()
    assert out["works"] == []
    assert out["error"], "排行榜空了也不给原因，界面上就只能写一句「没有作品」"


def test_normalize_keeps_what_the_tiles_need() -> None:
    """兜底用的排行榜条目也要能画出来。"""
    work = pixiv_mod.normalize_work(_work("7"))
    assert work["id"] == "7"
    assert work["thumb"]


# ------------------------------------------------ 过期的 cookie 会把公开接口一起带死

class _StubResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._body = body

    def json(self):  # noqa: ANN201
        return self._body


class _StubSession:
    """按脚本回话的 requests.Session，顺手记下每一次带的请求头。"""

    def __init__(self, script: list[tuple[int, object]]) -> None:
        self.headers: dict[str, str] = {}
        self._script = list(script)
        self.seen: list[dict[str, str]] = []

    def request(self, method, url, params=None, json=None, data=None,  # noqa: ANN001, ANN201
                headers=None, timeout=None):
        self.seen.append({"method": method, "url": url,
                          "cookie": self.headers.get("Cookie", "")})
        if not self._script:
            return _StubResponse(500, None)
        status, body = self._script.pop(0)
        return _StubResponse(status, body)


def test_an_expired_cookie_does_not_shut_the_public_routes_down() -> None:
    """用户报的那件事的根子。

    一个失效的 PHPSESSID 会让**公开接口**也回 401。带着它，发现页、搜索、
    排行榜全都拿不到东西；摘掉之后当游客，这些接口本来就能用。
    """
    session = _StubSession([
        (401, None),                                            # 带死 cookie
        # 摘掉 cookie 之后重打的是**同一个**接口，所以回答也得是这个接口
        # 的形状（thumbnails.illust），而且是 pixiv 那层包络
        # `{error, body}`——`_ajax` 剥的就是它。
        (200, {"error": False,
               "body": {"thumbnails": {"illust": [_work("11"), _work("12")]}}}),
    ])
    client = PixivClient(session=session)
    client.apply_cookie("PHPSESSID=deadbeef")

    works = client.discovery()

    assert [w["id"] for w in works] == ["11", "12"]
    assert client.session_expired is True
    assert client.signed_in is False, "失效之后不该再自称已登录"
    # 第一次带着 cookie，第二次必须不带——不然重试也是白试。
    assert session.seen[0]["cookie"], "第一次应该带着 cookie 去撞一次"
    assert session.seen[1]["cookie"] == "", "重试的时候 cookie 必须摘掉"
    # 存的那份还留着：账号页要能说「已失效，请重新登录」。
    assert "PHPSESSID" in client.cookie_string()


def test_a_fresh_cookie_puts_it_back() -> None:
    session = _StubSession([(401, None),
                            (200, {"error": False, "body": {"illusts": []}})])
    client = PixivClient(session=session)
    client.apply_cookie("PHPSESSID=dead")
    client.discovery()
    assert client.session_expired is True

    client.apply_cookie("PHPSESSID=alive")
    assert client.session_expired is False
    assert client.signed_in is True
    assert client.cookie_string() == "PHPSESSID=alive"
