"""B站桥接层：把核心客户端的回答翻译成前端要的形状。

用的是假客户端，不打真网络。
"""
from __future__ import annotations

import pytest

from src.gui.bilibili_bridge import BilibiliBridge, track_payload


class FakeClient:
    """假 B站客户端：每个方法都返回固定答案。"""

    def __init__(self) -> None:
        self.is_logged_in = False
        self.cookie = ""
        self.calls: list[tuple[str, tuple]] = []

    def _record(self, name: str, *args):
        self.calls.append((name, args))

    def set_cookie(self, cookie: str) -> None:
        self.cookie = cookie
        self.is_logged_in = "SESSDATA" in cookie

    def account_info(self) -> dict:
        self._record("account_info")
        return {"logged_in": self.is_logged_in, "mid": "42", "uname": "某人",
                "face": "https://example.invalid/face.jpg", "level": 5,
                "vip_status": 1, "vip_label": "大会员", "coins": 88}

    def login_qrcode(self) -> dict:
        self._record("login_qrcode")
        return {"success": True, "qrcode_url": "https://passport.invalid/qr.png",
                "key": "key-1", "message": ""}

    def login_poll(self, key: str) -> dict:
        self._record("login_poll", key)
        if key == "key-1":
            return {"success": False, "status": "waiting", "cookie": "",
                    "message": "等待扫码"}
        return {"success": True, "status": "success",
                "cookie": "SESSDATA=abc; bili_jct=def", "message": ""}

    def login_cookie(self, cookie: str) -> dict:
        self._record("login_cookie", cookie)
        ok = "SESSDATA" in cookie
        return {"success": ok, "cookie": cookie if ok else "",
                "message": "" if ok else "这个 Cookie 无效"}

    def login_password(self, username, password, **kwargs) -> dict:
        self._record("login_password", username, password)
        if password == "wrong":
            return {"success": False, "cookie": "", "message": "密码不对",
                    "captcha_required": True, "captcha_key": "ck-1"}
        return {"success": True, "cookie": "SESSDATA=abc; bili_jct=def",
                "message": "", "captcha_required": False, "captcha_key": ""}

    def captcha(self) -> dict:
        self._record("captcha")
        return {"success": True, "gt": "gt", "challenge": "ch", "token": "tk",
                "message": ""}

    def send_sms_code(self, tel, cid="86", **kwargs) -> dict:
        self._record("send_sms_code", tel, cid)
        return {"success": True, "message": ""}

    def login_sms(self, tel, code, cid="86", **kwargs) -> dict:
        self._record("login_sms", tel, code, cid)
        return {"success": True, "cookie": "SESSDATA=abc", "message": ""}

    def register_send_code(self, tel, cid="86", **kwargs) -> dict:
        self._record("register_send_code", tel, cid)
        return {"success": True, "message": ""}

    def register(self, username, password, tel, code, cid="86", **kwargs) -> dict:
        self._record("register", username, password, tel, code, cid)
        return {"success": True, "cookie": "SESSDATA=abc", "message": ""}

    def logout(self) -> dict:
        self._record("logout")
        return {"success": True, "message": ""}

    def favorite_folders(self) -> list[dict]:
        self._record("favorite_folders")
        return [{"id": "9", "title": "我的收藏", "media_count": 2, "cover": "c"}]

    def favorite_videos(self, folder_id, page=1, page_size=30) -> dict:
        self._record("favorite_videos", folder_id, page, page_size)
        return {"items": [{"bvid": "BV1aa", "aid": "11", "title": "第一",
                           "author": "甲", "cover": "cover.jpg", "duration": 61}],
                "has_more": False, "page": page}

    # -- interactions ---------------------------------------------------------
    def video_stat(self, bvid) -> dict:
        self._record("video_stat", bvid)
        return {"success": True, "stat": {"view": 100, "danmaku": 5, "reply": 2,
                                         "favorite": 3, "coin": 4, "like": 6,
                                         "share": 1}, "message": ""}

    def like_video(self, bvid, like=True) -> dict:
        self._record("like_video", bvid, like)
        return {"success": True, "message": ""}

    def coin_video(self, bvid, count=1, also_like=False) -> dict:
        self._record("coin_video", bvid, count, also_like)
        return {"success": True, "message": "", "balance": 42}

    def coin_balance(self) -> dict:
        self._record("coin_balance")
        return {"success": True, "balance": 42, "coin": 7, "message": ""}

    def favorite_video(self, bvid, folder_id="", remove=False) -> dict:
        self._record("favorite_video", bvid, folder_id, remove)
        return {"success": True, "folder_id": folder_id or "9", "message": ""}

    def favorite_folder(self, folder_id, remove=False) -> dict:
        self._record("favorite_folder", folder_id, remove)
        return {"success": True, "message": ""}

    def comments(self, bvid, page=1, sort="hot") -> dict:
        self._record("comments", bvid, page, sort)
        return {"success": True, "items": [{"rpid": "1", "uname": "甲",
                                            "content": "好看"}],
                "count": 1, "has_more": False, "message": ""}

    def post_comment(self, bvid, content, parent_rpid=0) -> dict:
        self._record("post_comment", bvid, content, parent_rpid)
        return {"success": True, "rpid": "99", "message": ""}

    def like_comment(self, rpid, like=True) -> dict:
        self._record("like_comment", rpid, like)
        return {"success": True, "message": ""}

    def history(self, page=1, page_size=30) -> dict:
        self._record("history", page, page_size)
        return {"success": True, "items": [{"bvid": "BV1aa"}], "count": 1,
                "has_more": False, "message": ""}

    def space(self, mid) -> dict:
        self._record("space", mid)
        return {"success": True, "profile": {"mid": mid, "name": "某人",
                                             "fans": 10, "following": 2},
                "message": ""}

    def space_videos(self, mid, page=1, page_size=30) -> dict:
        self._record("space_videos", mid, page, page_size)
        return {"success": True, "items": [{"bvid": "BV1bb"}], "count": 1,
                "has_more": False, "message": ""}

    def follow_user(self, mid, follow=True) -> dict:
        self._record("follow_user", mid, follow)
        return {"success": True, "message": ""}

    def follow_state(self, mid) -> dict:
        self._record("follow_state", mid)
        return {"success": True, "following": True, "relation": 2, "message": ""}

    def video_streams(self, bvid, quality=0) -> dict:
        self._record("video_streams", bvid, quality)
        return {
            "success": True, "bvid": bvid, "cid": 7, "duration": 61,
            "qualities": [{"id": 80, "name": "1080P 高清", "available": True}],
            "audio": {"url": "https://upos.bilivideo.com/a.m4s", "mime": "audio/mp4",
                      "codecs": "mp4a.40.2", "bandwidth": 128000, "size": 100,
                      "init_range": "0-821"},
            "video": {"url": "https://upos.bilivideo.com/v.m4s", "mime": "video/mp4",
                      "codecs": "avc1.640032", "bandwidth": 1000000, "size": 200,
                      "width": 1920, "height": 1080, "init_range": "0-821"},
            "muxed": None, "flac": None, "dolby": None, "message": "",
        }


