"""Tests for the playlist write routes: the client identity and the
subscribe / unsubscribe route split.

Both were wrong until 2026-10-25 and both produced the same user-visible
symptom: "收藏失败：illegal request!".  See ``docs/notes.md`` section 21.
"""
from __future__ import annotations

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
    """The cookie has to be **complete**.

    The eapi header of this app carries ``os=pc`` and ``appver=8.9.70``.
    A cookie that states only ``os`` contradicts that header, and the risk
    engine answers ``code -462`` where the complete identity is accepted
    (measured 2026-10-25: ``manipulate/tracks`` answered -462 with ``os``
    alone and 200 with the full set).

    It is the same idea as the official clients, which put their whole
    eapi header into the cookie (``createHeaderCookie``).
    """
    from src.core.crypto import build_client_header

    header = build_client_header()
    assert api_module.WRITE_IDENTITY_COOKIE == {
        "os": "pc", "appver": "8.9.70", "channel": "netease"}
    # Every field the cookie states has to agree with the header.
    assert api_module.WRITE_IDENTITY_COOKIE["os"] == header["os"]
    assert api_module.WRITE_IDENTITY_COOKIE["appver"] == header["appver"]


def test_the_identity_is_not_half_stated():
    """A half-stated identity is what draws the risk control."""
    from src.core.crypto import build_client_header

    header = build_client_header()
    cookie = api_module.WRITE_IDENTITY_COOKIE
    for field in ("os", "appver"):
        if field in header:
            assert field in cookie, field


def test_the_identity_is_decided_by_the_route_not_by_the_caller(bridge):
    """The identity belongs to the route, so no call site can forget it.

    It used to be a flag that every write method had to pass; a new
    write method would have silently sent no identity and answered
    ``403 illegal request!``.
    """
    api = _write_api(bridge)
    api._throttle = lambda: None
    session = _FakeSession()
    api.session = session  # type: ignore[assignment]

    api._eapi(api_module.PLAYLIST_SUBSCRIBE_ENDPOINT, {"id": "7"}, use_cache=False)

    assert session.posted[0]["cookies"] == api_module.WRITE_IDENTITY_COOKIE


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

    assert session.posted[0]["cookies"] == api_module.WRITE_IDENTITY_COOKIE
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
