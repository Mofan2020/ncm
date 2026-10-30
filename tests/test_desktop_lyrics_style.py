"""The desktop lyrics overlay: the height fix, the text styles, the settings."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"


@pytest.fixture()
def overlay_js() -> str:
    return (ASSETS / "desktop-lyrics.js").read_text(encoding="utf-8")


@pytest.fixture()
def overlay_html() -> str:
    return (ASSETS / "desktop-lyrics.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------- height
def test_the_height_is_reported_after_the_lyrics_are_rendered(overlay_js):
    """The window must grow *after* the text arrives, not only at startup.

    The content is 112 px at 18 px and 196 px at 36 px, but the native
    window is created at 120 px.  Reporting the height only once at
    startup left every larger font size clipped.
    """
    render = overlay_js[overlay_js.index("function renderLyricLines"):
                        overlay_js.index("function updateLyricsIndex")]
    assert "reportContentHeight()" in render, (
        "renderLyricLines must ask for a taller window once the lyrics are in")


def test_the_height_is_measured_after_the_layout(overlay_js):
    """Reading the box in the same tick as the DOM change gives the old height."""
    body = overlay_js[overlay_js.index("function reportContentHeight"):
                      overlay_js.index("function applyTextStyle")]
    assert "requestAnimationFrame" in body, (
        "the height must be measured on the next frame, not in the same tick")


def test_the_resize_call_reaches_the_backend(overlay_js):
    assert "resize_desktop_lyrics" in overlay_js


def test_the_window_leaves_room_for_the_drag_handle(overlay_js):
    """The handle sits 10px above the bubble; the window must fit it.

    The bubble is centred in the window, so the slack above it is
    ``(height - content) / 2``.  The handle needs 10px of it, so the
    window has to be at least 20px taller than the content.
    """
    body = overlay_js[overlay_js.index("function reportContentHeight"):
                      overlay_js.index("function applyTextStyle")]
    assert "+ 24" in body, (
        "the reported height must leave room for the drag handle")


def test_the_lyrics_fit_inside_the_reported_height(overlay_js):
    """The content must not be taller than the window that was asked for."""
    body = overlay_js[overlay_js.index("function reportContentHeight"):
                      overlay_js.index("function applyTextStyle")]
    assert "getBoundingClientRect().height" in body
    assert "Math.ceil" in body


# --------------------------------------------------------------- text styles
def test_three_text_styles_are_supported(overlay_js):
    for mode in ("transparent", "translucent", "white"):
        assert f"'{mode}'" in overlay_js, f"the {mode} text style is missing"


def test_the_transparent_style_uses_an_outline(overlay_js):
    body = overlay_js[overlay_js.index("function applyTextStyle"):
                      overlay_js.index("function applyBackgroundMode")]
    assert "webkitTextStroke" in body


def test_the_white_style_is_fully_opaque(overlay_js):
    body = overlay_js[overlay_js.index("function applyTextStyle"):
                      overlay_js.index("function applyBackgroundMode")]
    # The 'white' branch must not dim the rows.
    assert "rowOpacity = 1" in body


def test_the_text_style_is_read_from_the_url(overlay_html):
    assert "textStyle" in overlay_html
    assert "['transparent', 'translucent', 'white']" in overlay_html


def test_the_text_style_is_part_of_the_saved_settings(overlay_js):
    assert "textStyle: 'white'" in overlay_js


# -------------------------------------------------------------- the settings
def test_the_settings_model_knows_the_three_styles(isolated_home):
    from src.config.settings import Settings

    settings = Settings()
    assert settings.ui.desktop_lyrics_text_style == "white"


def test_an_unknown_text_style_falls_back_to_white(isolated_home):
    from src.config.settings import Settings

    settings = Settings()
    settings.update_ui(desktop_lyrics_text_style="nonsense")
    assert settings.ui.desktop_lyrics_text_style == "white"


def test_the_three_styles_survive_a_save_and_load(isolated_home):
    from src.config.settings import Settings

    for mode in ("transparent", "translucent", "white"):
        settings = Settings()
        settings.update_ui(desktop_lyrics_text_style=mode)
        Settings._instance = None
        assert Settings().ui.desktop_lyrics_text_style == mode


def test_the_bridge_saves_the_desktop_lyrics_options(bridge):
    """The allowlist must not drop them.

    They were dropped before: the user picked a font size, the UI showed it,
    and the next launch started from the default again.
    """
    result = bridge.update_settings("ui", {
        "desktop_lyrics_font_size": 24,
        "desktop_lyrics_color": "#ff0000",
        "desktop_lyrics_opacity": 0.5,
        "desktop_lyrics_show_translation": False,
        "desktop_lyrics_background": "none",
        "desktop_lyrics_text_style": "translucent",
    })
    assert result["success"] is True
    ui = result["settings"]["ui"]
    assert ui["desktop_lyrics_font_size"] == 24
    assert ui["desktop_lyrics_color"] == "#ff0000"
    assert ui["desktop_lyrics_opacity"] == 0.5
    assert ui["desktop_lyrics_show_translation"] is False
    assert ui["desktop_lyrics_background"] == "none"
    assert ui["desktop_lyrics_text_style"] == "translucent"


def test_the_bridge_reports_the_desktop_lyrics_options(bridge):
    ui = bridge.get_settings()["ui"]
    for key in ("desktop_lyrics_font_size", "desktop_lyrics_color",
                "desktop_lyrics_opacity", "desktop_lyrics_show_translation",
                "desktop_lyrics_background", "desktop_lyrics_text_style"):
        assert key in ui, f"{key} is missing from the settings payload"


# ------------------------------------------------------------------ the URL
def test_the_overlay_url_carries_the_text_style():
    from src.gui.bridge import GuiBridge

    source = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")
    assert '"textStyle": ui.desktop_lyrics_text_style' in source
    assert GuiBridge is not None


# ------------------------------------------------------- the floating panel
def test_the_overlay_window_is_transparent(bridge):
    """The panel must not paint a white rectangle on the wallpaper.

    pywebview's default window background is an opaque #FFFFFF, and macOS
    draws a frameless window as a rounded rectangle in that colour -- the
    lyrics floated in a white slab with the dark card inside it.
    """
    captured: dict = {}

    class FakeModule:
        @staticmethod
        def create_window(**kwargs):
            captured.update(kwargs)
            window = type("W", (), {"events": type(
                "E", (), {"closed": type("C", (), {"__iadd__": lambda s, o: s})()})()})()
            return window

    bridge._webview_module = FakeModule
    bridge._desktop_lyrics_window = None
    bridge._call_js = lambda *a, **k: None

    bridge.open_desktop_lyrics()

    assert captured.get("transparent") is True, captured
    assert captured.get("background_color") == "#000000", captured


def test_the_overlay_colour_is_one_pywebview_accepts():
    """The window must actually be created, not rejected on the way.

    pywebview checks ``background_color`` against a 3-or-6-digit hex
    pattern and raises otherwise.  An 8-digit "#00000000" looks like the
    obvious way to say "transparent", but it is rejected: the window is
    never created and the button does nothing at all.
    """
    import re

    bridge_source = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")
    match = re.search(r'background_color="(#[0-9a-fA-F]*)"', bridge_source)
    assert match, "the desktop lyrics window has no background_color"
    colour = match.group(1)

    # The same pattern pywebview's create_window uses.
    assert re.match(r"^#(?:[0-9a-fA-F]{3}){1,2}$", colour), (
        f"{colour} is not a hex pywebview accepts -- the window would never "
        "be created")


def test_a_double_click_does_not_hide_the_lyrics(overlay_js):
    """A double click is a gesture people make by accident.

    It used to hide the whole overlay, and the hidden card has
    ``pointer-events: none`` -- so the lyrics were gone with no way back
    except closing and reopening the window from the main window.
    """
    assert "dblclick" not in overlay_js, (
        "the overlay must not hide itself on a double click")
    assert "toggleVisibility" not in overlay_js, (
        "the visibility toggle had exactly one caller: the double click")


def test_the_hidden_lyrics_can_be_brought_back(overlay_js, overlay_html):
    """Hiding has to leave a way out.

    The card fades out completely and stops taking pointer events, so the
    drag handle stays visible and clickable; clicking it restores the card.
    """
    assert "state.hidden" in overlay_js, "the handle has to know the state"
    hidden = overlay_js[overlay_js.index("handle.addEventListener('mousedown'"):
                        overlay_js.index("document.addEventListener('mousemove'")]
    assert "setVisibility(true)" in hidden, (
        "clicking the handle while hidden must bring the lyrics back")

    # The handle keeps its pointer events while the card is hidden.
    hidden_css = overlay_html[overlay_html.index(".desktop-lyrics-hidden .desktop-lyrics-drag-handle"):
                              overlay_html.index(".desktop-lyrics-hidden .desktop-lyrics-drag-handle:hover")]
    assert "pointer-events: none" not in hidden_css, hidden_css
    assert "pointer-events: auto" in hidden_css, hidden_css


def test_the_card_is_the_glass_panel_and_not_white(overlay_html):
    """The card is the translucent grey panel, not a white board.

    The material matches the main window's "liquid glass": a neutral grey
    that lets the wallpaper through, blurred, with a hairline edge.
    """
    assert "rgba(58, 58, 62, 0.62)" in overlay_html, (
        "the card has to be the translucent grey glass")
    assert "backdrop-filter: blur(var(--dl-blur))" in overlay_html
    # A readable text on a see-through panel needs a shadow of its own.
    assert "text-shadow" in overlay_html
