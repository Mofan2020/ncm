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
from src.core.crypto import eapi_body, encode_type_for_level
from src.core.lyrics import LYRIC_ENDPOINT

__all__ = ["NeteaseAPI", "get_api", "QUALITY_ORDER", "quality_fallbacks",
           "SEARCH_ENDPOINT", "USER_PLAYLIST_ENDPOINT", "LIKE_ENDPOINT",
           "LIKE_LIST_ENDPOINT", "WEBLOG_ENDPOINT"]

#: Search / account endpoints (all verified live on 2026-10-01).
SEARCH_ENDPOINT = "/api/cloudsearch/pc"
USER_PLAYLIST_ENDPOINT = "/api/user/playlist"
LIKE_ENDPOINT = "/api/song/like"
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

#: Playlist management endpoints (verified live 2026-10-01 onwards)
PLAYLIST_CREATE_ENDPOINT = "/api/playlist/create"
PLAYLIST_UPDATE_ENDPOINT = "/api/playlist/update"
PLAYLIST_DELETE_ENDPOINT = "/api/playlist/delete"
PLAYLIST_TRACKS_ADD_ENDPOINT = "/api/playlist/tracks/add"
PLAYLIST_TRACKS_DEL_ENDPOINT = "/api/playlist/tracks/del"
PLAYLIST_SUBSCRIBE_ENDPOINT = "/api/playlist/subscribe"
PLAYLIST_DETAIL_DYNAMIC_ENDPOINT = "/api/playlist/detail/dynamic"  # for track operations
PLAYLIST_COVER_UPDATE_ENDPOINT = "/api/playlist/cover/update"

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

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.login_manager = get_login_manager()
        self.settings = get_settings()

        self._host_index = 0
        self._request_lock = threading.Lock()
        self._last_request_ts = 0.0

        # Session (cookies shared with the login manager).
        self.session = self.login_manager.get_session()

        self.stats: dict[str, Any] = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "host_switches": 0,
        }

        self._playlist_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # Cache for search and other API responses
        self._response_cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = 60.0  # 60 seconds cache TTL
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

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15"
            ),
            "Referer": "https://music.163.com/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _post(self, url: str, data: dict[str, Any], timeout: tuple[int, int] = (5, 20)):
        self._throttle()
        self.stats["total_requests"] += 1
        return self.session.post(url, data=data, headers=self._headers(), timeout=timeout)

    def _get(self, url: str, params: dict[str, Any], timeout: tuple[int, int] = (5, 20)):
        self._throttle()
        self.stats["total_requests"] += 1
        return self.session.get(url, params=params, headers=self._headers(), timeout=timeout)

    # --------------------------------------------------------------- eapi API
    def _eapi_once(self, path: str, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """Single attempt at a signed eapi call (no retries)."""
        body = eapi_body(path, data)
        url = f"{self.eapi_host}/eapi{path}"
        try:
            resp = self._post(url, body)
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

    def _set_cached(self, cache_key: str, value: Any) -> None:
        """Cache a response."""
        self._response_cache[cache_key] = (time.time() + self._cache_ttl, value)

    def _eapi(self, path: str, data: dict[str, Any] | None = None, use_cache: bool = True) -> dict[str, Any] | None:
        """Signed eapi call with retries, host rotation, and optional caching."""
        cache_key = self._cache_key(path, data) if use_cache else None
        if cache_key:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached

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
                self.stats["successful_requests"] += 1
                if cache_key:
                    self._set_cached(cache_key, result)
                return result
            if attempt < self.MAX_RETRIES - 1:
                self._switch_host()
                time.sleep(0.4 * (attempt + 1) + random.uniform(0.1, 0.4))

        self.stats["failed_requests"] += 1
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
        cache_key = self._cache_key(endpoint, payload) if use_cache else None

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
                        self._set_cached(cache_key, result)
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
    def get_playlist_info(self, playlist_id: str, max_tracks: int = 1000) -> dict[str, Any] | None:
        """Fetch playlist metadata (with up to ``max_tracks`` tracks inline)."""
        cache_key = str(playlist_id)
        cached = self._playlist_cache.get(cache_key)
        if cached and time.time() - cached[0] < self.PLAYLIST_CACHE_TTL:
            return cached[1]

        payload = {"id": int(playlist_id) if str(playlist_id).isdigit() else playlist_id,
                   "n": max_tracks, "s": 8}
        result = self._eapi("/api/v6/playlist/detail", payload)
        playlist = (result or {}).get("playlist")
        if not playlist:
            legacy = self._legacy("/v3/playlist/detail", {"id": playlist_id, "n": max_tracks})
            playlist = (legacy or {}).get("playlist") or (legacy or {}).get("result")
        if not playlist:
            return None

        self._playlist_cache[cache_key] = (time.time(), playlist)
        return playlist

    def get_playlist_songs(self, playlist_id: str) -> list[dict[str, Any]] | None:
        """Return the complete track list of a playlist, in playlist order.

        Playlists longer than the inline page size are completed through
        ``/api/v3/song/detail`` using the ``trackIds`` of the playlist.
        """
        info = self.get_playlist_info(playlist_id)
        if not info:
            return None

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
        """Fetch details for many songs, batching the requests."""
        ids = [str(i) for i in song_ids]
        songs: list[dict[str, Any]] = []
        for start in range(0, len(ids), batch_size):
            batch = ids[start : start + batch_size]
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
            songs.extend(batch_songs or [])
        return songs

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
                           offset: int = 0) -> dict[str, Any]:
        """All playlists of ``uid`` -- ``{"playlists": [...], "more": bool}``.

        Verified live 2026-10-01: ``eapi /api/user/playlist`` answers ``code 200``
        with 100 entries per page plus a ``more`` flag (the ``/api/v1/...``
        variant is 404).
        """
        uid = str(uid or "").strip()
        if not uid:
            return {"playlists": [], "more": False}
        payload = {"uid": int(uid) if uid.isdigit() else uid,
                   "limit": max(1, min(int(limit or 100), 1000)),
                   "offset": max(0, int(offset or 0)),
                   "includeVideo": "true"}
        result = self._eapi(USER_PLAYLIST_ENDPOINT, payload)
        playlists = (result or {}).get("playlist")
        if not isinstance(playlists, list):
            legacy = self._legacy("/user/playlist", payload)
            playlists = (legacy or {}).get("playlist") or []
        return {"playlists": [p for p in playlists if isinstance(p, dict)],
                "more": bool((result or {}).get("more"))}

    def get_user_playlists_all(self, uid: str, max_pages: int = 20) -> list[dict[str, Any]]:
        """Page through :meth:`get_user_playlists` until ``more`` is false."""
        collected: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max(1, max_pages)):
            page = self.get_user_playlists(uid, limit=100, offset=offset)
            batch = page["playlists"]
            collected.extend(batch)
            if not page["more"] or len(batch) < 100:
                break
            offset += len(batch)
        return collected

    def get_liked_song_ids(self, uid: str) -> list[str]:
        """Ids of the songs in the user's "liked" list (needs a login)."""
        uid = str(uid or "").strip()
        if not uid:
            return []
        result = self._eapi(LIKE_LIST_ENDPOINT, {"uid": int(uid) if uid.isdigit() else uid})
        ids = (result or {}).get("ids")
        if not isinstance(ids, list):
            return []
        return [str(item) for item in ids]

    def set_song_like(self, song_id: str, like: bool = True) -> bool:
        """Like / unlike one song.  Returns whether the call was accepted."""
        song_id = str(song_id or "").strip()
        if not song_id:
            return False
        payload = {"trackId": int(song_id) if song_id.isdigit() else song_id,
                   "like": "true" if like else "false",
                   "alg": "itembased", "time": 3}
        result = self._eapi(LIKE_ENDPOINT, payload)
        code = (result or {}).get("code")
        if code == 200:
            return True
        # `/api/radio/like` is the older route; it still answers on some networks.
        legacy = self._eapi("/api/radio/like", payload)
        return (legacy or {}).get("code") == 200

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
        return {
            "lyric": ((result.get("lrc") or {}).get("lyric") or ""),
            "translation": translation,
            "code": 200,
        }

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
        result = self._eapi(PLAYLIST_CREATE_ENDPOINT, payload)
        return result

    def update_playlist(self, playlist_id: str, name: str | None = None,
                        description: str | None = None, privacy: int | None = None) -> dict[str, Any] | None:
        """Update playlist metadata."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        payload = {"id": playlist_id}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if privacy is not None:
            payload["privacy"] = privacy
        result = self._eapi(PLAYLIST_UPDATE_ENDPOINT, payload)
        return result

    def delete_playlist(self, playlist_ids: list[str]) -> dict[str, Any] | None:
        """Delete one or more playlists."""
        ids = [str(i) for i in playlist_ids if str(i).strip()]
        if not ids:
            return None
        payload = {"ids": json.dumps(ids)}
        result = self._eapi(PLAYLIST_DELETE_ENDPOINT, payload)
        return result

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> dict[str, Any] | None:
        """Add tracks to a playlist."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        ids = [str(i) for i in track_ids if str(i).strip()]
        if not ids:
            return None
        payload = {
            "pid": playlist_id,
            "trackIds": json.dumps(ids),
            "op": "add",
        }
        result = self._eapi(PLAYLIST_TRACKS_ADD_ENDPOINT, payload)
        if not result:
            # Try alternative endpoint
            result = self._legacy("/playlist/tracks/add", payload)
        return result

    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: list[str]) -> dict[str, Any] | None:
        """Remove tracks from a playlist."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        ids = [str(i) for i in track_ids if str(i).strip()]
        if not ids:
            return None
        payload = {
            "pid": playlist_id,
            "trackIds": json.dumps(ids),
            "op": "del",
        }
        result = self._eapi(PLAYLIST_TRACKS_DEL_ENDPOINT, payload)
        if not result:
            result = self._legacy("/playlist/tracks/del", payload)
        return result

    def subscribe_playlist(self, playlist_id: str, subscribe: bool = True) -> dict[str, Any] | None:
        """Subscribe or unsubscribe from a playlist."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return None
        payload = {
            "id": playlist_id,
            "t": 1 if subscribe else 2,  # 1=subscribe, 2=unsubscribe
        }
        result = self._eapi(PLAYLIST_SUBSCRIBE_ENDPOINT, payload)
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
        result = self._eapi(PLAYLIST_COVER_UPDATE_ENDPOINT, payload)
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
