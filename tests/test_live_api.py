"""Live API smoke tests.

These hit the real NetEase endpoints and are therefore opt-in::

    NCM_LIVE=1 pytest tests/test_live_api.py

They exist so that an endpoint change (NetEase retires routes regularly) is
noticed immediately instead of silently breaking downloads.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.api import get_api
from src.core.downloader import SongDownloader

pytestmark = pytest.mark.network

LIVE = os.environ.get("NCM_LIVE") == "1"

if not LIVE:
    pytest.skip("set NCM_LIVE=1 to run live API tests", allow_module_level=True)

PLAYLIST_ID = os.environ.get("NCM_PLAYLIST_ID", "3778678")


@pytest.fixture(scope="module")
def api():
    return get_api(debug=True)


def test_playlist_detail_returns_tracks(api):
    info = api.get_playlist_info(PLAYLIST_ID)
    assert info, "playlist detail must answer"
    assert info.get("name")
    assert info.get("trackCount", 0) > 0


def test_complete_track_list(api):
    songs = api.get_playlist_songs(PLAYLIST_ID)
    assert songs and len(songs) > 10
    first = songs[0]
    assert first.get("id") and first.get("name")


def test_play_url_for_a_free_track(api):
    songs = api.get_playlist_songs(PLAYLIST_ID)
    free = [song for song in songs if song.get("fee") == 0]
    if not free:
        pytest.skip("no royalty free track in this playlist")
    info = api.get_song_url_info(str(free[0]["id"]), "standard")
    assert info, f"no play url: {api.last_url_error}"
    assert info["url"].startswith("http")
    assert info["br"] >= 128000
    assert info["type"] in ("mp3", "flac", "m4a", "aac", "wav", "ogg")


def test_real_download(tmp_path: Path, api):
    songs = [song for song in api.get_playlist_songs(PLAYLIST_ID) if song.get("fee") == 0][:1]
    if not songs:
        pytest.skip("no royalty free track in this playlist")
    downloader = SongDownloader(download_dir=str(tmp_path), quality="standard",
                                overwrite=True, max_concurrent=1)
    stats = downloader.batch_download(songs, api)
    assert stats.success == 1, stats.failed_songs
    files = [path for path in tmp_path.iterdir() if path.suffix in (".mp3", ".flac")]
    assert files and files[0].stat().st_size > 200_000


def test_account_status_endpoint(api):
    from src.auth import get_login_manager

    manager = get_login_manager()
    logged_in, account, profile = manager._fetch_account()
    assert isinstance(logged_in, bool), "the account endpoint must answer (200 + JSON)"
    if logged_in:
        assert account.get("id") and profile.get("nickname") is not None
