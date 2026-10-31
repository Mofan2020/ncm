#!/usr/bin/env python3
"""Generate the application icons (PNG / ICO / ICNS).

Run from the repository root:

    python3 scripts/make_icon.py

Outputs into ``assets/``:
    icon.png    1024x1024 master
    icon.ico    Windows (16-256 px)
    icon.icns   macOS (via iconutil, skipped on non-macOS)

The icons are committed, so this only needs to run when the design changes.

The mark is a play triangle inside a rounded square, with a waveform
behind it: SkycMedia plays music from more than one place, and the old
NetEase-red note only spoke about one of them.
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

#: The deep blue the square fades from, and the lighter blue it fades to.
BASE = (26, 42, 74, 255)
BASE_DEEP = (14, 22, 44, 255)

#: The accent the mark is drawn in: the cyan a screen glows with.
ACCENT = (94, 214, 255, 255)
ACCENT_SOFT = (255, 255, 255, 255)


def rounded_square(size: int, radius_ratio: float = 0.22) -> Image.Image:
    """The plate the mark sits on: a rounded square, dark blue."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(size * radius_ratio)

    # The square, filled with a vertical fade.  The fade is drawn as a
    # stack of bands rather than a gradient the image library cannot do
    # for a rounded shape: each band is clipped by redrawing the rounded
    # outline, which keeps the corners clean.
    band = max(1, size // 220)
    for top in range(0, size, band):
        bottom = min(size, top + band)
        ratio = top / max(1, size - 1)
        colour = tuple(
            int(BASE_DEEP[index] + (BASE[index] - BASE_DEEP[index]) * ratio)
            for index in range(3)
        ) + (255,)
        draw.rounded_rectangle((0, top, size - 1, bottom), radius=radius, fill=colour)
    return image


def draw_waveform(image: Image.Image) -> None:
    """The bars behind the mark: a spectrum, drawn as rounded columns.

    Every bar is clipped to the inside of the ring.  A bar that simply
    ends outside the circle gets cut by the stroke and leaves a stub
    hanging past the ring, which reads as a mistake rather than as a
    spectrum -- so the half-height of each bar is capped by how far the
    circle still reaches at that distance from its centre.
    """
    size = image.size[0]
    draw = ImageDraw.Draw(image)
    unit = size / 100.0

    # (centre x, height) in percent of the icon, measured from the
    # middle of the ring.  The bars rise and fall around it so the mark
    # in front reads as the loudest part.  The middle is left out: the
    # play triangle sits there, and a bar behind it would look like the
    # triangle is stabbing through it.
    bars = [
        (-25, 15), (-18, 24), (-11, 32),
        (11, 18), (18, 27), (25, 19),
    ]
    width = 3.4 * unit
    ring_cx, ring_cy = 50 * unit, 52 * unit
    ring_r = 27 * unit
    ring_w = 5.5 * unit
    # The circle the bars have to stay inside: the ring's stroke takes
    # half of its width, and the rest is the margin.
    inner_r = ring_r - ring_w / 2 - 1.5 * unit
    colour = (120, 190, 235, 60)
    for x_offset, height in bars:
        x = ring_cx + x_offset * unit
        # How far the circle still reaches at this x.
        dx = abs(x - ring_cx)
        if dx >= inner_r:
            continue
        reach = (inner_r ** 2 - dx ** 2) ** 0.5
        half = min(height * unit / 2, reach)
        draw.rounded_rectangle(
            (x - width / 2, ring_cy - half, x + width / 2, ring_cy + half),
            radius=width / 2, fill=colour,
        )


def draw_play_mark(image: Image.Image) -> None:
    """The mark itself: a play triangle inside a ring."""
    size = image.size[0]
    draw = ImageDraw.Draw(image)
    unit = size / 100.0

    # The ring: an open circle, so the mark reads as "playing" and not
    # as a record.
    ring_cx, ring_cy = 50 * unit, 52 * unit
    ring_r = 27 * unit
    ring_w = 5.5 * unit
    draw.ellipse(
        (ring_cx - ring_r, ring_cy - ring_r, ring_cx + ring_r, ring_cy + ring_r),
        outline=ACCENT_SOFT, width=int(ring_w),
    )

    # The triangle.  Small enough to sit inside the ring with air around
    # it -- a triangle that fills the ring fights with it.
    half = 9.5 * unit
    draw.polygon([
        (ring_cx - half * 0.62, ring_cy - half),
        (ring_cx - half * 0.62, ring_cy + half),
        (ring_cx + half * 0.78, ring_cy),
    ], fill=ACCENT)


def build_master() -> Image.Image:
    image = rounded_square(SIZE)
    draw_waveform(image)
    draw_play_mark(image)
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
