"""Concurrent song downloader.

Design notes (why it looks like this):

* **URL resolution happens inside the worker**, not up front.  A signed play URL
  has to be fetched per song and is valid for a limited time; resolving a
  1000-song playlist before starting the first byte wastes minutes and the early
  URLs expire.  Each worker resolves its own URL right before downloading, so
  ``N`` songs start moving at once.
* **At most 3 concurrent files** (user selectable 1-3).  NetEase throttles
  aggressively above that and the gain is negligible on typical home links.
* **The container comes from the API** (``type`` field), not from the requested
  quality: asking for ``lossless`` on a track that only has a 320 kbps version
  returns an mp3, and we must not save it as ``.flac``.
* Downloads go to ``<name>.part`` and are moved into place atomically only after
  the byte count matches what the API announced.
* **Lyrics are a sidecar, never a blocker.**  When enabled, a ``<name>.lrc`` is
  written next to the audio file (atomically, same temp-file trick).  A song
  without lyrics, or a failed lyrics request, must never fail the download, so
  the whole lyrics path is wrapped and only recorded on the task.
"""
from __future__ import annotations

import logging
import os
import random
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from src.core.lyrics import build_lyrics, lyrics_path_for, write_lyrics

__all__ = ["DownloadTask", "DownloadStats", "SongDownloader", "MAX_CONCURRENT"]

#: Hard cap on simultaneous file downloads.
MAX_CONCURRENT = 3
DEFAULT_CONCURRENT = 2

#: 64 KiB chunks keep the GIL/IO overhead low on fast connections.
CHUNK_SIZE = 64 * 1024


class _Cancelled(Exception):
    """Raised internally when the user cancels a download."""


@dataclass
class DownloadTask:
    """One song's download state."""

    index: int
    song_id: str
    song_name: str
    artists: str
    requested_quality: str
    status: str = "waiting"  # waiting|resolving|downloading|completed|failed|skipped|cancelled
    progress: float = 0.0
    speed: float = 0.0            # bytes / second
    downloaded_size: int = 0
    total_size: int = 0
    url: str | None = None
    filepath: str | None = None
    level: str | None = None   # level actually delivered by the API
    error: str | None = None
    error_code: str | None = None
    lyrics_path: str | None = None
    lyrics_status: str = "disabled"   # disabled|saved|exists|none|failed
    lyrics_error: str | None = None
    retries: int = 0
    start_time: float = 0.0
    finish_time: float = 0.0
    _last_notify: float = 0.0
    _speed_mark_ts: float = 0.0
    _speed_mark_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "song_id": self.song_id,
            "name": self.song_name,
            "artists": self.artists,
            "status": self.status,
            "progress": round(self.progress, 2),
            "speed": self.speed,
            "downloaded_size": self.downloaded_size,
            "total_size": self.total_size,
            "filepath": self.filepath,
            "level": self.level,
            "quality": self.requested_quality,
            "error": self.error,
            "error_code": self.error_code,
            "lyrics_path": self.lyrics_path,
            "lyrics_status": self.lyrics_status,
            "lyrics_error": self.lyrics_error,
            "retries": self.retries,
        }


@dataclass
class DownloadStats:
    """Aggregate state of one batch."""

    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    time_elapsed: float = 0.0
    total_bytes: int = 0
    speed: float = 0.0                 # bytes / second (whole batch)
    lyrics_saved: int = 0
    lyrics_missing: int = 0
    failed_songs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def finished(self) -> int:
        return self.success + self.failed + self.skipped + self.cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "finished": self.finished,
            "time_elapsed": round(self.time_elapsed, 2),
            "total_bytes": self.total_bytes,
            "speed": self.speed,
            "lyrics_saved": self.lyrics_saved,
            "lyrics_missing": self.lyrics_missing,
        }


