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
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import webview

from src.auth import LoginStatus, get_login_manager
from src.config import default_download_dir, get_settings
from src.core import get_api
from src.core.downloader import MAX_CONCURRENT, DownloadStats, DownloadTask, SongDownloader
from src.gui.theme import effective_theme, normalize_theme, system_theme
from src.i18n import get_i18n, t
from src.version import APP_DISPLAY_NAME, AUTHOR, BUNDLE_ID, HOMEPAGE, LICENSE, __version__

__all__ = ["GuiBridge"]


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

        self.login_manager.on("status_change", self._on_login_status_change)
        self.login_manager.on("qrcode_update", self._on_qrcode_update)
        self.login_manager.on("login_success", self._on_login_success)
        self.login_manager.on("login_failed", self._on_login_failed)

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
            "ui": {
                "language": self.settings.ui.language,
                "theme": self.settings.ui.theme,
                "remember_window_size": self.settings.ui.remember_window_size,
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
            elif category == "ui":
                self.settings.update_ui(**self._clean(data, {"language", "theme",
                                                             "remember_window_size"}))
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
            "user": {
                "user_id": user.user_id,
                "nickname": user.nickname,
                "avatar_url": user.avatar_url,
                "vip_type": user.vip_type,
            } if user else None,
        }

    def login_qrcode(self) -> dict[str, Any]:
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
            "error_key": None if result["success"] else "login.code_send_failed",
            "message": result.get("message", ""),
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
        self._call_js("onLoginStatusChange", {"status": status.value})

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
        import re

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
        return {
            "index": index,
            "id": str(song.get("id", "")),
            "name": song.get("name") or "Unknown",
            "artists": ", ".join(a.get("name", "Unknown") for a in artists if isinstance(a, dict))
            or "Unknown",
            "album": album.get("name") or "",
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

    # --------------------------------------------------------------- utilities
    def get_api_stats(self) -> dict[str, Any]:
        return self.api.get_request_stats()
