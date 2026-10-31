"""Tests for the Bilibili client (no network -- the transport is faked)."""
from __future__ import annotations

import hashlib
import urllib.parse

import pytest

from src.core.bilibili import (
    BILI_REFERER,
    BILI_RIDS,
    BILI_UA,
    BiliClient,
    mixin_key,
    sign_params,
)

NAV_PAYLOAD = {
    "code": -101,
    "message": "账号未登录",
    "data": {
        "isLogin": False,
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        },
        "ip_region": "CN",
    },
}

SPI_PAYLOAD = {
    "code": 0,
    "data": {"b_3": "B3-DEV", "b_4": "B4-DEV"},
}

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
            },
            {
                "aid": 2,
                "bvid": "BV1xx411c7mD",
                "title": "第二支",
                "author": "UP2",
                "pic": "http://i0.hdslb.com/bfs/archive/second.jpg",
                "duration": "1:02:03",
                "play": 7,
                "typename": "",
                "typeid": "4",
            },
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
        "owner": {"mid": 1943648897, "name": "Oo柯蟠桃oO"},
        "stat": {"view": 232, "like": 9},
    },
}

PLAYURL_PAYLOAD = {
    "code": 0,
    "data": {
        "quality": 32,
        "dash": {
            "audio": [
                {
                    "id": 30216,
                    "bandwidth": 65632,
                    "baseUrl": "https://upos.example/low.m4s",
                    "base_url": "https://upos.example/low-alt.m4s",
                    "mimeType": "audio/mp4",
                },
                {
                    "id": 30280,
                    "bandwidth": 192000,
                    "baseUrl": "https://upos.example/high.m4s",
                    "baseUrl_missing": None,
                    "mimeType": "audio/mp4",
                },
            ]
        },
    },
}

PLAYURL_DURL_PAYLOAD = {
    "code": 0,
    "data": {
        "quality": 32,
        "durl": [{"url": "http://upos.example/durl.flv", "size": 1024}],
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
                "tname": "手机游戏",
                "owner": {"name": "热门UP"},
                "stat": {"view": 999},
            }
        ]
    },
}

RANKING_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {
                "aid": 22,
                "bvid": "BV1rank2222",
                "title": "排行一",
                "pic": "http://i1.hdslb.com/bfs/archive/rank.jpg",
                "duration": 200,
                "tid": 28,
                "tname": "音乐",
                "stat": {"view": 888},
            }
        ]
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
    """Answers the few endpoints the client touches.

    It deliberately has no ``post``/``put``/``delete``: if the client ever tries
    to write something, the test fails with an AttributeError instead of
    silently hitting the real API.
    """

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


def make_client(routes=None, default=None, cookie=""):
    session = FakeSession(routes, default)
    return BiliClient(session=session, cookie=cookie), session


# ---------------------------------------------------------------------------
# WBI signature
# ---------------------------------------------------------------------------

def test_mixin_key_follows_the_official_permutation():
    img = "7cd084941338484aae1ad9425b84077c"
    sub = "4932caff0ff746eab6f01bf08b70ac45"
    assert mixin_key(img, sub) == "ea1db124af3c7062474693fa704f4ff8"
    assert len(mixin_key(img, sub)) == 32


def test_mixin_key_is_empty_for_short_input():
    assert mixin_key("abc", "de") == ""


def test_the_client_builds_the_mixin_key_from_the_right_key_pair():
    """The nav answer carries two urls; the image key comes first.

    ``mixin_key`` and ``sign_params`` are each tested on their own, and
    both pass when the client hands them the two keys in the wrong
    order -- the signature is then wrong and the server answers -403
    while every unit test stays green.  So the wiring is checked here:
    the key the client builds must be the key the standalone function
    produces from the same pair.
    """
    client = BiliClient()
    img_url = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
    sub_url = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"

    client._mixin = ""
    client._nav_keys = lambda: ("7cd084941338484aae1ad9425b84077c",
                                 "4932caff0ff746eab6f01bf08b70ac45")

    built = client._ensure_mixin_key()
    expected = mixin_key("7cd084941338484aae1ad9425b84077c",
                          "4932caff0ff746eab6f01bf08b70ac45")
    assert built == expected
    # Swapping the two keys gives a different key: that is the whole
    # point of the check.
    assert built != mixin_key("4932caff0ff746eab6f01bf08b70ac45",
                               "7cd084941338484aae1ad9425b84077c")
    assert img_url.endswith(".png") and sub_url.endswith(".png")


