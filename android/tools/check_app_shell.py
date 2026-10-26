#!/usr/bin/env python3
"""Smoke-test the assembled app: build it, resize it, check it reacts.

The screen-level check (`check_screens.py`) proves each page fits.  This one
proves the **shell** reacts to a size change the way the design says: the
bottom bar appears on a phone, the side rail on a tablet, and rotating the
device actually rebuilds the layout instead of leaving it stale.
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
from kivy.metrics import dp  # noqa: E402
from ncmkivy.app import NcmApp  # noqa: E402


class FakeController:
    """Empty answers; the shell does not care about the data."""

    def login_status(self):
        return {"logged_in": False}

    def start(self):
        return None

    def stop(self):
        return None

    def list_playlists(self):
        return []

    def playlist_tracks(self, playlist_id):
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


#: The screen's density.  ``dp(x)`` returns *pixels*, so the density has to
#: travel with it or every comparison is off by that factor.  Kivy keeps it
#: on ``Metrics``, not on the window.
from kivy.metrics import Metrics  # noqa: E402

DENSITY = getattr(Metrics, "density", 1.0) or 1.0


def _apply(app, width_dp: float) -> None:
    """Apply a layout width given in dp."""
    app._apply_size_class(dp(width_dp), DENSITY)


def _tick(times: int = 3) -> None:
    for _ in range(times):
        Clock.tick()


def main() -> int:
    problems: list[str] = []

    app = NcmApp()
    app.controller = FakeController()
    app.build()  # builds the shell; the children hang off the app
    _tick()

    # --- phone: bottom bar on, side rail off -----------------------------
    # The width is handed over directly: setting Window.size only takes
    # effect once the event loop dispatches it, which a headless check
    # cannot rely on.
    _apply(app, 360)
    _tick()
    if app.theme.size_class.name != "compact":
        problems.append(f"360dp is {app.theme.size_class.name}, expected compact")
    if app._bottom_nav is None or app._bottom_nav.height <= 0:
        problems.append("compact: the bottom navigation bar has no height")
    if app._nav_rail is None or app._nav_rail.width != 0:
        problems.append("compact: the side rail is still visible")

    # --- tablet: side rail on, bottom bar off ----------------------------
    _apply(app, 1280)
    _tick()
    if app.theme.size_class.name != "expanded":
        problems.append(f"1280dp is {app.theme.size_class.name}, expected expanded")
    if app._nav_rail is None or app._nav_rail.width <= 0:
        problems.append("expanded: the side rail has no width")
    if app._bottom_nav is None or app._bottom_nav.height != 0:
        problems.append("expanded: the bottom bar is still visible")

    # --- rotate back: the layout has to come back ------------------------
    _apply(app, 411)
    _tick()
    if app.theme.size_class.name != "compact":
        problems.append(f"411dp is {app.theme.size_class.name}, expected compact")
    if app._bottom_nav is None or app._bottom_nav.height <= 0:
        problems.append("after rotating back: the bottom bar is missing again")

    # --- a tablet in portrait and landscape -----------------------------
    _apply(app, 800)
    _tick()
    if app.theme.size_class.name != "medium":
        problems.append(f"800dp is {app.theme.size_class.name}, expected medium")

    # --- every section builds -------------------------------------------
    for section, _icon, _label in NcmApp.SECTIONS:
        try:
            app.go(section)
            _tick()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"section {section!r} failed to build: {exc}")

    report = ["app shell smoke test"]
    if problems:
        report.append(f"FAIL ({len(problems)} problems)")
        report.extend(f"  - {item}" for item in problems)
        code = 1
    else:
        report.append("OK: the shell switches layouts on resize and every "
                      "section builds")
        code = 0

    out = Path(os.environ["NCM_LAYOUT_REPORT"]) if os.environ.get(
        "NCM_LAYOUT_REPORT") else None
    if out:
        out.write_text("\n".join(report) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
