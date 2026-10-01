"""Timeline ("scrolling") lyrics: LRC with multiple stamps and translations."""
from __future__ import annotations

from src.core.lyrics import build_timed_lyrics, merge_translation, parse_lrc_timed

LRC = """[ti:Test]
[00:01.50]line one
[00:05.00][01:05.25]repeated line
[00:10.00]line three
"""

TRANSLATED = """[00:10.00]第一行
[01:05.25]重复的行
"""


def test_parse_lrc_timed_orders_and_converts_to_ms():
    # parse_lrc_timed yields (milliseconds, text) pairs, sorted by time.
    lines = parse_lrc_timed(LRC)

    assert [stamp for stamp, _ in lines] == [1500, 5000, 10000, 65250]
    assert lines[0] == (1500, "line one")
    # A line with two stamps becomes two entries with the same text
    assert (5000, "repeated line") in lines
    assert (65250, "repeated line") in lines


def test_build_timed_lyrics_merges_translation():
    lines = build_timed_lyrics(LRC, TRANSLATED)

    by_time = {line["time"]: line for line in lines}
    assert by_time[10000]["translation"] == "第一行"
    assert by_time[65250]["translation"] == "重复的行"
    assert by_time[1500]["text"] == "line one"
    assert "translation" not in by_time[1500] or not by_time[1500].get("translation")


def test_build_timed_lyrics_handles_metadata_and_junk():
    lines = build_timed_lyrics("[ar:Artist]\n[by:someone]\n\n[00:03]plain\nnot a lyric line")

    assert len(lines) == 1
    assert lines[0]["time"] == 3000
    # An untimestamped line right after a lyric continues it (wrapped lyrics);
    # header metadata before the first stamp is dropped instead.
    assert lines[0]["text"] == "plain not a lyric line"


def test_build_timed_lyrics_without_any_stamps_is_empty():
    assert build_timed_lyrics("no timestamps here") == []
    assert build_timed_lyrics("") == []


def test_orphan_translation_lines_are_ignored():
    lines = build_timed_lyrics("[00:01.00]a\n[00:02.00]b", "[00:09.00]never matches")

    assert len(lines) == 2
    assert all(not line.get("translation") for line in lines)


def test_merge_translation_keeps_plain_lyrics_when_translation_is_empty():
    merged = merge_translation("[00:01.00]a", "")

    assert "[00:01.00]a" in merged
    assert merged.count("\n") <= 1
