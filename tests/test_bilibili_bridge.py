"""Tests for the Bilibili bridge layer (no network -- the transport is faked).

The bridge is the piece the desktop frontend talks to, so these tests care
about the shape of its answers: ``success`` plus the fields the UI reads,
and a translated ``error_key`` when something goes wrong.
"""
from __future__ import annotations

import pytest

from src.core.bilibili import BiliClient
from src.gui.bilibili_bridge import BilibiliBridge

NAV_PAYLOAD = {
    "code": -101,
    "message": "账号未登录",
    "data": {
        "isLogin": False,
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        },
    },
}

SPI_PAYLOAD = {"code": 0, "data": {"b_3": "B3-DEV", "b_4": "B4-DEV"}}

SEARCH_PAYLOAD = {
    "code": 0,
    "data": {
        "result": [
            {
                "aid": 115021136794799,
                "bvid": "BV1EHbhz6EHw",
                "title": "手搓降调版<em class=\"keyword\">晴天</em>伴奏",
                "author": "Oo柯蟠桃oO",
                "pic": "//i2.hdslb.com/bfs/archive/cover.jpg",
                "duration": "4:54",
                "play": 232,
                "typename": "原创音乐",
                "typeid": "28",
            }
        ]
    },
}

POPULAR_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {
                "aid": 11,
                "bvid": "BV1popular11",
                "title": "热门一",
                "pic": "//i1.hdslb.com/bfs/archive/pop.jpg",
                "duration": 100,
                "tid": 28,
                "tname": "音乐",
                "owner": {"name": "热门UP"},
                "stat": {"view": 999},
            }
        ]
    },
}

VIEW_PAYLOAD = {
    "code": 0,
    "data": {
        "bvid": "BV1EHbhz6EHw",
        "aid": 115021136794799,
        "cid": 25841439697,
        "title": "手搓降调版晴天伴奏",
        "pic": "http://i0.hdslb.com/bfs/archive/cover.jpg",
        "duration": 294,
        "pubdate": 1755083508,
        "desc": "简介内容",
        "tid": 28,
        "tname": "",
        "owner": {"mid": 1, "name": "Oo柯蟠桃oO"},
        "stat": {"view": 232},
    },
}

PLAYURL_PAYLOAD = {
    "code": 0,
    "data": {
        "quality": 32,
        "dash": {
            "audio": [
                {"id": 30216, "bandwidth": 65632,
                 "baseUrl": "https://upos.example/low.m4s", "mimeType": "audio/mp4"},
                {"id": 30280, "bandwidth": 192000,
                 "baseUrl": "https://upos.example/high.m4s", "mimeType": "audio/mp4"},
            ]
        },
    },
}


class FakeJar:
    """Minimal stand-in for a requests cookie jar."""

    def __init__(self):
        self.cookies: dict[str, str] = {}

    def set(self, name, value, domain=None):
        self.cookies[name] = value


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Answers the few endpoints the client touches -- no network."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default
        self.jar = FakeJar()
        self.cookies = self.jar
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": headers,
                           "timeout": timeout})
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        if self.default is not None:
            if isinstance(self.default, Exception):
                raise self.default
            return FakeResponse(self.default)
        return FakeResponse({"code": -404, "message": "no route"}, status_code=404)


ROUTES = {
    "/x/frontend/finger/spi": SPI_PAYLOAD,
    "/x/web-interface/nav": NAV_PAYLOAD,
    "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    "/x/web-interface/popular": POPULAR_PAYLOAD,
    "/x/web-interface/view": VIEW_PAYLOAD,
    "/x/player/wbi/playurl": PLAYURL_PAYLOAD,
}


def make_bridge(routes=None, default=None):
    """A bridge whose client talks to a fake session."""
    session = FakeSession(routes if routes is not None else ROUTES, default)
    return BilibiliBridge(client=BiliClient(session=session)), session


def search_call(session):
    return [call for call in session.calls if "search/type" in call["url"]][-1]


# ---------------------------------------------------------------------- search

def test_search_returns_the_items_and_the_count():
    bridge, _ = make_bridge()
    result = bridge.search("晴天")
    assert result["success"] is True
    assert result["keyword"] == "晴天"
    assert result["count"] == 1
    item = result["items"][0]
    assert item["bvid"] == "BV1EHbhz6EHw"
    assert item["title"] == "手搓降调版晴天伴奏"
    assert item["author"] == "Oo柯蟠桃oO"
    assert item["cover"] == "https://i2.hdslb.com/bfs/archive/cover.jpg"
    assert item["duration"] == 294
    assert item["play_count"] == 232


def test_search_rejects_an_empty_keyword_without_calling_out():
    bridge, session = make_bridge()
    result = bridge.search("   ")
    assert result["success"] is False
    assert result["error_key"] == "player.search_empty"
    assert session.calls == []


