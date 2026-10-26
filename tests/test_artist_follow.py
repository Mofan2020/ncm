"""The artist page, the follow button and the list of followed artists.

The route shapes were verified live on 2026-10-26:

* ``/api/v1/artist/<id>``   -- the profile plus the hot songs
* ``/api/artist/albums/<id>`` -- the artist's own albums
* ``/api/artist/sub`` / ``/api/artist/unsub`` -- follow / unfollow
* ``/api/artist/sublist``   -- the followed artists (``code 301`` when
  the session is gone)
* ``/api/artist/follow/count/get`` -- the follow state of one artist

These tests pin the mapping from those answers to the shape the GUI
expects.  No network: the HTTP layer is replaced by a table.
"""
from __future__ import annotations

import logging

import pytest

from src.core.api import (
    ARTIST_ALBUMS_ENDPOINT,
    ARTIST_FOLLOW_COUNT_ENDPOINT,
    ARTIST_SUB_ENDPOINT,
    ARTIST_SUBLIST_ENDPOINT,
    ARTIST_UNSUB_ENDPOINT,
    WRITE_ROUTES,
    NeteaseAPI,
)


class _FakeApi(NeteaseAPI):
    """A NeteaseAPI whose eapi layer answers from a table."""

    def __init__(self, answers: dict[str, dict]) -> None:  # noqa: D107
        # Deliberately not calling super().__init__: the singleton would
        # touch the real session and the settings.
        self._answers = answers
        self.calls: list[tuple[str, dict]] = []
        self.debug = False
        self.logger = logging.getLogger("ncm.test.artist")

    def _eapi(self, path, data=None, use_cache=True):  # noqa: D401, ANN001
        self.calls.append((path, dict(data or {})))
        return self._answers.get(path)

    def _legacy(self, endpoint, params=None, method="GET", use_cache=True):  # noqa: ANN001
        self.calls.append((endpoint, dict(params or {})))
        return None

    def invalidate_artist_follow(self, artist_id: str = "") -> int:  # noqa: D102
        self.calls.append(("invalidate", {"artist_id": artist_id}))
        return 1


# --------------------------------------------------------------- the API layer


def test_artist_detail_folds_the_songs_and_the_albums_in():
    """The profile, the hot songs and the albums come back as one dict."""
    api = _FakeApi({
        "/api/v1/artist/6452": {
            "code": 200,
            "artist": {"id": 6452, "name": "Someone", "picUrl": "http://x/y.jpg"},
            "hotSongs": [{"id": 1, "name": "One"}],
        },
        f"{ARTIST_ALBUMS_ENDPOINT}/6452": {
            "code": 200,
            "hotAlbums": [{"id": 9, "name": "Album"}],
        },
    })
    detail = api.get_artist_detail("6452")
    assert detail["name"] == "Someone"
    assert detail["hotSongs"] == [{"id": 1, "name": "One"}]
    assert detail["albums"] == [{"id": 9, "name": "Album"}]


def test_artist_detail_without_an_id_never_calls_the_server():
    api = _FakeApi({})
    assert api.get_artist_detail("") is None
    assert api.calls == []


def test_artist_detail_survives_a_missing_artist():
    """A payload without an artist is not an exception, it is nothing."""
    api = _FakeApi({"/api/v1/artist/1": {"code": 200}})
    assert api.get_artist_detail("1") is None


def test_the_albums_use_the_path_route_not_the_flat_one():
    """``/api/artist/album`` answers ``code 400``; the path route works."""
    api = _FakeApi({
        f"{ARTIST_ALBUMS_ENDPOINT}/6452": {
            "code": 200,
            "hotAlbums": [{"id": 9, "name": "Album"}],
        },
    })
    albums = api.get_artist_albums("6452")
    assert albums == [{"id": 9, "name": "Album"}]
    assert api.calls[0][0] == f"{ARTIST_ALBUMS_ENDPOINT}/6452"


def test_the_albums_clamp_the_limit():
    api = _FakeApi({})
    api.get_artist_albums("6452", limit=9999)
    assert api.calls[0][1]["limit"] == 100


def test_the_follow_state_reads_the_counter_route():
    api = _FakeApi({
        ARTIST_FOLLOW_COUNT_ENDPOINT: {
            "code": 200,
            "data": {"isFollow": True, "fansCnt": 12, "followDayCnt": 3},
        },
    })
    state = api.get_artist_follow_state("6452")
    assert state["followed"] is True
    assert state["fans_count"] == 12
    assert state["follow_days"] == 3


def test_the_follow_state_of_an_unfollowed_artist():
    api = _FakeApi({
        ARTIST_FOLLOW_COUNT_ENDPOINT: {"code": 200, "data": {"isFollow": False}},
    })
    assert api.get_artist_follow_state("6452")["followed"] is False


def test_following_uses_the_sub_route_and_unfollowing_the_unsub_route():
    """The two directions are two routes, like the playlist pair."""
    api = _FakeApi({
        ARTIST_SUB_ENDPOINT: {"code": 200},
        ARTIST_UNSUB_ENDPOINT: {"code": 200},
    })
    assert api.set_artist_follow("6452", True) == "ok"
    assert api.set_artist_follow("6452", False) == "ok"
    requests = [call[0] for call in api.calls if call[0] != "invalidate"]
    assert requests == [ARTIST_SUB_ENDPOINT, ARTIST_UNSUB_ENDPOINT]


