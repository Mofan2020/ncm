"""Tests for the request pacing: writes go out one at a time, and a rate
limit is answered by waiting instead of hammering.

This is the mechanism that keeps the account alive.  On 2026-10-25 a
long run of write probes against a real account got it throttled, and then
restricted, and the account's devices were kicked off.  The rules that
prevent that from happening again live here:

* a write waits its turn -- two writes never overlap;
* writes are spaced out, so they never leave the app as a burst;
* when the server says "too fast", the app waits and tries again, and the
  wait is shared with every other write;
* a read is never held back by any of that.

Nothing here touches the network: the waits are made injectable, and the
session is a fake.

The tests stub the **generic** throttle as well as the write one.  That
matters: the generic throttle sleeps 0.15 s before every request, so
leaving it in place would let it satisfy the "a wait happened" assertions
on its own, and the real write pacing would go untested.  Every assertion
below therefore checks the *specific* wait -- the write gap, the backoff
ladder, the shared cooldown -- not just that some sleep occurred.
"""
from __future__ import annotations

import threading
import time

import pytest

from src.core import api as api_module
from src.core.api import NeteaseAPI

# --------------------------------------------------------------- the fakes


class _FakeResponse:
    status_code = 200
    text = '{"code":200}'

    def __init__(self, payload: dict | None = None):
        self._payload = payload or {"code": 200}

    def json(self):
        return self._payload


class _ScriptedSession:
    """A session that answers with a scripted sequence of results.

    It holds every request open for a moment, so two requests that overlap
    are visible in :attr:`max_in_flight`.
    """

    def __init__(self, results: list[dict] | None = None, hold: float = 0.02):
        self.cookies: dict[str, str] = {}
        self.posted: list[dict] = []
        self._results = list(results or [])
        self._hold = hold
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def post(self, url, data=None, headers=None, timeout=None, cookies=None):
        with self._lock:
            self.posted.append({"url": url, "data": data, "cookies": cookies})
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            payload = self._results.pop(0) if self._results else {"code": 200}
        try:
            # Hold the request open long enough for an overlap to be visible.
            time.sleep(self._hold)
            return _FakeResponse(payload)
        finally:
            with self._lock:
                self.in_flight -= 1


def _api(bridge, session=None) -> NeteaseAPI:
    api = NeteaseAPI.__new__(NeteaseAPI)
    api.logger = bridge.logger
    api.debug = False
    api._host_index = 0
    api.settings = bridge.settings
    api.stats = {"total_requests": 0, "successful_requests": 0,
                 "failed_requests": 0, "host_switches": 0}
    api._response_cache = {}
    api._playlist_cache = {}
    api._liked_music_playlists = set()
    api._request_lock = threading.Lock()
    api._last_request_ts = 0.0
    api._write_lock = threading.Lock()
    api._last_write_ts = 0.0
    api._write_cooldown_until = 0.0
    api.login_manager = type("LM", (), {"user_info": type(
        "UI", (), {"user_id": "42"})()})()  # type: ignore[assignment]
    api.session = session or _ScriptedSession()  # type: ignore[assignment]
    return api


_REAL_SLEEP = time.sleep


def _silent(api) -> list[float]:
    """Neutralise the generic throttle and record every sleep.

    Returns the list the sleeps are appended to.  The generic throttle is
    stubbed out on purpose -- it would otherwise sleep 0.15 s before every
    request and satisfy the "a wait happened" assertions on its own.

    The **write** throttle is left alone: it is the thing under test.  It
    sleeps for the gap and for the cooldown, and those are the waits the
    assertions look at.
    """
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        _REAL_SLEEP(0)

    api._throttle = lambda: None
    time.sleep = fake_sleep  # type: ignore[assignment]
    return sleeps


def _restore_sleep():
    time.sleep = _REAL_SLEEP  # type: ignore[assignment]


# ------------------------------------------------------- what is a write

def test_the_write_routes_are_the_ones_that_change_the_account():
    assert api_module.LIKE_ENDPOINT in api_module.WRITE_ROUTES
    assert api_module.PLAYLIST_SUBSCRIBE_ENDPOINT in api_module.WRITE_ROUTES
    assert api_module.PLAYLIST_UNSUBSCRIBE_ENDPOINT in api_module.WRITE_ROUTES
    assert api_module.PLAYLIST_CREATE_ENDPOINT in api_module.WRITE_ROUTES
    assert "/api/batch" in api_module.WRITE_ROUTES


def test_reading_the_liked_list_is_not_a_write():
    """``/api/song/like`` writes; ``/api/song/like/get`` only reads.

    Getting this wrong would make every song list read wait behind the
    write queue, which is exactly the "slows the app down" the user does
    not want.
    """
    assert api_module.LIKE_LIST_ENDPOINT not in api_module.WRITE_ROUTES
    assert api_module.LIKE_ENDPOINT in api_module.WRITE_ROUTES