class SongDownloader:
    """Batch song downloader with pause / resume / cancel support."""

    def __init__(
        self,
        download_dir: str,
        quality: str = "standard",
        overwrite: bool = False,
        max_concurrent: int = DEFAULT_CONCURRENT,
        download_lyrics: bool = True,
        lyrics_translation: bool = True,
        timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        self.download_dir = Path(download_dir).expanduser()
        self.quality = quality
        self.overwrite = overwrite
        self.max_concurrent = max(1, min(int(max_concurrent), MAX_CONCURRENT))
        self.download_lyrics = bool(download_lyrics)
        self.lyrics_translation = bool(lyrics_translation)
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))

        self.logger = logging.getLogger("ncm.downloader")

        self._ensure_download_dir()

        self._lock = threading.RLock()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._cancelled = threading.Event()

        self._progress_callback: Callable[[DownloadTask], None] | None = None
        self._task_callback: Callable[[DownloadTask], None] | None = None
        self._stats_callback: Callable[[DownloadStats], None] | None = None

        self._tasks: list[DownloadTask] = []
        self.stats = DownloadStats()

        self._session = requests.Session()
        self._session.headers.update(self._base_headers())
        self._session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))
        self._session.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8))

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _base_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15"),
            "Referer": "https://music.163.com/",
            "Accept": "*/*",
        }
        if extra:
            headers.update(extra)
        return headers

    def _ensure_download_dir(self) -> None:
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            probe = self.download_dir / ".ncm_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            fallback = Path.home() / "Downloads" / "NeteaseMusic"
            fallback.mkdir(parents=True, exist_ok=True)
            self.logger.warning("cannot use %s (%s); falling back to %s",
                                self.download_dir, exc, fallback)
            self.download_dir = fallback

    # -------------------------------------------------------------- callbacks
    def set_progress_callback(self, callback: Callable[[DownloadTask], None]) -> None:
        self._progress_callback = callback

    def set_task_complete_callback(self, callback: Callable[[DownloadTask], None]) -> None:
        self._task_callback = callback

    def set_stats_callback(self, callback: Callable[[DownloadStats], None]) -> None:
        self._stats_callback = callback

    def _notify_progress(self, task: DownloadTask, force: bool = False) -> None:
        if not self._progress_callback:
            return
        now = time.time()
        if not force and now - task._last_notify < 0.35:
            return
        task._last_notify = now
        try:
            self._progress_callback(task)
        except Exception as exc:  # pragma: no cover - UI callback must not kill us
            self.logger.debug("progress callback failed: %s", exc)

    def _notify_complete(self, task: DownloadTask) -> None:
        if not self._task_callback:
            return
        try:
            self._task_callback(task)
        except Exception as exc:  # pragma: no cover
            self.logger.debug("task callback failed: %s", exc)

    def _notify_stats(self, stats: DownloadStats) -> None:
        if not self._stats_callback:
            return
        try:
            self._stats_callback(stats)
        except Exception as exc:  # pragma: no cover
            self.logger.debug("stats callback failed: %s", exc)

    # ------------------------------------------------------------ flow control
    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._pause_event.set()  # wake up paused workers so they can bail out

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    # ------------------------------------------------------------------ naming
    @staticmethod
    def sanitize_filename(filename: str, max_len: int = 180) -> str:
        if not filename:
            return "Unknown"
        for char in ('/', '\\', ':', '*', '?', '"', '<', '>', '|', '\0', '\n', '\r', '\t'):
            filename = filename.replace(char, "-")
        filename = " ".join(filename.split()).strip(" .")
        if not filename:
            return "Unknown"
        if len(filename) > max_len:
            stem, ext = os.path.splitext(filename)
            filename = stem[: max_len - len(ext)] + ext
        return filename

    @staticmethod
    def build_filename(artists: str, title: str, extension: str) -> str:
        extension = extension if extension.startswith(".") else f".{extension}"
        return SongDownloader.sanitize_filename(f"{artists} - {title}") + extension

    @staticmethod
    def extension_for(url: str, api_type: str | None, content_type: str | None) -> str:
        """Pick the file extension, preferring what the API announced."""
        if api_type:
            api_type = api_type.lower().lstrip(".")
            if api_type in ("mp3", "flac", "m4a", "aac", "wav", "ogg", "ape", "mp4"):
                return api_type
        content_type = (content_type or "").lower()
        for needle, ext in (("audio/flac", "flac"), ("audio/x-flac", "flac"),
                            ("audio/mpeg", "mp3"), ("audio/mp4", "m4a"),
                            ("audio/aac", "aac"), ("audio/wav", "wav"),
                            ("audio/ogg", "ogg")):
            if needle in content_type:
                return ext
        path = (url or "").split("?")[0]
        _, ext = os.path.splitext(path)
        if ext and len(ext) <= 6:
            return ext.lstrip(".")
        return "mp3"

    # ------------------------------------------------------------------ single
    def _mark(self, task: DownloadTask, status: str, error: str | None = None,
              error_code: str | None = None) -> None:
        task.status = status
        task.error = error
        task.error_code = error_code
        if status in ("completed", "failed", "skipped", "cancelled"):
            task.finish_time = time.time()
        self._notify_progress(task, force=True)

    def _download(self, task: DownloadTask, url: str, expected_size: int,
                  api_type: str | None, destination: Path) -> int:
        """Stream ``url`` into ``destination``; returns bytes written."""
        part = destination.with_name(destination.name + ".part")
        headers = self._base_headers()
        written = 0
        content_length = 0
        try:
            with self._session.get(url, headers=headers, stream=True,
                                   timeout=(10, self.timeout),
                                   allow_redirects=True) as resp:
                resp.raise_for_status()
                content_length = int(resp.headers.get("content-length") or 0)
                task.total_size = expected_size or content_length
                task._speed_mark_ts = time.time()
                task._speed_mark_bytes = 0
                with open(part, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if self._cancelled.is_set():
                            raise _Cancelled()
                        self._pause_event.wait()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        task.downloaded_size = written
                        if task.total_size:
                            task.progress = min(99.9, written / task.total_size * 100)
                        now = time.time()
                        window = now - task._speed_mark_ts
                        if window >= 0.6:
                            task.speed = (written - task._speed_mark_bytes) / window
                            task._speed_mark_ts = now
                            task._speed_mark_bytes = written
                        self._notify_progress(task)
        except BaseException:
            # Cancelled, interrupted or truncated: never leave a .part behind.
            self._discard(part)
            raise

        expected = expected_size or int(content_length or 0)
        if expected and written != expected:
            self._discard(part)
            raise OSError(f"incomplete download: {written}/{expected} bytes")

        if self._cancelled.is_set():
            self._discard(part)
            raise _Cancelled()

        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination.unlink()
            shutil.move(str(part), str(destination))
        return written

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ lyrics
    def _save_lyrics(self, task: DownloadTask, destination: Path, api_client: Any = None) -> None:
        """Write the ``.lrc`` sidecar for ``destination``; never raises.

        Runs for downloaded *and* already-existing audio files, so enabling
        lyrics later backfills a library that was downloaded without them.
        """
        if not self.download_lyrics:
            task.lyrics_status = "disabled"
            return
        if api_client is None:
            task.lyrics_status = "failed"
            task.lyrics_error = "no api client"
            return

        path = lyrics_path_for(destination)
        try:
            if not self.overwrite and path.exists() and path.stat().st_size > 0:
                task.lyrics_status = "exists"
                task.lyrics_path = str(path)
                return
            text = build_lyrics(api_client, task.song_id, translation=self.lyrics_translation)
            if not text:
                task.lyrics_status = "none"
                self.logger.debug("no lyrics available for %s", task.song_id)
                return
            write_lyrics(path, text)
            task.lyrics_path = str(path)
            task.lyrics_status = "saved"
        except Exception as exc:  # a missing lyric must never fail the song
            task.lyrics_status = "failed"
            task.lyrics_error = str(exc)
            self.logger.debug("lyrics failed for %s: %s", task.song_id, exc)

    def download_single(self, task: DownloadTask, api_client: Any = None) -> bool:
        """Resolve + download one task. Returns ``True`` when the file is on disk."""
        if self._cancelled.is_set():
            self._mark(task, "cancelled")
            return False

        task.start_time = time.time()
        extension = self.extension_for(task.url or "", None, None)

        for attempt in range(self.max_retries):
            if self._cancelled.is_set():
                self._mark(task, "cancelled")
                return False

            try:
                # 1. resolve a fresh play url
                if not task.url and api_client is not None:
                    self._mark(task, "resolving")
                    info = api_client.get_song_url_info(task.song_id, task.requested_quality)
                    if not info:
                        reason = getattr(api_client, "last_url_error", None) or "no_url"
                        self._mark(task, "failed", reason, reason)
                        self.logger.info("no url for %s (%s)", task.song_id, reason)
                        return False
                    task.url = info["url"]
                    task.level = info.get("level")
                    extension = self.extension_for(task.url, info.get("type"), None)
                    task.total_size = int(info.get("size") or 0)

                filename = self.build_filename(task.artists, task.song_name, extension)
                destination = self.download_dir / filename
                task.filepath = str(destination)

                # 2. skip already-downloaded files
                if destination.exists() and not self.overwrite:
                    if destination.stat().st_size > 0:
                        task.progress = 100.0
                        task.total_size = task.total_size or destination.stat().st_size
                        task.downloaded_size = destination.stat().st_size
                        self._save_lyrics(task, destination, api_client)
                        self._mark(task, "skipped")
                        self._notify_complete(task)
                        return True

                # 3. stream it
                self._mark(task, "downloading")
                written = self._download(task, task.url, task.total_size, None, destination)
                task.downloaded_size = written
                task.total_size = task.total_size or written
                task.progress = 100.0
                task.speed = 0.0
                self._save_lyrics(task, destination, api_client)
                self._mark(task, "completed")
                self._notify_complete(task)
                return True

            except _Cancelled:
                self._mark(task, "cancelled")
                return False
            except Exception as exc:
                task.retries = attempt + 1
                task.error = str(exc)
                self.logger.warning("download failed for %s (attempt %d/%d): %s",
                                    task.song_id, attempt + 1, self.max_retries, exc)
                if attempt < self.max_retries - 1:
                    # Signed URLs expire / get rejected -> throw it away and retry.
                    task.url = None
                    time.sleep(0.8 * (2 ** attempt) + random.uniform(0.2, 0.8))

        self._mark(task, "failed", task.error or "unknown error")
        self._notify_complete(task)
        return False

    # ------------------------------------------------------------------- batch
    def batch_download(self, songs_info: list[dict[str, Any]], api_client: Any = None) -> DownloadStats:
        """Download ``songs_info`` (raw API song dicts) concurrently."""
        self._cancelled.clear()
        self._pause_event.set()

        tasks: list[DownloadTask] = []
        for index, song in enumerate(songs_info):
            tasks.append(DownloadTask(
                index=index,
                song_id=str(song.get("id", "")),
                song_name=song.get("name") or "Unknown",
                artists=self._artists_of(song),
                requested_quality=self.quality,
                total_size=0,
            ))

        with self._lock:
            self._tasks = tasks

        stats = DownloadStats(total=len(tasks))
        start = time.time()
        stats.total_bytes = 0
        last_stats_push = 0.0

        def refresh_stats(force: bool = False) -> None:
            nonlocal last_stats_push
            with self._lock:
                stats.success = sum(1 for t in tasks if t.status == "completed")
                stats.failed = sum(1 for t in tasks if t.status == "failed")
                stats.skipped = sum(1 for t in tasks if t.status == "skipped")
                stats.cancelled = sum(1 for t in tasks if t.status == "cancelled")
                stats.total_bytes = sum(t.downloaded_size for t in tasks)
                stats.lyrics_saved = sum(1 for t in tasks if t.lyrics_status == "saved")
                stats.lyrics_missing = sum(1 for t in tasks if t.lyrics_status == "none")
                stats.time_elapsed = time.time() - start
                stats.speed = stats.total_bytes / stats.time_elapsed if stats.time_elapsed > 0 else 0
                stats.failed_songs = [
                    {"name": t.song_name, "artists": t.artists, "id": t.song_id,
                     "error": t.error, "error_code": t.error_code}
                    for t in tasks if t.status == "failed"
                ]
            self.stats = stats
            now = time.time()
            if force or now - last_stats_push >= 0.5:
                last_stats_push = now
                self._notify_stats(stats)

        self._notify_stats(stats)

        if not tasks:
            return stats

        def run(task: DownloadTask) -> None:
            self.download_single(task, api_client)

        with ThreadPoolExecutor(max_workers=self.max_concurrent,
                                thread_name_prefix="ncm-dl") as executor:
            futures = {executor.submit(run, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    self._mark(task, "failed", str(exc))
                    self._notify_complete(task)
                refresh_stats()

        refresh_stats(force=True)

        if stats.failed_songs:
            self._write_failure_report(stats.failed_songs)

        return stats

    @staticmethod
    def _artists_of(song: dict[str, Any]) -> str:
        artists = song.get("artists") or song.get("ar") or []
        names = [a.get("name", "Unknown") for a in artists if isinstance(a, dict)]
        return ", ".join(names) if names else "Unknown"

    def _write_failure_report(self, failed: list[dict[str, Any]]) -> None:
        path = self.download_dir / "failed_downloads.txt"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"# failed downloads - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                for song in failed:
                    handle.write(f"{song['artists']} - {song['name']} "
                                 f"(id={song['id']}, {song.get('error_code') or ''} {song.get('error') or ''})\n")
        except OSError as exc:
            self.logger.warning("cannot write failure report: %s", exc)

    def get_tasks(self) -> list[DownloadTask]:
        with self._lock:
            return list(self._tasks)
