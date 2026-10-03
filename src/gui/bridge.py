"""Python <-> JavaScript bridge exposed to the pywebview window.

Every method here is callable from the UI as ``window.pywebview.api.<name>(...)``
and must return JSON-serialisable data.  User facing messages are returned as
``*_key`` translation keys (the frontend renders them) with a localised
``message`` as a fallback, so the UI never shows a raw traceback.

Backend -> frontend pushes go through the ``on*`` functions defined in
``assets/app.js`` and :meth:`GuiBridge._call_js`.
"""
from __future__ import annotations

import base64
import json
import logging
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import webview

from src.auth import LoginStatus, get_login_manager
from src.config import PLAY_MODES, default_download_dir, get_settings
from src.core import get_api
from src.core.downloader import MAX_CONCURRENT, DownloadStats, DownloadTask, SongDownloader
from src.core.library import get_library, track_key
from src.core.localmusic import (
    get_local_music,
    read_embedded_lyrics,
    sidecar_lyrics_path,
)
from src.core.lyrics import build_timed_lyrics
from src.core.mediaserver import get_media_server
from src.gui.theme import effective_theme, normalize_theme, system_theme
from src.i18n import get_i18n, t
from src.version import APP_DISPLAY_NAME, AUTHOR, BUNDLE_ID, HOMEPAGE, LICENSE, __version__

__all__ = ["GuiBridge"]

#: Characters stripped when the player looks for a local copy of an online song.
_MATCH_NOISE = re.compile(r"[\s\u3000()（）\[\]【】<>《》,，.。!！?？'\"“”‘’\-_~·・:：;；/\\|+&]+")


def _match_key(text: str) -> str:
    """Normalise a title/artist for the online <-> local matching index."""
    return _MATCH_NOISE.sub("", str(text or "").lower())


def _reason_code(value: Any) -> str:
    """Reduce ``"lossless:copyright_unavailable"`` to its reason code.

    ``api.get_song_url_info`` reports the last failed quality together with the
    reason; only the reason is translatable.
    """
    text = str(value or "").strip()
    if not text:
        return "no_url"
    return text.split(":")[-1] or "no_url"


