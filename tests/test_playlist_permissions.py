"""Tests for the playlist permission model, the live write routes and the
richer favourite outcome."""
from __future__ import annotations

import json

# --------------------------------------------------------------- permissions

def _playlist(bridge, *, pid="7", creator="42", special=0, subscribed=False):
    """Put a playlist in the account's list, as the API returns it."""
    bridge.api.playlists = [{
        "id": pid,
        "name": "A list",
        "trackCount": 2,
        "specialType": special,
        "subscribed": subscribed,
        "creator": {"userId": creator, "nickname": "someone"},
    }]
    return bridge.api.playlists[0]


def test_a_playlist_of_somebody_else_can_only_be_unsubscribed(bridge):
    _logged_in(bridge)
    _playlist(bridge, creator="999", subscribed=True)

    payload = bridge._playlist_payload(bridge.api.playlists[0])

    assert payload["is_own"] is False
    assert payload["can_unsubscribe"] is True
    assert payload["can_rename"] is False
    assert payload["can_edit_tracks"] is False
    assert payload["can_delete"] is False


def test_an_own_playlist_can_be_edited_but_not_unsubscribed(bridge):
    _logged_in(bridge)
    _playlist(bridge, creator=bridge.login_manager.user_info.user_id)

    payload = bridge._playlist_payload(bridge.api.playlists[0])

    assert payload["is_own"] is True
    assert payload["can_rename"] is True
    assert payload["can_edit_tracks"] is True
    assert payload["can_delete"] is True
    # Un-subscribing your own playlist is not what the star means there.
    assert payload["can_unsubscribe"] is False


def test_the_liked_music_playlist_is_neither_renamed_nor_deleted(bridge):
    _logged_in(bridge)
    _playlist(bridge, creator=bridge.login_manager.user_info.user_id, special=5)

    payload = bridge._playlist_payload(bridge.api.playlists[0])

    assert payload["is_liked_music"] is True
    assert payload["can_rename"] is False
    assert payload["can_edit_tracks"] is False
    assert payload["can_delete"] is False


def test_renaming_somebody_elses_playlist_is_refused(bridge):
    _logged_in(bridge)
    _playlist(bridge, creator="999")

    result = bridge.update_playlist("7", {"name": "mine now"})

    assert result["success"] is False
    assert result["error_key"] == "player.playlist_not_owned"


def test_deleting_the_liked_music_playlist_is_refused(bridge):
    _logged_in(bridge)
    _playlist(bridge, creator=bridge.login_manager.user_info.user_id, special=5)

    result = bridge.delete_playlist(["7"])

    assert result["success"] is False
    assert result["error_key"] == "player.playlist_liked_locked"


def test_adding_tracks_to_somebody_elses_playlist_is_refused(bridge):
    _logged_in(bridge)
    _playlist(bridge, creator="999")

    result = bridge.add_tracks_to_playlist("7", ["1"])

    assert result["success"] is False
    assert result["error_key"] == "player.playlist_not_owned"


# ------------------------------------------------------------- write routes

def test_the_track_manipulation_route_is_the_live_one(bridge):
    """``/api/playlist/tracks/add`` answers 404; the live route is
    ``/api/playlist/manipulate/tracks`` with ``op``/``pid``/``trackIds``."""
    from src.core import api as api_module

    assert api_module.PLAYLIST_TRACKS_ENDPOINT == "/api/playlist/manipulate/tracks"
    assert api_module.PLAYLIST_UPDATE_ENDPOINT == "/api/playlist/update/name"
    assert api_module.PLAYLIST_DELETE_ENDPOINT == "/api/playlist/remove"


def _real_api(bridge):
    """The real NeteaseAPI with its HTTP layer replaced.

    The fake API in conftest implements only the read side; the write
    methods live on the real class, so they are tested there.
    """
    from src.core.api import NeteaseAPI

    api = NeteaseAPI.__new__(NeteaseAPI)
    api.logger = bridge.logger
    api._host_index = 0
    api.settings = bridge.settings
    return api


def test_adding_tracks_sends_the_documented_payload(bridge):
    sent = {}
    api = _real_api(bridge)

    def fake_eapi(path, data=None, use_cache=True):
        sent["path"] = path
        sent["data"] = data
        return {"code": 200}

    api._eapi = fake_eapi

    api.add_tracks_to_playlist("7", ["1", "2"])

    assert sent["path"] == "/api/playlist/manipulate/tracks"
    assert sent["data"]["op"] == "add"
    assert sent["data"]["pid"] == "7"
    assert json.loads(sent["data"]["trackIds"]) == ["1", "2"]
    assert sent["data"]["imme"] == "true"


def test_removing_tracks_uses_the_del_op(bridge):
    sent = {}
    api = _real_api(bridge)

    def fake_eapi(path, data=None, use_cache=True):
        sent["data"] = data
        return {"code": 200}

    api._eapi = fake_eapi

    api.remove_tracks_from_playlist("7", ["1"])

    assert sent["data"]["op"] == "del"


def test_renaming_goes_through_the_batch_route(bridge):
    sent = {}
    api = _real_api(bridge)

    def fake_eapi(path, data=None, use_cache=True):
        sent["path"] = path
        sent["data"] = data
        return {"code": 200}

    api._eapi = fake_eapi

    api.update_playlist("7", name="New name")

    assert sent["path"] == "/api/batch"
    assert "/api/playlist/update/name" in sent["data"]
    assert json.loads(sent["data"]["/api/playlist/update/name"]) == {
        "id": "7", "name": "New name"}


# --------------------------------------------------------- favourite outcome

def test_an_unsupported_track_is_reported_as_such(bridge):
    """``code 524`` is a refusal of that track, not a broken request."""
    _logged_in(bridge)
    bridge.api.like_result = "unsupported"

    result = bridge.set_favorite({"id": "1", "source": "online"}, True)

    assert result["success"] is True
    assert result["cloud"] == "unsupported"
    # The local favourite still happened.
    assert result["favorite"] is True


def test_a_rate_limited_request_is_reported_as_such(bridge):
    _logged_in(bridge)
    bridge.api.like_result = "rate_limited"

    result = bridge.set_favorite({"id": "1", "source": "online"}, True)

    assert result["cloud"] == "rate_limited"


def test_the_heart_covers_the_cloud_favourites_too(bridge):
    """Everything in the account's NetEase "liked music" shows a filled
    heart, not only the locally stored favourites."""
    _logged_in(bridge)
    bridge.library.set_favorite("online:local-one", True, {"name": "Local"})

    stats = bridge.get_play_stats()

    assert "online:local-one" in stats["favorites"]
    # The fake API reports "1" as liked in the cloud.
    assert "online:1" in stats["favorites"]
    # ...but the local list is reported separately.
    assert stats["local_favorites"] == ["online:local-one"]


# ------------------------------------------------------------------ helpers

def _logged_in(bridge):
    from src.auth.login import LoginStatus, UserInfo

    user = UserInfo(user_id="42", nickname="tester", avatar_url="",
                    vip_type=0, cookie="", profile={})
    bridge.login_manager._user_info = user
    bridge.login_manager._status = LoginStatus.SUCCESS
    return user
