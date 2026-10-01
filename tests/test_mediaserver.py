"""The loopback media server: range requests, root sandboxing and CDN proxying."""
from __future__ import annotations

import http.server
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_fixtures import build_mp3, build_mp3_with_cover  # noqa: E402

from src.core.mediaserver import get_media_server  # noqa: E402

CDN_BODY = bytes(range(256)) * 8  # 2048 deterministic bytes


class FakeCdn(http.server.BaseHTTPRequestHandler):
    """Stands in for the NetEase CDN: records the headers it was asked with."""

    seen: list[dict[str, str]] = []
    body = CDN_BODY

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        pass

    def do_GET(self):  # noqa: N802 - stdlib signature
        FakeCdn.seen.append({k.lower(): v for k, v in self.headers.items()})
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
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


class FakePlayerAPI:
    def __init__(self, url):
        self.url = url
        self.calls = 0

    def get_song_url_info(self, song_id, quality="standard"):
        self.calls += 1
        return {"url": self.url, "level": quality, "br": 320000, "size": len(CDN_BODY),
                "type": "mp3", "code": 200}


@pytest.fixture()
def cdn():
    FakeCdn.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCdn)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/song.mp3"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def media(isolated_home, cdn):
    """A started media server with one file inside and one outside its roots."""
    inside = isolated_home / "Music" / "inside.mp3"
    build_mp3(inside, title="Inside")
    outside = isolated_home / "outside.mp3"
    build_mp3(outside, title="Outside")

    api = FakePlayerAPI(cdn)
    server = get_media_server()
    server.configure(api_provider=lambda: api)
    server.set_allowed_roots([isolated_home / "Music"])
    server.start()
    try:
        yield server, inside, outside, api
    finally:
        server.stop()


