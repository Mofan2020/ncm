"""main.spec must keep working on both platforms.

Two failure modes this pins:

1. The Windows branch had two NameErrors (``SPEC_DIR`` instead of ``_SPEC_DIR``,
   and ``_shutil`` bound only inside an ``if``) that only execute on a Windows
   runner -- the macOS build is fine, so a green local build proves nothing.
2. ``datas`` used to list files one by one, which silently dropped
   desktop-lyrics.html / .js from every bundle.

The spec is executed as real source against a throwaway copy of ``src``, with
``sys.platform`` temporarily set, so the actual spec text is what gets tested.
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "main.spec"
ASSET_DIRS = ("src/gui/assets", "src/i18n")
# Everything before the hiddenimports block: version info + datas construction.
# Stops before Analysis(), which needs PyInstaller's own globals.
HEAD_MARKER = "# ------------------------------------------------------------ hidden imports"


def _project_copy(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    shutil.copytree(ROOT / "src", proj / "src",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("main.spec", "LICENSE", "README.md"):
        src = ROOT / name
        if src.exists():
            shutil.copy(src, proj / name)
    return proj


def _run_spec(proj: Path, platform: str) -> dict:
    head = SPEC.read_text(encoding="utf-8").split(HEAD_MARKER)[0]
    assert "a = Analysis(" not in head
    ns = {"SPECPATH": str(proj)}
    real = sys.platform
    sys.platform = platform          # the spec does its own `import sys`
    try:
        exec(compile(head, "main.spec", "exec"), ns)  # noqa: S102 - testing the spec
    finally:
        sys.platform = real
    return ns


@pytest.fixture
def win_spec(tmp_path):
    return _run_spec(_project_copy(tmp_path), "win32")


# ------------------------------------------------------------------- Windows

def test_windows_branch_builds_one_archive(win_spec):
    datas = win_spec["datas"]
    assert len(datas) == 1, f"expected a single archive entry, got {datas}"
    src, dest = datas[0]
    assert Path(src).name == win_spec["RESOURCES_ARCHIVE"]
    assert dest == "."
    assert Path(src).is_file(), "the spec did not create the archive"


def test_windows_archive_matches_the_source_tree(win_spec, tmp_path):
    proj = tmp_path / "proj"
    archive = Path(win_spec["datas"][0][0])
    with zipfile.ZipFile(archive) as zf:
        names = sorted(zf.namelist())

    expected = sorted(
        p.relative_to(proj).as_posix()
        for d in ASSET_DIRS
        for p in (proj / d).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert names == expected
    assert not any("__pycache__" in n for n in names)


def test_windows_archive_carries_desktop_lyrics(win_spec):
    with zipfile.ZipFile(Path(win_spec["datas"][0][0])) as zf:
        names = zf.namelist()
    assert "src/gui/assets/desktop-lyrics.html" in names
    assert "src/gui/assets/desktop-lyrics.js" in names


def test_windows_archive_members_cannot_escape(win_spec):
    with zipfile.ZipFile(Path(win_spec["datas"][0][0])) as zf:
        for name in zf.namelist():
            assert not name.startswith("/")
            assert ".." not in Path(name).parts


def test_windows_build_has_no_zip_left_in_the_project(tmp_path):
    proj = _project_copy(tmp_path)
    _run_spec(proj, "win32")
    # the staging dir must be cleaned up after zipping
    assert not (proj / "build" / "ncm-resources").exists()


# --------------------------------------------------------------------- macOS

def test_macos_keeps_plain_directory_datas(tmp_path):
    ns = _run_spec(_project_copy(tmp_path), "darwin")
    datas = dict(ns["datas"])
    # macOS must ship the real directories, never the zip
    assert datas["src/gui/assets"] == "src/gui/assets"
    assert datas["src/i18n"] == "src/i18n"
    assert not any("ncm-resources.zip" in str(k) for k in datas), \
        "macOS must not ship the Windows resource archive"
    assert not any(".zip" in str(k) for k in datas)


def test_macos_still_ships_licence_and_readme(tmp_path):
    ns = _run_spec(_project_copy(tmp_path), "darwin")
    names = {src for src, _ in ns["datas"]}
    assert "LICENSE" in names
    assert "README.md" in names


def test_archive_name_matches_the_runtime_constant():
    """main.spec and src/resources.py must agree on the archive filename."""
    import re

    const = re.search(r'^RESOURCES_ARCHIVE\s*=\s*"([^"]+)"',
                      (ROOT / "src" / "resources.py").read_text(encoding="utf-8"),
                      re.MULTILINE).group(1)
    assert const == "ncm-resources.zip"
