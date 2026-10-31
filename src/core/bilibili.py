"""Bilibili (哔哩哔哩) music source client.

The module talks to the public web API of ``api.bilibili.com`` and is read-only:
every request is a GET, nothing likes, follows, comments or uploads anything.

Two hidden contracts drive the implementation (see ``API.md`` §4 and §5):

* **WBI signature** -- paths containing ``/wbi/`` must carry ``w_rid`` + ``wts``.
  The key pair is fetched at runtime from ``/x/web-interface/nav`` because the
  server rotates it; the hardcoded pair is only a fallback.
* **Device identity** -- ``buvid3`` / ``buvid4`` from ``/x/frontend/finger/spi``.
  They are fetched once and then reused, because every extra call is another
  chance to trip the risk-control layer.

The client never raises at the caller: the UI above it would turn an exception
into a spinner that never stops.  List endpoints answer ``[]`` and
:meth:`BiliClient.audio_url` answers a failure dictionary instead.
"""
from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse
from typing import Any

import requests

__all__ = [
    "BiliClient",
    "BILI_RIDS",
    "BILI_RID_NAMES",
    "BILI_REFERER",
    "BILI_UA",
    "BILI_API_BASE",
    "MIXIN_PERMUTE",
]

log = logging.getLogger(__name__)

#: API host.  Only read-only endpoints are used.
BILI_API_BASE = "https://api.bilibili.com"

#: The CDN refuses audio requests without a matching Referer.
BILI_REFERER = "https://www.bilibili.com"

#: A desktop browser UA; the API answers 412 to obvious bots.
BILI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: Partition ids accepted by :meth:`BiliClient.search` and friends.
BILI_RIDS: dict[str, int] = {
    "all": 0,
    "animation": 1,
    "bangumi": 2,
    "music": 28,
    "music_video": 29,
    "douga": 24,
    "knowledge": 36,
    "game": 4,
    "life": 160,
    "film": 3,
    "documentary": 33,
}

#: Human readable partition names, used when the API omits ``tname``.
BILI_RID_NAMES: dict[int, str] = {
    0: "全部",
    1: "动画",
    2: "番剧",
    3: "电影",
    4: "游戏",
    24: "MAD·AMV",
    28: "音乐",
    29: "音乐视频",
    33: "纪录片",
    36: "知识",
    160: "生活",
}

#: Search keyword used when a partition has no ranking board of its own.
#: ``ranking/v2`` only serves the main partitions; the music partitions answer
#: ``-400``/``-352`` there, so the fallback searches inside the partition.
BILI_RID_KEYWORDS: dict[int, str] = {
    2: "番剧",
    24: "MAD",
    28: "音乐",
    29: "音乐视频",
    33: "纪录片",
}

#: Sub-partition names that belong to the music areas.  The hot list is not
#: filtered server side, so the music entries have to be recognised by name.
_MUSIC_NAMES = (
    "音乐",
    "翻唱",
    "演奏",
    "电音",
    "古风",
    "钢琴",
    "吉他",
    "MV",
    "VOCALOID",
    "虚拟歌手",
    "乐评",
    "原创音乐",
    "音乐综合",
    "音乐现场",
    "音乐可视化",
    "音乐教学",
)

#: Partitions that belong to the music area.  ``search`` filters them with
#: ``tids``; the ranking board does not serve them at all.
BILI_MUSIC_RIDS = frozenset({28, 29})

#: Reverse map of :data:`BILI_RIDS` so a partition id can be handed to
#: :meth:`BiliClient.search` as the ``category`` argument.
_RID_CATEGORY = {rid: key for key, rid in BILI_RIDS.items()}

#: Official 64-slot permutation table (``API.md`` §5.1).
MIXIN_PERMUTE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

#: Snapshot from the web homepage bundle, used only when ``/nav`` is unreachable.
FALLBACK_IMG_KEY = "c6618eced9b64a019d0e6247aed7a38e"
FALLBACK_SUB_KEY = "6bb3adc89c244ccda6d9abf22fc6f910"

