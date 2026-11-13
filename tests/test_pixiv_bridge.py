"""Pixiv 的界面层：过滤、缓存、登录取 cookie、写接口的边界。

全部离线：客户端是一个记录调用的替身，媒体服务器和内嵌缓存同理。
"""
from __future__ import annotations

import pytest

from src.gui.pixiv_bridge import PixivBridge

# ------------------------------------------------------------------- 替身


class FakeSettings:
    def __init__(self, **kwargs) -> None:
        self.nsfw_mode = "hide"
        self.include_r18 = False
        self.hide_ai = False
        self.quality = "original"
        self.download_dir = ""
        self.template = "{artist} - {title} - {id}_p{page}"
        self.bookmark_private = False
        self.page_size = 48
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def get(self, kind, key):  # noqa: ANN001, ANN201
        return self.store.get(f"{kind}|{key}")

    def set(self, kind, key, value, ttl=600.0):  # noqa: ANN001, ANN201
        self.store[f"{kind}|{key}"] = value

    def discard(self, kind, key):  # noqa: ANN001, ANN201
        self.store.pop(f"{kind}|{key}", None)

    def discard_prefix(self, kind, prefix):  # noqa: ANN001, ANN201
        for key in [k for k in self.store if k.startswith(f"{kind}|{prefix}")]:
            self.store.pop(key, None)


class FakeClient:
    """记录调用；回答由用例预先写好。"""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []
        self.last_error = ""

    def _answer(self, name: str, default):
        self.calls.append((name, {}))
        return self.answers.get(name, default)

    def search(self, word, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("search", {"word": word, **kwargs}))
        return self.answers.get("search", {"works": [], "total": 0})

    def discovery(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("discovery", kwargs))
        return self.answers.get("discovery", [])

    def feed(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("feed", kwargs))
        return self.answers.get("feed", {"works": [], "has_more": False})

    def ranking(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("ranking", kwargs))
        return self.answers.get("ranking", {"works": [], "has_more": False})

    def bookmarks(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("bookmarks", kwargs))
        return self.answers.get("bookmarks", {"works": [], "total": 0})

    def following(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("following", kwargs))
        return self.answers.get("following", {"users": [], "total": 0})

    def user(self, uid):  # noqa: ANN001, ANN201
        self.calls.append(("user", {"uid": uid}))
        return self.answers.get("user", {})

    def user_work_ids(self, uid):  # noqa: ANN001, ANN201
        self.calls.append(("user_work_ids", {"uid": uid}))
        return self.answers.get("user_work_ids", {"illust": [], "manga": []})

    def user_works(self, uid, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("user_works", {"uid": uid, **kwargs}))
        return self.answers.get("user_works", {"works": [], "total": 0})

    def illust(self, pid):  # noqa: ANN001, ANN201
        self.calls.append(("illust", {"pid": pid}))
        return self.answers.get("illust", {})

    def illust_pages(self, pid):  # noqa: ANN001, ANN201
        self.calls.append(("illust_pages", {"pid": pid}))
        return self.answers.get("illust_pages", [])

    def ugoira_meta(self, pid):  # noqa: ANN001, ANN201
        self.calls.append(("ugoira_meta", {"pid": pid}))
        return self.answers.get("ugoira_meta", {})

    def bookmark_add(self, pid, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("bookmark_add", {"pid": pid, **kwargs}))
        return self.answers.get("bookmark_add", {"ok": True, "message": ""})

    def bookmark_delete(self, bookmark_id):  # noqa: ANN001, ANN201
        self.calls.append(("bookmark_delete", {"bookmark_id": bookmark_id}))
        return self.answers.get("bookmark_delete", {"ok": True, "message": ""})

    def like(self, pid):  # noqa: ANN001, ANN201
        self.calls.append(("like", {"pid": pid}))
        return self.answers.get("like", {"ok": True, "already": False, "message": ""})

    def follow(self, uid, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(("follow", {"uid": uid, **kwargs}))
        return self.answers.get("follow", {"ok": True, "message": ""})

    def unfollow(self, uid):  # noqa: ANN001, ANN201
        self.calls.append(("unfollow", {"uid": uid}))
        return self.answers.get("unfollow", {"ok": True, "message": ""})


class FakeSession:
    def __init__(self, *, logged_in: bool = True, account: dict | None = None) -> None:
        self.client = FakeClient()
        self.logged_in = logged_in
        self.saved: list[tuple[str, dict]] = []
        self.cleared = 0
        self.account = account or {"id": "42", "name": "星", "account": "skyc",
                                   "avatar": "https://i.pximg.net/a.png",
                                   "premium": True, "following": 12}

    @property
    def is_logged_in(self) -> bool:
        return self.logged_in

    def save(self, cookie, account=None):  # noqa: ANN001, ANN201
        self.saved.append((cookie, account or {}))
        return True

    def clear(self) -> bool:
        self.cleared += 1
        self.logged_in = False
        return True

    def account_info(self, refresh: bool = False):  # noqa: ANN001, ANN201
        if not self.logged_in:
            return {"logged_in": False, "expired": False}
        return {**self.account, "logged_in": True, "expired": False}


class FakeMedia:
    def __init__(self, reject: bool = False) -> None:
        self.reject = reject
        self.seen: list[str] = []

    def url_for_pixiv(self, url):  # noqa: ANN001, ANN201
        self.seen.append(url)
        if self.reject or "pximg.net" not in str(url):
            return None
        return "http://127.0.0.1:1/pixiv?u=" + str(url)


def _bridge(monkeypatch, settings: FakeSettings | None = None, *, logged_in: bool = True,
            media=None, client_answers: dict | None = None) -> PixivBridge:
    session = FakeSession(logged_in=logged_in)
    if client_answers:
        session.client.answers.update(client_answers)
    bridge = PixivBridge(session=session, cache=FakeCache(), media=media)
    chosen = settings or FakeSettings()
    # 设置走单例，用例里直接盖掉，别碰真的 settings.yaml。
    import src.config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings",
                        lambda: type("S", (), {"pixiv": chosen})())
    return bridge


