"""The Android entry point: the paths and the logging it sets up.

Nothing here needs a phone.  The two things that matter are the
directories the app writes into -- settings, logs and the cache -- and
the log level, because a log file that stays empty is no log at all.

These are checked here because they are the parts of the Android build
that no desktop run exercises: on the desktop the paths come from the
environment and the level comes from the command line.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = ROOT / "android" / "main.py"


def _load_main():
    """Load android/main.py as a module.

    It lives outside the package, so it is loaded by path.  The module
    name is unique per test run to keep the import system from caching a
    stale copy.
    """
    name = "ncm_android_main_under_test"
    spec = importlib.util.spec_from_file_location(name, MAIN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture()
def main_module():
    return _load_main()


# ------------------------------------------------------------------- paths
def test_prepare_home_sets_the_four_directories(main_module, tmp_path,
                                                monkeypatch):
    """The app writes its settings, logs and cache into its own directory.

    Android hands the process no HOME and no ANDROID_PRIVATE at all --
    the environment of a running app holds only the variables the system
    sets (checked on an Android 14 emulator, /proc/<pid>/environ).  The
    private directory is still known, because "~" resolves through the
    password database to the app's own files directory.
    """
    private = tmp_path / "files"
    private.mkdir()
    monkeypatch.delenv("ANDROID_PRIVATE", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda _: str(private))
    for name in ("HOME", "XDG_CONFIG_HOME", "NCM_LOG_DIR", "TMPDIR"):
        monkeypatch.delenv(name, raising=False)

    main_module._prepare_home()

    assert os.environ["HOME"] == str(private)
    assert os.environ["XDG_CONFIG_HOME"] == str(private / ".config")
    assert os.environ["NCM_LOG_DIR"] == str(private / "logs")
    assert os.environ["TMPDIR"] == str(private / "tmp")
    # The directories themselves have to exist: the log handler creates
    # its own, but the settings and the cache do not.
    assert (private / ".config").is_dir()
    assert (private / "logs").is_dir()
    assert (private / "tmp").is_dir()


def test_prepare_home_keeps_the_config_where_it_already_is(main_module,
                                                            tmp_path,
                                                            monkeypatch):
    """The settings must not move when the app is upgraded.

    An existing installation keeps its login and its cache in
    <private>/.config/NeteaseMusicDownloader, so XDG_CONFIG_HOME has to
    stay exactly there.
    """
    private = tmp_path / "files"
    private.mkdir()
    monkeypatch.setenv("ANDROID_PRIVATE", str(private))
    for name in ("HOME", "XDG_CONFIG_HOME", "NCM_LOG_DIR", "TMPDIR"):
        monkeypatch.delenv(name, raising=False)

    main_module._prepare_home()

    assert os.environ["XDG_CONFIG_HOME"] == str(private / ".config")


def test_prepare_home_does_nothing_without_a_usable_directory(main_module,
                                                               monkeypatch,
                                                               tmp_path):
    """A directory that is not absolute is no directory at all.

    Falling back to it would put the settings somewhere they cannot be
    read back.
    """
    monkeypatch.delenv("ANDROID_PRIVATE", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda _: "")
    for name in ("HOME", "XDG_CONFIG_HOME", "NCM_LOG_DIR", "TMPDIR"):
        monkeypatch.delenv(name, raising=False)

    main_module._prepare_home()

    assert "XDG_CONFIG_HOME" not in os.environ
    assert "NCM_LOG_DIR" not in os.environ


# ----------------------------------------------------------------- logging
def test_the_log_file_gets_the_ordinary_lines(main_module, tmp_path,
                                              monkeypatch):
    """INFO, not WARNING.

    With the level at WARNING every ordinary line was dropped, the log
    file stayed empty, and a crash on the phone left nothing to read.
    """
    monkeypatch.setenv("NCM_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        root.handlers.clear()
        main_module._configure_logging(debug=False)
        logging.getLogger("ncm.test").info("一条普通的信息")
        for handler in root.handlers:
            handler.flush()
        log_file = tmp_path / "ncm.log"
        assert log_file.is_file()
        text = log_file.read_text(encoding="utf-8")
        assert "一条普通的信息" in text, text
    finally:
        for handler in list(root.handlers):
            handler.close()
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


def test_the_stream_handler_carries_the_same_level(main_module, tmp_path,
                                                    monkeypatch):
    """The stream is what logcat shows.

    python-for-android pipes stdout and stderr into logcat, and logcat
    is the only place a crash on the phone can be read from, so the
    stream cannot be quieter than the file.
    """
    monkeypatch.setenv("NCM_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        root.handlers.clear()
        main_module._configure_logging(debug=False)
        levels = [h.level for h in root.handlers]
        assert levels, "no handler was installed"
        for level in levels:
            assert level <= logging.INFO, levels
    finally:
        for handler in list(root.handlers):
            handler.close()
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


def test_debug_mode_is_still_verbose(main_module, tmp_path, monkeypatch):
    """The debug switch keeps its meaning."""
    monkeypatch.setenv("NCM_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        root.handlers.clear()
        main_module._configure_logging(debug=True)
        logging.getLogger("ncm.test").debug("一条调试信息")
        for handler in root.handlers:
            handler.flush()
        text = (tmp_path / "ncm.log").read_text(encoding="utf-8")
        assert "一条调试信息" in text, text
    finally:
        for handler in list(root.handlers):
            handler.close()
        root.handlers[:] = old_handlers
        root.setLevel(old_level)