#: ``encodeURIComponent`` leaves these five alone, the server strips them from
#: the values before signing -- so we have to strip them too.
_STRIP_CHARS = str.maketrans("", "", "!'()*")

#: ``fnval`` asking for DASH (16) plus the extra formats the API may attach.
_FNVAL_DASH = 4048


def mixin_key(img_key: str, sub_key: str) -> str:
    """Return the 32 character WBI mixin key for an image/sub key pair."""
    raw = img_key + sub_key
    if len(raw) < max(MIXIN_PERMUTE) + 1:
        return ""
    return "".join(raw[i] for i in MIXIN_PERMUTE)[:32]


def _encode_value(value: Any) -> str:
    """Encode one parameter value the way ``encodeURIComponent`` does."""
    text = "" if value is None else str(value)
    text = text.translate(_STRIP_CHARS)
    return urllib.parse.quote(text, safe="")


def sign_params(params: dict[str, Any], key: str, wts: int | None = None) -> dict[str, Any]:
    """Add ``wts`` and ``w_rid`` to ``params`` and return a new dictionary.

    The server sorts the query by key name, strips ``!'()*`` from every value and
    hashes the result together with the mixin key -- any deviation produces
    ``-403``.
    """
    signed = dict(params)
    signed["wts"] = int(time.time()) if wts is None else int(wts)
    query = "&".join(
        f"{name}={_encode_value(signed[name])}" for name in sorted(signed)
    )
    signed["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    return signed


def _strip_html(text: str) -> str:
    """Remove the ``<em class="keyword">`` markers the search endpoint adds."""
    if "<" not in text:
        return text
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out).strip()


def _duration_seconds(value: Any) -> int:
    """Normalise ``294``, ``"4:54"`` and ``"1:02:03"`` into seconds."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    total = 0
    for number in numbers:
        total = total * 60 + number
    return total


def _absolute_url(url: Any) -> str:
    """Turn ``//i0.hdslb.com/x.jpg`` and ``http://`` into an ``https://`` URL."""
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text


