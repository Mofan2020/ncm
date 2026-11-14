"""Pixiv 的界面层：把 :mod:`src.core.pixiv` 翻成前端读得懂的形状。

这一层做三件客户端不做的事：

* **登录**——开一个真正的 pixiv 登录页给用户登（用户名密码不经过我们），
  登完从 webview 自己的 cookie 里把会话取出来。注册走同一个窗口，因为
  登录页上有「注册」的入口，跳过去还是同一个站。
* **成人内容过滤**——``hide`` 直接不返回，``blur`` 返回但打上 ``blurred``
  让界面盖一层，``show`` 原样给。判定用 :func:`src.core.pixiv.is_nsfw`。
* **缓存**——搜索结果、作品详情、用户资料这一类的读接口走
  :class:`~src.core.metacache.MetaCache`；收藏、关注流、账号状态**不缓存**，
  它们跟着账号变，不跟着时间变。

写接口只有四样：收藏、取消收藏、点赞、关注/取关。点赞在 pixiv 那边是
**不可撤销**的，界面上必须先问一句。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from src.core.pixiv import (
    PixivClient,
    image_url_for,
    is_nsfw,
    normalize_work,
)

__all__ = ["PixivBridge", "PIXIV_LOGIN_URL", "PIXIV_SIGNUP_URL"]

log = logging.getLogger(__name__)

#: 登录和注册都在这两个页面上，参数让它登完回作品的首页。
PIXIV_LOGIN_URL = (
    "https://accounts.pixiv.net/login?return_to="
    "https%3A%2F%2Fwww.pixiv.net%2F&lang=zh"
)
PIXIV_SIGNUP_URL = (
    "https://accounts.pixiv.net/signup?return_to="
    "https%3A%2F%2Fwww.pixiv.net%2F&lang=zh"
)

#: 多久看一次登录窗口的 cookie。太快没意义（登录是个几秒的手动过程），
#: 太慢用户会以为按了没反应。
COOKIE_READ_TIMEOUT = 6.0
COOKIE_POLL_SECONDS = 1.5

#: 十分钟还没登完就把窗口关掉：留在那儿只会被忘记，下次开机突然冒出来。
LOGIN_TIMEOUT_SECONDS = 600.0

#: 缓存寿命（秒）。收藏和关注流不在表里——它们不缓存。
_CACHE_NS = "pixiv2"   # 换一代就把旧的空结果清空了，不用等 TTL

_TTL = {
    "search": 600.0,
    "illust": 3600.0,
    "pages": 3600.0,
    "user": 1800.0,
    "userworks": 1800.0,
    "ranking": 900.0,
    "related": 1800.0,
    "discovery": 900.0,
}


class PixivBridge:
    """前端看到的 Pixiv。"""

    def __init__(self, session: Any = None, cache: Any = None,
                 media: Any = None,
                 on_event: Callable[[str, Any], None] | None = None) -> None:
        self._session = session
        self._cache = cache
        #: 媒体服务器。缩略图必须走它的 ``/pixiv`` 代理（i.pximg.net 要
        #: Referer，而 <img> 设不了请求头）。可以没有——那时前端直接用原地址。
        self.media = media
        self._on_event = on_event
        self._lock = threading.RLock()
        self._login_state = "idle"      # idle|waiting|ok|failed|cancelled
        self._login_message = ""
        self._login_window: Any = None
        self._login_thread: threading.Thread | None = None
        # 读 cookie 的那个子线程：上一次没回来就不再叠一个。
        self._cookie_reader: threading.Thread | None = None
        self._login_cancel = threading.Event()

    # ------------------------------------------------------------ 依赖（延迟）
    @property
    def session(self) -> Any:
        if self._session is None:
            from src.auth.pixiv_login import get_pixiv_session

            self._session = get_pixiv_session()
        return self._session

    @property
    def client(self) -> PixivClient:
        return self.session.client

    @property
    def cache(self) -> Any:
        if self._cache is None:
            from src.core.metacache import get_meta_cache

            self._cache = get_meta_cache()
        return self._cache

    def _settings(self) -> Any:
        from src.config.settings import get_settings

        return get_settings().pixiv

    def _emit(self, name: str, payload: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(name, payload)
        except Exception:  # noqa: BLE001 - a push must never break the call
            log.debug("cannot push %s to the interface", name)

    # ------------------------------------------------------------------ 缓存
    @staticmethod
    def _worth_caching(value: Any) -> bool:
        """这份结果配不配进缓存。

        **失败和空都不进。** 一次 400/404 或者一次网络抖动把空列表写进缓存，
        那个页签就会一直空到 TTL 到期（最长一小时），而界面上什么原因都看
        不到——用户看到的是「发现了但作品就是不出来」。宁可多打一次接口。
        """
        if value is None:
            return False
        if isinstance(value, dict):
            if value.get("error"):
                return False
            for key in ("works", "illusts", "items", "users", "thumbnails"):
                if key in value:
                    return bool(value.get(key))
            return bool(value)
        if isinstance(value, list | tuple):
            return bool(value)
        return True

    def _cached(self, kind: str, key: str, maker: Callable[[], Any]) -> Any:
        """读缓存，没有就现取；**只有拿到内容才写回**。"""
        try:
            hit = self.cache.get(f"{_CACHE_NS}-{kind}", key)
        except Exception:  # noqa: BLE001 - the cache is optional
            hit = None
        if hit is not None:
            return hit
        value = maker()
        if not self._worth_caching(value):
            return value
        try:
            self.cache.set(f"{_CACHE_NS}-{kind}", key, value, _TTL.get(kind, 600.0))
        except Exception:  # noqa: BLE001
            pass
        return value

    def _drop(self, kind: str, key: str) -> None:
        try:
            self.cache.discard(f"{_CACHE_NS}-{kind}", key)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 过滤
    def _filter(self, works: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按设置处理成人内容和 AI 作品。

        ``hide`` 丢掉，``blur`` 留下但打 ``blurred``，``show`` 原样。**过滤
        放在缓存之外**：缓存里存的是 pixiv 给的原样结果，设置一改立刻生效，
        不用等缓存过期。
        """
        settings = self._settings()
        mode = str(getattr(settings, "nsfw_mode", "hide") or "hide")
        hide_ai = bool(getattr(settings, "hide_ai", False))
        out: list[dict[str, Any]] = []
        for work in works or []:
            if not isinstance(work, dict) or not work.get("id"):
                continue
            if hide_ai and int(work.get("ai_type") or 0) > 0:
                continue
            if work.get("nsfw") or is_nsfw(work):
                work = {**work, "nsfw": True}
                if mode == "hide":
                    continue
                # 只有 blur 才留这个键。show 的时候写上 ``blurred: False``
                # 会让前端每次都要先判断一次「有没有这个键、值是什么」，
                # 而它本来就该是「没有键 = 不用盖」。
                if mode == "blur":
                    work["blurred"] = True
            out.append(work)
        return out

    def _search_mode(self) -> str:
        """请求 pixiv 时带哪个 ``mode``。

        ``safe`` 让服务端先筛一遍；``all`` 才拿得到 R-18。两边都做是因为
        服务端那层只对「账号自己开了 R-18」的用户生效，界面上设了 hide
        而搜索结果里照样冒出来是很坏的体验。
        """
        settings = self._settings()
        if getattr(settings, "nsfw_mode", "hide") == "hide":
            return "safe"
        return "all" if getattr(settings, "include_r18", False) else "safe"

    def _thumb(self, work: dict[str, Any]) -> dict[str, Any]:
        """给每个作品补一个「界面直接能用」的缩略图地址。

        缩略图走本地媒体服务器的 ``/pixiv`` 代理：``i.pximg.net`` 没有
        ``Referer`` 就 403，而 ``<img>`` 设不了请求头。
        """
        media = getattr(self, "media", None)
        proxy = getattr(media, "url_for_pixiv", None) if media is not None else None
        if not callable(proxy):
            return work
        try:
            # 主机不是 pixiv 图床时它回 None —— 那时前端退回原地址，
            # 最多是裂一张图，不会把代理指到别处去。
            proxied = proxy(image_url_for(work, "thumb"))
        except Exception:  # noqa: BLE001
            return work
        return {**work, "thumb_proxy": proxied} if proxied else work

    def _proxy(self, works: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._thumb(work) for work in works]

    # ------------------------------------------------------------------ 状态
    def state(self) -> dict[str, Any]:
        """界面启动时问一次：登录了没有、设置是什么。"""
        settings = self._settings()
        info = self.session.account_info()
        return {
            "logged_in": bool(info.get("logged_in")),
            "expired": bool(info.get("expired")),
            "account": {
                "id": str(info.get("id") or ""),
                "name": str(info.get("name") or ""),
                "account": str(info.get("account") or ""),
                "avatar": str(info.get("avatar") or ""),
                "premium": bool(info.get("premium")),
                "following": int(info.get("following") or 0),
            },
            "error": str(info.get("error") or ""),
            "settings": {
                "nsfw_mode": str(getattr(settings, "nsfw_mode", "hide")),
                "include_r18": bool(getattr(settings, "include_r18", False)),
                "hide_ai": bool(getattr(settings, "hide_ai", False)),
                "quality": str(getattr(settings, "quality", "original")),
                "bookmark_private": bool(getattr(settings, "bookmark_private", False)),
                "template": str(getattr(settings, "template", "")),
                "page_size": int(getattr(settings, "page_size", 48)),
            },
            "login_state": self._login_state,
            "login_message": self._login_message,
        }

    def update_settings(self, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        """改 Pixiv 的设置（界面上那几个开关）。

        写成 ``update_pixiv(**patch)``，里面会做 _normalize：nsfw_mode 的
        非法值回落到 hide，模板里的路径分隔符会被换掉。
        """
        from src.config.settings import get_settings

        patch = patch or {}
        allowed = {"nsfw_mode", "include_r18", "hide_ai", "quality",
                   "download_dir", "template", "bookmark_private", "page_size"}
        clean = {key: value for key, value in patch.items() if key in allowed}
        if clean:
            get_settings().update_pixiv(**clean)
        return self.state()

    # ------------------------------------------------------------------ 登录
    def login_open(self, webview: Any = None, signup: bool = False) -> dict[str, Any]:
        """开一个 pixiv 自己的登录页。

        **不去抓密码**：用户在 pixiv 的页面上自己登，我们只等它的
        ``PHPSESSID`` 出现在 webview 的 cookie 里。注册也走这条路——页面
        上本来就有注册入口，而 pixiv 的注册全程有验证码，第三方没法替它做。

        这个窗口**不注入 js_api**：页面是 pixiv 的，把我们的桥挂上去等于
        把整个后端的调用面交给一个外部网页。
        """
        if webview is None or not hasattr(webview, "create_window"):
            return {"success": False, "state": self._login_state,
                    "message": "当前环境开不了登录窗口（缺少 webview）。"}
        if self._login_thread is not None and self._login_thread.is_alive():
            # 已经有一个在等：把窗口拉到前面，别再开第二个。
            window = self._login_window
            if window is not None:
                try:
                    window.restore()
                    window.on_top = True
                except Exception:  # noqa: BLE001 - the window may be gone
                    pass
            return {"success": True, "state": "waiting", "message": ""}

        url = PIXIV_SIGNUP_URL if signup else PIXIV_LOGIN_URL
        try:
            window = webview.create_window(
                title="Pixiv",
                url=url,
                width=520,
                height=760,
                min_size=(420, 560),
                resizable=True,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            log.warning("cannot open the pixiv login window: %s", exc)
            self._login_state = "failed"
            self._login_message = str(exc)
            return {"success": False, "state": "failed", "message": str(exc)}

        with self._lock:
            self._login_window = window
            self._login_state = "waiting"
            self._login_message = ""
            self._login_cancel.clear()
            self._login_thread = threading.Thread(
                target=self._watch_login, args=(window,),
                name="pixiv-login", daemon=True,
            )
            self._login_thread.start()
        return {"success": True, "state": "waiting", "message": ""}

    def _watch_login(self, window: Any) -> None:
        """盯着登录窗口，直到 cookie 里有了能用的会话。

        每 1.5 秒取一次 cookie；**只有当 cookie 变了才去验证**——
        ``get_cookies()`` 只是读本地 cookie store，很便宜，但
        ``refresh_self()`` 是一次真实的网络请求，每秒一次就是在刷人家的
        首页。

        **窗口是不是被关掉，只能靠 ``events.closed``。** 以前是看
        ``window.native`` 是不是 None，可那扇窗刚建出来时 ``native`` 本来就
        是 None（pywebview 在 start() 之后才把原生对象挂上去），于是这个线程
        第一拍就判定「用户把窗口关了」，直接以 cancelled 结束——窗口挂在屏幕
        上没人管，cookie 永远不会被取，界面上一直写着未登录。这就是「登录完
        了窗口不关、关了自己也不算登录」的全部原因。
        """
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        last_cookie = ""
        closed = threading.Event()
        try:
            window.events.closed += lambda: closed.set()
        except Exception:  # noqa: BLE001 - older/newer pywebview differ
            log.debug("pixiv login: this webview has no closed event", exc_info=True)

        # closed 事件是主信号；_window_gone 兜底那些不发 closed 的后端。
        while (not self._login_cancel.is_set() and not closed.is_set()
               and not self._window_gone(window)):
            if time.monotonic() > deadline:
                self._finish_login("failed", "登录窗口开了十分钟还没登完，已关闭。")
                self._close_login_window()
                return
            time.sleep(COOKIE_POLL_SECONDS)
            cookie = self._read_window_cookie(window)
            if not cookie or "PHPSESSID=" not in cookie:
                continue
            if cookie == last_cookie:
                continue
            last_cookie = cookie
            account = self._adopt_cookie(cookie)
            if account is not None:
                self._finish_login("ok", "", account=account)
                self._close_login_window()
                return
            # 有 cookie 但读不到身份：多半是刚登完还没跳转完，继续等。
            log.info("pixiv login: cookie seen, identity not readable yet (%s)",
                     self.session.client.last_error or "")

        if self._login_cancel.is_set():
            self._finish_login("cancelled", "")
            return

        # 用户自己把窗口关了。**关窗之前登没登成，得再看一眼**：很多人是
        # 看着页面已经进了自己的主页才关窗的，这时候 cookie 是好的，只是
        # 我们还来不及轮询到。
        self._final_login_check(window, last_cookie)

    def _final_login_check(self, window: Any, last_cookie: str) -> None:
        """窗口已经关了之后再看一眼 cookie。

        关窗那一刻 cookie store 里通常已经是对的（用户是看着页面进去了才关
        的），只是轮询还没轮到。这一眼决定「登录成功」还是「用户不想登了」。
        """
        cookie = self._read_window_cookie(window) or last_cookie
        account = self._adopt_cookie(cookie) if cookie else None
        if account is not None:
            self._finish_login("ok", "", account=account)
            return
        self._finish_login("cancelled", "")

    def _adopt_cookie(self, cookie: str) -> dict[str, Any] | None:
        """把一串 cookie 装进会话，读得出身份就返回账号信息。"""
        if not cookie or "PHPSESSID=" not in cookie:
            return None
        self._session.save(cookie, {})
        info = self.session.account_info(refresh=True)
        if not info.get("id"):
            return None
        self._session.save(cookie, info)
        return {
            "id": str(info.get("id") or ""),
            "name": str(info.get("name") or ""),
            "account": str(info.get("account") or ""),
            "avatar": str(info.get("avatar") or ""),
            "premium": bool(info.get("premium")),
        }

    @staticmethod
    def _window_gone(window: Any) -> bool:
        """窗口还活着吗。

        ``native`` 是 None **不代表窗口没了**：窗口刚建出来、原生对象还没挂上
        去的时候它就是 None。这里只把「有原生对象但已经没了」当成关掉。
        """
        try:
            native = getattr(window, "native", None)
            if native is None:
                return False
            return not bool(native)
        except Exception:  # noqa: BLE001
            return False

    def _read_window_cookie(self, window: Any) -> str:
        """从 webview 自己的 cookie store 里读。

        ``get_cookies()`` 走的是系统那一层（WKWebView 的
        ``WKHTTPCookieStore`` / WebView2 的 ``CookieManager``），所以
        **HttpOnly 的 PHPSESSID 也在里面**——用 ``document.cookie`` 是永远
        拿不到的。

        **读要有上限。** 两个后端的实现都是「往主线程丢一个活，然后等一个
        信号量」：主线程那边没应答（窗口正在关、事件循环卡住），这一句就
        永远不返回，等待线程卡死在这里，登录窗口开着也永远拿不到 cookie，
        界面只会一直显示「等待登录」。所以读放在子线程里，超时就放弃这一
        轮；上一次还没回来就不再叠一个上去。
        """
        if self._cookie_reader is not None and self._cookie_reader.is_alive():
            return ""
        slot: dict[str, Any] = {}

        def grab() -> None:
            try:
                slot["cookies"] = window.get_cookies()
            except Exception as exc:  # noqa: BLE001 - a platform quirk, keep waiting
                slot["error"] = exc

        reader = threading.Thread(target=grab, name="pixiv-cookie-read", daemon=True)
        self._cookie_reader = reader
        reader.start()
        reader.join(COOKIE_READ_TIMEOUT)
        if reader.is_alive():
            log.warning(
                "reading cookies from the login window did not answer within %.0fs",
                COOKIE_READ_TIMEOUT)
            return ""
        if "error" in slot:
            log.debug("cannot read cookies from the login window: %s", slot["error"])
            return ""
        return PixivClient.cookie_from_webview(slot.get("cookies") or [])

    def _finish_login(self, state: str, message: str,
                      account: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._login_state = state
            self._login_message = message
        payload = {"state": state, "message": message,
                   "account": account or {}, "logged_in": state == "ok"}
        if state == "ok":
            # 账号换了，账号相关的东西全部作废。
            self.invalidate_account()
        self._emit("onPixivLoginState", payload)
        log.info("pixiv login %s%s", state, f" ({message})" if message else "")

    def _close_login_window(self) -> None:
        window = self._login_window
        self._login_window = None
        if window is None:
            return
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - already gone
            pass

    def login_status(self) -> dict[str, Any]:
        """前端轮询用：登录走到哪一步了。"""
        return {
            "state": self._login_state,
            "message": self._login_message,
            "logged_in": self._login_state == "ok",
        }

    def login_cancel(self) -> dict[str, Any]:
        self._login_cancel.set()
        self._finish_login("cancelled", "")
        self._close_login_window()
        return self.login_status()

    def login_cookie(self, cookie: str) -> dict[str, Any]:
        """手动贴一串 cookie。

        给「webview 读不到 cookie」的环境留的退路：有些 Linux 后端没有
        实现 ``get_cookies()``，那时候用户还能从浏览器里把 cookie 复制过来。
        """
        cookie = PixivClient.cookie_from_webview(
            [{"name": part.split("=", 1)[0].strip(),
              "value": part.split("=", 1)[1].strip(),
              "domain": ".pixiv.net"}
             for part in str(cookie or "").split(";") if "=" in part]
        )
        if "PHPSESSID=" not in cookie:
            return {"success": False, "message": "这串 cookie 里没有 PHPSESSID。"}
        self._session.save(cookie, {})
        info = self.session.account_info(refresh=True)
        if not info.get("id"):
            return {"success": False,
                    "message": info.get("error") or "cookie 无效或已过期。"}
        self._session.save(cookie, info)
        self._finish_login("ok", "")
        return {"success": True, "message": "",
                "account": {key: info.get(key) for key in
                            ("id", "name", "account", "avatar", "premium")}}

    def logout(self) -> dict[str, Any]:
        """退出登录。只清本地——pixiv 那边没有能让第三方调的登出接口。"""
        self._login_cancel.set()
        self._close_login_window()
        self._session.clear()
        self._login_state = "idle"
        self._login_message = ""
        self.invalidate_account()
        return self.state()

    def invalidate_account(self) -> None:
        """账号变了：收藏、关注流这些跟着账号走的东西全部作废。"""
        for kind in ("userworks",):
            try:
                self.cache.discard_prefix(f"{_CACHE_NS}-{kind}", "")
            except Exception:  # noqa: BLE001 - older caches have no prefix drop
                pass

    # ------------------------------------------------------------------ 读
    def home(self) -> dict[str, Any]:
        """首页：推荐流。登录了就再给一条关注流的第一页。

        ``discover_error`` 是给界面看的：发现页空着的时候，得能说清是「没
        登录」「网络不通」还是「接口改了」，而不是只写一句「没有作品」。
        """
        held: dict[str, str] = {}

        def fetch() -> list[dict[str, Any]]:
            works = self.client.discovery()
            # 有作品但带话（比如兜底成排行榜）也要把话留下。
            held["error"] = str(getattr(self.client, "last_error", "") or "")
            return works

        works = self._proxy(self._filter(self._cached("discovery", "home", fetch)))
        discover_error = held.get("error", "")
        if not works and not discover_error:
            discover_error = "发现页没有返回作品。"
        feed: list[dict[str, Any]] = []
        feed_error = ""
        if self.session.is_logged_in:
            body = self.client.feed(page=1)
            feed = self._proxy(self._filter(body.get("works") or []))
            feed_error = str(body.get("error") or "")
        return {"discover": works, "discover_error": discover_error,
                "feed": feed, "feed_error": feed_error,
                "logged_in": self.session.is_logged_in}

    def discover_more(self, shown_ids: list[str] | None = None) -> dict[str, Any]:
        """发现页的「更多」。

        **pixiv 的推荐流没有分页。** 同一个地址每次都给一批新的随机推荐，
        ``offset`` 会被直接忽略（实测 offset=30 和第一页一模一样，两页 id
        完全相同）。所以「更多」只能是再要一批，然后把已经显示过的挑掉
        ——不然用户按了「更多」，看到的还是那几十张。
        """
        seen = {str(item) for item in (shown_ids or []) if str(item)}
        # 接口自己把 limit 封在 60，一次要满这个数，才有足够的“新”作品。
        works = self.client.discovery(limit=60)
        fresh = [work for work in works if str(work.get("id") or "") not in seen]
        return {"works": self._proxy(self._filter(fresh)),
                "shown": len(seen) + len(fresh),
                "error": str(getattr(self.client, "last_error", "") or "")}

    def search(self, word: str = "", page: int = 1, order: str = "date_d",
               kind: str = "artworks") -> dict[str, Any]:
        word = str(word or "").strip()
        if not word:
            return {"works": [], "total": 0, "related_tags": [], "word": "",
                    "page": 1}
        settings = self._settings()
        mode = self._search_mode()
        ai_type = 1 if getattr(settings, "hide_ai", False) else 0
        key = f"{word}|{page}|{order}|{kind}|{mode}|{ai_type}"
        raw = self._cached("search", key, lambda: self.client.search(
            word, page=page, mode=mode, order=order, kind=kind, ai_type=ai_type))
        return {
            "works": self._proxy(self._filter(raw.get("works") or [])),
            "total": int(raw.get("total") or 0),
            "related_tags": raw.get("related_tags") or [],
            "word": word,
            "page": int(page),
            "error": str(raw.get("error") or ""),
        }

    def ranking(self, mode: str = "daily", page: int = 1,
                content: str = "illust", date: str = "") -> dict[str, Any]:
        """排行榜。``date`` 留空就是「最近一期」，pixiv 自己决定是哪天。

        档位和内容都先过一遍白名单：客户端上写着 original / male / female
        这几个选项时，选了只会得到一片空白（那几条路线全是 404）。
        """
        if mode not in PixivClient.RANKING_MODES:
            mode = "daily"
        if content not in PixivClient.RANKING_CONTENTS:
            content = "illust"
        key = f"{mode}|{content}|{page}|{date}"
        raw = self._cached(
            "ranking", key,
            lambda: self.client.ranking(mode=mode, content=content,
                                        page=page, date=date))
        works = self._proxy(self._filter(raw.get("works") or []))
        error = str(raw.get("error") or "")
        if not works and not error:
            error = "排行榜没有返回作品。"
        return {
            "works": works,
            "has_more": bool(raw.get("has_more")),
            "date": str(raw.get("date") or ""),
            "page": int(page),
            "mode": mode,
            "content": content,
            "note": str(raw.get("note") or ""),
            "error": error,
        }

    def related(self, pid: str = "") -> dict[str, Any]:
        """这个作品的相关推荐。空结果不算错误，但要能说出为什么是空的。"""
        pid = str(pid or "").strip()
        if not pid:
            return {"works": [], "error": "缺少作品 id。"}
        raw = self._cached("related", pid, lambda: self.client.related(pid))
        works = self._proxy(self._filter(raw or []))
        return {
            "works": works,
            "error": "" if works else "这部作品没有相关推荐。",
        }

    def feed(self, page: int = 1) -> dict[str, Any]:
        body = self.client.feed(page=page)
        return {"works": self._proxy(self._filter(body.get("works") or [])),
                "has_more": bool(body.get("has_more")),
                "page": int(page),
                "error": str(body.get("error") or "")}

    def bookmarks(self, tag: str = "", offset: int = 0, rest: str = "",
                  uid: str = "") -> dict[str, Any]:
        """收藏。``rest`` 空字符串表示公开和私密一起要——那是本人的收藏页。"""
        body = self.client.bookmarks(uid=uid, tag=tag, offset=offset,
                                    limit=self._settings().page_size, rest=rest)
        return {
            "works": self._proxy(self._filter(body.get("works") or [])),
            "total": int(body.get("total") or 0),
            "has_more": bool(body.get("has_more")),
            "offset": int(offset),
            "tag": str(tag or ""),
            "error": str(body.get("error") or ""),
        }

    def following(self, offset: int = 0) -> dict[str, Any]:
        body = self.client.following(offset=offset, limit=24)
        return {"users": body.get("users") or [],
                "total": int(body.get("total") or 0),
                "has_more": bool(body.get("has_more")),
                "error": str(body.get("error") or "")}

    def user(self, uid: str = "") -> dict[str, Any]:
        uid = str(uid or "").strip()
        if not uid:
            return {}
        user = self._cached("user", uid, lambda: self.client.user(uid))
        if not user:
            return {"error": self.client.last_error}
        ids = self._cached("userworks", f"ids|{uid}",
                           lambda: self.client.user_work_ids(uid))
        user = dict(user)
        user["illust_count"] = len(ids.get("illust") or [])
        user["manga_count"] = len(ids.get("manga") or [])
        media = getattr(self, "media", None)
        proxy = getattr(media, "url_for_pixiv", None) if media is not None else None
        if callable(proxy) and user.get("avatar"):
            user["avatar_proxy"] = proxy(user["avatar"]) or ""
        return user

    def user_works(self, uid: str = "", offset: int = 0,
                   kind: str = "illust") -> dict[str, Any]:
        uid = str(uid or "").strip()
        if not uid:
            return {"works": [], "total": 0, "has_more": False}
        body = self.client.user_works(uid, kind=kind, offset=offset,
                                      limit=self._settings().page_size)
        return {
            "works": self._proxy(self._filter(body.get("works") or [])),
            "total": int(body.get("total") or 0),
            "has_more": bool(body.get("has_more")),
            "offset": int(offset),
        }

    def illust(self, pid: str = "") -> dict[str, Any]:
        """作品详情：多页的所有地址、动图信息、作者、收藏状态。

        **这里不过滤成人内容**：用户是自己点进来的，直接不显示等于按钮
        坏了。改成把 ``nsfw`` 带出去，界面先盖一层「含成人内容，点开看」。
        """
        pid = str(pid or "").strip()
        if not pid:
            return {}
        work = self._cached("illust", pid, lambda: self.client.illust(pid))
        if not work:
            return {"error": self.client.last_error}
        pages = self._cached("pages", pid, lambda: self.client.illust_pages(pid))
        work = dict(work)
        work["nsfw"] = bool(work.get("nsfw")) or is_nsfw(work)
        media = getattr(self, "media", None)
        proxy = getattr(media, "url_for_pixiv", None) if media is not None else None
        if callable(proxy):
            try:
                work["pages"] = [
                    {**page,
                     "proxy": proxy(page.get("regular") or page.get("original") or "") or ""}
                    for page in pages
                ]
                work["original_proxy"] = proxy(work.get("original") or "") or ""
            except Exception:  # noqa: BLE001
                work["pages"] = pages
        else:
            work["pages"] = pages
        if work.get("type") == 2:
            work["ugoira"] = self.client.ugoira_meta(pid)
        else:
            work["ugoira"] = {}
        # 同一作者的其他作品，详情页底下那条「作者的其他作品」用它。
        others = self.client.user_works(work.get("user_id") or "", limit=12)
        work["artist_works"] = self._proxy(self._filter(
            [item for item in (others.get("works") or [])
             if item.get("id") != pid])[:11])
        return work

    # ------------------------------------------------------------------ 写
    def bookmark(self, pid: str = "", add: bool = True,
                 private: bool | None = None) -> dict[str, Any]:
        """收藏 / 取消收藏。

        取消要用 **bookmark id**，不是作品 id——两个号不一样，传错对端回
        400 且不解释。所以取消时先取一次详情把 id 拿出来。
        """
        pid = str(pid or "").strip()
        if not pid:
            return {"ok": False, "message": "缺少作品编号。"}
        if not self.session.is_logged_in:
            return {"ok": False, "message": "先登录 Pixiv 账号。"}
        if add:
            if private is None:
                private = bool(self._settings().bookmark_private)
            result = self.client.bookmark_add(pid, private=bool(private))
        else:
            detail = self.client.illust(pid)
            bookmark_id = str(detail.get("bookmark_id") or "")
            if not bookmark_id:
                return {"ok": False, "message": "这条作品没有收藏记录。"}
            result = self.client.bookmark_delete(bookmark_id)
        if result.get("ok"):
            # 详情缓存里的收藏状态立刻过时了。
            self._drop("illust", pid)
        return result

    def like(self, pid: str = "") -> dict[str, Any]:
        """点赞。**不可撤销**，界面必须先把这句话说清楚。"""
        pid = str(pid or "").strip()
        if not pid:
            return {"ok": False, "message": "缺少作品编号。"}
        result = self.client.like(pid)
        if result.get("ok"):
            self._drop("illust", pid)
        return result

    def follow(self, uid: str = "", follow: bool = True) -> dict[str, Any]:
        uid = str(uid or "").strip()
        if not uid:
            return {"ok": False, "message": "缺少用户编号。"}
        result = self.client.follow(uid) if follow else self.client.unfollow(uid)
        if result.get("ok"):
            self._drop("user", uid)
        return result

    # ------------------------------------------------------------------ 下载
    def _download_root(self, settings: Any) -> Any:
        """作品放哪个目录：设置里填了就用它，否则 Pictures/Pixiv。

        没有 Pictures 的系统（比如某些精简安装）退到 Downloads，别去凭空
        建一个 ~/Pictures —— 那个目录在 macOS 上是有系统含义的。
        """
        from pathlib import Path

        raw = str(getattr(settings, "download_dir", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
        pictures = Path.home() / "Pictures"
        base = pictures if pictures.is_dir() else Path.home() / "Downloads"
        return base / "Pixiv"

    def download(self, pid: str = "", quality: str = "") -> dict[str, Any]:
        """下载一个作品的全部页（动图下 zip 并解帧）。

        ``private_ok`` 跟着成人内容的设置走：设成「不显示」时连下载也不
        悄悄下 R-18 作品——那是用户明确说不想要的东西。
        """
        pid = str(pid or "").strip()
        if not pid:
            return {"ok": False, "error": "缺少作品编号。"}
        settings = self._settings()
        try:
            from src.core.pixiv_download import PixivDownloader
        except Exception as exc:  # noqa: BLE001 - 打包缺模块时说清楚
            log.warning("the pixiv downloader is unavailable: %s", exc)
            return {"ok": False, "error": f"下载模块不可用：{exc}"}
        root = self._download_root(settings)
        downloader = PixivDownloader(
            self.client,
            root=root,
            template=str(getattr(settings, "template", "") or ""),
        )
        return downloader.download(
            pid,
            quality=str(quality or getattr(settings, "quality", "original")),
            private_ok=str(getattr(settings, "nsfw_mode", "hide")) != "hide",
        )

    # ------------------------------------------------------------------ 导出
    def export_data(self) -> dict[str, Any]:
        """导出账号数据。

        要的是「能备份、能看」，所以全是纯数据：账号资料、公开+私密收藏
        的全部条目、关注列表。翻页有上限——收藏几万条的用户不该因为点了
        导出就等十分钟。
        """
        if not self.session.is_logged_in:
            return {"ok": False, "message": "先登录 Pixiv 账号。"}
        info = self.session.account_info(refresh=True)
        account = {key: info.get(key) for key in
                   ("id", "name", "account", "avatar", "premium", "following")}
        bookmarks: list[dict[str, Any]] = []
        offset = 0
        while offset < 1200:
            body = self.client.bookmarks(offset=offset, limit=48, rest="")
            page = body.get("works") or []
            bookmarks.extend(page)
            if not page or not body.get("has_more"):
                break
            offset += len(page)
        followings: list[dict[str, Any]] = []
        offset = 0
        while offset < 1200:
            body = self.client.following(offset=offset, limit=24)
            page = body.get("users") or []
            followings.extend(page)
            if not page or not body.get("has_more"):
                break
            offset += len(page)
        return {
            "ok": True,
            "message": "",
            "data": {
                "account": account,
                "bookmarks": [normalize_work(item) for item in bookmarks],
                "following": followings,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        }
