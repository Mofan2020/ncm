"""The force-refresh escape hatch: clear every cache and restart the app."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def bridge_with_cache(bridge):
    """A bridge whose caches actually hold something."""
    from src.core.metacache import get_meta_cache

    cache = get_meta_cache()
    cache.set_meta("song:1", {"id": 1})
    cache.set_lyric("song:1", {"lyric": "x"})
    cache.set_playlist("list:1", [{"id": 1}])
    return bridge


def test_force_refresh_clears_the_metadata_cache(bridge_with_cache):
    from src.core.metacache import get_meta_cache

    cache = get_meta_cache()
    assert cache.get_meta("song:1") is not None

    bridge_with_cache._relaunch = lambda: None
    result = bridge_with_cache.force_refresh()

    assert result["success"] is True
    assert cache.get_meta("song:1") is None
    assert cache.get_lyric("song:1") is None
    assert cache.get_playlist("list:1") is None


def test_force_refresh_clears_the_media_cache(bridge_with_cache, monkeypatch):
    calls = []

    class FakeMedia:
        def forget_online(self, song_id=None):
            calls.append(song_id)
            return None

    bridge_with_cache.media = FakeMedia()
    bridge_with_cache._relaunch = lambda: None
    bridge_with_cache.force_refresh()
    assert calls == [None]


def test_force_refresh_clears_the_in_memory_caches(bridge_with_cache):
    cleared = []

    class FakeApi:
        def clear_playlist_cache(self):
            cleared.append("playlist")

        def clear_response_cache(self):
            cleared.append("response")

    bridge_with_cache.api = FakeApi()
    bridge_with_cache._relaunch = lambda: None
    bridge_with_cache.force_refresh()
    assert sorted(cleared) == ["playlist", "response"]


def test_force_refresh_relaunches_the_app(bridge_with_cache):
    relaunched = []
    bridge_with_cache._relaunch = lambda: relaunched.append(True)
    bridge_with_cache.force_refresh()
    assert relaunched == [True]


def test_a_failing_cache_does_not_stop_the_refresh(bridge_with_cache):
    """One broken cache must not leave the others uncleared."""
    from src.core.metacache import get_meta_cache

    cache = get_meta_cache()
    cache.set_meta("song:1", {"id": 1})

    def boom(*args, **kwargs):
        raise RuntimeError("nope")

    bridge_with_cache.api.clear_playlist_cache = boom
    bridge_with_cache._relaunch = lambda: None
    result = bridge_with_cache.force_refresh()

    assert result["success"] is True
    # The metadata cache was still cleared despite the API cache failing.
    assert cache.get_meta("song:1") is None


def test_the_cache_stats_include_the_metadata_cache(bridge_with_cache):
    from src.core.metacache import get_meta_cache

    get_meta_cache().set_meta("song:1", {"id": 1})
    stats = bridge_with_cache.get_cache_stats()
    assert stats["success"] is True
    assert stats["meta_count"] >= 1
    assert stats["meta_bytes"] > 0


def test_clearing_the_metadata_cache_through_the_panel(bridge_with_cache):
    from src.core.metacache import get_meta_cache

    get_meta_cache().set_meta("song:1", {"id": 1})
    result = bridge_with_cache.clear_cache("meta")
    assert result["success"] is True
    assert get_meta_cache().get_meta("song:1") is None


def test_the_button_exists_in_the_settings():
    html = (ROOT / "src" / "gui" / "assets" / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-force-refresh"' in html


def test_the_button_is_wired_up():
    js = (ROOT / "src" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")
    assert "btn-force-refresh" in js
    assert "forceRefresh" in js
    assert "state.api.force_refresh" in js


def test_the_confirm_prompt_exists_in_both_languages():
    import json

    for lang in ("zh_cn", "en_us"):
        data = json.loads(
            (ROOT / "src" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert data["settings"]["cache_force_refresh"]
        assert data["settings"]["cache_force_refresh_confirm"]
