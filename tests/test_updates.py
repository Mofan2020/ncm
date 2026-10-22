"""The GitHub update check: parsing, comparison and failure handling."""
from __future__ import annotations

import pytest

from src.core import updates
from src.version import __version__


@pytest.mark.parametrize("text,expected", [
    ("v3.2.2", (3, 2, 2)),
    ("3.2.10", (3, 2, 10)),
    ("release-1.0.0", (1, 0, 0)),
    ("", (0, 0, 0)),
    ("nonsense", (0, 0, 0)),
])
def test_parse_version(text, expected):
    assert updates.parse_version(text) == expected


def test_comparison_is_numeric_not_lexicographic():
    """3.2.10 is newer than 3.2.9 -- a string compare gets this backwards."""
    assert updates.is_newer("v3.2.10", "3.2.9")
    assert not updates.is_newer("v3.2.9", "3.2.10")


def test_the_running_version_is_not_newer_than_itself():
    assert not updates.is_newer(f"v{__version__}", __version__)


def test_older_release_is_not_an_update():
    assert not updates.is_newer("v3.0.0", __version__)


def test_drafts_are_ignored_and_prereleases_are_considered():
    releases = [
        {"tag_name": "v9.0.0", "draft": True, "prerelease": False},
        {"tag_name": "v4.0.0", "draft": False, "prerelease": True},
        {"tag_name": "v3.1.0", "draft": False, "prerelease": False},
    ]
    picked = updates._pick_release(releases)
    assert picked["tag_name"] == "v4.0.0"


def test_pick_release_prefers_the_highest_version_not_the_first():
    releases = [
        {"tag_name": "v3.1.0", "draft": False},
        {"tag_name": "v3.10.0", "draft": False},
        {"tag_name": "v3.9.0", "draft": False},
    ]
    assert updates._pick_release(releases)["tag_name"] == "v3.10.0"


def test_no_releases_means_no_update():
    assert updates._pick_release([]) is None


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_a_newer_release_is_reported(monkeypatch):
    payload = [{
        "tag_name": "v99.0.0",
        "draft": False,
        "prerelease": False,
        "html_url": "https://example.invalid/r",
        "body": "notes",
    }]
    monkeypatch.setattr(updates.requests, "get", lambda *a, **k: _Response(payload))
    result = updates.check_for_update()
    assert result["success"] is True
    assert result["available"] is True
    assert result["latest"] == "99.0.0"


def test_the_current_release_is_not_reported_as_an_update(monkeypatch):
    payload = [{"tag_name": f"v{__version__}", "draft": False, "prerelease": False}]
    monkeypatch.setattr(updates.requests, "get", lambda *a, **k: _Response(payload))
    assert updates.check_for_update()["available"] is False


def test_a_network_failure_is_swallowed(monkeypatch):
    def explode(*args, **kwargs):
        raise updates.requests.ConnectionError("offline")

    monkeypatch.setattr(updates.requests, "get", explode)
    result = updates.check_for_update()
    assert result["success"] is False
    assert result["available"] is False


def test_garbage_json_is_swallowed(monkeypatch):
    monkeypatch.setattr(updates.requests, "get",
                        lambda *a, **k: _Response({"not": "a list"}))
    assert updates.check_for_update()["available"] is False


def test_the_cache_spares_the_api_a_second_call(monkeypatch):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return _Response([{"tag_name": f"v{__version__}", "draft": False}])

    monkeypatch.setattr(updates.requests, "get", get)
    checker = updates.UpdateChecker()
    checker.check()
    checker.check()
    assert len(calls) == 1
    checker.check(force=True)
    assert len(calls) == 2


def test_a_failed_check_is_not_cached(monkeypatch):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        raise updates.requests.ConnectionError("offline")

    monkeypatch.setattr(updates.requests, "get", get)
    checker = updates.UpdateChecker()
    checker.check()
    checker.check()
    assert len(calls) == 2, "a failure must be retried, not cached as 'no update'"
