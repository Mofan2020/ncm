"""账号数据备份与恢复的测试。

用的是假 API 对象，不打真网络。覆盖的东西：

* 导出产出的 JSON 结构完整，带 ``format`` 和版本号
* 某一节读失败时导出**不整体失败**，别的部分照常，失败的部分在结果里报告
* 云端读不到（401）时退到本地缓存导出
* 云端限流（405）时重试几次就好
* 导入时同名歌单被认成冲突
* 导入时已经存在的收藏被跳过，不重复加
* 导入前必须先拿到用户的冲突决定，没决定就不写入
* 导入调用写操作的次数和分批逻辑
"""
from __future__ import annotations

import json

import pytest

from src.core import backup as backup_module
from src.core.backup import (
    BACKUP_FORMAT,
    BACKUP_SUFFIX,
    BACKUP_VERSION,
    SOURCE_CACHE,
    SOURCE_CLOUD,
    SOURCE_MISSING,
    apply_import,
    default_backup_name,
    export_account_data,
    plan_import,
    read_backup_file,
    write_backup_file,
)


class FakeAPIError(Exception):
    """带状态码的假接口错误。"""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(f"code {code} {message}".strip())
        self.code = code


class FakeCache:
    """假本地缓存：只有几个键而已。

    键跟真的 :class:`~src.core.metacache.MetaCache` 一样：歌单列表是
    ``userplaylists:<uid>:100:0``，歌单歌曲是 ``tracks:<歌单 id>``。
    """

    def __init__(self, playlists=None, tracks=None) -> None:
        self._playlists = playlists or {}
        self._tracks = tracks or {}

    def get_playlist(self, key: str):
        if key.startswith("userplaylists:"):
            return self._playlists.get(key)
        if key.startswith("tracks:"):
            return self._tracks.get(key[len("tracks:"):])
        return None

    def get_meta(self, key: str):
        return None


def make_playlist(pid: str, name: str, owner: str = "42", track_count: int = 2,
                  special_type: int = 0) -> dict:
    """一个接口形状的歌单。"""
    return {
        "id": int(pid),
        "name": name,
        "coverImgUrl": f"https://example.invalid/{pid}.jpg",
        "creator": {"userId": int(owner), "nickname": f"user{owner}"},
        "trackCount": track_count,
        "subscribedCount": 3,
        "specialType": special_type,
    }


class FakeAPI:
    """假 API：每个方法都能单独安排它成功、失败或者限流。"""

    def __init__(self, playlists=None, liked=None, tracks=None, details=None) -> None:
        self.playlists = playlists if playlists is not None else []
        self.liked = liked if liked is not None else []
        self.tracks = tracks or {}
        self.details = details or {}
        #: 账号关注的歌手。
        self.artists: list[dict] = []
        #: 每个方法失败几次之后才成功。
        self.fail_times: dict[str, int] = {}
        #: 每个方法一直失败时抛的错误。
        self.fail_with: dict[str, FakeAPIError] = {}
        #: 只有这几个歌单读不出来（键是歌单 id）。
        self.broken_playlists: set[str] = set()
        self.calls: list[tuple[str, tuple]] = []
        self.liked_calls: list[str] = []
        self.created: list[str] = []
        self.added: list[tuple[str, list[str]]] = []
        #: 关注 / 取关的记录。
        self.followed: list[tuple[str, bool]] = []

    def _call(self, name: str, *args):
        self.calls.append((name, args))
        error = self.fail_with.get(name)
        if error is not None:
            raise error
        remaining = self.fail_times.get(name, 0)
        if remaining > 0:
            self.fail_times[name] = remaining - 1
            raise FakeAPIError(405, "操作过于频繁")
        return None

    # -- reads ------------------------------------------------------------
    def get_user_playlists_all(self, uid, max_pages=20, fresh=False):
        self._call("playlists")
        return list(self.playlists)

    def get_liked_song_ids(self, uid, use_cache=True):
        self._call("liked")
        self.liked_calls.append(str(uid))
        return list(self.liked)

    def get_playlist_info(self, playlist_id, max_tracks=1000, fresh=False):
        self._call("info")
        if str(playlist_id) in self.broken_playlists:
            raise FakeAPIError(500, "boom")
        return self.details.get(str(playlist_id))

    def get_playlist_songs(self, playlist_id, fresh=False):
        self._call("songs")
        if str(playlist_id) in self.broken_playlists:
            raise FakeAPIError(500, "boom")
        return self.tracks.get(str(playlist_id))

    def get_user_detail(self, user_id):
        self._call("detail")
        return {"profile": {"userId": user_id, "level": 8}}

    def get_followed_artists(self, limit=25, offset=0):
        self._call("artists")
        window = self.artists[offset:offset + limit]
        return {
            "artists": list(window),
            "count": len(self.artists),
            "has_more": offset + limit < len(self.artists),
            "code": 200,
        }

    # -- writes -----------------------------------------------------------
    def set_artist_follow(self, artist_id, follow=True):
        self._call("follow", artist_id)
        self.followed.append((str(artist_id), bool(follow)))
        return "ok"

    def set_song_like(self, song_id, like=True):
        self._call("like", song_id)
        self.liked_calls.append(str(song_id))
        return "ok"

    def create_playlist(self, name, privacy=0, description=""):
        self._call("create", name)
        self.created.append(name)
        return {"code": 200, "id": f"new-{len(self.created)}"}

    def add_tracks_to_playlist(self, playlist_id, track_ids):
        self._call("add", playlist_id)
        self.added.append((str(playlist_id), list(track_ids)))
        return {"code": 200}

    def subscribe_playlist(self, playlist_id, subscribe=True):
        self._call("subscribe", playlist_id)
        return {"code": 200}


