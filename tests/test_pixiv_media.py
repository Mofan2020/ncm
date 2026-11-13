"""回环媒体服务器的 pixiv 图片代理。

``i.pximg.net`` 对不带 ``Referer: https://www.pixiv.net/`` 的请求一律 403，
而网页里的 ``<img>`` 设不了请求头 —— 图片只能从这一层过。于是这一层同时
担着三件事：带上该带的头、转发 ``Range``、**只**放行 pixiv 自己的图床。

这里没有真实网络：``FakeCdn`` 是一个本地 HTTP 服务器，主机白名单按需放宽
到 ``127.0.0.1``（只放宽「额外允许」，真正的白名单照旧生效，所以
``i.pximg.net.evil.com`` 那一条仍然走的是实现里的判断）。
"""
from __future__ import annotations

import http.server
import logging
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.core import mediaserver as media_module  # noqa: E402
from src.core.mediaserver import get_media_server  # noqa: E402
from src.core.pixiv import PIXIV_REFERER, PIXIV_UA  # noqa: E402

#: 一张「小图」：几 KB，够大得能看出字节没被截断。
SMALL = bytes(range(256)) * 4
#: 一张「原图」：比 2 MB 的缓存上限大一点点。
BIG = b"\xff" * (media_module.MAX_PIXIV_CACHE_BYTES + 4096)


class FakeCdn(http.server.BaseHTTPRequestHandler):
    """假 pixiv 图床：记录它收到的请求头，支持 Range，也能故意拒一次。"""

    seen: list[dict[str, str]] = []

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):  # noqa: N802 - stdlib signature
        FakeCdn.seen.append({k.lower(): v for k, v in self.headers.items()})
        if "forbidden" in self.path:
            # 真实图床对不带 Referer 的请求就是这样答的。
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = BIG if "original" in self.path else SMALL
        range_header = self.headers.get("Range")
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
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # HEAD 那条用例会让代理在读之前就关掉连接，假图床照旧把整张图
            # 往外写 —— 断管是预期的，不是失败。
            pass


@pytest.fixture()
def cdn():
    """本地假图床。返回的地址指向 ``/thumb_p0.jpg``。"""
    FakeCdn.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCdn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def allow_local(monkeypatch):
    """把 127.0.0.1 额外加进白名单，**不改**真正的规则。

    用例必须打到本地假图床，而白名单里只有 pixiv 的域名。所以放宽的是
    「另外允许 127.0.0.1」，实现里的 ``PIXIV_IMG_HOSTS`` 判断原样保留 ——
    否则「后缀伪装要拒」的那条用例测到的就只是个空壳。
    """
    real = media_module._is_pixiv_img_url
    monkeypatch.setattr(
        media_module, "_is_pixiv_img_url",
        lambda url: "127.0.0.1:" in str(url) or real(url),
    )


@pytest.fixture()
def media(isolated_home, cdn, allow_local):
    server = get_media_server()
    server.configure(api_provider=lambda: None)
    server.set_allowed_roots([isolated_home / "Music"])
    server.start()
    try:
        yield server, cdn
    finally:
        server.stop()


