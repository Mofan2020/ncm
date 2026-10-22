"""The cache panel: footprint read-out and clearing."""
from __future__ import annotations

import pytest


@pytest.fixture()
def bridge(isolated_home):
    from src.gui.bridge import GuiBridge

    return GuiBridge()


def test_stats_report_audio_and_cover_totals(bridge):
    result = bridge.get_cache_stats()
    assert result["success"] is True
    for key in ("audio", "cover"):
        assert key in result
        assert "bytes" in result[key]
        assert "budget" in result[key]


def test_clearing_an_empty_cache_is_harmless(bridge):
    result = bridge.clear_cache("all")
    assert result["success"] is True
    assert result["removed"] == 0


def test_clearing_drops_the_files_and_reports_a_count(bridge):
    from src.core.cache import get_media_cache

    cache = get_media_cache()
    cache.cover_dir.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (cache.cover_dir / f"probe{index}.jpg").write_bytes(b"x" * 16)
    before = bridge.get_cache_stats()["cover"]["count"]
    assert before >= 3

    result = bridge.clear_cache("cover")
    assert result["success"] is True
    assert result["removed"] >= 3
    assert bridge.get_cache_stats()["cover"]["count"] == 0


def test_clearing_one_kind_leaves_the_other_alone(bridge):
    from src.core.cache import get_media_cache

    cache = get_media_cache()
    cache.cover_dir.mkdir(parents=True, exist_ok=True)
    cache.audio_dir.mkdir(parents=True, exist_ok=True)
    (cache.cover_dir / "keepme.jpg").write_bytes(b"x" * 8)
    (cache.audio_dir / "keepme.mp3").write_bytes(b"x" * 8)

    bridge.clear_cache("audio")

    assert bridge.get_cache_stats()["audio"]["count"] == 0
    assert bridge.get_cache_stats()["cover"]["count"] >= 1


def test_an_unknown_target_clears_nothing(bridge):
    from src.core.cache import get_media_cache

    cache = get_media_cache()
    cache.cover_dir.mkdir(parents=True, exist_ok=True)
    (cache.cover_dir / "survivor.jpg").write_bytes(b"x" * 8)

    result = bridge.clear_cache("nonsense")
    assert result["success"] is True
    assert result["removed"] == 0
    assert bridge.get_cache_stats()["cover"]["count"] >= 1
