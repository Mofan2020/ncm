"""The Android screens.

Every screen is a plain widget that the app swaps into the content area.
The screens share one rule: **nothing overflows**.  A screen that has to
show more than fits uses a scroll view; a list uses a recycle view; text is
shortened, never squeezed.

The screen factory :func:`build_screen` maps a section key to a screen, so
the app only has to know about sections, not about widgets.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from kivy.graphics import Color, Line
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

from ncmkivy.widgets import (
    CoverImage,
    EmptyState,
    IconButton,
    LoadingView,
    SectionHeader,
    SurfaceCard,
    TappableRow,
    ThemedLabel,
    TrackList,
    TrackRow,
    rgba,
    theme_color,
)

__all__ = ["build_screen", "HomeScreen", "SearchScreen", "LibraryScreen",
           "DownloadsScreen", "MeScreen", "ArtistScreen", "PlaylistScreen",
           "FollowedArtistsScreen"]


def _t(app: Any, key: str, fallback: str = "") -> str:
    """Translate a key through the shared i18n catalogue."""
    try:
        from src.i18n import t as translate

        text = translate(key)
        if text and text != key:
            return text
    except Exception:  # pragma: no cover - i18n is optional at runtime
        pass
    return fallback or key


class _Screen(BoxLayout):
    """Common bits every screen needs: a title bar and a body."""

    def __init__(self, app: Any, title: str = "", **kwargs: Any) -> None:
        super().__init__(orientation="vertical", **kwargs)
        self.app = app
        self.controller = app.controller

        self.header = BoxLayout(orientation="horizontal", size_hint_y=None,
                                height=dp(64), spacing=dp(8),
                                padding=(dp(16), dp(8)))
        self.title_label = ThemedLabel(text=title, role="headline_medium",
                                       color_name="on_surface")
        self.header.add_widget(self.title_label)
        self.add_widget(self.header)
        # A hairline under the title bar.  It separates the chrome from the
        # content without the weight of a border.
        self.header.bind(pos=self._sync_header, size=self._sync_header)
        self._sync_header()

        self.body = BoxLayout(orientation="vertical")
        self.add_widget(self.body)

    def _sync_header(self, *_args: Any) -> None:
        self.header.canvas.before.clear()
        with self.header.canvas.before:
            Color(*rgba(theme_color("outline_variant")))
            Line(points=[self.header.x, self.header.y,
                         self.header.x + self.header.width, self.header.y],
                 width=dp(1))

    def set_title(self, title: str) -> None:
        self.title_label.text = title

    def show_loading(self, message: str = "") -> None:
        self.body.clear_widgets()
        self.body.add_widget(LoadingView(message=message))

    def show_empty(self, message: str, action_text: str = "",
                   on_action: Callable[[], None] | None = None,
                   title: str = "", icon: str = "music") -> None:
        self.body.clear_widgets()
        self.body.add_widget(EmptyState(message=message, action_text=action_text,
                                        on_action=on_action, title=title,
                                        icon=icon))

    def show_error(self, message: str) -> None:
        self.show_empty(message)


class HomeScreen(_Screen):
    """The landing screen: the sources, plus what is playing."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        # ``self.app`` must exist before the title is looked up.
        self.app = app
        super().__init__(app, title=_t(self.app, "player.library", "音乐库"), **kwargs)
        self._build()

    def _build(self) -> None:
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(2))
        column = BoxLayout(orientation="vertical", size_hint_y=None,
                           spacing=dp(8), padding=(dp(16), dp(8)))
        column.bind(minimum_height=column.setter("height"))
        scroll.add_widget(column)

        status = self.controller.login_status()
        if not status.get("logged_in"):
            column.add_widget(EmptyState(
                title=_t(self.app, "app.title", "网易云音乐歌单下载器"),
                message=_t(self.app, "player.login_required", "请先登录账号"),
                action_text=_t(self.app, "login.title", "登录"),
                on_action=self._open_login,
                icon="person",
            ))

        column.add_widget(SectionHeader(text=_t(self.app, "player.online_sources", "在线来源")))
        for label, action in (
            (_t(self.app, "player.source_playlists", "我的歌单"), self._open_playlists),
            (_t(self.app, "player.source_liked", "我喜欢的"), self._open_liked),
            (_t(self.app, "player.source_recent", "最近播放"), self._open_recent),
            (_t(self.app, "android.followed_artists", "已关注歌手"), self._open_followed),
            (_t(self.app, "player.source_local", "本地音乐"), self._open_local),
        ):
            column.add_widget(self._entry(label, action))

        column.add_widget(SectionHeader(text=_t(self.app, "player.local_sources", "本地来源")))
        column.add_widget(self._entry(
            _t(self.app, "player.source_local_playlists", "本地歌单"), self._open_local))

        self.body.add_widget(scroll)

    def _entry(self, label: str, action: Callable[[], None]) -> BoxLayout:
        """One row of the source list: a tinted disc, the name, a chevron."""
        card = SurfaceCard(level="surface_container_low", radius=dp(18),
                           padding=(dp(10), dp(2), dp(16), dp(2)))
        row = TappableRow(orientation="horizontal", size_hint_y=None, height=dp(56),
                           spacing=dp(12))
        icon = IconButton(icon="music", size=dp(44), variant="tonal",
                          glyph_scale=1.2, color_name="on_secondary_container")
        # The disc is decoration: it must not swallow the tap meant for the
        # row.
        icon.disabled = True
        row.add_widget(icon)
        row.add_widget(ThemedLabel(text=label, role="title_medium"))
        row.add_widget(ThemedLabel(text="›", role="title_large",
                                   color_name="on_surface_variant"))
        row.bind(on_release=lambda *_a: action())
        card.add_widget(row)
        return card

    def _open_login(self) -> None:
        self.app.go("me")

    def _open_playlists(self) -> None:
        self.app.go("library")

    def _open_liked(self) -> None:
        self.app.go("library")

    def _open_recent(self) -> None:
        self.app.go("library")

    def _open_followed(self) -> None:
        self.app.go("followed")

    def _open_local(self) -> None:
        self.app.go("library")


