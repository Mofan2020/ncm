"""The caches must not outlive the changes they describe.

Every write the app makes to the account invalidates the cached answers
that the write made wrong.  Without this, the cache turns a successful
write into a lie: the song *was* liked on the server, but the app kept
answering from the minute-old cached id list, so the playlist never showed
the new song.

The tests here are written so that they FAIL without the invalidation --
they check the observable behaviour, not the presence of a method.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def api(isolated_home):
    """A NeteaseAPI with the HTTP layer replaced by a recording fake."""
    from src.core import api as api_module

    api_module._api_instance = None
    instance = api_module.NeteaseAPI()
    instance.calls = []

    # The fake replaces ``_eapi_once`` -- the single network attempt -- and
    # NOT ``_eapi``.  ``_eapi`` owns the cache, so faking it would take the
    # cache out of the picture and the tests would prove nothing.
    def fake_once(path, data=None):
        instance.calls.append((path, data))
        return instance.answers.get(path, {"code": 200})

    instance.answers = {}
    instance._eapi_once = fake_once
    instance._legacy = lambda *a, **k: None

    # The write methods need a signed-in account (create_playlist refuses
    # otherwise, and then nothing is invalidated).
    from src.auth.login import LoginStatus, UserInfo

    instance.login_manager._user_info = UserInfo(
        user_id="42", nickname="tester", avatar_url="", vip_type=0,
        cookie="", profile={})
    instance.login_manager._status = LoginStatus.SUCCESS

    yield instance
    api_module._api_instance = None


# --------------------------------------------------------------- the like path
def test_a_like_is_visible_in_the_liked_ids_right_away(api):
    """The ids the app reads must not be the ones from before the like."""
    api.answers["/api/song/like/get"] = {"code": 200, "ids": [1, 2]}

    assert api.get_liked_song_ids("42") == ["1", "2"]

    # The account likes a third song.  The cached id list must not survive.
    api.answers["/api/song/like/get"] = {"code": 200, "ids": [1, 2, 3]}
    api.set_song_like("3", True)

    assert api.get_liked_song_ids("42") == ["1", "2", "3"]


def test_an_unlike_is_visible_in_the_liked_ids_right_away(api):
    api.answers["/api/song/like/get"] = {"code": 200, "ids": [1, 2, 3]}

    assert api.get_liked_song_ids("42") == ["1", "2", "3"]

    api.answers["/api/song/like/get"] = {"code": 200, "ids": [1, 2]}
    api.set_song_like("3", False)

    assert api.get_liked_song_ids("42") == ["1", "2"]


def test_a_like_refreshes_the_liked_music_playlist(api):
    """The "liked music" playlist is the liked list under another name."""
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 77, "name": "我喜欢的音乐", "specialType": 5,
                     "tracks": [{"id": 1}], "trackIds": [{"id": 1}], "trackCount": 1},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}]}

    assert api.get_playlist_songs("77") == [{"id": 1}]

    # A like changes the contents of that playlist.
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 77, "name": "我喜欢的音乐", "specialType": 5,
                     "tracks": [{"id": 1}, {"id": 2}], "trackIds": [{"id": 1}, {"id": 2}],
                     "trackCount": 2},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}, {"id": 2}]}
    api.set_song_like("2", True)

    assert api.get_playlist_songs("77") == [{"id": 1}, {"id": 2}]


# -------------------------------------------------------- the playlist writes
def test_adding_a_track_shows_up_in_the_playlist(api):
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Mine", "tracks": [{"id": 1}],
                     "trackIds": [{"id": 1}], "trackCount": 1},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}]}

    assert api.get_playlist_songs("7") == [{"id": 1}]

    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Mine", "tracks": [{"id": 1}, {"id": 2}],
                     "trackIds": [{"id": 1}, {"id": 2}], "trackCount": 2},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}, {"id": 2}]}
    api.add_tracks_to_playlist("7", ["2"])

    assert api.get_playlist_songs("7") == [{"id": 1}, {"id": 2}]


def test_removing_a_track_disappears_from_the_playlist(api):
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Mine", "tracks": [{"id": 1}, {"id": 2}],
                     "trackIds": [{"id": 1}, {"id": 2}], "trackCount": 2},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}, {"id": 2}]}

    assert api.get_playlist_songs("7") == [{"id": 1}, {"id": 2}]

    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Mine", "tracks": [{"id": 1}],
                     "trackIds": [{"id": 1}], "trackCount": 1},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}]}
    api.remove_tracks_from_playlist("7", ["2"])

    assert api.get_playlist_songs("7") == [{"id": 1}]


def test_a_rename_shows_the_new_name(api):
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Old name", "tracks": [], "trackIds": []},
    }
    assert api.get_playlist_info("7")["name"] == "Old name"

    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "New name", "tracks": [], "trackIds": []},
    }
    api.update_playlist("7", name="New name")

    assert api.get_playlist_info("7")["name"] == "New name"


def test_a_new_playlist_shows_up_in_the_account_list(api):
    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False,
        "playlist": [{"id": 1, "name": "Old"}],
    }
    assert [p["id"] for p in api.get_user_playlists_all("42")] == [1]

    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False,
        "playlist": [{"id": 1, "name": "Old"}, {"id": 2, "name": "New"}],
    }
    api.create_playlist("New")

    assert [p["id"] for p in api.get_user_playlists_all("42")] == [1, 2]


def test_a_deleted_playlist_disappears_from_the_account_list(api):
    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False,
        "playlist": [{"id": 1, "name": "Old"}, {"id": 2, "name": "Doomed"}],
    }
    assert len(api.get_user_playlists_all("42")) == 2

    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False,
        "playlist": [{"id": 1, "name": "Old"}],
    }
    api.delete_playlist(["2"])

    assert [p["id"] for p in api.get_user_playlists_all("42")] == [1]


def test_subscribing_shows_the_playlist_in_the_account_list(api):
    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False, "playlist": [{"id": 1, "name": "Old"}],
    }
    assert len(api.get_user_playlists_all("42")) == 1

    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False,
        "playlist": [{"id": 1, "name": "Old"}, {"id": 9, "name": "New"}],
    }
    api.subscribe_playlist("9", True)

    assert len(api.get_user_playlists_all("42")) == 2


# -------------------------------------------------------------- the metadata
def test_the_playlist_name_on_disk_is_refreshed_after_a_rename(api):
    """The on-disk copy is what survives a restart; it must not be stale."""
    from src.core.metacache import get_meta_cache

    disk = get_meta_cache()
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Old name", "tracks": [], "trackIds": []},
    }
    api.get_playlist_info("7")
    assert disk.get_meta("playlist:7")["name"] == "Old name"

    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "New name", "tracks": [], "trackIds": []},
    }
    api.update_playlist("7", name="New name")

    stored = disk.get_meta("playlist:7")
    assert stored is None or stored["name"] == "New name"


def test_the_track_list_on_disk_is_dropped_after_a_write(api):
    from src.core.metacache import get_meta_cache

    disk = get_meta_cache()
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Mine", "tracks": [{"id": 1}],
                     "trackIds": [{"id": 1}], "trackCount": 1},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}]}
    api.get_playlist_songs("7")
    assert disk.get_playlist("tracks:7") is not None

    api.add_tracks_to_playlist("7", ["2"])

    assert disk.get_playlist("tracks:7") is None


# ------------------------------------------------------------------ the scope
def test_the_scope_is_derived_from_the_request(api):
    assert api._scope_for("/api/song/like/get", {"uid": 42}) == "liked:42"
    assert api._scope_for("/api/user/playlist", {"uid": 42}) == "userplaylists:42"
    assert api._scope_for("/api/v6/playlist/detail", {"id": 7}) == "playlist:7"
    assert api._scope_for("/api/cloudsearch/pc", {"s": "x"}) == ""


def test_two_scopes_do_not_share_one_cache_entry(api):
    api.answers["/api/song/like/get"] = {"code": 200, "ids": [1]}
    assert api.get_liked_song_ids("42") == ["1"]

    api.answers["/api/song/like/get"] = {"code": 200, "ids": [2]}
    # A different account must not see the first account's answer.
    assert api.get_liked_song_ids("99") == ["2"]


# ------------------------------------------------------------------ fresh =
def test_fresh_reads_bypass_the_cache(api):
    api.answers["/api/song/like/get"] = {"code": 200, "ids": [1]}
    assert api.get_liked_song_ids("42") == ["1"]

    api.answers["/api/song/like/get"] = {"code": 200, "ids": [1, 2]}
    assert api.get_liked_song_ids("42", use_cache=False) == ["1", "2"]


def test_fresh_playlist_reads_bypass_the_cache(api):
    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Mine", "tracks": [{"id": 1}],
                     "trackIds": [{"id": 1}], "trackCount": 1},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}]}
    assert api.get_playlist_songs("7") == [{"id": 1}]

    api.answers["/api/v6/playlist/detail"] = {
        "code": 200,
        "playlist": {"id": 7, "name": "Mine", "tracks": [{"id": 1}, {"id": 2}],
                     "trackIds": [{"id": 1}, {"id": 2}], "trackCount": 2},
    }
    api.answers["/api/v3/song/detail"] = {"code": 200, "songs": [{"id": 1}, {"id": 2}]}
    assert api.get_playlist_songs("7", fresh=True) == [{"id": 1}, {"id": 2}]


def test_fresh_user_playlists_bypass_the_cache(api):
    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False, "playlist": [{"id": 1, "name": "Old"}],
    }
    assert len(api.get_user_playlists_all("42")) == 1

    api.answers["/api/user/playlist"] = {
        "code": 200, "more": False,
        "playlist": [{"id": 1, "name": "Old"}, {"id": 2, "name": "New"}],
    }
    assert len(api.get_user_playlists_all("42", fresh=True)) == 2


# ------------------------------------------------------------- the disk cache
def test_the_disk_cache_can_drop_everything_with_one_prefix(isolated_home):
    from src.core.metacache import get_meta_cache

    disk = get_meta_cache()
    disk.set_playlist("userplaylists:42:100:0", {"a": 1})
    disk.set_playlist("userplaylists:42:100:100", {"a": 2})
    disk.set_playlist("userplaylists:99:100:0", {"a": 3})
    disk.set_playlist("tracks:7", [1])

    removed = disk.discard_prefix("playlist", "userplaylists:42:")

    assert removed == 2
    assert disk.get_playlist("userplaylists:42:100:0") is None
    assert disk.get_playlist("userplaylists:42:100:100") is None
    assert disk.get_playlist("userplaylists:99:100:0") == {"a": 3}
    assert disk.get_playlist("tracks:7") == [1]


def test_the_disk_cache_drops_unreadable_files_too(isolated_home):
    """A file we cannot read is a file we cannot trust."""
    from src.core.metacache import get_meta_cache

    disk = get_meta_cache()
    disk.set_playlist("userplaylists:42:100:0", {"a": 1})
    broken = disk.root / "playlist" / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    removed = disk.discard_prefix("playlist", "userplaylists:42:")

    assert removed == 2
    assert not broken.exists()