class FakeMedia:
    """假回环服务器：把 CDN 地址换成回环地址。"""

    def __init__(self) -> None:
        self.cookie = ""

    def url_for_bili(self, url: str):
        if "bilivideo.com" not in url:
            return None
        return f"http://127.0.0.1:9999/m/token/bili?u={url}"

    def set_bili_cookie(self, cookie: str) -> None:
        self.cookie = cookie


class FakeSession:
    """假会话：记住 cookie。"""

    def __init__(self, client) -> None:
        self.client = client
        self.saved = ""

    @property
    def is_logged_in(self) -> bool:
        return bool(self.client.is_logged_in)

    def account_info(self, refresh: bool = False) -> dict:
        return self.client.account_info()

    def save(self, cookie: str, account=None) -> bool:
        self.saved = cookie
        self.client.set_cookie(cookie)
        return True

    def clear(self) -> bool:
        self.saved = ""
        self.client.set_cookie("")
        return True

    def apply_to_media(self, media) -> None:
        media.set_bili_cookie(self.saved)


@pytest.fixture()
def bridge():
    client = FakeClient()
    media = FakeMedia()
    session = FakeSession(client)
    # 默认就是登录状态：收藏夹这类接口没有登录时直接拒绝。
    client.set_cookie("SESSDATA=abc; bili_jct=def")
    return BilibiliBridge(client=client, session=session, media=media)


def test_the_account_is_reported(bridge):
    bridge.login_cookie("SESSDATA=abc; bili_jct=def")

    result = bridge.account(refresh=True)

    assert result["logged_in"] is True
    assert result["uname"] == "某人"
    assert result["level"] == 5


