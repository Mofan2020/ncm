"""Synthesised audio files for the tests.

The project parses tags itself (no mutagen -- see src/core/localmusic.py), so
the tests build tiny but *valid* containers byte by byte: a real ID3v2.3 tag in
front of MPEG frames, and a real FLAC stream with a Vorbis comment block.  That
way the parser is exercised against the file format rather than against a
mock of it.
"""
from __future__ import annotations

import struct
from pathlib import Path

__all__ = ["build_mp3", "build_mp3_with_cover", "build_flac", "build_unknown",
           "MPEG_FRAME"]

#: One MPEG-1 Layer III frame: 128 kbps, 44.1 kHz, no padding -> 417 bytes.
MPEG_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
MPEG_FRAME_SIZE = 417


def _synchsafe(size: int) -> bytes:
    """ID3v2 sizes are 7 bits per byte ("synchsafe" integers)."""
    return bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])


def _id3_text_frame(frame_id: bytes, text: str) -> bytes:
    body = b"\x03" + text.encode("utf-8")  # encoding 3 = UTF-8
    return frame_id + len(body).to_bytes(4, "big") + b"\x00\x00" + body


def _id3_uslt_frame(language: str, descriptor: str, lyrics: str) -> bytes:
    """USLT = encoding + 3-byte language + NUL-terminated descriptor + text."""
    body = b"\x03" + language.encode("latin-1")[:3].ljust(3, b" ")
    body += descriptor.encode("utf-8") + b"\x00" + lyrics.encode("utf-8")
    return b"USLT" + len(body).to_bytes(4, "big") + b"\x00\x00" + body


def build_mp3(path: Path, *, title: str = "", artist: str = "", album: str = "",
              lyrics: str = "", seconds: int = 3, with_tag: bool = True) -> Path:
    """Write a playable-looking MP3 with (optionally) an ID3v2.3 tag."""
    frames = b""
    if title:
        frames += _id3_text_frame(b"TIT2", title)
    if artist:
        frames += _id3_text_frame(b"TPE1", artist)
    if album:
        frames += _id3_text_frame(b"TALB", album)
    if lyrics:
        frames += _id3_uslt_frame("eng", "desc", lyrics)

    payload = b""
    if with_tag and frames:
        payload += b"ID3\x03\x00\x00" + _synchsafe(len(frames)) + frames
    payload += MPEG_FRAME * max(1, seconds * 38)  # ~38 frames per second at 128 kbps
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _id3_apic_frame(image: bytes, mime: bytes = b"image/jpeg") -> bytes:
    """APIC = encoding + mime + NUL + picture type + NUL-terminated descriptor + data."""
    body = b"\x03" + mime + b"\x00" + b"\x03" + b"\x00" + image
    return b"APIC" + len(body).to_bytes(4, "big") + b"\x00\x00" + body


def build_mp3_with_cover(path: Path, image: bytes, *, title: str = "Covered",
                         artist: str = "Artist", mime: bytes = b"image/jpeg") -> Path:
    """An MP3 whose ID3v2.3 tag carries front-cover art."""
    frames = _id3_text_frame(b"TIT2", title) + _id3_text_frame(b"TPE1", artist)
    frames += _id3_apic_frame(image, mime)
    payload = b"ID3\x03\x00\x00" + _synchsafe(len(frames)) + frames + MPEG_FRAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _vorbis_comment(vendor: str, comments: dict[str, str]) -> bytes:
    body = struct.pack("<I", len(vendor)) + vendor.encode("utf-8")
    body += struct.pack("<I", len(comments))
    for key, value in comments.items():
        item = f"{key}={value}".encode()
        body += struct.pack("<I", len(item)) + item
    return body


def build_flac(path: Path, *, title: str = "", artist: str = "", album: str = "",
               sample_rate: int = 44100, seconds: int = 3) -> Path:
    """Write a minimal FLAC stream: STREAMINFO + VORBIS_COMMENT.

    ``_read_flac`` only needs the metadata blocks, so the audio frames are left
    out -- a real decoder would reject the file, the parser does not care.
    """
    total_samples = sample_rate * seconds
    bits = (sample_rate << 44) | ((2 - 1) << 41) | ((16 - 1) << 36) | total_samples
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + struct.pack(">Q", bits)

    comments = {}
    if title:
        comments["TITLE"] = title
    if artist:
        comments["ARTIST"] = artist
    if album:
        comments["ALBUM"] = album
    vorbis = _vorbis_comment("ncm-tests", comments)

    payload = b"fLaC"
    payload += bytes([0x00]) + len(streaminfo).to_bytes(3, "big") + streaminfo
    payload += bytes([0x80 | 0x04]) + len(vorbis).to_bytes(3, "big") + vorbis
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def build_unknown(path: Path, *, size: int = 4096) -> Path:
    """A file with a music extension but no parsable container."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really audio" + bytes(size))
    return path
