"""The startup report must not be written into the app bundle.

A file added to a signed macOS bundle invalidates its code signature:
``codesign --verify`` starts failing and macOS can refuse to start the
app on the next launch.  The app writes this report on every start, so
the place it lands is not a detail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import main


def test_the_frozen_report_does_not_land_next_to_the_executable(monkeypatch,
                                                                tmp_path: Path) -> None:
    """打包之后报告写到用户目录，不是可执行文件旁边。"""
    exe_dir = tmp_path / "NeteaseMusicDownloader.app" / "Contents" / "MacOS"
    exe_dir.mkdir(parents=True)
    monkeypatch.setattr(main, "is_frozen", lambda: True)
    monkeypatch.setattr(main.sys, "executable", str(exe_dir / "NeteaseMusicDownloader"))
    monkeypatch.setattr(main, "_diagnostics_dir", lambda: tmp_path / "state")

    main._write_load_diagnostics(None, "http://127.0.0.1:1/",
                                 tmp_path / "index.html")

    report = tmp_path / "state" / "ncm-startup.log"
    assert report.is_file()
    assert not (exe_dir / "ncm-startup.log").exists()


def test_the_report_stays_next_to_the_source_when_it_is_not_packaged(
        monkeypatch, tmp_path: Path) -> None:
    """从源码跑的时候报告还在工作目录，方便开发时看。"""
    monkeypatch.setattr(main, "is_frozen", lambda: False)
    monkeypatch.chdir(tmp_path)

    main._write_load_diagnostics(None, "http://127.0.0.1:1/",
                                 tmp_path / "index.html")

    assert (tmp_path / "ncm-startup.log").is_file()


def test_the_diagnostics_dir_is_the_settings_directory() -> None:
    """报告和设置文件放在一起，都在包外面。"""
    from src.config import get_settings

    assert main._diagnostics_dir() == Path(get_settings().config_path).parent
    assert main._diagnostics_dir() != Path(sys.executable).resolve().parent
