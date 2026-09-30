"""Lyrics: fetching, LRC merging and writing.

Endpoint: ``eapi /api/song/lyric`` (POST with ``id`` / ``lv`` / ``kv`` / ``tv``),
verified live on 2026-09-30 from a mainland-CN network:

* ``/api/song/lyric`` (eapi)  -> ``code 200`` with ``lrc.lyric`` for every track
  tried (40/40 of a chart playlist), plus ``tlyric.lyric`` when the song has a
  translation (empty for purely Chinese tracks).
* ``/api/song/lyric/v1``      -> ``code 400``; useless.
* ``GET /api/song/lyric``     -> gone (the legacy ``/api/*`` routes only survive
  for the login flow on that network).

There is **no romanisation** in the response (no ``romalrc`` key), and
``klyric``/``yrc`` (word-by-word karaoke) come back empty, so those are not
downloaded -- see ``docs/notes.md``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = [
    "LYRICS_EXTENSION",
    "LYRIC_ENDPOINT",
    "build_lyrics",
    "lyrics_path_for",
    "merge_translation",
    "parse_lrc",
    "write_lyrics",
]

#: eapi path (the ``/eapi`` HTTP prefix is added by the API client).
LYRIC_ENDPOINT = "/api/song/lyric"

#: Lyrics live in a sidecar file next to the audio file.
LYRICS_EXTENSION = ".lrc"

#: ``[ti:...]``, ``[ar:...]`` ... -- metadata, not timed lines.
_METADATA_RE = re.compile(
    r"^\[(ti|ar|al|by|offset|re|ve|length|kana|au|encoding):([^\]]*)\]\s*$",
    re.IGNORECASE,
)
_TIMESTAMP = r"\[\d{1,4}:\d{1,2}(?:[.:]\d{1,3})?\]"
_LINE_RE = re.compile(rf"^((?:{_TIMESTAMP})+)(.*)$")
_FIRST_STAMP_RE = re.compile(_TIMESTAMP)


def parse_lrc(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split an LRC into ``(metadata tags, [(timestamps, content), ...])``.

    Untimed text is folded into the previous entry (multi-line lyrics) or kept
    as a tag when it comes first.
    """
    tags: list[str] = []
    entries: list[tuple[str, str]] = []
    for raw_line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if _METADATA_RE.match(line):
            tags.append(line)
            continue
        match = _LINE_RE.match(line)
        if match:
            entries.append((match.group(1), match.group(2).strip()))
        elif entries:
            stamps, content = entries[-1]
            entries[-1] = (stamps, f"{content} {line}".strip())
        else:
            tags.append(line)
    return tags, entries


def _first_stamp(stamps: str) -> str:
    match = _FIRST_STAMP_RE.search(stamps or "")
    return match.group(0) if match else ""


def _as_text(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def merge_translation(lrc: str, translation: str) -> str:
    """Return ``lrc`` with every available translation added in place.

    The translated line is emitted with the *original* line's timestamps, so a
    player that shows one line per timestamp ends up showing both, and a
    multi-timestamp original line gets its translation at each of them.
    """
    if not (translation or "").strip():
        return _as_text(lrc)

    tags, entries = parse_lrc(lrc)
    _, translated = parse_lrc(translation)
    if not translated:
        return _as_text(lrc)

    by_stamp: dict[str, list[str]] = {}
    order: list[str] = []
    for stamps, content in translated:
        key = _first_stamp(stamps)
        if not key or not content:
            continue
        if key not in by_stamp:
            order.append(key)
            by_stamp[key] = []
        by_stamp[key].append(content)

    lines: list[str] = list(tags)
    used: set[str] = set()
    for stamps, content in entries:
        lines.append(f"{stamps}{content}")
        key = _first_stamp(stamps)
        for extra in by_stamp.get(key, []):
            lines.append(f"{stamps}{extra}")
        if key in by_stamp:
            used.add(key)

    # A translation whose timestamp has no matching original line would be lost;
    # keep it at the end rather than dropping content.
    for key in order:
        if key in used:
            continue
        for extra in by_stamp[key]:
            lines.append(f"{key}{extra}")

    return _as_text("\n".join(lines))


def lyrics_path_for(audio_path: Path) -> Path:
    """``Artist - Title.mp3`` -> ``Artist - Title.lrc``."""
    return Path(audio_path).with_suffix(LYRICS_EXTENSION)


def write_lyrics(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        part.write_text(text, encoding="utf-8")
        path.unlink(missing_ok=True)
        part.replace(path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def build_lyrics(api: Any, song_id: str, *, translation: bool = True) -> str | None:
    """Fetch (and merge) the lyrics of ``song_id``.

    Returns the LRC text to write, or ``None`` when the song has no lyrics.
    ``api`` is a :class:`src.core.api.NeteaseAPI` (or any object with a
    compatible ``get_lyrics``), so this stays trivially testable.
    """
    if api is None or not song_id:
        return None
    result = api.get_lyrics(str(song_id))
    if not isinstance(result, dict):
        return None

    lyric = (result.get("lyric") or "").strip()
    if not lyric:
        return None

    if translation:
        merged = merge_translation(lyric, result.get("translation") or "")
        if merged:
            return merged
    return _as_text(lyric)
