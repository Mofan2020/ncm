"""Pixiv (ピクシブ) client, built on the website's own JSON routes.

A desktop client cannot use the **app** API: it wants an OAuth refresh token
that only the phone app can mint, and the token pair is bound to the app's
client credentials -- the registration flow does not exist for a third party.
The **website** does the same work through ``https://www.pixiv.net/ajax/*``
with nothing but the session cookie, so that is the surface used here:

* reads (search, ranking, user, discovery, feed) work **without** a login;
* the account reads and every write need ``PHPSESSID``;
* writes additionally need the CSRF token, which the site embeds in the home
  page (``__NEXT_DATA__`` → ``api.token``).  It is fetched lazily and once.

Three things in the protocol are easy to get wrong, and each of them fails
*silently* rather than loudly:

* **Images live behind a Referer check.**  ``i.pximg.net`` answers 403 to a
  request that does not carry ``Referer: https://www.pixiv.net/``.  The
  webview cannot set a header on an ``<img>``, so everything goes through
  :mod:`src.core.mediaserver` (see ``PIXIV_IMG_HOSTS``).
* **The envelope.**  ``/ajax/*`` answers ``{error, message, body}`` -- an
  HTTP 200 with ``error: true`` is a failure, and ``body`` is then often an
  empty *array* rather than an object.  ``/ranking.php`` and the two legacy
  PHP routes answer bare JSON with **no** envelope and **snake_case** keys.
* **A like cannot be undone.**  ``/ajax/illusts/like`` has no opposite route,
  so the UI must confirm before sending it.

The client never raises at the caller: list methods answer ``[]``, detail
methods answer ``{}`` and every failure is available as :attr:`last_error`.
An exception here would become a spinner that never stops.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Any

import requests

__all__ = [
    "PixivClient",
    "PixivError",
    "PIXIV_BASE",
    "PIXIV_ACCOUNTS_BASE",
    "PIXIV_REFERER",
    "PIXIV_UA",
    "PIXIV_IMG_HOSTS",
    "PIXIV_NSFW_TAGS",
    "is_nsfw",
    "normalize_work",
    "image_url_for",
]

log = logging.getLogger(__name__)

#: The website itself.  Every JSON route hangs off it.
PIXIV_BASE = "https://www.pixiv.net"

#: The login/registration pages.  A client logs in by *showing* these inside
#: a webview, so the module never posts a password anywhere.
PIXIV_ACCOUNTS_BASE = "https://accounts.pixiv.net"

#: What the image CDN checks before it serves a byte.
PIXIV_REFERER = "https://www.pixiv.net/"

#: Hosts the media server is allowed to fetch on behalf of the interface.
PIXIV_IMG_HOSTS = ("i.pximg.net", "s.pximg.net")

#: A desktop Chrome UA.  pixiv answers a generic "python-requests" UA with the
#: HTML login wall instead of JSON on some routes.
PIXIV_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Tags that mean adult content.  ``xRestrict`` is the authoritative field,
#: but a work can carry the tag without the flag, and the flag is what the
#: site uses for its own blur.  Both are checked.
PIXIV_NSFW_TAGS = frozenset({
    "r-18", "r18", "r-18g", "r18g", "18禁", "成人向", "成人向け",
    "エロ", "ero", "nsfw", "adult",
})

#: ``sl`` (sanity level) at or above this is adult.  The site's own values are
#: 2 (general), 4 (mild) and 6 (restricted).
_NSFW_SANITY_LEVEL = 6

#: Routes are cheap for pixiv, but a page turning over 48 thumbnails should not
#: become 48 requests in 200 ms.  Writes get a wider gap: the account is real.
_MIN_INTERVAL = 0.12
_MIN_WRITE_INTERVAL = 0.6

#: First retry gap; doubles each time.  A module constant so the tests can put
#: it at zero instead of spending two seconds per retry case.
_RETRY_BASE = 0.8

#: Cookies worth remembering.  ``PHPSESSID`` is the session; the rest keep the
#: device fingerprint stable, which is what stops pixiv from treating every
#: restart as a new machine and asking for a password again.
_WANTED_COOKIES = (
    "PHPSESSID",
    "device_token",
    "p_ab_id",
    "p_ab_id_2",
    "p_ab_d_id",
    "c_type",
    "yuid_b",
    "cf_clearance",
    "__cf_bm",
    "privacy_policy_agreement",
    "login_ever",
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S,
)
_GLOBAL_DATA_RE = re.compile(r'<meta name="global-data" content=\'(.*?)\'', re.S)


class PixivError(RuntimeError):
    """A request that the caller asked to have raised (writes only)."""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_tags(raw: Any) -> list[str]:
    """Flatten the three shapes a tag list comes in.

    ``["a"]`` (listing endpoints), ``[{"tag": "a"}]`` (detail) and
    ``{"a": null}`` (some legacy routes) all appear in the same client.
    """
    if isinstance(raw, dict):
        # The detail route wraps the list: {"authorId": ..., "tags": [...]}.
        inner = raw.get("tags")
        if isinstance(inner, list | tuple):
            return _as_tags(inner)
        return [str(k) for k in raw]
    if not isinstance(raw, list | tuple):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            tag = item.get("tag") or item.get("name")
            if tag:
                out.append(str(tag))
    return out


def _nested_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _nested_str(raw: Any, *keys: str) -> str:
    """Walk a chain of dict keys, answering ``""`` as soon as it stops being one.

    pixiv is not consistent about the depth of these: ``region`` is sometimes
    ``{"name": ...}`` and sometimes a plain string, and ``webpage`` is a URL
    on one route and ``{"url": ...}`` on another.  Reaching through with
    ``body["a"]["b"]`` is how a profile page ends up throwing on one account
    out of a hundred.
    """
    node: Any = raw
    for key in keys:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return "" if node is None or isinstance(node, dict) else str(node)


def _plain_str(raw: Any) -> str:
    return raw if isinstance(raw, str) else ""


def is_nsfw(work: dict[str, Any]) -> bool:
    """Whether a work counts as adult.

    ``xRestrict`` is what the account's own NSFW setting gates, so it wins.
    The tag scan is a safety net for works the flag misses, and ``sl`` catches
    the ones the tag list cannot be trusted on.
    """
    if _as_int(work.get("xRestrict") or work.get("x_restrict")) > 0:
        return True
    if _as_int(work.get("sl")) >= _NSFW_SANITY_LEVEL:
        return True
    for tag in work.get("tags") or []:
        if str(tag).strip().lower() in PIXIV_NSFW_TAGS:
            return True
    return False


def normalize_work(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn any of the three artwork shapes into the one the UI reads.

    Search/ranking/bookmark/detail each spell the same work differently:
    ``url`` vs ``urls.thumb``, ``userId`` vs ``user_id``, ``pageCount`` as a
    number or a string.  Keeping that mess out of the frontend is the whole
    point of this function.

    Ad slots come back mixed into listings as ``{"isAdContainer": true}``.
    They are returned with ``id == ""`` so callers can drop them with one
    check, instead of guessing from a missing title.
    """
    if not isinstance(raw, dict):
        return {}
    if raw.get("isAdContainer"):
        return {"id": "", "is_ad": True}

    raw_urls = raw.get("urls")
    urls: dict[str, Any] = raw_urls if isinstance(raw_urls, dict) else {}
    thumb = urls.get("thumb") or raw.get("url") or raw.get("profileImageUrl") or ""
    regular = urls.get("regular") or ""
    original = urls.get("original") or ""
    small = urls.get("small") or ""

    bm = raw.get("bookmarkData")
    bookmark_id = ""
    private = False
    if isinstance(bm, dict):
        bookmark_id = str(bm.get("id") or "")
        private = bool(bm.get("private"))

    pid = raw.get("id") or raw.get("illust_id") or raw.get("illustId")
    uid = raw.get("userId") or raw.get("user_id")
    work = {
        "id": str(pid or ""),
        "title": str(raw.get("title") or ""),
        "type": _as_int(raw.get("illustType", raw.get("illust_type")), 0),
        "x_restrict": _as_int(raw.get("xRestrict", raw.get("x_restrict")), 0),
        "sl": _as_int(raw.get("sl"), 0),
        "ai_type": _as_int(raw.get("aiType", raw.get("ai_type")), 0),
        "thumb": str(thumb),
        "small": str(small),
        "regular": str(regular),
        "original": str(original),
        "user_id": str(uid or ""),
        "user_name": str(raw.get("userName") or raw.get("user_name") or ""),
        "tags": _as_tags(raw.get("tags")),
        "page_count": _as_int(raw.get("pageCount", raw.get("illust_page_count")), 1),
        "width": _as_int(raw.get("width"), 0),
        "height": _as_int(raw.get("height"), 0),
        "create_date": str(raw.get("createDate") or raw.get("create_date") or ""),
        "bookmark_id": bookmark_id,
        "private": private,
        "bookmarked": bool(bookmark_id),
        "bookmark_count": _as_int(raw.get("bookmarkCount", raw.get("bookmark_count")), 0),
        "like_count": _as_int(raw.get("likeCount", raw.get("like_count")), 0),
        "view_count": _as_int(raw.get("viewCount", raw.get("view_count")), 0),
        "description": str(raw.get("description") or ""),
        "rank": _as_int(raw.get("rank"), 0),
    }
    if not work["page_count"]:
        work["page_count"] = 1
    work["nsfw"] = is_nsfw(work)
    return work


