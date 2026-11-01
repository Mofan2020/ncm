"""The hosts the Bilibili proxy is allowed to fetch from.

The proxy is the only way the webview reaches the Bilibili CDN, so a host
that is missing from the list means "this video does not play" -- and the
refusal is a 403 from the proxy itself, not from Bilibili.  The CDN hands
out a different host for every stream, and it changed the single-file
(muxed) stream to ``*.edge.mountaintoys.cn``; without that entry **no**
video plays on macOS, because the WKWebView there has no ``MediaSource``
and can only use the muxed stream.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.mediaserver import _is_bili_url  # noqa: E402


def test_the_muxed_cdn_host_is_allowed():
    """The muxed stream is where macOS plays from."""
    url = ("https://b-baaaw30uqv1hzw3czupt73k9fsi.edge.mountaintoys.cn:4483/"
           "upgcxcode/09/66/43205266609/43205266609-1-192.mp4?e=ig8euxZM2rNc")
    assert _is_bili_url(url) is True


def test_the_dash_cdn_hosts_are_allowed():
    for url in (
        "https://upos-sz-mirrorcoso1.bilivideo.com/upgcxcode/a.m4s",
        "https://xy171x43x247x140xy.mcdn.bilivideo.cn:8082/v1/resource/a.m4s",
        "https://i0.hdslb.com/bfs/archive/cover.jpg",
        "https://i0.hdslb.com/bfs/face/avatar.jpg",
        "https://api.bilibili.com/x/web-interface/nav",
        "https://www.bilibili.com/video/BV1xx411c7mD",
    ):
        assert _is_bili_url(url) is True, url


def test_a_host_outside_the_bilibili_domains_is_refused():
    """The proxy must not become an open proxy for the whole internet."""
    for url in (
        "https://example.com/a.jpg",
        "https://evil.test/x",
        "https://bilibili.com.evil.test/x",
        "https://notmountaintoys.cn/x",
        "https://hdslb.com.evil.test/x",
    ):
        assert _is_bili_url(url) is False, url


def test_a_non_http_scheme_is_refused():
    for url in ("file:///etc/passwd", "ftp://example.com/x", "//i0.hdslb.com/a.jpg"):
        assert _is_bili_url(url) is False, url
