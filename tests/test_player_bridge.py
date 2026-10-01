"""The player half of the JS bridge: sources, playback, favourites, session."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_fixtures import build_mp3  # noqa: E402

ONLINE_TRACK = {"key": "online:1", "source": "online", "id": "1", "name": "One",
                "artists": "A", "album": "Album", "duration": 1000, "cover_url": ""}


def _logged_in(bridge, user_id="12345"):
    """Swap in a signed-in login manager (the real one needs the network)."""
    bridge.login_manager = SimpleNamespace(
        user_info=SimpleNamespace(user_id=user_id, nickname="tester"),
        is_logged_in=True,
    )


def _local_folder(bridge, tmp_path, *, title="One", artist="A", name="one.mp3",
                  lyrics=None):
    folder = tmp_path / "music"
    path = build_mp3(folder / name, title=title, artist=artist, lyrics=lyrics or "")
    bridge.settings.update_playback(local_dirs=[str(folder)])
    bridge.scan_local_music()
    return path


# --------------------------------------------------------------- settings

def test_playback_settings_round_trip(bridge):
    result = bridge.update_settings("playback", {
        "prefer_online": False, "online_quality": "lossless", "play_mode": "shuffle",
        "volume": 0.42, "resume_playback": False, "report_play_count": True,
        "local_dirs": ["/tmp/music", "  ", "/tmp/other"],
        "show_translation": False,
    })

    assert result["success"] is True
    playback = result["settings"]["playback"]
    assert playback["prefer_online"] is False
    assert playback["online_quality"] == "lossless"
    assert playback["play_mode"] == "shuffle"
    assert playback["volume"] == 0.42
    # Blank directories are dropped, duplicates are not introduced
    assert playback["local_dirs"] == ["/tmp/music", "/tmp/other"]


def test_invalid_play_mode_falls_back_to_list(bridge):
    result = bridge.update_settings("playback", {"play_mode": "reverse"})

    assert result["settings"]["playback"]["play_mode"] == "list"
    assert bridge.settings.playback.play_mode == "list"


def test_volume_is_clamped(bridge):
    bridge.update_settings("playback", {"volume": 4})

    assert bridge.settings.playback.volume == 1.0


# ------------------------------------------------------------- resolution

def test_prefers_online_when_configured(bridge):
    bridge.update_settings("playback", {"prefer_online": True})

    result = bridge.resolve_track(ONLINE_TRACK)

    assert result["success"] is True
    assert result["kind"] == "online"
    assert result["quality"] == bridge.settings.playback.online_quality
    assert "/online/1" in result["url"]


def test_prefers_a_matching_local_copy(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path)
    bridge.update_settings("playback", {"prefer_online": False})

    result = bridge.resolve_track(ONLINE_TRACK)

    assert result["success"] is True
    assert result["kind"] == "local"
    assert result["path"] == str(path)
    assert result["matched_local"] is True
    assert result["url"].startswith(bridge.media.base_url)


def test_falls_back_to_online_without_a_local_copy(bridge, tmp_path):
    _local_folder(bridge, tmp_path, title="Something else", artist="B")
    bridge.update_settings("playback", {"prefer_online": False})

    result = bridge.resolve_track(ONLINE_TRACK)

    assert result["kind"] == "online"


def test_local_preference_plays_a_local_track_directly(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path)
    bridge.update_settings("playback", {"prefer_online": True})   # irrelevant here

    result = bridge.resolve_track({"key": f"local:{path}", "source": "local",
                                   "path": str(path), "name": "One"})

    assert result["success"] is True
    assert result["kind"] == "local"
    assert result["matched_local"] is False


def test_missing_file_reports_an_error(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path)
    path.unlink()
    bridge.update_settings("playback", {"prefer_online": False})

    result = bridge.resolve_track(ONLINE_TRACK)

    # No local copy left, but the online version still resolves.
    assert result["kind"] == "online"

    bridge.api.get_song_url_info = lambda *a, **k: None
    bridge.api.last_url_error = "lossless:song_not_found"
    failed = bridge.resolve_track(ONLINE_TRACK)
    assert failed["success"] is False
    assert failed["error_key"] == "error.reason.song_not_found"


# ------------------------------------------------------------ play counts

def test_record_play_counts_locally_without_login(bridge):
    result = bridge.record_play(ONLINE_TRACK)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["reported"] is False          # not signed in -> no cloud report

    assert bridge.record_play(ONLINE_TRACK)["count"] == 2
    assert bridge.get_play_stats()["counts"] == {"online:1": 2}


def test_record_play_reports_to_netease_when_logged_in(bridge):
    _logged_in(bridge)
    bridge.update_settings("playback", {"report_play_count": True})

    result = bridge.record_play(ONLINE_TRACK)

    assert result["reported"] is True
    for _ in range(50):
        if getattr(bridge.api, "reported", None):
            break
        time.sleep(0.02)          # the report runs on a background thread
    assert bridge.api.reported[0] == "1"
    assert bridge.api.reported[1] == 1.0      # 1000 ms -> 1 s


def test_reset_play_counts(bridge):
    bridge.record_play(ONLINE_TRACK)
    result = bridge.reset_play_counts()

    assert result["stats"]["total_plays"] == 0
    assert bridge.get_play_stats()["counts"] == {}


def test_recent_tracks_are_listed(bridge):
    bridge.record_play(ONLINE_TRACK)

    tracks = bridge.get_recent_tracks(10)["tracks"]

    assert len(tracks) == 1
    assert tracks[0]["key"] == "online:1"
    assert tracks[0]["name"] == "One"
    assert tracks[0]["played"] > 0


# ------------------------------------------------------------ favourites

def test_favourite_without_login_stays_local(bridge):
    result = bridge.set_favorite(ONLINE_TRACK, True)

    assert result["success"] is True
    assert result["favorite"] is True
    assert result["cloud"] == "login_required"
    assert bridge.get_favorites()["keys"] == ["online:1"]

    bridge.set_favorite(ONLINE_TRACK, False)
    assert bridge.get_favorites()["keys"] == []


def test_favourite_syncs_to_netease_when_logged_in(bridge):
    _logged_in(bridge)

    result = bridge.set_favorite(ONLINE_TRACK, True)

    assert result["cloud"] == "ok"
    assert bridge.api.liked == ("1", True)


def test_local_favourite_skips_the_cloud(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path)

    result = bridge.set_favorite({"key": f"local:{path}", "source": "local",
                                  "path": str(path), "name": "One"}, True)

    assert result["success"] is True
    assert result["cloud"] is None
    assert bridge.get_favorites()["keys"] == [f"local:{path}"]


# --------------------------------------------------------------- lyrics

def test_lyrics_from_a_sidecar_file(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path)
    path.with_suffix(".lrc").write_text("[00:01.00]hello\n[00:02.50]world", encoding="utf-8")

    result = bridge.get_track_lyrics({"key": f"local:{path}", "source": "local",
                                      "path": str(path), "name": "One"})

    assert result["has_lyrics"] is True
    assert result["source"] == "sidecar"
    assert [line["time"] for line in result["lines"]] == [1000, 2500]
    assert [line["text"] for line in result["lines"]] == ["hello", "world"]


def test_lyrics_from_the_embedded_tag(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path, lyrics="[00:03.00]tagged")

    result = bridge.get_track_lyrics({"key": f"local:{path}", "source": "local",
                                      "path": str(path), "name": "One"})

    assert result["source"] == "embedded"
    assert result["lines"][0]["text"] == "tagged"


def test_lyrics_looked_up_online_and_saved_on_request(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path, title="One", artist="A")

    result = bridge.get_track_lyrics({"key": f"local:{path}", "source": "local",
                                      "path": str(path), "name": "One", "artists": "A"})

    assert result["source"] == "netease"
    assert [line["text"] for line in result["lines"]] == ["hello", "world"]
    assert result["lyrics_path"] == ""            # nothing written without save=True

    saved = bridge.get_track_lyrics({"key": f"local:{path}", "source": "local",
                                     "path": str(path), "name": "One", "artists": "A"},
                                    save=True)
    assert saved["lyrics_path"].endswith(".lrc")
    assert Path(saved["lyrics_path"]).is_file()
    assert "[00:01.00]hello" in Path(saved["lyrics_path"]).read_text(encoding="utf-8")


def test_lyrics_for_an_online_track(bridge):
    result = bridge.get_track_lyrics(ONLINE_TRACK)

    assert result["source"] == "netease"
    assert len(result["lines"]) == 2


def test_lyrics_without_any_source(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path, title="Nothing", artist="Nobody")
    bridge.api.get_lyrics = lambda song_id: {"lyric": "", "translation": "", "code": 200}

    result = bridge.get_track_lyrics({"key": f"local:{path}", "source": "local",
                                      "path": str(path), "name": "Nothing",
                                      "artists": "Nobody"})

    assert result["success"] is True
    assert result["has_lyrics"] is False
    assert result["lines"] == []


# ----------------------------------------------------------- the session

def test_playback_session_round_trip(bridge):
    saved = bridge.save_playback_state({
        "queue": [ONLINE_TRACK, {"key": "online:2", "source": "online", "id": "2",
                                 "name": "Two", "artists": "B", "junk": "dropped"}],
        "index": 1, "position": 42.5, "mode": "shuffle",
        "source": {"kind": "playlist", "id": "9", "name": "List"},
    })

    assert saved == {"success": True, "stored": True}
    state = bridge.load_playback_state()["state"]

    assert state["index"] == 1
    assert state["position"] == 42.5
    assert state["mode"] == "shuffle"
    assert state["source"] == {"kind": "playlist", "id": "9", "name": "List"}
    assert len(state["queue"]) == 2
    # The queue is persisted, so it only keeps the fields a track needs
    assert "junk" not in state["queue"][1]
    assert state["queue"][1]["key"] == "online:2"


def test_session_index_is_clamped_and_junk_is_cleaned(bridge):
    bridge.save_playback_state({"queue": [ONLINE_TRACK], "index": 99, "position": "nope",
                                "mode": "sideways"})

    state = bridge.load_playback_state()["state"]

    assert state["index"] == 0
    assert state["position"] == 0.0
    assert state["mode"] == "list"


def test_empty_queue_clears_the_session(bridge):
    bridge.save_playback_state({"queue": [ONLINE_TRACK], "index": 0})
    assert bridge.load_playback_state()["state"] is not None

    result = bridge.save_playback_state({"queue": []})

    assert result == {"success": True, "stored": False}
    assert bridge.load_playback_state()["state"] is None

    bridge.save_playback_state({"queue": [ONLINE_TRACK], "index": 0})
    bridge.clear_playback_state()
    assert bridge.load_playback_state()["state"] is None


# ------------------------------------------------------------- sources

def test_playlists_need_a_login(bridge):
    result = bridge.get_playlists()

    assert result["success"] is False
    assert result["error_key"] == "player.login_required"


def test_playlists_come_from_the_account(bridge):
    _logged_in(bridge)

    result = bridge.get_playlists()

    assert result["success"] is True
    assert result["playlists"][0]["id"] == "7"
    assert result["playlists"][0]["name"] == "Fake list"
    assert result["playlists"][0]["creator"] == "tester"


def test_playlist_tracks_do_not_touch_the_download_tab(bridge):
    bridge.current_playlist = {"id": 42, "name": "Download tab"}
    bridge.playlist_songs = [{"id": "keep"}]

    result = bridge.get_playlist_tracks("42")

    assert result["success"] is True
    assert [track["name"] for track in result["tracks"]] == ["One", "Two"]
    assert result["tracks"][0]["key"] == "online:1"
    assert result["tracks"][0]["source"] == "online"
    assert bridge.playlist_songs == [{"id": "keep"}]        # untouched

    assert bridge.get_playlist_tracks("not-a-number")["success"] is False


def test_liked_tracks(bridge):
    assert bridge.get_liked_tracks()["error_key"] == "player.login_required"

    _logged_in(bridge)
    result = bridge.get_liked_tracks()

    assert result["success"] is True
    assert [track["id"] for track in result["tracks"]] == ["1"]


def test_search(bridge):
    result = bridge.search_tracks("anything", limit=10)

    assert result["success"] is True
    assert result["keyword"] == "anything"
    assert len(result["tracks"]) == 2

    assert bridge.search_tracks("   ")["success"] is False


def test_local_tracks_and_filtering(bridge, tmp_path):
    folder = tmp_path / "music"
    build_mp3(folder / "a.mp3", title="Alpha", artist="Ann")
    build_mp3(folder / "b.mp3", title="Beta", artist="Bob")
    bridge.settings.update_playback(local_dirs=[str(folder)])

    summary = bridge.scan_local_music()
    assert summary["tracks"] == 2

    everything = bridge.get_local_tracks()
    assert everything["total"] == 2

    filtered = bridge.get_local_tracks("beta")
    assert [track["name"] for track in filtered["tracks"]] == ["Beta"]
    assert filtered["total"] == 1

    assert bridge.get_local_stats()["directories"] == [str(folder)]

    removed = bridge.remove_local_folder(str(folder))
    assert removed["directories"] == []
    assert bridge.get_local_tracks()["total"] == 2      # the index is kept


# ------------------------------------------------------- quick download

def test_download_tracks_queues_online_songs(bridge, monkeypatch):
    captured = {}

    def fake_launch(songs, options):
        captured["songs"] = songs
        captured["options"] = options
        return {"success": True, "total": len(songs)}

    monkeypatch.setattr(bridge, "_launch_download", fake_launch)

    result = bridge.download_tracks([ONLINE_TRACK], {"quality": "lossless"})

    assert result == {"success": True, "total": 1}
    assert captured["options"] == {"quality": "lossless"}
    # The downloader needs the raw song shape, so it is re-read by id
    again = captured["songs"][0]
    assert again.get("ar") or again.get("artists")
    assert str(again["id"]) == "1"


def test_download_tracks_rejects_local_only_input(bridge, tmp_path):
    path = _local_folder(bridge, tmp_path)

    result = bridge.download_tracks([{"key": f"local:{path}", "source": "local",
                                      "path": str(path)}])

    assert result["success"] is False
    assert result["error_key"] == "player.download_local_only"


# ------------------------------------------------------------ appearance

def test_no_background_url_until_one_is_chosen(bridge):
    assert bridge.get_settings()["ui"]["background_url"] == ""

    assert bridge.clear_background_image()["background_url"] == ""


def test_background_url_needs_a_running_server(bridge, tmp_path):
    image = tmp_path / "wall.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    assert bridge.settings.set_background_image(image)      # stores a copy of the file

    # No media server yet -> no URL to hand to the webview
    assert bridge.background_url() == ""

    bridge.media.configure(background_provider=bridge.settings.background_path)
    bridge.media.start()
    try:
        url = bridge.background_url()
        assert url.endswith("/bg")
        assert bridge.get_settings()["ui"]["background_url"] == url
    finally:
        bridge.media.stop()


def test_refresh_allowed_roots_covers_downloads_and_music(bridge, tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    downloads = Path(bridge._download_dir())
    downloads.mkdir(parents=True, exist_ok=True)
    bridge.settings.update_playback(local_dirs=[str(folder)])

    roots = bridge.refresh_allowed_roots()

    assert str(folder) in roots
    assert str(downloads) in roots

    # A folder that no longer exists is not a usable root
    gone = tmp_path / "gone"
    gone.mkdir()
    bridge.settings.update_playback(local_dirs=[str(folder), str(gone)])
    bridge.refresh_allowed_roots()
    gone.rmdir()
    assert str(gone) not in bridge.refresh_allowed_roots()


def test_local_tracks_are_paged(bridge, tmp_path):
    folder = tmp_path / "music"
    for index in range(5):
        build_mp3(folder / f"{index}.mp3", title=f"Track {index}", artist="A")
    bridge.settings.update_playback(local_dirs=[str(folder)])
    bridge.scan_local_music()

    first = bridge.get_local_tracks("", 2, 0)
    second = bridge.get_local_tracks("", 2, 2)

    assert first["total"] == 5
    assert len(first["tracks"]) == 2
    assert second["offset"] == 2
    assert len(second["tracks"]) == 2
    assert {t["key"] for t in first["tracks"]} & {t["key"] for t in second["tracks"]} == set()

    # The filter runs before paging, so `total` counts the matches
    filtered = bridge.get_local_tracks("Track 3", 200, 0)
    assert filtered["total"] == 1
    assert filtered["tracks"][0]["name"] == "Track 3"


def test_cloud_reporting_is_off_by_default(bridge):
    """Nothing leaves the machine unless the user turns the switch on."""
    assert bridge.settings.playback.report_play_count is False
    _logged_in(bridge)

    result = bridge.record_play(ONLINE_TRACK)

    assert result["reported"] is False
    assert getattr(bridge.api, "reported", None) is None
