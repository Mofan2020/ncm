"""排行榜的档位/日期、以及详情页的相关作品。

这两块都是拿真实接口量出来的：

* ``/ranking.php`` 只有 ``daily`` / ``weekly`` / ``monthly`` / ``rookie`` 出榜，
  ``original`` / ``male`` / ``female`` / ``day`` / ``week`` 全是 404；
  内容只有 ``illust`` / ``manga``（``novel`` 404）。
* **今天和未来都没有榜**（pixiv 按东京时间，第二天才出）。所以日期一律夹到
  昨天或更早；那天真的没有就退回最近一期并说明。
* ``/ajax/illust/{id}/related`` 是 404，能用的是 ``/recommend/init``，而且
  不带 ``limit`` 会 400。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.pixiv import PixivClient
from src.gui.pixiv_bridge import PixivBridge


def _rank_item(pid: str, rank: int = 1) -> dict:
    return {
        "illust_id": pid, "title": "t" + pid, "url": "https://i.pximg.net/x.jpg",
        "rank": rank, "yes_rank": 0, "user_id": "1", "user_name": "u",
        "tags": ["tag"], "illust_type": 0, "illust_page_count": 1,
    }


class _Recorder(PixivClient):
    """按脚本回话，同时把每次请求的参数记下来。"""

    def __init__(self, script: list[tuple[object, str]] | None = None) -> None:
        super().__init__()
        self._script = list(script or [])
        self.requests: list[tuple[str, dict]] = []
        self.ajax: list[tuple[str, dict]] = []

    def _request(self, method, path, *, params=None, json_body=None,
                 form=None, write=False, _allow_guest=True):  # noqa: ANN001, ANN201
        self.requests.append((path, dict(params or {})))
        if not self._script:
            return {}, "没有安排回答"
        return self._script.pop(0)

    def _ajax(self, path, params=None):  # noqa: ANN001, ANN201
        self.ajax.append((path, dict(params or {})))
        if not self._script:
            return None
        return self._script.pop(0)[0]


# ---------------------------------------------------------------- 档位与内容

def test_only_modes_that_really_answer_are_allowed() -> None:
    assert PixivClient.RANKING_MODES == ("daily", "weekly", "monthly", "rookie")
    assert PixivClient.RANKING_CONTENTS == ("illust", "manga")


def test_a_mode_that_404s_falls_back_to_daily() -> None:
    client = _Recorder([({"contents": [_rank_item("1")]}, "")])
    client.ranking(mode="original", content="novel")
    _path, params = client.requests[0]
    assert params["mode"] == "daily"
    assert params["content"] == "illust"


# ------------------------------------------------------------------ 日期

def test_a_day_with_no_ranking_yet_is_clamped_to_yesterday() -> None:
    """今天和未来都没有榜，直接夹到东京时间的昨天。"""
    tokyo_today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    yesterday = (datetime.now(timezone(timedelta(hours=9)))
                 - timedelta(days=1)).strftime("%Y%m%d")
    assert PixivClient.ranking_date(tokyo_today) == yesterday
    assert PixivClient.ranking_date("29991231") == yesterday


def test_an_old_date_is_kept_as_is() -> None:
    assert PixivClient.ranking_date("20251010") == "20251010"
    assert PixivClient.ranking_date("2025-10-10") == "20251010"


def test_no_date_means_let_pixiv_pick_the_latest() -> None:
    """空日期不往请求里塞 date——pixiv 自己知道最近一期是哪天。"""
    assert PixivClient.ranking_date("") == ""
    assert PixivClient.ranking_date("乱写的") == ""
    client = _Recorder([({"contents": [_rank_item("1")], "date": "20261112"}, "")])
    client.ranking()
    _path, params = client.requests[0]
    assert "date" not in params


def test_a_chosen_day_in_the_future_is_sent_as_yesterday_not_as_itself() -> None:
    client = _Recorder([({"contents": [_rank_item("1")]}, "")])
    client.ranking(date="29991231")
    _path, params = client.requests[0]
    assert params["date"] != "29991231"
    assert len(params["date"]) == 8


def test_a_day_without_a_ranking_falls_back_and_says_so() -> None:
    """选了那天但榜还没出：退回最近一期，并且说明退回了。"""
    client = _Recorder([
        ({"contents": []}, ""),                                  # 那天没有
        ({"contents": [_rank_item("9")], "date": "20261112"}, ""),  # 最近一期
    ])
    out = client.ranking(date="20200101")
    assert [work["id"] for work in out["works"]] == ["9"]
    assert "date" not in client.requests[1][1]
    assert out["note"], "退回了却不说明，用户只会以为日期选错了"
    assert out["date"] == "20261112"


def test_the_answer_carries_mode_and_content_back() -> None:
    client = _Recorder([({"contents": [_rank_item("1")], "date": "20261112"}, "")])
    out = client.ranking(mode="weekly", content="manga")
    assert out["mode"] == "weekly"
    assert out["content"] == "manga"


# -------------------------------------------------------------- 相关作品

def test_related_uses_the_route_that_actually_answers() -> None:
    client = _Recorder([({"illusts": [{"id": "5", "title": "r", "url": "https://x/5.jpg",
                                       "userId": "1", "userName": "u", "pageCount": 1}]}, "")])
    works = client.related("110000000")
    path, params = client.ajax[0]
    # /related 是 404；不带 limit 会 400。
    assert path == "/ajax/illust/110000000/recommend/init"
    assert params.get("limit") == 18
    assert [work["id"] for work in works] == ["5"]


def test_related_without_an_id_does_not_go_out() -> None:
    client = _Recorder()
    assert client.related("") == []
    assert client.ajax == []


def test_related_ignores_junk_items() -> None:
    client = _Recorder([({"illusts": [{"id": ""}, "不是字典", {"id": "7", "url": "https://x/7.jpg"}]}, "")])
    assert [work["id"] for work in client.related("1")] == ["7"]


# ------------------------------------------------------------------ 桥接

class _FakeSession:
    is_logged_in = False

    def __init__(self, client: PixivClient) -> None:
        self.client = client

    def save(self, cookie, info):  # noqa: ANN001
        return None


class _Cache:
    def __init__(self) -> None:
        self.stored: dict[tuple[str, str], object] = {}

    def get(self, kind, key):  # noqa: ANN001, ANN201
        return self.stored.get((kind, key))

    def set(self, kind, key, value, ttl):  # noqa: ANN001, ANN201
        self.stored[(kind, key)] = value

    def discard(self, kind, key):  # noqa: ANN001
        return None

    def discard_prefix(self, kind, prefix):  # noqa: ANN001
        return None


def test_the_bridge_passes_content_and_date_through() -> None:
    client = _Recorder([({"contents": [_rank_item("1")], "date": "20261112"}, "")])
    bridge = PixivBridge(session=_FakeSession(client), cache=_Cache())
    out = bridge.ranking(mode="monthly", page=2, content="manga", date="20261201")
    _path, params = client.requests[0]
    assert params["mode"] == "monthly"
    assert params["content"] == "manga"
    assert params["p"] == 2
    assert out["mode"] == "monthly" and out["content"] == "manga"


def test_the_bridge_caches_related_and_explains_an_empty_answer() -> None:
    client = _Recorder([({"illusts": []}, "")])
    cache = _Cache()
    bridge = PixivBridge(session=_FakeSession(client), cache=cache)
    first = bridge.related("42")
    assert first["works"] == []
    assert first["error"], "空结果也要给原因"
    # 空结果不进缓存：再点一次要真的再问一次。
    bridge.related("42")
    assert len(client.ajax) == 2
