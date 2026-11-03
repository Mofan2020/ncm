"""B站那半边的备份与恢复。

用的是假对象，不打真网络，也不碰真实账号：

* 导出时把 B站账号和收藏夹读出来放进同一个 ``.ncmbk`` 文件
* 备份里没有 B站数据时，网易云那半边照常导入
* 备份里有 B站数据但当前没登录 B站时，那一节跳过并说明原因
* 登录了 B站时，收藏夹会被重建，视频会被加进去
"""
from __future__ import annotations

from src.core import backup as backup_module
from src.core.backup import (
    BACKUP_FORMAT,
    BACKUP_VERSION,
    apply_import,
    export_bilibili_data,
    plan_import,
    read_backup_file,
    write_backup_file,
)


class FakeResponse:
    """假 HTTP 回答。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """假传输层：记录写操作，回一个写成功的回答。"""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.next_id = 900

    def post(self, url: str, data: dict | None = None, headers: dict | None = None,
             timeout=None):
        self.posts.append((url, dict(data or {})))
        if url.endswith("/x/v3/fav/folder/add"):
            self.next_id += 1
            return FakeResponse({"code": 0, "message": "0",
                                 "data": {"id": self.next_id}})
        return FakeResponse({"code": 0, "message": "0", "data": None})


class FakeBili:
    """假 B站客户端：读得到收藏夹，也写得进去。"""

    def __init__(self, folders=None, videos=None, signed_in: bool = True) -> None:
        self._folders = folders if folders is not None else [
            {"id": "9", "title": "我的收藏", "media_count": 2, "cover": ""},
        ]
        self._videos = videos if videos is not None else {
            "9": [{"bvid": "BV1aa", "aid": "11", "title": "第一", "author": "甲"},
                  {"bvid": "BV1bb", "aid": "22", "title": "第二", "author": "乙"}],
        }
        self.is_logged_in = signed_in
        self._session = FakeSession()

    def account_info(self) -> dict:
        return {"logged_in": self.is_logged_in, "mid": "42", "uname": "某人",
                "face": "https://example.invalid/face.jpg", "level": 5,
                "vip_status": 1, "vip_label": "大会员", "coins": 100}

    def favorite_folders(self) -> list[dict]:
        return list(self._folders)

    def favorite_videos(self, folder_id: str, page: int = 1,
                        page_size: int = 30) -> dict:
        items = self._videos.get(str(folder_id), [])
        return {"items": items, "has_more": False, "page": page}

    def csrf(self) -> str:
        return "token"

    def bili_headers(self, extra: dict | None = None) -> dict:
        return {"User-Agent": "test", "Cookie": "SESSDATA=x"}


class FakeAPI:
    """最小的假网易云接口。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_user_playlists_all(self, uid, fresh=False):
        self.calls.append("playlists")
        return []

    def get_liked_song_ids(self, uid):
        self.calls.append("liked")
        return []

    def get_followed_artists(self, limit=25, offset=0):
        self.calls.append("artists")
        return {"artists": [], "hasMore": False}

    def get_liked_albums(self, uid, limit=100, offset=0):
        self.calls.append("albums")
        return {"albums": [], "hasMore": False}

    def get_subscribed_djradios(self, uid, limit=100, offset=0):
        self.calls.append("djradios")
        return {"djRadios": [], "hasMore": False}

    def get_subscribed_mvs(self, uid, limit=100, offset=0):
        self.calls.append("mvs")
        return {"mvs": [], "hasMore": False}

    def get_play_record(self, uid, scope="all"):
        self.calls.append("record")
        return []


def _bili_payload() -> dict:
    return {
        "account": {"mid": "42", "uname": "某人", "face": ""},
        "folders": [
            {"id": "9", "title": "我的收藏", "media_count": 2,
             "videos": [{"bvid": "BV1aa", "aid": "11", "title": "第一", "author": "甲"},
                        {"bvid": "BV1bb", "aid": "22", "title": "第二", "author": "乙"}]},
        ],
        "errors": [],
    }


# ------------------------------------------------------------------ 导出

def test_the_export_carries_the_account_and_the_folders():
    bili = FakeBili()

    section = export_bilibili_data(bili, sleep=lambda _s: None)

    assert section["account"]["uname"] == "某人"
    assert len(section["folders"]) == 1
    assert section["folders"][0]["title"] == "我的收藏"
    assert [video["bvid"] for video in section["folders"][0]["videos"]] == \
        ["BV1aa", "BV1bb"]


def test_the_export_asks_for_a_page_size_bilibili_accepts():
    """B站 caps the folder list at 40 items per page.

    Asking for 41 comes back as code -400 and an *empty* list, so the
    export used to produce a backup with every folder empty and no error
    at all.  The page size has to stay inside the cap.
    """
    assert backup_module.BILI_FOLDER_PAGE <= 40, \
        "B站 refuses more than 40 items per page"

    seen: list[int] = []

    class Watching(FakeBili):
        def favorite_videos(self, folder_id: str, page: int = 1,
                            page_size: int = 30) -> dict:
            seen.append(page_size)
            return super().favorite_videos(folder_id, page=page,
                                           page_size=page_size)

    export_bilibili_data(Watching(), sleep=lambda _s: None)

    assert seen, "the export did not read any folder"
    assert max(seen) <= 40