def image_url_for(work: dict[str, Any], size: str = "thumb") -> str:
    """Pick one quality out of a normalised work, deriving it when missing.

    Listing endpoints give a thumbnail only; the ``/c/<size>/`` prefix plus a
    filename suffix is all that separates the qualities, so the other levels
    can be reconstructed without another request (see the CDN section of the
    web API notes).  ``original`` cannot be derived when the extension is
    ``.png`` -- pixiv answers 404 for a wrong extension, which is why the
    caller falls back to ``regular``.
    """
    size = size if size in ("thumb", "small", "regular", "original") else "thumb"
    direct = work.get(size) or ""
    if direct:
        return direct
    if size == "thumb":
        return str(work.get("thumb") or work.get("small") or work.get("regular") or "")
    base = str(work.get("thumb") or work.get("small") or work.get("regular") or "")
    if not base:
        return ""
    # Drop the /c/<whatever>/ prefix.
    base = re.sub(r"/c/[^/]+/", "/", base, count=1)
    if size == "regular":
        base = base.replace("/custom-thumb/", "/img-master/")
        base = re.sub(r"_(square1200|custom1200)\.", "_master1200.", base)
        return base
    # original
    base = base.replace("/img-master/", "/img-original/")
    base = base.replace("/custom-thumb/", "/img-original/")
    base = re.sub(r"_(square1200|custom1200|master1200)\.", ".", base)
    return base


