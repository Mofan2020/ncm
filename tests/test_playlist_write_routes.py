"""Tests for the playlist write routes: the client identity and the
subscribe / unsubscribe route split.

Both were wrong until 2026-10-25 and both produced the same user-visible
symptom: "收藏失败：illegal request!".  See ``docs/notes.md`` section 21.
"""
from __future__ import annotations

import threading

from src.core import api as api_module

# --------------------------------------------------------------- the routes

def test_subscribe_and_unsubscribe_use_separate_routes():
    """``/api/playlist/subscribe`` with ``t=2`` is not the way to un-subscribe.

    The reference implementation picks the route by direction
    (``query.t == 1 ? 'subscribe' : 'unsubscribe'``), and the live API
    answers the combined shape with ``code 403 illegal request!``.
    """
    assert api_module.PLAYLIST_SUBSCRIBE_ENDPOINT == "/api/playlist/subscribe"
    assert api_module.PLAYLIST_UNSUBSCRIBE_ENDPOINT == "/api/playlist/unsubscribe"


def _real_api(bridge):
    from src.core.api import NeteaseAPI

    api = NeteaseAPI.__new__(NeteaseAPI)
    api.logger = bridge.logger
    api._host_index = 0
    api.settings = bridge.settings
    return api


def _write_api(bridge):
    """A NeteaseAPI with the state the write methods touch."""
    api = _real_api(bridge)
    api.stats = {"total_requests": 0, "successful_requests": 0,
                 "failed_requests": 0, "host_switches": 0}
    api._response_cache = {}
    api._playlist_cache = {}
    api._liked_music_playlists = set()
    # The write queue (see ``NeteaseAPI._eapi_write``).
    api._request_lock = threading.Lock()
    api._last_request_ts = 0.0
    api._write_lock = threading.Lock()
    api._last_write_ts = 0.0
    api._write_cooldown_until = 0.0
    api.login_manager = type("LM", (), {"user_info": type(
        "UI", (), {"user_id": "42"})()})()  # type: ignore[assignment]
    return api


def _capture(api):
    """Replace ``_eapi`` with a recorder that keeps the path and payload."""
    sent: dict = {}

    def fake_eapi(path, data=None, use_cache=True):
        sent["path"] = path
        sent["data"] = data
        return {"code": 200}

    api._eapi = fake_eapi
    return sent


def test_subscribing_goes_to_the_subscribe_route(bridge):
    api = _real_api(bridge)
    sent = _capture(api)

    api.subscribe_playlist("7", True)

    assert sent["path"] == "/api/playlist/subscribe"
    assert sent["data"]["id"] == "7"


def test_unsubscribing_goes_to_the_unsubscribe_route(bridge):
    """The bug: the old code sent ``t=2`` to the subscribe route."""
    api = _write_api(bridge)
    sent = _capture(api)

    api.subscribe_playlist("7", False)

    assert sent["path"] == "/api/playlist/unsubscribe"
    assert sent["data"]["id"] == "7"
    # No ``t`` field: the route carries the direction.
    assert "t" not in sent["data"]


def test_the_unsubscribe_route_is_actually_used(bridge):
    """Guard against a regression to the combined shape."""
    api = _write_api(bridge)
    sent = _capture(api)

    api.subscribe_playlist("7", False)

    assert sent["path"] != api_module.PLAYLIST_SUBSCRIBE_ENDPOINT


# ----------------------------------------------------------- the identity

def test_the_gated_routes_are_the_ones_that_need_the_identity():
    """Measured on 2026-10-25, one call each, against the live API.

    These routes answer ``code 403 illegal request!`` without a client
    identity in the cookie, and do the work with one.
    """
    assert api_module.IDENTITY_REQUIRED_ROUTES == frozenset({
        "/api/playlist/create",
        "/api/playlist/update/name",
        "/api/playlist/subscribe",
        "/api/playlist/unsubscribe",
        "/api/batch",
    })


