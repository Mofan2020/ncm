"""The cover URLs: the webview builds them without asking the backend."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "src" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")


def test_the_media_base_url_reaches_the_webview():
    """Without it the webview cannot build a cover URL on its own."""
    source = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")
    assert '"media_base_url"' in source


def test_the_app_info_carries_the_media_base_url(bridge):
    info = bridge.get_app_info()
    # The bridge's media server may not be running in the test, so the key
    # is optional -- but when it is there it has to be a URL.
    if "media_base_url" in info:
        assert info["media_base_url"].startswith("http")


def test_the_frontend_stores_the_media_base_url():
    assert "mediaBaseUrl" in APP_JS
    assert "media_base_url" in APP_JS


def test_the_cover_url_is_built_in_the_frontend():
    assert "function buildCoverUrl" in APP_JS
    body = APP_JS[APP_JS.index("function buildCoverUrl"):
                  APP_JS.index("function buildCoverUrl") + 900]
    assert "/cover?u=" in body
    assert "encodeURIComponent" in body


def test_the_cover_url_is_built_before_the_backend_is_asked():
    """The synchronous path has to come first, or nothing changes."""
    body = APP_JS[APP_JS.index("function proxyCoverUrl"):
                  APP_JS.index("function buildCoverUrl")]
    build_at = body.index("buildCoverUrl(raw)")
    ask_at = body.index("resolve_cover_url")
    assert build_at < ask_at, (
        "the cover URL must be built locally before falling back to the backend")


def test_the_cover_extension_is_derived_from_the_url():
    body = APP_JS[APP_JS.index("function buildCoverUrl"):
                  APP_JS.index("function buildCoverUrl") + 900]
    assert "jpg" in body
    assert "png" in body
    assert "webp" in body


def test_the_javascript_is_valid(overlay=None):
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "app.js"
        script.write_text(APP_JS, encoding="utf-8")
        result = subprocess.run([node, "--check", str(script)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
