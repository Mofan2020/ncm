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
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import webview

from src.auth import LoginStatus, get_login_manager
from src.config import PLAY_MODES, default_download_dir, get_settings
from src.core import backup as backup_core
from src.core import get_api
from src.core.api import LOGIN_REQUIRED_CODES
from src.core.downloader import MAX_CONCURRENT, DownloadStats, DownloadTask, SongDownloader
from src.core.library import get_library, track_key
from src.core.localmusic import (
    get_local_music,
    read_embedded_lyrics,
    sidecar_lyrics_path,
)
from src.core.lyrics import build_timed_lyrics
from src.core.mediaserver import get_media_server
from src.core.metacache import get_meta_cache
from src.gui.bangumi_bridge import BangumiBridge
from src.gui.bilibili_bridge import BilibiliBridge
from src.gui.theme import effective_theme, normalize_theme, system_theme
from src.i18n import get_i18n, t
from src.version import (
    APP_DISPLAY_NAME,
    AUTHOR,
    BUNDLE_ID,
    DISCLAIMER_VERSION,
    HOMEPAGE,
    LICENSE,
    __version__,
)

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


_KNOWN_COVER_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "bmp", "heic")


def _ext_from_cover_url(url: str) -> str:
    """Guess the file suffix for a NetEase cover URL.

    The NetEase CDN answers with the requested size parameter (``?param=300y300``)
    and rarely includes a real suffix.  We pick the safest default (``jpg``)
    when nothing is recognised.
    """
    text = str(url or "").lower()
    for ext in _KNOWN_COVER_EXTS:
        if f".{ext}" in text:
            return "jpg" if ext == "jpeg" else ext
    return "jpg"


def _decode_data_url(data_url: str) -> bytes:
    """The bytes behind a ``data:`` url, or empty when there are none.

    The phone has no native file dialog the bridge can open, so a picked
    file travels through the page as a data url.  Only the base64 form is
    accepted; a plain-text url is not a file.
    """
    raw = str(data_url or "").strip()
    if not raw.startswith("data:"):
        return b""
    _, _, payload = raw.partition(",")
    if not payload:
        return b""
    try:
        return base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return b""


