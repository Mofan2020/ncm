"""Bilibili (哔哩哔哩) music source client.

The module talks to the public web API of ``api.bilibili.com`` and to the
login endpoints of ``passport.bilibili.com``.

The music part is read-only: every request of it is a GET, nothing likes,
follows, comments or uploads anything.  The account part only ever touches
the endpoints that carry the credential in and out -- the login, the
registration and the logout.

Two hidden contracts drive the implementation (see ``API.md`` §4 and §5):

* **WBI signature** -- paths containing ``/wbi/`` must carry ``w_rid`` + ``wts``.
  The key pair is fetched at runtime from ``/x/web-interface/nav`` because the
  server rotates it; the hardcoded pair is only a fallback.
* **Device identity** -- ``buvid3`` / ``buvid4`` from ``/x/frontend/finger/spi``.
  They are fetched once and then reused, because every extra call is another
  chance to trip the risk-control layer.

The ``passport`` endpoints must not carry a WBI signature -- that host does
not know the key pair -- but they do need the UA, the Referer and the Origin,
and the session cookie arrives in the ``Set-Cookie`` header of the answer,
never in the JSON body.

The client never raises at the caller: the UI above it would turn an exception
into a spinner that never stops.  List endpoints answer ``[]`` and
:meth:`BiliClient.audio_url` answers a failure dictionary instead.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import urllib.parse
import uuid
from typing import Any

import requests

from src.core.bili_app_login import (
    app_login_params,
    app_sms_login_params,
    app_sms_params,
)
from src.core.bili_rsa import bili_password_cipher

__all__ = [
    "BiliClient",
    "BILI_RIDS",
    "BILI_RID_NAMES",
    "BILI_REFERER",
    "BILI_UA",
    "BILI_API_BASE",
    "BILI_PASSPORT_BASE",
    "BILI_QUALITY_NAMES",
    "MIXIN_PERMUTE",
    "video_quality_name",
]

log = logging.getLogger(__name__)

#: API host.  Only read-only endpoints are used.
BILI_API_BASE = "https://api.bilibili.com"

#: Login and account endpoints live on their own host.  They must not carry
#: a WBI signature -- ``passport`` does not know the key pair and answers
#: ``-403`` when a stray ``w_rid`` shows up.
BILI_PASSPORT_BASE = "https://passport.bilibili.com"

#: The registration SMS route wants a fixed ``key`` next to the geetest
#: parameters.  It is the key the registration page of the website sends;
#: the route answers ``20000``（该版本已不支持当前功能）for a third party
#: client no matter what the key is.
REGISTER_SMS_KEY = "6eeb28e7bbd64b389da2be3a2778c7e3"

#: The order in which the cookies are written back to the caller.  The
#: session credential comes first because the layer above stores the string
#: as it is and re-reads it after a restart.
_COOKIE_ORDER = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
    "b_nut",
    "b_lsid",
)

#: Attributes that may follow a cookie inside a ``Set-Cookie`` header.  They
#: are not cookies and must not end up in the credential string.
_COOKIE_ATTRS = frozenset({
    "path",
    "domain",
    "expires",
    "max-age",
    "httponly",
    "secure",
    "samesite",
    "priority",
    "partitioned",
    "version",
})

#: ``Set-Cookie`` may carry several cookies separated by a comma; the comma
#: inside an ``Expires`` date must not be treated as a separator.
_SET_COOKIE_SPLIT = re.compile(r",\s*(?=[A-Za-z0-9_\-]+=)")

#: Quality code -> readable name.  The server sends its own wording in
#: ``support_formats``; this map is the fallback for the codes it omits.
BILI_QUALITY_NAMES: dict[int, str] = {
    16: "360P 流畅",
    32: "480P 清晰",
    64: "720P 高清",
    80: "1080P 高清",
    112: "1080P 高码率",
    116: "1080P 60帧",
    120: "4K 超清",
    125: "HDR 真彩",
    126: "杜比视界",
    127: "8K 超高清",
}

#: QR login status codes (they live in ``data.code``, not in ``code``).
_QR_STATUS: dict[int, str] = {
    0: "success",
    86101: "waiting",
    86090: "scanned",
    86038: "expired",
}

_QR_MESSAGES: dict[str, str] = {
    "waiting": "等待扫码，请在手机上打开哔哩哔哩",
    "scanned": "已扫码，请在手机上确认登录",
    "expired": "二维码已过期，请重新获取",
}

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


def video_quality_name(qn: int) -> str:
    """Return the readable name of a quality code (``80`` -> ``"1080P 高清"``)."""
    try:
        code = int(qn)
    except (TypeError, ValueError):
        return ""
    return BILI_QUALITY_NAMES.get(code, f"{code}P")


def _parse_set_cookie(raw: str) -> dict[str, str]:
    """Parse the cookies out of a ``Set-Cookie`` header value.

    The QR login hands the session out through this header, never through
    the JSON body, so the credential would be empty without this parser.
    """
    found: dict[str, str] = {}
    for part in _SET_COOKIE_SPLIT.split(raw or ""):
        name, sep, rest = part.partition("=")
        if not sep:
            continue
        name = name.strip()
        if not name or name.lower() in _COOKIE_ATTRS:
            continue
        found[name] = rest.split(";")[0].strip()
    return found


def _fav_duration(value: Any) -> int:
    """The favourite folder answers the duration in milliseconds."""
    seconds = _duration_seconds(value)
    if seconds > 1000:
        return seconds // 1000
    return seconds


def _needs_captcha(data: dict[str, Any], code: int) -> bool:
    """Tell whether the server wants a captcha before it lets the login pass.

    The codes are the ones the web login answers with; the url of the
    answer carries the captcha page and is the most reliable signal.
    """
    if "captcha" in str(data.get("url") or "").lower():
        return True
    if data.get("captcha_key"):
        return True
    return code in (-120, -125, -352, -401)


def _captcha_key(data: dict[str, Any]) -> str:
    """Read the captcha key out of a login answer."""
    key = str(data.get("captcha_key") or "")
    if key:
        return key
    url = str(data.get("url") or "")
    if not url:
        return ""
    query = urllib.parse.urlparse(url).query
    for name, value in urllib.parse.parse_qsl(query):
        if name == "captcha_key":
            return value
    return ""


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
    if isinstance(value, int | float):
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


#: How fast each kind of CDN host actually is, measured on one machine on
#: 2026-11-01 while fetching 6 MB of real video (kB/s):
#:
#:   upos-sz-estgoss.bilivideo.com          8200
#:   upos-sz-mirrorhwo1.bilivideo.com       1100
#:   upos-sz-estghw.bilivideo.com           1000
#:   *.edge.mountaintoys.cn:4483             330
#:   *.mcdn.bilivideo.cn:8082        210, and two read timeouts
#:
#: B站 tends to put the ``mcdn`` node first in ``baseUrl``, which is the
#: difference between a video that starts at once and one that stares at a
#: spinner.  The rank only decides the order; every candidate is kept, and the
#: proxy fails over to the next one when a host does not answer.
_BILI_HOST_RANKS = (
    ("mcdn.bilivideo", 3),
    ("mountaintoys", 2),
    ("upos-", 0),
)


def _bili_host_rank(url: str) -> int:
    """Lower is better: the order the CDN hosts are tried in."""
    try:
        host = (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return 4
    for marker, rank in _BILI_HOST_RANKS:
        if marker in host:
            return rank
    return 1


def _ordered_bili_urls(urls: Any) -> list[str]:
    """The same file on several hosts, fastest host first and deduplicated."""
    seen: list[str] = []
    for item in urls or []:
        text = str(item or "")
        if text and text not in seen:
            seen.append(text)
    return sorted(seen, key=_bili_host_rank)


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
        """Store a raw cookie string such as ``"SESSDATA=...; bili_jct=..."``.

        The credential is replaced as a whole: a name that is gone from the
        new string is also dropped from the session jar, otherwise a logout
        would keep the old session alive on the server.
        """
        previous = dict(self._cookie)
        parsed: dict[str, str] = {}
        for part in str(cookie or "").split(";"):
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            if name:
                parsed[name] = value.strip()
        self._cookie = parsed
        for name in previous:
            if name not in parsed:
                self._forget_cookie(name)
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

    def _forget_cookie(self, name: str) -> None:
        """Remove one cookie from the session jar, if the jar supports it."""
        jar = getattr(self._session, "cookies", None)
        if jar is None:
            return
        clear = getattr(jar, "clear", None)
        if callable(clear):
            for kwargs in ({"domain": ".bilibili.com", "name": name}, {"name": name}):
                try:
                    clear(**kwargs)
                    break
                except TypeError:
                    continue
                except Exception:  # noqa: BLE001
                    break
        try:
            del jar[name]
        except Exception:  # noqa: BLE001
            pass

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
    def _get_response(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform one GET and return the raw response (``None`` on failure).

        The login flow needs the response object itself because the session
        cookie arrives in the ``Set-Cookie`` header and not in the body.
        """
        url = path if path.startswith("http") else BILI_API_BASE + path
        try:
            return self._session.get(
                url,
                params=params or {},
                headers=headers or self._headers(),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see an exception
            log.warning("bilibili request failed: %s (%s)", path, exc)
            return None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Perform one GET and return the parsed envelope, or ``None`` on failure."""
        response = self._get_response(path, params)
        if response is None:
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

    def _passport_request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> tuple[dict[str, Any] | None, Any]:
        """Perform one request against ``passport.bilibili.com``.

        Returns ``(payload, response)``.  The payload is ``None`` when the
        answer was not a JSON object; the response is ``None`` when the
        request never left the machine.  Login requests carry no WBI
        signature -- ``passport`` would answer ``-403`` -- but they do carry
        the UA, the Referer and the Origin.
        """
        url = path if path.startswith("http") else BILI_PASSPORT_BASE + path
        headers = self._headers()
        try:
            if method.upper() == "POST":
                response = self._session.post(
                    url,
                    data=params or {},
                    headers=headers,
                    timeout=self._timeout,
                )
            else:
                response = self._session.get(
                    url,
                    params=params or {},
                    headers=headers,
                    timeout=self._timeout,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili passport request failed: %s (%s)", path, exc)
            return None, None
        payload: dict[str, Any] | None = None
        try:
            body = response.json()
            if isinstance(body, dict):
                payload = body
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili passport response %s is not JSON: %s", path, exc)
        return payload, response

    def _response_cookies(self, response: Any) -> dict[str, str]:
        """Collect the cookies the server attached to a response."""
        if response is None:
            return {}
        found: dict[str, str] = {}
        jar = getattr(response, "cookies", None)
        if isinstance(jar, dict):
            found.update({str(name): str(value) for name, value in jar.items()})
        elif jar is not None:
            try:
                for item in jar:
                    name = getattr(item, "name", None)
                    if name:
                        found[str(name)] = str(getattr(item, "value", ""))
            except TypeError:
                pass
            try:
                for name in jar.keys():
                    found[str(name)] = str(jar.get(name))
            except Exception:  # noqa: BLE001
                pass
        headers = getattr(response, "headers", None) or {}
        try:
            raw = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
        except Exception:  # noqa: BLE001
            raw = ""
        found.update(_parse_set_cookie(str(raw)))
        return found

    def _cookie_string(self, cookies: dict[str, str]) -> str:
        """Render a cookie dictionary as a ``Cookie:`` header value."""
        if not cookies:
            return ""
        names = [name for name in _COOKIE_ORDER if name in cookies]
        names += [name for name in cookies if name not in _COOKIE_ORDER]
        return "; ".join(f"{name}={cookies[name]}" for name in names)

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

    # ------------------------------------------------------------------
    # account state
    # ------------------------------------------------------------------
    def cookie_dict(self) -> dict[str, str]:
        """Return the stored cookies as a dictionary."""
        return dict(self._cookie)

    def cookie_header(self) -> str:
        """Return the cookies as a ``Cookie:`` header value."""
        return self._cookie_string(dict(self._cookie))

    def csrf(self) -> str:
        """Return the CSRF token (``bili_jct``); empty when not signed in."""
        return str(self._cookie.get("bili_jct") or "")

    def bili_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return the headers the CDN and the downloader need.

        The CDN refuses a request without a matching Referer, so the caller
        must not build its own header set.
        """
        headers = {
            "User-Agent": BILI_UA,
            "Referer": BILI_REFERER,
            "Origin": "https://www.bilibili.com",
        }
        cookie = self.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        if extra:
            for name, value in extra.items():
                headers[str(name)] = str(value)
        return headers

    @property
    def session(self) -> Any:
        """The HTTP session.

        The helper modules (danmaku, private messages, uploading) borrow it so
        that every request carries the same cookies and the same connection
        pool.
        """
        return self._session

    def wbi_key(self) -> str:
        """The current WBI mixin key; fetched once and then kept."""
        return self._ensure_mixin_key()

    def get_wbi(self, path: str, params: dict[str, Any] | None = None,
                base: str = BILI_API_BASE) -> dict[str, Any] | None:
        """A signed GET against the Bilibili API.

        The ``/wbi/`` routes only answer a request that carries ``w_rid`` and
        ``wts``.  Returns the parsed envelope, or ``None`` when the request
        failed.
        """
        signed = sign_params(dict(params or {}), self._ensure_mixin_key())
        url = path if path.startswith("http") else base + path
        try:
            response = self._session.get(url, params=signed, headers=self._headers(),
                                         timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
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
        return payload if isinstance(payload, dict) else None

    def post_form(self, path: str, data: dict[str, Any] | None = None,
                  base: str = BILI_API_BASE) -> dict[str, Any] | None:
        """A form POST against the Bilibili API.

        The CSRF token is added here: every write route refuses a request
        without it.  Returns the parsed envelope, or ``None`` on failure.
        """
        payload = dict(data or {})
        payload.setdefault("csrf", self.csrf())
        url = path if path.startswith("http") else base + path
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = self._session.post(url, data=payload, headers=headers,
                                          timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("bilibili write failed: %s (%s)", path, exc)
            return None
        status = getattr(response, "status_code", 200)
        if status != 200:
            log.warning("bilibili write %s answered HTTP %s", path, status)
            return None
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili write %s is not JSON: %s", path, exc)
            return None
        return body if isinstance(body, dict) else None

    def account_info(self) -> dict[str, Any]:
        """Return the account state of the current credential.

        ``/x/web-interface/nav`` answers ``-101`` for an anonymous client;
        that is the "not signed in" case, not an error.
        """
        empty: dict[str, Any] = {
            "logged_in": False,
            "mid": "",
            "uname": "",
            "face": "",
            "level": 0,
            "vip_status": 0,
            "vip_label": "",
            "coins": 0,
        }
        payload = self._get("/x/web-interface/nav")
        if not payload:
            return empty
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("isLogin"):
            return empty
        level_info = data.get("level_info") or {}
        return {
            "logged_in": True,
            "mid": str(data.get("mid") or ""),
            "uname": str(data.get("uname") or ""),
            "face": _absolute_url(data.get("face")),
            "level": int(level_info.get("current_level") or 0),
            "vip_status": int(data.get("vipStatus") or 0),
            "vip_label": str(data.get("vipLabel") or ""),
            "coins": int(data.get("money") or 0),
        }

    def _clear_cookie(self) -> None:
        """Forget the credential, both locally and in the session jar."""
        self.set_cookie("")
        jar = getattr(self._session, "cookies", None)
        clear = getattr(jar, "clear", None)
        if callable(clear):
            try:
                clear()
            except TypeError:
                pass
            except Exception:  # noqa: BLE001
                pass
        # The device identity is gone with the jar; it is fetched again on
        # the next request.
        self._buvid_ready = False

    # ------------------------------------------------------------------
    # login
    # ------------------------------------------------------------------
    def login_qrcode(self) -> dict[str, Any]:
        """Ask for a login QR code and the key that polls its state."""
        payload, _ = self._passport_request("/x/passport-login/web/qrcode/generate")
        if not payload:
            return {"success": False, "qrcode_url": "", "key": "",
                    "message": "网络异常，二维码获取失败，请稍后重试"}
        if payload.get("code") != 0:
            return {
                "success": False,
                "qrcode_url": "",
                "key": "",
                "message": "二维码获取失败：" + str(payload.get("message") or payload.get("code")),
            }
        data = payload.get("data") or {}
        url = _absolute_url(data.get("url"))
        key = str(data.get("qrcode_key") or "")
        if not url or not key:
            return {"success": False, "qrcode_url": "", "key": "",
                    "message": "二维码获取失败：服务端未返回二维码"}
        return {"success": True, "qrcode_url": url, "key": key, "message": ""}

    def login_poll(self, key: str) -> dict[str, Any]:
        """Read the state of a QR code; the credential arrives on success.

        The session cookie is only ever handed out through the ``Set-Cookie``
        header of the answer, so the response object is needed here.
        """
        key = str(key or "").strip()
        if not key:
            return {"success": False, "status": "failed", "cookie": "",
                    "message": "缺少二维码 key"}
        payload, response = self._passport_request(
            "/x/passport-login/web/qrcode/poll", {"qrcode_key": key}
        )
        if not payload:
            return {"success": False, "status": "failed", "cookie": "",
                    "message": "网络异常，二维码状态获取失败，请稍后重试"}
        data = payload.get("data") or {}
        code = data.get("code", payload.get("code"))
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = -1
        status = _QR_STATUS.get(code, "failed")
        if status == "success":
            cookies = self._response_cookies(response)
            if "SESSDATA" not in cookies:
                return {"success": False, "status": "failed", "cookie": "",
                        "message": "登录未完成：服务端没有下发会话 Cookie"}
            cookie = self._cookie_string(cookies)
            self.set_cookie(cookie)
            return {"success": True, "status": "success", "cookie": cookie, "message": ""}
        reason = str(data.get("message") or payload.get("message") or "")
        message = _QR_MESSAGES.get(status) or reason or "登录失败"
        return {"success": False, "status": status, "cookie": "", "message": message}

    def login_cookie(self, cookie: str) -> dict[str, Any]:
        """Adopt an existing credential and check that it still works."""
        self.set_cookie(cookie)
        info = self.account_info()
        if info.get("logged_in"):
            return {"success": True, "cookie": self.cookie_header(), "message": ""}
        return {"success": False, "cookie": "",
                "message": "Cookie 无效或已过期，请重新登录"}

    def password_key(self) -> dict[str, str]:
        """Fetch the public key and the salt the password is encrypted with.

        The salt lives about twenty seconds, so it is asked for right before
        the encryption, not once at startup.
        """
        payload, _ = self._passport_request(
            "/x/passport-login/web/key", {"r": str(int(time.time() * 1000))}
        )
        if not payload or payload.get("code") != 0:
            return {"hash": "", "key": "", "message": "网络异常，取不到公钥"}
        data = payload.get("data") or {}
        hash_salt = str(data.get("hash") or "")
        pem = str(data.get("key") or "")
        if not hash_salt or not pem:
            return {"hash": "", "key": "", "message": "取公钥失败：服务端没有返回密钥"}
        return {"hash": hash_salt, "key": pem, "message": ""}

    def login_password(
        self,
        username: str,
        password: str,
        captcha_key: str = "",
        captcha: str = "",
        challenge: str = "",
        validate: str = "",
        seccode: str = "",
        token: str = "",
    ) -> dict[str, Any]:
        """Sign in with a user name and a password.

        The web route (``/x/passport-login/web/login``) cannot be used from
        a desktop client: the server checks the slider and answers ``-105``
        （验证码错误）for every request without one, so the login used to
        fail with "网络异常" no matter what the user typed.

        The app route (``/x/passport-login/oauth2/login``) does not check
        the slider at all.  With a correct signature and the four geetest
        fields left empty it goes straight to the credential check, so the
        password login works without a browser.

        The password is encrypted with the public key the server hands out,
        exactly the way the phone app does it.
        """
        key = self.password_key()
        if not key.get("key"):
            return {
                "success": False,
                "cookie": "",
                "message": key.get("message") or "网络异常，登录失败，请稍后重试",
            }
        try:
            cipher = bili_password_cipher(str(password or ""), key["hash"], key["key"])
        except Exception as exc:  # noqa: BLE001
            log.warning("bilibili password encryption failed: %s", exc)
            return {"success": False, "cookie": "",
                    "message": "登录失败：密码加密出错，请稍后重试"}

        params = app_login_params(str(username or ""), cipher)
        payload, response = self._passport_request(
            "/x/passport-login/oauth2/login", params, method="POST"
        )
        if not payload:
            return {"success": False, "cookie": "", "message": "网络异常，登录失败，请稍后重试"}
        raw_data = payload.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        raw_code = data.get("code", payload.get("code"))
        try:
            code = int(raw_code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            code = -1
        if code == 0:
            # The app route hands the cookies over in the body, not in a
            # ``Set-Cookie`` header.
            cookies = self._cookie_info_cookies(data)
            if not cookies:
                cookies = self._response_cookies(response)
            if "SESSDATA" not in cookies:
                return {"success": False, "cookie": "",
                        "message": "登录未完成：服务端没有下发会话 Cookie"}
            cookie = self._cookie_string(cookies)
            self.set_cookie(cookie)
            return {"success": True, "cookie": cookie, "message": ""}
        if _needs_captcha(data, code):
            return {
                "success": False,
                "cookie": "",
                "message": "需要完成验证码验证",
                "captcha_required": True,
                "captcha_key": _captcha_key(data),
            }
        reason = str(data.get("message") or payload.get("message") or code)
        return {"success": False, "cookie": "", "message": "登录失败：" + reason}

    def _cookie_info_cookies(self, data: dict[str, Any]) -> dict[str, str]:
        """Read the cookies out of ``data.cookie_info.cookies``."""
        info = data.get("cookie_info") if isinstance(data.get("cookie_info"), dict) else {}
        found: dict[str, str] = {}
        for item in info.get("cookies") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = item.get("value")
            if name and value is not None:
                found[name] = str(value)
        return found

    def captcha(self) -> dict[str, Any]:
        """Fetch the parameters of the captcha the risk control asks for."""
        payload, _ = self._passport_request(
            "/x/passport-login/captcha", {"source": "main_mini"}
        )
        if not payload:
            return {"success": False, "gt": "", "challenge": "", "token": "",
                    "captcha_key": "", "message": "网络异常，验证码获取失败，请稍后重试"}
        if payload.get("code") != 0:
            return {
                "success": False,
                "gt": "",
                "challenge": "",
                "token": "",
                "captcha_key": "",
                "message": "验证码获取失败："
                + str(payload.get("message") or payload.get("code")),
            }
        data = payload.get("data") or {}
        geetest = data.get("geetest") or {}
        gt = str(geetest.get("gt") or "")
        challenge = str(geetest.get("challenge") or "")
        token = str(geetest.get("token") or "")
        if not gt or not challenge:
            return {"success": False, "gt": gt, "challenge": challenge, "token": token,
                    "captcha_key": str(data.get("captcha_key") or ""),
                    "message": "验证码获取失败：服务端未返回验证参数"}
        return {"success": True, "gt": gt, "challenge": challenge, "token": token,
                "captcha_key": str(data.get("captcha_key") or ""), "message": ""}

    def send_sms_code(
        self,
        tel: str,
        cid: str = "86",
        challenge: str = "",
        validate: str = "",
        seccode: str = "",
        token: str = "",
    ) -> dict[str, Any]:
        """Ask the server to send a login code to a phone number.

        The web route (``/x/passport-login/web/sms/send``) needs a slider:
        without one it answers ``-105``（验证码错误）, so the code never
        arrives and the interface shows a network error.

        The app route (``/x/passport-login/sms/send``) answers ``code 0``
        without a slider.  When the risk control does not fire it hands out
        the ``captcha_key`` right away and the whole login runs without a
        browser.  When it does fire, the answer carries a ``recaptcha_url``
        -- the page the user has to solve the slider on; that url is passed
        back so the interface can open it.

        The ``captcha_key`` is what the login that follows has to send
        back, otherwise the server answers ``86205``（验证码失效）.
        """
        params = app_sms_params(str(tel or ""), cid)
        payload, _ = self._passport_request(
            "/x/passport-login/sms/send", params, method="POST"
        )
        if not payload:
            return {"success": False, "cookie": "", "captcha_key": "",
                    "message": "网络异常，验证码发送失败，请稍后重试"}
        raw_data = payload.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if payload.get("code") != 0:
            return {"success": False, "cookie": "", "captcha_key": "",
                    "message": "验证码发送失败："
                    + str(payload.get("message") or payload.get("code"))}
        captcha_key = str(data.get("captcha_key") or "")
        recaptcha_url = str(data.get("recaptcha_url") or "")
        if not captcha_key and recaptcha_url:
            # The risk control wants the slider before the code is sent.
            return {
                "success": False,
                "cookie": "",
                "captcha_key": "",
                "captcha_required": True,
                "recaptcha_url": recaptcha_url,
                "message": "需要完成滑块验证后才能发送验证码",
            }
        return {"success": True, "cookie": "",
                "captcha_key": captcha_key,
                "message": "验证码已发送"}

    def login_sms(
        self,
        tel: str,
        code: str,
        cid: str = "86",
        challenge: str = "",
        validate: str = "",
        seccode: str = "",
        token: str = "",
        captcha_key: str = "",
    ) -> dict[str, Any]:
        """Sign in with the code that was sent to a phone number.

        The app route (``/x/passport-login/login/sms``) is used: it wants
        the ``captcha_key`` that the send step handed out, and it answers
        ``-400`` when that key is missing and ``86205``（验证码失效）when it
        is stale.
        """
        params = app_sms_login_params(
            str(tel or ""), str(code or ""), str(captcha_key or ""), cid
        )
        payload, response = self._passport_request(
            "/x/passport-login/login/sms", params, method="POST"
        )
        if not payload:
            return {"success": False, "cookie": "", "message": "网络异常，登录失败，请稍后重试"}
        raw_data = payload.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        raw_code = data.get("code", payload.get("code"))
        try:
            code_value = int(raw_code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            code_value = -1
        if code_value == 0:
            # The app route hands the cookies over in the body.
            cookies = self._cookie_info_cookies(data)
            if not cookies:
                cookies = self._response_cookies(response)
            if "SESSDATA" not in cookies:
                return {"success": False, "cookie": "",
                        "message": "登录未完成：服务端没有下发会话 Cookie"}
            cookie = self._cookie_string(cookies)
            self.set_cookie(cookie)
            return {"success": True, "cookie": cookie, "message": ""}
        if _needs_captcha(data, code_value):
            return {
                "success": False,
                "cookie": "",
                "message": "需要完成验证码验证",
                "captcha_required": True,
                "captcha_key": _captcha_key(data),
            }
        reason = str(data.get("message") or payload.get("message") or code_value)
        return {"success": False, "cookie": "", "message": "登录失败：" + reason}

    def register_send_code(
        self,
        tel: str,
        cid: str = "86",
        challenge: str = "",
        validate: str = "",
        seccode: str = "",
        token: str = "",
    ) -> dict[str, Any]:
        """Ask the server to send a registration code to a phone number.

        The registration route is ``/web/sms/general/v2/send`` -- the old
        ``/x/passport-register/web/sendcode`` is a 404.  It needs a ``key``
        (the registration captcha key) besides the geetest parameters, and
        it only answers ``POST``.

        B站 retired this route for third party clients: the answer is
        ``code 20000``（该版本已不支持当前功能）.  The reason is passed on
        to the caller as it is, so the interface can say what happened
        instead of "网络异常".
        """
        params = {
            "key": REGISTER_SMS_KEY,
            "captchaType": "6",
            # type 1 is the registration text; the login route uses 21.
            "type": "1",
            "cid": str(cid or "86"),
            "tel": str(tel or ""),
            "source": "main_landing",
            "token": str(token or ""),
            "challenge": str(challenge or ""),
            "seccode": str(seccode or ""),
            "validate": str(validate or ""),
        }
        payload, _ = self._passport_request(
            "/web/sms/general/v2/send", params, method="POST"
        )
        if not payload:
            return {"success": False, "cookie": "", "captcha_key": "",
                    "message": "网络异常，验证码发送失败，请稍后重试"}
        code = payload.get("code")
        if code != 0:
            message = str(payload.get("message") or code)
            if code == 20000:
                message = ("B站已停止对第三方客户端开放注册验证码（服务端答复："
                           + message + "）。请改用扫码或账号密码登录。")
            return {"success": False, "cookie": "", "captcha_key": "",
                    "message": "验证码发送失败：" + message}
        return {"success": True, "cookie": "", "captcha_key": "",
                "message": "验证码已发送"}

    def register(
        self,
        username: str,
        password: str,
        tel: str,
        code: str,
        cid: str = "86",
        challenge: str = "",
        validate: str = "",
        seccode: str = "",
        token: str = "",
    ) -> dict[str, Any]:
        """Create a new account.

        The registration route is ``/web/reg/tel`` -- the old
        ``/x/passport-register/web/register`` is a 404.  It takes
        ``nickName`` / ``pwd`` / ``plat`` instead of ``username`` /
        ``password``, and only answers ``POST``.

        B站 retired this route for third party clients too: the answer is
        ``code 20000``（该版本已不支持当前功能）.
        """
        params = {
            "nickName": str(username or ""),
            "pwd": str(password or ""),
            "tel": str(tel or ""),
            "code": str(code or ""),
            "cid": str(cid or "86"),
            "plat": "0",
            "gourl": "https://www.bilibili.com/",
            "source": "main_landing",
            "token": str(token or ""),
            "challenge": str(challenge or ""),
            "seccode": str(seccode or ""),
            "validate": str(validate or ""),
        }
        payload, response = self._passport_request(
            "/web/reg/tel", params, method="POST"
        )
        if not payload:
            return {"success": False, "cookie": "",
                    "message": "网络异常，注册失败，请稍后重试"}
        if payload.get("code") != 0:
            reason = str(payload.get("message") or payload.get("code"))
            if payload.get("code") == 20000:
                reason = ("B站已停止对第三方客户端开放注册（服务端答复：" + reason
                          + "）。请改用扫码或账号密码登录。")
            return {"success": False, "cookie": "", "message": "注册失败：" + reason}
        # The server sometimes signs the new account in right away; when it
        # does, the session cookie comes with the answer.
        cookies = self._response_cookies(response)
        cookie = self._cookie_string(cookies) if "SESSDATA" in cookies else ""
        if cookie:
            self.set_cookie(cookie)
        return {"success": True, "cookie": cookie, "message": "注册成功，请登录"}

    def logout(self) -> dict[str, Any]:
        """Drop the credential locally and tell the server to end the session."""
        payload, _ = self._passport_request("/login/exit/v2")
        self._clear_cookie()
        if payload is None:
            # The server could not be reached; the local credential is gone
            # either way, so the caller may still report a clean logout.
            log.warning("bilibili logout call failed, the local cookie is gone anyway")
        return {"success": True, "message": "已退出登录"}

    # ------------------------------------------------------------------
    # favourite folders
    # ------------------------------------------------------------------
    def favorite_folders(self) -> list[dict[str, Any]]:
        """Return the favourite folders of the signed in user."""
        mid = ""
        info = self.account_info()
        if info.get("logged_in"):
            mid = str(info.get("mid") or "")
        if not mid:
            mid = str(self._cookie.get("DedeUserID") or "")
        if not mid:
            return []
        payload = self._get("/x/v3/fav/folder/created/list-all", {"up_mid": mid})
        if not payload or payload.get("code") != 0:
            return []
        data = payload.get("data") or {}
        items = data.get("list")
        if not isinstance(items, list):
            return []
        folders: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            folders.append({
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "media_count": int(item.get("media_count") or 0),
                "cover": _absolute_url(item.get("cover")),
            })
        return folders

    def favorite_videos(
        self,
        folder_id: str,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        """Return one page of the videos inside a favourite folder."""
        folder_id = str(folder_id or "").strip()
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 30), 50))
        empty: dict[str, Any] = {"items": [], "has_more": False, "page": page}
        if not folder_id:
            return empty
        payload = self._get(
            "/x/v3/fav/resource/list",
            {
                "media_id": folder_id,
                "pn": page,
                "ps": page_size,
                "platform": "web",
            },
        )
        if not payload or payload.get("code") != 0:
            return empty
        data = payload.get("data") or {}
        medias = data.get("medias")
        if not isinstance(medias, list):
            return empty
        items: list[dict[str, Any]] = []
        for item in medias:
            if not isinstance(item, dict):
                continue
            upper = item.get("upper") or {}
            items.append({
                "bvid": str(item.get("bvid") or ""),
                "aid": int(item.get("id") or 0),
                "title": str(item.get("title") or ""),
                "author": str(upper.get("name") or ""),
                "author_mid": str(upper.get("mid") or ""),
                "cover": _absolute_url(item.get("cover")),
                "duration": _fav_duration(item.get("duration")),
                "intro": str(item.get("intro") or ""),
                "fav_time": int(item.get("fav_time") or 0),
            })
        has_more = data.get("has_more")
        if has_more is None:
            has_more = len(medias) >= page_size
        return {"items": items, "has_more": bool(has_more), "page": page}

    # ------------------------------------------------------------------
    # playback
    # ------------------------------------------------------------------
    def video_streams(self, bvid: str, quality: int = 0,
                       with_muxed: bool = True) -> dict[str, Any]:
        """Resolve the playable streams of one video.

        ``quality=0`` means "the best the account may watch".  The answer
        carries the separated DASH streams and, when the video has them, a
        lossless (FLAC) and a Dolby audio track.

        ``with_muxed`` asks for a single file (picture and sound together) as
        well.  It costs one more request, and it is what the webview needs on
        macOS: the WKWebView there has no ``MediaSource``, so a separated
        DASH stream cannot be played back.
        """
        bvid = str(bvid or "").strip()
        result: dict[str, Any] = {
            "success": False,
            "bvid": bvid,
            "cid": 0,
            "duration": 0,
            "title": "",
            "author": "",
            "author_mid": "",
            "cover": "",
            "qualities": [],
            "audio": None,
            "video": None,
            "muxed": None,
            "flac": None,
            "dolby": None,
            "message": "",
        }
        if not bvid:
            result["message"] = "缺少视频号"
            return result
        self._ensure_buvid()
        view = self._get("/x/web-interface/view", {"bvid": bvid})
        if not view or view.get("code") != 0:
            result["message"] = "视频信息获取失败"
            return result
        view_data = view.get("data") or {}
        cid = int(view_data.get("cid") or 0)
        owner = view_data.get("owner") or {}
        result["cid"] = cid
        result["duration"] = _duration_seconds(view_data.get("duration"))
        result["title"] = _strip_html(str(view_data.get("title") or ""))
        result["author"] = str(owner.get("name") or "")
        # 上传的 mid：个人主页就是靠它打开的。
        result["author_mid"] = str(owner.get("mid") or "")
        result["cover"] = _absolute_url(view_data.get("pic"))
        if not cid:
            result["message"] = "无法定位视频分 P"
            return result

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
            result["message"] = "播放地址获取失败"
            return result
        if payload.get("code") != 0:
            result["message"] = str(payload.get("message") or payload.get("code"))
            return result
        data = payload.get("data") or {}
        dash = data.get("dash") or {}

        result["audio"] = self._pick_audio(dash.get("audio") or [])
        video_entries = [item for item in (dash.get("video") or []) if isinstance(item, dict)]
        chosen = self._pick_video(video_entries, quality)
        result["video"] = self._stream_payload(chosen, with_video=True)
        got_ids = {int(item.get("id") or 0) for item in video_entries if item.get("id")}
        result["qualities"] = self._build_qualities(data, got_ids)

        # The Hi-Res track lives in its own section; when it does not, the
        # codec id of a regular audio entry may still name it.
        flac_section = (dash.get("flac") or {}).get("audio") or []
        dolby_section = (dash.get("dolby") or {}).get("audio") or []
        result["flac"] = self._pick_audio(flac_section) or self._pick_special_audio(
            dash.get("audio") or [], flac=True
        )
        result["dolby"] = self._pick_audio(dolby_section) or self._pick_special_audio(
            dash.get("audio") or [], flac=False
        )

        durl = data.get("durl") or []
        if durl and isinstance(durl[0], dict):
            url = _absolute_url(durl[0].get("url"))
            if url:
                result["muxed"] = {
                    "url": url,
                    "mime": "video/mp4",
                    "size": int(durl[0].get("size") or 0),
                    "quality": int(data.get("quality") or 0),
                }
        if not result["muxed"] and with_muxed:
            # macOS 的 WKWebView 没有 MediaSource，播不了分轨的 DASH。
            # DASH 的回答里又不带 durl，所以要再单独要一份「画面和声音在
            # 一起」的单文件流，作为播放器的兜底。
            result["muxed"] = self._fetch_muxed(bvid, cid, quality)
        result["success"] = bool(result["audio"] or result["video"] or result["muxed"])
        if not result["success"]:
            result["message"] = "服务端没有返回可用的播放流"
        return result

    def _pick_audio(self, entries: list[Any]) -> dict[str, Any] | None:
        """Return the audio entry with the highest bandwidth."""
        best: dict[str, Any] | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if best is None or int(entry.get("bandwidth") or 0) > int(best.get("bandwidth") or 0):
                best = entry
        return self._stream_payload(best)

    def _pick_special_audio(self, entries: list[Any], flac: bool) -> dict[str, Any] | None:
        """Return the lossless or the Dolby audio entry of a DASH answer."""
        best: dict[str, Any] | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            codecid = int(entry.get("codecid") or 0)
            codecs = str(entry.get("codecs") or "").lower()
            if flac:
                match = codecid == 30280 or "flac" in codecs
            else:
                match = codecid in (30250, 30251) or "mp4a.40.5" in codecs
            if not match:
                continue
            if best is None or int(entry.get("bandwidth") or 0) > int(best.get("bandwidth") or 0):
                best = entry
        return self._stream_payload(best)

    def _pick_video(self, entries: list[dict[str, Any]], quality: int) -> dict[str, Any] | None:
        """Pick the video entry that matches the requested quality.

        The ``id`` of a DASH video entry is the quality code; the entries
        the server sends may also carry a resolution the caller did not
        ask for, so the filter comes first.
        """
        wanted = int(quality or 0)
        usable = [
            entry
            for entry in entries
            if (entry.get("baseUrl") or entry.get("base_url"))
            and (not wanted or int(entry.get("id") or 0) <= wanted)
        ]
        if not usable:
            return None
        # Same resolution is offered more than once (different bitrates and
        # different CDN hosts).  Taking the first entry in the answer picks a
        # 750 kB/s stream where a 2.6 MB/s one was right next to it.
        usable.sort(
            key=lambda entry: (int(entry.get("height") or 0),
                               int(entry.get("bandwidth") or 0),
                               int(entry.get("id") or 0)),
            reverse=True,
        )
        return usable[0]

    def _stream_payload(self, entry: dict[str, Any] | None, with_video: bool = False) -> dict[str, Any] | None:
        """Normalise one DASH entry into the flat structure the UI expects."""
        if not entry:
            return None
        url = str(entry.get("baseUrl") or entry.get("base_url") or "")
        if not url:
            return None
        segment = entry.get("segment_base") or entry.get("segmentBase") or {}
        candidates = _ordered_bili_urls(
            [_absolute_url(url)]
            + [_absolute_url(item) for item in (entry.get("backupUrl") or entry.get("backup_url") or [])
               if isinstance(item, str)]
        )
        payload = {
            "url": candidates[0],
            "backup_urls": candidates[1:],
            "mime": str(entry.get("mimeType") or entry.get("mime_type") or ""),
            "codecs": str(entry.get("codecs") or ""),
            "bandwidth": int(entry.get("bandwidth") or 0),
            "size": int(entry.get("size") or 0),
            "init_range": str(segment.get("initialization") or ""),
        }
        if with_video:
            payload["width"] = int(entry.get("width") or 0)
            payload["height"] = int(entry.get("height") or 0)
        return payload

    def _fetch_muxed(self, bvid: str, cid: int, quality: int) -> dict[str, Any] | None:
        """Ask for one file that carries both the picture and the sound.

        The separated DASH streams are the better choice, but a browser
        without ``MediaSource`` cannot put them back together.  ``fnval=1``
        asks the server for the single-file variant; its highest quality is
        720P, which is why it is only a fallback.
        """
        params = sign_params(
            {
                "bvid": bvid,
                "cid": cid,
                # The single-file route tops out at 720P.
                "qn": min(int(quality or 0) or 64, 64),
                "fnval": 1,
                "fnver": 0,
                "fourk": 0,
                "platform": "pc",
            },
            self._ensure_mixin_key(),
        )
        payload = self._get("/x/player/wbi/playurl", params)
        if not payload or payload.get("code") != 0:
            return None
        data = payload.get("data") or {}
        durl = data.get("durl") or []
        if not durl or not isinstance(durl[0], dict):
            return None
        url = _absolute_url(durl[0].get("url"))
        if not url:
            return None
        candidates = _ordered_bili_urls(
            [url] + [_absolute_url(item) for item in (durl[0].get("backup_url") or [])
                     if isinstance(item, str)]
        )
        return {
            "url": candidates[0],
            "backup_urls": candidates[1:],
            "mime": "video/mp4",
            "size": int(durl[0].get("size") or 0),
            "quality": int(data.get("quality") or 0),
        }

    def _build_qualities(self, data: dict[str, Any], got_ids: set[int]) -> list[dict[str, Any]]:
        """Describe every quality the account may pick.

        ``available`` means "this account may watch it": the code is inside
        ``accept_quality``.  The server is asked for one quality at a time, so
        the other qualities are still worth offering -- picking one asks for
        it.  ``current`` marks the resolution this answer actually carries.
        """
        names: dict[int, str] = {}
        for item in data.get("support_formats") or []:
            if not isinstance(item, dict):
                continue
            code = int(item.get("quality") or 0)
            if not code:
                continue
            names[code] = str(item.get("new_description") or item.get("format") or "")
        accepted: set[int] = set()
        for part in str(data.get("accept_quality") or "").split(","):
            part = part.strip()
            if part.isdigit():
                accepted.add(int(part))
        qualities: list[dict[str, Any]] = []
        for code in sorted(set(names) | accepted):
            qualities.append({
                "id": code,
                "name": names.get(code) or video_quality_name(code),
                "available": code in accepted,
                "current": code in got_ids,
            })
        return qualities

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

    def recommend(self, page_size: int = 12, fresh_index: int = 1,
                  session_id: str = "") -> list[dict[str, Any]]:
        """The personalised home feed of this account.

        ``/x/web-interface/wbi/index/top/feed/rcmd`` is the route the web home
        page uses.  Without a login it still answers -- with the site wide feed
        instead of a personal one -- so the caller gets rows either way and can
        say so in the interface.

        ``fresh_index`` is "the n-th refresh"; reusing 1 keeps the feed stable
        while the user scrolls, which is what the page does too.
        """
        self._ensure_buvid()
        page_size = max(1, min(int(page_size or 12), 30))
        params = {
            # The home slot; the route wants it even though it is not in any
            # public parameter list.
            "web_location": "1430650",
            "y_num": "8",
            "last_y_num": "8",
            "fresh_type": str(max(1, int(fresh_index))),
            "fresh_idx": str(max(1, int(fresh_index))),
            "fresh_idx_1h": "1",
            "brush": str(max(1, int(fresh_index))),
            "fetch_row": "1",
            "feed_version": "V8",
            "homepage_ver": "1",
            "ps": str(page_size),
            "screen": "1920-1080",
            "seo_info": "",
            "uniq_id": session_id or str(int(time.time())),
        }
        payload = self.get_wbi("/x/web-interface/wbi/index/top/feed/rcmd", params)
        if not payload or payload.get("code") != 0:
            return []
        data = payload.get("data") or {}
        items = data.get("item") if isinstance(data.get("item"), list) else []
        rows: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # An advertisement slot carries the real video one level down.
            business = item.get("business_info") if isinstance(item.get("business_info"), dict) else {}
            archive = business.get("archive") if isinstance(business.get("archive"), dict) else {}
            source = archive or item
            entry = self._from_archive_item(source, 0)
            # An ad has no bvid of its own; without one it cannot be played.
            if not entry.get("bvid"):
                continue
            owner = source.get("owner") if isinstance(source.get("owner"), dict) else {}
            entry["author"] = str(owner.get("name") or entry.get("author") or "")
            entry["author_mid"] = str(owner.get("mid") or "")
            reason = item.get("rcmd_reason") if isinstance(item.get("rcmd_reason"), dict) else {}
            entry["reason"] = str(reason.get("content") or "")
            entry["followed"] = bool(_as_int(item.get("is_followed")))
            rows.append(entry)
        return rows

    def report_playback(
        self,
        bvid: str,
        cid: int = 0,
        played_time: int = 0,
        video_duration: int = 0,
        quality: int = 0,
        start_ts: int = 0,
        session: str = "",
        play_type: int = 0,
        realtime: int = 0,
    ) -> dict[str, Any]:
        """Tell B站 that a video is being watched.

        This is what fills the Bilibili watch history and what gives the
        recommendation feed something to work with.  It also writes to the
        account, so it only ever runs when the user turned it on in the
        settings -- the caller decides, not this method.

        The web player sends one of these every 15 seconds; ``play_type`` is
        0 while playing, 1 on start, 2 on pause, 3 on resume, 4 on end.
        """
        result: dict[str, Any] = {"success": False, "message": ""}
        bvid = str(bvid or "").strip()
        if not bvid or not self.is_logged_in:
            return result
        aid = int(self.video(bvid).get("aid") or 0)
        if not aid:
            return result
        duration = max(0, _as_int(video_duration, 0))
        played = max(0, _as_int(played_time, 0))
        real = max(0, _as_int(realtime, 0)) or played
        form = {
            "aid": aid,
            "bvid": bvid,
            "cid": _as_int(cid, 0),
            "played_time": played,
            "realtime": real,
            "real_played_time": real,
            "video_duration": duration,
            "last_play_progress_time": played,
            "max_play_progress_time": played,
            "start_ts": _as_int(start_ts, 0) or int(time.time()),
            # 3 = 投稿视频, 0 = 普通视频
            "type": 3,
            "sub_type": 0,
            "dt": 2,
            "play_type": _as_int(play_type, 0),
            "mid": str(self._cookie.get("DedeUserID") or ""),
            "refer_url": "https://www.bilibili.com/",
            "quality": _as_int(quality, 0),
            "session": str(session or uuid.uuid4().hex),
            "outer": 0,
            "spmid": "333.788.0.0",
            "extra": json.dumps({"player_version": "4.8.36"}),
        }
        payload = self.post_form("/x/click-interface/web/heartbeat", form)
        if not payload:
            result["message"] = "网络异常，播放数据没有上报"
            return result
        if payload.get("code") != 0:
            result["message"] = _error_message(payload, "播放数据上报失败")
            return result
        result["success"] = True
        return result

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
        """Hot videos of a partition.

        The partition board (``/x/web-interface/ranking/region``) is the
        right source: it answers for every video partition (``rid`` 1, 3,
        4, 24, 28, 29, 33, 36, 160 -- verified live) and carries the same
        fields the hot list does.  The keyword search that was used before
        returns nothing for several of them (the 纪录片 partition answered
        0 or 1 rows, the 番剧 partition always 0), which is what left
        those tabs empty.

        The board has no paging, so only the first page comes from it; the
        rest falls back to the search.
        """
        rid = int(rid or 0)
        if page == 1 and rid:
            board = self._partition_board(rid)
            if board:
                return board[:max(1, int(page_size))]
        keyword = BILI_RID_KEYWORDS.get(rid) or BILI_RID_NAMES.get(rid) or ""
        if not keyword:
            return []
        items = self.search(
            keyword,
            page=page,
            page_size=page_size,
            order=order,
            category=_RID_CATEGORY.get(rid, "all"),
        )
        if items:
            return items
        # 番剧 (rid 2) is PGC content: it has no ``bvid`` at all, so the
        # partition scoped search answers nothing for it.  The videos people
        # upload about anime do carry a ``bvid``, and the same keyword
        # without the partition filter finds them.
        if rid == 2:
            return self.search(
                keyword,
                page=page,
                page_size=page_size,
                order=order,
                category="all",
            )
        return items

    def _partition_board(self, rid: int) -> list[dict[str, Any]]:
        """The partition board of one partition (``[]`` when it has none)."""
        payload = self._get("/x/web-interface/ranking/region",
                            {"rid": int(rid or 0), "day": 3})
        if not payload or payload.get("code") != 0:
            return []
        entries = payload.get("data")
        if not isinstance(entries, list):
            return []
        items = [
            self._from_archive_item(entry, int(rid or 0))
            for entry in entries
            if isinstance(entry, dict)
        ]
        return [item for item in items if item.get("bvid")]

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
            # 上传的 mid：个人主页就是靠它打开的。
            "author_mid": "",
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
        out["author_mid"] = str(item.get("mid") or item.get("author_mid") or "")
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
        out["author_mid"] = str(owner.get("mid") or "")
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
        out["author_mid"] = str(owner.get("mid") or item.get("mid") or "")
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

    # ------------------------------------------------------------------
    # 互动：统计、点赞、投币、收藏、关注、观看历史、空间主页
    #
    # 这一批方法会改到账号，所以每一条都遵守两条规矩：写操作先确认
    # 登录状态（未登录时服务端有时会回 code 0 但实际什么都没做），
    # 出错时把错误码翻译成给用户看的中文，绝不把原始 code 抛出去。
    # ------------------------------------------------------------------
    #: 观看历史的游标：第一页之后要用上一页回答里的 ``cursor.max``。
    _history_cursor: dict[str, str] | None = None
    _history_page: int = 0

    def video_stat(self, bvid: str) -> dict[str, Any]:
        """Return the counters of one video.

        ``stat`` carries ``view`` / ``danmaku`` / ``reply`` / ``favorite``
        / ``coin`` / ``like`` / ``share``; a failed request answers
        ``success=False`` with an empty set of zeros instead of raising.

        The counters ride along with the video details: the separate
        ``/x/web-interface/archive/stat`` route is gone (it answers 404), but
        ``/x/web-interface/view`` carries the same numbers.
        """
        bvid = str(bvid or "").strip()
        stat = {
            "view": 0,
            "danmaku": 0,
            "reply": 0,
            "favorite": 0,
            "coin": 0,
            "like": 0,
            "share": 0,
        }
        if not bvid:
            return {"success": False, "stat": stat, "message": "缺少视频号"}
        self._ensure_buvid()
        payload = self._get("/x/web-interface/view", {"bvid": bvid})
        if not payload:
            return {"success": False, "stat": stat, "message": "统计数据获取失败，请稍后重试"}
        if payload.get("code") != 0:
            return {
                "success": False,
                "stat": stat,
                "message": _error_message(payload, "统计数据获取失败"),
            }
        data = payload.get("data") or {}
        raw = data.get("stat") if isinstance(data.get("stat"), dict) else {}
        return {
            "success": True,
            "stat": {
                "view": _as_int(raw.get("view")),
                "danmaku": _as_int(raw.get("danmaku")),
                "reply": _as_int(raw.get("reply")),
                "favorite": _as_int(raw.get("favorite")),
                "coin": _as_int(raw.get("coin")),
                "like": _as_int(raw.get("like")),
                "share": _as_int(raw.get("share")),
            },
            "message": "",
        }

    def like_video(self, bvid: str, like: bool = True) -> dict[str, Any]:
        """Like a video or take the like back.

        未登录时服务端会回 ``code 0`` 但其实什么都没做，所以这里先看
        登录状态，没登录就直接失败，不发请求。
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return {"success": False, "message": "缺少视频号"}
        if not self.is_logged_in:
            return {"success": False, "message": "请先登录B站账号"}
        aid = int(self.video(bvid).get("aid") or 0)
        if not aid:
            return {"success": False, "message": "无法获取视频信息，点赞失败"}
        payload = self.post_form(
            "/x/web-interface/archive/like",
            {
                "bvid": bvid,
                "aid": aid,
                # 1 是点赞，2 是取消点赞。
                "like": 1 if like else 2,
            },
        )
        if not payload:
            return {"success": False, "message": "网络异常，点赞失败，请稍后重试"}
        if payload.get("code") != 0:
            return {"success": False, "message": _error_message(payload, "点赞失败")}
        return {"success": True, "message": "已点赞" if like else "已取消点赞"}

    def coin_video(
        self,
        bvid: str,
        count: int = 1,
        also_like: bool = False,
    ) -> dict[str, Any]:
        """Give one or two coins to a video.

        ``count`` is clamped to 1..2 -- that is what the server accepts.
        ``balance`` is the number of coins that are left afterwards.
        """
        bvid = str(bvid or "").strip()
        result: dict[str, Any] = {"success": False, "message": "", "balance": 0}
        if not bvid:
            result["message"] = "缺少视频号"
            return result
        if not self.is_logged_in:
            result["message"] = "请先登录B站账号"
            return result
        aid = int(self.video(bvid).get("aid") or 0)
        if not aid:
            result["message"] = "无法获取视频信息，投币失败"
            return result
        multiply = max(1, min(_as_int(count, 1), 2))
        payload = self.post_form(
            "/x/web-interface/coin/add",
            {
                "bvid": bvid,
                "aid": aid,
                "multiply": multiply,
                # select_like=1 表示顺便点个赞。
                "select_like": 1 if also_like else 0,
            },
        )
        if not payload:
            result["message"] = "网络异常，投币失败，请稍后重试"
            return result
        if payload.get("code") != 0:
            result["message"] = _error_message(payload, "投币失败")
            return result
        data = payload.get("data") or {}
        balance = _as_int(data.get("balance")) if isinstance(data, dict) else 0
        if not balance:
            # 有的回答不带余额，多问一次拿最新的。
            balance = int(self.coin_balance().get("balance") or 0)
        result["success"] = True
        result["balance"] = balance
        result["message"] = f"投币成功，投了{multiply}个币"
        return result

    def coins_given(self, bvid: str) -> dict[str, Any]:
        """How many coins this account already gave the video (0-2).

        B站 lets one account put at most two coins on a submission.  Without
        asking, the interface cannot tell "one more is fine" from "the second
        click will be refused", so it asks before drawing the buttons.
        """
        result: dict[str, Any] = {"success": False, "given": 0, "message": ""}
        bvid = str(bvid or "").strip()
        if not bvid or not self.is_logged_in:
            return result
        payload = self._get("/x/web-interface/archive/coins", {"bvid": bvid})
        if not payload or payload.get("code") != 0:
            return result
        data = payload.get("data")
        given = _as_int((data or {}).get("multiply")) if isinstance(data, dict) else 0
        result["success"] = True
        result["given"] = max(0, min(given, 2))
        return result

    def coin_balance(self) -> dict[str, Any]:
        """Return the coin balance of the signed in account.

        The balance comes with the account state: the ``/x/web-interface/nav``
        answer carries the number of coins the account owns.  The separate
        ``/x/web-interface/coin/number`` route is gone (it answers 404).
        """
        result: dict[str, Any] = {
            "success": False,
            "balance": 0,
            "coin": 0,
            "message": "",
        }
        if not self.is_logged_in:
            result["message"] = "请先登录B站账号"
            return result
        self._ensure_buvid()
        payload = self._get("/x/web-interface/nav")
        if not payload:
            result["message"] = "硬币余额获取失败，请稍后重试"
            return result
        if payload.get("code") != 0:
            result["message"] = _error_message(payload, "硬币余额获取失败")
            return result
        data = payload.get("data") or {}
        coins = _as_int(data.get("money"))
        result["success"] = True
        # 硬币没有「总额」和「余额」之分，账号上有几枚就能投几枚。
        result["balance"] = coins
        result["coin"] = coins
        return result

    def favorite_video(
        self,
        bvid: str,
        folder_id: str = "",
        remove: bool = False,
    ) -> dict[str, Any]:
        """Put a video into a favourite folder, or take it out again.

        An empty ``folder_id`` means the default folder -- the one with
        the smallest id.
        """
        bvid = str(bvid or "").strip()
        folder_id = str(folder_id or "").strip()
        result: dict[str, Any] = {
            "success": False,
            "message": "",
            "folder_id": folder_id,
        }
        if not bvid:
            result["message"] = "缺少视频号"
            return result
        if not self.is_logged_in:
            result["message"] = "请先登录B站账号"
            return result
        if not folder_id:
            folders = self.favorite_folders()
            if not folders:
                result["message"] = "找不到收藏夹，收藏失败"
                return result
            # 默认收藏夹的 id 最小。
            folder_id = min(folders, key=lambda item: _as_int(item.get("id")))["id"]
            result["folder_id"] = folder_id
        aid = int(self.video(bvid).get("aid") or 0)
        if not aid:
            result["message"] = "无法获取视频信息，收藏失败"
            return result
        data: dict[str, Any] = {
            # ``rid`` 是资源 id（稿件的 aid），``add_media_ids`` / ``del_media_ids``
            # 才是收藏夹 id。这两样以前装反了，收藏一律回「请求错误」。
            "rid": aid,
            # 2 = 视频。
            "type": 2,
            "platform": "web",
        }
        if remove:
            data["del_media_ids"] = folder_id
        else:
            data["add_media_ids"] = folder_id
        payload = self.post_form("/x/v3/fav/resource/deal", data)
        if not payload:
            result["message"] = "网络异常，收藏失败，请稍后重试"
            return result
        if payload.get("code") != 0:
            result["message"] = _error_message(payload, "收藏失败")
            return result
        result["success"] = True
        result["message"] = "已取消收藏" if remove else "收藏成功"
        return result

    def favorite_folder(self, folder_id: str, remove: bool = False) -> dict[str, Any]:
        """Favourite somebody else's folder into the own collection."""
        folder_id = str(folder_id or "").strip()
        if not folder_id:
            return {"success": False, "message": "缺少收藏夹 ID"}
        if not self.is_logged_in:
            return {"success": False, "message": "请先登录B站账号"}
        path = "/x/v3/fav/folder/unfav" if remove else "/x/v3/fav/folder/fav"
        payload = self.post_form(path, {"media_id": folder_id})
        if not payload:
            return {"success": False, "message": "网络异常，操作失败，请稍后重试"}
        if payload.get("code") != 0:
            return {"success": False, "message": _error_message(payload, "收藏夹操作失败")}
        return {"success": True, "message": "已取消收藏收藏夹" if remove else "收藏夹已收藏"}

    def follow_user(self, mid: str, follow: bool = True) -> dict[str, Any]:
        """Follow a user, or stop following them.

        B 站把两个方向放在同一个路由里，用 ``act`` 区分：1 是关注，
        2 是取消关注。
        """
        mid = str(mid or "").strip()
        if not mid:
            return {"success": False, "message": "缺少用户 ID"}
        if not self.is_logged_in:
            return {"success": False, "message": "请先登录B站账号"}
        payload = self.post_form(
            "/x/relation/modify",
            {
                "fid": mid,
                "act": 1 if follow else 2,
                "re_src": 11,
            },
        )
        if not payload:
            return {"success": False, "message": "网络异常，操作失败，请稍后重试"}
        if payload.get("code") != 0:
            return {"success": False, "message": _error_message(payload, "关注操作失败")}
        return {"success": True, "message": "已关注" if follow else "已取消关注"}

    def follow_state(self, mid: str) -> dict[str, Any]:
        """Tell whether the account follows the given user.

        ``/x/relation/relations`` is not reliable for this, so the answer
        comes from ``/x/relation`` (signed) and falls back to the card
        route.  ``attribute`` 2 and 3 mean "following", 0 and 1 do not.
        """
        mid = str(mid or "").strip()
        result: dict[str, Any] = {
            "success": False,
            "following": False,
            "relation": 0,
            "message": "",
        }
        if not mid:
            result["message"] = "缺少用户 ID"
            return result
        attribute: int | None = None
        payload = self.get_wbi("/x/relation", {"fid": mid})
        if payload and payload.get("code") == 0:
            data = payload.get("data") or {}
            attribute = _as_int(data.get("attribute"), -1)
        if attribute is None or attribute < 0:
            card = self._get("/x/web-interface/card", {"mid": mid, "photo": "false"})
            if card and card.get("code") == 0:
                data = card.get("data") or {}
                attribute = _as_int(data.get("attribute"), -1)
        if attribute is None or attribute < 0:
            result["message"] = "关注状态获取失败"
            return result
        result["success"] = True
        result["relation"] = attribute
        result["following"] = attribute in (2, 3)
        return result

    def history(self, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        """Return the watched videos of the signed in account.

        The route is a cursor route: the first page uses ``max=0`` and
        every later page uses the ``cursor.max`` of the previous answer.
        """
        page = max(1, _as_int(page, 1))
        page_size = max(1, min(_as_int(page_size, 30), 50))
        result: dict[str, Any] = {
            "success": False,
            "items": [],
            "count": 0,
            "has_more": False,
            "message": "",
        }
        if not self.is_logged_in:
            result["message"] = "请先登录B站账号"
            return result
        # 游标是「max + view_at + business」三件套，必须整体回填：只回填 max、
        # 自己编一个 view_at，翻到后面就会重复或者跳条。第一次用 max=0。
        if page == 1 or not self._history_cursor:
            cursor = {"max": "0", "view_at": "", "business": ""}
        else:
            cursor = dict(self._history_cursor)
        params = {
            "max": str(cursor.get("max") or "0"),
            "type": "archive",
            "ps": str(page_size),
        }
        if cursor.get("view_at"):
            params["view_at"] = str(cursor["view_at"])
        if cursor.get("business"):
            params["business"] = str(cursor["business"])
        payload = self.get_wbi("/x/web-interface/history/cursor", params)
        if not payload:
            result["message"] = "观看历史获取失败，请稍后重试"
            return result
        if payload.get("code") != 0:
            result["message"] = _error_message(payload, "观看历史获取失败")
            return result
        data = payload.get("data") or {}
        entries = data.get("list")
        if not isinstance(entries, list):
            entries = []
        items = [
            self._from_history_item(entry)
            for entry in entries
            if isinstance(entry, dict)
        ]
        cursor_data = data.get("cursor") or {}
        if isinstance(cursor_data, dict):
            self._history_cursor = {
                "max": str(cursor_data.get("max") or ""),
                "view_at": str(cursor_data.get("view_at") or ""),
                "business": str(cursor_data.get("business") or ""),
            }
        else:
            self._history_cursor = None
        self._history_page = page
        result["success"] = True
        result["items"] = items
        result["count"] = len(items)
        result["has_more"] = bool(self._history_cursor and self._history_cursor.get("max"))
        return result

    def space(self, mid: str) -> dict[str, Any]:
        """Return the profile of a user space.

        ``profile`` carries ``mid`` / ``name`` / ``face`` / ``sign`` /
        ``level`` / ``fans`` / ``following`` / ``video_count`` /
        ``like_count``.  The counters come from a second route; when that
        one fails they stay 0.
        """
        mid = str(mid or "").strip()
        profile: dict[str, Any] = {
            "mid": mid,
            "name": "",
            "face": "",
            "sign": "",
            "level": 0,
            "fans": 0,
            "following": 0,
            "video_count": 0,
            "like_count": 0,
        }
        result: dict[str, Any] = {"success": False, "profile": profile, "message": ""}
        if not mid:
            result["message"] = "缺少用户 ID"
            return result
        payload = self.get_wbi(
            "/x/space/wbi/acc/info",
            {
                "mid": mid,
                "token": "",
                "platform": "web",
                "web_location": "1550101",
            },
        )
        if payload and payload.get("code") == 0:
            data = payload.get("data") or {}
            profile["mid"] = str(data.get("mid") or mid)
            profile["name"] = str(data.get("name") or "")
            profile["face"] = _absolute_url(data.get("face"))
            profile["sign"] = str(data.get("sign") or "")
            level = data.get("level")
            if isinstance(level, dict):
                profile["level"] = _as_int(level.get("current_level"))
            else:
                profile["level"] = _as_int(level)
        else:
            # ``/x/space/wbi/acc/info`` answers ``-352``（风控校验失败）when
            # the request comes from a third party client -- the avatar and
            # the name then stay empty and the profile page shows nothing.
            # The card route answers the same fields and is not behind the
            # space risk control, so it is the fallback.
            card = self._get("/x/web-interface/card", {"mid": mid})
            card_data = (card.get("data") or {}) if card and card.get("code") == 0 else {}
            inner = card_data.get("card") if isinstance(card_data.get("card"), dict) else {}
            if not inner:
                result["message"] = (_error_message(payload, "空间信息获取失败")
                                     if payload else "空间信息获取失败，请稍后重试")
                return result
            profile["mid"] = str(inner.get("mid") or mid)
            profile["name"] = str(inner.get("name") or "")
            profile["face"] = _absolute_url(inner.get("face"))
            profile["sign"] = str(inner.get("sign") or "")
            level_info = inner.get("level_info")
            if isinstance(level_info, dict):
                profile["level"] = _as_int(level_info.get("current_level"))
        # 粉丝数、关注数、投稿数在另一个路由上；它失败就留 0。
        upstat = self._get("/x/space/upstat", {"mid": mid})
        if upstat and upstat.get("code") == 0:
            stats = upstat.get("data") or {}
            profile["fans"] = _as_int(stats.get("fans"))
            profile["following"] = _as_int(stats.get("following"))
            profile["video_count"] = _as_int(stats.get("video"))
            profile["like_count"] = _as_int(stats.get("likes"))
        result["success"] = True
        return result

    def space_videos(
        self,
        mid: str,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        """Return one page of the videos a user uploaded.

        Every entry is normalised to ``bvid`` / ``aid`` / ``title`` /
        ``cover`` / ``author`` / ``duration`` / ``play_count`` /
        ``created_at``.
        """
        mid = str(mid or "").strip()
        page = max(1, _as_int(page, 1))
        page_size = max(1, min(_as_int(page_size, 30), 50))
        result: dict[str, Any] = {
            "success": False,
            "items": [],
            "count": 0,
            "has_more": False,
            "message": "",
        }
        if not mid:
            result["message"] = "缺少用户 ID"
            return result
        payload = self.get_wbi(
            "/x/space/wbi/arc/search",
            {
                "mid": mid,
                "pn": page,
                "ps": page_size,
                "order": "pubdate",
                "platform": "web",
                "web_location": "1550101",
            },
        )
        if not payload:
            result["message"] = "投稿列表获取失败，请稍后重试"
            return result
        if payload.get("code") != 0:
            result["message"] = _error_message(payload, "投稿列表获取失败")
            return result
        data = payload.get("data") or {}
        entries: Any = data.get("list")
        if isinstance(entries, dict):
            entries = entries.get("vlist")
        if not isinstance(entries, list):
            entries = []
        items = [
            self._from_space_video(entry)
            for entry in entries
            if isinstance(entry, dict)
        ]
        page_info = data.get("page") or {}
        total = _as_int(page_info.get("count")) if isinstance(page_info, dict) else 0
        result["success"] = True
        result["items"] = items
        result["count"] = len(items)
        result["has_more"] = bool(total > page * page_size) if total else len(items) >= page_size
        return result

    def _from_history_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Normalise one entry of the watched-video list."""
        duration = _duration_seconds(item.get("duration"))
        raw_progress = _as_int(item.get("progress"), -1)
        if raw_progress < 0:
            # -1 表示已经看完。
            progress = 100
        elif duration > 0:
            progress = max(0, min(100, int(raw_progress * 100 / duration)))
        else:
            progress = 0
        # 这里的上传者是一个对象，不是字符串。
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        return {
            "bvid": str(item.get("bvid") or ""),
            "aid": _as_int(item.get("aid")),
            "title": str(item.get("title") or ""),
            "cover": _absolute_url(item.get("cover")),
            "author": str(author.get("name") or item.get("author_name") or ""),
            "author_mid": str(author.get("mid") or ""),
            "duration": duration,
            "viewed_at": _as_int(item.get("view_at")),
            "progress": progress,
            "business": str(item.get("business") or ""),
            "cid": _as_int(item.get("cid")),
        }

    def _from_space_video(self, item: dict[str, Any]) -> dict[str, Any]:
        """Normalise one entry of the uploaded-video list."""
        return {
            "bvid": str(item.get("bvid") or ""),
            "aid": _as_int(item.get("aid")),
            "title": _strip_html(str(item.get("title") or "")),
            "cover": _absolute_url(item.get("pic") or item.get("cover")),
            "author": str(item.get("author") or ""),
            "author_mid": str(item.get("mid") or ""),
            "duration": _duration_seconds(item.get("length") or item.get("duration")),
            "play_count": _as_int(item.get("play")),
            "created_at": _as_int(item.get("created")),
        }

    # ------------------------------------------------------------------
    # 评论
    # ------------------------------------------------------------------
    def comments(self, bvid: str, page: int = 1, sort: str = "hot") -> dict[str, Any]:
        """返回某个视频的评论。

        ``oid`` 要的是 **aid** 而不是 bvid，所以先用 ``video()`` 换一次。

        翻页靠 ``data.cursor.next``：那是下一页的 ``pagination_str``，
        而游标只在上一页的回答里，所以这里按 (bvid, 排序) 把游标记下来；
        缺哪一页的游标，就先把前面那页走一遍。
        """
        result: dict[str, Any] = {
            "success": False,
            "items": [],
            "count": 0,
            "has_more": False,
            "message": "",
        }
        bvid = str(bvid or "").strip()
        if not bvid:
            result["message"] = "缺少视频号"
            return result
        page = max(1, _as_int(page, 1))
        sort = "time" if str(sort or "").lower() == "time" else "hot"
        # mode：2 是按热度，3 是按时间。
        mode = 3 if sort == "time" else 2
        try:
            aid = _as_int(self.video(bvid).get("aid"))
            if not aid:
                result["message"] = "无法获取视频信息，评论加载失败"
                return result
            # 自己发的评论要标出来；拿不到 mid 就当都不是自己发的。
            owner_mid = str(self.account_info().get("mid") or "")
            cache = self._comment_cursor_cache()
            cursor = self._comment_cursor(cache, bvid, sort, page, aid, mode)
            if cursor is None:
                result["message"] = "评论加载失败，请稍后重试"
                return result
            payload = self._comment_request(aid, mode, cursor)
            if not payload:
                result["message"] = "网络异常，评论加载失败，请稍后重试"
                return result
            if payload.get("code") != 0:
                result["message"] = _error_message(payload, "评论加载失败")
                return result
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            replies = data.get("replies")
            replies = replies if isinstance(replies, list) else []
            items = [self._from_reply(item, owner_mid) for item in replies if isinstance(item, dict)]
            # 置顶评论也是主楼评论，放在最前面；同一条不重复出现。
            tops = data.get("top_replies")
            tops = tops if isinstance(tops, list) else []
            seen = {item["rpid"] for item in items}
            pinned = []
            for item in tops:
                if not isinstance(item, dict):
                    continue
                entry = self._from_reply(item, owner_mid)
                if not entry["rpid"] or entry["rpid"] in seen:
                    continue
                seen.add(entry["rpid"])
                pinned.append(entry)
            items = pinned + items
            cursor_data = data.get("cursor") if isinstance(data.get("cursor"), dict) else {}
            # 下一页的游标存起来，下一次翻页直接用。
            next_cursor = _pagination_value(cursor_data.get("next"))
            if next_cursor:
                cache.setdefault(f"{bvid}|{sort}", {})[page + 1] = next_cursor
            result.update({
                "success": True,
                "items": items,
                "count": len(items),
                # 没有下一页游标就翻不动了，跟 is_end 一样算到底。
                "has_more": bool(next_cursor) and not bool(cursor_data.get("is_end")),
                "message": "",
            })
            return result
        except Exception as exc:  # noqa: BLE001 - the UI must never see an exception
            log.warning("bilibili comments failed: %s", exc)
            result["message"] = "网络异常，评论加载失败，请稍后重试"
            return result

    def _comment_request(self, aid: int, mode: int, cursor: str) -> dict[str, Any] | None:
        """发一次 ``/x/v2/reply/main``。``cursor`` 为空表示第一页。"""
        params: dict[str, Any] = {
            "oid": aid,
            "type": 1,
            "mode": mode,
            "plat": 1,
            "seek_rpid": 0,
            "web_location": 1550101,
        }
        if cursor:
            params["pagination_str"] = cursor
        return self.get_wbi("/x/v2/reply/main", params)

    def _comment_cursor(
        self,
        cache: dict[str, dict[int, str]],
        bvid: str,
        sort: str,
        page: int,
        aid: int,
        mode: int,
    ) -> str | None:
        """取出第 ``page`` 页用的 ``pagination_str``。

        第一页没有游标。往后翻时游标来自上一页的回答，所以缓存里
        存的是「第 N 页该带哪个游标」；缺哪一页就把前面那页走一遍。
        """
        cursors = cache.setdefault(f"{bvid}|{sort}", {})
        cursors.setdefault(1, "")
        if page in cursors:
            return cursors[page]
        current = 1
        while current < page:
            # 第 current 页的游标已经知道的话就不用再问一次了。
            if current + 1 not in cursors:
                payload = self._comment_request(aid, mode, cursors.get(current, ""))
                if not payload or payload.get("code") != 0:
                    return None
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                cursor_data = data.get("cursor") if isinstance(data.get("cursor"), dict) else {}
                next_cursor = _pagination_value(cursor_data.get("next"))
                if not next_cursor:
                    return None
                cursors[current + 1] = next_cursor
            current += 1
        return cursors.get(page)

    def _comment_cursor_cache(self) -> dict[str, dict[int, str]]:
        """评论翻页游标的缓存（按实例存放，不写进 ``__init__``）。"""
        cache = getattr(self, "_comment_cursors", None)
        if cache is None:
            cache = {}
            self._comment_cursors = cache
        return cache

    def _from_reply(self, reply: dict[str, Any], owner_mid: str) -> dict[str, Any]:
        """把一条评论（含子评论）归一化成前端要的样子。"""
        member = reply.get("member") if isinstance(reply.get("member"), dict) else {}
        content = reply.get("content") if isinstance(reply.get("content"), dict) else {}
        mid = str(reply.get("mid") or "")
        children_raw = reply.get("replies")
        children_raw = children_raw if isinstance(children_raw, list) else []
        # 子评论最多留 3 条，多出来的前端用 reply_count 显示「查看全部 N 条回复」。
        children = [
            self._from_reply(child, owner_mid)
            for child in children_raw[:3]
            if isinstance(child, dict)
        ]
        return {
            "rpid": str(reply.get("rpid") or ""),
            "mid": mid,
            "uname": str(member.get("uname") or ""),
            "face": _absolute_url(member.get("avatar") or member.get("face")),
            "level": _as_int((member.get("level_info") or {}).get("current_level")
                             if isinstance(member.get("level_info"), dict)
                             else member.get("level")),
            "content": str(content.get("message") or ""),
            "like": _as_int(reply.get("like")),
            "reply_count": _as_int(reply.get("rcount")),
            "ctime": _as_int(reply.get("ctime")),
            "location": str(reply.get("location") or ""),
            "is_owner": bool(owner_mid) and mid == owner_mid,
            "children": children,
        }

    def post_comment(
        self,
        bvid: str,
        content: str,
        parent_rpid: int = 0,
    ) -> dict[str, Any]:
        """发一条评论，或者回复某条评论。

        未登录和空正文都不发请求：服务端对这两种情况给不出有用的
        答案，只会留下一条无意义的写操作。
        """
        result: dict[str, Any] = {"success": False, "rpid": "", "message": ""}
        text = str(content or "").strip()
        if not text:
            result["message"] = "评论不能是空的"
            return result
        bvid = str(bvid or "").strip()
        if not bvid:
            result["message"] = "缺少视频号"
            return result
        if not self.is_logged_in:
            result["message"] = "请先登录B站账号"
            return result
        try:
            aid = _as_int(self.video(bvid).get("aid"))
            if not aid:
                result["message"] = "无法获取视频信息，评论发送失败"
                return result
            parent = max(0, _as_int(parent_rpid))
            payload = self.post_form(
                "/x/v2/reply/add",
                {
                    "type": 1,
                    "oid": aid,
                    "message": text,
                    # 回复时填对方的 rpid，发主楼时是 0。
                    "parent": parent,
                    "buvid3": self._buvid3(),
                },
            )
            if not payload:
                result["message"] = "网络异常，评论发送失败，请稍后重试"
                return result
            if payload.get("code") != 0:
                result["message"] = _error_message(payload, "评论发送失败")
                return result
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            reply = data.get("reply") if isinstance(data.get("reply"), dict) else {}
            result["success"] = True
            result["rpid"] = str(reply.get("rpid") or reply.get("id_str") or "")
            result["message"] = "回复已发布" if parent else "评论已发布"
            return result
        except Exception as exc:  # noqa: BLE001 - the UI must never see an exception
            log.warning("bilibili post_comment failed: %s", exc)
            result["message"] = "网络异常，评论发送失败，请稍后重试"
            return result

    def like_comment(self, rpid: str, like: bool = True) -> dict[str, Any]:
        """给一条评论点赞，或者取消点赞。

        ``action`` 是 1（赞）和 -1（取消赞），跟视频点赞的 1/2 不一样。
        """
        result: dict[str, Any] = {"success": False, "message": ""}
        rpid = str(rpid or "").strip()
        if not rpid:
            result["message"] = "缺少评论 ID"
            return result
        try:
            payload = self.post_form(
                "/x/v2/reply/action",
                {
                    "rpid": rpid,
                    "action": 1 if like else -1,
                },
            )
            if not payload:
                result["message"] = "网络异常，操作失败，请稍后重试"
                return result
            if payload.get("code") != 0:
                result["message"] = _error_message(payload, "操作失败")
                return result
            result["success"] = True
            result["message"] = "已点赞" if like else "已取消点赞"
            return result
        except Exception as exc:  # noqa: BLE001 - the UI must never see an exception
            log.warning("bilibili like_comment failed: %s", exc)
            result["message"] = "网络异常，操作失败，请稍后重试"
            return result

    def _buvid3(self) -> str:
        """取出 ``buvid3``：写接口少了它容易被风控拦下。"""
        value = str(self.cookie_dict().get("buvid3") or "")
        if value:
            return value
        self._ensure_buvid()
        value = str(self.cookie_dict().get("buvid3") or "")
        if value:
            return value
        # ``_ensure_buvid`` 只把 cookie 写进 session 的 jar，所以这里
        # 再从 jar 里找一次。
        jar = getattr(self._session, "cookies", None)
        source = getattr(jar, "cookies", jar)
        try:
            raw = source.get("buvid3") if source is not None else None
        except Exception:  # noqa: BLE001
            raw = None
        value = str(raw or "")
        # jar 里拿到的是 "name=value" 形式的 cookie 对象。
        return value.split("=", 1)[1] if "=" in value else value

def qr_png_data_url(text: str, size: int = 256) -> str:
    """把一段文字画成二维码，返回 ``data:`` 图片地址。

    B站给的二维码「图片」其实是一个网页（``account.bilibili.com`` 的扫码确认页），
    直接塞进 ``<img>`` 是什么都显示不出来。所以这里自己画：二维码的内容就是
    那个地址，用 B站 App 扫它就能确认登录。

    图片是自己写出来的 PNG（:mod:`src.core.png`），不牵进 Pillow。
    """
    payload = str(text or "").strip()
    if not payload:
        return ""
    try:
        import qrcode

        from src.core.png import render_matrix

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        scale = max(1, round(size / len(matrix))) if size else qr.box_size
        png = render_matrix(matrix, scale)
    except Exception as exc:  # noqa: BLE001 - 没有二维码也要能继续登录
        log.warning("bilibili QR render failed: %s", exc)
        return ""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


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


#: 风控与登录相关的错误码 -> 给用户看的中文短句。原始 code 不该出现在
#: 界面上，用户看不懂也不需要知道。
_ERROR_MESSAGES: dict[int, str] = {
    -101: "请先登录B站账号",
    -111: "登录状态已失效，请重新登录后再试",
    -352: "操作被B站风控拦截，请稍后再试",
    -401: "该操作需要二次验证，请稍后再试",
    -104: "当前账号没有权限执行这个操作",
    # 评论专有的两个：内容被审核和发得太频繁。
    12002: "评论内容被审核，暂时发不出去",
    12006: "评论发得太频繁，请稍后再试",
}


def _error_message(payload: dict[str, Any] | None, fallback: str = "操作失败，请稍后再试") -> str:
    """Turn an API answer into a Chinese sentence the user can read.

    The raw code never reaches the UI: the known risk-control codes get
    their own wording, anything else keeps the server's own message when
    it has one.
    """
    if not payload:
        return "网络异常，操作失败，请稍后重试"
    try:
        code = int(payload.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    known = _ERROR_MESSAGES.get(code)
    if known:
        return known
    reason = str(payload.get("message") or "").strip()
    if reason and not reason.lstrip("-").isdigit():
        return reason
    return fallback


def _as_int(value: Any, default: int = 0) -> int:
    """Read a number the API may have sent as a string or as ``None``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _pagination_value(raw: Any) -> str:
    """Turn ``data.cursor.next`` into the ``pagination_str`` to send back.

    ``next`` 是服务端给的 JSON 字符串，而它里面偶尔还套了一层字符串。
    所以先 ``json.loads`` 一次：解出来是字符串就取那层，是 JSON 本身
    就原样送回去。
    """
    if raw is None:
        return ""
    if isinstance(raw, dict | list):
        return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    text = str(raw).strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(parsed, str):
        return parsed.strip()
    return text
