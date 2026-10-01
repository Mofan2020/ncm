"""Configuration management with YAML persistence.

The settings file lives in the platform's application-data directory and is
shared by every launch:

* macOS   ``~/Library/Application Support/NeteaseMusicDownloader/settings.yaml``
* Windows ``%APPDATA%\\NeteaseMusicDownloader\\settings.yaml``
* Linux   ``~/.config/NeteaseMusicDownloader/settings.yaml``

Unknown keys in an existing file are ignored (and dropped on the next save)
instead of aborting the load, so upgrading between versions never resets the
user's configuration.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from src.version import APP_NAME

__all__ = ["Settings", "get_settings", "default_download_dir", "PLAY_MODES", "BACKGROUND_IMAGE"]

#: Playback order modes the player offers.  ``shuffle`` is a uniform random
#: pick -- play counts never influence the probability.
PLAY_MODES = ("single", "list", "shuffle")

#: Background images are copied here (inside the config dir) so replacing the
#: original file later cannot break the wallpaper.
BACKGROUND_IMAGE = "background"

#: Extensions accepted for a background image, in preference order.
BACKGROUND_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")


def default_download_dir() -> Path:
    """Where downloads go unless the user picks something else."""
    music = Path.home() / "Music"
    if music.exists() or os.name != "nt":
        return music / "Downloads"
    return Path.home() / "Downloads" / APP_NAME


@dataclass
class DownloadSettings:
    """Download-related settings."""

    quality: str = "standard"        # standard|higher|exhigh|lossless|hires
    max_concurrent: int = 2          # 1-3
    overwrite: bool = False
    download_lyrics: bool = True      # write a .lrc next to every song
    lyrics_translation: bool = True   # merge the translation into that .lrc
    download_dir: str = ""


@dataclass
class PlaybackSettings:
    """Player-related settings."""

    #: ``True`` prefers the online stream, ``False`` prefers a local file.
    prefer_online: bool = True
    #: Level requested for online playback (standard|higher|exhigh|lossless|hires).
    online_quality: str = "standard"
    #: single|list|shuffle -- shuffle is uniform random, never weighted.
    play_mode: str = "list"
    volume: float = 0.8
    #: Restore the last track/position when the app starts again.
    resume_playback: bool = True
    #: Report playbacks to NetEase.  Off by default: the local counter is the
    #: real record, and sending anything to a third party is opt-in.
    report_play_count: bool = False
    #: Folders scanned for local music.
    local_dirs: list[str] = field(default_factory=list)
    #: Folder the last "play a folder" action used.
    last_local_dir: str = ""
    scan_depth: int = 6
    show_translation: bool = True


@dataclass
class UISettings:
    """UI-related settings."""

    language: str = "zh_cn"          # zh_cn|en_us
    theme: str = "system"            # light|dark|system
    window_width: int = 1080
    window_height: int = 720
    remember_window_size: bool = True
    #: File name of the chosen background image inside the config dir.
    background_image: str = ""
    #: 0-40 px of blur and 0-80 % darkening applied over the background.
    background_blur: int = 24
    background_dim: int = 30
    #: Liquid-glass surfaces (translucent + blurred) on or off.
    glass: bool = True
    #: Wallpaper accent is sampled from the background image when available.
    accent_from_background: bool = True


@dataclass
class AuthSettings:
    """Authentication settings."""

    login_method: str = ""           # qrcode|phone|cookie
    saved_cookie: str = ""
    remember_login: bool = False


@dataclass
class AppSettings:
    """Root settings container."""

    download: DownloadSettings = field(default_factory=DownloadSettings)
    playback: PlaybackSettings = field(default_factory=PlaybackSettings)
    ui: UISettings = field(default_factory=UISettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    debug: bool = False


def _filtered(cls: Any, data: dict[str, Any] | None) -> dict[str, Any]:
    """Drop keys the dataclass does not know about."""
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in known}


def _as_bool(value: Any) -> bool:
    """Tolerant truthiness for values that may come from JSON or YAML."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class Settings:
    """Thread-safe settings manager with YAML persistence (singleton)."""

    _instance: Settings | None = None
    _lock = threading.Lock()

    def __new__(cls) -> Settings:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self.logger = logging.getLogger("ncm.settings")
        self._data = AppSettings()
        self._config_path = self._config_path_for_platform()
        self._save_lock = threading.Lock()
        self.load()
        # Set last: a background thread that grabs the singleton while __init__
        # is still running must not see it as ready before the state exists.
        self._initialized = True

    # ------------------------------------------------------------------- paths
    @staticmethod
    def _config_dir() -> Path:
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        return base / APP_NAME

    def _config_path_for_platform(self) -> Path:
        directory = self._config_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("cannot create config dir %s: %s", directory, exc)
        return directory / "settings.yaml"

    @property
    def config_path(self) -> Path:
        return self._config_path

    # -------------------------------------------------------------------- I/O
    def load(self) -> None:
        if not self._config_path.exists():
            return
        try:
            with open(self._config_path, encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
            if not isinstance(raw, dict):
                raise ValueError("settings file is not a mapping")
            self._data = AppSettings(
                download=DownloadSettings(**_filtered(DownloadSettings, raw.get("download"))),
                playback=PlaybackSettings(**_filtered(PlaybackSettings, raw.get("playback"))),
                ui=UISettings(**_filtered(UISettings, raw.get("ui"))),
                auth=AuthSettings(**_filtered(AuthSettings, raw.get("auth"))),
                debug=bool(raw.get("debug", False)),
            )
            self._normalize()
        except Exception as exc:
            self.logger.warning("failed to load settings (%s); using defaults", exc)
            self._data = AppSettings()

    def _normalize(self) -> None:
        """Re-apply the ranges and shapes a hand-edited file may have broken."""
        ui = self._data.ui
        ui.background_blur = self._clamp_range(ui.background_blur, 0, 40, 24)
        ui.background_dim = self._clamp_range(ui.background_dim, 0, 80, 30)
        for flag in ("remember_window_size", "glass", "accent_from_background"):
            setattr(ui, flag, _as_bool(getattr(ui, flag)))
        playback = self._data.playback
        for flag in ("prefer_online", "resume_playback", "report_play_count",
                     "show_translation"):
            setattr(playback, flag, _as_bool(getattr(playback, flag)))
        playback.scan_depth = self._clamp_range(playback.scan_depth, 1, 12, 6)
        try:
            playback.volume = min(1.0, max(0.0, float(playback.volume)))
        except (TypeError, ValueError):
            playback.volume = 0.8
        if playback.play_mode not in PLAY_MODES:
            playback.play_mode = "list"
        if not isinstance(playback.local_dirs, list):
            playback.local_dirs = []
        playback.local_dirs = [str(item) for item in playback.local_dirs if str(item or "").strip()]
        for flag in ("overwrite", "download_lyrics", "lyrics_translation"):
            setattr(self._data.download, flag, _as_bool(getattr(self._data.download, flag)))

    @staticmethod
    def _clamp_range(value: Any, low: int, high: int, fallback: int) -> int:
        try:
            return max(low, min(int(value), high))
        except (TypeError, ValueError):
            return fallback

    def save(self) -> None:
        with self._save_lock:
            payload = {
                "download": asdict(self._data.download),
                "playback": asdict(self._data.playback),
                "ui": asdict(self._data.ui),
                "auth": asdict(self._data.auth),
                "debug": self._data.debug,
            }
            try:
                with open(self._config_path, "w", encoding="utf-8") as handle:
                    yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
            except OSError as exc:
                self.logger.warning("failed to save settings: %s", exc)

    # -------------------------------------------------------------- accessors
    @property
    def download(self) -> DownloadSettings:
        return self._data.download

    @property
    def playback(self) -> PlaybackSettings:
        return self._data.playback

    @property
    def ui(self) -> UISettings:
        return self._data.ui

    @property
    def auth(self) -> AuthSettings:
        return self._data.auth

    @property
    def debug(self) -> bool:
        return self._data.debug

    @debug.setter
    def debug(self, value: bool) -> None:
        self._data.debug = bool(value)
        self.save()

    def update_download(self, **kwargs: Any) -> None:
        for key, value in _filtered(DownloadSettings, kwargs).items():
            setattr(self._data.download, key, value)
        # The UI sends JSON, so booleans may arrive as "true"/"false" strings.
        for flag in ("overwrite", "download_lyrics", "lyrics_translation"):
            setattr(self._data.download, flag, _as_bool(getattr(self._data.download, flag)))
        self._data.download.max_concurrent = self._clamp_concurrency(
            self._data.download.max_concurrent)
        self.save()

    @staticmethod
    def _clamp_concurrency(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 2
        return max(1, min(number, 3))

    def update_ui(self, **kwargs: Any) -> None:
        for key, value in _filtered(UISettings, kwargs).items():
            setattr(self._data.ui, key, value)
        self._normalize()
        self.save()

    def update_playback(self, **kwargs: Any) -> None:
        for key, value in _filtered(PlaybackSettings, kwargs).items():
            setattr(self._data.playback, key, value)
        self._normalize()
        self.save()

    def update_auth(self, **kwargs: Any) -> None:
        for key, value in _filtered(AuthSettings, kwargs).items():
            setattr(self._data.auth, key, value)
        self.save()

    # ---------------------------------------------------------------- wallpaper
    def background_path(self) -> Path | None:
        """Absolute path of the stored wallpaper, or ``None`` when unset/missing."""
        name = str(self._data.ui.background_image or "").strip()
        if not name:
            return None
        candidate = self._config_path.parent / name
        return candidate if candidate.is_file() else None

    def set_background_image(self, source: str | Path | None) -> str:
        """Copy ``source`` into the config dir and remember it.

        Passing ``None`` (or an unreadable path) removes the wallpaper.  Copies
        instead of referencing, so the wallpaper survives the original being
        moved or deleted, and travels with a config backup.
        """
        directory = self._config_path.parent
        if source is None:
            self.clear_background_image()
            return ""
        try:
            src = Path(source).expanduser()
            if not src.is_file():
                raise FileNotFoundError(str(src))
            extension = src.suffix.lower()
            if extension not in BACKGROUND_EXTENSIONS:
                extension = ".jpg"
            target = directory / f"{BACKGROUND_IMAGE}{extension}"
            for old in directory.glob(f"{BACKGROUND_IMAGE}.*"):
                if old != target:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            shutil.copyfile(src, target)
        except Exception as exc:
            self.logger.warning("cannot store the background image: %s", exc)
            return ""
        self.update_ui(background_image=target.name)
        return target.name

    def clear_background_image(self) -> None:
        directory = self._config_path.parent
        for old in directory.glob(f"{BACKGROUND_IMAGE}.*"):
            try:
                old.unlink()
            except OSError:
                pass
        self.update_ui(background_image="")

    def reset(self) -> None:
        self._data = AppSettings()
        self.save()


def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return Settings()
