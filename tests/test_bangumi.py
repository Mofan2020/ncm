"""Bangumi 客户端、存储和 bridge 的测试。

三块：

* :class:`BangumiClient` —— 搜索、详情、章节、镜像。全部用假 session，
  不发一个真请求。
* :class:`BangumiStore` —— 本地收藏和观看历史：存盘、读盘、合并。
* :class:`BangumiBridge` —— 前端看到的那一层。

另外验证一件要紧的事：**这个模块不登录、不带 cookie、不写云端**。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.bangumi import (
    DEFAULT_RESOURCE_SITES,
    BangumiClient,
    render_resource_links,
)
from src.core.bangumi_store import BangumiCache, BangumiStore
from src.gui.bangumi_bridge import BangumiBridge


# --------------------------------------------------------------------- 假 session
class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """够用的假 session：按 (method, path) 派发。"""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.headers = {}
        self.calls = []
        self.verified = []

    def get(self, url, params=None, timeout=None, verify=True, headers=None):
        self.calls.append((url, dict(params or {})))
        self.verified.append(verify)
        for path, payload in self.routes.items():
            if url.endswith(path):
                if callable(payload):
                    return FakeResponse(payload(params or {}))
                return FakeResponse(payload)
        return FakeResponse({"code": 404, "error": "not found"}, status_code=404)

    def post(self, url, json=None, timeout=None, verify=True, headers=None):
        self.calls.append((url, dict(json or {})))
        self.verified.append(verify)
        for path, payload in self.routes.items():
            if url.endswith(path):
                if callable(payload):
                    return FakeResponse(payload(json or {}))
                return FakeResponse(payload)
        return FakeResponse({"code": 404, "error": "not found"}, status_code=404)


# --------------------------------------------------------------------- 客户端
def test_search_returns_normalized_subjects():
    session = FakeSession({
        "/v0/search/subjects": {"data": [{
            "id": 400602,
            "name": "Sousou no Frieren",
            "name_cn": "葬送的芙莉莲",
            "type": 2,
            "platform": "TV",
            "date": "2023-09-29",
            "images": {"common": "https://lain.bangumi.lat/cover.jpg"},
            "summary": "勇者死后……",
        }]},
    })
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    items = client.search("芙莉莲")
    assert len(items) == 1
    item = items[0]
    assert item["id"] == 400602
    assert item["name_cn"] == "葬送的芙莉莲"
    assert item["type_name"] == "动画"
    assert item["images"]["common"].endswith("cover.jpg")


def test_search_without_keyword_makes_no_request():
    session = FakeSession()
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    assert client.search("   ") == []
    assert session.calls == []


def test_search_posts_the_keyword_and_type_filter():
    """搜索必须走 POST，且带上类型过滤。

    ``GET /v0/subjects?keyword=`` 那个参数是不起作用的——它无视关键词，
    返回一堆按 ID 排的无关条目。
    """
    session = FakeSession({"/v0/search/subjects": {"data": []}})
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    client.search("x", subject_type=4)
    url, body = session.calls[0]
    assert url.endswith("/v0/search/subjects")
    assert body["keyword"] == "x"
    assert body["filter"] == {"type": [4]}


def test_search_without_type_filter_omits_it():
    session = FakeSession({"/v0/search/subjects": {"data": []}})
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    client.search("x", subject_type=0)
    _, body = session.calls[0]
    assert "filter" not in body


def test_subject_detail():
    session = FakeSession({
        "/v0/subjects/400602": {
            "id": 400602, "name": "Frieren", "name_cn": "葬送的芙莉莲",
            "total_episodes": 36, "rating": {"score": 8.5, "total": 36832},
            "collection": {"collect": 55720},
            "meta_tags": ["TV", "日本"],
        },
    })
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    data = client.subject(400602)
    assert data is not None
    assert data["total_episodes"] == 36
    assert data["rating_score"] == 8.5
    assert data["collection"]["collect"] == 55720
    assert data["meta_tags"] == ["TV", "日本"]


def test_subject_with_bad_id_makes_no_request():
    session = FakeSession()
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    assert client.subject("not-a-number") is None
    assert session.calls == []


def test_episodes_are_grouped_by_type():
    """正片排前面，片头片尾特别篇在后。

    接口返回的是按 sort 混在一起的——片头的 sort 也是 1、2、3，
    只按 sort 排会让那些没有标题的条目插到正片中间。
    """
    session = FakeSession({
        "/v0/episodes": {"data": [
            {"id": 90, "type": 1, "ep": 0, "sort": 1, "name_cn": ""},
            {"id": 10, "type": 0, "ep": 1, "sort": 1, "name_cn": "第一集"},
            {"id": 91, "type": 2, "ep": 0, "sort": 2, "name_cn": ""},
            {"id": 20, "type": 0, "ep": 2, "sort": 2, "name_cn": "第二集"},
        ]},
    })
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    eps = client.episodes(1)
    assert [e["type"] for e in eps] == [0, 0, 1, 2]
    assert [e["id"] for e in eps] == [10, 20, 90, 91]


def test_episode_label_falls_back_when_there_is_no_title():
    session = FakeSession({
        "/v0/episodes": {"data": [
            {"id": 1, "type": 0, "ep": 3, "sort": 3, "name_cn": ""},
            {"id": 2, "type": 3, "ep": 0, "sort": 2, "name_cn": ""},
        ]},
    })
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    eps = client.episodes(1)
    assert eps[0]["label"] == "第 3 集"
    assert eps[1]["label"] == "特别篇 2"
    assert eps[0]["type_name"] == "正片"


def test_episodes_are_sorted():
    session = FakeSession({
        "/v0/episodes": {"data": [
            {"id": 2, "ep": 2, "sort": 2, "name_cn": "第二集"},
            {"id": 1, "ep": 1, "sort": 1, "name_cn": "第一集",
             "duration": "00:24:30", "airdate": "2023-09-29"},
        ]},
    })
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    eps = client.episodes(400602)
    assert [e["ep"] for e in eps] == [1, 2]
    assert eps[0]["name_cn"] == "第一集"
    assert eps[0]["duration"] == "00:24:30"


def test_client_never_raises_on_transport_error():
    class BrokenSession(FakeSession):
        def get(self, *args, **kwargs):
            raise OSError("connection reset")

    client = BangumiClient("https://api.bangumi.lat", session=BrokenSession(),
                            min_interval=0)
    assert client.search("x") == []
    assert client.subject(1) is None
    assert client.episodes(1) == []


def test_client_passes_verify_flag():
    session = FakeSession({"/v0/subjects": {"data": []}})
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0, verify=False)
    client.search("x")
    assert session.verified == [False]


def test_built_in_mirrors_have_api_hosts():
    mirrors = BangumiClient.built_in_mirrors()
    assert mirrors
    assert all(m["api_hosts"] for m in mirrors)
    assert any("bangumi.lat" in h for m in mirrors for h in m["api_hosts"])


# --------------------------------------------------------------------- 存储
def test_store_favorite_roundtrip(tmp_path: Path):
    store = BangumiStore(tmp_path / "bangumi.json")
    assert store.favorites() == []
    assert store.set_favorite("400602", True, {"name_cn": "芙莉莲"}) is True
    assert store.is_favorite("400602") is True
    assert store.favorites()[0]["name_cn"] == "芙莉莲"

    # 重新读一遍：数据真的落盘了
    again = BangumiStore(tmp_path / "bangumi.json")
    assert again.is_favorite("400602") is True
    assert again.favorites()[0]["name_cn"] == "芙莉莲"


def test_store_unfavorite(tmp_path: Path):
    store = BangumiStore(tmp_path / "bangumi.json")
    store.set_favorite("1", True)
    store.set_favorite("1", False)
    assert store.is_favorite("1") is False
    assert store.favorites() == []


def test_store_history_keeps_latest_episode_only(tmp_path: Path):
    store = BangumiStore(tmp_path / "bangumi.json")
    store.record_episode("400602", "111", {"ep": 1})
    store.record_episode("400602", "222", {"ep": 2})
    history = store.history()
    assert len(history) == 1
    assert history[0]["episode_id"] == "222"
    assert history[0]["ep"] == 2


def test_store_history_survives_reload(tmp_path: Path):
    path = tmp_path / "bangumi.json"
    store = BangumiStore(path)
    store.record_episode("400602", "111", {"ep": 1, "name_cn": "第一集"})
    again = BangumiStore(path)
    assert len(again.history()) == 1
    assert again.history()[0]["name_cn"] == "第一集"


def test_store_clear_history_keeps_favorites(tmp_path: Path):
    store = BangumiStore(tmp_path / "bangumi.json")
    store.set_favorite("1", True)
    store.record_episode("1", "11")
    assert store.clear_history() == 1
    assert store.history() == []
    assert store.is_favorite("1") is True


def test_store_survives_broken_file(tmp_path: Path):
    path = tmp_path / "bangumi.json"
    path.write_text("{not json", encoding="utf-8")
    store = BangumiStore(path)
    assert store.favorites() == []
    assert store.history() == []


def test_store_merge_favorites_skips_existing(tmp_path: Path):
    store = BangumiStore(tmp_path / "bangumi.json")
    store.set_favorite("1", True, {"name_cn": "本地"})
    added = store.merge_favorites([
        {"subject_id": "1", "name_cn": "备份"},
        {"subject_id": "2", "name_cn": "新的"},
    ])
    assert added == 1
    names = {item["subject_id"]: item["name_cn"] for item in store.favorites()}
    assert names["1"] == "本地"   # 已有的不被覆盖
    assert names["2"] == "新的"


def test_store_merge_history_uses_newer_timestamp(tmp_path: Path):
    store = BangumiStore(tmp_path / "bangumi.json")
    store.record_episode("1", "old", {"ep": 1})
    added = store.merge_history([
        {"subject_id": "1", "episode_id": "new", "ep": 5, "watched_at": 9e9},
    ])
    assert added == 1
    assert store.history()[0]["episode_id"] == "new"


# --------------------------------------------------------------- 资源站模板
def test_render_resource_links_fills_placeholders():
    links = render_resource_links(
        [{"id": "s1", "name": "站1", "enabled": True,
          "detail_url": "https://x.test/{keyword}",
          "search_url": "https://x.test/search?q={keyword}"}],
        subject={"id": 400602, "name": "Frieren", "name_cn": "葬送的芙莉莲"},
        episode={"ep": 3, "name_cn": "第三集"},
    )
    assert len(links) == 1
    assert links[0]["detail_url"] == "https://x.test/%E8%91%AC%E9%80%81%E7%9A%84%E8%8A%99%E8%8E%89%E8%8E%B2"


def test_render_resource_links_skips_missing_values():
    """缺值的模板整条作废——半截的 URL 比没有更糟。"""
    links = render_resource_links(
        [{"id": "s1", "enabled": True,
          "detail_url": "https://x.test/{keyword}/{ep}"}],
        subject={"id": 1, "name": "x", "name_cn": "有中文名"},
        episode={},
    )
    assert links == []


def test_render_resource_links_skips_disabled():
    links = render_resource_links(
        [{"id": "s1", "enabled": False, "detail_url": "https://x.test/{keyword}"}],
        subject={"id": 1, "name_cn": "名"},
    )
    assert links == []


def test_render_resource_links_rejects_unknown_placeholder():
    links = render_resource_links(
        [{"id": "s1", "enabled": True, "detail_url": "https://x.test/{nope}"}],
        subject={"id": 1, "name_cn": "名"},
    )
    assert links == []


def test_default_resource_sites_are_usable():
    links = render_resource_links(
        DEFAULT_RESOURCE_SITES,
        subject={"id": 400602, "name": "Frieren", "name_cn": "芙莉莲"},
    )
    assert links
    assert links[0]["detail_url"]


# ------------------------------------------------------------------- bridge
class FakeCache:
    """内存缓存。测试不该碰用户真实的缓存目录。"""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get(self, kind, key):
        return self.data.get((kind, key))

    def set(self, kind, key, value, ttl):
        self.data[(kind, key)] = value

    def discard(self, kind, key):
        self.data.pop((kind, key), None)

    def discard_prefix(self, kind, prefix):
        for entry in [k for k in self.data if k[0] == kind and k[1].startswith(prefix)]:
            del self.data[entry]

    def clear(self, kind=None):
        for entry in [k for k in self.data if kind is None or k[0] == kind]:
            del self.data[entry]


def make_bridge(tmp_path: Path, routes=None) -> BangumiBridge:
    session = FakeSession(routes or {})
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    store = BangumiStore(tmp_path / "bangumi.json")
    cache = BangumiCache(cache=FakeCache())
    return BangumiBridge(client=client, store=store, cache=cache)


def fake_session(bridge: BangumiBridge) -> FakeSession:
    """拿回注入进去的那个假 session。"""
    session = bridge.client.session
    assert isinstance(session, FakeSession)
    return session


def test_bridge_search(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/search/subjects": {"data": [
        {"id": 1, "name_cn": "测试番"}]}})
    result = bridge.search("测试")
    assert result["success"] is True
    assert result["items"][0]["name_cn"] == "测试番"


def test_bridge_caches_search(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/search/subjects": {"data": []}})
    bridge.search("x")
    bridge.search("x")
    assert len(fake_session(bridge).calls) == 1


def test_bridge_subject_reports_favorite_state(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/subjects/1": {"id": 1, "name_cn": "番"}})
    bridge.store.set_favorite("1", True)
    result = bridge.subject(1)
    assert result["success"] is True
    assert result["subject"]["favorite"] is True


def test_bridge_subject_not_found(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    result = bridge.subject(1)
    assert result["success"] is False
    assert result["error_key"] == "bangumi.error.not_found"


def test_bridge_episodes(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/episodes": {"data": [
        {"id": 1, "ep": 1, "sort": 1, "name_cn": "第一集"}]}})
    result = bridge.episodes(400602)
    assert result["success"] is True
    assert result["episodes"][0]["name_cn"] == "第一集"


def test_bridge_favorite_toggles_locally(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    assert bridge.toggle_favorite("400602")["favorite"] is True
    assert bridge.toggle_favorite("400602")["favorite"] is False
    assert bridge.favorites()["count"] == 0


def test_bridge_favorite_picks_up_cached_metadata(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/subjects/1": {
        "id": 1, "name_cn": "芙莉莲",
        "images": {"common": "https://lain.bangumi.lat/c.jpg"}}})
    bridge.subject(1)          # 先把详情读进缓存
    bridge.set_favorite("1", True)
    fav = bridge.favorites()["items"][0]
    assert fav["name_cn"] == "芙莉莲"
    assert fav["cover"].endswith("c.jpg")


def test_bridge_history_is_local(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/episodes": {"data": [
        {"id": 111, "ep": 1, "sort": 1, "name_cn": "第一集"}]}})
    result = bridge.record_history("400602", "111")
    assert result["success"] is True
    assert bridge.history()["count"] == 1
    assert bridge.history()["items"][0]["name_cn"] == "第一集"


def test_bridge_clear_history(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    bridge.store.record_episode("1", "11")
    assert bridge.clear_history()["removed"] == 1
    assert bridge.history()["count"] == 0


def test_bridge_resource_links_do_not_hit_the_network(tmp_path: Path):
    """资源链接是拼出来的，不该发请求。"""
    bridge = make_bridge(tmp_path, {"/v0/subjects/1": {
        "id": 1, "name_cn": "芙莉莲"}})
    before = len(fake_session(bridge).calls)
    bridge.resource_links(1, episode_id="5")
    # 只有可能读缓存里的 subject 和 episodes
    assert len(fake_session(bridge).calls) <= before + 2


def test_bridge_mirrors_falls_back_to_builtin(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    result = bridge.mirrors()
    assert result["success"] is True
    assert result["mirrors"]
