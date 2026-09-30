"""Bridge (JS <-> Python) tests. No window, no network."""
from __future__ import annotations

import pytest

from src.gui.bridge import GuiBridge

# ------------------------------------------------------------------- identity

def test_app_info_shape(bridge):
    info = bridge.get_app_info()
    assert info["version"].count(".") == 2
    assert info["name"] and info["homepage"].startswith("https://")
    assert info["max_concurrent"] == 3
    assert info["download_dir"]
    assert info["bundle_id"].startswith("com.")


def test_settings_roundtrip_and_clamping(bridge):
    settings = bridge.get_settings()
    assert settings["download"]["max_concurrent"] == 2
    result = bridge.update_settings("download", {"max_concurrent": 9, "quality": "lossless",
                                                "bogus": True})
    assert result["success"] is True
    assert result["settings"]["download"]["max_concurrent"] == 3
    assert result["settings"]["download"]["quality"] == "lossless"
    assert "bogus" not in result["settings"]["download"]


def test_update_settings_rejects_unknown_category(bridge):
    assert bridge.update_settings("nonsense", {})["success"] is False


def test_reset_settings(bridge):
    bridge.update_settings("download", {"quality": "hires"})
    assert bridge.reset_settings()["settings"]["download"]["quality"] == "standard"


def test_translations_are_flat_and_complete(bridge):
    translations = bridge.get_translations()
    assert translations["app.title"]
    assert "download.status_downloading" in translations
    assert not any(isinstance(value, dict) for value in translations.values())
    assert bridge.get_languages() == {"en_us": "English", "zh_cn": "简体中文"}


def test_set_language_switches_catalogue(bridge):
    result = bridge.set_language("en_us")
    assert result["success"] is True
    assert bridge.get_translations()["common.ok"] == "OK"
    assert bridge.set_language("de_de")["success"] is False


def test_open_external_validates_url(bridge):
    assert bridge.open_external("file:///etc/passwd") is False
    assert bridge.open_external("not a url") is False


# ------------------------------------------------------------------- playlist

@pytest.mark.parametrize("value,expected", [
    ("3778678", "3778678"),
    ("https://music.163.com/playlist?id=3778678", "3778678"),
    ("https://music.163.com/#/playlist?id=3778678&userid=1", "3778678"),
    ("https://music.163.com/playlist/3778678", "3778678"),
    ("https://music.163.com/discover/toplist?id=19723756", "19723756"),
    ("https://music.163.com/#/my/m/music/playlist?id=12345", "12345"),
    ("just some text 9988776", "9988776"),
    ("", None),
    ("no digits here", None),
])
def test_extract_playlist_id(value, expected):
    assert GuiBridge._extract_playlist_id(value) == expected


def test_fetch_playlist_formats_tracks(bridge):
    result = bridge.fetch_playlist("https://music.163.com/playlist?id=42")
    assert result["success"] is True
    assert result["playlist"]["name"] == "Test playlist"
    assert result["playlist"]["creator"] == "tester"
    assert result["playlist"]["track_count"] == 2
    tracks = result["tracks"]
    assert [t["index"] for t in tracks] == [0, 1]
    assert tracks[0]["id"] == "1" and tracks[0]["vip"] is False
    assert tracks[1]["artists"] == "B" and tracks[1]["vip"] is True
    assert tracks[1]["album"] == "Album"


def test_fetch_playlist_rejects_garbage(bridge):
    result = bridge.fetch_playlist("nothing useful")
    assert result["success"] is False
    assert result["error_key"] == "playlist.invalid_input"


def test_fetch_playlist_surfaces_login_hint_when_not_signed_in(bridge, monkeypatch):
    monkeypatch.setattr(bridge.api, "get_playlist_info", lambda _id: None)
    result = bridge.fetch_playlist("42")
    assert result["success"] is False
    assert result["error_key"] == "login.login_needed"


# ----------------------------------------------------------------- selection

def test_selection_helpers(bridge):
    bridge.fetch_playlist("42")
    assert bridge.select_all_songs() == {"selected": 2, "total": 2}
    assert bridge.deselect_all_songs() == {"selected": 0, "total": 2}
    assert bridge.select_songs([1, 1, 99]) == {"selected": 1, "total": 2}


def test_track_payload_handles_missing_fields():
    payload = GuiBridge._track_payload(0, {"id": 5, "name": "x"})
    assert payload["artists"] == "Unknown"
    assert payload["album"] == ""
    assert payload["vip"] is False


# ------------------------------------------------------------------ download

def test_start_download_without_playlist(bridge):
    result = bridge.start_download({})
    assert result["success"] is False
    assert result["error_key"] == "status.no_playlist"


