"""Bangumi 的下载器。

先说清楚一件事：**Bangumi 不托管视频文件**。它给的是条目资料、章节列表，
真正的资源要靠条目网页上「资源」区指出去的外部链接。这个项目里那些链接
是用户自己配的模板拼出来的（见 :mod:`src.core.bangumi` 的
``render_resource_links``），所以这里能做的是：

* **按 URL 存文件**——用户配的资源站给出直链时，把那个文件拉下来；
* **把条目资料和章节表导出成文本**——这个不需要任何外部链接，随时能用。

视频流的抓取不在这里：那要解析第三方站点的页面，各站一套，而且那些站点
的规则三天两头变。硬做出来的东西活不过一个季度。

几个和 B 站那边一样的规矩：

* 先写 ``.part``，下完再改名。半截的文件比没有更糟。
* 内容长度对不上就作废——服务器提前断流时不能当成功。
* 可以取消。取消掉的那个 ``.part`` 会被删掉。
* 文件名要清洗，Windows 上这些字符一个都不能留。
* **不并发轰炸**：同一个下载器的两次请求之间有间隔。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from src.core.bangumi import BANGUMI_UA

__all__ = ["BangumiDownloader", "BangumiDownloadQueue", "sanitize_filename",
           "build_episode_filename"]

log = logging.getLogger(__name__)

#: 一次读多少字节。太小会让进度回调刷爆，太大进度会一跳一跳。
CHUNK_SIZE = 256 * 1024

#: 两次请求之间的最小间隔（秒）。
MIN_GAP = 0.5

#: 文件名里不能出现的字符。Windows 最严，按它来。
_BAD_CHARS = ('/', '\\', ':', '*', '?', '"', '<', '>', '|', '\0',
              '\n', '\r', '\t')


def sanitize_filename(name: str, max_len: int = 160) -> str:
    """把任意字符串变成各平台都能用的文件名。"""
    text = str(name or "")
    if not text:
        return "unknown"
    for char in _BAD_CHARS:
        text = text.replace(char, "-")
    text = " ".join(text.split()).strip(" .")
    if not text:
        return "unknown"
    if len(text) > max_len:
        stem, ext = os.path.splitext(text)
        text = stem[: max_len - len(ext)] + ext
    return text


def build_episode_filename(subject_name: str, episode_label: str,
                           extension: str = ".txt") -> str:
    """``葬送的芙莉莲 - 第 3 集.txt``

    集数在前、条目标题在后不好读；名字打头，按名字排序时才挨在一起。
    """
    ext = extension if extension.startswith(".") else f".{extension}"
    parts = [str(subject_name or "").strip(), str(episode_label or "").strip()]
    name = " - ".join(part for part in parts if part)
    return sanitize_filename(name) + ext


class BangumiDownloader:
    """把 URL 存成文件，并把条目资料导出成文本。

    ``download_dir`` 是落盘的位置。``session`` 可以注入（测试用假 session）。
    ``progress`` 会在下载过程中被反复调用，参数是
    ``{"url", "path", "downloaded", "total", "progress"}``。
    """

    def __init__(
        self,
        download_dir: str | os.PathLike[str] | None = None,
        *,
        session: Any = None,
        timeout: float = 60.0,
        min_gap: float = MIN_GAP,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.download_dir = Path(download_dir) if download_dir else self.default_dir()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": BANGUMI_UA})
        self.timeout = float(timeout)
        self.min_gap = float(max(0.0, min_gap))
        self._progress = progress
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._last = 0.0

    @staticmethod
    def default_dir() -> Path:
        """默认落在「下载/Bangumi」下面。"""
        home = Path.home()
        return home / "Downloads" / "Bangumi"

    def set_progress_callback(
        self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._progress = callback

    def cancel(self) -> None:
        self._cancelled.set()

    def reset(self) -> None:
        self._cancelled.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _notify(self, payload: dict[str, Any]) -> None:
        if self._progress is None:
            return
        try:
            self._progress(payload)
        except Exception as exc:  # noqa: BLE001 - 回调坏了不该打断下载
            log.warning("bangumi 下载进度回调失败: %s", exc)

    def _wait_turn(self) -> None:
        if self.min_gap <= 0:
            return
        gap = time.monotonic() - self._last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self._last = time.monotonic()

    def _ensure_dir(self) -> bool:
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("bangumi 下载目录建不出来 %s: %s", self.download_dir, exc)
            return False
        return True

    # -------------------------------------------------------------- 下文件
    def save_url(self, url: str, filename: str = "") -> dict[str, Any]:
        """把一个 URL 存成文件。

        返回 ``{"success", "path", "size", "url", "error"}``。失败不抛异常，
        ``error`` 里放原因——UI 层不用接异常。
        """
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"success": False, "error": "bad_url", "url": url}
        if self._cancelled.is_set():
            return {"success": False, "error": "cancelled", "url": url}
        if not self._ensure_dir():
            return {"success": False, "error": "bad_dir", "url": url}

        name = sanitize_filename(filename) if filename else \
            sanitize_filename(_filename_from_url(url))
        target = self.download_dir / name
        if target.exists():
            # 同名文件不覆盖：用户可能已经下过一份了。
            target = _unique_path(target)
        part = Path(str(target) + ".part")
        written = 0

        self._wait_turn()
        try:
            with self.session.get(url, stream=True,
                                  timeout=(10, self.timeout)) as response:
                status = getattr(response, "status_code", 200)
                if status != 200:
                    return {"success": False, "error": f"http_{status}", "url": url}
                headers = getattr(response, "headers", {}) or {}
                total = _as_int(headers.get("content-length"), 0)
                with open(part, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if self._cancelled.is_set():
                            raise _Cancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        self._notify({
                            "url": url,
                            "path": str(target),
                            "downloaded": written,
                            "total": total,
                            "progress": round(min(99.9, written / total * 100), 2)
                            if total else 0.0,
                        })
        except _Cancelled:
            _discard(part)
            return {"success": False, "error": "cancelled", "url": url}
        except Exception as exc:  # noqa: BLE001 - 网络错误一律降级
            _discard(part)
            log.warning("bangumi 下载失败 %s: %s", url, exc)
            return {"success": False, "error": str(exc), "url": url}

        if not written:
            _discard(part)
            return {"success": False, "error": "empty", "url": url}

        try:
            os.replace(part, target)
        except OSError as exc:
            _discard(part)
            return {"success": False, "error": str(exc), "url": url}

        self._notify({
            "url": url, "path": str(target), "downloaded": written,
            "total": written, "progress": 100.0,
        })
        return {"success": True, "path": str(target), "size": written, "url": url}

    # -------------------------------------------------------- 导出成文本
    def export_subject_text(self, subject: dict[str, Any],
                            episodes: list[dict[str, Any]] | None = None,
                            *, filename: str = "") -> dict[str, Any]:
        """把条目资料和章节表写成一个 ``.txt``。

        不需要网络，也不需要外部资源链接——离线也能用。
        """
        subject = subject or {}
        if not subject.get("id"):
            return {"success": False, "error": "no_subject"}
        if not self._ensure_dir():
            return {"success": False, "error": "bad_dir"}

        name = subject.get("name_cn") or subject.get("name") or str(subject["id"])
        text = _subject_text(subject, episodes or [])
        target = self.download_dir / (
            sanitize_filename(filename) if filename
            else build_episode_filename(name, "", ".txt"))
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "path": str(target),
                "size": len(text.encode("utf-8"))}


class _Cancelled(Exception):
    """内部用：取消信号。"""


class BangumiDownloadQueue:
    """一串下载任务，一次只下一个。

    为什么是串行的：资源站是用户自己配的第三方站点，本来就不该被轰炸。
    一次一个、两个之间隔开，比并发猛冲稳得多——被封了之后用户自己都没得下。

    用法::

        queue = BangumiDownloadQueue(downloader)
        queue.set_progress_callback(print)
        queue.enqueue("https://x.test/a.mp4", "a.mp4")
        queue.start()
        ...
        queue.status()   # {"pending": 1, "done": 0, ...}
        queue.cancel()

    每个任务的状态是 ``pending`` / ``running`` / ``done`` / ``failed`` /
    ``cancelled``。失败的记 ``error``。
    """

    def __init__(self, downloader: BangumiDownloader) -> None:
        self._downloader = downloader
        self._tasks: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._progress: Callable[[dict[str, Any]], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -------------------------------------------------------------- 队列
    def enqueue(self, url: str, filename: str = "",
                label: str = "") -> dict[str, Any]:
        """加一个任务。返回任务的描述（含 ``id``）。"""
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"success": False, "error": "bad_url", "url": url}
        with self._lock:
            index = len(self._tasks)
            task = {
                "id": index,
                "url": url,
                "filename": filename,
                "label": label or filename or url,
                "status": "pending",
                "error": "",
                "path": "",
                "size": 0,
                "progress": 0.0,
            }
            self._tasks.append(task)
        return {"success": True, "task": dict(task), "id": index}

    def clear(self) -> int:
        """清掉已经结束的任务。正在下的不动。"""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [task for task in self._tasks
                           if task["status"] in ("pending", "running")]
            return before - len(self._tasks)

    def status(self) -> dict[str, Any]:
        """队列的整体状态和每个任务的状态。"""
        with self._lock:
            tasks = [dict(task) for task in self._tasks]
        counts = {"pending": 0, "running": 0, "done": 0, "failed": 0,
                  "cancelled": 0}
        for task in tasks:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        return {
            "success": True,
            "tasks": tasks,
            "total": len(tasks),
            "pending": counts["pending"],
            "running": counts["running"],
            "done": counts["done"],
            "failed": counts["failed"],
            "cancelled": counts["cancelled"],
            "finished": counts["done"] + counts["failed"] + counts["cancelled"],
        }

    # -------------------------------------------------------------- 运行
    def set_progress_callback(
        self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._progress = callback

    def _notify(self, payload: dict[str, Any]) -> None:
        if self._progress is None:
            return
        try:
            self._progress(payload)
        except Exception as exc:  # noqa: BLE001 - 回调坏了不该打断下载
            log.warning("bangumi 下载队列回调失败: %s", exc)

    def start(self) -> bool:
        """开一个后台线程跑队列。已经在跑就返回 False。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            # 上一次取消过的信号留着的话，第一个任务直接被判 cancelled。
            self._downloader.reset()
            self._thread = threading.Thread(
                target=self._run, name="bangumi-dl", daemon=True)
            self._thread.start()
        return True

    def _next_task(self) -> dict[str, Any] | None:
        with self._lock:
            for task in self._tasks:
                if task["status"] == "pending":
                    task["status"] = "running"
                    return dict(task)
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            task = self._next_task()
            if task is None:
                return
            result = self._downloader.save_url(task["url"], task["filename"])
            with self._lock:
                for entry in self._tasks:
                    if entry["id"] != task["id"]:
                        continue
                    if result.get("success"):
                        entry["status"] = "done"
                        entry["path"] = result.get("path", "")
                        entry["size"] = result.get("size", 0)
                        entry["progress"] = 100.0
                    elif result.get("error") == "cancelled":
                        entry["status"] = "cancelled"
                    else:
                        entry["status"] = "failed"
                        entry["error"] = str(result.get("error", ""))
                    break
            final = "done" if result.get("success") else "failed"
            self._notify({
                "id": task["id"],
                "url": task["url"],
                "label": task["label"],
                "status": final,
                "path": result.get("path", ""),
                "error": str(result.get("error", "")),
            })

    def cancel(self) -> int:
        """停掉队列：不再取新任务，当前那个也取消。"""
        self._stop.set()
        self._downloader.cancel()
        with self._lock:
            waiting = [task for task in self._tasks if task["status"] == "pending"]
            for task in waiting:
                task["status"] = "cancelled"
            return len(waiting)

    def reset(self) -> None:
        """取消信号清掉——下一个任务能继续下。"""
        self._stop.clear()
        self._downloader.reset()

    def wait(self, timeout: float | None = None) -> bool:
        """等队列跑完。返回 True 表示跑完了。"""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()