class SearchScreen(_Screen):
    """Search across songs, playlists, artists and albums."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        # ``self.app`` must exist before the title is looked up.
        self.app = app
        super().__init__(app, title=_t(self.app, "player.search", "搜索"), **kwargs)
        self._build()

    def _build(self) -> None:
        self.input = TextInput(
            hint_text=_t(self.app, "player.search_placeholder", "搜索歌曲、歌手或专辑"),
            multiline=False, size_hint_y=None, height=dp(48),
            padding=(dp(12), dp(12)),
        )
        self.input.bind(on_text_validate=self._run)
        self.body.add_widget(self.input)

        self.results = BoxLayout(orientation="vertical", size_hint_y=None,
                                 spacing=dp(4), padding=(0, dp(4), 0, dp(4)))
        self.results.bind(minimum_height=self.results.setter("height"))
        # The results live in a scroll view.  A plain box grows with its
        # content and spills over the header as soon as there are more rows
        # than the screen can hold.
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(2))
        scroll.add_widget(self.results)
        self.body.add_widget(scroll)
        self._show_hint(_t(self.app, "player.search_hint", "输入关键词后按回车开始搜索"))

    def _show_hint(self, message: str) -> None:
        """Put a message in the results area.

        ``show_empty`` clears the whole body, which also holds the search
        box -- calling it from here would delete the input the user is about
        to type into.
        """
        self.results.clear_widgets()
        self.results.add_widget(EmptyState(message=message))

    def _run(self, *_args: Any) -> None:
        keyword = self.input.text.strip()
        if not keyword:
            return
        # Close the keyboard: it resizes the window, which slides the header
        # under the results and leaves the first row sitting on top of the
        # search box.
        self.input.focus = False
        self.results.clear_widgets()
        self.results.add_widget(LoadingView(message=_t(self.app, "player.search", "搜索")))
        threading.Thread(target=self._search, args=(keyword,), daemon=True).start()

    def _search(self, keyword: str) -> None:
        from kivy.clock import Clock

        try:
            payload = self.controller.search(keyword)
        except Exception as exc:  # pragma: no cover - network
            # ``exc`` is deleted when the except block ends, so the message
            # is bound to a plain name before the callback runs.
            message = str(exc)
            Clock.schedule_once(lambda *_a: self._show_hint(message), 0)
            return
        Clock.schedule_once(lambda *_a: self._render(payload), 0)

    def _render(self, payload: dict[str, Any]) -> None:
        self.results.clear_widgets()
        songs = payload.get("songs") or []
        artists = payload.get("artists") or []
        albums = payload.get("albums") or []

        if not songs and not artists and not albums:
            self._show_hint(_t(self.app, "player.search_empty", "没有搜索内容"))
            return

        if artists:
            self.results.add_widget(SectionHeader(
                text=_t(self.app, "player.search_multi_artists", "歌手")))
            for artist in artists:
                self.results.add_widget(self._artist_row(artist))

        if songs:
            self.results.add_widget(SectionHeader(
                text=_t(self.app, "player.search_multi_songs", "歌曲")))
            listing = TrackList(songs, on_play=self._play)
            # Inside the scroll column the list may be as tall as its
            # content; the scroll view around it does the scrolling.
            listing.size_hint_y = None
            listing.height = dp(64) * len(songs) + dp(8)
            self.results.add_widget(listing)

    def _artist_row(self, artist: dict[str, Any]) -> BoxLayout:
        row = TappableRow(orientation="horizontal", size_hint_y=None, height=dp(64),
                           spacing=dp(12))
        cover = CoverImage(size=dp(48))
        if artist.get("cover_url"):
            cover.source = artist["cover_url"]
        row.add_widget(cover)
        row.add_widget(ThemedLabel(text=artist.get("name") or "", role="title_medium"))
        # The id is bound now: a lambda would capture the loop variable and
        # every row would open the last artist.
        artist_id = str(artist.get("id") or "")
        row.bind(on_release=lambda *_a, aid=artist_id: self.app.open_artist(aid))
        return row

    def _play(self, track: dict[str, Any]) -> None:
        self.app.play_tracks([track], 0)


class LibraryScreen(_Screen):
    """The playlists, the liked songs and the local music."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        # ``self.app`` must exist before the title is looked up.
        self.app = app
        super().__init__(app, title=_t(self.app, "player.library", "音乐库"), **kwargs)
        self._build()

    def _build(self) -> None:
        self.tabs = BoxLayout(orientation="horizontal", size_hint_y=None,
                              height=dp(48), spacing=dp(4))
        self.body.add_widget(self.tabs)
        self.content = BoxLayout(orientation="vertical")
        self.body.add_widget(self.content)
        # Loading goes into the content area: show_loading() clears the whole
        # body, which would take the tab row down with it.
        self.content.add_widget(LoadingView())

        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        from kivy.clock import Clock

        try:
            playlists = self.controller.list_playlists()
        except Exception as exc:  # pragma: no cover - network
            # ``exc`` is deleted when the except block ends, so the message
            # is bound to a plain name before the callback runs.
            message = str(exc)
            Clock.schedule_once(lambda *_a: self.show_error(message), 0)
            return
        Clock.schedule_once(lambda *_a: self._render(playlists), 0)

    def _render(self, playlists: list[dict[str, Any]]) -> None:
        self.content.clear_widgets()
        if not playlists:
            self.content.add_widget(EmptyState(
                message=_t(self.app, "player.playlist_empty", "没有获取到歌单，请先登录账号")))
            return
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(2))
        column = BoxLayout(orientation="vertical", size_hint_y=None,
                           spacing=dp(4), padding=(dp(16), dp(8)))
        column.bind(minimum_height=column.setter("height"))
        scroll.add_widget(column)
        for playlist in playlists:
            column.add_widget(self._playlist_row(playlist))
        self.content.add_widget(scroll)

    def _playlist_row(self, playlist: dict[str, Any]) -> BoxLayout:
        row = TappableRow(orientation="horizontal", size_hint_y=None, height=dp(64),
                           spacing=dp(12))
        cover = CoverImage(size=dp(48))
        if playlist.get("cover_url"):
            cover.source = playlist["cover_url"]
        row.add_widget(cover)
        text_box = BoxLayout(orientation="vertical")
        text_box.add_widget(ThemedLabel(text=playlist.get("name") or "",
                                        role="title_medium"))
        text_box.add_widget(ThemedLabel(
            text=_t(self.app, "player.playlist_meta", "{count} 首 · {creator}").format(
                count=playlist.get("track_count", 0),
                creator=playlist.get("creator") or ""),
            role="body_small", color_name="on_surface_variant"))
        row.add_widget(text_box)
        playlist_id = playlist.get("id")
        row.bind(on_release=lambda *_a: self.app.open_playlist(playlist_id))
        return row


