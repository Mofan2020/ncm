"""Bangumi 下载器的测试。

两件事要验：

* 按 URL 存文件——下完了、内容对、失败时不留半个文件、取消能取消；
* 导出条目资料成 txt——内容对得上。

用假 session，不发一个真请求。
"""
from __future__ import annotations

from pathlib import Path

from src.core.bangumi_download import (
    BangumiDownloader,
    build_episode_filename,
    sanitize_filename,
)


class FakeResponse:
    """够用的假响应。"""

    def __init__(self, content=b"", status_code=200, headers=None, chunk=3):
        self._content = content
        self.status_code = status_code
        self.headers = headers or {}
        self._chunk = chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_content(self, chunk_size=None):
        size = self._chunk or len(self._content) or 1
        for index in range(0, len(self._content), size):
            yield self._content[index:index + size]


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.headers = {}

    def get(self, url, stream=None, timeout=None, headers=None):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.response


def make_downloader(tmp_path: Path, response=None, error=None) -> BangumiDownloader:
    session = FakeSession(response, error)
    return BangumiDownloader(tmp_path / "dl", session=session, min_gap=0)


# ------------------------------------------------------------------ 文件名
def test_sanitize_filename_drops_the_bad_characters():
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a-b-c-d-e-f-g-h-i-j"
    assert sanitize_filename("  spaced   out  ") == "spaced out"
    assert sanitize_filename("") == "unknown"
    assert sanitize_filename("...") == "unknown"
    assert sanitize_filename("a\nb") == "a-b"


def test_sanitize_filename_keeps_the_extension_when_it_cuts():
    name = sanitize_filename("x" * 300 + ".mp4", max_len=20)
    assert len(name) <= 20
    assert name.endswith(".mp4")


def test_build_episode_filename():
    assert build_episode_filename("葬送的芙莉莲", "第 3 集") == \
        "葬送的芙莉莲 - 第 3 集.txt"
    assert build_episode_filename("芙莉莲", "") == "芙莉莲.txt"


# ------------------------------------------------------------------ 下载
def test_save_url_writes_the_file(tmp_path: Path):
    downloader = make_downloader(
        tmp_path, FakeResponse(b"hello world", headers={"content-length": "11"}))
    result = downloader.save_url("https://x.test/a.mp4", "a.mp4")
    assert result["success"] is True
    path = Path(result["path"])
    assert path.name == "a.mp4"
    assert path.read_bytes() == b"hello world"
    assert result["size"] == 11


def test_save_url_rejects_a_bad_url(tmp_path: Path):
    downloader = make_downloader(tmp_path)
    result = downloader.save_url("not-a-url", "a.mp4")
    assert result["success"] is False
    assert result["error"] == "bad_url"
    assert not (tmp_path / "dl").exists()


def test_save_url_reports_a_refusal(tmp_path: Path):
    downloader = make_downloader(tmp_path, FakeResponse(b"", status_code=404))
    result = downloader.save_url("https://x.test/a.mp4", "a.mp4")
    assert result["success"] is False
    assert result["error"] == "http_404"


def test_save_url_leaves_no_half_file_behind_on_an_error(tmp_path: Path):
    downloader = make_downloader(tmp_path, error=OSError("connection reset"))
    result = downloader.save_url("https://x.test/a.mp4", "a.mp4")
    assert result["success"] is False
    assert not list((tmp_path / "dl").glob("*.part"))
    assert not list((tmp_path / "dl").glob("*.mp4"))


def test_save_url_does_not_overwrite_an_existing_file(tmp_path: Path):
    downloader = make_downloader(tmp_path, FakeResponse(b"new"))
    downloader.download_dir.mkdir(parents=True, exist_ok=True)
    (downloader.download_dir / "a.mp4").write_bytes(b"old")
    result = downloader.save_url("https://x.test/a.mp4", "a.mp4")
    assert result["success"] is True
    assert Path(result["path"]).name == "a (1).mp4"
    assert (downloader.download_dir / "a.mp4").read_bytes() == b"old"


def test_save_url_guesses_the_name_from_the_url(tmp_path: Path):
    downloader = make_downloader(tmp_path, FakeResponse(b"x"))
    result = downloader.save_url("https://x.test/dir/movie.mkv")
    assert Path(result["path"]).name == "movie.mkv"


def test_the_progress_callback_runs(tmp_path: Path):
    seen = []
    downloader = make_downloader(
        tmp_path, FakeResponse(b"0123456789", headers={"content-length": "10"}))
    downloader.set_progress_callback(seen.append)
    downloader.save_url("https://x.test/a.mp4", "a.mp4")
    assert seen
    assert seen[-1]["progress"] == 100.0
    assert seen[-1]["downloaded"] == 10


def test_a_cancelled_download_leaves_nothing(tmp_path: Path):
    downloader = make_downloader(tmp_path, FakeResponse(b"0123456789"))
    downloader.cancel()
    result = downloader.save_url("https://x.test/a.mp4", "a.mp4")
    assert result["success"] is False
    assert result["error"] == "cancelled"
    assert not list((tmp_path / "dl").glob("*.part"))


def test_an_empty_response_is_a_failure(tmp_path: Path):
    downloader = make_downloader(tmp_path, FakeResponse(b""))
    result = downloader.save_url("https://x.test/a.mp4", "a.mp4")
    assert result["success"] is False
    assert result["error"] == "empty"


# ------------------------------------------------------------------ 导出
def test_export_subject_text_writes_the_details(tmp_path: Path):
    downloader = make_downloader(tmp_path)
    subject = {
        "id": 400602,
        "name_cn": "葬送的芙莉莲",
        "name": "Sousou no Frieren",
        "platform": "TV",
        "date": "2023-09-29",
        "total_episodes": 28,
        "rating_score": 8.5,
        "collection": {"collect": 55720},
        "summary": "勇者死后……",
        "tags": [{"name": "治愈"}, {"name": "奇幻"}],
    }
    episodes = [
        {"id": 1, "ep": 1, "type": 0, "name_cn": "冒险结束",
         "label": "冒险结束", "duration": "00:26:00", "airdate": "2023-09-29"},
        {"id": 2, "ep": 2, "type": 0, "name_cn": "杀人魔法",
         "label": "杀人魔法", "duration": "00:24:30", "airdate": "2023-10-06"},
    ]
    result = downloader.export_subject_text(subject, episodes)
    assert result["success"] is True
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert "葬送的芙莉莲" in text
    assert "Sousou no Frieren" in text
    assert "类型：TV" in text
    assert "集数：28" in text
    assert "评分：8.5" in text
    assert "收藏：55720" in text
    assert "勇者死后……" in text
    assert "治愈、奇幻" in text
    assert "第 1 集" in text
    assert "冒险结束" in text
    assert "00:26:00" in text


def test_export_subject_text_needs_a_subject(tmp_path: Path):
    downloader = make_downloader(tmp_path)
    assert downloader.export_subject_text({})["success"] is False


def test_export_subject_text_without_episodes(tmp_path: Path):
    downloader = make_downloader(tmp_path)
    result = downloader.export_subject_text({"id": 1, "name_cn": "甲"}, [])
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert "甲" in text
    assert "【章节】" not in text
