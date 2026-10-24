"""A persistent, on-disk cache for the data that rarely changes.

The app used to re-fetch song metadata, lyrics and playlist listings on
every launch, which is both slow and rude to the server.  This module keeps
them on disk so the second start is instant and the server sees far fewer
requests.

Three kinds of entry, with different rules:

============  ==========================  ===========================
Kind          What it holds                When it is refreshed
============  ==========================  ===========================
``meta``      song / album / artist info  after 7 days
``lyric``     a song's lyrics (+trans.)   after 30 days
``playlist`` a playlist's listing        after 10 minutes
============  ==========================  ===========================

Everything is stored under ``<config dir>/cache/meta/`` as one small JSON
file per entry, so the cache is inspectable, survives a restart, and can be
wiped without touching the audio or cover caches.

The cache is a **cache**, never a source of truth: every reader treats a
miss as "ask the server", and a corrupt or partial file is discarded rather
than raised.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ncm.metacache")

__all__ = ["MetaCache", "get_meta_cache"]

#: How long each kind of entry stays valid, in seconds.
TTL_META = 7 * 24 * 60 * 60        # 7 days
TTL_LYRIC = 30 * 24 * 60 * 60      # 30 days
TTL_PLAYLIST = 10 * 60             # 10 minutes

#: The largest single entry we keep.  A pathological response (a playlist
#: with 50 000 tracks) would otherwise write a huge file.
MAX_ENTRY_BYTES = 4 * 1024 * 1024

#: How many entries of one kind we keep.  Beyond that the oldest is dropped.
MAX_ENTRIES = 2000

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str) -> str:
    """A filesystem-safe, collision-resistant key."""
    cleaned = _SAFE.sub("_", str(text or ""))[:80]
    digest = hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:12]
    return f"{cleaned}_{digest}" if cleaned else digest


class MetaCache:
    """Disk-backed cache for metadata, lyrics and playlist listings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._root: Path | None = None
        self._available = False

    # ------------------------------------------------------------------ paths
    @property
    def root(self) -> Path:
        """Where the cache files live.

        Resolved from the *current* settings every time, so a test that
        moves the config directory (or a user who changes it) is honoured
        instead of writing into a stale location.
        """
        from src.config import get_settings

        root = get_settings().config_path.parent / "cache" / "meta"
        if root != self._root:
            self._root = root
            self._available = False
        return self._root

    def _ensure_root(self) -> None:
        if self._available:
            return
        try:
            for kind in ("meta", "lyric", "playlist"):
                (self.root / kind).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create the metadata cache at %s: %s", self.root, exc)
            return
        self._available = True

    def _path(self, kind: str, key: str) -> Path:
        return self.root / kind / f"{_slug(key)}.json"

    # ------------------------------------------------------------------- read
    def get(self, kind: str, key: str) -> Any | None:
        """The cached value, or ``None`` when it is missing or expired."""
        path = self._path(kind, key)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            # A truncated file (the app was killed mid-write): drop it.
            self.discard(kind, key)
            return None
        if not isinstance(entry, dict):
            return None
        expires = float(entry.get("expires") or 0)
        if expires and expires <= time.time():
            return None
        return entry.get("value")

    # ------------------------------------------------------------------ write
    def set(self, kind: str, key: str, value: Any, ttl: float) -> None:
        """Store a value.  Failures are ignored: the cache is optional."""
        if not value:
            return
        self._ensure_root()
        if not self._available:
            return
        try:
            payload = json.dumps(
                {"key": str(key), "stored": time.time(), "expires": time.time() + ttl,
                 "value": value},
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            return
        if len(payload.encode("utf-8")) > MAX_ENTRY_BYTES:
            return
        path = self._path(kind, key)
        try:
            # Write to a temp file and rename: a crash mid-write must not
            # leave a half-written entry that fails to parse.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.debug("cannot write the cache entry %s: %s", key, exc)
            return
        self._prune(kind)

    # ---------------------------------------------------------------- discard
    def discard(self, kind: str, key: str) -> None:
        try:
            self._path(kind, key).unlink()
        except OSError:
            pass

    def discard_prefix(self, kind: str, prefix: str) -> int:
        """Drop every entry of ``kind`` whose key starts with ``prefix``.

        The keys are hashed, so the prefix cannot be matched on disk.  The
        stored key is kept inside the file, which makes this a read of every
        entry -- acceptable, because invalidation happens after a write and
        the folders hold at most :data:`MAX_ENTRIES` files.
        """
        prefix = str(prefix or "")
        if not prefix:
            return 0
        folder = self.root / kind
        removed = 0
        try:
            entries = list(folder.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                raw = entry.read_text(encoding="utf-8")
                stored = json.loads(raw)
                key = str(stored.get("key") or "")
            except (OSError, UnicodeDecodeError, ValueError):
                # A file we cannot read is a file we cannot trust either.
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    pass
                continue
            if key.startswith(prefix):
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def clear(self, kind: str | None = None) -> int:
        """Drop cached entries.  Returns how many files went."""
        removed = 0
        kinds = [kind] if kind else ("meta", "lyric", "playlist")
        for one in kinds:
            folder = self.root / one
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                try:
                    if entry.is_file():
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
        return removed

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        """Occupancy per kind, for the settings panel."""
        result: dict[str, Any] = {"available": False, "root": str(self.root)}
        for kind in ("meta", "lyric", "playlist"):
            folder = self.root / kind
            count = 0
            size = 0
            if folder.is_dir():
                for entry in folder.iterdir():
                    try:
                        if entry.is_file():
                            count += 1
                            size += entry.stat().st_size
                    except OSError:
                        continue
            result[kind] = {"count": count, "bytes": size}
        result["available"] = self._available
        return result

    def _prune(self, kind: str) -> None:
        """Keep the folder under :data:`MAX_ENTRIES`."""
        folder = self.root / kind
        try:
            entries = [p for p in folder.iterdir() if p.is_file()]
        except OSError:
            return
        if len(entries) <= MAX_ENTRIES:
            return
        entries.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
        for path in entries[:len(entries) - MAX_ENTRIES]:
            try:
                path.unlink()
            except OSError:
                continue

    # ------------------------------------------------------- typed helpers
    def get_meta(self, key: str) -> Any | None:
        return self.get("meta", key)

    def set_meta(self, key: str, value: Any) -> None:
        self.set("meta", key, value, TTL_META)

    def get_lyric(self, key: str) -> Any | None:
        return self.get("lyric", key)

    def set_lyric(self, key: str, value: Any) -> None:
        self.set("lyric", key, value, TTL_LYRIC)

    def get_playlist(self, key: str) -> Any | None:
        return self.get("playlist", key)

    def set_playlist(self, key: str, value: Any) -> None:
        self.set("playlist", key, value, TTL_PLAYLIST)


_instance: MetaCache | None = None
_instance_lock = threading.Lock()


def get_meta_cache() -> MetaCache:
    """The process-wide metadata cache."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = MetaCache()
    return _instance
