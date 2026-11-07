"""Bangumi bridge 还没测过的接口。

* get_bangumi_resource_links
* toggle_bangumi_favorite
* clear_bangumi_history
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.bangumi import BangumiClient, render_resource_links
from src.core.bangumi_store import BangumiCache, BangumiStore
from src.gui.bangumi_bridge import BangumiBridge


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


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


class FakeSession:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, verify=True, headers=None):
        self.calls.append((url, dict(params or {})))
        for path, payload in self.routes.items():
            if url.endswith(path):
                if callable(payload):
                    return FakeResponse(payload(params or {}))
                return FakeResponse(payload)
        return FakeResponse({"code": 404, "error": "not found"}, status_code=404)

    def post(self, url, **kwargs):
        raise AssertionError("Bangumi 不该发 POST")


def make_bridge(tmp_path: Path, routes=None) -> BangumiBridge:
    session = FakeSession(routes or {})
    client = BangumiClient("https://api.bangumi.lat", session=session,
                            min_interval=0)
    store = BangumiStore(tmp_path / "bangumi.json")
    cache = BangumiCache(cache=FakeCache())
    return BangumiBridge(client=client, store=store, cache=cache)


# ------------------------------------------------------------------ 资源链接
def test_resource_links_renders_the_templates(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/subjects/400602": {
        "id": 400602, "name_cn": "葬送的芙莉莲", "name": "Sousou no Frieren",
        "images": {"common": "https://lain.bangumi.lat/c.jpg"}}})
    result = bridge.resource_links(400602)
    assert result["success"] is True
    assert result["links"]
    link = result["links"][0]
    assert "detail_url" in link or "search_url" in link


def test_resource_links_does_not_hit_the_network(tmp_path: Path):
    """资源链接是拼出来的，不该发请求。"""
    bridge = make_bridge(tmp_path, {"/v0/subjects/1": {"id": 1, "name_cn": "甲"}})
    before = len(bridge.client.session.calls)
    bridge.resource_links(1)
    assert len(bridge.client.session.calls) <= before + 1


# ------------------------------------------------------------------ 收藏切换
def test_toggle_favorite_flips_the_state(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    assert bridge.toggle_favorite("400602")["favorite"] is True
    assert bridge.toggle_favorite("400602")["favorite"] is False


def test_toggle_favorite_picks_up_cached_metadata(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/subjects/1": {
        "id": 1, "name_cn": "芙莉莲",
        "images": {"common": "https://lain.bangumi.lat/c.jpg"}}})
    bridge.subject(1)          # 先把详情读进缓存
    bridge.set_favorite("1", True)
    # record_episode -> record_history
    bridge.record_history("1", "111")
    fav = bridge.favorites()["items"][0]
    assert fav["name_cn"] == "芙莉莲"
    assert fav["cover"].endswith("c.jpg")


# ------------------------------------------------------------------ 观看历史
def test_clear_history(tmp_path: Path):
    bridge = make_bridge(tmp_path)
    bridge.store.record_episode("1", "11")
    assert bridge.clear_history()["removed"] == 1
    assert bridge.history()["count"] == 0


def test_history_records_episode(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/episodes": {"data": [
        {"id": 111, "ep": 1, "sort": 1, "name_cn": "第一集"}]}})
    result = bridge.record_history("400602", "111")
    assert result["success"] is True
    assert bridge.history()["count"] == 1
    assert bridge.history()["items"][0]["name_cn"] == "第一集"


def test_history_for_returns_the_latest(tmp_path: Path):
    bridge = make_bridge(tmp_path, {"/v0/episodes": {"data": [
        {"id": 111, "ep": 1, "sort": 1, "name_cn": "第一集"},
        {"id": 222, "ep": 2, "sort": 2, "name_cn": "第二集"}]}})
    bridge.record_history("400602", "111")
    bridge.record_history("400602", "222")
    res = bridge.history()
    assert res["success"]
    assert res["items"]
    assert res["items"][0]["episode_id"] == "222"


# ------------------------------------------------------------------ 资源站模板
def test_render_resource_links_handles_missing_values():
    """缺值的模板整条作废——半截的 URL 比没有更糟。"""
    links = render_resource_links(
        [{"id": "s1", "enabled": True, "detail_url": "https://x.test/{keyword}/{ep}"}],
        subject={"id": 1, "name_cn": "有中文名"},
        episode={},
    )
    assert links == []


def test_render_resource_links_rejects_unknown_placeholder():
    links = render_resource_links(
        [{"id": "s1", "enabled": True, "detail_url": "https://x.test/{nope}"}],
        subject={"id": 1, "name_cn": "名"},
    )
    assert links == []


def test_render_resource_links_skips_disabled():
    links = render_resource_links(
        [{"id": "s1", "enabled": False, "detail_url": "https://x.test/{keyword}"}],
        subject={"id": 1, "name_cn": "名"},
    )
    assert links == []


def test_render_resource_links_rejects_unknown_placeholder_in_search():
    links = render_resource_links(
        [{"id": "s1", "enabled": True, "search_url": "https://x.test/{nope}"}],
        subject={"id": 1, "name_cn": "名"},
    )
    assert links == []
