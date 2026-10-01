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


def _reset_stores() -> None:
    """Drop the process-wide stores so every test starts from an empty library.

    ``Settings``/``Library``/``LocalMusicLibrary``/``MediaServer`` are all
    singletons keyed to the config directory, which changes per test.
    """
    from src.config import settings as settings_module

    settings_module.Settings._instance = None

    from src.core import library as library_module

    library_module.Library._instance = None

    from src.core import localmusic as localmusic_module

    localmusic_module.LocalMusicLibrary._instance = None
    localmusic_module._local_music = None

    from src.core import mediaserver as mediaserver_module

    server = mediaserver_module.MediaServer._instance
    if server is not None:
        try:
            server.stop()
        except Exception:  # pragma: no cover - already stopped
            pass
    mediaserver_module.MediaServer._instance = None


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point the app's config directory at a throwaway location."""
    home = tmp_path / "home"
    (home / "Music").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.delenv("XDG_CONFIG_HOME_BACKUP", raising=False)

    _reset_stores()
    yield home
    _reset_stores()


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

    # ---- player surface (mirrors NeteaseAPI, no network) ------------------
    def search_songs(self, keyword, limit=30, offset=0):
        return [dict(song) for song in self.SONGS]

    def get_songs_detail(self, song_ids, batch_size=200):
        wanted = {str(i) for i in song_ids}
        return [dict(song) for song in self.SONGS if str(song["id"]) in wanted]

    def get_user_playlists(self, uid, limit=100, offset=0):
        return {"playlists": [{"id": 7, "name": "Fake list", "trackCount": 2,
                               "creator": {"nickname": "tester"}}], "more": False}

    def get_user_playlists_all(self, uid, max_pages=20):
        return self.get_user_playlists(uid)["playlists"]

    def get_liked_song_ids(self, uid):
        return ["1"]

    def set_song_like(self, song_id, like=True):
        self.liked = (str(song_id), bool(like))
        return True

    def report_play(self, song_id, seconds, source_id=0, played_at=None):
        self.reported = (str(song_id), float(seconds))
        return True

    def get_lyrics(self, song_id):
        return {"lyric": "[00:01.00]hello\n[00:02.00]world", "translation": "", "code": 200}


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
