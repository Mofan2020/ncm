"""Locate bundled data files in both source and frozen (PyInstaller) layouts.

``Path(__file__)`` is useless inside a frozen app: the modules live in the PYZ
archive while the HTML/CSS/JSON assets are unpacked next to the executable, so
every resource lookup goes through :func:`resource_path`.

The Windows build ships the assets as a zip and unpacks them beside the ``.exe``
(:func:`ensure_resources`) instead of serving them straight out of the onefile
temp directory, which WebView2 could lose track of.  Every other platform --
macOS included -- keeps the plain ``_MEIPASS`` / source-tree lookup.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import zipfile
from pathlib import Path

__all__ = [
    "bundle_root",
    "resource_path",
    "is_frozen",
    "extracted_resources_dir",
    "ensure_resources",
    "resource_archive_digest",
    "RESOURCES_DIRNAME",
    "RESOURCES_ARCHIVE",
    "RESOURCES_STAMP",
]

log = logging.getLogger("ncm")

#: Directory created beside the executable on Windows.
RESOURCES_DIRNAME = "_resources"
#: Zip inside the PyInstaller archive holding the GUI/i18n assets.
RESOURCES_ARCHIVE = "ncm-resources.zip"
#: Stamp file inside the extraction directory: it holds the digest of the
#: archive the directory was unpacked from, so the next launch can tell
#: whether the files still belong to the executable that is running.
RESOURCES_STAMP = ".ncm-resources.digest"

#: Files that must exist once the archive has been unpacked.
_REQUIRED = (
    "src/gui/assets/index.html",
    "src/gui/assets/app.js",
    "src/gui/assets/style.css",
    "src/i18n/zh_cn.json",
)


def is_frozen() -> bool:
    """Whether we are running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def extracted_resources_dir() -> Path | None:
    """Directory the Windows build unpacks its assets into.

    ``None`` everywhere except a frozen Windows build -- that guard is what
    keeps macOS and the source tree on their existing code path.
    """
    if not is_frozen() or sys.platform != "win32":
        return None
    return Path(sys.executable).resolve().parent / RESOURCES_DIRNAME


def _is_complete(target: Path) -> bool:
    return all((target / name).is_file() for name in _REQUIRED)


def resource_archive_digest(archive: Path) -> str:
    """Digest of the resource archive bundled with the running executable.

    The digest is the judge of "do the unpacked files still belong to this
    executable", so it has to be derived from the bytes themselves: a version
    number would have to be maintained by hand, and the same executable
    rebuilt from the same sources would then look like an update.
    """
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp_matches(target: Path, digest: str) -> bool:
    """Whether the unpacked files came from the archive with this digest."""
    stamp = target / RESOURCES_STAMP
    try:
        return stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == digest
    except OSError:
        return False


def ensure_resources() -> Path | None:
    """Unpack the bundled asset archive next to the executable (Windows only).

    Returns the extraction directory, or ``None`` when this build does not use
    the archive.  Extraction goes into a sibling staging directory that is
    swapped in at the end, so a crash can never leave a half-populated
    ``_resources`` behind to fail the next launch.

    The unpacked files are compared against the archive of the *running*
    executable, not merely checked for existence: replacing the executable
    with a newer build leaves the old files on disk, and an interface that
    runs new code against old assets is worse than a stale interface.  A
    directory without a matching digest stamp -- every installation made
    before this check existed -- is unpacked again.

    Never fatal: if the archive is missing or unusable we fall back to the
    onefile directory so the caller still gets a usable path.
    """
    target = extracted_resources_dir()
    if target is None:
        return None
    if not is_frozen():
        return None

    # The archive always lives in the onefile dir.  Never look for it inside the
    # extraction dir: once _resources exists, bundle_root() points there, so
    # searching it would find nothing and a partial extraction could never be
    # repaired.
    archive = Path(getattr(sys, "_MEIPASS", "")) / RESOURCES_ARCHIVE
    if not archive.is_file():
        log.warning("resource archive %s is missing; using the bundle directory", archive)
        return None

    digest = resource_archive_digest(archive)
    if _is_complete(target) and _stamp_matches(target, digest):
        return target

    staging = target.with_name(RESOURCES_DIRNAME + ".new")
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            # Refuse absolute paths and parent traversal.  The archive ships
            # with the app, but unpacking it should never be able to write
            # outside the target directory even if it were tampered with.
            for member in zf.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ValueError(f"unsafe path in resource archive: {member}")
            zf.extractall(staging)

        if not _is_complete(staging):
            raise ValueError("resource archive is incomplete after extraction")

        # The stamp travels with the files, so it always describes what is
        # actually in the directory.
        (staging / RESOURCES_STAMP).write_text(digest + "\n", encoding="utf-8")

        # A previous run's directory can still be held open by WebView2, so
        # removing it is best-effort: never block startup on a locked file.
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        staging.replace(target)
        log.info("extracted %s to %s", archive.name, target)
        return target
    except Exception as exc:
        log.warning("could not unpack %s (%s); using the bundle directory",
                    archive.name, exc)
        shutil.rmtree(staging, ignore_errors=True)
        return None


def bundle_root() -> Path:
    """Directory that holds the application's data files."""
    target = extracted_resources_dir()
    if target is not None and target.is_dir():
        return target
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Return the absolute path of a bundled resource."""
    return bundle_root().joinpath(*parts)
