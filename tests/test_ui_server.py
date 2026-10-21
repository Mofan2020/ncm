"""The /ui route: serving the interface over loopback HTTP instead of file://.

Windows cannot open a ``file://`` URL whose path is not pure ASCII.  With a
Chinese install directory WebView2 shows its "file may have been deleted or
moved" page instead of the app, while CI (ASCII paths) and macOS both look
fine.  These tests pin the HTTP route, the non-ASCII case specifically, and
the guards that keep it from becoming a file-read hole.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import requests

from src.core.mediaserver import MediaServer

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src/gui/assets"
UI_FILES = ("index.html", "style.css", "app.js", "desktop-lyrics.html",
            "desktop-lyrics.js")


@pytest.fixture
def ui_server(tmp_path):
    """A server whose UI root is a non-ASCII path, like a real install."""
    media = MediaServer()
    media._initialized = False
    media.__init__()
    assets = tmp_path / "下载" / "NCM" / "_resources" / "src" / "gui" / "assets"
    assets.mkdir(parents=True)
    for name in UI_FILES:
        shutil.copy(ASSETS / name, assets / name)
    media.set_ui_root(assets)
    media.start()
    yield media
    media.stop()


# --------------------------------------------------------------- the regression

def test_url_is_pure_ascii_even_from_a_chinese_path(ui_server):
    """The whole point: no non-ASCII ever reaches the webview."""
    url = ui_server.ui_url("index.html", query="theme=dark&mode=dark&lang=zh_cn")
    assert url.isascii(), f"url is not ASCII: {url}"
    assert url.startswith("http://127.0.0.1:")
    assert "theme=dark" in url


def test_index_is_served_from_a_chinese_install_path(ui_server):
    response = requests.get(ui_server.ui_url("index.html"), timeout=10)
    assert response.status_code == 200
    assert "text/html" in response.headers["Content-Type"]
    assert len(response.content) > 10_000


@pytest.mark.parametrize("name,expected", [
    ("style.css", "text/css"),
    ("app.js", "text/javascript"),
    ("desktop-lyrics.html", "text/html"),
])
def test_sibling_assets_are_served_with_usable_types(ui_server, name, expected):
    """index.html references these relatively, so they must resolve too."""
    response = requests.get(ui_server.ui_url(name), timeout=10)
    assert response.status_code == 200
    assert expected in response.headers["Content-Type"]


def test_query_string_reaches_the_page(ui_server):
    url = ui_server.ui_url("index.html", query="theme=dark&mode=dark&lang=zh_cn")
    assert url.endswith("?theme=dark&mode=dark&lang=zh_cn")


# ------------------------------------------------------------------- guards

@pytest.mark.parametrize("parts", [
    ["..", "..", "..", "etc", "passwd"],
    [".."],
    [""],
    ["nope.html"],
])
def test_traversal_and_unknown_files_are_refused(ui_server, parts):
    assert ui_server.resolve_ui_asset(parts) is None


def test_traversal_over_http_is_404(ui_server):
    response = requests.get(ui_server.ui_url("..", "..", "..", "etc", "passwd"),
                             timeout=10)
    assert response.status_code == 404


def test_a_wrong_token_is_refused(ui_server):
    bad = ui_server.base_url.replace(ui_server.token, "not-the-token")
    response = requests.get(f"{bad}/ui/index.html", timeout=10)
    assert response.status_code == 404


def test_ui_root_can_be_disabled(ui_server):
    ui_server.set_ui_root(None)
    assert ui_server.ui_root is None
    assert ui_server.resolve_ui_asset(["index.html"]) is None


def test_missing_ui_root_serves_nothing(tmp_path):
    media = MediaServer()
    media._initialized = False
    media.__init__()
    media.set_ui_root(tmp_path / "does-not-exist")
    assert media.ui_root is None


def test_health_route_still_works(ui_server):
    """The playback probe must not regress."""
    response = requests.get(ui_server.url_for_health(), timeout=10)
    assert response.status_code == 200
    assert response.text.strip() == "ok"
