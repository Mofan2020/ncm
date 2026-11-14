"""The NetEase half of the aggregate search.

The user hit a bug where the aggregate search came back without songs, users
or playlists.  These tests pin down that every kind is asked for, that one
broken kind does not take the others down with it, and that the bridge hands
the frontend a payload it can render.
"""
from src.core.api import NeteaseAPI
from src.gui.bridge import GuiBridge

_TYPE_TO_KEY = {value: key for key, value in NeteaseAPI.SEARCH_TYPES.items()}


class _StubApi(NeteaseAPI):
    """Stands in for the NetEase API.

    ``failing`` names the type ids that blow up, the way a flaky endpoint
    does.  Every other kind answers with one row.
    """

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls = []

    def _eapi(self, path, data=None, **kwargs):
        payload = data or {}
        self.calls.append(payload)
        if payload.get("type") in self.failing:
            raise RuntimeError("endpoint exploded")
        key = _TYPE_TO_KEY.get(payload.get("type"), "songs")
        if key == "users":
            # Users come back under a different key than the type name.
            key = "userprofiles"
        return {"code": 200, "result": {key: [{"id": "1"}], "songCount": 1}}


def test_search_multi_asks_for_every_kind():
    result = NeteaseAPI.search_multi(_StubApi(), "周杰伦")
    assert result["songs"]
    assert result["playlists"]
    assert result["artists"]
    assert result["albums"]
    assert result["users"]


def test_one_broken_kind_does_not_take_the_songs_down():
    # The songs are the most important thing on that screen.  A broken
    # album search used to take the whole call down with it.
    api = _StubApi(failing={NeteaseAPI.SEARCH_TYPES["albums"]})
    result = NeteaseAPI.search_multi(api, "周杰伦")
    assert result["songs"], "a broken kind must not empty the songs"
    assert result["errors"], "the broken kind has to say why"


def test_search_multi_without_a_keyword_is_empty():
    result = NeteaseAPI.search_multi(_StubApi(), "   ")
    assert not result["songs"]
    assert result["errors"] == {}


class _BridgeStub(GuiBridge):
    def __init__(self, payload):
        super().__init__()
        self.api = type("A", (), {"search_multi": staticmethod(lambda *a, **k: payload)})()

    def _safe(self, *args, **kwargs):
        return self.api.search_multi()


def test_bridge_hands_the_users_to_the_frontend():
    payload = {
        "songs": [], "playlists": [], "artists": [], "albums": [], "mvs": [],
        "users": [{"userId": 639943, "profile": {"nickname": "周杰伦-",
                                                  "avatarUrl": "http://x/u.jpg"},
                   "playlistCount": 9}],
        "errors": {},
    }
    result = _BridgeStub(payload).search_multi("周杰伦")
    assert result["success"] is True
    users = result["results"]["users"]
    assert len(users) == 1
    assert users[0]["id"] == "639943"
    assert users[0]["name"] == "周杰伦-"
    assert users[0]["avatar_url"].endswith("u.jpg")
    assert users[0]["playlist_count"] == 9


def test_bridge_passes_the_errors_on():
    payload = {"songs": [], "playlists": [], "artists": [], "albums": [],
               "mvs": [], "users": [], "errors": {"songs": "boom"}}
    result = _BridgeStub(payload).search_multi("周杰伦")
    assert result["errors"]["songs"] == "boom"
