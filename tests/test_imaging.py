"""The wallpaper is resized by the platform's own image tool.

No imaging library ships with the app (see :mod:`src.core.imaging`), so the
tests here split in two: the arithmetic and the fallbacks run everywhere,
the real conversion only where the platform tool exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.core import imaging
from src.core.png import render_matrix

requires_tool = pytest.mark.skipif(
    not imaging.has_backend(), reason="no platform image tool on this OS"
)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Width and height from the JPEG frame header."""
    assert data.startswith(b"\xff\xd8"), "not a JPEG"
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + length
    raise AssertionError("no JPEG frame header")


def _fixture(path: Path, size: int = 64) -> Path:
    pattern = [[(column + row) % 3 == 0 for column in range(size)] for row in range(size)]
    path.write_bytes(render_matrix(pattern, 4))
    return path


# ------------------------------------------------------------------ geometry

def test_a_small_image_is_left_alone():
    """sips happily enlarges a small image; the callers must not."""
    assert imaging.target_size(800, 600, 3840) == (800, 600)


def test_the_aspect_ratio_is_kept():
    assert imaging.target_size(4000, 3000, 1920) == (1920, 1440)
    assert imaging.target_size(3000, 4000, 1920) == (1440, 1920)


def test_a_very_thin_image_still_gets_a_pixel():
    assert imaging.target_size(10000, 4, 100) == (100, 1)


def test_the_size_is_unchanged_without_a_limit():
    assert imaging.target_size(640, 480, 0) == (640, 480)


# ------------------------------------------------------------------ fallback

def test_a_missing_source_is_not_an_error(tmp_path):
    assert imaging.downscale_to_jpeg(tmp_path / "nope.png", tmp_path / "out.jpg",
                                     max_edge=100, quality=80) is False


def test_no_backend_means_no_conversion(tmp_path, monkeypatch):
    """On a platform without a tool the caller keeps the original file."""
    source = _fixture(tmp_path / "wall.png")
    target = tmp_path / "wall.jpg"
    monkeypatch.setattr(sys, "platform", "linux")
    assert imaging.downscale_to_jpeg(source, target, max_edge=100, quality=80) is False
    assert not target.exists(), "a half-written file must not be left behind"


# ------------------------------------------------------------- the real tool

@requires_tool
def test_the_wallpaper_is_downscaled_to_a_jpeg(tmp_path):
    source = _fixture(tmp_path / "wall.png")          # 256x256
    target = tmp_path / "wall.jpg"
    assert imaging.downscale_to_jpeg(source, target, max_edge=128, quality=80)
    width, height = _jpeg_size(target.read_bytes())
    assert (width, height) == (128, 128)


@requires_tool
def test_a_small_wallpaper_is_not_enlarged(tmp_path):
    source = _fixture(tmp_path / "small.png", size=32)   # 128x128
    target = tmp_path / "small.jpg"
    assert imaging.downscale_to_jpeg(source, target, max_edge=1920, quality=88)
    width, height = _jpeg_size(target.read_bytes())
    assert (width, height) == (128, 128)


@requires_tool
def test_an_unreadable_file_returns_false(tmp_path):
    source = tmp_path / "broken.png"
    source.write_bytes(b"not an image")
    target = tmp_path / "broken.jpg"
    assert imaging.downscale_to_jpeg(source, target, max_edge=100, quality=80) is False
    assert not target.exists()
