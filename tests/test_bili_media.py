"""回环媒体服务器的 B站代理。

视频和音频都在 B站的 CDN 上，而 CDN 只认带 ``Referer`` 的请求，所以浏览器
不能直接去取：这一层代理负责带上该带的头，转发 ``Range``，并且**只**放行
B站自己的域名。
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

from src.core import mediaserver as media_module  # noqa: E402
from src.core.mediaserver import get_media_server  # noqa: E402

BODY = bytes(range(256)) * 8


class FakeCdn(http.server.BaseHTTPRequestHandler):
    """假 B站 CDN：记录它收到的请求头。"""

    seen: list[dict[str, str]] = []

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):  # noqa: N802 - stdlib signature
        FakeCdn.seen.append({k.lower(): v for k, v in self.headers.items()})
        range_header = self.headers.get("Range")
        payload = BODY
        if range_header and range_header.startswith("bytes="):
            start, _, end = range_header[6:].partition("-")
            first = int(start or 0)
            last = int(end) if end else len(payload) - 1
            payload = payload[first:last + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {first}-{last}/{len(BODY)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def cdn(monkeypatch):
    """A fake CDN on the loopback, plus the host check that lets it through."""
    FakeCdn.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCdn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(media_module, "_is_bili_url",
                        lambda url: "127.0.0.1" in str(url))
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/video.mp4"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def media(isolated_home, cdn):
    server = get_media_server()
    server.start()
    try:
        yield server, cdn
    finally:
        server.stop()


def fetch(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_the_proxy_url_is_built_for_a_cdn_address():
    server = get_media_server()

    url = server.url_for_bili("https://upos-sz-mirrorcos.bilivideo.com/x.m4s")

    assert url is not None
    assert url.startswith(server.base_url)
    assert "/bili?u=" in url


def test_a_url_outside_the_cdn_is_refused():
    server = get_media_server()

    assert server.url_for_bili("https://example.invalid/song.mp3") is None
    assert server.url_for_bili("file:///etc/passwd") is None


def test_the_proxy_sends_the_site_referer_and_the_cookie(media):
    server, cdn = media
    server.set_bili_cookie("SESSDATA=abc; bili_jct=def")

    status, headers, body = fetch(server.url_for_bili(cdn))

    assert status == 200
    assert body == BODY
    assert FakeCdn.seen[0]["referer"] == "https://www.bilibili.com/"
    assert FakeCdn.seen[0]["cookie"] == "SESSDATA=abc; bili_jct=def"


def test_the_proxy_forwards_the_range(media):
    server, cdn = media

    status, headers, body = fetch(server.url_for_bili(cdn),
                                  headers={"Range": "bytes=16-31"})

    assert status == 206
    assert body == BODY[16:32]
    assert headers["Content-Range"] == f"bytes 16-31/{len(BODY)}"


def test_the_proxy_refuses_a_foreign_host(media):
    server, _cdn = media
    # 手拼这个地址：url_for_bili 本身就会拦下外站的地址，拦不住的才要看
    # 服务端这一层是不是也拦得住。
    url = f"{server.base_url}/bili?u=https%3A%2F%2Fexample.invalid%2Fx.mp4"

    status, _headers, _body = fetch(url)

    assert status == 403
