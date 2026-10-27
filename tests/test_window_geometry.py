"""The window has to fit the screen it is opened on.

A window bigger than the screen is cut off by it, and the parts hanging over
the edge cannot be reached -- on a small or high-DPI display the player bar
ends up outside the visible area.  MIN_SIZE used to win over the screen, so
the window was inflated past the edge of the display no matter how small
that display was.
"""
from __future__ import annotations

import main
from main import FLOOR_SIZE, MIN_SIZE, _fit_to_screen, _initial_geometry, _window_min_size


def test_a_roomy_screen_keeps_the_stored_size():
    assert _fit_to_screen(1080, 720, (1920, 1080)) == (1080, 720)


def test_the_screen_wins_over_the_minimum_size():
    """1366x768 at 150% scaling is 911x512 logical pixels."""
    width, height = _fit_to_screen(1080, 720, (911, 512))
    assert width <= 911
    assert height <= 512
    assert height < MIN_SIZE[1], "the minimum size was allowed past the screen"


def test_a_small_screen_still_gets_a_usable_window():
    width, height = _fit_to_screen(1080, 720, (640, 480))
    assert width >= FLOOR_SIZE[0]
    assert height >= FLOOR_SIZE[1]
    assert width <= 640
    assert height <= 480


def test_the_window_is_placed_inside_the_screen():
    width, height, x, y = _initial_geometry(1080, 720, (911, 512, 0, 0))
    assert x is not None and y is not None
    assert x >= 0 and x + width <= 911
    assert y >= 0 and y + height <= 512


def test_the_minimum_size_never_exceeds_the_screen():
    min_width, min_height = _window_min_size((911, 512, 0, 0))
    assert min_width <= 911
    assert min_height <= 512
    assert _window_min_size((2560, 1440, 0, 0)) == MIN_SIZE


def test_a_window_that_fits_is_not_moved_upwards():
    """The window keeps its place when the screen is big enough."""
    assert _initial_geometry(1080, 720, (2560, 1440, 0, 0)) == (
        1080, 720, (2560 - 1080) // 2, (1440 - 720) // 3)


def test_without_a_screen_the_size_is_untouched(monkeypatch):
    monkeypatch.setattr(main, "_primary_screen", lambda: None)
    assert main._initial_geometry(1080, 720) == (1080, 720, None, None)
