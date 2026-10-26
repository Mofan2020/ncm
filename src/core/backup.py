"""账号数据的备份与恢复。

用户担心账号被封，想把账号数据导成一个文件，换账号之后能导回来。这个模块
只管两件事：

* **导出**：先从云端把账号资料、云端收藏、歌单列表和每个歌单的歌曲 id 全部
  拉下来；某一类读不出来时，不让整次导出失败，而是退到本地缓存
  （:mod:`src.core.metacache` 的磁盘缓存）尽力导出。导出的文件里每一部分都
  标了自己的来源（云端 / 本地缓存 / 缺失），用户据此判断这份备份有多完整。
* **导入**：把备份里的数据尽力合并到当前登录的账号。收藏列表里新账号没有的
  歌补进去，已经有的跳过；备份里自己创建的歌单在新账号没有同名歌单就新建
  一个，同名就是冲突，交给用户逐条决定（跳过 / 合并 / 换名字）。别人创建
  的歌单没法在当前账号下重建，跳过并说明。

文件格式是 JSON，只是后缀用 ``.ncmbk``：

.. code-block:: json

    {
      "format": "ncm-account-backup",
      "version": 3,
      "sources": {"account": "cloud", "favorites": "cloud", ...},
      "account": {...},
      "liked_song_ids": ["1", "2"],
      "playlists": [{"id": "7", "name": "...", "song_ids": ["1"], ...}],
      "followed_artists": [{"id": "6452", "name": "..."}],
      "liked_albums": [{"id": "35377042", "name": "..."}],
      "subscribed_djradios": [{"id": "1227859651", "name": "..."}],
      "liked_mvs": [{"id": "14557498", "name": "..."}],
      "play_record": [{"song_id": "30352891", "name": "...", "play_count": 25}],
      "local_state": {"play_counts": {...}, "favorites": [...], "recent": [...]}
    }

模块不碰 GUI，也不直接发网络请求：数据从 :class:`NeteaseAPI` 的公开方法读，
写操作也只用它的公开方法，所以测试可以拿假 API 对象跑（``tests/test_account_backup.py``）。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_VERSION",
    "BACKUP_SUFFIX",
    "SOURCE_CLOUD",
    "SOURCE_CACHE",
    "SOURCE_MISSING",
    "BackupError",
    "BackupFormatError",
    "apply_import",
    "default_backup_name",
    "export_account_data",
    "plan_import",
    "read_backup_file",
    "write_backup_file",
]

#: 备份文件的格式标识。改格式就要同时改 :data:`BACKUP_VERSION`。
BACKUP_FORMAT = "ncm-account-backup"

#: 备份格式的版本号。读文件时版本比这个新就拒绝（老版本读不了新格式）。
#:
#: 2 增加了 ``followed_artists``（关注的歌手）和 ``local_state``（播放次数、
#: 本地收藏、最近播放）。1 版的文件照样能读：这些新字段读不到就当没有。
#:
#: 3 补上了剩下的收藏面：``liked_albums``（收藏的专辑）、
#: ``subscribed_djradios``（订阅的电台）、``liked_mvs``（收藏的 MV）和
#: ``play_record``（云端的听歌排行，带播放次数）。2 版的文件照样能读。
BACKUP_VERSION = 3

#: 备份文件的后缀。
BACKUP_SUFFIX = ".ncmbk"

#: 某一节数据的来源：云端。
SOURCE_CLOUD = "cloud"

#: 某一节数据的来源：本地磁盘缓存。
SOURCE_CACHE = "cache"

#: 某一节数据的来源：哪儿都没有。
SOURCE_MISSING = "missing"

#: 一个云端读取最多试几次。限流（405）几秒后就过去了，所以要重试；
#: 封号 / 会话失效（401、301）重试也没用，见 :func:`_is_fatal`.
DEFAULT_ATTEMPTS = 3

#: 重试之间隔几秒。逐次加长，免得把还在冷却的接口再打一遍。
DEFAULT_RETRY_DELAY = 2.0

#: 导出时一次读几个歌单，读完歇一下——接口有自己的节流，别把它打爆。
PLAYLIST_BATCH = 5

#: 一批歌单读完之后的间隔（秒）。
PLAYLIST_PAUSE = 0.5

#: 导入时每批处理多少首收藏（一首一次请求）。
FAVOURITE_BATCH = 20

#: 导入时一次往歌单里塞多少首歌。
TRACK_BATCH = 100

#: 批次之间的间隔（秒）。写接口比读接口更容易被限流，留一点空隙；
#: 真正的大退避交给 API 层，这里只是别把请求连成一片。
BATCH_PAUSE = 1.0

#: 进度阶段名。前端按这些名字取 i18n 文案。
PHASE_ACCOUNT = "account"
PHASE_FAVORITES = "favorites"
PHASE_PLAYLISTS = "playlists"
PHASE_SONGS = "songs"
PHASE_ARTISTS = "artists"
PHASE_ALBUMS = "albums"
PHASE_DJRADIOS = "djradios"
PHASE_MVS = "mvs"
PHASE_RECORD = "record"
PHASE_LOCAL = "local"

#: 导出时一页读多少个关注的歌手。
ARTIST_PAGE = 100

#: 最多读多少页。5000 个关注已经比任何人真的关注的多得多。
ARTIST_MAX_PAGES = 50

#: 导出时一页读多少个收藏（专辑 / 电台 / MV）。
SAVED_PAGE = 100

#: 这些收藏最多翻多少页。
SAVED_MAX_PAGES = 20


class BackupError(Exception):
    """备份 / 恢复过程中的错误。"""


class BackupFormatError(BackupError):
    """备份文件不是本程序写出来的，或者版本太新。"""


# --------------------------------------------------------------------- 小工具

def _safe_filename(text: str) -> str:
    """把任意文本变成能当文件名的片段。"""
    cleaned = "".join(ch for ch in str(text or "") if ch not in '\\/:*?"<>|\r\n\t')
    cleaned = cleaned.strip().strip(".")
    return cleaned[:40]


def default_backup_name(nickname: str, when: datetime | None = None) -> str:
    """默认的备份文件名：``<用户名>_YYYYmmdd-HHMMSS.ncmbk``。"""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{_safe_filename(nickname) or 'account'}_{stamp}{BACKUP_SUFFIX}"


def _code_of(exc: BaseException) -> int | None:
    """从异常上取出接口返回的状态码（取不到就是 None）。"""
    code = getattr(exc, "code", None)
    try:
        return int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _is_fatal(exc: BaseException) -> bool:
    """这个错误重试也没用吗？

    限流（405）过几秒就好了，值得重试；会话失效（301、401）和风控
    （-462）重试多少次都是同一个结果，直接放弃这一节。
    """
    return _code_of(exc) in (301, 401, -462)


def _is_rate_limited(exc: BaseException) -> bool:
    """这个错误是限流吗（405 / 429 / -460）？"""
    if _code_of(exc) in (405, 429, -460):
        return True
    text = str(exc).lower()
    return "too frequent" in text or "过于频繁" in text or "rate limit" in text


def _retry(label: str, call: Callable[[], Any], *, attempts: int, delay: float,
           sleep: Callable[[float], None]) -> tuple[Any, str]:
    """调用一个云端读取，失败就隔几秒重试。

    返回 ``(值, 错误说明)``：成功时错误说明是空串，失败时值是 ``None``。
    会话失效这类重试无望的错误立刻放弃，不浪费时间。
    """
    attempts = max(1, int(attempts))
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            value = call()
        except Exception as exc:  # 网络错误 / 限流 / 服务端报错
            if _is_fatal(exc):
                return None, f"{label}: {exc}"
            last = f"{label}: {exc}"
            value = None
        if value is not None:
            return value, ""
        last = last or f"{label}: no answer"
        if attempt < attempts:
            sleep(delay * attempt)
    return None, last


def _song_ids(songs: Any) -> list[str]:
    """从一份歌曲列表里取出 id。"""
    ids: list[str] = []
    for song in songs or []:
        if not isinstance(song, dict):
            continue
        song_id = str(song.get("id") or "").strip()
        if song_id:
            ids.append(song_id)
    return ids


def _track_ids(info: Any) -> list[str]:
    """从歌单详情里取出全部歌曲 id。

    歌单详情自带 ``trackIds``，一次请求就能拿到整份 id 列表——比逐首拉
    详情便宜得多，导出几百首歌的歌单时差别很大。
    """
    if not isinstance(info, dict):
        return []
    ids: list[str] = []
    for item in (info.get("trackIds") or []):
        if isinstance(item, dict):
            song_id = str(item.get("id") or "").strip()
        else:
            song_id = str(item or "").strip()
        if song_id:
            ids.append(song_id)
    return ids


def _playlist_entry(raw: dict[str, Any], uid: str) -> dict[str, Any]:
    """把一个歌单整理成备份文件里的形状。

    ``is_own`` 说的是「这个歌单是不是备份里那个账号自己创建的」：导出时按
    创建者判断；导入时备份文件里已经存好了，直接用备份里的值——拿当前账号
    去比会把所有歌单都算成别人的。
    """
    # 接口给的 creator 是对象，备份文件里存的是昵称字符串。
    raw_creator = raw.get("creator")
    creator = raw_creator if isinstance(raw_creator, dict) else {}
    creator_name = str(creator.get("nickname") or "") if creator else str(raw_creator or "")
    creator_id = str(creator.get("userId") or raw.get("creator_id") or "")
    own = raw.get("is_own")
    if not isinstance(own, bool):
        own = bool(uid) and creator_id == str(uid)
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "cover_url": str(raw.get("cover_url") or raw.get("coverImgUrl")
                         or raw.get("picUrl") or ""),
        "creator": creator_name,
        "creator_id": creator_id,
        "is_own": own,
        "subscribed_count": int(raw.get("subscribed_count")
                                or raw.get("subscribedCount") or 0),
        "track_count": int(raw.get("track_count") or raw.get("trackCount") or 0),
        # specialType 5 是「我喜欢的音乐」，它没法被当成普通歌单重建。
        "special_type": int(raw.get("special_type") or raw.get("specialType") or 0),
        "description": str(raw.get("description") or ""),
    }


# --------------------------------------------------------------------- 导出

def export_account_data(
    api: Any,
    *,
    uid: str,
    account: dict[str, Any] | None = None,
    cloud_profile: Callable[[], dict[str, Any] | None] | None = None,
    cache: Any = None,
    local: dict[str, Any] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    playlist_batch: int = PLAYLIST_BATCH,
    playlist_pause: float = PLAYLIST_PAUSE,
) -> dict[str, Any]:
    """把账号数据抓下来，整理成备份文件的内容。

    ``api`` 是 :class:`~src.core.api.NeteaseAPI`（或任何长得一样的假对象），
    ``cache`` 是 :meth:`~src.core.metacache.get_meta_cache` 给的东西——云端读
    不到的时候从它那里兜底。``local`` 是本机的 :meth:`Library.exportable_state`
    给的播放次数 / 收藏 / 最近播放。

    返回的就是要写进文件的那个字典：``format``、``version``、``sources``
    （每一节的来源）、``account``、``liked_song_ids``、``playlists``、
    ``followed_artists``、``local_state`` 和 ``errors``（读失败的说明）。
    """
    uid = str(uid or "").strip()
    account = dict(account or {})
    errors: list[dict[str, str]] = []
    sources: dict[str, str] = {}

    def report(phase: str, current: int, total: int) -> None:
        if progress is not None:
            progress(phase, current, total)

    # -- 账号资料 ---------------------------------------------------------
    report(PHASE_ACCOUNT, 0, 1)
    profile: dict[str, Any] = {}
    reader = cloud_profile or (lambda: _read_cloud_profile(api, uid))
    try:
        fetched = reader()
    except Exception as exc:
        fetched = None
        errors.append({"section": PHASE_ACCOUNT, "message": str(exc)})
    if isinstance(fetched, dict) and fetched:
        profile = fetched
        sources[PHASE_ACCOUNT] = SOURCE_CLOUD
    if not profile:
        # 云端读不到就退到本地会话里的账号信息（登录时读到的）。
        profile = dict(account.get("profile") or {})
        sources[PHASE_ACCOUNT] = SOURCE_CACHE if profile else SOURCE_MISSING
    account_section = {
        "user_id": str(account.get("user_id") or uid),
        "nickname": str(account.get("nickname") or profile.get("nickname") or ""),
        "avatar_url": str(account.get("avatar_url") or profile.get("avatarUrl") or ""),
        "vip_type": account.get("vip_type") if account.get("vip_type") is not None
        else profile.get("vipType", 0),
        "level": profile.get("level"),
        "listen_songs": profile.get("listenSongs"),
        "listen_days": profile.get("listenDays"),
        "register_time": (profile.get("regTime") or profile.get("registerTime")
                          or profile.get("createTime")),
        "profile": profile,
    }
    report(PHASE_ACCOUNT, 1, 1)

    # -- 歌单列表（同时也是「云端还通不通」的探针）------------------------
    # 每个账号都有「我喜欢的音乐」，所以读到空列表 = 这一节读失败了，
    # 不是真的没有歌单。
    report(PHASE_PLAYLISTS, 0, 1)
    raw_playlists, playlists_error = _retry(
        "playlists",
        lambda: api.get_user_playlists_all(uid, fresh=True) or None,
        attempts=attempts, delay=retry_delay, sleep=sleep,
    )
    cloud_alive = bool(raw_playlists)
    if cloud_alive:
        sources[PHASE_PLAYLISTS] = SOURCE_CLOUD
        entries = [_playlist_entry(raw, uid) for raw in raw_playlists
                   if isinstance(raw, dict)]
    else:
        cached = _cached_playlists(cache, uid)
        entries = [_playlist_entry(raw, uid) for raw in cached]
        sources[PHASE_PLAYLISTS] = SOURCE_CACHE if entries else SOURCE_MISSING
        errors.append({"section": PHASE_PLAYLISTS, "message": playlists_error})
    report(PHASE_PLAYLISTS, 1, 1)

    # -- 云端收藏 ---------------------------------------------------------
    report(PHASE_FAVORITES, 0, 1)
    expected = next((item["track_count"] for item in entries
                     if item["special_type"] == 5), 0)
    liked_ids, liked_error = _read_liked(api, uid, expected, cloud_alive=cloud_alive,
                                          attempts=attempts, delay=retry_delay,
                                          sleep=sleep)
    if liked_error:
        cached = _cached_liked(cache, entries)
        if cached:
            liked_ids = cached
            sources[PHASE_FAVORITES] = SOURCE_CACHE
        else:
            liked_ids = []
            sources[PHASE_FAVORITES] = SOURCE_MISSING
        errors.append({"section": PHASE_FAVORITES, "message": liked_error})
    else:
        sources[PHASE_FAVORITES] = SOURCE_CLOUD
    report(PHASE_FAVORITES, 1, 1)

    # -- 每个歌单的歌曲 id -------------------------------------------------
    total = len(entries)
    batch = max(1, int(playlist_batch))
    for index, entry in enumerate(entries):
        report(PHASE_SONGS, index, total)
        song_ids: list[str] | None = None
        songs_error = ""
        if cloud_alive:
            song_ids, songs_error = _playlist_song_ids(api, entry, attempts=attempts,
                                                        delay=retry_delay, sleep=sleep)
        else:
            songs_error = "cloud unreachable"
        if song_ids is None:
            cached = _cached_tracks(cache, entry["id"])
            if cached:
                entry["song_ids"] = cached
                entry["song_ids_source"] = SOURCE_CACHE
            else:
                entry["song_ids"] = []
                entry["song_ids_source"] = SOURCE_MISSING
            errors.append({"section": f"playlist:{entry['id']}",
                           "message": songs_error})
        else:
            entry["song_ids"] = song_ids
            entry["song_ids_source"] = SOURCE_CLOUD
        if (index + 1) % batch == 0 and index + 1 < total:
            # 分批读，中间歇一下，别把服务器打爆。
            sleep(playlist_pause)
    report(PHASE_SONGS, total, total)

    # -- 关注的歌手 ---------------------------------------------------------
    report(PHASE_ARTISTS, 0, 1)
    artists, artists_error = _read_followed_artists(api, attempts=attempts,
                                                     delay=retry_delay, sleep=sleep)
    if artists_error:
        sources[PHASE_ARTISTS] = SOURCE_MISSING
        errors.append({"section": PHASE_ARTISTS, "message": artists_error})
    else:
        sources[PHASE_ARTISTS] = SOURCE_CLOUD
    report(PHASE_ARTISTS, 1, 1)

    # -- 收藏的专辑 / 订阅的电台 / 收藏的 MV --------------------------------
    # 这三节接口形状一样：一个列表接口回答 data / djRadios，翻页到没有为止。
    report(PHASE_ALBUMS, 0, 1)
    albums, albums_error = _read_saved_collection(
        lambda offset: api.get_liked_albums(uid, limit=SAVED_PAGE, offset=offset),
        "albums", ("id",), ("name",))
    if albums_error:
        sources[PHASE_ALBUMS] = SOURCE_MISSING
        errors.append({"section": PHASE_ALBUMS, "message": albums_error})
    else:
        sources[PHASE_ALBUMS] = SOURCE_CLOUD
    report(PHASE_ALBUMS, 1, 1)

    report(PHASE_DJRADIOS, 0, 1)
    djradios, djradios_error = _read_saved_collection(
        lambda offset: api.get_subscribed_djradios(uid, limit=SAVED_PAGE,
                                                   offset=offset),
        "djradios", ("id",), ("name",))
    if djradios_error:
        sources[PHASE_DJRADIOS] = SOURCE_MISSING
        errors.append({"section": PHASE_DJRADIOS, "message": djradios_error})
    else:
        sources[PHASE_DJRADIOS] = SOURCE_CLOUD
    report(PHASE_DJRADIOS, 1, 1)

    report(PHASE_MVS, 0, 1)
    mvs, mvs_error = _read_saved_collection(
        lambda offset: api.get_subscribed_mvs(uid, limit=SAVED_PAGE, offset=offset),
        "mvs", ("vid", "id"), ("title", "name"))
    if mvs_error:
        sources[PHASE_MVS] = SOURCE_MISSING
        errors.append({"section": PHASE_MVS, "message": mvs_error})
    else:
        sources[PHASE_MVS] = SOURCE_CLOUD
    report(PHASE_MVS, 1, 1)

    # -- 云端的听歌排行（带播放次数）----------------------------------------
    # 这是服务器那边记的播放次数，比本机的全。导入时只写进本机的计数，
    # 不会向网易云报告——本地记录想怎么改就怎么改。
    report(PHASE_RECORD, 0, 1)
    record: list[dict[str, Any]] = []
    record_error = ""
    try:
        raw_record = api.get_play_record(uid, scope="all")
        record = [dict(item) for item in (raw_record or [])
                  if isinstance(item, dict)]
    except Exception as exc:
        record_error = str(exc)
    if record_error:
        sources[PHASE_RECORD] = SOURCE_MISSING
        errors.append({"section": PHASE_RECORD, "message": record_error})
    else:
        sources[PHASE_RECORD] = SOURCE_CLOUD
    report(PHASE_RECORD, 1, 1)

    # -- 本机状态（播放次数 / 收藏 / 最近播放）--------------------------------
    # 云端没有对应的东西，所以它们的来源永远是本地；导入时也只写回本地，
    # 不会向网易云报告。
    report(PHASE_LOCAL, 0, 1)
    local_state = _local_state(local)
    sources[PHASE_LOCAL] = SOURCE_CACHE if local_state else SOURCE_MISSING
    report(PHASE_LOCAL, 1, 1)

    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": _app_version(),
        "sources": sources,
        "account": account_section,
        "liked_song_ids": list(liked_ids),
        "playlists": entries,
        "followed_artists": artists,
        "liked_albums": albums,
        "subscribed_djradios": djradios,
        "liked_mvs": mvs,
        "play_record": record,
        "local_state": local_state,
        "errors": errors,
    }


def _local_state(local: Any) -> dict[str, Any]:
    """整理本机那部分状态：播放次数、收藏、最近播放。

    这三样只存在于 ``library.json``，网易云没有对应的接口——导入时写回本地，
    不向服务器报告任何东西。
    """
    if not isinstance(local, dict):
        return {}
    counts: dict[str, int] = {}
    raw_counts = local.get("play_counts")
    if isinstance(raw_counts, dict):
        for raw_key, raw_value in raw_counts.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            try:
                counts[key] = max(0, int(raw_value))
            except (TypeError, ValueError):
                continue
    favorites = [dict(item) for item in (local.get("favorites") or [])
                  if isinstance(item, dict) and item.get("key")]
    recent = [dict(item) for item in (local.get("recent") or [])
              if isinstance(item, dict) and item.get("key")]
    if not counts and not favorites and not recent:
        return {}
    return {
        "play_counts": counts,
        "favorites": favorites,
        "recent": recent,
    }


def _read_followed_artists(api: Any, *, attempts: int, delay: float,
                            sleep: Callable[[float], None]) -> tuple[list[dict[str, Any]], str]:
    """读出账号关注的全部歌手。

    接口是分页的，所以一直问到它说没有下一页为止。读失败就把这一节留空并
    记下原因，不让整次导出失败。
    """
    artists: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    last = ""
    for _ in range(ARTIST_MAX_PAGES):
        page, error = _retry(
            f"followed artists @{offset}",
            # The offset is bound here: without it the lambda would read the
            # loop variable when it runs, not when it is created.
            lambda offset=offset: api.get_followed_artists(
                limit=ARTIST_PAGE, offset=offset) or None,
            attempts=attempts, delay=delay, sleep=sleep,
        )
        if page is None:
            last = error
            break
        code = page.get("code")
        if code in (301, 401):
            last = "not signed in"
            break
        for artist in (page.get("artists") or []):
            if not isinstance(artist, dict):
                continue
            # ``or ""`` would drop a real id of 0, so the None check comes
            # first.
            raw_id = artist.get("id")
            artist_id = str(raw_id).strip() if raw_id is not None else ""
            if not artist_id or artist_id in seen:
                continue
            seen.add(artist_id)
            artists.append({
                "id": artist_id,
                "name": str(artist.get("name") or "Unknown"),
            })
        if not page.get("has_more"):
            return artists, ""
        offset += ARTIST_PAGE
    else:
        last = "too many pages"
    if not artists and last:
        return [], last
    return artists, ""


def _saved_entry(item: dict[str, Any], id_keys: Sequence[str],
                 name_keys: Sequence[str]) -> dict[str, str] | None:
    """一节收藏里的某一项：id 和名字。

    专辑和电台用 ``id`` / ``name``，MV 用 ``vid`` / ``title``，所以字段名由
    调用方给。
    """
    entry_id = ""
    for key in id_keys:
        raw = item.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            entry_id = text
            break
    if not entry_id:
        return None
    name = ""
    for key in name_keys:
        raw = item.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            name = text
            break
    return {"id": entry_id, "name": name or entry_id}


def _read_saved_collection(fetch: Callable[[int], dict[str, Any]], items_key: str,
                           id_keys: Sequence[str],
                           name_keys: Sequence[str]) -> tuple[list[dict[str, str]], str]:
    """读一节「收藏了的东西」：专辑、电台、MV 都是这个形状。

    ``fetch`` 负责取一页，``items_key`` 是这一页里列表的字段名。读失败就把
    已经读到的部分留着，并记下原因，不让整次导出失败。
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    offset = 0
    for _ in range(SAVED_MAX_PAGES):
        try:
            page = fetch(offset)
        except Exception as exc:
            return entries, str(exc)
        if not isinstance(page, dict):
            return entries, "unexpected answer"
        code = page.get("code")
        if code is not None and str(code) != "200":
            return entries, f"code {code}"
        raw_items = page.get(items_key)
        if not isinstance(raw_items, list):
            raw_items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            entry = _saved_entry(item, id_keys, name_keys)
            if entry and entry["id"] not in seen:
                seen.add(entry["id"])
                entries.append(entry)
        if not raw_items or not page.get("has_more"):
            break
        offset += len(raw_items)
    return entries, ""


