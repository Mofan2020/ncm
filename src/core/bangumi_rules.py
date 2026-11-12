"""Kazumi 规则：解析、校验、从仓库拉取。

规则是 Kazumi（一个 Flutter 番剧客户端）那套 JSON，社区仓库在
`Predidit/KazumiRules`。一条规则用 XPath 描述三件事：

* 怎么搜 —— ``searchURL`` 里把 ``@keyword`` 换成关键词；
* 结果怎么取 —— ``searchList`` 选出每一条，``searchName`` 取标题，
  ``searchResult`` 取详情页链接；
* 章节怎么取 —— ``chapterRoads`` 选出线路分组，``chapterResult`` 取每集链接。

**规则里没有视频地址。** Kazumi 是把详情页塞进一个 webview，然后 hook
JS 或拦网络请求拿到 m3u8/mp4。这一点在仓库 README 里写得很清楚
（"JavaScript hook-based parser"、"Legacy Parser: iframe src-based"）。
所以本模块只负责"搜到哪一集"，取流地址是 :mod:`bangumi_source` 的事。

字段随 API 版本增长（仓库里同时存在 api 1 到 9），这里按字段名容错读取：
认不出的键原样留着，缺的不报错——一条规则坏了不该让整个仓库不可用。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 官方规则仓库。``{owner}`` / ``{repo}`` 由 URL 解析得出。
KAZUMI_RULES_REPO = "https://github.com/Predidit/KazumiRules"

#: 拉 GitHub 时的镜像模板，``{url}`` 是原始地址。第一个是直连。
#: 只在设置里开了自动镜像时才依次尝试。
DEFAULT_MIRRORS: tuple[str, ...] = (
    "{url}",
    "https://gh-proxy.com/{url}",
    "https://ghproxy.net/{url}",
    "https://ghfast.top/{url}",
)

#: 索引文件的名字。仓库的 CI 会自动生成它，里面是每条规则的名称和版本。
INDEX_NAME = "index.json"

#: 索引多久算过期。规则更新不频繁，一小时够了。
INDEX_TTL_SECONDS = 3600

#: XPath 字段。少了 searchList 就没法搜，其余可以缺（缺了就是取不到）。
_XPATH_FIELDS = (
    "searchList",
    "searchName",
    "searchResult",
    "chapterRoads",
    "chapterResult",
)


class RuleError(RuntimeError):
    """规则本身不可用——不是说没搜到，是说这条规则用不了。"""


def normalise_repo_url(url: str) -> str:
    """把用户填的各种写法统一成 GitHub 仓库地址。

    接受 ``https://github.com/Owner/Repo``、带 ``.git`` 的、带 ``/tree/<branch>``
    的，以及直接给 ``index.json`` 的地址。认不出的原样返回，让拉取阶段去失败
    ——比在这里猜一个能说得清楚。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    match = re.match(
        r"^(?P<base>https?://github\.com/[^/]+/[^/]+?)(?:/tree/(?P<branch>[^/]+))?$",
        text,
    )
    if match:
        return match.group("base")
    return text


def github_owner_repo(url: str) -> tuple[str, str] | None:
    match = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)/?$", normalise_repo_url(url))
    if not match:
        return None
    return match.group(1), match.group(2)


def raw_url_for(url: str, path: str, branch: str = "main") -> str:
    """把一个仓库地址拼成 raw 文件地址。

    非 GitHub 的地址（自建服务器、gist 之类）就当作目录根，直接拼路径。
    """
    url = normalise_repo_url(url)
    owner_repo = github_owner_repo(url)
    if owner_repo:
        owner, repo = owner_repo
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    if url.endswith("/"):
        return url + path
    return f"{url}/{path}"


