"""Lyrics tests: parsing, merging, writing and (in test_downloader) integration."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.lyrics import (
    LYRICS_EXTENSION,
    build_lyrics,
    lyrics_path_for,
    merge_translation,
    parse_lrc,
    write_lyrics,
)

LRC = """[ti:Test]
[ar:Tester]
[00:00.00] Hello
[00:05.50] World
"""

TRANSLATION = """[00:00.00] 你好
[00:05.50] 世界
"""


def test_parse_lrc_splits_metadata_and_lines():
    tags, entries = parse_lrc(LRC)
    assert tags == ["[ti:Test]", "[ar:Tester]"]
    assert entries == [("[00:00.00]", "Hello"), ("[00:05.50]", "World")]


def test_parse_lrc_handles_multi_timestamps_and_continuations():
    _, entries = parse_lrc("[00:01.00][00:31.00] chorus line\nstill chorus\n[00:10.00] next")
    assert entries[0] == ("[00:01.00][00:31.00]", "chorus line still chorus")
    assert entries[1] == ("[00:10.00]", "next")


def test_parse_lrc_normalises_line_endings():
    _, entries = parse_lrc(LRC.replace("\n", "\r\n"))
    assert len(entries) == 2
    _, bare = parse_lrc(LRC.replace("\n", "\r"))
    assert len(bare) == 2


def test_parse_lrc_of_empty_input():
    assert parse_lrc("") == ([], [])
    assert parse_lrc(None) == ([], [])


def test_merge_translation_interleaves_lines():
    merged = merge_translation(LRC, TRANSLATION)
    lines = merged.strip().split("\n")
    assert lines[:2] == ["[ti:Test]", "[ar:Tester]"]
    assert lines[2:] == ["[00:00.00]Hello", "[00:00.00]你好",
                         "[00:05.50]World", "[00:05.50]世界"]
    assert merged.endswith("\n")


def test_merge_translation_without_translation_keeps_the_original():
    assert merge_translation(LRC, "") == LRC
    assert merge_translation(LRC.strip(), "   ") == LRC.strip() + "\n"


def test_merge_translation_covers_every_timestamp_of_a_shared_line():
    merged = merge_translation("[00:01.00][00:31.00] chorus\n", "[00:01.00] 副歌\n")
    assert merged == "[00:01.00][00:31.00]chorus\n[00:01.00][00:31.00]副歌\n"


def test_merge_translation_keeps_unmatched_lines():
    merged = merge_translation("[00:01.00] a\n", "[00:01.00] A\n[00:09.00] B\n")
    assert "[00:01.00]A" in merged
    assert "[00:09.00]B" in merged


def test_merge_translation_ignores_a_translation_of_junk():
    assert merge_translation(LRC, "not an lrc at all") == LRC


def test_lyrics_path_for_swaps_the_extension():
    assert lyrics_path_for(Path("/tmp/A - B.mp3")) == Path("/tmp/A - B.lrc")
    assert lyrics_path_for(Path("/tmp/A - B.flac")).name == f"A - B{LYRICS_EXTENSION}"


def test_write_lyrics_is_atomic_and_overwrites(tmp_path):
    target = tmp_path / "sub" / "song.lrc"
    write_lyrics(target, "first\n")
    assert target.read_text(encoding="utf-8") == "first\n"
    write_lyrics(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert sorted(p.name for p in target.parent.iterdir()) == ["song.lrc"]  # no .part left


def test_write_lyrics_leaves_nothing_behind_on_failure(tmp_path):
    with pytest.raises(TypeError):
        write_lyrics(tmp_path / "x.lrc", b"bytes, not text")  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


class FakeLyricsAPI:
    """Any object with ``get_lyrics`` works (that is the whole point)."""

    def __init__(self, result=None):
        self.result = result
        self.calls: list[str] = []

    def get_lyrics(self, song_id):
        self.calls.append(song_id)
        return self.result


def test_build_lyrics_merges_the_translation():
    api = FakeLyricsAPI({"lyric": LRC, "translation": TRANSLATION, "code": 200})
    text = build_lyrics(api, "1")
    assert api.calls == ["1"]
    assert "Hello" in text and "你好" in text


def test_build_lyrics_can_skip_the_translation():
    api = FakeLyricsAPI({"lyric": LRC, "translation": TRANSLATION, "code": 200})
    text = build_lyrics(api, "1", translation=False)
    assert "Hello" in text and "你好" not in text


def test_build_lyrics_returns_none_without_content():
    assert build_lyrics(FakeLyricsAPI(None), "1") is None
    assert build_lyrics(FakeLyricsAPI({"lyric": "  ", "translation": ""}), "1") is None
    assert build_lyrics(None, "1") is None
    assert build_lyrics(FakeLyricsAPI({"lyric": LRC}), "") is None


def test_build_lyrics_always_ends_with_a_newline():
    api = FakeLyricsAPI({"lyric": "[00:01.00] x", "translation": ""})
    assert build_lyrics(api, "1").endswith("\n")


def test_build_lyrics_propagates_api_errors_to_the_caller():
    """The *downloader* is what swallows this -- build_lyrics stays honest."""
    class Boom:
        def get_lyrics(self, song_id):
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        build_lyrics(Boom(), "1")


# ------------------------------------------------------------------- settings

def test_lyrics_settings_defaults_and_roundtrip(isolated_home):
    from src.config import get_settings
    from src.config.settings import DownloadSettings

    assert DownloadSettings().download_lyrics is True
    assert DownloadSettings().lyrics_translation is True

    settings = get_settings()
    assert settings.download.download_lyrics is True
    assert settings.download.lyrics_translation is True

    # the UI sends JSON: "false"/0 must not end up truthy
    settings.update_download(download_lyrics="false", lyrics_translation=0)
    assert settings.download.download_lyrics is False
    assert settings.download.lyrics_translation is False

    settings.update_download(download_lyrics="true", lyrics_translation=True)
    assert settings.download.download_lyrics is True
    assert settings.download.lyrics_translation is True
