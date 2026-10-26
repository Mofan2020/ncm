"""Tests for the artist endpoints (no network).

The route shapes were verified live on 2026-10-26; these tests pin the
mapping from the API answer to the shape the GUI and the Android UI expect.
"""
from __future__ import annotations

from src.core.api import ARTIST_SONGS_ENDPOINT, NeteaseAPI


class _FakeApi(NeteaseAPI):
    """A NeteaseAPI whose eapi layer answers from a table."""

    def __init__(self, answers: dict[str, dict]) -> None:  # noqa: D107
        # Deliberately not calling super().__init__: the singleton would
        # touch the real session and the settings.
        self._answers = answers
        self.calls: list[tuple[str, dict]] = []

    def _eapi(self, path, data=None, use_cache=True):  # noqa: D401, ANN001
        self.calls.append((path, dict(data or {})))
        return self._answers.get(path)


def test_artist_songs_returns_the_paged_shape():
    """The songs route answers ``songs`` / ``total`` / ``more``."""
    api = _FakeApi({
        ARTIST_SONGS_ENDPOINT: {
            "code": 200,
            "total": 580,
            "more": True,
            "songs": [{"id": 1, "name": "One", "artists": [{"name": "A"}],
                       "album": {"name": "B"}, "duration": 1000}],
        },
    })
    result = api.get_artist_songs("6452", limit=10, offset=20)
    assert result["total"] == 580
    assert result["more"] is True
    assert len(result["songs"]) == 1
    assert result["songs"][0]["name"] == "One"


def test_artist_songs_sends_the_offset_and_the_limit():
    """Paging has to reach the server, or the list never grows."""
    api = _FakeApi({ARTIST_SONGS_ENDPOINT: {"code": 200, "songs": []}})
    api.get_artist_songs("6452", limit=25, offset=50)
    path, payload = api.calls[0]
    assert path == ARTIST_SONGS_ENDPOINT
    assert payload["id"] == "6452"
    assert payload["limit"] == 25
    assert payload["offset"] == 50


def test_artist_songs_clamps_the_limit():
    """The server rejects a huge limit; clamp before sending it."""
    api = _FakeApi({ARTIST_SONGS_ENDPOINT: {"code": 200, "songs": []}})
    api.get_artist_songs("6452", limit=9999)
    assert api.calls[0][1]["limit"] == 100


def test_artist_songs_without_an_id_never_calls_the_server():
    api = _FakeApi({})
    result = api.get_artist_songs("")
    assert result["songs"] == []
    assert result["total"] == 0
    assert api.calls == []


def test_artist_songs_survives_a_malformed_answer():
    """A transport failure must not raise; the caller shows an empty list."""
    api = _FakeApi({ARTIST_SONGS_ENDPOINT: {"code": 200, "songs": None}})
    result = api.get_artist_songs("6452")
    assert result["songs"] == []
    assert result["more"] is False


def test_artist_songs_keeps_the_full_field_names():
    """This route uses ``artists``/``album``/``duration``, not ``ar``/``al``.

    The GUI and the Android UI both flatten the song, so the shape has to
    survive the trip.
    """
    api = _FakeApi({
        ARTIST_SONGS_ENDPOINT: {
            "code": 200,
            "songs": [{"id": 1, "name": "Song",
                       "artists": [{"name": "A"}, {"name": "B"}],
                       "album": {"name": "Album", "picUrl": "http://x/y.jpg"},
                       "duration": 319039, "fee": 1}],
        },
    })
    song = api.get_artist_songs("6452")["songs"][0]
    assert [a["name"] for a in song["artists"]] == ["A", "B"]
    assert song["album"]["name"] == "Album"
    assert song["duration"] == 319039
