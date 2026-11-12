"""Configuration management with YAML persistence.

The settings file lives in the platform's application-data directory and is
shared by every launch:

* macOS   ``~/Library/Application Support/NeteaseMusicDownloader/settings.yaml``
* Windows ``%APPDATA%\\NeteaseMusicDownloader\\settings.yaml``
* Linux   ``~/.config/NeteaseMusicDownloader/settings.yaml``

Unknown keys in an existing file are ignored (and dropped on the next save)
instead of aborting the load, so upgrading between versions never resets the
user's configuration.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - the Android build has no PyYAML wheel
    # PyYAML has no wheel that pip can use for an Android target, so the
    # phone build stores the same settings as JSON.  The file format is the
    # only difference: the settings themselves are identical.
    yaml = None

from src.version import APP_NAME

__all__ = ["Settings", "get_settings", "default_download_dir", "PLAY_MODES", "BACKGROUND_IMAGE"]

#: Playback order modes the player offers.  ``shuffle`` is a uniform random
#: pick -- play counts never influence the probability.
PLAY_MODES = ("single", "list", "shuffle")

#: Background images are copied here (inside the config dir) so replacing the
#: original file later cannot break the wallpaper.
BACKGROUND_IMAGE = "background"

#: Extensions accepted for a background image, in preference order.
BACKGROUND_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")

#: The wallpaper is shown blurred and dimmed behind a glass panel, so
#: storing more pixels than this buys nothing.  3840 px covers a 4K
#: display at native resolution.
BACKGROUND_MAX_EDGE = 3840
#: JPEG quality for the stored wallpaper.  88 is visually lossless for
#: a blurred background and roughly a twentieth of the PNG size.
BACKGROUND_JPEG_QUALITY = 88

#: A stored PNG above this size is re-encoded at startup.  Below it the
#: saving is not worth the risk of touching the user's file.
BACKGROUND_REWRITE_THRESHOLD = 2 * 1024 * 1024


def default_download_dir() -> Path:
    """Where downloads go unless the user picks something else."""
    music = Path.home() / "Music"
    if music.exists() or os.name != "nt":
        return music / "Downloads"
    return Path.home() / "Downloads" / APP_NAME


@dataclass
class DownloadSettings:
    """Download-related settings."""

    quality: str = "standard"        # standard|higher|exhigh|lossless|hires
    max_concurrent: int = 2          # 1-3
    overwrite: bool = False
    download_lyrics: bool = True      # write a .lrc next to every song
    lyrics_translation: bool = True   # merge the translation into that .lrc
    download_dir: str = ""


@dataclass
class PlaybackSettings:
    """Player-related settings."""

    #: ``True`` prefers the online stream, ``False`` prefers a local file.
    prefer_online: bool = True
    #: Level requested for online playback (standard|higher|exhigh|lossless|hires).
    online_quality: str = "standard"
    #: single|list|shuffle -- shuffle is uniform random, never weighted.
    play_mode: str = "list"
    volume: float = 0.8
    #: Restore the last track/position when the app starts again.
    resume_playback: bool = True
    #: Report playbacks to NetEase.  Off by default: the local counter is the
    #: real record, and sending anything to a third party is opt-in.
    report_play_count: bool = False
    #: Folders scanned for local music.
    local_dirs: list[str] = field(default_factory=list)
    #: Folders scanned for local video.  Kept apart from ``local_dirs``: a
    #: video folder inside a music folder would otherwise be listed twice,
    #: once by each side, and the two lists are chosen separately.
    local_video_dirs: list[str] = field(default_factory=list)
    #: Folder the last "play a folder" action used.
    last_local_dir: str = ""
    scan_depth: int = 6
    show_translation: bool = True
    #: Seconds the lyrics panel waits before it returns to the line that
    #: is playing after the user scrolled it somewhere else (1-30).
    lyrics_scroll_idle_seconds: int = 4
    #: Crossfade duration in seconds (0 = disabled, 1-12).
    crossfade_duration: float = 0.0
    #: Exclusive audio output mode: off|wasapi|coreaudio|auto
    exclusive_mode: str = "off"
    #: Sleep timer duration in minutes (0 = disabled, persisted per session).
    sleep_timer_minutes: int = 0


@dataclass
class BiliSettings:
    """Bilibili-related settings."""

    #: Quality asked for when playing or downloading a video.  0 means
    #: "the best the account may watch" -- the account decides.
    quality: int = 0
    #: Show the danmaku over the picture.
    danmaku_enabled: bool = True
    #: Font size of the danmaku (12-40).
    danmaku_font_size: int = 18
    #: How opaque the danmaku are (0.2-1.0).
    danmaku_opacity: float = 0.9
    #: Download the picture and the sound as two files.
    download_audio: bool = True
    #: Put the picture and the sound back together into one .mp4 after the
    #: download (needs ffmpeg; falls back to two files when it is missing).
    merge_audio: bool = True
    #: Where Bilibili downloads go.  Empty means the NetEase download folder,
    #: so an existing install keeps working; the Bilibili half can be pointed
    #: somewhere else without touching the music one.
    download_dir: str = ""
    #: Play the video in its own window instead of inside the app.
    separate_player_window: bool = True
    #: Report playback to B站 so its watch history and recommendations keep up.
    #: Off by default: it writes to the account and it tells a third party what
    #: is being watched, which is the user's call, not the app's.
    report_playback: bool = False
    #: Continue a video from where it was left off.  This reads the position
    #: out of the watch history, so it only has an effect when the history is
    #: actually filled -- that is, when report_playback is on.
    resume_playback: bool = True
    #: Words the user wants more of / less of in the recommendation feed.
    #: B站网页端没有「兴趣标签」接口，所以这些词只在本地对推荐结果做筛选：
    #: 命中的留下并排前面，被屏蔽的直接不显示。
    interest_tags: list[str] = field(default_factory=list)
    blocked_tags: list[str] = field(default_factory=list)


@dataclass
class BangumiSettings:
    """Bangumi（番组计划）的设置。

    收藏和观看历史只存在本地，这里不放它们——那部分在
    ``bangumi.json`` 里。
    """
    #: API 主机。国内用 ``api.bangumi.lat``，境外可以用 ``api.bgm.tv``。
    api_host: str = "api.bangumi.lat"
    #: 网页主机（条目页、资源区）。国内默认走镜像。
    web_host: str = "bangumi.lat"
    #: 镜像清单，顺序即默认顺序。第一个会拿来当默认。
    mirrors: list[dict[str, Any]] = field(default_factory=list)
    #: 资源站模板。用户自己填，程序按模板生成搜索/下载链接。
    resource_sites: list[dict[str, Any]] = field(default_factory=list)
    #: 两次请求之间的最小间隔（秒）。别调到太小，别人服务器会限流。
    request_interval: float = 1.0
    #: 跳过 SSL 校验。给用加速器代理的用户用。
    skip_ssl_verify: bool = False
    #: 下载落盘的位置。空字符串表示用默认的「下载/Bangumi」。
    download_dir: str = ""
    #: 用 Kazumi 规则搜索和播放。默认关：它会去请求第三方站点，
    #: 该由用户自己决定要不要开。
    source_enabled: bool = False
    #: 规则仓库。默认官方那个，可以换成自己的。
    rule_repos: list[str] = field(default_factory=lambda: ["https://github.com/Predidit/KazumiRules"])
    #: 当前选中的规则名。
    active_rule: str = ""
    #: 拉规则和拉页面时自动换镜像。默认关——用户自己走代理时多试几个只会更慢。
    auto_mirror: bool = False
    #: 找播放地址用的正则。空表示用内置的那几条。
    stream_patterns: list[str] = field(default_factory=list)


@dataclass
class UISettings:
    """UI-related settings."""

    language: str = "zh_cn"          # zh_cn|en_us
    theme: str = "system"            # light|dark|system
    window_width: int = 1080
    window_height: int = 720
    remember_window_size: bool = True
    #: File name of the chosen background image inside the config dir.
    background_image: str = ""
    #: 0-40 px of blur and 0-80 % darkening applied over the background.
    background_blur: int = 24
    background_dim: int = 30
    #: Liquid-glass surfaces (translucent + blurred) on or off.
    glass: bool = True
    #: Wallpaper accent is sampled from the background image when available.
    accent_from_background: bool = True
    #: Desktop lyrics window settings
    desktop_lyrics_font_size: int = 18
    desktop_lyrics_color: str = "#ffffff"
    desktop_lyrics_opacity: float = 0.9
    desktop_lyrics_show_translation: bool = True
    #: How the bubble behind the desktop lyrics is drawn:
    #:   'dark'        - a solid dark pill (the default)
    #:   'translucent' - a see-through pill, the desktop shows through
    #:   'none'        - no bubble at all, just the text
    desktop_lyrics_background: str = "dark"
    #: How the lyric text itself is drawn:
    #:   'transparent' - see-through glyphs, only the outline/shadow shows
    #:   'translucent' - semi-transparent, the bubble shows through the text
    #:   'white'       - pure white, fully opaque
    desktop_lyrics_text_style: str = "white"
    #: Set once the user has accepted the disclaimer.  It is stored with the
    #: version that was shown, so a new disclaimer is shown again after an
    #: update that changes the text.
    disclaimer_accepted_version: str = ""
    #: Look for a new release when the app starts.  Off by default: checking
    #: costs a request to GitHub on every launch, and the answer is only
    #: useful when the user actually wants to upgrade.
    check_updates_on_start: bool = False
    #: 发现新版本后自动下载并替换，还是先问一句。默认先问：
    #: 自动替换会在用户没看着的时候换掉他正在用的程序。
    auto_install_updates: bool = False
    #: 允许从预发布版本升级。默认关：选了 latest 通道的用户
    #: 不该被塞一个 beta 进去。
    update_include_prerelease: bool = False
    #: 关闭 SSL 证书校验（给用加速器代理的用户）。默认关：
    #: 代理会改写证书，不关校验就报 CERTIFICATE_VERIFY_FAILED。
    update_skip_ssl_verify: bool = False
    #: Tags of the releases the user said "skip this one" to, newest last.
    skipped_update_versions: list[str] = field(default_factory=list)


@dataclass
class LocalPlaylistSettings:
    """Local playlist settings (stored in config, independent of cloud)."""

    # List of local playlists, each: {"id", "name", "tracks": [{"id", "name", "artists", "album", "duration", "path"}]}
    playlists: list[dict[str, Any]] = field(default_factory=list)
    # Current local playlist sort order
    sort_order: str = "manual"  # manual|name|artist|album|date_added
    # Auto-save local playlists to JSON file in config dir
    auto_save: bool = True


@dataclass
class AuthSettings:
    """Authentication settings."""

    login_method: str = ""           # qrcode|phone|cookie
    saved_cookie: str = ""
    # On by default: the session cookie is written to disk after a login,
    # so the app comes back with the account still signed in.  Turning it
    # off in the settings keeps the app from saving the cookie.
    remember_login: bool = True


@dataclass
class AppSettings:
    """Root settings container."""

    download: DownloadSettings = field(default_factory=DownloadSettings)
    playback: PlaybackSettings = field(default_factory=PlaybackSettings)
    ui: UISettings = field(default_factory=UISettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    local_playlists: LocalPlaylistSettings = field(default_factory=LocalPlaylistSettings)
    bili: BiliSettings = field(default_factory=BiliSettings)
    bangumi: BangumiSettings = field(default_factory=BangumiSettings)
    debug: bool = False


def _filtered(cls: Any, data: dict[str, Any] | None) -> dict[str, Any]:
    """Drop keys the dataclass does not know about."""
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in (data or {}).items() if k in known}


def _as_bool(value: Any) -> bool:
    """Tolerant truthiness for values that may come from JSON or YAML."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _clean_host(value: Any) -> str:
    """Pull a bare host out of whatever the user typed.

    Accepts ``bangumi.lat``, ``https://bangumi.lat/``, ``api.bgm.tv/v0``
    and gives back ``bangumi.lat`` / ``api.bgm.tv``.  A host is only a host
    name -- a path in there is a mistake worth silently dropping.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    text = text.split("?", 1)[0].strip()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    # A port is not part of the host name.
    if ":" in text:
        text = text.split(":", 1)[0]
    # Only a host name is allowed here -- no spaces, no slashes.
    if not text or any(ch.isspace() for ch in text):
        return ""
    if not all(ch.isalnum() or ch in ".-" for ch in text):
        return ""
    # A bare number is a YAML accident (``api_host: 123``), not a host name:
    # a host has at least one dot or a letter in it.
    if not any(ch.isalpha() for ch in text):
        return ""
    return text.lower()


def _clean_string_list(
    value: Any,
    fallback: list[str] | None = None,
    limit: int = 20,
) -> list[str]:
    """把一串字符串洗净：去空白、丢空项、去重、限长。

    ``fallback`` 只在结果为空时用——用户清空列表想要的是"没有"，不是
    "回到默认"，所以不能无条件补回去。但规则仓库列表清空了程序就没法工作，
    那种情况调用方自己决定要不要给 fallback。
    """
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, list | tuple):
        items = list(value)
    else:
        items = []

    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= max(1, limit):
            break
    if not out and fallback:
        return list(fallback)
    return out


def _clean_mirrors(value: Any) -> list[dict[str, Any]]:
    """Keep the mirror list well-formed.

    Each entry needs an id and at least one host; everything else is
    optional.  Broken entries are dropped rather than repaired -- a mirror
    with no host is useless.
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("id") or "").strip()
        api_hosts = [_clean_host(h) for h in (raw.get("api_hosts") or [])
                     if isinstance(h, str)]
        api_hosts = [h for h in api_hosts if h]
        main_hosts = [_clean_host(h) for h in (raw.get("main_hosts") or [])
                      if isinstance(h, str)]
        main_hosts = [h for h in main_hosts if h]
        if not mid:
            mid = (api_hosts or main_hosts or ["mirror"])[0]
        if not (api_hosts or main_hosts):
            continue
        if mid in seen:
            continue
        seen.add(mid)
        out.append({
            "id": mid,
            "name": str(raw.get("name") or mid),
            "url": str(raw.get("url") or ""),
            "api_hosts": api_hosts,
            "main_hosts": main_hosts,
            "recommended": bool(raw.get("recommended")),
            "note": str(raw.get("note") or ""),
        })
    return out


