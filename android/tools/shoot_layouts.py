#!/usr/bin/env python3
"""Render the Android UI at phone/tablet sizes and save PNGs.

The overflow checker proves nothing pokes outside its parent; it does not
prove the result looks like a phone app.  This drives the real widget tree
at real dp sizes and writes one screenshot per size class, so the layout can
be looked at instead of inferred.

    python3 android/tools/shoot_layouts.py [--out DIR]

Screenshots go to ``$NCM_SHOT_DIR`` (default: a temp dir) and the paths are
printed.  This runs the desktop Kivy window, so it needs a display.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "android"))

from kivy.clock import Clock  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.metrics import Metrics  # noqa: E402
from ncmkivy.app import NcmApp  # noqa: E402

#: (label, width_dp, height_dp) -- the sizes a real device reports.
SIZES = [
    ("phone_portrait", 360, 780),
    ("phone_small", 320, 640),
    ("phone_landscape", 780, 360),
    ("tablet_portrait", 800, 1280),
    ("tablet_landscape", 1280, 800),
]


class DemoController:
    """Canned data so the shots show a populated UI, not empty states."""

    def __init__(self) -> None:
        self._tracks = [
            {"id": "1", "name": "布拉格广场", "artists": "蔡依林, 周杰伦",
             "album": "看我72变", "duration": 295000, "vip": True},
            {"id": "2", "name": "晴天", "artists": "周杰伦", "album": "叶惠美",
             "duration": 269000, "vip": False},
            {"id": "3", "name": "夜曲", "artists": "周杰伦", "album": "十一月的萧邦",
             "duration": 226000, "vip": True},
            {"id": "4", "name": "稻香", "artists": "周杰伦", "album": "魔杰座",
             "duration": 223000, "vip": False},
            {"id": "5", "name": "告白气球", "artists": "周杰伦",
             "album": "周杰伦的床边故事", "duration": 215000, "vip": False},
            {"id": "6", "name": "七里香", "artists": "周杰伦", "album": "七里香",
             "duration": 299000, "vip": False},
        ]

    def login_status(self):
        return {"logged_in": True, "nickname": "星空喵"}

    def start(self):
        return None

    def stop(self):
        return None

    def list_playlists(self):
        return [{"id": str(i), "name": f"我的歌单 {i}", "track_count": 20 + i,
                 "cover_url": ""} for i in range(1, 7)]

    def playlist_tracks(self, playlist_id):
        return self._tracks

    def recent_tracks(self):
        return self._tracks

    def favorite_tracks(self):
        return self._tracks

    def download_status(self):
        return {"running": False, "total": 0, "success": 0, "failed": 0,
                "download_dir": "/sdcard/Music"}

    def artist_page(self, artist_id):
        return {
            "success": True,
            "artist": {"id": "6452", "name": "周杰伦",
                       "description": "周杰伦，华语流行乐男歌手、音乐人、演员、导演。",
                       "cover_url": ""},
            "followed": False,
            "fans": 19139626,
            "hot_songs": self._tracks,
            "total": 580,
        }

    def followed_artists(self, limit=25, offset=0):
        return {"artists": [
            {"id": "1", "name": "win7mus", "album_count": 16, "cover_url": ""},
            {"id": "2", "name": "封茗囧菌", "album_count": 211, "cover_url": ""},
            {"id": "3", "name": "Alstroemeria Records", "album_count": 74,
             "cover_url": ""},
        ], "count": 3, "has_more": False, "code": 200}

    def search(self, keyword, offset=0, limit=30):
        return {"songs": self._tracks, "artists": [], "albums": [],
                "playlists": []}

    def search_songs(self, keyword, offset=0, limit=30):
        return self._tracks


def _settle(times: int = 8, pause: float = 0.0) -> None:
    """Advance frames.  ``pause`` gives the OS compositor a moment to draw:
    without it the framebuffer a screenshot reads can still be from the
    previous size (or empty, which is how the first run produced black PNGs).
    """
    for _ in range(times):
        Clock.tick()
        if pause:
            time.sleep(pause)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.environ.get("NCM_SHOT_DIR", ""))
    parser.add_argument("--section", default="player")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(
        os.environ.get("TMPDIR", "/tmp")) / "ncm_shots"
    out.mkdir(parents=True, exist_ok=True)

    app = NcmApp()
    app.controller = DemoController()
    app.build()
    _settle()

    written: list[str] = []
    for label, w_dp, h_dp in SIZES:
        density = getattr(Metrics, "density", 1.0) or 1.0
        # Size the window in pixels: on a retina host one dp is several px,
        # and the window has to be physically big enough to fit them.
        Window.size = (int(w_dp * density), int(h_dp * density))
        _settle(6, pause=0.05)
        # The resize arrives asynchronously; drive the bucket explicitly so
        # the shot does not depend on the event loop having caught up.
        app._apply_size_class(w_dp * density, density)
        app.go(args.section)
        _settle(10, pause=0.05)

        path = out / f"{label}_{w_dp}x{h_dp}.png"
        # Two frames: one to paint the new layout, one for the capture.
        _settle(4, pause=0.08)
        Window.screenshot(name=str(path))
        _settle(4, pause=0.08)
        written.append(str(path))

    for item in written:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
