"""B站视频与音频的下载。

用本地的假服务器当 CDN，不打真网络。
"""
from __future__ import annotations

import http.server
import threading

import pytest

from src.core.bili_download import BiliDownloader

BODY = b"x" * 4096


class FakeCdn(http.server.BaseHTTPRequestHandler):
    """假 CDN：记录被请求过的地址。"""

    seen: list[str] = []
    fail_for = ""

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        pass

    def do_HEAD(self):  # noqa: N802 - stdlib signature
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()

    def do_GET(self):  # noqa: N802 - stdlib signature
        FakeCdn.seen.append(self.path)
        if FakeCdn.fail_for and FakeCdn.fail_for in self.path:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)


class FakeClient:
    """假 B站客户端：给出两条流。"""

    def __init__(self, base: str, with_flac: bool = False) -> None:
        self.base = base
        self.with_flac = with_flac

    def bili_headers(self, extra=None):
        return {"User-Agent": "test", "Referer": "https://www.bilibili.com/"}

    def video_streams(self, bvid, quality=0, with_muxed=True):
        answer = {
            "success": True, "bvid": bvid, "cid": 1, "duration": 60,
            "video": {"url": f"{self.base}/video.m4s", "mime": "video/mp4",
                      "codecs": "avc1.640032", "bandwidth": 100, "size": len(BODY),
                      "width": 1920, "height": 1080, "init_range": "0-100"},
            "audio": {"url": f"{self.base}/audio.m4s", "mime": "audio/mp4",
                      "codecs": "mp4a.40.2", "bandwidth": 128, "size": len(BODY),
                      "init_range": "0-100"},
            "muxed": None, "flac": None, "dolby": None,
        }
        if self.with_flac:
            answer["flac"] = {"url": f"{self.base}/audio-flac.m4s",
                               "mime": "audio/mp4", "codecs": "flac",
                               "bandwidth": 320, "size": len(BODY),
                               "init_range": "0-100"}
        return answer


@pytest.fixture()
def cdn():
    FakeCdn.seen = []
    FakeCdn.fail_for = ""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCdn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_video_and_its_sound_are_downloaded(tmp_path, cdn):
    downloader = BiliDownloader(tmp_path, FakeClient(cdn))

    results = downloader.download([{"bvid": "BV1aa", "name": "第一", "artists": "甲"}])

    kinds = {item.kind for item in results}
    assert kinds == {"video", "audio"}
    assert all(item.status == "completed" for item in results)
    names = sorted(item.path.rsplit("/", 1)[-1] for item in results)
    assert names == ["甲 - 第一.m4a", "甲 - 第一.mp4"]
    for item in results:
        assert (tmp_path / item.path.rsplit("/", 1)[-1]).is_file()


def test_the_lossless_track_wins(tmp_path, cdn):
    downloader = BiliDownloader(tmp_path, FakeClient(cdn, with_flac=True))

    results = downloader.download([{"bvid": "BV1aa", "name": "第一"}])

    audio = [item for item in results if item.kind == "audio"][0]
    assert audio.path.endswith(".flac")
    assert any("audio-flac" in path for path in FakeCdn.seen)
    assert not any(path.endswith("audio.m4s") for path in FakeCdn.seen)


def test_a_failed_file_leaves_nothing_behind(tmp_path, cdn):
    FakeCdn.fail_for = "video.m4s"
    downloader = BiliDownloader(tmp_path, FakeClient(cdn), max_retries=1)

    results = downloader.download([{"bvid": "BV1aa", "name": "第一"}])

    video = [item for item in results if item.kind == "video"][0]
    assert video.status == "failed"
    assert not list(tmp_path.glob("*.part"))


def test_an_existing_file_is_skipped(tmp_path, cdn):
    existing = tmp_path / "甲 - 第一.mp4"
    existing.write_bytes(BODY)
    downloader = BiliDownloader(tmp_path, FakeClient(cdn))

    results = downloader.download([{"bvid": "BV1aa", "name": "第一", "artists": "甲"}])

    video = [item for item in results if item.kind == "video"][0]
    assert video.status == "skipped"