def test_following_drops_the_cached_answers_it_made_wrong():
    """The profile, the counter and the list all carry the follow state."""
    api = _FakeApi({ARTIST_SUB_ENDPOINT: {"code": 200}})
    api.set_artist_follow("6452", True)
    assert ("invalidate", {"artist_id": "6452"}) in api.calls


def test_a_rate_limited_follow_is_told_apart_from_a_failure():
    api = _FakeApi({ARTIST_SUB_ENDPOINT: {"code": 405}})
    assert api.set_artist_follow("6452", True) == "rate_limited"


def test_a_follow_without_a_session_is_told_apart():
    api = _FakeApi({ARTIST_SUB_ENDPOINT: {"code": 301}})
    assert api.set_artist_follow("6452", True) == "not_logged_in"


def test_an_unexpected_code_is_a_failure():
    api = _FakeApi({ARTIST_SUB_ENDPOINT: {"code": 500}})
    assert api.set_artist_follow("6452", True) == "failed"


def test_the_followed_list_keeps_the_count_and_the_has_more_flag():
    api = _FakeApi({
        ARTIST_SUBLIST_ENDPOINT: {
            "code": 200,
            "count": 42,
            "hasMore": True,
            "data": [{"id": 1, "name": "A"}],
        },
    })
    result = api.get_followed_artists(limit=25, offset=0)
    assert result["artists"] == [{"id": 1, "name": "A"}]
    assert result["count"] == 42
    assert result["has_more"] is True


def test_the_followed_list_keeps_the_code_so_the_caller_can_tell():
    """``code 301`` means "not signed in", not "follows nobody"."""
    api = _FakeApi({ARTIST_SUBLIST_ENDPOINT: {"code": 301}})
    result = api.get_followed_artists()
    assert result["code"] == 301
    assert result["artists"] == []


def test_the_followed_list_sends_the_paging():
    api = _FakeApi({})
    api.get_followed_artists(limit=25, offset=50)
    assert api.calls[0][1]["offset"] == 50
    assert api.calls[0][1]["limit"] == 25


def test_both_follow_routes_are_writes():
    """A write is sent on its own, so the routes have to be registered."""
    assert ARTIST_SUB_ENDPOINT in WRITE_ROUTES
    assert ARTIST_UNSUB_ENDPOINT in WRITE_ROUTES


# -------------------------------------------------------------- the bridge


class _BridgeApi:
    """The handful of API methods the artist bridge calls."""

    def __init__(self) -> None:  # noqa: D107
        self.followed = False
        self.follow_result = "ok"
        self.detail: dict | None = {
            "id": 6452,
            "name": "Someone",
            "picUrl": "http://x/y.jpg",
            "briefDesc": "A brief description",
            "albumSize": 3,
            "musicSize": 40,
            "followed": False,
            "hotSongs": [{"id": 1, "name": "One", "ar": [{"id": 2, "name": "A"}],
                          "al": {"id": 3, "name": "Album"}, "dt": 1000, "fee": 0}],
            "albums": [{"id": 9, "name": "Album", "picUrl": "http://x/a.jpg",
                        "publishTime": 1000, "size": 8}],
        }
        self.follow_state: dict = {"followed": False, "fans_count": 12, "follow_days": 0}
        self.followed_list: dict = {
            "artists": [{"id": 6452, "name": "Someone", "picUrl": "http://x/y.jpg",
                         "albumSize": 3, "mvSize": 1}],
            "count": 1,
            "has_more": False,
            "code": 200,
        }
        self.calls: list[tuple] = []

    def get_artist_detail(self, artist_id):
        self.calls.append(("detail", artist_id))
        return self.detail

    def get_artist_follow_state(self, artist_id):
        self.calls.append(("state", artist_id))
        return self.follow_state

    def set_artist_follow(self, artist_id, follow=True):
        self.calls.append(("follow", artist_id, follow))
        return self.follow_result

    def get_followed_artists(self, limit=25, offset=0):
        self.calls.append(("list", limit, offset))
        return self.followed_list


@pytest.fixture()
def artist_bridge(isolated_home):
    """A GuiBridge whose API answers from a table (no network)."""
    from src.auth import login as login_module
    from src.core import api as api_module
    from src.gui.bridge import GuiBridge

    login_module.LoginManager._instance = None
    api_module._api_instance = None
    instance = GuiBridge()
    instance.api = _BridgeApi()
    yield instance
    login_module.LoginManager._instance = None
    api_module._api_instance = None


def test_the_bridge_normalises_the_artist(artist_bridge):
    result = artist_bridge.get_artist_detail("6452")
    assert result["success"] is True
    assert result["artist"]["id"] == "6452"
    assert result["artist"]["name"] == "Someone"
    assert result["artist"]["cover_url"] == "http://x/y.jpg"
    assert result["artist"]["description"] == "A brief description"
    assert result["artist"]["followed"] is False
    assert result["hot_songs"][0]["name"] == "One"
    assert result["albums"][0]["name"] == "Album"


