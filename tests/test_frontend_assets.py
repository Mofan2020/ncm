"""The frontend/backend contract: ids, i18n keys and bridge method names.

None of this shows up as a Python error at runtime -- a missing key renders a raw
`player.play` on screen and a missing id silently kills a button -- so the three
links are checked here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "src" / "gui" / "assets"
I18N = Path(__file__).resolve().parent.parent / "src" / "i18n"
BRIDGE = Path(__file__).resolve().parent.parent / "src" / "gui" / "bridge.py"
JS = (ASSETS / "app.js").read_text(encoding="utf-8")
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")
#: The icon helpers live in their own file (icons.js), which index.html loads
#: before app.js.  The identifier check has to see both.
ICONS_JS = (ASSETS / "icons.js").read_text(encoding="utf-8")


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


def _used_keys() -> set[str]:
    keys = set(re.findall(r"\bt\(['\"]([A-Za-z0-9_.]+)['\"]", JS))
    keys |= set(re.findall(r'data-i18n(?:-title|-placeholder)?="([^"]+)"', HTML))
    return keys


def _dynamic_prefixes() -> set[str]:
    """``t('player.mode_' + mode)`` -> ``player.mode_``."""
    return set(re.findall(r"\bt\(['\"]([A-Za-z0-9_.]+_)['\"]\s*\+", JS))


def test_every_element_id_the_script_uses_exists():
    ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", JS))

    missing = sorted(i for i in ids if f'id="{i}"' not in HTML)

    assert ids, "the script should look up elements by id"
    assert missing == [], f"app.js refers to missing elements: {missing}"


def test_every_i18n_key_the_frontend_uses_exists():
    catalogue = _flat_catalogue("zh_cn")
    used = _used_keys() | _dynamic_prefixes()
    # A trailing "_" or "." means "built by concatenation", checked per family.
    literal = {key for key in used if not key.endswith(("_", "."))}

    missing = sorted(k for k in literal if k not in catalogue)

    assert missing == [], f"frontend uses undefined i18n keys: {missing}"


def test_dynamic_i18n_families_are_complete():
    """The concatenated keys must exist for every value the code can produce."""
    catalogue = _flat_catalogue("zh_cn")
    families = {
        # `none` has no label: the UI clears the line instead (and an empty
        # translation is rejected by the i18n test), so it is not a key here.
        "lyrics.source_": ["netease", "sidecar", "embedded"],
        "player.mode_": re.findall(r"mode: '([a-z_]+)'", JS),
        "settings.theme_": ["light", "dark", "system"],
        "player.badge_": ["online", "local"],
        "download.status_": re.findall(r"status: '([a-z_]+)'", JS),
        # updateStatus() builds 'status.<key>' at the call site.
        "status.": re.findall(r"updateStatus\('([a-z_]+)'", JS),
    }
    for prefix, values in families.items():
        for value in sorted(set(values)):
            key = prefix + value
            if key not in catalogue:
                pytest.fail(f"missing i18n key {key}")

    assert "error.reason.no_local_copy" in catalogue


def test_every_bridge_method_the_frontend_calls_exists():
    methods = set(re.findall(r"^    def (\w+)", BRIDGE.read_text(encoding="utf-8"), re.M))
    called = set(re.findall(r"state\.api\.(\w+)\(", JS))

    missing = sorted(m for m in called if m not in methods)

    assert called, "the script should call the bridge"
    assert missing == [], f"app.js calls undefined bridge methods: {missing}"


def test_backend_pushes_have_a_matching_frontend_handler():
    """`_call_js('onFoo')` must land on a global `onFoo` in app.js."""
    pushed = set(re.findall(r'_call_js\(\s*["\'](\w+)["\']', BRIDGE.read_text(encoding="utf-8")))

    missing = sorted(name for name in pushed if f"function {name}(" not in JS)

    assert pushed, "the bridge should push at least one event"
    assert missing == [], f"app.js has no handler for: {missing}"


#: Built-ins and browser globals that are legitimately not defined in app.js.
_JS_GLOBALS = {
    "console", "document", "window", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "requestAnimationFrame", "parseInt", "parseFloat", "isNaN",
    "isFinite", "fetch", "alert", "confirm", "encodeURIComponent",
    "decodeURIComponent", "Boolean", "Error", "JSON", "Object", "Array", "Math",
    "Date", "String", "Number", "Promise", "Map", "Set", "Image", "URL", "Blob",
    "RegExp", "Infinity", "NaN", "undefined", "arguments", "true", "false",
    "null", "this", "globalThis", "Intl", "Audio", "Event", "FileReader",
    "MediaMetadata", "localStorage", "navigator", "prompt", "sessionStorage",
    "performance", "location", "history", "screen", "crypto", "indexedDB",
    "l", "panel", "ResizeObserver",
}

#: Reserved words: they are not variables, so they never need a declaration.
_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "finally", "try", "function",
    "return", "typeof", "instanceof", "new", "do", "await", "delete", "void",
    "in", "of", "else", "case", "async", "yield", "throw", "break", "continue",
    "const", "let", "var", "class", "extends", "super", "import", "export",
    "default", "get", "set", "static",
}


def _skip_quoted(source: str, start: int) -> int:
    """Index just past the ``'``/``"``/``` ` ``` string that opens at ``start``."""
    quote = source[start]
    index = start + 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if source[index] == quote:
            return index + 1
        index += 1
    return len(source)


