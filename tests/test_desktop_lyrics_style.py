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