@dataclass
class Rule:
    """一条规则。字段名照抄 Kazumi，包括它那句 ``muliSources`` 的拼写。"""

    name: str
    base_url: str = ""
    search_url: str = ""
    search_list: str = ""
    search_name: str = ""
    search_result: str = ""
    chapter_roads: str = ""
    chapter_result: str = ""
    api: str = ""
    version: str = ""
    type: str = "anime"
    deprecated: bool = False
    use_post: bool = False
    use_legacy_parser: bool = False
    use_native_player: bool = True
    use_webview: bool = True
    user_agent: str = ""
    referer: str = ""
    multi_sources: bool = True
    author: str = ""
    last_update: int = 0
    anti_crawler: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    #: 校验没过的字段，形如 ``{"searchList": "Invalid expression"}``。
    problems: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ 读取

    @property
    def usable(self) -> bool:
        """能用来搜就行。章节字段缺了不算废，只是取不到集数。"""
        return bool(self.search_url and self.search_list and not self.problems.get("searchList"))

    @property
    def label(self) -> str:
        suffix = "（已废弃）" if self.deprecated else ""
        return f"{self.name}{suffix}"

    def search_link(self, keyword: str) -> str:
        """把 ``@keyword`` 换成关键词。

        规则里也有写成 ``@keyword`` 但站点要求 URL 编码的情况；两者都试一遍，
        先给原样的（有些站点自己的 JS 会再编码一次，提前编码反而坏）。
        """
        if not self.search_url:
            raise RuleError(f"规则 {self.name} 没有 searchURL")
        return self.search_url.replace("@keyword", str(keyword))

    def absolute(self, href: str) -> str:
        """把详情页/章节里的相对链接补成绝对地址。"""
        href = str(href or "").strip()
        if not href:
            return ""
        if href.startswith(("http://", "https://")):
            return href
        base = self.base_url or ""
        if not base:
            return href
        if href.startswith("//"):
            return ("https:" if base.startswith("https") else "http:") + href
        if href.startswith("/"):
            return base.rstrip("/") + href
        return base.rstrip("/") + "/" + href

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        """从一条规则的 JSON 建出来。缺字段不报错，认不出的键留在 raw 里。"""
        if not isinstance(data, dict):
            raise RuleError("规则必须是一个 JSON 对象")

        def text(key: str, default: str = "") -> str:
            value = data.get(key, default)
            return default if value is None else str(value)

        def flag(key: str, default: bool) -> bool:
            value = data.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        name = text("name").strip()
        if not name:
            # 名字是给人认的，没有就退到 baseURL 的主机名，再没有就用 unknown
            name = text("baseURL").replace("https://", "").replace("http://", "").split("/")[0]
        if not name:
            raise RuleError("规则没有 name，也没有 baseURL 可以顶替")

        anti = data.get("antiCrawlerConfig")
        anti_enabled = flag("antiCrawlerEnabled", False)
        if isinstance(anti, dict) and anti.get("enabled"):
            anti_enabled = True

        rule = cls(
            name=name,
            base_url=text("baseURL"),
            search_url=text("searchURL"),
            search_list=text("searchList"),
            search_name=text("searchName"),
            search_result=text("searchResult"),
            chapter_roads=text("chapterRoads"),
            chapter_result=text("chapterResult"),
            api=text("api"),
            version=text("version"),
            type=text("type", "anime") or "anime",
            deprecated=flag("deprecated", False),
            use_post=flag("usePost", False),
            use_legacy_parser=flag("useLegacyParser", False),
            use_native_player=flag("useNativePlayer", True),
            use_webview=flag("useWebview", True),
            user_agent=text("userAgent"),
            referer=text("referer"),
            # 注意拼写：Kazumi 的字段就叫 muliSources（少个 t），照抄。
            multi_sources=flag("muliSources", True),
            author=text("author"),
            last_update=int(data.get("lastUpdate") or 0),
            anti_crawler=anti_enabled,
            raw=dict(data),
        )
        rule.problems = rule._check_xpath()
        return rule

    def _check_xpath(self) -> dict[str, str]:
        """把 XPath 都编译一遍，编译不过的记下来。

        不抛异常：仓库里确实有写错的规则（把 CSS 的 ``[@class*='x']`` 当
        XPath 用、只剩一个 ``//``），一条坏了不该让整个仓库不可用。但也不能
        静默——坏在哪必须能查。
        """
        problems: dict[str, str] = {}
        try:
            from lxml import etree
        except ImportError:  # pragma: no cover - 装依赖时不会有这条路
            return problems

        for name in _XPATH_FIELDS:
            expr = getattr(self, _snake(name), "")
            if not expr:
                continue
            try:
                etree.XPath(expr)
            except Exception as exc:  # noqa: BLE001 - 写错的规则就是会抛各种错
                problems[name] = str(exc)
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "baseURL": self.base_url,
            "searchURL": self.search_url,
            "searchList": self.search_list,
            "searchName": self.search_name,
            "searchResult": self.search_result,
            "chapterRoads": self.chapter_roads,
            "chapterResult": self.chapter_result,
            "api": self.api,
            "version": self.version,
            "type": self.type,
            "deprecated": self.deprecated,
            "usePost": self.use_post,
            "useLegacyParser": self.use_legacy_parser,
            "useNativePlayer": self.use_native_player,
            "useWebview": self.use_webview,
            "userAgent": self.user_agent,
            "referer": self.referer,
            "muliSources": self.multi_sources,
            "author": self.author,
            "lastUpdate": self.last_update,
            "antiCrawlerEnabled": self.anti_crawler,
        }


