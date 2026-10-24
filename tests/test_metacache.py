"""The disk metadata cache: entries, TTLs, clearing and the API wiring."""
from __future__ import annotations

import json

import pytest

from src.core.metacache import (
    MAX_ENTRY_BYTES,
    TTL_LYRIC,
    TTL_META,
    TTL_PLAYLIST,
    MetaCache,
    get_meta_cache,
)


@pytest.fixture()
def cache(isolated_home):
    """A metadata cache rooted in the throwaway config directory."""
    from src.core import metacache as metacache_module

    metacache_module._instance = None
    instance = MetaCache()
    yield instance
    metacache_module._instance = None


# --------------------------------------------------------------- basic use
def test_a_stored_value_comes_back(cache):
    cache.set_meta("song:1", {"id": 1, "name": "One"})
    assert cache.get_meta("song:1") == {"id": 1, "name": "One"}


def test_a_missing_key_returns_none(cache):
    assert cache.get_meta("song:missing") is None
    assert cache.get_lyric("999") is None
    assert cache.get_playlist("nope") is None


def test_the_three_kinds_do_not_collide(cache):
    cache.set_meta("k", "meta")
    cache.set_lyric("k", "lyric")
    cache.set_playlist("k", "playlist")
    assert cache.get_meta("k") == "meta"
    assert cache.get_lyric("k") == "lyric"
    assert cache.get_playlist("k") == "playlist"


def test_an_empty_value_is_not_stored(cache):
    cache.set_meta("song:empty", {})
    cache.set_meta("song:none", None)
    assert cache.get_meta("song:empty") is None
    assert cache.get_meta("song:none") is None


# ------------------------------------------------------------------- expiry
def test_an_expired_entry_is_not_returned(cache):
    cache.set("meta", "song:old", {"x": 1}, ttl=-1)
    assert cache.get("meta", "song:old") is None


def test_a_fresh_entry_survives(cache):
    cache.set("meta", "song:new", {"x": 1}, ttl=3600)
    assert cache.get("meta", "song:new") == {"x": 1}


def test_the_default_ttls_are_the_documented_ones():
    assert TTL_META == 7 * 24 * 60 * 60
    assert TTL_LYRIC == 30 * 24 * 60 * 60
    assert TTL_PLAYLIST == 10 * 60


# ----------------------------------------------------------------- robustness
def test_a_corrupt_file_is_discarded_not_raised(cache):
    cache.set_meta("song:bad", {"x": 1})
    path = next((cache.root / "meta").glob("*.json"))
    path.write_text("{not json", encoding="utf-8")
    assert cache.get_meta("song:bad") is None
    # The broken file is gone, so the next write starts clean.
    assert not path.exists()


def test_a_half_written_entry_never_appears(cache):
    """The write is atomic: the temp file is renamed into place."""
    cache.set_meta("song:atomic", {"x": 1})
    leftovers = list((cache.root / "meta").glob("*.tmp"))
    assert leftovers == []


def test_a_value_that_is_not_json_serialisable_is_skipped(cache):
    cache.set_meta("song:obj", {"x": object()})
    assert cache.get_meta("song:obj") is None


def test_a_huge_value_is_not_written(cache):
    cache.set_meta("song:huge", {"blob": "x" * (MAX_ENTRY_BYTES + 10)})
    assert cache.get_meta("song:huge") is None


def test_keys_with_odd_characters_are_safe(cache):
    cache.set_meta("song:../../etc/passwd", {"x": 1})
    files = list((cache.root / "meta").glob("*.json"))
    assert len(files) == 1
    assert cache.get_meta("song:../../etc/passwd") == {"x": 1}


def test_different_keys_do_not_collide(cache):
    cache.set_meta("a/b", {"which": 1})
    cache.set_meta("a_b", {"which": 2})
    assert cache.get_meta("a/b") == {"which": 1}
    assert cache.get_meta("a_b") == {"which": 2}


# ------------------------------------------------------------------- clearing
def test_clear_drops_every_entry(cache):
    cache.set_meta("song:1", {"x": 1})
    cache.set_lyric("song:1", {"x": 1})
    cache.set_playlist("song:1", {"x": 1})
    removed = cache.clear()
    assert removed == 3
    assert cache.get_meta("song:1") is None
    assert cache.get_lyric("song:1") is None
    assert cache.get_playlist("song:1") is None


def test_clear_one_kind_leaves_the_others(cache):
    cache.set_meta("song:1", {"x": 1})
    cache.set_lyric("song:1", {"x": 1})
    cache.clear("meta")
    assert cache.get_meta("song:1") is None
    assert cache.get_lyric("song:1") == {"x": 1}