def _work(pid: str = "1", **kwargs) -> dict:
    work = {"id": pid, "title": "t", "x_restrict": 0, "sl": 2,
            "thumb": f"https://i.pximg.net/thumb/{pid}.jpg",
            "user_id": "7", "user_name": "a", "ai_type": 0, "nsfw": False}
    work.update(kwargs)
    return work


# ------------------------------------------------------------------- 过滤


def test_adult_work_is_dropped_by_default(monkeypatch):
    bridge = _bridge(monkeypatch, client_answers={
        "search": {"works": [_work("1"), _work("2", x_restrict=1)], "total": 2}})

    result = bridge.search("x")

    assert [w["id"] for w in result["works"]] == ["1"]


def test_blur_mode_keeps_it_but_marks_it(monkeypatch):
    bridge = _bridge(monkeypatch, FakeSettings(nsfw_mode="blur"), client_answers={
        "search": {"works": [_work("2", x_restrict=1)], "total": 1}})

    work = bridge.search("x")["works"][0]

    assert work["blurred"] is True and work["nsfw"] is True


def test_show_mode_does_not_mark_anything(monkeypatch):
    bridge = _bridge(monkeypatch, FakeSettings(nsfw_mode="show"), client_answers={
        "search": {"works": [_work("2", x_restrict=1)], "total": 1}})

    work = bridge.search("x")["works"][0]

    assert "blurred" not in work


def test_the_tag_alone_is_enough_to_treat_a_work_as_adult(monkeypatch):
    """有些作品带 R-18 标签但没有 xRestrict 标记。"""
    bridge = _bridge(monkeypatch, client_answers={
        "search": {"works": [_work("3", tags=["R-18"])], "total": 1}})

    assert bridge.search("x")["works"] == []


def test_ai_works_can_be_hidden(monkeypatch):
    bridge = _bridge(monkeypatch, FakeSettings(hide_ai=True), client_answers={
        "search": {"works": [_work("1"), _work("2", ai_type=2)], "total": 2}})

    assert [w["id"] for w in bridge.search("x")["works"]] == ["1"]


def test_hiding_adult_content_also_asks_the_server_for_safe_mode(monkeypatch):
    """界面藏起来不算够，请求本身就得带 safe，否则流量和缩略图都白拿。"""
    bridge = _bridge(monkeypatch, client_answers={"search": {"works": [], "total": 0}})

    bridge.search("x")

    call = [c for c in bridge.session.client.calls if c[0] == "search"][0]
    assert call[1]["mode"] == "safe"


