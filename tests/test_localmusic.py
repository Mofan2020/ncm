"""Scanning a local music folder and reading tags without mutagen."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_fixtures import build_flac, build_mp3, build_unknown  # noqa: E402

from src.core.localmusic import (  # noqa: E402
    LocalMusicLibrary,
    get_local_music,
    guess_from_filename,
    read_cover,
    read_embedded_lyrics,
    read_metadata,
    sidecar_lyrics_path,
)


def test_guess_from_filename():
    assert guess_from_filename("01 - Artist - Title") == ("Artist", "Title")
    assert guess_from_filename("Artist - Title") == ("Artist", "Title")
    assert guess_from_filename("Artist – Title") == ("Artist", "Title")   # en dash
    assert guess_from_filename("Title") == ("", "Title")
    assert guess_from_filename("02. Artist - Title") == ("Artist", "Title")
    assert guess_from_filename("") == ("", "")


def test_reads_id3v2_tags(tmp_path):
    path = build_mp3(tmp_path / "song.mp3", title="夜曲", artist="周杰伦",
                     album="十一月的萧邦")

    tags = read_metadata(path)

    assert tags["title"] == "夜曲"
    assert tags["artist"] == "周杰伦"
    assert tags["album"] == "十一月的萧邦"
    assert tags["duration_ms"] > 2000


def test_reads_lyrics_only_when_asked(tmp_path):
    path = build_mp3(tmp_path / "song.mp3", title="T", lyrics="[00:01.00]line one")

    assert "lyrics" not in read_metadata(path)          # opt-in, and expensive
    assert read_embedded_lyrics(path) == "[00:01.00]line one"


def test_reads_flac_vorbis_comments(tmp_path):
    path = build_flac(tmp_path / "song.flac", title="Song", artist="Artist",
                      album="Album", seconds=4)

    tags = read_metadata(path)

    assert (tags["title"], tags["artist"], tags["album"]) == ("Song", "Artist", "Album")
    assert 3900 <= tags["duration_ms"] <= 4100


def test_unparsable_file_is_not_fatal(tmp_path):
    path = build_unknown(tmp_path / "broken.mp3")

    assert read_metadata(path) == {}
    assert read_embedded_lyrics(path) == ""
    assert read_cover(path) is None
    assert read_metadata(tmp_path / "missing.mp3") == {}


def test_sidecar_lyrics_lookup(tmp_path):
    audio = build_mp3(tmp_path / "song.mp3", title="T")
    assert sidecar_lyrics_path(audio) is None

    sidecar = tmp_path / "song.lrc"
    sidecar.write_text("[00:01.00]hi", encoding="utf-8")
    assert sidecar_lyrics_path(audio) == sidecar

    # An upper-case extension is found too.  macOS filesystems are usually
    # case-insensitive, so compare the resolved file instead of the spelling.
    sidecar.rename(tmp_path / "song.LRC")
    found = sidecar_lyrics_path(audio)
    assert found is not None
    assert found.exists()
    assert found.name.lower() == "song.lrc"
    assert found.read_text(encoding="utf-8") == "[00:01.00]hi"


def test_refresh_indexes_the_folder(tmp_path):
    folder = tmp_path / "music"
    build_mp3(folder / "a.mp3", title="Alpha", artist="A")
    build_mp3(folder / "sub" / "b.mp3", title="Beta", artist="B")
    build_unknown(folder / "notes.txt")
    (folder / "cover.jpg").write_bytes(b"\xff\xd8\xff")

    library = get_local_music()
    summary = library.refresh([str(folder)])

    assert summary["tracks"] == 2
    tracks = library.tracks()
    names = sorted(track["name"] for track in tracks)
    assert names == ["Alpha", "Beta"]
    assert all(track["source"] == "local" and track["key"].startswith("local:") for track in tracks)
    assert library.stats()["tracks"] == 2


def test_depth_limit_and_removal(tmp_path):
    folder = tmp_path / "music"
    build_mp3(folder / "top.mp3", title="Top")
    build_mp3(folder / "one" / "two" / "three" / "deep.mp3", title="Deep")

    library = get_local_music()
    shallow = library.refresh([str(folder)], max_depth=1)
    assert shallow["tracks"] == 1

    deep = library.refresh([str(folder)], max_depth=4)
    assert deep["tracks"] == 2

    (folder / "top.mp3").unlink()
    after = library.refresh([str(folder)], max_depth=4)
    assert after["tracks"] == 1
    assert [track["name"] for track in library.tracks()] == ["Deep"]


def test_filename_fallback_and_lyrics_flag(tmp_path):
    folder = tmp_path / "music"
    path = build_mp3(folder / "01 - Some Artist - Some Title.mp3")   # no tags at all
    (folder / "01 - Some Artist - Some Title.lrc").write_text("[00:01.00]x", encoding="utf-8")

    library = get_local_music()
    library.refresh([str(folder)])
    track = library.tracks()[0]

    assert track["name"] == "Some Title"
    assert track["artists"] == "Some Artist"
    assert track["has_lyrics"] is True
    assert track["path"] == str(path)

    assert library.track(track["key"]) is not None
    assert library.track(str(path))["name"] == "Some Title"
    assert library.track("/nope.mp3") is None


def test_tag_cache_is_reused_between_scans(tmp_path, monkeypatch):
    """A rescan of unchanged files must not parse a single tag again."""
    import src.core.localmusic as module

    folder = tmp_path / "music"
    for index in range(3):
        build_mp3(folder / f"{index}.mp3", title=f"Track {index}", artist="A")

    library = get_local_music()
    library.refresh([str(folder)])

    calls = []
    original = module.read_metadata

    def counting_read_metadata(path, *args, **kwargs):
        calls.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module, "read_metadata", counting_read_metadata)
    again = library.refresh([str(folder)])

    assert again["tracks"] == 3
    assert calls == []                                  # served from the cache
    assert library.stats()["cached_files"] == 3

    # A changed file is re-read, and only that one.
    changed = folder / "1.mp3"
    build_mp3(changed, title="Renamed", artist="A")
    library.refresh([str(folder)])

    assert calls == [str(changed)]


def test_cache_drops_deleted_files(tmp_path):
    folder = tmp_path / "music"
    build_mp3(folder / "a.mp3", title="Alpha")
    keep = build_mp3(folder / "b.mp3", title="Beta")

    library = get_local_music()
    library.refresh([str(folder)])
    assert library.stats()["cached_files"] == 2

    (folder / "a.mp3").unlink()
    library.refresh([str(folder)])

    assert library.stats()["cached_files"] == 1
    assert [t["name"] for t in library.tracks()] == ["Beta"]
    assert library.track(str(keep))["name"] == "Beta"


def test_state_survives_a_restart(tmp_path):
    folder = tmp_path / "music"
    build_mp3(folder / "a.mp3", title="Alpha", artist="A")

    library = get_local_music()
    library.refresh([str(folder)])

    LocalMusicLibrary._instance = None
    import src.core.localmusic as module

    module._local_music = None
    reloaded = get_local_music()

    tracks = reloaded.tracks()
    assert len(tracks) == 1
    assert tracks[0]["name"] == "Alpha"


def test_clear_empties_everything(tmp_path):
    folder = tmp_path / "music"
    build_mp3(folder / "a.mp3", title="Alpha")
    library = get_local_music()
    library.refresh([str(folder)])

    library.clear()

    assert library.tracks() == []
    assert library.stats()["cached_files"] == 0
