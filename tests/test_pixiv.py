"""Pixiv 客户端：包络、规范化、图片地址推导、写接口的形状。

这一层没有网络：``FakeSession`` 按 URL 片段给回答。真正从 pixiv 拿到的
字段名和坑都记在 ``docs/pixiv-notes.md``，用例只钉住「拿到那种回答之后
我们会怎么解释它」。
"""
from __future__ import annotations

import json

import pytest

from src.core import pixiv
from src.core.pixiv import (
    PixivClient,
    image_url_for,
    is_nsfw,
    normalize_work,
)

# ------------------------------------------------------------------ 假 session


class FakeResponse:
    def __init__(self, payload=None, status: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """按顺序匹配 URL 片段；同一个 key 给一串回答时按次取用。"""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = {key: list(value) if isinstance(value, list) else [value]
                       for key, value in routes.items()}
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}

    def _pick(self, url: str):
        # **最长的那个 key 赢。** 首页的 key 是 ``www.pixiv.net/``，它同时
        # 是每一条 ajax 路由的前缀；按插入顺序匹配的话所有请求都会拿到
        # 首页的 HTML，「写接口到底发了什么」就永远测不到。
        for key in sorted(self.routes, key=len, reverse=True):
            answers = self.routes[key]
            if key in url:
                if isinstance(answers, Exception):
                    raise answers
                if len(answers) > 1:
                    return answers.pop(0)
                return answers[0]
        raise AssertionError(f"用例没有准备这条路由：{url}")

    def request(self, method, url, params=None, json=None, data=None,
                headers=None, timeout=None):  # noqa: ANN001, ANN201
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "data": data, "headers": headers})
        answer = self._pick(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url, timeout=None):  # noqa: ANN001, ANN201
        self.calls.append({"method": "GET", "url": url})
        answer = self._pick(url)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """别让用例替 pixiv 的礼貌间隔买单。"""
    monkeypatch.setattr(pixiv, "_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(pixiv, "_MIN_WRITE_INTERVAL", 0.0)
    monkeypatch.setattr(pixiv, "_RETRY_BASE", 0.0)


def _json(payload, status: int = 200) -> FakeResponse:
    return FakeResponse(payload, status)


def _client(routes: dict[str, object]) -> tuple[PixivClient, FakeSession]:
    session = FakeSession(routes)
    return PixivClient(session=session), session


#: 真机上抓到的、最小化的一份搜索回答。
SEARCH_BODY = {
    "error": False,
    "message": "",
    "body": {
        "illustManga": {
            "total": 622576,
            "data": [
                {
                    "id": "152013340",
                    "title": "有線イヤホン",
                    "illustType": 0,
                    "xRestrict": 0,
                    "sl": 2,
                    "url": "https://i.pximg.net/c/250x250_80_a2/img-master/img/2026/11/13/18/08/29/152013340_p0_square1200.jpg",
                    "tags": ["初音ミク", "VOCALOID"],
                    "userId": "121048946",
                    "userName": "DRAGONⅢ",
                    "pageCount": 1,
                    "bookmarkData": None,
                    "createDate": "2026-11-13T09:08:00+00:00",
                },
                {"isAdContainer": True, "title": "広告"},
            ],
        },
        "relatedTags": ["VOCALOID", "ボカロ"],
    },
}

#: 详情回答：tags 是对象、urls 齐全、bookmarkData 有 id。
DETAIL_BODY = {
    "error": False,
    "body": {
        "id": "152013340",
        "title": "有線イヤホン",
        "illustType": 0,
        "xRestrict": 1,
        "sl": 6,
        "userId": "121048946",
        "userName": "DRAGONⅢ",
        "pageCount": 3,
        "urls": {
            "thumb": "https://i.pximg.net/c/250x250_80_a2/img-master/img/a_p0_square1200.jpg",
            "regular": "https://i.pximg.net/img-master/img/a_p0_master1200.jpg",
            "original": "https://i.pximg.net/img-original/img/a_p0.jpg",
        },
        "tags": {"authorId": "121048946", "tags": [
            {"tag": "初音ミク", "translation": {"en": "Hatsune Miku"}},
            {"tag": "R-18"},
        ]},
        "bookmarkData": {"id": "999", "private": True},
        "bookmarkCount": 41,
        "likeCount": 7,
        "likeData": False,
    },
}


# --------------------------------------------------------------- 会话与 cookie


def test_the_cookie_is_cut_down_to_what_pixiv_needs():
    """webview 交出来的是整台机器的 cookie，我们只留 pixiv 自己的那几个。

    整个 cookie 罐原样带上是两个问题：请求头会被越拖越长，而且那些
    广告和统计 cookie 跟登录态一点关系都没有。
    """
    cookies = [
        {"name": "_ga", "value": "GA1", "domain": ".pixiv.net"},
        {"name": "PHPSESSID", "value": "123_deadbeef", "domain": ".pixiv.net"},
        {"name": "device_token", "value": "abc", "domain": ".pixiv.net"},
        {"name": "SESSDATA", "value": "bili", "domain": ".bilibili.com"},
        {"name": "p_ab_id", "value": "6", "domain": ".pixiv.net"},
    ]

    cookie = PixivClient.cookie_from_webview(cookies)

    assert cookie.split("; ")[0] == "PHPSESSID=123_deadbeef", "会话 cookie 必须打头"
    assert "device_token=abc" in cookie
    assert "p_ab_id=6" in cookie
    assert "_ga=" not in cookie, "统计 cookie 不该进请求头"
    assert "SESSDATA" not in cookie, "别的站点的 cookie 混进来了"


def test_a_cookie_from_another_site_is_not_taken_for_a_login():
    client, _ = _client({})
    assert client.signed_in is False

    client.apply_cookie("SESSDATA=abc; bili_jct=def")

    assert client.signed_in is False, "没有 PHPSESSID 就不算登录"


def test_clearing_the_cookie_clears_the_request_header_too():
    client, session = _client({})
    client.apply_cookie("PHPSESSID=abc")
    assert session.headers.get("Cookie") == "PHPSESSID=abc"

    client.apply_cookie("")

    assert "Cookie" not in session.headers, "退出登录后请求头里不能还挂着 cookie"


# ------------------------------------------------------------- 数据形状规范化


def test_a_listing_work_and_a_detail_work_end_up_the_same_shape():
    listing = normalize_work(SEARCH_BODY["body"]["illustManga"]["data"][0])
    detail = normalize_work(DETAIL_BODY["body"])

    for key in ("id", "title", "user_id", "user_name", "page_count", "nsfw"):
        assert key in listing and key in detail, key
    assert listing["id"] == "152013340"
    assert detail["id"] == "152013340"
    assert detail["bookmarked"] is True and detail["bookmark_id"] == "999"
    assert detail["private"] is True
    assert listing["bookmarked"] is False


def test_an_authoritative_object_inside_the_tag_list_is_flattened():
    """详情里的 tags 是对象数组，列表里的是字符串数组，两种都要读成字符串列表。"""
    assert normalize_work(DETAIL_BODY["body"])["tags"] == ["初音ミク", "R-18"]
    assert normalize_work(SEARCH_BODY["body"]["illustManga"]["data"][0])["tags"] == [
        "初音ミク", "VOCALOID"]


def test_an_ad_slot_is_marked_so_the_grid_can_drop_it():
    """搜索结果里混着广告位，它没有 id，用「有没有标题」判断会漏。"""
    ad = normalize_work({"isAdContainer": True, "title": "広告"})

    assert ad == {"id": "", "is_ad": True}


def test_ranking_rows_are_snake_case_and_have_no_envelope():
    """``/ranking.php`` 是老路由：没有 body 包一层，字段是下划线风格。"""
    client, _ = _client({
        "/ranking.php": _json({
            "contents": [
                {"illust_id": 152013340, "title": "作品", "url": "https://i.pximg.net/c/480x960/img-master/img/x_p0_master1200.jpg",
                 "rank": 1, "yes_rank": 4, "user_id": 121048946, "user_name": "某人",
                 "tags": ["タグ"], "illust_type": "0", "illust_page_count": "2"},
            ],
            "next": 2,
            "date": "20261112",
        }),
    })

    result = client.ranking(mode="daily")

    work = result["works"][0]
    assert work["id"] == "152013340", "illust_id 是数字，也要读成字符串 id"
    assert work["user_id"] == "121048946"
    assert work["page_count"] == 2
    assert work["rank"] == 1
    assert result["has_more"] is True


def test_an_error_envelope_is_a_failure_even_though_the_status_is_200():
    """pixiv 说「不行」的方式是 HTTP 200 + error:true。"""
    client, _ = _client({
        "/ajax/search/artworks/": _json(
            {"error": True, "message": "无法找到您所请求的页面", "body": []}, 200),
    })

    result = client.search("初音ミク")

    assert result["works"] == []
    assert "无法找到" in client.last_error, client.last_error


def test_an_empty_body_array_does_not_crash_the_reader():
    client, _ = _client({"/ajax/illust/1/pages": _json({"error": False, "body": []})})

    assert client.illust_pages("1") == []


# ------------------------------------------------------------ 图片地址推导


@pytest.mark.parametrize("size,expected", [
    ("thumb", "https://i.pximg.net/c/250x250_80_a2/img-master/img/2026/11/13/18/08/29/152013340_p0_square1200.jpg"),
    ("regular", "https://i.pximg.net/img-master/img/2026/11/13/18/08/29/152013340_p0_master1200.jpg"),
    ("original", "https://i.pximg.net/img-original/img/2026/11/13/18/08/29/152013340_p0.jpg"),
])
def test_every_quality_can_be_derived_from_a_thumbnail(size, expected):
    """列表接口只给缩略图，别的尺寸靠字符串推导，不再多打一次请求。"""
    work = normalize_work(SEARCH_BODY["body"]["illustManga"]["data"][0])

    assert image_url_for(work, size) == expected


def test_a_custom_thumbnail_still_derives_a_working_original_url():
    """画师设了自定义缩略图时路径段是 custom-thumb，直接替换会拼出 403 的地址。"""
    work = {"thumb": "https://i.pximg.net/c/250x250_80_a2/custom-thumb/img/2024/10/27/00/08/15/123706421_p0_custom1200.jpg"}

    assert image_url_for(work, "regular") == (
        "https://i.pximg.net/img-master/img/2024/10/27/00/08/15/123706421_p0_master1200.jpg")
    assert image_url_for(work, "original") == (
        "https://i.pximg.net/img-original/img/2024/10/27/00/08/15/123706421_p0.jpg")


def test_a_known_url_is_used_as_it_is():
    work = normalize_work(DETAIL_BODY["body"])

    assert image_url_for(work, "original").endswith("/img-original/img/a_p0.jpg")


# ------------------------------------------------------------------ NSFW 判定


def test_nsfw_comes_from_the_flag_the_tag_or_the_sanity_level():
    """三个判据缺一不可：有些作品带标签但没有 flag，有些两者都没有。"""
    assert is_nsfw({"x_restrict": 1}) is True
    assert is_nsfw({"tags": ["R-18"]}) is True
    assert is_nsfw({"tags": ["r18g"]}) is True
    assert is_nsfw({"sl": 6}) is True
    assert is_nsfw({"x_restrict": 0, "sl": 2, "tags": ["初音ミク"]}) is False


# ---------------------------------------------------------------------- 读接口


def test_search_drops_the_ad_slot_and_keeps_the_total():
    client, _ = _client({"/ajax/search/artworks/": _json(SEARCH_BODY)})

    result = client.search("初音ミク")

    assert len(result["works"]) == 1, "广告位不该留在结果里"
    assert result["total"] == 622576
    assert result["related_tags"] == ["VOCALOID", "ボカロ"]


def test_search_asks_for_safe_mode_and_translated_tags():
    client, session = _client({"/ajax/search/artworks/": _json(SEARCH_BODY)})

    client.search("初音ミク", mode="safe")

    params = session.calls[0]["params"]
    assert params["mode"] == "safe"
    assert params["lang"] == "zh"
    assert params["word"] == "初音ミク"


def test_an_empty_keyword_never_reaches_the_network():
    client, session = _client({})

    assert client.search("   ")["works"] == []
    assert session.calls == []


def test_user_works_keeps_the_order_it_asked_for():
    """``/ajax/user/{uid}/illusts`` 按 id 作键回答，顺序不可信。"""
    client, _ = _client({
        "/ajax/user/7/profile/all": _json(
            {"error": False, "body": {"illusts": {"102": None, "101": None}, "manga": {}}}),
        "/ajax/user/7/illusts": _json({"error": False, "body": {
            "101": {"id": "101", "title": "旧", "illustType": 0, "url": "u1", "userId": "7"},
            "102": {"id": "102", "title": "新", "illustType": 0, "url": "u2", "userId": "7"},
        }}),
    })

    result = client.user_works("7", limit=2)

    assert [work["id"] for work in result["works"]] == ["102", "101"], "新的在前"
    assert result["total"] == 2


def test_bookmarks_without_a_login_say_so_and_stay_off_the_wire():
    client, session = _client({})

    result = client.bookmarks()

    assert result["works"] == []
    assert "先登录" in client.last_error
    assert session.calls == [], "没登录就没必要发这个请求"


def test_the_profile_reads_a_region_that_is_only_a_string():
    client, _ = _client({"/ajax/user/121048946": _json({"error": False, "body": {
        "userId": "121048946", "name": "DRAGONⅢ", "webpage": "https://lit.link/x",
        "region": "Japan", "social": {"twitter": {"url": "https://twitter.com/x"}},
    }})})

    user = client.user("121048946")

    assert user["website"] == "https://lit.link/x"
    assert user["region"] == "", "region 是字符串时读不到名字，但不能抛"
    assert user["twitter"] == "https://twitter.com/x"


# ---------------------------------------------------------------------- 写接口


def _home_html(token: str = "CSRF123", self_user: dict | None = None) -> str:
    """按真机那份结构拼一个首页。

    ``serverSerializedPreloadedState`` 是一段**被转义过的 JSON 字符串**，
    手写很容易少一个大括号 —— 拼出来的东西 json.loads 会报 Extra data，
    而解析器只会安静地当成「没登录」。
    """
    state = {
        "api": {"token": token},
        "userData": {"self": self_user or {
            "id": "42", "name": "星", "pixivId": "skyc", "premium": True}},
    }
    page = {"props": {"pageProps": {
        "serverSerializedPreloadedState": json.dumps(state, ensure_ascii=False)}}}
    return ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(page, ensure_ascii=False) + "</script></body></html>")


