"""Translation layer between the Bilibili client and the desktop frontend.

The core client (:mod:`src.core.bilibili`) speaks the Bilibili web API and
answers with plain lists; the frontend speaks dictionaries.  This module is
the bridge between the two: every method returns ``{"success": True, ...}``
when the call worked and ``{"success": False, "error_key": ..., ...}`` when
it did not, so :mod:`src.gui.bridge` never has to know how Bilibili works.

No request is made here -- the traffic goes through
:class:`src.core.bilibili.BiliClient`, which is injected in the tests.

Two conventions worth knowing:

* ``error_key`` is an i18n key of the existing catalogue, so the frontend can
  render it like any other bridge error.  The raw reason of the core client
  (for example ``api_error``) travels in ``reason``.
* The core client never raises and swallows transport errors into an empty
  list.  An empty ``items`` list therefore means "the endpoint answered
  nothing" -- it cannot be told apart from "the request failed".
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.core.bili_cache import BiliCache
from src.core.bilibili import BILI_RID_NAMES, BILI_RIDS, BiliClient
from src.core.library import track_key

__all__ = ["BilibiliBridge", "BILI_ORDER_ALIASES", "track_payload"]

log = logging.getLogger(__name__)

#: B站自己的「没有登录」错误码。前端靠它把「请先登录 B站账号」这句话
#: 显示出来，而不是把一串英文异常丢给用户。
CODE_NOT_LOGGED_IN = -101

#: The helper modules and the class each one exposes.
_HELPER_MODULES = {
    "BiliDanmaku": "bili_danmaku",
    "BiliIm": "bili_im",
    "BiliUploader": "bili_upload",
}


def track_payload(item: dict[str, Any]) -> dict[str, Any]:
    """One Bilibili item, in the shape the player list renders.

    The same shape comes out of the search, the hot list and the folders, so
    the frontend can render all three with one code path.
    """
    item = item or {}
    bvid = str(item.get("bvid") or item.get("id") or "")
    return {
        "id": bvid,
        "key": track_key("bilibili", bvid),
        "source": "bilibili",
        "name": str(item.get("title") or item.get("name") or ""),
        "artists": str(item.get("author") or item.get("artists") or ""),
        "author_mid": str(item.get("author_mid") or ""),
        "album": str(item.get("category") or ""),
        "cover_url": str(item.get("cover") or item.get("cover_url") or ""),
        "duration": _as_int(item.get("duration"), 0),
        "play_count": _as_int(item.get("play_count"), 0),
        "url": str(item.get("url") or ""),
        "bvid": bvid,
        "cid": _as_int(item.get("cid"), 0),
        "kind": "video",
    }

#: Sort orders the search endpoint understands, keyed by the friendly name
#: the desktop uses.  A value that is not listed here is passed through, so
#: the raw API names (``click``, ``pubdate``, ``dm`` ...) keep working.
BILI_ORDER_ALIASES: dict[str, str] = {
    "default": "totalrank",
    "rank": "totalrank",
    "totalrank": "totalrank",
    "play": "click",
    "click": "click",
    "hot": "click",
    "date": "pubdate",
    "pubdate": "pubdate",
    "new": "pubdate",
    "danmaku": "dm",
    "dm": "dm",
}

#: The core client's own failure codes, mapped onto i18n keys that exist in
#: the catalogue today.  The keys carry no placeholder: the desktop shows an
#: ``error_key`` without interpolating one, so ``error.api`` would end up on
#: the screen as a literal ``{message}``.
_AUDIO_ERROR_KEYS: dict[str, str] = {
    "invalid_bvid": "error.reason.song_not_found",
    "no_cid": "error.reason.song_not_found",
    "network_error": "error.network",
    "api_error": "error.unknown",
    "no_audio": "error.reason.no_url",
}


def _as_int(value: Any, default: int) -> int:
    """``int(value)`` that falls back to ``default`` instead of raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _order_value(order: Any) -> str:
    """The API sort order for a friendly name (``play`` -> ``click``)."""
    text = str(order or "default").strip().lower()
    return BILI_ORDER_ALIASES.get(text, text)


