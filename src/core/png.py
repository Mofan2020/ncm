"""A minimal PNG writer -- just enough to draw a QR code.

The login QR code used to be rendered with Pillow, which dragged about
16 MB of native image libraries (Pillow plus the OpenSSL it links against)
into the bundle for a 200x200 black-and-white image.  A QR code is a
boolean matrix, so the PNG can be written by hand out of the standard
library: ``zlib`` compresses the scanlines and ``struct`` frames the chunks.

Only 8-bit greyscale PNGs are produced; that is all a QR code needs and it
keeps the encoder small enough to read in one sitting.
"""
from __future__ import annotations

import struct
import zlib

__all__ = ["encode_grayscale", "render_matrix"]

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, tag, payload, CRC over tag+payload."""
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_grayscale(width: int, height: int, pixels: bytes, *, level: int = 9) -> bytes:
    """Encode 8-bit greyscale pixels (one byte per pixel, row-major) as a PNG."""
    if width <= 0 or height <= 0:
        raise ValueError("the image needs a positive width and height")
    expected = width * height
    if len(pixels) != expected:
        raise ValueError(f"expected {expected} pixel bytes, got {len(pixels)}")
    # Every scanline starts with a filter byte; 0 means "no filter", which is
    # all this encoder needs -- flat colours and sharp module edges.
    raw = b"".join(
        b"\x00" + pixels[row * width:(row + 1) * width] for row in range(height)
    )
    # bit depth 8, colour type 0 (greyscale), no compression/filter tricks,
    # no interlacing.
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        _SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, level))
        + _chunk(b"IEND", b"")
    )


def render_matrix(
    matrix: list[list[bool]], scale: int = 1, *, dark: int = 0, light: int = 255
) -> bytes:
    """Render a matrix of booleans (``True`` means dark) as a greyscale PNG.

    Each cell becomes a ``scale`` by ``scale`` block of pixels, so the result
    is crisp at any size: no resampling is involved.  An even module size is
    what makes a QR code easy to scan.
    """
    rows = len(matrix)
    if rows == 0:
        raise ValueError("the matrix is empty")
    columns = len(matrix[0])
    step = max(1, int(scale))
    dark_pixel = bytes((dark,)) * step
    light_pixel = bytes((light,)) * step
    lines = []
    for row in matrix:
        line = b"".join(dark_pixel if cell else light_pixel for cell in row)
        lines.append(line * step)
    return encode_grayscale(columns * step, rows * step, b"".join(lines))