HOME_HTML = _home_html()


def _signed_in(routes: dict[str, object]) -> tuple[PixivClient, FakeSession]:
    routes = {"www.pixiv.net/": FakeResponse(text=HOME_HTML, payload={}), **routes}
    client, session = _client(routes)
    client.apply_cookie("PHPSESSID=abc; device_token=xyz")
    return client, session


def test_the_login_state_and_the_csrf_token_come_out_of_the_home_page():
    client, session = _signed_in({})

    account = client.refresh_self()

    assert account["id"] == "42"
    assert account["name"] == "星"
    assert account["premium"] is True
    assert session.calls[0]["url"] == "https://www.pixiv.net/"


def test_the_legacy_meta_also_yields_a_token():
    """旧版页面把 token 直接写在 <meta name="global-data"> 里。"""
    html = ('<meta name="global-data" content=\'{"token":"OLD1","userData":'
            '{"self":{"id":"7","name":"旧"}}}\' />')

    state = PixivClient._parse_preload(html)

    assert state["api"]["token"] == "OLD1"
    assert state["userData"]["self"]["name"] == "旧"


def test_the_flat_legacy_meta_still_names_the_account():
    """更老的版本连 ``userData`` 都没有，账号四个字段是平铺的。"""
    html = ('<meta name="global-data" content=\'{"token":"OLD2","userId":"9",'
            '"userName":"平铺","userAccount":"flat"}\' />')

    state = PixivClient._parse_preload(html)

    assert state["userData"]["self"] == {
        "id": "9", "name": "平铺", "pixivId": "flat"}


