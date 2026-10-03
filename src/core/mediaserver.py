"""A loopback HTTP media server for the player.

The UI is an HTML page inside a webview while the audio lives somewhere else:
either in a signed NetEase CDN URL (which wants the desktop client's ``Referer``)
or in a local file (which the webview's opaque ``file://`` origin is not allowed
to read).  Both are served from here, so the ``<audio>`` element only ever sees
``http://127.0.0.1:<port>/m/<token>/...``:

* ``/m/<token>/online/<song_id>?q=<level>`` - resolve + proxy the CDN stream
* ``/m/<token>/file?p=<abs path>``         - a local file, restricted to the
  configured music folders (and the download folder)
* ``/m/<token>/art?p=<abs path>``          - cover art embedded in a local file
* ``/m/<token>/bg``                        - the user's background image
* ``/m/<token>/health``                    - readiness probe

``Range`` requests are forwarded (online) or honoured directly (local), which is
what makes seeking work.  Everything binds to ``127.0.0.1`` only and every path
carries a random token, so no other process on the machine can use the server to
read arbitrary files.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

__all__ = ["MediaServer", "get_media_server"]

#: How long a resolved CDN url is reused before it is asked for again.
URL_TTL = 600.0

#: Streaming chunk size (kept small so a seek aborts quickly).
CHUNK = 256 * 1024

#: Cover-art cache budget (bytes) and the biggest single image we keep.  The UI
#: only ever draws small thumbnails, so this stays deliberately small: the
#: player must not eat hundreds of megabytes on an 8 GB machine.
COVER_CACHE_BYTES = 12 * 1024 * 1024
MAX_COVER_ENTRY_BYTES = 4 * 1024 * 1024

#: How many resolved CDN urls we remember (they expire after :data:`URL_TTL`).
MAX_CACHED_URLS = 512

log = logging.getLogger("ncm.media")


class _CoverCache:
    """Tiny LRU-ish cache so scrolling a local list does not re-read files."""

    def __init__(self, budget: int = COVER_CACHE_BYTES) -> None:
        self._budget = budget
        self._items: dict[str, tuple[bytes, str]] = {}
        self._order: list[str] = []
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bytes, str] | None:
        with self._lock:
            return self._items.get(key)

    def put(self, key: str, value: tuple[bytes, str]) -> None:
        with self._lock:
            if key in self._items:
                return
            size = len(value[0])
            if size > MAX_COVER_ENTRY_BYTES:
                return
            self._items[key] = value
            self._order.append(key)
            self._size += size
            while self._size > self._budget and self._order:
                oldest = self._order.pop(0)
                dropped = self._items.pop(oldest, None)
                if dropped:
                    self._size -= len(dropped[0])

    @property
    def count(self) -> int:
        """How many images are held (for the memory report)."""
        with self._lock:
            return len(self._items)

    @property
    def size(self) -> int:
        """Bytes of image data held."""
        with self._lock:
            return self._size


class _Handler(BaseHTTPRequestHandler):
    """Request handler bound to one :class:`MediaServer`."""

    server_version = "ncm-media/1.0"
    protocol_version = "HTTP/1.1"

    #: Injected by :meth:`MediaServer._make_server`.
    media: MediaServer

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- dispatching
    def do_GET(self) -> None:
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:
        self._dispatch(send_body=False)

    def _dispatch(self, *, send_body: bool) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        if len(parts) < 3 or parts[0] != "m" or parts[1] != self.media.token:
            self._error(404, "not found")
            return
        route = parts[2]
        try:
            if route == "health":
                self._plain(b"ok", "text/plain; charset=utf-8", send_body)
            elif route == "online":
                song_id = parts[3] if len(parts) > 3 else ""
                self._online(song_id, query.get("q", ["standard"])[0], send_body)
            elif route == "file":
                self._local(query.get("p", [""])[0], send_body)
            elif route == "art":
                self._art(query.get("p", [""])[0], send_body)
            elif route == "bg":
                self._background(send_body)
            else:
                self._error(404, "unknown route")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - never kill the server
            log.warning("media request %s failed: %s", self.path, exc)
            self._error(500, "internal error")

    # ----------------------------------------------------------- small helpers
    def _error(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def _plain(self, body: bytes, content_type: str, send_body: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_headers(self, status: int, content_type: str, length: int | None,
                      extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _range_for(self, size: int) -> tuple[int, int] | None:
        """Parse the ``Range`` header; ``None`` means "the whole thing"."""
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return None
        spec = header[len("bytes="):].split(",")[0].strip()
        start_text, _, end_text = spec.partition("-")
        try:
            if not start_text:                      # bytes=-500 -> last 500 bytes
                length = int(end_text)
                if length <= 0:
                    return None
                start = max(0, size - length)
                return start, size - 1
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        except ValueError:
            return None
        if start >= size:
            return (-1, -1)                          # signals 416
        return start, min(end, size - 1)

    def _stream_file(self, path: Path, content_type: str, send_body: bool) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self._error(404, "file missing")
            return
        window = self._range_for(size)
        if window == (-1, -1):
            self._error(416, "range not satisfiable")
            return
        start, end = window if window else (0, max(0, size - 1))
        length = max(0, end - start + 1)
        status = 206 if window else 200
        extra = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        if window:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send_headers(status, content_type, length, extra)
        if not send_body or length == 0:
            return
        remaining = length
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ------------------------------------------------------------------ routes
    def _local(self, raw_path: str, send_body: bool) -> None:
        path = self.media.resolve_local(raw_path)
        if path is None:
            self._error(403, "path not allowed")
            return
        self._stream_file(path, _content_type_for(path), send_body)

    def _art(self, raw_path: str, send_body: bool) -> None:
        path = self.media.resolve_local(raw_path)
        if path is None:
            self._error(403, "path not allowed")
            return
        cover = self.media.cover_for(path)
        if cover is None:
            self._error(404, "no cover")
            return
        data, mime = cover
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _background(self, send_body: bool) -> None:
        path = self.media.background_path()
        if path is None or not path.is_file():
            self._error(404, "no background")
            return
        self._stream_file(path, _content_type_for(path), send_body)

    def _online(self, song_id: str, quality: str, send_body: bool) -> None:
        if not song_id:
            self._error(400, "missing song id")
            return
        upstream = self.media.resolve_online(song_id, quality)
        if not upstream:
            self._error(404, "no playable url")
            return
        self._proxy(upstream, send_body, retry=song_id, quality=quality)

    def _proxy(self, url: str, send_body: bool, *, retry: str | None = None,
               quality: str = "standard") -> None:
        headers = {
            "User-Agent": _UA,
            "Referer": "https://music.163.com/",
            "Accept": "*/*",
        }
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        try:
            response = self.media.session.get(url, headers=headers, stream=True,
                                              timeout=(10, 30))
        except requests.exceptions.RequestException as exc:
            log.warning("upstream error for %s: %s", url, exc)
            self._error(502, "upstream error")
            return

        # An expired signed url answers 403/410: refresh it once and retry.
        if response.status_code in (403, 410) and retry:
            response.close()
            fresh = self.media.resolve_online(retry, quality, force=True)
            if fresh and fresh != url:
                self._proxy(fresh, send_body, quality=quality)
                return

        if response.status_code not in (200, 206):
            response.close()
            self._error(response.status_code, "upstream refused")
            return

        length = response.headers.get("content-length")
        extra = {"Accept-Ranges": response.headers.get("accept-ranges", "bytes"),
                 "Cache-Control": "no-store"}
        content_range = response.headers.get("content-range")
        if content_range:
            extra["Content-Range"] = content_range
        self._send_headers(response.status_code, _content_type_for(Path(url)),
                           int(length) if length and length.isdigit() else None, extra)
        if not send_body:
            response.close()
            return
        try:
            for chunk in response.iter_content(CHUNK):
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            response.close()


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/16.6 Safari/605.1.15")


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    known = {
        ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".oga": "audio/ogg", ".opus": "audio/ogg", ".ape": "audio/x-ape",
        ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff", ".aif": "audio/aiff",
        ".alac": "audio/mp4", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
        ".bmp": "image/bmp", ".heic": "image/heic",
    }
    if suffix in known:
        return known[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class MediaServer:
    """Loopback media server shared by the whole app (singleton)."""

    _instance: MediaServer | None = None
    _lock = threading.Lock()

    def __new__(cls) -> MediaServer:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.token = secrets.token_urlsafe(16)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self._roots: list[Path] = []
        self._url_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._cache_lock = threading.Lock()
        self._covers = _CoverCache()
        self._api_provider: Callable[[], Any] | None = None
        self._background_provider: Callable[[], Path | None] | None = None
        self.session = requests.Session()
        self._crossfade_duration: float = 0.0

    # ------------------------------------------------------------------ config
    def configure(self, *, api_provider: Callable[[], Any] | None = None,
                  background_provider: Callable[[], Path | None] | None = None,
                  allowed_roots: Iterable[str | Path] | None = None) -> None:
        if api_provider is not None:
            self._api_provider = api_provider
        if background_provider is not None:
            self._background_provider = background_provider
        if allowed_roots is not None:
            self.set_allowed_roots(allowed_roots)

    def set_allowed_roots(self, roots: Iterable[str | Path]) -> None:
        """File requests are only served from inside these directories."""
        resolved: list[Path] = []
        for root in roots:
            try:
                path = Path(root).expanduser().resolve()
            except OSError:
                continue
            if path.is_dir() and path not in resolved:
                resolved.append(path)
        self._roots = resolved

    @property
    def allowed_roots(self) -> list[Path]:
        return list(self._roots)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/m/{self.token}"

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> str:
        """Bind the server (no-op when it already runs) and return its base url."""
        with self._lock:
            if self._server is None:
                handler = type("_BoundHandler", (_Handler,), {"media": self})
                self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                self._server.daemon_threads = True
                self.port = int(self._server.server_address[1])
                self._thread = threading.Thread(target=self._server.serve_forever,
                                                name="ncm-media", daemon=True)
                self._thread.start()
                log.info("media server listening on %s", self.base_url)
            return self.base_url

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:  # pragma: no cover
                log.debug("media server shutdown failed: %s", exc)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return self._server is not None

    # --------------------------------------------------------------------- urls
    def url_for_local(self, path: str | Path) -> str:
        return f"{self.base_url}/file?p={quote(str(path), safe='')}"

    def url_for_online(self, song_id: str, quality: str = "standard") -> str:
        return f"{self.base_url}/online/{quote(str(song_id), safe='')}?q={quote(quality)}"

    def url_for_cover(self, path: str | Path) -> str:
        return f"{self.base_url}/art?p={quote(str(path), safe='')}"

    def url_for_background(self) -> str:
        return f"{self.base_url}/bg"

    def url_for_health(self) -> str:
        return f"{self.base_url}/health"

    # ---------------------------------------------------------------- resolution
    def resolve_local(self, raw_path: str) -> Path | None:
        """Return the path when it lives under an allowed root, else ``None``."""
        if not raw_path:
            return None
        try:
            candidate = Path(raw_path).expanduser().resolve()
        except OSError:
            return None
        for root in self._roots:
            if candidate == root or root in candidate.parents:
                return candidate if candidate.is_file() else None
        return None

    def cover_for(self, path: Path) -> tuple[bytes, str] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        key = f"{path}:{stat.st_mtime_ns}"
        cached = self._covers.get(key)
        if cached is not None:
            return cached
        from src.core.localmusic import read_cover

        cover = read_cover(path)
        if cover is not None:
            self._covers.put(key, cover)
        return cover

    def background_path(self) -> Path | None:
        if self._background_provider is None:
            return None
        try:
            return self._background_provider()
        except Exception as exc:  # pragma: no cover - provider is ours
            log.debug("background provider failed: %s", exc)
            return None

    def resolve_online(self, song_id: str, quality: str, *, force: bool = False) -> str | None:
        """Resolve (and briefly cache) the CDN url of an online track."""
        key = (str(song_id), str(quality or "standard"))
        now = time.time()
        if not force:
            with self._cache_lock:
                cached = self._url_cache.get(key)
            if cached and cached[1] > now:
                return cached[0]
        api = self._api_provider() if self._api_provider else None
        if api is None:
            return None
        try:
            info = api.get_song_url_info(str(song_id), quality or "standard")
        except Exception as exc:
            log.warning("cannot resolve %s: %s", song_id, exc)
            return None
        if not info or not info.get("url"):
            return None
        with self._cache_lock:
            self._url_cache[key] = (info["url"], now + URL_TTL)
            self._prune_urls(now)
        return info["url"]

    def _prune_urls(self, now: float) -> None:
        """Drop expired entries, then the oldest ones past the cap (called locked)."""
        expired = [key for key, value in self._url_cache.items() if value[1] <= now]
        for key in expired:
            self._url_cache.pop(key, None)
        overflow = len(self._url_cache) - MAX_CACHED_URLS
        if overflow > 0:
            for key in list(self._url_cache)[:overflow]:
                self._url_cache.pop(key, None)

    def forget_online(self, song_id: str | None = None) -> None:
        with self._cache_lock:
            if song_id is None:
                self._url_cache.clear()
            else:
                for key in [k for k in self._url_cache if k[0] == str(song_id)]:
                    self._url_cache.pop(key, None)

    def set_crossfade_duration(self, seconds: float) -> None:
        """Set the crossfade duration in seconds (0 = disabled)."""
        self._crossfade_duration = max(0.0, min(float(seconds or 0), 12.0))

    @property
    def crossfade_duration(self) -> float:
        return self._crossfade_duration

    def stats(self) -> dict[str, Any]:
        with self._cache_lock:
            cached_urls = len(self._url_cache)
        return {
            "running": self.running,
            "port": self.port,
            "allowed_roots": [str(root) for root in self._roots],
            "cached_urls": cached_urls,
            # Rough memory picture of the caches, handy when tuning the player.
            "cached_covers": self._covers.count,
            "cached_cover_bytes": self._covers.size,
            "pid": os.getpid(),
        }


def get_media_server() -> MediaServer:
    """Return the process-wide media server."""
    return MediaServer()
