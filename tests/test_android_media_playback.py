"""Playing a song that lives in the phone's media library.

On Android the songs are not files: they are rows in MediaStore, and the
only way to reach one is through its ``content://`` uri.  Two things have
to work for the local music page to be more than a list of names:

* the media server has to serve those bytes (it serves files, not uris),
* the bridge has to recognise a uri and hand out that url instead of
  the file url it would build for a path on disk.

Both are covered here.  The Java side is stubbed -- the point is the
decision the Python makes, not the query the system answers.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.mediaserver import MediaServer  # noqa: E402
from src.gui.bridge import GuiBridge  # noqa: E402

AUDIO_URI = "content://media/external/audio/media/42"


# --------------------------------------------------------------- the uri gate

def test_only_the_audio_collection_is_allowed():
    """The loopback server must not become a reader for the whole phone."""
    server = MediaServer()

    assert server.is_media_uri(AUDIO_URI)
    assert server.is_media_uri("content://media/external/audio/media/7")

    # Another collection, another provider, a file: all refused.
    assert not server.is_media_uri("content://media/external/video/media/42")
    assert not server.is_media_uri("content://com.android.contacts/data/42")
    assert not server.is_media_uri("file:///data/data/com.example/secret")
    assert not server.is_media_uri("/data/data/com.example/secret")
    assert not server.is_media_uri("")

    # The id is the row number: anything else is not a media entry.
    assert not server.is_media_uri("content://media/external/audio/media/abc")
    assert not server.is_media_uri("content://media/external/audio/media/")
    assert not server.is_media_uri("content://media/external/audio/media/42/other")


def test_the_media_url_carries_the_uri():
    server = MediaServer()
    url = server.url_for_content(AUDIO_URI)
    assert url.startswith(server.base_url)
    assert "content?u=" in url
    # The uri is percent-encoded, so it survives the query string.
    assert "content%3A%2F%2Fmedia" in url


def test_the_media_url_is_built_without_a_running_server():
    """The url is a string; the server does not have to be up yet."""
    server = MediaServer()
    server.token = "test-token"
    server.port = 1234
    url = server.url_for_content(AUDIO_URI)
    assert url == ("http://127.0.0.1:1234/m/test-token/content?u="
                   "content%3A%2F%2Fmedia%2Fexternal%2Faudio%2Fmedia%2F42")


# ----------------------------------------------------------- the bridge side

def _bridge_with_local(track: dict) -> GuiBridge:
    """A bridge whose local library knows exactly one track."""
    bridge = GuiBridge.__new__(GuiBridge)

    class _Library:
        def tracks(self):
            return [track]

        def track(self, path):
            return track if path == track.get("path") else None

        def refresh(self, *args, **kwargs):
            return 1

        def stats(self):
            return {}

    bridge.local_music = _Library()  # type: ignore[attr-defined]
    bridge.settings = type("S", (), {
        "playback": type("P", (), {
            "prefer_online": False,
            "online_quality": "standard",
        })(),
    })()
    bridge.media = MediaServer()
    bridge.media.token = "tok"
    bridge.media.port = 4321
    return bridge


def test_a_media_library_song_is_resolved_to_the_media_url():
    bridge = _bridge_with_local({
        "key": f"local:{AUDIO_URI}",
        "source": "local",
        "name": "Song",
        "artists": "Artist",
        "album": "Album",
        "duration": 1000,
        "path": AUDIO_URI,
    })

    result = bridge.resolve_track({
        "source": "local",
        "path": AUDIO_URI,
        "id": AUDIO_URI,
        "name": "Song",
        "artists": "Artist",
        "album": "Album",
        "duration": 1000,
    })

    assert result["success"] is True
    assert result["kind"] == "local"
    assert result["url"] == (
        "http://127.0.0.1:4321/m/tok/content?u="
        "content%3A%2F%2Fmedia%2Fexternal%2Faudio%2Fmedia%2F42")
    assert result["path"] == AUDIO_URI
    assert result["duration"] == 1000


def test_a_media_library_song_is_not_mistaken_for_a_file():
    """The uri must not go through the file route."""
    bridge = _bridge_with_local({
        "key": f"local:{AUDIO_URI}",
        "source": "local",
        "name": "Song",
        "path": AUDIO_URI,
    })

    result = bridge.resolve_track({"source": "local", "path": AUDIO_URI})
    assert "/file?p=" not in result["url"]
    assert "/art?p=" not in result.get("cover_url", "")