@pytest.fixture()
def sleeper():
    """记录睡眠时长，不真的等。"""
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(float(seconds))

    fake_sleep.calls = calls  # type: ignore[attr-defined]
    return fake_sleep


def make_export(api: FakeAPI, **kwargs):
    """跑一次导出，睡眠用假的。"""
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("attempts", 3)
    return export_account_data(api, uid="42", **kwargs)


# ------------------------------------------------------------------ 导出

def test_export_produces_a_complete_document():
    """导出的 JSON 有 format、版本号和三大块数据。"""
    api = FakeAPI(
        playlists=[make_playlist("7", "My list", track_count=2)],
        liked=["11", "12"],
        details={"7": {"id": 7, "name": "My list", "trackIds": [{"id": 21}, {"id": 22}]}},
    )

    payload = make_export(api)

    assert payload["format"] == BACKUP_FORMAT
    assert payload["version"] == BACKUP_VERSION
    assert payload["account"]["user_id"] == "42"
    assert payload["liked_song_ids"] == ["11", "12"]
    assert len(payload["playlists"]) == 1
    playlist = payload["playlists"][0]
    assert playlist["id"] == "7"
    assert playlist["name"] == "My list"
    assert playlist["song_ids"] == ["21", "22"]
    assert playlist["is_own"] is True
    assert playlist["subscribed_count"] == 3
    assert playlist["track_count"] == 2
    assert playlist["cover_url"].endswith("7.jpg")


def test_export_marks_where_every_part_came_from():
    """每一节都标了来源：云端 / 本地缓存 / 缺失。"""
    api = FakeAPI(
        playlists=[make_playlist("7", "My list", track_count=1)],
        liked=["11"],
        details={"7": {"id": 7, "trackIds": [{"id": 21}]}},
    )

    payload = make_export(api)

    assert payload["sources"]["playlists"] == SOURCE_CLOUD
    assert payload["sources"]["favorites"] == SOURCE_CLOUD
    assert payload["sources"]["account"] == SOURCE_CLOUD
    assert payload["playlists"][0]["song_ids_source"] == SOURCE_CLOUD


def test_export_survives_one_failing_section():
    """一个歌单读失败时，导出照样完成，别的歌单照常写进去。"""
    api = FakeAPI(
        playlists=[make_playlist("7", "Good", track_count=1),
                   make_playlist("8", "Broken", track_count=1)],
        liked=["11"],
        details={"7": {"id": 7, "trackIds": [{"id": 21}]}},
    )
    # 8 号歌单的详情一直读不出来。
    api.broken_playlists.add("8")

    payload = make_export(api)

    assert payload["format"] == BACKUP_FORMAT
    assert payload["liked_song_ids"] == ["11"]
    good = next(item for item in payload["playlists"] if item["id"] == "7")
    broken = next(item for item in payload["playlists"] if item["id"] == "8")
    assert good["song_ids"] == ["21"]
    assert broken["song_ids"] == []
    assert broken["song_ids_source"] == SOURCE_MISSING
    # 失败的那一节在结果里报告出来。
    assert any(error["section"] == "playlist:8" for error in payload["errors"])