def _clean_resource_sites(value: Any) -> list[dict[str, Any]]:
    """Keep the resource-site templates well-formed.

    A site without a ``detail_url`` or a ``search_url`` is useless, so it
    is dropped.  The URLs are not validated any further -- the user may
    legitimately use a template with a placeholder in it.
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        detail = str(raw.get("detail_url") or "").strip()
        search = str(raw.get("search_url") or "").strip()
        if not detail and not search:
            continue
        sid = str(raw.get("id") or "").strip()
        if not sid:
            sid = detail or search
        out.append({
            "id": sid,
            "name": str(raw.get("name") or sid),
            "kind": str(raw.get("kind") or ""),
            "enabled": _as_bool(raw.get("enabled", True)),
            "detail_url": detail,
            "search_url": search,
            "note": str(raw.get("note") or ""),
        })
    return out


class Settings:
    """Thread-safe settings manager with YAML persistence (singleton)."""

    _instance: Settings | None = None
    _lock = threading.Lock()

    def __new__(cls) -> Settings:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self.logger = logging.getLogger("ncm.settings")
        self._data = AppSettings()
        self._config_path = self._config_path_for_platform()
        self._save_lock = threading.Lock()
        self.load()
        # Set last: a background thread that grabs the singleton while __init__
        # is still running must not see it as ready before the state exists.
        self._initialized = True

    # ------------------------------------------------------------------- paths
    @staticmethod
    def _config_dir() -> Path:
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        return base / APP_NAME

    @staticmethod
    def _legacy_config_dir() -> Path | None:
        """The config directory of the name the project had before."""
        try:
            from src.version import LEGACY_APP_NAME
        except Exception:
            return None
        if not LEGACY_APP_NAME or LEGACY_APP_NAME == APP_NAME:
            return None
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        return base / LEGACY_APP_NAME

    def _config_path_for_platform(self) -> Path:
        directory = self._config_dir()
        # The project was renamed, so the config directory moved with it --
        # except on the machines that already used the old name.  Those
        # keep their old directory: it holds the saved login, the settings
        # and the cache, and moving it would ask the user to sign in again
        # for nothing.  A machine that has never run the old version gets
        # the new directory.
        legacy = self._legacy_config_dir()
        if legacy is not None and legacy.is_dir():
            current_has_settings = any(
                (directory / name).is_file()
                for name in ("settings.yaml", "settings.json")
            )
            legacy_has_settings = any(
                (legacy / name).is_file()
                for name in ("settings.yaml", "settings.json")
            )
            if legacy_has_settings and not current_has_settings:
                directory = legacy
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("cannot create config dir %s: %s", directory, exc)
        # Without PyYAML the file is JSON; the extension follows the writer so
        # nobody tries to read one format as the other.
        return directory / ("settings.yaml" if yaml is not None else "settings.json")

    @property
    def config_path(self) -> Path:
        return self._config_path

    # -------------------------------------------------------------------- I/O
    def load(self) -> None:
        if not self._config_path.exists():
            return
        try:
            with open(self._config_path, encoding="utf-8") as handle:
                if yaml is not None:
                    raw = yaml.safe_load(handle) or {}
                else:
                    raw = json.load(handle) or {}
            if not isinstance(raw, dict):
                raise ValueError("settings file is not a mapping")
            self._data = AppSettings(
                download=DownloadSettings(**_filtered(DownloadSettings, raw.get("download"))),
                playback=PlaybackSettings(**_filtered(PlaybackSettings, raw.get("playback"))),
                ui=UISettings(**_filtered(UISettings, raw.get("ui"))),
                auth=AuthSettings(**_filtered(AuthSettings, raw.get("auth"))),
                local_playlists=LocalPlaylistSettings(**_filtered(LocalPlaylistSettings, raw.get("local_playlists"))),
                bili=BiliSettings(**_filtered(BiliSettings, raw.get("bili"))),
                bangumi=BangumiSettings(**_filtered(BangumiSettings, raw.get("bangumi"))),
                debug=bool(raw.get("debug", False)),
            )
            self._normalize()
        except Exception as exc:
            self.logger.warning("failed to load settings (%s); using defaults", exc)
            self._data = AppSettings()

    def _normalize(self) -> None:
        """Re-apply the ranges and shapes a hand-edited file may have broken."""
        ui = self._data.ui
        # An unknown value must fall back to the default, not be stored
        # verbatim: the overlay only knows three modes.
        if ui.desktop_lyrics_background not in ("dark", "translucent", "none"):
            ui.desktop_lyrics_background = "dark"
        if ui.desktop_lyrics_text_style not in ("transparent", "translucent", "white"):
            ui.desktop_lyrics_text_style = "white"
        ui.background_blur = self._clamp_range(ui.background_blur, 0, 40, 24)
        ui.background_dim = self._clamp_range(ui.background_dim, 0, 80, 30)
        for flag in ("remember_window_size", "glass", "accent_from_background",
                     "check_updates_on_start", "auto_install_updates",
                     "update_include_prerelease", "update_skip_ssl_verify"):
            setattr(ui, flag, _as_bool(getattr(ui, flag)))
        if not isinstance(ui.skipped_update_versions, list):
            ui.skipped_update_versions = []
        ui.skipped_update_versions = [
            str(item).strip() for item in ui.skipped_update_versions
            if str(item or "").strip()
        ]
        ui.desktop_lyrics_font_size = self._clamp_range(ui.desktop_lyrics_font_size, 10, 36, 18)
        ui.desktop_lyrics_opacity = self._clamp_float(ui.desktop_lyrics_opacity, 0.1, 1.0, 0.9)
        if not ui.desktop_lyrics_color or not str(ui.desktop_lyrics_color).startswith("#"):
            ui.desktop_lyrics_color = "#ffffff"
        ui.desktop_lyrics_show_translation = _as_bool(getattr(ui, "desktop_lyrics_show_translation", True))

        playback = self._data.playback
        for flag in ("prefer_online", "resume_playback", "report_play_count",
                     "show_translation"):
            setattr(playback, flag, _as_bool(getattr(playback, flag)))
        playback.scan_depth = self._clamp_range(playback.scan_depth, 1, 12, 6)
        try:
            playback.volume = min(1.0, max(0.0, float(playback.volume)))
        except (TypeError, ValueError):
            playback.volume = 0.8
        if playback.play_mode not in PLAY_MODES:
            playback.play_mode = "list"
        if not isinstance(playback.local_dirs, list):
            playback.local_dirs = []
        playback.local_dirs = [str(item) for item in playback.local_dirs if str(item or "").strip()]
        if not isinstance(playback.local_video_dirs, list):
            playback.local_video_dirs = []
        playback.local_video_dirs = [str(item) for item in playback.local_video_dirs
                                     if str(item or "").strip()]
        for flag in ("overwrite", "download_lyrics", "lyrics_translation"):
            setattr(self._data.download, flag, _as_bool(getattr(self._data.download, flag)))
        playback.crossfade_duration = self._clamp_float(playback.crossfade_duration, 0.0, 12.0, 0.0)
        playback.lyrics_scroll_idle_seconds = self._clamp_range(
            playback.lyrics_scroll_idle_seconds, 1, 30, 4)
        if playback.exclusive_mode not in ("off", "wasapi", "coreaudio", "auto"):
            playback.exclusive_mode = "off"
        playback.sleep_timer_minutes = self._clamp_range(playback.sleep_timer_minutes, 0, 720, 0)

        bili = self._data.bili
        bili.quality = self._clamp_range(bili.quality, 0, 127, 0)
        bili.danmaku_font_size = self._clamp_range(bili.danmaku_font_size, 12, 40, 18)
        bili.danmaku_opacity = self._clamp_float(bili.danmaku_opacity, 0.2, 1.0, 0.9)
        for flag in ("danmaku_enabled", "download_audio", "separate_player_window",
                     "merge_audio", "report_playback", "resume_playback"):
            setattr(bili, flag, _as_bool(getattr(bili, flag)))
        bili.download_dir = str(bili.download_dir or "")
        for name in ("interest_tags", "blocked_tags"):
            value = getattr(bili, name)
            if not isinstance(value, list):
                value = []
            setattr(bili, name, [str(item).strip() for item in value if str(item or "").strip()])

        bangumi = self._data.bangumi
        bangumi.api_host = _clean_host(bangumi.api_host) or "api.bangumi.lat"
        bangumi.web_host = _clean_host(bangumi.web_host) or "bangumi.lat"
        bangumi.request_interval = self._clamp_float(
            bangumi.request_interval, 0.0, 10.0, 1.0)
        bangumi.skip_ssl_verify = _as_bool(bangumi.skip_ssl_verify)
        bangumi.download_dir = str(bangumi.download_dir or "")
        bangumi.mirrors = _clean_mirrors(bangumi.mirrors)
        bangumi.resource_sites = _clean_resource_sites(bangumi.resource_sites)
        bangumi.source_enabled = _as_bool(bangumi.source_enabled)
        bangumi.auto_mirror = _as_bool(bangumi.auto_mirror)
        bangumi.rule_repos = _clean_string_list(
            bangumi.rule_repos, fallback=["https://github.com/Predidit/KazumiRules"], limit=10)
        bangumi.stream_patterns = _clean_string_list(bangumi.stream_patterns, limit=20)
        bangumi.active_rule = str(bangumi.active_rule or "").strip()[:80]

    @staticmethod
    def _clamp_range(value: Any, low: int, high: int, fallback: int) -> int:
        try:
            return max(low, min(int(value), high))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
        try:
            return max(low, min(float(value), high))
        except (TypeError, ValueError):
            return fallback

    def save(self) -> None:
        with self._save_lock:
            payload = {
                "download": asdict(self._data.download),
                "playback": asdict(self._data.playback),
                "ui": asdict(self._data.ui),
                "auth": asdict(self._data.auth),
                "local_playlists": asdict(self._data.local_playlists),
                # 少了这一段，B站设置页保存时写得进内存、重启就回默认值。
                "bili": asdict(self._data.bili),
                # Bangumi 的镜像和资源站模板同理。
                "bangumi": asdict(self._data.bangumi),
                "debug": self._data.debug,
            }
            try:
                with open(self._config_path, "w", encoding="utf-8") as handle:
                    if yaml is not None:
                        yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
                    else:
                        json.dump(payload, handle, ensure_ascii=False, indent=2)
            except OSError as exc:
                self.logger.warning("failed to save settings: %s", exc)

    # -------------------------------------------------------------- accessors
    @property
    def download(self) -> DownloadSettings:
        return self._data.download

    @property
    def playback(self) -> PlaybackSettings:
        return self._data.playback

    @property
    def ui(self) -> UISettings:
        return self._data.ui

    @property
    def auth(self) -> AuthSettings:
        return self._data.auth

    @property
    def bili(self) -> BiliSettings:
        return self._data.bili

    def local_playlists(self) -> LocalPlaylistSettings:
        return self._data.local_playlists

    @property
    def debug(self) -> bool:
        return self._data.debug

    @debug.setter
    def debug(self, value: bool) -> None:
        self._data.debug = bool(value)
        self.save()

    def update_download(self, **kwargs: Any) -> None:
        for key, value in _filtered(DownloadSettings, kwargs).items():
            setattr(self._data.download, key, value)
        # The UI sends JSON, so booleans may arrive as "true"/"false" strings.
        for flag in ("overwrite", "download_lyrics", "lyrics_translation"):
            setattr(self._data.download, flag, _as_bool(getattr(self._data.download, flag)))
        self._data.download.max_concurrent = self._clamp_concurrency(
            self._data.download.max_concurrent)
        self.save()

    @staticmethod
    def _clamp_concurrency(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 2
        return max(1, min(number, 3))

    def update_ui(self, **kwargs: Any) -> None:
        for key, value in _filtered(UISettings, kwargs).items():
            setattr(self._data.ui, key, value)
        self._normalize()
        self.save()

    def update_playback(self, **kwargs: Any) -> None:
        for key, value in _filtered(PlaybackSettings, kwargs).items():
            setattr(self._data.playback, key, value)
        self._normalize()
        self.save()

    def update_auth(self, **kwargs: Any) -> None:
        for key, value in _filtered(AuthSettings, kwargs).items():
            setattr(self._data.auth, key, value)
        self.save()

    def update_bili(self, **kwargs: Any) -> None:
        """Store the Bilibili settings and write them out."""
        for key, value in kwargs.items():
            if hasattr(self._data.bili, key):
                setattr(self._data.bili, key, value)
        self._normalize()
        self.save()

    def update_bangumi(self, **kwargs: Any) -> None:
        """Store the Bangumi settings and write them out."""
        for key, value in kwargs.items():
            if hasattr(self._data.bangumi, key):
                setattr(self._data.bangumi, key, value)
        self._normalize()
        self.save()

    def update_local_playlists(self, **kwargs: Any) -> None:
        for key, value in _filtered(LocalPlaylistSettings, kwargs).items():
            setattr(self._data.local_playlists, key, value)
        self.save()

    # ---------------------------------------------------------------- wallpaper
    def background_path(self) -> Path | None:
        """Absolute path of the stored wallpaper, or ``None`` when unset/missing."""
        name = str(self._data.ui.background_image or "").strip()
        if not name:
            return None
        candidate = self._config_path.parent / name
        return candidate if candidate.is_file() else None

    def set_background_image(self, source: str | Path | None) -> str:
        """Copy ``source`` into the config dir and remember it.

        Passing ``None`` (or an unreadable path) removes the wallpaper.  Copies
        instead of referencing, so the wallpaper survives the original being
        moved or deleted, and travels with a config backup.

        The copy is **downscaled and re-encoded**.  A 3600x2400 RGBA PNG is
        23 MB in the config directory, and the app only ever shows the image
        blurred and dimmed behind a glass panel -- the extra pixels cost disk
        and decoding time for nothing.  Stored as JPEG at the display size,
        the same wallpaper is under 1 MB.
        """
        directory = self._config_path.parent
        if source is None:
            self.clear_background_image()
            return ""
        try:
            src = Path(source).expanduser()
            if not src.is_file():
                raise FileNotFoundError(str(src))
            target = directory / f"{BACKGROUND_IMAGE}.jpg"
            for old in directory.glob(f"{BACKGROUND_IMAGE}.*"):
                if old != target:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            if not self._write_background(src, target):
                shutil.copyfile(src, target)
        except Exception as exc:
            self.logger.warning("cannot store the background image: %s", exc)
            return ""
        # Build the display-sized copy now, while the user is looking at the
        # settings dialog.  Doing it on the first request instead would put
        # the decode-and-resize into the app's request path, and the startup
        # request would pay for it.
        self._write_display_background(target)
        self.update_ui(background_image=target.name)
        return target.name

    def _write_background(self, src: Path, target: Path) -> bool:
        """Downscale ``src`` into ``target`` as a JPEG.  False if unavailable.

        The resizing is done by the platform's own image tool (see
        :mod:`src.core.imaging`), so no imaging library ships with the app.
        The caller falls back to a plain copy when it is not available.
        """
        from src.core.imaging import downscale_to_jpeg

        return downscale_to_jpeg(
            src,
            target,
            max_edge=BACKGROUND_MAX_EDGE,
            quality=BACKGROUND_JPEG_QUALITY,
        )

    def _write_display_background(self, source: Path) -> None:
        """Build the display-sized copy of the wallpaper.

        The browser decodes an image at its full pixel size no matter how
        small the CSS box is, so a 3600x2400 wallpaper costs the WebView
        ~34 MB of RGBA pixels -- plus the blurred copy the compositor keeps
        for the glass panels.  A 1920 px copy removes most of that, and the
        CSS blur hides the difference.

        The scaling itself lives in :meth:`src.core.mediaserver.MediaServer.scaled_background`
        -- this only asks for it eagerly, so the first request does not pay
        for the decode.
        """
        try:
            from src.core.mediaserver import get_media_server

            get_media_server().scaled_background(source)
        except Exception as exc:
            self.logger.info("cannot write the display wallpaper: %s", exc)

    def migrate_background_image(self) -> bool:
        """Re-encode a stored wallpaper that is still a full-size PNG.

        Wallpapers saved before the downscale existed are still the original
        files -- a 23 MB PNG for an image the app blurs anyway.  This runs
        once at startup and rewrites them in place; it is a no-op for a
        wallpaper that is already a JPEG or already small.

        Returns True when something was rewritten.
        """
        current = self.background_path()
        if current is None or current.suffix.lower() != ".png":
            return False
        try:
            if current.stat().st_size <= BACKGROUND_REWRITE_THRESHOLD:
                return False
        except OSError:
            return False
        try:
            if not self._write_background(current, current.with_suffix(".jpg")):
                return False
        except Exception as exc:
            self.logger.info("cannot re-encode the stored wallpaper: %s", exc)
            return False
        try:
            current.unlink()
        except OSError:
            pass
        self._write_display_background(current.with_suffix(".jpg"))
        self.update_ui(background_image="background.jpg")
        self.logger.info("re-encoded the stored wallpaper to JPEG")
        return True

    def clear_background_image(self) -> None:
        directory = self._config_path.parent
        for old in directory.glob(f"{BACKGROUND_IMAGE}.*"):
            try:
                old.unlink()
            except OSError:
                pass
        self.update_ui(background_image="")

    def reset(self) -> None:
        self._data = AppSettings()
        self.save()


def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return Settings()