def test_the_qr_code_is_asked_for_and_polled(bridge):
    qr = bridge.login_qrcode()
    assert qr["success"] is True
    assert qr["qrcode_url"].endswith("qr.png")

    waiting = bridge.login_poll()
    assert waiting["status"] == "waiting"

    # 换一个 key 代表换了一张码，这次轮询拿到的是登录成功。
    bridge._client.calls.append(("x", ()))
    bridge._qr_key = "key-2"
    done = bridge.login_poll()
    assert done["status"] == "success"
    assert bridge.session.saved.startswith("SESSDATA=")


def test_a_bad_cookie_is_reported(bridge):
    result = bridge.login_cookie("nothing")

    assert result["success"] is False
    assert result["message"]


def test_a_good_cookie_is_remembered(bridge):
    result = bridge.login_cookie("SESSDATA=abc; bili_jct=def")

    assert result["success"] is True
    assert "SESSDATA=abc" in bridge.session.saved


def test_the_password_login_reports_a_captcha(bridge):
    result = bridge.login_password("user", "wrong")

    assert result["success"] is False
    assert result["captcha_required"] is True
    assert result["captcha_key"] == "ck-1"


def test_the_folders_are_normalised(bridge):
    result = bridge.favorites()

    assert result["success"] is True
    assert result["folders"][0]["title"] == "我的收藏"
    assert result["count"] == 1


def test_the_favourite_videos_become_tracks(bridge):
    result = bridge.favorite_videos("9")

    assert result["success"] is True
    track = result["items"][0]
    assert track["source"] == "bilibili"
    assert track["bvid"] == "BV1aa"
    assert track["key"] == "bilibili:BV1aa"
    assert track["name"] == "第一"
    assert track["duration"] == 61


def test_the_streams_go_through_the_loopback(bridge):
    result = bridge.video_streams("BV1aa", quality=80)

    assert result["success"] is True
    assert result["video"]["url"].startswith("http://127.0.0.1:9999/m/token/bili")
    # 原始地址留着，排查问题时用得上。
    assert result["video"]["origin_url"] == "https://upos.bilivideo.com/v.m4s"
    assert result["audio"]["init_range"] == "0-821"


def test_an_empty_download_is_refused(bridge):
    result = bridge.download([], {})

    assert result["success"] is False
    assert result["count"] == 0


def test_the_track_payload_carries_the_video_kind():
    payload = track_payload({"bvid": "BV1xx", "title": "标题", "author": "甲"})

    assert payload["kind"] == "video"
    assert payload["source"] == "bilibili"
    assert payload["key"] == "bilibili:BV1xx"


# ------------------------------------------------------------------ 互动

def test_the_video_counters_are_translated(bridge):
    result = bridge.video_stat("BV1aa")

    assert result["success"] is True
    assert result["stat"]["view"] == 100
    assert result["stat"]["coin"] == 4


def test_the_like_and_the_coin_reach_the_core(bridge):
    assert bridge.like_video("BV1aa", like=True)["success"] is True
    assert bridge.coin_video("BV1aa", count=2)["success"] is True
    assert bridge.coin_balance()["balance"] == 42

    names = [name for name, _ in bridge.client.calls]
    assert "like_video" in names
    assert "coin_video" in names


def test_the_favourite_write_reaches_the_core(bridge):
    result = bridge.favorite_video("BV1aa", folder_id="9")

    assert result["success"] is True
    assert result["folder_id"] == "9"


def test_the_comments_are_translated(bridge):
    result = bridge.comments("BV1aa")

    assert result["success"] is True
    assert result["comments"][0]["content"] == "好看"
    assert result["count"] == 1


def test_writing_a_comment_reports_the_new_id(bridge):
    result = bridge.post_comment("BV1aa", "好看", parent_rpid=3)

    assert result["success"] is True
    assert result["rpid"] == "99"


def test_the_history_and_the_profile_are_translated(bridge):
    assert bridge.history()["items"][0]["bvid"] == "BV1aa"
    assert bridge.space("42")["profile"]["name"] == "某人"
    assert bridge.space_videos("42")["items"][0]["bvid"] == "BV1bb"


def test_following_is_translated(bridge):
    assert bridge.follow_user("42", follow=True)["success"] is True
    assert bridge.follow_state("42")["following"] is True


def test_a_broken_core_method_becomes_a_failure(bridge):
    def broken(*args, **kwargs):
        raise RuntimeError("network down")

    bridge.client.like_video = broken

    result = bridge.like_video("BV1aa")

    assert result["success"] is False
    assert result["message"]
