"""NetEase Cloud Music API client.

Endpoint map (every entry below was verified against the live API on
2026-09-30 from a mainland-CN network -- see ``docs/notes.md``):

=====================  =======================================  =========
Need                   Endpoint                                 Channel
=====================  =======================================  =========
playlist detail        ``/api/v6/playlist/detail``              eapi
song detail (batch)    ``/api/v3/song/detail``                  eapi
song play url          ``/api/song/enhance/player/url/v1``      eapi
song lyrics            ``/api/song/lyric``                       eapi
song search            ``/api/cloudsearch/pc``                   eapi
account playlists      ``/api/user/playlist``                    eapi
like / unlike          ``/api/song/like``                        eapi
liked song ids         ``/api/song/like/get``                    eapi
play report            ``/api/feedback/weblog``                  eapi
account / login status ``/api/w/nuser/account/get``              eapi
QR key                 ``/api/login/qrcode/unikey``             legacy
QR poll                ``/api/login/qrcode/client/login``       legacy
SMS captcha            ``/api/sms/captcha/sent``                legacy
phone login            ``/api/w/login/cellphone``               legacy
logout                 ``/api/logout``                          legacy
=====================  =======================================  =========

Verification dates: the playlist/song/login rows were measured on 2026-09-30,
the search/account/like/weblog rows on 2026-10-01.  `/api/radio/like`
(``code -460``), `/api/scrobble` and `/api/v1/play/record` (404) were tried and
rejected -- do not reintroduce them.

Endpoints that **do not exist any more** (all answer ``404 接口未找到``) and must
never be reintroduced: ``/api/song/url``, ``/api/song/url/v1``,
``/api/v3/song/url``, ``/api/login/status``, ``/api/sent/verificationcode``,
``/api/homepage/block/page``, ``GET /api/song/lyric`` (the eapi route above is the
one that works).  ``/api/song/lyric/v1`` exists but always answers ``code 400``.

``/weapi/*`` is blackholed on many networks (HTTP 200 + empty body for every
request) -- see :mod:`src.core.crypto`.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Sequence
from typing import Any

import requests

from src.auth import get_login_manager
from src.config import get_settings
from src.core.crypto import (
    CLIENT_USER_AGENT,
    build_client_header,
    build_client_identity,
    eapi_body,
    encode_type_for_level,
)
from src.core.lyrics import LYRIC_ENDPOINT
from src.core.metacache import get_meta_cache

__all__ = ["NeteaseAPI", "get_api", "QUALITY_ORDER", "quality_fallbacks",
           "SEARCH_ENDPOINT", "USER_PLAYLIST_ENDPOINT", "LIKE_ENDPOINT",
           "LIKE_LIST_ENDPOINT", "WEBLOG_ENDPOINT"]

#: Search / account endpoints (all verified live on 2026-10-01).
SEARCH_ENDPOINT = "/api/cloudsearch/pc"
USER_PLAYLIST_ENDPOINT = "/api/user/playlist"
#: Like / unlike.  The reference implementation uses ``/api/radio/like``;
#: both routes answer identically (``/api/song/like`` answers 524 for a group
#: of tracks, and so does ``/api/radio/like`` -- see
#: :meth:`NeteaseAPI.set_song_like`), so we keep the one the project used.
LIKE_ENDPOINT = "/api/song/like"
LIKE_RADIO_ENDPOINT = "/api/radio/like"
LIKE_LIST_ENDPOINT = "/api/song/like/get"
WEBLOG_ENDPOINT = "/api/feedback/weblog"

#: Discovery endpoints (verified live 2026-10-01 onwards)
ALBUM_ENDPOINT = "/api/v1/album"
ARTIST_ENDPOINT = "/api/v1/artist"
RECOMMEND_PLAYLIST_ENDPOINT = "/api/v1/discovery/recommend/resource"
PERSONAL_FM_ENDPOINT = "/api/v1/radio/get"
NEW_SONGS_ENDPOINT = "/api/v1/discovery/new/songs"
RECOMMEND_MV_ENDPOINT = "/api/mv/recommend"
TOP_LIST_ENDPOINT = "/api/v3/playlist/detail"  # with special IDs
SEARCH_SUGGEST_ENDPOINT = "/api/search/suggest"
SEARCH_MULTI_ENDPOINT = "/api/cloudsearch/pc"  # same as search but with type parameter

#: Playlist management endpoints.
#:
#: Re-verified live on 2026-10-23.  The endpoints this project used before
#: are **dead**: ``/api/playlist/update``, ``/api/playlist/tracks/add`` and
#: ``/api/playlist/tracks/del`` answer ``404 接口未找到！``, and
#: ``/api/playlist/delete`` answers ``400 请求参数错误``.  The live routes are:
#:
#: * rename -> ``/api/playlist/update/name``  ``{"id": ..., "name": ...}``
#: * delete -> ``/api/playlist/remove``        ``{"ids": "[id]"}``
#: * tracks -> ``/api/playlist/manipulate/tracks``
#:             ``{"op": "add"|"del", "pid": ..., "trackIds": "[id]",
#:             "imme": "true"}``
PLAYLIST_CREATE_ENDPOINT = "/api/playlist/create"
PLAYLIST_UPDATE_ENDPOINT = "/api/playlist/update/name"
PLAYLIST_DELETE_ENDPOINT = "/api/playlist/remove"
PLAYLIST_TRACKS_ENDPOINT = "/api/playlist/manipulate/tracks"
PLAYLIST_SUBSCRIBE_ENDPOINT = "/api/playlist/subscribe"
PLAYLIST_UNSUBSCRIBE_ENDPOINT = "/api/playlist/unsubscribe"
PLAYLIST_DETAIL_DYNAMIC_ENDPOINT = "/api/playlist/detail/dynamic"  # for track operations
PLAYLIST_COVER_UPDATE_ENDPOINT = "/api/playlist/cover/update"

#: The client identity NetEase's risk control wants to see on some of the
#: playlist **write** routes.  Without it those routes answer
#: ``code 403 illegal request!`` -- measured on 2026-10-25, see
#: ``docs/notes.md`` section 21.
#:
#: The official client states the same client in the eapi header and in the
#: cookie, so the two agree; the cookie is built from the very header that
#: goes into the signed body (see
#: :func:`src.core.crypto.build_client_identity`).  It has to be
#: **complete** -- a cookie with only ``os`` while the header carries ``os``
#: *and* ``appver`` is a half-stated identity, and the risk engine answers
#: ``code -462`` ("your network environment looks risky") where the complete
#: one is accepted.
#:
#: The cookie is per request, never in the session jar: a session that
#: carries this identity makes the like endpoint answer ``code 524``
#: (section 18).
#:
#: It is built on demand rather than kept as a constant, because the header
#: carries a fresh ``requestId`` on every request and the cookie has to
#: match it.

#: The routes that are gated behind that identity, measured 2026-10-25.
#: Everything else is **not** gated, and sending an identity to them is
#: not free: a partial identity makes the track manipulation route answer
#: ``code -462`` ("your network environment looks risky") instead of doing
#: the work.  So the identity goes to these routes and to nothing else.
#:
#: ``/api/batch`` is here because the routes *inside* it are gated -- its
#: outer answer is always 200, whatever the inner route said.
IDENTITY_REQUIRED_ROUTES: frozenset[str] = frozenset({
    PLAYLIST_CREATE_ENDPOINT,
    PLAYLIST_UPDATE_ENDPOINT,
    PLAYLIST_SUBSCRIBE_ENDPOINT,
    PLAYLIST_UNSUBSCRIBE_ENDPOINT,
    "/api/batch",
})

#: The routes that **write**: they change something on the account, and
#: NetEase limits them far harder than the reads.  Measured on 2026-10-25:
#: a dozen writes in a row answer ``code 405`` ("操作过于频繁") and the
#: account then needs minutes to breathe -- see ``docs/notes.md`` section 22.
#:
#: The routes are matched exactly, so ``/api/song/like`` (a write) and
#: ``/api/song/like/get`` (a read) never get mixed up.
WRITE_ROUTES: frozenset[str] = frozenset({
    LIKE_ENDPOINT,
    LIKE_RADIO_ENDPOINT,
    WEBLOG_ENDPOINT,
    PLAYLIST_CREATE_ENDPOINT,
    PLAYLIST_UPDATE_ENDPOINT,
    PLAYLIST_DELETE_ENDPOINT,
    PLAYLIST_TRACKS_ENDPOINT,
    PLAYLIST_SUBSCRIBE_ENDPOINT,
    PLAYLIST_UNSUBSCRIBE_ENDPOINT,
    PLAYLIST_COVER_UPDATE_ENDPOINT,
    "/api/batch",
})

#: The words NetEase uses when it refuses a request because it came too
#: fast.  ``code 405`` is the usual answer; the message is what tells a
#: rate limit apart from another refusal that happens to use the same code.
#:
#: The Chinese entries are the whole phrases, not fragments: a message that
#: merely contains a character like "频" would match far too much.
RATE_LIMIT_HINTS: tuple[str, ...] = (
    "操作过于频繁",
    "请求过于频繁",
    "访问过于频繁",
    "too often",
    "too frequent",
    "rate limit",
)

#: Audio levels from lowest to highest.
QUALITY_ORDER: tuple[str, ...] = ("standard", "higher", "exhigh", "lossless", "hires")

#: Human-readable description of the codes the play-url endpoint returns.
PLAY_URL_CODES = {
    200: "ok",
    -110: "copyright_unavailable",
    -460: "rate_limited",
    -447: "login_required",
    404: "song_not_found",
}

#: API codes that mean "you have to log in for this".
LOGIN_REQUIRED_CODES = {301, 401, -462, -447, -475}


def quality_fallbacks(quality: str) -> list[str]:
    """Return the requested level followed by progressively lower ones."""
    if quality not in QUALITY_ORDER:
        quality = "standard"
    index = QUALITY_ORDER.index(quality)
    return list(reversed(QUALITY_ORDER[: index + 1]))


def _scope_matches(scope: str, wanted: str) -> bool:
    """Whether a cached scope is covered by an invalidation request.

    ``liked:*`` matches every account's liked ids; everything else matches
    exactly.
    """
    if wanted.endswith(":*"):
        return scope.startswith(wanted[:-1])
    return scope == wanted


class NeteaseAPI:
    """HTTP client for the NetEase Cloud Music API."""

    #: eapi hosts.  Rotated when a host misbehaves.
    EAPI_HOSTS: tuple[str, ...] = (
        "https://interface.music.163.com",
        "https://interface3.music.163.com",
        "https://music.163.com",
    )
    LEGACY_HOST = "https://music.163.com"

    #: Minimum seconds between two outbound API calls.  The endpoints start
    #: answering ``code 400`` when hammered without any pacing.
    MIN_REQUEST_INTERVAL = 0.15  # Reduced from 0.30 for better responsiveness
    MAX_RETRIES = 3
    PLAYLIST_CACHE_TTL = 300.0

    #: Minimum seconds between two **writes**, plus a little jitter.  A write
    #: is something the user asked for (a like, a subscribe), so the wait has
    #: to be invisible -- but the writes still go out one at a time, never as
    #: a burst, because NetEase counts them per route.
    WRITE_MIN_INTERVAL = 0.4

    #: How long to wait before retrying a request the server refused for
    #: coming too fast.  The ladder is walked one step per attempt, so a
    #: single refusal costs a few seconds and a persistent one costs a
    #: minute.
    RATE_LIMIT_BACKOFF: tuple[float, ...] = (3.0, 8.0, 15.0, 30.0, 60.0)

    #: How many times a write is sent before giving up.  One more than the
    #: length of the backoff ladder, so the last attempt waits a minute.
    WRITE_MAX_ATTEMPTS = 6

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.login_manager = get_login_manager()
        self.settings = get_settings()

        self._host_index = 0
        self._request_lock = threading.Lock()
        self._last_request_ts = 0.0

        #: Held while a write is in flight, so two writes never overlap.
        self._write_lock = threading.Lock()
        #: When the last write went out.
        self._last_write_ts = 0.0
        #: Until when no write may go out.  A rate limit anywhere raises
        #: this, so a refusal on one route also slows the others down.
        self._write_cooldown_until = 0.0

        # Session (cookies shared with the login manager).
        self.session = self.login_manager.get_session()

        self.stats: dict[str, Any] = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "host_switches": 0,
        }

        self._playlist_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        #: The "liked music" playlists (specialType 5) we have seen.  Their
        #: contents are the account's favourites, so a like or an unlike has
        #: to invalidate their cached track list too.
        self._liked_music_playlists: set[str] = set()
        # Cache for search and other API responses
        self._response_cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = 60.0  # 60 seconds cache TTL
        #: Hard cap on the response cache.  256 entries is far more than a
        #: UI session needs and keeps the worst case bounded.
        self._response_cache_max = 256
        #: Reason the last play-url lookup failed ("copyright_unavailable", ...).
        self.last_url_error: str | None = None

        self.logger = logging.getLogger("ncm.api")

    # ------------------------------------------------------------------ utils
    @property
    def eapi_host(self) -> str:
        return self.EAPI_HOSTS[self._host_index % len(self.EAPI_HOSTS)]

    def _switch_host(self) -> None:
        self._host_index = (self._host_index + 1) % len(self.EAPI_HOSTS)
        self.stats["host_switches"] += 1
        self._log(f"switched eapi host to {self.eapi_host}", "warning")

    def _log(self, message: str, level: str = "debug") -> None:
        if level == "debug" and not self.debug:
            return
        self.logger.log(logging.DEBUG if level == "debug" else logging.INFO, message)

    def _throttle(self) -> None:
        """Enforce a global minimum gap between requests (thread safe)."""
        with self._request_lock:
            now = time.monotonic()
            wait = self.MIN_REQUEST_INTERVAL - (now - self._last_request_ts)
            if wait > 0:
                time.sleep(wait + random.uniform(0.0, 0.08))
            self._last_request_ts = time.monotonic()

    def _throttle_write(self) -> None:
        """Hold back a write until it may go out.

        Two things are waited for, and both are about not being the client
        that gets the account throttled:

        * the gap since the last write, plus a little jitter, so the writes
          do not arrive in a burst;
        * the shared cooldown, which a rate limit anywhere raises.  That is
          what makes a refusal on one route slow the other routes down too.

        The caller holds :attr:`_write_lock` while calling this, so only one
        write is ever in flight -- and while it sleeps, the next write is
        already queued behind it.
        """
        with self._request_lock:
            now = time.monotonic()
            gap = self.WRITE_MIN_INTERVAL - (now - self._last_write_ts)
            cooldown = self._write_cooldown_until - now
            wait = max(gap, cooldown, 0.0)
            if wait > 0:
                self._log(f"write waits {wait:.1f}s", "debug")
                time.sleep(wait)
            self._last_write_ts = time.monotonic()

    def _raise_cooldown(self, attempt: int) -> float:
        """Put every write on hold after a rate limit.  Returns the wait."""
        ladder = self.RATE_LIMIT_BACKOFF
        delay = ladder[min(attempt, len(ladder) - 1)]
        self._write_cooldown_until = time.monotonic() + delay
        self._log(f"rate limited, writes pause for {delay:.0f}s", "warning")
        return delay

    @staticmethod
    def _is_rate_limited(result: dict[str, Any] | None) -> bool:
        """Whether the server refused the request for coming too fast.

        ``code 405`` is the usual answer, but the same code is used for
        other refusals, so the message is what confirms it.
        """
        if not result:
            return False
        code = result.get("code")
        if code in (-460, 429):
            return True
        if code != 405:
            return False
        message = str(result.get("msg") or result.get("message") or "")
        if not message:
            # A bare 405 with no explanation is the rate limit.
            return True
        return any(hint in message for hint in RATE_LIMIT_HINTS)

    def _headers(self) -> dict[str, str]:
        return {
            # The official desktop client's UA names the client it is
            # pretending to be, and that has to agree with the eapi header:
            # "NeteaseMusicDesktop/<appver>.<buildver>" next to a header that
            # says os=osx / appver=3.1.9 / buildver=3364.
            "User-Agent": CLIENT_USER_AGENT,
            "Referer": "https://music.163.com/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _post(self, url: str, data: dict[str, Any], timeout: tuple[int, int] = (5, 20),
              cookies: dict[str, str] | None = None):
        self._throttle()
        self.stats["total_requests"] += 1
        # ``cookies`` is passed per request, not written into the session
        # jar: the identity cookie must reach the write routes and nothing
        # else (see ``IDENTITY_REQUIRED_ROUTES``).
        return self.session.post(url, data=data, headers=self._headers(), timeout=timeout,
                                 cookies=cookies)

    def _get(self, url: str, params: dict[str, Any], timeout: tuple[int, int] = (5, 20)):
        self._throttle()
        self.stats["total_requests"] += 1
        return self.session.get(url, params=params, headers=self._headers(), timeout=timeout)

    # --------------------------------------------------------------- eapi API
    def _eapi_once(self, path: str, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """Single attempt at a signed eapi call (no retries)."""
        # One header, used twice: the signed body carries it and the cookie
        # repeats it.  A request whose two halves disagree is a request that
        # looks forged, so they must come from the same dictionary.
        header = build_client_header()
        body = eapi_body(path, data, header=header)
        url = f"{self.eapi_host}/eapi{path}"
        # The identity cookie is added here, for the routes that need it and
        # for those alone -- see IDENTITY_REQUIRED_ROUTES.
        cookies = build_client_identity(header) if path in IDENTITY_REQUIRED_ROUTES else None
        try:
            resp = self._post(url, body, cookies=cookies)
        except requests.exceptions.RequestException as exc:
            self._log(f"eapi transport error {path}: {exc}", "warning")
            return None

        if resp.status_code != 200:
            self._log(f"eapi HTTP {resp.status_code} on {path}", "warning")
            return None

        text = resp.text.strip()
        if not text:
            # Same signature as the weapi blackhole -> treat as "host unusable".
            self._log(f"eapi returned an empty body for {path}", "warning")
            return None

        try:
            return resp.json()
        except ValueError:
            self._log(f"eapi returned non-JSON for {path}: {text[:120]!r}", "warning")
            return None

    def _scope_for(self, path: str, data: dict[str, Any] | None) -> str:
        """What a cached answer depends on, derived from the request.

        The scope is what makes an answer *invalidatable*: when the account
        likes a song, the answers that were built from the old liked list
        have to be thrown away.  It is derived from the endpoint and the
        request rather than passed in, so no caller can forget it.
        """
        payload = data or {}
        if path == LIKE_LIST_ENDPOINT:
            uid = payload.get("uid")
            return f"liked:{uid}" if uid else ""
        if path == USER_PLAYLIST_ENDPOINT:
            uid = payload.get("uid")
            return f"userplaylists:{uid}" if uid else ""
        if path in ("/api/v6/playlist/detail", "/v3/playlist/detail"):
            pid = payload.get("id")
            return f"playlist:{pid}" if pid else ""
        return ""

    def _cache_key(self, path: str, data: dict[str, Any] | None) -> str:
        """Generate a cache key for an API call."""
        import hashlib
        key_data = f"{path}:{str(data or '')}"
        return hashlib.md5(key_data.encode()).hexdigest()

    def _get_cached(self, cache_key: str) -> Any | None:
        """Get cached response if still valid."""
        now = time.time()
        cached = self._response_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        return None

    # ------------------------------------------------------------ invalidation
    def invalidate_scopes(self, *scopes: str) -> int:
        """Drop the cached responses that belong to the given scopes.

        A *scope* is a label for what a cached answer depends on -- the
        account's liked ids, one playlist, the account's playlist list.  When
        the account changes something, the answers that depend on it are no
        longer true, and the only safe way to keep them is to throw them
        away.

        Without this, the cache turns every write into a lie: the song was
        liked on the server, but the app kept answering from the minute-old
        cached id list, so "我喜欢的音乐" never showed the new song.

        A scope ending in ``:*`` is a prefix match, so ``liked:*`` drops the
        liked ids of every account.
        """
        wanted = {str(scope) for scope in scopes if scope}
        if not wanted:
            return 0
        cache = getattr(self, "_response_cache", None)
        if not cache:
            return 0
        removed = 0
        for key, entry in list(cache.items()):
            scope = entry[3] if len(entry) > 3 else ""
            if scope and any(_scope_matches(scope, want) for want in wanted):
                cache.pop(key, None)
                removed += 1
        return removed

    def invalidate_paths(self, *paths: str) -> int:
        """Drop the cached responses of the given endpoints."""
        wanted = {str(path) for path in paths if path}
        if not wanted:
            return 0
        cache = getattr(self, "_response_cache", None)
        if not cache:
            return 0
        removed = 0
        for key, entry in list(cache.items()):
            if len(entry) > 2 and entry[2] in wanted:
                cache.pop(key, None)
                removed += 1
        return removed

    def invalidate_playlist(self, playlist_id: str) -> int:
        """Forget everything the app knows about one playlist.

        The in-memory playlist cache holds the **whole** playlist object,
        track list included, so it has to go as well -- otherwise the
        short-lived track list would still be read out of a five-minute-old
        snapshot.
        """
        pid = str(playlist_id or "").strip()
        if not pid:
            return 0
        removed = 0
        cache = getattr(self, "_playlist_cache", None)
        if cache is not None:
            removed += cache.pop(pid, None) is not None
        removed += self.invalidate_scopes(f"playlist:{pid}")
        disk = get_meta_cache()
        disk.discard("playlist", f"tracks:{pid}")
        disk.discard("meta", f"playlist:{pid}")
        return int(removed)

    def invalidate_liked(self, uid: str = "") -> int:
        """Forget the account's liked list.

        The liked ids are the source of truth for every heart in the app, so
        after a like or an unlike they have to be re-read.  The "liked music"
        playlist *is* that list, so its cached track list goes too.

        Called without a uid it drops the liked ids of every account -- the
        app only ever has one account, and the like endpoint does not need
        to know which one it was.
        """
        uid = str(uid or "").strip()
        scope = f"liked:{uid}" if uid else "liked:*"
        removed = self.invalidate_scopes(scope)
        for pid in list(getattr(self, "_liked_music_playlists", ())):
            removed += self.invalidate_playlist(pid)
        # The "liked music" playlist also appears in the account's playlist
        # list, with its track count.  A like changes that number, so the
        # list has to be re-read as well.
        removed += self.invalidate_user_playlists(uid)
        return removed

    def invalidate_user_playlists(self, uid: str = "") -> int:
        """Forget the account's own playlist list.

        The list holds the account's playlists **and** their track counts, so
        a change to any of them makes it wrong.  Called without a uid it
        drops the list of every account.
        """
        uid = str(uid or "").strip()
        scope = f"userplaylists:{uid}" if uid else "userplaylists:*"
        removed = self.invalidate_scopes(scope)
        disk = get_meta_cache()
        removed += disk.discard_prefix("playlist", f"userplaylists:{uid}:" if uid
                                        else "userplaylists:")
        return removed

    def _set_cached(self, cache_key: str, value: Any, path: str = "",
                    scope: str = "") -> None:
        """Cache a response.

        The cache is **bounded** in both directions.  An expired entry used to
        stay in the dict forever: the lookup skipped it, but nothing ever
        removed it, so a long session that searched a lot grew a dict of dead
        entries (each holding a full API response) that nothing could reclaim.
        Expired keys are dropped on write, and the dict is capped so a burst
        of distinct requests cannot blow it up.

        Every entry remembers the endpoint it came from and the *scope* it
        depends on, so a write can throw away exactly the answers that the
        write made wrong.
        """
        now = time.time()
        if len(self._response_cache) >= self._response_cache_max:
            self._prune_response_cache(now)
        self._response_cache[cache_key] = (now + self._cache_ttl, value, path, scope)

    def _prune_response_cache(self, now: float | None = None) -> int:
        """Drop expired cache entries.  Returns how many went."""
        now = now if now is not None else time.time()
        expired = [key for key, (until, *_rest) in self._response_cache.items() if until <= now]
        for key in expired:
            self._response_cache.pop(key, None)
        # Still over the cap: drop the oldest entries.
        overflow = len(self._response_cache) - self._response_cache_max
        if overflow > 0:
            oldest = sorted(self._response_cache.items(), key=lambda kv: kv[1][0])
            for key, _ in oldest[:overflow]:
                self._response_cache.pop(key, None)
        return len(expired)

    def _eapi(self, path: str, data: dict[str, Any] | None = None,
              use_cache: bool = True) -> dict[str, Any] | None:
        """Signed eapi call with retries, host rotation, and optional caching.

        Whether the request carries the client identity cookie is decided
        by the route, not by the caller: see ``IDENTITY_REQUIRED_ROUTES``.

        Whether it is a **write** is decided the same way: see
        ``WRITE_ROUTES``.  A write is sent on its own -- one at a time, with
        a gap between the two -- and when the server refuses it for coming
        too fast it is retried after a wait.  A read is not held back by
        that: reading is not what the risk engine counts.
        """
        scope = self._scope_for(path, data)
        # The scope is part of the key: two answers to the same endpoint
        # that depend on different things must not share one entry.
        cache_key = self._cache_key(f"{path}|{scope}", data) if use_cache else None
        if cache_key:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached

        if path in WRITE_ROUTES:
            result = self._eapi_write(path, data)
        else:
            result = self._eapi_read(path, data)

        if result is not None:
            self.stats["successful_requests"] += 1
            if cache_key:
                self._set_cached(cache_key, result, path=path, scope=scope)
            return result

        self.stats["failed_requests"] += 1
        return None

    def _eapi_read(self, path: str, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """A read: fast, with host rotation when a host misbehaves.

        A read that is refused for coming too fast is retried after a
        short wait, but the wait stays **local to this read**: it does not
        hold up the other reads, and it does not hold up the writes either.
        The server counts the requests per route, so a read route being
        throttled says nothing about the write routes.
        """
        for attempt in range(self.MAX_RETRIES):
            result = self._eapi_once(path, data)
            if result is not None:
                code = result.get("code")
                if code in (-460, 429):
                    delay = 2.0 * (attempt + 1) + random.uniform(0.2, 0.6)
                    self._log(f"rate limited on {path}, sleeping {delay:.1f}s", "warning")
                    time.sleep(delay)
                    self._switch_host()
                    continue
                if self._is_rate_limited(result) and attempt < self.MAX_RETRIES - 1:
                    delay = self.RATE_LIMIT_BACKOFF[min(attempt, len(self.RATE_LIMIT_BACKOFF) - 1)]
                    self._log(f"rate limited on {path}, sleeping {delay:.1f}s", "warning")
                    time.sleep(delay)
                    continue
                return result
            if attempt < self.MAX_RETRIES - 1:
                self._switch_host()
                time.sleep(0.4 * (attempt + 1) + random.uniform(0.1, 0.4))
        return None

    def _eapi_write(self, path: str, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """A write: one at a time, and retried when the server says "too fast".

        The whole call happens under :attr:`_write_lock`, so a second write
        waits for the first one -- including the wait after a rate limit.
        That is the point: the writes leave the app one by one, spaced out,
        and when the server pushes back the app waits instead of hammering.

        A rate limit is answered by waiting and trying again, walking
        :attr:`RATE_LIMIT_BACKOFF` one step per attempt.  The refusal is
        returned to the caller once the ladder is exhausted.
        """
        with self._write_lock:
            for attempt in range(self.WRITE_MAX_ATTEMPTS):
                self._throttle_write()
                result = self._eapi_once(path, data)
                if result is None:
                    # A transport failure: the request never reached the
                    # server, so it does not count against the rate limit.
                    if attempt < self.WRITE_MAX_ATTEMPTS - 1:
                        self._switch_host()
                        time.sleep(0.4 * (attempt + 1) + random.uniform(0.1, 0.4))
                        continue
                    return None
                if not self._is_rate_limited(result):
                    return result
                if attempt < self.WRITE_MAX_ATTEMPTS - 1:
                    self._raise_cooldown(attempt)
                    continue
                self._log(f"giving up on {path} after {attempt + 1} attempts", "warning")
                return result
        return None

    # ------------------------------------------------------------- legacy API
    def _legacy(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        use_cache: bool = True,
    ) -> dict[str, Any] | None:
        """Call one of the still-served plain ``/api/*`` routes."""
        url = f"{self.LEGACY_HOST}{endpoint}"
        payload = dict(params or {})
        payload.setdefault("timestamp", int(time.time() * 1000))
        scope = self._scope_for(endpoint, payload)
        cache_key = self._cache_key(f"{endpoint}|{scope}", payload) if use_cache else None

        if cache_key:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached

        for attempt in range(self.MAX_RETRIES):
            try:
                if method.upper() == "GET":
                    resp = self._get(url, payload)
                else:
                    resp = self._post(url, payload)
                if resp.status_code == 200 and resp.text.strip():
                    result = resp.json()
                    self.stats["successful_requests"] += 1
                    if cache_key:
                        self._set_cached(cache_key, result, path=endpoint, scope=scope)
                    return result
                self._log(f"legacy {endpoint} -> HTTP {resp.status_code} {resp.text[:80]!r}", "warning")
            except requests.exceptions.RequestException as exc:
                self._log(f"legacy transport error {endpoint}: {exc}", "warning")
            except ValueError:
                self._log(f"legacy {endpoint} returned non-JSON", "warning")
                return None
            if attempt < self.MAX_RETRIES - 1:
                time.sleep(0.4 * (attempt + 1))
        self.stats["failed_requests"] += 1
        return None

    # -------------------------------------------------------------- playlists
    def _fetch_playlist_detail(self, playlist_id: str,
                               max_tracks: int = 1000,
                               use_cache: bool = True) -> dict[str, Any] | None:
        """Ask the server for one playlist.  No caching happens here.

        This is the single point where a playlist is fetched, so the two
        cache layers above it can be reasoned about separately: the
        in-memory one for the metadata, the on-disk one for the track list.
        """
        payload = {"id": int(playlist_id) if str(playlist_id).isdigit() else playlist_id,
                   "n": max_tracks, "s": 8}
        result = self._eapi("/api/v6/playlist/detail", payload, use_cache=use_cache)
        playlist = (result or {}).get("playlist")
        if not playlist:
            legacy = self._legacy("/v3/playlist/detail", {"id": playlist_id, "n": max_tracks})
            playlist = (legacy or {}).get("playlist") or (legacy or {}).get("result")
        return playlist

    def _store_playlist_info(self, playlist_id: str, playlist: dict[str, Any]) -> None:
        """Remember a playlist's metadata and refresh the on-disk copy."""
        cache_key = str(playlist_id)
        self._playlist_cache[cache_key] = (time.time(), playlist)
        # The "liked music" playlist is the account's favourites; a like or
        # an unlike changes it, so it has to be droppable by id.
        if int(playlist.get("specialType") or 0) == 5:
            self._liked_music_playlists.add(cache_key)
        # Keep only the metadata, not the inline track list: the list is
        # long and changes; the name and cover do not.
        disk = get_meta_cache()
        disk.set_meta(f"playlist:{cache_key}", {
            "id": playlist.get("id"),
            "name": playlist.get("name"),
            "coverImgId": playlist.get("coverImgId"),
            "picUrl": playlist.get("picUrl"),
            "picId": playlist.get("picId"),
            "description": playlist.get("description"),
            "trackCount": playlist.get("trackCount"),
            "userId": playlist.get("userId"),
            "creator": (playlist.get("creator") or {}),
            "subscribedCount": playlist.get("subscribedCount"),
            "shareCount": playlist.get("shareCount"),
            "commentCount": playlist.get("commentCount"),
        })

    def get_playlist_info(self, playlist_id: str, max_tracks: int = 1000,
                          fresh: bool = False) -> dict[str, Any] | None:
        """Fetch playlist metadata (with up to ``max_tracks`` tracks inline).

        The metadata (name, cover, description) is cached on disk.  A
        playlist's *contents* change when its owner edits it, but its name
        and cover rarely do -- and the cover is a separate image request
        the user should not pay for every time they open the list.

        ``fresh=True`` skips the in-memory copy; it is what a manual
        refresh asks for.
        """
        cache_key = str(playlist_id)
        if not fresh:
            cached = self._playlist_cache.get(cache_key)
            if cached and time.time() - cached[0] < self.PLAYLIST_CACHE_TTL:
                return cached[1]

        disk = get_meta_cache()
        stored = disk.get_meta(f"playlist:{cache_key}") or {}

        playlist = self._fetch_playlist_detail(playlist_id, max_tracks,
                                               use_cache=not fresh)
        if not playlist:
            # The server is unreachable, but the cached metadata still shows
            # the playlist's name and cover.  Return it with an empty track
            # list rather than nothing at all.
            if stored:
                fallback = dict(stored)
                fallback.setdefault("tracks", [])
                fallback.setdefault("trackIds", [])
                return fallback
            return None

        self._store_playlist_info(cache_key, playlist)
        return playlist

    def get_playlist_songs(self, playlist_id: str,
                            fresh: bool = False) -> list[dict[str, Any]] | None:
        """Return the complete track list of a playlist, in playlist order.

        Playlists longer than the inline page size are completed through
        ``/api/v3/song/detail`` using the ``trackIds`` of the playlist.

        The finished list is cached on disk for a short while (see
        :data:`src.core.metacache.TTL_PLAYLIST`).  "Timely" matters here:
        a playlist's *contents* change when the owner edits it, so the
        cache is short-lived and the playlist's own metadata (name, cover)
        is refreshed separately.

        ``fresh=True`` skips the cached list entirely -- that is the manual
        refresh, and it is also what a write uses to make sure the change
        shows up at once.
        """
        cache = get_meta_cache()
        if not fresh:
            cached = cache.get_playlist(f"tracks:{playlist_id}")
            if cached:
                return cached
        tracks = self._fetch_playlist_songs(playlist_id, use_cache=not fresh)
        if tracks:
            cache.set_playlist(f"tracks:{playlist_id}", tracks)
        return tracks

    def _fetch_playlist_songs(self, playlist_id: str,
                               use_cache: bool = True) -> list[dict[str, Any]] | None:
        """The playlist's tracks, read from the server.

        This always asks the server.  Reading the tracks out of the
        playlist-info cache instead would re-cache a stale list under a
        fresh timestamp, and the staleness would then last another whole
        TTL -- the cache would keep the wrong answer alive.
        """
        info = self._fetch_playlist_detail(playlist_id, use_cache=use_cache)
        if not info:
            return None
        # The fetch above is the freshest answer, so it may as well refresh
        # the metadata cache too; the caller that needs the metadata right
        # after this gets it without another request.
        self._store_playlist_info(str(playlist_id), info)

        tracks: list[dict[str, Any]] = list(info.get("tracks") or [])
        track_ids = [str(item.get("id")) for item in (info.get("trackIds") or [])]
        track_count = info.get("trackCount") or len(track_ids) or len(tracks)

        if track_ids and len(tracks) < track_count:
            detailed = self.get_songs_detail(track_ids)
            if detailed:
                by_id = {str(song.get("id")): song for song in detailed}
                ordered = [by_id[tid] for tid in track_ids if tid in by_id]
                # Songs the API refused to return were deleted/copyright-blocked;
                # keep whatever we did get, in playlist order.
                if ordered:
                    return ordered

        return tracks

    def get_songs_detail(self, song_ids: Sequence[str], batch_size: int = 200) -> list[dict[str, Any]]:
        """Fetch details for many songs, batching the requests.

        Song metadata (title, artist, album, cover) barely ever changes, so
        it is cached on disk.  Only the ids that are **not** cached are
        requested, and the answers are stored for next time.  A playlist of
        500 songs that the user opens every morning costs one request for
        the songs that changed, not five hundred.
        """
        ids = [str(i) for i in song_ids]
        cache = get_meta_cache()
        songs: list[dict[str, Any]] = []
        missing: list[str] = []
        cached_by_id: dict[str, dict[str, Any]] = {}

        for song_id in ids:
            entry = cache.get_meta(f"song:{song_id}")
            if entry:
                cached_by_id[song_id] = entry
            else:
                missing.append(song_id)

        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            payload = {"c": json.dumps([{"id": int(i)} for i in batch if str(i).isdigit()])}
            result = self._eapi("/api/v3/song/detail", payload)
            batch_songs = (result or {}).get("songs")
            if not batch_songs:
                legacy = self._legacy("/v3/song/detail", payload)
                batch_songs = (legacy or {}).get("songs")
            if not batch_songs:
                legacy = self._legacy("/song/detail",
                                      {"ids": json.dumps([int(i) for i in batch if str(i).isdigit()])})
                batch_songs = (legacy or {}).get("songs")
            for song in batch_songs or []:
                song_id = str(song.get("id") or "")
                if not song_id:
                    continue
                cached_by_id[song_id] = song
                cache.set_meta(f"song:{song_id}", song)
            songs.extend(batch_songs or [])

        # The caller expects the songs in the order it asked for them, and
        # every song that the server knows about -- the cached ones and the
        # freshly fetched ones mixed.  A song that no longer exists is simply
        # missing from the list.
        ordered = [cached_by_id[song_id] for song_id in ids if song_id in cached_by_id]
        return ordered or songs

    # --------------------------------------------------------------- play url
    def get_song_url_info(self, song_id: str, quality: str = "standard") -> dict[str, Any] | None:
        """Resolve a playable URL for ``song_id``.

        Tries the requested level first, then progressively lower levels (the
        API answers ``code -110`` when a level is not licensed for the caller).
        Returns a dict with ``url``, ``level``, ``br``, ``size``, ``type`` and
        ``code``; ``self.last_url_error`` carries the reason on failure.
        """
        song_id = str(song_id)
        attempts: list[str] = []

        for level in quality_fallbacks(quality):
            payload = {
                "ids": json.dumps([int(song_id)]) if song_id.isdigit() else json.dumps([song_id]),
                "level": level,
                "encodeType": encode_type_for_level(level),
            }
            result = self._eapi("/api/song/enhance/player/url/v1", payload)
            entry = ((result or {}).get("data") or [None])[0]

            if not entry:
                # eapi came back empty/unsupported -> one legacy attempt.
                legacy = self._legacy("/song/enhance/player/url/v1",
                                      {"ids": payload["ids"], "id": song_id, "level": level})
                entry = ((legacy or {}).get("data") or [None])[0]
            if not entry:
                attempts.append(f"{level}:no_response")
                continue

            code = entry.get("code")
            if entry.get("url") and code == 200:
                info = {
                    "url": entry["url"],
                    "level": level,
                    "br": entry.get("br") or 0,
                    "size": entry.get("size") or 0,
                    "type": (entry.get("type") or encode_type_for_level(level)).lower(),
                    "md5": entry.get("md5"),
                    "fee": entry.get("fee", 0),
                    "code": code,
                }
                self.last_url_error = None
                return info

            attempts.append(f"{level}:{PLAY_URL_CODES.get(code, code)}")

        self.last_url_error = attempts[-1] if attempts else "unknown"
        self._log(f"no play url for {song_id}: {attempts}", "warning")
        return None

    def get_song_url(self, song_id: str, quality: str = "standard") -> str | None:
        """Backwards-compatible helper returning just the URL."""
        info = self.get_song_url_info(song_id, quality)
        return info["url"] if info else None

    # ------------------------------------------------------------------ search
    def search_songs(self, keyword: str, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        """Search songs (``eapi /api/cloudsearch/pc``, verified live 2026-10-01).

        The response carries the same ``ar``/``al``/``dt`` shape the playlist
        endpoints use, so the GUI can render both with one code path.
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        limit = max(1, min(int(limit or 30), 100))
        payload = {"s": keyword, "type": 1, "limit": limit, "offset": max(0, int(offset or 0)),
                   "total": "true"}
        # Use cache for first page of results only
        use_cache = offset == 0
        result = self._eapi(SEARCH_ENDPOINT, payload, use_cache=use_cache)
        songs = ((result or {}).get("result") or {}).get("songs")
        if not songs:
            legacy = self._legacy("/search/get",
                                  {"s": keyword, "type": 1, "limit": limit,
                                   "offset": max(0, int(offset or 0))}, use_cache=use_cache)
            songs = ((legacy or {}).get("result") or {}).get("songs")
        return [song for song in (songs or []) if isinstance(song, dict)]

    # ----------------------------------------------------------- discovery
    def get_album_detail(self, album_id: str) -> dict[str, Any] | None:
        """Fetch album detail including tracks."""
        album_id = str(album_id or "").strip()
        if not album_id:
            return None
        # Try eapi first
        result = self._eapi(f"{ALBUM_ENDPOINT}/{album_id}", {"id": album_id})
        album = (result or {}).get("album") or (result or {}).get("data")
        if not album:
            legacy = self._legacy(f"{ALBUM_ENDPOINT}/{album_id}", {"id": album_id})
            album = (legacy or {}).get("album") or (legacy or {}).get("data")
        return album

    def get_artist_detail(self, artist_id: str) -> dict[str, Any] | None:
        """Fetch artist detail including hot songs, albums, description."""
        artist_id = str(artist_id or "").strip()
        if not artist_id:
            return None
        result = self._eapi(f"{ARTIST_ENDPOINT}/{artist_id}", {"id": artist_id})
        artist = (result or {}).get("artist") or (result or {}).get("data")
        if not artist:
            legacy = self._legacy(f"{ARTIST_ENDPOINT}/{artist_id}", {"id": artist_id})
            artist = (legacy or {}).get("artist") or (legacy or {}).get("data")
        return artist

    def get_recommend_playlists(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get daily recommended playlists (personalized, needs login)."""
        payload = {"limit": limit, "total": "true"}
        result = self._eapi(RECOMMEND_PLAYLIST_ENDPOINT, payload)
        playlists = (result or {}).get("recommend") or (result or {}).get("playlists")
        if not playlists:
            legacy = self._legacy("/discovery/recommend/resource", payload)
            playlists = (legacy or {}).get("recommend") or (legacy or {}).get("playlists")
        return [p for p in (playlists or []) if isinstance(p, dict)]

    def get_personal_fm(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get personal FM track list (personalized radio, needs login)."""
        payload = {"limit": limit}
        result = self._eapi(PERSONAL_FM_ENDPOINT, payload)
        data = (result or {}).get("data")
        if not data:
            legacy = self._legacy("/personal_fm", payload)
            data = (legacy or {}).get("data")
        return [song for song in (data or []) if isinstance(song, dict)]

    def get_new_songs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Get newly released songs."""
        payload = {"limit": limit, "offset": offset, "area": "ALL", "type": "new"}
        result = self._eapi(NEW_SONGS_ENDPOINT, payload)
        songs = (result or {}).get("data") or (result or {}).get("songs")
        if not songs:
            legacy = self._legacy("/discovery/new/songs", payload)
            songs = (legacy or {}).get("data") or (legacy or {}).get("songs")
        return [song for song in (songs or []) if isinstance(song, dict)]

    def get_recommend_mvs(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """Get recommended MVs."""
        payload = {"limit": limit, "offset": offset}
        result = self._eapi(RECOMMEND_MV_ENDPOINT, payload)
        mvs = (result or {}).get("result") or (result or {}).get("mvs")
        if not mvs:
            legacy = self._legacy("/mv/recommend", payload)
            mvs = (legacy or {}).get("result") or (legacy or {}).get("mvs")
        return [mv for mv in (mvs or []) if isinstance(mv, dict)]

    def get_top_lists(self) -> list[dict[str, Any]]:
        """Get official toplist/charts (uses special playlist IDs)."""
        # NetEase toplist IDs (verified):
        toplist_ids = {
            "飙升榜": "3778678",
            "新歌榜": "3779629",
            "热歌榜": "3779630",
            "原创榜": "2884035",
            "华语榜": "19723756",
            "欧美榜": "19723757",
            "日韩榜": "19723758",
            "网络歌曲榜": "19723759",
            "抖音榜": "2250011882",
            "电音榜": "10169002",
            "UK榜": "2023401535",
            "美国榜": "2023401536",
            "韩国榜": "2023401537",
            "日本榜": "2023401538",
            "古典榜": "71385702",
            "爵士榜": "71385703",
            "乡村榜": "71385704",
            "说唱榜": "991319590",
            "轻音乐榜": "71385705",
        }
        toplists = []
        for name, pid in toplist_ids.items():
            info = self.get_playlist_info(pid, max_tracks=100)
            if info:
                toplists.append({
                    "name": name,
                    "id": pid,
                    "coverImgUrl": info.get("coverImgUrl") or "",
                    "trackCount": info.get("trackCount") or 0,
                    "updateFrequency": info.get("updateFrequency") or "每日更新",
                    "description": info.get("description") or "",
                })
        return toplists

    def get_top_list_tracks(self, toplist_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Get tracks for a specific toplist."""
        info = self.get_playlist_info(toplist_id, max_tracks=limit)
        if not info:
            return []
        tracks = info.get("tracks") or []
        return tracks

    def search_suggest(self, keyword: str, limit: int = 10) -> list[str]:
        """Get search suggestions/autocomplete."""
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        payload = {"s": keyword, "limit": limit, "type": "mobile"}
        result = self._eapi(SEARCH_SUGGEST_ENDPOINT, payload)
        suggestions = (result or {}).get("result", {}).get("suggests")
        if not suggestions:
            legacy = self._legacy("/search/suggest", {"s": keyword, "limit": limit})
            suggestions = (legacy or {}).get("result", {}).get("suggests")
        return [s for s in (suggestions or []) if isinstance(s, str)]

    def search_multi(self, keyword: str, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """Search multiple types: songs, playlists, artists, albums, mvs, users, lyrics."""
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"songs": [], "playlists": [], "artists": [], "albums": [], "mvs": [], "users": [], "lyrics": []}

        payload = {"s": keyword, "type": 1000, "limit": limit, "offset": offset, "total": "true"}
        # type=1000 means "all types"
        result = self._eapi(SEARCH_MULTI_ENDPOINT, payload)
        res = (result or {}).get("result") or {}

        return {
            "songs": [song for song in (res.get("songs") or []) if isinstance(song, dict)],
            "playlists": [p for p in (res.get("playlists") or []) if isinstance(p, dict)],
            "artists": [a for a in (res.get("artists") or []) if isinstance(a, dict)],
            "albums": [a for a in (res.get("albums") or []) if isinstance(a, dict)],
            "mvs": [m for m in (res.get("mvs") or []) if isinstance(m, dict)],
            "users": [u for u in (res.get("userprofiles") or []) if isinstance(u, dict)],
            "lyrics": [lyric for lyric in (res.get("lyrics") or []) if isinstance(lyric, dict)],
        }

    # ----------------------------------------------------------- account music
    def get_user_playlists(self, uid: str, limit: int = 100,
                           offset: int = 0, fresh: bool = False) -> dict[str, Any]:
        """All playlists of ``uid`` -- ``{"playlists": [...], "more": bool}``.

        Verified live 2026-10-01: ``eapi /api/user/playlist`` answers ``code 200``
        with 100 entries per page plus a ``more`` flag (the ``/api/v1/...``
        variant is 404).
        """
        uid = str(uid or "").strip()
        if not uid:
            return {"playlists": [], "more": False}
        cache = get_meta_cache()
        page_key = f"userplaylists:{uid}:{limit}:{offset}"
        if offset == 0 and not fresh:
            cached = cache.get_playlist(page_key)
            if cached:
                return cached
        payload = {"uid": int(uid) if uid.isdigit() else uid,
                   "limit": max(1, min(int(limit or 100), 1000)),
                   "offset": max(0, int(offset or 0)),
                   "includeVideo": "true"}
        result = self._eapi(USER_PLAYLIST_ENDPOINT, payload, use_cache=not fresh)
        playlists = (result or {}).get("playlist")
        if not isinstance(playlists, list):
            legacy = self._legacy("/user/playlist", payload)
            playlists = (legacy or {}).get("playlist") or []
        answer = {"playlists": [p for p in playlists if isinstance(p, dict)],
                  "more": bool((result or {}).get("more"))}
        if offset == 0:
            # Only the first page is cached; the rest is paged through in
            # one go by get_user_playlists_all.
            cache.set_playlist(page_key, answer)
        return answer

    def get_user_playlists_all(self, uid: str, max_pages: int = 20,
                               fresh: bool = False) -> list[dict[str, Any]]:
        """Page through :meth:`get_user_playlists` until ``more`` is false."""
        collected: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max(1, max_pages)):
            page = self.get_user_playlists(uid, limit=100, offset=offset, fresh=fresh)
            batch = page["playlists"]
            collected.extend(batch)
            if not page["more"] or len(batch) < 100:
                break
            offset += len(batch)
        return collected

    def get_liked_song_ids(self, uid: str, use_cache: bool = True) -> list[str]:
        """Ids of the songs in the user's "liked" list (needs a login).

        ``use_cache=False`` reads the list straight from the server.  The
        response cache holds the answer for a minute, which is fine for a
        read but wrong right after a like or an unlike: the UI would keep
        showing the old hearts.
        """
        uid = str(uid or "").strip()
        if not uid:
            return []
        result = self._eapi(LIKE_LIST_ENDPOINT,
                            {"uid": int(uid) if uid.isdigit() else uid},
                            use_cache=use_cache)
        ids = (result or {}).get("ids")
        if not isinstance(ids, list):
            return []
        return [str(item) for item in ids]

    def set_song_like(self, song_id: str, like: bool = True) -> str:
        """Like / unlike one song.

        Returns one of:

        ``"ok"``
            the server accepted the change
        ``"unsupported"``
            the track itself is refused (``code 524``) -- NetEase answers
            ``当前环境异常，已取消喜欢`` for a group of tracks and no request
            shape gets past it, so this is a server-side restriction on the
            track rather than something the client can fix
        ``"rate_limited"``
            the request came too fast (``code 405``); it succeeds when retried
            after a pause
        ``"not_logged_in"``
            the session is gone (``code 301``)
        ``"failed"``
            anything else (network error, an unexpected code)

        The distinction matters: "unsupported" and "rate_limited" need
        different messages, and both are *not* the user's fault.
        """
        song_id = str(song_id or "").strip()
        if not song_id:
            return "failed"
        payload = {"trackId": int(song_id) if song_id.isdigit() else song_id,
                   "like": "true" if like else "false",
                   "alg": "itembased", "time": 3}
        result = self._eapi(LIKE_ENDPOINT, payload, use_cache=False)
        code = (result or {}).get("code")
        if code == 200:
            # The liked ids, and with them every heart and the "liked music"
            # playlist, are now different.  Cached answers would keep
            # showing the old state until they expired.
            self.invalidate_liked()
            return "ok"
        if code in (524, 512):
            # The reference implementation uses /api/radio/like; it answers
            # the same way for the same tracks, but try it anyway -- a
            # different route can carry a different risk-control decision.
            legacy = self._eapi(LIKE_RADIO_ENDPOINT, payload, use_cache=False)
            legacy_code = (legacy or {}).get("code")
            if legacy_code == 200:
                self.invalidate_liked()
                return "ok"
            code = legacy_code if legacy_code is not None else code
        if code == 405:
            return "rate_limited"
        if code in LOGIN_REQUIRED_CODES:
            return "not_logged_in"
        if code == 524:
            return "unsupported"
        self._log(f"like failed for {song_id}: code={code}", "warning")
        return "failed"

    def report_play(self, song_id: str, seconds: float, source_id: int = 0,
                    played_at: float | None = None) -> bool:
        """Tell NetEase that a track was played (best effort).

        Local play counting never depends on this; a failure here is only
        logged, because the account's listening history is a bonus.
        """
        song_id = str(song_id or "").strip()
        if not song_id:
            return False
        entry = {
            "action": "play",
            "json": {
                "id": int(song_id) if song_id.isdigit() else song_id,
                "type": "song",
                "time": max(0, int(seconds or 0)),
                "playedTime": max(0, int(seconds or 0)),
                "sourceid": str(source_id or 0),
                "mainsite": 1,
                "download": 0,
                "end": "playend",
            },
        }
        stamp = int((played_at or time.time()) * 1000)
        result = self._eapi(WEBLOG_ENDPOINT, {"logs": json.dumps([entry], ensure_ascii=False),
                                              "ts": stamp})
        ok = (result or {}).get("code") == 200
        if not ok:
            self._log(f"play report for {song_id} refused: {(result or {}).get('code')}", "debug")
        return ok

    # ------------------------------------------------------------------- misc
    def get_lyrics(self, song_id: str) -> dict[str, Any] | None:
        """Fetch the raw lyrics of ``song_id``.

        Returns ``{"lyric": str, "translation": str, "code": 200}`` or ``None``
        when the song has none / the call failed.  Merging and writing is done by
        :mod:`src.core.lyrics`.
        """
        song_id = str(song_id)
        cache = get_meta_cache()
        cached = cache.get_lyric(song_id)
        if cached:
            return cached
        payload = {
            "id": int(song_id) if song_id.isdigit() else song_id,
            "lv": -1, "kv": -1, "tv": -1,
        }
        result = self._eapi(LYRIC_ENDPOINT, payload)
        if not result:
            # Kept for networks where the plain route still answers.
            result = self._legacy("/song/lyric", {"id": song_id, "lv": -1, "kv": -1, "tv": -1})
        if not result or result.get("code") != 200:
            self._log(f"no lyrics for {song_id}: {(result or {}).get('code')}", "debug")
            return None

        translation = ""
        tlyric = result.get("tlyric")
        if isinstance(tlyric, dict):
            translation = tlyric.get("lyric") or ""
        lyrics = {
            "lyric": ((result.get("lrc") or {}).get("lyric") or ""),
            "translation": translation,
            "code": 200,
        }
        cache.set_lyric(song_id, lyrics)
        return lyrics

    def get_user_detail(self, user_id: str) -> dict[str, Any] | None:
        result = self._legacy(f"/v1/user/detail/{user_id}")
        if result and not result.get("code"):
            return result
        return None

    # ----------------------------------------------------------- playlist management
    def create_playlist(self, name: str, privacy: int = 0, description: str = "") -> dict[str, Any] | None:
        """Create a new playlist. privacy: 0=public, 10=private."""
        uid = self.login_manager.user_info
        if not uid or not uid.user_id:
            return None
        payload = {
            "name": name,
            "privacy": privacy,
            "description": description,
        }
        # No caching: a write must always reach the server.
        result = self._eapi(PLAYLIST_CREATE_ENDPOINT, payload, use_cache=False)
        if (result or {}).get("code") == 200:
            self.invalidate_user_playlists()
        return result

    def update_playlist(self, playlist_id: str, name: str | None = None,
                        description: str | None = None, privacy: int | None = None) -> dict[str, Any] | None:
        """Update playlist metadata.

        The rename lives on its own route (``/api/playlist/update/name``);
        the description and the privacy flag have theirs.  ``/api/batch``
        takes a mapping of route -> JSON body and runs them in one request,
        which is what the reference implementation does -- and it keeps the
        number of round trips down when several fields change at once.
        """
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        batch: dict[str, str] = {}
        if name is not None:
            batch["/api/playlist/update/name"] = json.dumps(
                {"id": playlist_id, "name": name}, ensure_ascii=False)
        if description is not None:
            batch["/api/playlist/desc/update"] = json.dumps(
                {"id": playlist_id, "desc": description}, ensure_ascii=False)
        if privacy is not None:
            batch["/api/playlist/update/privacy"] = json.dumps(
                {"id": playlist_id, "privacy": privacy}, ensure_ascii=False)
        if not batch:
            return None
        result = self._eapi("/api/batch", batch, use_cache=False)
        result = self._batch_result(batch, result)
        if (result or {}).get("code") == 200:
            # The name and the cover of the playlist changed, and the
            # account's playlist list shows the old name.
            self.invalidate_playlist(playlist_id)
            self.invalidate_user_playlists()
        return result

    @staticmethod
    def _batch_result(batch: dict[str, str],
                      result: dict[str, Any] | None) -> dict[str, Any] | None:
        """The real answer of a ``/api/batch`` call.

        The outer ``code`` of a batch is **always** 200, whatever the routes
        inside it answered -- so reading it tells the caller nothing.  Each
        inner route has its own answer, keyed by its path, and that is where
        a failure shows up: a rename without the client identity comes back
        as ``inner code 403 illegal request!`` under a cheerful outer 200.

        The first inner failure is returned as the result of the call, so
        the caller sees the code and the message that actually happened.
        """
        if not result:
            return result
        for route in batch:
            inner = result.get(route)
            if isinstance(inner, dict) and inner.get("code") not in (None, 200):
                return {"code": inner.get("code"),
                        "msg": inner.get("msg") or inner.get("message") or "",
                        "route": route}
        return result

    def delete_playlist(self, playlist_ids: list[str]) -> dict[str, Any] | None:
        """Delete one or more playlists."""
        ids = [str(i) for i in playlist_ids if str(i).strip()]
        if not ids:
            return None
        payload = {"ids": json.dumps(ids)}
        result = self._eapi(PLAYLIST_DELETE_ENDPOINT, payload, use_cache=False)
        if (result or {}).get("code") == 200:
            for playlist_id in ids:
                self.invalidate_playlist(playlist_id)
            self.invalidate_user_playlists()
        return result

    def _manipulate_playlist_tracks(self, playlist_id: str, track_ids: list[str],
                                    op: str) -> dict[str, Any] | None:
        """Add or remove tracks in a playlist.

        The live route is ``/api/playlist/manipulate/tracks`` and it wants
        ``trackIds`` as a JSON string plus ``imme``; the older
        ``/api/playlist/tracks/add`` answers ``404``.
        """
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        ids = [str(i) for i in track_ids if str(i).strip()]
        if not ids:
            return None
        payload = {
            "op": op,
            "pid": playlist_id,
            "trackIds": json.dumps(ids),
            "imme": "true",
        }
        result = self._eapi(PLAYLIST_TRACKS_ENDPOINT, payload, use_cache=False)
        if (result or {}).get("code") == 200:
            # The track list of this playlist is different now.  The
            # in-memory playlist cache holds the whole playlist object, so it
            # has to go too -- otherwise it would keep serving the old list.
            self.invalidate_playlist(playlist_id)
        return result

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> dict[str, Any] | None:
        """Add tracks to a playlist."""
        return self._manipulate_playlist_tracks(playlist_id, track_ids, "add")

    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: list[str]) -> dict[str, Any] | None:
        """Remove tracks from a playlist."""
        return self._manipulate_playlist_tracks(playlist_id, track_ids, "del")

    def subscribe_playlist(self, playlist_id: str, subscribe: bool = True) -> dict[str, Any] | None:
        """Subscribe or unsubscribe from a playlist.

        The two directions have **separate routes**: ``/api/playlist/subscribe``
        and ``/api/playlist/unsubscribe``.  The old shape -- one route with a
        ``t`` field -- is what the reference implementation used to send and
        what this app sent until 2026-10-25; NetEase answers it with
        ``code 403 illegal request!`` on the write routes.  See
        ``docs/notes.md`` section 21.
        """
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        if subscribe:
            endpoint = PLAYLIST_SUBSCRIBE_ENDPOINT
        else:
            endpoint = PLAYLIST_UNSUBSCRIBE_ENDPOINT
        # The payload is just the id -- the direction is in the route, not in
        # a ``t`` field.  The old shape (one route, ``t`` 1 or 2) is what
        # this app sent until 2026-10-25; NetEase answers the ``t`` form on
        # the write routes with ``code 403 illegal request!``.
        payload = {"id": playlist_id}
        result = self._eapi(endpoint, payload, use_cache=False)
        if (result or {}).get("code") == 200:
            # The account's playlist list gains or loses this playlist.
            self.invalidate_user_playlists()
            self.invalidate_playlist(playlist_id)
        return result

    def update_playlist_cover(self, playlist_id: str, cover_url: str) -> dict[str, Any] | None:
        """Update playlist cover image."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        payload = {
            "playlistId": playlist_id,
            "imgUrl": cover_url,
        }
        result = self._eapi(PLAYLIST_COVER_UPDATE_ENDPOINT, payload, use_cache=False)
        if (result or {}).get("code") == 200:
            self.invalidate_playlist(playlist_id)
        return result

    def get_playlist_detail_dynamic(self, playlist_id: str) -> dict[str, Any] | None:
        """Get playlist detail with track operations support."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        result = self._eapi(PLAYLIST_DETAIL_DYNAMIC_ENDPOINT, {"id": playlist_id})
        return (result or {}).get("playlist") or (result or {}).get("data")

    def clear_playlist_cache(self) -> None:
        self._playlist_cache.clear()

    def clear_response_cache(self) -> None:
        """Clear the response cache."""
        self._response_cache.clear()

    def get_request_stats(self) -> dict[str, Any]:
        stats = dict(self.stats)
        if stats["total_requests"]:
            stats["success_rate"] = (
                f"{stats['successful_requests'] / stats['total_requests'] * 100:.1f}%"
            )
        return stats


_api_instance: NeteaseAPI | None = None
_api_lock = threading.Lock()


def get_api(debug: bool = False) -> NeteaseAPI:
    """Return the process-wide API client."""
    global _api_instance
    with _api_lock:
        if _api_instance is None:
            _api_instance = NeteaseAPI(debug=debug)
        elif debug:
            _api_instance.debug = True
        return _api_instance
