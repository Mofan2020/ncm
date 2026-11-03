"""The hidden attribute has to win over the stylesheets.

The player window hides the overlay with the hidden attribute.  A rule that
gives the overlay a display of its own beats the rule the browser has for
the hidden attribute, so the overlay stays on screen: the picture plays
under a translucent black sheet, the sound comes out, and the window looks
broken.  The overlay is not even the only sheet -- the fatal notice covers
the whole window the same way.

This walks every page and every stylesheet in the assets folder and says
which elements are in that spot, so a new sheet cannot sneak in.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "src" / "gui" / "assets"

#: A rule block: the selector and the declarations inside it.
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)


def _rules(css: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _RULE.finditer(css):
        selector = " ".join(match.group(1).split())
        body = match.group(2)
        if selector.startswith("/*"):
            # A comment before the selector: the comment is not a selector.
            end = selector.rfind("*/")
            if end >= 0:
                selector = " ".join(selector[end + 2:].split())
        if not selector:
            continue
        out.append((selector, body))
    return out


def _display(body: str) -> str | None:
    match = re.search(r"display\s*:\s*([^;]+)", body)
    if not match:
        return None
    # "!important" says how loud the rule is, not what it does.
    return match.group(1).strip().replace("!important", "").strip()


def _hides_hidden(rules: list[tuple[str, str]], classes: list[str]) -> bool:
    """Does some rule put an element with these classes out of sight?"""
    for selector, body in rules:
        if _display(body) != "none":
            continue
        parts = [p.strip() for p in selector.split(",")]
        for part in parts:
            # A rule that targets the element itself and carries the
            # attribute, like ".bp-overlay[hidden]".
            if "[hidden]" not in part:
                continue
            base = part.split("[hidden]")[0].strip()
            if base in ("", "*"):
                # The blanket rule: [hidden] { display: none; }
                return True
            if any(cls in base.split() for cls in classes):
                return True
            if base.lstrip(".") in classes:
                return True
    return False


def _blanket_rule(rules: list[tuple[str, str]]) -> bool:
    """Is there a rule that hides every hidden element?"""
    for selector, body in rules:
        if _display(body) != "none":
            continue
        for part in (p.strip() for p in selector.split(",")):
            if part in ("[hidden]", "[hidden] !important", "*[hidden]"):
                return True
            if re.fullmatch(r"\*?\s*\[hidden\]", part):
                return True
    return False


def _hidden_elements(html: str) -> list[tuple[str, list[str]]]:
    """Every element in the page that carries the hidden attribute."""
    out: list[tuple[str, list[str]]] = []
    for match in re.finditer(r"<(\w+)([^>]*\bhidden\b[^>]*)>", html, re.I):
        attrs = match.group(2)
        ident = re.search(r'id="([^"]+)"', attrs)
        classes = re.search(r'class="([^"]*)"', attrs)
        name = ident.group(1) if ident else match.group(1)
        class_list = classes.group(1).split() if classes else []
        out.append((name, class_list))
    return out


PAGES = sorted(p for p in ASSETS.glob("*.html"))


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_hidden_attribute_is_not_overridden_by_the_stylesheet(page: Path) -> None:
    """Nothing that is hidden may be given a display of its own."""
    html = page.read_text(encoding="utf-8")
    sheets = re.findall(r'<link[^>]+href="([^"]+\.css)"', html)
    if not sheets:
        # A fragment that is written into another page: it has no stylesheet
        # of its own, so there is nothing to check here.
        pytest.skip(f"{page.name} loads no stylesheet")
    rules: list[tuple[str, str]] = []
    for sheet in sheets:
        css_path = (page.parent / sheet).resolve()
        assert css_path.exists(), f"{page.name} points at a missing {sheet}"
        rules.extend(_rules(css_path.read_text(encoding="utf-8")))

    # The blanket rule is enough on its own.
    if _blanket_rule(rules):
        return

    problems: list[str] = []
    for name, classes in _hidden_elements(html):
        for selector, body in rules:
            if _display(body) in (None, "none"):
                continue
            # Does the rule talk about one of the classes of the element?
            mentioned = any(
                re.search(r"(^|[\s,>+~])\." + re.escape(cls) + r"(?![\w-])", selector)
                for cls in classes
            )
            if not mentioned and not re.search(r"(^|[\s,>+~])#" + re.escape(name) + r"(?![\w-])", selector):
                continue
            if _hides_hidden(rules, classes):
                break
            problems.append(
                f"#{name} ({' '.join(classes) or 'no class'}) is given "
                f"'display: {_display(body)}' by '{selector}' and no rule puts "
                "it back out of sight"
            )
            break
    assert not problems, "\n".join(problems)


def test_the_player_window_hides_its_overlay() -> None:
    """The overlay of the standalone window is the sheet that hid the picture."""
    css = (ASSETS / "bili-player.css").read_text(encoding="utf-8")
    rules = _rules(css)
    assert _blanket_rule(rules) or _hides_hidden(rules, ["bp-overlay"]), (
        "bili-player.css has no rule that takes the overlay away again: "
        "the overlay covers the picture of the standalone window"
    )


def test_the_main_window_hides_its_overlay() -> None:
    """The same for the overlay inside the main window."""
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    rules = _rules(css)
    assert _blanket_rule(rules) or _hides_hidden(rules, ["bp-overlay"]), (
        "style.css has no rule that takes the overlay away again"
    )
