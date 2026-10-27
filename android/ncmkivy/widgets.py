"""Reusable Kivy widgets.

Two rules drive everything here:

* **nothing may overflow.**  Every text widget sets a real width budget and
  shortens instead of pushing its container sideways; every scrollable
  container scrolls vertically only;
* **touch targets are at least 48dp.**  The icon buttons go through
  :func:`IconButton`, which pads a 24dp glyph up to a 48dp target.

The widgets read their colours from ``app.theme`` so a theme switch does not
need a rebuild.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kivy.graphics import Color, Line, Rectangle, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import AsyncImage
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.widget import Widget

log = logging.getLogger("ncm.kivy.widgets")

__all__ = [
    "ThemedLabel",
    "IconButton",
    "CoverImage",
    "SurfaceCard",
    "NavItem",
    "TrackRow",
    "TrackList",
    "MiniPlayer",
    "EmptyState",
    "AppButton",
    "SectionHeader",
    "LoadingView",
]


def _theme() -> Any:
    """The active theme, or ``None`` before the app is built."""
    from kivy.app import App

    app = App.get_running_app()
    return getattr(app, "theme", None)


def _color(name: str, fallback: str = "#FFFFFF") -> str:
    theme = _theme()
    if theme is None:
        return fallback
    return theme.color(name)


class ThemedLabel(Label):
    """A label that follows the theme and never overflows its box.

    ``shorten`` cuts the text with an ellipsis, and the default is a single
    line: a two-line title in a list row is what makes a phone layout look
    broken.
    """

    def __init__(self, text: str = "", role: str = "body_medium",
                 color_name: str = "on_surface", **kwargs: Any) -> None:
        theme = _theme()
        size = _role_size(role) if theme is None else getattr(theme, role)()
        kwargs.setdefault("font_size", size)
        kwargs.setdefault("shorten", True)
        kwargs.setdefault("shorten_from", "right")
        kwargs.setdefault("halign", "left")
        kwargs.setdefault("valign", "middle")
        kwargs.setdefault("color", _color(color_name))
        super().__init__(text=text, **kwargs)
        self._color_name = color_name
        self._role = role
        self.bind(size=self._refresh, texture_size=self._refresh)

    def _refresh(self, *_args: Any) -> None:
        """Re-apply the colour and the text box: the theme may have changed."""
        self.color = _color(self._color_name)
        # Kivy centres text unless ``text_size`` is set, and ``halign`` only
        # takes effect inside that box.  The height stays unset on purpose:
        # the texture is clipped to ``text_size``, so a box with a height
        # makes the texture shorter, the label's minimum height follow it
        # down, and in a container that shares its height with other
        # widgets the label collapses into a sliver.  Width alone is enough
        # for alignment and for ``shorten``.
        if self.text_size[0] != self.width:
            self.text_size = (self.width, None)

    def set_role(self, role: str) -> None:
        theme = _theme()
        if theme is not None and hasattr(theme, role):
            self.font_size = getattr(theme, role)()
            self._role = role

    def set_color_name(self, color_name: str) -> None:
        """Switch the colour role, e.g. when a row becomes the active one."""
        self._color_name = color_name
        self.color = _color(color_name)


def _role_size(role: str) -> float:
    """Fallback sizes for the case where the theme is not up yet."""
    return {
        "display_large": dp(52),
        "headline_medium": dp(26),
        "title_large": dp(20),
        "title_medium": dp(16),
        "body_large": dp(16),
        "body_medium": dp(14),
        "body_small": dp(12),
        "label_small": dp(11),
    }.get(role, dp(14))


class IconButton(ButtonBehavior, Widget):
    """A square button with a glyph in the middle.

    The glyph is drawn from a small built-in icon set -- Kivy has no icon
    font, and shipping an SVG font would add a dependency.  Icons are drawn
    as rectangles and lines on the canvas, which keeps them crisp at any
    density.
    """

    #: Class-level defaults: the size callback can fire while the widget is
    #: still being constructed, so the attributes must exist before that.
    _color_name = "on_surface"
    _glyph_color = (1.0, 1.0, 1.0, 1.0)
    icon = "more"
    variant = "plain"
    glyph_scale = 1.0
    _pressed = False

    #: The glyphs this button knows how to draw.
    ICONS = {
        "play": "triangle",
        "pause": "bars",
        "next": "triangle_skip",
        "prev": "triangle_prev",
        "search": "magnifier",
        "home": "house",
        "library": "stack",
        "heart": "heart",
        "heart_outline": "heart_outline",
        "download": "arrow_down",
        # ``SECTIONS`` in app.py names the glyph directly, so the kind names
        # have to resolve too -- without these two the bottom bar falls back
        # to the "dots" placeholder for the library and download tabs.
        "arrow_down": "arrow_down",
        "stack": "stack",
        "more": "dots",
        "back": "chevron_left",
        "list": "list",
        "person": "person",
        "close": "cross",
        "refresh": "arrows",
        "plus": "plus",
        "check": "check",
        "music": "note",
    }

    def __init__(self, icon: str = "more", size: float | None = None,
                 color_name: str = "on_surface", variant: str = "plain",
                 glyph_scale: float = 1.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        theme = _theme()
        target = size if size is not None else (
            theme.touch_target() if theme is not None else dp(48))
        self.size_hint = (None, None)
        self.width = target
        self.height = target
        self.icon = icon
        # These two must exist before anything can trigger a redraw: the
        # size callback fires while the widget is still being built.
        self._color_name = color_name
        self._glyph_color = _color(color_name)
        self.variant = variant
        self.glyph_scale = glyph_scale
        self._pressed = False
        self._draw()
        # ``_draw`` can only paint where the widget is *now*, and at
        # construction it sits at (0, 0) with no size.  Redraw whenever the
        # layout moves or resizes it, otherwise the glyph is invisible.
        self.bind(pos=self._on_geometry, size=self._on_geometry)

    def _on_geometry(self, *_args: Any) -> None:
        self._draw()

    def _draw(self) -> None:
        self.canvas.clear()
        with self.canvas:
            Color(1, 1, 1, 0)
            self._glyph_color = _color(self._color_name)
            if self.variant in ("tonal", "filled"):
                from kivy.graphics import Ellipse

                base = "secondary_container" if self.variant == "tonal" else "primary"
                Color(*_rgba(_color(base)))
                Ellipse(pos=self.pos, size=self.size)
            if self._pressed:
                # Material 3's state layer: eight percent of the label colour
                # over whatever is underneath, so the press is visible on
                # every background.
                from kivy.graphics import Ellipse

                Color(*_rgba(_color(self._color_name), 0.12))
                Ellipse(pos=self.pos, size=self.size)
            Color(*_rgba(self._glyph_color))
            self._paint(self._glyph_color)

    def _paint(self, glyph_color: str) -> None:
        """Draw the glyph.

        Everything is expressed as a fraction of the button size so the icon
        scales with the touch target.  ``glyph_color`` arrives as a hex
        string: it is converted once here because every shape branch below
        hands the value straight to ``Color``, and a hex string given to
        ``Color`` that way draws nothing at all -- no error, no glyph.
        """
        rgb = _rgba(glyph_color)
        size = min(self.width, self.height) * self.glyph_scale
        cx, cy = self.center_x, self.center_y
        kind = self.ICONS.get(self.icon, "dots")

        if kind == "triangle":
            half = size * 0.18
            Color(*rgb)
            from kivy.graphics import Triangle
            Triangle(points=(cx - half * 0.6, cy - half,
                            cx - half * 0.6, cy + half,
                            cx + half, cy))
        elif kind == "triangle_skip" or kind == "triangle_prev":
            from kivy.graphics import Triangle
            half = size * 0.16
            sign = 1 if kind == "triangle_skip" else -1
            Color(*rgb)
            Triangle(points=(cx - half * sign, cy - half,
                            cx - half * sign, cy + half,
                            cx + half * 0.7 * sign, cy))
            Triangle(points=(cx + half * 0.2 * sign, cy - half,
                            cx + half * 0.2 * sign, cy + half,
                            cx + half * 1.1 * sign, cy))
        elif kind == "bars":
            from kivy.graphics import Rectangle
            Color(*rgb)
            width = size * 0.12
            gap = size * 0.1
            Rectangle(pos=(cx - width - gap / 2, cy - size * 0.2),
                      size=(width, size * 0.4))
            Rectangle(pos=(cx + gap / 2, cy - size * 0.2),
                      size=(width, size * 0.4))
        elif kind == "dots":
            from kivy.graphics import Ellipse
            Color(*rgb)
            radius = size * 0.06
            for dx in (-size * 0.18, 0, size * 0.18):
                Ellipse(pos=(cx + dx - radius, cy - radius),
                        size=(radius * 2, radius * 2))
        elif kind == "heart" or kind == "heart_outline":
            from kivy.graphics import Ellipse
            Color(*rgb)
            r = size * 0.16
            Ellipse(pos=(cx - r * 1.3, cy - r * 0.6), size=(r * 2, r * 2))
            Ellipse(pos=(cx - r * 0.3, cy - r * 0.6), size=(r * 2, r * 2))
            from kivy.graphics import Triangle
            Triangle(points=(cx - r * 1.3, cy - r * 0.2,
                            cx + r * 1.3, cy - r * 0.2,
                            cx, cy - r * 1.9))
        elif kind == "magnifier":
            from kivy.graphics import Ellipse, Line
            Color(*rgb)
            r = size * 0.15
            # Circle to the upper left, handle running out to the lower right.
            Ellipse(pos=(cx - r * 1.7, cy - r * 0.3), size=(r * 2, r * 2))
            Line(points=[cx, cy, cx + r * 1.3, cy - r * 1.3],
                 width=dp(2.2), cap="round")
        elif kind == "arrow_down":
            from kivy.graphics import Line, Triangle
            Color(*rgb)
            Line(points=[cx, cy + size * 0.22, cx, cy - size * 0.1], width=2)
            Triangle(points=(cx - size * 0.12, cy - size * 0.05,
                            cx + size * 0.12, cy - size * 0.05,
                            cx, cy - size * 0.22))
        elif kind == "chevron_left":
            from kivy.graphics import Line
            Color(*rgb)
            Line(points=[cx + size * 0.1, cy + size * 0.18,
                         cx - size * 0.08, cy,
                         cx + size * 0.1, cy - size * 0.18], width=2.2)
        elif kind == "cross":
            from kivy.graphics import Line
            Color(*rgb)
            Line(points=[cx - size * 0.15, cy - size * 0.15,
                         cx + size * 0.15, cy + size * 0.15], width=2)
            Line(points=[cx - size * 0.15, cy + size * 0.15,
                         cx + size * 0.15, cy - size * 0.15], width=2)
        elif kind == "plus":
            from kivy.graphics import Line
            Color(*rgb)
            Line(points=[cx - size * 0.15, cy, cx + size * 0.15, cy], width=2)
            Line(points=[cx, cy - size * 0.15, cx, cy + size * 0.15], width=2)
        elif kind == "check":
            from kivy.graphics import Line
            Color(*rgb)
            Line(points=[cx - size * 0.16, cy,
                         cx - size * 0.04, cy - size * 0.12,
                         cx + size * 0.18, cy + size * 0.16], width=2.4)
        elif kind == "note":
            from kivy.graphics import Ellipse, Rectangle
            Color(*rgb)
            r = size * 0.1
            Ellipse(pos=(cx - r * 1.4, cy - r * 1.6), size=(r * 2, r * 2))
            Rectangle(pos=(cx - r * 0.3, cy - r * 1.6), size=(r * 0.4, size * 0.32))
        elif kind == "arrows":
            from kivy.graphics import Line
            Color(*rgb)
            Line(points=[cx - size * 0.16, cy + size * 0.05,
                         cx, cy + size * 0.2,
                         cx + size * 0.16, cy + size * 0.05], width=2)
            Line(points=[cx - size * 0.16, cy - size * 0.05,
                         cx, cy - size * 0.2,
                         cx + size * 0.16, cy - size * 0.05], width=2)
        elif kind == "house":
            from kivy.graphics import Rectangle, Triangle
            Color(*rgb)
            Triangle(points=(cx, cy + size * 0.22,
                            cx - size * 0.22, cy,
                            cx + size * 0.22, cy))
            Rectangle(pos=(cx - size * 0.16, cy - size * 0.2),
                      size=(size * 0.32, size * 0.22))
        elif kind == "stack":
            from kivy.graphics import Rectangle
            Color(*rgb)
            for index in range(3):
                Rectangle(pos=(cx - size * 0.18, cy - size * 0.18 + index * size * 0.13),
                          size=(size * 0.36, size * 0.1))
        elif kind == "list":
            from kivy.graphics import Rectangle
            Color(*rgb)
            for index in range(3):
                y = cy + size * 0.16 - index * size * 0.15
                Rectangle(pos=(cx - size * 0.2, y - size * 0.03),
                          size=(size * 0.08, size * 0.06))
                Rectangle(pos=(cx - size * 0.08, y - size * 0.03),
                          size=(size * 0.28, size * 0.06))
        elif kind == "person":
            from kivy.graphics import Ellipse, Rectangle
            Color(*rgb)
            Ellipse(pos=(cx - size * 0.1, cy + size * 0.02),
                    size=(size * 0.2, size * 0.2))
            Rectangle(pos=(cx - size * 0.16, cy - size * 0.2),
                      size=(size * 0.32, size * 0.2))

    def on_touch_down(self, touch: Any) -> bool:
        if not self.collide_point(*touch.pos):
            return False
        self._pressed = True
        self._draw()
        return super().on_touch_down(touch)

    def on_touch_up(self, touch: Any) -> None:
        self._pressed = False
        self._draw()
        super().on_touch_up(touch)

    def on_size(self, *_args: Any) -> None:
        self._draw()

    def set_color_name(self, color_name: str) -> None:
        """Switch the colour role of the glyph."""
        self._color_name = color_name
        self._draw()

    def set_variant(self, variant: str) -> None:
        """Switch the background treatment: plain, tonal or filled."""
        self.variant = variant
        self._draw()


def _rgba(color: str, alpha: float | None = None) -> tuple[float, float, float, float]:
    """``"#RRGGBB"`` or ``"#RRGGBBAA"`` -> an RGBA tuple.

    The eight digit form carries the alpha of a translucent surface, which is
    what the glass panels are made of.  ``alpha`` overrides it, which is how
    the press state layer gets its eight percent.
    """
    value = color.lstrip("#")
    if len(value) in (3, 4):
        value = "".join(ch * 2 for ch in value)
    try:
        red = int(value[0:2], 16) / 255.0
        green = int(value[2:4], 16) / 255.0
        blue = int(value[4:6], 16) / 255.0
        stored = int(value[6:8], 16) / 255.0 if len(value) >= 8 else 1.0
    except (ValueError, IndexError):
        return 1.0, 1.0, 1.0, 1.0
    return (red, green, blue, stored if alpha is None else alpha)


class CoverImage(AsyncImage):
    """A square cover with a rounded, tinted placeholder.

    ``AsyncImage`` alone shows a blank rectangle while loading and a broken
    image on a bad URL; this one paints the container colour underneath so
    the layout never jumps.
    """

    def __init__(self, size: float = dp(56), radius: float | None = None,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.size_hint = (None, None)
        self.width = size
        self.height = size
        self.fit_mode = "cover"
        self._radius = radius if radius is not None else dp(8)
        self.canvas.before.add(Color(1, 1, 1, 0))
        self.canvas.before.add(RoundedRectangle(
            pos=self.pos, size=self.size, radius=[self._radius]))
        self.bind(pos=self._sync, size=self._sync)
        # Cover URLs come back from the API as ``http://``.  Android is
        # strict about cleartext traffic and the loader silently falls back
        # to the "missing image" texture, so every cover shows as a broken
        # glyph.  The same hosts serve https, so upgrade on the way in.
        self.bind(source=self._upgrade_source)
        self.bind(on_error=self._log_load_error)

    def _log_load_error(self, *_args: Any) -> None:
        log.warning("cover failed to load: %s", self.source)

    def _upgrade_source(self, *_args: Any) -> None:
        source = self.source or ""
        if not source.startswith("http://"):
            return
        # The loopback proxy is plain http on purpose; do not touch it.
        if "127.0.0.1" in source or "localhost" in source:
            return
        secure = "https://" + source[len("http://"):]
        if secure != self.source:
            self.source = secure

    def _sync(self, *_args: Any) -> None:
        for item in self.canvas.before.children:
            if isinstance(item, RoundedRectangle):
                item.pos = self.pos
                item.size = self.size


def _fit_height(label: Widget) -> None:
    """Make a label exactly as tall as its text.

    ``minimum_height`` on a Label is an AliasProperty -- there is no event
    to bind to -- so the texture height is what has to be followed.
    """
    label.size_hint_y = None
    label.bind(
        texture_size=lambda _i, _v, _l=label: setattr(_l, "height", _l.texture_size[1]))


class MiniPlayer(BoxLayout):
    """The strip above the navigation: what is playing right now.

    It takes no space when nothing is playing, so the app never carries a
    bar nobody is listening to.
    """

    def __init__(self, on_toggle: Callable[[], None] | None = None,
                 on_next: Callable[[], None] | None = None, **kwargs: Any) -> None:
        super().__init__(orientation="horizontal", size_hint_y=None, height=dp(64),
                         spacing=dp(10), padding=(dp(12), dp(6)), **kwargs)
        self._on_toggle = on_toggle
        self._on_next = on_next

        # The strip takes no space until something plays, and a widget of zero
        # height still draws its children: the icon buttons are a fixed size,
        # so without this they float above the navigation bar on their own.
        self.opacity = 0

        self.cover = CoverImage(size=dp(44), radius=dp(6))
        self.add_widget(self.cover)

        text_box = BoxLayout(orientation="vertical", spacing=dp(2))
        self.title = ThemedLabel(text="", role="title_medium")
        self.subtitle = ThemedLabel(text="", role="body_small",
                                     color_name="on_surface_variant")
        for label in (self.title, self.subtitle):
            _fit_height(label)
        text_box.add_widget(self.title)
        text_box.add_widget(self.subtitle)
        self.add_widget(text_box)

        self.toggle_button = IconButton(icon="play", size=dp(44))
        self.toggle_button.bind(on_release=lambda *_a: self._toggle())
        self.add_widget(self.toggle_button)

        self.next_button = IconButton(icon="next", size=dp(44))
        self.next_button.bind(on_release=lambda *_a: self._next())
        self.add_widget(self.next_button)

    def _toggle(self) -> None:
        if self._on_toggle is not None:
            self._on_toggle()

    def _next(self) -> None:
        if self._on_next is not None:
            self._on_next()

    def show_track(self, track: dict[str, Any], playing: bool = True) -> None:
        """Show a track and make the bar visible."""
        self.opacity = 1
        self.title.text = str(track.get("name") or "")
        self.subtitle.text = str(track.get("artists") or "")
        url = str(track.get("cover_url") or "")
        if url:
            self.cover.source = url
        self.set_playing(playing)

    def set_playing(self, playing: bool) -> None:
        self.toggle_button.icon = "pause" if playing else "play"


class TappableRow(ButtonBehavior, BoxLayout):
    """A row that can be pressed.

    ``BoxLayout`` has no ``on_release`` event of its own: binding one to it
    does nothing, because nothing ever dispatches it.  ``ButtonBehavior``
    brings the press handling and the event with it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class TrackRow(TappableRow):
    """One row in a track list.

    Layout: cover, title / artist, and an optional trailing action.  The row
    is a fixed height so a long list can use a recycle view without the rows
    jumping around.
    """

    def __init__(self, track: dict[str, Any],
                 on_play: Callable[[dict[str, Any]], None] | None = None,
                 on_action: Callable[[dict[str, Any]], None] | None = None,
                 show_cover: bool = True, **kwargs: Any) -> None:
        row_height = dp(64) if show_cover else dp(56)
        super().__init__(orientation="horizontal", size_hint_y=None,
                         height=row_height, spacing=dp(12), **kwargs)
        self.track = track
        self._on_play = on_play
        self._on_action = on_action
        self._pressed = False

        # A press state and a hairline between the rows: without them a list
        # is a wall of text with nothing to catch the eye.
        self.bind(pos=self._sync_row, size=self._sync_row)

        if show_cover:
            cover = CoverImage(size=dp(48))
            url = str(track.get("cover_url") or "")
            if url:
                cover.source = url
            self.add_widget(cover)

        text_box = BoxLayout(orientation="vertical", spacing=dp(2))
        title = ThemedLabel(text=str(track.get("name") or ""),
                            role="title_medium", color_name="on_surface")
        subtitle_text = str(track.get("artists") or "")
        if track.get("album"):
            subtitle_text = f"{subtitle_text} · {track['album']}"
        subtitle = ThemedLabel(text=subtitle_text, role="body_small",
                               color_name="on_surface_variant")
        text_box.add_widget(title)
        text_box.add_widget(subtitle)
        self.add_widget(text_box)

        if on_action is not None:
            action = IconButton(icon="more", color_name="on_surface_variant")
            action.bind(on_release=lambda *_a: on_action(track))
            self.add_widget(action)

        self.bind(on_release=self._handle_release)

    def _handle_release(self, *_args: Any) -> None:
        if self._on_play:
            self._on_play(self.track)

    def on_touch_down(self, touch: Any) -> bool:
        if not self.collide_point(*touch.pos):
            return False
        self._pressed = True
        self._sync_row()
        return super().on_touch_down(touch)

    def on_touch_up(self, touch: Any) -> None:
        self._pressed = False
        self._sync_row()
        super().on_touch_up(touch)

    def _sync_row(self, *_args: Any) -> None:
        self.canvas.before.clear()
        with self.canvas.before:
            if self._pressed:
                Color(*_rgba(_color("on_surface"), 0.08))
                Rectangle(pos=self.pos, size=self.size)
            Color(*_rgba(_color("outline_variant")))
            Line(points=[self.x, self.y, self.x + self.width, self.y], width=dp(1))


