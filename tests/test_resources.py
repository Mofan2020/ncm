"""Resource resolution: the Windows zip-extraction path and macOS parity.

The Windows build ships its assets as a zip unpacked next to the .exe, because
WebView2 renders its "file may have been deleted or moved" page when the
onefile temp directory (_MEIPASS) is gone or quarantined.  Every other platform
must keep resolving exactly as before -- these tests pin both halves.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from src import resources as R

ROOT = Path(__file__).resolve().parent.parent
ASSET_DIRS = ("src/gui/assets", "src/i18n")


def _build_archive(tmp_path: Path, meipass: Path) -> Path:
    """Create the same archive main.spec's win32 branch builds."""
    staging = tmp_path / "stage"
    for entry in ASSET_DIRS:
        for f in sorted((ROOT / entry).rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            target = staging / f.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read_bytes())
    archive = meipass / R.RESOURCES_ARCHIVE
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(staging.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(staging).as_posix())
    return archive


@pytest.fixture
def frozen_windows(tmp_path, monkeypatch):
    """Pretend to be a frozen Windows build and restore sys afterwards."""
    exe_dir = tmp_path / "app"
    meipass = tmp_path / "_MEI123"
    exe_dir.mkdir()
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "NeteaseMusicDownloader.exe"))
    return exe_dir, meipass


# --------------------------------------------------------------- the bug fix

def test_extraction_dir_is_next_to_the_exe(frozen_windows):
    exe_dir, _ = frozen_windows
    assert R.extracted_resources_dir() == exe_dir / R.RESOURCES_DIRNAME


def test_assets_land_beside_the_exe_not_in_meipass(frozen_windows, tmp_path):
    exe_dir, meipass = frozen_windows
    _build_archive(tmp_path, meipass)

    out = R.ensure_resources()

    assert out is not None
    assert out == exe_dir / R.RESOURCES_DIRNAME
    for name in R._REQUIRED:
        assert (out / name).is_file(), f"{name} was not extracted"
    assert R.bundle_root() == out


def test_resource_path_is_under_the_exe_dir(frozen_windows, tmp_path):
    """The URL handed to the webview must not point into the onefile temp dir."""
    exe_dir, meipass = frozen_windows
    _build_archive(tmp_path, meipass)
    assert R.ensure_resources() is not None

    html = R.resource_path("src", "gui", "assets", "index.html")
    url = html.as_uri()

    assert str(exe_dir) in url
    assert "_MEI" not in url, f"still served from the onefile temp dir: {url}"


def test_desktop_lyrics_is_bundled(frozen_windows, tmp_path):
    """Regression: these two files were absent from the bundle entirely."""
    exe_dir, meipass = frozen_windows
    _build_archive(tmp_path, meipass)
    assert R.ensure_resources() is not None

    for name in ("desktop-lyrics.html", "desktop-lyrics.js"):
        assert (exe_dir / R.RESOURCES_DIRNAME / "src/gui/assets" / name).is_file()


# ---------------------------------------------------------------- robustness

def test_second_run_does_not_reextract(frozen_windows, tmp_path):
    exe_dir, meipass = frozen_windows
    _build_archive(tmp_path, meipass)
    out = R.ensure_resources()
    assert out is not None

    marker = out / "marker.txt"
    marker.write_text("keep me")
    assert R.ensure_resources() == out
    assert marker.exists(), "a complete directory was re-extracted over"


def test_partial_directory_is_repaired(frozen_windows, tmp_path):
    exe_dir, meipass = frozen_windows
    _build_archive(tmp_path, meipass)
    out = R.ensure_resources()
    assert out is not None

    (out / "src/gui/assets/app.js").unlink()
    assert R.ensure_resources() is not None
    assert (out / "src/gui/assets/app.js").is_file()


def test_missing_archive_falls_back_without_raising(frozen_windows):
    exe_dir, meipass = frozen_windows
    assert not (meipass / R.RESOURCES_ARCHIVE).exists()

    assert R.ensure_resources() is None
    assert R.bundle_root() == Path(str(sys._MEIPASS))


def test_corrupt_archive_does_not_leave_half_a_tree(frozen_windows):
    _, meipass = frozen_windows
    (meipass / R.RESOURCES_ARCHIVE).write_bytes(b"not a zip")
    exe_dir, _ = frozen_windows

    assert R.ensure_resources() is None
    assert not (exe_dir / R.RESOURCES_DIRNAME).exists()
    assert not (exe_dir / (R.RESOURCES_DIRNAME + ".new")).exists()


def test_parent_traversal_in_the_archive_is_refused(frozen_windows):
    exe_dir, meipass = frozen_windows
    archive = meipass / R.RESOURCES_ARCHIVE
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("src/gui/assets/index.html", "<h1>ok</h1>")
        zf.writestr("src/gui/assets/app.js", "x")
        zf.writestr("src/gui/assets/style.css", "x")
        zf.writestr("src/i18n/zh_cn.json", "{}")
        zf.writestr("../escaped.txt", "nope")

    assert R.ensure_resources() is None
    assert not (frozen_windows[0] / "escaped.txt").exists()


# ------------------------------------------------------- everything else is inert

@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_non_frozen_builds_never_use_the_archive(platform, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert R.extracted_resources_dir() is None
    assert R.ensure_resources() is None
    assert R.bundle_root() == ROOT


def test_frozen_macos_keeps_the_old_meipass_lookup(monkeypatch, tmp_path):
    """macOS must not be diverted to the extraction dir."""
    meipass = tmp_path / "_MEI999"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    assert R.extracted_resources_dir() is None
    assert R.ensure_resources() is None
    assert R.bundle_root() == meipass
