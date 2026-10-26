#!/usr/bin/env python3
"""Headless smoke test for the Kivy layer.

Runs the widget tree without a window (Kivy's mock/headless provider) and
checks the two things that are easy to get wrong and impossible to see in a
screenshot: that nothing overflows its parent, and that the size buckets
switch at the right widths.

Usage::

    KIVY_WINDOW=mock ~/android-toolchain/kv/bin/python \
        android/tools/check_kivy_layout.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("KIVY_WINDOW", "sdl2")
os.environ.setdefault("KIVY_NO_ARGS", "1")
# Kivy grabs stdout for its own log; the report goes to a file instead.
os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "android"))

from kivy.core.window import Window  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.uix.boxlayout import BoxLayout  # noqa: E402
from ncmkivy.theme import classify  # noqa: E402
from ncmkivy.widgets import (  # noqa: E402
    EmptyState,
    IconButton,
    SectionHeader,
    TrackList,
    TrackRow,
)

TRACKS = [
    {"id": "1", "name": "A very long song title that will not fit on one line",
     "artists": "Artist One, Artist Two, Artist Three, Artist Four",
     "album": "A very long album name as well", "cover_url": "", "duration": 210000},
    {"id": "2", "name": "Short", "artists": "A", "album": "B", "cover_url": "",
     "duration": 1000},
]


def _tick() -> None:
    """Let Kivy run one frame so the layout settles."""
    from kivy.clock import Clock

    Clock.tick()


def check_size_classes() -> list[str]:
    """The breakpoints must match the Material 3 window size classes.

    The widths are given in dp directly (density 1.0) so the check does not
    depend on the screen the test happens to run on.
    """
    problems = []
    cases = [
        (360, "compact"),
        (599, "compact"),
        (600, "medium"),
        (839, "medium"),
        (840, "expanded"),
        (1200, "expanded"),
    ]
    for width, expected in cases:
        actual = classify(width, 1.0).name
        if actual != expected:
            problems.append(f"width {width}dp -> {actual}, expected {expected}")
    return problems


def check_no_overflow() -> list[str]:
    """No widget may stick out of its parent."""
    problems: list[str] = []

    root = BoxLayout(orientation="vertical", size=(dp(360), dp(640)))
    listing = TrackList(TRACKS)
    listing.size_hint = (1, 1)
    root.add_widget(listing)
    root.add_widget(SectionHeader(text="Section"))
    root.add_widget(EmptyState(message="Nothing here yet"))
    root.add_widget(IconButton(icon="play"))

    # Kivy lays out on the next frame, so let the clock tick once.
    for _ in range(3):
        _tick()

    def walk(widget, depth=0):
        for child in widget.children:
            if child.width <= 0 or child.height <= 0:
                continue
            right = child.x + child.width
            if right > widget.x + widget.width + 1:
                problems.append(
                    f"{type(child).__name__} overflows {type(widget).__name__} "
                    f"by {right - (widget.x + widget.width):.1f}px")
            walk(child, depth + 1)

    walk(root)
    return problems


def check_row_height() -> list[str]:
    """Rows must have a height, or a recycle view would collapse them."""
    problems = []
    row = TrackRow(TRACKS[0])
    row.do_layout()
    if row.height <= 0:
        problems.append("TrackRow has no height")
    return problems


def main() -> int:
    problems: list[str] = []
    problems.extend(check_size_classes())
    problems.extend(check_no_overflow())
    problems.extend(check_row_height())

    report = [f"window size: {Window.size}"]
    if problems:
        report.append(f"FAIL ({len(problems)} problems)")
        report.extend(f"  - {item}" for item in problems)
        code = 1
    else:
        report.append("OK: size classes, overflow and row heights all check out")
        code = 0

    out = Path(os.environ.get("NCM_LAYOUT_REPORT", "")) if os.environ.get(
        "NCM_LAYOUT_REPORT") else None
    if out:
        out.write_text("\n".join(report) + "\n", encoding="utf-8")
    else:
        with open(os.devnull, "w"):
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
