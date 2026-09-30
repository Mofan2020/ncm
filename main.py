#!/usr/bin/env python3
"""NetEase Music Playlist Downloader - desktop entry point.

Launches the pywebview window that hosts ``src/gui/assets`` and wires it to the
Python bridge.  Works from source (``python3 main.py``) and from the frozen
PyInstaller builds produced by ``main.spec``.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_settings  # noqa: E402
from src.resources import resource_path  # noqa: E402
from src.version import APP_DISPLAY_NAME, __version__  # noqa: E402

__all__ = ["main"]

MIN_SIZE = (780, 560)
DEFAULT_SIZE = (1080, 720)


def _configure_logging(debug: bool) -> None:
    """Log to ``<config dir>/ncm.log``; echo to stderr in debug mode."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    settings = get_settings()
    try:
        log_path = settings.config_path.parent / "ncm.log"
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setLevel(logging.DEBUG if debug else logging.WARNING)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError as exc:  # pragma: no cover - unwritable config dir
        print(f"warning: cannot write log file: {exc}", file=sys.stderr)

    if debug:
        stream = logging.StreamHandler()
        stream.setLevel(logging.DEBUG)
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(stream)


def _initial_geometry(width: int, height: int) -> tuple[int, int, int | None, int | None]:
    """Clamp the stored window size to the current screen and centre it."""
    try:
        import webview  # imported lazily so --version works without a GUI

        screen = (webview.screens or [None])[0]
        if screen is not None:
            max_width = int(screen.width * 0.92)
            max_height = int(screen.height * 0.92)
            width = max(MIN_SIZE[0], min(int(width), max_width))
            height = max(MIN_SIZE[1], min(int(height), max_height))
            x = int(getattr(screen, "x", 0) or 0) + max(0, (screen.width - width) // 2)
            y = int(getattr(screen, "y", 0) or 0) + max(0, (screen.height - height) // 3)
            return width, height, x, y
    except Exception as exc:  # pragma: no cover - headless / exotic backends
        logging.getLogger("ncm").debug("cannot query screens: %s", exc)
    return width, height, None, None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="NeteaseMusicDownloader",
        description=f"{APP_DISPLAY_NAME} / NetEase Music Playlist Downloader",
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging and dev tools")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--self-test", action="store_true", dest="self_test",
                        help="check bundled assets and exit (used by CI builds)")
    parser.add_argument("--report", metavar="PATH", default=None,
                        help="write the --self-test report to PATH (frozen builds have no stdout)")
    return parser.parse_args(argv)


def _self_test(report_path: str | None = None) -> int:
    """Verify a frozen build can find its assets and its dependencies work.

    Deliberately headless: this is the smoke test CI runs against the packaged
    binary on Windows and macOS, where a GUI window cannot be opened.

    It must never raise: a windowed build with an unhandled exception pops a
    modal dialog and hangs an unattended runner forever (observed on Windows).
    """
    try:
        return _run_self_test(report_path)
    except BaseException as exc:  # pragma: no cover - guard against CI hangs
        _emit_report([("self-test crashed", False, f"{type(exc).__name__}: {exc}")],
                     report_path)
        return 1


def _run_self_test(report_path: str | None = None) -> int:
    checks: list[tuple[str, bool, str]] = []

    assets = resource_path("src", "gui", "assets")
    missing = [name for name in ("index.html", "app.js", "style.css")
               if not (assets / name).exists()]
    checks.append(("ui assets", not missing, str(assets) if not missing
                   else f"missing: {', '.join(missing)}"))

    from src.i18n import get_i18n

    i18n = get_i18n()
    languages = i18n.available_languages
    catalogue = i18n.get_flat_translations("zh_cn")
    checks.append(("translations", len(languages) >= 2 and len(catalogue) > 100,
                   f"{len(catalogue)} strings, {', '.join(sorted(languages))}"))

    from src.core.crypto import eapi_body, sign_eapi

    params = sign_eapi("/api/test", {"a": 1})
    checks.append(("eapi signing", len(params) > 64 and params == params.upper(),
                   f"{len(params)} hex chars"))
    checks.append(("eapi body", set(eapi_body("/api/test", {})) == {"params"}, "params key"))

    from src.core.downloader import MAX_CONCURRENT

    checks.append(("downloader", MAX_CONCURRENT == 3, f"max_concurrent={MAX_CONCURRENT}"))

    from src.config import get_settings

    settings = get_settings()
    checks.append(("settings", settings.download.max_concurrent <= MAX_CONCURRENT,
                   str(settings.config_path)))

    from src.gui.bridge import GuiBridge

    bridge = GuiBridge()
    strings = bridge.get_translations()
    checks.append(("bridge api", len(strings) > 100, f"{len(strings)} strings"))

    try:
        import webview

        backend = getattr(webview, "__version__", "?")
        gui_ok = hasattr(webview, "create_window") and hasattr(webview, "start")
        screens = len(getattr(webview, "screens", []) or [])
        detail = f"pywebview {backend}, {screens} screen(s)"
    except Exception as exc:  # pragma: no cover - depends on the platform
        gui_ok, detail = False, f"{type(exc).__name__}: {exc}"
    checks.append(("pywebview", gui_ok, detail))

    try:
        from qrcode import QRCode

        code = QRCode(box_size=4, border=1)
        code.add_data("self-test")
        code.make(fit=True)
        checks.append(("qrcode", True, "qr rendering"))
    except Exception as exc:  # pragma: no cover
        checks.append(("qrcode", False, f"{type(exc).__name__}: {exc}"))

    failed = [name for name, passed, _ in checks if not passed]
    _emit_report(checks, report_path)
    return 0 if not failed else 1


def _emit_report(checks: list[tuple[str, bool, str]], report_path: str | None) -> None:
    """Print the report and write it to ``report_path`` -- never raise.

    A windowed (``console=False``) build has ``sys.stdout is None`` and its
    console defaults to a legacy code page (the app name is Chinese), so both
    the print and the write have to be fully guarded.
    """
    import platform

    lines = [f"{APP_DISPLAY_NAME} {__version__} self-test",
             f"python {platform.python_version()} on {platform.system()} {platform.machine()}",
             f"frozen: {bool(getattr(sys, 'frozen', False))}",
             ""]
    lines += [f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}" for name, passed, detail in checks]
    failed = [name for name, passed, _ in checks if not passed]
    lines += ["", f"result: {'OK' if not failed else 'FAILED (' + ', '.join(failed) + ')'}"]
    report = "\n".join(lines)

    if report_path:
        try:
            Path(report_path).write_text(report + "\n", encoding="utf-8")
        except OSError:
            pass
    try:
        for stream in (sys.stdout, sys.stderr):
            if stream is None:
                continue
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass
        if sys.stdout is not None:
            print(report, flush=True)
    except BaseException:
        pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    if args.debug:
        settings.debug = True
    _configure_logging(settings.debug)
    log = logging.getLogger("ncm")

    if args.self_test:
        return _self_test(args.report)

    try:
        import webview
    except ImportError:
        print(
            "pywebview is not installed.\n"
            "  pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    html_path = resource_path("src", "gui", "assets", "index.html")
    if not html_path.exists():
        print(f"error: UI assets are missing ({html_path})", file=sys.stderr)
        return 1

    from src.gui.bridge import GuiBridge
    from src.gui.theme import (
        apply_native_theme,
        effective_theme,
        normalize_theme,
        window_background_color,
    )

    bridge = GuiBridge()
    width, height, x, y = _initial_geometry(
        settings.ui.window_width or DEFAULT_SIZE[0],
        settings.ui.window_height or DEFAULT_SIZE[1],
    )

    # The theme and language travel in the URL so the very first paint is
    # already correct (no white flash when the app opens in dark mode).
    theme_mode = normalize_theme(settings.ui.theme)
    ui_url = f"{html_path.as_uri()}?theme={effective_theme(theme_mode)}" \
             f"&mode={theme_mode}&lang={settings.ui.language}"

    window = webview.create_window(
        title=APP_DISPLAY_NAME,
        url=ui_url,
        js_api=bridge,
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=MIN_SIZE,
        resizable=True,
        text_select=True,
        background_color=window_background_color(theme_mode),
    )
    bridge.set_window(window)
    bridge.apply_theme = lambda mode: apply_native_theme(window, mode)

    def _sync_native_theme() -> None:
        apply_native_theme(window, settings.ui.theme)

    window.events.loaded += _sync_native_theme

    def _remember_geometry() -> None:
        try:
            if settings.ui.remember_window_size and window.width and window.height:
                settings.update_ui(window_width=int(window.width),
                                   window_height=int(window.height))
        except Exception as exc:  # pragma: no cover - window may be gone
            log.debug("cannot persist window size: %s", exc)

    window.events.closed += _remember_geometry

    log.info("starting %s %s (pywebview %s)", APP_DISPLAY_NAME, __version__,
             getattr(webview, "__version__", "?"))

    try:
        webview.start(debug=settings.debug)
    except Exception as exc:  # pragma: no cover - backend/runtime missing
        log.exception("pywebview failed to start")
        print(
            f"error: could not open the application window: {exc}\n"
            "  Windows: install the Microsoft Edge WebView2 runtime "
            "(https://developer.microsoft.com/microsoft-edge/webview2/)\n"
            "  Linux: install pywebview's GUI extras (pygobject or qt)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
