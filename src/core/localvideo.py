"""Local video library: folder scanning, container durations and subtitles.

**Dependency free on purpose.**  The project ships MIT and every third party
tag parser for video containers is GPL (or pulls in ffmpeg), so the container
headers are read by hand here.  Only the standard library is used:

* MP4 / MOV / M4V -- the ``mvhd`` atom carries the timescale and the duration.
* MKV / WebM -- the EBML ``Info`` element carries ``TimecodeScale`` and
  ``Duration``.

Every other container (avi, flv, ts) reports a duration of 0: those formats
have no header that says how long the file is, and guessing from the file size
would be a lie.  The UI shows the real duration once the element has loaded
it, so the missing number is not fatal.

The subtitles are converted to WebVTT, because that is the only format a
``<track>`` element accepts -- an SRT file served as-is is silently ignored by
the browser.
"""
from __future__ import annotations

import logging
import os
import re
import struct
from pathlib import Path
from typing import Any

__all__ = [
    "SUBTITLE_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "ass_to_vtt",
    "library",
    "parse_ass",
    "parse_srt",
    "scan_folder",
    "srt_to_vtt",
    "subtitle_for",
    "video_duration",
]

#: Containers the scanner accepts.
VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts",
)

#: Sidecar subtitle formats, in the order they are looked for.
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".vtt")

#: Never walk deeper than this below the folder that was handed in.
MAX_DEPTH = 8

#: Safety valve: how many files one scan will report.
MAX_FILES = 20000

#: How much of an MKV is read while looking for its duration.  The ``Info``
#: element sits right behind the segment header, so a megabyte is plenty;
#: the point is not to pull a 4 GB file into memory.
MKV_HEADER_BYTES = 1024 * 1024

# ---------------------------------------------------------------- EBML markers
_EBML_SEGMENT = 0x18538067
_EBML_INFO = 0x1549A966
_EBML_TIMECODE_SCALE = 0x2AD7B1
_EBML_DURATION = 0x4489

#: The default timecode scale of an MKV (1 ms), in nanoseconds.
_EBML_DEFAULT_SCALE = 1_000_000

# ------------------------------------------------------------------- the times
#: ``00:00:01,000`` / ``0:00:01.00`` / ``01:02`` -- the three shapes that turn
#: up in SRT and ASS files.
_TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?")

#: The inline style commands of an ASS subtitle (``{\i1}`` and friends).
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")


# --------------------------------------------------------------------- helpers
def _parse_time(value: str) -> float | None:
    """``"00:01:04,250"`` -> ``64.25`` seconds (``None`` when unparsable)."""
    match = _TIME_RE.search(str(value or ""))
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    fraction = match.group(4) or ""
    # The fraction is written with 2 (ASS) or 3 (SRT) digits; both are
    # fractions of a second, so it has to be scaled by its own length.
    millis = int(fraction.ljust(3, "0")) if fraction else 0
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _vtt_time(seconds: float) -> str:
    """Seconds as the ``HH:MM:SS.mmm`` stamp a WebVTT cue needs."""
    total = max(0.0, float(seconds))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    rest = total % 60
    return f"{hours:02d}:{minutes:02d}:{rest:06.3f}"


# ------------------------------------------------------------------- the atoms
def _iter_atoms(handle, start: int, end: int):
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
        if size == 1:                       # 64 bit size in a second field
            extended = handle.read(8)
            if len(extended) < 8:
                return
            size = int.from_bytes(extended, "big")
            header_size = 16
        elif size == 0:                     # the atom runs to the end
            size = end - offset
        if size < header_size or offset + size > end:
            return
        yield atom_type, offset + header_size, size - header_size
        offset += size


