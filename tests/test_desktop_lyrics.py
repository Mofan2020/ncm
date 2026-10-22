"""The desktop lyrics overlay must receive the lines on *every* push.

The overlay used to get the lines only on the first push per window, keyed on
``id(target)``.  Because the normal flow is "open the overlay, then press
play", that one push carried an empty array and the overlay stayed blank for
the rest of the session -- and switching tracks never updated it.
"""
from __future__ import annotations

import json

import pytest


class FakeWindow:
    """Stands in for ``pywebview.Window``: records ``evaluate_js`` calls."""

    def __init__(self):
        self.calls: list[str] = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def destroy(self):
        self.calls.append("DESTROYED")


@pytest.fixture()
def bridge(isolated_home):
    from src.gui.bridge import GuiBridge

    return GuiBridge()


def _payload(script: str) -> dict:
    """``handleLyricsUpdate({...})`` -> the parsed dict."""
    start = script.index("(") + 1
    end = script.rindex(")")
    return json.loads(script[start:end])


def test_the_first_push_carries_the_lines(bridge):
    window = FakeWindow()
    bridge._desktop_lyrics_window = window
    lines = [{"time": 0, "text": "hello", "translation": ""}]
    assert bridge.update_desktop_lyrics(lines, 0)["success"] is True
    payload = _payload(window.calls[-1])
    assert payload["full"] is True
    assert payload["lines"] == lines


def test_a_later_push_still_carries_the_lines(bridge):
    """This is the bug: the second push used to be index-only."""
    window = FakeWindow()
    bridge._desktop_lyrics_window = window
    first = [{"time": 0, "text": "hello", "translation": ""}]
    bridge.update_desktop_lyrics(first, 0)

    second = [{"time": 0, "text": "other song", "translation": ""}]
    bridge.update_desktop_lyrics(second, 0)

    payload = _payload(window.calls[-1])
    assert payload["full"] is True
    assert payload["lines"] == second, "switching tracks must replace the lines"


def test_opening_the_overlay_before_playback_still_ends_up_with_lyrics(bridge):
    """Open overlay (no lyrics yet), then lyrics arrive."""
    window = FakeWindow()
    bridge._desktop_lyrics_window = window
    bridge.update_desktop_lyrics([], -1)          # overlay opened, nothing playing

    lines = [{"time": 0, "text": "later", "translation": "后来"}]
    bridge.update_desktop_lyrics(lines, 0)        # lyrics finished loading

    payload = _payload(window.calls[-1])
    assert payload["lines"] == lines


def test_an_empty_lyrics_array_is_sent_rather_than_skipped(bridge):
    """Clearing a track has to clear the overlay, not leave the old song."""
    window = FakeWindow()
    bridge._desktop_lyrics_window = window
    bridge.update_desktop_lyrics([{"time": 0, "text": "old", "translation": ""}], 0)
    bridge.update_desktop_lyrics([], -1)
    payload = _payload(window.calls[-1])
    assert payload["full"] is True
    assert payload["lines"] == []


def test_a_push_without_a_window_is_a_no_op(bridge):
    bridge._desktop_lyrics_window = None
    result = bridge.update_desktop_lyrics([], -1)
    assert result["success"] is False


def test_a_dead_window_does_not_raise(bridge):
    class Exploding(FakeWindow):
        def evaluate_js(self, script):
            raise RuntimeError("window is gone")

    bridge._desktop_lyrics_window = Exploding()
    result = bridge.update_desktop_lyrics([], -1)
    assert result["success"] is False


def test_the_overlay_url_encodes_the_colour(bridge):
    """``#ffffff`` used to start the URL fragment, losing every later setting."""
    from urllib.parse import parse_qs, urlparse

    captured = {}

    class FakeModule:
        windows: list = []

        @staticmethod
        def create_window(**kwargs):
            captured.update(kwargs)
            return FakeWindow()

    class FakeEvents:
        def __init__(self):
            self.closed = type("E", (), {"__iadd__": lambda s, o: s})()

    window = FakeWindow()
    window.events = FakeEvents()
    bridge._webview_module = FakeModule
    bridge._desktop_lyrics_window = None
    bridge._call_js = lambda *a, **k: None

    bridge.open_desktop_lyrics()

    url = captured.get("url") or ""
    query = parse_qs(urlparse(url).query)
    assert query.get("color") == ["#ffffff"], url
    assert "opacity" in query, url
    assert "showTranslation" in query, url