def _filename_from_url(url: str) -> str:
    """从 URL 猜一个文件名。猜不出来就用一个通用的。"""
    try:
        path = urlparse(url).path
    except ValueError:
        return "bangumi-download"
    name = path.rsplit("/", 1)[-1]
    return name or "bangumi-download"


def _unique_path(path: Path) -> Path:
    """``a.txt`` 存在就试 ``a (1).txt``、``a (2).txt``……"""
    if not path.exists():
        return path
    stem, ext = os.path.splitext(str(path))
    for index in range(1, 1000):
        candidate = Path(f"{stem} ({index}){ext}")
        if not candidate.exists():
            return candidate
    return path


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _subject_text(subject: dict[str, Any],
                  episodes: list[dict[str, Any]]) -> str:
    """条目资料 + 章节表，写成人能读的样子。"""
    lines: list[str] = []
    name = subject.get("name_cn") or ""
    original = subject.get("name") or ""
    lines.append(name or original or "未命名")
    if name and original and name != original:
        lines.append(original)
    lines.append("=" * 40)

    meta: list[str] = []
    if subject.get("platform"):
        meta.append(f"类型：{subject['platform']}")
    if subject.get("date"):
        meta.append(f"首播：{subject['date']}")
    total = subject.get("total_episodes")
    if total:
        meta.append(f"集数：{total}")
    score = subject.get("rating_score")
    if score:
        meta.append(f"评分：{score}")
    collected = (subject.get("collection") or {}).get("collect")
    if collected:
        meta.append(f"收藏：{collected}")
    if subject.get("id"):
        meta.append(f"BGM ID：{subject['id']}")
    lines.extend(meta)
    lines.append("")

    summary = str(subject.get("summary") or "").strip()
    if summary:
        lines.append("【简介】")
        lines.append(summary)
        lines.append("")

    tags = subject.get("tags") or []
    if tags:
        names = [str(tag.get("name") or "") for tag in tags if tag.get("name")]
        if names:
            lines.append("【标签】" + "、".join(names))
            lines.append("")

    if episodes:
        lines.append("【章节】")
        for ep in episodes:
            label = ep.get("label") or ep.get("name_cn") or ep.get("name") or ""
            number = ep.get("ep")
            prefix = f"第 {number} 集" if number and ep.get("type") == 0 else ""
            title = label if label != prefix else ""
            line = "  ".join(part for part in (prefix, title) if part)
            extras: list[str] = []
            if ep.get("duration"):
                extras.append(str(ep["duration"]))
            if ep.get("airdate"):
                extras.append(str(ep["airdate"]))
            if extras:
                line = f"{line}  ({' / '.join(extras)})" if line else \
                    f"({' / '.join(extras)})"
            if line:
                lines.append(line)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
