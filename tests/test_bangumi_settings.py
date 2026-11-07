"""Bangumi 设置的清洗和持久化。

设置是用户手填的，所以要防手滑：主机名里混进路径、镜像没有主机、
资源站的模板一个占位符都没有——这些都得被挡下来，而不是存进配置文件
再在别的什么地方炸掉。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from src.config.settings import AppSettings, Settings


def fresh_settings(tmp_path: Path) -> Settings:
    """A Settings instance of its own, pointed at ``tmp_path``.

    ``Settings`` is a singleton: ``Settings()`` hands out the same object
    every time and ``__init__`` returns early after the first call, so a
    second ``Settings()`` would keep the first one's config path.  Building
    the object with ``object.__new__`` sidesteps that and gives every test
    a clean instance.
    """
    instance = object.__new__(Settings)
    instance._initialized = True
    instance.logger = logging.getLogger("ncm.settings.test")
    instance._data = AppSettings()
    instance._config_path = tmp_path / "settings.yaml"
    instance._save_lock = threading.Lock()
    instance.load()
    return instance


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return fresh_settings(tmp_path)


# ------------------------------------------------------------------ 主机名
def test_host_is_cleaned(settings: Settings):
    from src.config.settings import _clean_host

    assert _clean_host("bangumi.lat") == "bangumi.lat"
    assert _clean_host("https://bangumi.lat/") == "bangumi.lat"
    assert _clean_host("api.bgm.tv/v0/subjects") == "api.bgm.tv"
    assert _clean_host("  api.bgm.tv  ") == "api.bgm.tv"
    assert _clean_host("api.bgm.tv:443") == "api.bgm.tv"
    assert _clean_host("user@api.bgm.tv") == "api.bgm.tv"
    assert _clean_host("") == ""
    assert _clean_host("有 空格") == ""
    # A path in there is dropped, not rejected: the host part is what
    # matters and the user typed a url by mistake.
    assert _clean_host("has/slash") == "has"


# -------------------------------------------------------------------- 镜像
def test_mirrors_keep_the_good_ones(settings: Settings):
    from src.config.settings import _clean_mirrors

    result = _clean_mirrors([
        {"id": "a", "api_hosts": ["api.a.test"], "main_hosts": ["a.test"]},
        {"id": "b", "api_hosts": [], "main_hosts": []},     # 没有主机，丢掉
        "not a dict",                                       # 丢掉
        {"id": "c", "api_hosts": ["api.c.test"]},
    ])
    assert [item["id"] for item in result] == ["a", "c"]
    assert result[0]["api_hosts"] == ["api.a.test"]


def test_mirrors_deduplicate_by_id(settings: Settings):
    from src.config.settings import _clean_mirrors

    result = _clean_mirrors([
        {"id": "same", "api_hosts": ["api.a.test"]},
        {"id": "same", "api_hosts": ["api.b.test"]},
    ])
    assert len(result) == 1
    assert result[0]["api_hosts"] == ["api.a.test"]


def test_mirror_id_defaults_to_the_host(settings: Settings):
    from src.config.settings import _clean_mirrors

    result = _clean_mirrors([{"api_hosts": ["api.a.test"]}])
    assert result[0]["id"] == "api.a.test"


def test_mirrors_clean_the_host_names(settings: Settings):
    from src.config.settings import _clean_mirrors

    result = _clean_mirrors([
        {"id": "a", "api_hosts": ["https://api.a.test/v0"]}])
    assert result[0]["api_hosts"] == ["api.a.test"]


# ---------------------------------------------------------------- 资源站
def test_resource_sites_without_a_url_are_dropped(settings: Settings):
    from src.config.settings import _clean_resource_sites

    result = _clean_resource_sites([
        {"id": "a", "detail_url": "", "search_url": ""},     # 没有地址，丢掉
        {"id": "b", "detail_url": "https://b.test/{keyword}"},
        {"id": "c", "search_url": "https://c.test/?q={keyword}"},
    ])
    assert [item["id"] for item in result] == ["b", "c"]


def test_resource_site_keeps_the_placeholder(settings: Settings):
    """模板里的占位符必须留着——那是模板的意义所在。"""
    from src.config.settings import _clean_resource_sites

    result = _clean_resource_sites([
        {"id": "a", "detail_url": "https://a.test/{keyword}/{ep}"}])
    assert result[0]["detail_url"] == "https://a.test/{keyword}/{ep}"


def test_resource_site_is_enabled_by_default(settings: Settings):
    from src.config.settings import _clean_resource_sites

    result = _clean_resource_sites([{"id": "a", "detail_url": "https://a.test"}])
    assert result[0]["enabled"] is True


# ---------------------------------------------------------------- 持久化
def test_bangumi_settings_survive_a_reload(settings: Settings, tmp_path: Path):
    settings._data.bangumi.api_host = "api.my.test"
    settings._data.bangumi.web_host = "my.test"
    settings._data.bangumi.request_interval = 2.5
    settings._data.bangumi.skip_ssl_verify = True
    settings._data.bangumi.mirrors = [
        {"id": "a", "name": "甲", "api_hosts": ["api.a.test"],
         "main_hosts": [], "recommended": False, "note": ""}]
    settings._data.bangumi.resource_sites = [
        {"id": "s1", "name": "站1", "kind": "custom", "enabled": True,
         "detail_url": "https://x.test/{keyword}", "search_url": "",
         "note": ""}]
    settings.save()

    again = fresh_settings(tmp_path)
    assert again._data.bangumi.api_host == "api.my.test"
    assert again._data.bangumi.web_host == "my.test"
    assert again._data.bangumi.request_interval == 2.5
    assert again._data.bangumi.skip_ssl_verify is True
    assert again._data.bangumi.mirrors[0]["id"] == "a"
    assert again._data.bangumi.mirrors[0]["api_hosts"] == ["api.a.test"]
    assert again._data.bangumi.resource_sites[0]["detail_url"] == \
        "https://x.test/{keyword}"


def test_defaults_are_sane(settings: Settings):
    assert settings._data.bangumi.api_host == "api.bangumi.lat"
    assert settings._data.bangumi.web_host == "bangumi.lat"
    assert settings._data.bangumi.request_interval == 1.0
    assert settings._data.bangumi.skip_ssl_verify is False
    assert settings._data.bangumi.mirrors == []
    assert settings._data.bangumi.resource_sites == []


def test_the_interval_is_clamped(settings: Settings):
    settings._data.bangumi.request_interval = 999
    settings._normalize()
    assert settings._data.bangumi.request_interval == 10.0

    settings._data.bangumi.request_interval = -5
    settings._normalize()
    assert settings._data.bangumi.request_interval == 0.0


def test_a_broken_bangumi_section_does_not_break_the_load(
        settings: Settings, tmp_path: Path):
    """手改坏的文件不该让整个设置读不出来。"""
    (tmp_path / "settings.yaml").write_text(
        "bangumi:\n  api_host: 123\n  mirrors: not-a-list\n",
        encoding="utf-8")
    instance = fresh_settings(tmp_path)
    assert instance._data.bangumi.api_host == "api.bangumi.lat"
    assert instance._data.bangumi.mirrors == []


def test_the_ssl_flag_reads_a_string(settings: Settings, tmp_path: Path):
    """YAML 里写成字符串也要认。"""
    (tmp_path / "settings.yaml").write_text(
        "bangumi:\n  skip_ssl_verify: 'true'\n", encoding="utf-8")
    instance = fresh_settings(tmp_path)
    assert instance._data.bangumi.skip_ssl_verify is True