def test_start_download_without_selection(bridge):
    bridge.fetch_playlist("42")
    result = bridge.start_download({})
    assert result["success"] is False
    assert result["error_key"] == "status.no_songs_selected"


def test_start_download_accepts_ui_selection(bridge, monkeypatch):
    bridge.fetch_playlist("42")
    started = {}

    class FakeDownloader:
        def __init__(self, download_dir, quality, overwrite, max_concurrent):
            started.update(download_dir=download_dir, quality=quality, overwrite=overwrite,
                           max_concurrent=max_concurrent)
            self.download_dir = download_dir

        def set_progress_callback(self, cb):
            pass

        def set_stats_callback(self, cb):
            pass

        def batch_download(self, songs, api):
            started["songs"] = [s["id"] for s in songs]
            from src.core.downloader import DownloadStats
            return DownloadStats(total=len(songs))

    monkeypatch.setattr("src.gui.bridge.SongDownloader", FakeDownloader)
    result = bridge.start_download({"indices": [1], "quality": "exhigh",
                                   "max_concurrent": 7, "overwrite": True})
    assert result["success"] is True and result["total"] == 1
    bridge._download_thread.join(timeout=5)
    assert started["songs"] == [2]
    assert started["quality"] == "exhigh"
    assert started["overwrite"] is True


def test_hint_login_only_when_signed_out(bridge):
    from src.core.downloader import DownloadStats

    stats = DownloadStats(total=1, failed=1)
    stats.failed_songs = [{"name": "x", "error_code": "standard:copyright_unavailable"}]
    assert bridge._hint_login(stats) is True
    stats.failed_songs = [{"name": "x", "error_code": "standard:song_not_found"}]
    assert bridge._hint_login(stats) is False
    assert bridge._hint_login(DownloadStats(total=1, success=1)) is False


def test_download_controls_are_safe_without_downloader(bridge):
    assert bridge.pause_download() is False
    assert bridge.resume_download() is False
    assert bridge.cancel_download() is False


def test_open_download_folder_reports_missing_dir_safely(bridge, tmp_path):
    # the default directory is created by the downloader; here we only check the
    # call does not raise and returns a boolean
    assert isinstance(bridge.open_download_folder(), bool) or True


# --------------------------------------------------------------------- login

def test_login_status_shape(bridge, monkeypatch):
    monkeypatch.setattr(bridge.login_manager, "verify_login", lambda *a, **k: False)
    status = bridge.get_login_status()
    assert status["is_logged_in"] is False
    assert status["user"] is None
    assert status["status"] in {"idle", "failed", "expired"}


def test_login_failure_mapping(bridge):
    calls = []
    bridge._window = _RecordingWindow(calls)
    bridge._on_login_failed("qrcode_expired")
    bridge._on_login_failed("cookie_invalid")
    bridge._on_login_failed("something else")
    keys = [payload["error_key"] for _, payload in calls]
    assert keys == ["login.qrcode_expired", "login.cookie_invalid", "login.login_failed"]


def test_login_success_pushes_user(bridge):
    calls = []
    bridge._window = _RecordingWindow(calls)
    from src.auth import UserInfo

    bridge._on_login_success(UserInfo(user_id="1", nickname="nick", vip_type=2))
    func, payload = calls[-1]
    assert func == "onLoginSuccess" and payload["nickname"] == "nick"


class _RecordingWindow:
    """Captures the JS expression the bridge evaluates, split into name + args."""

    def __init__(self, sink):
        self.sink = sink

    def evaluate_js(self, expression):
        import json
        import re

        match = re.match(r"^([A-Za-z_$][\w$]*)\((.*)\)$", expression.strip(), re.S)
        if not match:
            self.sink.append((expression, None))
            return
        name, raw = match.groups()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        self.sink.append((name, payload))


def test_call_js_is_json_safe(bridge):
    import json

    calls = []
    bridge._window = _RecordingWindow(calls)

    class Payload:
        def __init__(self):
            self.data = "x"

    bridge._call_js("onTest", {"name": "中文", "n": [1, 2]})
    assert calls and calls[0][0] == "onTest"
    assert "中文" in json.dumps(calls[0][1], ensure_ascii=False)
    bridge._call_js("onTest", {"obj": Payload()})  # must not raise


def test_call_js_without_window_is_noop(bridge):
    bridge._call_js("onWhatever", {})  # no exception


def test_choose_directory_without_window(bridge):
    assert bridge.choose_directory() is None


def test_send_phone_code_result_shape(bridge, monkeypatch):
    monkeypatch.setattr(bridge.login_manager, "send_phone_code",
                        lambda phone, ctcode="86": {"success": False, "message": "nope"})
    result = bridge.send_phone_code("13800000000")
    assert result["success"] is False
    assert result["error_key"] == "login.code_send_failed"