class DownloadsScreen(_Screen):
    """The download queue and its progress."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        # ``self.app`` must exist before the title is looked up.
        self.app = app
        super().__init__(app, title=_t(self.app, "player.source_local", "下载"), **kwargs)
        self._build()

    def _build(self) -> None:
        self.status_label = ThemedLabel(text="", role="body_medium",
                                        color_name="on_surface_variant")
        self.body.add_widget(self.status_label)
        self.rows = BoxLayout(orientation="vertical", size_hint_y=None,
                              spacing=dp(4))
        self.rows.bind(minimum_height=self.rows.setter("height"))
        # A plain box grows with its content and spills over the header once
        # the download list gets long.
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(2))
        scroll.add_widget(self.rows)
        self.body.add_widget(scroll)
        self._refresh()

    def _refresh(self, *_args: Any) -> None:
        from kivy.clock import Clock

        status = self.controller.download_status()
        self.status_label.text = (
            f"{status.get('success', 0)} / {status.get('total', 0)}"
        )
        if status.get("running"):
            Clock.schedule_once(lambda *_a: self._refresh(), 1.0)


class MeScreen(_Screen):
    """The account: sign in, the profile, sign out."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        # ``self.app`` must exist before the title is looked up.
        self.app = app
        super().__init__(app, title=_t(self.app, "login.title", "账号"), **kwargs)
        self._build()

    def _build(self) -> None:
        status = self.controller.login_status()
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(2))
        column = BoxLayout(orientation="vertical", size_hint_y=None,
                           spacing=dp(12), padding=(dp(16), dp(16)))
        column.bind(minimum_height=column.setter("height"))
        scroll.add_widget(column)

        if status.get("logged_in"):
            row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(64),
                            spacing=dp(12))
            avatar = CoverImage(size=dp(48))
            if status.get("avatar_url"):
                avatar.source = status["avatar_url"]
            row.add_widget(avatar)
            row.add_widget(ThemedLabel(text=status.get("nickname") or "",
                                        role="title_medium"))
            column.add_widget(row)
            column.add_widget(self._button(
                _t(self.app, "android.sign_out", "退出登录"), self._logout))
        else:
            column.add_widget(EmptyState(
                message=_t(self.app, "player.login_required", "请先登录账号")))
            column.add_widget(self._button(
                _t(self.app, "android.qr_login", "扫码登录"), self._qr_login))
            column.add_widget(self._button(
                _t(self.app, "android.phone_login", "手机号登录"), self._phone_login))

        self.body.add_widget(scroll)

    def _button(self, text: str, action: Callable[[], None]) -> Any:
        from ncmkivy.widgets import _TextButton

        return _TextButton(text=text, on_press=action)

    def _qr_login(self) -> None:
        self.app.start_qr_login()

    def _phone_login(self) -> None:
        self.app.start_phone_login()

    def _logout(self) -> None:
        self.controller.logout()