def test_search_maps_a_partition_id_onto_the_partition_key():
    bridge, session = make_bridge()
    bridge.search("晴天", category="28")
    assert search_call(session)["params"]["tids"] == 28


def test_search_maps_a_partition_name_onto_the_partition_key():
    bridge, session = make_bridge()
    bridge.search("晴天", category="music")
    assert search_call(session)["params"]["tids"] == 28


def test_search_refuses_an_unknown_partition():
    bridge, session = make_bridge()
    result = bridge.search("晴天", category="nonsense")
    assert result["success"] is False
    assert result["error_key"] == "error.unknown"
    assert session.calls == []


def test_search_maps_the_sort_names_onto_the_api_orders():
    bridge, session = make_bridge()
    bridge.search("晴天", order="play")
    assert search_call(session)["params"]["order"] == "click"
    bridge.search("晴天", order="date")
    assert search_call(session)["params"]["order"] == "pubdate"
    bridge.search("晴天", order="default")
    assert search_call(session)["params"]["order"] == "totalrank"


def test_search_passes_the_page_through():
    bridge, session = make_bridge()
    result = bridge.search("晴天", page=3, page_size=20)
    assert result["page"] == 3
    assert result["page_size"] == 20
    params = search_call(session)["params"]
    assert params["page"] == 3
    assert params["page_size"] == 20


def test_search_clamps_the_page_size():
    bridge, session = make_bridge()
    result = bridge.search("晴天", page_size=500)
    assert result["page_size"] == 50
    assert search_call(session)["params"]["page_size"] == 50


def test_search_survives_a_dead_network():
    bridge, _ = make_bridge(routes={}, default=ConnectionError("boom"))
    result = bridge.search("晴天")
    assert result["success"] is True
    assert result["items"] == []
    assert result["count"] == 0


def test_search_survives_an_api_refusal():
    bridge, _ = make_bridge({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": {"code": -412, "message": "请求被拦截"},
    })
    result = bridge.search("晴天")
    assert result["success"] is True
    assert result["items"] == []


# ----------------------------------------------------------------- partitions

def test_partitions_list_the_id_and_the_name_of_each_one():
    bridge, _ = make_bridge()
    result = bridge.partitions()
    assert result["success"] is True
    partitions = result["partitions"]
    assert result["count"] == len(partitions)
    by_key = {item["key"]: item for item in partitions}
    assert by_key["music"]["rid"] == 28
    assert by_key["music"]["name"] == "音乐"
    assert by_key["all"]["rid"] == 0
    assert by_key["music_video"]["rid"] == 29


def test_partitions_never_call_out():
    bridge, session = make_bridge()
    bridge.partitions()
    assert session.calls == []


# -------------------------------------------------------------------- popular

def test_popular_of_the_whole_site():
    bridge, session = make_bridge()
    result = bridge.popular(rid=0)
    assert result["success"] is True
    assert result["rid"] == 0
    assert result["count"] == 1
    assert result["items"][0]["bvid"] == "BV1popular11"
    call = [c for c in session.calls if "popular" in c["url"]][-1]
    assert call["params"]["pn"] == 1
    assert call["params"]["ps"] == 30


def test_popular_of_the_music_partition_uses_the_partition_feed():
    bridge, session = make_bridge()
    result = bridge.popular(rid=28)
    assert result["success"] is True
    assert result["items"][0]["bvid"] == "BV1EHbhz6EHw"
    params = search_call(session)["params"]
    assert params["tids"] == 28
    assert params["order"] == "click"


def test_popular_paginates():
    bridge, session = make_bridge()
    result = bridge.popular(rid=0, page=2, page_size=10)
    assert result["page"] == 2
    assert result["page_size"] == 10
    call = [c for c in session.calls if "popular" in c["url"]][-1]
    assert call["params"]["pn"] == 2
    assert call["params"]["ps"] == 10


def test_popular_refuses_an_unknown_partition():
    bridge, session = make_bridge()
    result = bridge.popular(rid=999)
    assert result["success"] is False
    assert result["error_key"] == "error.unknown"
    assert session.calls == []


def test_popular_survives_a_dead_network():
    bridge, _ = make_bridge(routes={}, default=ConnectionError("boom"))
    result = bridge.popular(rid=0)
    assert result["success"] is True
    assert result["items"] == []


# --------------------------------------------------------------------- audio

