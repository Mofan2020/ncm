"""The local video library: scanning, durations and subtitles (offline).

The two container fixtures are built byte by byte here on purpose: the point
of the module is that it reads a duration **without** a third party library,
so the test has to prove it against a real (if tiny) header rather than
against a mock of its own reader.
"""
from __future__ import annotations

import struct
from pathlib import Path

from src.core.localvideo import (
    ass_to_vtt,
    library,
    parse_ass,
    parse_srt,
    scan_folder,
    srt_to_vtt,
    subtitle_for,
    video_duration,
)


# ------------------------------------------------------------------- fixtures
def _box(kind: bytes, payload: bytes) -> bytes:
    """One MP4 atom: a 32 bit size, the type, then the payload."""
    return struct.pack(">I", len(payload) + 8) + kind + payload


def build_mp4(path: Path, seconds: float = 9.0) -> Path:
    """A minimal MP4: ``ftyp`` plus a ``moov`` that carries one ``mvhd``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2mp41")
    timescale = 1000
    duration = int(seconds * timescale)
    # version + flags, creation, modification, timescale, duration, ...
    mvhd = (
        b"\x00\x00\x00\x00"
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + struct.pack(">I", timescale)
        + struct.pack(">I", duration)
        + b"\x00" * 80
    )
    moov = _box(b"moov", _box(b"mvhd", mvhd))
    # An mdat with some bytes, so the file looks like a video and not a header.
    payload = b"\x00" * 1024
    path.write_bytes(ftyp + moov + _box(b"mdat", payload))
    return path


def _ebml(element_id: bytes, payload: bytes) -> bytes:
    """One EBML element: id, a one byte size, then the payload."""
    if len(payload) > 126:
        raise ValueError("this fixture only writes one byte sizes")
    return element_id + bytes([0x80 | len(payload)]) + payload


def build_mkv(path: Path, seconds: float = 12.5,
              scale: int = 1_000_000) -> Path:
    """A minimal MKV: an EBML header and a segment holding ``Info``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = _ebml(b"\x1a\x45\xdf\xa3", b"\x42\x86\x81\x01")
    info = _ebml(
        b"\x15\x49\xa9\x66",
        _ebml(b"\x2a\xd7\xb1", struct.pack(">I", scale))
        + _ebml(b"\x44\x89", struct.pack(">d", seconds * 1_000_000_000 / scale)),
    )
    segment = _ebml(b"\x18\x53\x80\x67", info)
    path.write_bytes(header + segment)
    return path