class PixivClient:
    """One session against ``www.pixiv.net``.

    The instance is cheap and thread-safe enough for the bridge (one lock
    around the pacemaker and the CSRF fetch).  A cookie is set through
    :meth:`apply_cookie`; until then the client works anonymously, which the
    reads allow.
    """

    def __init__(self, session: Any = None, timeout: int = 20) -> None:
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.last_error = ""
        self._cookie = ""
        # 这个 cookie 已经被 pixiv 判死（401/403）。**存的那份留着**——账号页
        # 要能说「已失效，请重新登录」；只是不再随请求发出去，否则连公开
        # 接口都会一起 401，发现页、搜索、排行榜全部拿不到东西。
        self.session_expired = False
        self._csrf = ""
        self._self: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._last_call = 0.0
        self._last_write = 0.0
        self.session.headers.update({
            "User-Agent": PIXIV_UA,
            "Referer": PIXIV_REFERER,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
        })

    # ---------------------------------------------------------------- session

    @property
    def signed_in(self) -> bool:
        """Whether a session cookie is present.

        This is *not* "the cookie still works" -- only
        :meth:`refresh_self` can answer that, and the caller is expected not
        to call it on every render.
        """
        return (bool(self._cookie) and "PHPSESSID=" in self._cookie
                and not self.session_expired)

    def apply_cookie(self, cookie: str) -> None:
        """Install a cookie string (``k=v; k=v``) and forget cached state."""
        cookie = str(cookie or "").strip()
        with self._lock:
            self._cookie = cookie
            self.session_expired = False
            self._csrf = ""
            self._self = {}
            if cookie:
                self.session.headers["Cookie"] = cookie
            else:
                self.session.headers.pop("Cookie", None)

    def drop_expired_cookie(self) -> None:
        """不再把这个 cookie 发出去，但把它留着给界面看。

        pixiv 对一个失效的 ``PHPSESSID`` 不只拒绝写操作：推荐流、搜索、排行榜
        这些**公开接口**也一起回 401。带着死 cookie 就等于把整个 Pixiv 关掉，
        而界面上只看到「没有作品」——连原因都没有。摘掉之后退化成游客，这些
        接口本来就能用。
        """
        with self._lock:
            self.session_expired = True
            self._csrf = ""
            self._self = {}
            self.session.headers.pop("Cookie", None)

    def cookie_string(self) -> str:
        """The cookie as stored -- what the settings file keeps."""
        return self._cookie

    @staticmethod
    def _cookie_triples(item: Any) -> list[tuple[str, str, str]]:
        """把 pywebview 给的一个「cookie」拆成 (名字, 值, 域名)。

        **它给的不是字典。** ``webview.create_cookie()`` 返回的是
        ``SimpleCookie``——「cookie 名字 → Morsel」的映射，而且 SimpleCookie
        是 ``dict`` 的子类。按 ``item.get("name")`` 去取永远拿到 None，
        于是每一个 cookie 都被当成「没有名字」扔掉：窗口里明明登好了，
        程序一个 cookie 都读不到。这就是「登录成功但拿不到 cookie」。

        三种形状都认：SimpleCookie（现在这种）、``{name, value, ...}``
        字典（老版本 / 别的后端）、带 ``.name`` 属性的对象。
        """
        found: list[tuple[str, str, str]] = []
        items: Any = getattr(item, "items", None)
        if callable(items):
            try:
                pairs: list[tuple[Any, Any]] = list(items())
            except Exception:  # noqa: BLE001 - 形状不对就往下试
                pairs = []
            # SimpleCookie：key 是 cookie 名字，值是 Morsel（.key/.value/['domain']）
            if pairs and all(hasattr(value, "key") for _, value in pairs):
                for key, morsel in pairs:
                    name = str(getattr(morsel, "key", "") or key or "").strip()
                    value = str(getattr(morsel, "value", "") or "")
                    domain = ""
                    try:
                        domain = str(morsel["domain"] or "")
                    except Exception:  # noqa: BLE001 - 没有 domain 就当没有
                        domain = ""
                    found.append((name, value, domain))
                return found
            # 普通字典：{name, value, domain}
            if isinstance(item, dict) and "name" in item:
                found.append((
                    str(item.get("name") or "").strip(),
                    str(item.get("value") or ""),
                    str(item.get("domain") or ""),
                ))
                return found
        # 带属性的对象
        if hasattr(item, "name"):
            found.append((
                str(getattr(item, "name", "") or "").strip(),
                str(getattr(item, "value", "") or ""),
                str(getattr(item, "domain", "") or ""),
            ))
        return found

    @staticmethod
    def cookie_from_webview(cookies: Any) -> str:
        """Build the cookie string out of what ``webview.get_cookies()`` gives.

        The webview hands over **every** cookie of the machine's cookie store
        for that domain -- ``_ga``, ``__cf_bm``, ad ids and whatnot.  Only the
        ones pixiv itself needs are kept: a cookie jar that grows without
        bound is both a privacy problem and a request-header that eventually
        gets rejected for its size.
        """
        pairs: list[str] = []
        seen: set[str] = set()
        for item in cookies or []:
            for name, value, domain in PixivClient._cookie_triples(item):
                name = str(name or "").strip()
                if not name or name in seen:
                    continue
                # 域名过滤只挡「明确不是 pixiv」的：有些后端不给 domain，
                # 那时候名字白名单已经是足够严的筛子了。
                if domain and "pixiv.net" not in str(domain):
                    continue
                if name not in _WANTED_COOKIES:
                    continue
                seen.add(name)
                pairs.append(f"{name}={value}")
        # PHPSESSID first: it is the one that matters if anything truncates.
        pairs.sort(key=lambda pair: 0 if pair.startswith("PHPSESSID=") else 1)
        return "; ".join(pairs)

    def refresh_self(self) -> dict[str, Any]:
        """Read the signed-in user (and the CSRF token) from the home page.

        Everything the account panel needs is already in the page the site
        sends a browser: ``__NEXT_DATA__`` carries a serialised preload state
        with ``api.token`` (CSRF) and ``userData.self``.  One request instead
        of three, and no extra route for pixiv to change under us.
        """
        self.last_error = ""
        html, error = self._get_text(PIXIV_BASE + "/")
        if error:
            self.last_error = error
            return {}
        state = self._parse_preload(html)
        if not state:
            self._self = {}
            if self._cookie:
                # 读不出登录态基本就是 cookie 死了。别让它继续跟着每个请求
                # 走——公开接口会被它一起带下水。
                self.drop_expired_cookie()
                self.last_error = "无法读取登录状态，可能是 cookie 已失效。"
            return {}
        token = str((state.get("api") or {}).get("token") or "")
        user_data = state.get("userData") or {}
        self_raw = user_data.get("self") if isinstance(user_data, dict) else None
        with self._lock:
            self._csrf = token
        if not isinstance(self_raw, dict):
            self._self = {}
            if self._cookie:
                self.drop_expired_cookie()
                self.last_error = "登录状态已失效，请重新登录。"
            return {}
        account = {
            "id": str(self_raw.get("id") or ""),
            "name": str(self_raw.get("name") or ""),
            "account": str(self_raw.get("pixivId") or ""),
            "avatar": str(self_raw.get("profileImageUrl")
                          or self_raw.get("image") or ""),
            "comment": str(self_raw.get("comment") or ""),
            "premium": bool(self_raw.get("premium")),
            "mail_authorized": bool(self_raw.get("isMailAuthorized")
                                    or self_raw.get("isEmailVerified")),
            "following": _as_int(self_raw.get("following"), 0),
        }
        self._self = account
        return dict(account)

    @property
    def account(self) -> dict[str, Any]:
        """The last :meth:`refresh_self` result (``{}`` before the first)."""
        return dict(self._self)

    @staticmethod
    def _parse_preload(html: str) -> dict[str, Any]:
        """Pull the serialised preload state out of the home page.

        Two shapes exist depending on how pixiv is deploying that week: the
        Next.js one inside a ``<script id="__NEXT_DATA__">`` (whose
        ``serverSerializedPreloadedState`` is itself a JSON *string*), and the
        older ``<meta name="global-data">``.  Both are tried; a page that has
        neither means the client was served the login wall.
        """
        if not html:
            return {}
        match = _NEXT_DATA_RE.search(html)
        if match:
            try:
                data = json.loads(match.group(1))
            except (ValueError, TypeError):
                data = {}
            page_props = ((data.get("props") or {}).get("pageProps") or {})
            serialized = page_props.get("serverSerializedPreloadedState")
            if isinstance(serialized, str):
                try:
                    state = json.loads(serialized)
                except (ValueError, TypeError):
                    state = {}
                if isinstance(state, dict) and state:
                    return state
            if isinstance(page_props.get("initialState"), dict):
                return page_props["initialState"]
        match = _GLOBAL_DATA_RE.search(html)
        if match:
            try:
                data = json.loads(match.group(1))
            except (ValueError, TypeError):
                return {}
            if isinstance(data, dict):
                # The legacy page keeps the account **flat**: userId /
                # userName / userAccount sit next to the token, with no
                # `self` wrapper at all.  Some deployments wrap it anyway, so
                # all three shapes are folded into `userData.self`.
                account = data.get("userData")
                if not isinstance(account, dict):
                    account = {}
                if isinstance(account.get("self"), dict):
                    self_obj = account["self"]
                elif account:
                    self_obj = account
                else:
                    self_obj = {
                        "id": data.get("userId", ""),
                        "name": data.get("userName", ""),
                        "pixivId": data.get("userAccount", ""),
                    }
                return {"api": {"token": data.get("token", "")},
                        "userData": {"self": self_obj}}
        return {}

    # ------------------------------------------------------------------ http

    def _pace(self, *, write: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            gap = _MIN_WRITE_INTERVAL if write else _MIN_INTERVAL
            last = self._last_write if write else self._last_call
            wait = gap - (now - last)
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            self._last_call = now
            if write:
                self._last_write = now

    def _get_text(self, url: str) -> tuple[str, str]:
        """A GET that answers text.  Returns ``(text, error)``."""
        self._pace()
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            return "", f"网络错误：{exc}"
        if response.status_code != 200:
            return "", f"服务器回了 {response.status_code}。"
        return response.text, ""

    def _request(self, method: str, path: str, *,
                 params: Any = None,
                 json_body: Any = None,
                 form: dict[str, Any] | None = None,
                 write: bool = False,
                 _allow_guest: bool = True) -> tuple[Any, str]:
        """One request, with the retry that pixiv's 429/5xx deserves.

        A 429 or a 5xx is worth two more tries with a growing gap; a 4xx is
        not -- it never becomes a 200 by waiting, and retrying it just makes
        the account look abusive.  That includes the CSRF rejection: the
        caller re-fetches the token instead (see :meth:`_ensure_csrf`).
        """
        url = path if path.startswith("http") else PIXIV_BASE + path
        headers: dict[str, str] = {}
        if write and not self.signed_in:
            # Without a session the POST comes back 401 and reads as "your
            # login expired", which is confusing on a first run: there was
            # never a login.  Say the true thing instead.
            self.last_error = "先登录 Pixiv 账号。"
            return None, self.last_error
        if write:
            token = self._ensure_csrf()
            if not token:
                return None, self.last_error or "没有登录，无法修改账号。"
            headers["X-CSRF-TOKEN"] = token
            headers["Origin"] = PIXIV_BASE
            headers["Content-Type"] = ("application/x-www-form-urlencoded"
                                       if form is not None else "application/json")
        last = ""
        for attempt in range(3):
            self._pace(write=write)
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, data=form,
                    headers=headers or None, timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                last = f"网络错误：{exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json(), ""
                    except ValueError:
                        return None, "服务器回的不是 JSON。"
                if response.status_code in (429, 500, 502, 503, 504):
                    last = f"服务器回了 {response.status_code}，稍后再试。"
                elif response.status_code in (401, 403):
                    # **过期的 cookie 会把公开接口也一起弄死。** 摘掉它，同一
                    # 个请求当游客再来一次：读接口本身就允许匿名，退一步至少
                    # 让发现页有东西看，同时把「已失效」留给账号页去说。
                    if self._cookie and not self.session_expired and not write:
                        self.drop_expired_cookie()
                        log.info("pixiv: the session cookie was rejected, "
                                 "carrying on as a guest")
                        return self._request(method, path, params=params,
                                             json_body=json_body, form=form,
                                             write=write, _allow_guest=False)
                    return None, "登录已失效，请重新登录。"
                else:
                    return None, f"服务器回了 {response.status_code}。"
            if attempt < 2:
                time.sleep(_RETRY_BASE * (2 ** attempt))
        return None, last

    def _ensure_csrf(self) -> str:
        """The CSRF token, fetched once and reused.

        A write without it answers ``400``; the token expires with the
        session, so it is dropped whenever the cookie changes.
        """
        with self._lock:
            if self._csrf:
                return self._csrf
        self.refresh_self()
        with self._lock:
            return self._csrf

    def _ajax(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """A read against ``/ajax/*``, unwrapping the envelope.

        Returns ``body`` or ``None``.  ``error: true`` with an HTTP 200 is the
        normal way pixiv says "no" -- for example a bookmark list for an
        account that is not signed in -- so it is checked before the body.
        """
        self.last_error = ""
        payload, error = self._request("GET", path, params=params)
        if error:
            self.last_error = error
            return None
        if not isinstance(payload, dict):
            self.last_error = "回答格式不对。"
            return None
        if payload.get("error"):
            message = str(payload.get("message") or "").strip()
            self.last_error = message or "请求被拒绝。"
            return None
        return payload.get("body")

    # ----------------------------------------------------------------- reads

    def search(self, word: str, *, page: int = 1, mode: str = "all",
               s_mode: str = "s_tag", order: str = "date_d",
               kind: str = "artworks", ai_type: int = 0) -> dict[str, Any]:
        """Search artworks.

        ``mode="safe"`` asks the server to leave adult works out; the client
        filters again afterwards, because the server side only honours it for
        an account whose own NSFW setting is on.
        """
        word = str(word or "").strip()
        if not word:
            return {"works": [], "total": 0, "related_tags": [], "word": ""}
        route = {
            "artworks": "/ajax/search/artworks/",
            "illustrations": "/ajax/search/illustrations/",
            "manga": "/ajax/search/manga/",
        }.get(kind, "/ajax/search/artworks/")
        params: dict[str, Any] = {
            "word": word,
            "order": order,
            "mode": mode,
            "p": max(1, int(page)),
            "s_mode": s_mode,
            "lang": "zh",
        }
        if ai_type:
            params["ai_type"] = 1
        body = self._ajax(route + urllib.parse.quote(word), params)
        if not isinstance(body, dict):
            return {"works": [], "total": 0, "related_tags": [], "word": word,
                    "error": self.last_error}
        bucket = body.get("illustManga") or body.get("illust") or body.get("manga") or {}
        works = [normalize_work(item) for item in (bucket.get("data") or [])]
        works = [work for work in works if work.get("id")]
        return {
            "works": works,
            "total": _as_int(bucket.get("total"), 0),
            "related_tags": [str(tag) for tag in (body.get("relatedTags") or [])],
            "word": word,
        }

    def illust(self, pid: str) -> dict[str, Any]:
        """One artwork, fully spelled out (tags with translations, urls)."""
        self.last_error = ""
        body = self._ajax(f"/ajax/illust/{pid}", {"full": 1})
        if not isinstance(body, dict):
            return {}
        work = normalize_work(body)
        # The detail route carries its own like/bookmark state; the listing
        # shape does not, so this is the only place `is_liked` exists.
        work["is_liked"] = bool(body.get("isLiked") or body.get("likeData"))
        work["comment_count"] = _as_int(body.get("commentCount"), 0)
        tag_list = _as_tags(body.get("tags"))
        work["tags"] = tag_list
        work["tag_details"] = [
            {"tag": str(item.get("tag")), "translation": (item.get("translation") or {}).get("zh")
             or (item.get("translation") or {}).get("en") or ""}
            for item in ((body.get("tags") or {}).get("tags") or [])
            if isinstance(item, dict) and item.get("tag")
        ] if isinstance(body.get("tags"), dict) else []
        work["nsfw"] = is_nsfw(work)
        return work

    def illust_pages(self, pid: str) -> list[dict[str, Any]]:
        """Every page of an artwork, at every quality.

        A single-page work still comes back as one entry -- the pager in the
        detail view keys off ``len(pages) > 1`` rather than a separate call.
        """
        body = self._ajax(f"/ajax/illust/{pid}/pages")
        if not isinstance(body, list):
            return []
        pages: list[dict[str, Any]] = []
        for index, item in enumerate(body):
            urls = (item or {}).get("urls") or {}
            pages.append({
                "index": index,
                "thumb": str(urls.get("thumb_mini") or urls.get("small") or ""),
                "small": str(urls.get("small") or ""),
                "regular": str(urls.get("regular") or ""),
                "original": str(urls.get("original") or ""),
                "width": _as_int((item or {}).get("width"), 0),
                "height": _as_int((item or {}).get("height"), 0),
            })
        return pages

    def ugoira_meta(self, pid: str) -> dict[str, Any]:
        """Frame delays and the zip address of an animated work.

        Answers ``{}`` for a still image -- pixiv replies 404 with
        「您所指定的ID不是动图」, which is a normal answer, not a failure.
        """
        body = self._ajax(f"/ajax/illust/{pid}/ugoira_meta")
        if not isinstance(body, dict):
            return {}
        frames = [{"file": str(frame.get("file") or ""),
                   "delay": _as_int(frame.get("delay"), 100)}
                  for frame in (body.get("frames") or []) if isinstance(frame, dict)]
        return {
            "src": str(body.get("src") or ""),
            "original_src": str(body.get("originalSrc") or ""),
            "mime_type": str(body.get("mime_type") or "image/jpeg"),
            "frames": frames,
        }

    def user(self, uid: str) -> dict[str, Any]:
        """A user profile, including whether the signed-in account follows."""
        body = self._ajax(f"/ajax/user/{uid}", {"full": 1})
        if not isinstance(body, dict):
            return {}
        return {
            "id": str(body.get("userId") or uid),
            "name": str(body.get("name") or ""),
            "avatar": str(body.get("imageBig") or body.get("image") or ""),
            "comment": str(body.get("comment") or ""),
            "following": _as_int(body.get("following"), 0),
            "followed": bool(body.get("isFollowed")),
            "followed_back": bool(body.get("followedBack")),
            "premium": bool(body.get("premium")),
            # **作品数不在这条回答里.** `/ajax/user/{uid}?full=1` carries no
            # `illusts` / `manga` count at all (both are null); the numbers
            # come from `user_work_ids()`.  Reading them here used to answer
            # 0 for every artist.
            "region": _nested_str(body.get("region"), "name"),
            "twitter": _nested_str(_nested_dict(body.get("social")), "twitter", "url"),
            # `webpage` is a URL *string* on some answers and {"url": ...} on
            # others -- reading it as a dict crashes the whole profile.
            "website": _nested_str(body.get("webpage"), "url") or _plain_str(body.get("webpage")),
        }

    def user_work_ids(self, uid: str) -> dict[str, list[str]]:
        """Every work id of a user, newest first, split by kind.

        The site pages a profile client-side: one call lists the ids, then a
        second call fetches the details for the slice on screen.  Doing it in
        one call per page would re-download the whole id list every time.
        """
        body = self._ajax(f"/ajax/user/{uid}/profile/all")
        if not isinstance(body, dict):
            return {"illust": [], "manga": []}
        def ids(key: str) -> list[str]:
            raw = body.get(key) or {}
            if isinstance(raw, dict):
                values = list(raw.keys())
            elif isinstance(raw, list | tuple):
                values = [str(item) for item in raw]
            else:
                values = []
            return sorted({str(v) for v in values if str(v)}, key=lambda v: -_as_int(v))
        return {"illust": ids("illusts"), "manga": ids("manga")}

    def user_works(self, uid: str, *, kind: str = "illust",
                   offset: int = 0, limit: int = 48) -> dict[str, Any]:
        """One page of a user's works, in the shape the grid wants."""
        offset = max(0, int(offset))
        limit = max(1, min(100, int(limit)))
        all_ids = self.user_work_ids(uid)
        ids = all_ids.get("manga" if kind == "manga" else "illust", [])
        if not ids:
            return {"works": [], "total": 0, "has_more": False}
        slice_ = ids[offset:offset + limit]
        if not slice_:
            return {"works": [], "total": len(ids), "has_more": False}
        params: list[tuple[str, str]] = [("ids[]", pid) for pid in slice_]
        params += [
            ("work_category", "manga" if kind == "manga" else "illustManga"),
            ("is_first_page", "1" if offset == 0 else "0"),
            ("lang", "zh"),
        ]
        self.last_error = ""
        payload, error = self._request(
            "GET", f"/ajax/user/{uid}/illusts", params=params,
        )
        if error:
            self.last_error = error
            return {"works": [], "total": len(ids), "has_more": False}
        body = (payload or {}).get("body") if isinstance(payload, dict) else None
        raw_works = []
        if isinstance(body, dict):
            # keyed by id, and NOT in the order asked for
            ordered = [body.get(pid) for pid in slice_]
            raw_works = [item for item in ordered if isinstance(item, dict)]
            if not raw_works:
                raw_works = [item for item in body.values() if isinstance(item, dict)]
        works = [normalize_work(item) for item in raw_works]
        works = [work for work in works if work.get("id")]
        return {
            "works": works,
            "total": len(ids),
            "has_more": offset + limit < len(ids),
        }

    def bookmarks(self, *, uid: str = "", tag: str = "", offset: int = 0,
                  limit: int = 48, rest: str = "show") -> dict[str, Any]:
        """Bookmarks of an account -- yours when ``uid`` is empty.

        ``rest`` is ``show`` (public), ``hide`` (private) or ``""`` for both;
        only the owner may ask for the private ones, which is why the account
        id has to come from :meth:`refresh_self`.
        """
        target = str(uid or self._self.get("id") or "").strip()
        if not target:
            self.last_error = "先登录才能看收藏。"
            return {"works": [], "total": 0, "has_more": False}
        params = {
            "tag": str(tag or ""),
            "offset": max(0, int(offset)),
            "limit": max(1, min(100, int(limit))),
            "rest": rest,
            "lang": "zh",
        }
        body = self._ajax(f"/ajax/user/{target}/illusts/bookmarks", params)
        if not isinstance(body, dict):
            return {"works": [], "total": 0, "has_more": False,
                    "error": self.last_error}
        works = [normalize_work(item) for item in (body.get("works") or [])]
        works = [work for work in works if work.get("id")]
        total = _as_int(body.get("total"), 0)
        return {
            "works": works,
            "total": total,
            "has_more": params["offset"] + len(works) < total,
        }

    def following(self, *, uid: str = "", offset: int = 0, limit: int = 24,
                  rest: str = "show") -> dict[str, Any]:
        """The accounts someone follows."""
        target = str(uid or self._self.get("id") or "").strip()
        if not target:
            self.last_error = "先登录才能看关注列表。"
            return {"users": [], "total": 0, "has_more": False}
        params = {"offset": max(0, int(offset)),
                  "limit": max(1, min(100, int(limit))),
                  "rest": rest, "lang": "zh"}
        body = self._ajax(f"/ajax/user/{target}/following", params)
        if not isinstance(body, dict):
            return {"users": [], "total": 0, "has_more": False,
                    "error": self.last_error}
        users = []
        for item in body.get("users") or []:
            if not isinstance(item, dict):
                continue
            users.append({
                "id": str(item.get("userId") or ""),
                "name": str(item.get("userName") or ""),
                "avatar": str(item.get("profileImageUrl") or ""),
                "comment": str(item.get("comment") or ""),
                "followed": bool(item.get("isFollowed")),
            })
        total = _as_int(body.get("total"), 0)
        return {"users": users, "total": total,
                "has_more": params["offset"] + len(users) < total}

    def feed(self, *, page: int = 1, mode: str = "all") -> dict[str, Any]:
        """New works from the accounts you follow."""
        body = self._ajax("/ajax/follow_latest/illust",
                          {"p": max(1, int(page)), "mode": mode, "lang": "zh"})
        if not isinstance(body, dict):
            return {"works": [], "has_more": False, "error": self.last_error}
        thumbs = (body.get("thumbnails") or {}).get("illust") or []
        works = [normalize_work(item) for item in thumbs]
        works = [work for work in works if work.get("id")]
        page_info = body.get("page") or {}
        return {"works": works,
                "has_more": not bool(page_info.get("isLastPage"))}

    def ranking(self, *, mode: str = "daily", content: str = "illust",
                page: int = 1, date: str = "") -> dict[str, Any]:
        """The public ranking.

        ``/ranking.php`` is a legacy route: no envelope and snake_case keys.
        ``date`` has to be earlier than today (Tokyo time) or pixiv answers
        the current day anyway.
        """
        params: dict[str, Any] = {
            "format": "json", "mode": mode, "content": content,
            "p": max(1, int(page)),
        }
        if date:
            params["date"] = str(date)
        self.last_error = ""
        payload, error = self._request("GET", "/ranking.php", params=params)
        if error or not isinstance(payload, dict):
            self.last_error = error or "排行榜读取失败。"
            return {"works": [], "has_more": False}
        works = []
        for item in payload.get("contents") or []:
            if not isinstance(item, dict):
                continue
            works.append({
                "id": str(item.get("illust_id") or ""),
                "title": str(item.get("title") or ""),
                "thumb": str(item.get("url") or ""),
                "rank": _as_int(item.get("rank"), 0),
                "yes_rank": _as_int(item.get("yes_rank"), 0),
                "user_id": str(item.get("user_id") or ""),
                "user_name": str(item.get("user_name") or ""),
                "tags": [str(tag) for tag in (item.get("tags") or [])],
                "type": _as_int(item.get("illust_type"), 0),
                "page_count": _as_int(item.get("illust_page_count"), 1),
                "x_restrict": 0,
                "sl": 0,
                "nsfw": False,
            })
        works = [work for work in works if work["id"]]
        for work in works:
            work["nsfw"] = is_nsfw(work)
        next_page = payload.get("next")
        return {"works": works,
                "has_more": bool(next_page) and str(next_page) != "0",
                "date": str(payload.get("date") or "")}

    def discovery(self, *, mode: str = "safe", limit: int = 60) -> list[dict[str, Any]]:
        """发现页：三个来源依次试，失败的原因全留下。

        1. ``/ajax/discovery/artworks`` —— 新的推荐流，**要会话**，游客回 400。
        2. ``/ajax/illust/discovery`` —— 老接口，游客能用，最多 18 条。
        3. ``/ranking.php?mode=daily`` —— 排行榜本来就是公开的，最后的兜底。

        **失败不能被吞掉。** 以前两个接口都拿不到就返回空列表，界面只能说
        「没有作品」，分不清是没登录、网络不通还是接口改了。现在每次尝试的
        错误都攒进 ``last_error``，界面能照原样显示出来。
        """
        self.last_error = ""
        problems: list[str] = []

        body = self._ajax("/ajax/discovery/artworks",
                          {"mode": mode, "limit": max(1, min(60, int(limit))),
                           "lang": "zh"})
        thumbs: list[Any] = []
        if isinstance(body, dict):
            thumbs = ((body.get("thumbnails") or {}).get("illust") or [])
        works = [w for w in (normalize_work(item) for item in thumbs) if w.get("id")]
        if works:
            return works
        problems.append("推荐流：" + (self.last_error or "没有返回作品"))

        body = self._ajax("/ajax/illust/discovery", {"mode": mode, "max": 18})
        if isinstance(body, dict):
            thumbs = body.get("illusts") or []
        works = [w for w in (normalize_work(item) for item in thumbs) if w.get("id")]
        if works:
            return works
        problems.append("发现老接口：" + (self.last_error or "没有返回作品"))

        # 排行榜是公开的，游客也能看。拿来兜底比一片空白强得多，但要在数据
        # 上标一句，界面才能说清楚「这是排行榜的内容」。
        ranking = self.ranking(mode="daily", content="illust", page=1)
        works = []
        for item in ranking.get("works") or []:
            if not item.get("id"):
                continue
            works.append({
                **item,
                "page_count": 1,
                "from_ranking": True,
            })
        if works:
            self.last_error = "推荐流没取到（" + "；".join(problems) + "），下面是排行榜。"
            return works
        problems.append("排行榜：" + (self.last_error or "没有返回作品"))
        self.last_error = "；".join(problems)
        return []

    # ---------------------------------------------------------------- writes

    def bookmark_add(self, pid: str, *, private: bool = False,
                     tags: list[str] | None = None,
                     comment: str = "") -> dict[str, Any]:
        """Bookmark a work, publicly or privately."""
        self.last_error = ""
        payload, error = self._request(
            "POST", "/ajax/illusts/bookmarks/add",
            json_body={
                "illust_id": _as_int(pid, 0),
                "restrict": 1 if private else 0,
                "comment": str(comment or ""),
                "tags": list(tags or []),
            },
            write=True,
        )
        if error or not isinstance(payload, dict) or payload.get("error"):
            self.last_error = error or str((payload or {}).get("message") or "收藏失败。")
            return {"ok": False, "message": self.last_error}
        return {"ok": True, "message": ""}

    def bookmark_delete(self, bookmark_id: str) -> dict[str, Any]:
        """Remove a bookmark by its **bookmark** id, not the artwork id.

        The two are different numbers and the route only accepts the former;
        sending the artwork id answers ``400`` with no explanation.  The id
        comes from ``bookmarkData.id`` on the artwork (``bookmark_id`` once
        normalised).
        """
        self.last_error = ""
        bookmark_id = str(bookmark_id or "").strip()
        if not bookmark_id:
            return {"ok": False, "message": "这条收藏没有 id，无法取消。"}
        payload, error = self._request(
            "POST", "/ajax/illusts/bookmarks/delete",
            form={"bookmark_id": bookmark_id},
            write=True,
        )
        if error:
            self.last_error = error
            return {"ok": False, "message": error}
        # The legacy route answers a bare JSON body with no envelope.
        if isinstance(payload, dict) and payload.get("error"):
            self.last_error = str(payload.get("message") or "取消收藏失败。")
            return {"ok": False, "message": self.last_error}
        return {"ok": True, "message": ""}

    def like(self, pid: str) -> dict[str, Any]:
        """Like a work.  **There is no unlike** -- the caller must say so."""
        self.last_error = ""
        payload, error = self._request(
            "POST", "/ajax/illusts/like",
            json_body={"illust_id": str(pid)},
            write=True,
        )
        if error or not isinstance(payload, dict) or payload.get("error"):
            self.last_error = error or str((payload or {}).get("message") or "点赞失败。")
            return {"ok": False, "message": self.last_error}
        body = payload.get("body") or {}
        already = bool(body.get("is_liked")) if isinstance(body, dict) else False
        return {"ok": True, "already": already, "message": ""}

    def follow(self, uid: str, *, private: bool = False) -> dict[str, Any]:
        """Follow an account (the legacy PHP route, which is still the one)."""
        self.last_error = ""
        payload, error = self._request(
            "POST", "/bookmark_add.php",
            form={"mode": "add", "type": "user", "user_id": str(uid),
                  "tag": "", "restrict": "1" if private else "0",
                  "format": "json"},
            write=True,
        )
        if error:
            self.last_error = error
            return {"ok": False, "message": error}
        return {"ok": True, "message": ""}

    def unfollow(self, uid: str) -> dict[str, Any]:
        """Stop following an account."""
        self.last_error = ""
        payload, error = self._request(
            "POST", "/rpc_group_setting.php",
            form={"mode": "del", "type": "bookuser", "id": str(uid)},
            write=True,
        )
        if error:
            self.last_error = error
            return {"ok": False, "message": error}
        if isinstance(payload, dict) and payload.get("error"):
            self.last_error = str(payload.get("message") or "取消关注失败。")
            return {"ok": False, "message": self.last_error}
        return {"ok": True, "message": ""}
