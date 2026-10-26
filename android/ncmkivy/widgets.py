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

from collections.abc import Callable
from typing import Any

from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import AsyncImage
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.widget import Widget

__all__ = [
    "ThemedLabel",
    "IconButton",
    "CoverImage",
    "TrackRow",
    "TrackList",
    "EmptyState",
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
        """Re-apply the colour: the theme may have changed."""
        self.color = _color(self._color_name)

    def set_role(self, role: str) -> None:
        theme = _theme()
        if theme is not None and hasattr(theme, role):
            self.font_size = getattr(theme, role)()
            self._role = role


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
                 color_name: str = "on_surface", **kwargs: Any) -> None:
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
        self._draw()

    def _draw(self) -> None:
        self.canvas.clear()
        with self.canvas:
            Color(1, 1, 1, 0)
            self._glyph_color = _color(self._color_name)
            Color(*_rgba(self._glyph_color))
            self._paint(self._glyph_color)

    def _paint(self, rgb: tuple[float, float, float]) -> None:
        """Draw the glyph.

        Everything is expressed as a fraction of the button size so the icon
        scales with the touch target.
        """
        size = min(self.width, self.height)
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
            from kivy.graphics import Ellipse, Line, Rectangle
            Color(*rgb)
            r = size * 0.14
            Ellipse(pos=(cx - r * 1.2 - r, cy - r * 1.2 + r * 0.4),
                    size=(r * 2, r * 2))
            Line(points=[cx - r * 0.4, cy + r * 0.1,
                         cx - r * 0.1, cy - r * 0.2], width=1.6)
            Rectangle(pos=(cx - size * 0.3, cy + size * 0.22),
                      size=(size * 0.6, size * 0.04))
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
        return super().on_touch_down(touch)

    def on_touch_up(self, touch: Any) -> None:
        self._pressed = False
        super().on_touch_up(touch)

    def on_size(self, *_args: Any) -> None:
        self._draw()


def _rgba(color: str) -> tuple[float, float, float, float]:
    """``"#RRGGBB"`` -> an RGBA tuple."""
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        red = int(value[0:2], 16) / 255.0
        green = int(value[2:4], 16) / 255.0
        blue = int(value[4:6], 16) / 255.0
    except (ValueError, IndexError):
        return 1.0, 1.0, 1.0, 1.0
    return red, green, blue, 1.0


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

    def _sync(self, *_args: Any) -> None:
        for item in self.canvas.before.children:
            if isinstance(item, RoundedRectangle):
                item.pos = self.pos
                item.size = self.size


class TrackRow(BoxLayout):
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


class EmptyState(BoxLayout):
    """The "nothing here yet" placeholder."""

    def __init__(self, message: str = "", action_text: str = "",
                 on_action: Callable[[], None] | None = None, **kwargs: Any) -> None:
        super().__init__(orientation="vertical", padding=dp(24),
                         spacing=dp(12), **kwargs)
        label = ThemedLabel(text=message, role="body_large",
                             color_name="on_surface_variant", halign="center")
        label.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        self.add_widget(label)
        if action_text and on_action is not None:
            button = _TextButton(text=action_text, on_press=on_action)
            self.add_widget(button)


class _TextButton(ButtonBehavior, Label):
    """A filled button."""

    def __init__(self, text: str = "", on_press: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.text = text
        self.size_hint = (None, None)
        self.height = dp(40)
        self.width = max(dp(96), len(text) * dp(9) + dp(32))
        self.font_size = dp(14)
        self.color = _color("on_primary")
        self.bind(on_release=lambda *_a: on_press() if on_press else None)
        with self.canvas.before:
            Color(*_rgba(_color("primary")))
            self._rect = RoundedRectangle(pos=self.pos, size=self.size,
                                          radius=[dp(20)])
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_args: Any) -> None:
        self._rect.pos = self.pos
        self._rect.size = self.size


class SectionHeader(BoxLayout):
    """A title above a group of content."""

    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(orientation="horizontal", size_hint_y=None, height=dp(32),
                         **kwargs)
        self.add_widget(ThemedLabel(text=text, role="title_medium",
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
