"""B站视频 / 音频下载。

网易云的下载器（:mod:`src.core.downloader`）是围着网易云的取流接口写的，
B站这边要自己解决三件事，所以单独一个模块：

* **取流在下载线程里做。** 播放地址带签名，过一会儿就失效；先把整个歌单的
  地址都取出来，前几个还没下完就过期了。
* **视频和音频分开存。** B站的高画质是 DASH 格式，画面和声音是两条流。
  只下画面的话文件没有声音，所以两条流分别落盘（``.mp4`` 和 ``.m4a``）。
  没有 DASH 只有整体 mp4 时就只下一个文件。
* **每个文件都先写 ``.part``，字节数对得上再改名。** 少了一半的视频在
  播放器里表现为「播到一半卡住」，比直接报错更难查。

和网易云那边一样：这个模块不碰界面，只管把文件写好，界面通过回调拿进度。
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

__all__ = ["BiliDownloader", "BiliDownloadResult"]

log = logging.getLogger("ncm.bili.download")

#: 64 KiB 一块：足够快，又不会让界面卡住。
CHUNK_SIZE = 64 * 1024

#: 同时下几个文件。视频文件比歌大，再多就把带宽吃满了。
DEFAULT_CONCURRENT = 2


class BiliDownloadResult:
    """One finished (or failed) file."""

    def __init__(self, kind: str, url: str, filename: str) -> None:
        self.kind = kind          # video|audio
        self.url = url
        self.filename = filename
        # The destination.  It is known from the start: the name is decided
        # before the bytes are fetched.
        self.path = filename
        self.size = 0
        self.status = "waiting"   # waiting|downloading|completed|failed|skipped
        self.error = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "size": self.size,
            "status": self.status,
            "error": self.error,
        }


class BiliDownloader:
    """Download Bilibili videos and their audio tracks."""

    def __init__(
        self,
        download_dir: str | Path,
        client: Any,
        quality: int = 0,
        overwrite: bool = False,
        max_concurrent: int = DEFAULT_CONCURRENT,
        timeout: int = 60,
        max_retries: int = 3,
    ) -> None:
        self.download_dir = Path(download_dir).expanduser()
        self.client = client
        self.quality = int(quality or 0)
        self.overwrite = bool(overwrite)
        self.max_concurrent = max(1, min(int(max_concurrent), 3))
        self.timeout = int(timeout)
        self.max_retries = max(1, int(max_retries))
        self.logger = logging.getLogger("ncm.bili.downloader")

        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._progress_callback: Callable[[dict[str, Any]], None] | None = None
        self._done_callback: Callable[[dict[str, Any]], None] | None = None
        self._session = requests.Session()

    # -------------------------------------------------------------- callbacks
    def set_progress_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._progress_callback = callback

    def set_done_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._done_callback = callback

    def _notify(self, payload: dict[str, Any]) -> None:
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(payload)
        except Exception as exc:  # pragma: no cover - a UI callback must not kill us
            self.logger.debug("progress callback failed: %s", exc)

    # ------------------------------------------------------------ flow control
    def cancel(self) -> None:
        self._cancelled.set()

    # ------------------------------------------------------------------ naming
    @staticmethod
    def sanitize_filename(filename: str, max_len: int = 160) -> str:
        """Make a string usable as a file name on every platform."""
        if not filename:
            return "unknown"
        for char in ('/', '\\', ':', '*', '?', '"', '<', '>', '|', '\0', '\n', '\r', '\t'):
            filename = filename.replace(char, "-")
        filename = " ".join(filename.split()).strip(" .")
        if not filename:
            return "unknown"
        if len(filename) > max_len:
            stem, ext = os.path.splitext(filename)
            filename = stem[: max_len - len(ext)] + ext
        return filename

    def build_filename(self, author: str, title: str, extension: str) -> str:
        """``<up主> - <标题>.mp4``"""
        extension = extension if extension.startswith(".") else f".{extension}"
        name = self.sanitize_filename(f"{author} - {title}" if author else str(title))
        return name + extension

    # ------------------------------------------------------------------ files
    def _ensure_dir(self) -> None:
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fallback = Path.home() / "Downloads" / "SkycMedia"
            fallback.mkdir(parents=True, exist_ok=True)
            self.logger.warning("cannot use %s (%s); falling back to %s",
                                self.download_dir, exc, fallback)
            self.download_dir = fallback

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "*/*",
        }
        try:
            extra = self.client.bili_headers()
        except Exception:  # pragma: no cover - the client does not raise
            extra = {}
        if isinstance(extra, dict):
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    def _fetch(self, result: BiliDownloadResult, expected: int) -> bool:
        """Stream one url into its file.  Returns whether it worked."""
        part = Path(result.path + ".part")
        written = 0
        try:
            with self._session.get(result.url, headers=self._headers(), stream=True,
                                   timeout=(10, self.timeout)) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0) or expected
                with open(part, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if self._cancelled.is_set():
                            raise RuntimeError("cancelled")
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        self._notify({
                            "kind": result.kind,
                            "path": result.path,
                            "downloaded": written,
                            "total": total,
                            "progress": round(min(99.9, written / total * 100), 2)
                            if total else 0.0,
                        })
        except Exception as exc:
            self._discard(part)
            result.status = "failed"
            result.error = str(exc)
            return False

        if expected and written != expected:
            # The stream was cut short: a half file is worse than no file.
            self._discard(part)
            result.status = "failed"
            result.error = f"不完整：{written}/{expected} 字节"
            return False

        try:
            os.replace(part, result.path)
        except OSError as exc:
            self._discard(part)
            result.status = "failed"
            result.error = str(exc)
            return False
        result.size = written
        result.status = "completed"
        return True

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------ batch
    def _resolve(self, bvid: str) -> dict[str, Any]:
        try:
            try:
                # 下载要的是分轨的两条流，单文件流用不上，少问一次是一次。
                return self.client.video_streams(bvid, quality=self.quality,
                                                 with_muxed=False) or {}
            except TypeError:
                # 旧的核心客户端没有这个参数。
                return self.client.video_streams(bvid, quality=self.quality) or {}
        except Exception as exc:  # pragma: no cover - the client does not raise
            self.logger.warning("cannot resolve the streams of %s: %s", bvid, exc)
            return {}

    @staticmethod
    def _best_audio(streams: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The best sound track of the answer, and the suffix it deserves.

        Bilibili serves the Hi-Res (FLAC) and Dolby tracks next to the normal
        one.  They are the same recording in a better codec, so there is no
        reason to download the worse one.
        """
        for key, extension in (("flac", "flac"), ("dolby", "m4a"), ("audio", "m4a")):
            stream = streams.get(key)
            if isinstance(stream, dict) and stream.get("url"):
                return stream, extension
        return {}, "m4a"

    def download_one(self, track: dict[str, Any]) -> list[BiliDownloadResult]:
        """Download one video: the picture file and the sound file."""
        bvid = str(track.get("bvid") or track.get("id") or "").strip()
        title = str(track.get("name") or track.get("title") or bvid)
        author = str(track.get("artists") or track.get("author") or "")
        results: list[BiliDownloadResult] = []
        if not bvid:
            return results

        streams = self._resolve(bvid)
        video = streams.get("video") or {}
        muxed = streams.get("muxed") or {}
        # 音质挑最好的那一条：Hi-Res 无损 > 杜比 > 普通音轨。
        audio, audio_ext = self._best_audio(streams)

        if muxed.get("url"):
            # No DASH: the whole thing comes as one mp4 that already has sound.
            target = self.download_dir / self.build_filename(author, title, "mp4")
            item = BiliDownloadResult("video", str(muxed["url"]), str(target))
            results.append(item)
        elif video.get("url"):
            target = self.download_dir / self.build_filename(author, title, "mp4")
            item = BiliDownloadResult("video", str(video["url"]), str(target))
            results.append(item)

        if audio.get("url"):
            # A separate sound file next to the picture, so the music can be
            # played on its own.
            target = self.download_dir / self.build_filename(author, title, audio_ext)
            item = BiliDownloadResult("audio", str(audio["url"]), str(target))
            results.append(item)

        for item in results:
            if self._cancelled.is_set():
                item.status = "cancelled"
                continue
            path = Path(item.path)
            if path.exists() and not self.overwrite and path.stat().st_size > 0:
                item.status = "skipped"
                item.size = path.stat().st_size
                self._notify({"kind": item.kind, "path": item.path,
                              "downloaded": item.size, "total": item.size,
                              "progress": 100.0})
                continue
            expected = int(item.size or 0)
            item.status = "downloading"
            for attempt in range(self.max_retries):
                if self._cancelled.is_set():
                    item.status = "cancelled"
                    break
                size = expected
                if not size:
                    try:
                        size = int(self._session.head(
                            item.url, headers=self._headers(),
                            timeout=(10, self.timeout),
                            allow_redirects=True).headers.get("content-length") or 0)
                    except Exception:
                        size = 0
                if self._fetch(item, size):
                    break
                if attempt < self.max_retries - 1:
                    time.sleep(0.8 * (2 ** attempt) + random.uniform(0.2, 0.8))
            if self._done_callback is not None:
                try:
                    self._done_callback(item.to_dict())
                except Exception:  # pragma: no cover
                    self.logger.debug("done callback failed")
        return results

    def download(self, tracks: list[dict[str, Any]]) -> list[BiliDownloadResult]:
        """Download every track of the list."""
        self._ensure_dir()
        self._cancelled.clear()
        results: list[BiliDownloadResult] = []
        items = [track for track in (tracks or []) if isinstance(track, dict)]
        if not items:
            return results
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            for produced in pool.map(self.download_one, items):
                results.extend(produced)
        return results
