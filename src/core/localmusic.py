"""Local music: directory scanning, metadata and embedded lyrics.

Deliberately **dependency free**: mutagen (and everything else in that space) is
GPL, and this project ships MIT, so the tag parsers below are written by hand
for the containers that actually matter -- ID3v2 (mp3), Vorbis comments
(flac/ogg) and the iTunes atoms (m4a).  Anything else falls back to
"Artist - Title" parsed out of the file name.

Performance rules that shape the code:

* Only the header region of a file is ever read; audio payloads are never
  loaded unless the caller explicitly asks for cover art.
* Lyrics and cover art are **not** read during a scan -- they are fetched
  lazily when a track is played, so scanning a 20 000 file library keeps a
  small, flat footprint.
* A successful scan is cached in ``local-index.json`` keyed by
  ``(mtime, size)``, so the next launch only re-parses files that changed.

See ``docs/notes.md`` for the measured numbers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
from pathlib import Path
from typing import Any, BinaryIO

from src.config import get_settings

__all__ = [
    "AUDIO_EXTENSIONS",
    "LocalMusicLibrary",
    "get_local_music",
    "guess_from_filename",
    "read_cover",
    "read_embedded_lyrics",
    "read_metadata",
    "sidecar_lyrics_path",
]

#: Containers the scanner accepts.
AUDIO_EXTENSIONS = (
    ".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus",
    ".wma", ".ape", ".aiff", ".aif", ".alac",
)

#: Never walk deeper than this below a configured root.
MAX_DEPTH = 8

#: Safety valve for the number of files one scan will look at.
#: Upper bound on how many files one library may hold.  Every track costs a few
#: KB in memory (payload + tag cache), so this keeps the worst case around
#: 100 MB instead of letting a stray folder scan eat the whole machine.
MAX_FILES = 30000

#: Upper bound for an ID3v2 tag we are willing to walk (guards against a
#: corrupt header announcing a multi-gigabyte tag).
MAX_TAG_BYTES = 8 * 1024 * 1024

#: Upper bound for a cover image we are willing to buffer.
MAX_COVER_BYTES = 12 * 1024 * 1024

#: How many repeated strings (artists, albums, titles) the index shares.
_MAX_STRING_TABLE = 20000

_LRC_SUFFIX = ".lrc"

_TRACK_NUMBER_RE = re.compile(r"^\s*(?:cd\s*)?\d{1,3}\s*[-.、_)\]]\s*", re.IGNORECASE)
_SEPARATORS = (" - ", " – ", " — ", "－", "_-_")

#: MPEG audio bitrate / sample-rate tables (index 0 and 15 are invalid).
_MP3_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MP3_RATES = (44100, 48000, 32000, 0)


# --------------------------------------------------------------------- helpers
def guess_from_filename(stem: str) -> tuple[str, str]:
    """``"01 - Artist - Title"`` -> ``("Artist", "Title")``.

    Returns ``(artist, title)``; the artist is empty when the name carries no
    separator, which the UI renders as an unknown artist.
    """
    cleaned = _TRACK_NUMBER_RE.sub("", str(stem or "")).strip()
    if not cleaned:
        return "", ""
    for separator in _SEPARATORS:
        if separator in cleaned:
            artist, _, title = cleaned.partition(separator)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return artist, title
    return "", cleaned


def _decode_text(data: bytes) -> str:
    """Decode an ID3 text frame (its first byte selects the encoding)."""
    if not data:
        return ""
    encoding, body = data[0], data[1:]
    return _decode_with_encoding(body, encoding)


def _decode_with_encoding(data: bytes, encoding: int) -> str:
    try:
        if encoding == 0:
            return data.decode("latin-1", "replace").replace("\x00", " ").strip()
        if encoding == 1:
            return data.decode("utf-16", "replace").replace("\x00", " ").strip()
        if encoding == 2:
            return data.decode("utf-16-be", "replace").replace("\x00", " ").strip()
        return data.decode("utf-8", "replace").replace("\x00", " ").strip()
    except Exception:  # pragma: no cover - errors="replace" never raises
        return ""


def _split_terminated(body: bytes, encoding: int) -> tuple[bytes, bytes]:
    """Split ``body`` on the first NUL terminator for ``encoding``."""
    terminator = b"\x00\x00" if encoding in (1, 2) else b"\x00"
    step = 2 if encoding in (1, 2) else 1
    index = 0
    while index + len(terminator) <= len(body):
        if body[index:index + len(terminator)] == terminator:
            return body[:index], body[index + len(terminator):]
        index += step
    return body, b""


# ------------------------------------------------------------------- ID3 (mp3)
def _id3_total_size(handle: BinaryIO) -> int:
    """Size of the ID3v2 tag at the start of ``handle`` (0 when there is none)."""
    handle.seek(0)
    header = handle.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        return 0
    size = 0
    for byte in header[6:10]:
        size = (size << 7) | (byte & 0x7F)
    footer = 10 if (header[5] & 0x10) else 0
    return 10 + size + footer


#: ID3v2.3/2.4 text frames we care about.
_ID3_TEXT_FRAMES = {"TIT2": "title", "TPE1": "artist", "TALB": "album"}


def _read_id3(handle: BinaryIO, want: frozenset[str]) -> dict[str, Any]:
    total = _id3_total_size(handle)
    if not total:
        return {}
    handle.seek(0)
    header = handle.read(10)
    if len(header) < 10:
        return {}
    major = header[3]
    offset = 10
    if header[5] & 0x40:                    # an extended header is in the way
        extended = handle.read(4)
        if len(extended) < 4:
            return {}
        if major >= 4:
            size = 0
            for byte in extended:
                size = (size << 7) | (byte & 0x7F)
        else:
            size = int.from_bytes(extended, "big") + 4
        offset += size
    handle.seek(offset)

    payload = min(total - offset, MAX_TAG_BYTES)
    result: dict[str, Any] = {}
    read = 0
    while read < payload:
        frame_header = handle.read(10)
        if len(frame_header) < 10:
            break
        read += 10
        frame_id = frame_header[:4]
        if frame_id[0:1] == b"\x00":       # padding reached
            break
        if major >= 4:
            size = 0
            for byte in frame_header[4:8]:
                size = (size << 7) | (byte & 0x7F)
        else:
            size = int.from_bytes(frame_header[4:8], "big")
        if size <= 0 or size > MAX_TAG_BYTES:
            break

        name = frame_id.decode("latin-1")
        heavy = (name == "USLT" and "lyrics" in want) or (name == "APIC" and "cover" in want)
        if name in _ID3_TEXT_FRAMES or name == "TLEN" or heavy:
            data = handle.read(size)
            read += len(data)
            _apply_id3_frame(result, name, data, want)
            if len(data) < size:
                break
        else:
            handle.seek(size, os.SEEK_CUR)
            read += size
    return result


def _apply_id3_frame(result: dict[str, Any], name: str, data: bytes,
                     want: frozenset[str]) -> None:
    if name in _ID3_TEXT_FRAMES:
        value = _decode_text(data)
        if value and value != "0":
            result.setdefault(_ID3_TEXT_FRAMES[name], value)
    elif name == "TLEN":
        digits = _decode_text(data)
        if digits.isdigit():
            result.setdefault("duration_ms", int(digits))
    elif name == "USLT" and "lyrics" in want and not result.get("lyrics"):
        encoding = data[0] if data else 0
        body = data[4:] if len(data) > 4 else b""      # skip encoding + language
        _, text = _split_terminated(body, encoding)
        decoded = _decode_with_encoding(text, encoding)
        if decoded:
            result["lyrics"] = decoded
    elif name == "APIC" and "cover" in want and not result.get("cover"):
        encoding = data[0] if data else 0
        mime, _, rest = data[1:].partition(b"\x00")
        if len(rest) < 1:
            return
        rest = rest[1:]                                # drop the picture type
        _, payload = _split_terminated(rest, encoding)
        if payload and len(payload) <= MAX_COVER_BYTES:
            result["cover"] = payload
            result["cover_mime"] = mime.decode("latin-1", "replace") or "image/jpeg"


def _mp3_duration_ms(handle: BinaryIO) -> int:
    """Estimate the duration of an mp3 from its first frame header."""
    try:
        audio_start = _id3_total_size(handle)
        handle.seek(audio_start)
        window = handle.read(65536)
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()
        for index in range(len(window) - 4):
            if window[index] != 0xFF or (window[index + 1] & 0xE0) != 0xE0:
                continue
            header = struct.unpack(">I", window[index:index + 4])[0]
            version = (header >> 19) & 0x3
            layer = (header >> 17) & 0x3
            bitrate_index = (header >> 12) & 0xF
            rate_index = (header >> 10) & 0x3
            if version == 1 or layer == 0 or bitrate_index in (0, 15) or rate_index == 3:
                continue
            bitrate = _MP3_BITRATES[bitrate_index] * 1000
            rate = _MP3_RATES[rate_index]
            if version == 2:                # MPEG2 halves the sample rate
                rate //= 2
            elif version == 0:              # MPEG2.5 quarters it
                rate //= 4
            if not bitrate or not rate:
                continue
            audio_bytes = max(0, file_size - audio_start - index)
            return int(audio_bytes * 8 / bitrate * 1000)
        return 0
    except Exception:  # pragma: no cover - malformed files
        return 0


# ------------------------------------------------------------------ FLAC / Ogg
def _read_flac(handle: BinaryIO, want: frozenset[str]) -> dict[str, Any]:
    handle.seek(0)
    magic = handle.read(4)
    if magic[:3] == b"ID3":                 # some taggers prepend an ID3v2 tag
        handle.seek(_id3_total_size(handle))
        magic = handle.read(4)
    if magic != b"fLaC":
        handle.seek(0)
        return _read_id3(handle, want)
    result: dict[str, Any] = {}
    while True:
        block_header = handle.read(4)
        if len(block_header) < 4:
            break
        last = bool(block_header[0] & 0x80)
        block_type = block_header[0] & 0x7F
        size = int.from_bytes(block_header[1:4], "big")
        if block_type == 0 and size >= 18:                 # STREAMINFO
            data = handle.read(min(size, 64))
            bits = int.from_bytes(data[10:18], "big")
            rate = (bits >> 44) & 0xFFFFF
            samples = bits & 0xFFFFFFFFF
            if rate:
                result["duration_ms"] = int(samples / rate * 1000)
            handle.seek(max(0, size - len(data)), os.SEEK_CUR)
        elif block_type == 4:                              # VORBIS_COMMENT
            data = handle.read(min(size, 1024 * 1024))
            _apply_vorbis(result, data, want)
            handle.seek(max(0, size - len(data)), os.SEEK_CUR)
        elif block_type == 6 and "cover" in want and not result.get("cover"):
            data = handle.read(min(size, MAX_COVER_BYTES + 4096))
            _apply_flac_picture(result, data)
            handle.seek(max(0, size - len(data)), os.SEEK_CUR)
        else:
            handle.seek(size, os.SEEK_CUR)
        if last:
            break
    return result


def _apply_vorbis(result: dict[str, Any], data: bytes, want: frozenset[str]) -> None:
    """Parse a Vorbis comment block (``vendor_len vendor count entries``)."""
    try:
        vendor_len = int.from_bytes(data[0:4], "little")
        offset = 4 + vendor_len
        count = int.from_bytes(data[offset:offset + 4], "little")
        offset += 4
        fields = {"TITLE": "title", "ARTIST": "artist", "ALBUM": "album",
                  "LYRICS": "lyrics", "UNSYNCEDLYRICS": "lyrics"}
        for _ in range(min(count, 500)):
            if offset + 4 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 4], "little")
            offset += 4
            if length < 0 or offset + length > len(data):
                break
            text = data[offset:offset + length].decode("utf-8", "replace")
            offset += length
            key, _, value = text.partition("=")
            field = fields.get(key.strip().upper())
            if not field or (field == "lyrics" and "lyrics" not in want):
                continue
            value = value.strip()
            if value:
                result.setdefault(field, value)
    except Exception:  # pragma: no cover - malformed comment block
        return


def _apply_flac_picture(result: dict[str, Any], data: bytes) -> None:
    try:
        offset = 4
        mime_len = int.from_bytes(data[offset:offset + 4], "big")
        offset += 4
        mime = data[offset:offset + mime_len].decode("latin-1", "replace")
        offset += mime_len
        desc_len = int.from_bytes(data[offset:offset + 4], "big")
        offset += 4 + desc_len + 16                    # description + w/h/depth/colors
        data_len = int.from_bytes(data[offset:offset + 4], "big")
        offset += 4
        payload = data[offset:offset + data_len]
        if payload:
            result["cover"] = payload
            result["cover_mime"] = mime or "image/jpeg"
    except Exception:  # pragma: no cover
        return


def _read_ogg(handle: BinaryIO, want: frozenset[str]) -> dict[str, Any]:
    """Ogg/Vorbis: the comment header is in the second page of the file."""
    try:
        handle.seek(0)
        window = handle.read(512 * 1024)
        marker = b"\x03vorbis"
        index = window.find(marker)
        if index < 0:
            return {}
        result: dict[str, Any] = {}
        _apply_vorbis(result, window[index + len(marker):], want)
        return result
    except Exception:  # pragma: no cover
        return {}


# ---------------------------------------------------------------------- MP4
def _read_mp4(handle: BinaryIO, want: frozenset[str]) -> dict[str, Any]:
    """Walk ``moov`` without ever loading ``mdat`` (which holds the audio)."""
    handle.seek(0, os.SEEK_END)
    end = handle.tell()
    result: dict[str, Any] = {}
    for atom in _iter_atoms(handle, 0, end):
        if atom[0] == b"moov":
            for child in _iter_atoms(handle, atom[1], atom[1] + atom[2]):
                if child[0] == b"mvhd":
                    _apply_mvhd(result, _atom_payload(handle, child, 128))
                elif child[0] == b"udta":
                    _read_mp4_udta(handle, child, result, want)
    return result


def _iter_atoms(handle: BinaryIO, start: int, end: int):
    """Yield ``(type, payload_start, payload_size)`` for the atoms in a range."""
    offset = start
    while offset + 8 <= end:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            return
        size = int.from_bytes(header[0:4], "big")
        atom_type = header[4:8]
        header_size = 8
        if size == 1:
            extended = handle.read(8)
            if len(extended) < 8:
                return
            size = int.from_bytes(extended, "big")
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size or offset + size > end:
            return
        yield atom_type, offset + header_size, size - header_size
        offset += size


def _atom_payload(handle: BinaryIO, atom: tuple[bytes, int, int], limit: int) -> bytes:
    handle.seek(atom[1])
    return handle.read(min(atom[2], limit))


def _apply_mvhd(result: dict[str, Any], data: bytes) -> None:
    try:
        if data[0] == 1:
            timescale = int.from_bytes(data[20:24], "big")
            duration = int.from_bytes(data[24:32], "big")
        else:
            timescale = int.from_bytes(data[12:16], "big")
            duration = int.from_bytes(data[16:20], "big")
        if timescale:
            result["duration_ms"] = int(duration / timescale * 1000)
    except Exception:  # pragma: no cover
        return


#: iTunes atom names -> our field names (the ``\xa9`` prefix is the © marker).
_MP4_FIELDS = {"\xa9nam": "title", "\xa9art": "artist", "\xa9alb": "album",
               "\xa9lyr": "lyrics", "tit2": "title", "tpe1": "artist"}


def _read_mp4_udta(handle: BinaryIO, atom: tuple[bytes, int, int],
                   result: dict[str, Any], want: frozenset[str]) -> None:
    for child in _iter_atoms(handle, atom[1], atom[1] + atom[2]):
        if child[0] != b"meta":
            continue
        # `meta` carries a 4 byte version/flags field before its children.
        for item in _iter_atoms(handle, child[1] + 4, child[1] + child[2]):
            if item[0] != b"ilst":
                continue
            for tag in _iter_atoms(handle, item[1], item[1] + item[2]):
                field = _MP4_FIELDS.get(tag[0].decode("latin-1", "replace").lower())
                if not field or (field == "lyrics" and "lyrics" not in want):
                    continue
                text = _mp4_text(_atom_payload(handle, tag, 2 * 1024 * 1024))
                if text:
                    result.setdefault(field, text)


def _mp4_text(payload: bytes) -> str:
    """Extract the value of an ``ilst`` item (its first ``data`` child)."""
    offset = 0
    while offset + 8 <= len(payload):
        size = int.from_bytes(payload[offset:offset + 4], "big")
        kind = payload[offset + 4:offset + 8]
        if size < 8 or offset + size > len(payload):
            break
        if kind == b"data" and size > 16:
            return payload[offset + 16:offset + size].decode("utf-8", "replace").strip()
        offset += size
    return ""


# ---------------------------------------------------------------------- WAV
def _read_wav(handle: BinaryIO, want: frozenset[str]) -> dict[str, Any]:
    """RIFF: duration from ``fmt``/``data``, tags from an ``INFO`` list."""
    result: dict[str, Any] = {}
    try:
        handle.seek(0)
        if handle.read(4) != b"RIFF":
            return result
        handle.seek(12)
        byte_rate = 0
        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            chunk_id = header[0:4]
            size = int.from_bytes(header[4:8], "little")
            if chunk_id == b"fmt ":
                data = handle.read(min(size, 32))
                if len(data) >= 12:
                    byte_rate = int.from_bytes(data[8:12], "little")
                handle.seek(max(0, size - len(data)), os.SEEK_CUR)
            elif chunk_id == b"LIST":
                data = handle.read(min(size, 256 * 1024))
                _apply_wav_info(result, data)
                handle.seek(max(0, size - len(data)), os.SEEK_CUR)
            elif chunk_id == b"data":
                if byte_rate:
                    result["duration_ms"] = int(size / byte_rate * 1000)
                break
            else:
                handle.seek(size, os.SEEK_CUR)
    except Exception:  # pragma: no cover
        return result
    return result


def _apply_wav_info(result: dict[str, Any], data: bytes) -> None:
    """Read the ``INFO`` sub-chunks of a RIFF ``LIST`` (INAM/IART/IPRD)."""
    fields = {b"INAM": "title", b"IART": "artist", b"IPRD": "album"}
    offset = data.find(b"INFO")
    if offset < 0:
        return
    offset += 4
    while offset + 8 <= len(data):
        key = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        value = data[offset + 8:offset + 8 + size]
        field = fields.get(key)
        if field and value:
            result.setdefault(field, value.decode("utf-8", "replace").strip("\x00").strip())
        if size <= 0:
            break
        offset += 8 + size + (size % 2)                 # chunks are word aligned
    return


# ------------------------------------------------------------------ public API
def read_metadata(path: str | Path, *, lyrics: bool = False,
                  cover: bool = False) -> dict[str, Any]:
    """Read the tags of one audio file.

    ``lyrics`` / ``cover`` are opt-in: they are the two expensive fields, and a
    plain scan never asks for them.
    """
    wanted = frozenset(name for name, flag in (("lyrics", lyrics), ("cover", cover)) if flag)
    target = Path(path)
    suffix = target.suffix.lower()
    try:
        with open(target, "rb") as handle:
            if suffix == ".flac":
                result = _read_flac(handle, wanted)
            elif suffix in (".m4a", ".m4b"):
                result = _read_mp4(handle, wanted)
            elif suffix == ".wav":
                result = _read_wav(handle, wanted)
            elif suffix in (".ogg", ".oga", ".opus"):
                result = _read_ogg(handle, wanted)
            else:
                result = _read_id3(handle, wanted)
                if not result.get("duration_ms"):
                    duration = _mp3_duration_ms(handle)
                    if duration:
                        result["duration_ms"] = duration
    except Exception as exc:  # unreadable file -> fall back to the file name
        logging.getLogger("ncm.localmusic").debug("cannot read %s: %s", target, exc)
        return {}
    return result


def read_embedded_lyrics(path: str | Path) -> str:
    """Return the lyrics embedded in the file's tags (``""`` when there are none)."""
    return str(read_metadata(path, lyrics=True).get("lyrics") or "")


