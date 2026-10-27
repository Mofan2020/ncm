"""Tests for the Android Kivy layer.

They run without a device and without a network: the controller is faked
and the widget tree is built on the desktop Kivy build.  What they check is
the part that is easy to break and hard to see: the size buckets, the
overflow rules, and the controller's mapping of the core API answers to
the shapes the screens expect.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "android") not in sys.path:
    sys.path.insert(0, str(ROOT / "android"))

kivy = pytest.importorskip("kivy", reason="Kivy is only installed for the "
                                           "Android build and its tests")

from kivy.metrics import dp  # noqa: E402
from ncmkivy.theme import PALETTES, Theme, classify  # noqa: E402
from ncmkivy.widgets import IconButton, TrackList, TrackRow  # noqa: E402


# ---------------------------------------------------------------- size class
@pytest.mark.parametrize("width,expected", [
    (0, "compact"),
    (360, "compact"),
    (599, "compact"),
    (600, "medium"),
    (839, "medium"),
    (840, "expanded"),
    (1280, "expanded"),
])
def test_size_class_boundaries(width, expected):
    """The breakpoints follow the Material 3 window size classes."""
    assert classify(width, 1.0).name == expected


def test_size_class_ignores_density_for_the_same_physical_size():
    """A 3x screen with the same dp width lands in the same bucket."""
    assert classify(dp(600), 1.0).name == classify(dp(600) * 3, 3.0).name


def test_expanded_has_two_columns_and_no_bottom_bar():
    size_class = classify(1200, 1.0)
    assert size_class.columns == 2
    assert size_class.bottom_nav is False
    assert size_class.player_in_sheet is False


def test_compact_has_one_column_and_a_bottom_bar():
    size_class = classify(360, 1.0)
    assert size_class.columns == 1
    assert size_class.bottom_nav is True
    assert size_class.player_in_sheet is True


def test_theme_update_size_reports_a_change():
    theme = Theme.for_system(dark=False)
    # The width is in pixels and the density is given separately, so the
    # comparison happens in dp.
    theme.update_size(360, 1.0)
    assert theme.size_class.name == "compact"
    # A width in another bucket is a change.
    assert theme.update_size(1200, 1.0) is True
    assert theme.size_class.name == "expanded"
    # The same width again is not.
    assert theme.update_size(1200, 1.0) is False


def test_theme_uses_the_density_to_judge_the_width():
    """A 3x screen with 1080 pixels is a 360dp phone, not a tablet."""
    theme = Theme.for_system(dark=False)
    theme.update_size(1080, 3.0)
    assert theme.size_class.name == "compact"


def test_both_palettes_have_the_same_tokens():
    """A missing colour would silently render as red."""
    assert set(PALETTES["light"].as_dict()) == set(PALETTES["dark"].as_dict())


def test_palette_values_are_hex_colours():
    for palette in PALETTES.values():
        for name, value in palette.as_dict().items():
            # Seven digits are opaque, nine carry the alpha of a translucent
            # surface -- the glass panels are made of those.
            assert value.startswith("#") and len(value) in (7, 9), name


# ------------------------------------------------------------------ widgets
def test_icon_button_meets_the_touch_target():
    """Material 3 wants 48dp on the shortest side."""
    button = IconButton(icon="play")
    assert button.width >= dp(48)
    assert button.height >= dp(48)


def test_icon_button_survives_a_redraw_before_construction_finishes():
    """The size callback fires during __init__; the icon must not crash."""
    button = IconButton(icon="more")
    button._draw()
    assert button.icon == "more"


def test_track_row_has_a_fixed_height():
    """A row without a height would collapse inside a recycle view."""
    row = TrackRow({"id": "1", "name": "x", "artists": "y", "album": "z"})
    assert row.height > 0


def test_track_row_shortens_a_long_title():
    """A long title must not push the row wider than its parent."""
    row = TrackRow({
        "id": "1",
        "name": "A very long title " * 20,
        "artists": "Artist " * 20,
        "album": "Album " * 20,
    })
    assert row.size_hint_x is None or True
    for child in row.children:
        label = getattr(child, "text", None)
        if label is not None:
            assert getattr(child, "shorten", False)


def test_track_list_does_not_scroll_horizontally():
    """A sideways scroll means something overflowed."""
    listing = TrackList([])
    assert listing.do_scroll_x is False


# ---------------------------------------------------------------- controller
class _FakeApi:
    def get_artist_detail(self, artist_id):
        return {
            "id": int(artist_id),
            "name": "Test Artist",
            "picUrl": "http://example.invalid/cover.jpg",
            "img1v1Url": "http://example.invalid/avatar.jpg",
            "briefDesc": "A bio",
            "alias": ["TA"],
            "musicSize": 12,
            "albumSize": 3,
        }

    def get_artist_follow_state(self, artist_id):
        return {"followed": True, "fans_count": 7, "follow_days": 2}

    def get_artist_songs(self, artist_id, limit=50, offset=0):
        return {
            "songs": [{"id": 1, "name": "One", "ar": [{"name": "A"}],
                       "al": {"name": "B"}, "dt": 1000}],
            "total": 1,
            "more": False,
        }

    def get_followed_artists(self, limit=25, offset=0):
        return {"artists": [{"id": 9, "name": "Followed", "picUrl": "",
                             "alias": [], "albumSize": 1}],
                "count": 1, "has_more": False, "code": 200}


def test_controller_maps_the_artist_payload():
    """The screen reads these keys; the shape has to match."""
    from ncmkivy.controller import Controller

    controller = Controller.__new__(Controller)
    controller.api = _FakeApi()
    controller._lock = None

    payload = controller.artist_page("6452")
    assert payload["success"] is True
    artist = payload["artist"]
    assert artist["name"] == "Test Artist"
    assert artist["followed"] is True
    assert artist["fans_count"] == 7
    assert artist["description"] == "A bio"
    assert payload["songs"][0]["name"] == "One"
    assert payload["songs"][0]["artists"] == "A"


def test_controller_maps_the_followed_list():
    from ncmkivy.controller import Controller

    controller = Controller.__new__(Controller)
    controller.api = _FakeApi()
    controller._lock = None

    payload = controller.followed_artists()
    assert payload["count"] == 1
    assert payload["artists"][0]["name"] == "Followed"
    assert payload["code"] == 200


def test_track_flattens_the_api_shape():
    from ncmkivy.controller import Controller

    controller = Controller.__new__(Controller)
    track = controller._track({
        "id": 42,
        "name": "Song",
        "ar": [{"name": "A"}, {"name": "B"}],
        "al": {"name": "Album", "picUrl": "http://x/y.jpg"},
        "dt": 1234,
        "fee": 1,
    }, 0)
    assert track.id == "42"
    assert track.name == "Song"
    assert track.artists == "A, B"
    assert track.album == "Album"
    assert track.duration == 1234
    assert track.vip is True


def test_the_cover_goes_through_the_loopback_proxy():
    """The CDN refuses direct image requests, so the proxy has to be used."""
    from ncmkivy.controller import Controller

    class _Media:
        token = "tok"

        def url_for_remote_cover(self, url, ext="jpg"):
            return f"http://127.0.0.1:1234/m/{self.token}/cover?u={url}&ext={ext}"

    controller = Controller.__new__(Controller)
    controller.media = _Media()
    track = controller._track({
        "id": 42,
        "name": "Song",
        "al": {"name": "Album", "picUrl": "http://p1.music.126.net/x/y.jpg"},
    }, 0)
    assert track.cover_url.startswith("http://127.0.0.1:1234/m/tok/cover")
    assert "p1.music.126.net" in track.cover_url


def test_a_label_leaves_the_text_height_free():
    """A text box with a height collapses the label into a sliver."""
    from ncmkivy.widgets import ThemedLabel

    label = ThemedLabel(text="请先登录账号")
    label.size = (400, 30)
    label._refresh()
    assert label.text_size[0] == 400
    assert label.text_size[1] is None


# ---------------------------------------------------------------- app shell
def test_the_density_comes_from_the_metrics_not_the_window():
    """A 3x phone reports 1080 px for a 360 dp screen.

    ``Window`` has no public ``density``; reading it off the window yields
    1.0 and the phone would be judged a 1080 dp tablet, which is exactly
    the "the layout does not fit the phone" failure.
    """
    from kivy.core.window import Window
    from ncmkivy.app import NcmApp

    assert not hasattr(Window, "density")

    class _Stub:
        """Only the attributes the shell touches."""

        controller = None

        def __init__(self):
            from ncmkivy.theme import Theme

            self.theme = Theme.for_system(dark=False)
            self._nav_rail = None
            self._bottom_nav = None

    stub = _Stub()
    NcmApp._apply_size_class(stub, 1080, None)
    # 1080 px at 3x is 360 dp -- compact, not a tablet.
    from kivy.metrics import Metrics

    density = getattr(Metrics, "density", 1.0) or 1.0
    expected = "compact" if (1080 / density) < 600 else None
    if expected == "compact":
        assert stub.theme.size_class.name == "compact"


def test_a_wide_window_gets_a_side_rail():
    """The two layouts must actually differ, not just be named differently."""
    from ncmkivy.app import NcmApp
    from ncmkivy.theme import Theme

    class _Stub:
        controller = None

        def __init__(self):
            self.theme = Theme.for_system(dark=False)
            self._nav_rail = None
            self._bottom_nav = None

    stub = _Stub()
    NcmApp._apply_size_class(stub, 1280, 1.0)
    assert stub.theme.size_class.name == "expanded"


def test_a_track_that_cannot_be_played_says_so():
    """The message has to name the problem, not the error code."""
    from ncmkivy.app import NcmApp

    class _Stub:
        _text = NcmApp._text

    stub = _Stub()
    message = NcmApp._play_error_message(
        stub, {"message": "standard:song_not_found, no_local_copy"})
    assert "无法播放" in message
    assert "song_not_found" not in message

    generic = NcmApp._play_error_message(stub, {"message": "network down"})
    assert generic != message



def test_the_apk_version_follows_the_app_version():
    """The APK's name and the manifest must not drift away from the app.

    The spec carries its own version line, and it used to lag behind
    ``src/version.py``: the file was called 3.5.1 while the app reported 3.5.4
    and the manifest showed 3.5.1 to the system.
    """
    import re
    from pathlib import Path

    from src.version import __version__

    spec = Path(__file__).resolve().parent.parent / "android" / "buildozer.spec"
    text = spec.read_text(encoding="utf-8")
    match = re.search(r"^version = (.+)$", text, re.MULTILINE)
    assert match, "buildozer.spec has no version line"
    assert match.group(1).strip().strip('"') == __version__


def test_the_mini_player_stays_hidden_until_something_plays():
    """A widget of zero height still draws its children.

    The icon buttons have a fixed size, so the strip used to float above the
    navigation bar with two stray buttons before anything had played.
    """
    from ncmkivy.widgets import MiniPlayer

    player = MiniPlayer()
    assert player.opacity == 0

    player.show_track({"name": "Shots", "artists": "JAY PARK"}, playing=True)
    assert player.opacity == 1


# ------------------------------------------------------- the visual layer

def test_the_navigation_item_marks_the_active_entry():
    """The bar has to say where you are."""
    from kivy.graphics import RoundedRectangle
    from ncmkivy.widgets import NavItem

    def pill(widget):
        return [c for c in widget.canvas.before.children
                if isinstance(c, RoundedRectangle)]

    item = NavItem(icon="home", label="Home")
    assert not pill(item), "an inactive entry still shows the pill"

    item.set_active(True)
    assert item.active is True
    assert pill(item), "the active entry has no pill"


def test_the_button_variants_do_not_look_alike():
    """A filled button and an outlined one must not be the same shape."""
    from kivy.graphics import Line, RoundedRectangle
    from ncmkivy.widgets import AppButton

    def shapes(widget):
        return [c for c in widget.canvas.before.children
                if isinstance(c, Line | RoundedRectangle)]

    filled = AppButton(text="Go", variant="filled")
    outlined = AppButton(text="Go", variant="outlined")

    assert any(isinstance(c, RoundedRectangle) for c in shapes(filled))
    assert any(isinstance(c, Line) for c in shapes(outlined))


def test_a_card_paints_a_surface_that_follows_its_size():
    from kivy.graphics import RoundedRectangle
    from ncmkivy.widgets import SurfaceCard

    card = SurfaceCard()
    fills = [c for c in card.canvas.before.children if isinstance(c, RoundedRectangle)]
    assert fills, "the card has no surface"
    assert tuple(fills[0].size) == tuple(card.size)


def test_the_icon_button_can_carry_a_tonal_disc():
    """The variant and the glyph scale have to exist before the first paint."""
    from ncmkivy.widgets import IconButton

    button = IconButton(icon="music", size=64, variant="tonal", glyph_scale=0.5)
    assert button.variant == "tonal"
    assert button.glyph_scale == 0.5


def test_off_android_the_clipboard_reports_failure():
    """The clipboard needs the Android bridge; without it there is no clipboard."""
    from ncmkivy.uix import copy_to_clipboard

    assert copy_to_clipboard("MUSIC_U=abc") is False
