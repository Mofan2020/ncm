"""Tests for the settings singleton (platform paths, persistence, clamping)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from src.config import default_download_dir, get_settings
from src.version import APP_NAME


def test_config_path_uses_app_data_dir(isolated_home):
    settings = get_settings()
    path = settings.config_path
    assert path.name == "settings.yaml"
    assert APP_NAME in str(path)
    if sys.platform == "darwin":
        assert str(path).startswith(str(isolated_home))
    assert path.parent.is_dir(), "the config directory is created on demand"


def test_defaults(isolated_home):
    settings = get_settings()
    assert settings.download.quality == "standard"
    assert settings.download.max_concurrent == 2
    assert settings.download.overwrite is False
    assert settings.ui.language == "zh_cn"
    assert settings.ui.theme == "system"
    assert settings.auth.remember_login is False
    assert settings.debug is False


def test_roundtrip_persistence(isolated_home):
    from src.config import settings as settings_module

    settings = get_settings()
    settings.update_download(quality="lossless", max_concurrent=3, overwrite=True,
                             download_dir="/tmp/somewhere")
    settings.update_ui(language="en_us", theme="dark")
    settings.update_auth(remember_login=True, saved_cookie="MUSIC_U=abc")

    settings_module.Settings._instance = None
    reloaded = get_settings()
    assert reloaded.download.quality == "lossless"
    assert reloaded.download.max_concurrent == 3
    assert reloaded.download.overwrite is True
    assert reloaded.download.download_dir == "/tmp/somewhere"
    assert reloaded.ui.language == "en_us"
    assert reloaded.ui.theme == "dark"
    assert reloaded.auth.remember_login is True
    assert reloaded.auth.saved_cookie == "MUSIC_U=abc"


def test_max_concurrent_is_clamped_to_three(isolated_home):
    settings = get_settings()
    settings.update_download(max_concurrent=99)
    assert settings.download.max_concurrent == 3
    settings.update_download(max_concurrent=0)
    assert settings.download.max_concurrent == 1
    settings.update_download(max_concurrent="2")
    assert settings.download.max_concurrent == 2


def test_unknown_keys_are_ignored(isolated_home):
    settings = get_settings()
    settings.update_download(quality="exhigh", skip_url_check=True, nonsense=1)
    assert settings.download.quality == "exhigh"
    assert not hasattr(settings.download, "skip_url_check")


def test_legacy_settings_file_with_stale_keys_still_loads(isolated_home):
    """An older settings.yaml (with removed fields) must not wipe the config."""
    from src.config import settings as settings_module

    path = get_settings().config_path
    path.write_text(
        "download:\n  quality: hires\n  skip_url_check: true\n  max_concurrent: 7\n"
        "ui:\n  language: en_us\n  theme: dark\n  unknown_field: x\n"
        "auth:\n  remember_login: true\n"
        "debug: true\n",
        encoding="utf-8",
    )
    settings_module.Settings._instance = None
    reloaded = get_settings()
    assert reloaded.download.quality == "hires"
    assert reloaded.download.max_concurrent == 7
    assert reloaded.ui.language == "en_us"
    assert reloaded.debug is True


def test_reset_restores_defaults(isolated_home):
    settings = get_settings()
    settings.update_download(quality="hires", max_concurrent=3)
    settings.reset()
    assert settings.download.quality == "standard"
    assert settings.download.max_concurrent == 2


def test_corrupt_file_falls_back_to_defaults(isolated_home):
    from src.config import settings as settings_module

    get_settings().config_path.write_text("not: [valid: yaml", encoding="utf-8")
    settings_module.Settings._instance = None
    settings = get_settings()
    assert settings.download.quality == "standard"


def test_default_download_dir_is_under_home(isolated_home):
    path = Path(default_download_dir())
    assert str(path).startswith(str(isolated_home))
    assert "Downloads" in str(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_config_dir_permissions_are_default(isolated_home):
    mode = get_settings().config_path.parent.stat().st_mode
    assert mode & 0o700  # owner can read/write/traverse


def test_settings_is_marked_ready_only_after_it_is_usable(isolated_home):
    """Another thread may grab the singleton mid-init; it must not look ready."""
    from src.config.settings import Settings

    Settings._instance = None
    seen = {}
    original = Settings._config_path_for_platform

    def spy(self):
        seen["ready_during_init"] = getattr(self, "_initialized", False)
        return original(self)

    Settings._config_path_for_platform = spy
    try:
        settings = Settings()
    finally:
        Settings._config_path_for_platform = original

    assert seen["ready_during_init"] is False
    assert settings.config_path.name.endswith(".yaml")