def _purge_directory(directory: Any) -> int:
    """Delete every regular file in ``directory``; return how many went."""
    from pathlib import Path

    root = Path(directory)
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_file():
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


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
        #: The Bilibili player windows, one per video that is playing.
        self._bili_player_windows: dict[str, Any] = {}

        # ---- player state ----------------------------------------------------
        self.library = get_library()
        self.local_music = get_local_music()
        self.media = get_media_server()
        #: Normalised "title|artist" -> local track, rebuilt on demand.
        self._match_index: dict[str, dict[str, Any]] = {}
        self._match_index_token: Any = None
        self._favorite_ids: set[str] = set()
        self._favorite_ids_loaded = False
        #: 上一次导入算出来的计划和备份内容：确认冲突要用，写入的时候才有。
        self._backup_plan: dict[str, Any] | None = None
        self._backup_payload: dict[str, Any] | None = None
        #: The Bilibili music source.  Composed, not inherited: the bridge
        #: layer knows how to talk to the frontend, the source knows how to
        #: talk to Bilibili.
        self.bilibili = BilibiliBridge(media=self.media)
        # Bangumi（番组计划）只读条目资料。没有账号——收藏和观看历史
        # 都存在本地。客户端跟着设置走，所以设置改了之后这里要重建。
        self.bangumi = BangumiBridge()
        self.bangumi.set_settings(self.settings)
        # The remembered Bilibili cookie has to reach the media server too:
        # the CDN refuses the video streams without it.
        bili_session = self.bilibili.session
        if bili_session is not None:
            bili_session.apply_to_media(self.media)

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
        info = {
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
        # The loopback base URL of the media server.  The webview needs it
        # to build cover URLs on its own: asking the backend for every
        # cover meant one IPC round-trip per image, so the album art
        # flickered and every launch re-resolved the whole list.
        media = getattr(self, "media", None)
        base_url = getattr(media, "base_url", None) if media is not None else None
        if base_url:
            info["media_base_url"] = base_url
        return info

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
                "local_video_dirs": list(self.settings.playback.local_video_dirs),
                "scan_depth": self.settings.playback.scan_depth,
                "show_translation": self.settings.playback.show_translation,
                "lyrics_scroll_idle_seconds": self.settings.playback.lyrics_scroll_idle_seconds,
                # The crossfade length is a playback setting, so the phone
                # can show it next to the other playback settings instead of
                # hiding it behind a control that only the desktop has.
                "crossfade_duration": self.settings.playback.crossfade_duration,
            },
            "ui": {
                "language": self.settings.ui.language,
                "desktop_lyrics_font_size": self.settings.ui.desktop_lyrics_font_size,
                "desktop_lyrics_color": self.settings.ui.desktop_lyrics_color,
                "desktop_lyrics_opacity": self.settings.ui.desktop_lyrics_opacity,
                "desktop_lyrics_show_translation": self.settings.ui.desktop_lyrics_show_translation,
                "desktop_lyrics_background": self.settings.ui.desktop_lyrics_background,
                "desktop_lyrics_text_style": self.settings.ui.desktop_lyrics_text_style,
                "theme": self.settings.ui.theme,
                "remember_window_size": self.settings.ui.remember_window_size,
                "background_image": self.settings.ui.background_image,
                "background_url": self.background_url(),
                "background_blur": self.settings.ui.background_blur,
                "background_dim": self.settings.ui.background_dim,
                "glass": self.settings.ui.glass,
                "accent_from_background": self.settings.ui.accent_from_background,
                # 自动更新的两个开关和「跳过的版本」：漏进回包的话，
                # 设置面板读回来永远是默认值。
                "check_updates_on_start": self.settings.ui.check_updates_on_start,
                "auto_install_updates": self.settings.ui.auto_install_updates,
                # 版本通道和 SSL 校验开关：少了这两个，前端读回来
                # 永远是默认值，用户勾了像没勾。
                "update_include_prerelease": self.settings.ui.update_include_prerelease,
                "update_skip_ssl_verify": self.settings.ui.update_skip_ssl_verify,
                "skipped_update_versions": list(self.settings.ui.skipped_update_versions or []),
            },
            "auth": {
                "remember_login": self.settings.auth.remember_login,
            },
            "bili": {
                "quality": self.settings.bili.quality,
                "danmaku_enabled": self.settings.bili.danmaku_enabled,
                "danmaku_font_size": self.settings.bili.danmaku_font_size,
                "danmaku_opacity": self.settings.bili.danmaku_opacity,
                "download_audio": self.settings.bili.download_audio,
                "merge_audio": self.settings.bili.merge_audio,
                "download_dir": self.settings.bili.download_dir or self._download_dir(),
                "separate_player_window": self.settings.bili.separate_player_window,
                # 这三个是新加的，漏掉的话设置面板读回来永远是「没勾选」，
                # 用户点保存看着像生效了，其实什么都没存。
                "report_playback": self.settings.bili.report_playback,
                "resume_playback": self.settings.bili.resume_playback,
                "interest_tags": list(self.settings.bili.interest_tags),
                "blocked_tags": list(self.settings.bili.blocked_tags),
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
            elif category == "bili":
                self.settings.update_bili(**self._clean(data, {
                    "quality", "danmaku_enabled", "danmaku_font_size",
                    "danmaku_opacity", "download_audio", "merge_audio",
                    "download_dir", "separate_player_window",
                    # 少了这几个，播放上报、续播的开关和两个关键词栏点保存就没反应。
                    "report_playback", "resume_playback",
                    "interest_tags", "blocked_tags"}))
            elif category == "bangumi":
                self.settings.update_bangumi(**self._clean(data, {
                    "api_host", "web_host", "request_interval",
                    "skip_ssl_verify", "download_dir",
                    "mirrors", "resource_sites"}))
                # 主机、间隔、目录都可能变了：把客户端和下载器扔掉重建，
                # 否则改完设置还是按旧的来。
                self.bangumi.reload()
            elif category == "ui":
                self.settings.update_ui(**self._clean(data, {
                    "language", "theme", "remember_window_size", "background_image",
                    "background_blur", "background_dim", "glass",
                    "accent_from_background",
                    # The desktop-lyrics options are part of the UI settings
                    # too; without them in the allowlist they were silently
                    # dropped and never persisted.
                    "desktop_lyrics_font_size", "desktop_lyrics_color",
                    "desktop_lyrics_opacity", "desktop_lyrics_show_translation",
                    "desktop_lyrics_background", "desktop_lyrics_text_style",
                    # 自动更新：同样要在白名单里，否则开关点了也存不下。
                    "check_updates_on_start", "auto_install_updates",
                    "update_include_prerelease", "update_skip_ssl_verify",
                    "skipped_update_versions"}))
                if "language" in data:
                    self.i18n.set_language(str(data["language"]))
                if "theme" in data:
                    self._sync_native_theme()
            elif category == "auth":
                cleaned = self._clean(data, {"remember_login", "login_method"})
                self.settings.update_auth(**cleaned)
                # Turning "remember the login" off has to take the saved
                # cookie with it, otherwise the next start finds the cookie
                # on disk and signs the account in again -- the switch
                # would only stop the next cookie from being written.
                if ("remember_login" in cleaned
                        and not cleaned["remember_login"]):
                    self.settings.update_auth(saved_cookie="")
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
                                      "local_video_dirs",
                                      "scan_depth", "show_translation", "last_local_dir",
                                      "lyrics_scroll_idle_seconds"})
        if "play_mode" in cleaned and cleaned["play_mode"] not in PLAY_MODES:
            cleaned["play_mode"] = "list"
        if isinstance(cleaned.get("local_dirs"), list):
            cleaned["local_dirs"] = [str(item) for item in cleaned["local_dirs"]
                                     if str(item or "").strip()]
        if isinstance(cleaned.get("local_video_dirs"), list):
            cleaned["local_video_dirs"] = [str(item) for item in cleaned["local_video_dirs"]
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

    def choose_file(self, kind: str = "video") -> dict[str, Any]:
        """弹原生打开对话框选一个文件（投稿要的视频或封面）。

        ``kind`` 取 ``"video"`` 或 ``"image"``。
        """
        if not self._window:
            return {"success": False, "path": ""}
        filters = {
            "video": "Video files (*.mp4;*.mov;*.mkv;*.flv;*.avi;*.webm)",
            "image": "Image files (*.jpg;*.jpeg;*.png;*.webp)",
        }
        wanted = str(kind or "video").lower()
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=(filters.get(wanted, filters["video"]),),
            )
        except Exception as exc:
            self.logger.warning("file dialog failed: %s", exc)
            return {"success": False, "path": ""}
        if not result:
            return {"success": False, "path": ""}
        chosen = result[0] if isinstance(result, list | tuple) else result
        return {"success": True, "path": str(chosen)}

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

    # ------------------------------------------------------------- login cookie
    def export_login_cookie(self) -> dict[str, Any]:
        """把当前登录 cookie 存成一个文件，方便在另一台设备上免密登录。

        这个 cookie 就是账号的会话凭据：拿到它的人就是这个账号。文件按 0600
        写，界面同时把内容显示出来，剪贴板或者文件哪条路都不通的时候还能
        手动复制。
        """
        cookie = self.login_manager.cookie_string()
        if not cookie:
            return {"success": False, "error_key": "backup.login_required"}
        path = self._choose_cookie_save_path()
        if not path:
            return {"success": False, "cancelled": True}
        try:
            path.write_text(cookie + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError as exc:
            self.logger.warning("cannot write the cookie file: %s", exc)
            return {"success": False, "error_key": "backup.write_failed",
                    "message": str(exc)}
        return {"success": True, "cookie": cookie, "path": str(path)}

    def copy_login_cookie(self) -> dict[str, Any]:
        """把当前登录 cookie 复制到系统剪贴板。"""
        cookie = self.login_manager.cookie_string()
        if not cookie:
            return {"success": False, "error_key": "backup.login_required"}
        return {"success": True, "cookie": cookie,
                "copied": self._copy_to_clipboard(cookie)}

    def _choose_cookie_save_path(self) -> Path | None:
        """弹原生保存对话框，返回用户选好的路径。"""
        if not self._window:
            return None
        try:
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path.home()),
                save_filename="ncm-login-cookie.txt",
                file_types=("Text (*.txt)",))
        except Exception as exc:
            self.logger.warning("cookie save dialog failed: %s", exc)
            return None
        if not result:
            return None
        chosen = result[0] if isinstance(result, list | tuple) else result
        return Path(str(chosen))

    def _copy_to_clipboard(self, text: str) -> bool:
        """把文本放进系统剪贴板。"""
        command = ["pbcopy"] if platform.system() == "Darwin" else ["clip"]
        try:
            subprocess.run(command, input=text.encode("utf-8"), check=True,
                           capture_output=True, timeout=10)
            return True
        except Exception as exc:
            self.logger.warning("clipboard write failed: %s", exc)
            return False

    # ------------------------------------------------------------ account backup
    def export_account_backup(self) -> dict[str, Any]:
        """把账号数据导出成一个 ``.ncmbk`` 文件。

        先弹原生保存对话框让用户选位置，再去云端把数据拉下来写进去。云端
        读不到的部分退到本地缓存，导出的文件里会标明每一节的来源。

        **网易云和 B站登录任意一个就能导出**：只登录了 B站时，文件里是
        B站那一半的数据；两边都登录了就都写进去。
        """
        user = self.login_manager.user_info
        netease_signed_in = bool(user and user.user_id)
        bili_signed_in = bool(self._bili_signed_in())
        if not netease_signed_in and not bili_signed_in:
            return {"success": False, "error_key": "backup.login_required"}
        nickname = (user.nickname if netease_signed_in else "") or ""
        path = self._choose_backup_save_path(nickname or "SkycMedia")
        if not path:
            return {"success": False, "cancelled": True}
        payload: dict[str, Any] = {}
        if netease_signed_in:
            try:
                payload = backup_core.export_account_data(
                    self.api,
                    uid=user.user_id,
                    account=self._backup_account_payload(user),
                    cloud_profile=self._backup_cloud_profile,
                    cache=get_meta_cache(),
                    local=self.library.exportable_state(),
                    progress=self._on_backup_progress,
                )
            except Exception as exc:
                self.logger.warning("account backup export failed: %s", exc)
                return {"success": False, "error_key": "backup.export_failed",
                        "message": str(exc)}
        else:
            # 只登录了 B站：文件里也要有格式标识和版本号，导入的时候才认得。
            payload = {
                "format": backup_core.BACKUP_FORMAT,
                "version": backup_core.BACKUP_VERSION,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "app_version": __version__,
                "sources": {},
                "account": {},
                "liked_song_ids": [],
                "playlists": [],
                "followed_artists": [],
                "liked_albums": [],
                "subscribed_djradios": [],
                "liked_mvs": [],
                "play_record": [],
                "local_state": {},
                "errors": [],
            }
        if bili_signed_in:
            try:
                payload["bilibili"] = backup_core.export_bilibili_data(
                    self.bilibili.client,
                    progress=self._on_backup_progress,
                )
            except Exception as exc:
                self.logger.warning("bilibili backup export failed: %s", exc)
                payload.setdefault("errors", []).append(
                    {"section": "bilibili", "message": str(exc)})
        # Bangumi 没有账号，但本地收藏和观看历史要进备份。
        # 这一节纯本地，不发请求。
        try:
            payload["bangumi"] = backup_core.export_bangumi_data(
                self.bangumi.store,
                progress=self._on_backup_progress,
            )
        except Exception as exc:
            self.logger.warning("bangumi backup export failed: %s", exc)
            payload.setdefault("errors", []).append(
                {"section": "bangumi", "message": str(exc)})
        try:
            backup_core.write_backup_file(path, payload)
        except OSError as exc:
            self.logger.warning("cannot write the backup file: %s", exc)
            return {"success": False, "error_key": "backup.write_failed",
                    "message": str(exc)}
        local_state = payload.get("local_state") or {}
        bili_section = payload.get("bilibili") or {}
        bili_folders = bili_section.get("folders") or []
        bangumi_section = payload.get("bangumi") or {}
        return {
            "success": True,
            "path": str(path),
            "sources": payload.get("sources", {}),
            "playlists": len(payload.get("playlists") or []),
            "songs": sum(len(item.get("song_ids") or [])
                         for item in (payload.get("playlists") or [])),
            "favorites": len(payload.get("liked_song_ids") or []),
            "artists": len(payload.get("followed_artists") or []),
            "albums": len(payload.get("liked_albums") or []),
            "djradios": len(payload.get("subscribed_djradios") or []),
            "mvs": len(payload.get("liked_mvs") or []),
            "play_record": len(payload.get("play_record") or []),
            "local_play_counts": len(local_state.get("play_counts") or {}),
            "local_favorites": len(local_state.get("favorites") or []),
            "local_recent": len(local_state.get("recent") or []),
            "bilibili_folders": len(bili_folders),
            "bilibili_videos": sum(len(item.get("videos") or [])
                                   for item in bili_folders),
            "bangumi_favorites": len(bangumi_section.get("favorites") or []),
            "bangumi_history": len(bangumi_section.get("history") or []),
            "netease": netease_signed_in,
            "bilibili": bili_signed_in,
            "errors": payload.get("errors", []),
        }

    def _bili_signed_in(self) -> bool:
        """Whether a Bilibili account is signed in."""
        session = self.bilibili.session
        if session is None:
            return False
        try:
            return bool(session.is_logged_in)
        except Exception:  # pragma: no cover - the session does not raise
            return False

    def import_account_backup(self, file_path: str) -> dict[str, Any]:
        """读一个 ``.ncmbk`` 文件，算出导入要做的事。

        这一步**不写任何东西**：先把冲突清单交给用户确认，拿到决定之后才
        调 :meth:`apply_account_backup` 真正写入。

        备份里有没有 B站那一节都能导入：没有就只恢复网易云的部分，有的话
        登录了 B站就一起恢复，没登录就跳过并说明。
        """
        user = self.login_manager.user_info
        netease_signed_in = bool(user and user.user_id)
        if not netease_signed_in and not self._bili_signed_in():
            # 备份里只有 Bangumi 那一节的话，其实不需要登录任何账号——
            # 那是纯本地的数据。先把文件读出来看一眼再决定。
            try:
                peek = backup_core.read_backup_file(file_path)
            except (OSError, backup_core.BackupError) as exc:
                return {"success": False, "error_key": "backup.read_failed",
                        "message": str(exc)}
            bangumi_only = bool(peek.get("bangumi")) and not any(
                peek.get(key) for key in
                ("account", "liked_song_ids", "playlists", "followed_artists",
                 "bilibili"))
            if not bangumi_only:
                return {"success": False, "error_key": "backup.login_required"}
        try:
            payload = backup_core.read_backup_file(file_path)
        except backup_core.BackupFormatError as exc:
            return {"success": False, "error_key": "backup.invalid_file",
                    "message": str(exc)}
        except (OSError, backup_core.BackupError) as exc:
            return {"success": False, "error_key": "backup.read_failed",
                    "message": str(exc)}
        try:
            plan = backup_core.plan_import(
                self.api, payload, uid=user.user_id if netease_signed_in else "",
                bili=self.bilibili.client if self._bili_signed_in() else None,
            )
        except Exception as exc:
            self.logger.warning("cannot plan the account backup import: %s", exc)
            return {"success": False, "error_key": "backup.plan_failed",
                    "message": str(exc)}
        self._backup_plan = plan
        self._backup_payload = payload
        return {
            "success": True,
            "summary": plan.get("summary", {}),
            "conflicts": plan.get("conflicts", []),
            "bilibili": plan.get("bilibili", {}),
            "bangumi": plan.get("bangumi", {}),
            "skipped": [{"id": item.get("id"), "name": item.get("name"),
                         "reason": item.get("reason")}
                        for item in plan.get("playlists", [])
                        if item.get("action") == "skip"],
            "source": plan.get("source", {}),
        }

    def apply_account_backup(self, decisions: dict[str, Any] | None) -> dict[str, Any]:
        """按用户的决定把备份并进当前账号。

        ``decisions`` 的形状是 ``{"playlists": {歌单 id: {"action": ..., "name": ...}}}``。
        冲突没给决定就一步都不写。
        """
        plan = self._backup_plan
        payload = self._backup_payload
        if not plan or not payload:
            return {"success": False, "error_key": "backup.no_plan"}
        try:
            result = backup_core.apply_import(
                self.api, payload, plan, decisions,
                progress=self._on_backup_progress,
                local=self.library,
                bili=self.bilibili.client if self._bili_signed_in() else None,
                store=self.bangumi.store,
            )
        except Exception as exc:
            self.logger.warning("account backup import failed: %s", exc)
            return {"success": False, "error_key": "backup.import_failed",
                    "message": str(exc)}
        if not result.get("ok"):
            return {"success": False, "error_key": "backup.conflict_unresolved",
                    "missing": result.get("missing", [])}
        self._backup_plan = None
        self._backup_payload = None
        return {
            "success": True,
            "songs_liked": result.get("songs_liked", 0),
            "songs_skipped": result.get("songs_skipped", 0),
            "playlists_created": result.get("playlists_created", 0),
            "playlists_merged": result.get("playlists_merged", 0),
            "playlists_skipped": result.get("playlists_skipped", 0),
            "artists_followed": result.get("artists_followed", 0),
            "albums_liked": result.get("albums_liked", 0),
            "djradios_subscribed": result.get("djradios_subscribed", 0),
            "mvs_liked": result.get("mvs_liked", 0),
            "record_counts": result.get("record_counts", 0),
            "tracks_added": result.get("tracks_added", 0),
            "failed": result.get("failed", 0),
            "skipped_detail": result.get("skipped_detail", []),
            "local_restored": result.get("local_restored", {}),
            "bilibili": result.get("bilibili", {}),
            "bangumi": result.get("bangumi", {}),
            "errors": result.get("errors", []),
        }

    def choose_backup_file(self) -> dict[str, Any]:
        """弹原生打开对话框选一个 ``.ncmbk`` 文件。"""
        if not self._window:
            return {"success": False, "error_key": "error.unknown"}
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=(f"Account backup (*{backup_core.BACKUP_SUFFIX})",))
        except Exception as exc:
            self.logger.warning("backup open dialog failed: %s", exc)
            return {"success": False, "error_key": "error.permission"}
        if not result:
            return {"success": False, "cancelled": True}
        chosen = result[0] if isinstance(result, list | tuple) else result
        return {"success": True, "path": str(chosen)}

    def _choose_backup_save_path(self, nickname: str) -> str | None:
        """弹原生保存对话框，返回用户选好的路径。"""
        if not self._window:
            return None
        default_name = backup_core.default_backup_name(nickname)
        try:
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path.home()),
                save_filename=default_name,
                file_types=(f"Account backup (*{backup_core.BACKUP_SUFFIX})",))
        except Exception as exc:
            self.logger.warning("backup save dialog failed: %s", exc)
            return None
        if not result:
            return None
        chosen = result[0] if isinstance(result, list | tuple) else result
        path = Path(str(chosen))
        # 用户改了扩展名也按备份文件写回去，省得下次导入认不出来。
        if path.suffix.lower() != backup_core.BACKUP_SUFFIX:
            path = path.with_name(path.name + backup_core.BACKUP_SUFFIX)
        return str(path)

    def _backup_account_payload(self, user: Any) -> dict[str, Any]:
        """导出时用的账号资料——本地会话里已经有的那些。"""
        profile = dict(getattr(user, "profile", None) or {})
        return {
            "user_id": str(getattr(user, "user_id", "") or ""),
            "nickname": str(getattr(user, "nickname", "") or ""),
            "avatar_url": str(getattr(user, "avatar_url", "") or ""),
            "vip_type": getattr(user, "vip_type", 0),
            "profile": profile,
        }

    def _backup_cloud_profile(self) -> dict[str, Any] | None:
        """再读一次账号资料，拿到最新的注册时间之类的字段。"""
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return None
        detail = self._safe("user detail", self.api.get_user_detail, user.user_id)
        if not isinstance(detail, dict):
            return None
        profile = detail.get("profile")
        return dict(profile) if isinstance(profile, dict) else None

    def _on_backup_progress(self, phase: str, current: int, total: int) -> None:
        """导出 / 导入的进度推给前端。"""
        self._call_js("onAccountBackupProgress", {
            "phase": phase,
            "current": int(current),
            "total": int(total),
        })

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
        return {
            "success": ok,
            "error_key": None if ok else "login.login_failed",
            # The QR image comes back with the answer, not as a separate
            # push.  The desktop front end listens for the ``onQrcodeUpdate``
            # event, but the phone front end polls: it calls this method and
            # paints ``result.qr_code`` straight away.  Without the image in
            # the reply the phone showed an empty frame and the message
            # "二维码取不到，稍后重试" while the QR code was perfectly fine.
            #
            # Only a code that was actually minted is worth returning: after
            # a refused key request the old key is still lying around, and
            # its picture is a QR code that nobody is polling for.
            "qr_code": self._qrcode_image() if ok else None,
        }

    def _qrcode_image(self) -> str | None:
        """The pending QR code as a data URL, or ``None`` if there is none."""
        url = getattr(self.login_manager, "qrcode_url", None)
        if not url:
            return None
        image = self.login_manager.get_qrcode_image(url)
        if not image:
            return None
        encoded = base64.b64encode(image).decode("ascii")
        return f"data:image/png;base64,{encoded}"

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

    def check_phone_registered(self, phone: str, ctcode: str = "86") -> dict[str, Any]:
        """Whether the number already has a NetEase account."""
        result = self.login_manager.check_phone_registered(phone, ctcode)
        return {
            "success": result["known"],
            "registered": result["registered"],
            "known": result["known"],
            "error_key": None if result["known"] else "login.register_check_failed",
            "message": result.get("message", ""),
        }

    def register_phone(self, phone: str, code: str, password: str,
                       nickname: str, ctcode: str = "86") -> dict[str, Any]:
        """Create an account for a phone number and sign in with it."""
        result = self.login_manager.register_phone(phone, code, password, nickname, ctcode)
        return {
            "success": result["success"],
            "error_key": result.get("error_key"),
            "message": result.get("message", ""),
            "registered": bool(result.get("registered")),
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
            # ``a.get("name", "Unknown")`` is not enough: when the key is
            # there but the value is null (NetEase does that in the big
            # playlists) it hands back None and the join below raises
            # TypeError.  The "or" makes the default win over a null.
            "artists": ", ".join(
                str(a.get("name") or "Unknown")
                for a in artists if isinstance(a, dict)) or "Unknown",
            # The ids as well as the names: the now-playing panel turns each
            # name into a link to that artist's page, which needs an id.
            "artist_list": [
                {"id": str(a.get("id") or ""), "name": str(a.get("name") or "Unknown")}
                for a in artists
                if isinstance(a, dict) and a.get("id") and a.get("name")
            ],
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

    # -- application update ----------------------------------------------------
    def check_app_update(self, include_prerelease: bool = False) -> dict[str, Any]:
        """Ask GitHub whether a newer release exists.

        The user can skip a version; a skipped tag is reported back as
        ``skipped: true`` so the UI can stay quiet about it instead of
        asking the same question on every start.

        The channel comes from the settings: "允许从预发布版本升级" decides
        whether a pre-release may be offered at all, and the argument is
        only kept so the frontend call does not break.  The certificate
        check is skipped when the user turned it off -- an accelerator
        proxy rewrites the certificate and the request would fail
        otherwise.
        """
        from src.core.updater import check_for_update
        from src.version import __version__

        try:
            result = check_for_update(
                __version__,
                include_prerelease=bool(self.settings.ui.update_include_prerelease),
                verify=not self.settings.ui.update_skip_ssl_verify,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("update check failed: %s", exc)
            return {"success": False, "message": str(exc), "skipped": False}
        skipped = str(result.get("latest") or "") in [
            str(item) for item in (self.settings.ui.skipped_update_versions or [])
        ]
        result["skipped"] = skipped
        # The frontend has to pick the file for this machine; it needs the
        # platform and the CPU it is running on to do that.
        result["platform"] = "windows" if os.name == "nt" else "macos"
        machine = platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            result["arch"] = "arm64"
        elif machine in ("x86_64", "amd64"):
            result["arch"] = "x64"
        else:
            result["arch"] = machine
        # Only a really newer version is worth a popup.
        result["available"] = bool(result.get("success")
                                   and result.get("version")
                                   and result.get("version") != __version__
                                   and result.get("message", "").startswith("发现新版本"))
        return result

    def skip_app_update(self, version: str = "") -> dict[str, Any]:
        """Remember that this release is not wanted."""
        tag = str(version or "").strip()
        if not tag:
            return {"success": False, "message": "缺少版本号"}
        versions = [str(item) for item in (self.settings.ui.skipped_update_versions or [])]
        if tag not in versions:
            versions.append(tag)
        self.settings.update_ui(skipped_update_versions=versions[-20:])
        return {"success": True, "message": ""}

    def download_app_update(self, url: str = "", filename: str = "") -> dict[str, Any]:
        """Download one release asset into the temp folder and return its path.

        The bytes never go through the webview: the UI only ever sees the
        path on disk, and only then can it ask to install it.
        """
        from src.core.updater import download_asset

        target_url = str(url or "").strip()
        if not target_url:
            return {"success": False, "message": "缺少下载地址", "path": ""}
        name = str(filename or "").strip() or target_url.rsplit("/", 1)[-1] or "update.bin"
        # 只取文件名部分，别让服务器给的路径跳出临时目录。
        name = Path(name).name
        dest = Path(tempfile.gettempdir()) / "skycmedia-update" / name
        try:
            result = download_asset(
                target_url, str(dest),
                verify=not self.settings.ui.update_skip_ssl_verify,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("update download failed: %s", exc)
            return {"success": False, "message": str(exc), "path": ""}
        return result

    def install_app_update(self, path: str = "") -> dict[str, Any]:
        """Replace the running application with the downloaded artefact."""
        from src.core.updater import install_update

        source = str(path or "").strip()
        if not source:
            return {"success": False, "message": "缺少更新文件", "needs_restart": False}
        try:
            return install_update(source)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("update install failed: %s", exc)
            return {"success": False, "message": str(exc), "needs_restart": False}

    # -- application update (end) ---------------------------------------------

    def open_bili_download_folder(self) -> bool:
        """Open the folder the Bilibili videos are downloaded to.

        Bilibili has a download directory of its own; the NetEase one is
        often a different place, and opening that one would show the user
        the wrong folder.
        """
        path = str(self.settings.bili.download_dir or self._download_dir())
        try:
            system = platform.system()
            if system == "Darwin":
                subprocess.Popen(["open", path])
            elif system == "Windows":
                subprocess.Popen(["explorer", str(Path(path))])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
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
        allowed = ("key", "source", "id", "path", "name", "artists", "artist_list",
                   "album", "cover_url", "duration", "vip", "ext", "size", "has_lyrics")
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

    @staticmethod
    def _is_media_uri(path: str) -> bool:
        """Whether the path is a media library uri rather than a file."""
        return str(path or "").startswith("content://media/external/audio/media/")

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
                raw_path = str((local or {}).get("path") or "")
                if local and self._is_media_uri(raw_path):
                    # A song in the phone's media library: not a file, but
                    # a row in MediaStore, reached through its uri.
                    return {
                        "success": True,
                        "kind": "local",
                        "url": self.media.url_for_content(raw_path),
                        "path": raw_path,
                        "matched_local": track.get("source") == "online",
                        "cover_url": "",
                        "name": local.get("name"),
                        "artists": local.get("artists"),
                        "album": local.get("album"),
                        "duration": local.get("duration") or track.get("duration") or 0,
                        "has_lyrics": bool(local.get("has_lyrics")),
                    }
                path = Path(raw_path)
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

    def resolve_cover_url(self, track: dict[str, Any]) -> dict[str, Any]:
        """Return a loopback URL for the track's cover, cached on disk.

        The webview can not talk to the NetEase CDN directly without tripping
        CORS / ``Referer`` rules, and even when it could every album scroll
        would mean another CDN round-trip.  Going through this endpoint
        proxies the cover once and writes the bytes to the persistent cache.
        """
        track = track or {}
        raw_url = str(track.get("cover_url") or "").strip()
        if not raw_url:
            return {"success": False, "error_key": "player.cover_missing"}
        ext = _ext_from_cover_url(raw_url)
        return {
            "success": True,
            "url": self.media.url_for_remote_cover(raw_url, ext),
            "remote_url": raw_url,
            "ext": ext,
        }

    def cache_stats(self) -> dict[str, Any]:
        return {
            "success": True,
            **self.media.cache_stats(),
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
        """Play counters, and the favourites as the heart buttons must show them.

        The heart is filled when the track is in the local favourites **or**
        in the account's NetEase "liked music".  Those are two different
        lists: the local one survives a session without a login, the cloud
        one holds everything the account liked on any device.  The "liked
        music" playlist is exactly the cloud list, so every song in it has
        to show a filled heart.
        """
        keys = set(self.library.favorite_keys())
        cloud = self._safe("cloud favourites", self._cloud_favorite_keys) or set()
        keys |= cloud
        return {
            "success": True,
            "counts": self.library.play_counts(),
            "stats": self.library.stats(),
            "favorites": sorted(keys),
            "local_favorites": self.library.favorite_keys(),
        }

    def _cloud_favorite_keys(self, fresh: bool = False) -> set[str]:
        """The account's NetEase favourites, as ``online:<song id>`` keys.

        ``fresh=True`` skips the response cache, which matters right after a
        like or an unlike: the cached answer is a minute old and the hearts
        would not follow the click.
        """
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return set()
        ids = self.api.get_liked_song_ids(user.user_id, use_cache=not fresh) or []
        return {f"online:{song_id}" for song_id in ids}

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
        """Toggle a favourite locally and mirror it to NetEase when possible.

        ``cloud`` says what happened on the server, and the values are not
        interchangeable -- the UI needs to tell "this track is not accepted
        by NetEase" apart from "you were too quick" apart from "your session
        expired":

        ``ok``            the change is on the server
        ``login_required`` not signed in; the favourite is local only
        ``unsupported``   NetEase refuses this particular track
        ``rate_limited``  too many requests; retrying later works
        ``failed``        anything else
        """
        key = self._key_of(track)
        if not key:
            return {"success": False, "error_key": "error.unknown"}
        state = self.library.set_favorite(key, bool(favorite), self._meta_of(track))
        cloud: str | None = None
        if track.get("source") != "local" and track.get("id"):
            if not self.login_manager.is_logged_in:
                cloud = "login_required"
            else:
                outcome = self._safe("cloud favourite", self.api.set_song_like,
                                      str(track["id"]), state)
                cloud = outcome if isinstance(outcome, str) and outcome else "failed"
        # The merged list: the local favourites plus the ones NetEase knows.
        # Returning only the local ones would make every cloud favourite
        # lose its heart after a single click.
        #
        # The cloud is read fresh, because the response cache still holds
        # the pre-click answer.  When the cloud refused the change the fresh
        # read keeps the old state -- which is the honest one: the song is
        # still in the account's liked list, and the toast says so.
        keys = set(self.library.favorite_keys())
        keys |= self._safe("cloud favourites", self._cloud_favorite_keys, True) or set()
        return {"success": True, "key": key, "favorite": state, "cloud": cloud,
                "favorites": sorted(keys),
                "local_favorites": self.library.favorite_keys(),
                # The account's liked list changed, so the "liked music"
                # playlist has to be re-read.  The frontend asks for it.
                "liked_dirty": cloud == "ok"}

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
    def get_playlists(self, fresh: bool = False) -> dict[str, Any]:
        """Every playlist of the signed-in account.

        ``fresh=True`` re-reads the list from the server; without it the
        cached list is used, which is what keeps the sidebar quick.
        """
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return {"success": False, "error_key": "player.login_required", "playlists": []}
        playlists = self._safe("account playlists", self.api.get_user_playlists_all,
                               user.user_id, fresh=fresh)
        if playlists is None:
            return {"success": False, "error_key": "error.network", "playlists": []}
        return {
            "success": bool(playlists),
            "error_key": None if playlists else "player.playlist_empty",
            "playlists": [self._playlist_payload(item) for item in playlists],
        }

    def _playlist_payload(self, playlist: dict[str, Any]) -> dict[str, Any]:
        """One playlist, with what the user is allowed to do to it.

        The permissions are decided **here**, on the server, from data the
        account endpoint provides -- not in the frontend from a name or a
        flag the UI happens to have.  The rules:

        * a playlist somebody else created can only be un-subscribed; it
          cannot be renamed, its tracks cannot be changed and it cannot be
          deleted
        * a playlist this account created can be renamed, edited and deleted,
          but not un-subscribed -- un-subscribing your own playlist is not
          what the star button means there
        * the "liked music" playlist (``specialType 5``) is never editable
          and never deletable: its contents are the account's favourites,
          which are managed through the heart buttons
        """
        creator = playlist.get("creator") or {}
        creator_id = str(creator.get("userId") or "")
        user = self.login_manager.user_info
        user_id = str(user.user_id) if user and user.user_id else ""
        special_type = int(playlist.get("specialType") or 0)
        is_liked_music = special_type == 5
        is_own = bool(user_id) and creator_id == user_id
        subscribed = bool(playlist.get("subscribed"))
        return {
            "id": str(playlist.get("id", "")),
            "name": playlist.get("name") or "Unknown",
            "cover_url": playlist.get("coverImgUrl") or playlist.get("coverImgUrlStr") or "",
            "track_count": playlist.get("trackCount") or 0,
            "creator": creator.get("nickname") or "",
            "creator_id": creator_id,
            "special_type": special_type,
            "subscribed": subscribed,
            "description": playlist.get("description") or "",
            "is_own": is_own,
            "is_liked_music": is_liked_music,
            # What the UI may offer for this playlist.
            "can_unsubscribe": subscribed and not is_own,
            "can_subscribe": not subscribed and not is_own,
            "can_rename": is_own and not is_liked_music,
            "can_edit_tracks": is_own and not is_liked_music,
            "can_delete": is_own and not is_liked_music,
        }

    def get_playlist_tracks(self, playlist_id: str, fresh: bool = False) -> dict[str, Any]:
        """Tracks of one playlist, in playlist order (does not touch the download tab).

        ``fresh=True`` skips every cached copy of this playlist -- it is what
        the refresh button asks for, so that a playlist edited elsewhere
        shows its new contents right away.
        """
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id.isdigit():
            return {"success": False, "error_key": "playlist.invalid_input", "tracks": []}
        info = self._safe("playlist info", self.api.get_playlist_info, playlist_id, fresh=fresh)
        songs = self._safe("playlist songs", self.api.get_playlist_songs, playlist_id,
                           fresh=fresh) or []
        if not songs:
            return {"success": False, "error_key": "playlist.fetch_failed", "tracks": []}
        return {
            "success": True,
            "playlist": self._playlist_payload(info or {"id": playlist_id}),
            "tracks": [self._track_payload(index, song) for index, song in enumerate(songs)],
        }

    def get_liked_tracks(self, fresh: bool = False) -> dict[str, Any]:
        """The account's "liked" list (needs a login).

        ``fresh=True`` skips the cached id list, so a song that was just
        liked appears at once instead of after the cache expires.
        """
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return {"success": False, "error_key": "player.login_required", "tracks": []}
        ids = self._safe("liked ids", self.api.get_liked_song_ids, user.user_id,
                         use_cache=not fresh)
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
        # The follow state lives on its own route, so it is a separate
        # read.  Without a login the account follows nobody and the
        # endpoint still answers.
        follow = self._safe("artist follow state",
                            self.api.get_artist_follow_state, artist_id) or {}
        return {
            "success": True,
            "artist": self._artist_payload(artist),
            "hot_songs": [self._track_payload(index, song) for index, song in enumerate(artist.get("hotSongs") or [])],
            "albums": [self._album_payload_mini(album) for album in (artist.get("albums") or [])],
            "followed": bool(follow.get("followed")),
            "fans_count": int(follow.get("fans_count") or 0),
            "follow_days": int(follow.get("follow_days") or 0),
        }

    def follow_artist(self, artist_id: str, follow: bool = True) -> dict[str, Any]:
        """Follow or unfollow an artist.

        The two directions fail for different reasons, so they get
        different messages: a refusal because the request came too fast is
        not the user's fault, and a dead session is not either.
        """
        artist_id = str(artist_id or "").strip()
        if not artist_id.isdigit():
            return {"success": False, "error_key": "player.invalid_artist_id"}
        status = self._safe("follow artist", self.api.set_artist_follow,
                            artist_id, bool(follow))
        if status == "ok":
            return {"success": True, "followed": bool(follow)}
        if status == "rate_limited":
            return {"success": False, "error_key": "player.artist_follow_busy"}
        if status == "not_logged_in":
            return {"success": False, "error_key": "player.login_required"}
        key = "player.artist_follow_failed" if follow else "player.artist_unfollow_failed"
        return {"success": False, "error_key": key}

    def get_followed_artists(self, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """The artists the account follows."""
        result = self._safe("followed artists", self.api.get_followed_artists,
                            limit, offset)
        if result is None:
            return {"success": False, "error_key": "error.network", "artists": []}
        code = result.get("code")
        if code in LOGIN_REQUIRED_CODES:
            return {"success": False, "error_key": "player.login_required", "artists": []}
        return {
            "success": True,
            # Every artist in this list is followed by definition -- the
            # sublist answer does not repeat a ``followed`` flag, so the row
            # would offer to "follow" an artist the user already follows.
            "artists": [{**self._artist_payload(artist), "followed": True}
                        for artist in (result.get("artists") or [])],
            "count": int(result.get("count") or 0),
            "has_more": bool(result.get("has_more")),
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
            # The server says whether the account already follows this
            # artist, so the button starts in the right state.
            "followed": bool(artist.get("followed")),
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

    # -- bilibili source -------------------------------------------------------
    def search_bilibili_tracks(self, keyword: str, category: str = "all",
                               order: str = "default", page: int = 1,
                               page_size: int = 30) -> dict[str, Any]:
        """Search the Bilibili music source by keyword.

        ``category`` is a partition name (``music``) or a partition id
        (``28``); ``order`` is a friendly sort name (``play``, ``date``).
        The answer carries the items of the source and the tracks in the
        shape the player list expects.
        """
        result = self.bilibili.search(keyword, category=category, order=order,
                                      page=page, page_size=page_size)
        items = result.get("items") or []
        result["tracks"] = [self._bilibili_track_payload(item) for item in items]
        return result

    def get_bilibili_partitions(self) -> dict[str, Any]:
        """The Bilibili partitions, with the id and the name of each one."""
        return self.bilibili.partitions()

    def search_bilibili_pgc(self, keyword: str = "", page: int = 1) -> dict[str, Any]:
        """Search the Bilibili PGC catalogue (番剧 / 国创 / 影视)."""
        return self.bilibili.search_pgc(keyword, page=page)

    def get_bilibili_popular(self, rid: int = 28, page: int = 1,
                              page_size: int = 30) -> dict[str, Any]:
        """The hot list of a Bilibili partition."""
        result = self.bilibili.popular(rid=rid, page=page, page_size=page_size)
        items = result.get("items") or []
        result["tracks"] = [self._bilibili_track_payload(item) for item in items]
        return result

    def get_bilibili_recommend(self, page_size: int = 12, fresh_index: int = 1,
                               scroll: bool = False) -> dict[str, Any]:
        """The Bilibili home feed (personalised when the account is signed in).

        ``scroll`` asks for the next page of the feed rather than the home
        page itself, which is what the "load more" button needs.
        """
        result = self.bilibili.recommend(page_size=page_size,
                                         fresh_index=fresh_index,
                                         scroll=scroll)
        items = result.get("items") or []
        result["tracks"] = [self._bilibili_track_payload(item) for item in items]
        return result

    def get_bili_subscribed_collections(self, page: int = 1,
                                        page_size: int = 20) -> dict[str, Any]:
        """The collections the signed-in account follows.

        ``page`` walks the list: one page holds 20 and an account can have
        dozens of them.
        """
        return self.bilibili.subscribed_collections(page=page,
                                                    page_size=page_size)

    def get_bili_bangumi(self) -> dict[str, Any]:
        """The anime the signed-in account follows."""
        return self.bilibili.bangumi()

    def get_bili_bangumi_streams(self, season_id: int = 0, episode_id: int = 0,
                                 quality: int = 0) -> dict[str, Any]:
        """Playable streams of one bangumi episode, same shape as a video."""
        return self.bilibili.bangumi_streams(season_id=season_id,
                                             episode_id=episode_id,
                                             quality=quality)

    def get_bili_dynamics(self, mid: str = "", page: int = 1) -> dict[str, Any]:
        """The posts of one account."""
        return self.bilibili.dynamics(mid=mid, page=page)

    # ------------------------------------------------------------ Bangumi
    def search_bangumi(self, keyword: str, limit: int = 20,
                       subject_type: int = 2) -> dict[str, Any]:
        """Search the Bangumi subject catalogue."""
        return self.bangumi.search(keyword, limit=limit,
                                   subject_type=subject_type)

    def get_bangumi_subject(self, subject_id: int | str,
                            refresh: bool = False) -> dict[str, Any]:
        """One subject's details, with its local favourite state."""
        return self.bangumi.subject(subject_id, refresh=refresh)

    def get_bangumi_episodes(self, subject_id: int | str,
                             episode_type: int | None = None,
                             refresh: bool = False) -> dict[str, Any]:
        """The episode list of one subject."""
        return self.bangumi.episodes(subject_id, episode_type=episode_type,
                                     refresh=refresh)

    def get_bangumi_resource_links(self, subject_id: int | str,
                                   episode_id: int | str | None = None
                                   ) -> dict[str, Any]:
        """Download links rendered from the user's resource-site templates."""
        return self.bangumi.resource_links(subject_id, episode_id=episode_id)

    def get_bangumi_favorites(self) -> dict[str, Any]:
        """The local favourites.  Local only -- nothing was ever uploaded."""
        return self.bangumi.favorites()

    def set_bangumi_favorite(self, subject_id: int | str,
                             favorite: bool = True) -> dict[str, Any]:
        """Add or drop a local favourite."""
        return self.bangumi.set_favorite(subject_id, favorite)

    def toggle_bangumi_favorite(self, subject_id: int | str) -> dict[str, Any]:
        """Flip the local favourite state."""
        return self.bangumi.toggle_favorite(subject_id)

    def get_bangumi_history(self) -> dict[str, Any]:
        """The local watch history."""
        return self.bangumi.history()

    def record_bangumi_history(self, subject_id: int | str,
                               episode_id: int | str) -> dict[str, Any]:
        """Note that one episode was watched.  Local only."""
        return self.bangumi.record_history(subject_id, episode_id)

    def clear_bangumi_history(self) -> dict[str, Any]:
        """Wipe the local watch history.  The favourites stay."""
        return self.bangumi.clear_history()

    # -- bangumi rules (Kazumi) -------------------------------------------------
    def get_bangumi_source_settings(self) -> dict[str, Any]:
        """规则播放的设置。"""
        return self.bangumi.source_settings()

    def save_bangumi_source_settings(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.bangumi.save_source_settings(values or {})

    def get_bangumi_rules(self, repo: str = "", refresh: bool = False) -> dict[str, Any]:
        """规则仓库里的规则列表。"""
        return self.bangumi.list_rules(repo, refresh=refresh)

    def get_bangumi_rule_detail(self, name: str, repo: str = "") -> dict[str, Any]:
        return self.bangumi.rule_detail(name, repo=repo)

    def search_bangumi_by_rule(self, keyword: str, rule_name: str = "",
                               repo: str = "") -> dict[str, Any]:
        """用规则搜番。"""
        return self.bangumi.rule_search(keyword, rule_name=rule_name, repo=repo)

    def get_bangumi_rule_chapters(self, url: str, rule_name: str = "",
                                  repo: str = "") -> dict[str, Any]:
        return self.bangumi.rule_chapters(url, rule_name=rule_name, repo=repo)

    def get_bangumi_rule_streams(self, url: str, rule_name: str = "",
                                 repo: str = "") -> dict[str, Any]:
        """从剧集页取播放地址（已转成本地代理地址）。"""
        return self.bangumi.rule_streams(url, rule_name=rule_name, repo=repo)

    def get_bangumi_mirrors(self, refresh: bool = False) -> dict[str, Any]:
        """The mirror list, in the order the user put them."""
        return self.bangumi.mirrors(refresh=refresh)

    def get_bangumi_built_in_mirrors(self) -> dict[str, Any]:
        """The mirrors that ship with the app."""
        return self.bangumi.built_in_mirrors()

    def get_bangumi_download_dir(self) -> dict[str, Any]:
        """Where a Bangumi download would land."""
        return self.bangumi.download_dir()

    def download_bangumi_url(self, url: str, filename: str = "") -> dict[str, Any]:
        """Save one resource link to disk."""
        return self.bangumi.download_url(url, filename)

    def cancel_bangumi_download(self) -> dict[str, Any]:
        """Cancel the download that is running."""
        return self.bangumi.cancel_download()

    def export_bangumi_subject(self, subject_id: int | str,
                               filename: str = "") -> dict[str, Any]:
        """Write the subject and its episodes to a txt file."""
        return self.bangumi.export_subject_text(subject_id, filename)

    def enqueue_bangumi_downloads(self, urls: list[dict[str, Any]],
                                  start: bool = True) -> dict[str, Any]:
        """Put a batch of resource links into the download queue."""
        return self.bangumi.enqueue_download(urls, start=start)

    def get_bangumi_download_queue(self) -> dict[str, Any]:
        """The state of the download queue."""
        return self.bangumi.download_queue_status()

    def clear_bangumi_download_queue(self) -> dict[str, Any]:
        """Drop the finished tasks from the queue."""
        return self.bangumi.clear_download_queue()

    def cancel_bangumi_download_queue(self) -> dict[str, Any]:
        """Stop the whole queue, including the download in flight."""
        return self.bangumi.cancel_download_queue()

    # ------------------------------------------------------ 本地视频（实验性）
    def add_local_video_folder(self) -> dict[str, Any]:
        """Ask for a video folder and remember it."""
        if self._window is None:
            return {"success": False, "message": "窗口还没准备好", "dirs": []}
        try:
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG,
                                                     directory=str(Path.home()))
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("video folder dialog failed: %s", exc)
            return {"success": False, "message": str(exc), "dirs": []}
        if not result:
            # Cancelled: not a failure, the list simply did not change.
            return {"success": True, "cancelled": True,
                    "dirs": list(self.settings.playback.local_video_dirs)}
        chosen = str(Path(result[0]).expanduser().resolve())
        dirs = list(self.settings.playback.local_video_dirs)
        if chosen not in dirs:
            dirs.append(chosen)
            self.settings.playback.local_video_dirs = dirs
            self.settings.save()
        self._allow_local_video_dirs(dirs)
        return {"success": True, "cancelled": False, "dirs": dirs, "added": chosen}

    def remove_local_video_folder(self, path: str = "") -> dict[str, Any]:
        """Forget a video folder."""
        target = str(path or "").strip()
        dirs = [item for item in self.settings.playback.local_video_dirs if item != target]
        self.settings.playback.local_video_dirs = dirs
        self.settings.save()
        return {"success": True, "dirs": dirs}

    def get_local_video_library(self) -> dict[str, Any]:
        """Everything the remembered video folders hold.

        The media server is told about the folders first: it refuses to hand
        out a file outside its allowed roots, and a video the app cannot
        fetch is a video the player window cannot play.
        """
        from src.core import localvideo

        dirs = list(self.settings.playback.local_video_dirs)
        self._allow_local_video_dirs(dirs)
        try:
            answer = localvideo.library(dirs)
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("local video scan failed: %s", exc)
            return {"success": False, "items": [], "count": 0,
                    "dirs": dirs, "errors": [str(exc)]}
        items = []
        for item in answer.get("items") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            items.append({
                "path": path,
                "name": str(item.get("name") or ""),
                "size": int(item.get("size") or 0),
                "mtime": item.get("mtime") or 0,
                "duration": float(item.get("duration") or 0),
                "url": self._local_video_url(path),
            })
        return {"success": True, "items": items, "count": len(items),
                "dirs": dirs, "errors": list(answer.get("errors") or [])}

    def pick_local_video_subtitle(self) -> dict[str, Any]:
        """Ask for a subtitle file for the local video that is open."""
        if self._window is None:
            return {"success": False, "message": "窗口还没准备好"}
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Subtitle (*.srt;*.ass;*.vtt)", "All files (*.*)"))
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("subtitle dialog failed: %s", exc)
            return {"success": False, "message": str(exc)}
        if not result:
            return {"success": True, "cancelled": True}
        chosen = str(Path(result[0]).expanduser().resolve())
        return self._subtitle_answer(chosen, "")

    def get_local_video_subtitle(self, video: str = "", extra: str = "") -> dict[str, Any]:
        """The subtitle of one video, as WebVTT text.

        The text is handed over instead of a file url: the player window is
        served by the media server, and a subtitle read out of a file://
        path would be blocked there.
        """
        return self._subtitle_answer(extra, video)

    def open_local_video(self, path: str = "", subtitle: str = "") -> dict[str, Any]:
        """Open a local video in a window of its own."""
        target = str(path or "").strip()
        if not target:
            return {"success": False, "message": "缺少视频文件"}
        # The media server only serves files under its allowed roots.
        self._allow_local_video_dirs([str(Path(target).parent),
                                      *self.settings.playback.local_video_dirs])
        video_url = self._local_video_url(target)
        if not video_url:
            return {"success": False, "message": "这个文件读不了"}

        query: dict[str, str] = {"video": video_url,
                                 "title": Path(target).name,
                                 "name": Path(target).name}
        if subtitle:
            answer = self._subtitle_answer(subtitle, target)
            if answer.get("success") and answer.get("text"):
                query["subtitleText"] = str(answer["text"])
        # The rest of the folder goes along, so the window can offer the next
        # episode without asking the backend for it again.
        siblings = self._sibling_videos(target)
        if siblings:
            query["files"] = json.dumps(siblings, ensure_ascii=False)

        if self._webview_module is None:
            return {"success": False, "message": "", "url": self._local_video_url_page(query)}
        url = self._local_video_url_page(query)
        try:
            window = self._webview_module.create_window(
                title=Path(target).name or "SkycMedia",
                url=url,
                js_api=self,
                width=1180,
                height=780,
                min_size=(760, 520),
                resizable=True,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("could not open the local video window: %s", exc)
            return {"success": False, "message": str(exc)}
        self._local_video_windows.append(window)

        def forget() -> None:
            try:
                self._local_video_windows.remove(window)
            except ValueError:
                pass

        try:
            window.events.closed += forget
        except Exception:  # noqa: BLE001 - a missing event is not fatal
            pass
        return {"success": True, "opened": True, "message": "", "url": url}

    # ------------------------------------------------------------ 本地视频助手
    def _allow_local_video_dirs(self, dirs: list[str]) -> None:
        """Let the media server read the video folders.

        ``set_allowed_roots`` replaces the whole list, so the roots that were
        already there are carried over; dropping them would take the covers
        and the local music away with it.
        """
        media = getattr(self, "media", None)
        if media is None:
            return
        roots = [str(item) for item in getattr(media, "allowed_roots", []) or []]
        for item in dirs:
            text = str(item or "").strip()
            if text and text not in roots:
                roots.append(text)
        try:
            media.set_allowed_roots(roots)
        except Exception as exc:  # noqa: BLE001 - never break the window
            self.logger.warning("could not widen the media roots: %s", exc)

    def _local_video_url(self, path: str) -> str:
        """A loopback url for one file, or an empty string."""
        media = getattr(self, "media", None)
        if media is None or not str(path or "").strip():
            return ""
        try:
            return media.url_for_local(path) or ""
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("could not url the local video: %s", exc)
            return ""

    def _local_video_url_page(self, query: dict[str, str]) -> str:
        """The address of the local video page."""
        from src.resources import resource_path
        html_path = resource_path("src", "gui", "assets", "local-video.html")
        media = getattr(self, "media", None)
        encoded = urlencode(query)
        if media is not None and getattr(media, "running", False):
            media.set_ui_root(html_path.parent)
            return media.ui_url("local-video.html", query=encoded)
        return f"{html_path.as_uri()}?{encoded}"

    def _sibling_videos(self, path: str) -> list[dict[str, Any]]:
        """The other videos next to this one, for the list in the window."""
        from src.core import localvideo

        folder = Path(path).parent
        try:
            answer = localvideo.scan_folder(str(folder))
        except Exception:  # noqa: BLE001 - the list is a nicety
            return []
        rows = []
        for item in answer.get("items") or []:
            if not isinstance(item, dict):
                continue
            file_path = str(item.get("path") or "")
            url = self._local_video_url(file_path)
            if not url:
                continue
            rows.append({
                "path": file_path,
                "name": str(item.get("name") or ""),
                "url": url,
                "duration": float(item.get("duration") or 0),
            })
        return rows[:200]

    def _subtitle_answer(self, subtitle: str, video: str) -> dict[str, Any]:
        """Read one subtitle file and hand back WebVTT text."""
        from src.core import localvideo

        chosen = str(subtitle or "").strip()
        try:
            if chosen:
                answer = localvideo.subtitle_for(video or chosen, chosen)
            elif video:
                answer = localvideo.subtitle_for(video)
            else:
                return {"success": False, "message": "缺少字幕文件", "text": ""}
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("subtitle read failed: %s", exc)
            return {"success": False, "message": str(exc), "text": ""}
        text = str(answer.get("text") or "")
        if not text:
            return {"success": False, "message": "这个字幕文件读不出内容", "text": ""}
        return {"success": True, "message": "", "text": text,
                "path": str(answer.get("path") or ""),
                "kind": str(answer.get("kind") or "")}


    def bili_report_playback(self, bvid: str = "", cid: int = 0,
                             played_time: int = 0, video_duration: int = 0,
                             quality: int = 0, start_ts: int = 0,
                             session: str = "", play_type: int = 0) -> dict[str, Any]:
        """Report what is playing to B站 (no-op unless the setting is on)."""
        return self.bilibili.report_playback(
            bvid, cid=cid, played_time=played_time,
            video_duration=video_duration, quality=quality,
            start_ts=start_ts, session=session, play_type=play_type)

    def get_bilibili_audio_url(self, bvid: str, quality: int = 0) -> dict[str, Any]:
        """Resolve the audio stream of one Bilibili video."""
        return self.bilibili.audio_url(bvid, quality=quality)

    @staticmethod
    def _bilibili_track_payload(item: dict[str, Any]) -> dict[str, Any]:
        """One Bilibili item, in the shape the player list renders."""
        from src.gui.bilibili_bridge import track_payload

        return track_payload(item)

    # -- bilibili account ------------------------------------------------------
    def get_bilibili_account(self, refresh: bool = False) -> dict[str, Any]:
        """The account information of the signed-in Bilibili user."""
        return self.bilibili.account(refresh=refresh)

    def bili_login_qrcode(self) -> dict[str, Any]:
        """Ask Bilibili for a login QR code."""
        return self.bilibili.login_qrcode()

    def bili_login_poll(self) -> dict[str, Any]:
        """Check the state of the login QR code."""
        return self.bilibili.login_poll()

    def bili_login_cookie(self, cookie: str) -> dict[str, Any]:
        """Sign in to Bilibili with a cookie from the browser."""
        return self.bilibili.login_cookie(cookie)

    def bili_login_password(self, username: str, password: str,
                            captcha_key: str = "", captcha: str = "",
                            challenge: str = "", validate: str = "",
                            seccode: str = "") -> dict[str, Any]:
        """Sign in to Bilibili with a user name and a password."""
        return self.bilibili.login_password(
            username, password, captcha_key=captcha_key, captcha=captcha,
            challenge=challenge, validate=validate, seccode=seccode,
        )

    def bili_captcha(self) -> dict[str, Any]:
        """The verification challenge of the password and SMS flows."""
        return self.bilibili.captcha()

    def bili_send_sms_code(self, tel: str, cid: str = "86") -> dict[str, Any]:
        """Send a verification code to a phone number."""
        return self.bilibili.send_sms_code(tel, cid=cid)

    def bili_login_sms(self, tel: str, code: str, cid: str = "86",
                       captcha_key: str = "") -> dict[str, Any]:
        """Sign in to Bilibili with the code sent to a phone number."""
        return self.bilibili.login_sms(tel, code, cid=cid,
                                        captcha_key=captcha_key)

    def bili_register_send_code(self, tel: str, cid: str = "86") -> dict[str, Any]:
        """Send the code needed to register a new Bilibili account."""
        return self.bilibili.register_send_code(tel, cid=cid)

    def bili_register(self, username: str, password: str, tel: str, code: str,
                      cid: str = "86") -> dict[str, Any]:
        """Create a new Bilibili account and sign in with it."""
        return self.bilibili.register(username, password, tel, code, cid=cid)

    def bili_logout(self) -> dict[str, Any]:
        """Sign out of Bilibili."""
        return self.bilibili.logout()

    # -- bilibili favourites ----------------------------------------------------
    def get_bilibili_favorites(self) -> dict[str, Any]:
        """The favourite folders of the signed-in Bilibili user."""
        return self.bilibili.favorites()

    def get_bilibili_favorite_videos(self, folder_id: str = "", page: int = 1,
                                     page_size: int = 30) -> dict[str, Any]:
        """The videos inside one favourite folder."""
        return self.bilibili.favorite_videos(folder_id, page=page,
                                              page_size=page_size)

    # -- bilibili playback / download -------------------------------------------
    def get_bilibili_video_streams(self, bvid: str, quality: int = 0) -> dict[str, Any]:
        """The playable streams of one Bilibili video."""
        return self.bilibili.video_streams(bvid, quality=quality)

    def get_bilibili_video_progress(self, bvid: str) -> dict[str, Any]:
        """The watch progress of one Bilibili video from the user's history.

        Returns {progress: float (0-100), duration: int (seconds)} or zeros if
        the video is not in the history or the user is not logged in.
        """
        bvid = str(bvid or "").strip()
        if not bvid or not self.bilibili.client.is_logged_in:
            return {"success": False, "progress": 0, "duration": 0}
        try:
            # Fetch the first few pages of history; the video is usually recent.
            for page in range(1, 4):
                hist = self.bilibili.history(page=page, page_size=30)
                if not hist.get("success"):
                    break
                for item in hist.get("items", []):
                    if item.get("bvid") == bvid:
                        return {
                            "success": True,
                            "progress": float(item.get("progress", 0)),
                            "duration": int(item.get("duration", 0)),
                        }
                if not hist.get("has_more"):
                    break
            return {"success": True, "progress": 0, "duration": 0}
        except Exception as exc:  # noqa: BLE001 - history is best-effort
            self.logger.warning("get_bilibili_video_progress failed: %s", exc)
            return {"success": False, "progress": 0, "duration": 0}

    def open_bili_player_window(self, bvid: str = "", page: int = 1,
                                quality: int = 0) -> dict[str, Any]:
        """Open the Bilibili player in a window of its own.

        The main window stays usable while the video runs, and the player
        window carries the comments and the danmaku next to the picture.
        The video that is already open is brought to the front instead of
        being opened twice.
        """
        video_id = str(bvid or "").strip()
        if not video_id:
            return {"success": False, "message": "缺少视频编号"}

        existing = self._bili_player_windows.get(video_id)
        if existing is not None:
            try:
                existing.restore()
                return {"success": True, "opened": False, "message": ""}
            except Exception:  # noqa: BLE001 - the window may be gone already
                self._bili_player_windows.pop(video_id, None)

        url = self._bili_player_url(video_id, page, quality)
        if self._webview_module is None:
            # Dev-only path: a plain browser cannot open a native window.
            # The page is opened as a tab and says that it needs the app.
            self._call_js("openBiliPlayerWindowFallback", {"url": url})
            return {"success": False, "message": "", "url": url}

        try:
            window = self._webview_module.create_window(
                title="SkycMedia",
                url=url,
                js_api=self,
                width=1180,
                height=780,
                min_size=(760, 520),
                resizable=True,
            )
        except Exception as exc:  # noqa: BLE001 - the UI must never see it
            self.logger.warning("could not open the player window: %s", exc)
            return {"success": False, "message": str(exc)}

        self._bili_player_windows[video_id] = window
        try:
            window.events.closed += self._bili_player_closed(video_id)
        except Exception:  # noqa: BLE001 - a missing event is not fatal
            pass
        self._ensure_window_bridge(window)
        return {"success": True, "opened": True, "message": "", "url": url}

    def _ensure_window_bridge(self, window: Any) -> None:
        """Make sure a window created at run time got its JS bridge.

        pywebview injects ``window.pywebview`` when the page finishes loading.
        For a window opened from a JS call that step sometimes never runs: the
        page then has no API at all, its own scripts give up after a few
        seconds, and the user is left looking at an empty window -- the
        separate video window came up black for exactly this reason.

        So: wait for the injection, and if it does not come, do it here.  The
        injection goes through ``run_js``, which does not wait for the loaded
        event, so this cannot deadlock on the event it is about to set.
        """
        try:
            if window.events.loaded.wait(6):
                return
        except Exception:  # noqa: BLE001 - no events object at all
            return
        try:
            from webview.util import inject_pywebview

            platform = "cocoa" if sys.platform == "darwin" else None
            if platform is None:
                # The other backends are not exercised here; the page falls
                # back to its own timeout, which stays long enough to cover
                # a slow injection.
                self.logger.warning("no manual bridge injection for %s", sys.platform)
                return
            self.logger.warning("window bridge was not injected; injecting by hand")
            inject_pywebview(platform, window)
        except Exception as exc:  # noqa: BLE001 - never break the window
            self.logger.warning("could not inject the bridge by hand: %s", exc)

    def close_bili_player_window(self, bvid: str = "") -> dict[str, Any]:
        """Close the player window of one video (all of them without a id)."""
        video_id = str(bvid or "").strip()
        if video_id:
            targets = [self._bili_player_windows.pop(video_id, None)]
        else:
            targets = list(self._bili_player_windows.values())
            self._bili_player_windows.clear()
        for window in targets:
            if window is None:
                continue
            try:
                window.destroy()
            except Exception:  # noqa: BLE001 - it may be closed already
                pass
        return {"success": True}

    def _bili_player_closed(self, bvid: str) -> Callable[[], None]:
        """The handler that forgets a player window when it is closed."""

        def handler() -> None:
            self._bili_player_windows.pop(bvid, None)

        return handler

    def _bili_player_url(self, bvid: str, page: int = 1, quality: int = 0) -> str:
        """The address of the player page of one video."""
        query = urlencode({
            "bvid": bvid,
            "page": max(1, int(page or 1)),
            "quality": int(quality or 0),
        })
        from src.resources import resource_path
        html_path = resource_path("src", "gui", "assets", "bili-player.html")
        media = getattr(self, "media", None)
        if media is not None and getattr(media, "running", False):
            media.set_ui_root(html_path.parent)
            return media.ui_url("bili-player.html", query=query)
        return f"{html_path.as_uri()}?{query}"

    def download_bilibili_tracks(self, tracks: list[dict[str, Any]] | None = None,
                                 options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Download the video and the sound of every track of the list."""
        return self.bilibili.download(
            tracks, options,
            progress=lambda payload: self._call_js("onBiliDownloadProgress", payload),
        )


    # -- bilibili interactions -------------------------------------------------
    def get_bilibili_video_stat(self, bvid: str) -> dict[str, Any]:
        """The counters of one Bilibili video."""
        return self.bilibili.video_stat(bvid)

    def bili_like_video(self, bvid: str, like: bool = True) -> dict[str, Any]:
        """Like (or unlike) one Bilibili video."""
        return self.bilibili.like_video(bvid, like=like)

    def bili_coin_video(self, bvid: str, count: int = 1,
                        also_like: bool = False) -> dict[str, Any]:
        """Throw coins at a Bilibili video."""
        return self.bilibili.coin_video(bvid, count=count, also_like=also_like)

    def get_bilibili_coin_balance(self) -> dict[str, Any]:
        """The coin balance of the signed-in Bilibili account."""
        return self.bilibili.coin_balance()

    def get_bilibili_coins_given(self, bvid: str = "") -> dict[str, Any]:
        """How many coins this account already gave one video (0-2)."""
        return self.bilibili.coins_given(bvid)

    def bili_favorite_video(self, bvid: str, folder_id: str = "",
                            remove: bool = False) -> dict[str, Any]:
        """Put a Bilibili video into a favourite folder."""
        return self.bilibili.favorite_video(bvid, folder_id=folder_id,
                                             remove=remove)

    def get_bilibili_favorite_states(self, bvid: str = "") -> dict[str, Any]:
        """The favourite folders, each flagged with whether it holds this video."""
        return self.bilibili.favorite_folders_state(bvid)

    def bili_favorite_video_multi(self, bvid: str,
                                  add_folder_ids: list[str] | None = None,
                                  remove_folder_ids: list[str] | None = None) -> dict[str, Any]:
        """Put one video into several folders (and out of others) at once."""
        return self.bilibili.favorite_video_multi(bvid, add_folder_ids,
                                                  remove_folder_ids)

    def bili_favorite_move(self, src_folder_id: str, target_folder_id: str,
                           aids: list[int] | None = None,
                           copy: bool = False) -> dict[str, Any]:
        """Move (or copy) videos from one favourite folder into another."""
        return self.bilibili.favorite_move(src_folder_id, target_folder_id,
                                           aids, copy)

    def bili_favorite_batch_delete(self, folder_id: str,
                                   aids: list[int] | None = None) -> dict[str, Any]:
        """Take several videos out of one favourite folder at once."""
        return self.bilibili.favorite_batch_delete(folder_id, aids)

    def get_bili_collection_videos(self, season_id: str = "", mid: str = "",
                                   page: int = 1) -> dict[str, Any]:
        """One page of the videos inside a UGC collection (合集)."""
        return self.bilibili.collection_videos(season_id, mid, page)

    def get_bili_bangumi_season(self, season_id: str = "") -> dict[str, Any]:
        """One bangumi season with its episode list (选集)."""
        return self.bilibili.bangumi_season(season_id)

    def bili_favorite_folder(self, folder_id: str,
                             remove: bool = False) -> dict[str, Any]:
        """Favourite somebody else's Bilibili folder."""
        return self.bilibili.favorite_folder(folder_id, remove=remove)

    # -- bilibili comments -----------------------------------------------------
    def get_bilibili_comments(self, bvid: str, page: int = 1,
                              sort: str = "hot") -> dict[str, Any]:
        """The comments of one Bilibili video."""
        return self.bilibili.comments(bvid, page=page, sort=sort)

    def bili_post_comment(self, bvid: str, content: str,
                          parent_rpid: int = 0, root_rpid: int = 0) -> dict[str, Any]:
        """Write a Bilibili comment or a reply."""
        return self.bilibili.post_comment(bvid, content, parent_rpid=parent_rpid, root_rpid=root_rpid)

    def bili_like_comment(self, rpid: str, like: bool = True) -> dict[str, Any]:
        """Like (or unlike) a Bilibili comment."""
        return self.bilibili.like_comment(rpid, like=like)

    def get_bilibili_comment_replies(self, bvid: str, root_rpid: str,
                                    page: int = 1) -> dict[str, Any]:
        """Load sub-replies of a Bilibili comment (楼中楼)."""
        return self.bilibili.get_comment_replies(bvid, root_rpid, page=page)

    # -- bilibili danmaku ------------------------------------------------------
    def get_bilibili_danmaku(self, bvid: str, cid: int = 0) -> dict[str, Any]:
        """The danmaku of one Bilibili video."""
        return self.bilibili.danmaku(bvid, cid=cid)

    def bili_send_danmaku(self, bvid: str, text: str, cid: int = 0,
                          color: int = 16777215, size: int = 25,
                          mode: int = 1) -> dict[str, Any]:
        """Send one Bilibili danmaku."""
        return self.bilibili.send_danmaku(bvid, text, cid=cid, color=color,
                                           size=size, mode=mode)

    # -- bilibili history / space ----------------------------------------------
    def get_bilibili_history(self, page: int = 1,
                             page_size: int = 30) -> dict[str, Any]:
        """What the signed-in Bilibili account watched."""
        return self.bilibili.history(page=page, page_size=page_size)

    def get_bilibili_space(self, mid: str) -> dict[str, Any]:
        """The profile of a Bilibili uploader."""
        return self.bilibili.space(mid)

    def get_bilibili_space_videos(self, mid: str, page: int = 1,
                                  page_size: int = 30) -> dict[str, Any]:
        """The videos a Bilibili uploader published."""
        return self.bilibili.space_videos(mid, page=page, page_size=page_size)

    def bili_follow_user(self, mid: str, follow: bool = True) -> dict[str, Any]:
        """Follow (or unfollow) a Bilibili uploader."""
        return self.bilibili.follow_user(mid, follow=follow)

    def get_bilibili_follow_state(self, mid: str) -> dict[str, Any]:
        """Whether the account follows this Bilibili uploader."""
        return self.bilibili.follow_state(mid)

    # -- bilibili folder management --------------------------------------------
    def bili_create_favorite_folder(self, title: str, intro: str = "",
                                   privacy: int = 0) -> dict[str, Any]:
        """Create a new Bilibili favourite folder."""
        return self.bilibili.create_favorite_folder(title, intro=intro, privacy=privacy)

    def bili_edit_favorite_folder(self, folder_id: str, title: str = "",
                                 intro: str = "", privacy: int | None = None) -> dict[str, Any]:
        """Rename a Bilibili folder, change its description or privacy.

        ``privacy`` of ``None`` means "leave it as it is".
        """
        return self.bilibili.edit_favorite_folder(folder_id, title=title, intro=intro, privacy=privacy)

    def bili_delete_favorite_folder(self, folder_id: str) -> dict[str, Any]:
        """Delete a Bilibili favourite folder."""
        return self.bilibili.delete_favorite_folder(folder_id)

    # -- bilibili follow / followers -------------------------------------------
    def get_bilibili_followings(self, mid: str = "", page: int = 1,
                               page_size: int = 50) -> dict[str, Any]:
        """Get the list of users the Bilibili account follows (关注列表)."""
        return self.bilibili.get_followings(mid, page=page, page_size=page_size)

    def get_bilibili_followers(self, mid: str = "", page: int = 1,
                              page_size: int = 50) -> dict[str, Any]:
        """Get the list of users following the Bilibili account (粉丝列表)."""
        return self.bilibili.get_followers(mid, page=page, page_size=page_size)

    # -- bilibili private messages ----------------------------------------------
    def get_bilibili_messages(self, size: int = 20) -> dict[str, Any]:
        """The Bilibili private-message conversations."""
        return self.bilibili.messages(size=size)

    def get_bilibili_message_history(self, session_id: str, talker_id: str,
                                     size: int = 20) -> dict[str, Any]:
        """The messages of one Bilibili conversation."""
        return self.bilibili.message_history(session_id, talker_id, size=size)

    def bili_send_message(self, talker_id: str, text: str,
                          session_id: str = "") -> dict[str, Any]:
        """Send one Bilibili private message."""
        return self.bilibili.send_message(talker_id, text, session_id=session_id)

    # -- bilibili uploading -----------------------------------------------------
    def get_bilibili_upload_categories(self) -> dict[str, Any]:
        """The categories a Bilibili video can be posted to."""
        return self.bilibili.upload_categories()

    def bili_upload_video(self, video_path: str, title: str, tid: int,
                          desc: str = "", tags: str = "",
                          cover_path: str = "") -> dict[str, Any]:
        """Post a video to Bilibili."""
        return self.bilibili.upload_video(video_path, title, tid, desc=desc,
                                           tags=tags, cover_path=cover_path)

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

    def _playlist_permission(self, playlist_id: str) -> dict[str, Any] | None:
        """What the account may do with ``playlist_id``.

        The check is made against the account's own playlist list, so it
        cannot be fooled by a stale or missing flag in the UI.  ``None``
        means the playlist is not in the account's list at all.
        """
        user = self.login_manager.user_info
        if not user or not user.user_id:
            return None
        playlists = self._safe("account playlists", self.api.get_user_playlists_all,
                               user.user_id)
        if not playlists:
            return None
        wanted = str(playlist_id)
        for item in playlists:
            if str(item.get("id")) == wanted:
                return self._playlist_payload(item)
        return None

    def _forbidden(self, playlist_id: str, action: str = "edit") -> dict[str, Any] | None:
        """Refuse the operation when the account may not perform it.

        ``action`` is one of "edit" (rename / change the tracks) or "delete".
        Returns the error payload, or ``None`` when the operation is allowed.
        """
        payload = self._playlist_permission(playlist_id)
        if payload is None:
            return {"success": False, "error_key": "playlist.invalid_input"}
        if payload.get("is_liked_music"):
            return {"success": False, "error_key": "player.playlist_liked_locked"}
        if not payload.get("is_own"):
            # Somebody else's playlist: it may be un-subscribed, but nothing
            # else.  Editing it would fail on the server anyway ("无权限操作
            # 歌单"), and saying so here keeps the UI and the API in step.
            return {"success": False, "error_key": "player.playlist_not_owned"}
        if action == "delete" and not payload.get("can_delete"):
            return {"success": False, "error_key": "player.playlist_not_owned"}
        return None

    def update_playlist(self, playlist_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Update playlist metadata."""
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            return {"success": False, "error_key": "playlist.invalid_input"}
        denied = self._forbidden(playlist_id)
        if denied:
            return denied
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
        for playlist_id in ids:
            denied = self._forbidden(playlist_id, "delete")
            if denied:
                return denied
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
        denied = self._forbidden(playlist_id)
        if denied:
            return denied
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
        denied = self._forbidden(playlist_id)
        if denied:
            return denied
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
            # The two directions fail for different reasons, so they get
            # different messages.
            key = "player.subscribe_failed" if subscribe else "player.unsubscribe_failed"
            return {"success": False, "error_key": key, "message": result.get("msg") or ""}
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
        """Scan every configured folder; ``rescan`` re-reads the tag cache from disk.

        On a phone there are no folders to configure: the songs live in
        MediaStore, so the scan asks the system for them instead of
        walking a directory list that would always be empty.
        """
        if self._library_is_media_store():
            if rescan:
                self.local_music.clear()
            found = self.local_music.refresh()
            self._match_index_token = None
            return {"success": True, "tracks": found, "scanned": found,
                    "directories": [], "stats": self.local_music.stats()}
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

    def _library_is_media_store(self) -> bool:
        """Whether the local library is the phone's MediaStore one."""
        return hasattr(self.local_music, "is_media_store")

    def get_local_stats(self) -> dict[str, Any]:
        return {"success": True, "stats": self.local_music.stats(),
                "directories": list(self.settings.playback.local_dirs),
                "media": self.media.stats()}

    def request_audio_permission(self) -> dict[str, Any]:
        """Ask the phone for the permission to read the media library.

        Only meaningful on Android; everywhere else it answers that the
        library is already readable, because a desktop reads its folders
        without asking anyone.

        The Android answer arrives asynchronously (the system shows a
        dialog), so this waits for it -- with a timeout, because a dialog
        the user walks away from never answers.
        """
        if not self._library_is_media_store():
            return {"success": True, "granted": True, "state": "granted"}
        try:
            module = __import__("ncmandroid.permissions",
                                fromlist=["request_audio_permission"])
        except Exception as exc:
            self.logger.debug("no permission module: %s", exc)
            return {"success": False, "granted": False, "state": "unavailable"}
        done = threading.Event()
        state = {"value": "denied"}

        def _answered(result: str) -> None:
            state["value"] = result
            done.set()

        module.request_audio_permission(_answered)
        # The permission may already be granted, in which case the
        # callback ran before this line and the event is set already.
        if not done.wait(timeout=60):
            return {"success": False, "granted": False, "state": "timeout"}
        granted = state["value"] == module.ALREADY_GRANTED
        return {"success": granted, "granted": granted, "state": state["value"]}

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

    def set_background_image_data(self, data_url: str) -> dict[str, Any]:
        """Store a wallpaper that the page picked.

        The phone has no native file dialog the bridge can open, so the
        picture arrives as a data url and is written out first.  The rest
        is the same path the desktop takes: the file is copied into the
        config directory, downscaled and remembered by name.
        """
        raw = _decode_data_url(str(data_url or ""))
        if not raw:
            return {"success": False, "error_key": "settings.background_failed"}
        try:
            target = self.settings.config_path.parent / "wallpaper-picked.tmp"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        except OSError as exc:
            self.logger.warning("cannot store the picked wallpaper: %s", exc)
            return {"success": False, "error_key": "settings.background_failed"}
        try:
            name = self.settings.set_background_image(target)
        finally:
            try:
                target.unlink()
            except OSError:
                pass
        if not name:
            return {"success": False, "error_key": "settings.background_failed"}
        return {"success": True, "background_url": self.background_url(),
                "settings": self.get_settings()}

    def import_account_data(self, data_url: str) -> dict[str, Any]:
        """Import an account backup the page picked.

        The backup file travels as a data url (the phone has no native
        open dialog), is written to disk, and then goes through the same
        two steps the desktop does: plan the import, let the user decide
        about the conflicts, then apply it.
        """
        raw = _decode_data_url(str(data_url or ""))
        if not raw:
            return {"success": False, "error_key": "backup.invalid_file"}
        try:
            target = self.settings.config_path.parent / "account-picked.ncmbk"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        except OSError as exc:
            self.logger.warning("cannot store the picked backup: %s", exc)
            return {"success": False, "error_key": "backup.read_failed"}
        try:
            return self.import_account_backup(str(target))
        finally:
            try:
                target.unlink()
            except OSError:
                pass

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
            # Fallback to JS-based window.open -- happens only in the
            # browser/dev environment, not in a packaged build.
            self._open_desktop_lyrics_browser_fallback()
            return {"success": True}

        try:
            from src.resources import resource_path
            html_path = resource_path("src", "gui", "assets", "desktop-lyrics.html")
            if not html_path.exists():
                self.logger.warning("Desktop lyrics HTML not found at %s", html_path)
                self._open_desktop_lyrics_browser_fallback()
                return {"success": True}

            settings = self.settings
            ui = settings.ui
            font_size = ui.desktop_lyrics_font_size
            color = ui.desktop_lyrics_color
            opacity = ui.desktop_lyrics_opacity
            show_translation = ui.desktop_lyrics_show_translation

            # urlencode, not f-string: the colour is "#ffffff", and a raw
            # "#" starts the URL fragment -- the overlay then saw
            # ``?fontSize=18&color=`` plus a hash of the remaining settings,
            # silently losing the colour, the opacity and showTranslation.
            query = urlencode({
                "fontSize": font_size,
                "color": color,
                "opacity": opacity,
                "showTranslation": show_translation,
                "background": ui.desktop_lyrics_background,
                "textStyle": ui.desktop_lyrics_text_style,
            })

            # Same reasoning as the main window: prefer the loopback server so a
            # non-ASCII install path cannot break the file:// URL.
            url = None
            media = getattr(self, "media", None)
            if media is not None and getattr(media, "running", False):
                media.set_ui_root(html_path.parent)
                url = media.ui_url("desktop-lyrics.html", query=query)
            if url is None:
                url = f"{html_path.as_uri()}?{query}"

            # The overlay is a floating panel, not a document window: it has
            # no title bar, it floats above everything and it must not paint
            # a white rectangle on the wallpaper.
            #
            # The white came from the window background.  pywebview's default
            # is an opaque #FFFFFF, and macOS draws a frameless window as a
            # rounded rectangle in that colour -- so the lyrics floated in a
            # white slab with the dark card inside it.  ``transparent`` makes
            # the window background clear and lets the WebView composite
            # over the desktop, and ``vibrancy`` puts the system material
            # (the translucent grey panel macOS uses for floating windows)
            # behind the content.
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
                # pywebview only accepts a 3- or 6-digit hex here: an
                # 8-digit "#00000000" is rejected by create_window with
                # "not a valid hex triplet color", and the window is then
                # never created at all.  The alpha is not ours to set --
                # with transparent=True the backend forces it to 0 and
                # clears the window background itself.
                background_color="#000000",
                vibrancy=True,
            )

            # Store reference to this bridge for the desktop lyrics window
            self._desktop_lyrics_window.events.closed += self._on_desktop_lyrics_closed

            # The previous code invoked `onOpenDesktopLyrics` here, but that just
            # delegated back to the main window's `openDesktopLyrics()`
            # -> infinite recursion.  We now signal the main window via a
            # uniquely named event so the JS side can `pushLyricsToDesktop`
            # exactly once without re-creating the window.  `update_desktop_lyrics`
            # then evaluates directly on the new window.
            self._call_js("onDesktopLyricsWindowReady", {})
            return {"success": True}
        except Exception as exc:
            self.logger.warning("Failed to create desktop lyrics window: %s", exc)
            self._open_desktop_lyrics_browser_fallback()
            return {"success": True}

    def _open_desktop_lyrics_browser_fallback(self) -> None:
        """Dev-only path: open via JS `window.open`.  The main window then
        pushes updates directly to the returned window object via postMessage
        (the only way a vanilla browser tab can reach the popup)."""
        self._call_js("openDesktopLyricsFallback", {})

    def _on_desktop_lyrics_closed(self) -> None:
        self._desktop_lyrics_window = None
        # Reset the payload tracker so the next open will receive the full
        # lyrics array again instead of the stale "index-only" payload.
        self._desktop_lyrics_payload_id = -1

    def close_desktop_lyrics(self) -> dict[str, Any]:
        """Close the desktop lyrics window."""
        if self._desktop_lyrics_window is not None:
            try:
                self._desktop_lyrics_window.destroy()
            except Exception:
                pass
            self._desktop_lyrics_window = None
        return {"success": True}

    def desktop_lyrics_open(self) -> dict[str, Any]:
        """Whether the desktop lyrics window is currently open.

        The main window cannot see the native window (it lives in this
        bridge), so the toolbar button asks here to decide between opening
        and closing it.
        """
        window = self._desktop_lyrics_window
        if window is None:
            return {"success": True, "open": False}
        # pywebview.Window has no `closed` attribute (only an `events.closed`
        # hook), so ask the module for the live window list instead.
        alive = True
        windows = getattr(self._webview_module, "windows", None)
        if isinstance(windows, list | tuple):
            alive = any(open_window is window for open_window in windows)
        if not alive:
            self._desktop_lyrics_window = None
            return {"success": True, "open": False}
        return {"success": True, "open": True}

    # ---------------------------------------------------------------- cache
    def force_refresh(self) -> dict[str, Any]:
        """Clear every cache and restart the app.

        The escape hatch: if a cache ever holds something stale enough to
        make the app unusable, this wipes it and starts over.  It clears the
        metadata cache, the media cache (audio + covers), the in-memory
        response caches and the resolved-URL cache, then relaunches.

        The app is restarted rather than just cleared because some state
        (the media server's port, the webview's loaded page) has already
        been built from the cached data.
        """
        cleared = {"meta": 0, "media": 0}
        try:
            from src.core.metacache import get_meta_cache

            cleared["meta"] = get_meta_cache().clear()
        except Exception as exc:
            self.logger.info("cannot clear the metadata cache: %s", exc)
        try:
            # forget_online drops the resolved-URL cache and every cached
            # audio file in one go.
            self.media.forget_online(None)
            cleared["media"] = 1
        except Exception as exc:
            self.logger.info("cannot clear the media cache: %s", exc)
        try:
            self.api.clear_playlist_cache()
            self.api.clear_response_cache()
        except Exception as exc:
            self.logger.info("cannot clear the response caches: %s", exc)
        # Relaunch: the current process is about to be replaced.
        self._relaunch()
        return {"success": True, "cleared": cleared}

    def _relaunch(self) -> None:
        """Restart the app, replacing this process.

        The new instance must not fight the old one for the media server's
        port, so the old process only exits *after* the new one is up: the
        media server binds a random free port (port 0), so the two never
        collide.
        """
        import subprocess
        import sys

        try:
            if getattr(sys, "frozen", False):
                # A packaged build: the binary is the app.
                args = [sys.executable]
                cwd = Path(sys.executable).resolve().parent
            else:
                # A source checkout: run the same entry point main.py uses.
                root = Path(__file__).resolve().parents[2]
                args = [sys.executable, str(root / "main.py")]
                cwd = root
            subprocess.Popen(args, cwd=str(cwd), close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.logger.warning("cannot relaunch: %s", exc)
            return
        # Give the new process a moment, then get out of the way.
        threading.Timer(2.0, self._quit).start()

    def _quit(self) -> None:
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass
        os._exit(0)

    def get_cache_stats(self) -> dict[str, Any]:
        """How much disk the caches are using (audio, cover art, metadata)."""
        try:
            from src.core.cache import get_media_cache
            from src.core.metacache import get_meta_cache

            media = get_media_cache().stats()
            meta = get_meta_cache().stats()
            meta_bytes = sum(meta.get(kind, {}).get("bytes", 0)
                             for kind in ("meta", "lyric", "playlist"))
            meta_count = sum(meta.get(kind, {}).get("count", 0)
                             for kind in ("meta", "lyric", "playlist"))
            return {"success": True, **media,
                    "meta_bytes": meta_bytes, "meta_count": meta_count,
                    "meta": meta}
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.info("cache stats failed: %s", exc)
            return {"success": False}

    def clear_cache(self, what: str = "all") -> dict[str, Any]:
        """Drop cached audio, cover art, metadata, or all of them.

        Nothing here is user data: every file is re-downloadable, so clearing
        is always safe.  It is the one lever a user has on the app's local
        footprint, hence a button rather than a config file.
        """
        try:
            from src.core.cache import get_media_cache
            from src.core.metacache import get_meta_cache

            cache = get_media_cache()
            removed = 0
            if what in ("all", "audio"):
                removed += cache.forget_audio()
            if what in ("all", "cover"):
                removed += _purge_directory(cache.cover_dir)
            if what in ("all", "meta"):
                removed += get_meta_cache().clear()
            return {"success": True, "removed": removed, **self.get_cache_stats()}
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.info("cache clear failed: %s", exc)
            return {"success": False, "removed": 0}

    # -------------------------------------------------------------- account
    def get_user_profile(self, refresh: bool = False) -> dict[str, Any]:
        """The signed-in user's profile for the account panel.

        ``refresh=True`` re-reads it from the server (the nickname, avatar and
        counts change over time); the default returns the cached copy so the
        panel opens instantly.
        """
        manager = self.login_manager
        if not manager.is_logged_in:
            return {"success": False, "logged_in": False,
                    "error_key": "account.login_required"}
        try:
            profile = manager.refresh_profile() if refresh else manager.get_profile()
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.info("profile read failed: %s", exc)
            return {"success": False, "logged_in": True,
                    "error_key": "account.refresh_failed"}
        return {"success": True, "logged_in": True, "profile": profile}

    # ------------------------------------------------------------ auto update
    def check_for_update(self, force: bool = False) -> dict[str, Any]:
        """Ask GitHub whether a newer release exists.

        Kept for the corner notice of the main window; the settings page
        calls :meth:`check_app_update`, which returns the same facts plus
        the files to download.  Both go through ``src/core/updater`` so the
        two places can never disagree about what the newest version is.

        The channel and the certificate check come from the same settings
        as the settings page, so both places answer alike.
        """
        from src.core.updater import check_for_update as _check
        from src.version import __version__

        try:
            result = _check(
                __version__,
                include_prerelease=bool(self.settings.ui.update_include_prerelease),
                verify=not self.settings.ui.update_skip_ssl_verify,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.info("update check failed: %s", exc)
            return {"success": False, "available": False, "current": __version__}
        # The notice only knows "available" and the page to open.
        result["available"] = bool(
            result.get("success") and result.get("version")
            and result.get("message", "").startswith("发现新版本")
        )
        return result

    # ------------------------------------------------------------- disclaimer
    def quit_app(self) -> dict[str, Any]:
        """Close the app (the disclaimer's "quit" button)."""
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception as exc:  # pragma: no cover - platform specific
                self.logger.info("could not close the window: %s", exc)
        return {"success": True}

    def get_disclaimer_status(self) -> dict[str, Any]:
        """Whether the disclaimer still has to be shown.

        The accepted version is remembered, so the dialog appears on the
        first start and after an update that ships new disclaimer text.
        """
        accepted = self.settings.ui.disclaimer_accepted_version
        return {
            "success": True,
            "required": accepted != DISCLAIMER_VERSION,
            "version": DISCLAIMER_VERSION,
            "accepted_version": accepted,
        }

    def accept_disclaimer(self) -> dict[str, Any]:
        """Record that the user has read and accepted the disclaimer."""
        self.settings.update_ui(disclaimer_accepted_version=DISCLAIMER_VERSION)
        return {"success": True, "version": DISCLAIMER_VERSION}

    def update_desktop_lyrics(self, lines: list[dict[str, Any]], index: int) -> dict[str, Any]:
        """Push lyric update to the desktop lyrics window.

        The desktop window is a separate pywebview instance with its own
        JS context, so it cannot receive callbacks fired on the main
        window -- ``window.opener`` is ``null`` and localStorage is not
        shared.  We therefore evaluate the lyrics payload directly on the
        desktop window, where ``window.handleLyricsUpdate`` (set by
        ``desktop-lyrics.js``) dispatches into the IIFE.
        """
        target = self._desktop_lyrics_window
        if target is None:
            return {"success": False, "reason": "desktop_lyrics_window_closed"}
        try:
            # Always send the whole lines array.
            #
            # This used to send the lines only on the first push per window
            # and then index-only updates, keyed on ``id(target)``.  That
            # optimisation is what made the overlay permanently empty: the
            # normal flow is "open the overlay, then press play", so the one
            # and only "full" push carried ``lines == []`` (the lyrics arrive
            # from the network later), and every push after it was an index
            # into an array the overlay never received.  Switching tracks had
            # the same effect -- the overlay froze on whatever song was
            # playing when it was opened.
            #
            # The caller only pushes when the active line actually changes
            # (``updateLyricHighlight`` early-returns otherwise), so the
            # array is sent at most once per lyric line: cheap enough, and
            # correct regardless of what the overlay has seen before.
            payload = {
                "action": "update",
                "full": True,
                "lines": lines,
                "index": index,
            }
            args = json.dumps(payload, ensure_ascii=False)
            target.evaluate_js(f"handleLyricsUpdate({args})")
            return {"success": True}
        except Exception as exc:
            self.logger.debug("desktop lyrics push failed: %s", exc)
            return {"success": False, "reason": str(exc)}

    def resize_desktop_lyrics(self, height: int) -> dict[str, Any]:
        """Grow the overlay window so the lyric text is not clipped.

        The window is created at a fixed height, but the content inside it
        scales with the font size.  A window that is too short clips the
        text, and because the bubble was anchored at the bottom the clipped
        part was the top line -- the lyrics looked "too high" and cut off.
        The overlay measures its own content and asks for the height it
        needs.
        """
        target = self._desktop_lyrics_window
        if target is None:
            return {"success": False, "reason": "desktop_lyrics_window_closed"}
        try:
            # The card is a fixed 468 px wide, so a long line at a large font
            # size wraps to several rows: at 36 px the content is 381 px tall.
            # A 400 px ceiling clipped the last row, so the cap follows the
            # font size range (10-36 px) instead.
            wanted = max(80, min(600, int(height)))
            current = getattr(target, "height", None)
            if current is None or int(current) == wanted:
                return {"success": True, "height": wanted}
            target.height = wanted
            return {"success": True, "height": wanted}
        except Exception as exc:
            self.logger.debug("desktop lyrics resize failed: %s", exc)
            return {"success": False, "reason": str(exc)}

    def update_desktop_lyrics_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Push a styling tweak (font size / color / opacity / showTranslation)
        to the open desktop window.  Mirrors `update_desktop_lyrics` but only
        carries the settings dictionary; the JS side merges it into its own
        state and reapplies the styles."""
        target = self._desktop_lyrics_window
        if target is None:
            return {"success": False, "reason": "desktop_lyrics_window_closed"}
        try:
            payload = {"action": "settings", "settings": settings}
            args = json.dumps(payload, ensure_ascii=False)
            target.evaluate_js(f"handleLyricsUpdate({args})")
            return {"success": True}
        except Exception as exc:
            self.logger.debug("desktop lyrics settings push failed: %s", exc)
            return {"success": False, "reason": str(exc)}

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
