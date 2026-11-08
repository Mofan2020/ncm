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
from src.core.bilibili import BILI_RID_NAMES, BILI_RIDS, BiliClient, qr_png_data_url
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
        # 收藏夹的批量接口（移动 / 移出）要的是 aid 列表，只有 bvid 不够。
        "aid": _as_int(item.get("aid"), 0),
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
            return {
                "success": True,
                "keyword": keyword,
                "category": category_key,
                "order": api_order,
                "page": page_number,
                "page_size": size,
                "items": [],
                "count": 0,
            }
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
            return {
                "success": True,
                "rid": partition,
                "page": page_number,
                "page_size": size,
                "items": [],
                "count": 0,
            }
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
        """The account information of the signed-in Bilibili user.

        头像、昵称、等级这些几乎不变，所以走磁盘缓存（10 分钟）。
        ``refresh=True`` 是用户点了「刷新」——那时必须真的去问一次，
        缓存不能拦着。

        缓存里只放慢变的那几样：硬币余额和会员状态是会变的，但它们本来
        也不来自 /nav（/nav 只给等级），而且各自有专门的接口，
        这里沿用 /nav 的那份，避免多打一次请求。
        """
        if refresh:
            self._cache.invalidate_account()
        cached = None
        if not refresh:
            cached = self._cache.get("account", "current")
        if isinstance(cached, dict) and cached:
            info = cached
        elif self._session is not None:
            info = self._session.account_info(refresh=refresh)
        else:
            info = self._client.account_info()
        if info.get("logged_in"):
            self._cache.set("account", "current", {
                "logged_in": True,
                "mid": str(info.get("mid") or ""),
                "uname": str(info.get("uname") or ""),
                "face": str(info.get("face") or ""),
                "level": _as_int(info.get("level"), 0),
                "vip_status": _as_int(info.get("vip_status"), 0),
                "vip_label": str(info.get("vip_label") or ""),
            })
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
        # 登录成功 = 换了个人。旧账号的缓存必须全清，否则新账号进来
        # 看到的是上一个人的头像、收藏夹和合集。
        self._cache.invalidate_account()
        self._cache.invalidate_folders()
        self._cache.invalidate_collections()
        self._cache.invalidate_bangumi()

    def login_qrcode(self) -> dict[str, Any]:
        """Ask for a login QR code."""
        result = self._client.login_qrcode() or {}
        self._qr_key = str(result.get("key") or "")
        url = str(result.get("qrcode_url") or "")
        return {
            "success": bool(result.get("success")),
            "qrcode_url": url,
            # B站给的是扫码确认页的地址，不是图片，所以自己画一张。
            "qrcode_data_url": qr_png_data_url(url) if url else "",
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
        """Send a verification code to a phone number.

        The answer carries the ``captcha_key`` that the login step has to
        send back, so it is handed to the frontend.
        """
        result = self._client.send_sms_code(tel, cid=cid) or {}
        return {
            "success": bool(result.get("success")),
            "captcha_key": str(result.get("captcha_key") or ""),
            # When the risk control fires, the server hands out the page
            # the user has to solve the slider on.
            "captcha_required": bool(result.get("captcha_required")),
            "recaptcha_url": str(result.get("recaptcha_url") or ""),
            "message": str(result.get("message") or ""),
        }

    def login_sms(self, tel: str, code: str, cid: str = "86",
                  captcha_key: str = "") -> dict[str, Any]:
        """Sign in with the code sent to a phone number.

        ``captcha_key`` is the one the send step handed out; without it the
        server answers ``86205``（验证码失效）.
        """
        result = self._client.login_sms(tel, code, cid=cid,
                                         captcha_key=captcha_key) or {}
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
        # 登出后账号缓存必须清掉：留着的话下一个登录的人会看到
        # 上一个人的头像和昵称。
        self._cache.invalidate_account()
        self._cache.invalidate_folders()
        self._cache.invalidate_collections()
        self._cache.invalidate_bangumi()
        return {
            "success": True,
            "message": str(result.get("message") or ""),
        }

    # --------------------------------------------------------------- favorites
    # ------------------------------------------- 合集 / 追番 / 番剧 / 动态
    def subscribed_collections(self, page: int = 1,
                               page_size: int = 20) -> dict[str, Any]:
        """The UGC collections the signed-in user follows.

        Not the favourites: a collection (合集) is a series of videos an
        uploader put together, and following it is what keeps new episodes
        coming.  The favourites are a separate list, in :meth:`favorites`.

        ``page`` walks the list: the first page alone hides most of it
        (this account has 65 collections, a page holds 20).
        """
        if not self._client.is_logged_in:
            return {"success": False, "code": CODE_NOT_LOGGED_IN,
                    "collections": [], "count": 0, "has_more": False,
                    "page": 1, "total": 0, "message": "没有登录 B站"}
        page_number = max(1, _as_int(page, 1))
        size = max(1, min(_as_int(page_size, 20), 40))
        cache_key = f"{page_number}|{size}"
        try:
            result, _cached = self._cache.remember(
                "collections", cache_key,
                lambda: self._client.subscribed_collections(
                    page=page_number, page_size=size) or {},
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili collections failed: %s", exc)
            return {"success": False, "collections": [], "count": 0,
                    "has_more": False, "page": page_number, "total": 0,
                    "message": str(exc)}
        rows = [
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "cover": str(item.get("cover") or ""),
                "count": _as_int(item.get("count"), 0),
                "author_mid": str(item.get("author_mid") or ""),
                "author_name": str(item.get("author_name") or ""),
                "description": str(item.get("description") or ""),
                "url": str(item.get("url") or ""),
            }
            for item in (result.get("items") or []) if isinstance(item, dict)
        ]
        return {
            "success": True,
            "collections": rows,
            "count": len(rows),
            "has_more": bool(result.get("has_more")),
            "page": _as_int(result.get("page"), page_number),
            "total": _as_int(result.get("total"), len(rows)),
            "message": "",
        }

    def bangumi(self) -> dict[str, Any]:
        """The anime the signed-in user follows (追番).

        An account that follows nothing gets an empty list and success: that
        is an answer, not a failure.
        """
        if not self._client.is_logged_in:
            return {"success": False, "code": CODE_NOT_LOGGED_IN,
                    "shows": [], "count": 0, "message": "没有登录 B站"}
        try:
            raw, _cached = self._cache.remember(
                "bangumi", "all", lambda: self._client.bangumi() or [],
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili bangumi failed: %s", exc)
            return {"success": False, "shows": [], "count": 0, "message": str(exc)}
        shows = [
            {
                "season_id": _as_int(item.get("season_id"), 0),
                "media_id": _as_int(item.get("media_id"), 0),
                "title": str(item.get("title") or ""),
                "cover": str(item.get("cover") or ""),
                "total": _as_int(item.get("total"), 0),
                "watched": _as_int(item.get("watched"), 0),
                "progress": str(item.get("progress") or ""),
                "progress_percent": _as_int(item.get("progress_percent"), 0),
                "ep_id": _as_int(item.get("ep_id"), 0),
                "ep_index": str(item.get("ep_index") or ""),
                "follow_status": _as_int(item.get("follow_status"), 0),
                "url": str(item.get("url") or ""),
            }
            for item in raw if isinstance(item, dict)
        ]
        return {"success": True, "shows": shows, "count": len(shows), "message": ""}

    def bangumi_streams(self, season_id: int = 0, episode_id: int = 0,
                        quality: int = 0) -> dict[str, Any]:
        """The playable streams of one bangumi episode.

        The shape is the same as :meth:`video_streams`, so the same player
        can show it.  What it cannot do is give away a member-only episode:
        B站 answers "大会员专享限制" and that sentence goes to the user as it
        came, because it is the truth and nothing here can change it.
        """
        season = _as_int(season_id, 0)
        if not season:
            return {"success": False, "message": "缺少番剧编号"}
        try:
            streams = self._client.bangumi_streams(
                season, _as_int(episode_id, 0), _as_int(quality, 0)) or {}
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili bangumi streams failed: %s", exc)
            return {"success": False, "message": str(exc)}
        if not streams.get("success"):
            return {"success": False, "message": str(streams.get("message") or "取播放地址失败")}
        return {
            "success": True,
            "season_id": season,
            "episode_id": _as_int(episode_id, 0),
            "duration": _as_int(streams.get("duration"), 0),
            "title": str(streams.get("title") or ""),
            "qualities": streams.get("qualities") or [],
            "audio": self._proxied(streams.get("audio")),
            "video": self._proxied(streams.get("video")),
            "muxed": self._proxied(streams.get("muxed")),
            "message": "",
        }

    def dynamics(self, mid: str = "", page: int = 1) -> dict[str, Any]:
        """The posts of one account (图文 / 视频 / 转发).

        Reading someone else's dynamics works without their permission as
        long as the account is public; a private one simply answers nothing.
        """
        author = str(mid or "").strip()
        if not author:
            return {"success": False, "items": [], "count": 0, "message": "缺少用户编号"}
        try:
            raw = self._client.dynamics(author, _as_int(page, 1)) or []
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili dynamics failed: %s", exc)
            return {"success": False, "items": [], "count": 0, "message": str(exc)}
        items = [
            {
                "id": str(item.get("id") or ""),
                "type": str(item.get("type") or "other"),
                "author_mid": str(item.get("author_mid") or ""),
                "author_name": str(item.get("author_name") or ""),
                "author_face": str(item.get("author_face") or ""),
                "pub_time": _as_int(item.get("pub_time"), 0),
                "pub_time_text": str(item.get("pub_time_text") or ""),
                "text": str(item.get("text") or ""),
                "title": str(item.get("title") or ""),
                "cover": str(item.get("cover") or ""),
                "bvid": str(item.get("bvid") or ""),
                "duration": str(item.get("duration") or ""),
                "images": [str(src) for src in (item.get("images") or []) if src],
                "url": str(item.get("url") or ""),
            }
            for item in raw if isinstance(item, dict)
        ]
        return {"success": True, "items": items, "count": len(items), "message": ""}

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
                # attr 的第 0 位是「私密」：默认收藏夹是 1，公开的是 22，
                # 私密的是 23（见 docs/bili-notes.md）。
                "attr": _as_int(item.get("attr"), 0),
                "private": bool(_as_int(item.get("attr"), 0) & 1),
                "intro": str(item.get("intro") or ""),
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
    def search_pgc(self, keyword: str = "", page: int = 1,
                   page_size: int = 20) -> dict[str, Any]:
        """Search the PGC catalogue (番剧 / 国创 / 影视), playable by season."""
        try:
            items = self._client.search_pgc(keyword, page=page, page_size=page_size)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili search_pgc failed: %s", exc)
            return {"success": False, "items": [], "count": 0,
                    "message": "搜索失败，请稍后再试"}
        rows = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("season_id"):
                continue
            rows.append({
                "kind": "pgc",
                "type": str(item.get("type") or "bangumi"),
                "season_id": _as_int(item.get("season_id"), 0),
                "media_id": _as_int(item.get("media_id"), 0),
                "title": str(item.get("title") or ""),
                "cover": str(item.get("cover") or ""),
                "url": str(item.get("url") or ""),
                "index_show": str(item.get("index_show") or ""),
                "season_type_name": str(item.get("season_type_name") or ""),
                "styles": list(item.get("styles") or []),
                "areas": list(item.get("areas") or []),
                "score": float(item.get("score") or 0),
                "is_follow": bool(item.get("is_follow")),
            })
        return {"success": True, "keyword": keyword, "items": rows, "count": len(rows)}

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
        backups = [str(item) for item in (stream.get("backup_urls") or [])
                   if isinstance(item, str) and item]
        proxied = url
        if self._media is not None:
            # The backups ride along, so the proxy can fall over to another
            # host without the player noticing a dead CDN node.
            proxied = self._media.url_for_bili(url, backups) or ""
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
        # The counters changed, so the cached ones are stale; and the video may
        # now be at the two-coin limit, which the interface has to know about.
        self._cache.discard("stat", str(bvid or ""))
        given = 0
        if result.get("success"):
            state = self._call_core("coins_given", self._client.coins_given, bvid)
            given = int(state.get("given") or 0)
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or ""),
                "balance": int(result.get("balance") or 0),
                "given": given}

    def coins_given(self, bvid: str) -> dict[str, Any]:
        """How many coins this account already gave the video (0-2)."""
        result = self._call_core("coins_given", self._client.coins_given, bvid)
        return {"success": bool(result.get("success")),
                "given": int(result.get("given") or 0),
                "message": str(result.get("message") or "")}

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

    def favorite_folders_state(self, bvid: str = "") -> dict[str, Any]:
        """The favourite folders, each flagged with "holds this video"."""
        try:
            folders = self._client.favorite_folders_with_state(bvid)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili favorite_folders_with_state failed: %s", exc)
            return {"success": False, "folders": [], "count": 0,
                    "message": "操作失败，请稍后再试"}
        rows = []
        for folder in folders or []:
            if not isinstance(folder, dict):
                continue
            rows.append({
                "id": str(folder.get("id") or ""),
                "title": str(folder.get("title") or ""),
                "media_count": _as_int(folder.get("media_count"), 0),
                "cover": str(folder.get("cover") or ""),
                "favoured": bool(folder.get("favoured")),
            })
        return {"success": True, "folders": rows, "count": len(rows)}

    def favorite_video_multi(self, bvid: str,
                             add_folder_ids: list[str] | None = None,
                             remove_folder_ids: list[str] | None = None) -> dict[str, Any]:
        """Put one video into several folders (and out of others) in one request."""
        result = self._call_core("favorite_video_multi",
                                 self._client.favorite_video_multi,
                                 bvid, add_folder_ids, remove_folder_ids)
        if result.get("success"):
            # 收藏夹里的内容变了：列表和条目都得重取，
            # 否则用户收藏完看到的还是收藏前的列表。
            self._cache.invalidate_folders()
            self._cache.discard("stat", str(bvid or ""))
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or ""),
                "folder_ids": list(result.get("folder_ids") or [])}

    def favorite_move(self, src_folder_id: str, target_folder_id: str,
                      aids: list[int] | None = None, copy: bool = False) -> dict[str, Any]:
        """Move (or copy) videos from one favourite folder into another."""
        result = self._call_core("favorite_move", self._client.favorite_move,
                                 src_folder_id, target_folder_id, aids, copy)
        if result.get("success"):
            self._cache.invalidate_folders()
        return {"success": bool(result.get("success")),
                "count": _as_int(result.get("count"), 0),
                "message": str(result.get("message") or "")}

    def favorite_batch_delete(self, folder_id: str,
                              aids: list[int] | None = None) -> dict[str, Any]:
        """Take several videos out of one favourite folder at once."""
        result = self._call_core("favorite_batch_delete",
                                 self._client.favorite_batch_delete,
                                 folder_id, aids)
        if result.get("success"):
            self._cache.invalidate_folders()
        return {"success": bool(result.get("success")),
                "count": _as_int(result.get("count"), 0),
                "message": str(result.get("message") or "")}

    def collection_videos(self, season_id: str = "", mid: str = "",
                          page: int = 1) -> dict[str, Any]:
        """One page of the videos inside a UGC collection (合集)."""
        result = self._call_core("collection_videos",
                                 self._client.collection_videos,
                                 season_id, mid, page)
        items = [
            track_payload(item) for item in (result.get("items") or [])
            if isinstance(item, dict) and item.get("bvid")
        ]
        return {
            "success": not result.get("error"),
            "message": str(result.get("error") or ""),
            "items": items,
            "tracks": items,
            "count": len(items),
            "has_more": bool(result.get("has_more")),
            "page": _as_int(result.get("page"), 1),
        }

    def bangumi_season(self, season_id: str = "") -> dict[str, Any]:
        """One bangumi season with its episode list (选集)."""
        result = self._call_core("bangumi_season", self._client.bangumi_season,
                                 season_id)
        episodes = []
        for item in result.get("episodes") or []:
            if not isinstance(item, dict):
                continue
            episodes.append({
                "ep_id": _as_int(item.get("ep_id"), 0),
                "aid": _as_int(item.get("aid"), 0),
                "cid": _as_int(item.get("cid"), 0),
                "title": str(item.get("title") or ""),
                "long_title": str(item.get("long_title") or ""),
                "cover": str(item.get("cover") or ""),
            })
        return {
            "success": bool(result.get("success")),
            "message": str(result.get("message") or ""),
            "title": str(result.get("title") or ""),
            "cover": str(result.get("cover") or ""),
            "episodes": episodes,
            "count": len(episodes),
        }

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
                     parent_rpid: int = 0, root_rpid: int = 0) -> dict[str, Any]:
        """Write a comment (or a reply to one)."""
        result = self._call_core("post_comment", self._client.post_comment,
                                 bvid, content, parent_rpid, root_rpid)
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

    def get_comment_replies(self, bvid: str, root_rpid: str,
                           page: int = 1) -> dict[str, Any]:
        """Load sub-replies of a comment (楼中楼)."""
        cache_key = f"replies|{root_rpid}|{page}"
        result, _from_disk = self._cache.remember(
            "comment_replies", cache_key,
            lambda: self._call_core("get_comment_replies",
                                    self._client.get_comment_replies,
                                    bvid, root_rpid, page),
        )
        return {
            "success": bool(result.get("success")),
            "comments": result.get("comments") or [],
            "count": int(result.get("count") or 0),
            "has_more": bool(result.get("has_more")),
            "message": str(result.get("message") or ""),
        }

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

    # ------------------------------------------------------------ 推荐
    def recommend(self, page_size: int = 12, fresh_index: int = 1,
                  scroll: bool = False) -> dict[str, Any]:
        """The home feed, as B站 thinks it should look for this account.

        ``scroll`` asks for the next page of the feed instead of a fresh
        home page: B站 pulls the same route with a growing fresh index, and
        without that the list can never grow past the first batch.
        """
        try:
            rows = self._client.recommend(page_size=page_size,
                                           fresh_index=fresh_index,
                                           scroll=scroll) or []
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili recommend failed: %s", exc)
            return {"success": False, "items": [], "count": 0,
                    "message": "推荐加载失败，请稍后重试"}
        items = [track_payload(item) for item in rows if isinstance(item, dict)]
        return {"success": True, "items": items, "count": len(items), "message": ""}

    def report_playback(self, bvid: str, cid: int = 0, played_time: int = 0,
                        video_duration: int = 0, quality: int = 0,
                        start_ts: int = 0, session: str = "",
                        play_type: int = 0) -> dict[str, Any]:
        """Tell B站 what is being watched -- only when the user asked for it."""
        from src.config.settings import get_settings

        if not get_settings().bili.report_playback:
            return {"success": False, "message": "", "skipped": True}
        if not str(bvid or "").strip():
            return {"success": False, "message": "缺少视频号"}
        try:
            result = self._client.report_playback(
                bvid, cid=cid, played_time=played_time,
                video_duration=video_duration, quality=quality,
                start_ts=start_ts, session=session, play_type=play_type) or {}
        except Exception as exc:  # noqa: BLE001 - the player must not break
            log.warning("bilibili playback report failed: %s", exc)
            return {"success": False, "message": str(exc)}
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

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

    # ------------------------------------------------------------------ Folder management
    def create_favorite_folder(self, title: str, intro: str = "",
                              privacy: int = 0) -> dict[str, Any]:
        """Create a new favourite folder."""
        result = self._call_core("create_favorite_folder",
                                 self._client.create_favorite_folder,
                                 title, intro, privacy)
        if result.get("success"):
            self._cache.invalidate_folders()
        return {"success": bool(result.get("success")),
                "id": str(result.get("id") or ""),
                "message": str(result.get("message") or "")}

    def edit_favorite_folder(self, folder_id: str, title: str = "",
                            intro: str = "", privacy: int | None = None) -> dict[str, Any]:
        """Rename a folder, change its description or privacy.

        ``privacy`` of ``None`` means "leave it as it is".
        """
        result = self._call_core("edit_favorite_folder",
                                 self._client.edit_favorite_folder,
                                 folder_id, title, intro, privacy)
        if result.get("success"):
            self._cache.invalidate_folders()
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    def delete_favorite_folder(self, folder_id: str) -> dict[str, Any]:
        """Delete a favourite folder."""
        result = self._call_core("delete_favorite_folder",
                                 self._client.delete_favorite_folder,
                                 folder_id)
        if result.get("success"):
            self._cache.invalidate_folders()
        return {"success": bool(result.get("success")),
                "message": str(result.get("message") or "")}

    # ------------------------------------------------------------------ Follow / Followers
    def get_followings(self, mid: str = "", page: int = 1,
                       page_size: int = 50) -> dict[str, Any]:
        """Get the list of users the account follows (关注列表)."""
        result = self._call_core("get_followings", self._client.get_followings,
                                 str(mid or ""), page, page_size)
        return {"success": bool(result.get("success")),
                "items": result.get("items") or [],
                "count": int(result.get("count") or 0),
                "has_more": bool(result.get("has_more")),
                "message": str(result.get("message") or "")}

    def get_followers(self, mid: str = "", page: int = 1,
                      page_size: int = 50) -> dict[str, Any]:
        """Get the list of users following the account (粉丝列表)."""
        result = self._call_core("get_followers", self._client.get_followers,
                                 str(mid or ""), page, page_size)
        return {"success": bool(result.get("success")),
                "items": result.get("items") or [],
                "count": int(result.get("count") or 0),
                "has_more": bool(result.get("has_more")),
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
        from src.core.bili_download import DEFAULT_INTERVAL, BiliDownloader

        items = [track for track in (tracks or []) if isinstance(track, dict)]
        if not items:
            return {"success": False, "count": 0, "message": "没有要下载的内容"}
        options = options or {}
        download_dir = str(options.get("download_dir") or "")
        merge_audio = options.get("merge_audio")
        if not download_dir or merge_audio is None:
            from src.config.settings import get_settings

            settings = get_settings()
            if not download_dir:
                # B站在设置里有自己的目录；没设过就和网易云共用一个。
                download_dir = settings.bili.download_dir or settings.download.download_dir
            if merge_audio is None:
                merge_audio = settings.bili.merge_audio
        downloader = BiliDownloader(
            download_dir,
            self._client,
            quality=_as_int(options.get("quality"), 0),
            overwrite=bool(options.get("overwrite")),
            max_concurrent=_as_int(options.get("max_concurrent"), 2),
            merge_audio=bool(merge_audio),
            interval=float(options.get("interval") or DEFAULT_INTERVAL),
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