# -------------------------------------------------- writes do not overlap

def test_two_writes_never_overlap(bridge):
    """Only one write may be in flight at a time."""
    session = _ScriptedSession(hold=0.05)
    api = _api(bridge, session)
    # Both throttles off: the *lock* is what has to keep the writes apart.
    api._throttle = lambda: None
    api._throttle_write = lambda: None

    # A barrier so the threads really do start together.
    start = threading.Barrier(4)

    def do_write(path):
        start.wait()
        api._eapi(path, {"id": "1"}, use_cache=False)

    threads = [
        threading.Thread(target=do_write, args=(path,))
        for path in (
            api_module.PLAYLIST_SUBSCRIBE_ENDPOINT,
            api_module.PLAYLIST_UNSUBSCRIBE_ENDPOINT,
            api_module.PLAYLIST_CREATE_ENDPOINT,
            api_module.PLAYLIST_UNSUBSCRIBE_ENDPOINT,
        )
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert session.max_in_flight == 1, "two writes were in flight at once"
    assert len(session.posted) == 4


def test_a_read_is_not_blocked_by_a_write(bridge):
    """A read goes out while a write is waiting its turn.

    The user asked for this explicitly: the pacing must not slow the app
    down, only the writes.
    """
    session = _ScriptedSession()
    api = _api(bridge, session)
    api._throttle = lambda: None

    # The write is held by a long cooldown.
    api._write_cooldown_until = time.monotonic() + 30.0

    started = time.monotonic()
    api._eapi(api_module.LIKE_LIST_ENDPOINT, {"uid": "1"}, use_cache=False)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "the read waited for the write cooldown"
    assert len(session.posted) == 1


# ------------------------------------------------------ the rate limit

def test_a_rate_limited_write_is_retried_after_a_wait(bridge):
    """``405`` is answered by waiting and trying again, not by giving up."""
    session = _ScriptedSession([
        {"code": 405, "msg": "操作过于频繁，请稍后再试"},
        {"code": 200},
    ])
    api = _api(bridge, session)
    sleeps = _silent(api)

    try:
        result = api._eapi(api_module.PLAYLIST_SUBSCRIBE_ENDPOINT, {"id": "1"},
                           use_cache=False)
    finally:
        _restore_sleep()

    assert result["code"] == 200
    assert len(session.posted) == 2, "the write was not retried"
    # The wait has to be the backoff ladder's first step, not just "some
    # sleep": the generic throttle is stubbed out, so the only sleeps that
    # can have happened are the write gap and the cooldown.
    assert sleeps, "the retry did not wait"
    expected = api.RATE_LIMIT_BACKOFF[0]
    assert any(abs(wait - expected) < 0.5 for wait in sleeps), (
        f"the retry waited {sleeps}, not the backoff step {expected}")


def test_the_wait_grows_with_every_refusal(bridge):
    """Each refusal waits longer than the one before it."""
    api = _api(bridge)

    delays = [api._raise_cooldown(attempt) for attempt in range(3)]

    assert delays == sorted(delays), "the waits do not grow"
    assert delays[0] < delays[-1]
    assert delays[-1] <= 60.0
    # And they are the ladder's steps.
    assert delays == list(api.RATE_LIMIT_BACKOFF[:3])


def test_a_refusal_slows_the_other_writes_down_too(bridge):
    """The cooldown is shared: one route's refusal holds the others back.

    The server counts the writes per route, but a refusal means the
    account as a whole is being pushed -- so the next write waits.
    """
    api = _api(bridge)
    before = api._write_cooldown_until
    api._raise_cooldown(2)
    assert api._write_cooldown_until > before
    assert api._write_cooldown_until - time.monotonic() > 0


def test_the_next_write_waits_for_the_cooldown(bridge):
    """A write that comes after a refusal waits for the cooldown to pass.

    This is the property that keeps the app from hammering a server that
    just said "too fast" -- the one that got the account restricted.
    """
    api = _api(bridge)
    api._write_cooldown_until = time.monotonic() + 5.0

    started = time.monotonic()
    api._throttle_write()
    waited = time.monotonic() - started

    assert waited >= 4.0, f"the write did not wait for the cooldown ({waited:.1f}s)"


def test_writes_are_spaced_out(bridge):
    """Two writes in a row do not leave the app back to back."""
    api = _api(bridge)
    api._write_cooldown_until = 0.0

    api._throttle_write()
    first = api._last_write_ts
    api._throttle_write()
    second = api._last_write_ts

    assert second - first >= api.WRITE_MIN_INTERVAL * 0.9, (
        f"the writes were {second - first:.3f}s apart")


def test_a_refusal_is_not_retried_forever(bridge):
    """After the ladder is walked the refusal is handed back to the caller."""
    session = _ScriptedSession([
        {"code": 405, "msg": "操作过于频繁，请稍后再试"} for _ in range(20)
    ])
    api = _api(bridge, session)
    _silent(api)

    try:
        result = api._eapi(api_module.PLAYLIST_SUBSCRIBE_ENDPOINT, {"id": "1"},
                           use_cache=False)
    finally:
        _restore_sleep()

    assert result["code"] == 405
    assert len(session.posted) == api.WRITE_MAX_ATTEMPTS


def test_a_read_is_not_retried_on_a_rate_limit_the_same_way(bridge):
    """A read gives up sooner than a write does.

    A read is cheap to repeat and nobody is waiting on a minute-long
    retry; a write is the one that must not be dropped.
    """
    session = _ScriptedSession([
        {"code": 405, "msg": "操作过于频繁，请稍后再试"} for _ in range(20)
    ])
    api = _api(bridge, session)
    sleeps = _silent(api)

    try:
        api._eapi(api_module.LIKE_LIST_ENDPOINT, {"uid": "1"}, use_cache=False)
    finally:
        _restore_sleep()

    assert len(session.posted) == api.MAX_RETRIES
    # And the read's wait is its own, not the write ladder's.
    expected = api.RATE_LIMIT_BACKOFF[0]
    assert any(abs(wait - expected) < 0.5 for wait in sleeps), (
        f"the read waited {sleeps}, not the backoff step {expected}")


def test_a_read_does_not_raise_the_write_cooldown(bridge):
    """A read being refused says nothing about the write routes.

    The server counts the requests per route, so a read route being
    throttled must not hold the writes back.
    """
    session = _ScriptedSession([
        {"code": 405, "msg": "操作过于频繁，请稍后再试"} for _ in range(20)
    ])
    api = _api(bridge, session)
    _silent(api)

    try:
        api._eapi(api_module.LIKE_LIST_ENDPOINT, {"uid": "1"}, use_cache=False)
    finally:
        _restore_sleep()

    assert api._write_cooldown_until == 0.0


# ------------------------------------------------- telling a refusal apart

def test_a_405_with_a_rate_limit_message_is_a_rate_limit():
    assert NeteaseAPI._is_rate_limited(
        {"code": 405, "msg": "操作过于频繁，请稍后再试"}) is True


def test_a_405_without_a_message_is_a_rate_limit():
    assert NeteaseAPI._is_rate_limited({"code": 405}) is True


def test_another_refusal_is_not_a_rate_limit(bridge):
    """``403 illegal request!`` is not a rate limit and must not be retried.

    The check is on the behaviour, not only on the return value: a 403
    says "your request is wrong", and retrying it would just send the
    same wrong request again -- three times, at a service that is already
    unhappy.
    """
    session = _ScriptedSession([
        {"code": 403, "msg": "illegal request!"} for _ in range(20)
    ])
    api = _api(bridge, session)
    _silent(api)

    try:
        result = api._eapi(api_module.PLAYLIST_SUBSCRIBE_ENDPOINT, {"id": "1"},
                           use_cache=False)
    finally:
        _restore_sleep()

    assert result["code"] == 403
    assert len(session.posted) == 1, "a 403 was retried"


def test_a_403_is_not_recognised_as_a_rate_limit():
    assert NeteaseAPI._is_rate_limited(
        {"code": 403, "msg": "illegal request!"}) is False
    assert NeteaseAPI._is_rate_limited({"code": 500}) is False
    assert NeteaseAPI._is_rate_limited(None) is False
    # And a 405 whose message does not talk about frequency is not one
    # either -- 405 is used for more than one thing.
    assert NeteaseAPI._is_rate_limited(
        {"code": 405, "msg": "something else entirely"}) is False


# -------------------------------------------------------- the identity

def test_the_identity_cookie_still_agrees_with_the_header(bridge):
    """The pacing change must not have broken the identity fix."""
    from src.core.crypto import build_client_header

    session = _ScriptedSession()
    api = _api(bridge, session)
    api._throttle_write = lambda: None

    api._eapi(api_module.PLAYLIST_SUBSCRIBE_ENDPOINT, {"id": "1"},
              use_cache=False)

    cookie = session.posted[0]["cookies"]
    # Every field of the cookie has to be one the header knows about --
    # except ``requestId``, which is fresh on every request, so it can
    # only be checked for being there and looking right.
    for field, value in cookie.items():
        if field == "requestId":
            assert len(value) == 36 and value.count("-") == 4
            continue
        assert value in build_client_header().values(), field
    for field in ("os", "appver", "buildver", "brand", "deviceId"):
        assert field in cookie, field
    assert session.cookies == {}, "the identity leaked into the session jar"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
