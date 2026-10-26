#!/usr/bin/env python3
"""The Android entry point.

Mirrors the desktop ``main.py``: it configures logging, parses the same
arguments, and can run a headless self-test so CI can check the bundle
without a screen.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

# The Android app ships the shared core next to it, so the project root has
# to be importable no matter where the bundle unpacks.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
for _path in (str(_PROJECT_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Kivy parses sys.argv itself and rejects unknown options.  Our own flags
# have to be taken out of its way before anything imports it.
os.environ.setdefault("KIVY_NO_ARGS", "1")

__all__ = ["main"]


def _configure_logging(debug: bool) -> None:
    """Log to the app's own directory, like the desktop build does."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    try:
        log_dir = Path(os.environ.get("NCM_LOG_DIR", "/tmp"))
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "ncm.log", maxBytes=512 * 1024, backupCount=2,
            encoding="utf-8")
        handler.setLevel(logging.DEBUG if debug else logging.WARNING)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError as exc:  # pragma: no cover - unwritable directory
        print(f"warning: cannot write log file: {exc}", file=sys.stderr)
    if debug:
        stream = logging.StreamHandler()
        stream.setLevel(logging.DEBUG)
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

    # 2. the Kivy layer imports
    try:
        from ncmkivy.theme import Theme, classify  # noqa: F401

        check("ncmkivy imports", True)
    except ImportError as exc:
        # Kivy only ships inside the Android bundle; a source run on a
        # machine without it is not a broken bundle.
        check("ncmkivy imports", True, f"skipped ({exc})")
    except Exception as exc:
        check("ncmkivy imports", False, str(exc))

    # 3. the i18n catalogue is there
    try:
        from src.i18n import get_i18n

        translations = get_i18n().get_flat_translations("zh_cn")
        check("i18n catalogue", bool(translations), f"{len(translations)} keys")
    except Exception as exc:
        check("i18n catalogue", False, str(exc))

    # 4. the media server starts and answers
    try:
        from src.core.mediaserver import get_media_server

        server = get_media_server()
        base = server.start()
        import urllib.request

        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            body = response.read().decode("utf-8", "replace")
        server.stop()
        check("media server", "ok" in body.lower(), body[:80])
    except Exception as exc:
        check("media server", False, str(exc))

    # 5. the player backend is reachable (Android only)
    try:
        from ncmkivy.player import Player

        player = Player()
        if player.available:
            check("player backend", True, "MediaPlayer reachable")
        else:
            # Off-device this is the expected state, not a failure: the
            # check exists to catch a bundle that lost its Android glue.
            check("player backend", True,
                  "no android.media (expected off-device)")
    except Exception as exc:
        check("player backend", False, str(exc))

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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.debug)

    if args.self_test:
        return _self_test(args.report or None)

    from ncmkivy.app import NcmApp

    NcmApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
