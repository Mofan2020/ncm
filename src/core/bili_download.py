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
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import requests

__all__ = ["BiliDownloader", "BiliDownloadResult"]

log = logging.getLogger("ncm.bili.download")

#: 64 KiB 一块：足够快，又不会让界面卡住。
CHUNK_SIZE = 64 * 1024

#: 同时下几个文件。视频文件比歌大，再多就把带宽吃满了。
DEFAULT_CONCURRENT = 2

#: 两个视频之间最少隔多久去问一次取流地址（秒）。
#:
#: 真正会被 B站限流的是「取流」这一步，不是搬字节：一次点十几集，十几条
#: 取流请求同时打过去，回的就是 -352 风控。文件传输还在并发跑，只有取流
#: 这一下按这个间隔排队。
DEFAULT_INTERVAL = 1.5

#: 间隔的上限。再慢下去用户会以为卡住了。
MAX_INTERVAL = 10.0


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
        merge_audio: bool = False,
        interval: float = DEFAULT_INTERVAL,
    ) -> None:
        self.download_dir = Path(download_dir).expanduser()
        self.client = client
        self.quality = int(quality or 0)
        self.overwrite = bool(overwrite)
        self.max_concurrent = max(1, min(int(max_concurrent), 3))
        self.timeout = int(timeout)
        self.max_retries = max(1, int(max_retries))
        #: Put the picture and the sound back into one file after the download.
        self.merge_audio = bool(merge_audio)
        #: 取流请求之间的最小间隔（秒）。0 表示完全不排队。
        self.interval = max(0.0, min(float(interval or 0), MAX_INTERVAL))
        self.logger = logging.getLogger("ncm.bili.downloader")

        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        #: 取流是排队做的：一把锁 + 上一次发请求的时刻。
        self._pace_lock = threading.Lock()
        self._pace_last = 0.0
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

    def _merge_pair(self, video: BiliDownloadResult,
                    audio: BiliDownloadResult) -> str:
        """Put the picture and the sound back into one mp4.

        B站 serves high qualities as two streams, so a download without this
        step is a silent video plus a loose .m4a.  Merging needs ffmpeg; when
        it is not installed the two files stay and the caller is told why --
        pretending it worked would leave the user with a silent video and no
        explanation.

        We use imageio-ffmpeg which ships a static ffmpeg binary, so the user
        does not need to install anything.  If for some reason that fails we
        fall back to a system ffmpeg.

        Returns an empty string on success, otherwise the reason for the user.
        """
        # imageio-ffmpeg ships a static binary; fall back to system ffmpeg.
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 - best effort
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return "没有找到 ffmpeg，画面和声音保持两个文件"
        video_path = Path(video.path)
        audio_path = Path(audio.path)
        if not video_path.is_file() or not audio_path.is_file():
            return "文件不齐，没有合并"
        stem = video_path.stem
        # ffmpeg cannot write over one of its own inputs, so an existing .mp4
        # is written next to it first and swapped in afterwards.
        out = video_path if video_path.suffix.lower() != ".mp4" else \
            video_path.with_name(stem + ".merged.mp4")
        if out.suffix.lower() != ".mp4":
            out = out.with_suffix(".mp4")
        self._notify({"status": "merging", "kind": "merge", "path": str(out),
                      "filename": out.name, "progress": 100.0})
        command = [ffmpeg, "-y", "-loglevel", "error",
                   "-i", str(video_path), "-i", str(audio_path),
                   "-c", "copy", "-movflags", "+faststart", str(out)]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            self._discard(out)
            return f"合并失败：{exc}"
        if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            self._discard(out)
            detail = (proc.stderr or "").strip().splitlines()
            return "合并失败：" + (detail[-1][:160] if detail else "ffmpeg 报错")
        if out != video_path:
            self._discard(video_path)
        self._discard(audio_path)
        self._notify({"status": "merged", "kind": "merge", "path": str(out),
                      "filename": out.name, "downloaded": out.stat().st_size,
                      "total": out.stat().st_size, "progress": 100.0})
        return ""

    # ------------------------------------------------------------------ batch
    @contextmanager
    def _paced(self):
        """Take the resolve turn, and keep a gap between two of them.

        取流是唯一会被 B站当成「刷接口」的一步：一次排十个视频，十条取流
        请求一起飞过去就是 -352。这里把这一步排成队，并且保证两次之间至少
        隔 ``interval`` 秒；字节搬运不在锁里，所以下载速度不受影响。
        """
        with self._pace_lock:
            if self.interval > 0 and self._pace_last > 0:
                wait = self._pace_last + self.interval + random.uniform(0, self.interval / 3)
                delay = wait - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            try:
                yield
            finally:
                self._pace_last = time.monotonic()

    def _resolve(self, bvid: str, cid: int = 0) -> dict[str, Any]:
        try:
            try:
                # 下载要的是分轨的两条流，单文件流用不上，少问一次是一次。
                return self.client.video_streams(bvid, quality=self.quality,
                                                 with_muxed=False, cid=cid) or {}
            except TypeError:
                # 旧的核心客户端没有这些参数。
                return self.client.video_streams(bvid, quality=self.quality) or {}
        except Exception as exc:  # pragma: no cover - the client does not raise
            self.logger.warning("cannot resolve the streams of %s: %s", bvid, exc)
            return {}

    def _resolve_paced(self, bvid: str, cid: int = 0) -> dict[str, Any]:
        """Resolve the streams of one video, inside the paced queue."""
        with self._paced():
            return self._resolve(bvid, cid=cid)

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
        # 分P：track 里带了 cid 就下那一集，文件名后面也标上是第几集。
        page_cid = 0
        try:
            page_cid = int(track.get("cid") or 0)
        except (TypeError, ValueError):
            page_cid = 0
        page_title = str(track.get("page_title") or "").strip()
        if page_title and page_title not in title:
            title = f"{title} - {page_title}"
        results: list[BiliDownloadResult] = []
        if not bvid:
            return results

        streams = self._resolve_paced(bvid, cid=page_cid)
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

        # Both halves landed: put them back together when the user asked for it.
        if self.merge_audio and len(results) == 2:
            video = next((item for item in results if item.kind == "video"), None)
            audio = next((item for item in results if item.kind == "audio"), None)
            if (video is not None and audio is not None
                    and video.status == "completed" and audio.status == "completed"):
                reason = self._merge_pair(video, audio)
                if reason:
                    self.logger.info("merge skipped for %s: %s", bvid, reason)
                    self._notify({"status": "merge_skipped", "kind": "merge",
                                  "path": video.path, "message": reason,
                                  "progress": 100.0})
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