def test_a_write_carries_the_csrf_token_and_asks_for_it_only_once():
    client, session = _signed_in({
        "/ajax/illusts/bookmarks/add": _json({"error": False, "body": {}}),
    })

    client.bookmark_add("152013340")
    client.bookmark_add("152013341")

    posts = [call for call in session.calls if call["method"] == "POST"]
    assert len(posts) == 2
    assert posts[0]["headers"]["X-CSRF-TOKEN"] == "CSRF123"
    home = [call for call in session.calls if call["url"].endswith("www.pixiv.net/")]
    assert len(home) == 1, "token 只用取一次，不该每次写都回首页"


def test_removing_a_bookmark_sends_a_form_not_json():
    """这条路由收到 JSON 会回 400，而且不解释。"""
    client, session = _signed_in({
        "/ajax/illusts/bookmarks/delete": _json({"error": False, "body": {}}),
    })

    assert client.bookmark_delete("999")["ok"] is True

    post = [call for call in session.calls if call["method"] == "POST"][0]
    assert post["json"] is None
    assert post["data"] == {"bookmark_id": "999"}
    assert post["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_a_like_is_sent_as_a_string_id_in_json():
    client, session = _signed_in({
        "/ajax/illusts/like": _json({"error": False, "body": {"is_liked": False}}),
    })

    result = client.like("152013340")

    assert result == {"ok": True, "already": False, "message": ""}
    post = [call for call in session.calls if call["method"] == "POST"][0]
    assert post["json"] == {"illust_id": "152013340"}


def test_an_already_liked_answer_is_reported_not_hidden():
    """``is_liked: true`` 的意思是「本来就已经赞过」，不是失败。"""
    client, _ = _signed_in({
        "/ajax/illusts/like": _json({"error": False, "body": {"is_liked": True}}),
    })

    assert client.like("1")["already"] is True


def test_a_write_without_a_login_is_refused_before_any_request():
    client, session = _client({})

    result = client.like("152013340")

    assert result["ok"] is False
    assert "先登录" in result["message"]
    assert session.calls == []


def test_follow_and_unfollow_use_the_two_legacy_routes_with_the_right_field_names():
    """关注是 ``user_id``，取关是 ``id``，两条路由还不一样。"""
    client, session = _signed_in({
        "/bookmark_add.php": _json({"result": "ok"}),
        "/rpc_group_setting.php": _json({"result": "ok"}),
    })

    assert client.follow("121048946")["ok"] is True
    assert client.unfollow("121048946")["ok"] is True

    posts = [call for call in session.calls if call["method"] == "POST"]
    assert posts[0]["url"].endswith("/bookmark_add.php")
    assert posts[0]["data"]["user_id"] == "121048946"
    assert posts[0]["data"]["mode"] == "add"
    assert posts[1]["url"].endswith("/rpc_group_setting.php")
    assert posts[1]["data"]["id"] == "121048946"
    assert posts[1]["data"]["mode"] == "del"


def test_a_server_error_is_retried_but_a_bad_request_is_not():
    """503 值得再试两次，400 再试一百次也是 400。"""
    flaky, session = _signed_in({
        "/ajax/illusts/like": [_json(None, 503), _json({"error": False, "body": {}})],
    })
    assert flaky.like("1")["ok"] is True
    assert len([c for c in session.calls if c["method"] == "POST"]) == 2

    broken, session2 = _client({
        "/ajax/search/artworks/": _json({"error": True, "message": "bad"}, 400),
    })
    assert broken.search("x")["works"] == []
    assert len(session2.calls) == 1, "400 不该重试"


def test_an_expired_session_is_reported_as_a_login_problem():
    client, _ = _signed_in({"/ajax/illusts/bookmarks/add": _json(None, 403)})

    result = client.bookmark_add("1")

    assert result["ok"] is False
    assert "重新登录" in result["message"]


def test_the_state_that_json_dumps_for_the_backup_is_plain_data():
    """导出账户数据要能直接 json.dumps —— 里面不能有 datetime 或对象。"""
    client, _ = _signed_in({})

    payload = {"account": client.refresh_self(), "cookie": client.cookie_string()}

    text = json.dumps(payload, ensure_ascii=False)
    assert "PHPSESSID=abc" in text, "导出的 cookie 就是恢复登录要用的那一串"
