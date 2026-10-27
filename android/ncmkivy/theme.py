"""Material 3 theme tokens and the responsive size buckets.

Two jobs live here:

* the **palette** -- a Material 3 colour set for light and dark, applied to
  the Kivy widget tree through :meth:`Theme.apply`;
* the **size buckets** -- ``compact`` / ``medium`` / ``expanded`` decided by the
  *available width*, not by the device model, so a phone in landscape and a
  small tablet end up in the same bucket when they have the same room.

Kivy has no density-independent pixels of its own, so every number here goes
through :func:`kivy.metrics.dp`; fonts go through ``sp`` so the system font
scale is respected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kivy.metrics import dp, sp

__all__ = ["Theme", "SizeClass", "PALETTES", "TOUCH_TARGET_DP"]


#: Material 3 says a touch target is at least 48dp on its shortest side.
TOUCH_TARGET_DP = 48


@dataclass(frozen=True)
class Palette:
    """One Material 3 colour set."""

    primary: str
    on_primary: str
    primary_container: str
    on_primary_container: str
    secondary: str
    on_secondary: str
    secondary_container: str
    on_secondary_container: str
    background: str
    on_background: str
    surface: str
    on_surface: str
    surface_variant: str
    on_surface_variant: str
    surface_container: str
    surface_container_high: str
    outline: str
    outline_variant: str
    error: str
    on_error: str
    scrim: str
    # The surface levels below and above the container surface.  A card
    # needs a level to sit on: without them every panel is the same flat
    # colour and the page looks like a wireframe.
    surface_container_lowest: str = "#FFFFFF"
    surface_container_low: str = "#F7F2FA"
    surface_container_highest: str = "#E6E0E9"
    inverse_surface: str = "#313033"
    inverse_on_surface: str = "#F4EFF4"
    inverse_primary: str = "#D0BCFF"
    # The desktop brand red.  Only for large shapes, selection glows and
    # cover placeholders -- white text on it is 3.89:1, not enough for body copy.
    brand: str = "#EC4141"
    # The glass layer.  A floating panel is a translucent surface with a
    # bright hairline edge, not an opaque card -- that is what gives the
    # desktop its liquid look, and the phone wants the same trick.
    glass_surface: str = "#FFFFFFB8"
    glass_border: str = "#FFFFFFA0"
    glass_highlight: str = "#FFFFFFFF"

    def as_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


#: Material 3 baseline schemes.  The dark set is the "baseline dark" tone;
#: the light set is the baseline light tone.
#: The two schemes, tuned to the brand.  Values and contrast ratios come
#: from android/UI_SPEC.md; the desktop brand red #EC4141 is kept as
#: ``brand`` for large shapes only -- white text on it is 3.89:1.
PALETTES: dict[str, Palette] = {
    "light": Palette(
        primary="#C4302B",
        on_primary="#FFFFFF",
        primary_container="#FFDAD6",
        on_primary_container="#410E0B",
        secondary="#775651",
        on_secondary="#FFFFFF",
        secondary_container="#F5DDD9",
        on_secondary_container="#2C1512",
        background="#FFF8F7",
        on_background="#201A19",
        surface="#FFF8F7",
        on_surface="#201A19",
        surface_variant="#F5DED9",
        on_surface_variant="#534341",
        surface_container="#FCEAE7",
        surface_container_high="#F7E4E1",
        outline="#857370",
        outline_variant="#D8C2BE",
        error="#8C1D18",
        on_error="#FFFFFF",
        scrim="#000000",
        surface_container_lowest="#FFFFFF",
        surface_container_low="#FFF1EF",
        surface_container_highest="#F1DFDC",
        inverse_surface="#362F2E",
        inverse_on_surface="#FBEEEB",
        inverse_primary="#FFB4AB",
        glass_surface="#FFFFFFB8",
        glass_border="#FFFFFFA0",
        glass_highlight="#FFFFFFFF",
    ),
    "dark": Palette(
        primary="#FFB4AB",
        on_primary="#690005",
        primary_container="#93000A",
        on_primary_container="#FFDAD6",
        secondary="#E7BDB7",
        on_secondary="#442925",
        secondary_container="#4E3A37",
        on_secondary_container="#FFDAD5",
        background="#181211",
        on_background="#EDE0DE",
        surface="#181211",
        on_surface="#EDE0DE",
        surface_variant="#534341",
        on_surface_variant="#D8C2BE",
        surface_container="#251E1D",
        surface_container_high="#302826",
        outline="#A08C89",
        outline_variant="#534341",
        error="#FFB4AB",
        on_error="#690005",
        scrim="#000000",
        surface_container_lowest="#0E0908",
        surface_container_low="#201A19",
        surface_container_highest="#3B3331",
        inverse_surface="#EDE0DE",
        inverse_on_surface="#362F2E",
        inverse_primary="#C4302B",
        glass_surface="#221C1BBD",
        glass_border="#FFFFFF24",
        glass_highlight="#FFFFFF29",
    ),
}


@dataclass(frozen=True)
class SizeClass:
    """The layout bucket the current window falls into."""

    name: str
    #: Number of list/detail columns: 1 = single pane, 2 = list + detail.
    columns: int
    #: Whether the navigation is a bottom bar (phones) or a side rail (tablets).
    bottom_nav: bool
    #: Whether the player lives in a bottom sheet or in the side pane.
    player_in_sheet: bool

    @property
    def is_expanded(self) -> bool:
        return self.name == "expanded"


#: Width breakpoints in dp.  Material 3's window size classes.
_BREAKPOINT_MEDIUM = 600
_BREAKPOINT_EXPANDED = 840


def classify(width_px: float, density: float = 1.0) -> SizeClass:
    """Return the layout bucket for a window width given in pixels.

    ``density`` is the display scale; a phone at 3x reports three times as
    many pixels for the same physical width, so the comparison has to happen
    in dp.
    """
    width_dp = (width_px / density) if density > 0 else width_px
    if width_dp >= _BREAKPOINT_EXPANDED:
        return SizeClass("expanded", columns=2, bottom_nav=False, player_in_sheet=False)
    if width_dp >= _BREAKPOINT_MEDIUM:
        return SizeClass("medium", columns=2, bottom_nav=True, player_in_sheet=False)
    return SizeClass("compact", columns=1, bottom_nav=True, player_in_sheet=True)


@dataclass
class Theme:
    """The active palette plus the spacing and type scale."""

    dark: bool = False
    size_class: SizeClass = field(default_factory=lambda: classify(dp(400)))
    colors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.colors:
            self.colors = dict(PALETTES["dark" if self.dark else "light"].as_dict())

    # ------------------------------------------------------------------ setup
    @classmethod
    def for_system(cls, dark: bool) -> Theme:
        return cls(dark=dark)

    def apply(self, widget) -> None:
        """Push the palette onto a widget tree.

        Kivy widgets do not inherit CSS, so the colours have to be written
        onto each widget.  Widgets that read ``self.theme_colors`` pick them
        up from here.
        """
        widget.theme_colors = dict(self.colors)

    def update_size(self, width_px: float, density: float = 1.0) -> bool:
        """Recompute the size bucket.  Returns True when it changed."""
        new_class = classify(width_px, density)
        if new_class.name == self.size_class.name:
            return False
        self.size_class = new_class
        return True

    # ------------------------------------------------------------- accessors
    def color(self, name: str) -> str:
        return self.colors.get(name, "#FF0000")

    # ------------------------------------------------------------ type scale
    @staticmethod
    def display_large() -> float:
        return sp(52)

    @staticmethod
    def headline_medium() -> float:
        return sp(26)

    @staticmethod
    def title_large() -> float:
        return sp(20)

    @staticmethod
    def title_medium() -> float:
        return sp(16)

    @staticmethod
    def body_large() -> float:
        return sp(16)

    @staticmethod
    def body_medium() -> float:
        return sp(14)

    @staticmethod
    def body_small() -> float:
        return sp(12)

    @staticmethod
    def label_small() -> float:
        return sp(11)

    # --------------------------------------------------------------- spacing
    @staticmethod
    def space_xs() -> float:
        return dp(4)

    @staticmethod
    def space_s() -> float:
        return dp(8)

    @staticmethod
    def space_m() -> float:
        return dp(16)

    @staticmethod
    def space_l() -> float:
        return dp(24)

    @staticmethod
    def space_xl() -> float:
        return dp(32)

    @staticmethod
    def touch_target() -> float:
        return dp(TOUCH_TARGET_DP)

    @staticmethod
    def radius_s() -> float:
        return dp(8)

    @staticmethod
    def radius_m() -> float:
        return dp(12)

    @staticmethod
    def radius_l() -> float:
        return dp(16)
