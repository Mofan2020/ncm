"""关注动态的请求参数。

用户原话：「SkycMedia 还是在登录成功情况下无法查看关注」。真机探针查出来
根本不是登录问题：``/ajax/follow_latest/illust`` 不认 ``mode=safe``——带它
回 400「不正确的请求」，不带或带 ``all`` / ``r18`` 都回 200。而界面上的成人
内容设置（默认隐藏 R-18）正好被映射成 ``safe``，于是默认设置下关注页永远是
空的，界面只能说一句「服务器回了 400」。

成人内容本来就由 ``PixivBridge._filter`` 在本地过一遍，服务端那层不必再传。
"""
from __future__ import annotations

from src.core.pixiv import PixivClient


def _client_with(monkeypatch, captured: dict):
    client = PixivClient()

    def fake_ajax(path, params=None):
        captured["path"] = path
        captured["params"] = dict(params or {})
        return {"page": {"isLastPage": True}, "thumbnails": {"illust": []}}

    monkeypatch.setattr(client, "_ajax", fake_ajax)
    return client


def test_the_feed_route_is_asked_without_a_mode(monkeypatch) -> None:
    captured: dict = {}
    client = _client_with(monkeypatch, captured)

    client.feed(page=2)

    assert captured["path"] == "/ajax/follow_latest/illust"
    assert "mode" not in captured["params"], (
        "mode=safe 会让这条路线回 400，关注页就永远是空的"
    )
    assert captured["params"]["p"] == 2


def test_the_feed_route_still_takes_a_page(monkeypatch) -> None:
    captured: dict = {}
    client = _client_with(monkeypatch, captured)

    client.feed(page=0)

    assert captured["params"]["p"] == 1, "页码至少是 1"
