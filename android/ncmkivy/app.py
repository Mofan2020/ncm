"""The Kivy application: window, theme, navigation, and the screen stack.

The window reacts to its size.  A phone, a tablet and a phone in landscape
are three different layouts, and the app has to switch between them while
it runs -- the user can rotate the device or fold the window open.

Three navigation shapes:

``compact``
    one pane at a time, a bottom navigation bar for the top-level sections
    and a bottom sheet for the player
``medium``
    the same bottom bar, but the list and the detail sit side by side
``expanded``
    a side rail for navigation, a list pane and a detail pane
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kivy.app import App
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.widget import Widget
from kivy.utils import get_color_from_hex

from ncmkivy.controller import Controller
from ncmkivy.player import Player
from ncmkivy.theme import Theme

log = logging.getLogger("ncm.kivy.app")

__all__ = ["NcmApp"]


def _install_cjk_font() -> None:
    """Point Kivy's default font at one that actually has Chinese glyphs.

    Kivy ships Roboto, which covers Latin only: every Chinese label comes out
    as a row of empty boxes.  Android itself carries a full CJK font, so
    registering the system one costs nothing in APK size.
    """
    from kivy.core.text import LabelBase

    for path in (
        "/system/fonts/NotoSansCJK-Regular.ttc",
        "/system/fonts/NotoSansCJKsc-Regular.otf",
        "/system/fonts/NotoSansSC-Regular.otf",
        "/system/fonts/NotoSansSC-Regular.ttf",
        "/system/fonts/DroidSansFallback.ttf",
    ):
        try:
            if Path(path).is_file():
                LabelBase.register(name="Roboto", fn_regular=path)
                log.info("font: using %s", path)
                return
        except OSError:
            continue
    log.warning("no CJK font on this device; labels will render as boxes")


class NcmApp(App):
    """The Android application."""

    title = "NetEase Music"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.controller = Controller()
        self.player = Player()
        self.theme = Theme.for_system(dark=False)
        self.root_widget: Widget | None = None
        self._nav_rail: BoxLayout | None = None
        self._bottom_nav: BoxLayout | None = None
        self._content: BoxLayout | None = None
        self._detail: BoxLayout | None = None
        self._current_section = "home"
        #: The queue the player is working through.
        self.current_tracks: list[dict[str, Any]] = []
        self.current_index = 0
        self._qr_popup: Any = None

    # ------------------------------------------------------------- lifecycle
    def build(self) -> Widget:
        _install_cjk_font()
        self.theme.apply(self)
        # Kivy clears the window to opaque black, and widgets here only carry
        # their *foreground* colours -- nothing paints a background.  With the
        # light palette that means dark text on black, which looks like the app
        # never drew anything at all.  Paint the theme background first.
        Window.clearcolor = get_color_from_hex(self.theme.color("background"))
        root = BoxLayout(orientation="vertical")
        self.root_widget = root

        # The body: navigation + content.
        body = BoxLayout(orientation="horizontal")
        root.add_widget(body)

        self._nav_rail = self._build_nav_rail()
        body.add_widget(self._nav_rail)

        self._content = BoxLayout(orientation="vertical")
        body.add_widget(self._content)

        # The bottom bar (compact / medium).
        self._bottom_nav = self._build_bottom_nav()
        root.add_widget(self._bottom_nav)

        self._apply_size_class()
        Window.bind(size=self._on_window_resize)
        return root

    def on_start(self) -> None:
        self.controller.start()
        self.player.on_completed = self._on_track_finished
        self.player.on_error = self._on_play_error
        # Nothing else ever calls go() at startup, so without this the content
        # area stays empty and the app looks like a blank page.
        self.go(self._current_section)

    def on_stop(self) -> None:
        self.controller.stop()

    def on_pause(self) -> None:
        """The app went to the background; keep the music playing."""
        return None

    def on_resume(self) -> None:
        return None

    # ----------------------------------------------------------------- theme
    def apply_theme(self, dark: bool) -> None:
        """Switch between the light and the dark palette."""
        self.theme.dark = dark
        self.theme.colors = dict(
            self.theme.__class__ and __import__(
                "ncmkivy.theme", fromlist=["PALETTES"]).PALETTES[
                    "dark" if dark else "light"].as_dict())
        if self.root_widget is not None:
            self.theme.apply(self.root_widget)
        self._refresh_widgets()

    def _refresh_widgets(self) -> None:
        """Tell the children to redraw with the new colours."""
        if self.root_widget is None:
            return
        for child in self.root_widget.walk(restrict=True):
            if hasattr(child, "_refresh"):
                try:
                    child._refresh()
                except Exception:  # pragma: no cover - defensive
                    pass

    # ----------------------------------------------------------------- size
    def _on_window_resize(self, _window: Any, _size: tuple[int, int]) -> None:
        if self._apply_size_class():
            self._refresh_widgets()

    def _apply_size_class(self, width: float | None = None,
                          density: float | None = None) -> bool:
        """Recompute the layout bucket and apply it.  True when it changed.

        ``width`` / ``density`` exist so the layout can be driven from a
        test with known values; the running app passes nothing and asks the
        window and the metrics.

        The density has to come from ``kivy.metrics.Metrics``: ``Window``
        only carries a private ``_density``, so reading it off the window
        silently yields 1.0 and a 3x phone (1080 px wide) would be judged
        as a 1080 dp tablet and get the two-column layout.
        """
        from kivy.metrics import Metrics

        if width is None:
            width = Window.width
        if density is None:
            density = getattr(Metrics, "density", 1.0) or 1.0
        self.theme.update_size(width, density)
        size_class = self.theme.size_class
        # The bars have to be sized on the first call too.  Returning early
        # when the bucket happens to match the default leaves both navigation
        # bars at their constructor size, which is zero.
        if self._nav_rail is not None:
            self._nav_rail.width = dp(80) if size_class.name == "expanded" else 0
            self._nav_rail.opacity = 1 if size_class.name == "expanded" else 0
            self._nav_rail.disabled = size_class.name != "expanded"
        if self._bottom_nav is not None:
            self._bottom_nav.height = 0 if size_class.name == "expanded" else dp(64)
            self._bottom_nav.opacity = 0 if size_class.name == "expanded" else 1
            self._bottom_nav.disabled = size_class.name == "expanded"
        return True

    # ----------------------------------------------------------- navigation
    #: The top-level sections, in the order they appear.
    SECTIONS = (
        ("home", "home", "player.home"),
        ("search", "search", "player.search"),
        ("library", "stack", "player.library"),
        ("downloads", "arrow_down", "player.source_local"),
        ("me", "person", "player.login_required"),
    )

    def _build_nav_rail(self) -> BoxLayout:
        """The side rail used on wide screens."""
        rail = BoxLayout(orientation="vertical", size_hint=(None, 1),
                         width=dp(80), spacing=dp(4), padding=(dp(8), dp(16)))
        from ncmkivy.widgets import IconButton

        for key, icon, _label in self.SECTIONS:
            button = IconButton(icon=icon)
            button.bind(on_release=lambda *_a, k=key: self.go(k))
            rail.add_widget(button)
        return rail

    def _build_bottom_nav(self) -> BoxLayout:
        """The bottom bar used on phones."""
        bar = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=dp(64), spacing=dp(4), padding=(dp(8), dp(4)))
        from ncmkivy.widgets import IconButton

        for key, icon, _label in self.SECTIONS:
            button = IconButton(icon=icon)
            # IconButton is fixed-size by default, which leaves five buttons
            # bunched against the left edge.  A bottom bar wants them spread
            # evenly across the width.
            button.size_hint_x = 1
            button.bind(on_release=lambda *_a, k=key: self.go(k))
            bar.add_widget(button)
        return bar

    def go(self, section: str) -> None:
        """Switch to a top-level section."""
        self._current_section = section
        if self._content is None:
            return
        from ncmkivy.screens import build_screen

        self._content.clear_widgets()
        screen = build_screen(self, section)
        if screen is not None:
            self._content.add_widget(screen)

    # ------------------------------------------------------------ sub-pages
    def open_artist(self, artist_id: str) -> None:
        """Open the artist page."""
        if self._content is None or not artist_id:
            return
        from ncmkivy.screens import ArtistScreen

        self._content.clear_widgets()
        self._content.add_widget(ArtistScreen(self, artist_id=str(artist_id)))

    def open_playlist(self, playlist_id: str) -> None:
        """Open one playlist."""
        if self._content is None or not playlist_id:
            return
        from ncmkivy.screens import PlaylistScreen

        self._content.clear_widgets()
        self._content.add_widget(PlaylistScreen(self, playlist_id=str(playlist_id)))

    def open_followed_artists(self) -> None:
        """Open the list of followed artists."""
        self.go("followed")

    def start_qr_login(self) -> None:
        """Start the QR login flow and show the code."""
        from kivy.uix.popup import Popup

        from ncmkivy.uix import QrCodeView

        result = self.controller.start_qr_login()
        if not result.get("success"):
            return
        url = result.get("qrcode_url") or ""
        content = QrCodeView(url)
        popup = Popup(title="扫码登录", content=content, size_hint=(None, None),
                      size=(dp(320), dp(380)), auto_dismiss=False)
        popup.open()
        self._qr_popup = popup

    def start_phone_login(self) -> None:
        """Open the phone-number login dialog."""
        from ncmkivy.uix import PhoneLoginDialog

        PhoneLoginDialog(self).open()

    # --------------------------------------------------------------- player
    def play_tracks(self, tracks: list[dict[str, Any]], index: int = 0) -> None:
        """Play a list of tracks, starting at ``index``.

        The URL comes from the shared media server, so the stream is proxied
        with the headers NetEase asks for -- the same path the desktop
        build uses.
        """
        if not tracks:
            return
        track = tracks[index]
        resolved = self.controller.resolve(track)
        if not resolved.get("success"):
            log.warning("cannot resolve track: %s", resolved.get("message"))
            return
        self.current_tracks = list(tracks)
        self.current_index = index
        self.player.play(resolved.get("url") or "")

    def _on_track_finished(self) -> None:
        """Move on to the next track when one ends."""
        if not self.current_tracks:
            return
        next_index = self.current_index + 1
        if next_index >= len(self.current_tracks):
            return
        self.play_tracks(self.current_tracks, next_index)

    def _on_play_error(self, message: str) -> None:
        log.warning("playback error: %s", message)