def test_export_falls_back_to_the_cache_when_the_cloud_is_gone():
    """云端 401 拿不到数据时，退到本地缓存尽力导出。"""
    api = FakeAPI()
    api.fail_with["playlists"] = FakeAPIError(401, "未登录")
    cache = FakeCache(
        playlists={
            "userplaylists:42:100:0": {
                "playlists": [make_playlist("7", "Cached list", track_count=2)],
                "more": False,
            },
        },
        tracks={"7": [{"id": 31}, {"id": 32}]},
    )

    payload = make_export(api, cache=cache)

    assert payload["sources"]["playlists"] == SOURCE_CACHE
    assert payload["playlists"][0]["id"] == "7"
    assert payload["playlists"][0]["song_ids"] == ["31", "32"]
    assert payload["playlists"][0]["song_ids_source"] == SOURCE_CACHE
    assert payload["sources"]["favorites"] == SOURCE_MISSING
    assert any(error["section"] == "playlists" for error in payload["errors"])


def test_export_retries_a_rate_limited_read():
    """405 限流不算失败，隔几秒重试几次就好。"""
    api = FakeAPI(
        playlists=[make_playlist("7", "My list", track_count=1)],
        liked=["11"],
        details={"7": {"id": 7, "trackIds": [{"id": 21}]}},
    )
    api.fail_times["playlists"] = 2

    slept: list[float] = []
    payload = make_export(api, sleep=slept.append)

    assert payload["sources"]["playlists"] == SOURCE_CLOUD
    assert payload["playlists"][0]["song_ids"] == ["21"]
    # 重试之间确实歇了几秒。
    assert slept and all(delay > 0 for delay in slept)


def test_export_gives_up_on_a_dead_session():
    """401 不是限流，重试没有意义，立刻放弃转缓存。"""
    api = FakeAPI()
    api.fail_with["playlists"] = FakeAPIError(401, "未登录")
    slept: list[float] = []

    payload = make_export(api, sleep=slept.append)

    assert payload["sources"]["playlists"] == SOURCE_MISSING
    # 一次都不该重试。
    assert slept == []


def test_export_uses_the_local_profile_when_the_cloud_profile_fails():
    """账号资料读不到就用本地会话里的那份。"""
    api = FakeAPI()
    account = {"user_id": "42", "nickname": "me", "avatar_url": "a", "vip_type": 3,
               "profile": {"level": 5}}

    payload = make_export(api, account=account, cloud_profile=lambda: None)

    assert payload["sources"]["account"] == SOURCE_CACHE
    assert payload["account"]["nickname"] == "me"
    assert payload["account"]["vip_type"] == 3
    assert payload["account"]["level"] == 5


# ------------------------------------------------------------------ 文件

def test_the_backup_file_is_json_with_the_ncmbk_suffix(tmp_path):
    """文件后缀是 .ncmbk，内容是 JSON，带 format 和版本号。"""
    api = FakeAPI(playlists=[make_playlist("7", "My list")], liked=["11"])
    payload = make_export(api)
    path = tmp_path / ("me_20261025-120000" + BACKUP_SUFFIX)

    written = write_backup_file(path, payload)

    assert written.endswith(BACKUP_SUFFIX)
    raw = json.loads((tmp_path / path.name).read_text(encoding="utf-8"))
    assert raw["format"] == BACKUP_FORMAT
    assert raw["version"] == BACKUP_VERSION


def test_the_default_file_name_carries_a_timestamp_and_the_suffix():
    """默认文件名带时间戳和 .ncmbk 后缀。"""
    from datetime import datetime

    name = default_backup_name("小明", datetime(2026, 10, 25, 13, 4, 5))

    assert name == "小明_20261025-130405.ncmbk"
    assert name.endswith(BACKUP_SUFFIX)


def test_reading_a_foreign_file_is_refused(tmp_path):
    """不是本程序写出来的文件不能当备份读。"""
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    with pytest.raises(backup_module.BackupFormatError):
        read_backup_file(path)


