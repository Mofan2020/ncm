"""On-disk media cache: audio + cover bytes, LRU-by-budget eviction.

The cache is intentionally simple -- no SQLite, no metadata files, the
folder layout itself is the index.  These tests pin down that simplicity:
each test uses a fresh ``tmp_path`` so eviction math is reproducible, and
the budget is set explicitly (no surprises when the defaults change).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.core import cache as cache_module
from src.core.cache import MediaCache, get_media_cache


@pytest.fixture()
def fresh_cache(tmp_path):
    """A MediaCache pinned to a temp dir and a fresh singleton."""
    cache_module._instance = None
    cache = MediaCache()
    cache._root = tmp_path / "cache"
    cache._audio_dir = cache._root / "audio"
    cache._cover_dir = cache._root / "cover"
    cache._audio_budget = 4 * 1024
    cache._cover_budget = 4 * 1024
    cache._available = False
    yield cache
    cache_module._instance = None


def _write(cache, song_id, quality, payload, ext="mp3"):
    target = cache.write_audio(song_id, quality, payload, ext)
    assert target is not None
    return target


def test_audio_round_trip(fresh_cache):
    cache = fresh_cache
    payload = b"\xff\xfb\x90\x00" + b"music" * 32

    written = _write(cache, "186016", "standard", payload)
    assert written.read_bytes() == payload

    again = cache.open_audio("186016", "standard")
    assert again is not None
    assert again.read_bytes() == payload


def test_audio_path_is_quality_scoped(fresh_cache):
    cache = fresh_cache
    _write(cache, "186016", "standard", b"low", "mp3")
    _write(cache, "186016", "lossless", b"high", "flac")

    assert cache.open_audio("186016", "standard").read_bytes() == b"low"
    assert cache.open_audio("186016", "lossless", "flac").read_bytes() == b"high"


def test_audio_lookup_miss_returns_none(fresh_cache):
    cache = fresh_cache
    assert cache.open_audio("never-seen", "standard") is None
    assert cache.open_audio("186016", "hires") is None  # wrong quality too


def test_lru_eviction_drops_oldest(fresh_cache):
    cache = fresh_cache
    cache._available = True  # so the evict helpers can scan the dir
    _write(cache, "a", "standard", b"A" * 1024)
    time.sleep(0.01)  # mtime granularity on Windows + APFS is coarse
    _write(cache, "b", "standard", b"B" * 2048)
    _write(cache, "c", "standard", b"C" * 2048)
    # "a" should be evicted first -- the budget is 4 KB and the three
    # files together add up to 5 KB with "a" being the oldest.
    assert cache.open_audio("a", "standard") is None
    assert cache.open_audio("b", "standard") is not None
    assert cache.open_audio("c", "standard") is not None


def test_cover_round_trip(fresh_cache):
    cache = fresh_cache
    payload = b"\xff\xd8\xff\xe0" + b"jpeg" * 16  # plausible JPEG header
    written = cache.write_cover("https://example.com/cover.jpg?param=300y300",
                                 payload, "image/jpeg")
    assert written is not None
    again = cache.open_cover("https://example.com/cover.jpg?param=300y300",
                             "jpg")
    assert again is not None
    assert again[0] == payload
    assert again[1] == "image/jpeg"


def test_cover_extension_is_deduced_from_mime(fresh_cache):
    cache = fresh_cache
    cache.write_cover("https://a/x?param=1", b"\x89PNG", "image/png")
    # The hash key is stable, but the file suffix follows the mime.
    found = list(cache._cover_dir.glob("*.png"))
    assert found, "png cover should be saved with a .png suffix"


def test_cover_path_differs_per_url(fresh_cache):
    cache = fresh_cache
    cache.write_cover("https://a/1.jpg", b"x", "image/jpeg")
    cache.write_cover("https://a/2.jpg", b"y", "image/jpeg")
    assert len(list(cache._cover_dir.glob("*.jpg"))) == 2


def test_over_size_audio_is_refused(fresh_cache):
    cache = fresh_cache
    cache._available = True
    too_big = b"\xff" * (cache_module.NEXT_DOWNLOAD_BYTES + 1)
    assert cache.write_audio("big", "standard", too_big) is None


def test_forget_audio_drops_one_song(fresh_cache):
    cache = fresh_cache
    cache._available = True
    _write(cache, "a", "standard", b"x")
    _write(cache, "b", "standard", b"y")
    assert cache.forget_audio("a") == 1
    assert cache.open_audio("a", "standard") is None
    assert cache.open_audio("b", "standard") is not None


def test_forget_audio_with_none_clears_all(fresh_cache):
    cache = fresh_cache
    cache._available = True
    _write(cache, "a", "standard", b"x")
    _write(cache, "b", "lossless", b"y", "flac")
    assert cache.forget_audio(None) == 2
    assert cache.open_audio("a", "standard") is None
    assert cache.open_audio("b", "lossless") is None


def test_song_id_segment_is_sanitised(fresh_cache):
    """Path traversal characters in a song id must not escape the cache."""
    cache = fresh_cache
    cache._available = True
    sneaky = "../../../etc/passwd"
    target = cache.write_audio(sneaky, "standard", b"x")
    assert target is not None
    # The resolved file lives inside the audio dir, never above it.
    assert target.resolve().is_relative_to(cache._audio_dir.resolve())


def test_concurrent_writes_dont_corrupt(fresh_cache):
    """A small thundering-herd test -- the cache must serialise writes."""
    cache = fresh_cache
    cache._available = True

    def writer(suffix):
        for index in range(20):
            _write(cache, f"song-{suffix}-{index}", "standard",
                    b"x" * (256 + index))

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # No partial ``.part`` files must linger -- the atomic rename is the
    # whole reason we use it.
    parts = list(cache._audio_dir.rglob("*.part"))
    assert parts == [], f"partial files remain: {parts!r}"


def test_stats_reports_disk_usage(fresh_cache):
    cache = fresh_cache
    cache._available = True
    _write(cache, "a", "standard", b"x" * 100)
    _write(cache, "b", "standard", b"y" * 200)
    cache.write_cover("https://a/1.jpg", b"\xff\xd8\xff\xe0" + b"z" * 64,
                       "image/jpeg")

    stats = cache.stats()
    assert stats["available"] is True
    assert stats["audio"]["count"] == 2
    assert stats["audio"]["bytes"] == 300
    assert stats["cover"]["count"] == 1


def test_get_media_cache_is_a_singleton():
    """The singleton lives for the life of the process."""
    cache_module._instance = None
    a = get_media_cache()
    b = get_media_cache()
    assert a is b
