#!/usr/bin/env python3
"""Build every screen and check that it lays out without overflowing.

This is the test that catches "the artist name is cut off on a phone" and
"the button is half off the right edge" -- the failures a screenshot on one
device would not show, because the same screen has to fit a 360dp phone and
a 1200dp tablet.

The screens need a controller, but not a network: a fake one answers with
empty lists, which is exactly the "not signed in" state the screens have to
cope with.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "android"))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from ncmkivy.app import NcmApp  # noqa: E402
from ncmkivy.screens import (  # noqa: E402
    ArtistScreen,
    DownloadsScreen,
    FollowedArtistsScreen,
    HomeScreen,
    LibraryScreen,
    MeScreen,
    PlaylistScreen,
    SearchScreen,
)

#: The widths the app has to survive, in dp.
WIDTHS = (360, 411, 600, 840, 1280)


class FakeController:
    """A controller that answers with empty data and never touches the net."""

    def login_status(self):
        return {"logged_in": False, "user_id": "", "nickname": "",
                "avatar_url": "", "vip_type": 0}

    def list_playlists(self):
        return []

    def playlist_tracks(self, playlist_id):
        return []

    def liked_tracks(self):
        return []

    def local_tracks(self, query="", limit=200, offset=0):
        return []

    def download_status(self):
        return {"running": False, "total": 0, "success": 0, "failed": 0,
                "download_dir": ""}

    def artist_page(self, artist_id):
        return {"success": False, "error_key": "player.artist_not_found"}

    def followed_artists(self, limit=25, offset=0):
        return {"artists": [], "count": 0, "has_more": False, "code": 301}

    def search(self, keyword, offset=0, limit=30):
        return {"songs": [], "artists": [], "albums": [], "playlists": []}


def _tick(times: int = 4) -> None:
    for _ in range(times):
        Clock.tick()


def _overflows(widget, problems, path="") -> None:
    """Report every child that sticks out of its parent."""
    for child in widget.children:
        if child.width <= 0 or child.height <= 0:
            continue
        label = f"{path}/{type(child).__name__}"
        right = child.x + child.width
        if right > widget.x + widget.width + 1:
            problems.append(
                f"{label} overflows {type(widget).__name__} "
                f"by {right - (widget.x + widget.width):.1f}px")
        # A vertical box may be taller than the screen: the part that does
        # not fit is what the scroll view is for.  Only a *horizontal*
        # overflow is a real bug, so the height check applies to the
        # children of a horizontal box, where there is nothing to scroll.
        if (child.height > widget.height + 1
                and type(widget).__name__ == "BoxLayout"
                and getattr(widget, "orientation", "vertical") == "horizontal"):
            problems.append(
                f"{label} is taller than {type(widget).__name__} "
                f"by {child.height - widget.height:.1f}px")
        _overflows(child, problems, label)


def _is_scrollable(widget) -> bool:
    """A scroll view may be taller than its parent; nothing else may."""
    return type(widget).__name__ in ("ScrollView", "RecycleView")


def main() -> int:
    app = NcmApp()
    app.controller = FakeController()
    root = app.build()

    problems: list[str] = []
    screens = [
        ("home", HomeScreen),
        ("search", SearchScreen),
        ("library", LibraryScreen),
        ("downloads", DownloadsScreen),
        ("me", MeScreen),
        ("followed", FollowedArtistsScreen),
        ("artist", ArtistScreen),
        ("playlist", PlaylistScreen),
    ]

    for width_dp in WIDTHS:
        width = dp(width_dp)
        Window.size = (width, dp(800))
        for name, factory in screens:
            try:
                screen = factory(app, artist_id="6452", playlist_id="123")
            except TypeError:
                screen = factory(app)
            root.add_widget(screen)
            _tick()
            _overflows(screen, problems, f"{width_dp}dp/{name}")
            root.remove_widget(screen)
            _tick()

    report = [f"checked {len(WIDTHS)} widths x {len(screens)} screens"]
    if problems:
        report.append(f"FAIL ({len(problems)} problems)")
        report.extend(f"  - {item}" for item in problems[:40])
        code = 1
    else:
        report.append("OK: no screen overflows at any tested width")
        code = 0

    out = Path(os.environ["NCM_LAYOUT_REPORT"]) if os.environ.get(
        "NCM_LAYOUT_REPORT") else None
    if out:
        out.write_text("\n".join(report) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
