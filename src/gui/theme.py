"""Keep the native window chrome in sync with the in-app theme.

The web layer styles itself through the ``data-theme`` attribute on ``<html>``;
this module only handles the parts of the window the DOM cannot reach:

* **macOS** -- ``NSApp.setAppearance_`` recolours the title bar, menus and
  system dialogs (``None`` makes the app follow the OS again).
* **Windows** -- ``DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE)``
  repaints the title bar; older Windows 10 builds answer to attribute 19.

Everything here is best effort: a failure is logged and never breaks startup,
because the GUI must keep working on systems where these APIs are unavailable.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger("ncm.theme")

#: Theme modes the UI offers (``system`` follows the OS).
THEMES = ("light", "dark", "system")

#: Window background used before the first paint, matching ``--bg`` in style.css.
BACKGROUND_COLORS = {"light": "#f5f6f8", "dark": "#14161c"}


def normalize_theme(value: Any) -> str:
    """Return a valid theme mode (unknown values fall back to ``system``)."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in THEMES:
            return candidate
    return "system"


def system_theme() -> str:
    """The OS appearance: ``light`` or ``dark``."""
    try:
        if sys.platform == "darwin":
            from AppKit import NSApplication

            appearance = NSApplication.sharedApplication().effectiveAppearance()
            if appearance is not None and "Dark" in str(appearance.name()):
                return "dark"
            return "light"
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            try:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if int(value) else "dark"
            finally:
                winreg.CloseKey(key)
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("cannot detect the system theme: %s", exc)
    return "light"


def effective_theme(theme: Any) -> str:
    """Resolve a theme mode to the palette that must actually be shown."""
    mode = normalize_theme(theme)
    if mode == "system":
        return system_theme()
    return mode


def window_background_color(theme: Any) -> str:
    """Background colour to hand to ``webview.create_window`` (avoids a flash)."""
    return BACKGROUND_COLORS[effective_theme(theme)]


def apply_native_theme(window: Any, theme: Any) -> bool:
    """Repaint the OS window chrome. Returns ``True`` when it was applied."""
    mode = normalize_theme(theme)
    dark = effective_theme(mode) == "dark"
    try:
        if sys.platform == "darwin":
            return _apply_macos(dark if mode != "system" else None)
        if sys.platform == "win32":
            return _apply_windows(window, dark)
    except Exception as exc:  # pragma: no cover - platform specific
        log.warning("cannot apply the native theme: %s", exc)
        return False
    log.debug("native theme switching is not implemented on %s", sys.platform)
    return False


def _apply_macos(dark: bool | None) -> bool:
    """``None`` = follow the system, otherwise force the given appearance."""
    from AppKit import NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSApplication

    app = NSApplication.sharedApplication()
    if dark is None:
        app.setAppearance_(None)
        return True
    name = NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua
    appearance = NSAppearance.appearanceNamed_(name)
    if appearance is None:
        return False
    app.setAppearance_(appearance)
    return True


def _windows_handle(window: Any) -> int | None:
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    if handle is not None:
        try:
            return int(handle.ToInt64())  # pythonnet IntPtr
        except AttributeError:
            try:
                return int(handle)
            except (TypeError, ValueError):
                pass
    title = getattr(window, "title", None)
    if title:
        try:
            import ctypes

            found = ctypes.windll.user32.FindWindowW(None, title)
            return int(found) or None
        except Exception:  # pragma: no cover - no ctypes/windll
            return None
    return None


def _apply_windows(window: Any, dark: bool) -> bool:
    import ctypes

    handle = _windows_handle(window)
    if not handle:
        log.debug("no window handle available for the dark title bar")
        return False
    value = ctypes.c_int(1 if dark else 0)
    for attribute in (20, 19):  # 20 = Win10 2004+/Win11, 19 = older builds
        try:
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(handle), attribute, ctypes.byref(value),
                ctypes.sizeof(value))
        except Exception as exc:  # pragma: no cover - very old Windows
            log.debug("DwmSetWindowAttribute(%s) failed: %s", attribute, exc)
            continue
        if result == 0:
            return True
    return False