def test_reading_a_newer_backup_is_refused(tmp_path):
    """版本比程序新的备份读不了。"""
    path = tmp_path / ("new" + BACKUP_SUFFIX)
    path.write_text(json.dumps({"format": BACKUP_FORMAT, "version": 99}),
                    encoding="utf-8")

    with pytest.raises(backup_module.BackupFormatError):
        read_backup_file(path)


# ------------------------------------------------------------------ 导入

def make_backup(playlists=None, liked=None) -> dict:
    """一份备份文件的内容（导出结果的形状）。"""
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": "2026-10-25T12:00:00",
        "sources": {"account": SOURCE_CLOUD, "favorites": SOURCE_CLOUD,
                    "playlists": SOURCE_CLOUD},
        "account": {"user_id": "42", "nickname": "me"},
        "liked_song_ids": list(liked or []),
        "playlists": playlists or [],
        "errors": [],
    }


def test_import_detects_a_playlist_with_the_same_name():
    """新账号里已经有同名歌单，那就是冲突，要问用户。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "My list", "is_own": True, "song_ids": ["21", "22"],
    }])
    api = FakeAPI(playlists=[make_playlist("99", "My list")])

    plan = plan_import(api, backup, uid="42")

    assert len(plan["conflicts"]) == 1
    conflict = plan["conflicts"][0]
    assert conflict["name"] == "My list"
    assert conflict["current_id"] == "99"
    assert conflict["song_count"] == 2
    entry = next(item for item in plan["playlists"] if item["id"] == "7")
    assert entry["action"] == "conflict"
    assert entry["current_id"] == "99"


def test_import_creates_a_playlist_when_the_name_is_free():
    """没有同名歌单就直接新建一个。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "Fresh", "is_own": True, "song_ids": ["21"],
    }])
    api = FakeAPI(playlists=[make_playlist("99", "Other")])

    plan = plan_import(api, backup, uid="42")

    assert plan["conflicts"] == []
    entry = next(item for item in plan["playlists"] if item["id"] == "7")
    assert entry["action"] == "create"


def test_import_skips_playlists_created_by_somebody_else():
    """别人创建的歌单没法在当前账号下重建，跳过并说明。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "Someone else's", "is_own": False, "song_ids": ["21"],
    }])
    api = FakeAPI()

    plan = plan_import(api, backup, uid="42")

    entry = next(item for item in plan["playlists"] if item["id"] == "7")
    assert entry["action"] == "skip"
    assert entry["reason"] == "foreign"
    assert plan["summary"]["skipped"] == 1


def test_import_skips_the_favourites_the_account_already_has():
    """已经收藏的歌不重复加。"""
    backup = make_backup(liked=["11", "12", "13"])
    api = FakeAPI(liked=["11"])

    plan = plan_import(api, backup, uid="42")

    assert plan["favorites"]["to_add"] == ["12", "13"]
    assert plan["favorites"]["present"] == ["11"]
    assert plan["summary"]["favorites_to_add"] == 2


def test_import_writes_nothing_until_the_user_decides():
    """冲突还没决定就一步都不写。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "My list", "is_own": True, "song_ids": ["21"],
    }])
    api = FakeAPI(playlists=[make_playlist("99", "My list")])
    plan = plan_import(api, backup, uid="42")

    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None)

    assert result["ok"] is False
    assert result["error"] == "missing_decisions"
    assert result["missing"] == ["7"]
    # 一个写操作都没有发生。
    assert api.created == []
    assert api.added == []
    assert not [call for call in api.calls if call[0] in ("like", "create", "add")]


def test_import_likes_only_the_missing_favourites():
    """导入只加新账号没有的歌。"""
    backup = make_backup(liked=["11", "12", "13"])
    api = FakeAPI(liked=["11"])

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None)

    assert result["ok"] is True
    assert result["songs_liked"] == 2
    assert result["songs_skipped"] == 1
    liked = [call[1][0] for call in api.calls if call[0] == "like"]
    assert liked == ["12", "13"]