def _mp4_duration(path: Path) -> float:
    """Seconds from the ``mvhd`` atom of an MP4 / MOV / M4V file."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            for atom in _iter_atoms(handle, 0, end):
                if atom[0] != b"moov":
                    continue
                for child in _iter_atoms(handle, atom[1], atom[1] + atom[2]):
                    if child[0] != b"mvhd":
                        continue
                    handle.seek(child[1])
                    data = handle.read(min(child[2], 128))
                    if len(data) < 20:
                        return 0.0
                    if data[0] == 1:          # version 1: 64 bit times
                        timescale = int.from_bytes(data[20:24], "big")
                        duration = int.from_bytes(data[24:32], "big")
                    else:
                        timescale = int.from_bytes(data[12:16], "big")
                        duration = int.from_bytes(data[16:20], "big")
                    if not timescale:
                        return 0.0
                    return duration / timescale
    except Exception as exc:                # a broken file is not a crash
        logging.getLogger("ncm.localvideo").debug("cannot read %s: %s", path, exc)
    return 0.0


# ------------------------------------------------------------------ the EBML
def _ebml_id(data: bytes, pos: int) -> tuple[int, int]:
    """``(id, length)`` of the EBML element id at ``pos``.

    An id keeps its marker bits -- they are part of the number (``0x1A45DFA3``
    is the EBML header), unlike the size which drops them.
    """
    first = data[pos]
    if first == 0:
        raise ValueError("invalid EBML id")
    mask = 0x80
    length = 1
    while not first & mask:
        mask >>= 1
        length += 1
        if length > 4:
            raise ValueError("invalid EBML id")
    if pos + length > len(data):
        raise ValueError("truncated EBML id")
    return int.from_bytes(data[pos:pos + length], "big"), length


def _ebml_size(data: bytes, pos: int) -> tuple[int, int, bool]:
    """``(value, length, unknown)`` of the EBML size at ``pos``.

    ``unknown`` is the "size until the end of the file" marker, which several
    muxers write for the segment.
    """
    first = data[pos]
    if first == 0:
        raise ValueError("invalid EBML size")
    mask = 0x80
    length = 1
    while not first & mask:
        mask >>= 1
        length += 1
        if length > 8:
            raise ValueError("invalid EBML size")
    if pos + length > len(data):
        raise ValueError("truncated EBML size")
    value = int.from_bytes(data[pos:pos + length], "big") & ~mask
    unknown = value == (1 << (7 * length)) - 1
    return value, length, unknown


def _mkv_info_duration(data: bytes, start: int, end: int) -> float:
    """Seconds from the ``Info`` element of an MKV / WebM file."""
    scale = _EBML_DEFAULT_SCALE
    duration = 0.0
    pos = start
    while pos < end:
        try:
            element_id, id_len = _ebml_id(data, pos)
            size, size_len, unknown = _ebml_size(data, pos + id_len)
        except (IndexError, ValueError):
            break
        payload = pos + id_len + size_len
        stop = end if unknown else min(end, payload + size)
        if element_id == _EBML_TIMECODE_SCALE:
            scale = int.from_bytes(data[payload:stop], "big") or scale
        elif element_id == _EBML_DURATION:
            raw = data[payload:stop]
            try:
                if len(raw) >= 8:
                    duration = struct.unpack(">d", raw[:8])[0]
                elif len(raw) == 4:
                    duration = struct.unpack(">f", raw)[0]
            except struct.error:
                duration = 0.0
        pos = stop
    if duration <= 0:
        return 0.0
    # The duration is counted in timecode scale units (nanoseconds by default).
    return duration * scale / 1_000_000_000


def _mkv_duration(path: Path) -> float:
    """Seconds from the EBML header of an MKV / WebM file."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MKV_HEADER_BYTES)
    except Exception as exc:                # unreadable file -> no duration
        logging.getLogger("ncm.localvideo").debug("cannot read %s: %s", path, exc)
        return 0.0

    pos = 0
    segment = None
    while pos < len(data):
        try:
            element_id, id_len = _ebml_id(data, pos)
            size, size_len, unknown = _ebml_size(data, pos + id_len)
        except (IndexError, ValueError):
            break
        payload = pos + id_len + size_len
        stop = len(data) if unknown else min(len(data), payload + size)
        if element_id == _EBML_SEGMENT:
            segment = (payload, stop)
            break
        pos = stop
    if not segment:
        return 0.0

    pos = segment[0]
    while pos < segment[1]:
        try:
            element_id, id_len = _ebml_id(data, pos)
            size, size_len, unknown = _ebml_size(data, pos + id_len)
        except (IndexError, ValueError):
            break
        payload = pos + id_len + size_len
        stop = segment[1] if unknown else min(segment[1], payload + size)
        if element_id == _EBML_INFO:
            return _mkv_info_duration(data, payload, stop)
        pos = stop
    return 0.0


