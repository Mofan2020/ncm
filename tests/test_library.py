"""The persistent player library: play counts, favourites and the last session."""
from __future__ import annotations

import json
from pathlib import Path

from src.core.library import Library, get_library, split_key, track_key


def test_track_key_round_trip():
    assert track_key("online", 186016) == "online:186016"
    assert track_key("local", "/music/a.mp3") == "local:/music/a.mp3"
    assert split_key("online:186016") == ("online", "186016")
    assert split_key("local:/music/a.mp3") == ("local", "/music/a.mp3")
    # A key without a separator is treated as a bare source
    assert split_key("weird") == ("weird", "")


def test_record_play_counts_and_recent(isolated_home):
    library = get_library()
    meta = {"name": "One", "artists": "A"}

    assert library.record_play("online:1", meta=meta) == 1
    assert library.record_play("online:1", meta=meta) == 2
    assert library.record_play("local:/m/b.mp3", meta={"name": "Two"}) == 1

    assert library.play_count("online:1") == 2
    assert library.play_counts() == {"online:1": 2, "local:/m/b.mp3": 1}
    assert library.stats()["total_plays"] == 3

    recent = library.recent(10)
    assert [entry["key"] for entry in recent[:2]] == ["local:/m/b.mp3", "online:1"]
    assert recent[0]["meta"]["name"] == "Two"


def test_counts_and_favourites_survive_a_restart(isolated_home):
    library = get_library()
    library.record_play("online:9", meta={"name": "Nine", "artists": "N"})
    library.set_favorite("online:9", True, {"name": "Nine", "artists": "N"})

    # A new process would build a new singleton from the same config dir.
    Library._instance = None
    reloaded = get_library()

    assert reloaded.play_count("online:9") == 1
    assert reloaded.favorite_keys() == ["online:9"]
    assert reloaded.favorites()[0]["meta"]["name"] == "Nine"


def test_favorite_toggle_and_metadata(isolated_home):
    library = get_library()
    meta = {"name": "Song", "artists": "Artist", "album": "Album",
            "cover_url": "http://x/cover.jpg", "source": "online", "id": "5"}

    assert library.set_favorite("online:5", True, meta) is True
    assert library.is_favorite("online:5") is True
    entry = library.favorites()[0]
    assert entry["meta"]["artists"] == "Artist"
    assert entry["meta"]["cover_url"] == "http://x/cover.jpg"
    assert isinstance(entry["added"], float)

    assert library.set_favorite("online:5", False) is False
    assert library.favorite_keys() == []
    # Re-adding keeps the newest metadata
    library.set_favorite("online:5", True, {"name": "Renamed"})
    assert library.favorites()[0]["meta"]["name"] == "Renamed"


def test_playback_session_round_trip(isolated_home):
    library = get_library()
    assert library.playback_state() is None

    state = {"queue": [{"key": "online:1"}, {"key": "online:2"}], "index": 1,
             "position": 12.5, "mode": "shuffle", "source": {"kind": "playlist", "id": "42"}}
    library.save_playback(state)
    stored = library.playback_state()

    assert stored["index"] == 1
    assert stored["position"] == 12.5
    assert stored["mode"] == "shuffle"
    assert stored["source"] == {"kind": "playlist", "id": "42"}
    assert stored["saved_at"] > 0

    library.clear_playback()
    assert library.playback_state() is None


def test_reset_play_counts_keeps_favourites(isolated_home):
    library = get_library()
    library.record_play("online:1")
    library.set_favorite("online:1", True, {"name": "One"})

    library.reset_play_counts()

    assert library.play_counts() == {}
    assert library.stats()["total_plays"] == 0
    assert library.favorite_keys() == ["online:1"]


def test_corrupt_library_degrades_to_defaults(isolated_home):
    library = get_library()
    library.record_play("online:1")
    library.save(force=True)

    library.path.write_text("{ not json", encoding="utf-8")
    library.load()

    assert library.play_counts() == {}
    assert library.stats()["favorites"] == 0