def test_the_ungated_routes_are_left_alone():
    """The other playlist writes work without an identity.

    Sending one anyway is not free: on ``manipulate/tracks`` a partial
    identity made the answer ``code -462`` (risk control) where no
    identity at all answered normally.
    """
    for route in ("/api/playlist/remove",
                  "/api/playlist/manipulate/tracks",
                  "/api/playlist/cover/update"):
        assert route not in api_module.IDENTITY_REQUIRED_ROUTES, route


def test_the_identity_cookie_states_the_same_client_as_the_header():
    """The cookie has to be **complete** and it has to match the header.

    The eapi header states the official desktop client: ``os=osx``,
    ``appver=3.1.9``, ``buildver=3364``, ``brand=Apple``.  A cookie that
    states something else contradicts that header, and the risk engine can
    see the contradiction (measured 2026-10-25: ``manipulate/tracks``
    answered -462 with ``os`` alone and 200 with the full set).

    The cookie is built from the very header that goes into the signed
    body, so the two cannot drift apart.
    """
    from src.core.crypto import build_client_header, build_client_identity

    header = build_client_header()
    cookie = build_client_identity(header)

    # Every field the cookie states has to agree with the header.
    for field, value in cookie.items():
        assert header[field] == value, field
    # And the fields the risk engine looks at have to be there.
    for field in ("os", "appver", "buildver", "brand", "deviceId"):
        assert field in cookie, field


def test_the_identity_is_not_half_stated():
    """A half-stated identity is what draws the risk control."""
    from src.core.crypto import build_client_header, build_client_identity

    header = build_client_header()
    cookie = build_client_identity(header)
    for field in ("os", "appver", "buildver", "brand"):
        if field in header:
            assert field in cookie, field


def test_the_identity_does_not_contradict_itself():
    """The version number has to belong to the platform it is paired with.

    The identity used to be ``os=pc`` with ``appver=8.9.70`` -- 8.9.70 is
    the **Android** client's version, so a request claiming to be a Windows
    PC was carrying an Android release number.  Nothing broke because of
    it, but it is a contradiction for free, so it is gone.
    """
    from src.core.crypto import CLIENT_APPVER, CLIENT_BODY_OS, CLIENT_OS, build_client_header

    header = build_client_header()
    assert header["os"] == CLIENT_OS == "osx"
    assert header["appver"] == CLIENT_APPVER == "3.1.9"
    # The business os is upper case, the header's is lower case -- the
    # official client spells them differently on purpose.
    assert CLIENT_BODY_OS == "OSX"
    # The Android version number must not come back.
    assert "8.9.70" not in (header["appver"], header.get("buildver", ""))


def test_the_identity_is_decided_by_the_route_not_by_the_caller(bridge):
    """The identity belongs to the route, so no call site can forget it.

    It used to be a flag that every write method had to pass; a new
    write method would have silently sent no identity and answered
    ``403 illegal request!``.
    """
    from src.core.crypto import build_client_header

    api = _write_api(bridge)
    api._throttle = lambda: None
    session = _FakeSession()
    api.session = session  # type: ignore[assignment]

    api._eapi(api_module.PLAYLIST_SUBSCRIBE_ENDPOINT, {"id": "7"}, use_cache=False)

    cookie = session.posted[0]["cookies"]
    # The cookie repeats the header's fields, so the two cannot disagree.
    # ``requestId`` is the one field that is new on every request, so it is
    # checked for being there and looking right rather than for equality.
    header = build_client_header()
    for field, value in cookie.items():
        if field == "requestId":
            assert len(value) == 36 and value.count("-") == 4
            continue
        assert value in header.values(), field
    # And the fields the risk engine looks at have to be there.
    assert cookie["os"] == "osx"


def test_a_read_does_not_carry_the_identity(bridge):
    """A read must not look like a client write -- and the like route
    answers 524 when the request claims a PC identity."""
    api = _write_api(bridge)
    api._throttle = lambda: None
    session = _FakeSession()
    api.session = session  # type: ignore[assignment]

    api._eapi(api_module.LIKE_LIST_ENDPOINT, {"uid": "1"}, use_cache=False)

    assert session.posted[0]["cookies"] is None