def _category_key(category: Any) -> str | None:
    """The partition key for a name (``music``) or a partition id (``28``).

    ``None`` means the partition is unknown.
    """
    text = str(category if category is not None else "all").strip()
    if not text:
        return "all"
    if text in BILI_RIDS:
        return text
    if text.isdigit():
        for key, rid in BILI_RIDS.items():
            if rid == int(text):
                return key
    return None


class BilibiliBridge:
    """The Bilibili capabilities the desktop frontend can call.

    ``client`` may be replaced in the tests; the default one talks to the
    real (read-only) Bilibili web API.
    """

    def __init__(
        self,
        client: BiliClient | None = None,
        session: Any | None = None,
        media: Any | None = None,
    ) -> None:
        self._session = session
        self._media = media
        #: 搜索、榜单、详情、弹幕这些重复要用的东西缓存在磁盘上。
        self._cache = BiliCache()
        if client is not None:
            self._client = client
        elif session is not None:
            self._client = session.client
        else:
            from src.auth.bili_login import get_bili_session

            self._session = get_bili_session()
            self._client = self._session.client
        self._qr_key = ""

    @property
    def client(self) -> BiliClient:
        """The underlying core client."""
        return self._client

    @property
    def session(self) -> Any:
        """The session that remembers the cookie (may be ``None``)."""
        return self._session

    # ------------------------------------------------------------------ search
    def search(
        self,
        keyword: str,
        category: str = "all",
        order: str = "default",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        """Search Bilibili videos by keyword.

        ``category`` is a partition name (``music``) or a partition id
        (``28``); ``order`` is a friendly sort name (``play``, ``date``) or
        a raw API order.  The answer carries the normalised items of the
        core client.
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": False, "error_key": "player.search_empty", "message": ""}
        category_key = _category_key(category)
        if category_key is None:
            return {
                "success": False,
                "error_key": "error.unknown",
                "message": f"unknown category: {category}",
            }
        api_order = _order_value(order)
        page_number = max(1, _as_int(page, 1))
        size = max(1, min(_as_int(page_size, 30), 50))
        cache_key = f"{keyword}|{category_key}|{api_order}|{page_number}|{size}"
        try:
            items, _cached = self._cache.remember(
                "search", cache_key,
                lambda: self._client.search(
                    keyword,
                    page=page_number,
                    page_size=size,
                    order=api_order,
                    category=category_key,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili search failed: %s", exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}
        return {
            "success": True,
            "keyword": keyword,
            "category": category_key,
            "order": api_order,
            "page": page_number,
            "page_size": size,
            "items": items,
            "count": len(items),
        }

    # -------------------------------------------------------------- partitions
    def partitions(self) -> dict[str, Any]:
        """The Bilibili partitions, with the id and the name of each one."""
        items = [
            {
                "rid": rid,
                "name": BILI_RID_NAMES.get(rid, key),
                "key": key,
            }
            for key, rid in BILI_RIDS.items()
        ]
        return {"success": True, "partitions": items, "count": len(items)}

    # ------------------------------------------------------------------ popular
    def popular(self, rid: int = 28, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        """The hot list of one partition.

        ``rid=0`` is the site wide hot list; the music partitions (``28``,
        ``29``) are served by the partition feed, because the site wide list
        ignores the partition.
        """
        partition = _as_int(rid, 28)
        if partition != 0 and partition not in set(BILI_RIDS.values()):
            return {
                "success": False,
                "error_key": "error.unknown",
                "message": f"unknown partition: {rid}",
            }
        page_number = max(1, _as_int(page, 1))
        size = max(1, min(_as_int(page_size, 30), 50))
        cache_key = f"{partition}|{page_number}|{size}"
        try:
            items, _cached = self._cache.remember(
                "popular", cache_key,
                lambda: self._client.popular(rid=partition, page=page_number,
                                             page_size=size),
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili popular failed: %s", exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}
        return {
            "success": True,
            "rid": partition,
            "page": page_number,
            "page_size": size,
            "items": items,
            "count": len(items),
        }

    # ------------------------------------------------------------------ audio
    def audio_url(self, bvid: str, quality: int = 0) -> dict[str, Any]:
        """Resolve the audio stream of one video.

        The core client answers with a failure dictionary instead of raising,
        so the reason is translated into an i18n key and kept in ``reason``.
        """
        video_id = str(bvid or "").strip()
        if not video_id:
            return {
                "success": False,
                "error_key": _AUDIO_ERROR_KEYS["invalid_bvid"],
                "reason": "invalid_bvid",
                "message": "missing bvid",
            }
        try:
            result = self._client.audio_url(video_id, quality=_as_int(quality, 0))
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili audio url failed: %s", exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}
        if not result.get("success"):
            reason = str(result.get("error_key") or "")
            return {
                "success": False,
                "error_key": _AUDIO_ERROR_KEYS.get(reason, "error.unknown"),
                "reason": reason,
                "message": str(result.get("message") or ""),
            }
        return {
            "success": True,
            "bvid": video_id,
            "url": str(result.get("url") or ""),
            "mime": str(result.get("mime") or ""),
            "quality": _as_int(result.get("quality"), 0),
            "need_referer": bool(result.get("need_referer", True)),
        }

    # ----------------------------------------------------------------- account
    def account(self, refresh: bool = False) -> dict[str, Any]:
        """The account information of the signed-in Bilibili user."""
        if self._session is not None:
            info = self._session.account_info(refresh=refresh)
        else:
            info = self._client.account_info()
        return {
            "success": True,
            "logged_in": bool(info.get("logged_in")),
            "mid": str(info.get("mid") or ""),
            "uname": str(info.get("uname") or ""),
            "face": str(info.get("face") or ""),
            "level": _as_int(info.get("level"), 0),
            "vip_status": _as_int(info.get("vip_status"), 0),
            "vip_label": str(info.get("vip_label") or ""),
            "coins": _as_int(info.get("coins"), 0),
            "message": "",
        }

    def _remember(self, cookie: str) -> None:
        """Store a fresh cookie and hand it to the media server."""
        if self._session is None or not cookie:
            return
        self._session.save(cookie)
        if self._media is not None:
            self._session.apply_to_media(self._media)

    def login_qrcode(self) -> dict[str, Any]:
        """Ask for a login QR code."""
        result = self._client.login_qrcode() or {}
        self._qr_key = str(result.get("key") or "")
        return {
            "success": bool(result.get("success")),
            "qrcode_url": str(result.get("qrcode_url") or ""),
            "message": str(result.get("message") or ""),
        }

    def login_poll(self) -> dict[str, Any]:
        """Check the state of the login QR code."""
        if not self._qr_key:
            return {"success": False, "status": "failed", "message": "没有二维码"}
        result = self._client.login_poll(self._qr_key) or {}
        status = str(result.get("status") or "failed")
        cookie = str(result.get("cookie") or "")
        if status == "success" and cookie:
            self._remember(cookie)
        return {
            "success": bool(result.get("success")),
            "status": status,
            "message": str(result.get("message") or ""),
        }

    def login_cookie(self, cookie: str) -> dict[str, Any]:
        """Sign in with a cookie copied from the browser."""
        result = self._client.login_cookie(cookie) or {}
        ok = bool(result.get("success"))
        if ok:
            self._remember(str(result.get("cookie") or cookie))
        return {
            "success": ok,
            "message": str(result.get("message") or ""),
        }

    def login_password(self, username: str, password: str, captcha_key: str = "",
                       captcha: str = "", challenge: str = "", validate: str = "",
                       seccode: str = "") -> dict[str, Any]:
        """Sign in with a user name and a password."""
        result = self._client.login_password(
            username, password, captcha_key=captcha_key, captcha=captcha,
            challenge=challenge, validate=validate, seccode=seccode,
        ) or {}
        ok = bool(result.get("success"))
        if ok:
            self._remember(str(result.get("cookie") or ""))
        return {
            "success": ok,
            "message": str(result.get("message") or ""),
            "captcha_required": bool(result.get("captcha_required")),
            "captcha_key": str(result.get("captcha_key") or ""),
        }

    def captcha(self) -> dict[str, Any]:
        """Ask for the verification challenge used by the password and SMS flows."""
        result = self._client.captcha() or {}
        return {
            "success": bool(result.get("success")),
            "gt": str(result.get("gt") or ""),
            "challenge": str(result.get("challenge") or ""),
            "token": str(result.get("token") or ""),
            "message": str(result.get("message") or ""),
        }

    def send_sms_code(self, tel: str, cid: str = "86") -> dict[str, Any]:
        """Send a verification code to a phone number."""
        result = self._client.send_sms_code(tel, cid=cid) or {}
        return {
            "success": bool(result.get("success")),
            "message": str(result.get("message") or ""),
        }

    def login_sms(self, tel: str, code: str, cid: str = "86") -> dict[str, Any]:
        """Sign in with the code sent to a phone number."""
        result = self._client.login_sms(tel, code, cid=cid) or {}
        ok = bool(result.get("success"))
        if ok:
            self._remember(str(result.get("cookie") or ""))
        return {
            "success": ok,
            "message": str(result.get("message") or ""),
        }

    def register_send_code(self, tel: str, cid: str = "86") -> dict[str, Any]:
        """Send the code needed to register a new account."""
        result = self._client.register_send_code(tel, cid=cid) or {}
        return {
            "success": bool(result.get("success")),
            "message": str(result.get("message") or ""),
        }

    def register(self, username: str, password: str, tel: str, code: str,
                 cid: str = "86") -> dict[str, Any]:
        """Create a new Bilibili account and sign in with it."""
        result = self._client.register(username, password, tel, code, cid=cid) or {}
        ok = bool(result.get("success"))
        if ok:
            self._remember(str(result.get("cookie") or ""))
        return {
            "success": ok,
            "message": str(result.get("message") or ""),
        }

    def logout(self) -> dict[str, Any]:
        """Sign out."""
        result = self._client.logout() or {}
        if self._session is not None:
            self._session.clear()
            if self._media is not None:
                self._session.apply_to_media(self._media)
        return {
            "success": True,
            "message": str(result.get("message") or ""),
        }

    # --------------------------------------------------------------- favorites
    def favorites(self) -> dict[str, Any]:
        """The folders of the signed-in user."""
        if not self._client.is_logged_in:
            return {"success": False, "code": CODE_NOT_LOGGED_IN,
                    "folders": [], "count": 0, "message": "没有登录 B站"}
        try:
            raw, _cached = self._cache.remember(
                "folders", "all", lambda: self._client.favorite_folders() or [],
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili favourites failed: %s", exc)
            return {"success": False, "folders": [], "count": 0, "message": str(exc)}
        folders = [
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "media_count": _as_int(item.get("media_count"), 0),
                "cover": str(item.get("cover") or ""),
            }
            for item in raw if isinstance(item, dict)
        ]
        return {"success": True, "folders": folders, "count": len(folders), "message": ""}

    def favorite_videos(self, folder_id: str = "", page: int = 1,
                        page_size: int = 30) -> dict[str, Any]:
        """The videos inside one folder."""
        page_number = max(1, _as_int(page, 1))
        size = max(1, min(_as_int(page_size, 30), 50))
        folder = str(folder_id or "").strip()
        if not self._client.is_logged_in:
            return {
                "success": False, "code": CODE_NOT_LOGGED_IN, "folder_id": folder,
                "page": page_number, "items": [], "count": 0, "has_more": False,
                "message": "没有登录 B站",
            }
        if not folder:
            folders = self.favorites().get("folders") or []
            if not folders:
                return {
                    "success": False, "folder_id": "", "page": page_number,
                    "items": [], "count": 0, "has_more": False,
                    "message": "没有收藏夹",
                }
            folder = str(folders[0].get("id") or "")
        cache_key = f"{folder}|{page_number}|{size}"
        try:
            result, _cached = self._cache.remember(
                "folder_items", cache_key,
                lambda: self._client.favorite_videos(folder, page=page_number,
                                                      page_size=size) or {},
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili favourites failed: %s", exc)
            return {
                "success": False, "folder_id": folder, "page": page_number,
                "items": [], "count": 0, "has_more": False, "message": str(exc),
            }
        items = [track_payload(item) for item in (result.get("items") or [])
                 if isinstance(item, dict)]
        return {
            "success": True,
            "folder_id": folder,
            "page": page_number,
            "items": items,
            "count": len(items),
            "has_more": bool(result.get("has_more")),
            "message": "",
        }

    # ---------------------------------------------------------------- playback
    def video_streams(self, bvid: str, quality: int = 0) -> dict[str, Any]:
        """The playable streams of one video.

        The CDN urls are replaced with loopback urls: the webview may not
        talk to the CDN directly, and the signed url has to stay in the
        backend anyway.
        """
        video_id = str(bvid or "").strip()
        if not video_id:
            return {"success": False, "message": "缺少视频编号"}
        try:
            streams = self._client.video_streams(video_id, quality=_as_int(quality, 0)) or {}
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili streams failed: %s", exc)
            return {"success": False, "message": str(exc)}
        if not streams.get("success"):
            return {
                "success": False,
                "message": str(streams.get("message") or "取播放地址失败"),
            }
        answer = {
            "success": True,
            "bvid": video_id,
            "cid": _as_int(streams.get("cid"), 0),
            "duration": _as_int(streams.get("duration"), 0),
            # The details the video page needs: title, uploader and cover.
            "title": str(streams.get("title") or ""),
            "author": str(streams.get("author") or ""),
            "author_mid": str(streams.get("author_mid") or ""),
            "cover": str(streams.get("cover") or ""),
            "qualities": streams.get("qualities") or [],
            "audio": self._proxied(streams.get("audio")),
            "video": self._proxied(streams.get("video")),
            "muxed": self._proxied(streams.get("muxed")),
            "flac": self._proxied(streams.get("flac")),
            "dolby": self._proxied(streams.get("dolby")),
            "message": "",
        }
        return answer

    def _proxied(self, stream: Any) -> dict[str, Any] | None:
        """Replace the CDN url of one stream with a loopback url."""
        if not isinstance(stream, dict):
            return None
        url = str(stream.get("url") or "")
        if not url:
            return None
        proxied = url
        if self._media is not None:
            proxied = self._media.url_for_bili(url) or ""
        result = dict(stream)
        result["url"] = proxied
        result["origin_url"] = url
        return result

    # ------------------------------------------------------- 互动（点赞/投币/收藏）
    def video_stat(self, bvid: str) -> dict[str, Any]:
        """The counters of one video (播放/弹幕/评论/收藏/投币/点赞)."""
        cached, _from_disk = self._cache.remember(
            "stat", str(bvid or ""),
            lambda: self._call_core("video_stat", self._client.video_stat, bvid),
        )
        if not cached.get("success"):
            return {"success": False, "stat": {},
                    "message": cached.get("message", "")}
        return {"success": True, "stat": cached.get("stat") or {}, "message": ""}

    def like_video(self, bvid: str, like: bool = True) -> dict[str, Any]:
        """Like (or unlike) one video."""
        result = self._call_core("like_video", self._client.like_video, bvid, like)
        if result.get("success"):
            # The counters changed; the cached ones are stale now.
            self._cache.discard("stat", str(bvid or ""))
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    def coin_video(self, bvid: str, count: int = 1,
                   also_like: bool = False) -> dict[str, Any]:
        """Throw one or two coins at a video."""
        result = self._call_core("coin_video", self._client.coin_video, bvid,
                                 count, also_like)
        if result.get("success"):
            self._cache.discard("stat", str(bvid or ""))
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or ""),
                "balance": int(result.get("balance") or 0)}

    def coin_balance(self) -> dict[str, Any]:
        """The coin balance of the signed-in account."""
        result = self._call_core("coin_balance", self._client.coin_balance)
        return {"success": bool(result.get("success")),
                "balance": int(result.get("balance") or 0),
                "coin": int(result.get("coin") or 0),
                "message": str(result.get("message") or "")}

    def favorite_video(self, bvid: str, folder_id: str = "",
                       remove: bool = False) -> dict[str, Any]:
        """Put a video into a favourite folder (or take it out)."""
        result = self._call_core("favorite_video", self._client.favorite_video,
                                 bvid, folder_id, remove)
        if result.get("success"):
            self._cache.invalidate_folders()
            self._cache.discard("stat", str(bvid or ""))
        return {"success": bool(result.get("success")),
                "folder_id": str(result.get("folder_id") or ""),
                "message": str(result.get("message") or "")}

    def favorite_folder(self, folder_id: str, remove: bool = False) -> dict[str, Any]:
        """Favourite somebody else's folder."""
        result = self._call_core("favorite_folder", self._client.favorite_folder,
                                 folder_id, remove)
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    # ------------------------------------------------------------------ 评论
    def comments(self, bvid: str, page: int = 1, sort: str = "hot") -> dict[str, Any]:
        """The comments of one video."""
        cache_key = f"{bvid}|{page}|{sort}"
        result, _from_disk = self._cache.remember(
            "comments", cache_key,
            lambda: self._call_core("comments", self._client.comments,
                                     bvid, page, sort),
        )
        return {
            "success": bool(result.get("success")),
            "comments": result.get("items") or result.get("comments") or [],
            "count": int(result.get("count") or 0),
            "has_more": bool(result.get("has_more")),
            "message": str(result.get("message") or ""),
        }

    def post_comment(self, bvid: str, content: str,
                     parent_rpid: int = 0) -> dict[str, Any]:
        """Write a comment (or a reply to one)."""
        result = self._call_core("post_comment", self._client.post_comment,
                                 bvid, content, parent_rpid)
        if result.get("success"):
            # 键是 "<bvid>|<页码>|<排序>"，按前缀扔，别把别的视频的也清了。
            self._cache.discard_prefix("comments", f"{bvid or ''}|")
        return {"success": bool(result.get("success")),
                "rpid": str(result.get("rpid") or ""),
                "message": str(result.get("message") or "")}

    def like_comment(self, rpid: str, like: bool = True) -> dict[str, Any]:
        """Like (or unlike) a comment."""
        result = self._call_core("like_comment", self._client.like_comment,
                                 rpid, like)
        if result.get("success"):
            self._cache.discard_prefix("comments", "")
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    # ------------------------------------------------------------------ 弹幕
    def danmaku(self, bvid: str, cid: int = 0) -> dict[str, Any]:
        """The danmaku of one video."""
        helper = self._helper("BiliDanmaku")
        if helper is None:
            return {"success": False, "items": [], "count": 0, "mask": {},
                    "message": "弹幕模块不可用"}
        result, _from_disk = self._cache.remember(
            "danmaku", f"{bvid}|{cid}",
            lambda: helper.danmaku(bvid, cid=cid),
        )
        return {
            "success": bool(result.get("success")),
            "items": result.get("items") or [],
            "count": int(result.get("count") or 0),
            "mask": result.get("mask") or {},
            "message": str(result.get("message") or ""),
        }

    def send_danmaku(self, bvid: str, text: str, cid: int = 0,
                     color: int = 16777215, size: int = 25,
                     mode: int = 1) -> dict[str, Any]:
        """Send one danmaku."""
        helper = self._helper("BiliDanmaku")
        if helper is None:
            return {"success": False, "message": "弹幕模块不可用"}
        result = helper.send(bvid, text, cid=cid, color=color, size=size, mode=mode)
        if result.get("success"):
            self._cache.discard("danmaku", f"{bvid}|{cid}")
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    # ------------------------------------------------------------ 观看历史
    def history(self, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        """What the account watched."""
        result = self._call_core("history", self._client.history, page, page_size)
        return {
            "success": bool(result.get("success")),
            "items": result.get("items") or [],
            "count": int(result.get("count") or 0),
            "has_more": bool(result.get("has_more")),
            "message": str(result.get("message") or ""),
        }

    # ------------------------------------------------------------ 个人主页
    def space(self, mid: str) -> dict[str, Any]:
        """The profile of an uploader."""
        result, _from_disk = self._cache.remember(
            "space", str(mid or ""),
            lambda: self._call_core("space", self._client.space, str(mid or "")),
        )
        return {"success": bool(result.get("success")),
                "profile": result.get("profile") or {},
                "message": str(result.get("message") or "")}

    def space_videos(self, mid: str, page: int = 1,
                     page_size: int = 30) -> dict[str, Any]:
        """The videos an uploader published."""
        result, _from_disk = self._cache.remember(
            "space_videos", f"{mid}|{page}|{page_size}",
            lambda: self._call_core("space_videos", self._client.space_videos,
                                     str(mid or ""), page, page_size),
        )
        return {
            "success": bool(result.get("success")),
            "items": result.get("items") or [],
            "count": int(result.get("count") or 0),
            "has_more": bool(result.get("has_more")),
            "message": str(result.get("message") or ""),
        }

    def follow_user(self, mid: str, follow: bool = True) -> dict[str, Any]:
        """Follow (or unfollow) an uploader."""
        result = self._call_core("follow_user", self._client.follow_user,
                                 str(mid or ""), follow)
        if result.get("success"):
            self._cache.invalidate_follow(str(mid or ""))
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    def follow_state(self, mid: str) -> dict[str, Any]:
        """Whether the account follows this uploader."""
        result, _from_disk = self._cache.remember(
            "follow", str(mid or ""),
            lambda: self._call_core("follow_state", self._client.follow_state,
                                     str(mid or "")),
        )
        return {"success": bool(result.get("success")),
                "following": bool(result.get("following")),
                "relation": int(result.get("relation") or 0),
                "message": str(result.get("message") or "")}

    # ------------------------------------------------------------------ 私信
    def messages(self, size: int = 20) -> dict[str, Any]:
        """The private-message conversations."""
        helper = self._helper("BiliIm")
        if helper is None:
            return {"success": False, "items": [], "count": 0,
                    "message": "私信模块不可用"}
        result = helper.conversations(size=size)
        return {
            "success": bool(result.get("success")),
            "items": result.get("items") or [],
            "count": int(result.get("count") or 0),
            "message": str(result.get("message") or ""),
        }

    def message_history(self, session_id: str, talker_id: str,
                        size: int = 20) -> dict[str, Any]:
        """The messages of one conversation."""
        helper = self._helper("BiliIm")
        if helper is None:
            return {"success": False, "items": [], "count": 0,
                    "message": "私信模块不可用"}
        result = helper.messages(str(session_id or ""), str(talker_id or ""),
                                 size=size)
        return {
            "success": bool(result.get("success")),
            "items": result.get("items") or [],
            "count": int(result.get("count") or 0),
            "message": str(result.get("message") or ""),
        }

    def send_message(self, talker_id: str, text: str,
                     session_id: str = "") -> dict[str, Any]:
        """Send one private message."""
        helper = self._helper("BiliIm")
        if helper is None:
            return {"success": False, "message": "私信模块不可用"}
        result = helper.send(str(talker_id or ""), str(text or ""),
                             session_id=str(session_id or ""))
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    # ------------------------------------------------------------------ 投稿
    def upload_categories(self) -> dict[str, Any]:
        """The categories a video can be posted to."""
        helper = self._helper("BiliUploader")
        if helper is None:
            return {"success": False, "categories": [], "message": "投稿模块不可用"}
        result = helper.categories()
        return {
            "success": bool(result.get("success")),
            "categories": result.get("categories") or [],
            "message": str(result.get("message") or ""),
        }

    def upload_video(self, video_path: str, title: str, tid: int,
                     desc: str = "", tags: str = "",
                     cover_path: str = "") -> dict[str, Any]:
        """Post a video."""
        helper = self._helper("BiliUploader")
        if helper is None:
            return {"success": False, "message": "投稿模块不可用", "aid": "",
                    "bvid": "", "steps": []}
        result = helper.upload(str(video_path or ""), str(title or ""), int(tid or 0),
                               desc=str(desc or ""), tags=str(tags or ""),
                               cover_path=str(cover_path or ""))
        return {
            "success": bool(result.get("success")),
            "message": str(result.get("message") or ""),
            "aid": str(result.get("aid") or ""),
            "bvid": str(result.get("bvid") or ""),
            "steps": result.get("steps") or [],
        }

    # ------------------------------------------------------------- 内部工具
    def _call_core(self, label: str, method: Callable[..., Any],
                   *args: Any) -> dict[str, Any]:
        """Call a core method and turn an exception into a failure answer."""
        try:
            result = method(*args)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili %s failed: %s", label, exc)
            return {"success": False, "message": "操作失败，请稍后再试"}
        return result if isinstance(result, dict) else {"success": False}

    def _helper(self, class_name: str) -> Any:
        """One of the helper modules, or ``None`` when it is not available."""
        import importlib

        try:
            module = importlib.import_module(f"src.core.{_HELPER_MODULES[class_name]}")
        except Exception as exc:  # pragma: no cover - the module is ours
            log.warning("bilibili helper %s is unavailable: %s", class_name, exc)
            return None
        return getattr(module, class_name)(self._client)

    # ---------------------------------------------------------------- download
    def download(self, tracks: list[dict[str, Any]] | None = None,
                 options: dict[str, Any] | None = None,
                 progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Download the video and the sound of every track of the list.

        ``progress`` is called with one dictionary per file: ``kind``,
        ``path``, ``downloaded``, ``total`` and ``progress`` (percent), and
        ``status`` once the file is done.
        """
        from src.core.bili_download import BiliDownloader

        items = [track for track in (tracks or []) if isinstance(track, dict)]
        if not items:
            return {"success": False, "count": 0, "message": "没有要下载的内容"}
        options = options or {}
        download_dir = str(options.get("download_dir") or "")
        if not download_dir:
            from src.config.settings import get_settings

            download_dir = get_settings().download.download_dir
        downloader = BiliDownloader(
            download_dir,
            self._client,
            quality=_as_int(options.get("quality"), 0),
            overwrite=bool(options.get("overwrite")),
            max_concurrent=_as_int(options.get("max_concurrent"), 2),
        )
        if progress is not None:
            downloader.set_progress_callback(progress)
            downloader.set_done_callback(progress)
        try:
            results = downloader.download(items)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili download failed: %s", exc)
            return {"success": False, "count": 0, "message": str(exc)}
        failed = [item for item in results if item.status == "failed"]
        return {
            "success": not failed,
            "count": len(results),
            "files": [item.to_dict() for item in results],
            "message": "下载失败" if failed else "",
        }

