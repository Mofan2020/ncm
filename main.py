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
from src.resources import (  # noqa: E402
    bundle_root,
    ensure_resources,
    is_frozen,
    resource_path,
)
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

    # Exercise the Windows unpack path here too, so CI proves the archive is
    # usable rather than merely present.  No-op everywhere else.
    unpacked = ensure_resources()
    checks.append(("resource unpack", True, str(unpacked) if unpacked
                   else "not used on this build"))

    assets = resource_path("src", "gui", "assets")
    index_path = assets / "index.html"
    missing = [name for name in ("index.html", "app.js", "style.css")
               if not (assets / name).exists()]
    checks.append(("ui assets", not missing, str(assets) if not missing
                   else f"missing: {', '.join(missing)}"))

    # Existence is not loadability.  Assert the URL handed to the webview is a
    # well-formed file URI whose query survives parsing -- the failure mode this
    # guards is "the file is on disk but the browser cannot load its URL", which
    # a file-existence check passes straight through.
    try:
        from urllib.parse import urlsplit

        probe = urlsplit(f"{index_path.as_uri()}?theme=dark&mode=dark&lang=zh_cn")
        url_ok = (probe.scheme == "file"
                  and probe.path.replace("\\", "/").endswith("index.html")
                  and probe.query == "theme=dark&mode=dark&lang=zh_cn")
        url_detail = f"scheme={probe.scheme} query={probe.query or '-'}"
    except Exception as exc:  # pragma: no cover - defensive
        url_ok, url_detail = False, f"{type(exc).__name__}: {exc}"
    checks.append(("ui url", url_ok, url_detail))

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

    from src.core.library import get_library

    library = get_library()
    checks.append(("library store", library.path.name == "library.json",
                   str(library.path)))

    from src.core.localmusic import guess_from_filename

    artist, title = guess_from_filename("01 - Artist - Title")
    checks.append(("local scanner", (artist, title) == ("Artist", "Title"),
                   f"{artist} / {title} (tags read on demand)"))

    from src.core.lyrics import build_timed_lyrics

    timed = build_timed_lyrics("[00:01.50]line one\n[00:02.00]line two")
    checks.append(("timed lyrics", len(timed) == 2 and timed[0]["time"] == 1500,
                   f"{len(timed)} lines, first at {timed[0]['time'] if timed else '-'} ms"))

    from src.core.mediaserver import MediaServer

    server = MediaServer()
    base_url = ""
    healthy = False
    ui_ok = False
    ui_url = None
    ui_detail = "not checked"
    try:
        import requests

        base_url = server.start()
        response = requests.get(server.url_for_health(), timeout=5)
        healthy = response.status_code == 200 and response.text.strip() == "ok"

        # The webview loads the interface over this server, so prove the route
        # really serves index.html -- not just that the file exists on disk.
        server.set_ui_root(assets)
        ui_url = server.ui_url("index.html", query="theme=dark&mode=dark&lang=zh_cn")
        page = requests.get(ui_url, timeout=5)
        ui_ok = (page.status_code == 200
                 and "text/html" in page.headers.get("Content-Type", "")
                 and len(page.content) > 10_000)
        ui_detail = (f"{page.status_code} {page.headers.get('Content-Type', '-')} "
                     f"{len(page.content)}B, ascii_url={ui_url.isascii()}")
        if not ui_url.isascii():
            ui_ok = False
            ui_detail += " (url is not ASCII - webview may refuse it)"
    except Exception as exc:  # pragma: no cover - port/permission problems
        base_url = f"error: {type(exc).__name__}: {exc}"
    finally:
        server.stop()
    checks.append(("media server", healthy, base_url or "did not start"))
    checks.append(("ui over http", ui_ok, ui_detail))

    # Leave behind the same diagnostics file the real startup writes, so CI can
    # assert on the URL that would actually be handed to the webview.
    if ui_ok:
        try:
            _write_load_diagnostics(report_path, ui_url, index_path)
        except Exception:  # pragma: no cover - diagnostics are best effort
            pass

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


def _write_load_diagnostics(report_path: str | None, ui_url: str | None,
                            html_path: Path, error: str | None = None) -> None:
    """Write what the webview was actually asked to load, plus the real error.

    When the window shows an error page instead of the app, the only useful
    information is the URL the webview ended up on and WebView2's own error
    string.  Neither reaches a ``console=False`` build's stdout, so they are
    written next to the executable where the user can open them.
    """
    import json
    import platform
    import traceback

    exe_dir = Path(sys.executable).resolve().parent if is_frozen() else Path.cwd()
    target = Path(report_path) if report_path else exe_dir / "ncm-startup.log"

    info: dict[str, object] = {
        "time": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "version": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "frozen": is_frozen(),
        "sys_executable": sys.executable,
        "_MEIPASS": getattr(sys, "_MEIPASS", None),
        "exe_dir": str(exe_dir),
        "html_path": str(html_path),
        "html_exists": html_path.is_file(),
        "html_size": html_path.stat().st_size if html_path.is_file() else None,
        "ui_url": ui_url,
        "resource_root": str(bundle_root()),
    }
    assets = html_path.parent
    if assets.is_dir():
        info["assets"] = {p.name: p.stat().st_size
                          for p in sorted(assets.iterdir()) if p.is_file()}
    if error:
        info["error"] = error
        info["traceback"] = traceback.format_exc()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except OSError:
        pass

    # If the webview did load, ask it what happened.  This must never raise:
    # an unhandled exception in a windowed build pops a modal error dialog.
    try:
        window = _active_window()
        if window is not None:
            probe = (
                "JSON.stringify({href: location.href, title: document.title,"
                " body: (document.body ? document.body.innerText : '')"
                ".slice(0, 400)})"
            )
            result = window.evaluate_js(probe)
            if result:
                try:
                    info["page"] = json.loads(result)
                except (TypeError, ValueError):
                    info["page"] = str(result)
            target.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:  # pragma: no cover - diagnostics must never break startup
        pass


