#!/usr/bin/env python3
"""The Android entry point: the desktop's shell with a different front end.

The phone runs the same Python core, the same bridge and the same media
server as the desktop.  Only the interface differs: ``android/web`` holds a
touch-sized version of it, served over the loopback server so the WebView
gets a plain http:// URL (the same reason the desktop does not use a file://
URL: a WebView cannot read local files the way the desktop's can).

Nothing here is Android-specific except where it has to be: the writable
directories, and the media library permission.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

# The Android app ships the shared core next to it, so the project root has
# to be importable no matter where the bundle unpacks.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
for _path in (str(_PROJECT_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

__all__ = ["main"]

log = logging.getLogger("ncm.android")

#: The name of the bundled interface directory.
WEB_DIR = "web"


def _configure_logging(debug: bool) -> None:
    """Log to the app's own directory, like the desktop build does."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    try:
        log_dir = Path(os.environ.get("NCM_LOG_DIR") or "/tmp")
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "ncm.log", maxBytes=512 * 1024, backupCount=2,
            encoding="utf-8")
        # INFO, not WARNING: with the level at WARNING every ordinary
        # line was dropped and the log file stayed empty, so a crash on
        # the phone left nothing to read.
        handler.setLevel(logging.DEBUG if debug else logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError as exc:  # pragma: no cover - unwritable directory
        print(f"warning: cannot write log file: {exc}", file=sys.stderr)

    # python-for-android pipes stdout and stderr into logcat, and logcat is
    # the only place a crash on the phone can be read from: the app's own
    # directory is not reachable without a file manager or adb.  So the
    # stream carries the same level as the file, and a report of a crash
    # can be copied straight out of logcat.
    stream = logging.StreamHandler()
    stream.setLevel(logging.DEBUG if debug else logging.INFO)
    stream.setFormatter(logging.Formatter(
        "%(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(stream)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NetEase Music for Android")
    parser.add_argument("--debug", action="store_true",
                        help="verbose logging")
    parser.add_argument("--self-test", action="store_true",
                        help="check the bundle and exit (no window)")
    parser.add_argument("--report", default="",
                        help="write the self-test report to this file")
    return parser.parse_args(argv)


def _prepare_home() -> None:
    """Point the app at directories it can actually write to.

    On Android the app process gets no ``HOME`` at all and there is no
    ``ANDROID_PRIVATE``: the environment of a running app holds only the
    handful of variables the system itself sets (checked on an Android 14
    emulator, ``/proc/<pid>/environ``).  The private directory is still
    known, though -- ``~`` resolves through the password database to the
    app's own files directory, which is the one place the app may write.

    Everything the settings, the logs and the downloads need has to live
    there, so the four variables are set outright.
    """
    private = os.environ.get("ANDROID_PRIVATE") or os.path.expanduser("~")
    if not private or not os.path.isabs(private):
        return
    # These are set outright, not with setdefault: the process may already
    # carry a HOME that points somewhere the app may not write, and
    # setdefault would keep it because the variable exists.  Everything the
    # app writes -- settings, logs, downloads, the media server's cache --
    # has to land inside the app's own directory.
    os.environ["HOME"] = private
    os.environ["XDG_CONFIG_HOME"] = str(Path(private) / ".config")
    os.environ["NCM_LOG_DIR"] = str(Path(private) / "logs")
    # The media server writes its cache next to the app data; a read-only
    # /tmp would break playback.
    os.environ["TMPDIR"] = str(Path(private) / "tmp")
    for name in ("XDG_CONFIG_HOME", "NCM_LOG_DIR", "TMPDIR"):
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)


def _self_test(report_path: str | None = None) -> int:
    """Check that the bundle is complete, without opening a window.

    The desktop build does the same thing so that a broken bundle can never
    reach a release; the Android build needs it for the same reason.
    """
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, bool(ok), detail))

    # 1. the shared core imports
    try:
        from src.core.api import NeteaseAPI  # noqa: F401

        check("core.api imports", True)
    except Exception as exc:
        check("core.api imports", False, str(exc))

    # 2. the bridge imports -- it is the whole interface
    try:
        from src.gui.bridge import GuiBridge  # noqa: F401

        check("bridge imports", True)
    except Exception as exc:
        check("bridge imports", False, str(exc))

    # 3. the webview shell is in the bundle
    try:
        import webview

        check("pywebview", hasattr(webview, "create_window"),
              getattr(webview, "__version__", "?"))
    except Exception as exc:
        check("pywebview", False, str(exc))

    # 4. the i18n catalogue is there
    try:
        from src.i18n import get_i18n

        translations = get_i18n().get_flat_translations("zh_cn")
        check("i18n catalogue", bool(translations), f"{len(translations)} keys")
    except Exception as exc:
        check("i18n catalogue", False, str(exc))

    # 5. the bundled interface is there
    web_root = _HERE / WEB_DIR
    missing = [name for name in ("index.html", "app.js", "style.css")
               if not (web_root / name).is_file()]
    check("web interface", not missing,
          str(web_root) if not missing else f"missing {missing}")

    # 6. the media server starts and answers
    try:
        from src.core.mediaserver import get_media_server

        server = get_media_server()
        base = server.start()
        import urllib.request

        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            body = response.read().decode("utf-8", "replace")
        server.set_ui_root(web_root)
        ui = server.ui_url("index.html")
        with urllib.request.urlopen(ui, timeout=5) as response:
            page = response.read().decode("utf-8", "replace")
        server.stop()
        check("media server", "ok" in body.lower(), body[:80])
        check("interface served",
              "<html" in page.lower() or "<!doctype" in page.lower(),
              f"{len(page)}B")
    except Exception as exc:
        check("media server", False, str(exc))

    if report_path:
        lines = []
        for label, ok, detail in checks:
            lines.append(f"{'PASS' if ok else 'FAIL'}\t{label}"
                         + (f"\t{detail}" if detail else ""))
        Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    failed = [item for item in checks if not item[1]]
    if failed:
        print(f"self-test: {len(failed)} of {len(checks)} checks failed")
        for label, _ok, detail in failed:
            print(f"  FAIL {label} {detail}")
        return 1
    print(f"self-test: {len(checks)} checks passed")
    return 0