def test_a_refused_folder_read_is_reported_and_not_silently_empty():
    """An empty list and a refused request look the same from outside.

    When B站 says no, the reason has to come along, otherwise the backup
    looks complete while holding nothing.
    """
    class Refused(FakeBili):
        def favorite_videos(self, folder_id: str, page: int = 1,
                            page_size: int = 30) -> dict:
            return {"items": [], "has_more": False, "page": page,
                    "error": "请求错误"}

    section = export_bilibili_data(Refused(), sleep=lambda _s: None)

    assert section["errors"], "a refused read was swallowed"
    assert "请求错误" in section["errors"][0]["message"]


def test_the_export_survives_a_failing_read():
    class Broken(FakeBili):
        def favorite_folders(self):
            raise RuntimeError("network down")

    section = export_bilibili_data(Broken(), sleep=lambda _s: None)

    assert section["folders"] == []
    assert section["errors"]


def test_the_backup_file_carries_the_bilibili_section(tmp_path):
    payload = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "bilibili": export_bilibili_data(FakeBili(), sleep=lambda _s: None),
    }
    path = tmp_path / "account.ncmbk"

    write_backup_file(path, payload)
    restored = read_backup_file(path)

    assert restored["bilibili"]["folders"][0]["title"] == "我的收藏"


# ------------------------------------------------------------------ 导入

def test_a_backup_without_bilibili_still_imports():
    payload = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION,
               "liked_song_ids": [], "playlists": []}
    api = FakeAPI()

    plan = plan_import(api, payload, uid="42")
    result = apply_import(api, payload, plan, {}, sleep=lambda _s: None)

    assert plan["bilibili"]["available"] is False
    assert plan["bilibili"]["folder_count"] == 0
    assert result["ok"] is True
    assert result["bilibili"]["folders_created"] == 0


def test_the_bilibili_part_is_skipped_when_not_signed_in():
    payload = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION,
               "liked_song_ids": [], "playlists": [], "bilibili": _bili_payload()}
    api = FakeAPI()

    plan = plan_import(api, payload, uid="42", bili=None)

    assert plan["bilibili"]["available"] is False
    assert plan["bilibili"]["folder_count"] == 1
    assert plan["bilibili"]["message"]
    # 网易云那半边照常计划。
    assert plan["netease_available"] is True


def test_the_bilibili_part_is_planned_when_signed_in():
    payload = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION,
               "liked_song_ids": [], "playlists": [], "bilibili": _bili_payload()}
    bili = FakeBili()

    plan = plan_import(FakeAPI(), payload, uid="42", bili=bili)

    assert plan["bilibili"]["available"] is True
    assert plan["bilibili"]["video_count"] == 2


def test_the_import_rebuilds_the_folders_and_adds_the_videos():
    payload = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION,
               "liked_song_ids": [], "playlists": [], "bilibili": _bili_payload()}
    bili = FakeBili()
    api = FakeAPI()

    plan = plan_import(api, payload, uid="42", bili=bili)
    result = apply_import(api, payload, plan, {}, sleep=lambda _s: None, bili=bili)

    assert result["bilibili"]["folders_created"] == 1
    assert result["bilibili"]["videos_added"] == 2
    urls = [url for url, _ in bili._session.posts]
    assert any(url.endswith("/x/v3/fav/folder/add") for url in urls)
    assert any(url.endswith("/x/v3/fav/resource/deal") for url in urls)
    deal = [data for url, data in bili._session.posts
            if url.endswith("/x/v3/fav/resource/deal")]
    assert deal[0]["add_media_ids"] == "11:2,22:2"
    assert deal[0]["csrf"] == "token"


def test_importing_without_a_signed_in_account_still_restores_the_folders():
    """只有 B站数据、没有网易云账号时，也能把 B站收藏夹恢复回去。"""
    payload = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION,
               "liked_song_ids": [], "playlists": [], "bilibili": _bili_payload()}
    bili = FakeBili()
    api = FakeAPI()

    plan = plan_import(api, payload, uid="", bili=bili)
    result = apply_import(api, payload, plan, {}, sleep=lambda _s: None, bili=bili)

    assert plan["netease_available"] is False
    assert result["bilibili"]["folders_created"] == 1
    # 没登录网易云就不该去碰网易云的接口。
    assert api.calls == []


def test_the_backup_format_is_version_four():
    assert BACKUP_VERSION == 4
    assert BACKUP_FORMAT == "ncm-account-backup"


def test_an_old_backup_still_reads():
    payload = {"format": BACKUP_FORMAT, "version": 3,
               "liked_song_ids": ["1"], "playlists": []}
    section = backup_module._bili_plan(payload, None)

    assert section["available"] is False
    assert section["folder_count"] == 0