# --------------------------------------------------------------------- the scan
def test_scan_lists_videos_and_skips_the_noise(tmp_path):
    folder = tmp_path / "videos"
    build_mp4(folder / "one.mp4", seconds=9)
    build_mkv(folder / "nested" / "two.mkv", seconds=12.5)
    (folder / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (folder / "notes.txt").write_text("not a video", encoding="utf-8")
    (folder / ".hidden.mp4").write_bytes(b"\x00" * 8)
    (folder / "__MACOSX").mkdir()
    (folder / "__MACOSX" / "._one.mp4").write_bytes(b"\x00" * 8)
    (folder / ".DS_Store").write_bytes(b"\x00" * 8)
    (folder / "sub").mkdir()

    result = scan_folder(str(folder))

    names = sorted(item["name"] for item in result["items"])
    assert names == ["one.mp4", "two.mkv"]
    assert result["errors"] == []

    item = next(entry for entry in result["items"] if entry["name"] == "one.mp4")
    assert Path(item["path"]).is_absolute()
    assert Path(item["path"]) == folder / "one.mp4"
    assert item["size"] == (folder / "one.mp4").stat().st_size
    assert item["mtime"] == (folder / "one.mp4").stat().st_mtime
    assert 8.9 <= item["duration"] <= 9.1


def test_scan_reports_a_missing_folder_instead_of_raising(tmp_path):
    result = scan_folder(str(tmp_path / "nope"))

    assert result["items"] == []
    assert result["errors"] and "不存在" in result["errors"][0]


def test_scan_of_a_file_says_it_is_not_a_folder(tmp_path):
    path = build_mp4(tmp_path / "one.mp4")

    result = scan_folder(str(path))

    assert result["items"] == []
    assert result["errors"]


# ------------------------------------------------------------------ the library
def test_library_dedupes_the_same_file(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    build_mp4(first / "one.mp4")
    build_mp4(second / "two.mp4")

    # The same folder twice, plus the folder that contains it: three quarters
    # of the way to the duplicate case a real library creates with symlinks.
    result = library([str(first), str(first), str(tmp_path)])

    paths = [item["path"] for item in result["items"]]
    assert len(paths) == len(set(paths))
    assert sorted(item["name"] for item in result["items"]) == ["one.mp4", "two.mp4"]
    assert result["items"] == sorted(result["items"], key=lambda item: item["path"])


def test_library_keeps_going_when_one_folder_is_missing(tmp_path):
    folder = tmp_path / "a"
    build_mp4(folder / "one.mp4")

    result = library([str(tmp_path / "missing"), str(folder)])

    assert [item["name"] for item in result["items"]] == ["one.mp4"]
    assert result["errors"]


# ------------------------------------------------------------------ durations
def test_mp4_duration_comes_from_mvhd(tmp_path):
    path = build_mp4(tmp_path / "one.mp4", seconds=42)

    assert abs(video_duration(path) - 42) < 0.01


def test_mkv_duration_comes_from_the_ebml_info(tmp_path):
    path = build_mkv(tmp_path / "one.mkv", seconds=12.5)

    assert abs(video_duration(path) - 12.5) < 0.01


def test_a_container_without_a_length_reports_zero(tmp_path):
    path = tmp_path / "one.avi"
    path.write_bytes(b"RIFF" + b"\x00" * 64)

    assert video_duration(path) == 0
    assert scan_folder(str(tmp_path))["items"][0]["duration"] == 0


def test_a_broken_file_is_not_fatal(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"\x00\x01\x02")

    assert video_duration(path) == 0
    assert scan_folder(str(tmp_path))["items"][0]["duration"] == 0


# ------------------------------------------------------------------ subtitles
SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "第一句\n"
    "第二行\n"
    "\n"
    "2\n"
    "00:00:05,500 --> 00:00:07,250\n"
    "Second cue\n"
)

ASS = """[Script Info]
Title: test
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,{\\i1}斜体{\\i0}一句, 带逗号
Dialogue: 0,0:00:05.50,0:00:07.25,Default,,0,0,0,,Next\\Nline
"""


def test_parse_srt_reads_the_cues():
    cues = parse_srt(SRT)

    assert len(cues) == 2
    assert cues[0]["start"] == 1.0
    assert cues[0]["end"] == 4.0
    assert cues[0]["text"] == "第一句\n第二行"
    assert cues[1]["start"] == 5.5
    assert cues[1]["end"] == 7.25
    assert cues[1]["text"] == "Second cue"


def test_srt_to_vtt_is_what_a_track_tag_wants():
    vtt = srt_to_vtt(SRT)

    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:04.000" in vtt
    assert "00:00:05.500 --> 00:00:07.250" in vtt
    assert "第一句" in vtt


def test_parse_ass_reads_the_dialogue_lines():
    cues = parse_ass(ASS)

    assert len(cues) == 2
    # The inline style commands are gone, the line break is not.
    assert cues[0]["text"] == "斜体一句, 带逗号"
    assert cues[1]["text"] == "Next\nline"
    assert cues[0]["start"] == 1.0


def test_ass_to_vtt_is_webvtt():
    vtt = ass_to_vtt(ASS)

    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:04.000" in vtt


def test_subtitle_for_prefers_the_sidecar_with_the_same_name(tmp_path):
    video = build_mp4(tmp_path / "movie.mp4")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")
    (tmp_path / "movie.ass").write_text(ASS, encoding="utf-8")

    result = subtitle_for(str(video))

    assert result["kind"] == "srt"
    assert result["path"].endswith("movie.srt")
    assert result["text"].startswith("WEBVTT")


def test_subtitle_for_falls_back_to_ass_and_vtt(tmp_path):
    video = build_mp4(tmp_path / "movie.mp4")
    (tmp_path / "movie.ass").write_text(ASS, encoding="utf-8")
    assert subtitle_for(str(video))["kind"] == "ass"

    (tmp_path / "movie.ass").unlink()
    (tmp_path / "movie.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n",
                                        encoding="utf-8")
    result = subtitle_for(str(video))
    assert result["kind"] == "vtt"
    assert result["text"].startswith("WEBVTT")


def test_subtitle_for_takes_the_hand_picked_file(tmp_path):
    video = build_mp4(tmp_path / "movie.mp4")
    (tmp_path / "movie.srt").write_text(SRT, encoding="utf-8")
    extra = tmp_path / "picked.vtt"
    extra.write_text("WEBVTT\n\n00:00:02.000 --> 00:00:03.000\npicked\n", encoding="utf-8")

    result = subtitle_for(str(video), str(extra))

    assert result["path"] == str(extra)
    assert result["kind"] == "vtt"
    assert "picked" in result["text"]


def test_subtitle_for_returns_empty_when_there_is_none(tmp_path):
    video = build_mp4(tmp_path / "movie.mp4")

    assert subtitle_for(str(video)) == {"path": "", "kind": "", "text": ""}
    assert subtitle_for(str(video), str(tmp_path / "nope.srt")) == {
        "path": "", "kind": "", "text": "",
    }
    assert subtitle_for("") == {"path": "", "kind": "", "text": ""}


def test_subtitle_for_finds_an_upper_case_extension(tmp_path):
    """macOS and Windows file systems are case insensitive; the lookup is too."""
    video = build_mp4(tmp_path / "movie.mp4")
    (tmp_path / "MOVIE.SRT").write_text(SRT, encoding="utf-8")

    found = subtitle_for(str(video))

    assert found["kind"] == "srt"
    assert Path(found["path"]).name.lower() == "movie.srt"