def _start_app() -> int:
    """Open the window and run the event loop."""
    import webview

    from src.config import get_settings
    from src.gui.bridge import GuiBridge

    settings = get_settings()
    bridge = GuiBridge()

    # The player streams through a loopback HTTP server (see
    # src/core/mediaserver): it is the only way a WebView may read local
    # files and the only way we can control the Referer the CDN sees.  A
    # failure here disables playback but must never stop the app from opening.
    media = bridge.media
    media.configure(api_provider=lambda: bridge.api,
                    background_provider=settings.background_path)
    try:
        media.start()
        bridge.refresh_allowed_roots()
    except Exception as exc:  # pragma: no cover - port exhaustion
        log.warning("the media server could not start (%s); playback is disabled", exc)
    log.info("media server: %s", media.stats())

    # The interface is served over the loopback server, not from a file://
    # URL: a WebView may not read local files, and the same server is
    # already running for playback.
    web_root = _HERE / WEB_DIR
    ui_url = None
    if media.running:
        media.set_ui_root(web_root)
        ui_url = media.ui_url("index.html", query=f"lang={settings.ui.language}")
    if ui_url is None:
        ui_url = (web_root / "index.html").as_uri()
        log.warning("falling back to a file:// url: %s", ui_url)
    log.info("loading the interface from %s", ui_url)

    window = webview.create_window(
        title="网易云音乐",
        url=ui_url,
        js_api=bridge,
    )
    bridge.set_window(window)
    webview.start()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Before the logging, because that is what decides where the log file
    # goes: NCM_LOG_DIR is set here, and without it the log would land in
    # /tmp, which is read-only on Android.
    _prepare_home()
    _configure_logging(args.debug)

    if args.self_test:
        return _self_test(args.report or None)

    try:
        return _start_app()
    except Exception as exc:
        # The traceback goes to stdout and stderr on purpose: p4a forwards
        # both to logcat, and without it a crash on the phone is a black
        # screen with nothing to go on.
        log.exception("the app could not start")
        traceback.print_exc()
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