def test_the_bridge_reports_the_follow_state(artist_bridge):
    artist_bridge.api.follow_state = {"followed": True, "fans_count": 99, "follow_days": 5}
    result = artist_bridge.get_artist_detail("6452")
    assert result["followed"] is True
    assert result["fans_count"] == 99
    assert result["follow_days"] == 5


def test_the_bridge_rejects_a_bogus_artist_id(artist_bridge):
    result = artist_bridge.get_artist_detail("not-a-number")
    assert result["success"] is False
    assert result["error_key"] == "player.invalid_artist_id"
    assert artist_bridge.api.calls == []


def test_the_bridge_reports_a_missing_artist(artist_bridge):
    artist_bridge.api.detail = None
    result = artist_bridge.get_artist_detail("6452")
    assert result["success"] is False
    assert result["error_key"] == "player.artist_not_found"


def test_the_bridge_follows_and_unfollows(artist_bridge):
    assert artist_bridge.follow_artist("6452", True) == {"success": True, "followed": True}
    assert artist_bridge.follow_artist("6452", False) == {"success": True, "followed": False}
    assert artist_bridge.api.calls == [("follow", "6452", True), ("follow", "6452", False)]


def test_the_bridge_rejects_a_bogus_id_on_a_write(artist_bridge):
    result = artist_bridge.follow_artist("nope", True)
    assert result["success"] is False
    assert result["error_key"] == "player.invalid_artist_id"
    assert artist_bridge.api.calls == []


def test_a_rate_limited_follow_gets_its_own_message(artist_bridge):
    artist_bridge.api.follow_result = "rate_limited"
    result = artist_bridge.follow_artist("6452", True)
    assert result["success"] is False
    assert result["error_key"] == "player.artist_follow_busy"


def test_a_follow_without_a_session_gets_the_login_message(artist_bridge):
    artist_bridge.api.follow_result = "not_logged_in"
    result = artist_bridge.follow_artist("6452", True)
    assert result["success"] is False
    assert result["error_key"] == "player.login_required"


def test_a_failed_follow_is_told_apart_from_a_failed_unfollow(artist_bridge):
    artist_bridge.api.follow_result = "failed"
    assert artist_bridge.follow_artist("6452", True)["error_key"] == "player.artist_follow_failed"
    assert artist_bridge.follow_artist("6452", False)["error_key"] == "player.artist_unfollow_failed"


def test_the_followed_list_normalises_the_artists(artist_bridge):
    result = artist_bridge.get_followed_artists(25, 0)
    assert result["success"] is True
    assert result["artists"][0]["id"] == "6452"
    assert result["artists"][0]["cover_url"] == "http://x/y.jpg"
    assert result["count"] == 1
    assert result["has_more"] is False
    assert artist_bridge.api.calls == [("list", 25, 0)]


def test_every_artist_in_the_followed_list_is_marked_followed(artist_bridge):
    """The row would otherwise offer to follow an artist already followed."""
    result = artist_bridge.get_followed_artists(25, 0)
    assert all(artist["followed"] is True for artist in result["artists"])


def test_the_followed_list_without_a_session_asks_for_a_login(artist_bridge):
    artist_bridge.api.followed_list = {"artists": [], "count": 0, "has_more": False,
                                       "code": 301}
    result = artist_bridge.get_followed_artists()
    assert result["success"] is False
    assert result["error_key"] == "player.login_required"
    assert result["artists"] == []


def test_a_transport_failure_in_the_followed_list_is_an_error(artist_bridge):
    def boom(*args, **kwargs):
        raise OSError("no network")

    artist_bridge.api.get_followed_artists = boom
    result = artist_bridge.get_followed_artists()
    assert result["success"] is False
    assert result["error_key"] == "error.network"


# ------------------------------------------------------------ the cache scope


def test_the_follow_state_and_the_list_have_their_own_cache_scopes(fresh_api):
    """A follow changes the profile, the counter and the list."""
    assert fresh_api._scope_for(ARTIST_SUBLIST_ENDPOINT, {}) == "artistfollows"
    assert fresh_api._scope_for(ARTIST_FOLLOW_COUNT_ENDPOINT, {"id": 6452}) == "artist:6452"
    assert fresh_api._scope_for("/api/v1/artist/6452", {"id": 6452}) == "artist:6452"


def test_invalidating_a_follow_drops_the_list_and_the_artist(fresh_api):
    """A follow changes the artist profile, the counter and the list."""
    fresh_api._set_cached("k-list", {"code": 200}, path=ARTIST_SUBLIST_ENDPOINT,
                          scope="artistfollows")
    fresh_api._set_cached("k-artist", {"code": 200}, path="/api/v1/artist/6452",
                          scope="artist:6452")
    fresh_api._set_cached("k-other", {"code": 200}, path="/api/v1/artist/7",
                          scope="artist:7")
    assert fresh_api.invalidate_artist_follow("6452") == 2

    scopes = {entry[3] for entry in fresh_api._response_cache.values()}
    assert scopes == {"artist:7"}