def _snake(xpath_field: str) -> str:
    """``searchList`` -> ``search_list``。"""
    out = []
    for index, char in enumerate(xpath_field):
        if char.isupper():
            if index:
                out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)


def parse_rules(payload: Any) -> list[Rule]:
    """把一份规则数据变成规则列表。

    三种形状都要认：

    * 单个对象 —— 一个文件一条规则；
    * 数组 —— 一个文件里打包多条（自定义仓库常见）；
    * ``{"rules": [...]}`` —— 有些仓库会套一层。
    """
    if isinstance(payload, dict) and isinstance(payload.get("rules"), list):
        payload = payload["rules"]

    items = payload if isinstance(payload, list) else [payload]
    rules: list[Rule] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # 索引条目只有 name/version，没有 XPath，跳过它们而不是当成坏规则
        if not any(item.get(key) for key in _XPATH_FIELDS) and not item.get("searchURL"):
            continue
        try:
            rules.append(Rule.from_dict(item))
        except RuleError as exc:
            log.debug("rule skipped: %s", exc)
    return rules


def parse_index(payload: Any) -> list[dict[str, Any]]:
    """解析仓库的 ``index.json``。

    拿来给界面列名字用。认不出就返回空——真正的规则列表可以退回扫描目录。
    """
    if isinstance(payload, dict):
        payload = payload.get("rules") or payload.get("data") or []
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "version": str(item.get("version") or ""),
            "author": str(item.get("author") or ""),
            "last_update": int(item.get("lastUpdate") or 0),
            "deprecated": bool(item.get("deprecated") or False),
        })
    return out