class ArtistScreen(_Screen):
    """The artist page: header, follow button, and all of their songs."""

    def __init__(self, app: Any, artist_id: str = "", **kwargs: Any) -> None:
        # ``self.app`` must exist before the title is looked up.
        self.app = app
        super().__init__(app, title=_t(self.app, "player.artist_detail", "歌手详情"),
                         **kwargs)
        self.artist_id = artist_id
        self._followed = False
        self._offset = 0
        self._build()

    def _build(self) -> None:
        self.header_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                    height=dp(180), spacing=dp(8),
                                    padding=(dp(16), dp(8)))
        self.body.add_widget(self.header_box)

        self.follow_button = None
        self.song_list = TrackList([], on_play=self._play)
        self.body.add_widget(self.song_list)
        # No show_loading() here: it clears the body, and the body already
        # holds the header and the song list this screen renders into.
        # Clearing would leave both detached from the window.

        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        from kivy.clock import Clock

        try:
            payload = self.controller.artist_page(self.artist_id)
        except Exception as exc:  # pragma: no cover - network
            # ``exc`` is deleted when the except block ends, so the message
            # is bound to a plain name before the callback runs.
            message = str(exc)
            Clock.schedule_once(lambda *_a: self.show_error(message), 0)
            return
        Clock.schedule_once(lambda *_a: self._render(payload), 0)

    def _render(self, payload: dict[str, Any]) -> None:
        if not payload.get("success"):
            self.show_empty(_t(self.app, "player.artist_not_found", "歌手不存在"))
            return
        artist = payload.get("artist") or {}
        self.set_title(artist.get("name") or "")
        self._followed = bool(artist.get("followed"))

        self.header_box.clear_widgets()
        top = BoxLayout(orientation="horizontal", spacing=dp(16),
                        size_hint_y=None, height=dp(88))
        cover = CoverImage(size=dp(80), radius=dp(40))
        if artist.get("cover_url"):
            cover.source = artist["cover_url"]
        top.add_widget(cover)

        info = BoxLayout(orientation="vertical", spacing=dp(4))
        info.add_widget(ThemedLabel(text=artist.get("name") or "", role="title_large"))
        alias = ", ".join(artist.get("alias") or [])
        if alias:
            info.add_widget(ThemedLabel(text=alias, role="body_small",
                                        color_name="on_surface_variant"))
        info.add_widget(ThemedLabel(
            text=f"{artist.get('music_size', 0)} {artist.get('album_size', 0)}",
            role="body_small", color_name="on_surface_variant"))
        top.add_widget(info)

        self.follow_button = IconButton(
            icon="check" if self._followed else "plus",
            color_name="primary",
        )
        self.follow_button.bind(on_release=self._toggle_follow)
        top.add_widget(self.follow_button)
        self.header_box.add_widget(top)

        description = artist.get("description") or ""
        if description:
            label = ThemedLabel(text=description, role="body_small",
                                color_name="on_surface_variant")
            label.bind(size=lambda inst, val: setattr(inst, "text_size", val))
            label.height = dp(60)
            self.header_box.add_widget(label)

        songs = payload.get("songs") or []
        self._offset = len(songs)
        self.song_list.set_tracks(songs, on_play=self._play)
        if payload.get("has_more"):
            self.song_list._box.add_widget(self._more_button())

    def _more_button(self) -> Any:
        from ncmkivy.widgets import _TextButton

        button = _TextButton(text=_t(self.app, "player.load_more", "加载更多"),
                             on_press=self._load_more)
        return button

    def _load_more(self) -> None:
        threading.Thread(target=self._load_more_thread, daemon=True).start()

    def _load_more_thread(self) -> None:
        from kivy.clock import Clock

        try:
            payload = self.controller.artist_songs(self.artist_id, self._offset)
        except Exception:  # pragma: no cover - network
            return
        Clock.schedule_once(lambda *_a: self._append(payload), 0)

    def _append(self, payload: dict[str, Any]) -> None:
        songs = payload.get("songs") or []
        self._offset += len(songs)
        self.song_list.set_tracks(
            list(self.song_list._box.children[:0]) or [], on_play=self._play)
        for song in songs:
            self.song_list._box.add_widget(TrackRow(song, on_play=self._play))

    def _toggle_follow(self, *_args: Any) -> None:
        threading.Thread(target=self._toggle_follow_thread, daemon=True).start()

    def _toggle_follow_thread(self) -> None:
        from kivy.clock import Clock

        try:
            result = self.controller.set_artist_follow(self.artist_id, not self._followed)
        except Exception:  # pragma: no cover - network
            return
        if result.get("success"):
            self._followed = not self._followed
        Clock.schedule_once(lambda *_a: self._refresh_follow_icon(), 0)

    def _refresh_follow_icon(self) -> None:
        if self.follow_button is None:
            return
        self.follow_button.icon = "check" if self._followed else "plus"
        self.follow_button._draw()

    def _play(self, track: dict[str, Any]) -> None:
        self.app.play_tracks([track], 0)