def test_import_creates_the_new_playlist_and_adds_its_songs():
    """没有冲突时：新建歌单并把歌加进去。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "Fresh", "is_own": True, "song_ids": ["21", "22"],
    }])
    api = FakeAPI()

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None)

    assert result["ok"] is True
    assert result["playlists_created"] == 1
    assert result["tracks_added"] == 2
    assert api.created == ["Fresh"]
    assert api.added == [("new-1", ["21", "22"])]


def test_import_merges_into_the_existing_playlist():
    """同名歌单选了「合并」：只加目标歌单里还没有的歌。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "My list", "is_own": True, "song_ids": ["21", "22"],
    }])
    api = FakeAPI(playlists=[make_playlist("99", "My list")])
    api.tracks["99"] = [{"id": 21}, {"id": 22}]

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan,
                          {"playlists": {"7": {"action": "merge"}}},
                          sleep=lambda _s: None)

    assert result["ok"] is True
    assert result["playlists_merged"] == 1
    assert result["playlists_created"] == 0
    # 两首都已经在目标歌单里了，不用再加。
    assert api.added == []


def test_import_renames_the_new_playlist():
    """同名歌单选了「换个名字」：用新名字建一个。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "My list", "is_own": True, "song_ids": ["21"],
    }])
    api = FakeAPI(playlists=[make_playlist("99", "My list")])

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan,
                          {"playlists": {"7": {"action": "rename",
                                               "name": "My list (2)"}}},
                          sleep=lambda _s: None)

    assert result["ok"] is True
    assert result["playlists_created"] == 1
    assert api.created == ["My list (2)"]
    assert api.added == [("new-1", ["21"])]


def test_import_skips_a_conflict_the_user_skipped():
    """冲突选了「跳过」：什么都不做，但算进跳过数。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "My list", "is_own": True, "song_ids": ["21"],
    }])
    api = FakeAPI(playlists=[make_playlist("99", "My list")])

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan,
                          {"playlists": {"7": {"action": "skip"}}},
                          sleep=lambda _s: None)

    assert result["ok"] is True
    assert result["playlists_skipped"] == 1
    assert result["playlists_created"] == 0
    assert api.created == []
    assert api.added == []


def test_import_batches_the_song_writes():
    """写操作是分批做的：收藏一批一批加，歌单一批一批塞。"""
    songs = [str(index) for index in range(1, 26)]
    backup = make_backup(playlists=[{
        "id": "7", "name": "Big", "is_own": True, "song_ids": songs,
    }], liked=songs)
    api = FakeAPI()

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None,
                          favorite_batch=10, track_batch=8)

    assert result["ok"] is True
    assert result["songs_liked"] == 25
    assert result["tracks_added"] == 25
    # 收藏 25 首 = 10 + 10 + 5 三批。
    likes = [call for call in api.calls if call[0] == "like"]
    assert len(likes) == 25
    # 歌单 25 首按 8 一批 = 4 批。
    assert [len(ids) for _pid, ids in api.added] == [8, 8, 8, 1]


def test_import_pauses_between_the_batches():
    """批次之间要歇一下，别把账号打进限流。"""
    songs = [str(index) for index in range(1, 11)]
    backup = make_backup(liked=songs)
    api = FakeAPI()
    slept: list[float] = []

    plan = plan_import(api, backup, uid="42")
    apply_import(api, backup, plan, {}, sleep=slept.append, favorite_batch=5)

    # 10 首分 2 批，批次之间歇一次。
    assert slept


def test_import_reports_what_it_did():
    """结果里有加了多少歌、建了几个歌单、跳过多少、失败多少。"""
    backup = make_backup(playlists=[{
        "id": "7", "name": "Fresh", "is_own": True, "song_ids": ["21"],
    }, {
        "id": "8", "name": "Foreign", "is_own": False, "song_ids": ["31"],
    }], liked=["11"])
    api = FakeAPI()

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None)

    assert result["songs_liked"] == 1
    assert result["playlists_created"] == 1
    assert result["playlists_skipped"] == 1
    assert result["failed"] == 0
    assert result["skipped_detail"][0]["name"] == "Foreign"


# ------------------------------------------------ 关注的歌手与本机状态