class TrackList(ScrollView):
    """A vertical list of :class:`TrackRow` widgets.

    Horizontal scrolling is off, so a long title can never make the page
    scroll sideways.
    """

    def __init__(self, tracks: list[dict[str, Any]] | None = None,
                 on_play: Callable[[dict[str, Any]], None] | None = None,
                 **kwargs: Any) -> None:
        kwargs.setdefault("do_scroll_x", False)
        kwargs.setdefault("bar_width", dp(2))
        super().__init__(**kwargs)
        self._box = BoxLayout(orientation="vertical", size_hint_y=None,
                              spacing=dp(2), padding=(0, dp(4), 0, dp(4)))
        self._box.bind(minimum_height=self._box.setter("height"))
        self.add_widget(self._box)
        self.set_tracks(tracks or [], on_play=on_play)

    def set_tracks(self, tracks: list[dict[str, Any]],
                   on_play: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._box.clear_widgets()
        for track in tracks:
            self._box.add_widget(TrackRow(track, on_play=on_play))

    def show_loading(self, message: str = "") -> None:
        """Put a placeholder in the list while the data arrives.

        A ScrollView takes exactly one child, so the placeholder has to go
        into the box the rows live in -- adding it to the scroll view itself
        raises.
        """
        self._box.clear_widgets()
        self._box.add_widget(LoadingView(message=message))


class SurfaceCard(BoxLayout):
    """A rounded panel that sits on the page.

    A card is a tonal surface with a hairline edge.  Text straight on the
    background is what makes an interface look like a wireframe, and on a
    phone the panels are what separate one kind of content from another.
    """

    def __init__(self, level: str = "surface_container",
                 radius: float | None = None,
                 padding: tuple[float, float, float, float] = (dp(16), dp(12),
                                                              dp(16), dp(12)),
                 **kwargs: Any) -> None:
        super().__init__(orientation="vertical", padding=padding, **kwargs)
        self.level = level
        self._radius = radius if radius is not None else dp(16)
        self.bind(pos=self._sync, size=self._sync)
        self._sync()

    def _sync(self, *_args: Any) -> None:
        # The colour of a shape is a separate instruction in Kivy, so the
        # card is painted again instead of being recoloured in place.
        self.canvas.before.clear()
        with self.canvas.before:
            Color(*_rgba(_color(self.level)))
            RoundedRectangle(pos=self.pos, size=self.size, radius=[self._radius])
            Color(*_rgba(_color("outline_variant")))
            Line(rounded_rectangle=(self.x, self.y, self.width, self.height,
                                    self._radius), width=dp(1))


class NavItem(ButtonBehavior, BoxLayout):
    """One entry of the bottom navigation.

    Material 3 puts a pill behind the active icon and keeps a label under
    every entry.  Without the pill the bar is a row of glyphs with no hint
    of where you are; without the labels it is a row of glyphs full stop.
    """

    def __init__(self, icon: str, label: str = "",
                 on_press: Callable[[], None] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(orientation="vertical", size_hint_x=1, spacing=dp(2),
                         padding=(0, dp(4), 0, dp(2)), **kwargs)
        self.icon_name = icon
        self.active = False
        self._on_press = on_press

        self.icon = IconButton(icon=icon, size=dp(40),
                               color_name="on_surface_variant")
        self.add_widget(self.icon)

        self.label = ThemedLabel(text=label, role="label_small",
                                 color_name="on_surface_variant", halign="center")
        self.label.size_hint_y = None
        _fit_height(self.label)
        self.add_widget(self.label)

        self.icon.bind(pos=self._sync, size=self._sync)
        self.bind(pos=self._sync, size=self._sync)
        self._sync()

    def set_active(self, active: bool) -> None:
        """Move the indicator and recolour the entry."""
        self.active = active
        self.icon.set_color_name("on_primary_container" if active
                                 else "on_surface_variant")
        self.label.set_color_name("on_surface" if active else "on_surface_variant")
        self._sync()

    def _sync(self, *_args: Any) -> None:
        self.canvas.before.clear()
        if not self.active:
            return
        width, height = dp(64), dp(32)
        with self.canvas.before:
            Color(*_rgba(_color("secondary_container")))
            RoundedRectangle(pos=(self.icon.center_x - width / 2,
                                  self.icon.center_y - height / 2),
                             size=(width, height), radius=[dp(16)])

    def on_release(self, *_args: Any) -> None:
        if self._on_press is not None:
            self._on_press()


class EmptyState(BoxLayout):
    """The "nothing here yet" placeholder.

    An icon on a tinted disc, a title, a line of explanation and one button.
    A single grey line in the middle of an empty page is what an unfinished
    screen looks like.
    """

    def __init__(self, message: str = "", action_text: str = "",
                 on_action: Callable[[], None] | None = None,
                 title: str = "", icon: str = "music", **kwargs: Any) -> None:
        super().__init__(orientation="vertical", padding=dp(24),
                         spacing=dp(10), **kwargs)
        # Size to the content.  With the default size_hint the block takes a
        # share of whatever the parent hands out, and next to widgets with a
        # fixed height it gets squeezed until the message is a single line of
        # pixels.
        self.size_hint_y = None
        self.bind(minimum_height=self.setter("height"))

        self.disc = IconButton(icon=icon, size=dp(64), variant="tonal",
                               glyph_scale=0.42, color_name="on_secondary_container")
        self.add_widget(self.disc)

        if title:
            heading = ThemedLabel(text=title, role="title_medium",
                                  color_name="on_surface", halign="center")
            heading.size_hint_y = None
            _fit_height(heading)
            self.add_widget(heading)

        label = ThemedLabel(text=message, role="body_medium",
                             color_name="on_surface_variant", halign="center")
        label.size_hint_y = None
        # ``minimum_height`` on a Label is an AliasProperty: it has no event
        # to bind to, and binding it raises KeyError.  The texture height is
        # what the label actually needs.
        _fit_height(label)
        self.add_widget(label)

        if action_text and on_action is not None:
            button = AppButton(text=action_text, on_press=on_action)
            self.add_widget(button)


class AppButton(ButtonBehavior, Label):
    """A button with the Material 3 variants.

    ``filled`` is the loud one, ``tonal`` the quiet one, ``outlined`` and
    ``text`` the borderless pair.  The width is still guessed from the
    character count, but the height and the radius are fixed, so two labels
    of different length no longer come out in different shapes.
    """

    VARIANTS = ("filled", "tonal", "outlined", "text")

    def __init__(self, text: str = "", on_press: Any = None,
                 variant: str = "filled", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.text = text
        self.variant = variant if variant in self.VARIANTS else "filled"
        self.size_hint = (None, None)
        self.height = dp(40)
        self.width = max(dp(96), len(text) * dp(9) + dp(32))
        self.font_size = dp(14)
        self._pressed = False
        self.bind(on_release=lambda *_a: on_press() if on_press else None)
        self.bind(pos=self._sync, size=self._sync)
        self._sync()

    def on_touch_down(self, touch: Any) -> bool:
        if not self.collide_point(*touch.pos):
            return False
        self._pressed = True
        self._sync()
        return super().on_touch_down(touch)

    def on_touch_up(self, touch: Any) -> None:
        self._pressed = False
        self._sync()
        super().on_touch_up(touch)

    def _sync(self, *_args: Any) -> None:
        self.canvas.before.clear()
        if self.variant == "filled":
            fill, text, overlay, edge = "primary", "on_primary", "on_primary", None
        elif self.variant == "tonal":
            fill, text, overlay, edge = ("secondary_container",
                                         "on_secondary_container",
                                         "on_secondary_container", None)
        elif self.variant == "outlined":
            fill, text, overlay, edge = None, "primary", "on_surface", "outline"
        else:
            fill, text, overlay, edge = None, "primary", "on_surface", None
        with self.canvas.before:
            if fill:
                Color(*_rgba(_color(fill)))
                RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(20)])
            if edge:
                Color(*_rgba(_color(edge)))
                Line(rounded_rectangle=(self.x, self.y, self.width, self.height,
                                        dp(20)), width=dp(1))
            if self._pressed:
                Color(*_rgba(_color(overlay), 0.12))
                RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(20)])
        self.color = _color(text)


#: The old name, kept because a couple of screens import it.
_TextButton = AppButton

#: Public aliases: the screens paint with the same helpers.
rgba = _rgba
theme_color = _color


class SectionHeader(BoxLayout):
    """A title above a group of content."""

    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(orientation="horizontal", size_hint_y=None, height=dp(40),
                         **kwargs)
        self.add_widget(ThemedLabel(text=text, role="title_large",
                                    color_name="on_surface"))


class LoadingView(BoxLayout):
    """A spinner with a label, shown while a screen waits for data."""

    def __init__(self, message: str = "", **kwargs: Any) -> None:
        super().__init__(orientation="vertical", padding=dp(24),
                         spacing=dp(12), **kwargs)
        from kivy.uix.spinner import Spinner  # module import list short

        self._spinner = Spinner(size=(dp(40), dp(40)))
        self.add_widget(self._spinner)
        if message:
            self.add_widget(ThemedLabel(text=message, role="body_medium",
                                         color_name="on_surface_variant"))