def read_cover(path: str | Path) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, mime)`` for the embedded cover art, if any."""
    result = read_metadata(path, cover=True)
    data = result.get("cover")
    if isinstance(data, bytes) and data:
        return data, str(result.get("cover_mime") or "image/jpeg")
    return None


#: Folder -> (mtime, {lowercase stem: .lrc path}).  Listing a folder again for
#: every file made a scan quadratic: 1000 files in one folder meant 1000 full
#: directory walks.  Keyed by mtime, so adding or removing lyrics invalidates it.
_SIDECAR_DIRS: dict[str, tuple[float, dict[str, Path]]] = {}
_SIDECAR_LOCK = threading.Lock()
_MAX_SIDECAR_DIRS = 64


def _sidecar_index(directory: Path) -> dict[str, Path] | None:
    """Case-insensitive ``.lrc`` table for one folder (cached per mtime)."""
    key = str(directory)
    try:
        mtime = directory.stat().st_mtime
    except OSError:
        return None
    with _SIDECAR_LOCK:
        cached = _SIDECAR_DIRS.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    index: dict[str, Path] = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(_LRC_SUFFIX):
                    continue
                stem = entry.name[: -len(_LRC_SUFFIX)].lower()
                if stem not in index:
                    index[stem] = Path(entry.path)
    except OSError:
        return None
    with _SIDECAR_LOCK:
        _SIDECAR_DIRS[key] = (mtime, index)
        while len(_SIDECAR_DIRS) > _MAX_SIDECAR_DIRS:
            _SIDECAR_DIRS.pop(next(iter(_SIDECAR_DIRS)))
    return index


def sidecar_lyrics_path(audio_path: str | Path) -> Path | None:
    """Locate the ``.lrc`` next to an audio file, tolerating case differences."""
    audio = Path(audio_path)
    direct = audio.with_suffix(_LRC_SUFFIX)
    if direct.is_file():
        return direct
    index = _sidecar_index(audio.parent)
    if not index:
        return None
    return index.get(audio.stem.lower())


def _track_payload(path: Path, tags: dict[str, Any], dedupe=None) -> dict[str, Any]:
    """Build the JSON-safe track dict the frontend plays.

    ``dedupe`` is an optional string table: a big library repeats its artists and
    albums on every track, so sharing those strings keeps the index small.  The
    path is only stored once (``key`` is derived from it) for the same reason.
    """
    fallback_artist, fallback_title = guess_from_filename(path.stem)
    location = str(path)
    title = str(tags.get("title") or "").strip() or fallback_title or path.stem
    artists = str(tags.get("artist") or "").strip() or fallback_artist
    album = str(tags.get("album") or "").strip()
    if dedupe is not None:
        title = dedupe(title)
        artists = dedupe(artists)
        album = dedupe(album)
    duration = tags.get("duration_ms")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "key": f"local:{location}",
        "source": "local",
        "name": title,
        "artists": artists,
        "album": album,
        "duration": int(duration) if isinstance(duration, int | float) else 0,
        "path": location,
        "ext": path.suffix.lower().lstrip("."),
        "size": size,
        "has_lyrics": sidecar_lyrics_path(path) is not None,
    }


class LocalMusicLibrary:
    """Scans the configured folders and caches the parsed tags (singleton)."""

    _instance: LocalMusicLibrary | None = None
    _lock = threading.Lock()

    INDEX_NAME = "local-index.json"

    def __new__(cls) -> LocalMusicLibrary:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.logger = logging.getLogger("ncm.localmusic")
        #: Shared strings for the index (see :meth:`_dedupe`).
        self._strings: dict[str, str] = {}
        self._lock = threading.RLock()
        self._tracks: list[dict[str, Any]] = []
        self._cache: dict[str, dict[str, Any]] = {}
        self._last_scan: dict[str, Any] = {}
        self._load_cache()

    # ------------------------------------------------------------------ cache
    @property
    def index_path(self) -> Path:
        return get_settings().config_path.with_name(self.INDEX_NAME)

    def _load_cache(self, force: bool = False) -> None:
        """Read ``local-index.json``; ``force`` re-reads it from disk."""
        with self._lock:
            if self._cache and not force:
                return
        try:
            if not self.index_path.exists():
                return
            with open(self.index_path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            self.logger.debug("cannot read the local index: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        files = raw.get("files")
        with self._lock:
            if isinstance(files, dict):
                self._cache = {str(k): v for k, v in files.items() if isinstance(v, dict)}
            stored = raw.get("tracks")
            if isinstance(stored, list) and not self._tracks:
                self._tracks = [t for t in stored if isinstance(t, dict)]
            scanned = raw.get("scanned_at")
            if isinstance(scanned, dict) and not self._last_scan:
                self._last_scan = scanned

    def _save_cache(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "files": self._cache,
                "tracks": self._tracks,
                "scanned_at": self._last_scan,
            }
        try:
            path = self.index_path
            part = path.with_name(path.name + ".part")
            part.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            path.unlink(missing_ok=True)
            part.replace(path)
        except OSError as exc:
            self.logger.debug("cannot write the local index: %s", exc)

    # ------------------------------------------------------------------- scan
    def refresh(self, directories: list[str] | None = None, *,
                max_depth: int = MAX_DEPTH) -> dict[str, Any]:
        """Scan ``directories``.

        Returns ``{"tracks", "scanned", "added", "removed", "directories"}``.
        Unchanged files (same mtime + size) reuse their cached tags, so only the
        first scan after a change pays for the parsing.
        """
        roots = [str(Path(d).expanduser()) for d in (directories or []) if str(d or "").strip()]
        found: list[Path] = []
        for root in roots:
            base = Path(root)
            if not base.is_dir():
                continue
            for path in self._iter_audio(base, max_depth):
                found.append(path)
                if len(found) >= MAX_FILES:
                    self.logger.warning("stopping the scan at %d files", MAX_FILES)
                    break

        found_paths = {str(path) for path in found}
        seen: dict[str, dict[str, Any]] = {}
        for path in found:
            payload = _track_payload(path, self._tags_for(path), self._dedupe)
            seen[payload["key"]] = payload

        with self._lock:
            # The tag cache is keyed by file path, the track map by track key:
            # comparing the two directly would drop every cached tag on each scan.
            for stale in [k for k in self._cache if k not in found_paths]:
                self._cache.pop(stale, None)
            previous = {t.get("key") for t in self._tracks}
            tracks = sorted(seen.values(), key=lambda t: (t["artists"], t["name"]))
            self._tracks = tracks
            self._last_scan = {"directories": roots, "count": len(tracks)}
        self._save_cache()
        return {
            "tracks": len(tracks),
            "scanned": len(found),
            "added": len(set(seen) - previous),
            "removed": len(previous - set(seen)),
            "directories": roots,
        }

    def _dedupe(self, value: str) -> str:
        """Share repeated strings (artist/album) across the index."""
        if not value:
            return value
        table = self._strings
        found = table.get(value)
        if found is not None:
            return found
        if len(table) >= _MAX_STRING_TABLE:
            table.clear()
        table[value] = value
        return value

    def _tags_for(self, path: Path) -> dict[str, Any]:
        """Cached tag lookup: a file is only re-parsed when it changed."""
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return {}
        with self._lock:
            cached = self._cache.get(key)
        if cached and cached.get("mtime") == stat.st_mtime and cached.get("size") == stat.st_size:
            tags = cached.get("tags")
            return dict(tags) if isinstance(tags, dict) else {}
        tags = read_metadata(path)
        with self._lock:
            self._cache[key] = {"mtime": stat.st_mtime, "size": stat.st_size, "tags": tags}
        return tags

    @staticmethod
    def _iter_audio(root: Path, max_depth: int):
        """Depth-limited walk that skips hidden entries and non-audio files."""
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name.startswith("."):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if depth < max_depth:
                                    stack.append((Path(entry.path), depth + 1))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if entry.name.lower().endswith(AUDIO_EXTENSIONS):
                            yield Path(entry.path)
            except OSError as exc:
                logging.getLogger("ncm.localmusic").debug("cannot list %s: %s", directory, exc)

    # -------------------------------------------------------------- accessors
    def tracks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(track) for track in self._tracks]

    def track(self, path: str) -> dict[str, Any] | None:
        key = path if str(path).startswith("local:") else f"local:{path}"
        with self._lock:
            for track in self._tracks:
                if track.get("key") == key:
                    return dict(track)
        return None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "tracks": len(self._tracks),
                "cached_files": len(self._cache),
                "last_scan": dict(self._last_scan),
            }

    def forget(self, path: str) -> None:
        """Drop one file from the cache (used when its tags are known to be stale)."""
        key = path if str(path).startswith("local:") else f"local:{path}"
        with self._lock:
            self._cache.pop(str(key).removeprefix("local:"), None)
            self._tracks = [t for t in self._tracks if t.get("key") != key]

    def clear(self) -> None:
        with self._lock:
            self._tracks = []
            self._cache = {}
            self._last_scan = {}
        self._save_cache()


_local_music: LocalMusicLibrary | None = None
_local_music_lock = threading.Lock()


def get_local_music() -> LocalMusicLibrary:
    """Return the process-wide local music library."""
    global _local_music
    with _local_music_lock:
        if _local_music is None:
            _local_music = LocalMusicLibrary()
        return _local_music