def video_duration(path: str | Path) -> float:
    """The length of a video file in seconds (0 when it cannot be read)."""
    target = Path(str(path)).expanduser()
    suffix = target.suffix.lower()
    try:
        if suffix in (".mp4", ".m4v", ".mov"):
            return _mp4_duration(target)
        if suffix in (".mkv", ".webm"):
            return _mkv_duration(target)
    except Exception as exc:                # a broken file is not a crash
        logging.getLogger("ncm.localvideo").debug("cannot read %s: %s", target, exc)
    # avi / flv / ts carry no length in their header.
    return 0.0


# ------------------------------------------------------------------ the scan
def _iter_videos(base: Path, errors: list[str]) -> list[Path]:
    """Every video file below ``base`` (hidden files and ``__MACOSX`` skipped)."""
    found: list[Path] = []
    depth_base = len(base.parts)

    def on_error(exc: OSError) -> None:
        errors.append(f"无法读取目录: {exc.filename or ''} ({exc.strerror or exc})")

    for root, dirnames, filenames in os.walk(base, onerror=on_error, followlinks=False):
        if len(Path(root).parts) - depth_base >= MAX_DEPTH:
            dirnames[:] = []
            continue
        # A dot file is a hidden file on macOS and Windows; ``__MACOSX`` is the
        # resource branch a zip of a folder brings along.
        dirnames[:] = sorted(
            name for name in dirnames
            if not name.startswith(".") and name != "__MACOSX"
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if not name.lower().endswith(VIDEO_EXTENSIONS):
                continue
            found.append(Path(root) / name)
            if len(found) >= MAX_FILES:
                errors.append(f"文件太多，只列出前 {MAX_FILES} 个")
                return found
    return found


def scan_folder(path: str) -> dict[str, Any]:
    """List the video files below one folder.

    Returns ``{"items": [...], "errors": [...]}``.  Every item carries the
    absolute ``path``, the ``name``, the ``size`` in bytes, the ``mtime`` as a
    timestamp and the ``duration`` in seconds (0 when the container does not
    say).
    """
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    raw = str(path or "").strip()
    if not raw:
        return {"items": [], "errors": ["没有给出文件夹"]}
    base = Path(os.path.abspath(os.path.expanduser(raw)))
    if not base.exists():
        return {"items": [], "errors": [f"文件夹不存在: {base}"]}
    if not base.is_dir():
        return {"items": [], "errors": [f"不是一个文件夹: {base}"]}

    for file_path in _iter_videos(base, errors):
        try:
            stat = file_path.stat()
        except OSError as exc:
            errors.append(f"无法读取文件: {file_path} ({exc.strerror or exc})")
            continue
        items.append({
            "path": str(file_path),
            "name": file_path.name,
            "size": int(stat.st_size),
            "mtime": float(stat.st_mtime),
            "duration": round(video_duration(file_path), 3),
        })
    return {"items": items, "errors": errors}


def library(dirs: list[str]) -> dict[str, Any]:
    """Scan several folders and report every file exactly once.

    The same file can be reachable through two roots (a symlinked library, a
    folder inside another one), so the paths are compared after resolving
    them; the first folder that lists a file wins.
    """
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in dirs or []:
        text = str(raw or "").strip()
        if not text:
            continue
        result = scan_folder(text)
        errors.extend(result["errors"])
        for item in result["items"]:
            key = os.path.normcase(os.path.realpath(item["path"]))
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    items.sort(key=lambda item: item["path"])
    return {"items": items, "errors": errors}


# ---------------------------------------------------------------- subtitles
def parse_srt(text: str) -> list[dict[str, Any]]:
    """The cues of an SRT subtitle.

    A cue is ``{"start": seconds, "end": seconds, "text": "..."}``.  The number
    in front of a block is optional and the text may run over several lines.
    """
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", normalized.strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing = -1
        for index, line in enumerate(lines[:2]):
            if "-->" in line:
                timing = index
                break
        if timing < 0:
            continue
        start_raw, _, end_raw = lines[timing].partition("-->")
        start = _parse_time(start_raw)
        end = _parse_time(end_raw)
        if start is None or end is None:
            continue
        body = "\n".join(lines[timing + 1:]).strip()
        cues.append({"start": start, "end": end, "text": body})
    return cues


def parse_ass(text: str) -> list[dict[str, Any]]:
    """The cues of an ASS subtitle.

    Only the ``[Events]`` section is read; the style of a cue is dropped,
    because a ``<track>`` element cannot carry it anyway.
    """
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    section = ""
    fields: list[str] = []
    cues: list[dict[str, Any]] = []
    for line in normalized.split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section != "events":
            continue
        if stripped.lower().startswith("format:"):
            fields = [name.strip().lower() for name in stripped.split(":", 1)[1].split(",")]
            continue
        if not stripped.lower().startswith("dialogue:"):
            continue
        if not fields:
            continue
        parts = stripped.split(":", 1)[1].split(",")
        if len(parts) < len(fields):
            continue
        # The text is the last field and may contain commas of its own, so the
        # split above must be undone for everything past the fixed fields.
        record = dict(zip(fields[:-1], parts[:len(fields) - 1], strict=True))
        body = ",".join(parts[len(fields) - 1:])
        start = _parse_time(record.get("start", ""))
        end = _parse_time(record.get("end", ""))
        if start is None or end is None:
            continue
        cues.append({"start": start, "end": end, "text": _clean_ass_text(body)})
    return cues


def _clean_ass_text(body: str) -> str:
    """Strip the style commands of an ASS cue and unfold its line breaks."""
    text = _ASS_TAG_RE.sub("", str(body or ""))
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return text.strip()


def _cues_to_vtt(cues: list[dict[str, Any]]) -> str:
    """Render cues as a WebVTT document."""
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(f"{_vtt_time(cue['start'])} --> {_vtt_time(cue['end'])}")
        lines.append(str(cue.get("text") or ""))
        lines.append("")
    return "\n".join(lines)


def srt_to_vtt(text: str) -> str:
    """An SRT subtitle as WebVTT (the only format a ``<track>`` accepts)."""
    return _cues_to_vtt(parse_srt(text))


def ass_to_vtt(text: str) -> str:
    """An ASS subtitle as WebVTT."""
    return _cues_to_vtt(parse_ass(text))


def _read_text(path: Path) -> str:
    """Read a subtitle file, trying the encodings that turn up in the wild."""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _subtitle_payload(path: Path) -> dict[str, Any]:
    """The ``{"path", "kind", "text"}`` record of one subtitle file."""
    kind = path.suffix.lower().lstrip(".")
    try:
        content = _read_text(path)
    except OSError as exc:
        logging.getLogger("ncm.localvideo").debug("cannot read %s: %s", path, exc)
        return {"path": str(path), "kind": kind, "text": ""}
    if kind == "srt":
        text = srt_to_vtt(content)
    elif kind == "ass":
        text = ass_to_vtt(content)
    else:
        text = content
        if not text.lstrip().upper().startswith("WEBVTT"):
            text = "WEBVTT\n\n" + text
    return {"path": str(path), "kind": kind, "text": text}


def subtitle_for(video_path: str, extra: str = "") -> dict[str, Any]:
    """Find the subtitle of a video.

    ``extra`` is a subtitle the user picked by hand; it wins when it is there.
    Otherwise the sidecar with the same name is used: ``.srt`` first, then
    ``.ass``, then ``.vtt``.  The returned text is WebVTT, ready to be handed
    to a ``<track>`` element.
    """
    empty: dict[str, Any] = {"path": "", "kind": "", "text": ""}
    picked = str(extra or "").strip()
    if picked:
        candidate = Path(os.path.abspath(os.path.expanduser(picked)))
        if not candidate.is_file():
            return empty
        return _subtitle_payload(candidate)

    raw = str(video_path or "").strip()
    if not raw:
        return empty
    video = Path(os.path.abspath(os.path.expanduser(raw)))
    for suffix in SUBTITLE_EXTENSIONS:
        sidecar = video.with_suffix(suffix)
        if sidecar.is_file():
            return _subtitle_payload(sidecar)

    # A file system that is case insensitive (macOS, Windows) accepts
    # "Movie.SRT" for "movie.mp4"; the direct check above misses that.
    try:
        entries = list(os.scandir(video.parent))
    except OSError:
        return empty
    stem = video.stem.lower()
    for suffix in SUBTITLE_EXTENSIONS:
        for entry in entries:
            name = entry.name.lower()
            if name.endswith(suffix) and name[:-len(suffix)] == stem:
                return _subtitle_payload(Path(entry.path))
    return empty
