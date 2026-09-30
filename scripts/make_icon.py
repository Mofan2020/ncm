#!/usr/bin/env python3
"""Generate the application icons (PNG / ICO / ICNS).

Run from the repository root:

    python3 scripts/make_icon.py

Outputs into ``assets/``:
    icon.png    1024x1024 master
    icon.ico    Windows (16-256 px)
    icon.icns   macOS (via iconutil, skipped on non-macOS)
The icons are committed, so this only needs to run when the design changes.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZE = 1024
BRAND = (236, 65, 65, 255)
BRAND_DARK = (198, 42, 42, 255)


def rounded_square(size: int, radius_ratio: float = 0.22) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(size * radius_ratio)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BRAND)
    # subtle top highlight so the icon does not look flat
    draw.rounded_rectangle((0, 0, size - 1, int(size * 0.52)), radius=radius, fill=BRAND_DARK)
    draw.rounded_rectangle((0, int(size * 0.06), size - 1, int(size * 0.5)),
                           radius=radius, fill=BRAND)
    return image


def draw_music_note(image: Image.Image) -> None:
    """White quaver + a download arrow, drawn with geometry only."""
    size = image.size[0]
    draw = ImageDraw.Draw(image)
    unit = size / 100.0
    white = (255, 255, 255, 255)

    # note head (ellipse) and stem
    head_w, head_h = 26 * unit, 20 * unit
    head_x, head_y = 26 * unit, 56 * unit
    draw.ellipse((head_x, head_y, head_x + head_w, head_y + head_h), fill=white)
    stem_w = 6 * unit
    stem_x = head_x + head_w - stem_w
    draw.rounded_rectangle((stem_x, 26 * unit, stem_x + stem_w, head_y + head_h * 0.6),
                           radius=stem_w / 2, fill=white)
    # flag
    draw.polygon([
        (stem_x + stem_w, 26 * unit),
        (stem_x + stem_w + 20 * unit, 34 * unit),
        (stem_x + stem_w + 20 * unit, 46 * unit),
        (stem_x + stem_w, 40 * unit),
    ], fill=white)

    # download arrow, bottom right
    arrow_cx = 72 * unit
    draw.rounded_rectangle((arrow_cx - 4 * unit, 44 * unit, arrow_cx + 4 * unit, 68 * unit),
                           radius=4 * unit, fill=white)
    draw.polygon([
        (arrow_cx - 14 * unit, 62 * unit),
        (arrow_cx + 14 * unit, 62 * unit),
        (arrow_cx, 82 * unit),
    ], fill=white)
    draw.rounded_rectangle((arrow_cx - 16 * unit, 86 * unit, arrow_cx + 16 * unit, 93 * unit),
                           radius=3.5 * unit, fill=white)


def build_master() -> Image.Image:
    image = rounded_square(SIZE)
    draw_music_note(image)
    return image


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = build_master()
    png_path = ASSETS / "icon.png"
    master.save(png_path)
    print(f"wrote {png_path} ({master.size[0]}x{master.size[1]})")

    ico_path = ASSETS / "icon.ico"
    master.save(ico_path, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"wrote {ico_path}")

    if sys.platform == "darwin":
        icns_path = ASSETS / "icon.icns"
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "icon.iconset"
            iconset.mkdir()
            for size in (16, 32, 64, 128, 256, 512):
                master.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
                master.resize((size * 2, size * 2), Image.LANCZOS).save(
                    iconset / f"icon_{size}x{size}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)], check=True)
        print(f"wrote {icns_path}")
    else:
        print("skipped .icns (only generated on macOS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
