"""The phone's music library, read through Android's MediaStore.

The desktop scans the configured folders for audio files.  Android 10 and
later hand an app no blanket access to /sdcard/Music, so that scan finds
nothing there.  MediaStore is the index the system keeps anyway: query it
for the audio files and read them through their content URI.

The interface is the one the controller already expects -- ``tracks()``
returns the same dictionaries as the desktop's ``LocalMusicLibrary`` -- so
the caller does not have to know which one it is talking to.

Everything that touches Java lives in :meth:`MediaStoreLibrary._query`; the
query itself is :meth:`MediaStoreLibrary._read`, which takes the Java
objects as arguments and can therefore be tested with fakes.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("ncm.kivy.mediastore")

__all__ = ["MediaStoreLibrary", "is_android"]

#: The MediaStore columns the library is built from.
COLUMNS = ("_id", "title", "artist", "album", "duration", "size",
           "_display_name")


def is_android() -> bool:
    """Whether this runs on Android."""
    import sys

    return hasattr(sys, "getandroidapilevel") or "ANDROID_ARGUMENT" in __import__(
        "os").environ


def _haystack(track: dict[str, Any]) -> str:
    return " ".join(str(track.get(key) or "")
                    for key in ("name", "artists", "album")).lower()


class MediaStoreLibrary:
    """The music on the phone, listed through MediaStore."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracks: list[dict[str, Any]] = []
        self._loaded = False
        self._error = ""

    # ------------------------------------------------------------- the query
    def _query(self, resolver: Any) -> list[dict[str, Any]]:
        """Ask MediaStore for the audio files."""
        from jnius import autoclass

        ContentUris = autoclass("android.content.ContentUris")
        MediaStore = autoclass("android.provider.MediaStore")
        return self._read(resolver, MediaStore.Audio.Media, ContentUris)

    def _read(self, resolver: Any, audio: Any, content_uris: Any) -> list[dict[str, Any]]:
        """Build the track list from a MediaStore cursor."""
        projection = [getattr(audio, name.upper(), None) for name in COLUMNS]
        projection = [item for item in projection if item is not None]
        cursor = resolver.query(audio.EXTERNAL_CONTENT_URI, projection,
                                None, None, audio.TITLE + " ASC")
        tracks: list[dict[str, Any]] = []
        if cursor is None:
            return tracks
        try:
            index = {name: cursor.getColumnIndex(name) for name in COLUMNS}
            while cursor.moveToNext():
                media_id = cursor.getLong(index["_id"])
                uri = content_uris.withAppendedId(audio.EXTERNAL_CONTENT_URI,
                                                   media_id)
                display = self._string(cursor, index["_display_name"])
                tracks.append({
                    "key": f"local:{uri}",
                    "source": "local",
                    "name": self._string(cursor, index["title"]) or display,
                    "artists": self._string(cursor, index["artist"]),
                    "album": self._string(cursor, index["album"]),
                    "duration": self._long(cursor, index["duration"]),
                    "path": str(uri),
                    "ext": display.rsplit(".", 1)[-1].lower() if "." in display else "",
                    "size": self._long(cursor, index["size"]),
                    "has_lyrics": False,
                })
        finally:
            cursor.close()
        return tracks

    @staticmethod
    def _string(cursor: Any, column: int) -> str:
        if column is None or column < 0:
            return ""
        value = cursor.getString(column)
        return "" if value is None else str(value)

    @staticmethod
    def _long(cursor: Any, column: int) -> int:
        if column is None or column < 0:
            return 0
        value = cursor.getLong(column)
        return int(value or 0)

    # ------------------------------------------------------------ the library
    def refresh(self, directories: list[str] | None = None, *,
                force: bool = False) -> int:
        """Read the library.  Returns how many tracks were found.

        A phone without the audio permission answers with an empty list and
        an error message; that is not a crash, the screen shows its empty
        state.
        """
        try:
            from android import activity

            resolver = activity.getActivity().getContentResolver()
            tracks = self._query(resolver)
        except Exception as exc:
            log.warning("MediaStore is not available: %s", exc)
            with self._lock:
                self._tracks = []
                self._loaded = True
                self._error = str(exc)
            return 0
        with self._lock:
            self._tracks = tracks
            self._loaded = True
            self._error = ""
        return len(tracks)

    def tracks(self) -> list[dict[str, Any]]:
        if not self._loaded:
            self.refresh()
        with self._lock:
            return [dict(track) for track in self._tracks]

    def track(self, path: str) -> dict[str, Any] | None:
        """One track, looked up by its key or by its path."""
        wanted = str(path)
        for track in self.tracks():
            if wanted in (track.get("key"), track.get("path")):
                return track
            if track.get("key") == f"local:{wanted}":
                return track
        return None

    def get_tracks(self, query: str = "", limit: int = 200,
                   offset: int = 0) -> list[dict[str, Any]]:
        """The tracks the controller asks for."""
        tracks = self.tracks()
        if query:
            needle = query.strip().lower()
            tracks = [track for track in tracks if needle in _haystack(track)]
        if offset:
            tracks = tracks[offset:]
        if limit:
            tracks = tracks[:limit]
        return tracks

    @property
    def error(self) -> str:
        """Why the last query failed, if it did."""
        return self._error