def test_nav_keys_take_the_image_key_from_the_image_url():
    """The two nav urls must not be mixed up when they are read."""
    client = BiliClient()
    payload = {
        "code": 0,
        "data": {
            "isLogin": False,
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/aaaabbbbcccc.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/dddd1111eeee.png",
            },
        },
    }
    client._get = lambda path, params=None: payload
    img, sub = client._nav_keys()
    assert img == "aaaabbbbcccc"
    assert sub == "dddd1111eeee"
    assert img != sub


def test_sign_params_matches_an_independent_implementation():
    key = mixin_key("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45")
    params = {"keyword": "晴天", "page": 1, "name": "a!b'c(d)e*f"}
    signed = sign_params(params, key, wts=1700000000)

    assert signed["wts"] == 1700000000
    assert len(signed["w_rid"]) == 32

    # Rebuild the query the way the server does it: sorted keys, the five
    # characters stripped from the values, encodeURIComponent semantics.
    stripped = {"keyword": "晴天", "page": "1", "name": "abcdef", "wts": "1700000000"}
    query = "&".join(
        f"{name}={urllib.parse.quote(stripped[name], safe='')}" for name in sorted(stripped)
    )
    assert signed["w_rid"] == hashlib.md5((query + key).encode()).hexdigest()


def test_sign_params_keeps_the_original_parameters():
    signed = sign_params({"a": 1}, "k", wts=5)
    assert signed["a"] == 1
    assert set(signed) == {"a", "wts", "w_rid"}


# ---------------------------------------------------------------------------
# session / identity
# ---------------------------------------------------------------------------

def test_cookie_parsing_and_login_flag():
    client, _ = make_client(cookie="SESSDATA=abc; bili_jct=def")
    assert client.is_logged_in is True
    client.set_cookie("bili_jct=def")
    assert client.is_logged_in is False


def test_anonymous_client_works_without_cookies():
    client, _ = make_client()
    assert client.is_logged_in is False


def test_buvid_is_fetched_once_and_reused():
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
    })
    client.search("晴天")
    client.search("晴天", page=2)
    spi_calls = [c for c in session.calls if "finger/spi" in c["url"]]
    assert len(spi_calls) == 1
    assert session.jar.cookies["buvid3"] == "B3-DEV"
    assert session.jar.cookies["buvid4"] == "B4-DEV"


def test_nav_keys_are_cached_after_the_first_use():
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    })
    client.search("晴天")
    client.search("晴天", page=2)
    nav_calls = [c for c in session.calls if c["url"].endswith("/nav")]
    assert len(nav_calls) == 1


def test_request_headers_carry_referer_origin_and_ua():
    client, session = make_client({"/x/web-interface/nav": NAV_PAYLOAD})
    client.nav()
    headers = session.calls[-1]["headers"]
    assert headers["Referer"] == BILI_REFERER
    assert headers["Origin"] == "https://www.bilibili.com"
    assert headers["User-Agent"] == BILI_UA
    assert "Accept" in headers


# ---------------------------------------------------------------------------
# nav
# ---------------------------------------------------------------------------

def test_nav_returns_code_and_wbi_images():
    client, _ = make_client({"/x/web-interface/nav": NAV_PAYLOAD})
    result = client.nav()
    assert result["code"] == -101
    assert result["wbi_img"]["img_url"].endswith("7cd084941338484aae1ad9425b84077c.png")
    assert result["wbi_img"]["sub_url"].endswith("4932caff0ff746eab6f01bf08b70ac45.png")


def test_nav_survives_a_network_error():
    client, _ = make_client({"/x/web-interface/nav": ConnectionError("boom")})
    result = client.nav()
    assert result["code"] == -1
    assert result["wbi_img"] == {"img_url": "", "sub_url": ""}


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_normalises_the_list_items():
    client, _ = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    })
    items = client.search("晴天")
    assert len(items) == 2
    first = items[0]
    assert first == {
        "source": "bilibili",
        "id": "BV1EHbhz6EHw",
        "bvid": "BV1EHbhz6EHw",
        "aid": 115021136794799,
        "title": "手搓降调版晴天伴奏",
        "author": "Oo柯蟠桃oO",
        "cover": "https://i2.hdslb.com/bfs/archive/cover.jpg",
        "duration": 294,
        "play_count": 232,
        "category": "原创音乐",
        "url": "https://www.bilibili.com/video/BV1EHbhz6EHw",
    }
    assert items[1]["duration"] == 3723
    assert items[1]["cover"] == "https://i0.hdslb.com/bfs/archive/second.jpg"
    assert items[1]["category"] == "游戏"