def _active_window():
    """The pywebview window, once one exists."""
    try:
        import webview

        return webview.active_window()
    except Exception:
        return None


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

    # Re-encode a wallpaper that was stored before the downscale existed.  It
    # is a no-op for everyone else, and it runs before the media server starts
    # so the first paint already serves the smaller file.
    try:
        settings.migrate_background_image()
    except Exception as exc:  # pragma: no cover - defensive
        log.info("wallpaper migration failed: %s", exc)

    # Unpack the asset archive on Windows before anything resolves a resource
    # path.  No-op on macOS and from source, so the macOS startup path is
    # unchanged.
    unpacked = ensure_resources()
    if unpacked is not None:
        log.info("resources: %s", unpacked)

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
    # Inject webview module for desktop lyrics window creation
    bridge._webview_module = webview
    width, height, x, y = _initial_geometry(
        settings.ui.window_width or DEFAULT_SIZE[0],
        settings.ui.window_height or DEFAULT_SIZE[1],
    )

    # The player streams through a loopback HTTP server (see src/core/mediaserver):
    # it is the only way a webview may read local files and the only way we can
    # control the Referer the CDN sees.  A failure here disables playback but
    # must never stop the downloader from working.
    media = bridge.media
    media.configure(api_provider=lambda: bridge.api,
                    background_provider=settings.background_path)
    try:
        media.start()
        bridge.refresh_allowed_roots()
    except Exception as exc:  # pragma: no cover - port exhaustion / firewall
        log.warning("the media server could not start (%s); playback is disabled", exc)
    log.info("media server: %s", media.stats())

    # The theme and language travel in the URL so the very first paint is
    # already correct (no white flash when the app opens in dark mode).
    theme_mode = normalize_theme(settings.ui.theme)
    ui_query = (f"theme={effective_theme(theme_mode)}"
                f"&mode={theme_mode}&lang={settings.ui.language}")

    # Serve the interface over the loopback server rather than from a file://
    # URL.  Windows cannot open a file:// URL whose path is not pure ASCII --
    # with a Chinese install directory WebView2 renders its "file may have been
    # deleted or moved" page instead of the app.  An http:// URL on 127.0.0.1
    # sidesteps that entirely, and the server is already running for playback.
    ui_url = None
    if media.running:
        media.set_ui_root(html_path.parent)
        ui_url = media.ui_url("index.html", query=ui_query)
        if ui_url:
            log.info("loading the interface from %s", ui_url)
    if ui_url is None:
        # Fallback for the (unreachable in practice) case where the server
        # could not bind: a file:// URL still works on an ASCII path.
        ui_url = f"{html_path.as_uri()}?{ui_query}"
        log.warning("falling back to a file:// url: %s", ui_url)

    # Record what we are about to ask for, before anything can go wrong.
    _write_load_diagnostics(args.report, ui_url, html_path)

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

    def _record_loaded_page() -> None:
        """Once loaded, capture what the webview actually shows.

        If the window is an error page this is where its real text and URL land
        in ncm-startup.log, which is the only way to tell an ERR_FILE_NOT_FOUND
        apart from, say, a JS crash.
        """
        _write_load_diagnostics(args.report, ui_url, html_path)

    window.events.loaded += _sync_native_theme
    window.events.loaded += _record_loaded_page

    def _remember_geometry() -> None:
        try:
            if settings.ui.remember_window_size and window.width and window.height:
                settings.update_ui(window_width=int(window.width),
                                   window_height=int(window.height))
        except Exception as exc:  # pragma: no cover - window may be gone
            log.debug("cannot persist window size: %s", exc)

    window.events.closed += _remember_geometry

    def _shutdown() -> None:
        _remember_geometry()
        try:
            media.stop()
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("media server shutdown failed: %s", exc)

    window.events.closed += _shutdown

    log.info("starting %s %s (pywebview %s)", APP_DISPLAY_NAME, __version__,
             getattr(webview, "__version__", "?"))

    try:
        webview.start(debug=settings.debug)
    except Exception as exc:  # pragma: no cover - backend/runtime missing
        log.exception("pywebview failed to start")
        _write_load_diagnostics(args.report, ui_url, html_path,
                                error=f"{type(exc).__name__}: {exc}")
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
