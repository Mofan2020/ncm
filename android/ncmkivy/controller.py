"""The boundary between the Kivy UI and the shared business core.

Every screen talks to a :class:`Controller` and nothing else.  The controller
is the only place that knows about ``src.core``; the Kivy layer never imports
``NeteaseAPI`` directly.  That split is what lets the desktop build and the
Android build share the same ~12000 lines of business logic without either of
them reaching into the other's presentation code.

The methods here mirror the semantics of the desktop bridge
(``src/gui/bridge.py``) but return plain Python values instead of JSON-safe
dicts, and they never touch a webview.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.auth import get_login_manager
from src.config import get_settings
from src.core import get_api
from src.core.downloader import SongDownloader
from src.core.library import get_library, track_key
from src.core.localmusic import get_local_music
from src.core.lyrics import build_timed_lyrics, lyrics_path_for
from src.core.mediaserver import get_media_server

log = logging.getLogger("ncm.kivy.controller")


__all__ = ["Controller", "Track"]


#: Punctuation and spacing that must not take part in the online <-> local
#: title matching.  Same rule as the desktop bridge.
_MATCH_NOISE = re.compile(
    r"[\s　()（）\[\]【】<>《》,，.。!！?？'\"“”‘’\-_~·・:：;；/\\|+&]+"
)


def _match_key(text: Any) -> str:
    """Normalise a title/artist for the online <-> local matching index."""
    return _MATCH_NOISE.sub("", str(text or "").lower())


class Track(dict):
    """A playable item.

    It is a plain dict so it can cross the thread boundary between the
    network layer and the UI without any conversion step; the keys are the
    same ones the desktop bridge produces.
    """

    @property
    def id(self) -> str:
        return str(self.get("id") or "")

    @property
    def name(self) -> str:
        return str(self.get("name") or "")

    @property
    def artists(self) -> str:
        return str(self.get("artists") or "")

    @property
    def album(self) -> str:
        return str(self.get("album") or "")

    @property
    def duration(self) -> int:
        return int(self.get("duration") or 0)

    @property
    def source(self) -> str:
        return str(self.get("source") or "online")

    @property
    def cover_url(self) -> str:
        return str(self.get("cover_url") or "")

    @property
    def vip(self) -> bool:
        """Whether the track is a paid one."""
        return bool(self.get("vip"))

    @property
    def key(self) -> str:
        return str(self.get("key") or track_key(self.source, self.id))


class Controller:
    """Everything the Android UI is allowed to ask for."""

    def __init__(self) -> None:
        self.api = get_api()
        self.login = get_login_manager()
        self.settings = get_settings()
        self.library = get_library()
        self.media = get_media_server()
        self.local_music = get_local_music()
        self._downloader: SongDownloader | None = None
        self._download_thread: threading.Thread | None = None
        #: Local-file index for the online <-> local matching.
        self._match_index: dict[str, dict[str, Any]] = {}
        self._match_index_token: Any = None
        #: Called with a progress dict while a download runs.
        self.on_download_progress: Callable[[dict[str, Any]], None] | None = None
        self.on_download_finished: Callable[[dict[str, Any]], None] | None = None
        self.on_login_changed: Callable[[], None] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Start the loopback media server (playback goes through it)."""
        try:
            self.media.start()
        except Exception as exc:  # pragma: no cover - port already bound
            log.warning("media server did not start: %s", exc)

    def stop(self) -> None:
        try:
            self.media.stop()
        except Exception:  # pragma: no cover - best effort
            pass

    # ----------------------------------------------------------------- login
    def login_status(self) -> dict[str, Any]:
        user = self.login.user_info
        return {
            "logged_in": bool(self.login.is_logged_in),
            "user_id": str(getattr(user, "user_id", "") or ""),
            "nickname": str(getattr(user, "nickname", "") or ""),
            "avatar_url": str(getattr(user, "avatar_url", "") or ""),
            "vip_type": int(getattr(user, "vip_type", 0) or 0),
        }

    def start_qr_login(self) -> dict[str, Any]:
        """Ask for a QR code.

        The QR *image* is not produced here: rendering it as a PNG needs
        Pillow, which this build does not ship.  The raw login URL is
        returned instead and the screen draws it from a pure-Python QR
        matrix.
        """
        ok = self.login.login_qrcode()
        key = getattr(self.login, "_qrcode_key", "")
        url = f"{self.login.BASE_URL}/login?codekey={key}" if key else ""
        return {"success": bool(ok), "qrcode_url": url}
    def login_phone(self, phone: str, code: str, ctcode: str = "86") -> dict[str, Any]:
        result = self.login.login_phone(phone, code, ctcode)
        return dict(result)

    def logout(self) -> bool:
        self.login.logout()
        return True

    # -------------------------------------------------------------- playlists
    def list_playlists(self) -> list[dict[str, Any]]:
        uid = str(getattr(self.login.user_info, "user_id", "") or "")
        if not uid:
            return []
        raw = self.api.get_user_playlists_all(uid)
        out: list[dict[str, Any]] = []
        for item in raw or []:
            out.append({
                "id": str(item.get("id") or ""),
                "name": item.get("name") or "",
                "cover_url": item.get("coverImgUrl") or item.get("picUrl") or "",
                "track_count": int(item.get("trackCount") or 0),
                "creator": (item.get("creator") or {}).get("nickname") or "",
                "subscribed": bool(item.get("subscribed")),
            })
        return out

    def playlist_tracks(self, playlist_id: str) -> list[Track]:
        raw = self.api.get_playlist_songs(str(playlist_id))
        return [self._track(song, index) for index, song in enumerate(raw or [])]

    def liked_tracks(self) -> list[Track]:
        uid = str(getattr(self.login.user_info, "user_id", "") or "")
        if not uid:
            return []
        ids = self.api.get_liked_song_ids(uid)
        if not ids:
            return []
        songs = self.api.get_songs_detail(ids)
        return [self._track(song, index) for index, song in enumerate(songs or [])]

    def recent_tracks(self, limit: int = 50) -> list[Track]:
        return [Track(item) for item in self.library.recent(limit=limit)]

    def favorites(self) -> list[Track]:
        return [Track(item) for item in self.library.favorites()]

    def set_favorite(self, track: dict[str, Any], favorite: bool = True) -> dict[str, Any]:
        """Favourite a track locally and mirror it to the cloud when signed in."""
        key = track_key(str(track.get("source") or "online"), str(track.get("id") or ""))
        self.library.set_favorite(key, favorite)
        if not favorite:
            return {"success": True, "cloud": "skipped"}
        if not self.login.is_logged_in:
            return {"success": True, "cloud": "not_logged_in"}
        status = self.api.set_song_like(str(track.get("id") or ""), True)
        return {"success": status == "ok", "cloud": status}

    # ---------------------------------------------------------------- search
    def search(self, keyword: str, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        results = self.api.search_multi(str(keyword or ""), limit=limit, offset=offset)
        artists = results.get("artists") or []
        albums = results.get("albums") or []
        playlists = results.get("playlists") or []
        songs = results.get("songs") or []
        return {
            "songs": [self._track(song, index) for index, song in enumerate(songs)],
            "artists": [
                {
                    "id": str(a.get("id") or ""),
                    "name": a.get("name") or "",
                    "cover_url": a.get("picUrl") or a.get("img1v1Url") or "",
                    "album_size": int(a.get("albumSize") or 0),
                }
                for a in artists
            ],
            "albums": [
                {
                    "id": str(a.get("id") or ""),
                    "name": a.get("name") or "",
                    "cover_url": a.get("picUrl") or "",
                    "artist": (a.get("artist") or {}).get("name") or "",
                }
                for a in albums
            ],
            "playlists": [
                {
                    "id": str(p.get("id") or ""),
                    "name": p.get("name") or "",
                    "cover_url": p.get("coverImgUrl") or p.get("picUrl") or "",
                    "track_count": int(p.get("trackCount") or 0),
                }
                for p in playlists
            ],
        }

    # ---------------------------------------------------------------- artist
    def artist_page(self, artist_id: str) -> dict[str, Any]:
        """The artist header plus their first page of songs.

        ``get_artist_detail`` returns the profile with the hot songs folded
        in, and the follow state comes from its own route -- the profile
        itself does not carry a reliable "followed" flag.
        """
        artist = self.api.get_artist_detail(str(artist_id))
        if not artist:
            return {"success": False, "error_key": "player.artist_not_found"}
        follow = self.api.get_artist_follow_state(str(artist_id))
        songs = self.api.get_artist_songs(str(artist_id), limit=50, offset=0)
        return {
            "success": True,
            "artist": {
                "id": str(artist.get("id") or ""),
                "name": artist.get("name") or "",
                "cover_url": artist.get("picUrl") or artist.get("img1v1Url") or "",
                "avatar_url": artist.get("img1v1Url") or artist.get("picUrl") or "",
                "description": artist.get("briefDesc") or "",
                "alias": list(artist.get("alias") or []),
                "music_size": int(artist.get("musicSize") or 0),
                "album_size": int(artist.get("albumSize") or 0),
                "followed": bool(follow.get("followed")),
                "fans_count": int(follow.get("fans_count") or 0),
            },
            "songs": [self._track(song, index)
                      for index, song in enumerate(songs.get("songs") or [])],
            "total": int(songs.get("total") or 0),
            "has_more": bool(songs.get("more")),
        }

    def artist_songs(self, artist_id: str, offset: int, limit: int = 50) -> dict[str, Any]:
        """One page of an artist's songs, for the "load more" button."""
        songs = self.api.get_artist_songs(str(artist_id), limit=limit, offset=offset)
        return {
            "songs": [self._track(song, index)
                      for index, song in enumerate(songs.get("songs") or [])],
            "total": int(songs.get("total") or 0),
            "has_more": bool(songs.get("more")),
        }

    def followed_artists(self, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """The artists the account follows.

        The core returns ``code`` alongside the list so "not signed in" can
        be told apart from "follows nobody".
        """
        raw = self.api.get_followed_artists(limit=limit, offset=offset)
        artists = [
            {
                "id": str(a.get("id") or ""),
                "name": a.get("name") or "",
                "cover_url": a.get("picUrl") or a.get("img1v1Url") or "",
                "alias": list(a.get("alias") or []),
                "album_size": int(a.get("albumSize") or 0),
            }
            for a in (raw.get("artists") or [])
        ]
        return {
            "artists": artists,
            "count": int(raw.get("count") or 0),
            "has_more": bool(raw.get("has_more")),
            "code": raw.get("code"),
        }

    def set_artist_follow(self, artist_id: str, follow: bool = True) -> dict[str, Any]:
        """Follow or unfollow an artist."""
        status = self.api.set_artist_follow(str(artist_id), follow)
        return {"success": status == "ok", "status": status}

    # -------------------------------------------------------------- playback
    def resolve(self, track: dict[str, Any]) -> dict[str, Any]:
        """Pick a source for a track and return a media-server URL.

        Same order as the desktop bridge: the preferred source first, the
        other one as a fallback.
        """
        prefer_online = bool(self.settings.playback.prefer_online)
        quality = str(track.get("quality") or self.settings.playback.online_quality
                      or "standard")
        song_id = str(track.get("id") or "")
        attempts = ["online", "local"] if prefer_online else ["local", "online"]
        errors: list[str] = []

        for kind in attempts:
            if kind == "online":
                if track.get("source") != "online" or not song_id:
                    continue
                info = self.api.get_song_url_info(song_id, quality)
                if info and info.get("url"):
                    return {
                        "success": True,
                        "kind": "online",
                        "url": self.media.url_for_online(song_id, info.get("level") or quality),
                        "quality": info.get("level") or quality,
                        "size": int(info.get("size") or 0),
                    }
                errors.append(str(getattr(self.api, "last_url_error", None) or "no_url"))
            else:
                local = self._local_track_for(track)
                path = Path(str((local or {}).get("path") or ""))
                if local and path.is_file():
                    return {
                        "success": True,
                        "kind": "local",
                        "url": self.media.url_for_local(path),
                        "path": str(path),
                        "cover_url": self.media.url_for_cover(path),
                        "name": local.get("name"),
                        "artists": local.get("artists"),
                        "album": local.get("album"),
                        "duration": int(local.get("duration") or track.get("duration") or 0),
                    }
                errors.append("no_local_copy")

        return {"success": False, "error_key": "error.reason.no_url",
                "message": ", ".join(dict.fromkeys(errors))}

    def cover_url(self, track: dict[str, Any]) -> str:
        """A loopback URL for a cover, or an empty string."""
        raw = str(track.get("cover_url") or "").strip()
        if not raw:
            return ""
        if raw.startswith("http://127.0.0.1") or raw.startswith("http://localhost"):
            return raw
        ext = "jpg"
        if ".png" in raw.lower():
            ext = "png"
        try:
            return self.media.url_for_remote_cover(raw, ext)
        except Exception:  # pragma: no cover - defensive
            return ""

    def lyrics(self, track: dict[str, Any]) -> dict[str, Any]:
        """Lyrics for a track, timed where possible."""
        source = str(track.get("source") or "online")
        lyric = ""
        translation = ""
        if source == "local":
            path = Path(str(track.get("path") or ""))
            sidecar = lyrics_path_for(path)
            if sidecar.is_file():
                try:
                    lyric = sidecar.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    lyric = ""
        if not lyric:
            song_id = str(track.get("id") or "")
            if not song_id:
                return {"success": False, "text": "", "timed": []}
            payload = self.api.get_lyrics(song_id)
            if not payload:
                return {"success": False, "text": "", "timed": []}
            lyric = payload.get("lyric") or ""
            translation = payload.get("translation") or ""
        timed = build_timed_lyrics(lyric, translation)
        return {"success": True, "text": lyric, "timed": timed}

    # -------------------------------------------------------------- download
    def download(self, tracks: list[dict[str, Any]]) -> dict[str, Any]:
        """Queue tracks for download.

        The downloader owns its own thread pool, so the call returns as soon
        as the batch is queued; progress arrives through the callbacks.
        """
        songs = [t for t in (tracks or [])
                 if isinstance(t, dict) and str(t.get("id") or "").strip()]
        if not songs:
            return {"success": False, "error_key": "status.no_songs_selected"}
        with self._lock:
            if self._download_thread and self._download_thread.is_alive():
                return {"success": False, "error_key": "download.start_failed",
                        "message": "a download is already running"}
            downloader = SongDownloader(
                download_dir=self._download_dir(),
                quality=self.settings.download.quality,
                overwrite=bool(self.settings.download.overwrite),
                max_concurrent=self.settings.download.max_concurrent,
                download_lyrics=bool(self.settings.download.download_lyrics),
                lyrics_translation=bool(self.settings.download.lyrics_translation),
            )
            downloader.set_progress_callback(self._on_progress)
            downloader.set_stats_callback(self._on_finished)
            self._downloader = downloader

        def worker() -> None:
            try:
                downloader.batch_download(songs, self.api)
            except Exception:  # pragma: no cover - defensive
                log.exception("download batch crashed")

        self._download_thread = threading.Thread(target=worker, name="ncm-batch", daemon=True)
        self._download_thread.start()
        return {"success": True, "total": len(songs)}

    def download_status(self) -> dict[str, Any]:
        if self._downloader is None:
            return {"running": False, "total": 0, "success": 0, "failed": 0,
                    "download_dir": ""}
        stats = self._downloader.stats
        return {
            "running": bool(self._download_thread and self._download_thread.is_alive()),
            "total": int(getattr(stats, "total", 0)),
            "success": int(getattr(stats, "success", 0)),
            "failed": int(getattr(stats, "failed", 0)),
            "download_dir": str(self._downloader.download_dir),
        }

    def pause_download(self) -> bool:
        if self._downloader is None:
            return False
        self._downloader.pause()
        return True

    def resume_download(self) -> bool:
        if self._downloader is None:
            return False
        self._downloader.resume()
        return True

    def cancel_download(self) -> bool:
        if self._downloader is None:
            return False
        self._downloader.cancel()
        return True

    def _on_progress(self, task: Any) -> None:
        if self.on_download_progress:
            self.on_download_progress(task.to_dict() if hasattr(task, "to_dict") else {})

    def _on_finished(self, stats: Any) -> None:
        if self.on_download_finished:
            self.on_download_finished(stats.to_dict() if hasattr(stats, "to_dict") else {})

    def _download_dir(self) -> str:
        """Where downloads land.

        Android gives the app a private external directory; the desktop
        build uses the configured folder.
        """
        configured = str(self.settings.download.download_dir or "").strip()
        if configured:
            return configured
        return str(Path.home() / "Downloads" / "NeteaseMusic")

    # ------------------------------------------------------------ local music
    def local_tracks(self, query: str = "", limit: int = 200, offset: int = 0) -> list[Track]:
        try:
            raw = self.local_music.get_tracks(query=query, limit=limit, offset=offset)
        except Exception as exc:  # pragma: no cover - platform dependent
            log.warning("local music unavailable: %s", exc)
            return []
        return [Track(item) for item in (raw or [])]

    # --------------------------------------------------------------- helpers
    def _local_track_for(self, track: dict[str, Any]) -> dict[str, Any] | None:
        """A local file that is very likely the same song as ``track``.

        Mirrors the desktop bridge: an exact ``title|artist`` match wins, and
        a title that only one local file carries is accepted too.
        """
        if (track or {}).get("source") == "local" and track.get("path"):
            return track
        if track.get("source") == "local":
            return self.local_music.track(
                str(track.get("path") or track.get("id") or ""))
        self._ensure_match_index()
        title = _match_key(track.get("name"))
        artist = _match_key(track.get("artists"))
        if not title:
            return None
        return self._match_index.get(f"{title}|{artist}") or self._match_index.get(title)

    def _ensure_match_index(self) -> None:
        """Index the local files by ``title|artist``.

        The index is rebuilt only when the library or the folder list
        changed, so the per-track lookup stays cheap.
        """
        directories = tuple(self.settings.playback.local_dirs)
        tracks = self.local_music.tracks()
        token = (len(tracks), directories)
        if self._match_index_token == token:
            return
        index: dict[str, dict[str, Any]] = {}
        title_index: dict[str, list[dict[str, Any]]] = {}
        for track in tracks:
            title = _match_key(track.get("name"))
            artist = _match_key(track.get("artists"))
            if title and artist:
                index.setdefault(f"{title}|{artist}", track)
            if title:
                title_index.setdefault(title, []).append(track)
        for title, candidates in title_index.items():
            if len(candidates) == 1 and title not in index:
                index[title] = candidates[0]
        self._match_index = index
        self._match_index_token = token

    @staticmethod
    def _track(song: dict[str, Any], index: int) -> Track:
        """Turn a NetEase song object into the flat shape the UI uses."""
        artists = song.get("artists") or song.get("ar") or []
        album = song.get("album") or song.get("al") or {}
        song_id = str(song.get("id") or "")
        return Track({
            "index": index,
            "id": song_id,
            "key": track_key("online", song_id),
            "source": "online",
            "name": song.get("name") or "",
            "artists": ", ".join(
                a.get("name", "") for a in artists if isinstance(a, dict)
            ) or "",
            "album": album.get("name") or "",
            "cover_url": album.get("picUrl") or song.get("cover_url") or "",
            "duration": int(song.get("dt") or song.get("duration") or 0),
            "fee": int(song.get("fee") or 0),
            "vip": bool(song.get("fee") or 0),
        })
