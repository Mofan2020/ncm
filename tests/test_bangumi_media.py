"""Bangumi 资源流的代理测试。

资源站大多只把文件发给带着自己 Referer 的请求，网页里的 video 元素
没法设 Referer，所以走本地媒体代理转发。Range 也要转发。
"""
from __future__ import annotations

import http.server
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.core.mediaserver import get_media_server  # noqa: E402

BODY = bytes(range(256)) * 16  # 4096 deterministic bytes


class FakeSite(http.server.BaseHTTPRequestHandler):
    """Stands in for a resource site: records the headers it was asked with."""

    seen: list[dict[str, str]] = []
    body = BODY

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):  # noqa: N802 - stdlib signature
        FakeSite.seen.append({k.lower(): v for k, v in self.headers.items()})
        range_header = self.headers.get("Range")
        payload = self.body
        if range_header and range_header.startswith("bytes="):
            start, _, end = range_header[6:].partition("-")
            first = int(start or 0)
            last = int(end) if end else len(payload) - 1
            chunk = payload[first:last + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {first}-{last}/{len(payload)}")
        else:
            chunk = payload
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


@pytest.fixture()
def site():
    FakeSite.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/ep1.mp4"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def media(isolated_home, site):
    server = get_media_server()
    server.configure(api_provider=lambda: None)
    server.set_allowed_roots([isolated_home / "Music"])
    server.start()
    try:
        yield server, site
    finally:
        server.stop()


def fetch(url, headers=None, method="GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def media_url(server, target):
    from urllib.parse import quote

    return f"{server.base_url}/bgm-media?u={quote(target, safe='')}"


def test_the_stream_is_served(media):
    server, site = media
    status, headers, body = fetch(media_url(server, site))
    assert status == 200
    assert body == BODY
    assert headers.get("Content-Type") == "video/mp4"


def test_the_referer_points_at_the_site_itself(media):
    """资源站只把文件发给带着自己 Referer 的请求。"""
    server, site = media
    fetch(media_url(server, site))
    assert FakeSite.seen
    referer = FakeSite.seen[-1].get("referer", "")
    assert referer.startswith("http://127.0.0.1:")
    assert referer.endswith("/")


def test_the_range_is_forwarded(media):
    server, site = media
    status, headers, body = fetch(media_url(server, site),
                                   headers={"Range": "bytes=0-99"})
    assert status == 206
    assert len(body) == 100
    assert body == BODY[:100]
    assert headers.get("Content-Range") == f"bytes 0-99/{len(BODY)}"


def test_a_bad_url_is_refused(media):
    server, _site = media
    status, _headers, _body = fetch(media_url(server, "not-a-url"))
    assert status == 400


def test_an_unreachable_site_answers_502(media):
    server, _site = media
    from urllib.parse import quote

    dead = "http://127.0.0.1:1/nope.mp4"
    status, _headers, _body = fetch(f"{server.base_url}/bgm-media?u={quote(dead, safe='')}")
    assert status == 502


def test_a_head_request_has_headers_but_no_body(media):
    server, site = media
    status, headers, body = fetch(media_url(server, site), method="HEAD")
    assert status == 200
    assert body == b""
    assert headers.get("Content-Length") == str(len(BODY))