def _backup_saved(payload: dict[str, Any], key: str) -> list[dict[str, str]]:
    """备份文件里记着的一节收藏。"""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in (payload.get(key) or []):
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("id") or "").strip()
        if not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        entries.append({"id": entry_id,
                        "name": str(item.get("name") or entry_id)})
    return entries


def _saved_plan(api: Any, payload: dict[str, Any], key: str,
                fetch: Callable[[int], dict[str, Any]], items_key: str,
                id_keys: Sequence[str],
                name_keys: Sequence[str]) -> dict[str, Any]:
    """算一节收藏要补哪些、已经有哪些。

    读不到当前状态就当一节都没有：多收藏一次已经收藏的东西没有坏处，
    少收藏一个才是真的丢了。
    """
    backup = _backup_saved(payload, key)
    current = {item["id"] for item in
               _read_saved_collection(fetch, items_key, id_keys, name_keys)[0]}
    return {
        "total": len(backup),
        "to_add": [item for item in backup if item["id"] not in current],
        "present": [item for item in backup if item["id"] in current],
    }


def _payload_record(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """备份文件里记着的云端听歌排行。"""
    rows: list[dict[str, Any]] = []
    for item in (payload.get("play_record") or []):
        if not isinstance(item, dict):
            continue
        song_id = str(item.get("song_id") or "").strip()
        if not song_id:
            continue
        try:
            count = max(0, int(item.get("play_count") or 0))
        except (TypeError, ValueError):
            continue
        rows.append({"song_id": song_id,
                     "name": str(item.get("name") or ""),
                     "play_count": count})
    return rows


def _apply_saved(items: Any, action: Callable[[str], str], *, section: str,
                 sleep: Callable[[float], None], batch_pause: float,
                 errors: list[dict[str, str]]) -> tuple[int, int]:
    """把一节收藏补上：返回（成功的个数，失败的个数）。

    限流不算失败，隔几秒再试一次；会话没了就停下，后面的也补不上。
    """
    entries = [item for item in (items or []) if isinstance(item, dict)]
    done = 0
    failed = 0
    for index, item in enumerate(entries):
        entry_id = str(item.get("id") or "").strip()
        if not entry_id:
            continue
        outcome = action(entry_id)
        if outcome == "ok":
            done += 1
        elif outcome == "rate_limited":
            sleep(BATCH_PAUSE)
            outcome = action(entry_id)
            if outcome == "ok":
                done += 1
            else:
                failed += 1
                errors.append({"section": section,
                               "message": f"{entry_id}: {outcome}"})
        else:
            failed += 1
            errors.append({"section": section,
                           "message": f"{entry_id}: {outcome}"})
            if outcome == "not_logged_in":
                break
        if index + 1 < len(entries):
            sleep(batch_pause)
    return done, failed


def _read_cloud_profile(api: Any, uid: str) -> dict[str, Any] | None:
    """从云端读账号资料（注册时间之类的字段在本地会话里没有）。"""
    detail = api.get_user_detail(uid)
    if not isinstance(detail, dict):
        return None
    profile = detail.get("profile")
    if isinstance(profile, dict):
        return dict(profile)
    # 接口直接返回资料本身的时候（没有 profile 这一层）也认。
    return {key: value for key, value in detail.items() if key != "code"} or None


def _playlist_song_ids(api: Any, entry: dict[str, Any], *, attempts: int,
                        delay: float, sleep: Callable[[float], None]) -> tuple[list[str] | None, str]:
    """读一个歌单的歌曲 id。

    先用歌单详情里的 ``trackIds``（一次请求拿全）；详情被截断了再用
    :meth:`get_playlist_songs` 补齐。歌单说是有歌却一首都没读到，那就是
    这一节读失败了——返回 ``None``，让调用方退到本地缓存。
    """
    playlist_id = str(entry.get("id") or "")
    info, info_error = _retry(
        f"playlist {playlist_id}",
        lambda: api.get_playlist_info(playlist_id, max_tracks=1000, fresh=True),
        attempts=attempts, delay=delay, sleep=sleep,
    )
    ids = _track_ids(info)
    count = int(entry.get("track_count") or 0)
    if info is None:
        # 详情读不到，退一步用歌曲列表接口试试。
        songs, songs_error = _retry(
            f"playlist songs {playlist_id}",
            lambda: api.get_playlist_songs(playlist_id, fresh=True),
            attempts=attempts, delay=delay, sleep=sleep,
        )
        if songs is None:
            return None, info_error or songs_error
        ids = _song_ids(songs)
        count = count or len(ids)
    elif count and len(ids) < count:
        # 详情里的 id 不全（长歌单会被截断），补齐。
        songs, _ = _retry(
            f"playlist songs {playlist_id}",
            lambda: api.get_playlist_songs(playlist_id, fresh=True),
            attempts=attempts, delay=delay, sleep=sleep,
        )
        complete = _song_ids(songs)
        if len(complete) > len(ids):
            ids = complete
            count = len(ids)
    if count and not ids:
        return None, f"playlist {playlist_id}: no songs"
    return ids, ""


def _read_liked_ids(api: Any, uid: str, expected: int, *, attempts: int,
                    delay: float, sleep: Callable[[float], None]) -> tuple[list[str], str]:
    """读云端收藏的歌曲 id。

    这个接口读不出来时返回空列表，和「真的没收藏」长得一样。所以拿
    「我喜欢的音乐」的歌单曲目数对一下：说是有歌却读到 0 首，那就是读失败，
    重试几次再退到本地缓存。
    """
    last = ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            ids = list(api.get_liked_song_ids(uid, use_cache=False) or [])
        except Exception as exc:
            if _is_fatal(exc):
                return [], f"liked ids: {exc}"
            last = f"liked ids: {exc}"
            ids = None  # type: ignore[assignment]
        if ids is not None and (ids or not expected):
            return ids, ""
        last = last or "liked ids: empty"
        if attempt < attempts:
            sleep(delay * attempt)
    return [], last


def _read_liked(api: Any, uid: str, expected: int, *, cloud_alive: bool,
                attempts: int, delay: float,
                sleep: Callable[[float], None]) -> tuple[list[str], str]:
    """读云端收藏；云端整体不通就直接说失败，让调用方退到本地缓存。"""
    if not cloud_alive:
        return [], "cloud unreachable"
    return _read_liked_ids(api, uid, expected, attempts=attempts, delay=delay,
                           sleep=sleep)


def _cached_playlists(cache: Any, uid: str) -> list[dict[str, Any]]:
    """本地缓存里的账号歌单列表。"""
    if cache is None or not uid:
        return []
    try:
        page = cache.get_playlist(f"userplaylists:{uid}:100:0")
    except Exception:
        return []
    if not isinstance(page, dict):
        return []
    playlists = page.get("playlists")
    return [item for item in (playlists or []) if isinstance(item, dict)]


def _cached_tracks(cache: Any, playlist_id: str) -> list[str]:
    """本地缓存里某个歌单的歌曲 id。"""
    if cache is None or not playlist_id:
        return []
    try:
        tracks = cache.get_playlist(f"tracks:{playlist_id}")
    except Exception:
        return []
    return _song_ids(tracks)


def _cached_liked(cache: Any, entries: Sequence[dict[str, Any]]) -> list[str]:
    """本地缓存里的云端收藏（「我喜欢的音乐」的歌单缓存）。"""
    for entry in entries:
        if entry.get("special_type") == 5:
            return _cached_tracks(cache, str(entry.get("id") or ""))
    return []


def _app_version() -> str:
    try:
        from src.version import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - 版本号读不到不该让备份失败
        return ""


# --------------------------------------------------------------------- 文件

def write_backup_file(path: str | os.PathLike[str], payload: dict[str, Any]) -> str:
    """把备份内容写进文件，返回路径。

    先写临时文件再改名：写到一半被打断也不会留下一个解析不了的备份。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return str(target)


def read_backup_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读一个备份文件，格式不对就抛 :class:`BackupFormatError`。"""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise BackupError(str(exc)) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise BackupFormatError(f"not a json file: {exc}") from exc
    if not isinstance(payload, dict):
        raise BackupFormatError("not a backup file")
    if payload.get("format") != BACKUP_FORMAT:
        raise BackupFormatError("unknown backup format")
    try:
        version = int(payload.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise BackupFormatError("backup version missing") from exc
    if version > BACKUP_VERSION:
        raise BackupFormatError(f"backup version {version} is newer than {BACKUP_VERSION}")
    return payload


# --------------------------------------------------------------------- 导入

def plan_import(api: Any, payload: dict[str, Any], *, uid: str,
                sleep: Callable[[float], None] = time.sleep,
                attempts: int = DEFAULT_ATTEMPTS,
                retry_delay: float = DEFAULT_RETRY_DELAY) -> dict[str, Any]:
    """算一遍导入要做的事，**不写任何东西**。

    返回的 ``conflicts`` 是需要用户拍板的同名歌单；``playlists`` 里每一项
    带上 ``action``，说明这一条打算怎么处理。写入要等
    :func:`apply_import` 拿到用户的决定之后才发生。
    """
    uid = str(uid or "").strip()
    raw_playlists, playlists_error = _retry(
        "playlists",
        lambda: api.get_user_playlists_all(uid, fresh=True) or None,
        attempts=attempts, delay=retry_delay, sleep=sleep,
    )
    current = [_playlist_entry(raw, uid) for raw in (raw_playlists or [])
               if isinstance(raw, dict)]
    current_by_name = {item["name"]: item for item in current if item["name"]}

    liked_now, liked_error = _read_liked_ids(api, uid, 0, attempts=attempts,
                                              delay=retry_delay, sleep=sleep)
    liked_set = set(liked_now)
    if liked_error:
        # 读不到当前收藏，就当全都要加——重复喜欢一首网易云是允许的，
        # 但漏加一首就是真的丢了。
        liked_set = set()

    backup_playlists = [item for item in (payload.get("playlists") or [])
                        if isinstance(item, dict)]
    backup_liked = [str(item) for item in (payload.get("liked_song_ids") or [])]

    plan_playlists: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for raw in backup_playlists:
        entry = _playlist_entry(raw, uid)
        song_ids = [str(item) for item in (raw.get("song_ids") or [])]
        entry["song_ids"] = song_ids
        entry["song_count"] = len(song_ids)
        if entry["special_type"] != 0:
            # 「我喜欢的音乐」这类特殊歌单没法在当前账号下重建；它的内容
            # 已经单独记在 liked_song_ids 里了。
            entry["action"] = "skip"
            entry["reason"] = "special"
        elif not entry["is_own"]:
            # 别人创建的歌单：换个账号也没法重建，只能跳过。
            entry["action"] = "skip"
            entry["reason"] = "foreign"
        else:
            existing = current_by_name.get(entry["name"])
            if existing is None:
                entry["action"] = "create"
            else:
                entry["action"] = "conflict"
                entry["current_id"] = existing["id"]
                entry["current_name"] = existing["name"]
                conflicts.append({
                    "id": entry["id"],
                    "name": entry["name"],
                    "current_id": existing["id"],
                    "current_name": existing["name"],
                    "song_count": len(song_ids),
                })
        plan_playlists.append(entry)

    to_add = [song_id for song_id in backup_liked if song_id not in liked_set]
    already = [song_id for song_id in backup_liked if song_id in liked_set]

    # -- 关注的歌手 ---------------------------------------------------------
    backup_artists = _backup_artists(payload)
    current_artists = _current_artist_ids(api, attempts=attempts,
                                          delay=retry_delay, sleep=sleep)
    artists_to_follow = [item for item in backup_artists
                         if item["id"] not in current_artists]
    artists_present = [item for item in backup_artists
                       if item["id"] in current_artists]

    # -- 收藏的专辑 / 电台 / MV ---------------------------------------------
    albums_plan = _saved_plan(api, payload, "liked_albums",
                              lambda offset: api.get_liked_albums(
                                  uid, limit=SAVED_PAGE, offset=offset),
                              "albums", ("id",), ("name",))
    djradios_plan = _saved_plan(api, payload, "subscribed_djradios",
                                lambda offset: api.get_subscribed_djradios(
                                    uid, limit=SAVED_PAGE, offset=offset),
                                "djradios", ("id",), ("name",))
    mvs_plan = _saved_plan(api, payload, "liked_mvs",
                            lambda offset: api.get_subscribed_mvs(
                                uid, limit=SAVED_PAGE, offset=offset),
                            "mvs", ("vid", "id"), ("title", "name"))

    # -- 云端的听歌排行（播放次数只写本地）------------------------------------
    record = _payload_record(payload)

    # -- 本机状态（播放次数 / 收藏 / 最近播放）--------------------------------
    local_state = _payload_local_state(payload)

    return {
        "ok": True,
        "source": {
            "user_id": str((payload.get("account") or {}).get("user_id") or ""),
            "nickname": str((payload.get("account") or {}).get("nickname") or ""),
            "created_at": str(payload.get("created_at") or ""),
            "app_version": str(payload.get("app_version") or ""),
        },
        "summary": {
            "favorites_total": len(backup_liked),
            "favorites_to_add": len(to_add),
            "favorites_present": len(already),
            "playlists_total": len(plan_playlists),
            "playlists_to_create": sum(1 for item in plan_playlists
                                       if item["action"] == "create"),
            "conflicts": len(conflicts),
            "skipped": sum(1 for item in plan_playlists
                           if item["action"] == "skip"),
            "artists_total": len(backup_artists),
            "artists_to_follow": len(artists_to_follow),
            "artists_present": len(artists_present),
            "albums_total": albums_plan["total"],
            "albums_to_add": len(albums_plan["to_add"]),
            "albums_present": len(albums_plan["present"]),
            "djradios_total": djradios_plan["total"],
            "djradios_to_add": len(djradios_plan["to_add"]),
            "mvs_total": mvs_plan["total"],
            "mvs_to_add": len(mvs_plan["to_add"]),
            "record_songs": len(record),
            "local_play_counts": len(local_state["play_counts"]),
            "local_favorites": len(local_state["favorites"]),
            "local_recent": len(local_state["recent"]),
        },
        "favorites": {"to_add": to_add, "present": already},
        "playlists": plan_playlists,
        "conflicts": conflicts,
        "artists": {"to_follow": artists_to_follow,
                    "already_followed": artists_present},
        "albums": albums_plan,
        "djradios": djradios_plan,
        "mvs": mvs_plan,
        "record": record,
        "local_state": local_state,
        "warnings": ([{"section": "playlists", "message": playlists_error}]
                     if playlists_error else [])
        + ([{"section": "favorites", "message": liked_error}]
           if liked_error else []),
    }


def _backup_artists(payload: dict[str, Any]) -> list[dict[str, str]]:
    """备份文件里记着的关注歌手。"""
    artists: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in (payload.get("followed_artists") or []):
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        artist_id = str(raw_id).strip() if raw_id is not None else ""
        if not artist_id or artist_id in seen:
            continue
        seen.add(artist_id)
        artists.append({"id": artist_id,
                        "name": str(item.get("name") or "Unknown")})
    return artists


def _current_artist_ids(api: Any, *, attempts: int, delay: float,
                         sleep: Callable[[float], None]) -> set[str]:
    """当前账号已经关注的歌手 id。

    读不到就当作一个都没关注：多关注一次已经关注的人没有坏处，少关注一个
    才是真的丢了。
    """
    artists, error = _read_followed_artists(api, attempts=attempts,
                                             delay=delay, sleep=sleep)
    if error and not artists:
        return set()
    return {item["id"] for item in artists}


def _payload_local_state(payload: dict[str, Any]) -> dict[str, Any]:
    """备份文件里记着的本机状态。"""
    raw = payload.get("local_state")
    if not isinstance(raw, dict):
        return {"play_counts": {}, "favorites": [], "recent": []}
    counts = raw.get("play_counts")
    clean_counts: dict[str, int] = {}
    if isinstance(counts, dict):
        for raw_key, raw_value in counts.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            try:
                clean_counts[key] = max(0, int(raw_value))
            except (TypeError, ValueError):
                continue
    favorites = [dict(item) for item in (raw.get("favorites") or [])
                  if isinstance(item, dict) and item.get("key")]
    recent = [dict(item) for item in (raw.get("recent") or [])
              if isinstance(item, dict) and item.get("key")]
    return {
        "play_counts": clean_counts,
        "favorites": favorites,
        "recent": recent,
    }


def apply_import(api: Any, payload: dict[str, Any], plan: dict[str, Any],
                 decisions: dict[str, Any] | None, *,
                 progress: Callable[[str, int, int], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 favorite_batch: int = FAVOURITE_BATCH,
                 track_batch: int = TRACK_BATCH,
                 batch_pause: float = BATCH_PAUSE,
                 local: Any = None) -> dict[str, Any]:
    """按用户的决定把备份并进当前账号。

    ``decisions`` 的形状是 ``{"playlists": {歌单 id: {"action": ..., "name": ...}}}``，
    ``action`` 取 ``skip`` / ``merge`` / ``rename``。**每个冲突都必须有决定**，
    没决定就不写任何东西——闷头改账号是最糟的结果。

    ``local`` 是本机的 :class:`~src.core.library.Library`。播放次数、本地收藏
    和最近播放写回它，**不会向网易云报告**——这些数据云端没有对应的东西。

    返回一份结果：加了多少歌、关注了多少歌手、建了几个歌单、跳过多少、
    失败多少。
    """
    raw_decisions = (decisions or {}).get("playlists")
    choices: dict[str, Any] = raw_decisions if isinstance(raw_decisions, dict) else {}
    conflict_ids = [str(item["id"]) for item in (plan.get("conflicts") or [])]
    missing = [cid for cid in conflict_ids if cid not in choices]
    if missing:
        # 一个冲突都没定就动手，等于替用户做决定。
        return {
            "ok": False,
            "error": "missing_decisions",
            "missing": missing,
            "songs_liked": 0,
            "songs_skipped": 0,
            "playlists_created": 0,
            "playlists_merged": 0,
            "playlists_skipped": 0,
            "artists_followed": 0,
            "albums_liked": 0,
            "djradios_subscribed": 0,
            "mvs_liked": 0,
            "record_counts": 0,
            "tracks_added": 0,
            "failed": 0,
            "skipped_detail": [],
            "local_restored": {"play_counts": 0, "favorites": 0, "recent": 0},
            "errors": [{"section": "conflicts",
                        "message": "decisions missing: " + ", ".join(missing)}],
        }

    errors: list[dict[str, str]] = []
    skipped_detail: list[dict[str, str]] = []
    report = progress or (lambda *args: None)

    # -- 收藏列表 ---------------------------------------------------------
    to_add = [str(item) for item in ((plan.get("favorites") or {}).get("to_add") or [])]
    songs_skipped = len((plan.get("favorites") or {}).get("present") or [])
    songs_liked = 0
    failed = 0
    batch = max(1, int(favorite_batch))
    total = len(to_add)
    for index in range(0, total, batch):
        chunk = to_add[index:index + batch]
        report(PHASE_FAVORITES, index, total)
        for song_id in chunk:
            outcome = _like(api, song_id)
            if outcome == "ok":
                songs_liked += 1
                continue
            if outcome == "rate_limited":
                # 限流不是失败，隔几秒再试一次。
                sleep(BATCH_PAUSE)
                outcome = _like(api, song_id)
                if outcome == "ok":
                    songs_liked += 1
                    continue
            failed += 1
            errors.append({"section": PHASE_FAVORITES,
                           "message": f"{song_id}: {outcome}"})
            if outcome == "not_logged_in":
                # 会话没了，后面的歌也加不上，别再空转。
                break
        if index + batch < total:
            sleep(batch_pause)
    report(PHASE_FAVORITES, total, total)

    # -- 歌单 -------------------------------------------------------------
    playlists_created = 0
    playlists_merged = 0
    playlists_skipped = 0
    tracks_added = 0
    plan_playlists = [item for item in (plan.get("playlists") or [])
                      if isinstance(item, dict)]
    for index, item in enumerate(plan_playlists):
        report(PHASE_PLAYLISTS, index, len(plan_playlists))
        action = str(item.get("action") or "skip")
        playlist_id = str(item.get("id") or "")
        song_ids = [str(song) for song in (item.get("song_ids") or [])]
        if action == "skip":
            playlists_skipped += 1
            skipped_detail.append({"id": playlist_id,
                                   "name": str(item.get("name") or ""),
                                   "reason": str(item.get("reason") or "")})
            continue
        if action == "conflict":
            decision = choices.get(playlist_id)
            decision = decision if isinstance(decision, dict) else {}
            action = str(decision.get("action") or "skip")
            if action == "rename":
                if not str(decision.get("name") or "").strip():
                    failed += 1
                    errors.append({"section": f"playlist:{playlist_id}",
                                   "message": "rename without a name"})
                    continue
            elif action == "merge":
                target_id = str(item.get("current_id") or "")
            else:
                playlists_skipped += 1
                skipped_detail.append({"id": playlist_id,
                                       "name": str(item.get("name") or ""),
                                       "reason": "skipped"})
                continue
        if action in ("create", "rename"):
            name = str(item.get("name") or "").strip()
            if action == "rename":
                rename = choices.get(playlist_id)
                rename = rename if isinstance(rename, dict) else {}
                name = str(rename.get("name") or name)
            created = api.create_playlist(name, 0, "")
            new_id = _created_playlist_id(created)
            if not new_id:
                failed += 1
                errors.append({"section": f"playlist:{playlist_id}",
                               "message": "could not create the playlist"})
                continue
            playlists_created += 1
            target_id = new_id
            pending = song_ids
        else:  # merge
            target_id = str(item.get("current_id") or "")
            pending = _missing_tracks(api, target_id, song_ids)
            if pending is None:
                failed += 1
                errors.append({"section": f"playlist:{playlist_id}",
                               "message": "could not read the target playlist"})
                continue
            playlists_merged += 1
        added = _add_tracks(api, target_id, pending, batch=track_batch,
                            pause=batch_pause, sleep=sleep)
        tracks_added += added
        if added < len(pending):
            failed += len(pending) - added
            errors.append({"section": f"playlist:{playlist_id}",
                           "message": f"{len(pending) - added} songs not added"})
    report(PHASE_PLAYLISTS, len(plan_playlists), len(plan_playlists))

    # -- 关注的歌手 ---------------------------------------------------------
    artists_followed = 0
    to_follow = [item for item in ((plan.get("artists") or {}).get("to_follow") or [])
                 if isinstance(item, dict) and item.get("id")]
    report(PHASE_ARTISTS, 0, len(to_follow))
    for index, artist in enumerate(to_follow):
        report(PHASE_ARTISTS, index, len(to_follow))
        artist_id = str(artist.get("id"))
        outcome = _follow_artist(api, artist_id)
        if outcome == "ok":
            artists_followed += 1
        elif outcome == "rate_limited":
            # 限流不是失败，隔几秒再试一次。
            sleep(BATCH_PAUSE)
            outcome = _follow_artist(api, artist_id)
            if outcome == "ok":
                artists_followed += 1
            else:
                failed += 1
                errors.append({"section": PHASE_ARTISTS,
                               "message": f"{artist_id}: {outcome}"})
        else:
            failed += 1
            errors.append({"section": PHASE_ARTISTS,
                           "message": f"{artist_id}: {outcome}"})
            if outcome == "not_logged_in":
                # 会话没了，后面的也关注不上，别再空转。
                break
        if index + 1 < len(to_follow):
            sleep(batch_pause)
    report(PHASE_ARTISTS, len(to_follow), len(to_follow))

    # -- 收藏的专辑 / 电台 / MV ---------------------------------------------
    albums_to_add = (plan.get("albums") or {}).get("to_add") or []
    report(PHASE_ALBUMS, 0, len(albums_to_add))
    albums_liked, albums_failed = _apply_saved(
        albums_to_add, lambda album_id: api.set_album_like(album_id, True),
        section=PHASE_ALBUMS, sleep=sleep, batch_pause=batch_pause, errors=errors)
    failed += albums_failed
    report(PHASE_ALBUMS, len(albums_to_add), len(albums_to_add))

    djradios_to_add = (plan.get("djradios") or {}).get("to_add") or []
    report(PHASE_DJRADIOS, 0, len(djradios_to_add))
    djradios_subscribed, djradios_failed = _apply_saved(
        djradios_to_add, lambda dj_id: api.set_djradio_subscribe(dj_id, True),
        section=PHASE_DJRADIOS, sleep=sleep, batch_pause=batch_pause, errors=errors)
    failed += djradios_failed
    report(PHASE_DJRADIOS, len(djradios_to_add), len(djradios_to_add))

    mvs_to_add = (plan.get("mvs") or {}).get("to_add") or []
    report(PHASE_MVS, 0, len(mvs_to_add))
    mvs_liked, mvs_failed = _apply_saved(
        mvs_to_add, lambda mv_id: api.set_mv_like(mv_id, True),
        section=PHASE_MVS, sleep=sleep, batch_pause=batch_pause, errors=errors)
    failed += mvs_failed
    report(PHASE_MVS, len(mvs_to_add), len(mvs_to_add))

    # -- 本机状态（只写本地，不向网易云报告）---------------------------------
    local_restored = {"play_counts": 0, "favorites": 0, "recent": 0}
    record_counts = 0
    if local is not None:
        state = plan.get("local_state") or {}
        try:
            local_restored["play_counts"] = int(
                local.merge_play_counts(state.get("play_counts") or {}))
            local_restored["favorites"] = int(
                local.merge_favorites(state.get("favorites") or []))
            local_restored["recent"] = int(
                local.merge_recent(state.get("recent") or []))
        except Exception as exc:
            errors.append({"section": PHASE_LOCAL, "message": str(exc)})

        # 云端的听歌排行也是播放次数，但只写进本机记录——网易云那边不动。
        counts: dict[str, int] = {}
        for item in (plan.get("record") or []):
            if not isinstance(item, dict):
                continue
            song_id = str(item.get("song_id") or "").strip()
            try:
                count = int(item.get("play_count") or 0)
            except (TypeError, ValueError):
                continue
            if not song_id or count <= 0:
                continue
            # 本机计数器的键是 online:<歌曲 id>（见 core/library.py 的 track_key）。
            counts[f"online:{song_id}"] = count
        if counts:
            try:
                record_counts = int(local.merge_play_counts(counts))
                local_restored["play_counts"] += record_counts
            except Exception as exc:
                errors.append({"section": PHASE_RECORD, "message": str(exc)})
    report(PHASE_LOCAL, 1, 1)

    return {
        "ok": True,
        "songs_liked": songs_liked,
        "songs_skipped": songs_skipped,
        "playlists_created": playlists_created,
        "playlists_merged": playlists_merged,
        "playlists_skipped": playlists_skipped,
        "artists_followed": artists_followed,
        "albums_liked": albums_liked,
        "djradios_subscribed": djradios_subscribed,
        "mvs_liked": mvs_liked,
        "record_counts": record_counts,
        "tracks_added": tracks_added,
        "failed": failed,
        "skipped_detail": skipped_detail,
        "local_restored": local_restored,
        "errors": errors,
    }


def _follow_artist(api: Any, artist_id: str) -> str:
    """关注一个歌手，返回状态串。"""
    if not artist_id:
        return "failed"
    try:
        return str(api.set_artist_follow(artist_id, True))
    except Exception as exc:
        return f"failed: {exc}"


def _like(api: Any, song_id: str) -> str:
    """给一首歌点赞，返回 ``set_song_like`` 的状态串。"""
    try:
        return str(api.set_song_like(song_id, True))
    except Exception as exc:
        return f"failed: {exc}"


def _created_playlist_id(created: Any) -> str:
    """从建歌单的返回里取出新歌单的 id。"""
    if not isinstance(created, dict):
        return ""
    playlist = created.get("playlist")
    if isinstance(playlist, dict):
        candidate = str(playlist.get("id") or "")
        if candidate:
            return candidate
    for key in ("id", "playlistId"):
        candidate = str(created.get(key) or "")
        if candidate:
            return candidate
    return ""


def _missing_tracks(api: Any, playlist_id: str, song_ids: Sequence[str]) -> list[str] | None:
    """合并时真正要加的歌：目标歌单里还没有的那些。"""
    try:
        existing = api.get_playlist_songs(playlist_id, fresh=True)
    except Exception:
        return None
    if existing is None:
        return None
    have = set(_song_ids(existing))
    return [song_id for song_id in song_ids if song_id not in have]


def _add_tracks(api: Any, playlist_id: str, song_ids: Sequence[str], *, batch: int,
                pause: float, sleep: Callable[[float], None]) -> int:
    """分批把歌加进歌单，返回加进去多少首。"""
    ids = [song_id for song_id in song_ids if song_id]
    if not ids:
        return 0
    size = max(1, int(batch))
    added = 0
    for index in range(0, len(ids), size):
        chunk = ids[index:index + size]
        try:
            result = api.add_tracks_to_playlist(playlist_id, chunk)
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("code") == 200:
            added += len(chunk)
        if index + size < len(ids):
            sleep(pause)
    return added