class RuleCache:
    """规则落盘缓存。

    文件按仓库分开存：``<index 的摘要>.json``。带 TTL，过期后仍然可用
    （网络不通时拿旧的顶上，总比空手强），只是会标记 ``stale``。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, repo: str, kind: str) -> Path:
        owner_repo = github_owner_repo(repo)
        stem = "_".join(owner_repo) if owner_repo else re.sub(r"[^A-Za-z0-9]+", "_", repo)[:60]
        return self.root / f"{stem}__{kind}.json"

    def read(self, repo: str, kind: str, ttl: float = INDEX_TTL_SECONDS) -> tuple[Any, bool]:
        """返回 (数据, 是否过期)。没有缓存时返回 (None, True)。"""
        path = self._path(repo, kind)
        try:
            stat = path.stat()
        except OSError:
            return None, True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, True
        age = time.time() - stat.st_mtime
        return data, age > ttl

    def write(self, repo: str, kind: str, data: Any) -> None:
        path = self._path(repo, kind)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.debug("rule cache not written: %s", exc)


def mirrored_urls(url: str, mirrors: tuple[str, ...] | list[str] | None = None,
                  *, auto_mirror: bool = True) -> list[str]:
    """按顺序给出要尝试的地址。

    没开自动镜像就只给原地址——用户关掉它通常是因为自己在走代理，多试几个
    反而拖慢。
    """
    templates = list(mirrors) if mirrors else list(DEFAULT_MIRRORS)
    if not auto_mirror:
        return [url]
    out: list[str] = []
    for template in templates:
        candidate = str(template).replace("{url}", url)
        # 相对模板（没有 http 前缀又没占位符）视为无效，跳过
        if "http" not in candidate:
            continue
        if candidate not in out:
            out.append(candidate)
    return out or [url]


class RuleLibrary:
    """把仓库、缓存、拉取捏在一起。

    一个仓库的规则可能上百条，绝不能在列表时把每条都拉一遍——列表只读
    ``index.json``，点开哪条规则才拉哪条。索引和规则文件都落盘，带 TTL。
    """

    #: 规则文件本身过期很慢：规则更新不频繁，换版本时 index 的 version
    #: 会变，那时才需要重拉。
    RULE_TTL_SECONDS = 7 * 24 * 3600

    def __init__(self, cache: RuleCache, fetcher: Any, repos: list[str] | None = None) -> None:
        self.cache = cache
        self.fetcher = fetcher
        self.repos = [r for r in (repos or [KAZUMI_RULES_REPO]) if r]

    # ------------------------------------------------------------------ 列表

    def list_rules(self, repo: str) -> tuple[list[dict[str, Any]], str]:
        """返回 (规则索引, 来源)。来源是 ``cache`` / ``network`` / ``cache(stale)``。"""
        repo = normalise_repo_url(repo)
        if not repo:
            raise RuleError("没有规则仓库")

        cached, stale = self.cache.read(repo, "index")
        if cached is not None and not stale:
            return parse_index(cached), "cache"

        try:
            payload = self.fetcher.fetch_rules(raw_url_for(repo, INDEX_NAME))
        except Exception as exc:  # noqa: BLE001 - 拉不到就用旧的，比空手强
            if cached is not None:
                log.info("rule index for %s is stale, using the copy on disk: %s", repo, exc)
                return parse_index(cached), f"cache(stale): {exc}"
            raise RuleError(f"拉不到规则索引：{exc}") from exc

        self.cache.write(repo, "index", payload)
        return parse_index(payload), "network"

    # ------------------------------------------------------------------ 单条

    def load_rule(self, repo: str, name: str) -> Rule:
        """取一条规则。

        三层缓存，按代价从低到高：单条规则的缓存 → 整个仓库的打包缓存 →
        网络。打包缓存这一层是必要的：自定义仓库把上百条规则塞进一个文件时，
        没有它就会为每条规则重拉同一个文件。
        """
        repo = normalise_repo_url(repo)
        name = str(name or "").strip()
        if not name:
            raise RuleError("没有规则名")

        def pick_from(payload: Any) -> Rule | None:
            rules = parse_rules(payload)
            match = _pick(rules, name)
            if match is not None:
                return match
            # 单条文件的文件名和规则名对不上也算数
            return rules[0] if len(rules) == 1 else None

        # 1) 单条缓存
        kind = f"rule_{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}"
        cached, stale = self.cache.read(repo, kind, ttl=self.RULE_TTL_SECONDS)
        if cached is not None and not stale:
            match = pick_from(cached)
            if match is not None:
                return match

        # 2) 打包缓存
        bundle, bundle_stale = self.cache.read(repo, "_bundle", ttl=self.RULE_TTL_SECONDS)
        if bundle is not None and not bundle_stale:
            match = pick_from(bundle)
            if match is not None:
                return match

        # 3) 网络：先按惯例试 ``<名字>.json``，再试打包式仓库
        candidates: list[str] = []
        if repo.endswith(".json"):
            candidates.append(repo)
        else:
            candidates.append(raw_url_for(repo, f"{name}.json"))
            for bundle_name in ("rules.json", "all.json", "rulesets.json"):
                candidates.append(raw_url_for(repo, bundle_name))

        problems: list[str] = []
        for index, candidate in enumerate(candidates):
            short = candidate.rsplit("/", 1)[-1]
            try:
                payload = self.fetcher.fetch_rules(candidate)
            except Exception as exc:  # noqa: BLE001 - 换下一个候选
                problems.append(f"{short}: {exc}")
                continue

            rules = parse_rules(payload)
            match = _pick(rules, name)
            if match is not None:
                # 单条文件缓存它自己；打包文件缓存整个包，下次换规则不再重拉
                self.cache.write(repo, kind, payload)
                if index or len(rules) > 1:
                    self.cache.write(repo, "_bundle", payload)
                return match
            if len(rules) == 1 and index == 0:
                self.cache.write(repo, kind, payload)
                return rules[0]
            problems.append(f"{short}: 里面没有叫 {name} 的规则")

        raise RuleError(f"取不到规则 {name}（{'；'.join(problems)}）")

    def all_rules(self, repo: str, *, limit: int = 0) -> list[Rule]:
        """把整个仓库的规则都拉下来。只在需要全量校验时用，很慢。"""
        index, _ = self.list_rules(repo)
        out: list[Rule] = []
        for item in index:
            if limit and len(out) >= limit:
                break
            try:
                out.append(self.load_rule(repo, item["name"]))
            except (RuleError, Exception) as exc:  # noqa: BLE001
                log.debug("rule %s skipped: %s", item.get("name"), exc)
        return out


def _pick(rules: list[Rule], name: str) -> Rule | None:
    lowered = name.strip().lower()
    for rule in rules:
        if rule.name.strip().lower() == lowered:
            return rule
    return None
