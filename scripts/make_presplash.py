#!/usr/bin/env python3
"""Generate the Android launch screen (presplash).

Run from the repository root:

    .venv/bin/python scripts/make_presplash.py

Writes ``android/assets/presplash.png``.

Why a dedicated image instead of the app icon: p4a shows the presplash in a
full-screen ``ImageView`` with ``ScaleType.FIT_CENTER``.  Handing it the
square icon means the square fills the screen's width and the whole launch
looks like one giant red logo with black bars above and below.

A tall image with the logo in the middle and the app name under it is scaled
down to fit the screen instead, so the logo stays a logo.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "icon.png"
OUT = ROOT / "android" / "assets" / "presplash.png"

#: The canvas is tall: FIT_CENTER scales it down to the screen height, so
#: the logo has to sit in the middle of a lot of background.
WIDTH = 1080
HEIGHT = 1920

#: The brand red, matching scripts/make_icon.py.
BRAND = (236, 65, 65)
#: The same red, darkened, so the canvas is not flat.
BRAND_DEEP = (196, 40, 40)

#: How large the logo is on the canvas, as a fraction of its width.
LOGO_RATIO = 0.26

TEXT = "SkycMedia"


def _font(size: int) -> ImageFont.ImageFont:
    """A CJK-capable font, whatever the machine has."""
    for path in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
              radius: int, fill: tuple[int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def build() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BRAND)
    draw = ImageDraw.Draw(image)

    # A soft vertical gradient so the screen is not one flat slab of red.
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=tuple(
                int(BRAND[i] + (BRAND_DEEP[i] - BRAND[i]) * t) for i in range(3)
            ),
        )

    logo_size = int(WIDTH * LOGO_RATIO)
    logo = Image.open(ICON).convert("RGBA").resize(
        (logo_size, logo_size), Image.Resampling.LANCZOS
    )
    logo_x = (WIDTH - logo_size) // 2
    logo_y = int(HEIGHT * 0.34)

    # The icon is a red tile on a red tile: without a plate under it the
    # logo disappears into the background.
    plate_size = int(logo_size * 1.28)
    plate_x = (WIDTH - plate_size) // 2
    plate_y = logo_y - (plate_size - logo_size) // 2
    plate = Image.new("RGBA", (plate_size, plate_size), (0, 0, 0, 0))
    plate_draw = ImageDraw.Draw(plate)
    plate_draw.rounded_rectangle(
        (0, 0, plate_size - 1, plate_size - 1),
        radius=int(plate_size * 0.24),
        fill=(255, 255, 255, 235),
    )
    image.paste(plate, (plate_x, plate_y), plate)
    image.paste(logo, (logo_x, logo_y), logo)

    # The name under the logo, in the brand red's own white.
    font = _font(64)
    text = TEXT
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    draw.text(
        ((WIDTH - text_w) // 2, logo_y + logo_size + 48),
        text,
        font=font,
        fill=(255, 255, 255),
    )

    return image


def main() -> int:
    if not ICON.exists():
        print(f"missing {ICON}", flush=True)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    build().save(OUT)
    print(f"wrote {OUT} ({WIDTH}x{HEIGHT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
