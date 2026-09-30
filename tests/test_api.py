"""Tests for the quality/URL logic in the API client (no network)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core.api import LOGIN_REQUIRED_CODES, PLAY_URL_CODES, QUALITY_ORDER, quality_fallbacks

SRC = Path(__file__).resolve().parent.parent / "src"


def code_without_docstrings(path: Path) -> str:
    """Source text of a module with every docstring removed."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_quality_order_is_low_to_high():
    assert QUALITY_ORDER == ("standard", "higher", "exhigh", "lossless", "hires")


@pytest.mark.parametrize("requested,expected", [
    ("standard", ["standard"]),
    ("higher", ["higher", "standard"]),
    ("exhigh", ["exhigh", "higher", "standard"]),
    ("lossless", ["lossless", "exhigh", "higher", "standard"]),
    ("hires", ["hires", "lossless", "exhigh", "higher", "standard"]),
    ("bogus", ["standard"]),
])
def test_quality_fallback_chain(requested, expected):
    assert quality_fallbacks(requested) == expected


def test_fallback_chain_reaches_standard_for_every_level():
    for level in QUALITY_ORDER:
        assert quality_fallbacks(level)[-1] == "standard"
        assert quality_fallbacks(level)[0] == level


def test_play_url_codes_are_localised(fresh_i18n):
    flat = fresh_i18n.get_flat_translations("zh_cn")
    for name in PLAY_URL_CODES.values():
        assert f"error.reason.{name}" in flat, f"missing translation for {name}"


def test_login_required_codes():
    assert -475 in LOGIN_REQUIRED_CODES
    assert 301 in LOGIN_REQUIRED_CODES


def test_api_stats_shape(fresh_api):
    stats = fresh_api.get_request_stats()
    for key in ("total_requests", "successful_requests", "failed_requests", "host_switches"):
        assert key in stats


def test_dead_endpoints_are_not_referenced():
    """The removed NetEase routes must not come back (they answer 404)."""
    dead = ("/song/url/v1", "/song/url", "/v3/song/url", "/login/status",
            "/sent/verificationcode", "/homepage/block/page")
    for name in ("core/api.py", "auth/login.py"):
        code = code_without_docstrings(SRC / name)
        for endpoint in dead:
            assert f'"{endpoint}"' not in code, f"{name} uses dead endpoint {endpoint}"


def test_no_weapi_signing_anywhere():
    """weapi is blackholed on many networks -- eapi is the only channel used."""
    for name in ("core/api.py", "core/crypto.py", "auth/login.py"):
        code = code_without_docstrings(SRC / name)
        assert "weapi" not in code.lower(), f"{name} still references weapi"
        assert "/eapi/" not in code, "the '/eapi' URL prefix is added in one place only"


def test_eapi_prefix_is_built_in_the_client(fresh_api):
    assert fresh_api.eapi_host in fresh_api.EAPI_HOSTS
    assert fresh_api.LEGACY_HOST == "https://music.163.com"


def test_rate_limiting_is_configured(fresh_api):
    assert fresh_api.MIN_REQUEST_INTERVAL > 0, "requests must be paced or the API returns 400"
    assert fresh_api.MAX_RETRIES >= 2
    assert all(host.startswith("https://") for host in fresh_api.EAPI_HOSTS)


def test_playlist_cache_can_be_cleared(fresh_api):
    fresh_api._playlist_cache["1"] = (0.0, {"id": 1})
    fresh_api.clear_playlist_cache()
    assert fresh_api._playlist_cache == {}