def fetch(url, headers=None, method="GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def pixiv_url(server, target):
    """手拼代理地址 —— ``url_for_pixiv`` 自己就会拦外站，拦不住的才要测服务端。"""
    return f"{server.base_url}/pixiv?u={quote(target, safe='')}"


# --------------------------------------------------------------------- 地址


def test_the_proxy_url_is_built_for_an_image_host():
    server = get_media_server()

    url = server.url_for_pixiv(
        "https://i.pximg.net/c/250x250_80_a2/img-master/img/2026/11/13/1_p0_square1200.jpg")

    assert url is not None
    assert url.startswith(server.base_url)
    assert "/pixiv?u=" in url


def test_a_url_outside_the_image_hosts_has_no_proxy_url():
    server = get_media_server()

    assert server.url_for_pixiv("https://evil.com/x.jpg") is None
    assert server.url_for_pixiv("https://i.pximg.net.evil.com/x.jpg") is None
    assert server.url_for_pixiv("file:///etc/passwd") is None


def test_the_route_needs_the_server_token():
    """这条路和别的路由一样要过 token，不能变成一条不设防的代理。"""
    server = get_media_server()
    server.start()
    try:
        image = "https://i.pximg.net/a.jpg"
        status, _headers, _body = fetch(
            f"http://127.0.0.1:{server.port}/m/wrong-token/pixiv?u={quote(image, safe='')}")
    finally:
        server.stop()

    assert status == 404


# ------------------------------------------------------------------- 白名单


def test_a_spoofed_host_is_refused(media):
    """``i.pximg.net.evil.com`` 是一个谁都能注册的域名，后缀匹配会放它进来。"""
    server, _cdn = media

    status, _headers, _body = fetch(pixiv_url(server, "https://i.pximg.net.evil.com/x.jpg"))

    assert status == 403


def test_a_foreign_host_is_refused(media):
    server, _cdn = media

    status, _headers, _body = fetch(pixiv_url(server, "https://evil.com/x.jpg"))

    assert status == 403


def test_an_empty_url_is_refused(media):
    server, _cdn = media

    status, _headers, _body = fetch(f"{server.base_url}/pixiv?u=")

    assert status == 403


def test_the_userinfo_trick_is_refused(media):
    """``https://i.pximg.net@evil.com/`` 里的主机是 evil.com，不是 i.pximg.net。"""
    server, _cdn = media

    status, _headers, _body = fetch(pixiv_url(server, "https://i.pximg.net@evil.com/x.jpg"))

    assert status == 403


# --------------------------------------------------------------------- 上游


def test_the_site_referer_and_the_pixiv_user_agent_are_sent(media):
    server, cdn = media

    status, _headers, body = fetch(pixiv_url(server, f"{cdn}/thumb_p0.jpg"))

    assert status == 200
    assert body == SMALL
    assert FakeCdn.seen[0]["referer"] == PIXIV_REFERER
    assert FakeCdn.seen[0]["user-agent"] == PIXIV_UA


def test_the_range_is_forwarded(media):
    """图片查看器会先要好一段试试，206 和 Content-Range 都要原样回去。"""
    server, cdn = media

    status, headers, body = fetch(pixiv_url(server, f"{cdn}/thumb_p0.jpg"),
                                  headers={"Range": "bytes=0-99"})

    assert status == 206
    assert body == SMALL[:100]
    assert headers.get("Content-Range") == f"bytes 0-99/{len(SMALL)}"


# --------------------------------------------------------------------- 缓存


def test_a_small_image_is_cached_and_the_second_request_skips_the_cdn(media, monkeypatch):
    server, cdn = media
    calls: list[str] = []
    real = server.session.get

    def get(url, **kwargs):
        calls.append(url)
        return real(url, **kwargs)

    monkeypatch.setattr(server.session, "get", get)
    target = f"{cdn}/thumb_p0.jpg"

    first = fetch(pixiv_url(server, target))
    second = fetch(pixiv_url(server, target))

    assert first[0] == second[0] == 200
    assert second[2] == SMALL
    assert len(calls) == 1, "第二次命中缓存，不该再打图床"
    cached = list(server.pixiv_cache_dir.iterdir())
    assert len(cached) == 1
    assert cached[0].read_bytes() == SMALL


def test_the_cache_key_is_the_url_hash(media):
    """键是 URL 的 sha1：地址里有作品 id 和百分号编码，当不了文件名。"""
    server, _cdn = media
    target = "https://i.pximg.net/c/250x250_80_a2/img-master/x_p0_square1200.jpg"

    import hashlib

    expected = hashlib.sha1(target.encode("utf-8")).hexdigest()

    assert server.pixiv_cache_path(target).name == f"{expected}.jpg"
    assert server.pixiv_cache_path(target).parent == server.pixiv_cache_dir


def test_an_original_is_streamed_and_never_cached(media, monkeypatch):
    """原图十几 MB：只转发，落盘既占地方又不会命中。"""
    server, cdn = media
    calls: list[str] = []
    real = server.session.get

    def get(url, **kwargs):
        calls.append(url)
        return real(url, **kwargs)

    monkeypatch.setattr(server.session, "get", get)
    target = f"{cdn}/original_p0.jpg"

    status, headers, body = fetch(pixiv_url(server, target))
    fetch(pixiv_url(server, target))

    assert status == 200
    assert len(body) == len(BIG)
    assert headers.get("Content-Length") == str(len(BIG))
    assert len(calls) == 2, "大图不缓存，两次都得去图床拿"
    assert server.pixiv_cache_dir.is_dir() is False or not any(
        server.pixiv_cache_dir.iterdir())


def test_a_range_answer_is_not_written_to_the_cache(media):
    """半张图存下去，下次就会被当成完整的图发出去。"""
    server, cdn = media

    status, _headers, _body = fetch(pixiv_url(server, f"{cdn}/thumb_p0.jpg"),
                                    headers={"Range": "bytes=0-99"})

    assert status == 206
    assert not server.pixiv_cache_dir.is_dir() or not any(
        server.pixiv_cache_dir.iterdir())


# --------------------------------------------------------------------- 失败


def test_an_upstream_403_answers_502_and_logs_without_the_url(media, caplog):
    server, cdn = media

    with caplog.at_level(logging.WARNING, logger="ncm.media"):
        status, _headers, _body = fetch(pixiv_url(server, f"{cdn}/forbidden_p0.jpg"))

    assert status == 502
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "403" in logged
    assert f"127.0.0.1:{cdn.rsplit(':', 1)[-1]}" in logged
    # 整条图片地址不进日志：它带着用户在搜什么、在看谁的作品。
    assert "/forbidden_p0.jpg" not in logged


def test_an_unreachable_host_answers_502(media):
    server, _cdn = media

    status, _headers, _body = fetch(pixiv_url(server, "http://127.0.0.1:1/x.jpg"))

    assert status == 502


def test_a_head_request_has_headers_but_no_body(media):
    server, cdn = media

    status, headers, body = fetch(pixiv_url(server, f"{cdn}/original_p0.jpg"), method="HEAD")

    assert status == 200
    assert body == b""
    assert headers.get("Content-Length") == str(len(BIG))