class FakeLocalLibrary:
    """假的本机库：记下合并进来的东西。

    形状跟 :class:`~src.core.library.Library` 一样，只有备份要用到的三个
    合并方法。
    """

    def __init__(self, play_counts=None, favorites=None, recent=None) -> None:
        self.play_counts = dict(play_counts or {})
        self.favorites = [dict(item) for item in (favorites or [])]
        self.recent = [dict(item) for item in (recent or [])]

    def merge_play_counts(self, counts):
        changed = 0
        for key, value in (counts or {}).items():
            if int(value) > int(self.play_counts.get(key, 0)):
                self.play_counts[key] = int(value)
                changed += 1
        return changed

    def merge_favorites(self, entries):
        have = {item["key"] for item in self.favorites}
        added = 0
        for entry in entries or []:
            if not entry.get("key") or entry["key"] in have:
                continue
            have.add(entry["key"])
            self.favorites.append(dict(entry))
            added += 1
        return added

    def merge_recent(self, entries):
        have = {item["key"] for item in self.recent}
        added = 0
        for entry in entries or []:
            if not entry.get("key") or entry["key"] in have:
                continue
            have.add(entry["key"])
            self.recent.append(dict(entry))
            added += 1
        return added


def test_export_records_every_artist_the_account_follows():
    """关注的歌手全部写进备份，一页不够就接着翻。"""
    api = FakeAPI(playlists=[make_playlist("7", "Mine", track_count=1)],
                  liked=["11"],
                  details={"7": {"id": 7, "trackIds": [{"id": 21}]}})
    api.artists = [{"id": 6452, "name": "周杰伦"},
                   {"id": 12172529, "name": "封茗囧菌"}]

    payload = make_export(api)

    assert payload["followed_artists"] == [
        {"id": "6452", "name": "周杰伦"},
        {"id": "12172529", "name": "封茗囧菌"},
    ]
    assert payload["sources"]["artists"] == SOURCE_CLOUD


def test_export_reads_the_artist_list_until_there_is_no_more():
    """超过一页时接着翻，翻完为止。"""
    api = FakeAPI(playlists=[make_playlist("7", "Mine", track_count=1)],
                  liked=["11"],
                  details={"7": {"id": 7, "trackIds": [{"id": 21}]}})
    api.artists = [{"id": index, "name": f"artist{index}"} for index in range(150)]

    payload = make_export(api)

    assert len(payload["followed_artists"]) == 150
    assert payload["followed_artists"][-1]["name"] == "artist149"
    # 150 个要翻两页。
    artist_calls = [call for call in api.calls if call[0] == "artists"]
    assert len(artist_calls) == 2


def test_export_records_the_local_state():
    """播放次数、本地收藏、最近播放都写进备份。"""
    api = FakeAPI(playlists=[make_playlist("7", "Mine", track_count=1)],
                  liked=["11"],
                  details={"7": {"id": 7, "trackIds": [{"id": 21}]}})
    local = {
        "play_counts": {"online:186016": 12},
        "favorites": [{"key": "online:186016", "meta": {"name": "晴天"}, "added": 1.0}],
        "recent": [{"key": "online:186016", "meta": {"name": "晴天"}, "played": 2.0}],
    }

    payload = make_export(api, local=local)

    assert payload["local_state"]["play_counts"] == {"online:186016": 12}
    assert payload["local_state"]["favorites"][0]["key"] == "online:186016"
    assert payload["local_state"]["recent"][0]["key"] == "online:186016"
    assert payload["sources"]["local"] == SOURCE_CACHE


def test_export_survives_a_failing_artist_section():
    """关注的歌手读不出来时，导出照样完成，只是这一节标成缺失。"""
    api = FakeAPI(playlists=[make_playlist("7", "Mine", track_count=1)],
                  liked=["11"],
                  details={"7": {"id": 7, "trackIds": [{"id": 21}]}})
    api.fail_with["artists"] = FakeAPIError(500, "boom")

    payload = make_export(api)

    assert payload["followed_artists"] == []
    assert payload["sources"]["artists"] == SOURCE_MISSING
    assert payload["liked_song_ids"] == ["11"]
    assert any(item["section"] == "artists" for item in payload["errors"])


def test_the_plan_separates_new_artists_from_the_followed_ones():
    """已经关注的不用再关注一次。"""
    backup = make_backup()
    backup["followed_artists"] = [{"id": "6452", "name": "周杰伦"},
                                  {"id": "12172529", "name": "封茗囧菌"}]
    api = FakeAPI()
    api.artists = [{"id": 6452, "name": "周杰伦"}]

    plan = plan_import(api, backup, uid="42")

    assert [item["id"] for item in plan["artists"]["to_follow"]] == ["12172529"]
    assert [item["id"] for item in plan["artists"]["already_followed"]] == ["6452"]
    assert plan["summary"]["artists_to_follow"] == 1
    assert plan["summary"]["artists_present"] == 1


