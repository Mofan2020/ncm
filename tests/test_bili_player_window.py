"""The Bilibili player window: the page, its contract and the bridge method.

The player window is a page of its own with its own script, so it needs the
same three links as the main interface: the ids of the markup, the i18n keys
and the names of the bridge methods.  A missing one shows up as an empty
button or a raw key on screen, never as a Python error.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
I18N = ROOT / "src" / "i18n"

PLAYER_HTML = (ASSETS / "bili-player.html").read_text(encoding="utf-8")
PLAYER_JS = (ASSETS / "bili-player-window.js").read_text(encoding="utf-8")
PLAYER_CSS = (ASSETS / "bili-player.css").read_text(encoding="utf-8")


def _flat_catalogue(name: str) -> dict[str, str]:
    data = json.loads((I18N / f"{name}.json").read_text(encoding="utf-8"))
    flat: dict[str, str] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, path + ".")
            else:
                flat[path] = value

    walk(data)
    return flat


def test_the_player_assets_are_there():
    """A missing file only shows up as an empty window."""
    for name in ("bili-player.html", "bili-player.css", "bili-player-window.js"):
        assert (ASSETS / name).exists(), name


def test_the_page_loads_its_own_script_and_style():
    assert 'href="bili-player.css"' in PLAYER_HTML
    assert 'src="bili-player.js"' in PLAYER_HTML
    assert 'src="bili-player-window.js"' in PLAYER_HTML


def test_every_element_id_the_player_script_uses_exists():
    # The player script has its own little `el()` helper, so both spellings
    # have to be looked at.
    ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", PLAYER_JS))
    ids |= set(re.findall(r"\bel\(['\"]([^'\"]+)['\"]\)", PLAYER_JS))
    missing = sorted(i for i in ids if f'id="{i}"' not in PLAYER_HTML)
    assert ids, "the player script should look up elements by id"
    assert missing == [], f"the player script refers to missing elements: {missing}"


def test_every_i18n_key_the_player_uses_exists():
    catalogue = _flat_catalogue("zh_cn")
    used = set(re.findall(r"\bt\(['\"]([A-Za-z0-9_.]+)['\"]", PLAYER_JS))
    missing = sorted(key for key in used if key not in catalogue)
    assert missing == [], f"the player uses undefined i18n keys: {missing}"


def test_every_bridge_method_the_player_uses_exists():
    from src.gui.bridge import GuiBridge

    used = set(re.findall(r"\bapi\.([a-z_][a-z0-9_]*)\(", PLAYER_JS))
    missing = sorted(
        name for name in used
        if not hasattr(GuiBridge, name) and name != "playBiliVideo"
    )
    assert used, "the player should call the bridge"
    assert missing == [], f"the player calls methods the bridge does not have: {missing}"


def test_the_player_window_is_opened_by_the_bridge(bridge):
    """A video opens in a window of its own, with the id in the address."""
    captured: dict = {}

    class FakeEvent:
        """Stands in for pywebview's event object."""

        def __init__(self):
            self.handlers: list = []

        def __iadd__(self, other):
            self.handlers.append(other)
            return self

    class FakeEvents:
        def __init__(self):
            self.closed = FakeEvent()

    class FakeWindow:
        def __init__(self):
            self.events = FakeEvents()
            self.destroyed = False

        def restore(self):
            return None

        def destroy(self):
            self.destroyed = True

    class FakeModule:
        @staticmethod
        def create_window(**kwargs):
            captured.update(kwargs)
            return FakeWindow()

    bridge._webview_module = FakeModule
    bridge._call_js = lambda *a, **k: None

    result = bridge.open_bili_player_window("BV1xx411c7mD", 2, 16)

    assert result["success"] is True
    assert result["opened"] is True
    assert captured.get("js_api") is bridge
    url = captured.get("url") or ""
    query = parse_qs(urlparse(url).query)
    assert query.get("bvid") == ["BV1xx411c7mD"], url
    assert query.get("page") == ["2"], url
    assert query.get("quality") == ["16"], url
    assert "bili-player.html" in url, url
    assert bridge._bili_player_windows, "the window has to be remembered"


def test_an_open_video_is_not_opened_twice(bridge):
    """The second click brings the window to the front instead."""
    calls: list = []

    class FakeEvent:
        """Stands in for pywebview's event object."""

        def __init__(self):
            self.handlers: list = []

        def __iadd__(self, other):
            self.handlers.append(other)
            return self

    class FakeEvents:
        def __init__(self):
            self.closed = FakeEvent()

    class FakeWindow:
        def __init__(self):
            self.events = FakeEvents()
            self.restored = 0

        def restore(self):
            self.restored += 1

    class FakeModule:
        @staticmethod
        def create_window(**kwargs):
            calls.append(kwargs)
            return FakeWindow()

    bridge._webview_module = FakeModule
    bridge._call_js = lambda *a, **k: None

    first = bridge.open_bili_player_window("BV1xx411c7mD")
    second = bridge.open_bili_player_window("BV1xx411c7mD")

    assert first["opened"] is True
    assert second["opened"] is False
    assert len(calls) == 1


def test_a_closed_window_is_forgotten(bridge):
    """The window is closed by the user: the next click opens a new one."""
    class FakeEvent:
        """Stands in for pywebview's event object."""

        def __init__(self):
            self.handlers: list = []

        def __iadd__(self, other):
            self.handlers.append(other)
            return self

    class FakeEvents:
        def __init__(self):
            self.closed = FakeEvent()

    class FakeWindow:
        def __init__(self):
            self.events = FakeEvents()

        def restore(self):
            return None

    class FakeModule:
        @staticmethod
        def create_window(**kwargs):
            return FakeWindow()

    bridge._webview_module = FakeModule
    bridge._call_js = lambda *a, **k: None

    bridge.open_bili_player_window("BV1xx411c7mD")
    window = bridge._bili_player_windows["BV1xx411c7mD"]
    for handler in window.events.closed.handlers:
        handler()

    assert "BV1xx411c7mD" not in bridge._bili_player_windows


def test_a_missing_video_id_is_refused(bridge):
    result = bridge.open_bili_player_window("")
    assert result["success"] is False


def test_the_frontend_opens_the_window_instead_of_the_modal():
    """The video plays in its own window, so the main window stays usable."""
    app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "open_bili_player_window" in app_js
    assert "function openBiliPlayerWindowFallback" in app_js
