"""Shared pytest fixtures: isolated config dir + pywebview stub."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_webview_stub() -> None:
    """Give the tests a fake pywebview when no GUI backend is importable."""
    stub = types.ModuleType("webview")
    stub.FOLDER_DIALOG = "FOLDER_DIALOG"
    stub.OPEN_DIALOG = "OPEN_DIALOG"
    stub.screens = []
    stub.__version__ = "0.0.0-stub"
    stub.create_window = lambda *args, **kwargs: types.SimpleNamespace(
        events=types.SimpleNamespace(closed=[]),
        evaluate_js=lambda *a, **k: None,
    )
    stub.start = lambda *args, **kwargs: None
    sys.modules["webview"] = stub


try:  # pragma: no cover - depends on the environment
    import webview  # noqa: F401
except Exception:  # pragma: no cover
    _install_webview_stub()


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point the app's config directory at a throwaway location."""
    home = tmp_path / "home"
    (home / "Music").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.delenv("XDG_CONFIG_HOME_BACKUP", raising=False)

    from src.config import settings as settings_module

    settings_module.Settings._instance = None
    yield home
    settings_module.Settings._instance = None


@pytest.fixture()
def fresh_i18n(isolated_home):
    """Reload the translation catalogue against the isolated config."""
    from src.i18n import I18n

    I18n._instance = None
    instance = I18n()
    yield instance
    I18n._instance = None


@pytest.fixture()
def fresh_api(isolated_home):
    """A NeteaseAPI instance with no shared state."""
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()
    yield instance
    api_module._api_instance = None


class FakePlaylistAPI:
    """Stand-in for NeteaseAPI used by the bridge tests (no network)."""

    INFO = {
        "id": 42,
        "name": "Test playlist",
        "creator": {"nickname": "tester"},
        "coverImgUrl": "https://example.invalid/cover.jpg",
        "trackCount": 2,
        "playCount": 7,
        "description": "d",
    }
    SONGS = [
        {"id": 1, "name": "One", "artists": [{"name": "A"}], "al": {"name": "Album"},
         "dt": 1000, "fee": 0},
        {"id": 2, "name": "Two", "ar": [{"name": "B"}], "al": {"name": "Album"},
         "dt": 2000, "fee": 1},
    ]

    def __init__(self):
        self.debug = False
        self.last_url_error = None

    def get_playlist_info(self, playlist_id):
        return dict(self.INFO)

    def get_playlist_songs(self, playlist_id):
        return [dict(song) for song in self.SONGS]

    def get_song_url_info(self, song_id, quality="standard"):
        return {"url": "http://127.0.0.1/unused", "level": quality, "br": 128000,
                "size": 10, "type": "mp3", "code": 200}

    def get_request_stats(self):
        return {"total_requests": 1}


@pytest.fixture()
def bridge(isolated_home):
    """A GuiBridge with a fake API, no window and no network."""
    from src.auth import login as login_module
    from src.core import api as api_module
    from src.gui.bridge import GuiBridge

    login_module.LoginManager._instance = None
    api_module._api_instance = None
    instance = GuiBridge()
    instance.api = FakePlaylistAPI()
    yield instance
    login_module.LoginManager._instance = None
    api_module._api_instance = None