def test_the_like_route_does_not_carry_the_identity(bridge):
    """The 524 fix (section 18) depends on this."""
    api = _write_api(bridge)
    api._throttle = lambda: None
    session = _FakeSession()
    api.session = session  # type: ignore[assignment]

    api._eapi(api_module.LIKE_ENDPOINT, {"trackId": "1", "like": "true"},
              use_cache=False)

    assert session.posted[0]["cookies"] is None


# --------------------------------------------------- the session is clean

def test_the_identity_never_reaches_the_session_jar(bridge):
    """The cookie is per request; the session keeps no identity.

    A session that carries ``os`` makes the like endpoint answer
    ``code 524`` (section 18), so this is the property that keeps the
    524 fix intact.
    """
    api = _write_api(bridge)
    api._throttle = lambda: None
    session = _FakeSession()
    api.session = session  # type: ignore[assignment]

    api._eapi(api_module.PLAYLIST_SUBSCRIBE_ENDPOINT, {"id": "7"},
              use_cache=False)

    from src.core.crypto import build_client_header

    cookie = session.posted[0]["cookies"]
    header = build_client_header()
    for field, value in cookie.items():
        if field == "requestId":
            continue
        assert value in header.values(), field
    # The session jar itself is untouched.
    assert session.cookies == {}


class _FakeSession:
    """A session that records what was posted."""

    def __init__(self):
        self.cookies: dict[str, str] = {}
        self.posted: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None, cookies=None):
        self.posted.append({"url": url, "data": data, "cookies": cookies})
        return _FakeResponse()


class _FakeResponse:
    status_code = 200
    text = '{"code":200}'

    def json(self):
        return {"code": 200}


# ------------------------------------------------ the batch outer code lie

def test_a_failed_inner_route_is_reported(bridge):
    """``/api/batch`` answers 200 on the outside whatever happened inside.

    A rename without the client identity came back as ``inner code 403
    illegal request!`` under a cheerful outer 200 -- so the app reported
    "renamed" and nothing had happened.
    """
    api = _write_api(bridge)
    api._eapi = lambda path, data=None, use_cache=True: {
        "code": 200,
        "/api/playlist/update/name": {"code": 403, "msg": "illegal request!"},
    }

    result = api.update_playlist("7", name="new")

    assert result["code"] == 403
    assert result["msg"] == "illegal request!"


def test_a_successful_batch_reports_200(bridge):
    api = _write_api(bridge)
    api._eapi = lambda path, data=None, use_cache=True: {
        "code": 200,
        "/api/playlist/update/name": {"code": 200},
    }

    result = api.update_playlist("7", name="new")

    assert result["code"] == 200


def test_a_batch_without_a_failing_inner_route_is_left_alone(bridge):
    api = _write_api(bridge)
    api._eapi = lambda path, data=None, use_cache=True: {"code": 200}

    result = api.update_playlist("7", name="new")

    assert result == {"code": 200}


# ------------------------------------------------------------ the messages

def test_the_two_directions_report_different_messages(bridge):
    """Un-subscribing must not say "subscribe failed"."""
    api = _write_api(bridge)
    api._eapi = lambda path, data=None, use_cache=True: {
        "code": 403, "msg": "illegal request!"}

    bridge.api = api
    result = bridge.subscribe_playlist("7", False)

    assert result["success"] is False
    assert result["error_key"] == "player.unsubscribe_failed"
    assert result["message"] == "illegal request!"


def test_subscribing_still_reports_the_subscribe_message(bridge):
    api = _write_api(bridge)
    api._eapi = lambda path, data=None, use_cache=True: {
        "code": 403, "msg": "illegal request!"}

    bridge.api = api
    result = bridge.subscribe_playlist("7", True)

    assert result["error_key"] == "player.subscribe_failed"


def test_both_message_keys_exist_in_every_language():
    """The frontend falls back to ``player.unsubscribe_failed``; without the
    key the toast shows the raw key."""
    import json
    from pathlib import Path

    i18n_dir = Path(__file__).resolve().parent.parent / "src" / "i18n"
    for code in ("zh_cn", "en_us"):
        data = json.loads((i18n_dir / f"{code}.json").read_text(encoding="utf-8"))
        assert "unsubscribe_failed" in data["player"], code
        assert data["player"]["unsubscribe_failed"].strip()
