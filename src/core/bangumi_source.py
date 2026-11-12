"""按 Kazumi 规则搜番、取章节、找播放地址。

三件事分开看：

1. **搜索** —— 规则给 ``searchURL``（``@keyword`` 占位）和几个 XPath，
   取出「标题 + 详情页链接」。这部分纯 XPath，可靠。
2. **章节** —— 拉详情页，用 ``chapterRoads`` / ``chapterResult`` 取出
   线路分组和每集链接。这部分也可靠。
3. **播放地址** —— **规则里没有这个**。Kazumi 是把页面塞进 webview 然后
   hook JS / 拦请求拿到的（仓库 README 写的 "JavaScript hook-based
   parser"）。这里做不到那件事，退一步做两件事：

   * 在剧集页 HTML 里按正则找 m3u8 / mp4（能覆盖把地址写在页面源码里的站点，
     也就是 Kazumi 说的 "legacy parser" 那类）；
   * 找 ``<video src>`` / ``<source src>`` / ``data-src`` 这类属性。

   找得到就能播，找不到就如实说找不到——不编一个地址，也不假装支持。
   JS 里拼出来的 blob/加密流拿不到，这是本模块已知的边界。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

from .bangumi_rules import Rule, RuleError, mirrored_urls

log = logging.getLogger(__name__)

#: 页面上找播放地址用的正则。``(\?|$|["'\s])`` 收尾是为了不把 JSON 里的
#: 转义引号吞进去。用户可以在设置里覆盖它。
#: 地址常写在 JSON 里，斜杠是转义的（``https:\/\/...``）。正则必须允许
#: 反斜杠，否则根本匹配不到——解转义发生在匹配之后，那时已经太晚了。
DEFAULT_STREAM_PATTERNS: tuple[str, ...] = (
    r"https?:\\?/\\?/[^\s\"'<>]+?\.m3u8[^\s\"'<>]*",
    r"https?:\\?/\\?/[^\s\"'<>]+?\.mp4[^\s\"'<>]*",
    r"https?:\\?/\\?/[^\s\"'<>]+?\.flv[^\s\"'<>]*",
)

#: 可能藏着播放地址的属性。
_MEDIA_ATTRS = ("src", "data-src", "data-url", "data-video", "data-player", "data-original")

#: 默认 UA。空字符串表示用 requests 自己的。
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """页面拿不到。和"拿到了但没找到东西"是两回事，必须能分开。"""


@dataclass
class SearchHit:
    name: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "url": self.url}


@dataclass
class Chapter:
    group: str
    title: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"group": self.group, "title": self.title, "url": self.url}


@dataclass
class Stream:
    url: str
    kind: str          # "hls" | "file"
    source: str        # 是从哪儿找到的，排查时要看

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "kind": self.kind, "source": self.source}


class Fetcher:
    """取页面。

    ``skip_ssl_verify`` 和 ``auto_mirror`` 都来自设置，默认都不开：跳过证书
    校验是降低安全性的事，自动镜像会多发好几个请求，两个都该由用户自己决定。
    """

    def __init__(
        self,
        *,
        skip_ssl_verify: bool = False,
        auto_mirror: bool = False,
        mirrors: list[str] | None = None,
        interval: float = 0.0,
        timeout: float = 20.0,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = "",
    ) -> None:
        self.skip_ssl_verify = bool(skip_ssl_verify)
        self.auto_mirror = bool(auto_mirror)
        self.mirrors = list(mirrors or [])
        self.interval = max(0.0, float(interval or 0.0))
        self.timeout = max(1.0, float(timeout or 20.0))
        self.user_agent = user_agent or DEFAULT_UA
        self._sleep = sleep
        self._last = 0.0
        self._session = session

    # -------------------------------------------------------------- session

    @property
    def session(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _pace(self) -> None:
        if self.interval <= 0:
            return
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    # -------------------------------------------------------------- 取

    def get(self, url: str, *, referer: str = "", use_mirror: bool = False) -> str:
        """拿一页 HTML。失败抛 FetchError，里面带状态码或异常。"""
        if not url:
            raise FetchError("空的地址")

        headers = {"User-Agent": self.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"}
        if referer:
            headers["Referer"] = referer

        candidates = mirrored_urls(url, self.mirrors, auto_mirror=self.auto_mirror) \
            if use_mirror else [url]

        problems: list[str] = []
        for candidate in candidates:
            self._pace()
            try:
                response = self.session.get(
                    candidate,
                    headers=headers,
                    timeout=self.timeout,
                    verify=not self.skip_ssl_verify,
                )
            except Exception as exc:  # noqa: BLE001 - requests 的异常层数很多
                problems.append(f"{_host(candidate)}: {exc}")
                continue

            if response.status_code >= 400:
                problems.append(f"{_host(candidate)}: HTTP {response.status_code}")
                continue

            # 页面编码不一定是 utf-8；requests 猜错时中文会成乱码，而乱码的 XPath
            # 照样能跑，只是文本全错。用 apparent_encoding 纠一次。
            if not response.encoding or response.encoding.lower() in ("iso-8859-1", "ascii"):
                response.encoding = response.apparent_encoding or "utf-8"
            return response.text

        raise FetchError("；".join(problems) or "取不到页面")

    def fetch_rules(self, url: str) -> Any:
        """取一份规则 JSON。规则仓库走镜像候选。"""
        text = self.get(url, use_mirror=True)
        import json

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchError(f"不是合法 JSON：{exc}") from exc


def _host(url: str) -> str:
    match = re.match(r"^https?://([^/]+)", str(url))
    return match.group(1) if match else str(url)[:40]


# --------------------------------------------------------------------- XPath


def _compile(expr: str, ctx: Any = None) -> Any:
    from lxml import etree

    try:
        return etree.XPath(expr)
    except Exception as exc:  # noqa: BLE001 - 规则写错就是会抛
        raise RuleError(f"XPath 不合法：{expr!r}（{exc}）") from exc


def relative(expr: str) -> str:
    """把一条"看起来是绝对路径"的表达式改成相对当前节点的。

    规则里的 ``searchName`` / ``searchResult`` / ``chapterResult`` 大量写成
    ``//div/...``。XPath 里 ``//x`` 在某个节点上求值仍然是**全文档**搜索，
    不是"这个节点里面"——照原样用，每一行搜索结果都会取到页面上的第一个
    条目、每一条线路都会返回全站的剧集，而且不报错，界面看起来还挺正常。

    所以子表达式要改成相对的：``//a`` -> ``.//a``，``/self::x`` -> ``self::x``。
    带 ``|`` 的并集逐段处理。已经相对（``.``、``@``、``text()``、无斜杠开头）
    的原样返回。
    """
    text = str(expr or "").strip()
    if not text:
        return ""

    def one(part: str) -> str:
        part = part.strip()
        if part.startswith("//"):
            return "." + part
        if part.startswith("/"):
            return part[1:]
        return part

    if "|" in text:
        return " | ".join(one(part) for part in text.split("|"))
    return one(text)


def select_text(ctx: Any, expr: str) -> str:
    """取第一条结果当文本。取不到返回空串，不抛。"""
    if not expr:
        return ""
    result = _compile(expr)(ctx)
    if isinstance(result, list):
        if not result:
            return ""
        first = result[0]
        return str(first).strip() if isinstance(first, str) else \
            (first.text_content() if hasattr(first, "text_content") else str(first)).strip()
    return str(result).strip()


def select_nodes(ctx: Any, expr: str) -> list[Any]:
    if not expr:
        return []
    result = _compile(expr)(ctx)
    return result if isinstance(result, list) else []


def _href_of(node: Any, url_attr: str = "href") -> str:
    """从一个节点上取链接：元素看属性，字符串当链接本身。"""
    if isinstance(node, str):
        return node.strip()
    get = getattr(node, "get", None)
    if callable(get):
        for attr in (url_attr, "href", "data-href", "data-url"):
            value = get(attr)
            if value:
                return str(value).strip()
    return ""


# --------------------------------------------------------------------- 搜索


def search(rule: Rule, keyword: str, fetcher: Fetcher) -> list[SearchHit]:
    """按规则搜。搜不到返回空列表——那是结果，不是错误。"""
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    if not rule.usable:
        raise RuleError(f"规则 {rule.name} 不能用来搜索（缺少 searchURL 或 searchList）")

    url = rule.search_link(keyword)
    # 有些站点要求关键词已经编码过。原样不行就换编码后的再试一次。
    try:
        html = fetcher.get(url, referer=rule.referer or rule.base_url)
    except FetchError:
        encoded = rule.search_link(quote(keyword))
        if encoded == url:
            raise
        html = fetcher.get(encoded, referer=rule.referer or rule.base_url)

    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html)
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"页面解析失败：{exc}") from exc

    hits: list[SearchHit] = []
    name_expr = relative(rule.search_name)
    result_expr = relative(rule.search_result)
    for node in select_nodes(doc, rule.search_list):
        name = select_text(node, name_expr) or select_text(node, result_expr)
        href = _href_of(
            (select_nodes(node, result_expr) or [node])[0]
        )
        if not href:
            continue
        link = rule.absolute(href)
        if not link:
            continue
        hits.append(SearchHit(name=name or link, url=link))
        if len(hits) >= 60:
            break
    return hits


# --------------------------------------------------------------------- 章节


def chapters(rule: Rule, detail_url: str, fetcher: Fetcher) -> list[Chapter]:
    """拉详情页，取每条线路下的剧集。

    ``chapterRoads`` 是线路分组，``chapterResult`` 是每集。有的规则不给分组，
    那就整页当一组。
    """
    if not detail_url:
        return []
    html = fetcher.get(detail_url, referer=rule.referer or rule.base_url)

    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html)
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"页面解析失败：{exc}") from exc

    out: list[Chapter] = []
    seen: set[str] = set()

    def add(group: str, node: Any, index: int) -> None:
        href = _href_of(node)
        if not href:
            return
        link = rule.absolute(href)
        if not link or link in seen:
            return
        seen.add(link)
        title = ""
        get = getattr(node, "get", None)
        if callable(get):
            title = str(get("title") or "").strip()
        if not title:
            title = select_text(node, "text()") or getattr(node, "text_content", lambda: "")()
            title = str(title).strip()
        out.append(Chapter(group=group or "默认", title=title or f"第 {index} 集", url=link))

    roads = select_nodes(doc, rule.chapter_roads) if rule.chapter_roads else []
    if roads:
        for road_index, road in enumerate(roads, start=1):
            # 线路标题不能拿整块文字顶替：那通常是所有剧集名的拼接，
            # 又长又没意义。只有节点自己带标题属性时才用它。
            group = ""
            get = getattr(road, "get", None)
            if callable(get):
                group = str(get("title") or get("data-title") or "").strip()[:40]
            group = group or f"线路 {road_index}"
            inner = relative(rule.chapter_result) or ".//a"
            for index, node in enumerate(select_nodes(road, inner), start=1):
                add(group, node, index)
    else:
        for index, node in enumerate(select_nodes(doc, relative(rule.chapter_result) or ".//a"), start=1):
            add("", node, index)

    return out


# --------------------------------------------------------------------- 取流


def _absolute(candidate: str, page_url: str) -> str:
    candidate = candidate.replace("\\/", "/").replace("&amp;", "&").strip()
    if not candidate:
        return ""
    try:
        return urljoin(page_url, candidate)
    except ValueError:
        return candidate


def find_streams(
    html: str,
    page_url: str,
    *,
    patterns: list[str] | tuple[str, ...] | None = None,
) -> list[Stream]:
    """在页面源码里找播放地址。

    两路一起找：正则扫全文，加上 ``<video>`` / ``<source>`` 上的属性。结果去重、
    保持出现顺序——顺序往往就是画质从高到低。
    """
    if not html:
        return []

    found: list[Stream] = []
    seen: set[str] = set()

    def add(raw: str, source: str, *, trust: bool = False) -> None:
        url = _absolute(raw, page_url)
        if not url or url in seen:
            return
        lowered = url.lower()
        if lowered.startswith(("javascript:", "data:", "blob:", "about:")):
            return
        kind = "hls" if ".m3u8" in lowered else "file"
        # ``trust`` 表示这个地址是用户自己的正则找出来的：他既然写了那条
        # 规则，就说明他知道自己要什么，内置的扩展名白名单不该再否决它。
        if not trust and not any(
            ext in lowered for ext in (".m3u8", ".mp4", ".flv", ".mkv", ".ts")
        ):
            return
        seen.add(url)
        found.append(Stream(url=url, kind=kind, source=source))

    # 1) 正则扫全文
    custom = patterns is not None
    for pattern in (patterns or DEFAULT_STREAM_PATTERNS):
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            log.debug("bad stream pattern %r: %s", pattern, exc)
            continue
        for match in regex.finditer(html):
            add(match.group(0), "regex", trust=custom)

    # 2) 属性
    from lxml import html as lxml_html

    try:
        doc = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001 - 正则已经找过了，解析失败不算致命
        doc = None

    if doc is not None:
        # 按属性找，不按标签找。播放地址常挂在 <div data-src> 这种懒加载
        # 容器上，只扫 video/source 会漏掉一大半站点。
        wanted = " or ".join(f"@{attr}" for attr in _MEDIA_ATTRS)
        try:
            carriers = doc.xpath(f"//*[{wanted}]")
        except Exception:  # noqa: BLE001 - 解析出问题就只靠正则
            carriers = []
        for node in carriers:
            tag = getattr(node, "tag", "node")
            if not isinstance(tag, str):
                continue
            for attr in _MEDIA_ATTRS:
                value = node.get(attr)
                if value:
                    add(value, f"<{tag} {attr}>")

    return found


def resolve_streams(
    rule: Rule,
    episode_url: str,
    fetcher: Fetcher,
    *,
    patterns: list[str] | tuple[str, ...] | None = None,
) -> list[Stream]:
    """从剧集页取出播放地址。取不到就返回空——这是事实，不是错误。"""
    if not episode_url:
        return []
    html = fetcher.get(episode_url, referer=rule.referer or rule.base_url)
    return find_streams(html, episode_url, patterns=patterns)