def test_search_sends_the_wbi_signature():
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    })
    client.search("晴天")
    call = [c for c in session.calls if "search/type" in c["url"]][-1]
    assert "w_rid" in call["params"]
    assert "wts" in call["params"]
    assert call["params"]["search_type"] == "video"
    assert call["params"]["keyword"] == "晴天"
    assert call["params"]["page"] == 1


def test_search_filters_by_partition():
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    })
    client.search("晴天", category="music")
    call = [c for c in session.calls if "search/type" in c["url"]][-1]
    assert call["params"]["tids"] == BILI_RIDS["music"] == 28


def test_search_without_partition_sends_no_tids():
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    })
    client.search("晴天", category="all")
    call = [c for c in session.calls if "search/type" in c["url"]][-1]
    assert "tids" not in call["params"]


def test_search_returns_an_empty_list_on_failure():
    client, _ = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": {"code": -412, "message": "请求被拦截"},
    })
    assert client.search("晴天") == []


def test_search_returns_an_empty_list_when_the_network_dies():
    client, _ = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": ConnectionError("boom"),
    })
    assert client.search("晴天") == []


def test_search_with_an_empty_keyword_never_calls_out():
    client, session = make_client()
    assert client.search("   ") == []
    assert session.calls == []


# ---------------------------------------------------------------------------
# video
# ---------------------------------------------------------------------------

def test_video_returns_the_detail_fields():
    client, _ = make_client({"/x/web-interface/view": VIEW_PAYLOAD})
    detail = client.video("BV1EHbhz6EHw")
    assert detail["bvid"] == "BV1EHbhz6EHw"
    assert detail["cid"] == 25841439697
    assert detail["pubdate"] == 1755083508
    assert detail["description"] == "简介内容"
    assert detail["author"] == "Oo柯蟠桃oO"
    assert detail["duration"] == 294
    assert detail["play_count"] == 232
    assert detail["cover"] == "https://i0.hdslb.com/bfs/archive/cover.jpg"
    assert detail["url"] == "https://www.bilibili.com/video/BV1EHbhz6EHw"


def test_video_falls_back_to_the_partition_name():
    client, _ = make_client({"/x/web-interface/view": VIEW_PAYLOAD})
    assert client.video("BV1EHbhz6EHw")["category"] == "音乐"


def test_video_returns_an_empty_dict_on_failure():
    client, _ = make_client({"/x/web-interface/view": {"code": 61001, "message": "稿件不存在"}})
    assert client.video("BV1nope") == {}


def test_video_with_an_empty_bvid_never_calls_out():
    client, session = make_client()
    assert client.video("") == {}
    assert session.calls == []


# ---------------------------------------------------------------------------
# audio url
# ---------------------------------------------------------------------------

def test_audio_url_picks_the_highest_bandwidth_audio_track():
    client, _ = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": PLAYURL_PAYLOAD,
    })
    result = client.audio_url("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["url"] == "https://upos.example/high.m4s"
    assert result["mime"] == "audio/mp4"
    assert result["need_referer"] is True
    assert result["error_key"] == ""
    assert result["quality"] == 30280


def test_audio_url_falls_back_to_durl_and_forces_https():
    client, _ = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": PLAYURL_DURL_PAYLOAD,
    })
    result = client.audio_url("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["url"] == "https://upos.example/durl.flv"


def test_audio_url_reports_a_failure_instead_of_raising():
    client, _ = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": {"code": -403, "message": "访问权限不足"},
    })
    result = client.audio_url("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["url"] == ""
    assert result["error_key"] == "api_error"
    assert result["message"] == "访问权限不足"


def test_audio_url_reports_a_failure_when_the_view_fails():
    client, _ = make_client({"/x/web-interface/view": {"code": 61001}})
    result = client.audio_url("BV1nope")
    assert result["success"] is False
    assert result["error_key"] == "no_cid"


def test_audio_url_reports_a_failure_when_the_network_dies():
    client, _ = make_client({"/x/web-interface/view": ConnectionError("boom")})
    result = client.audio_url("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["error_key"] == "no_cid"


def test_audio_url_with_an_empty_bvid_is_rejected():
    client, session = make_client()
    result = client.audio_url("")
    assert result["success"] is False
    assert result["error_key"] == "invalid_bvid"
    assert session.calls == []


def test_audio_url_signs_the_playurl_request():
    client, session = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": PLAYURL_PAYLOAD,
    })
    client.audio_url("BV1EHbhz6EHw")
    call = [c for c in session.calls if "playurl" in c["url"]][-1]
    assert "w_rid" in call["params"]
    assert call["params"]["cid"] == 25841439697
    assert call["params"]["bvid"] == "BV1EHbhz6EHw"


