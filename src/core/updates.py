"""Check GitHub for a newer release.

The app only *tells* the user -- it never downloads or installs anything, so
this module is deliberately small: ask the GitHub API what the latest release
is, compare it with the running version, and report a human readable result.

Every failure is non-fatal.  A network hiccup must never delay the start or
surface an error the user did not ask for: the caller shows the result only
when ``available`` is true.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from src.version import HOMEPAGE, __version__

log = logging.getLogger(__name__)

#: ``https://github.com/<owner>/<repo>`` -> the API endpoint for its releases.
REPO = HOMEPAGE.rstrip("/").split("github.com/")[-1]
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"

#: Re-checking more often than this is pointless (and unfriendly to the API).
CACHE_SECONDS = 6 * 60 * 60

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple[int, int, int]:
    """``"v3.2.10"`` -> ``(3, 2, 10)``.  Unparsable text sorts lowest."""
    if not text:
        return (0, 0, 0)
    match = _VERSION_RE.search(str(text))
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str = __version__) -> bool:
    """Whether ``candidate`` is a strictly newer version than ``current``.

    Compared numerically component by component, so ``3.2.10`` is newer than
    ``3.2.9`` (a plain string compare gets that backwards).
    """
    return parse_version(candidate) > parse_version(current)


def _pick_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the newest non-draft release.

    Pre-releases are included on purpose: this project ships feature builds as
    pre-releases, and a user running one should still be told about the next.
    """
    best: dict[str, Any] | None = None
    best_version = (0, 0, 0)
    for release in releases:
        if release.get("draft"):
            continue
        version = parse_version(release.get("tag_name") or release.get("name") or "")
        if version > best_version:
            best, best_version = release, version
    return best


def check_for_update(current: str = __version__, timeout: float = 8.0) -> dict[str, Any]:
    """Ask GitHub whether a newer release exists.

    Returns a dict with ``success``, ``available``, ``current``, ``latest``,
    ``url`` and ``notes``.  Never raises: an offline machine simply gets
    ``available: False``.
    """
    try:
        response = requests.get(
            RELEASES_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"ncm/{current}",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        releases = response.json()
    except (requests.RequestException, ValueError) as exc:
        log.info("update check failed: %s", exc)
        return {
            "success": False,
            "available": False,
            "current": current,
            "error": str(exc),
        }

    if not isinstance(releases, list):
        return {"success": False, "available": False, "current": current}

    release = _pick_release(releases)
    if release is None:
        return {"success": True, "available": False, "current": current}

    latest = release.get("tag_name") or ""
    available = is_newer(latest, current)
    return {
        "success": True,
        "available": available,
        "current": current,
        "latest": latest.lstrip("v"),
        "url": release.get("html_url") or f"{HOMEPAGE}/releases",
        "notes": (release.get("body") or "").strip()[:1500],
        "prerelease": bool(release.get("prerelease")),
    }


class UpdateChecker:
    """Caches the last answer so the UI can poll cheaply."""

    def __init__(self) -> None:
        self._checked_at = 0.0
        self._result: dict[str, Any] | None = None

    def check(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force and self._result is not None and now - self._checked_at < CACHE_SECONDS:
            return dict(self._result, cached=True)
        result = check_for_update()
        # Only remember a successful lookup; a failed one should be retried
        # next time instead of being cached as "no update".
        if result.get("success"):
            self._result = result
            self._checked_at = now
        return result
