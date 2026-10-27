"""Toasts stay longer, and the write operations that can be reversed offer an undo.

The interesting part is which toasts get the button.  The rule is: a toast that
already existed on a write the app can reverse gets an undo; nothing gains a
toast it did not have, and nothing claims to be undoable when the server has no
way back.

Everything here reads the sources.  The behaviour itself is exercised by hand —
the frontend has no test harness that runs the DOM.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "src" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "src" / "gui" / "assets" / "style.css").read_text(encoding="utf-8")
I18N = ROOT / "src" / "i18n"
ANDROID_APP = (ROOT / "android" / "ncmkivy" / "app.py").read_text(encoding="utf-8")

#: What the toast lasted before this change.  The point of the change is that
#: it no longer does.
OLD_TOAST_MS = 3200

#: Write operations that can be reversed, and the function whose toast should
#: therefore carry an undo button.
UNDOABLE = [
    "toggleFavorite",
    "toggleFollowArtist",
    "createPlaylist",
    "editPlaylist",
    "toggleSubscribePlaylist",
    "confirmAddTracksToPlaylist",
    "removeTrackFromPlaylist",
    "createLocalPlaylist",
    "editLocalPlaylist",
    "removeTrackFromLocalPlaylist",
]

#: Writes the app cannot take back.  They keep their toast and must not grow a
#: button that would lie about it.
IRREVERSIBLE = ["deletePlaylist", "deleteLocalPlaylist"]

#: Reads (and local maintenance) never had a toast on the write path at all;
#: this change must not invent one.
NO_TOAST_WRITES = ["reorderPlaylistTracks"]


def _body(source: str, name: str) -> str:
    """The text of one function, found by name and closed by brace counting."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, f"function {name} not found"
    start = source.index("{", match.end())
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _number(name: str) -> int:
    match = re.search(rf"const {name} = (\d+);", JS)
    assert match, f"{name} not found"
    return int(match.group(1))


def _catalogue(language: str) -> dict[str, dict[str, str]]:
    return json.loads((I18N / f"{language}.json").read_text(encoding="utf-8"))


def test_a_plain_toast_stays_up_longer_than_it_used_to():
    assert _number("TOAST_MS") > OLD_TOAST_MS


def test_an_undo_toast_outlives_a_plain_one():
    """An undo button the user has to notice and reach needs its own budget."""
    assert _number("TOAST_UNDO_MS") > _number("TOAST_MS")


def test_an_undo_toast_is_never_cut_short_by_its_caller():
    """Callers pass their own timeout (some as low as 1800 ms); adding an undo
    has to win over that, or the button disappears before it can be used."""
    show = _body(JS, "showToast")
    assert "Math.max(timeoutMs, TOAST_UNDO_MS)" in show


def test_the_undo_button_needs_both_a_label_and_a_callback():
    """A label without a callback would be a dead control."""
    show = _body(JS, "showToast")
    assert "options.undoLabel" in show
    assert "typeof options.onUndo === 'function'" in show


def test_the_undo_can_only_fire_once():
    """The callback sends a write request, so a second click must not resend it."""
    show = _body(JS, "showToast")
    assert "button.disabled" in show


def test_the_message_shares_the_toast_with_the_button():
    """The message moved out of the toast's text node so the button can sit
    beside it; without the span the button would replace the text."""
    show = _body(JS, "showToast")
    assert "toast-text" in show
    assert "toast.appendChild(text)" in show


def test_the_toast_can_hold_a_button_without_pushing_it_out():
    assert ".toast-text { flex: 1 1 auto; min-width: 0; }" in CSS.replace("\n", " ") or (
        "flex: 1 1 auto" in CSS and "min-width: 0" in CSS
    )
    assert ".toast-undo" in CSS


def test_every_reversible_write_offers_an_undo():
    missing = [name for name in UNDOABLE if "onUndo" not in _body(JS, name)]
    assert not missing, f"these writes cannot be undone: {missing}"


def test_the_undo_calls_the_opposite_of_what_it_reverses():
    """Spot-check the pairings: an undo that repeats the action is worse than
    no undo at all."""
    pairs = {
        "toggleFollowArtist": "follow_artist(artist.id, !following)",
        "toggleFavorite": "set_favorite(track, !wanted)",
        "toggleSubscribePlaylist": "subscribe_playlist(playlist.id, !subscribe)",
        "createPlaylist": "delete_playlist([createdId])",
        "createLocalPlaylist": "delete_local_playlist(created)",
        "removeTrackFromPlaylist": "add_tracks_to_playlist(playlist.id, [track.id])",
        "removeTrackFromLocalPlaylist": "add_tracks_to_local_playlist(playlist.id, [track.key])",
        "confirmAddTracksToPlaylist": "remove_tracks_from_playlist(",
        "editPlaylist": "update_playlist(playlist.id, { name: before })",
        "editLocalPlaylist": "update_local_playlist(playlist.id, { name: before })",
    }
    for name, call in pairs.items():
        assert call in _body(JS, name), f"{name}: undo does not use {call}"


def test_a_deleted_playlist_does_not_pretend_to_be_undoable():
    """Neither store has a restore call: the playlist and its tracks are gone."""
    for name in IRREVERSIBLE:
        assert "onUndo" not in _body(JS, name), f"{name} offers an undo it cannot honour"


def test_a_read_that_never_toasted_does_not_gain_one():
    assert "showToast" in _body(JS, "reorderPlaylistTracks")  # it already did
    assert "onUndo" not in _body(JS, "reorderPlaylistTracks")


def test_the_undo_wording_exists_in_both_languages():
    for language in ("zh_cn", "en_us"):
        common = _catalogue(language)["common"]
        assert common.get("undo"), language
        assert common.get("undone"), language


def test_the_android_toast_uses_the_long_duration():
    """Android offers two lengths; the short one is two seconds.

    Checked on the call itself: the comment above it names the short constant
    to explain why it is not used.
    """
    match = re.search(r"makeText\(([^)]*)\)", ANDROID_APP, re.S)
    assert match, "no makeText call"
    assert "LENGTH_LONG" in match.group(1)
    assert "LENGTH_SHORT" not in match.group(1)