def test_clearing_an_empty_cache_is_harmless(cache):
    assert cache.clear() == 0


# ---------------------------------------------------------------------- stats
def test_stats_report_the_occupancy(cache):
    cache.set_meta("song:1", {"x": 1})
    cache.set_lyric("song:2", {"x": 1})
    stats = cache.stats()
    assert stats["meta"]["count"] == 1
    assert stats["lyric"]["count"] == 1
    assert stats["meta"]["bytes"] > 0


def test_the_cache_lives_next_to_the_config(cache):
    assert cache.root.name == "meta"
    assert cache.root.parent.name == "cache"


# ----------------------------------------------------------------- the API
def test_song_details_are_cached_per_song(isolated_home):
    """A second request for the same song does not hit the network."""
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()

    calls = []

    def fake_eapi(path, data=None):
        calls.append(path)
        return {"songs": [{"id": 1, "name": "One"}]}

    instance._eapi = fake_eapi

    first = instance.get_songs_detail(["1"])
    second = instance.get_songs_detail(["1"])

    assert first[0]["name"] == "One"
    assert second[0]["name"] == "One"
    # The second call was answered from the cache.
    assert len(calls) == 1

    api_module._api_instance = None


def test_only_the_missing_songs_are_requested(isolated_home):
    """A cached song does not cost a request; a new one does."""
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()

    requested = []

    def fake_eapi(path, data=None):
        requested.append(json.loads(data["c"]) if data and "c" in data else data)
        return {"songs": [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}]}

    instance._eapi = fake_eapi
    instance.get_songs_detail(["1", "2"])
    instance.get_songs_detail(["1", "2"])
    # Only one batch was ever requested.
    assert len(requested) == 1

    api_module._api_instance = None


def test_the_songs_come_back_in_the_requested_order(isolated_home):
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()

    def fake_eapi(path, data=None):
        ids = [item["id"] for item in json.loads(data["c"])]
        return {"songs": [{"id": i, "name": f"song{i}"} for i in reversed(ids)]}

    instance._eapi = fake_eapi
    songs = instance.get_songs_detail(["3", "1", "2"])
    assert [s["id"] for s in songs] == [3, 1, 2]

    api_module._api_instance = None


def test_the_lyrics_are_cached(isolated_home):
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()

    calls = []

    def fake_eapi(path, data=None):
        calls.append(path)
        return {"code": 200, "lrc": {"lyric": "[00:01.00] hi"}}

    instance._eapi = fake_eapi
    first = instance.get_lyrics("123")
    second = instance.get_lyrics("123")
    assert first == second
    assert len(calls) == 1

    api_module._api_instance = None


def test_the_playlist_tracks_are_cached(isolated_home):
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()

    calls = []

    def fake_fetch(playlist_id, use_cache=True):
        calls.append(playlist_id)
        return [{"id": 1}]

    instance._fetch_playlist_songs = fake_fetch
    first = instance.get_playlist_songs("42")
    second = instance.get_playlist_songs("42")
    assert first == second == [{"id": 1}]
    assert len(calls) == 1

    api_module._api_instance = None


def test_the_playlist_metadata_is_cached(isolated_home):
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()

    calls = []

    def fake_eapi(path, data=None, use_cache=True):
        calls.append(path)
        return {"playlist": {"id": 42, "name": "List", "coverImgId": 7,
                             "trackCount": 3, "creator": {"nickname": "me"}}}

    instance._eapi = fake_eapi
    first = instance.get_playlist_info("42")
    second = instance.get_playlist_info("42")
    assert first["name"] == "List"
    assert second["name"] == "List"
    assert len(calls) == 1

    api_module._api_instance = None


def test_the_cached_playlist_metadata_is_used_when_the_server_is_down(isolated_home):
    """The user still sees the playlist's name and cover offline."""
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()

    instance._eapi = lambda path, data=None, use_cache=True: {
        "playlist": {"id": 42, "name": "List", "coverImgId": 7}}
    instance.get_playlist_info("42")

    # Now the server is unreachable.
    instance._eapi = lambda path, data=None, use_cache=True: None
    instance._legacy = lambda *a, **k: None
    instance.clear_playlist_cache()
    fallback = instance.get_playlist_info("42")
    assert fallback is not None
    assert fallback["name"] == "List"
    assert fallback.get("tracks", []) == []

    api_module._api_instance = None


def test_the_singleton_is_stable():
    assert get_meta_cache() is get_meta_cache()
