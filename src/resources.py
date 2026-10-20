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
    "RESOURCES_DIRNAME",
    "RESOURCES_ARCHIVE",
]

log = logging.getLogger("ncm")

#: Directory created beside the executable on Windows.
RESOURCES_DIRNAME = "_resources"
#: Zip inside the PyInstaller archive holding the GUI/i18n assets.
RESOURCES_ARCHIVE = "ncm-resources.zip"

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


def ensure_resources() -> Path | None:
    """Unpack the bundled asset archive next to the executable (Windows only).

    Returns the extraction directory, or ``None`` when this build does not use
    the archive.  Extraction goes into a sibling staging directory that is
    swapped in at the end, so a crash can never leave a half-populated
    ``_resources`` behind to fail the next launch.

    Never fatal: if the archive is missing or unusable we fall back to the
    onefile directory so the caller still gets a usable path.
    """
    target = extracted_resources_dir()
    if target is None:
        return None
    if _is_complete(target):
        return target

    # The archive always lives in the onefile dir.  Never look for it inside the
    # extraction dir: once _resources exists, bundle_root() points there, so
    # searching it would find nothing and a partial extraction could never be
    # repaired.
    if not is_frozen():
        return None
    archive = Path(getattr(sys, "_MEIPASS", "")) / RESOURCES_ARCHIVE
    if not archive.is_file():
        log.warning("resource archive %s is missing; using the bundle directory", archive)
        return None

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