def test_unknown_entries_are_filtered_on_load(isolated_home):
    library = get_library()
    library.path.write_text(json.dumps({
        "version": 1,
        "play_counts": {"online:1": 4, "": 9, "online:2": "not-a-number"},
        "favorites": [{"key": "online:1", "meta": {"name": "One"}}, {"nope": True}],
        "recent": [{"key": ""}],
        "playback": {"queue": []},
    }), encoding="utf-8")
    library.load()

    assert library.play_counts() == {"online:1": 4}
    assert library.favorite_keys() == ["online:1"]
    assert library.recent() == []
    # An empty queue is not a resumable session
    assert library.playback_state() is None


def test_writes_are_atomic_and_debounced(isolated_home):
    library = get_library()
    library.record_play("online:1")
    library.record_play("online:1")

    # Debounced: the file is not written on every single play...
    library.save(force=True)
    payload = json.loads(library.path.read_text(encoding="utf-8"))
    assert payload["play_counts"]["online:1"] == 2
    assert not library.path.with_name(library.path.name + ".part").exists()

    # ...and a later forced save is a real rewrite, not an append.
    library.record_play("online:1")
    library.save(force=True)
    assert json.loads(library.path.read_text(encoding="utf-8"))["play_counts"]["online:1"] == 3


def test_play_counts_are_capped(monkeypatch):
    """The counted set stays bounded, and the loudest tracks keep their numbers."""
    import src.core.library as module

    monkeypatch.setattr(module, "MAX_PLAY_COUNTS", 3)
    library = get_library()
    for _ in range(5):
        library.record_play("online:loud")
    for index in range(4):
        library.record_play(f"online:{index}")

    counts = library.play_counts()

    assert len(counts) == 3
    assert counts["online:loud"] == 5                # the most played survives


def test_a_just_played_track_keeps_its_counter(monkeypatch):
    """A new track must not lose its count the moment it is counted."""
    import src.core.library as module

    monkeypatch.setattr(module, "MAX_PLAY_COUNTS", 2)
    library = get_library()
    for _ in range(3):
        library.record_play("online:a")
    for _ in range(3):
        library.record_play("online:b")
    assert library.play_count("online:b") == 3

    library.record_play("online:c")                  # evicts one of a/b

    assert library.play_count("online:c") == 1
    assert len(library.play_counts()) == 2


def test_unchanged_session_is_not_rewritten(isolated_home):
    library = get_library()
    queue = [{"key": "online:1"}, {"key": "online:2"}]
    library.save_playback({"queue": queue, "index": 0, "position": 10.0})
    library.save(force=True)
    before = library.path.read_text(encoding="utf-8")

    # Same queue/index, a fraction of a second later: nothing to write.
    library.save_playback({"queue": queue, "index": 0, "position": 10.4})
    assert library._dirty is False

    library.save_playback({"queue": queue, "index": 0, "position": 25.0})
    assert library._dirty is True
    library.save(force=True)
    assert library.playback_state()["position"] == 25.0
    assert library.path.read_text(encoding="utf-8") != before


def test_a_failed_write_is_retried(isolated_home, monkeypatch):
    """A write that blows up has to stay pending, not disappear."""
    library = get_library()
    library.record_play("online:1")
    library.save(force=True)
    assert library._dirty is False

    broken = Path("/nonexistent-root/library.json")
    monkeypatch.setattr(type(library), "path", property(lambda self: broken))
    library.record_play("online:2")
    library.save(force=True)                 # fails, but keeps the change

    assert library._dirty is True
    assert library.play_count("online:2") == 1

    monkeypatch.undo()
    library.save(force=True)                 # now it lands on disk

    assert library._dirty is False
    assert json.loads(library.path.read_text(encoding="utf-8"))["play_counts"]["online:2"] == 1