def _skip_template(source: str, start: int) -> int:
    """Index just past the template literal that opens at ``start``.

    ``${...}`` holes are code, but they are dropped along with the literal text
    (checked elsewhere); they still have to be counted correctly or a ``}`` of
    a nested object ends the scan in the wrong place.
    """
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            return index + 1
        if char == "$" and source[index + 1:index + 2] == "{":
            depth, index = 1, index + 2
            while index < len(source) and depth:
                inner = source[index]
                if inner in "'\"`":
                    index = _skip_quoted(source, index)
                    continue
                if inner == "{":
                    depth += 1
                elif inner == "}":
                    depth -= 1
                index += 1
            continue
        index += 1
    return len(source)


#: After these, a ``/`` opens a regex literal rather than dividing.
_JS_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%^~<>") | {
    "return", "typeof", "case", "in", "of", "new", "delete", "do", "else",
    "yield", "await", "instanceof", "void",
}


def _skip_regex(source: str, start: int) -> int:
    """Index just past the regex literal that opens at ``start``."""
    index = start + 1
    in_class = False
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(source) and source[index].isalpha():
                index += 1  # flags
            return index
        elif char == "\n":
            break
        index += 1
    return start + 1  # not a literal after all: leave the slash as code


def _js_code(source: str | None = None) -> str:
    """app.js reduced to code: comments, literals and props blanked out.

    A hand-rolled scanner rather than a pile of regexes -- an apostrophe in a
    comment, a ``//`` inside a string or a ``${}`` hole desynchronises any of
    them, and a wrong answer here is worse than no check at all.  Regex literals
    matter too: ``/^https?:\\/\\//`` would otherwise look like a comment.

    Pass ``source`` to scan another file (icons.js).
    """
    JS_source = JS if source is None else source
    out: list[str] = []
    index, length = 0, len(JS_source)
    previous = ""  # last significant character, to tell regex from division
    while index < length:
        pair = JS_source[index:index + 2]
        if pair == "//":
            newline = JS_source.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if pair == "/*":
            end = JS_source.find("*/", index + 2)
            index = length if end < 0 else end + 2
            out.append(" ")
            previous = " "
            continue
        char = JS_source[index]
        if char == "`":
            index = _skip_template(JS_source, index)
            out.append(" '' ")
            previous = "'"
            continue
        if char in "'\"":
            index = _skip_quoted(JS_source, index)
            out.append(" '' ")
            previous = "'"
            continue
        if char == "/" and (previous in _JS_REGEX_PRECEDERS or previous == ""):
            index = _skip_regex(JS_source, index)
            out.append(" '' ")
            previous = " "
            continue
        if not char.isspace():
            # A keyword counts as a whole word, so `return /re/` is seen correctly.
            if char.isalpha() or char in "_$":
                end = index
                while end < length and (JS_source[end].isalnum() or JS_source[end] in "_$"):
                    end += 1
                word = JS_source[index:end]
                out.append(word)
                previous = word if word not in _JS_KEYWORDS else " "
                index = end
                continue
            previous = char
        out.append(char)
        index += 1
    code = "".join(out)
    # `foo.bar` -> `foo.`: a property name is not a variable.
    return re.sub(r"\.[A-Za-z_$][\w$]*", ".", code)