# ---------------------------------------------------------------------------
# popular / ranking
# ---------------------------------------------------------------------------

def test_popular_normalises_the_list():
    client, session = make_client({"/x/web-interface/popular": POPULAR_PAYLOAD})
    items = client.popular(rid=0)
    assert len(items) == 1
    item = items[0]
    assert item["bvid"] == "BV1popular11"
    assert item["author"] == "热门UP"
    assert item["play_count"] == 999
    assert item["cover"] == "https://i1.hdslb.com/bfs/archive/pop.jpg"
    assert item["category"] == "手机游戏"
    call = [c for c in session.calls if "popular" in c["url"]][-1]
    assert call["params"]["pn"] == 1
    assert call["params"]["ps"] == 30


def test_popular_of_a_music_partition_uses_the_partition_feed():
    """``/popular`` ignores the partition, so the music areas need the search."""
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    })
    items = client.popular(rid=28)
    assert items and items[0]["bvid"] == "BV1EHbhz6EHw"
    call = [c for c in session.calls if "search/type" in c["url"]][-1]
    assert call["params"]["tids"] == 28
    assert call["params"]["order"] == "click"


def test_popular_returns_an_empty_list_on_failure():
    client, _ = make_client({"/x/web-interface/popular": {"code": -400}})
    assert client.popular(rid=0) == []


def test_ranking_requires_the_partition_id():
    client, session = make_client({"/x/web-interface/ranking/v2": RANKING_PAYLOAD})
    items = client.ranking(28)
    assert len(items) == 1
    assert items[0]["bvid"] == "BV1rank2222"
    assert items[0]["play_count"] == 888
    assert items[0]["category"] == "音乐"
    call = [c for c in session.calls if "ranking" in c["url"]][-1]
    assert call["params"]["rid"] == 28


def test_ranking_falls_back_to_the_partition_feed_when_the_board_refuses():
    """The music areas answer ``-400``/``-352`` on the ranking board."""
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/ranking/v2": {"code": -352, "message": "风控拦截"},
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
    })
    items = client.ranking(28)
    assert items and items[0]["bvid"] == "BV1EHbhz6EHw"
    call = [c for c in session.calls if "search/type" in c["url"]][-1]
    assert call["params"]["tids"] == 28


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,expected", [
    ("nav", {"code", "wbi_img"}),
    ("search", {"source", "id", "bvid", "aid", "title", "author", "cover",
                "duration", "play_count", "category", "url"}),
    ("video", {"source", "id", "bvid", "aid", "title", "author", "cover",
               "duration", "play_count", "category", "url", "cid", "pubdate",
               "description"}),
    ("audio_url", {"success", "url", "mime", "quality", "need_referer",
                   "error_key", "message"}),
])
def test_every_payload_carries_the_agreed_keys(method, expected):
    """The bridge binds to these field names -- they must not drift."""
    client, _ = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": PLAYURL_PAYLOAD,
        "/x/web-interface/popular": POPULAR_PAYLOAD,
        "/x/web-interface/ranking/v2": RANKING_PAYLOAD,
    })
    if method == "nav":
        payload = client.nav()
    elif method == "search":
        payload = client.search("晴天")[0]
    elif method == "video":
        payload = client.video("BV1EHbhz6EHw")
    else:
        payload = client.audio_url("BV1EHbhz6EHw")
    assert expected <= set(payload)


def test_the_client_only_ever_issues_get_requests():
    client, session = make_client({
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/wbi/search/type": SEARCH_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": PLAYURL_PAYLOAD,
        "/x/web-interface/popular": POPULAR_PAYLOAD,
        "/x/web-interface/ranking/v2": RANKING_PAYLOAD,
    })
    client.nav()
    client.search("晴天")
    client.video("BV1EHbhz6EHw")
    client.audio_url("BV1EHbhz6EHw")
    client.popular()
    client.ranking()
    assert session.calls
    for call in session.calls:
        assert call["url"].startswith("https://api.bilibili.com/")
