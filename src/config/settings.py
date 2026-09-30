"""Settings management with YAML persistence."""
import os
import yaml
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class DownloadSettings:
    """Download-related settings."""
    quality: str = "standard"  # standard, higher, exhigh, lossless, hires
    max_concurrent: int = 2
    overwrite: bool = False
    download_dir: str = ""
    skip_url_check: bool = False


@dataclass
class UISettings:
    """UI-related settings."""
    language: str = "zh_cn"  # zh_cn, en_us
    theme: str = "system"  # light, dark, system
    window_width: int = 1000
    window_height: int = 700
    remember_window_size: bool = True


@dataclass
class AuthSettings:
    """Authentication settings."""
    login_method: str = ""  # qrcode, phone, cookie
    saved_cookie: str = ""
    remember_login: bool = False
    auto_login: bool = False


@dataclass
class AppSettings:
    """Main application settings container."""
    download: DownloadSettings = field(default_factory=DownloadSettings)
    ui: UISettings = field(default_factory=UISettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    debug: bool = False


class Settings:
    """Thread-safe settings manager with YAML persistence."""
    
    _instance: Optional['Settings'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._data: AppSettings = AppSettings()
        self._config_path = self._get_config_path()
        self._load()
    
    def _get_config_path(self) -> Path:
        """Get configuration file path."""
        if os.name == 'nt':  # Windows
            base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        else:  # macOS/Linux
            base = Path.home() / 'Library' / 'Application Support' if sys.platform == 'darwin' else Path.home() / '.config'
        config_dir = base / 'NeteaseMusicDownloader'
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / 'settings.yaml'
    
    def _load(self) -> None:
        """Load settings from YAML file."""
        if self._config_path.exists():
            try:
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
                self._data = self._dict_to_settings(data)
            except Exception as e:
                print(f"Failed to load settings: {e}")
    
    def _save(self) -> None:
        """Save settings to YAML file."""
        try:
            with open(self._config_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(self._settings_to_dict(self._data), f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            print(f"Failed to save settings: {e}")
    
    def _dict_to_settings(self, data: Dict[str, Any]) -> AppSettings:
        """Convert dictionary to AppSettings."""
        download = DownloadSettings(**data.get('download', {}))
        ui = UISettings(**data.get('ui', {}))
        auth = AuthSettings(**data.get('auth', {}))
        return AppSettings(
            download=download,
            ui=ui,
            auth=auth,
            debug=data.get('debug', False)
        )
    
    def _settings_to_dict(self, settings: AppSettings) -> Dict[str, Any]:
        """Convert AppSettings to dictionary."""
        return {
            'download': asdict(settings.download),
            'ui': asdict(settings.ui),
            'auth': asdict(settings.auth),
            'debug': settings.debug
        }
    
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
        self._data.debug = value
        self._save()
    
    def update_download(self, **kwargs) -> None:
        """Update download settings."""
        for key, value in kwargs.items():
            if hasattr(self._data.download, key):
                setattr(self._data.download, key, value)
        self._save()
    
    def update_ui(self, **kwargs) -> None:
        """Update UI settings."""
        for key, value in kwargs.items():
            if hasattr(self._data.ui, key):
                setattr(self._data.ui, key, value)
        self._save()
    
    def update_auth(self, **kwargs) -> None:
        """Update auth settings."""
        for key, value in kwargs.items():
            if hasattr(self._data.auth, key):
                setattr(self._data.auth, key, value)
        self._save()
    
    def reset(self) -> None:
        """Reset to defaults."""
        self._data = AppSettings()
        self._save()


def get_settings() -> Settings:
    """Get global settings instance."""
    return Settings()


import sys