def fetch(url, headers=None, method="GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_health_endpoint(media):
    server, *_ = media
    status, headers, body = fetch(server.url_for_health())

    assert status == 200
    assert body == b"ok"
    assert headers["Access-Control-Allow-Origin"] == "*"


def test_token_is_required(media):
    server, *_ = media
    url = server.url_for_health().replace(server.token, "not-the-token")

    assert fetch(url)[0] == 404


def test_unknown_route_is_404(media):
    server, *_ = media
    assert fetch(f"{server.base_url}/nope")[0] == 404


def test_local_file_within_allowed_root(media):
    server, inside, _outside, _api = media

    status, headers, body = fetch(server.url_for_local(inside))

    assert status == 200
    assert len(body) == inside.stat().st_size
    assert headers["Content-Type"] == "audio/mpeg"
    assert headers["Accept-Ranges"] == "bytes"


def test_local_file_outside_allowed_roots_is_refused(media):
    server, _inside, outside, _api = media

    assert fetch(server.url_for_local(outside))[0] == 403


def test_path_traversal_is_refused(media):
    server, inside, _outside, _api = media
    sneaky = str(inside.parent / ".." / "outside.mp3")

    assert fetch(server.url_for_local(sneaky))[0] == 403


def test_range_request_returns_partial_content(media):
    server, inside, _outside, _api = media

    status, headers, body = fetch(server.url_for_local(inside),
                                  headers={"Range": "bytes=0-9"})

    assert status == 206
    assert len(body) == 10
    assert headers["Content-Range"].startswith("bytes 0-9/")
    assert headers["Content-Length"] == "10"


def test_suffix_range_request(media):
    server, inside, _outside, _api = media
    size = inside.stat().st_size

    status, headers, body = fetch(server.url_for_local(inside),
                                  headers={"Range": "bytes=-8"})

    assert status == 206
    assert len(body) == 8
    assert headers["Content-Range"] == f"bytes {size - 8}-{size - 1}/{size}"


def test_unsatisfiable_range_is_416(media):
    server, inside, _outside, _api = media

    status, _headers, _body = fetch(server.url_for_local(inside),
                                    headers={"Range": f"bytes={inside.stat().st_size + 10}-"})

    assert status == 416


def test_head_has_headers_but_no_body(media):
    server, inside, _outside, _api = media

    status, headers, body = fetch(server.url_for_local(inside), method="HEAD")

    assert status == 200
    assert body == b""
    assert headers["Content-Type"] == "audio/mpeg"


def test_online_proxying_sets_the_desktop_referer(media):
    server, _inside, _outside, api = media

    status, headers, body = fetch(server.url_for_online("12345", "lossless"))

    assert status == 200
    assert body == CDN_BODY
    assert headers["Content-Type"] == "audio/mpeg"
    assert api.calls == 1
    assert FakeCdn.seen[0]["referer"] == "https://music.163.com/"


def test_online_proxying_forwards_ranges(media):
    server, _inside, _outside, _api = media

    status, headers, body = fetch(server.url_for_online("12345", "standard"),
                                  headers={"Range": "bytes=16-31"})

    assert status == 206
    assert body == CDN_BODY[16:32]
    assert headers["Content-Range"] == f"bytes 16-31/{len(CDN_BODY)}"


def test_online_url_is_resolved_once_then_cached(media):
    server, _inside, _outside, api = media

    first = fetch(server.url_for_online("777", "standard"))
    second = fetch(server.url_for_online("777", "standard"))

    assert first[0] == 200 and second[0] == 200
    assert api.calls == 1                     # the second hit came from the cache

    server.forget_online("777")
    fetch(server.url_for_online("777", "standard"))
    assert api.calls == 2


def test_online_without_a_url_is_404(media):
    server, _inside, _outside, api = media
    api.url = ""
    server.forget_online()

    assert fetch(server.url_for_online("404", "standard"))[0] == 404


def test_cover_endpoint_extracts_embedded_art(isolated_home):
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    path = build_mp3_with_cover(isolated_home / "Music" / "art.mp3", jpeg)

    inode = get_media_server()
    inode.set_allowed_roots([isolated_home / "Music"])

    cover = inode.cover_for(path)
    assert cover is not None
    data, mime = cover
    assert data == jpeg
    assert mime == "image/jpeg"


def test_cover_endpoint_without_art_is_404(isolated_home):
    path = build_mp3(isolated_home / "Music" / "plain.mp3", title="Plain")
    server = get_media_server()
    server.set_allowed_roots([isolated_home / "Music"])

    assert server.cover_for(path) is None
    server.start()
    try:
        assert fetch(server.url_for_cover(path))[0] == 404
    finally:
        server.stop()


def test_background_endpoint_serves_the_stored_image(isolated_home):
    image = isolated_home / "wall.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    server = get_media_server()
    server.configure(background_provider=lambda: image)
    server.start()
    try:
        status, headers, body = fetch(server.url_for_background())
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body == image.read_bytes()
    finally:
        server.stop()


def test_local_resolution_rejects_unknown_paths(media):
    server, inside, _outside, _api = media

    assert server.resolve_local(str(inside)) == inside
    assert server.resolve_local("/etc/hosts") is None


def test_stats_describe_the_server(media):
    server, inside, _outside, api = media
    fetch(server.url_for_local(inside))
    fetch(server.url_for_online("1", "standard"))

    stats = server.stats()

    assert stats["running"] is True
    assert len(stats["allowed_roots"]) == 1
    assert stats["cached_urls"] >= 1
    assert stats["port"] > 0
    assert server.base_url.startswith("http://127.0.0.1:")
    assert str(stats["port"]) in server.base_url


def test_resolved_urls_are_capped(media, monkeypatch):
    import src.core.mediaserver as module

    monkeypatch.setattr(module, "MAX_CACHED_URLS", 4)
    server, _inside, _outside, _api = media

    for song_id in range(12):
        fetch(server.url_for_online(str(song_id), "standard"))

    assert len(server._url_cache) <= 4


def test_expired_urls_are_pruned(media, monkeypatch):
    import time as time_module

    import src.core.mediaserver as module

    server, _inside, _outside, _api = media
    monkeypatch.setattr(module, "URL_TTL", 0.05)         # 50 ms

    fetch(server.url_for_online("1", "standard"))
    fetch(server.url_for_online("2", "standard"))
    assert len(server._url_cache) == 2

    time_module.sleep(0.08)
    fetch(server.url_for_online("3", "standard"))        # triggers the prune

    assert list(server._url_cache) == [("3", "standard")]


def test_oversized_covers_are_not_cached(isolated_home, monkeypatch):
    import src.core.mediaserver as module

    monkeypatch.setattr(module, "MAX_COVER_ENTRY_BYTES", 64)
    big = b"\xff\xd8\xff\xe0" + b"\x00" * 512
    path = build_mp3_with_cover(isolated_home / "Music" / "big.mp3", big)

    server = get_media_server()
    server.set_allowed_roots([isolated_home / "Music"])

    # The image is still served from disk, it just does not stay in memory.
    assert server.cover_for(path)[0] == big
    assert server.stats()["cached_covers"] == 0
