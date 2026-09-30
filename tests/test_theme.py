"""Tests for the light/dark theme plumbing (DOM styling + native chrome)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.gui.theme import (
    BACKGROUND_COLORS,
    THEMES,
    apply_native_theme,
    effective_theme,
    normalize_theme,
    system_theme,
    window_background_color,
)

STYLE = Path(__file__).resolve().parent.parent / "src" / "gui" / "assets" / "style.css"
HTML = Path(__file__).resolve().parent.parent / "src" / "gui" / "assets" / "index.html"


@pytest.mark.parametrize("value,expected", [
    ("light", "light"),
    ("dark", "dark"),
    ("system", "system"),
    ("  DARK ", "dark"),
    ("DARK", "dark"),
    ("", "system"),
    (None, "system"),
    ("nonsense", "system"),
    (7, "system"),
])
def test_normalize_theme(value, expected):
    assert normalize_theme(value) == expected


def test_themes_constant():
    assert THEMES == ("light", "dark", "system")


def test_effective_theme_for_explicit_modes():
    assert effective_theme("light") == "light"
    assert effective_theme("dark") == "dark"


def test_effective_theme_for_system_uses_the_os(monkeypatch):
    monkeypatch.setattr("src.gui.theme.system_theme", lambda: "dark")
    assert effective_theme("system") == "dark"
    monkeypatch.setattr("src.gui.theme.system_theme", lambda: "light")
    assert effective_theme("system") == "light"


def test_system_theme_returns_a_known_value():
    assert system_theme() in ("light", "dark")


def test_background_colour_follows_the_effective_theme(monkeypatch):
    assert window_background_color("dark") == BACKGROUND_COLORS["dark"]
    # unknown modes fall back to `system`, i.e. whatever the OS reports
    monkeypatch.setattr("src.gui.theme.system_theme", lambda: "light")
    assert window_background_color("bogus") == BACKGROUND_COLORS["light"]
    monkeypatch.setattr("src.gui.theme.system_theme", lambda: "dark")
    assert window_background_color("system") == BACKGROUND_COLORS["dark"]


def test_background_colours_match_the_stylesheet():
    """The window colour pywebview paints must equal the CSS --bg variable."""
    css = STYLE.read_text(encoding="utf-8")
    light = re.search(r":root\s*\{[^}]*--bg:\s*(#[0-9a-fA-F]{6})", css)
    dark = re.search(r'\[data-theme="dark"\]\s*\{[^}]*--bg:\s*(#[0-9a-fA-F]{6})', css)
    assert light and dark, "could not read --bg from style.css"
    assert light.group(1).lower() == BACKGROUND_COLORS["light"].lower()
    assert dark.group(1).lower() == BACKGROUND_COLORS["dark"].lower()


def test_stylesheet_defines_both_palettes():
    css = STYLE.read_text(encoding="utf-8")
    assert '[data-theme="dark"]' in css
    assert "color-scheme: dark" in css
    assert "color-scheme: light" in css
    # every variable used inside the dark block must exist (no hardcoded gaps)
    dark_block = css.split('[data-theme="dark"]')[1].split("}")[0]
    assert "--text" in dark_block and "--surface" in dark_block and "--border" in dark_block


def test_window_background_can_be_applied_after_the_fact(monkeypatch):
    """pywebview cannot recolour a live window; we recolour the page instead."""
    from src.gui.theme import BACKGROUND_COLORS as colours

    assert set(colours) == {"light", "dark"}


def test_html_applies_the_theme_before_the_first_paint():
    """A light flash in dark mode is what this guards against."""
    html = HTML.read_text(encoding="utf-8")
    head = html.split("<body")[0]
    assert "data-theme" in head, "the theme must be set inside <head>"
    assert "prefers-color-scheme" in head
    assert "URLSearchParams" in head
    # the inline script has to run before the stylesheet is applied to the body
    assert head.index("data-theme") < head.index("</head>")


def test_html_has_the_theme_toggle():
    html = HTML.read_text(encoding="utf-8")
    assert 'id="btn-theme"' in html
    assert 'data-i18n-title="settings.theme_toggle"' in html


def test_apply_native_theme_never_raises():
    class Bogus:
        pass

    assert isinstance(apply_native_theme(None, "dark"), bool)
    assert isinstance(apply_native_theme(Bogus(), "system"), bool)
    assert isinstance(apply_native_theme(Bogus(), "bogus"), bool)


def test_apply_native_theme_handles_a_window_without_a_handle():
    """A handle object with no usable value must degrade gracefully."""

    class FakeWindow:
        class _Native:
            Handle = object()  # not an IntPtr, not an int

        native = _Native()
        title = None

    assert isinstance(apply_native_theme(FakeWindow(), "dark"), bool)


# ------------------------------------------------------------------- bridge

def test_bridge_set_theme_persists_and_notifies(bridge):
    calls = []

    def fake_apply(mode):
        calls.append(mode)
        return True

    bridge.apply_theme = fake_apply
    result = bridge.set_theme("dark")
    assert result["success"] is True
    assert result["theme"] == "dark"
    assert result["effective"] == "dark"
    assert result["native_applied"] is True
    assert calls == ["dark"]
    assert bridge.get_settings()["ui"]["theme"] == "dark"
    assert bridge.set_theme("nonsense")["theme"] == "system"
    assert calls == ["dark", "system"]


def test_bridge_update_settings_ui_syncs_the_native_chrome(bridge):
    calls = []
    bridge.apply_theme = lambda mode: calls.append(mode) or True
    bridge.update_settings("ui", {"theme": "light", "language": "en_us"})
    assert calls == ["light"]
    assert bridge.get_translations()["settings.theme_light"] == "Light"


def test_bridge_apply_theme_failure_is_not_fatal(bridge):
    def boom(_mode):
        raise RuntimeError("no window")

    bridge.apply_theme = boom
    assert bridge._sync_native_theme() is False
    assert bridge.set_theme("dark")["native_applied"] is False


def test_bridge_without_theme_hook(bridge):
    bridge.apply_theme = None
    assert bridge._sync_native_theme() is False
    assert bridge.set_theme("light")["success"] is True


def test_bridge_reports_the_system_theme(bridge):
    assert bridge.get_system_theme() in ("light", "dark")


def test_bridge_reset_settings_reapplies_the_theme(bridge):
    calls = []
    bridge.apply_theme = lambda mode: calls.append(mode) or True
    bridge.set_theme("dark")
    bridge.reset_settings()
    assert calls == ["dark", "system"]
    assert bridge.get_settings()["ui"]["theme"] == "system"
