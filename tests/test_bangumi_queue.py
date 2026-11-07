"""下载队列的测试。

队列一次只下一个，这是它最重要的性质——资源站是第三方站点，不能轰炸。
所以这里重点验：串行、状态流转、取消、失败不影响后面的。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from src.core.bangumi_download import BangumiDownloader, BangumiDownloadQueue


class FakeResponse:
    def __init__(self, content=b"x", status_code=200, headers=None):
        self._content = content
        self.status_code = status_code
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=None):
        yield self._content


class FakeSession:
    """按 URL 返回不同的东西。"""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default
        self.headers = {}
        self.calls = []

    def get(self, url, stream=None, timeout=None, headers=None):
        self.calls.append(url)
        response = self.mapping.get(url, self.default)
        if isinstance(response, Exception):
            raise response
        return response


def make_queue(tmp_path: Path, session) -> BangumiDownloadQueue:
    downloader = BangumiDownloader(tmp_path / "dl", session=session, min_gap=0)
    return BangumiDownloadQueue(downloader)


# ------------------------------------------------------------------ 入队
def test_enqueue_rejects_a_bad_url(tmp_path: Path):
    queue = make_queue(tmp_path, FakeSession())
    result = queue.enqueue("not-a-url")
    assert result["success"] is False
    assert result["error"] == "bad_url"
    assert queue.status()["total"] == 0


def test_enqueue_adds_a_pending_task(tmp_path: Path):
    queue = make_queue(tmp_path, FakeSession())
    result = queue.enqueue("https://x.test/a.mp4", "a.mp4", "第一集")
    assert result["success"] is True
    status = queue.status()
    assert status["total"] == 1
    assert status["pending"] == 1
    assert status["tasks"][0]["label"] == "第一集"


# ------------------------------------------------------------------ 跑起来
def test_the_queue_downloads_everything(tmp_path: Path):
    session = FakeSession(default=FakeResponse(b"hello"))
    queue = make_queue(tmp_path, session)
    queue.enqueue("https://x.test/a.mp4", "a.mp4")
    queue.enqueue("https://x.test/b.mp4", "b.mp4")
    queue.start()
    assert queue.wait(timeout=10) is True
    status = queue.status()
    assert status["done"] == 2
    assert status["failed"] == 0
    assert (tmp_path / "dl" / "a.mp4").read_bytes() == b"hello"
    assert (tmp_path / "dl" / "b.mp4").read_bytes() == b"hello"


def test_the_queue_downloads_one_after_another(tmp_path: Path):
    """串行：同一时刻只能有一个下载在进行。

    记下「进来」和「出去」的时刻。两个区间不许重叠——真并发的话重叠是
    必然的，不是偶尔。
    """
    spans: list[tuple[float, float]] = []
    lock = threading.Lock()

    class RecordingSession(FakeSession):
        def get(self, url, stream=None, timeout=None, headers=None):
            entered = time.monotonic()
            time.sleep(0.05)          # 下得有一点点时间，重叠才看得出来
            left = time.monotonic()
            with lock:
                spans.append((entered, left))
            return FakeResponse(b"x")

    queue = make_queue(tmp_path, RecordingSession())
    for index in range(3):
        queue.enqueue(f"https://x.test/{index}.mp4", f"{index}.mp4")
    queue.start()
    assert queue.wait(timeout=10) is True
    assert len(spans) == 3
    spans.sort()
    for before, after in zip(spans, spans[1:], strict=False):
        assert before[1] <= after[0], "两个下载重叠了，队列不是串行的"


def test_a_failure_does_not_stop_the_queue(tmp_path: Path):
    session = FakeSession(mapping={
        "https://x.test/bad.mp4": FakeResponse(b"", status_code=404),
    }, default=FakeResponse(b"ok"))
    queue = make_queue(tmp_path, session)
    queue.enqueue("https://x.test/bad.mp4", "bad.mp4")
    queue.enqueue("https://x.test/good.mp4", "good.mp4")
    queue.start()
    assert queue.wait(timeout=10) is True
    status = queue.status()
    assert status["failed"] == 1
    assert status["done"] == 1
    assert status["tasks"][0]["error"] == "http_404"
    assert status["tasks"][1]["status"] == "done"


def test_the_queue_reports_the_progress(tmp_path: Path):
    seen = []
    session = FakeSession(default=FakeResponse(b"x"))
    queue = make_queue(tmp_path, session)
    queue.set_progress_callback(seen.append)
    queue.enqueue("https://x.test/a.mp4", "a.mp4")
    queue.start()
    assert queue.wait(timeout=10) is True
    assert seen
    assert seen[-1]["status"] == "done"


# ------------------------------------------------------------------ 取消
def test_cancel_marks_the_waiting_tasks(tmp_path: Path):
    queue = make_queue(tmp_path, FakeSession(default=FakeResponse(b"x")))
    queue.enqueue("https://x.test/a.mp4", "a.mp4")
    queue.enqueue("https://x.test/b.mp4", "b.mp4")
    cancelled = queue.cancel()
    assert cancelled == 2
    status = queue.status()
    assert status["cancelled"] == 2
    assert status["pending"] == 0


def test_the_queue_can_run_again_after_a_cancel(tmp_path: Path):
    """取消之后还能再下——否则取消一次就废了。"""
    session = FakeSession(default=FakeResponse(b"x"))
    queue = make_queue(tmp_path, session)
    queue.enqueue("https://x.test/a.mp4", "a.mp4")
    queue.cancel()
    queue.enqueue("https://x.test/b.mp4", "b.mp4")
    queue.start()
    assert queue.wait(timeout=10) is True
    status = queue.status()
    assert status["done"] == 1


# ------------------------------------------------------------------ 清理
def test_clear_drops_the_finished_tasks(tmp_path: Path):
    queue = make_queue(tmp_path, FakeSession(default=FakeResponse(b"x")))
    queue.enqueue("https://x.test/a.mp4", "a.mp4")
    queue.start()
    assert queue.wait(timeout=10) is True
    assert queue.clear() == 1
    assert queue.status()["total"] == 0


def test_start_twice_does_not_run_two_workers(tmp_path: Path):
    session = FakeSession(default=FakeResponse(b"x"))
    queue = make_queue(tmp_path, session)
    queue.enqueue("https://x.test/a.mp4", "a.mp4")
    assert queue.start() is True
    assert queue.start() is False
    assert queue.wait(timeout=10) is True
    assert len(session.calls) == 1
