"""Downloader tests.

Everything runs against a local HTTP server, so the suite needs no network and
still exercises the real streaming / verification / cancel paths.
"""
from __future__ import annotations

import http.server
import socketserver
import threading
import time

import pytest

from src.core.downloader import MAX_CONCURRENT, DownloadStats, SongDownloader

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB deterministic body


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves /ok/<name> (full body), /short/<name> (truncated), /slow (throttled)."""

    protocol_version = "HTTP/1.1"
    server_version = "TestCDN/1.0"

    def log_message(self, *args):  # silence
        pass

    def _send(self, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/ok"):
            content_type = "audio/flac" if path.endswith(".flac") else "audio/mpeg"
            self._send(PAYLOAD, content_type)
        elif path.startswith("/short"):
            self._send(PAYLOAD[: len(PAYLOAD) // 3], "audio/mpeg")
        elif path.startswith("/slow"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            view = memoryview(PAYLOAD)
            for start in range(0, len(view), 16384):
                try:
                    self.wfile.write(view[start:start + 16384])
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.05)
        else:
            self.send_error(404)


class _Server:
    def __init__(self):
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(scope="module")
def cdn():
    server = _Server()
    yield server
    server.stop()


class FakeAPI:
    """Minimal stand-in for NeteaseAPI.get_song_url_info."""

    def __init__(self, base: str, mapping=None, quality="standard"):
        self.base = base
        self.mapping = mapping or {}
        self.quality = quality
        self.last_url_error = None
        self.calls = []

    def get_song_url_info(self, song_id, quality="standard"):
        self.calls.append(song_id)
        kind = self.mapping.get(str(song_id), "ok")
        if kind == "none":
            self.last_url_error = f"{quality}:copyright_unavailable"
            return None
        return {
            "url": f"{self.base}/{kind}/{song_id}.mp3",
            "level": quality,
            "br": 128000,
            "size": len(PAYLOAD),
            "type": "flac" if kind.endswith("flac") else "mp3",
            "code": 200,
        }


def songs(*ids):
    return [{"id": i, "name": f"Song {i}", "artists": [{"name": f"Artist {i}"}]} for i in ids]


# ------------------------------------------------------------------ unit level

def test_concurrency_is_capped_at_three(tmp_path):
    assert SongDownloader(tmp_path, max_concurrent=9).max_concurrent == MAX_CONCURRENT
    assert SongDownloader(tmp_path, max_concurrent=0).max_concurrent == 1
    assert SongDownloader(tmp_path, max_concurrent=3).max_concurrent == 3


@pytest.mark.parametrize("raw,expected", [
    ("Artist - Title", "Artist - Title"),
    ("a/b\\c:d*e?f\"g<h>i|j", "a-b-c-d-e-f-g-h-i-j"),
    ("  spaced   out  ", "spaced out"),
    ("", "Unknown"),
    ("x" * 400, "x" * 180),
])
def test_sanitize_filename(raw, expected):
    assert SongDownloader.sanitize_filename(raw) == expected


def test_build_filename_keeps_extension():
    assert SongDownloader.build_filename("A/B", "C:D", "mp3") == "A-B - C-D.mp3"
    assert SongDownloader.build_filename("A", "B", ".flac") == "A - B.flac"


@pytest.mark.parametrize("api_type,content_type,url,expected", [
    ("flac", "", "http://x/y.mp3", "flac"),          # the API wins
    ("MP3", "", "http://x/y.flac", "mp3"),
    ("", "audio/flac", "http://x/y", "flac"),
    ("", "audio/mpeg", "http://x/y", "mp3"),
    ("", "", "http://x/y.mp3?a=1", "mp3"),
    ("", "", "http://x/y", "mp3"),                    # default
    ("weird", "", "http://x/y.ogg", "ogg"),
])
def test_extension_resolution(api_type, content_type, url, expected):
    assert SongDownloader.extension_for(url, api_type, content_type) == expected


def test_stats_finished_counter():
    stats = DownloadStats(total=5, success=2, failed=1, skipped=1, cancelled=1)
    assert stats.finished == 5
    payload = stats.to_dict()
    assert payload["finished"] == 5 and payload["total"] == 5


# --------------------------------------------------------------- integration

def test_batch_download_writes_files(cdn, tmp_path):
    dl = SongDownloader(tmp_path, quality="standard", overwrite=True, max_concurrent=3)
    api = FakeAPI(cdn.base)
    stats = dl.batch_download(songs(1, 2, 3), api)

    assert stats.success == 3 and stats.failed == 0
    files = sorted(p.name for p in tmp_path.glob("*.mp3"))
    assert files == ["Artist 1 - Song 1.mp3", "Artist 2 - Song 2.mp3", "Artist 3 - Song 3.mp3"]
    for path in tmp_path.glob("*.mp3"):
        assert path.stat().st_size == len(PAYLOAD)
    assert not list(tmp_path.glob("*.part")), "temporary files must be cleaned up"
    assert stats.to_dict()["total_bytes"] == len(PAYLOAD) * 3


def test_skip_existing_files(cdn, tmp_path):
    api = FakeAPI(cdn.base)
    SongDownloader(tmp_path, overwrite=True, max_concurrent=2).batch_download(songs(1, 2), api)
    stats = SongDownloader(tmp_path, overwrite=False, max_concurrent=2).batch_download(songs(1, 2), api)
    assert stats.skipped == 2 and stats.success == 0


def test_overwrite_rewrites_files(cdn, tmp_path):
    api = FakeAPI(cdn.base)
    SongDownloader(tmp_path, overwrite=True, max_concurrent=1).batch_download(songs(1), api)
    target = tmp_path / "Artist 1 - Song 1.mp3"
    target.write_bytes(b"stale")
    stats = SongDownloader(tmp_path, overwrite=True, max_concurrent=1).batch_download(songs(1), api)
    assert stats.success == 1 and target.stat().st_size == len(PAYLOAD)


def test_failed_url_is_reported(cdn, tmp_path):
    api = FakeAPI(cdn.base, mapping={"9": "none"})
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=2)
    stats = dl.batch_download(songs(9), api)
    assert stats.failed == 1 and stats.success == 0
    assert "copyright_unavailable" in stats.failed_songs[0]["error_code"]
    report = tmp_path / "failed_downloads.txt"
    assert report.exists() and "Song 9" in report.read_text(encoding="utf-8")


def test_truncated_response_fails_and_leaves_no_part_file(cdn, tmp_path):
    api = FakeAPI(cdn.base, mapping={"5": "short"})
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1, timeout=10)
    stats = dl.batch_download(songs(5), api)
    assert stats.failed == 1
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.mp3")), "a truncated download must not be published"


def test_http_error_is_reported(cdn, tmp_path):
    api = FakeAPI(cdn.base, mapping={"7": "missing"})
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1, max_retries=1)
    stats = dl.batch_download(songs(7), api)
    assert stats.failed == 1 and stats.failed_songs[0]["error"]


def test_cancel_stops_downloads(cdn, tmp_path):
    api = FakeAPI(cdn.base, mapping={"1": "slow", "2": "slow"})
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=2)
    timer = threading.Timer(0.6, dl.cancel)
    timer.start()
    try:
        stats = dl.batch_download(songs(1, 2), api)
    finally:
        timer.cancel()
    assert stats.success == 0
    assert stats.cancelled + stats.failed == 2
    assert not list(tmp_path.glob("*.part")), "cancelled downloads must clean up"


def test_pause_and_resume(cdn, tmp_path):
    api = FakeAPI(cdn.base, mapping={"1": "slow"})
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1)
    runner = threading.Thread(target=dl.batch_download, args=(songs(1), api))
    runner.start()
    time.sleep(0.4)
    shown = []
    dl.set_progress_callback(shown.append)
    dl.pause()
    assert dl.is_paused()
    time.sleep(0.4)
    assert runner.is_alive(), "paused downloads must not finish"
    before = shown[-1].downloaded_size if shown else 0
    time.sleep(0.4)
    after = shown[-1].downloaded_size if shown else 0
    assert after - before <= 16384, "no new bytes may arrive while paused"
    dl.resume()
    assert not dl.is_paused()
    runner.join(timeout=30)
    assert not runner.is_alive()
    assert dl.is_cancelled() is False
    assert (tmp_path / "Artist 1 - Song 1.mp3").stat().st_size == len(PAYLOAD)


def test_progress_and_stats_callbacks_fire(cdn, tmp_path):
    api = FakeAPI(cdn.base)
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=2)
    seen_progress, seen_stats = [], []
    # snapshot the values: the downloader reuses one task object per song
    dl.set_progress_callback(lambda task: seen_progress.append(
        (task.status, task.progress, task.song_id)))
    dl.set_stats_callback(seen_stats.append)
    dl.batch_download(songs(1, 2), api)
    statuses = {status for status, _, _ in seen_progress}
    assert {"resolving", "downloading", "completed"} <= statuses
    assert seen_stats and seen_stats[-1].success == 2
    payload = dl.get_tasks()[0].to_dict()
    for key in ("index", "song_id", "name", "artists", "status", "progress",
                "speed", "downloaded_size", "total_size"):
        assert key in payload


def test_url_is_resolved_inside_the_worker(cdn, tmp_path):
    """No URL may be pre-fetched before the downloads start (start-up latency)."""
    api = FakeAPI(cdn.base)
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=3)
    dl.batch_download(songs(1, 2, 3, 4), api)
    assert sorted(api.calls) == ["1", "2", "3", "4"]
    assert len(dl.get_tasks()) == 4


def test_empty_batch_is_a_noop(tmp_path):
    dl = SongDownloader(tmp_path)
    stats = dl.batch_download([], FakeAPI("http://127.0.0.1:1"))
    assert stats.total == 0 and stats.to_dict()["finished"] == 0


def test_unwritable_dir_falls_back(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        dl = SongDownloader(blocked)
        # either we could still write (running as root) or we fell back
        assert dl.download_dir.exists()
    finally:
        blocked.chmod(0o700)


def test_filename_collision_same_artist_title(cdn, tmp_path):
    api = FakeAPI(cdn.base)
    songs_same = [{"id": 1, "name": "Same", "artists": [{"name": "A"}]},
                  {"id": 2, "name": "Same", "artists": [{"name": "A"}]}]
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1)
    stats = dl.batch_download(songs_same, api)
    assert stats.total == 2
    assert stats.success == 2
    assert (tmp_path / "A - Same.mp3").exists()
    assert not list(tmp_path.glob("*.part"))


# ---------------------------------------------------------------------- lyrics

class LyricsAPI(FakeAPI):
    """FakeAPI plus lyrics, so one stub covers both halves of a download."""

    def __init__(self, base, lyrics=None, error=None, **kwargs):
        super().__init__(base, **kwargs)
        self.lyrics = lyrics
        self.error = error
        self.lyrics_calls = []

    def get_lyrics(self, song_id):
        self.lyrics_calls.append(str(song_id))
        if self.error:
            raise self.error
        return self.lyrics


LRC_TEXT = {"lyric": "[00:01.00] line one\n[00:05.00] line two\n",
            "translation": "[00:01.00] 第一行\n", "code": 200}


def test_lyrics_are_written_next_to_the_audio(cdn, tmp_path):
    api = LyricsAPI(cdn.base, lyrics=LRC_TEXT)
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=2)
    stats = dl.batch_download(songs(1), api)
    assert stats.success == 1
    assert stats.lyrics_saved == 1
    assert (tmp_path / "Artist 1 - Song 1.mp3").exists()
    lyrics = tmp_path / "Artist 1 - Song 1.lrc"
    text = lyrics.read_text(encoding="utf-8")
    assert "[00:01.00]line one" in text
    assert "[00:01.00]第一行" in text      # translation merged in place
    assert not list(tmp_path.glob("*.part"))
    task = dl.get_tasks()[0]
    assert task.lyrics_status == "saved"
    assert task.to_dict()["lyrics_path"] == str(lyrics)
    assert api.lyrics_calls == ["1"]


def test_lyrics_disabled_downloads_no_lyrics(cdn, tmp_path):
    api = LyricsAPI(cdn.base, lyrics=LRC_TEXT)
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1, download_lyrics=False)
    stats = dl.batch_download(songs(1), api)
    assert stats.success == 1 and stats.lyrics_saved == 0
    assert api.lyrics_calls == []
    assert dl.get_tasks()[0].lyrics_status == "disabled"
    assert not list(tmp_path.glob("*.lrc"))


def test_translation_can_be_excluded(cdn, tmp_path):
    api = LyricsAPI(cdn.base, lyrics=LRC_TEXT)
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1, lyrics_translation=False)
    dl.batch_download(songs(1), api)
    text = (tmp_path / "Artist 1 - Song 1.lrc").read_text(encoding="utf-8")
    assert "line one" in text and "第一行" not in text


def test_a_song_without_lyrics_still_downloads(cdn, tmp_path):
    api = LyricsAPI(cdn.base, lyrics=None)
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1)
    stats = dl.batch_download(songs(1, 2), api)
    assert stats.success == 2 and stats.failed == 0
    assert stats.lyrics_missing == 2 and stats.lyrics_saved == 0
    assert not list(tmp_path.glob("*.lrc"))


def test_lyrics_failure_never_fails_the_song(cdn, tmp_path):
    api = LyricsAPI(cdn.base, error=RuntimeError("lyrics backend down"))
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1)
    stats = dl.batch_download(songs(1), api)
    assert stats.success == 1 and stats.failed == 0
    task = dl.get_tasks()[0]
    assert task.status == "completed"
    assert task.lyrics_status == "failed"
    assert "lyrics backend down" in (task.lyrics_error or "")


def test_existing_lyrics_are_kept(cdn, tmp_path):
    existing = tmp_path / "Artist 1 - Song 1.lrc"
    existing.write_text("my own lyrics\n", encoding="utf-8")
    api = LyricsAPI(cdn.base, lyrics=LRC_TEXT)
    dl = SongDownloader(tmp_path, max_concurrent=1)   # overwrite=False
    stats = dl.batch_download(songs(1), api)
    assert stats.lyrics_saved == 0
    assert existing.read_text(encoding="utf-8") == "my own lyrics\n"
    assert dl.get_tasks()[0].lyrics_status == "exists"
    assert api.lyrics_calls == []


def test_overwrite_rewrites_existing_lyrics(cdn, tmp_path):
    existing = tmp_path / "Artist 1 - Song 1.lrc"
    existing.write_text("stale\n", encoding="utf-8")
    api = LyricsAPI(cdn.base, lyrics=LRC_TEXT)
    dl = SongDownloader(tmp_path, overwrite=True, max_concurrent=1, download_lyrics=True)
    dl.overwrite = True                      # overwrite=True already set above
    stats = dl.batch_download(songs(1), api)
    assert stats.lyrics_saved == 1
    assert "line one" in existing.read_text(encoding="utf-8")


def test_lyrics_are_backfilled_for_an_already_downloaded_song(cdn, tmp_path):
    """Audio exists, lyrics do not: enabling lyrics later must fetch them."""
    (tmp_path / "Artist 1 - Song 1.mp3").write_bytes(PAYLOAD)
    api = LyricsAPI(cdn.base, lyrics=LRC_TEXT)
    dl = SongDownloader(tmp_path, max_concurrent=1)   # overwrite=False -> audio skipped
    stats = dl.batch_download(songs(1), api)
    assert stats.skipped == 1 and stats.lyrics_saved == 1
    assert (tmp_path / "Artist 1 - Song 1.lrc").exists()
    assert not list(tmp_path.glob("*.part"))
