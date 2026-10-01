"""Local library state: play counts, favourites and the last playback session.

Everything the player remembers between launches lives in ``library.json``
next to ``settings.yaml`` (see :mod:`src.config.settings` for the paths)::

    {
      "version": 1,
      "play_counts": {"online:186016": 12, "local:/music/a.mp3": 3},
      "favorites":   [{"key": "online:186016", "meta": {...}, "added": 1690000000}],
      "recent":      [{"key": "...", "meta": {...}, "played": 1690000000}],
      "playback":    {"queue": [...], "index": 0, "position": 42.5, "mode": "list"}
    }

Design notes:

* **One writer, atomic replace.**  The file is rewritten through a ``.part``
  temp file so a crash mid-write cannot truncate the library.
* **Debounced writes.**  The UI pushes the playback position every few seconds;
  writing the whole file each time would hammer the disk for no benefit, so a
  save is coalesced into one write per :data:`SAVE_DEBOUNCE` seconds.
* **Tolerant loading.**  A corrupt or older file degrades to the defaults
  instead of breaking startup -- the library is a convenience, never a blocker.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from src.config import PLAY_MODES, get_settings

__all__ = ["Library", "get_library", "track_key", "split_key", "PLAY_MODES"]

#: How long writes are coalesced (seconds).
SAVE_DEBOUNCE = 2.0

#: Caps so an abused library cannot grow without bound.
MAX_RECENT = 200
MAX_FAVORITES = 5000
#: One small integer per track; the least played entries are dropped first.
MAX_PLAY_COUNTS = 20000

#: Sources a key can come from.
SOURCE_ONLINE = "online"
SOURCE_LOCAL = "local"


def track_key(source: str, identifier: str) -> str:
    """Build the stable key of a track, e.g. ``online:186016``."""
    return f"{source}:{identifier}"


def split_key(key: str) -> tuple[str, str]:
    """Split a track key back into ``(source, identifier)``."""
    source, _, identifier = str(key or "").partition(":")
    return (source or SOURCE_ONLINE), identifier


class Library:
    """Favourites, play counts and the resumable playback session (singleton)."""

    _instance: Library | None = None
    _lock = threading.Lock()

    def __new__(cls) -> Library:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.logger = logging.getLogger("ncm.library")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._empty()
        self._save_timer: threading.Timer | None = None
        self._dirty = False
        #: Bumped on every change so a write that races with a newer one can tell.
        self._revision = 0
        self.load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "play_counts": {},
            "favorites": [],
            "recent": [],
            "playback": None,
        }

    # -------------------------------------------------------------------- I/O
    @property
    def path(self) -> Path:
        """``library.json`` beside ``settings.yaml``."""
        return get_settings().config_path.with_name("library.json")

    def load(self) -> None:
        with self._lock:
            self._data = self._empty()
            try:
                if not self.path.exists():
                    return
                with open(self.path, encoding="utf-8") as handle:
                    raw = json.load(handle)
            except Exception as exc:
                self.logger.warning("cannot read %s: %s", self.path, exc)
                return
            if not isinstance(raw, dict):
                return

            counts = raw.get("play_counts")
            if isinstance(counts, dict):
                self._data["play_counts"] = {
                    str(k): max(0, int(v)) for k, v in counts.items()
                    if str(k).strip() and isinstance(v, int | float) and not isinstance(v, bool)
                }
            favorites = raw.get("favorites")
            if isinstance(favorites, list):
                self._data["favorites"] = [self._clean_entry(item) for item in favorites
                                           if self._clean_entry(item)]
            recent = raw.get("recent")
            if isinstance(recent, list):
                self._data["recent"] = [self._clean_entry(item) for item in recent
                                        if self._clean_entry(item)][:MAX_RECENT]
            playback = raw.get("playback")
            if isinstance(playback, dict):
                queue = playback.get("queue")
                # An empty queue is not a session: nothing to resume.
                if isinstance(queue, list) and queue:
                    self._data["playback"] = playback

    @staticmethod
    def _clean_entry(entry: Any) -> dict[str, Any] | None:
        """Keep only well formed ``{key, meta, added|played}`` entries."""
        if not isinstance(entry, dict) or not entry.get("key"):
            return None
        cleaned = {"key": str(entry["key"])}
        meta = entry.get("meta")
        if isinstance(meta, dict):
            cleaned["meta"] = {str(k): v for k, v in meta.items()}
        for stamp in ("added", "played"):
            if isinstance(entry.get(stamp), int | float):
                cleaned[stamp] = float(entry[stamp])
        return cleaned

    def save(self, force: bool = False) -> None:
        """Write the library, coalescing bursts unless ``force`` is set."""
        with self._lock:
            self._dirty = True
            self._revision += 1
            if not force and self._save_timer is not None:
                return
            if not force:
                timer = threading.Timer(SAVE_DEBOUNCE, self._flush)
                timer.daemon = True
                self._save_timer = timer
                timer.start()
                return
        self._flush()

    def _flush(self) -> None:
        with self._lock:
            self._save_timer = None
            if not self._dirty:
                return
            payload = json.dumps(self._data, ensure_ascii=False)
            revision = self._revision
        try:
            path = self.path
            part = path.with_name(path.name + ".part")
            part.write_text(payload, encoding="utf-8")
            path.unlink(missing_ok=True)
            part.replace(path)
        except Exception as exc:
            # Stay dirty: the next save must retry rather than drop the change.
            self.logger.warning("cannot save the library: %s", exc)
            with self._lock:
                self._dirty = True
            return
        with self._lock:
            # Only clean when nothing changed while the file was being written.
            self._dirty = self._revision != revision

    # ------------------------------------------------------------- play counts
    def play_count(self, key: str) -> int:
        with self._lock:
            return int(self._data["play_counts"].get(key, 0))

    def play_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._data["play_counts"])

    def _trim_play_counts(self, keep_key: str | None = None) -> None:
        """Keep the counted set bounded (called with the lock held).

        The most played keys win, and the track that was just played is always
        kept -- otherwise a big library would reset the count of every new track
        to zero on the next play, which would make the numbers meaningless.
        """
        counts = self._data["play_counts"]
        if len(counts) <= MAX_PLAY_COUNTS:
            return
        kept: dict[str, int] = {}
        if keep_key and keep_key in counts:
            kept[keep_key] = counts[keep_key]
        for key, value in sorted(counts.items(), key=lambda item: item[1], reverse=True):
            if len(kept) >= MAX_PLAY_COUNTS:
                break
            kept.setdefault(key, value)
        self._data["play_counts"] = kept

    def record_play(self, key: str, meta: dict[str, Any] | None = None,
                    stamp: float | None = None) -> int:
        """Count one playback of ``key`` and remember it as recently played."""
        if not key:
            return 0
        with self._lock:
            counts = self._data["play_counts"]
            counts[key] = int(counts.get(key, 0)) + 1
            entry = {"key": key, "played": stamp or time.time()}
            if meta:
                entry["meta"] = {str(k): v for k, v in meta.items()}
            recent = [item for item in self._data["recent"] if item.get("key") != key]
            recent.insert(0, entry)
            self._data["recent"] = recent[:MAX_RECENT]
            self._trim_play_counts(keep_key=key)
            count = counts[key]
        self.save()
        return count

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._data["recent"][:max(1, limit)]]

    def reset_play_counts(self) -> None:
        """Forget every counter (the favourites and the session are untouched)."""
        with self._lock:
            self._data["play_counts"] = {}
            self._data["recent"] = []
        self.save(force=True)

    # -------------------------------------------------------------- favourites
    def favorite_keys(self) -> list[str]:
        with self._lock:
            return [str(item["key"]) for item in self._data["favorites"]]

    def favorites(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._data["favorites"]]

    def is_favorite(self, key: str) -> bool:
        with self._lock:
            return any(item["key"] == key for item in self._data["favorites"])

    def set_favorite(self, key: str, favorite: bool,
                     meta: dict[str, Any] | None = None) -> bool:
        """Add or remove a favourite; returns the resulting state."""
        if not key:
            return False
        with self._lock:
            existing = [item for item in self._data["favorites"] if item["key"] != key]
            if favorite:
                entry: dict[str, Any] = {"key": key, "added": time.time()}
                if meta:
                    entry["meta"] = {str(k): v for k, v in meta.items()}
                self._data["favorites"] = [entry, *existing][:MAX_FAVORITES]
            else:
                self._data["favorites"] = existing
        self.save(force=True)
        return bool(favorite)

    # ----------------------------------------------------------- playback state
    def playback_state(self) -> dict[str, Any] | None:
        with self._lock:
            state = self._data.get("playback")
            return dict(state) if isinstance(state, dict) else None

    def save_playback(self, state: dict[str, Any] | None) -> None:
        """Persist the current queue/index/position (``None`` clears it).

        The timestamp is stamped here so every caller gets one, even though the
        GUI also sends its own ``saved_at``.
        """
        if state is not None and not isinstance(state, dict):
            return
        if isinstance(state, dict):
            state = {**state, "saved_at": float(state.get("saved_at") or time.time())}
        with self._lock:
            current = self._data.get("playback")
            if isinstance(current, dict) and isinstance(state, dict) \
                    and self._same_session(current, state):
                return
            self._data["playback"] = state
        self.save()

    @staticmethod
    def _same_session(old: dict[str, Any], new: dict[str, Any]) -> bool:
        """True when only the playback position moved a fraction of a second."""
        if old.get("index") != new.get("index") or old.get("mode") != new.get("mode"):
            return False
        old_queue = old.get("queue") or []
        new_queue = new.get("queue") or []
        if len(old_queue) != len(new_queue):
            return False
        if any(a.get("key") != b.get("key") for a, b in zip(old_queue, new_queue, strict=True)):
            return False
        try:
            return abs(float(new.get("position") or 0) - float(old.get("position") or 0)) < 1.0
        except (TypeError, ValueError):
            return False

    def clear_playback(self) -> None:
        with self._lock:
            self._data["playback"] = None
        self.save(force=True)

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = self._data["play_counts"]
            return {
                "tracks": len(counts),
                "total_plays": sum(int(v) for v in counts.values()),
                "favorites": len(self._data["favorites"]),
                "recent": len(self._data["recent"]),
            }


def get_library() -> Library:
    """Return the process-wide library store."""
    return Library()
