"""Regression tests for the lyrics pairing and the desktop overlay payload.

Both were silent failures: the wrong data looked like "no data", so nothing
raised and no test noticed.
"""
from __future__ import annotations

from src.core.lyrics import build_timed_lyrics, merge_translation


def test_inlined_translation_is_paired_not_duplicated():
    """A saved ``.lrc`` holds the translation at the same stamp as the
    original.  Emitting both made the translation a ghost row with
    ``translation == ""``, and the highlight landed on it."""
    lrc = "[00:10.00]line three\n[00:10.00]第三行\n[00:20.00]line four\n"
    rows = build_timed_lyrics(lrc)
    assert len(rows) == 2, rows
    assert rows[0]["text"] == "line three"
    assert rows[0]["translation"] == "第三行"
    assert rows[1]["text"] == "line four"


def test_a_round_trip_through_merge_translation_survives():
    """merge_translation then build_timed_lyrics must not double the rows."""
    original = "[00:10.00]hello\n[00:20.00]world\n"
    translation = "[00:10.00]你好\n[00:20.00]世界\n"
    merged = merge_translation(original, translation)
    rows = build_timed_lyrics(merged)
    assert len(rows) == 2, rows
    assert [row["text"] for row in rows] == ["hello", "world"]
    assert [row["translation"] for row in rows] == ["你好", "世界"]


def test_a_separate_translation_block_still_pairs():
    original = "[00:10.00]hello\n[00:20.00]world\n"
    translation = "[00:10.00]你好\n[00:20.00]世界\n"
    rows = build_timed_lyrics(original, translation)
    assert [row["translation"] for row in rows] == ["你好", "世界"]


def test_a_slightly_offset_translation_still_pairs():
    original = "[00:10.00]hello\n"
    translation = "[00:10.20]你好\n"
    rows = build_timed_lyrics(original, translation)
    assert rows[0]["translation"] == "你好"


def test_no_translation_leaves_the_field_empty():
    rows = build_timed_lyrics("[00:10.00]hello\n[00:20.00]world\n")
    assert [row["translation"] for row in rows] == ["", ""]


def test_an_orphan_translation_does_not_become_a_row():
    original = "[00:10.00]hello\n"
    translation = "[00:10.00]你好\n[00:99.00]没有人对应\n"
    rows = build_timed_lyrics(original, translation)
    assert len(rows) == 1
    assert rows[0]["text"] == "hello"


def test_timestamps_stay_sorted():
    lrc = "[00:30.00]c\n[00:10.00]a\n[00:20.00]b\n"
    rows = build_timed_lyrics(lrc)
    assert [row["time"] for row in rows] == [10000, 20000, 30000]
