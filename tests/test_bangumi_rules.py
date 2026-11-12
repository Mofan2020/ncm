"""Kazumi 规则的解析、校验、仓库拉取。

用真实的规则样本（从 KazumiRules 仓库里抄下来的几条）当夹具，而不是自己编
几个好看的 JSON——编出来的样本只能证明代码符合我的想象。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.core.bangumi_rules import (
    DEFAULT_MIRRORS,
    KAZUMI_RULES_REPO,
    Rule,
    RuleCache,
    RuleError,
    RuleLibrary,
    github_owner_repo,
    mirrored_urls,
    normalise_repo_url,
    parse_index,
    parse_rules,
    raw_url_for,
)

# 真实样本：api=5 的 AGE 和 api=7 的 FQDM（截取关键字段）
AGE_RULE = {
    "api": "5",
    "type": "anime",
    "name": "AGE",
    "version": "1.6",
    "muliSources": True,
    "adBlocker": True,
    "useWebview": True,
    "useNativePlayer": True,
    "userAgent": "",
    "baseURL": "https://www.agedm.io/",
    "searchURL": "https://www.agedm.io/search?query=@keyword",
    "searchList": "//div[2]/div/section/div/div/div/div",
    "searchName": "//div/div[2]/h5/a",
    "searchResult": "//div/div[2]/h5/a",
    "chapterRoads": "//div[2]/div/section/div/div[2]/div[2]/div[2]/div",
    "chapterResult": "//ul/li/a",
}

FQDM_RULE = {
    "deprecated": True,
    "api": "7",
    "type": "anime",
    "name": "FQDM",
    "version": "2.0",
    "muliSources": True,
    "useWebview": True,
    "useNativePlayer": True,
    "usePost": False,
    "useLegacyParser": True,
    "adBlocker": True,
    "userAgent": "",
    "baseURL": "https://www.fqdm.cc/",
    "searchURL": "https://www.fqdm.cc/index.php/vod/search/wd/@keyword.html",
    "searchList": "//div[@class='module-items module-card-items']/div[contains(@class, 'module-card-item')]",
    "searchName": ".//div[@class='module-card-item-title']/a",
    "searchResult": ".//div[@class='module-card-item-title']/a",
    "chapterRoads": "//div[contains(@class,'module-play-list-content')]",
    "chapterResult": "//a[contains(@class,'module-play-list-link')]",
    "referer": "",
}


# ------------------------------------------------------------------ 单条解析


def test_parses_a_real_rule() -> None:
    rule = Rule.from_dict(AGE_RULE)
    assert rule.name == "AGE"
    assert rule.api == "5"
    assert rule.base_url == "https://www.agedm.io/"
    assert rule.search_url.endswith("@keyword")
    assert rule.search_list
    assert rule.usable, rule.problems


def test_parses_the_newer_api_shape() -> None:
    rule = Rule.from_dict(FQDM_RULE)
    assert rule.api == "7"
    assert rule.deprecated is True
    assert rule.use_legacy_parser is True
    assert rule.usable, rule.problems


def test_keeps_the_muli_sources_typo() -> None:
    """Kazumi 的字段就叫 muliSources（少个 t）。改名会让真实规则读不出来。"""
    rule = Rule.from_dict({"name": "x", "searchURL": "u", "searchList": "//a", "muliSources": False})
    assert rule.multi_sources is False
    assert "muliSources" in rule.to_dict()


def test_flags_accept_strings() -> None:
    """YAML/JSON 里布尔值经常写成字符串，照单全收。"""
    rule = Rule.from_dict({
        "name": "x", "searchURL": "u", "searchList": "//a",
        "usePost": "true", "deprecated": "1", "useWebview": "false",
    })
    assert rule.use_post is True
    assert rule.deprecated is True
    assert rule.use_webview is False


def test_missing_name_falls_back_to_the_host() -> None:
    rule = Rule.from_dict({"baseURL": "https://example.com/", "searchURL": "u", "searchList": "//a"})
    assert rule.name == "example.com"


def test_no_name_and_no_base_is_an_error() -> None:
    with pytest.raises(RuleError):
        Rule.from_dict({"searchURL": "u"})


def test_non_dict_is_an_error() -> None:
    with pytest.raises(RuleError):
        Rule.from_dict(["nope"])  # type: ignore[arg-type]


# ------------------------------------------------------------------ 校验


def test_broken_xpath_is_recorded_not_raised() -> None:
    """仓库里真有写错的规则。一条坏了不该让整个仓库不可用，但必须能查。"""
    rule = Rule.from_dict({
        "name": "broken",
        "searchURL": "https://e.com/s?q=@keyword",
        "searchList": "//",              # 光两个斜杠，lxml 直接拒
        "chapterResult": "//ol[@class*='x']",   # CSS 语法写进了 XPath
    })
    assert "searchList" in rule.problems
    assert "chapterResult" in rule.problems
    assert not rule.usable


def test_good_xpath_has_no_problems() -> None:
    rule = Rule.from_dict(AGE_RULE)
    assert rule.problems == {}


def test_usable_needs_search_url_and_list() -> None:
    assert not Rule(name="a", search_url="", search_list="//a").usable
    assert not Rule(name="a", search_url="u", search_list="").usable
    assert Rule(name="a", search_url="u", search_list="//a").usable


# ------------------------------------------------------------------ 链接


def test_keyword_replacement() -> None:
    rule = Rule(name="a", search_url="https://e.com/s?q=@keyword&p=1")
    assert rule.search_link("孤独摇滚") == "https://e.com/s?q=孤独摇滚&p=1"


def test_keyword_without_placeholder_is_an_error() -> None:
    with pytest.raises(RuleError):
        Rule(name="a", search_url="").search_link("x")


@pytest.mark.parametrize(
    "href,expected",
    [
        ("https://other.com/a", "https://other.com/a"),
        ("/v/1", "https://www.agedm.io/v/1"),
        ("v/1", "https://www.agedm.io/v/1"),
        ("//cdn.com/a", "https://cdn.com/a"),
        ("", ""),
    ],
)
def test_absolute(href: str, expected: str) -> None:
    rule = Rule(name="a", base_url="https://www.agedm.io/")
    assert rule.absolute(href) == expected


# ------------------------------------------------------------------ 仓库


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://github.com/Owner/Repo", "https://github.com/Owner/Repo"),
        ("https://github.com/Owner/Repo.git", "https://github.com/Owner/Repo"),
        ("https://github.com/Owner/Repo/", "https://github.com/Owner/Repo"),
        ("  https://github.com/Owner/Repo  ", "https://github.com/Owner/Repo"),
        ("https://example.com/rules", "https://example.com/rules"),
        ("", ""),
    ],
)
def test_normalise_repo_url(raw: str, expected: str) -> None:
    assert normalise_repo_url(raw) == expected


def test_github_owner_repo() -> None:
    assert github_owner_repo("https://github.com/Predidit/KazumiRules") == ("Predidit", "KazumiRules")
    assert github_owner_repo("https://example.com/x/y") is None


def test_raw_url_for_github() -> None:
    url = raw_url_for("https://github.com/Predidit/KazumiRules", "index.json")
    assert url == "https://raw.githubusercontent.com/Predidit/KazumiRules/main/index.json"


def test_raw_url_for_other_host() -> None:
    url = raw_url_for("https://example.com/rules", "index.json")
    assert url == "https://example.com/rules/index.json"


def test_mirrors_are_skipped_when_auto_mirror_is_off() -> None:
    urls = mirrored_urls("https://raw.githubusercontent.com/a/b/main/x.json", auto_mirror=False)
    assert urls == ["https://raw.githubusercontent.com/a/b/main/x.json"]


def test_mirrors_are_all_tried_when_on() -> None:
    urls = mirrored_urls("https://raw.githubusercontent.com/a/b/main/x.json", auto_mirror=True)
    assert len(urls) == len(DEFAULT_MIRRORS)
    assert urls[0].startswith("https://raw.githubusercontent.com")


def test_custom_mirrors_replace_the_defaults() -> None:
    urls = mirrored_urls("https://x/y", ["{url}", "https://m/{url}"], auto_mirror=True)
    assert urls == ["https://x/y", "https://m/https://x/y"]


# ------------------------------------------------------------------ 索引


def test_parse_index_from_the_real_shape() -> None:
    payload = [
        {"name": "AGE", "version": "1.6", "useNativePlayer": True,
         "antiCrawlerEnabled": False, "author": "", "lastUpdate": 1793200411000},
        {"name": "FQDM", "version": "2.0", "author": "x", "lastUpdate": 1},
    ]
    items = parse_index(payload)
    assert [i["name"] for i in items] == ["AGE", "FQDM"]
    assert items[0]["last_update"] == 1793200411000


def test_parse_index_tolerates_junk() -> None:
    payload = [{"name": ""}, "nope", {"version": "1"}, {"name": "ok"}]
    assert [i["name"] for i in parse_index(payload)] == ["ok"]


def test_parse_index_of_something_else_is_empty() -> None:
    assert parse_index("nope") == []
    assert parse_index([1, 2, 3]) == []


# ------------------------------------------------------------------ 打包形状


def test_parse_rules_single_object() -> None:
    rules = parse_rules(AGE_RULE)
    assert [r.name for r in rules] == ["AGE"]


def test_parse_rules_array() -> None:
    rules = parse_rules([AGE_RULE, FQDM_RULE])
    assert [r.name for r in rules] == ["AGE", "FQDM"]


def test_parse_rules_wrapped() -> None:
    rules = parse_rules({"rules": [AGE_RULE]})
    assert [r.name for r in rules] == ["AGE"]


def test_parse_rules_skips_index_entries() -> None:
    """索引条目只有 name/version，硬当规则解析会报一堆错。"""
    rules = parse_rules([{"name": "AGE", "version": "1.6"}, AGE_RULE])
    assert [r.name for r in rules] == ["AGE"]


# ------------------------------------------------------------------ 缓存与库


class FakeFetcher:
    """按 URL 给答案的取页器。记下被问过哪些地址。"""

    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    def fetch_rules(self, url: str):  # noqa: ANN201
        self.asked.append(url)
        for key, value in self.pages.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise RuntimeError(f"no answer for {url}")


def test_cache_roundtrip(tmp_path: pathlib.Path) -> None:
    cache = RuleCache(tmp_path)
    cache.write(KAZUMI_RULES_REPO, "index", [{"name": "AGE"}])
    data, stale = cache.read(KAZUMI_RULES_REPO, "index", ttl=3600)
    assert data == [{"name": "AGE"}]
    assert stale is False


def test_cache_reports_stale(tmp_path: pathlib.Path) -> None:
    cache = RuleCache(tmp_path)
    cache.write(KAZUMI_RULES_REPO, "index", [])
    data, stale = cache.read(KAZUMI_RULES_REPO, "index", ttl=-1)
    assert data == []
    assert stale is True


def test_cache_missing_is_stale(tmp_path: pathlib.Path) -> None:
    data, stale = RuleCache(tmp_path).read("https://github.com/a/b", "index")
    assert data is None
    assert stale is True


def test_library_lists_from_the_network_then_the_cache(tmp_path: pathlib.Path) -> None:
    fetcher = FakeFetcher({"index.json": [{"name": "AGE", "version": "1.6"}]})
    library = RuleLibrary(RuleCache(tmp_path), fetcher)

    items, source = library.list_rules(KAZUMI_RULES_REPO)
    assert [i["name"] for i in items] == ["AGE"]
    assert source == "network"

    items2, source2 = library.list_rules(KAZUMI_RULES_REPO)
    assert [i["name"] for i in items2] == ["AGE"]
    assert source2 == "cache"
    assert len(fetcher.asked) == 1, "第二次不该再请求"


def test_library_falls_back_to_the_stale_copy(tmp_path: pathlib.Path) -> None:
    cache = RuleCache(tmp_path)
    cache.write(KAZUMI_RULES_REPO, "index", [{"name": "OLD"}])
    # 把 mtime 弄老，让缓存过期
    path = cache._path(KAZUMI_RULES_REPO, "index")
    import os

    os.utime(path, (0, 0))

    fetcher = FakeFetcher({"index.json": RuntimeError("network down")})
    library = RuleLibrary(cache, fetcher)
    items, source = library.list_rules(KAZUMI_RULES_REPO)
    assert [i["name"] for i in items] == ["OLD"]
    assert source.startswith("cache(stale)")


def test_library_raises_when_nothing_is_cached_and_network_fails(tmp_path: pathlib.Path) -> None:
    fetcher = FakeFetcher({"index.json": RuntimeError("down")})
    with pytest.raises(RuleError):
        RuleLibrary(RuleCache(tmp_path), fetcher).list_rules(KAZUMI_RULES_REPO)


def test_library_loads_one_rule(tmp_path: pathlib.Path) -> None:
    fetcher = FakeFetcher({"AGE.json": AGE_RULE})
    library = RuleLibrary(RuleCache(tmp_path), fetcher)
    rule = library.load_rule(KAZUMI_RULES_REPO, "AGE")
    assert rule.name == "AGE"
    assert rule.usable


def test_library_caches_the_rule_file(tmp_path: pathlib.Path) -> None:
    fetcher = FakeFetcher({"AGE.json": AGE_RULE})
    library = RuleLibrary(RuleCache(tmp_path), fetcher)
    library.load_rule(KAZUMI_RULES_REPO, "AGE")
    library.load_rule(KAZUMI_RULES_REPO, "AGE")
    assert len(fetcher.asked) == 1


def test_library_accepts_a_repo_that_is_one_file(tmp_path: pathlib.Path) -> None:
    """自定义仓库常把多条规则打包进一个 json，地址直接指到文件。"""
    fetcher = FakeFetcher({"myrules.json": [AGE_RULE, FQDM_RULE]})
    library = RuleLibrary(RuleCache(tmp_path), fetcher)
    rule = library.load_rule("https://example.com/myrules.json", "FQDM")
    assert rule.name == "FQDM"


def test_library_falls_back_to_a_bundle_file(tmp_path: pathlib.Path) -> None:
    """仓库目录里放的是 rules.json 而不是每条一个文件，也要找得到。"""
    fetcher = FakeFetcher({"rules.json": [AGE_RULE, FQDM_RULE]})
    library = RuleLibrary(RuleCache(tmp_path), fetcher)
    rule = library.load_rule("https://github.com/me/rules", "AGE")
    assert rule.name == "AGE"


def test_library_reports_an_unknown_name(tmp_path: pathlib.Path) -> None:
    fetcher = FakeFetcher({"myrules.json": [AGE_RULE, FQDM_RULE]})
    library = RuleLibrary(RuleCache(tmp_path), fetcher)
    with pytest.raises(RuleError):
        library.load_rule("https://example.com/myrules.json", "NOPE")


def test_library_caches_a_bundle_by_repo(tmp_path: pathlib.Path) -> None:
    fetcher = FakeFetcher({"myrules.json": [AGE_RULE, FQDM_RULE]})
    library = RuleLibrary(RuleCache(tmp_path), fetcher)
    library.load_rule("https://example.com/myrules.json", "AGE")
    library.load_rule("https://example.com/myrules.json", "FQDM")
    assert len(fetcher.asked) == 1, "同一个打包文件不该拉两次"


def test_library_without_a_repo_is_an_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RuleError):
        RuleLibrary(RuleCache(tmp_path), FakeFetcher({})).list_rules("")


def test_rule_roundtrips_through_to_dict() -> None:
    rule = Rule.from_dict(FQDM_RULE)
    again = Rule.from_dict(rule.to_dict())
    assert again.name == rule.name
    assert again.use_post == rule.use_post
    assert again.deprecated == rule.deprecated
    assert again.referer == rule.referer


def test_json_serialisable() -> None:
    """规则要能跨 bridge 传给前端，转不成 JSON 的就是 bug。"""
    rule = Rule.from_dict(AGE_RULE)
    json.dumps(rule.to_dict(), ensure_ascii=False)