def test_the_import_follows_the_artists():
    """导入时把备份里关注过的歌手关注上。"""
    backup = make_backup()
    backup["followed_artists"] = [{"id": "6452", "name": "周杰伦"},
                                  {"id": "12172529", "name": "封茗囧菌"}]
    api = FakeAPI()

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None)

    assert result["artists_followed"] == 2
    assert api.followed == [("6452", True), ("12172529", True)]
    assert result["failed"] == 0


def test_the_import_restores_the_local_state():
    """播放次数、本地收藏、最近播放写回本机。"""
    backup = make_backup()
    backup["local_state"] = {
        "play_counts": {"online:186016": 12},
        "favorites": [{"key": "online:186016", "meta": {"name": "晴天"}}],
        "recent": [{"key": "online:186016", "meta": {"name": "晴天"}}],
    }
    api = FakeAPI()
    local = FakeLocalLibrary()

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None, local=local)

    assert result["local_restored"] == {"play_counts": 1, "favorites": 1, "recent": 1}
    assert local.play_counts == {"online:186016": 12}
    assert local.favorites[0]["key"] == "online:186016"
    assert local.recent[0]["key"] == "online:186016"


def test_the_import_never_reports_the_play_counts_to_the_server():
    """播放次数只改本地记录，不向网易云报告。"""
    backup = make_backup()
    backup["local_state"] = {"play_counts": {"online:186016": 12},
                             "favorites": [], "recent": []}
    api = FakeAPI()
    local = FakeLocalLibrary()

    plan = plan_import(api, backup, uid="42")
    apply_import(api, backup, plan, {}, sleep=lambda _s: None, local=local)

    # 假 API 上根本没有能上报播放次数的方法，所以只要没有多出别的调用就
    # 说明这一节没有走网络。
    assert not any("count" in name for name, _ in api.calls)


def test_the_local_play_count_keeps_the_bigger_number():
    """本机已经更大的次数不会被备份里的旧数字改小。"""
    backup = make_backup()
    backup["local_state"] = {"play_counts": {"online:186016": 3},
                             "favorites": [], "recent": []}
    api = FakeAPI()
    local = FakeLocalLibrary(play_counts={"online:186016": 9})

    plan = plan_import(api, backup, uid="42")
    apply_import(api, backup, plan, {}, sleep=lambda _s: None, local=local)

    assert local.play_counts["online:186016"] == 9


def test_an_old_backup_without_the_new_sections_still_imports():
    """1 版的备份文件没有关注歌手和本机状态，导入照样跑得完。"""
    backup = make_backup(liked=["11"])
    backup["version"] = 1
    assert "followed_artists" not in backup
    assert "local_state" not in backup
    api = FakeAPI()

    plan = plan_import(api, backup, uid="42")
    result = apply_import(api, backup, plan, {}, sleep=lambda _s: None,
                          local=FakeLocalLibrary())

    assert result["ok"] is True
    assert result["artists_followed"] == 0
    assert result["local_restored"] == {"play_counts": 0, "favorites": 0, "recent": 0}
    assert plan["artists"]["to_follow"] == []


def test_a_backup_round_trip_carries_everything():
    """导出再导入，关注的歌手和本机记录都还在。"""
    api = FakeAPI(playlists=[make_playlist("7", "Mine", track_count=1)],
                  liked=["11"],
                  details={"7": {"id": 7, "trackIds": [{"id": 21}]}})
    api.artists = [{"id": 6452, "name": "周杰伦"}]
    local = {
        "play_counts": {"online:186016": 12},
        "favorites": [{"key": "online:186016"}],
        "recent": [{"key": "online:186016"}],
    }

    payload = make_export(api, local=local)

    fresh = FakeAPI()
    target = FakeLocalLibrary()
    plan = plan_import(fresh, payload, uid="99")
    result = apply_import(fresh, payload, plan, {}, sleep=lambda _s: None,
                          local=target)

    assert result["artists_followed"] == 1
    assert fresh.followed == [("6452", True)]
    assert target.play_counts == {"online:186016": 12}
    assert target.favorites[0]["key"] == "online:186016"
