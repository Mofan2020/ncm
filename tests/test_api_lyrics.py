"""``NeteaseAPI.get_lyrics`` wiring: endpoint, payload, fallback, shape."""
from __future__ import annotations

from src.core.lyrics import LYRIC_ENDPOINT

LYRIC_RESPONSE = {
    "code": 200,
    "lrc": {"lyric": "[00:01.00] hi\n[00:05.00] there\n"},
    "tlyric": {"lyric": "[00:01.00] 嗨\n"},
    "klyric": {"lyric": ""},
}


def test_get_lyrics_uses_the_eapi_endpoint(fresh_api):
    calls = []

    def fake_eapi(path, data=None):
        calls.append((path, data))
        return LYRIC_RESPONSE

    fresh_api._eapi = fake_eapi
    result = fresh_api.get_lyrics("123")
    assert calls[0][0] == LYRIC_ENDPOINT
    assert calls[0][1] == {"id": 123, "lv": -1, "kv": -1, "tv": -1}
    assert result == {"lyric": LYRIC_RESPONSE["lrc"]["lyric"],
                      "translation": LYRIC_RESPONSE["tlyric"]["lyric"],
                      "code": 200}


def test_get_lyrics_accepts_a_non_numeric_id(fresh_api):
    seen = {}

    def fake_eapi(path, data=None):
        seen.update(data)
        return {"code": 200, "lrc": {"lyric": "x"}}

    fresh_api._eapi = fake_eapi
    fresh_api.get_lyrics("abc")
    assert seen["id"] == "abc"


def test_get_lyrics_falls_back_to_the_legacy_route(fresh_api):
    calls = []
    fresh_api._eapi = lambda path, data=None: None
    fresh_api._legacy = lambda endpoint, params=None, method="GET": (
        calls.append(endpoint) or {"code": 200, "lrc": {"lyric": "[00:01.00] hi\n"}})

    result = fresh_api.get_lyrics("123")
    assert calls == ["/song/lyric"]
    assert result is not None
    assert result["lyric"] == "[00:01.00] hi\n"
    assert result["translation"] == ""


def test_get_lyrics_returns_none_on_a_bad_code(fresh_api):
    fresh_api._eapi = lambda path, data=None: {"code": 404}
    fresh_api._legacy = lambda *args, **kwargs: None
    assert fresh_api.get_lyrics("123") is None


def test_get_lyrics_returns_none_when_everything_fails(fresh_api):
    fresh_api._eapi = lambda path, data=None: None
    fresh_api._legacy = lambda *args, **kwargs: None
    assert fresh_api.get_lyrics("123") is None


def test_get_lyrics_tolerates_missing_blocks(fresh_api):
    fresh_api._eapi = lambda path, data=None: {"code": 200}
    result = fresh_api.get_lyrics("123")
    assert result == {"lyric": "", "translation": "", "code": 200}


def test_get_lyrics_tolerates_a_broken_tlyric(fresh_api):
    fresh_api._eapi = lambda path, data=None: {
        "code": 200, "lrc": {"lyric": "x"}, "tlyric": "not-a-dict"}
    assert fresh_api.get_lyrics("123")["translation"] == ""
