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
PALETTES: dict[str, Palette] = {
    "light": Palette(
        primary="#6750A4",
        on_primary="#FFFFFF",
        primary_container="#EADDFF",
        on_primary_container="#21005D",
        secondary="#625B71",
        on_secondary="#FFFFFF",
        secondary_container="#E8DEF8",
        on_secondary_container="#1D192B",
        background="#FFFBFE",
        on_background="#1C1B1F",
        surface="#FFFBFE",
        on_surface="#1C1B1F",
        surface_variant="#E7E0EC",
        on_surface_variant="#49454F",
        surface_container="#F3EDF7",
        surface_container_high="#ECE6F0",
        outline="#79747E",
        outline_variant="#CAC4D0",
        error="#B3261E",
        on_error="#FFFFFF",
        scrim="#000000",
        surface_container_lowest="#FFFFFF",
        surface_container_low="#F7F2FA",
        surface_container_highest="#E6E0E9",
        inverse_surface="#313033",
        inverse_on_surface="#F4EFF4",
        inverse_primary="#D0BCFF",
        glass_surface="#FFFFFFBF",
        glass_border="#FFFFFFA6",
        glass_highlight="#FFFFFFFF",
    ),
    "dark": Palette(
        primary="#D0BCFF",
        on_primary="#381E72",
        primary_container="#4F378B",
        on_primary_container="#EADDFF",
        secondary="#CCC2DC",
        on_secondary="#332D41",
        secondary_container="#4A4458",
        on_secondary_container="#E8DEF8",
        background="#1C1B1F",
        on_background="#E6E1E5",
        surface="#1C1B1F",
        on_surface="#E6E1E5",
        surface_variant="#49454F",
        on_surface_variant="#CAC4D0",
        surface_container="#211F26",
        surface_container_high="#2B2930",
        outline="#938F99",
        outline_variant="#49454F",
        error="#F2B8B5",
        on_error="#601410",
        scrim="#000000",
        surface_container_lowest="#0F0E13",
        surface_container_low="#1D1B20",
        surface_container_highest="#36343B",
        inverse_surface="#E6E1E5",
        inverse_on_surface="#313033",
        inverse_primary="#6750A4",
        glass_surface="#2B2930D9",
        glass_border="#FFFFFF24",
        glass_highlight="#FFFFFF14",
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