class BiliClient:
    """Read-only Bilibili client for the music source.

    ``session`` may be injected to fake the transport in tests; anything with a
    ``requests``-like ``get()`` works.
    """

    def __init__(
        self,
        session: Any | None = None,
        cookie: str = "",
        timeout: tuple[int, int] = (10, 30),
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._cookie: dict[str, str] = {}
        self._mixin: str = ""
        self._buvid_ready = False
        self.set_cookie(cookie)

    # ------------------------------------------------------------------
    # session / identity
    # ------------------------------------------------------------------
    def set_cookie(self, cookie: str) -> None:
        """Store a raw cookie string such as ``"SESSDATA=...; bili_jct=..."``."""
        parsed: dict[str, str] = {}
        for part in str(cookie or "").split(";"):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            if name:
                parsed[name] = value.strip()
        self._cookie = parsed
        for name, value in parsed.items():
            self._apply_cookie(name, value)

    @property
    def is_logged_in(self) -> bool:
        """True when the credential carries a ``SESSDATA`` cookie."""
        return bool(self._cookie.get("SESSDATA"))

    def _apply_cookie(self, name: str, value: str) -> None:
        """Push one cookie into the session jar, if the session has one."""
        jar = getattr(self._session, "cookies", None)
        if jar is None or not hasattr(jar, "set"):
            return
        try:
            jar.set(name, value, domain=".bilibili.com")
        except TypeError:  # a jar that does not accept a domain
            jar.set(name, value)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": BILI_UA,
            "Referer": BILI_REFERER,
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }

    # ------------------------------------------------------------------
    # low level transport
    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Perform one GET and return the parsed envelope, or ``None`` on failure."""
        url = path if path.startswith("http") else BILI_API_BASE + path
        try:
            response = self._session.get(
                url,
                params=params or {},
                headers=self._headers(),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see an exception
            log.warning("bilibili request failed: %s (%s)", path, exc)
            return None
        status = getattr(response, "status_code", 200)
        if status != 200:
            log.warning("bilibili request %s answered HTTP %s", path, status)
            return None
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili response %s is not JSON: %s", path, exc)
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _ensure_buvid(self) -> None:
        """Fetch ``buvid3`` / ``buvid4`` once and keep them for later requests."""
        if self._buvid_ready:
            return
        self._buvid_ready = True  # never retry in a loop, even on failure
        payload = self._get("/x/frontend/finger/spi")
        if not payload or payload.get("code") != 0:
            return
        data = payload.get("data") or {}
        for name, key in (("buvid3", "b_3"), ("buvid4", "b_4")):
            value = data.get(key)
            if value:
                self._apply_cookie(name, str(value))

    def _ensure_mixin_key(self) -> str:
        """Return the WBI mixin key, fetching it from ``/nav`` on first use."""
        if self._mixin:
            return self._mixin
        img_key, sub_key = self._nav_keys()
        key = mixin_key(img_key, sub_key) if img_key and sub_key else ""
        if not key:
            key = mixin_key(FALLBACK_IMG_KEY, FALLBACK_SUB_KEY)
            log.warning("bilibili wbi keys unavailable, using the bundled fallback")
        self._mixin = key
        return key

    def _nav_keys(self) -> tuple[str, str]:
        """Ask ``/nav`` for the current key pair; empty strings on failure."""
        payload = self._get("/x/web-interface/nav")
        if not payload:
            return "", ""
        data = payload.get("data") or {}
        wbi = data.get("wbi_img") or {}
        img_url = str(wbi.get("img_url") or "")
        sub_url = str(wbi.get("sub_url") or "")
        return _key_from_url(img_url), _key_from_url(sub_url)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def nav(self) -> dict[str, Any]:
        """Return ``{"code": int, "wbi_img": {...}}`` for the current session."""
        payload = self._get("/x/web-interface/nav")
        if not payload:
            return {"code": -1, "wbi_img": {"img_url": "", "sub_url": ""}}
        data = payload.get("data") or {}
        wbi = data.get("wbi_img") or {}
        img_url = str(wbi.get("img_url") or "")
        sub_url = str(wbi.get("sub_url") or "")
        if img_url and sub_url:
            key = mixin_key(_key_from_url(img_url), _key_from_url(sub_url))
            if key:
                self._mixin = key
        return {
            "code": int(payload.get("code", -1)),
            "wbi_img": {"img_url": img_url, "sub_url": sub_url},
        }

    def search(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 30,
        order: str = "totalrank",
        category: str = "all",
    ) -> list[dict[str, Any]]:
        """Search videos; returns normalised entries (``[]`` on failure)."""
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        self._ensure_buvid()
        params: dict[str, Any] = {
            "search_type": "video",
            "keyword": keyword,
            "page": max(1, int(page)),
            "page_size": max(1, min(int(page_size), 50)),
            "order": order or "totalrank",
        }
        rid = BILI_RIDS.get(category, 0)
        if rid:
            # tid 0 means "no filter", so it must not be sent at all.
            params["tids"] = rid
        params = sign_params(params, self._ensure_mixin_key())
        payload = self._get("/x/web-interface/wbi/search/type", params)
        if not payload or payload.get("code") != 0:
            return []
        data = payload.get("data") or {}
        result = data.get("result")
        if not isinstance(result, list):
            return []
        items = [self._from_search_item(item) for item in result if isinstance(item, dict)]
        return [item for item in items if item.get("bvid")]

    def video(self, bvid: str) -> dict[str, Any]:
        """Return the details of one video, or ``{}`` when it is unavailable."""
        bvid = str(bvid or "").strip()
        if not bvid:
            return {}
        self._ensure_buvid()
        payload = self._get("/x/web-interface/view", {"bvid": bvid})
        if not payload or payload.get("code") != 0:
            return {}
        data = payload.get("data")
        if not isinstance(data, dict):
            return {}
        item = self._from_view(data)
        return item if item.get("bvid") else {}

    def audio_url(self, bvid: str, quality: int = 0) -> dict[str, Any]:
        """Resolve the audio stream of a video.

        Never raises: on failure it returns ``success=False`` with an empty
        ``url`` so the caller can show a message instead of a broken spinner.
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return _failure("invalid_bvid", "missing bvid")
        self._ensure_buvid()
        cid = 0
        view = self._get("/x/web-interface/view", {"bvid": bvid})
        if view and view.get("code") == 0:
            data = view.get("data") or {}
            cid = int(data.get("cid") or 0)
        if not cid:
            return _failure("no_cid", "could not resolve cid")

        params = sign_params(
            {
                "bvid": bvid,
                "cid": cid,
                "qn": int(quality or 0),
                "fnval": _FNVAL_DASH,
                "fnver": 0,
                "fourk": 1,
                "platform": "pc",
            },
            self._ensure_mixin_key(),
        )
        payload = self._get("/x/player/wbi/playurl", params)
        if not payload:
            return _failure("network_error", "playurl request failed")
        if payload.get("code") != 0:
            return _failure(
                "api_error",
                str(payload.get("message") or payload.get("code")),
            )
        data = payload.get("data") or {}

        dash = data.get("dash") or {}
        audio = dash.get("audio") or []
        best: dict[str, Any] | None = None
        for entry in audio:
            if not isinstance(entry, dict):
                continue
            if best is None or int(entry.get("bandwidth") or 0) > int(best.get("bandwidth") or 0):
                best = entry
        if best is not None:
            url = str(best.get("baseUrl") or best.get("base_url") or "")
            if url:
                return {
                    "success": True,
                    "url": url,
                    "mime": str(best.get("mimeType") or best.get("mime_type") or "audio/mp4"),
                    "quality": int(best.get("id") or 0),
                    "need_referer": True,
                    "error_key": "",
                    "message": "",
                }

        durl = data.get("durl") or []
        if durl and isinstance(durl[0], dict):
            url = _absolute_url(durl[0].get("url"))
            if url:
                return {
                    "success": True,
                    "url": url,
                    "mime": "audio/mp4",
                    "quality": int(data.get("quality") or 0),
                    "need_referer": True,
                    "error_key": "",
                    "message": "",
                }
        return _failure("no_audio", "no audio stream in the answer")

    def popular(self, rid: int = 28, page: int = 1, page_size: int = 30) -> list[dict[str, Any]]:
        """Return the hot list of one partition.

        ``/x/web-interface/popular`` is the site wide hot list and ignores the
        partition, so a partition other than "all" is served by a partition
        scoped search ordered by play count.
        """
        rid = int(rid or 0)
        if rid == 0:
            self._ensure_buvid()
            params = {"pn": max(1, int(page)), "ps": max(1, min(int(page_size), 50))}
            payload = self._get("/x/web-interface/popular", params)
            if not payload or payload.get("code") != 0:
                return []
            data = payload.get("data") or {}
            result = data.get("list")
            if not isinstance(result, list):
                return []
            items = [
                self._from_archive_item(item, rid)
                for item in result
                if isinstance(item, dict)
            ]
            return [item for item in items if item.get("bvid")]
        return self._partition_feed(rid, page, page_size)

    def ranking(self, rid: int = 28) -> list[dict[str, Any]]:
        """Return the ranking of one partition.  The partition id is mandatory.

        The board only serves the main partitions; the music areas answer
        ``-400``/``-352`` there, so those fall back to the partition feed.
        """
        rid = int(rid or 0)
        self._ensure_buvid()
        payload = self._get("/x/web-interface/ranking/v2", {"rid": rid})
        if payload and payload.get("code") == 0:
            data = payload.get("data") or {}
            result = data.get("list")
            if isinstance(result, list):
                items = [
                    self._from_archive_item(item, rid)
                    for item in result
                    if isinstance(item, dict)
                ]
                found = [item for item in items if item.get("bvid")]
                if found:
                    return found
        return self._partition_feed(rid, 1, 30)

    def _partition_feed(
        self,
        rid: int,
        page: int = 1,
        page_size: int = 30,
        order: str = "click",
    ) -> list[dict[str, Any]]:
        """Hot videos of a partition, served by a partition scoped search."""
        keyword = BILI_RID_KEYWORDS.get(rid) or BILI_RID_NAMES.get(rid) or ""
        if not keyword:
            return []
        return self.search(
            keyword,
            page=page,
            page_size=page_size,
            order=order,
            category=_RID_CATEGORY.get(rid, "all"),
        )

    # ------------------------------------------------------------------
    # normalisation
    # ------------------------------------------------------------------
    def _base_item(self, bvid: str, aid: Any, title: str) -> dict[str, Any]:
        return {
            "source": "bilibili",
            "id": bvid,
            "bvid": bvid,
            "aid": int(aid or 0),
            "title": title,
            "author": "",
            "cover": "",
            "duration": 0,
            "play_count": 0,
            "category": "",
            "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        }

    def _from_search_item(self, item: dict[str, Any]) -> dict[str, Any]:
        bvid = str(item.get("bvid") or "")
        title = _strip_html(str(item.get("title") or ""))
        out = self._base_item(bvid, item.get("aid"), title)
        out["author"] = str(item.get("author") or item.get("uname") or "")
        out["cover"] = _absolute_url(item.get("pic") or item.get("cover"))
        out["duration"] = _duration_seconds(item.get("duration"))
        out["play_count"] = int(item.get("play") or 0)
        out["category"] = self._category_name(
            item.get("typename") or item.get("tname") or item.get("cate_name"),
            item.get("typeid") or item.get("tid"),
        )
        return out

    def _from_view(self, data: dict[str, Any]) -> dict[str, Any]:
        bvid = str(data.get("bvid") or "")
        out = self._base_item(bvid, data.get("aid"), _strip_html(str(data.get("title") or "")))
        owner = data.get("owner") or {}
        out["author"] = str(owner.get("name") or "")
        out["cover"] = _absolute_url(data.get("pic"))
        out["duration"] = _duration_seconds(data.get("duration"))
        stat = data.get("stat") or {}
        out["play_count"] = int(stat.get("view") or 0)
        out["category"] = self._category_name(
            data.get("tname") or data.get("tname_v2"), data.get("tid")
        )
        out["cid"] = int(data.get("cid") or 0)
        out["pubdate"] = int(data.get("pubdate") or 0)
        out["description"] = _strip_html(str(data.get("desc") or ""))
        return out

    def _from_archive_item(self, item: dict[str, Any], fallback_rid: int) -> dict[str, Any]:
        bvid = str(item.get("bvid") or "")
        out = self._base_item(bvid, item.get("aid"), _strip_html(str(item.get("title") or "")))
        owner = item.get("owner") or {}
        out["author"] = str(owner.get("name") or item.get("author") or item.get("uname") or "")
        out["cover"] = _absolute_url(item.get("pic"))
        out["duration"] = _duration_seconds(item.get("duration"))
        stat = item.get("stat") or {}
        out["play_count"] = int(stat.get("view") or item.get("play") or 0)
        out["category"] = self._category_name(
            item.get("tname") or item.get("typename"),
            item.get("tid") or fallback_rid,
        )
        return out

    @staticmethod
    def _category_name(name: Any, tid: Any) -> str:
        text = str(name or "").strip()
        if text:
            return text
        try:
            return BILI_RID_NAMES.get(int(tid), "")
        except (TypeError, ValueError):
            return ""


def _key_from_url(url: str) -> str:
    """``https://i0.hdslb.com/bfs/wbi/<key>.png`` -> ``<key>``."""
    if not url:
        return ""
    tail = url.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0]


def _failure(error_key: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "url": "",
        "mime": "",
        "quality": 0,
        "need_referer": True,
        "error_key": error_key,
        "message": message,
    }
