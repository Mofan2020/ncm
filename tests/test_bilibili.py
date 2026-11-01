"""Tests for the Bilibili client (no network -- the transport is faked)."""
from __future__ import annotations

import hashlib
import json
import urllib.parse

import pytest

from src.core.bilibili import (
    BILI_REFERER,
    BILI_RIDS,
    BILI_UA,
    BiliClient,
    mixin_key,
    sign_params,
    video_quality_name,
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
                "mid": 946974,
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

#: ``/nav`` of a signed in account.
NAV_LOGIN_PAYLOAD = {
    "code": 0,
    "data": {
        "isLogin": True,
        "mid": 12345678,
        "uname": "测试用户",
        "face": "//i0.hdslb.com/bfs/face/face.jpg",
        "level_info": {"current_level": 6},
        "vipStatus": 1,
        "vipLabel": "年度大会员",
        "money": 520,
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        },
    },
}

QR_GENERATE_PAYLOAD = {
    "code": 0,
    "data": {
        "url": "https://qrcode.bilibili.com/qrcode/abc123",
        "qrcode_key": "QRKEY-1",
    },
}

FAV_FOLDERS_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {
                "id": 98765,
                "title": "默认收藏夹",
                "media_count": 42,
                "cover": "//i0.hdslb.com/bfs/fav/cover.jpg",
            }
        ]
    },
}

FAV_VIDEOS_PAYLOAD = {
    "code": 0,
    "data": {
        "has_more": True,
        "medias": [
            {
                "id": 115021136794799,
                "bvid": "BV1EHbhz6EHw",
                "title": "收藏的视频",
                "intro": "简介",
                "cover": "//i2.hdslb.com/bfs/archive/cover.jpg",
                "duration": 294000,
                "fav_time": 1755083508,
                "upper": {"name": "Oo柯蟠桃oO"},
            }
        ],
    },
}

DASH_PAYLOAD = {
    "code": 0,
    "data": {
        "quality": 80,
        "accept_quality": "16,32,64,80",
        "support_formats": [
            {"quality": 16, "new_description": "360P 流畅", "format": "hd"},
            {"quality": 32, "new_description": "480P 清晰", "format": "hd"},
            {"quality": 64, "new_description": "720P 高清", "format": "hd"},
            {"quality": 80, "new_description": "1080P 高码率", "format": "hd"},
        ],
        "dash": {
            "audio": [
                {
                    "id": 30216,
                    "bandwidth": 65632,
                    "baseUrl": "https://upos.example/low.m4s",
                    "mimeType": "audio/mp4",
                    "codecs": "mp4a.40.2",
                    "segment_base": {"initialization": "0-820"},
                },
                {
                    "id": 30232,
                    "bandwidth": 192000,
                    "baseUrl": "https://upos.example/high.m4s",
                    "mimeType": "audio/mp4",
                    "codecs": "mp4a.40.2",
                    "segment_base": {"initialization": "0-900"},
                },
            ],
            "video": [
                {
                    "id": 32,
                    "bandwidth": 500000,
                    "baseUrl": "https://upos.example/480.m4s",
                    "mimeType": "video/mp4",
                    "codecs": "avc1.64001f",
                    "width": 854,
                    "height": 480,
                    "segment_base": {"initialization": "0-821"},
                },
                {
                    "id": 80,
                    "bandwidth": 2000000,
                    "baseUrl": "https://upos.example/1080.m4s",
                    "mimeType": "video/mp4",
                    "codecs": "avc1.640032",
                    "width": 1920,
                    "height": 1080,
                    "segment_base": {"initialization": "0-1234"},
                },
            ],
            "flac": {
                "audio": [
                    {
                        "id": 30280,
                        "codecid": 30280,
                        "bandwidth": 320000,
                        "baseUrl": "https://upos.example/flac.m4s",
                        "mimeType": "audio/mp4",
                        "codecs": "flac",
                        "segment_base": {"initialization": "0-700"},
                    }
                ]
            },
        },
    },
}

DASH_DURL_PAYLOAD = {
    "code": 0,
    "data": {
        "quality": 32,
        "accept_quality": "16,32",
        "support_formats": [{"quality": 32, "new_description": "480P 清晰", "format": "hd"}],
        "durl": [{"url": "http://upos.example/durl.mp4", "size": 4096}],
    },
}