class FollowedArtistsScreen(_Screen):
    """The artists the account follows."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        # ``self.app`` must exist before the title is looked up.
        self.app = app
        super().__init__(app, title=_t(self.app, "android.followed_artists", "已关注歌手"),
                         **kwargs)
        self._build()

    def _build(self) -> None:
        self.list_box = BoxLayout(orientation="vertical")
        self.body.add_widget(self.list_box)
        # Into the list box, not the body: show_loading() clears the body and
        # would detach the list this screen renders into.
        self.list_box.add_widget(LoadingView())
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        from kivy.clock import Clock

        try:
            payload = self.controller.followed_artists()
        except Exception as exc:  # pragma: no cover - network
            # ``exc`` is deleted when the except block ends, so the message
            # is bound to a plain name before the callback runs.
            message = str(exc)
            Clock.schedule_once(lambda *_a: self.show_error(message), 0)
            return
        Clock.schedule_once(lambda *_a: self._render(payload), 0)

    def _render(self, payload: dict[str, Any]) -> None:
        self.list_box.clear_widgets()
        artists = payload.get("artists") or []
        if not artists:
            self.show_empty(_t(self.app, "android.no_followed_artists", "还没有关注任何歌手"))
            return
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(2))
        column = BoxLayout(orientation="vertical", size_hint_y=None,
                           spacing=dp(4), padding=(dp(16), dp(8)))
        column.bind(minimum_height=column.setter("height"))
        scroll.add_widget(column)
        for artist in artists:
            row = TappableRow(orientation="horizontal", size_hint_y=None, height=dp(64),
                               spacing=dp(12))
            cover = CoverImage(size=dp(48))
            if artist.get("cover_url"):
                cover.source = artist["cover_url"]
            row.add_widget(cover)
            row.add_widget(ThemedLabel(text=artist.get("name") or "", role="title_medium"))
            # The id is bound now: a lambda would capture the loop variable and
            # every row would open the last artist.
            artist_id = str(artist.get("id") or "")
            row.bind(on_release=lambda *_a, aid=artist_id: self.app.open_artist(aid))
            column.add_widget(row)
        self.list_box.add_widget(scroll)


class PlaylistScreen(_Screen):
    """The tracks of one playlist."""

    def __init__(self, app: Any, playlist_id: str = "", **kwargs: Any) -> None:
        self.app = app
        super().__init__(app, title="", **kwargs)
        self.playlist_id = playlist_id
        self._build()

    def _build(self) -> None:
        self.list = TrackList([], on_play=self._play)
        self.body.add_widget(self.list)
        # show_loading() clears the body, which would detach this list, so
        # the placeholder goes into the list itself.
        self.list.show_loading()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        from kivy.clock import Clock

        try:
            tracks = self.controller.playlist_tracks(self.playlist_id)
        except Exception as exc:  # pragma: no cover - network
            # ``exc`` is deleted when the except block ends, so the message
            # is bound to a plain name before the callback runs.
            message = str(exc)
            Clock.schedule_once(lambda *_a: self.show_error(message), 0)
            return
        Clock.schedule_once(lambda *_a: self._render(tracks), 0)

    def _render(self, tracks: list[dict[str, Any]]) -> None:
        self.list.set_tracks(tracks, on_play=self._play)

    def _play(self, track: dict[str, Any]) -> None:
        self.app.play_tracks([track], 0)


def build_screen(app: Any, section: str) -> BoxLayout | None:
    """Build the screen for a section key."""
    if section == "home":
        return HomeScreen(app)
    if section == "search":
        return SearchScreen(app)
    if section == "library":
        return LibraryScreen(app)
    if section == "downloads":
        return DownloadsScreen(app)
    if section == "me":
        return MeScreen(app)
    if section == "followed":
        return FollowedArtistsScreen(app)
    return None
