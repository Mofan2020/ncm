"""The lyrics panel: centring, the empty message, dual-line and karaoke.

These are layout/rendering rules that no Python code touches, so they are
pinned at the source level: each assertion names the mistake it prevents.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
CSS = (ASSETS / "style.css").read_text(encoding="utf-8")
JS = (ASSETS / "app.js").read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop the comments so their prose cannot satisfy an assertion."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    # `//` starts a line comment, but `https://` must survive.
    return re.sub(r"(?<!:)//[^\n]*", "", text)


def _css_rule(selector: str) -> str:
    """The declaration block of ``selector`` (first exact match), no comments."""
    pattern = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}", re.S)
    match = pattern.search(_strip_comments(CSS))
    assert match, f"{selector} is missing from style.css"
    return match.group(1)


def _js_body(name: str) -> str:
    """The body of a JS function, with comments stripped."""
    source = _strip_comments(JS)
    start = source.index(name)
    rest = source[start + len(name):]
    end = rest.find("\nfunction ")
    return rest[:end] if end >= 0 else rest


# ------------------------------------------------------------------ centring
def test_the_lyrics_padding_is_not_a_percentage():
    """A percentage padding resolves against the WIDTH, not the height.

    `padding: 40% 18px` pushed the lyrics down by 40% of the panel's width.
    That only looked centred when the panel happened to be about as wide as
    it is tall -- on a wide, short window the lyrics sat far too low.
    """
    block = _css_rule(".lyrics")
    padding = re.search(r"padding:\s*([^;]+);", block)
    assert padding, "the .lyrics rule must set its padding"
    vertical = padding.group(1).split()[0]
    assert "%" not in vertical, (
        "the vertical padding must be px (--lyrics-pad), not a percentage: "
        f"found {vertical!r}")


def test_the_lyrics_padding_comes_from_the_box_height():
    block = _css_rule(".lyrics")
    assert "var(--lyrics-pad" in block


def test_the_scroll_box_is_the_offset_parent():
    """`offsetTop` is relative to the nearest positioned ancestor.

    Without `position: relative` on the scroll box that ancestor was
    `.lyrics-panel`, which contains the header, so every scroll target was
    short by the header's height and the active line landed above centre.
    """
    block = _css_rule(".lyrics")
    assert "position: relative" in block


def test_the_padding_is_computed_from_the_height():
    assert "function syncLyricsPadding" in JS
    body = _js_body("function syncLyricsPadding")
    assert "clientHeight" in body, "the padding has to come from the box's height"
    assert "--lyrics-pad" in body


def test_the_padding_follows_a_resize():
    assert "ResizeObserver" in JS
    assert "watchLyricsBox" in JS


def test_centring_uses_rectangles_not_offset_top():
    """`offsetTop` is relative to the offsetParent; rectangles are absolute."""
    body = _js_body("function centerLyricLine")
    assert "getBoundingClientRect" in body
    assert "offsetTop" not in body, (
        "centerLyricLine must not use offsetTop -- it is measured against "
        "the offsetParent, not the scroll box")


def test_the_highlight_scrolls_through_the_helper():
    body = _js_body("function updateLyricHighlight")
    assert "centerLyricLine(box" in body


# ------------------------------------------------------------- empty message
def test_the_empty_message_is_centred():
    """The no-lyrics message must sit in the middle of the area.

    It used to be positioned by the panel's percentage padding, which on a
    short panel put it below the visible box -- the usual "暂无歌词 never
    shows" complaint.
    """
    assert ".lyrics.is-empty" in CSS
    block = _css_rule(".lyrics.is-empty")
    assert "align-items: center" in block
    assert "justify-content: center" in block


def test_the_empty_message_has_no_stray_offset():
    block = _css_rule(".lyrics .empty")
    assert "padding: 0" in block or "padding-top" not in block


def test_both_message_paths_use_the_centred_helper():
    """Loading and "no lyrics" both go through setLyricsMessage."""
    assert "function setLyricsMessage" in JS
    assert "setLyricsMessage(box, t('lyrics.loading'))" in JS
    assert "setLyricsMessage(box, t('lyrics.none'))" in JS


def test_the_empty_class_is_cleared_when_lyrics_arrive():
    body = _js_body("function renderLyricLines")
    assert "clearLyricsMessage(box)" in body


# ---------------------------------------------------------------- dual line
def test_toggling_a_mode_restores_the_highlight():
    """Rebuilding the DOM used to reset the index and scroll to the top.

    Nothing restored them until the next `timeupdate`, so switching modes
    while paused looked broken: no active line, list back at line one.
    """
    body = _js_body("function renderLyricLines")
    assert "updateLyricHighlight(true)" in body, (
        "renderLyricLines must re-apply the highlight at the current time")
    assert "box.scrollTop = 0" not in body, (
        "the renderer must not jump back to the top of the list")


def test_the_highlight_can_be_forced():
    assert "function updateLyricHighlight(force)" in JS


def test_dual_lines_group_original_and_translation_per_row():
    body = _js_body("function renderLyricLines")
    assert "lyric-line-wrapper" in body
    assert "lyric-original" in body


# ------------------------------------------------------------------ karaoke
def test_karaoke_splits_the_line_into_characters():
    assert "function karaokeTokens" in JS
    assert "karaoke-char" in JS
    assert "karaoke-text" in JS


def test_karaoke_handles_cjk_and_latin_differently():
    """CJK gets one span per character; Latin gets whole words."""
    body = _js_body("function karaokeTokens")
    assert "KARAOKE_CJK" in body
    assert "word +=" in body, "Latin text has to accumulate into words"


def test_karaoke_progress_is_driven_by_time():
    assert "function updateKaraokeProgress" in JS
    body = _js_body("function updateKaraokeProgress")
    assert "dataset.start" in body
    assert "dataset.end" in body
    assert "is-sung" in body


def test_karaoke_runs_on_every_tick_not_only_on_a_line_change():
    """A line-level index changes once per line; karaoke needs every tick."""
    body = _js_body("function updateLyricHighlight")
    karaoke_at = body.index("updateKaraokeProgress")
    early_return = body.index("if (!force && found === player.lyricIndex) return;")
    assert karaoke_at < early_return, (
        "the karaoke progress must be updated before the early return")


def test_karaoke_spaces_do_not_consume_the_line():
    body = _js_body("function updateKaraokeProgress")
    assert "textContent.trim()" in body


def test_the_block_fill_karaoke_rule_is_gone():
    """The old `::before` filled the whole line the moment it was active."""
    assert ":before" not in _css_rule(".lyric-line.lyric-karaoke")
    assert "lyric-karaoke::before" not in CSS
    assert "lyric-karaoke.is-active::before" not in CSS


def test_the_karaoke_styles_exist():
    assert ".karaoke-char" in CSS
    assert "is-sung" in CSS


# -------------------------------------------------------------- the mobile
def test_the_mobile_mirror_uses_the_same_centring():
    body = _js_body("function updateMobileLyricHighlight")
    assert "centerLyricLine(box" in body
    assert "offsetTop" not in body


# ------------------------------------------------------- scrolling reliably
def test_the_scroll_does_not_depend_on_native_smooth_behaviour():
    """`behavior: 'smooth'` is skipped in headless webviews and under
    reduced-motion, which left the active line wherever it happened to be.
    The glide is driven from a timer so it always lands."""
    assert "function smoothScrollTo" in JS
    body = _js_body("function smoothScrollTo")
    assert "setInterval" in body
    assert "behavior: 'auto'" in body


def test_the_css_scroll_behaviour_is_auto():
    """With `scroll-behavior: smooth` in the CSS, a plain assignment
    animates and cannot be used as an instant fallback."""
    block = _css_rule(".lyrics")
    assert "scroll-behavior: auto" in block


def test_a_new_target_cancels_the_previous_glide():
    body = _js_body("function smoothScrollTo")
    assert "clearInterval" in body, (
        "a second target must cancel the first animation, or two glides "
        "fight over the same list")


def test_the_glide_ends_exactly_on_target():
    body = _js_body("function smoothScrollTo")
    assert "progress >= 1" in body


def test_the_height_report_survives_a_stalled_frame_loop():
    """The overlay used to rely on requestAnimationFrame alone; frames stop
    being delivered when the window is occluded."""
    overlay = (ASSETS / "desktop-lyrics.js").read_text(encoding="utf-8")
    body = overlay[overlay.index("function reportContentHeight"):]
    body = body[:body.index("\n    function ", 10)]
    assert "requestAnimationFrame" in body
    assert "setTimeout" in body, (
        "a timer is needed as well, in case frames are not delivered")
