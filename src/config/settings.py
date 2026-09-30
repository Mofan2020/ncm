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
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from src.version import APP_NAME

__all__ = ["Settings", "get_settings", "default_download_dir"]


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
    download_dir: str = ""


@dataclass
class UISettings:
    """UI-related settings."""

    language: str = "zh_cn"          # zh_cn|en_us
    theme: str = "system"            # light|dark|system
    window_width: int = 1080
    window_height: int = 720
    remember_window_size: bool = True


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
    ui: UISettings = field(default_factory=UISettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    debug: bool = False


def _filtered(cls: Any, data: dict[str, Any] | None) -> dict[str, Any]:
    """Drop keys the dataclass does not know about."""
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in known}


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
        self._initialized = True
        self.logger = logging.getLogger("ncm.settings")
        self._data = AppSettings()
        self._config_path = self._config_path_for_platform()
        self._save_lock = threading.Lock()
        self.load()

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
                ui=UISettings(**_filtered(UISettings, raw.get("ui"))),
                auth=AuthSettings(**_filtered(AuthSettings, raw.get("auth"))),
                debug=bool(raw.get("debug", False)),
            )
        except Exception as exc:
            self.logger.warning("failed to load settings (%s); using defaults", exc)
            self._data = AppSettings()

    def save(self) -> None:
        with self._save_lock:
            payload = {
                "download": asdict(self._data.download),
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
        self.save()

    def update_auth(self, **kwargs: Any) -> None:
        for key, value in _filtered(AuthSettings, kwargs).items():
            setattr(self._data.auth, key, value)
        self.save()

    def reset(self) -> None:
        self._data = AppSettings()
        self.save()


def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return Settings()