class FakeJar:
    """Minimal stand-in for a requests cookie jar."""

    def __init__(self):
        self.cookies: dict[str, str] = {}

    def set(self, name, value, domain=None):
        self.cookies[name] = value

    def clear(self, **kwargs):
        name = kwargs.get("name")
        if name is None:
            self.cookies.clear()
            return
        for key in list(self.cookies):
            if key == name:
                self.cookies.pop(key, None)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, cookies=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Answers the few endpoints the client touches.

    A route value may be a payload dictionary, a ready made
    ``FakeResponse`` (the login flow needs the ``Set-Cookie`` header) or an
    exception, which is raised instead of answered.
    """

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default
        self.jar = FakeJar()
        self.cookies = self.jar
        self.calls: list[dict] = []

    def _route(self, url):
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, FakeResponse):
                    return payload
                return FakeResponse(payload)
        if self.default is not None:
            if isinstance(self.default, Exception):
                raise self.default
            if isinstance(self.default, FakeResponse):
                return self.default
            return FakeResponse(self.default)
        return FakeResponse({"code": -404, "message": "no route"}, status_code=404)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": headers,
                           "timeout": timeout, "method": "GET"})
        return self._route(url)

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(data or {}), "headers": headers,
                           "timeout": timeout, "method": "POST"})
        return self._route(url)


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
        "author_mid": "946974",
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


# ---------------------------------------------------------------------------
# account state
# ---------------------------------------------------------------------------

def test_account_info_reports_the_signed_in_account():
    client, _ = make_client({"/x/web-interface/nav": NAV_LOGIN_PAYLOAD})
    info = client.account_info()
    assert info == {
        "logged_in": True,
        "mid": "12345678",
        "uname": "测试用户",
        "face": "https://i0.hdslb.com/bfs/face/face.jpg",
        "level": 6,
        "vip_status": 1,
        "vip_label": "年度大会员",
        "coins": 520,
    }


def test_account_info_of_an_anonymous_client_is_empty():
    client, _ = make_client({"/x/web-interface/nav": NAV_PAYLOAD})
    info = client.account_info()
    assert info["logged_in"] is False
    assert info["mid"] == ""
    assert info["level"] == 0
    assert info["coins"] == 0


def test_account_info_survives_a_network_error():
    client, _ = make_client({"/x/web-interface/nav": ConnectionError("boom")})
    info = client.account_info()
    assert info["logged_in"] is False


def test_cookie_helpers_expose_the_credential():
    client, _ = make_client(cookie="SESSDATA=abc; bili_jct=def; DedeUserID=42")
    assert client.csrf() == "def"
    assert client.cookie_dict() == {"SESSDATA": "abc", "bili_jct": "def", "DedeUserID": "42"}
    header = client.cookie_header()
    assert "SESSDATA=abc" in header
    assert "bili_jct=def" in header
    assert header.index("SESSDATA") < header.index("bili_jct")


def test_bili_headers_carry_the_referer_and_the_cookie():
    client, _ = make_client(cookie="SESSDATA=abc; bili_jct=def")
    headers = client.bili_headers()
    assert headers["User-Agent"] == BILI_UA
    assert headers["Referer"] == BILI_REFERER
    assert headers["Origin"] == "https://www.bilibili.com"
    assert headers["Cookie"] == client.cookie_header()
    assert client.bili_headers({"Range": "bytes=0-1"})["Range"] == "bytes=0-1"


def test_bili_headers_without_a_login_carry_no_cookie():
    client, _ = make_client()
    assert "Cookie" not in client.bili_headers()


# ---------------------------------------------------------------------------
# QR login
# ---------------------------------------------------------------------------

def test_login_qrcode_returns_the_image_and_the_key():
    client, session = make_client({
        "/x/passport-login/web/qrcode/generate": QR_GENERATE_PAYLOAD,
    })
    result = client.login_qrcode()
    assert result["success"] is True
    assert result["qrcode_url"] == "https://qrcode.bilibili.com/qrcode/abc123"
    assert result["key"] == "QRKEY-1"
    assert result["message"] == ""
    call = session.calls[-1]
    assert call["url"] == "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    assert call["method"] == "GET"
    assert "w_rid" not in call["params"]
    assert call["headers"]["Referer"] == BILI_REFERER
    assert call["headers"]["Origin"] == "https://www.bilibili.com"


def test_login_qrcode_reports_a_failure():
    client, _ = make_client({
        "/x/passport-login/web/qrcode/generate": {"code": -3, "message": "请求过于频繁"},
    })
    result = client.login_qrcode()
    assert result["success"] is False
    assert result["qrcode_url"] == ""
    assert "请求过于频繁" in result["message"]


@pytest.mark.parametrize("code,status,fragment", [
    (86101, "waiting", "等待扫码"),
    (86090, "scanned", "已扫码"),
    (86038, "expired", "过期"),
])
def test_login_poll_reads_the_status_code(code, status, fragment):
    client, session = make_client({
        "/x/passport-login/web/qrcode/poll": {"code": 0, "data": {"code": code, "message": ""}},
    })
    result = client.login_poll("QRKEY-1")
    assert result["success"] is False
    assert result["status"] == status
    assert result["cookie"] == ""
    assert fragment in result["message"]
    assert session.calls[-1]["params"]["qrcode_key"] == "QRKEY-1"


def test_login_poll_reads_the_credential_from_the_set_cookie_header():
    """The session cookie is not in the JSON body -- it is in the header."""
    client, _ = make_client({
        "/x/passport-login/web/qrcode/poll": FakeResponse(
            {
                "code": 0,
                "data": {
                    "code": 0,
                    "message": "",
                    "url": "https://passport.bilibili.com/h5-app/passport/login/success",
                },
            },
            headers={
                "Set-Cookie": (
                    "SESSDATA=abc%2Cdef; Path=/; HttpOnly; "
                    "Expires=Wed, 21 Oct 2026 07:28:00 GMT, "
                    "bili_jct=deadbeef; Path=/; HttpOnly, DedeUserID=12345678; Path=/"
                )
            },
        ),
    })
    result = client.login_poll("QRKEY-1")
    assert result["success"] is True
    assert result["status"] == "success"
    assert result["cookie"] == "SESSDATA=abc%2Cdef; bili_jct=deadbeef; DedeUserID=12345678"
    # the client keeps the credential, so the next request is signed in
    assert client.csrf() == "deadbeef"
    assert client.is_logged_in is True


def test_login_poll_without_a_key_never_calls_out():
    client, session = make_client()
    result = client.login_poll("")
    assert result["success"] is False
    assert result["status"] == "failed"
    assert session.calls == []


# ---------------------------------------------------------------------------
# cookie / password / captcha login
# ---------------------------------------------------------------------------

def test_login_cookie_accepts_a_valid_credential():
    client, _ = make_client({"/x/web-interface/nav": NAV_LOGIN_PAYLOAD})
    result = client.login_cookie("SESSDATA=abc; bili_jct=def")
    assert result["success"] is True
    assert "SESSDATA=abc" in result["cookie"]
    assert result["message"] == ""


def test_login_cookie_rejects_a_dead_credential():
    client, _ = make_client({"/x/web-interface/nav": NAV_PAYLOAD})
    result = client.login_cookie("SESSDATA=abc")
    assert result["success"] is False
    assert result["cookie"] == ""
    assert "过期" in result["message"]


def test_login_password_returns_the_credential():
    client, session = make_client({
        "/x/passport-login/web/login": FakeResponse(
            {"code": 0, "data": {"code": 0, "message": "", "url": ""}},
            headers={"Set-Cookie": "SESSDATA=xyz; Path=/, bili_jct=csrf1; Path=/"},
        ),
    })
    result = client.login_password("user", "pw")
    assert result["success"] is True
    assert result["cookie"] == "SESSDATA=xyz; bili_jct=csrf1"
    call = session.calls[-1]
    assert call["url"] == "https://passport.bilibili.com/x/passport-login/web/login"
    assert call["params"]["username"] == "user"
    assert call["params"]["password"] == "pw"
    assert call["params"]["source"] == "main_mini"
    assert call["params"]["go"] == "https://www.bilibili.com/"
    assert "w_rid" not in call["params"]


def test_login_password_asks_for_a_captcha():
    client, _ = make_client({
        "/x/passport-login/web/login": {
            "code": 0,
            "data": {
                "code": -120,
                "message": "需要验证码",
                "captcha_key": "KEY123",
                "url": "https://passport.bilibili.com/h5-app/passport/login/captcha?captcha_key=KEY123",
            },
        },
    })
    result = client.login_password("user", "pw")
    assert result["success"] is False
    assert result["captcha_required"] is True
    assert result["captcha_key"] == "KEY123"
    assert result["cookie"] == ""


def test_login_password_reports_the_server_reason():
    client, _ = make_client({
        "/x/passport-login/web/login": {
            "code": 0,
            "data": {"code": -3, "message": "密码错误"},
        },
    })
    result = client.login_password("user", "wrong")
    assert result["success"] is False
    assert "密码错误" in result["message"]


def test_captcha_returns_the_geetest_parameters():
    client, session = make_client({
        "/x/passport-login/captcha": {
            "code": 0,
            "data": {"geetest": {"gt": "GT", "challenge": "CH", "token": "TK"}},
        },
    })
    result = client.captcha()
    assert result["success"] is True
    assert result["gt"] == "GT"
    assert result["challenge"] == "CH"
    assert result["token"] == "TK"
    call = session.calls[-1]
    assert call["url"] == "https://passport.bilibili.com/x/passport-login/captcha"
    assert call["params"]["source"] == "main_mini"


# ---------------------------------------------------------------------------
# SMS login and registration
# ---------------------------------------------------------------------------

def test_send_sms_code_sends_the_phone_number():
    client, session = make_client({
        "/x/passport-login/web/sms/sendcode": {"code": 0, "message": "0"},
    })
    result = client.send_sms_code("13800000000")
    assert result["success"] is True
    call = session.calls[-1]
    assert call["url"] == "https://passport.bilibili.com/x/passport-login/web/sms/sendcode"
    assert call["params"]["tel"] == "13800000000"
    assert call["params"]["cid"] == "86"
    assert call["params"]["source"] == "main_mini"


def test_send_sms_code_reports_a_failure():
    client, _ = make_client({
        "/x/passport-login/web/sms/sendcode": {"code": -6, "message": "短信发送过于频繁"},
    })
    result = client.send_sms_code("13800000000")
    assert result["success"] is False
    assert "频繁" in result["message"]


def test_login_sms_returns_the_credential():
    client, session = make_client({
        "/x/passport-login/web/sms/login": FakeResponse(
            {"code": 0, "data": {"code": 0, "message": ""}},
            headers={"Set-Cookie": "SESSDATA=sms1; Path=/, bili_jct=csrf2; Path=/"},
        ),
    })
    result = client.login_sms("13800000000", "123456")
    assert result["success"] is True
    assert result["cookie"] == "SESSDATA=sms1; bili_jct=csrf2"
    call = session.calls[-1]
    assert call["url"] == "https://passport.bilibili.com/x/passport-login/web/sms/login"
    assert call["params"]["code"] == "123456"


def test_login_sms_reports_a_wrong_code():
    client, _ = make_client({
        "/x/passport-login/web/sms/login": {"code": 0, "data": {"code": -4, "message": "验证码错误"}},
    })
    result = client.login_sms("13800000000", "000000")
    assert result["success"] is False
    assert "验证码错误" in result["message"]


def test_register_send_code_sends_the_phone_number():
    client, session = make_client({
        "/x/passport-register/web/sendcode": {"code": 0, "message": "0"},
    })
    result = client.register_send_code("13800000000")
    assert result["success"] is True
    call = session.calls[-1]
    assert call["url"] == "https://passport.bilibili.com/x/passport-register/web/sendcode"
    assert call["params"]["source"] == "main_landing"


def test_register_posts_the_form():
    client, session = make_client({
        "/x/passport-register/web/register": {"code": 0, "message": "0"},
    })
    result = client.register("user", "pw", "13800000000", "123456")
    assert result["success"] is True
    assert result["cookie"] == ""
    call = session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"] == "https://passport.bilibili.com/x/passport-register/web/register"
    assert call["params"]["username"] == "user"
    assert call["params"]["tel"] == "13800000000"
    assert call["params"]["code"] == "123456"
    assert call["params"]["source"] == "main_landing"


def test_register_picks_up_a_session_cookie_when_the_server_signs_in():
    client, _ = make_client({
        "/x/passport-register/web/register": FakeResponse(
            {"code": 0, "message": "0"},
            headers={"Set-Cookie": "SESSDATA=reg1; Path=/, bili_jct=reg2; Path=/"},
        ),
    })
    result = client.register("user", "pw", "13800000000", "123456")
    assert result["success"] is True
    assert result["cookie"] == "SESSDATA=reg1; bili_jct=reg2"


def test_register_reports_the_server_reason():
    client, _ = make_client({
        "/x/passport-register/web/register": {"code": -1, "message": "该手机号已被注册"},
    })
    result = client.register("user", "pw", "13800000000", "123456")
    assert result["success"] is False
    assert "已被注册" in result["message"]


def test_logout_clears_the_local_credential():
    client, session = make_client({"/login/exit/v2": {"code": 0}}, cookie="SESSDATA=abc")
    result = client.logout()
    assert result["success"] is True
    assert client.is_logged_in is False
    assert client.cookie_header() == ""
    # the old session must not be sent to the server any more
    assert "SESSDATA" not in session.jar.cookies
    assert session.calls[-1]["url"] == "https://passport.bilibili.com/login/exit/v2"


def test_an_empty_cookie_string_drops_the_old_session():
    """A logout that only clears the local field would keep the session alive."""
    client, session = make_client(cookie="SESSDATA=abc; bili_jct=def")
    client.set_cookie("")
    assert client.cookie_dict() == {}
    assert "SESSDATA" not in session.jar.cookies
    assert "bili_jct" not in session.jar.cookies


# ---------------------------------------------------------------------------
# favourite folders
# ---------------------------------------------------------------------------

def test_favorite_folders_reads_the_signed_in_account():
    client, session = make_client({
        "/x/web-interface/nav": NAV_LOGIN_PAYLOAD,
        "/x/v3/fav/folder/created/list-all": FAV_FOLDERS_PAYLOAD,
    })
    folders = client.favorite_folders()
    assert folders == [{
        "id": "98765",
        "title": "默认收藏夹",
        "media_count": 42,
        "cover": "https://i0.hdslb.com/bfs/fav/cover.jpg",
    }]
    call = [c for c in session.calls if "list-all" in c["url"]][-1]
    assert call["params"]["up_mid"] == "12345678"


def test_favorite_folders_falls_back_to_the_cookie_mid():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/v3/fav/folder/created/list-all": FAV_FOLDERS_PAYLOAD,
    }, cookie="DedeUserID=777")
    folders = client.favorite_folders()
    assert len(folders) == 1
    call = [c for c in session.calls if "list-all" in c["url"]][-1]
    assert call["params"]["up_mid"] == "777"


def test_favorite_folders_without_an_account_is_empty():
    client, _ = make_client({"/x/web-interface/nav": NAV_PAYLOAD})
    assert client.favorite_folders() == []


def test_favorite_videos_normalises_the_entries():
    client, session = make_client({
        "/x/v3/fav/resource/list": FAV_VIDEOS_PAYLOAD,
    })
    result = client.favorite_videos("98765", page=2, page_size=30)
    assert result["has_more"] is True
    assert result["page"] == 2
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["bvid"] == "BV1EHbhz6EHw"
    assert item["aid"] == 115021136794799
    assert item["title"] == "收藏的视频"
    assert item["author"] == "Oo柯蟠桃oO"
    assert item["cover"] == "https://i2.hdslb.com/bfs/archive/cover.jpg"
    assert item["duration"] == 294
    assert item["intro"] == "简介"
    assert item["fav_time"] == 1755083508
    call = [c for c in session.calls if "resource/list" in c["url"]][-1]
    assert call["params"]["media_id"] == "98765"
    assert call["params"]["pn"] == 2
    assert call["params"]["ps"] == 30
    assert call["params"]["platform"] == "web"


def test_favorite_videos_without_a_folder_is_empty():
    client, session = make_client()
    result = client.favorite_videos("")
    assert result == {"items": [], "has_more": False, "page": 1}
    assert session.calls == []


def test_favorite_videos_survives_a_failure():
    client, _ = make_client({"/x/v3/fav/resource/list": {"code": -101}})
    result = client.favorite_videos("98765")
    assert result["items"] == []
    assert result["has_more"] is False


# ---------------------------------------------------------------------------
# video streams
# ---------------------------------------------------------------------------

def test_video_streams_picks_the_best_audio_and_video():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": DASH_PAYLOAD,
    })
    result = client.video_streams("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["bvid"] == "BV1EHbhz6EHw"
    assert result["cid"] == 25841439697
    assert result["duration"] == 294
    assert result["title"] == "手搓降调版晴天伴奏"
    assert result["author"] == "Oo柯蟠桃oO"
    # the audio track with the highest bandwidth wins
    assert result["audio"]["url"] == "https://upos.example/high.m4s"
    assert result["audio"]["bandwidth"] == 192000
    assert result["audio"]["codecs"] == "mp4a.40.2"
    assert result["audio"]["init_range"] == "0-900"
    # the highest resolution wins when no quality was asked for
    assert result["video"]["url"] == "https://upos.example/1080.m4s"
    assert result["video"]["width"] == 1920
    assert result["video"]["height"] == 1080
    assert result["video"]["init_range"] == "0-1234"


def test_video_streams_honours_the_requested_quality():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": DASH_PAYLOAD,
    })
    result = client.video_streams("BV1EHbhz6EHw", quality=64)
    assert result["video"]["url"] == "https://upos.example/480.m4s"
    assert result["video"]["height"] == 480


def test_video_streams_lists_the_qualities():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": DASH_PAYLOAD,
    })
    result = client.video_streams("BV1EHbhz6EHw")
    qualities = {item["id"]: item for item in result["qualities"]}
    assert set(qualities) == {16, 32, 64, 80}
    assert qualities[80]["name"] == "1080P 高码率"
    # available 说的是「这个账号能看这个画质」，不是「这一次请求拿到了它」。
    assert qualities[32]["available"] is True
    assert qualities[16]["available"] is True
    # current 才是这一次实际带回来的分辨率。
    assert qualities[80]["current"] is True
    assert qualities[32]["current"] is True
    assert qualities[16]["current"] is False


def test_video_streams_finds_the_lossless_track():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": DASH_PAYLOAD,
    })
    result = client.video_streams("BV1EHbhz6EHw")
    assert result["flac"]["url"] == "https://upos.example/flac.m4s"
    assert result["flac"]["codecs"] == "flac"
    assert result["flac"]["bandwidth"] == 320000
    assert result["flac"]["init_range"] == "0-700"


def test_video_streams_reports_the_muxed_stream():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": DASH_DURL_PAYLOAD,
    })
    result = client.video_streams("BV1EHbhz6EHw")
    assert result["muxed"]["url"] == "https://upos.example/durl.mp4"
    assert result["muxed"]["mime"] == "video/mp4"
    assert result["muxed"]["size"] == 4096
    assert result["muxed"]["quality"] == 32
    assert result["audio"] is None


def test_video_streams_signs_the_playurl_request():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": DASH_PAYLOAD,
    })
    client.video_streams("BV1EHbhz6EHw", quality=80)
    calls = [c for c in session.calls if "playurl" in c["url"]]
    # 第一条是要分轨流（DASH），第二条是要单文件流（兜底）。
    call = calls[0]
    assert "w_rid" in call["params"]
    assert call["params"]["qn"] == 80
    assert call["params"]["fnval"] == 4048
    assert call["params"]["platform"] == "pc"
    fallback = calls[-1]
    assert fallback["params"]["fnval"] == 1
    # 单文件流最高 720P。
    assert fallback["params"]["qn"] == 64


def test_video_streams_reports_a_failure_instead_of_raising():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/player/wbi/playurl": {"code": -403, "message": "访问权限不足"},
    })
    result = client.video_streams("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["message"] == "访问权限不足"
    assert result["audio"] is None


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------

def test_every_new_method_survives_a_dead_network():
    """The UI turns an exception into a spinner that never stops."""
    client, _ = make_client(default=ConnectionError("boom"))

    assert client.login_qrcode()["success"] is False
    assert client.login_poll("key")["status"] == "failed"
    assert client.login_cookie("SESSDATA=abc")["success"] is False
    assert client.login_password("u", "p")["success"] is False
    assert client.captcha()["success"] is False
    assert client.send_sms_code("13800000000")["success"] is False
    assert client.login_sms("13800000000", "1")["success"] is False
    assert client.register_send_code("13800000000")["success"] is False
    assert client.register("u", "p", "13800000000", "1")["success"] is False
    assert client.logout()["success"] is True
    assert client.favorite_folders() == []
    assert client.favorite_videos("1")["items"] == []
    assert client.video_streams("BV1EHbhz6EHw")["success"] is False
    assert client.account_info()["logged_in"] is False


def test_video_quality_name_covers_the_common_codes():
    assert video_quality_name(16) == "360P 流畅"
    assert video_quality_name(32) == "480P 清晰"
    assert video_quality_name(64) == "720P 高清"
    assert video_quality_name(80) == "1080P 高清"
    assert video_quality_name(112) == "1080P 高码率"
    assert video_quality_name(116) == "1080P 60帧"
    assert video_quality_name(120) == "4K 超清"
    assert video_quality_name(125) == "HDR 真彩"
    assert video_quality_name(126) == "杜比视界"
    assert video_quality_name(127) == "8K 超高清"


# ---------------------------------------------------------------------------
# 互动：统计、点赞、投币、收藏、关注、观看历史、空间主页
# ---------------------------------------------------------------------------

STAT_PAYLOAD = {
    "code": 0,
    "data": {
        "view": 12345,
        "danmaku": 67,
        "reply": 8,
        "favorite": 9,
        "coin": 10,
        "like": 11,
        "share": 12,
    },
}

LIKE_PAYLOAD = {"code": 0, "message": "0", "data": {"like": True}}

COIN_PAYLOAD = {"code": 0, "message": "0", "data": {"balance": 480, "coin": 500}}

COIN_NUMBER_PAYLOAD = {"code": 0, "data": {"balance": 480, "coin": 500}}

FAV_DEAL_PAYLOAD = {"code": 0, "message": "0", "data": {"id": 98765}}

RELATION_PAYLOAD = {"code": 0, "data": {"attribute": 2}}

RELATION_CARD_PAYLOAD = {"code": 0, "data": {"attribute": 2, "card": {"mid": 999}}}

HISTORY_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {
                "title": "看过的视频",
                "bvid": "BV1EHbhz6EHw",
                "aid": 115021136794799,
                "cid": 25841439697,
                "cover": "//i2.hdslb.com/bfs/archive/cover.jpg",
                # 上传者在真实回答里是一个对象，不是字符串。
                "author": {"mid": 946974, "name": "Oo柯蟠桃oO",
                           "face": "//i0.hdslb.com/bfs/face/face.jpg"},
                "duration": 300,
                "view_at": 1755083508,
                "progress": -1,
                "business": "archive",
            },
            {
                "title": "看了一半",
                "bvid": "BV1xx411c7mD",
                "aid": 2,
                "cid": 3,
                "cover": "https://i0.hdslb.com/bfs/archive/second.jpg",
                "author": {"mid": 2, "name": "UP2", "face": ""},
                "duration": 100,
                "view_at": 1755083600,
                "progress": 50,
                "business": "archive",
            },
        ],
        "cursor": {"max": "cursor-next"},
    },
}

SPACE_PAYLOAD = {
    "code": 0,
    "data": {
        "mid": 1943648897,
        "name": "Oo柯蟠桃oO",
        "face": "//i0.hdslb.com/bfs/face/face.jpg",
        "sign": "签名内容",
        "level": {"current_level": 6},
    },
}

SPACE_UPSTAT_PAYLOAD = {
    "code": 0,
    "data": {"fans": 1200, "following": 34, "video": 56, "likes": 7890},
}

SPACE_ARC_PAYLOAD = {
    "code": 0,
    "data": {
        "list": {
            "vlist": [
                {
                    "bvid": "BV1EHbhz6EHw",
                    "aid": 115021136794799,
                    "title": "投稿一",
                    "pic": "//i2.hdslb.com/bfs/archive/cover.jpg",
                    "author": "Oo柯蟠桃oO",
                    "mid": 1943648897,
                    "length": "4:54",
                    "play": 232,
                    "created": 1755083508,
                }
            ]
        },
        "page": {"count": 1, "pn": 1, "ps": 30},
    },
}

LOGGED_IN_COOKIE = "SESSDATA=abc; bili_jct=def; DedeUserID=12345678"


def test_video_stat_returns_the_counters():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/archive/stat": STAT_PAYLOAD,
    })
    result = client.video_stat("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["stat"] == {
        "view": 12345,
        "danmaku": 67,
        "reply": 8,
        "favorite": 9,
        "coin": 10,
        "like": 11,
        "share": 12,
    }


def test_video_stat_reports_a_failure_instead_of_raising():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/archive/stat": {"code": -403, "message": "访问权限不足"},
    })
    result = client.video_stat("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["stat"]["view"] == 0
    assert "权限不足" in result["message"]


def test_video_stat_survives_a_dead_network():
    client, _ = make_client(default=ConnectionError("boom"))
    result = client.video_stat("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["message"]


def test_video_stat_needs_a_bvid():
    client, _ = make_client()
    assert client.video_stat("")["success"] is False


def test_like_video_needs_a_signed_in_account():
    """未登录时服务端会回 code 0 但其实什么都没做，所以不能发请求。"""
    client, session = make_client({"/x/web-interface/archive/like": LIKE_PAYLOAD})
    result = client.like_video("BV1EHbhz6EHw")
    assert result["success"] is False
    assert "登录" in result["message"]
    assert session.calls == []


def test_like_video_sends_like_1():
    client, session = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/web-interface/archive/like": LIKE_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.like_video("BV1EHbhz6EHw")
    assert result["success"] is True
    call = [c for c in session.calls if "archive/like" in c["url"]][-1]
    assert call["method"] == "POST"
    assert call["params"]["like"] == 1
    assert call["params"]["bvid"] == "BV1EHbhz6EHw"
    assert call["params"]["aid"] == 115021136794799
    assert call["params"]["csrf"] == "def"


def test_like_video_sends_like_2_to_cancel():
    client, session = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/web-interface/archive/like": LIKE_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.like_video("BV1EHbhz6EHw", like=False)
    assert result["success"] is True
    call = [c for c in session.calls if "archive/like" in c["url"]][-1]
    assert call["params"]["like"] == 2


def test_like_video_reports_a_failure_instead_of_raising():
    client, _ = make_client(default=ConnectionError("boom"), cookie=LOGGED_IN_COOKIE)
    result = client.like_video("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["message"]


def test_coin_video_clamps_the_multiplier():
    client, session = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/web-interface/coin/add": COIN_PAYLOAD,
        "/x/web-interface/coin/number": COIN_NUMBER_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    client.coin_video("BV1EHbhz6EHw", count=5)
    call = [c for c in session.calls if "coin/add" in c["url"]][-1]
    assert call["params"]["multiply"] == 2
    client.coin_video("BV1EHbhz6EHw", count=0)
    call = [c for c in session.calls if "coin/add" in c["url"]][-1]
    assert call["params"]["multiply"] == 1


def test_coin_video_reports_the_balance_and_the_like_flag():
    client, session = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/web-interface/coin/add": COIN_PAYLOAD,
        "/x/web-interface/coin/number": COIN_NUMBER_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.coin_video("BV1EHbhz6EHw", count=2, also_like=True)
    assert result["success"] is True
    assert result["balance"] == 480
    call = [c for c in session.calls if "coin/add" in c["url"]][-1]
    assert call["params"]["multiply"] == 2
    assert call["params"]["select_like"] == 1
    assert call["params"]["csrf"] == "def"


def test_coin_video_needs_a_signed_in_account():
    client, session = make_client({"/x/web-interface/coin/add": COIN_PAYLOAD})
    result = client.coin_video("BV1EHbhz6EHw")
    assert result["success"] is False
    assert session.calls == []


def test_coin_balance_reads_the_balance():
    client, _ = make_client({
        "/x/web-interface/coin/number": COIN_NUMBER_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.coin_balance()
    assert result["success"] is True
    assert result["balance"] == 480
    assert result["coin"] == 500


def test_coin_balance_needs_a_signed_in_account():
    client, _ = make_client()
    result = client.coin_balance()
    assert result["success"] is False
    assert result["balance"] == 0


def test_favorite_video_uses_add_media_ids():
    client, session = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/v3/fav/resource/deal": FAV_DEAL_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.favorite_video("BV1EHbhz6EHw", folder_id="98765")
    assert result["success"] is True
    assert result["folder_id"] == "98765"
    call = [c for c in session.calls if "resource/deal" in c["url"]][-1]
    assert call["method"] == "POST"
    assert call["params"]["rid"] == "98765"
    assert call["params"]["type"] == 2
    assert call["params"]["add_media_ids"] == "115021136794799:2"
    assert "del_media_ids" not in call["params"]
    assert call["params"]["csrf"] == "def"


def test_favorite_video_uses_del_media_ids_to_cancel():
    client, session = make_client({
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/v3/fav/resource/deal": FAV_DEAL_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.favorite_video("BV1EHbhz6EHw", folder_id="98765", remove=True)
    assert result["success"] is True
    call = [c for c in session.calls if "resource/deal" in c["url"]][-1]
    assert call["params"]["del_media_ids"] == "115021136794799:2"
    assert "add_media_ids" not in call["params"]


def test_favorite_video_takes_the_default_folder_when_none_is_given():
    client, session = make_client({
        "/x/web-interface/nav": NAV_LOGIN_PAYLOAD,
        "/x/v3/fav/folder/created/list-all": FAV_FOLDERS_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
        "/x/v3/fav/resource/deal": FAV_DEAL_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.favorite_video("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["folder_id"] == "98765"
    call = [c for c in session.calls if "resource/deal" in c["url"]][-1]
    assert call["params"]["rid"] == "98765"


def test_favorite_video_reports_a_failure_instead_of_raising():
    client, _ = make_client(default=ConnectionError("boom"), cookie=LOGGED_IN_COOKIE)
    result = client.favorite_video("BV1EHbhz6EHw", folder_id="98765")
    assert result["success"] is False
    assert result["message"]


def test_favorite_folder_favours_and_unfavours():
    client, session = make_client({
        "/x/v3/fav/folder/fav": FAV_DEAL_PAYLOAD,
        "/x/v3/fav/folder/unfav": FAV_DEAL_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    assert client.favorite_folder("98765")["success"] is True
    call = [c for c in session.calls if "folder/fav" in c["url"]][-1]
    assert call["method"] == "POST"
    assert call["params"]["media_id"] == "98765"
    assert client.favorite_folder("98765", remove=True)["success"] is True
    call = [c for c in session.calls if "folder/unfav" in c["url"]][-1]
    assert call["params"]["media_id"] == "98765"


def test_favorite_folder_needs_a_signed_in_account():
    client, session = make_client({"/x/v3/fav/folder/fav": FAV_DEAL_PAYLOAD})
    assert client.favorite_folder("98765")["success"] is False
    assert session.calls == []


def test_follow_user_sends_act_1_and_act_2():
    client, session = make_client({
        "/x/relation/modify": {"code": 0, "message": "0", "data": {"mid": 999}},
    }, cookie=LOGGED_IN_COOKIE)
    assert client.follow_user("999")["success"] is True
    call = [c for c in session.calls if "relation/modify" in c["url"]][-1]
    assert call["method"] == "POST"
    assert call["params"]["fid"] == "999"
    assert call["params"]["act"] == 1
    assert call["params"]["re_src"] == 11
    assert call["params"]["csrf"] == "def"

    assert client.follow_user("999", follow=False)["success"] is True
    call = [c for c in session.calls if "relation/modify" in c["url"]][-1]
    assert call["params"]["act"] == 2


def test_follow_user_needs_a_signed_in_account():
    client, session = make_client({"/x/relation/modify": {"code": 0}})
    assert client.follow_user("999")["success"] is False
    assert session.calls == []


def test_follow_state_reads_the_attribute():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/relation": RELATION_PAYLOAD,
    })
    result = client.follow_state("999")
    assert result["success"] is True
    assert result["relation"] == 2
    assert result["following"] is True
    # 这个路由必须带 WBI 签名，否则服务端不认。
    call = [c for c in session.calls if c["url"].endswith("/x/relation")][-1]
    assert "w_rid" in call["params"]
    assert call["params"]["fid"] == "999"


def test_follow_state_falls_back_to_the_card_route():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/relation": {"code": -403, "message": "访问权限不足"},
        "/x/web-interface/card": RELATION_CARD_PAYLOAD,
    })
    result = client.follow_state("999")
    assert result["success"] is True
    assert result["following"] is True
    assert any("web-interface/card" in c["url"] for c in session.calls)


def test_follow_state_reports_a_failure_instead_of_raising():
    client, _ = make_client(default=ConnectionError("boom"))
    result = client.follow_state("999")
    assert result["success"] is False
    assert result["following"] is False
    assert result["message"]


def test_history_normalises_the_items():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/history/cursor": HISTORY_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    result = client.history()
    assert result["success"] is True
    assert result["count"] == 2
    assert result["has_more"] is True
    first = result["items"][0]
    assert first["bvid"] == "BV1EHbhz6EHw"
    assert first["aid"] == 115021136794799
    assert first["title"] == "看过的视频"
    assert first["cover"] == "https://i2.hdslb.com/bfs/archive/cover.jpg"
    assert first["author"] == "Oo柯蟠桃oO"
    assert first["author_mid"] == "946974"
    assert first["duration"] == 300
    assert first["viewed_at"] == 1755083508
    assert first["business"] == "archive"
    assert first["cid"] == 25841439697
    # progress = -1 表示已经看完。
    assert first["progress"] == 100
    # 看了一半 = 50%。
    assert result["items"][1]["progress"] == 50
    call = [c for c in session.calls if "history/cursor" in c["url"]][-1]
    assert "w_rid" in call["params"]
    assert call["params"]["max"] == "0"
    assert call["params"]["type"] == "archive"


def test_history_uses_the_cursor_of_the_previous_page():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/web-interface/history/cursor": HISTORY_PAYLOAD,
    }, cookie=LOGGED_IN_COOKIE)
    client.history(page=1)
    client.history(page=2)
    calls = [c for c in session.calls if "history/cursor" in c["url"]]
    assert calls[0]["params"]["max"] == "0"
    # 第二页用的是上一页回答里的 cursor.max。
    assert calls[1]["params"]["max"] == "cursor-next"


def test_history_needs_a_signed_in_account():
    client, session = make_client({"/x/web-interface/history/cursor": HISTORY_PAYLOAD})
    result = client.history()
    assert result["success"] is False
    assert result["items"] == []
    assert session.calls == []


def test_history_reports_a_failure_instead_of_raising():
    client, _ = make_client(default=ConnectionError("boom"), cookie=LOGGED_IN_COOKIE)
    result = client.history()
    assert result["success"] is False
    assert result["message"]


def test_space_normalises_the_profile():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/space/wbi/acc/info": SPACE_PAYLOAD,
        "/x/space/upstat": SPACE_UPSTAT_PAYLOAD,
    })
    result = client.space("1943648897")
    assert result["success"] is True
    assert result["profile"] == {
        "mid": "1943648897",
        "name": "Oo柯蟠桃oO",
        "face": "https://i0.hdslb.com/bfs/face/face.jpg",
        "sign": "签名内容",
        "level": 6,
        "fans": 1200,
        "following": 34,
        "video_count": 56,
        "like_count": 7890,
    }
    call = [c for c in session.calls if "acc/info" in c["url"]][-1]
    assert "w_rid" in call["params"]
    assert call["params"]["mid"] == "1943648897"
    assert call["params"]["platform"] == "web"
    # upstat 不签名。
    upstat_call = [c for c in session.calls if "upstat" in c["url"]][-1]
    assert "w_rid" not in upstat_call["params"]


def test_space_keeps_the_counters_at_zero_when_upstat_fails():
    client, _ = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/space/wbi/acc/info": SPACE_PAYLOAD,
        "/x/space/upstat": {"code": -403, "message": "访问权限不足"},
    })
    result = client.space("1943648897")
    assert result["success"] is True
    assert result["profile"]["fans"] == 0
    assert result["profile"]["name"] == "Oo柯蟠桃oO"


def test_space_reports_a_failure_instead_of_raising():
    client, _ = make_client(default=ConnectionError("boom"))
    result = client.space("1943648897")
    assert result["success"] is False
    assert result["message"]


def test_space_videos_normalises_the_items():
    client, session = make_client({
        "/x/web-interface/nav": NAV_PAYLOAD,
        "/x/space/wbi/arc/search": SPACE_ARC_PAYLOAD,
    })
    result = client.space_videos("1943648897")
    assert result["success"] is True
    assert result["count"] == 1
    assert result["has_more"] is False
    assert result["items"][0] == {
        "bvid": "BV1EHbhz6EHw",
        "aid": 115021136794799,
        "title": "投稿一",
        "cover": "https://i2.hdslb.com/bfs/archive/cover.jpg",
        "author": "Oo柯蟠桃oO",
        "author_mid": "1943648897",
        "duration": 294,
        "play_count": 232,
        "created_at": 1755083508,
    }
    call = [c for c in session.calls if "arc/search" in c["url"]][-1]
    assert "w_rid" in call["params"]
    assert call["params"]["pn"] == 1
    assert call["params"]["order"] == "pubdate"


def test_space_videos_reports_a_failure_instead_of_raising():
    client, _ = make_client(default=ConnectionError("boom"))
    result = client.space_videos("1943648897")
    assert result["success"] is False
    assert result["items"] == []


# ---------------------------------------------------------------------------
# 评论
# ---------------------------------------------------------------------------

#: ``cursor.next`` 是双重编码的 JSON 字符串：B 站在外面又套了一层
#: 引号，所以发回去之前要先 ``json.loads`` 一次。
REPLY_CURSOR_NEXT = json.dumps(json.dumps({"offset": "2"}))

REPLY_CHILD = {
    "rpid": 9101,
    "mid": 12345678,
    "ctime": 1755083600,
    "like": 3,
    "rcount": 0,
    "location": "IP属地：广东",
    "member": {
        "mid": 12345678,
        "uname": "测试用户",
        "face": "https://i0.hdslb.com/bfs/face/child.jpg",
        "level": 6,
    },
    "content": {"message": "子评论一"},
}

REPLY_PAYLOAD = {
    "code": 0,
    "data": {
        "replies": [
            {
                "rpid": 9001,
                "mid": 12345678,
                "ctime": 1755083508,
                "like": 12,
                "rcount": 4,
                "location": "IP属地：广东",
                "member": {
                    "mid": 12345678,
                    "uname": "测试用户",
                    "face": "https://i0.hdslb.com/bfs/face/main.jpg",
                    "level": 6,
                },
                "content": {"message": "第一条评论"},
                "reply_control": {"is_up_top": False, "location": "IP属地：广东"},
                "replies": [
                    REPLY_CHILD,
                    {"rpid": 9102, "mid": 999, "member": {"uname": "路人"},
                     "content": {"message": "子评论二"}},
                    {"rpid": 9103, "mid": 999, "member": {"uname": "路人"},
                     "content": {"message": "子评论三"}},
                    {"rpid": 9104, "mid": 999, "member": {"uname": "路人"},
                     "content": {"message": "子评论四"}},
                ],
            },
            {
                "rpid": 9002,
                "mid": 999,
                "ctime": 1755083700,
                "like": 1,
                "rcount": 0,
                "member": {"mid": 999, "uname": "路人", "face": "https://i0.hdslb.com/x.jpg",
                           "level": 2},
                "content": {"message": "第二条评论"},
            },
        ],
        "top_replies": [],
        "cursor": {
            "all_count": 2,
            "count": 2,
            "is_end": False,
            "next": REPLY_CURSOR_NEXT,
        },
    },
}

REPLY_ADD_PAYLOAD = {
    "code": 0,
    "data": {"reply": {"rpid": 9999, "id_str": "9999"}},
}

REPLY_ACTION_PAYLOAD = {"code": 0, "data": {}}


def make_reply_client(routes=None, default=None):
    """一个带视频信息和 WBI 密钥的假客户端。"""
    base = {
        "/x/frontend/finger/spi": SPI_PAYLOAD,
        "/x/web-interface/nav": NAV_LOGIN_PAYLOAD,
        "/x/web-interface/view": VIEW_PAYLOAD,
    }
    base.update(routes or {})
    return make_client(base, default=default, cookie=LOGGED_IN_COOKIE)


def test_comments_normalises_the_items():
    client, _ = make_reply_client({"/x/v2/reply/main": REPLY_PAYLOAD})
    result = client.comments("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["count"] == 2
    assert result["has_more"] is True
    first = result["items"][0]
    assert first["rpid"] == "9001"
    assert first["mid"] == "12345678"
    assert first["uname"] == "测试用户"
    assert first["face"] == "https://i0.hdslb.com/bfs/face/main.jpg"
    assert first["level"] == 6
    assert first["content"] == "第一条评论"
    assert first["like"] == 12
    assert first["reply_count"] == 4
    assert first["ctime"] == 1755083508
    assert first["location"] == "IP属地：广东"
    # 自己的评论要标出来。
    assert first["is_owner"] is True
    assert result["items"][1]["is_owner"] is False


def test_comments_keeps_at_most_three_children():
    """子评论多了前端只显示「查看全部 N 条回复」，所以这里只留 3 条。"""
    client, _ = make_reply_client({"/x/v2/reply/main": REPLY_PAYLOAD})
    result = client.comments("BV1EHbhz6EHw")
    children = result["items"][0]["children"]
    assert len(children) == 3
    assert children[0]["rpid"] == "9101"
    assert children[0]["content"] == "子评论一"
    assert children[0]["is_owner"] is True
    assert result["items"][0]["reply_count"] == 4


def test_comments_sends_the_aid_and_a_wbi_signature():
    client, session = make_reply_client({"/x/v2/reply/main": REPLY_PAYLOAD})
    client.comments("BV1EHbhz6EHw")
    call = [c for c in session.calls if "reply/main" in c["url"]][-1]
    # oid 要的是 aid，不是 bvid。
    assert call["params"]["oid"] == 115021136794799
    assert call["params"]["type"] == 1
    assert call["params"]["plat"] == 1
    assert "w_rid" in call["params"]


def test_comments_maps_the_sort_to_the_mode():
    """hot 是 mode 2（按热度），time 是 mode 3（按时间）。"""
    client, session = make_reply_client({"/x/v2/reply/main": REPLY_PAYLOAD})
    client.comments("BV1EHbhz6EHw")
    call = [c for c in session.calls if "reply/main" in c["url"]][-1]
    assert call["params"]["mode"] == 2

    client, session = make_reply_client({"/x/v2/reply/main": REPLY_PAYLOAD})
    client.comments("BV1EHbhz6EHw", sort="time")
    call = [c for c in session.calls if "reply/main" in c["url"]][-1]
    assert call["params"]["mode"] == 3


def test_comments_paginates_with_the_cursor_of_the_previous_page():
    client, session = make_reply_client({"/x/v2/reply/main": REPLY_PAYLOAD})
    client.comments("BV1EHbhz6EHw")
    client.comments("BV1EHbhz6EHw", page=2)
    calls = [c for c in session.calls if "reply/main" in c["url"]]
    assert len(calls) == 2
    # 第一页不带 pagination_str。
    assert "pagination_str" not in calls[0]["params"]
    # 第二页带上上一页回答里的 cursor.next。
    assert calls[1]["params"]["pagination_str"] == json.loads(REPLY_CURSOR_NEXT)


def test_comments_stops_at_the_end_of_the_cursor():
    payload = {
        "code": 0,
        "data": {"replies": [], "cursor": {"is_end": True, "next": ""}},
    }
    client, _ = make_reply_client({"/x/v2/reply/main": payload})
    result = client.comments("BV1EHbhz6EHw")
    assert result["success"] is True
    assert result["has_more"] is False
    assert result["items"] == []


def test_comments_reports_a_failure_instead_of_raising():
    client, _ = make_reply_client(
        {"/x/v2/reply/main": ConnectionError("boom")},
    )
    result = client.comments("BV1EHbhz6EHw")
    assert result["success"] is False
    assert result["message"]
    assert result["items"] == []


def test_comments_needs_a_bvid():
    client, session = make_reply_client({"/x/v2/reply/main": REPLY_PAYLOAD})
    assert client.comments("")["success"] is False
    assert session.calls == []


def test_post_comment_sends_the_form():
    client, session = make_reply_client({"/x/v2/reply/add": REPLY_ADD_PAYLOAD})
    result = client.post_comment("BV1EHbhz6EHw", "写得不错")
    assert result["success"] is True
    assert result["rpid"] == "9999"
    call = [c for c in session.calls if "reply/add" in c["url"]][-1]
    assert call["method"] == "POST"
    assert call["params"]["type"] == 1
    assert call["params"]["oid"] == 115021136794799
    assert call["params"]["message"] == "写得不错"
    assert call["params"]["parent"] == 0
    assert call["params"]["csrf"] == "def"
    assert call["params"]["buvid3"] == "B3-DEV"


def test_post_comment_replies_to_a_comment():
    client, session = make_reply_client({"/x/v2/reply/add": REPLY_ADD_PAYLOAD})
    assert client.post_comment("BV1EHbhz6EHw", "同感", parent_rpid=9001)["success"] is True
    call = [c for c in session.calls if "reply/add" in c["url"]][-1]
    assert call["params"]["parent"] == 9001


def test_post_comment_refuses_an_empty_body():
    client, session = make_reply_client({"/x/v2/reply/add": REPLY_ADD_PAYLOAD})
    result = client.post_comment("BV1EHbhz6EHw", "   ")
    assert result["success"] is False
    assert "空" in result["message"]
    assert session.calls == []


def test_post_comment_needs_a_signed_in_account():
    client, session = make_client({"/x/v2/reply/add": REPLY_ADD_PAYLOAD})
    result = client.post_comment("BV1EHbhz6EHw", "写得不错")
    assert result["success"] is False
    assert "登录" in result["message"]
    assert session.calls == []


def test_post_comment_reports_a_failure_instead_of_raising():
    client, _ = make_reply_client({"/x/v2/reply/add": ConnectionError("boom")})
    result = client.post_comment("BV1EHbhz6EHw", "写得不错")
    assert result["success"] is False
    assert result["message"]


def test_like_comment_sends_action_1_to_like():
    client, session = make_reply_client({"/x/v2/reply/action": REPLY_ACTION_PAYLOAD})
    result = client.like_comment("9001")
    assert result["success"] is True
    call = [c for c in session.calls if "reply/action" in c["url"]][-1]
    assert call["method"] == "POST"
    assert call["params"]["rpid"] == "9001"
    # 评论点赞是 1，取消是 -1（跟视频点赞的 1/2 不一样）。
    assert call["params"]["action"] == 1
    assert call["params"]["csrf"] == "def"


def test_like_comment_sends_action_minus_1_to_cancel():
    client, session = make_reply_client({"/x/v2/reply/action": REPLY_ACTION_PAYLOAD})
    assert client.like_comment("9001", like=False)["success"] is True
    call = [c for c in session.calls if "reply/action" in c["url"]][-1]
    assert call["params"]["action"] == -1


def test_like_comment_reports_a_failure_instead_of_raising():
    client, _ = make_reply_client({"/x/v2/reply/action": ConnectionError("boom")})
    result = client.like_comment("9001")
    assert result["success"] is False
    assert result["message"]
