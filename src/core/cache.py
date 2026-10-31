"""Persistent cache for streamed media (audio and cover art).

Why this exists
---------------

The media server (``src/core/mediaserver.py``) proxies NetEase CDN audio
and serves embedded cover art for local files.  Without a persistent
cache every play of an online track costs another signed URL fetch plus
the audio bytes themselves; the server has to hit the CDN on every seek
and every reload of a long queue.  Every now-and-then a playlist has
several albums in a row -- the same cover art re-downloaded dozens of
times -- which is exactly the workload that makes NetEase rate-limit us.

Layout
------

The cache lives next to ``settings.yaml``::

    <config_dir>/cache/
        audio/<song_id>/<quality>.<ext>     -- whole-song audio bytes
        cover/<url-hash>.<ext>              -- remote cover art

Why ``song_id`` and not the signed URL: URLs expire after ``URL_TTL``
seconds (10 minutes by default), but the song id is permanent.  Keying on
the id means re-opening the app does not throw the whole cache away.

Why ``quality`` for audio: a hires cache hit must not serve a standard
stream to the user (or vice versa).  Different qualities are different
files, deliberately.

Threading
---------

The file system is touched under :data:`_LOCK`.  The LRU eviction runs
inside that lock too, so a concurrent read cannot observe a half-deleted
tree.  Read/write helpers return ``None`` / ``False`` rather than raising
when the cache is unavailable (disk full, permission denied) -- a cache
miss is harmless, a cache-induced crash is not.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

__all__ = ["MediaCache", "get_media_cache"]

#: Keep the audio cache under this many bytes by default.  ``0`` disables
#: the budget (useful for tests and for users who want unbounded cache).
DEFAULT_AUDIO_BUDGET = 2 * 1024 * 1024 * 1024  # 2 GB

#: Cover art is small but full-resig; 256 MB holds thousands of covers.
DEFAULT_COVER_BUDGET = 256 * 1024 * 1024  # 256 MB

#: Stop accepting new audio writes once a single file is bigger than this;
#: huge files usually mean a cost or are a download gone sideways.
NEXT_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

#: Cover art entries bigger than this are rejected up front -- the player
#: only ever draws small thumbnails, so a 20 MB PNG is never useful.
NEXT_COVER_BYTES = 8 * 1024 * 1024  # 8 MB

log = logging.getLogger("ncm.cache")


class _AudioWriter:
    """Streaming writer for one cached audio file.

    The bytes land in a ``.part`` file next to the target and are moved
    into place by :meth:`close` once the stream completed.  An aborted
    stream leaves the ``.part`` file behind and removes it, so a half
    song is never served as if it were the whole one.

    Used as a context manager is not required -- :meth:`close` is
    idempotent and the caller may simply forget it (the ``__del__``
    fallback then discards the partial file).
    """

    def __init__(self, cache: MediaCache, target: Path) -> None:
        self._cache = cache
        self._target = target
        self._part = target.with_suffix(target.suffix + ".part")
        self._handle = None
        self._written = 0
        self._closed = False
        try:
            self._handle = open(self._part, "wb")
        except OSError as exc:
            log.debug("audio cache open failed for %s: %s", target, exc)
            self._handle = None

    @property
    def usable(self) -> bool:
        """Whether the writer actually has a file to write to."""
        return self._handle is not None

    @property
    def written(self) -> int:
        """Bytes handed to the writer so far."""
        return self._written

    def write(self, chunk: bytes) -> None:
        """Append one chunk; a failure disables the cache, not the stream."""
        if self._handle is None or not chunk:
            return
        try:
            self._handle.write(chunk)
            self._written += len(chunk)
        except OSError as exc:
            log.debug("audio cache write failed for %s: %s", self._target, exc)
            self._close_handle()

    def close(self, complete: bool = True) -> None:
        """Finish the file.

        ``complete=False`` means the stream was cut short: the partial file
        is thrown away instead of being served later as a broken song.
        """
        if self._closed:
            return
        self._closed = True
        if self._handle is None:
            return
        self._close_handle()
        if not complete or self._written < 1024:
            self._discard()
            return
        try:
            self._part.replace(self._target)
        except OSError as exc:
            log.debug("audio cache rename failed for %s: %s", self._target, exc)
            self._discard()
            return
        with self._cache._lock:
            self._cache._evict_audio(self._target)

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except OSError:
            pass

    def _discard(self) -> None:
        try:
            self._part.unlink()
        except OSError:
            pass

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self.close(complete=False)
        except Exception:
            pass


class MediaCache:
    """Persistent cache of streamed media, with a soft byte budget per kind."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._root: Path | None = None
        self._audio_dir: Path | None = None
        self._cover_dir: Path | None = None
        self._audio_budget = DEFAULT_AUDIO_BUDGET
        self._cover_budget = DEFAULT_COVER_BUDGET
        self._available = False

    # ------------------------------------------------------------------ paths
    @property
    def root(self) -> Path:
        if self._root is None:
            # Imported lazily so test harnesses can monkey-patch the settings
            # module before the cache is first touched.
            from src.config import get_settings

            self._root = get_settings().config_path.parent / "cache"
        return self._root

    @property
    def audio_dir(self) -> Path:
        if self._audio_dir is None:
            self._audio_dir = self.root / "audio"
        return self._audio_dir

    @property
    def cover_dir(self) -> Path:
        if self._cover_dir is None:
            self._cover_dir = self.root / "cover"
        return self._cover_dir

    def configure(self, *, audio_budget: int | None = None,
                  cover_budget: int | None = None) -> None:
        """Set the budgets; ``None`` keeps the current value."""
        with self._lock:
            if audio_budget is not None and audio_budget >= 0:
                self._audio_budget = int(audio_budget)
            if cover_budget is not None and cover_budget >= 0:
                self._cover_budget = int(cover_budget)

    def stats(self) -> dict[str, Any]:
        """Diagnostic payload for the settings/about pages."""
        with self._lock:
            audio_bytes, audio_count = _scan(self.audio_dir)
            cover_bytes, cover_count = _scan(self.cover_dir)
        return {
            "available": self._available,
            "root": str(self.root),
            "audio": {
                "count": audio_count,
                "bytes": audio_bytes,
                "budget": self._audio_budget,
            },
            "cover": {
                "count": cover_count,
                "bytes": cover_bytes,
                "budget": self._cover_budget,
            },
        }

    # ------------------------------------------------------------------ audio
    def audio_path(self, song_id: str, quality: str, ext: str = "mp3") -> Path:
        """Where an audio file for ``song_id`` at ``quality`` would live."""
        safe_id = _safe_segment(song_id) or "unknown"
        safe_quality = _safe_segment(quality) or "standard"
        return self.audio_dir / safe_id / f"{safe_quality}.{_safe_segment(ext, default='bin')}"

    def has_audio(self, song_id: str, quality: str, ext: str = "mp3") -> bool:
        return self.audio_path(song_id, quality, ext).is_file()

    def open_audio(self, song_id: str, quality: str, ext: str = "mp3") -> Path | None:
        """Return the cached audio file when it exists and is within budget."""
        target = self.audio_path(song_id, quality, ext)
        if not target.is_file():
            return None
        return target

    def write_audio(self, song_id: str, quality: str, data: bytes,
                    ext: str = "mp3") -> Path | None:
        """Persist ``data`` as the audio file for ``song_id``/``quality``."""
        if not data:
            return None
        if len(data) > NEXT_DOWNLOAD_BYTES:
            return None
        target = self.audio_path(song_id, quality, ext)
        try:
            self._ensure_root()
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_suffix(target.suffix + ".part")
            part.write_bytes(data)
            part.replace(target)
        except OSError as exc:
            log.debug("audio cache write failed for %s: %s", song_id, exc)
            return None
        with self._lock:
            self._evict_audio(target)
        return target

    def audio_writer(self, song_id: str, quality: str,
                     ext: str = "mp3") -> _AudioWriter | None:
        """Open a streaming writer for the audio file of ``song_id``.

        The bytes are written to a ``.part`` file and moved into place when
        the stream completes, exactly like :meth:`write_audio`, but the
        caller never has to hold the song in memory: the writer is fed one
        chunk at a time.  That is what keeps the phone player alive -- a
        whole FLAC in RAM is enough for the system to kill the app.

        Returns ``None`` when the cache cannot be written to; the caller
        then just streams the bytes to the client without caching them.
        """
        target = self.audio_path(song_id, quality, ext)
        try:
            self._ensure_root()
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.debug("audio cache open failed for %s: %s", song_id, exc)
            return None
        return _AudioWriter(self, target)

    def forget_audio(self, song_id: str | None = None) -> int:
        """Drop one song's audio or the whole audio cache.  Returns entries removed."""
        with self._lock:
            # Do NOT gate on ``self._available``: that flag is only set once
            # ``_ensure_root`` has created the tree, so a cache that exists
            # on disk but was never written to in this session was left
            # untouched -- "clear the audio cache" silently did nothing.
            if not self._available and not self.audio_dir.is_dir():
                return 0
            if song_id is None:
                count, _ = _scan(self.audio_dir)
                _rm_tree(self.audio_dir)
                return count
            safe_id = _safe_segment(song_id)
            folder = self.audio_dir / safe_id
            if not folder.is_dir():
                return 0
            count = sum(1 for _ in folder.iterdir())
            _rm_tree(folder)
            return count

    # ------------------------------------------------------------------ cover
    def cover_path(self, url: str, ext: str = "jpg") -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:32]
        return self.cover_dir / f"{digest}.{_safe_segment(ext, default='bin')}"

    def has_cover(self, url: str, ext: str = "jpg") -> bool:
        return self.cover_path(url, ext).is_file()

    def open_cover(self, url: str, ext: str = "jpg") -> tuple[bytes, str] | None:
        target = self.cover_path(url, ext)
        if not target.is_file():
            return None
        try:
            data = target.read_bytes()
        except OSError as exc:
            log.debug("cover cache read failed for %s: %s", url, exc)
            return None
        if not data:
            return None
        return data, _mime_for(target.suffix)

    def write_cover(self, url: str, data: bytes, mime: str = "") -> Path | None:
        """Persist a cover image; ``mime`` is used to pick the file suffix."""
        if not data or len(data) > NEXT_COVER_BYTES:
            return None
        ext = _ext_for_mime(mime) or "jpg"
        target = self.cover_path(url, ext)
        try:
            self._ensure_root()
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_suffix(target.suffix + ".part")
            part.write_bytes(data)
            part.replace(target)
        except OSError as exc:
            log.debug("cover cache write failed for %s: %s", url, exc)
            return None
        with self._lock:
            self._evict_cover(target)
        return target

    # ---------------------------------------------------------------- internals
    def _ensure_root(self) -> None:
        """Create the on-disk tree once.  Idempotent."""
        if self._available:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            self.cover_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create media cache at %s: %s", self.root, exc)
            return
        self._available = True

    def _evict_audio(self, newest: Path) -> None:
        """Drop the oldest audio files until the budget is respected."""
        if self._audio_budget <= 0:
            return
        try:
            files = sorted(self._audio_dir_for_scan(),
                           key=lambda p: p.stat().st_mtime)
        except OSError as exc:
            # A concurrent writer just renamed / unlinked a file we wanted
            # to look at.  Skipping is safe: the next eviction pass will
            # catch whatever is left.
            log.debug("audio evict scan failed: %s", exc)
            return
        total = sum(p.stat().st_size for p in files)
        idx = 0
        # Walk from the bottom (oldest).  ``newest`` is the file we just
        # wrote and we never delete it; skip past it if it shows up before
        # idx.
        newest_dir = newest.parent
        for path in files:
            if total <= self._audio_budget:
                break
            if path == newest:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                # Same race as above: another thread already evicted this.
                continue
            idx += 1
        # Prune empty folders left behind so the cache does not fill up
        # with stale id directories.
        try:
            if newest_dir.exists() and not any(newest_dir.iterdir()):
                newest_dir.rmdir()
        except OSError:
            pass

    def _evict_cover(self, newest: Path) -> None:
        if self._cover_budget <= 0:
            return
        files = sorted(self.cover_dir.glob("*"),
                        key=lambda p: p.stat().st_mtime if p.is_file() else 0)
        total = sum(p.stat().st_size for p in files if p.is_file())
        for path in files:
            if total <= self._cover_budget:
                break
            if not path.is_file() or path == newest:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                pass

    def _audio_dir_for_scan(self) -> list[Path]:
        if not self.audio_dir.is_dir():
            return []
        return [path for path in self.audio_dir.rglob("*") if path.is_file()]


