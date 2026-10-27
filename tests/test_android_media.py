"""The Android local music library: MediaStore instead of a folder scan.

The desktop's folder scan finds nothing on Android 10 and later, so the
phone's music comes from MediaStore.  The query needs Java, so it is split
in two: the glue that reaches the content resolver, and the part that
turns a cursor into track dictionaries -- the second one is what these
tests cover.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "android") not in sys.path:
    sys.path.insert(0, str(ROOT / "android"))

from ncmkivy.mediastore import MediaStoreLibrary, is_android  # noqa: E402


class FakeCursor:
    """A MediaStore cursor with a fixed set of rows."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self._at = -1
        self.closed = False

    def moveToNext(self) -> bool:
        self._at += 1
        return self._at < len(self.rows)

    def getColumnIndex(self, name: str) -> int:
        return list((" _id title artist album duration size _display_name")
                    .split()).index(name)

    def getString(self, column: int) -> str | None:
        row = self.rows[self._at]
        return row.get((" _id title artist album duration size _display_name"
                        ).split()[column])  # type: ignore[arg-type]

    def getLong(self, column: int) -> int:
        value = self.getString(column)
        return int(value or 0) if value is not None else 0

    def close(self) -> None:
        self.closed = True


class FakeResolver:
    def __init__(self, cursor: FakeCursor | None) -> None:
        self.cursor = cursor
        self.queries: list[object] = []

    def query(self, uri, projection, selection, args, order) -> FakeCursor | None:
        self.queries.append(uri)
        return self.cursor


class FakeAudio:
    """The MediaStore.Audio.Media constants the query uses."""

    EXTERNAL_CONTENT_URI = "content://media/external/audio/media"
    TITLE = "title"
    _ID = "_id"
    ARTIST = "artist"
    ALBUM = "album"
    DURATION = "duration"
    SIZE = "size"
    DISPLAY_NAME = "_display_name"


class FakeContentUris:
    @staticmethod
    def withAppendedId(uri: str, media_id: int) -> str:
        return f"{uri}/{media_id}"


def _library(cursor: FakeCursor | None) -> MediaStoreLibrary:
    library = MediaStoreLibrary()
    library._tracks = library._read(FakeResolver(cursor), FakeAudio, FakeContentUris)
    library._loaded = True
    return library


def test_the_tracks_come_back_with_the_fields_the_ui_expects():
    cursor = FakeCursor([
        {"_id": "7", "title": "Shots", "artist": "JAY PARK", "album": "New Breed",
         "duration": "213000", "size": "5120000", "_display_name": "shots.mp3"},
    ])
    library = _library(cursor)

    tracks = library.tracks()
    assert len(tracks) == 1
    track = tracks[0]
    assert track["name"] == "Shots"
    assert track["artists"] == "JAY PARK"
    assert track["album"] == "New Breed"
    assert track["duration"] == 213000
    assert track["source"] == "local"
    assert track["ext"] == "mp3"
    assert track["path"].endswith("/7")


def test_a_phone_without_music_reports_nothing_and_no_crash():
    library = MediaStoreLibrary()
    assert library.tracks() == []
    assert library.error, "the failure has to be visible"


def test_the_query_looks_at_the_audio_collection():
    cursor = FakeCursor([])
    resolver = FakeResolver(cursor)
    library = MediaStoreLibrary()
    library._read(resolver, FakeAudio, FakeContentUris)
    assert resolver.queries == [FakeAudio.EXTERNAL_CONTENT_URI]
    assert cursor.closed, "the cursor was left open"


def test_the_search_matches_the_title_the_artist_and_the_album():
    cursor = FakeCursor([
        {"_id": "1", "title": "Shots", "artist": "JAY PARK", "album": "New Breed",
         "duration": "1", "size": "1", "_display_name": "a.mp3"},
        {"_id": "2", "title": "Another", "artist": "Someone", "album": "Else",
         "duration": "1", "size": "1", "_display_name": "b.mp3"},
    ])
    library = _library(cursor)

    assert [t["name"] for t in library.get_tracks(query="shots")] == ["Shots"]
    assert [t["name"] for t in library.get_tracks(query="jay")] == ["Shots"]
    assert [t["name"] for t in library.get_tracks(query="else")] == ["Another"]
    assert len(library.get_tracks()) == 2


def test_the_page_window_works():
    cursor = FakeCursor([
        {"_id": str(n), "title": f"Track {n}", "artist": "A", "album": "B",
         "duration": "1", "size": "1", "_display_name": f"{n}.mp3"}
        for n in range(10)
    ])
    library = _library(cursor)

    assert len(library.get_tracks(limit=3)) == 3
    assert library.get_tracks(limit=3)[0]["name"] == "Track 0"
    assert library.get_tracks(limit=3, offset=8)[0]["name"] == "Track 8"


def test_a_single_track_can_be_found_by_its_path():
    cursor = FakeCursor([
        {"_id": "7", "title": "Shots", "artist": "A", "album": "B",
         "duration": "1", "size": "1", "_display_name": "a.mp3"},
    ])
    library = _library(cursor)
    path = library.tracks()[0]["path"]

    assert library.track(path) is not None, "by path"
    assert library.track(library.tracks()[0]["key"]) is not None, "by key"
    assert library.track("content://nowhere") is None


def test_the_desktop_is_not_android():
    assert is_android() is False
