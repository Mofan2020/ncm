"""The phone front end and the bridge: the contract between them.

The desktop front end has ``tests/test_frontend_assets.py`` for the same
reason.  The phone front end grew separately and nothing checked the two
against each other, so a handful of mismatches survived:

* the profile page read ``status.nickname`` while the bridge puts the user
  under ``status.user`` -- the page said "已登录" and "UID 0" for an
  account that was signed in properly;
* the toast sat at a lower stacking order than the login sheet, so the
  message about the verification code never appeared;
* the code button had no cooldown, and every second request came back as
  "操作过于频繁".

None of these raise.  They are checked here instead.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANDROID_WEB = ROOT / "android" / "web"
APP_JS = (ANDROID_WEB / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ANDROID_WEB / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (ANDROID_WEB / "index.html").read_text(encoding="utf-8")
BRIDGE = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")
LOGIN_PY = (ROOT / "src" / "auth" / "login.py").read_text(encoding="utf-8")


def _css_rule(source: str, selector: str) -> str:
    """The declarations of the first rule that starts with ``selector``.

    Comments are stripped: a rule may well *mention* the value it is
    supposed to avoid ("no min-height: 100dvh here"), and the check has
    to look at what the browser reads, not at what the author wrote.
    """
    start = source.index(f"\n{selector} {{")
    start = source.index("{", start) + 1
    end = source.index("}", start)
    body = source[start:end]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return body


def _js_function(source: str, name: str) -> str:
    """The body of a function, up to the next top-level ``function``.

    Comments are stripped for the same reason as in the CSS: the source
    may *mention* the wrong value while explaining it ("the user lives
    under status.user"), and the check has to look at what runs.
    """
    start = source.index(f"function {name}")
    rest = source[start + len(f"function {name}"):]
    nxt = rest.find("\nfunction ")
    body = rest[:nxt] if nxt != -1 else rest
    return _without_comments(body)


def _without_comments(body: str) -> str:
    """Strip block and line comments."""
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return re.sub(r"//[^\n]*", "", body)


def _code_button_handler() -> str:
    """The click handler of the "send the code" button."""
    handler = APP_JS[APP_JS.index("sendButton.addEventListener('click'"):]
    handler = handler[:handler.index("const loginButton")]
    return _without_comments(handler)


# ------------------------------------------------------------ the shell
def test_the_shell_is_exactly_the_viewport():
    """The column must not grow past the screen.

    ``min-height: 100dvh`` let the whole column grow with the content, and
    the tab bar then sat wherever the content ended instead of at the
    bottom of the screen.
    """
    shell = _css_rule(STYLE_CSS, ".app")
    assert "min-height: 100dvh" not in shell, (
        "the shell must not be allowed to grow past the viewport")
    assert "height: 100dvh" in shell, "the shell has to fill the viewport"


def test_the_content_area_can_shrink_below_its_content():
    """A flex item's automatic minimum size is its content.

    Without ``min-height: 0`` the content area refuses to shrink, the
    column grows, and the tab bar is pushed off the bottom.
    """
    views = _css_rule(STYLE_CSS, ".views")
    assert "min-height: 0" in views, (
        "the content area must be allowed to shrink below its content")


def test_the_pages_scroll_inside_the_content_area():
    """The pages fill the area and scroll themselves.

    ``height: 100%`` on the page depends on the height of the container
    that the same rule is trying to establish, so it resolves against the
    content height and the layout grows instead of scrolling.
    """
    view = _css_rule(STYLE_CSS, ".view")
    assert "height: 100%" not in view, (
        "a percentage height here is circular -- the page has to be "
        "positioned inside the content area instead")
    assert "position: absolute" in view, view
    assert "inset: 0" in view, view
    assert "overflow-y: auto" in view, view


def test_the_scroll_listener_watches_the_page_not_the_container():
    """Scroll events do not bubble.

    The container has ``overflow: hidden`` and the pages scroll
    themselves, so a listener on the container never fires and the top bar
    never gets its scrolled shadow.
    """
    assert "$('views').addEventListener('scroll'" not in APP_JS, (
        "the scroll listener has to be on the pages themselves")
    assert "#views > .view').forEach((view) => {" in APP_JS, (
        "every page needs its own scroll listener")


# ------------------------------------------------------------ the profile
def test_the_profile_reads_the_user_from_the_right_place():
    """The bridge puts the signed-in user under ``status.user``."""
    bridge = BRIDGE[BRIDGE.index("def get_login_status"):]
    bridge = bridge[:bridge.index("    def ", 10)]
    assert '"user": {' in bridge, (
        "get_login_status has to report the user under 'user'")

    profile = _js_function(APP_JS, "loadMe")
    assert "status.user" in profile, (
        "the profile page has to read the user from status.user")
    assert "status.nickname" not in profile, (
        "status.nickname does not exist -- the user is one level deeper")
    assert "status.user_id" not in profile, (
        "status.user_id does not exist -- the user is one level deeper")


# ------------------------------------------------------------ the toast
def test_a_toast_is_not_hidden_behind_the_sheet_that_raised_it():
    """The toast has to sit above the login sheet and the modal."""
    layers = {}
    for match in re.finditer(r"\.([\w-]+)[^{]*\{([^}]*)\}", STYLE_CSS):
        name, body = match.group(1), match.group(2)
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        z = re.search(r"z-index:\s*(\d+)", body)
        if z:
            layers[name] = int(z.group(1))

    toast = layers.get("toast-host")
    sheet = layers.get("sheet-host")
    modal = layers.get("modal-host")
    assert toast is not None, "the toast host has no z-index"
    assert sheet is not None and modal is not None
    assert toast > sheet, (
        f"the toast ({toast}) is behind the sheet ({sheet}): a message "
        "raised by the login sheet would never be seen")
    assert toast > modal, (
        f"the toast ({toast}) is behind the modal ({modal})")


# ------------------------------------------------------- the code button
def test_the_code_button_counts_down():
    """A second request for the same number is refused for a while.

    The button has to stay disabled while the wait runs, or the user only
    finds out about the cooldown by getting "操作过于频繁" back.
    """
    assert "startCountdown" in APP_JS, (
        "the code button has no cooldown")
    countdown = APP_JS[APP_JS.index("const startCountdown"):]
    countdown = countdown[:countdown.index("\n    };")]
    countdown = _without_comments(countdown)
    assert "sendButton.disabled = true" in countdown, countdown
    assert "setInterval" in countdown, countdown
    assert "sendButton.disabled = false" in countdown, countdown


def test_the_countdown_is_actually_started():
    """The button has to *run* the countdown, not merely define it.

    A definition that nothing calls leaves the button clickable, and the
    second request comes back as "操作过于频繁".
    """
    handler = _code_button_handler()
    assert "startCountdown(" in handler, (
        "the click handler never starts the countdown")
    # One for every outcome: sent, throttled, refused.
    assert handler.count("startCountdown(") >= 3, (
        "every outcome has to put the button on a cooldown")


def test_the_code_button_is_disabled_while_the_request_runs():
    """A double tap sends two requests and extends the wait."""
    handler = _code_button_handler()
    assert "if (sendButton.disabled) return;" in handler, (
        "the button must ignore a second tap while it is disabled")


def test_the_throttled_answer_gets_the_long_wait():
    """A refused request waits longer than a plain failure."""
    handler = _code_button_handler()
    assert "throttled" in handler, (
        "a throttled answer must be told apart from an ordinary failure")
    assert "startCountdown(180)" in handler, (
        "a throttled answer waits three minutes, not one")


def test_the_bridge_reports_the_throttle():
    """The phone needs to know it was throttled.

    The key has to be checked as a key of the returned mapping -- the
    name also shows up inside ``result.get("throttled")``, and a check
    for the bare word would pass with the key renamed.
    """
    bridge = BRIDGE[BRIDGE.index("def send_phone_code"):]
    bridge = bridge[:bridge.index("def login_phone")]
    keys = re.findall(r'^\s{12}"([a-z_]+)":', bridge, flags=re.M)
    assert "throttled" in keys, (
        f"send_phone_code does not report the throttle: {keys}")


# -------------------------------------------------------------- the i18n
def test_the_phone_loads_the_translations():
    """The phone has to ask for the catalogue before it renders.

    Without it every message falls back to its key, and the user reads
    "login.code_send_failed" instead of a sentence.
    """
    boot = _js_function(APP_JS, "boot")
    assert "get_translations" in boot, (
        "the phone never loads the translation catalogue")
    assert "state.translations" in boot, boot


def test_the_phone_has_a_translation_lookup():
    assert "function t(key" in APP_JS, "the phone has no translation lookup"
    lookup = APP_JS[APP_JS.index("function t(key"):]
    lookup = lookup[:lookup.index("\n}")]
    assert "state.translations[key]" in lookup, lookup
    assert "{" in lookup and "}" in lookup, (
        "the lookup has to fill in the placeholders")


def test_a_failed_call_shows_the_reason_and_not_a_generic_line():
    """The bridge reports failures with an ``error_key``.

    Reading ``result.message`` instead gives an empty bubble for most
    calls, because the bridge only sends a message for some of them.
    """
    assert "function errorText(result" in APP_JS, (
        "the phone has no way to turn an error_key into a sentence")
    helper = APP_JS[APP_JS.index("function errorText(result"):]
    helper = helper[:helper.index("\n}")]
    assert "result.error_key" in helper, helper
    assert "t(result.error_key" in helper, helper


def test_the_phone_does_not_read_a_message_the_bridge_never_sends():
    """The calls that report a failure.

    Each of these used to read ``result.message``, which the bridge
    does not send on the failure path -- the toast fell back to a
    generic line every time.
    """
    for method in ("download_tracks", "export_account_backup", "login_phone"):
        idx = APP_JS.index(f"api('{method}'")
        tail = APP_JS[idx: idx + 700]
        assert "result.message" not in tail, (
            f"{method} still reads result.message -- the bridge sends "
            "error_key, not a message")


# --------------------------------------------------------- the statistics
def test_the_download_counters_use_the_names_the_bridge_sends():
    """The bridge counts with "finished" and "total".

    It sends no "completed" and no "downloading", so those two numbers
    used to read 0 forever.
    """
    stats = _js_function(APP_JS, "renderDownloadStats")
    assert "data.finished" in stats, stats
    assert "data.total" in stats, stats
    assert "data.completed" not in stats, stats
    assert "data.downloading" not in stats, stats


# --------------------------------------------------------- the code style
def test_the_phone_style_follows_the_desktop_palette():
    """The two front ends are one product and should look like one."""
    desktop = (ROOT / "src" / "gui" / "assets" / "style.css").read_text(
        encoding="utf-8")
    for token in ("--brand", "--surface", "--text"):
        assert token in STYLE_CSS, f"{token} is missing from the phone stylesheet"
        assert token in desktop, f"{token} is missing from the desktop stylesheet"