#: Process-wide singleton so the media server and the bridge share one store.
_instance: MediaCache | None = None
_instance_lock = threading.Lock()


def get_media_cache() -> MediaCache:
    """Return the process-wide cache singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = MediaCache()
    return _instance


# ---------------------------------------------------------------------- utils
_KNOWN_MIME: dict[str, str] = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp",
    "image/heic": "heic",
}

_KNOWN_EXT: dict[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".heic": "image/heic",
}


def _safe_segment(value: str, default: str = "") -> str:
    """Reduce ``value`` to something safe to use as a single path segment."""
    text = str(value or "").strip().replace("\\", "/").split("/")[-1]
    cleaned = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in text)
    return cleaned or default


def _ext_for_mime(mime: str) -> str:
    return _KNOWN_MIME.get((mime or "").split(";")[0].strip().lower(), "")


def _mime_for(suffix: str) -> str:
    return _KNOWN_EXT.get(suffix.lower(), "application/octet-stream")


def _scan(folder: Path) -> tuple[int, int]:
    if not folder.is_dir():
        return 0, 0
    total = 0
    count = 0
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            continue
    return total, count


def _rm_tree(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except OSError:
        # Best effort; if one file is locked another pass on next start
        # picks it up.
        try:
            for entry in folder.iterdir():
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except OSError:
                    continue
        except OSError:
            pass