def test_showing_adult_content_switches_the_request_to_all(monkeypatch):
    bridge = _bridge(monkeypatch, FakeSettings(nsfw_mode="show", include_r18=True),
                     client_answers={"search": {"works": [], "total": 0}})

    bridge.search("x")

    call = [c for c in bridge.session.client.calls if c[0] == "search"][0]
    assert call[1]["mode"] == "all"


# ------------------------------------------------------------------- 缓存


def test_the_same_search_does_not_hit_pixiv_twice(monkeypatch):
    bridge = _bridge(monkeypatch, client_answers={"search": {"works": [_work()], "total": 1}})

    bridge.search("x")
    bridge.search("x")

    assert len([c for c in bridge.session.client.calls if c[0] == "search"]) == 1


def test_changing_the_filter_does_not_need_a_new_request(monkeypatch):
    """过滤发生在缓存之外：设置一改立刻生效，不用等缓存过期。"""
    answers = {"search": {"works": [_work("1"), _work("2", x_restrict=1)], "total": 2}}
    bridge = _bridge(monkeypatch, client_answers=answers)
    assert len(bridge.search("x")["works"]) == 1

    import src.config.settings as settings_module

    visible = FakeSettings(nsfw_mode="show")
    monkeypatch.setattr(settings_module, "get_settings",
                        lambda: type("S", (), {"pixiv": visible})())

    assert len(bridge.search("x")["works"]) == 2
    assert len([c for c in bridge.session.client.calls if c[0] == "search"]) == 1


def test_bookmarks_are_not_cached(monkeypatch):
    """收藏跟着账号变，不跟着时间变。"""
    bridge = _bridge(monkeypatch, client_answers={
        "bookmarks": {"works": [_work()], "total": 1}})

    bridge.bookmarks()
    bridge.bookmarks()

    assert len([c for c in bridge.session.client.calls if c[0] == "bookmarks"]) == 2


# ------------------------------------------------------------------- 详情


def test_a_detail_page_keeps_adult_work_but_marks_it(monkeypatch):
    """用户自己点进来的作品不能因为设置就变成空页。"""
    bridge = _bridge(monkeypatch, client_answers={
        "illust": _work("9", x_restrict=1, sl=6),
        "illust_pages": [{"index": 0, "regular": "https://i.pximg.net/p0.jpg"}],
    })

    work = bridge.illust("9")

    assert work["id"] == "9"
    assert work["nsfw"] is True


def test_the_detail_pages_carry_a_proxy_url(monkeypatch):
    media = FakeMedia()
    bridge = _bridge(monkeypatch, media=media, client_answers={
        "illust": _work("9"),
        "illust_pages": [{"index": 0, "regular": "https://i.pximg.net/p0.jpg"}],
    })

    work = bridge.illust("9")

    assert work["pages"][0]["proxy"].startswith("http://127.0.0.1:1/pixiv")
    assert media.seen, "地址必须经过媒体服务器"


def test_a_rejected_image_host_leaves_the_url_alone(monkeypatch):
    """代理不认这个主机时不能拼出半个地址，退回原地址最多裂一张图。"""
    bridge = _bridge(monkeypatch, media=FakeMedia(reject=True), client_answers={
        "search": {"works": [_work("1")], "total": 1}})

    work = bridge.search("x")["works"][0]

    assert "thumb_proxy" not in work
    assert work["thumb"].startswith("https://i.pximg.net")


# ------------------------------------------------------------------- 写


def test_unbookmarking_uses_the_bookmark_id_not_the_artwork_id(monkeypatch):
    """取消收藏要的是 bookmark id，传作品 id 对端回 400 且不解释。"""
    bridge = _bridge(monkeypatch, client_answers={
        "illust": _work("9", bookmark_id="888"),
    })

    result = bridge.bookmark("9", add=False)

    assert result["ok"] is True
    call = [c for c in bridge.session.client.calls if c[0] == "bookmark_delete"][0]
    assert call[1]["bookmark_id"] == "888"


def test_unbookmarking_something_not_bookmarked_says_so(monkeypatch):
    bridge = _bridge(monkeypatch, client_answers={"illust": _work("9", bookmark_id="")})

    result = bridge.bookmark("9", add=False)

    assert result["ok"] is False
    assert "没有收藏" in result["message"]


def test_a_bookmark_uses_the_private_default_from_the_settings(monkeypatch):
    bridge = _bridge(monkeypatch, FakeSettings(bookmark_private=True), client_answers={})

    bridge.bookmark("9", add=True)

    call = [c for c in bridge.session.client.calls if c[0] == "bookmark_add"][0]
    assert call[1]["private"] is True


