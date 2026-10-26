"""The stored wallpaper is downscaled, not copied verbatim.

Pillow is no longer a runtime dependency: the resizing is done by the
platform's own image tool (:mod:`src.core.imaging`).  Pillow is still a
*development* dependency, so it builds the fixtures here; the tests that need
a real conversion are skipped on a machine whose OS has no such tool (Linux),
where the app keeps the original file instead.
"""
from __future__ import annotations

import pytest

from src.core import imaging

pytest.importorskip("PIL", reason="Pillow builds the fixtures (development only)")

from PIL import Image  # noqa: E402

requires_tool = pytest.mark.skipif(
    not imaging.has_backend(), reason="no platform image tool on this OS"
)


@pytest.fixture()
def settings(isolated_home):
    from src.config.settings import Settings

    return Settings()


def _write_png(path, size=(3600, 2400)):
    Image.new("RGBA", size, (30, 40, 60, 255)).save(path)
    return path


@requires_tool
def test_a_large_wallpaper_is_stored_as_a_jpeg(settings, tmp_path):
    source = _write_png(tmp_path / "wallpaper.png")
    name = settings.set_background_image(source)
    assert name.endswith(".jpg")

    stored = settings.config_path.parent / name
    assert stored.is_file()
    with Image.open(stored) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"


@requires_tool
def test_the_stored_wallpaper_is_smaller_than_the_source(settings, tmp_path):
    """A noisy image compresses far better as JPEG than as PNG."""
    source = tmp_path / "wallpaper.png"
    noise = Image.effect_noise((900, 600), 60).convert("RGBA")
    noise.save(source)

    name = settings.set_background_image(source)
    stored = settings.config_path.parent / name
    assert stored.stat().st_size < source.stat().st_size


@requires_tool
def test_an_oversized_wallpaper_is_scaled_down(settings, tmp_path):
    from src.config.settings import BACKGROUND_MAX_EDGE

    source = _write_png(tmp_path / "huge.png", (BACKGROUND_MAX_EDGE + 160, 900))
    name = settings.set_background_image(source)
    stored = settings.config_path.parent / name
    with Image.open(stored) as image:
        assert max(image.size) <= BACKGROUND_MAX_EDGE


@requires_tool
def test_a_small_wallpaper_keeps_its_size(settings, tmp_path):
    source = _write_png(tmp_path / "small.png", (800, 600))
    name = settings.set_background_image(source)
    stored = settings.config_path.parent / name
    with Image.open(stored) as image:
        assert image.size == (800, 600)


def test_without_an_image_tool_the_original_is_kept(settings, tmp_path, monkeypatch):
    """Linux (or a stripped Windows) must still get a wallpaper."""
    monkeypatch.setattr(imaging, "downscale_to_jpeg", lambda *a, **k: False)
    source = _write_png(tmp_path / "wallpaper.png", (400, 300))

    name = settings.set_background_image(source)
    stored = settings.config_path.parent / name
    assert stored.is_file()
    assert stored.read_bytes() == source.read_bytes()


def test_the_old_wallpaper_is_replaced_not_kept(settings, tmp_path):
    """A second wallpaper replaces the first one -- only one is kept.

    The display-sized copy (``background.display.jpg``) is a derived file,
    not a wallpaper, so it is allowed to sit next to it.
    """
    source = _write_png(tmp_path / "wallpaper.png")
    settings.set_background_image(source)
    name = settings.set_background_image(source)
    stored = settings.config_path.parent / name
    leftovers = [
        p for p in stored.parent.glob("background.*")
        if p != stored and ".display." not in p.name
    ]
    assert leftovers == [], leftovers


@requires_tool
def test_the_display_copy_is_built_next_to_the_wallpaper(settings, tmp_path):
    """The display-sized copy is written when the wallpaper is set."""
    source = _write_png(tmp_path / "wallpaper.png", (2400, 1600))
    name = settings.set_background_image(source)
    stored = settings.config_path.parent / name
    display = stored.parent / "background.display.jpg"
    assert display.is_file()
    with Image.open(display) as image:
        assert max(image.size) <= 1920


def test_setting_a_background_records_its_name(settings, tmp_path):
    source = _write_png(tmp_path / "wallpaper.png")
    name = settings.set_background_image(source)
    assert settings.ui.background_image == name
    assert settings.background_path() is not None


def test_clearing_removes_the_file(settings, tmp_path):
    source = _write_png(tmp_path / "wallpaper.png")
    settings.set_background_image(source)
    settings.clear_background_image()
    assert settings.ui.background_image == ""
    assert settings.background_path() is None


@requires_tool
def test_a_legacy_png_wallpaper_is_rewritten(settings, tmp_path):
    """A wallpaper saved before the downscale existed is migrated in place."""
    from src.config.settings import BACKGROUND_REWRITE_THRESHOLD

    source = settings.config_path.parent / "background.png"
    noise = Image.effect_noise((1200, 900), 60).convert("RGBA")
    noise.save(source)
    # Make it big enough to clear the threshold.
    with open(source, "ab") as handle:
        handle.write(b"\0" * BACKGROUND_REWRITE_THRESHOLD)
    settings.update_ui(background_image="background.png")

    assert settings.migrate_background_image() is True
    assert settings.ui.background_image == "background.jpg"
    stored = settings.config_path.parent / "background.jpg"
    assert stored.is_file()
    assert not source.exists()


def test_the_migration_is_a_no_op_for_a_jpeg(settings, tmp_path):
    source = _write_png(tmp_path / "wallpaper.png")
    settings.set_background_image(source)
    assert settings.migrate_background_image() is False


def test_the_migration_is_a_no_op_without_a_wallpaper(settings):
    settings.clear_background_image()
    assert settings.migrate_background_image() is False