def test_every_identifier_the_script_uses_is_defined():
    """A name nobody declares is a ReferenceError at runtime.

    Python cannot see it, so the window just comes up half dead -- v3.0.0 shipped
    a `renderSourceHeader()` call with no definition, which aborted boot() before
    the login badge was refreshed (the app looked logged out) and left the whole
    player uninitialised.  The check is on every *reference*, not only on call
    syntax, so a function handed to `safeRender()` is covered too.
    """
    code = _js_code()
    icons_code = _js_code(ICONS_JS)
    defined = set(re.findall(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", code))
    defined |= set(re.findall(r"\b(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", icons_code))
    defined |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b", icons_code))
    defined |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b", code))
    # Parameter names are defined too: `function safeRender(name, fn)` is why
    # `fn()` resolves, and `(resolve) => {}` is why `resolve(...)` does.
    signatures = re.findall(r"\b(?:async\s+)?function\s*[A-Za-z_$][\w$]*\s*\(([^)]*)\)", code)
    signatures += re.findall(r"\(([^()]*)\)\s*=>", code)
    signatures += re.findall(r"\b(?:async\s+)?function\s*\(([^)]*)\)", code)
    signatures += re.findall(r"\bcatch\s*\(([^)]*)\)", code)
    for params in signatures:
        defined |= set(re.findall(r"[A-Za-z_$][\w$]*", params))
    # Object literal keys (`{ kind, id, name }`) name nothing either.
    defined |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*:", code))
    used = set(re.findall(r"[A-Za-z_$][\w$]*", code))

    missing = sorted(used - defined - _JS_GLOBALS - _JS_KEYWORDS)

    assert used, "the script should use variables"
    assert missing == [], f"app.js uses undefined identifiers: {missing}"


# --------------------------------------------------------------- the refresh
def test_the_refresh_button_exists_and_is_wired_up():
    """There must be a way to re-read the list without restarting."""
    assert 'id="btn-refresh-list"' in HTML
    assert "btn-refresh-list" in JS
    assert "refreshCurrentSource" in JS


def test_the_refresh_button_has_a_title_in_both_languages():
    for lang in ("zh_cn", "en_us"):
        flat = _flat_catalogue(lang)
        assert flat.get("player.refresh_list"), f"{lang} misses player.refresh_list"


def test_the_refresh_skips_the_cache():
    """A refresh that reads the cache is not a refresh."""
    assert re.search(r"loadLiked\(true\)", JS), "the liked list must be read fresh"
    assert re.search(r"loadPlaylists\(true\)", JS), "the playlist list must be read fresh"
    assert re.search(r"openPlaylist\(\s*player\.playlistOpen\s*,\s*true\s*\)", JS), \
        "an open playlist must be re-read fresh"


def test_a_favorite_refreshes_the_list_it_came_from():
    """After a like the list on screen has to show the new state."""
    assert "result.liked_dirty" in JS
    assert re.search(r"if \(result\.liked_dirty\)\s*refreshCurrentSource\(\)", JS)


def test_the_bridge_passes_the_fresh_flag_through():
    bridge = BRIDGE.read_text(encoding="utf-8")
    assert "def get_playlist_tracks(self, playlist_id: str, fresh: bool = False)" in bridge
    assert "def get_playlists(self, fresh: bool = False)" in bridge
    assert "def get_liked_tracks(self, fresh: bool = False)" in bridge
    assert re.search(r"get_liked_song_ids\(\s*user\.user_id,\s*\n?\s*use_cache=not fresh\)", bridge)


# ---------------------------------------------------------------- followed artists
CSS = (ASSETS / "style.css").read_text(encoding="utf-8")


def test_the_followed_artists_source_is_in_the_sidebar():
    """"Followed artists" has to be reachable, not only via a search."""
    match = re.search(r'data-source="followed_artists"', HTML)
    assert match, "no sidebar entry for the followed artists"
    assert 'data-i18n="player.source_followed_artists"' in HTML


def test_the_followed_artists_source_is_selected_and_paged():
    assert re.search(r"source === 'followed_artists'\)\s*await loadFollowedArtists\(\)", JS)
    assert re.search(r"loadFollowedArtists\(true\)", JS), "the list has to be pageable"
    assert "FOLLOWED_PAGE" in JS


def test_the_followed_list_is_read_from_the_bridge():
    assert "state.api.get_followed_artists(" in JS
    assert "def get_followed_artists(" in BRIDGE.read_text(encoding="utf-8")


def test_an_artist_row_opens_the_artist_page():
    """A search result artist is not a track; it opens the page."""
    assert "function openArtistDetail(artist)" in JS
    assert "state.api.get_artist_detail(" in JS
    assert "def get_artist_detail(" in BRIDGE.read_text(encoding="utf-8")


def test_the_artist_page_has_a_working_follow_button():
    assert "state.api.follow_artist(" in JS
    assert "def follow_artist(" in BRIDGE.read_text(encoding="utf-8")
    # Both labels exist: a button that cannot say what it will do is a trap.
    for lang in ("zh_cn", "en_us"):
        flat = _flat_catalogue(lang)
        assert flat.get("player.artist_follow"), f"{lang} misses player.artist_follow"
        assert flat.get("player.artist_unfollow"), f"{lang} misses player.artist_unfollow"


def test_a_follow_refreshes_the_list_it_came_from():
    """The followed list is wrong the moment the follow changes."""
    assert re.search(
        r"if \(player\.sourceTab === 'followed_artists'\)\s*\{\s*await loadFollowedArtists\(\)",
        JS,
    )


def test_the_artist_page_classes_have_styles():
    """An unstyled artist page renders as a wall of text."""
    for selector in (".artist-detail-header", ".artist-detail-cover",
                     ".artist-detail-meta", ".artist-detail-actions",
                     ".artist-section", ".album-row"):
        assert selector + " " in CSS or selector + "{" in CSS or selector + "," in CSS, \
            f"{selector} has no rule in style.css"


def test_the_follow_state_is_told_apart_from_the_idle_state():
    """A followed artist must look followed."""
    assert ".artist-detail-actions .btn.is-active" in CSS


# ------------------------------------------------------- detail-page headers
def test_a_detail_header_survives_a_track_list_rerender():
    """The album/artist header lives in the same container as the rows.

    ``renderTrackList()`` clears that container before rebuilding the rows,
    which used to take the header (cover, name, bio, follow button) with it
    -- so both detail pages rendered as a bare list of song names.
    """
    assert "data-list-header" in JS, "no marker for a persistent header"
    # The clear has to keep the marked node.
    assert re.search(
        r"const keptHeader = list\.querySelector\(':scope > \[data-list-header\]'\)",
        JS,
    ), "renderTrackList does not look for the header before clearing"
    assert re.search(r"list\.innerHTML = '';\s*\n\s*if \(keptHeader\) list\.appendChild\(keptHeader\);", JS), \
        "renderTrackList does not restore the header after clearing"


def test_both_detail_pages_mark_their_header():
    """A page that forgets the marker loses its header again."""
    for klass in ("artist-detail-header", "album-detail-header"):
        assert re.search(rf"className = '{klass}';\s*\n\s*//[^\n]*\n\s*\w+\.dataset\.listHeader = '1';", JS), \
            f"{klass} is not marked as a persistent list header"


def test_the_next_track_preload_does_not_shadow_the_function_it_calls():
    """``const nextIndex = nextIndex(true)`` throws: the const shadows the
    function for the whole block, so the call hits the temporal dead zone."""
    assert not re.search(r"const nextIndex = nextIndex\(", JS), \
        "the local shadows the function it calls"