def test_writing_without_a_login_is_refused_early(monkeypatch):
    bridge = _bridge(monkeypatch, logged_in=False)

    result = bridge.bookmark("9")

    assert result["ok"] is False
    assert bridge.session.client.calls == []


def test_a_successful_write_drops_the_cached_detail(monkeypatch):
    """收藏状态变了，详情缓存里的 bookmarkData 立刻过时。"""
    bridge = _bridge(monkeypatch, client_answers={
        "illust": _work("9", bookmark_id=""),
        "illust_pages": [],
    })
    bridge.illust("9")
    bridge.bookmark("9")

    bridge.illust("9")

    assert len([c for c in bridge.session.client.calls if c[0] == "illust"]) == 2


# ------------------------------------------------------------------- 登录


def test_a_cookie_without_the_session_id_is_rejected(monkeypatch):
    bridge = _bridge(monkeypatch, )

    result = bridge.login_cookie("device_token=abc; p_ab_id=6")

    assert result["success"] is False
    assert bridge.session.saved == []


def test_a_pasted_cookie_is_cut_down_and_saved(monkeypatch):
    bridge = _bridge(monkeypatch, )

    result = bridge.login_cookie("PHPSESSID=abc; _ga=GA1; device_token=d")

    assert result["success"] is True
    cookie = bridge.session.saved[-1][0]
    assert cookie.startswith("PHPSESSID=abc")
    assert "device_token=d" in cookie
    assert "_ga" not in cookie


def test_logout_forgets_the_session_on_disk(monkeypatch):
    bridge = _bridge(monkeypatch, )

    bridge.logout()

    assert bridge.session.cleared == 1
    assert bridge.login_status()["state"] == "idle"


def test_the_login_state_reports_an_expired_cookie_instead_of_logging_out(monkeypatch):
    """cookie 过期只报过期，不删——用户可能只是断网了一分钟。"""
    bridge = _bridge(monkeypatch, )
    bridge.session.account_info = lambda refresh=False: {
        "id": "42", "name": "星", "logged_in": True, "expired": True}

    state = bridge.state()

    assert state["expired"] is True
    assert state["logged_in"] is True


def test_exporting_without_a_login_is_refused(monkeypatch):
    bridge = _bridge(monkeypatch, logged_in=False)

    result = bridge.export_data()

    assert result["ok"] is False
    assert "先登录" in result["message"]


# ------------------------------------------------------------------- 首页


def test_the_home_screen_works_without_a_login(monkeypatch):
    """没登录也要有东西看：推荐流是不需要会话的。"""
    bridge = _bridge(monkeypatch, logged_in=False, client_answers={
        "discovery": [_work("1")]})

    home = bridge.home()

    assert [w["id"] for w in home["discover"]] == ["1"]
    assert home["feed"] == []
    assert home["logged_in"] is False


def test_the_home_screen_adds_the_follow_feed_when_signed_in(monkeypatch):
    bridge = _bridge(monkeypatch, client_answers={
        "discovery": [_work("1")],
        "feed": {"works": [_work("2")], "has_more": True},
    })

    home = bridge.home()

    assert [w["id"] for w in home["feed"]] == ["2"]


def test_a_pixiv_error_is_carried_out_instead_of_swallowed(monkeypatch):
    """``error`` 要在返回值里，界面才有话可说；不然就是一片空白。"""
    bridge = _bridge(monkeypatch, client_answers={
        "search": {"works": [], "total": 0, "error": "服务器回了 429。"}})

    assert bridge.search("x")["error"] == "服务器回了 429。"


def test_an_empty_keyword_does_not_reach_the_client(monkeypatch):
    bridge = _bridge(monkeypatch, )

    assert bridge.search("  ")["works"] == []
    assert bridge.session.client.calls == []


@pytest.mark.parametrize("mode,expected", [
    ("hide", "safe"), ("blur", "safe"), ("show", "safe"),
])
def test_show_mode_without_include_r18_still_asks_for_safe(monkeypatch, mode, expected):
    """「允许显示」和「请求 R-18」是两件事：没勾后者就不要它。"""
    bridge = _bridge(monkeypatch, FakeSettings(nsfw_mode=mode, include_r18=False))

    assert bridge._search_mode() == expected
