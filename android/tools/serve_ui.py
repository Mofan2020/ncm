#!/usr/bin/env python3
"""Serve the desktop UI over HTTP with the REAL bridge behind it.

Why this exists: the artist page can only be judged by looking at it, and a
screenshot of a hand-written mock proves nothing.  This serves
``src/gui/assets`` and answers ``window.pywebview.api.<method>()`` from the
actual :class:`~src.gui.bridge.GuiBridge`, so what the browser renders is the
real page over real API data.

    python3 android/tools/serve_ui.py [--port 8777]

Then open http://127.0.0.1:8777/ in a browser.  A ``/shim.js`` endpoint
provides the ``window.pywebview`` stand-in; the page is rewritten to load it
before ``app.js``.

This is a development tool, not part of the app: it binds to 127.0.0.1 and
carries no authentication.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

ASSETS = ROOT / "src" / "gui" / "assets"
#: The phone interface lives in its own directory; the harness can serve
#: either, so the mobile pages can be driven from a browser.
MOBILE_ASSETS = ROOT / "android" / "web"

#: The shim installs a ``window.pywebview`` whose ``api`` methods are
#: ``fetch`` calls back to this server.  pywebview's methods are async on the
#: JS side, so every call returns a Promise.
SHIM = r"""
(function () {
  const PENDING = 'http://127.0.0.1:__PORT__';
  function call(name, args) {
    return fetch(PENDING + '/api/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(args || []),
    }).then(function (r) { return r.json(); })
      .then(function (payload) {
        if (payload && payload.__error__) throw new Error(payload.__error__);
        return payload ? payload.result : null;
      });
  }
  const api = new Proxy({}, {
    get: function (_target, name) {
      if (typeof name !== 'string') return undefined;
      // app.js probes for a known method to decide the bridge is ready.
      if (name === 'then') return undefined;
      return function () { return call(name, Array.prototype.slice.call(arguments)); };
    },
    has: function () { return true; },
  });
  window.pywebview = { api: api };
  window.dispatchEvent(new Event('pywebviewready'));
  // The push channel: the server posts the same calls bridge._call_js makes.
  window.__ncmPush = function (func, payload) {
    try { if (typeof window[func] === 'function') window[func](payload); }
    catch (e) { console.error('push ' + func + ' failed', e); }
  };
})();
"""


class Handler(BaseHTTPRequestHandler):
    """Serves the assets and proxies bridge calls."""

    protocol_version = "HTTP/1.1"

    #: The bridge, injected by :func:`main`.
    bridge = None

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass  # the browser console is loud enough

    # ------------------------------------------------------------------ serve
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/shim.js":
            body = SHIM.replace("__PORT__", str(self.server.server_port)).encode()
            self._send(200, "application/javascript; charset=utf-8", body)
            return

        if path in ("/", "/index.html"):
            html = (ASSETS / "index.html").read_text(encoding="utf-8")
            # Load the shim before app.js so the bridge is there when the
            # page starts polling for it.
            html = html.replace('<script src="icons.js"></script>',
                                '<script src="shim.js"></script>\n'
                                '    <script src="icons.js"></script>')
            if 'shim.js' not in html:
                # The phone interface has no icons.js; the shim still has to
                # be the first script, or the page polls for a bridge that
                # was never installed.
                html = html.replace('<script src="app.js"></script>',
                                    '<script src="shim.js"></script>\n'
                                    '    <script src="app.js"></script>')
            self._send(200, "text/html; charset=utf-8", html.encode())
            return

        candidate = (ASSETS / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(ASSETS.resolve())
        except ValueError:
            self._send(403, "text/plain", b"outside the assets dir")
            return
        if not candidate.is_file():
            self._send(404, "text/plain", b"not found")
            return
        suffix = candidate.suffix.lower()
        mime = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".txt": "text/plain; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        self._send(200, mime, candidate.read_bytes())

    # ------------------------------------------------------------------- call
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send(404, "application/json", b'{"__error__":"no such route"}')
            return
        method = parsed.path[len("/api/"):]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"[]"
        try:
            args = json.loads(raw or b"[]")
        except json.JSONDecodeError:
            args = []

        handler = getattr(self.bridge, method, None)
        if not callable(handler):
            self._send(200, "application/json",
                       json.dumps({"__error__": f"no bridge method {method}"}).encode())
            return
        try:
            result = handler(*args)
        except Exception as exc:  # noqa: BLE001 - report it to the page
            self._send(200, "application/json",
                       json.dumps({"__error__": f"{type(exc).__name__}: {exc}"}).encode())
            return
        try:
            body = json.dumps({"result": result}, ensure_ascii=False, default=str)
        except TypeError as exc:
            body = json.dumps({"__error__": f"result not serialisable: {exc}"})
        self._send(200, "application/json", body.encode())

    # ------------------------------------------------------------------ util
    def _send(self, code: int, mime: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--assets", default="",
                        help="serve this directory instead of the desktop interface")
    args = parser.parse_args()
    global ASSETS
    if args.assets:
        ASSETS = Path(args.assets).expanduser().resolve()
        print(f'serving {ASSETS}')

    from src.gui.bridge import GuiBridge

    bridge = GuiBridge()
    Handler.bridge = bridge
    # The bridge talks to the window for pushes; there is no window here, so
    # the push is forwarded to the page through a console call instead.  The
    # pages do not need it to render.
    bridge._window = None

    # The covers and the wallpaper are proxied through the loopback media
    # server, so it has to be running or ``media_base_url`` comes back with
    # port 0 and every cover renders as a broken image.
    try:
        bridge.media.start()
    except Exception as exc:  # pragma: no cover - port already bound
        print(f"media server did not start: {exc}", flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving the UI on http://127.0.0.1:{args.port}/", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
