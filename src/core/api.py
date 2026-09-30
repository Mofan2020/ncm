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
account / login status ``/api/w/nuser/account/get``              eapi
QR key                 ``/api/login/qrcode/unikey``             legacy
QR poll                ``/api/login/qrcode/client/login``       legacy
SMS captcha            ``/api/sms/captcha/sent``                legacy
phone login            ``/api/w/login/cellphone``               legacy
logout                 ``/api/logout``                          legacy
=====================  =======================================  =========

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

__all__ = ["NeteaseAPI", "get_api", "QUALITY_ORDER", "quality_fallbacks"]

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
    MIN_REQUEST_INTERVAL = 0.30
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

    def _eapi(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Signed eapi call with retries and host rotation."""
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
    ) -> dict[str, Any] | None:
        """Call one of the still-served plain ``/api/*`` routes."""
        url = f"{self.LEGACY_HOST}{endpoint}"
        payload = dict(params or {})
        payload.setdefault("timestamp", int(time.time() * 1000))
        for attempt in range(self.MAX_RETRIES):
            try:
                if method.upper() == "GET":
                    resp = self._get(url, payload)
                else:
                    resp = self._post(url, payload)
                if resp.status_code == 200 and resp.text.strip():
                    result = resp.json()
                    self.stats["successful_requests"] += 1
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

    def clear_playlist_cache(self) -> None:
        self._playlist_cache.clear()

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