class GuiBridge:
    """Bridge between the Python backend and the HTML frontend."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("ncm.bridge")
        self.settings = get_settings()
        self.i18n = get_i18n()
        self.login_manager = get_login_manager()
        self.api = get_api(debug=self.settings.debug)

        self.downloader: SongDownloader | None = None
        self.current_playlist: dict[str, Any] | None = None
        self.playlist_songs: list[dict[str, Any]] = []
        self.selected_songs: list[int] = []
        self.download_stats = DownloadStats()
        self._download_thread: threading.Thread | None = None
        self._window: Any | None = None
        #: Hook installed by main.py so the OS window chrome follows the theme.
        self.apply_theme: Callable[[str], bool] | None = None
        self._lock = threading.RLock()
        self._webview_module: Any = None  # Injected by main.py

        # ---- player state ----------------------------------------------------
        self.library = get_library()
        self.local_music = get_local_music()
        self.media = get_media_server()
        #: Normalised "title|artist" -> local track, rebuilt on demand.
        self._match_index: dict[str, dict[str, Any]] = {}
        self._match_index_token: Any = None
        self._favorite_ids: set[str] = set()
        self._favorite_ids_loaded = False

        self.login_manager.on("status_change", self._on_login_status_change)
        self.login_manager.on("qrcode_update", self._on_qrcode_update)
        self.login_manager.on("login_success", self._on_login_success)
        self.login_manager.on("login_failed", self._on_login_failed)
        self.login_manager.on("status_message", self._on_login_message)

    def set_window(self, window: Any) -> None:
        self._window = window

    # ------------------------------------------------------------------ plumbing
    def _call_js(self, func: str, payload: Any) -> None:
        """Invoke ``func(payload)`` in the frontend (safe to call from threads)."""
        if not self._window:
            return
        try:
            args = json.dumps(payload, ensure_ascii=False)
            self._window.evaluate_js(f"{func}({args})")
        except Exception as exc:  # pragma: no cover - window may be closing
            self.logger.debug("JS call %s failed: %s", func, exc)

    # -------------------------------------------------------------------- about
    def get_app_info(self) -> dict[str, Any]:
        return {
            "name": APP_DISPLAY_NAME,
            "version": __version__,
            "author": AUTHOR,
            "homepage": HOMEPAGE,
            "license": LICENSE,
            "bundle_id": BUNDLE_ID,
            "platform": platform.platform(),
            "config_path": str(self.settings.config_path),
            "download_dir": self._download_dir(),
            "max_concurrent": MAX_CONCURRENT,
        }

    def open_external(self, url: str) -> bool:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return False
        try:
            import webbrowser

            webbrowser.open(url)
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.warning("cannot open %s: %s", url, exc)
            return False

    def reveal_path(self, path: str) -> bool:
        """Show a file in the OS file manager (used by the local player).

        Unlike :meth:`open_external` this one is about files the user already
        has on disk, so it takes a path rather than a url.
        """
        if not isinstance(path, str) or not path.strip():
            return False
        target = Path(path).expanduser()
        if not target.exists():
            return False
        try:
            if platform.system() == "Darwin":
                subprocess.Popen(["open", "-R", str(target)])
            elif platform.system() == "Windows":
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                folder = target if target.is_dir() else target.parent
                subprocess.Popen(["xdg-open", str(folder)])
            return True
        except Exception as exc:  # pragma: no cover - depends on the OS
            self.logger.warning("cannot reveal %s: %s", target, exc)
            return False

    # ----------------------------------------------------------------- settings
    def _download_dir(self) -> str:
        configured = self.settings.download.download_dir
        return configured or str(default_download_dir())

    def get_settings(self) -> dict[str, Any]:
        return {
            "download": {
                "quality": self.settings.download.quality,
                "max_concurrent": self.settings.download.max_concurrent,
                "overwrite": self.settings.download.overwrite,
                "download_lyrics": self.settings.download.download_lyrics,
                "lyrics_translation": self.settings.download.lyrics_translation,
                "download_dir": self._download_dir(),
            },
            "playback": {
                "prefer_online": self.settings.playback.prefer_online,
                "online_quality": self.settings.playback.online_quality,
                "play_mode": self.settings.playback.play_mode,
                "volume": self.settings.playback.volume,
                "resume_playback": self.settings.playback.resume_playback,
                "report_play_count": self.settings.playback.report_play_count,
                "local_dirs": list(self.settings.playback.local_dirs),
                "scan_depth": self.settings.playback.scan_depth,
                "show_translation": self.settings.playback.show_translation,
            },
            "ui": {
                "language": self.settings.ui.language,
                "theme": self.settings.ui.theme,
                "remember_window_size": self.settings.ui.remember_window_size,
                "background_image": self.settings.ui.background_image,
                "background_url": self.background_url(),
                "background_blur": self.settings.ui.background_blur,
                "background_dim": self.settings.ui.background_dim,
                "glass": self.settings.ui.glass,
                "accent_from_background": self.settings.ui.accent_from_background,
            },
            "auth": {
                "remember_login": self.settings.auth.remember_login,
            },
            "debug": self.settings.debug,
        }

    def update_settings(self, category: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            if category == "download":
                self.settings.update_download(**self._clean_download(data))
            elif category == "playback":
                self.settings.update_playback(**self._clean_playback(data))
                if "local_dirs" in (data or {}):
                    self._match_index_token = None
            elif category == "ui":
                self.settings.update_ui(**self._clean(data, {
                    "language", "theme", "remember_window_size", "background_image",
                    "background_blur", "background_dim", "glass",
                    "accent_from_background"}))
                if "language" in data:
                    self.i18n.set_language(str(data["language"]))
                if "theme" in data:
                    self._sync_native_theme()
            elif category == "auth":
                self.settings.update_auth(**self._clean(data, {"remember_login", "login_method"}))
            elif category == "debug":
                self.settings.debug = bool(data.get("debug", False))
                self.api.debug = self.settings.debug
            else:
                return {"success": False, "error_key": "error.unknown"}
            return {"success": True, "settings": self.get_settings()}
        except Exception as exc:
            self.logger.warning("update_settings(%s) failed: %s", category, exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}

    @staticmethod
    def _clean(data: dict[str, Any], allowed: set) -> dict[str, Any]:
        return {k: v for k, v in (data or {}).items() if k in allowed}

    def _clean_download(self, data: dict[str, Any]) -> dict[str, Any]:
        cleaned = self._clean(data, {"quality", "max_concurrent", "overwrite", "download_dir",
                                     "download_lyrics", "lyrics_translation"})
        if "max_concurrent" in cleaned:
            try:
                cleaned["max_concurrent"] = max(1, min(int(cleaned["max_concurrent"]), MAX_CONCURRENT))
            except (TypeError, ValueError):
                cleaned.pop("max_concurrent")
        return cleaned

    def _clean_playback(self, data: dict[str, Any]) -> dict[str, Any]:
        cleaned = self._clean(data, {"prefer_online", "online_quality", "play_mode", "volume",
                                      "resume_playback", "report_play_count", "local_dirs",
                                      "scan_depth", "show_translation", "last_local_dir"})
        if "play_mode" in cleaned and cleaned["play_mode"] not in PLAY_MODES:
            cleaned["play_mode"] = "list"
        if isinstance(cleaned.get("local_dirs"), list):
            cleaned["local_dirs"] = [str(item) for item in cleaned["local_dirs"]
                                     if str(item or "").strip()]
        return cleaned

    def reset_settings(self) -> dict[str, Any]:
        self.settings.reset()
        self.i18n.set_language(self.settings.ui.language)
        self._sync_native_theme()
        self._call_js("onLanguageChanged", self.get_translations())
        return {"success": True, "settings": self.get_settings()}

    def set_theme(self, theme: str) -> dict[str, Any]:
        """Persist the theme mode and repaint the native window chrome.

        Called by the UI as soon as the user flips the theme, so the window
        frame never lags behind the page (and the choice survives a restart).
        """
        mode = normalize_theme(theme)
        self.settings.update_ui(theme=mode)
        applied = self._sync_native_theme()
        return {"success": True, "theme": mode,
                "effective": effective_theme(mode), "native_applied": bool(applied)}

    def get_system_theme(self) -> str:
        return system_theme()

    def _sync_native_theme(self) -> bool:
        if self.apply_theme is None:
            return False
        try:
            return bool(self.apply_theme(self.settings.ui.theme))
        except Exception as exc:  # pragma: no cover - platform specific
            self.logger.debug("native theme update failed: %s", exc)
            return False

    def choose_directory(self) -> str | None:
        if not self._window:
            return None
        try:
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=self._download_dir(),
            )
        except Exception as exc:
            self.logger.warning("folder dialog failed: %s", exc)
            return None
        if not result:
            return None
        chosen = result[0] if isinstance(result, list | tuple) else result
        self.settings.update_download(download_dir=str(chosen))
        return str(chosen)

    # ---------------------------------------------------------------- language
    def get_languages(self) -> dict[str, str]:
        return self.i18n.available_languages

    def get_translations(self) -> dict[str, str]:
        return self.i18n.get_flat_translations()

    def set_language(self, lang: str) -> dict[str, Any]:
        if not self.i18n.set_language(lang):
            return {"success": False, "error_key": "error.unknown"}
        translations = self.get_translations()
        self._call_js("onLanguageChanged", translations)
        return {"success": True, "language": self.i18n.current_language,
                "translations": translations}

    # ------------------------------------------------------------------- login
    def get_login_status(self) -> dict[str, Any]:
        self.login_manager.verify_login()
        user = self.login_manager.user_info
        return {
            "status": self.login_manager.status.value,
            "is_logged_in": self.login_manager.is_logged_in,
            "qr_code": self.login_manager.last_qr_code,
            "qr_message": self.login_manager.last_qr_message,
            "user": {
                "user_id": user.user_id,
                "nickname": user.nickname,
                "avatar_url": user.avatar_url,
                "vip_type": user.vip_type,
            } if user else None,
        }

    def login_qrcode(self) -> dict[str, Any]:
        # The manager reuses a QR code that is still pending on its own.
        ok = self.login_manager.login_qrcode()
        return {"success": ok, "error_key": None if ok else "login.login_failed"}

    def refresh_qrcode(self) -> dict[str, Any]:
        return {"success": self.login_manager.refresh_qrcode()}

    def cancel_login(self) -> bool:
        self.login_manager.cancel_login()
        return True

    def send_phone_code(self, phone: str, ctcode: str = "86") -> dict[str, Any]:
        result = self.login_manager.send_phone_code(phone, ctcode)
        return {
            "success": result["success"],
            "error_key": result.get("error_key") or (None if result["success"]
                                                     else "login.code_send_failed"),
            "message": result.get("message", ""),
            "throttled": bool(result.get("throttled")),
        }

    def login_phone(self, phone: str, code: str, ctcode: str = "86") -> dict[str, Any]:
        result = self.login_manager.login_phone(phone, code, ctcode)
        return {
            "success": result["success"],
            "error_key": None if result["success"] else "login.login_failed",
            "message": result.get("message", ""),
        }

    def login_cookie(self, cookie: str) -> dict[str, Any]:
        result = self.login_manager.login_cookie(cookie or "")
        return {
            "success": result["success"],
            "error_key": None if result["success"] else "login.cookie_invalid",
            "message": result.get("message", ""),
        }

    def logout(self) -> dict[str, Any]:
        self.login_manager.logout()
        return {"success": True}

    def _on_login_status_change(self, status: LoginStatus) -> None:
        self._call_js("onLoginStatusChange", {
            "status": status.value,
            "code": self.login_manager.last_qr_code,
            "message": self.login_manager.last_qr_message,
        })

    def _on_login_message(self, message: str) -> None:
        """An answer the QR flow did not expect -- show it instead of hiding it."""
        self._call_js("onLoginMessage", {"message": message})

    def _on_qrcode_update(self, qrcode_url: str) -> None:
        image = self.login_manager.get_qrcode_image(qrcode_url)
        encoded = base64.b64encode(image).decode("ascii") if image else None
        self._call_js("onQrcodeUpdate", {
            "url": qrcode_url,
            "image": f"data:image/png;base64,{encoded}" if encoded else None,
        })

    def _on_login_success(self, user) -> None:
        self._call_js("onLoginSuccess", {
            "user_id": user.user_id,
            "nickname": user.nickname,
            "avatar_url": user.avatar_url,
            "vip_type": user.vip_type,
        })

    def _on_login_failed(self, error: str) -> None:
        key = {
            "qrcode_expired": "login.qrcode_expired",
            "cookie_invalid": "login.cookie_invalid",
            "login_status_check_failed": "login.login_failed",
        }.get(error, "login.login_failed")
        self._call_js("onLoginFailed", {"error_key": key, "message": str(error)})

    # ---------------------------------------------------------------- playlist
    @staticmethod
    def _extract_playlist_id(value: str) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        if value.isdigit():
            return value
        for pattern in (r"[?&]id=(\d+)", r"playlist/(\d+)", r"play/(\d+)/", r"toplist/(\d+)",
                        r"discover/toplist\?id=(\d+)"):
            match = re.search(pattern, value)
            if match:
                return match.group(1)
        digits = re.search(r"(\d{5,})", value)
        return digits.group(1) if digits else None

    def fetch_playlist(self, playlist_input: str) -> dict[str, Any]:
        playlist_id = self._extract_playlist_id(playlist_input)
        if not playlist_id:
            return {"success": False, "error_key": "playlist.invalid_input"}

        self._call_js("onStatusUpdate", {"status": "fetching_playlist",
                                         "message": t("status.fetching_playlist")})
        info = self.api.get_playlist_info(playlist_id)
        if not info:
            return {"success": False,
                    "error_key": "login.login_needed" if not self.login_manager.is_logged_in
                    else "playlist.fetch_failed"}

        self._call_js("onStatusUpdate", {"status": "fetching_songs",
                                         "message": t("status.fetching_songs")})
        songs = self.api.get_playlist_songs(playlist_id) or []
        if not songs:
            return {"success": False, "error_key": "playlist.fetch_failed"}

        with self._lock:
            self.current_playlist = info
            self.playlist_songs = songs
            self.selected_songs = []

        tracks = [self._track_payload(index, song) for index, song in enumerate(songs)]
        return {
            "success": True,
            "playlist": {
                "id": str(info.get("id", playlist_id)),
                "name": info.get("name") or "Unknown",
                "creator": (info.get("creator") or {}).get("nickname") or "Unknown",
                "cover_url": info.get("coverImgUrl") or "",
                "track_count": info.get("trackCount") or len(tracks),
                "play_count": info.get("playCount") or 0,
                "description": info.get("description") or "",
            },
            "tracks": tracks,
        }

    @staticmethod
    def _track_payload(index: int, song: dict[str, Any]) -> dict[str, Any]:
        artists = song.get("artists") or song.get("ar") or []
        album = song.get("album") or song.get("al") or {}
        song_id = str(song.get("id", ""))
        return {
            "index": index,
            "id": song_id,
            "key": track_key("online", song_id),
            "source": "online",
            "name": song.get("name") or "Unknown",
            "artists": ", ".join(a.get("name", "Unknown") for a in artists if isinstance(a, dict))
            or "Unknown",
            "album": album.get("name") or "",
            "cover_url": album.get("picUrl") or song.get("cover_url") or "",
            "duration": song.get("dt") or song.get("duration") or 0,
            "fee": song.get("fee", 0),
            "vip": bool(song.get("fee", 0)),
        }

    def select_songs(self, indices: list[int]) -> dict[str, Any]:
        with self._lock:
            self.selected_songs = sorted({int(i) for i in (indices or [])
                                          if 0 <= int(i) < len(self.playlist_songs)})
            return {"selected": len(self.selected_songs), "total": len(self.playlist_songs)}

    def select_all_songs(self) -> dict[str, Any]:
        with self._lock:
            self.selected_songs = list(range(len(self.playlist_songs)))
            return {"selected": len(self.selected_songs), "total": len(self.playlist_songs)}

    def deselect_all_songs(self) -> dict[str, Any]:
        with self._lock:
            self.selected_songs = []
            return {"selected": 0, "total": len(self.playlist_songs)}

    # ---------------------------------------------------------------- download
    def start_download(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        with self._lock:
            # The UI owns the selection; accept it explicitly and fall back to
            # whatever was pushed through select_songs() before.
            indices = options.get("indices")
            if indices is not None:
                self.selected_songs = sorted({int(i) for i in indices
                                              if str(i).lstrip("-").isdigit()
                                              and 0 <= int(i) < len(self.playlist_songs)})
            if not self.playlist_songs:
                return {"success": False, "error_key": "status.no_playlist"}
            if not self.selected_songs:
                return {"success": False, "error_key": "status.no_songs_selected"}
            songs = [self.playlist_songs[i] for i in self.selected_songs
                     if 0 <= i < len(self.playlist_songs)]
        return self._launch_download(songs, options)

    def _launch_download(self, songs: list[dict[str, Any]],
                         options: dict[str, Any]) -> dict[str, Any]:
        """Start a batch download in the background (shared with the player)."""
        options = options or {}
        with self._lock:
            songs = [song for song in (songs or [])
                     if isinstance(song, dict) and str(song.get("id") or "").strip()]
            if not songs:
                return {"success": False, "error_key": "status.no_songs_selected"}
            if self._download_thread and self._download_thread.is_alive():
                return {"success": False, "error_key": "download.start_failed",
                        "message": "a download is already running"}

            download_dir = options.get("download_dir") or self._download_dir()
            quality = options.get("quality") or self.settings.download.quality
            overwrite = bool(options.get("overwrite", self.settings.download.overwrite))
            max_concurrent = options.get("max_concurrent", self.settings.download.max_concurrent)
            download_lyrics = bool(options.get("download_lyrics",
                                               self.settings.download.download_lyrics))
            lyrics_translation = bool(options.get("lyrics_translation",
                                                  self.settings.download.lyrics_translation))

            self.downloader = SongDownloader(
                download_dir=download_dir,
                quality=quality,
                overwrite=overwrite,
                max_concurrent=max_concurrent,
                download_lyrics=download_lyrics,
                lyrics_translation=lyrics_translation,
            )
            self.downloader.set_progress_callback(self._on_download_progress)
            self.downloader.set_stats_callback(self._on_download_stats)

            # Persist the choices so the next session starts where we left off.
            self.settings.update_download(quality=quality, overwrite=overwrite,
                                          max_concurrent=max_concurrent,
                                          download_lyrics=download_lyrics,
                                          lyrics_translation=lyrics_translation,
                                          download_dir=str(self.downloader.download_dir))
            self.refresh_allowed_roots()

            DownloadStats(total=len(songs))

        def worker() -> None:
            try:
                result = self.downloader.batch_download(songs, self.api)  # type: ignore[union-attr]
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.exception("download batch crashed")
                result = DownloadStats(total=len(songs), failed=len(songs))
                result.failed_songs = [{"name": "batch", "error": str(exc), "id": ""}]
                self._call_js("onDownloadFailed", {"message": str(exc)})
            self.download_stats = result
            self._call_js("onDownloadComplete", {
                "stats": result.to_dict(),
                "hint_login": self._hint_login(result),
                "download_dir": str(self.downloader.download_dir) if self.downloader else "",
            })

        self._download_thread = threading.Thread(target=worker, name="ncm-batch", daemon=True)
        self._download_thread.start()
        return {"success": True, "total": len(songs)}

    def _hint_login(self, stats: DownloadStats) -> bool:
        """Suggest signing in when failures look login/VIP related."""
        if self.login_manager.is_logged_in or not stats.failed_songs:
            return False
        reasons = " ".join(str(song.get("error_code") or song.get("error") or "")
                           for song in stats.failed_songs)
        return any(token in reasons for token in
                   ("copyright_unavailable", "login_required", "no_url", "no_response"))

    def pause_download(self) -> bool:
        if self.downloader:
            self.downloader.pause()
            return True
        return False

    def resume_download(self) -> bool:
        if self.downloader:
            self.downloader.resume()
            return True
        return False

    def cancel_download(self) -> bool:
        if self.downloader:
            self.downloader.cancel()
            return True
        return False

    def _on_download_progress(self, task: DownloadTask) -> None:
        self._call_js("onDownloadProgress", task.to_dict())

    def _on_download_stats(self, stats: DownloadStats) -> None:
        self._call_js("onDownloadStats", stats.to_dict())

    def open_download_folder(self) -> bool:
        path = str(self.downloader.download_dir) if self.downloader else self._download_dir()
        try:
            system = platform.system()
            if system == "Darwin":
                subprocess.Popen(["open", path])
            elif system == "Windows":
                subprocess.Popen(["explorer", str(Path(path))])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception as exc:
            self.logger.warning("cannot open %s: %s", path, exc)
            return False

    # =================================================================== player
    # -- shared helpers --------------------------------------------------------
    def _safe(self, label: str, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call a network helper, turning a transport error into ``None``.

        The UI shows a translated "failed" state for ``None``; letting the
        exception escape would only produce an unhandled promise in the webview.
        """
        try:
            return call(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - transport dependent
            self.logger.info("%s failed: %s", label, exc)
            return None

    def background_url(self) -> str:
        """URL of the stored wallpaper (``""`` when unset or the server is down)."""
        if not self.media.running or self.settings.background_path() is None:
            return ""
        return self.media.url_for_background()

    def refresh_allowed_roots(self) -> list[str]:
        """Let the media server read the download folder and every music folder."""
        roots = [self._download_dir(), *self.settings.playback.local_dirs]
        self.media.set_allowed_roots(roots)
        return [str(path) for path in self.media.allowed_roots]

    @staticmethod
    def _key_of(track: dict[str, Any]) -> str:
        key = str((track or {}).get("key") or "")
        if key:
            return key
        source = (track or {}).get("source") or "online"
        identifier = (track or {}).get("path") if source == "local" else (track or {}).get("id")
        return track_key(source, str(identifier or ""))

    @staticmethod
    def _meta_of(track: dict[str, Any]) -> dict[str, Any]:
        """The small subset of a track the library stores next to a favourite."""
        return {
            "name": track.get("name") or "",
            "artists": track.get("artists") or "",
            "album": track.get("album") or "",
            "cover_url": track.get("cover_url") or "",
            "duration": track.get("duration") or 0,
            "source": track.get("source") or "online",
            "id": track.get("id") or "",
            "path": track.get("path") or "",
        }

    @staticmethod
    def _clean_track(track: Any) -> dict[str, Any]:
        """Whitelist the fields a queue entry may carry (the queue is persisted)."""
        if not isinstance(track, dict):
            return {}
        allowed = ("key", "source", "id", "path", "name", "artists", "album",
                   "cover_url", "duration", "vip", "ext", "size", "has_lyrics")
        return {name: track[name] for name in allowed if name in track}

    # -- local <-> online matching --------------------------------------------
    def _ensure_match_index(self) -> None:
        """Index local files by "title|artist" so an online song can find its copy."""
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

    def _local_track_for(self, track: dict[str, Any]) -> dict[str, Any] | None:
        """A local file that is very likely the same song as ``track``."""
        if (track or {}).get("source") == "local" and track.get("path"):
            return track
        source = track.get("source")
        if source == "local":
            found = self.local_music.track(str(track.get("path") or track.get("id") or ""))
            return found
        self._ensure_match_index()
        title = _match_key(track.get("name"))
        artist = _match_key(track.get("artists"))
        if not title:
            return None
        return self._match_index.get(f"{title}|{artist}") or self._match_index.get(title)

    # -- resolving a playable url ---------------------------------------------
    def resolve_track(self, track: dict[str, Any]) -> dict[str, Any]:
        """Pick a source for ``track`` and return a media-server url.

        The order follows the playback setting (online first or local first) and
        falls back to the other side when the preferred one is unavailable.
        """
        track = track or {}
        prefer_online = bool(self.settings.playback.prefer_online)
        quality = str(track.get("quality") or self.settings.playback.online_quality
                      or "standard")
        song_id = str(track.get("id") or "")
        attempts: list[str] = ["online", "local"] if prefer_online else ["local", "online"]
        errors: list[str] = []

        for kind in attempts:
            if kind == "online":
                if track.get("source") != "online" or not song_id:
                    continue
                info = self._safe("resolve url", self.api.get_song_url_info, song_id, quality)
                if info and info.get("url"):
                    return {
                        "success": True,
                        "kind": "online",
                        "url": self.media.url_for_online(song_id, info.get("level") or quality),
                        "quality": info.get("level") or quality,
                        "level": info.get("level"),
                        "size": info.get("size") or 0,
                        "matched_local": False,
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
                        "matched_local": track.get("source") == "online",
                        # Embedded art is extracted on demand by the media server.
                        "cover_url": self.media.url_for_cover(path),
                        "name": local.get("name"),
                        "artists": local.get("artists"),
                        "album": local.get("album"),
                        "duration": local.get("duration") or track.get("duration") or 0,
                        "has_lyrics": bool(local.get("has_lyrics")),
                    }
                errors.append("no_local_copy")

        return {
            "success": False,
            "error_key": "error.reason." + _reason_code(errors[-1] if errors else "no_url"),
            "message": ", ".join(dict.fromkeys(errors)),
        }

    # -- play counts, favourites ----------------------------------------------
    def record_play(self, track: dict[str, Any]) -> dict[str, Any]:
        """Count one playback locally and (optionally) report it to NetEase."""
        key = self._key_of(track)
        if not key:
            return {"success": False, "error_key": "error.unknown"}
        count = self.library.record_play(key, meta=self._meta_of(track))
        reported = False
        if (track.get("source") != "local" and self.settings.playback.report_play_count
                and self.login_manager.is_logged_in and track.get("id")):
            seconds = float(track.get("duration") or 0) / 1000.0
            threading.Thread(
                target=self.api.report_play,
                args=(str(track.get("id")), seconds or 0.0),
                name="ncm-report", daemon=True,
            ).start()
            reported = True
        return {"success": True, "key": key, "count": count, "reported": reported}

    def get_play_stats(self) -> dict[str, Any]:
        return {
            "success": True,
            "counts": self.library.play_counts(),
            "stats": self.library.stats(),
            "favorites": self.library.favorite_keys(),
        }

    def reset_play_counts(self) -> dict[str, Any]:
        self.library.reset_play_counts()
        return {"success": True, "stats": self.library.stats()}

    def get_recent_tracks(self, limit: int = 50) -> dict[str, Any]:
        entries = self.library.recent(limit)
        tracks = []
        for entry in entries:
            meta = dict(entry.get("meta") or {})
            meta.setdefault("key", entry.get("key"))
            tracks.append({**meta, "played": entry.get("played")})
        return {"success": True, "tracks": tracks}

    def set_favorite(self, track: dict[str, Any], favorite: bool = True) -> dict[str, Any]:
        """Toggle a favourite locally and mirror it to NetEase when possible."""
        key = self._key_of(track)
        if not key:
            return {"success": False, "error_key": "error.unknown"}
        state = self.library.set_favorite(key, bool(favorite), self._meta_of(track))
        cloud: str | None = None
        if track.get("source") != "local" and track.get("id"):
            if not self.login_manager.is_logged_in:
                cloud = "login_required"
            else:
                liked = self._safe("cloud favourite", self.api.set_song_like,
                                   str(track["id"]), state)
                cloud = "ok" if liked else "failed"
        return {"success": True, "key": key, "favorite": state, "cloud": cloud,
                "favorites": self.library.favorite_keys()}

    def get_favorites(self) -> dict[str, Any]:
        return {"success": True, "tracks": self.library.favorites(),
                "keys": self.library.favorite_keys()}

    # -- lyrics ---------------------------------------------------------------
    def _lookup_online_lyrics(self, track: dict[str, Any]) -> dict[str, Any] | None:
        """Find lyrics for a local file by searching NetEase for its title."""
        name = str(track.get("name") or "").strip()
        if not name:
            return None
        query = f"{name} {str(track.get('artists') or '')}".strip()
        results = self._safe("lyric search", self.api.search_songs, query, limit=5) or []
        wanted = _match_key(name)
        ordered = sorted(
            (song for song in results if song.get("id")),
            key=lambda song: 0 if _match_key(song.get("name")) == wanted else 1,
        )
        for song in ordered[:3]:
            result = self._safe("lyrics", self.api.get_lyrics, str(song["id"]))
            if result is None:
                continue
            if result and (result.get("lyric") or "").strip():
                return {
                    "song_id": str(song["id"]),
                    "name": song.get("name"),
                    "artists": ", ".join(a.get("name", "") for a in (song.get("ar") or [])
                                         if isinstance(a, dict)),
                    "lyric": result.get("lyric") or "",
                    "translation": result.get("translation") or "",
                }
        return None

    def get_track_lyrics(self, track: dict[str, Any], save: bool = False) -> dict[str, Any]:
        """Lyrics for one track, with the source they came from.

        Local files try their ``.lrc`` sidecar, then the embedded tag, and only
        then ask NetEase.  ``save`` writes a sidecar for the lyrics that were
        found online, so the next playback is offline again.
        """
        track = track or {}
        lyric = ""
        translation = ""
        origin = "none"
        saved_path = ""
        is_local = track.get("source") == "local"
        path = Path(str(track.get("path") or track.get("id") or "")) if is_local else None

        if path is not None:
            sidecar = sidecar_lyrics_path(path) if path.is_file() else None
            if sidecar is not None:
                try:
                    lyric = sidecar.read_text(encoding="utf-8", errors="replace")
                    origin = "sidecar"
                except OSError:
                    lyric = ""
            if not lyric.strip() and path.is_file():
                try:
                    lyric = read_embedded_lyrics(path)
                    origin = "embedded" if lyric.strip() else origin
                except Exception:  # pragma: no cover - unreadable tag
                    lyric = ""
            if not lyric.strip():
                found = self._lookup_online_lyrics(track)
                if found:
                    lyric, translation, origin = found["lyric"], found["translation"], "netease"
                    if save:
                        from src.core.lyrics import merge_translation, write_lyrics

                        target = path.with_suffix(".lrc")
                        try:
                            write_lyrics(target, merge_translation(lyric, translation))
                            saved_path = str(target)
                        except OSError as exc:
                            self.logger.info("cannot save lyrics for %s: %s", path, exc)
        else:
            song_id = str(track.get("id") or "")
            if song_id:
                result = self._safe("lyrics", self.api.get_lyrics, song_id)
                if result:
                    lyric = result.get("lyric") or ""
                    translation = result.get("translation") or ""
                    origin = "netease" if lyric.strip() else "none"

        lines = build_timed_lyrics(lyric, translation)
        return {
            "success": True,
            "lines": lines,
            "source": origin,
            "has_lyrics": bool(lines),
            "lyrics_path": saved_path,
            "plain": lyric if not lines else "",
        }

    # -- playback session ------------------------------------------------------
    def save_playback_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Persist the queue/index/position so the next launch resumes there."""
        if not isinstance(state, dict):
            return {"success": False, "error_key": "error.unknown"}
        queue = [self._clean_track(item) for item in state.get("queue") or []]
        queue = [item for item in queue if item.get("key")][:2000]
        if not queue:
            self.library.save_playback(None)
            return {"success": True, "stored": False}
        try:
            index = int(state.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        try:
            position = max(0.0, float(state.get("position") or 0))
        except (TypeError, ValueError):
            position = 0.0
        mode = state.get("mode") if state.get("mode") in PLAY_MODES else "list"
        source = state.get("source")
        self.library.save_playback({
            "queue": queue,
            "index": max(0, min(index, len(queue) - 1)),
            "position": position,
            "mode": mode,
            "source": source if isinstance(source, dict) else None,
            "saved_at": time.time(),
        })
        return {"success": True, "stored": True}

    def load_playback_state(self) -> dict[str, Any]:
        state = self.library.playback_state()
        if not state or not state.get("queue"):
            return {"success": True, "state": None}
        return {"success": True, "state": state}

    def clear_playback_state(self) -> dict[str, Any]:
        self.library.clear_playback()
        return {"success": True}

    # -- online library --------------------------------------------------------
    def get_playlists(self) -> dict[str, Any]:
        """Every playlist of the signed-in account."""
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return {"success": False, "error_key": "player.login_required", "playlists": []}
        playlists = self._safe("account playlists", self.api.get_user_playlists_all,
                               user.user_id)
        if playlists is None:
            return {"success": False, "error_key": "error.network", "playlists": []}
        return {
            "success": bool(playlists),
            "error_key": None if playlists else "player.playlist_empty",
            "playlists": [self._playlist_payload(item) for item in playlists],
        }

    @staticmethod
    def _playlist_payload(playlist: dict[str, Any]) -> dict[str, Any]:
        creator = playlist.get("creator") or {}
        return {
            "id": str(playlist.get("id", "")),
            "name": playlist.get("name") or "Unknown",
            "cover_url": playlist.get("coverImgUrl") or playlist.get("coverImgUrlStr") or "",
            "track_count": playlist.get("trackCount") or 0,
            "creator": creator.get("nickname") or "",
            "special_type": playlist.get("specialType") or 0,
            "subscribed": bool(playlist.get("subscribed")),
            "description": playlist.get("description") or "",
        }

    def get_playlist_tracks(self, playlist_id: str) -> dict[str, Any]:
        """Tracks of one playlist, in playlist order (does not touch the download tab)."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id.isdigit():
            return {"success": False, "error_key": "playlist.invalid_input", "tracks": []}
        info = self._safe("playlist info", self.api.get_playlist_info, playlist_id)
        songs = self._safe("playlist songs", self.api.get_playlist_songs, playlist_id) or []
        if not songs:
            return {"success": False, "error_key": "playlist.fetch_failed", "tracks": []}
        return {
            "success": True,
            "playlist": self._playlist_payload(info or {"id": playlist_id}),
            "tracks": [self._track_payload(index, song) for index, song in enumerate(songs)],
        }

    def get_liked_tracks(self) -> dict[str, Any]:
        """The account's "liked" list (needs a login)."""
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return {"success": False, "error_key": "player.login_required", "tracks": []}
        ids = self._safe("liked ids", self.api.get_liked_song_ids, user.user_id)
        if not ids:
            return {"success": True, "tracks": []}
        songs = self._safe("liked songs", self.api.get_songs_detail, ids) or []
        return {"success": True, "tracks": [self._track_payload(index, song)
                                           for index, song in enumerate(songs)]}

    def search_tracks(self, keyword: str, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """Search NetEase for songs."""
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": False, "error_key": "player.search_empty", "tracks": []}
        songs = self._safe("search", self.api.search_songs, keyword,
                           limit=limit, offset=offset)
        if songs is None:
            return {"success": False, "error_key": "error.network", "tracks": []}
        return {
            "success": True,
            "keyword": keyword,
            "offset": max(0, int(offset or 0)),
            "tracks": [self._track_payload(index, song) for index, song in enumerate(songs)],
        }

    # -- discovery ---------------------------------------------------------------
    def get_album_detail(self, album_id: str) -> dict[str, Any]:
        """Fetch album detail including tracks."""
        album_id = str(album_id or "").strip()
        if not album_id.isdigit():
            return {"success": False, "error_key": "player.invalid_album_id", "album": None}
        album = self._safe("album detail", self.api.get_album_detail, album_id)
        if not album:
            return {"success": False, "error_key": "player.album_not_found", "album": None}
        return {
            "success": True,
            "album": self._album_payload(album),
            "tracks": [self._track_payload(index, song) for index, song in enumerate(album.get("songs") or [])],
        }

    @staticmethod
    def _album_payload(album: dict[str, Any]) -> dict[str, Any]:
        artist = album.get("artist") or {}
        return {
            "id": str(album.get("id", "")),
            "name": album.get("name") or "Unknown",
            "cover_url": album.get("picUrl") or album.get("coverImgUrl") or "",
            "description": album.get("description") or "",
            "artist": artist.get("name") or "Unknown",
            "artist_id": str(artist.get("id") or ""),
            "publish_time": album.get("publishTime") or 0,
            "size": album.get("size") or 0,
            "status": album.get("status") or 0,
            "sub_type": album.get("subType") or "",
        }

    def get_artist_detail(self, artist_id: str) -> dict[str, Any]:
        """Fetch artist detail including hot songs, albums, description."""
        artist_id = str(artist_id or "").strip()
        if not artist_id.isdigit():
            return {"success": False, "error_key": "player.invalid_artist_id", "artist": None}
        artist = self._safe("artist detail", self.api.get_artist_detail, artist_id)
        if not artist:
            return {"success": False, "error_key": "player.artist_not_found", "artist": None}
        return {
            "success": True,
            "artist": self._artist_payload(artist),
            "hot_songs": [self._track_payload(index, song) for index, song in enumerate(artist.get("hotSongs") or [])],
            "albums": [self._album_payload_mini(album) for album in (artist.get("albums") or [])],
        }

    @staticmethod
    def _artist_payload(artist: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(artist.get("id", "")),
            "name": artist.get("name") or "Unknown",
            "cover_url": artist.get("picUrl") or artist.get("img1v1Url") or "",
            "description": artist.get("briefDesc") or artist.get("description") or "",
            "alias": artist.get("alias") or [],
            "trans": artist.get("trans") or "",
            "music_size": artist.get("musicSize") or 0,
            "mv_size": artist.get("mvSize") or 0,
            "album_size": artist.get("albumSize") or 0,
        }

    @staticmethod
    def _album_payload_mini(album: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(album.get("id", "")),
            "name": album.get("name") or "Unknown",
            "cover_url": album.get("picUrl") or album.get("coverImgUrl") or "",
            "publish_time": album.get("publishTime") or 0,
            "size": album.get("size") or 0,
        }

    def get_recommend_playlists(self) -> dict[str, Any]:
        """Get daily recommended playlists (personalized, needs login)."""
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return {"success": False, "error_key": "player.login_required", "playlists": []}
        playlists = self._safe("recommend playlists", self.api.get_recommend_playlists)
        if playlists is None:
            return {"success": False, "error_key": "error.network", "playlists": []}
        return {
            "success": True,
            "playlists": [self._playlist_payload(p) for p in playlists],
        }

    def get_personal_fm(self) -> dict[str, Any]:
        """Get personal FM track list (personalized radio, needs login)."""
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return {"success": False, "error_key": "player.login_required", "tracks": []}
        tracks = self._safe("personal fm", self.api.get_personal_fm)
        if tracks is None:
            return {"success": False, "error_key": "error.network", "tracks": []}
        return {
            "success": True,
            "tracks": [self._track_payload(index, song) for index, song in enumerate(tracks)],
        }

    def get_new_songs(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Get newly released songs."""
        songs = self._safe("new songs", self.api.get_new_songs, limit, offset)
        if songs is None:
            return {"success": False, "error_key": "error.network", "tracks": []}
        return {
            "success": True,
            "tracks": [self._track_payload(index, song) for index, song in enumerate(songs)],
        }

    def get_recommend_mvs(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Get recommended MVs."""
        mvs = self._safe("recommend mvs", self.api.get_recommend_mvs, limit, offset)
        if mvs is None:
            return {"success": False, "error_key": "error.network", "mvs": []}
        return {
            "success": True,
            "mvs": [self._mv_payload(mv) for mv in mvs],
        }

    @staticmethod
    def _mv_payload(mv: dict[str, Any]) -> dict[str, Any]:
        artist = mv.get("artist") or mv.get("artists") or [{}]
        artist_name = artist[0].get("name") if artist else "Unknown"
        return {
            "id": str(mv.get("id", "")),
            "name": mv.get("name") or "Unknown",
            "cover_url": mv.get("cover") or mv.get("img") or "",
            "artist": artist_name,
            "duration": mv.get("duration") or 0,
            "play_count": mv.get("playCount") or 0,
        }

    def get_top_lists(self) -> dict[str, Any]:
        """Get official toplist/charts."""
        toplists = self._safe("top lists", self.api.get_top_lists)
        if toplists is None:
            return {"success": False, "error_key": "error.network", "toplists": []}
        return {
            "success": True,
            "toplists": toplists,
        }

    def get_top_list_tracks(self, toplist_id: str) -> dict[str, Any]:
        """Get tracks for a specific toplist."""
        toplist_id = str(toplist_id or "").strip()
        if not toplist_id.isdigit():
            return {"success": False, "error_key": "playlist.invalid_input", "tracks": []}
        tracks = self._safe("top list tracks", self.api.get_top_list_tracks, toplist_id)
        if tracks is None:
            return {"success": False, "error_key": "error.network", "tracks": []}
        return {
            "success": True,
            "tracks": [self._track_payload(index, song) for index, song in enumerate(tracks)],
        }

    def search_suggest(self, keyword: str) -> dict[str, Any]:
        """Get search suggestions/autocomplete."""
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": True, "suggestions": []}
        suggestions = self._safe("search suggest", self.api.search_suggest, keyword)
        if suggestions is None:
            return {"success": False, "error_key": "error.network", "suggestions": []}
        return {"success": True, "suggestions": suggestions}

    def search_multi(self, keyword: str, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """Search multiple types: songs, playlists, artists, albums, mvs, users, lyrics."""
        keyword = str(keyword or "").strip()
        if not keyword:
            return {"success": False, "error_key": "player.search_empty", "results": {}}
        result = self._safe("search multi", self.api.search_multi, keyword, limit, offset)
        if result is None:
            return {"success": False, "error_key": "error.network", "results": {}}
        return {
            "success": True,
            "keyword": keyword,
            "results": {
                "songs": [self._track_payload(index, song) for index, song in enumerate(result.get("songs") or [])],
                "playlists": [self._playlist_payload(p) for p in (result.get("playlists") or [])],
                "artists": [self._artist_payload(a) for a in (result.get("artists") or [])],
                "albums": [self._album_payload_mini(a) for a in (result.get("albums") or [])],
                "mvs": [self._mv_payload(m) for m in (result.get("mvs") or [])],
            },
        }

    # -- playlist management ---------------------------------------------------
    def create_playlist(self, name: str, privacy: int = 0, description: str = "") -> dict[str, Any]:
        """Create a new playlist."""
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return {"success": False, "error_key": "player.login_required"}
        name = str(name or "").strip()
        if not name:
            return {"success": False, "error_key": "player.playlist_name_empty"}
        result = self._safe("create playlist", self.api.create_playlist, name, privacy, description or "")
        if not result:
            return {"success": False, "error_key": "error.network"}
        if result.get("code") != 200:
            return {"success": False, "error_key": "player.create_failed", "message": result.get("msg") or ""}
        return {"success": True, "playlist_id": str(result.get("id") or result.get("playlistId") or "")}

    def update_playlist(self, playlist_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Update playlist metadata."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return {"success": False, "error_key": "playlist.invalid_input"}
        name = data.get("name")
        description = data.get("description")
        privacy = data.get("privacy")
        result = self._safe("update playlist", self.api.update_playlist, playlist_id, name, description, privacy)
        if not result:
            return {"success": False, "error_key": "error.network"}
        if result.get("code") != 200:
            return {"success": False, "error_key": "player.update_failed", "message": result.get("msg") or ""}
        return {"success": True}

    def delete_playlist(self, playlist_ids: list[str]) -> dict[str, Any]:
        """Delete one or more playlists."""
        ids = [str(i) for i in (playlist_ids or []) if str(i).strip()]
        if not ids:
            return {"success": False, "error_key": "playlist.invalid_input"}
        result = self._safe("delete playlist", self.api.delete_playlist, ids)
        if not result:
            return {"success": False, "error_key": "error.network"}
        if result.get("code") != 200:
            return {"success": False, "error_key": "player.delete_failed", "message": result.get("msg") or ""}
        return {"success": True}

    def add_tracks_to_playlist(self, playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
        """Add tracks to a playlist."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return {"success": False, "error_key": "playlist.invalid_input"}
        ids = [str(i) for i in (track_ids or []) if str(i).strip()]
        if not ids:
            return {"success": False, "error_key": "player.no_tracks_to_add"}
        result = self._safe("add tracks to playlist", self.api.add_tracks_to_playlist, playlist_id, ids)
        if not result:
            return {"success": False, "error_key": "error.network"}
        if result.get("code") != 200:
            return {"success": False, "error_key": "player.add_failed", "message": result.get("msg") or ""}
        return {"success": True}

    def remove_tracks_from_playlist(self, playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
        """Remove tracks from a playlist."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return {"success": False, "error_key": "playlist.invalid_input"}
        ids = [str(i) for i in (track_ids or []) if str(i).strip()]
        if not ids:
            return {"success": False, "error_key": "player.no_tracks_to_remove"}
        result = self._safe("remove tracks from playlist", self.api.remove_tracks_from_playlist, playlist_id, ids)
        if not result:
            return {"success": False, "error_key": "error.network"}
        if result.get("code") != 200:
            return {"success": False, "error_key": "player.remove_failed", "message": result.get("msg") or ""}
        return {"success": True}

    def subscribe_playlist(self, playlist_id: str, subscribe: bool = True) -> dict[str, Any]:
        """Subscribe or unsubscribe from a playlist."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return {"success": False, "error_key": "playlist.invalid_input"}
        result = self._safe("subscribe playlist", self.api.subscribe_playlist, playlist_id, subscribe)
        if not result:
            return {"success": False, "error_key": "error.network"}
        if result.get("code") != 200:
            return {"success": False, "error_key": "player.subscribe_failed", "message": result.get("msg") or ""}
        return {"success": True, "subscribed": subscribe}

    def update_playlist_cover(self, playlist_id: str, cover_url: str) -> dict[str, Any]:
        """Update playlist cover image."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return {"success": False, "error_key": "playlist.invalid_input"}
        cover_url = str(cover_url or "").strip()
        if not cover_url:
            return {"success": False, "error_key": "player.cover_url_empty"}
        result = self._safe("update playlist cover", self.api.update_playlist_cover, playlist_id, cover_url)
        if not result:
            return {"success": False, "error_key": "error.network"}
        if result.get("code") != 200:
            return {"success": False, "error_key": "player.cover_update_failed", "message": result.get("msg") or ""}
        return {"success": True}

    # -- local playlist management ---------------------------------------------
    def get_local_playlists(self) -> dict[str, Any]:
        """Get all local playlists."""
        lp = self.settings.local_playlists
        return {
            "success": True,
            "playlists": lp.playlists,
            "sort_order": lp.sort_order,
        }

    def create_local_playlist(self, name: str) -> dict[str, Any]:
        """Create a new local playlist."""
        name = str(name or "").strip()
        if not name:
            return {"success": False, "error_key": "player.playlist_name_empty"}
        lp = self.settings.local_playlists
        new_id = f"local_{int(time.time() * 1000)}"
        playlist = {
            "id": new_id,
            "name": name,
            "tracks": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        lp.playlists.append(playlist)
        self.settings.update_local_playlists(playlists=lp.playlists)
        return {"success": True, "playlist": playlist}

    def update_local_playlist(self, playlist_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Update a local playlist."""
        lp = self.settings.local_playlists
        playlist = next((p for p in lp.playlists if p["id"] == playlist_id), None)
        if not playlist:
            return {"success": False, "error_key": "playlist.not_found"}
        if "name" in data:
            playlist["name"] = str(data["name"] or "").strip() or playlist["name"]
        if "tracks" in data:
            playlist["tracks"] = data["tracks"]
        if "sort_order" in data:
            lp.sort_order = data["sort_order"]
        playlist["updated_at"] = time.time()
        self.settings.update_local_playlists(playlists=lp.playlists, sort_order=lp.sort_order)
        return {"success": True, "playlist": playlist}

    def delete_local_playlist(self, playlist_id: str) -> dict[str, Any]:
        """Delete a local playlist."""
        lp = self.settings.local_playlists
        lp.playlists = [p for p in lp.playlists if p["id"] != playlist_id]
        self.settings.update_local_playlists(playlists=lp.playlists)
        return {"success": True}

    def reorder_local_playlist_tracks(self, playlist_id: str, from_index: int, to_index: int) -> dict[str, Any]:
        """Reorder tracks in a local playlist."""
        lp = self.settings.local_playlists
        playlist = next((p for p in lp.playlists if p["id"] == playlist_id), None)
        if not playlist:
            return {"success": False, "error_key": "playlist.not_found"}
        tracks = playlist.get("tracks", [])
        if from_index < 0 or from_index >= len(tracks) or to_index < 0 or to_index >= len(tracks):
            return {"success": False, "error_key": "player.invalid_index"}
        track = tracks.pop(from_index)
        tracks.insert(to_index, track)
        playlist["updated_at"] = time.time()
        self.settings.update_local_playlists(playlists=lp.playlists)
        return {"success": True}

    def add_tracks_to_local_playlist(self, playlist_id: str, track_keys: list[str]) -> dict[str, Any]:
        """Add tracks to a local playlist by their keys."""
        lp = self.settings.local_playlists
        playlist = next((p for p in lp.playlists if p["id"] == playlist_id), None)
        if not playlist:
            return {"success": False, "error_key": "playlist.not_found"}
        
        # Resolve track keys to track info
        existing_keys = {t.get("key") for t in playlist.get("tracks", [])}
        new_tracks = []
        for key in track_keys:
            if key in existing_keys:
                continue
            # Try local music first
            if key.startswith("local:"):
                path = key[6:]
                track = self.local_music.track(path)
                if track:
                    new_tracks.append(track)
                    existing_keys.add(key)
            else:
                # Online track - store minimal info
                new_tracks.append({"key": key})
                existing_keys.add(key)
        
        playlist.setdefault("tracks", []).extend(new_tracks)
        playlist["updated_at"] = time.time()
        self.settings.update_local_playlists(playlists=lp.playlists)
        return {"success": True, "added": len(new_tracks)}

    def remove_tracks_from_local_playlist(self, playlist_id: str, track_keys: list[str]) -> dict[str, Any]:
        """Remove tracks from a local playlist."""
        lp = self.settings.local_playlists
        playlist = next((p for p in lp.playlists if p["id"] == playlist_id), None)
        if not playlist:
            return {"success": False, "error_key": "playlist.not_found"}
        
        tracks = playlist.get("tracks", [])
        playlist["tracks"] = [t for t in tracks if t.get("key") not in track_keys]
        playlist["updated_at"] = time.time()
        self.settings.update_local_playlists(playlists=lp.playlists)
        return {"success": True, "removed": len(tracks) - len(playlist["tracks"])}

    # -- local music -----------------------------------------------------------
    def get_local_tracks(self, query: str = "", limit: int = 200,
                         offset: int = 0) -> dict[str, Any]:
        """A page of the local library, optionally filtered by ``query``.

        The UI keeps its own copy of what it has fetched, so a library with tens
        of thousands of files never crosses the bridge in one go.
        """
        tracks = self.local_music.tracks()
        needle = _match_key(query)
        if needle:
            tracks = [track for track in tracks
                      if needle in _match_key(track.get("name"))
                      or needle in _match_key(track.get("artists"))
                      or needle in _match_key(track.get("album"))]
        try:
            size = max(1, min(int(limit or 200), 5000))
            start = max(0, int(offset or 0))
        except (TypeError, ValueError):
            size, start = 200, 0
        return {
            "success": True,
            "tracks": tracks[start:start + size],
            "offset": start,
            "total": len(tracks),
            "directories": list(self.settings.playback.local_dirs),
        }

    def add_local_folder(self, rescan: bool = True) -> dict[str, Any]:
        """Ask for a folder, remember it and (optionally) scan right away."""
        if not self._window:
            return {"success": False, "error_key": "error.unknown"}
        try:
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG, directory=str(Path.home()))
        except Exception as exc:
            self.logger.warning("folder dialog failed: %s", exc)
            return {"success": False, "error_key": "error.permission"}
        if not result:
            return {"success": False, "cancelled": True}
        chosen = result[0] if isinstance(result, list | tuple) else result
        path = str(Path(str(chosen)).expanduser())
        directories = list(dict.fromkeys([*self.settings.playback.local_dirs, path]))
        self.settings.update_playback(local_dirs=directories, last_local_dir=path)
        self._match_index_token = None
        self.refresh_allowed_roots()
        payload: dict[str, Any] = {"success": True, "directory": path, "directories": directories}
        if rescan:
            payload.update(self.scan_local_music())
        return payload

    def remove_local_folder(self, directory: str) -> dict[str, Any]:
        directory = str(directory or "")
        directories = [item for item in self.settings.playback.local_dirs if item != directory]
        self.settings.update_playback(local_dirs=directories)
        self._match_index_token = None
        return {"success": True, "directories": directories}

    def scan_local_music(self, rescan: bool = False) -> dict[str, Any]:
        """Scan every configured folder; ``rescan`` re-reads the tag cache from disk."""
        directories = list(self.settings.playback.local_dirs)
        if not directories:
            return {"success": True, "tracks": 0, "directories": [], "scanned": 0}
        if rescan:
            self.local_music.clear()
        summary = self.local_music.refresh(directories,
                                           max_depth=self.settings.playback.scan_depth)
        self._match_index_token = None
        self.refresh_allowed_roots()
        return {"success": True, **summary, "stats": self.local_music.stats()}

    def get_local_stats(self) -> dict[str, Any]:
        return {"success": True, "stats": self.local_music.stats(),
                "directories": list(self.settings.playback.local_dirs),
                "media": self.media.stats()}

    # -- appearance ------------------------------------------------------------
    def pick_background_image(self) -> dict[str, Any]:
        """Choose an image, copy it into the config dir and return its url."""
        if not self._window:
            return {"success": False, "error_key": "error.unknown"}
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Image (*.jpg;*.jpeg;*.png;*.webp;*.gif;*.bmp;*.heic)",))
        except Exception as exc:
            self.logger.warning("image dialog failed: %s", exc)
            return {"success": False, "error_key": "error.permission"}
        if not result:
            return {"success": False, "cancelled": True}
        chosen = result[0] if isinstance(result, list | tuple) else result
        if not self.settings.set_background_image(chosen):
            return {"success": False, "error_key": "settings.background_failed"}
        return {"success": True, "background_url": self.background_url(),
                "settings": self.get_settings()}

    def clear_background_image(self) -> dict[str, Any]:
        self.settings.clear_background_image()
        return {"success": True, "background_url": "", "settings": self.get_settings()}

    # -- quick download from the player ---------------------------------------
    def download_tracks(self, tracks: list[dict[str, Any]] | None = None,
                        options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Queue the given tracks in the normal download pipeline.

        Online tracks are re-read from ``/api/v3/song/detail`` so the downloader
        gets its usual raw song shape; local files have nothing to download.
        """
        wanted = [self._clean_track(item) for item in (tracks or []) if isinstance(item, dict)]
        ids = [str(item.get("id")) for item in wanted
               if item.get("source") != "local" and str(item.get("id") or "").isdigit()]
        if not ids:
            return {"success": False, "error_key": "player.download_local_only"}
        songs = self._safe("song details", self.api.get_songs_detail, ids) or []
        if not songs:
            songs = [{"id": int(song_id), "name": next(
                (item.get("name") for item in wanted if str(item.get("id")) == song_id), "Unknown"),
                "artists": [{"name": next(
                    (item.get("artists") for item in wanted
                     if str(item.get("id")) == song_id), "Unknown")}]}
                for song_id in ids]
        return self._launch_download(songs, options or {})

    # --------------------------------------------------------------- utilities
    def get_api_stats(self) -> dict[str, Any]:
        return self.api.get_request_stats()

    # -- queue management --------------------------------------------------------
    def save_queue_as_playlist(self, name: str, queue: list[dict[str, Any]]) -> dict[str, Any]:
        """Save the given queue as a local playlist (M3U/JSON)."""
        if not name or not queue:
            return {"success": False, "error_key": "error.unknown"}
        try:
            from src.config import get_settings
            settings = get_settings()
            playlist_dir = settings.config_path.parent / "playlists"
            playlist_dir.mkdir(exist_ok=True)
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", name)
            m3u_path = playlist_dir / f"{safe_name}.m3u"
            with open(m3u_path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for track in queue:
                    if track.get("source") == "local" and track.get("path"):
                        f.write(f"{track['path']}\n")
                    elif track.get("source") == "online" and track.get("id"):
                        f.write(f"#EXTINF:-1,{track.get('artists', '')} - {track.get('name', '')}\n")
                        f.write(f"https://music.163.com/song?id={track['id']}\n")
            return {"success": True, "path": str(m3u_path), "message": t("player.queue_saved_as_playlist", {"name": name})}
        except Exception as exc:
            self.logger.warning("save_queue_as_playlist failed: %s", exc)
            return {"success": False, "error_key": "error.unknown", "message": str(exc)}

    # -- desktop lyrics window ---------------------------------------------------
    _desktop_lyrics_window: Any = None

    def open_desktop_lyrics(self) -> dict[str, Any]:
        """Open the desktop lyrics window as a native pywebview window."""
        if self._desktop_lyrics_window is not None:
            try:
                # Check if window still exists (pywebview doesn't have a direct way)
                self._desktop_lyrics_window.restore()
                return {"success": True}
            except Exception:
                self._desktop_lyrics_window = None

        if self._webview_module is None:
            # Fallback to JS-based window.open
            self._call_js("onOpenDesktopLyrics", {})
            return {"success": True}

        try:
            from src.resources import resource_path
            html_path = resource_path("src", "gui", "assets", "desktop-lyrics.html")
            if not html_path.exists():
                self.logger.warning("Desktop lyrics HTML not found at %s", html_path)
                self._call_js("onOpenDesktopLyrics", {})
                return {"success": True}

            settings = self.settings
            ui = settings.ui
            font_size = ui.desktop_lyrics_font_size
            color = ui.desktop_lyrics_color
            opacity = ui.desktop_lyrics_opacity
            show_translation = ui.desktop_lyrics_show_translation

            url = f"{html_path.as_uri()}?fontSize={font_size}&color={color}&opacity={opacity}&showTranslation={show_translation}"

            self._desktop_lyrics_window = self._webview_module.create_window(
                title="Desktop Lyrics",
                url=url,
                js_api=self,
                width=600,
                height=120,
                min_size=(300, 80),
                resizable=True,
                frameless=True,
                easy_drag=True,
                on_top=True,
                transparent=True,
                background_color="#00000000",
            )

            # Store reference to this bridge for the desktop lyrics window
            self._desktop_lyrics_window.events.closed += self._on_desktop_lyrics_closed

            # Push current lyrics if available
            self._call_js("onOpenDesktopLyrics", {})
            return {"success": True}
        except Exception as exc:
            self.logger.warning("Failed to create desktop lyrics window: %s", exc)
            self._call_js("onOpenDesktopLyrics", {})
            return {"success": True}

    def _on_desktop_lyrics_closed(self) -> None:
        self._desktop_lyrics_window = None
        self._call_js("onCloseDesktopLyrics", {})

    def close_desktop_lyrics(self) -> dict[str, Any]:
        """Close the desktop lyrics window."""
        if self._desktop_lyrics_window is not None:
            try:
                self._desktop_lyrics_window.destroy()
            except Exception:
                pass
            self._desktop_lyrics_window = None
        self._call_js("onCloseDesktopLyrics", {})
        return {"success": True}

    def update_desktop_lyrics(self, lines: list[dict[str, Any]], index: int) -> dict[str, Any]:
        """Push lyric update to the desktop lyrics window."""
        self._call_js("onDesktopLyricsUpdate", {"lines": lines, "index": index})
        return {"success": True}

    # -- sleep timer -------------------------------------------------------------
    def set_sleep_timer(self, minutes: int, action: str = "pause") -> dict[str, Any]:
        """Set a sleep timer (0 to cancel). Action: stop|pause|quit."""
        self.settings.update_playback(sleep_timer_minutes=max(0, int(minutes or 0)))
        self._call_js("onSleepTimerSet", {"minutes": minutes, "action": action})
        return {"success": True, "minutes": minutes, "action": action}

    def get_sleep_timer(self) -> dict[str, Any]:
        """Get the current sleep timer setting."""
        return {"success": True, "minutes": self.settings.playback.sleep_timer_minutes}

    # -- detailed playback stats -------------------------------------------------
    def get_detailed_playback_stats(self) -> dict[str, Any]:
        """Return detailed playback statistics for the dashboard."""
        stats = self.library.stats()
        # Add time-based stats (would need library enhancement)
        return {
            "success": True,
            "tracks": stats.get("tracks", 0),
            "total_plays": stats.get("total_plays", 0),
            "favorites": stats.get("favorites", 0),
            "recent": stats.get("recent", 0),
            # Placeholder for future enhancements
            "total_time_hours": 0,
            "this_week_plays": 0,
            "this_month_plays": 0,
            "top_artists": [],
            "top_albums": [],
            "top_genres": [],
        }

    # -- crossfade / preload -----------------------------------------------------
    def set_crossfade_duration(self, seconds: float) -> dict[str, Any]:
        """Set crossfade duration (0-12 seconds)."""
        seconds = max(0.0, min(float(seconds or 0), 12.0))
        self.settings.update_playback(crossfade_duration=seconds)
        if self.media.running:
            self.media.set_crossfade_duration(seconds)
        return {"success": True, "crossfade_duration": seconds}

    def preload_next_track(self, song_id: str, quality: str = "standard") -> dict[str, Any]:
        """Pre-resolve and warm up the next track's URL."""
        if not song_id:
            return {"success": False, "error_key": "error.unknown"}
        try:
            info = self.api.get_song_url_info(song_id, quality)
            if info and info.get("url"):
                # Warm up the connection
                self.media.resolve_online(song_id, quality, force=True)
                return {"success": True, "preloaded": True}
        except Exception as exc:
            self.logger.debug("preload_next_track failed: %s", exc)
        return {"success": False, "preloaded": False}