def test_audio_url_resolves_the_stream():
    bridge, _ = make_bridge()
    result = bridge.audio_url("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["url"] == "https://upos.example/high.m4s"
    assert result["mime"] == "audio/mp4"
    assert result["quality"] == 30280
    assert result["need_referer"] is True
    assert result["bvid"] == "BV1EHbhz6EHw"


def test_audio_url_translates_an_api_failure():
    bridge, _ = make_bridge({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": {"code": -403, "message": "访问权限不足"},
    })
    result = bridge.audio_url("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["error_key"] == "error.unknown"
    assert result["reason"] == "api_error"
    assert result["message"] == "访问权限不足"


def test_every_error_key_exists_in_the_catalogue(fresh_i18n):
    """A key that is not in the catalogue shows up raw in the UI."""
    bridge, _ = make_bridge()
    failing, _ = make_bridge({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": {"code": -403, "message": "访问权限不足"},
    })
    failures = [
        bridge.search(""),
        bridge.search("晴天", category="nonsense"),
        bridge.popular(rid=999),
        bridge.audio_url(""),
        failing.audio_url("BV1EHbhz6EHw"),
    ]
    for failure in failures:
        assert failure["success"] is False
        assert failure["error_key"] in fresh_i18n.get_flat_translations()


def test_audio_url_translates_a_missing_cid():
    bridge, _ = make_bridge({"/x/web-interface/view": {"code": 61001}})
    result = bridge.audio_url("BV1nope")
    assert result["success"] is False
    assert result["error_key"] == "error.reason.song_not_found"
    assert result["reason"] == "no_cid"


def test_audio_url_refuses_an_empty_bvid_without_calling_out():
    bridge, session = make_bridge()
    result = bridge.audio_url("")
    assert result["success"] is False
    assert result["error_key"] == "error.reason.song_not_found"
    assert result["reason"] == "invalid_bvid"
    assert session.calls == []


def test_audio_url_survives_a_dead_network():
    bridge, _ = make_bridge(routes={}, default=ConnectionError("boom"))
    result = bridge.audio_url("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["error_key"] == "error.reason.song_not_found"


# ----------------------------------------------------------- the desktop side

def test_the_bridge_methods_delegate_to_the_source(bridge):
    """The GuiBridge methods must not re-implement the source."""
    from src.gui.bilibili_bridge import BilibiliBridge as Source

    assert isinstance(bridge.bilibili, Source)

    bridge.bilibili, session = make_bridge()
    result = bridge.search_bilibili_tracks("晴天", category="music", order="play")
    assert result["success"] is True
    assert result["count"] == 1
    assert search_call(session)["params"]["tids"] == 28

    bridge.bilibili, session = make_bridge()
    assert bridge.get_bilibili_partitions()["success"] is True

    bridge.bilibili, session = make_bridge()
    assert bridge.get_bilibili_popular(rid=0)["count"] == 1

    bridge.bilibili, session = make_bridge()
    assert bridge.get_bilibili_audio_url("BV1EHbhz6EHw")["url"] == "https://upos.example/high.m4s"


def test_the_bridge_methods_add_tracks_in_the_player_shape(bridge):
    bridge.bilibili, _ = make_bridge()
    result = bridge.search_bilibili_tracks("晴天")
    track = result["tracks"][0]
    assert track["id"] == "BV1EHbhz6EHw"
    assert track["key"] == "bilibili:BV1EHbhz6EHw"
    assert track["source"] == "bilibili"
    assert track["name"] == "手搓降调版晴天伴奏"
    assert track["artists"] == "Oo柯蟠桃oO"
    assert track["album"] == "原创音乐"
    assert track["cover_url"] == "https://i2.hdslb.com/bfs/archive/cover.jpg"
    assert track["duration"] == 294
    assert track["play_count"] == 232
    assert track["url"] == "https://www.bilibili.com/video/BV1EHbhz6EHw"


def test_the_bridge_methods_keep_the_failure_payload(bridge):
    bridge.bilibili, _ = make_bridge()
    result = bridge.search_bilibili_tracks("")
    assert result["success"] is False
    assert result["error_key"] == "player.search_empty"
    assert result["tracks"] == []


def test_every_bridge_method_answers_a_dictionary(bridge):
    """The webview can only carry JSON -- every answer must be a dict."""
    bridge.bilibili, _ = make_bridge()
    answers = [
        bridge.search_bilibili_tracks("晴天"),
        bridge.get_bilibili_partitions(),
        bridge.get_bilibili_popular(rid=28),
        bridge.get_bilibili_audio_url("BV1EHbhz6EHw"),
    ]
    for answer in answers:
        assert isinstance(answer, dict)
        assert "success" in answer


@pytest.mark.parametrize("method,arguments", [
    ("search", ("晴天",)),
    ("partitions", ()),
    ("popular", (28,)),
    ("audio_url", ("BV1EHbhz6EHw",)),
])
def test_the_source_methods_exist_on_the_bridge(method, arguments):
    source, _ = make_bridge()
    assert callable(getattr(source, method))
    answer = getattr(source, method)(*arguments)
    assert isinstance(answer, dict)
    assert "success" in answer
