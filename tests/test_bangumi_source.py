"""取流层：XPath 取值、搜索、章节、找播放地址。

重点测「找不到」的路径——这套东西最坏的失败方式不是报错，是静默返回空列表
然后界面显示"没有可播放的地址"，让人以为是站点的问题。
"""

from __future__ import annotations

import pytest

from src.core.bangumi_rules import Rule, RuleError
from src.core.bangumi_source import (
    DEFAULT_STREAM_PATTERNS,
    Fetcher,
    FetchError,
    chapters,
    find_streams,
    resolve_streams,
    search,
    select_nodes,
    select_text,
)

# ------------------------------------------------------------------ 假 session


class FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


class FakeSession:
    """按 URL 片段给页面。记下每次请求的参数，用来断言 verify/headers。"""

    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):  # noqa: ANN201
        self.calls.append({"url": url, **kwargs})
        for key, value in self.pages.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, FakeResponse):
                    return value
                return FakeResponse(str(value))
        raise OSError(f"no page for {url}")


SEARCH_HTML = """<html><body>
<div class="module-items module-card-items">
  <div class="module-card-item">
    <div class="module-card-item-title"><a href="/detail/1">孤独摇滚</a></div>
  </div>
  <div class="module-card-item">
    <div class="module-card-item-title"><a href="https://other.com/d/2">轻音少女</a></div>
  </div>
</div>
</body></html>"""

DETAIL_HTML = """<html><body>
<div class="module-play-list-content">
  <a class="module-play-list-link" href="/play/1-1"><span>第1话</span></a>
  <a class="module-play-list-link" href="/play/1-2"><span>第2话</span></a>
</div>
<div class="module-play-list-content">
  <a class="module-play-list-link" href="/play/2-1">第1话</a>
</div>
</body></html>"""


def make_rule(**overrides) -> Rule:
    base = {
        "name": "T",
        "baseURL": "https://www.fqdm.cc/",
        "searchURL": "https://www.fqdm.cc/search/wd/@keyword.html",
        "searchList": "//div[@class='module-items module-card-items']/div[contains(@class, 'module-card-item')]",
        "searchName": ".//div[@class='module-card-item-title']/a",
        "searchResult": ".//div[@class='module-card-item-title']/a",
        "chapterRoads": "//div[contains(@class,'module-play-list-content')]",
        "chapterResult": "//a[contains(@class,'module-play-list-link')]",
    }
    base.update(overrides)
    return Rule.from_dict(base)


def make_fetcher(pages: dict[str, object], **kwargs) -> tuple[Fetcher, FakeSession]:
    session = FakeSession(pages)
    return Fetcher(session=session, interval=0.0, **kwargs), session


# ------------------------------------------------------------------ 作用域


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("//a", ".//a"),
        ("//div[@class='x']/a", ".//div[@class='x']/a"),
        ("/self::*[@class='x']", "self::*[@class='x']"),
        (".//a", ".//a"),
        ("@href", "@href"),
        ("text()", "text()"),
        ("", ""),
    ],
)
def test_relative(raw: str, expected: str) -> None:
    from src.core.bangumi_source import relative

    assert relative(raw) == expected


def test_relative_handles_a_union() -> None:
    from src.core.bangumi_source import relative

    got = relative("//a[@class='x'] | /self::*[@class='y']")
    assert got == ".//a[@class='x'] | self::*[@class='y']"


def test_absolute_chapter_result_is_scoped_to_its_road() -> None:
    """规则里 chapterResult 常写成 ``//a``。

    XPath 里 ``//a`` 在某个节点上求值仍是全文档搜索，所以照原样用的话
    第二条线路会返回第一条线路的剧集——而且是静默的。这里钉住它被改成
    ``.//a`` 之后每条线路只拿到自己的。
    """
    rule = make_rule(chapterResult="//a[contains(@class,'module-play-list-link')]")
    fetcher, _ = make_fetcher({"detail": DETAIL_HTML})
    items = chapters(rule, "https://www.fqdm.cc/detail/1", fetcher)

    assert [c.group for c in items] == ["线路 1", "线路 1", "线路 2"]
    assert items[2].url == "https://www.fqdm.cc/play/2-1"


def test_absolute_search_name_is_scoped_to_its_row() -> None:
    """AGE 那类规则的 searchName 也写成 ``//...``。

    不改成相对的话，每一行都会取到页面上第一个标题。
    """
    rule = make_rule(
        searchName="//div[@class='module-card-item-title']/a",
        searchResult="//div[@class='module-card-item-title']/a",
    )
    fetcher, _ = make_fetcher({"search": SEARCH_HTML})
    hits = search(rule, "x", fetcher)
    assert [h.name for h in hits] == ["孤独摇滚", "轻音少女"]


# ------------------------------------------------------------------ 取值


def test_select_text_finds_the_title() -> None:
    from lxml import html

    doc = html.fromstring(SEARCH_HTML)
    node = select_nodes(doc, "//div[@class='module-card-item']")[0]
    assert select_text(node, ".//a") == "孤独摇滚"


def test_select_text_returns_empty_for_no_match() -> None:
    from lxml import html

    doc = html.fromstring(SEARCH_HTML)
    assert select_text(doc, "//nope") == ""


def test_select_nodes_returns_a_list() -> None:
    from lxml import html

    doc = html.fromstring(SEARCH_HTML)
    assert len(select_nodes(doc, "//div[@class='module-card-item']")) == 2


def test_bad_xpath_raises_rule_error() -> None:
    from lxml import html

    doc = html.fromstring(SEARCH_HTML)
    with pytest.raises(RuleError):
        select_nodes(doc, "//[")


# ------------------------------------------------------------------ 搜索


def test_search_returns_hits() -> None:
    rule = make_rule()
    fetcher, session = make_fetcher({"search": SEARCH_HTML})
    hits = search(rule, "孤独摇滚", fetcher)

    assert [h.name for h in hits] == ["孤独摇滚", "轻音少女"]
    assert hits[0].url == "https://www.fqdm.cc/detail/1"
    assert hits[1].url == "https://other.com/d/2", "已经是绝对地址的不该被改写"
    assert "search/wd/" in session.calls[0]["url"]


def test_search_sends_a_referer() -> None:
    rule = make_rule()
    fetcher, session = make_fetcher({"search": SEARCH_HTML})
    search(rule, "x", fetcher)
    assert session.calls[0]["headers"].get("Referer")


def test_search_with_an_empty_keyword_does_not_fetch() -> None:
    rule = make_rule()
    fetcher, session = make_fetcher({"search": SEARCH_HTML})
    assert search(rule, "   ", fetcher) == []
    assert session.calls == []


def test_search_with_an_unusable_rule_raises() -> None:
    rule = Rule(name="broken", search_url="", search_list="")
    fetcher, _ = make_fetcher({})
    with pytest.raises(RuleError):
        search(rule, "x", fetcher)


def test_search_returns_empty_when_nothing_matches() -> None:
    """搜不到是结果，不是错误。"""
    rule = make_rule()
    fetcher, _ = make_fetcher({"search": "<html><body>nothing here</body></html>"})
    assert search(rule, "x", fetcher) == []


def test_search_retries_with_an_encoded_keyword() -> None:
    """有的站点要求关键词已经 URL 编码。原样不行时要换编码后的再试一次。"""
    rule = make_rule()

    class EncodingSession(FakeSession):
        def get(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            if "%" in url:
                return FakeResponse(SEARCH_HTML)
            raise OSError("try the encoded one")

    fetcher = Fetcher(session=EncodingSession({}), interval=0.0)
    hits = search(rule, "孤独摇滚", fetcher)
    assert len(hits) == 2


# ------------------------------------------------------------------ 章节


def test_chapters_group_by_road() -> None:
    rule = make_rule()
    fetcher, _ = make_fetcher({"detail": DETAIL_HTML})
    items = chapters(rule, "https://www.fqdm.cc/detail/1", fetcher)

    assert len(items) == 3
    assert items[0].url == "https://www.fqdm.cc/play/1-1"
    # 两条线路要分开；名字按序号给，不拿整块剧集名拼接当标题
    assert items[0].group == "线路 1"
    assert items[2].group == "线路 2"
    assert items[0].group != items[2].group


def test_chapters_without_a_detail_url_does_not_fetch() -> None:
    rule = make_rule()
    fetcher, session = make_fetcher({})
    assert chapters(rule, "", fetcher) == []
    assert session.calls == []


def test_chapters_returns_empty_when_nothing_matches() -> None:
    rule = make_rule()
    fetcher, _ = make_fetcher({"detail": "<html><body>空</body></html>"})
    assert chapters(rule, "https://www.fqdm.cc/detail/1", fetcher) == []


def test_chapters_dedupes_the_same_url() -> None:
    rule = make_rule()
    html = """<html><body><div class="module-play-list-content">
      <a class="module-play-list-link" href="/p/1">A</a>
      <a class="module-play-list-link" href="/p/1">A 重复</a>
    </div></body></html>"""
    fetcher, _ = make_fetcher({"detail": html})
    items = chapters(rule, "https://www.fqdm.cc/detail/1", fetcher)
    assert len(items) == 1


# ------------------------------------------------------------------ 取流


def test_find_streams_picks_up_an_m3u8() -> None:
    html = '<script>var u = "https://cdn.example.com/a/index.m3u8?sign=1";</script>'
    streams = find_streams(html, "https://www.fqdm.cc/play/1")
    assert len(streams) == 1
    assert streams[0].kind == "hls"
    assert streams[0].url == "https://cdn.example.com/a/index.m3u8?sign=1"


def test_find_streams_picks_up_an_mp4() -> None:
    html = "see https://cdn.example.com/v/a.mp4 for the file"
    streams = find_streams(html, "https://www.fqdm.cc/play/1")
    assert streams[0].kind == "file"


def test_find_streams_reads_video_and_source_tags() -> None:
    html = ('<video src="https://c.com/a.mp4"><source src="https://c.com/b.m3u8"></video>')
    streams = find_streams(html, "https://www.fqdm.cc/play/1")
    urls = [s.url for s in streams]
    assert "https://c.com/a.mp4" in urls
    assert "https://c.com/b.m3u8" in urls


def test_find_streams_reads_data_attributes() -> None:
    """属性这条路要单独钉住，所以地址写成相对的。

    写完整 URL 的话内置正则也扫得到，"属性扫描"这一支实际上从没被执行过。
    相对地址只有属性扫描能认出来。
    """
    html = '<div data-src="/media/x.m3u8"></div>'
    streams = find_streams(html, "https://www.fqdm.cc/play/1")
    assert [s.url for s in streams] == ["https://www.fqdm.cc/media/x.m3u8"]
    assert streams[0].source.startswith("<div")


def test_find_streams_unescapes_json_slashes() -> None:
    """地址常写在 JSON 里，斜杠是转义的。"""
    html = '{"url":"https:\\/\\/cdn.example.com\\/a\\/i.m3u8"}'
    streams = find_streams(html, "https://www.fqdm.cc/play/1")
    assert streams[0].url == "https://cdn.example.com/a/i.m3u8"


def test_find_streams_makes_a_relative_url_absolute() -> None:
    html = '<video src="/media/a.mp4"></video>'
    streams = find_streams(html, "https://www.fqdm.cc/play/1")
    assert streams[0].url == "https://www.fqdm.cc/media/a.mp4"


def test_find_streams_dedupes() -> None:
    html = '<video src="https://c.com/a.mp4"></video><a href="https://c.com/a.mp4">x</a>'
    streams = find_streams(html, "https://www.fqdm.cc/play/1")
    assert len(streams) == 1


def test_find_streams_ignores_blob_and_javascript() -> None:
    """blob: 和 javascript: 是播放器自己造出来的，拿出去播不了。"""
    html = '<video src="blob:https://x/1"></video><a href="javascript:play()">x</a>'
    assert find_streams(html, "https://www.fqdm.cc/play/1") == []


def test_find_streams_returns_empty_for_a_js_only_page() -> None:
    """页面里只有脚本、地址在运行时才拼出来 —— 这是已知的边界，要老实返回空。"""
    html = "<script>player.src = buildUrl('/api/get', id);</script>"
    assert find_streams(html, "https://www.fqdm.cc/play/1") == []


def test_find_streams_honours_custom_patterns() -> None:
    html = 'src="https://c.com/live/abc.mpd"'
    default = find_streams(html, "https://e.com/")
    assert default == []
    custom = find_streams(html, "https://e.com/", patterns=[r"https?://[^\s\"'<>]+\.mpd"])
    assert custom[0].url == "https://c.com/live/abc.mpd"


def test_find_streams_survives_a_bad_pattern() -> None:
    streams = find_streams(
        'https://c.com/a.m3u8', "https://e.com/",
        patterns=[r"([unclosed", DEFAULT_STREAM_PATTERNS[0]],
    )
    assert len(streams) == 1


def test_resolve_streams_reads_the_episode_page() -> None:
    rule = make_rule()
    fetcher, session = make_fetcher({"play": '<video src="https://c.com/e.mp4"></video>'})
    streams = resolve_streams(rule, "https://www.fqdm.cc/play/1-1", fetcher)
    assert [s.url for s in streams] == ["https://c.com/e.mp4"]
    assert session.calls[0]["headers"].get("Referer")


def test_resolve_streams_without_a_url_does_not_fetch() -> None:
    rule = make_rule()
    fetcher, session = make_fetcher({})
    assert resolve_streams(rule, "", fetcher) == []
    assert session.calls == []


# ------------------------------------------------------------------ 取页器


def test_ssl_verification_is_on_by_default() -> None:
    fetcher, session = make_fetcher({"x": "<html></html>"})
    fetcher.get("https://e.com/x")
    assert session.calls[0]["verify"] is True


def test_ssl_verification_can_be_turned_off() -> None:
    fetcher, session = make_fetcher({"x": "<html></html>"}, skip_ssl_verify=True)
    fetcher.get("https://e.com/x")
    assert session.calls[0]["verify"] is False


def test_http_errors_become_fetch_errors() -> None:
    session = FakeSession({"x": FakeResponse("nope", status=503)})
    fetcher = Fetcher(session=session, interval=0.0)
    with pytest.raises(FetchError) as info:
        fetcher.get("https://e.com/x")
    assert "503" in str(info.value)


def test_transport_errors_become_fetch_errors() -> None:
    session = FakeSession({"x": OSError("connection reset")})
    fetcher = Fetcher(session=session, interval=0.0)
    with pytest.raises(FetchError):
        fetcher.get("https://e.com/x")


def test_an_empty_url_is_an_error() -> None:
    fetcher = Fetcher(session=FakeSession({}), interval=0.0)
    with pytest.raises(FetchError):
        fetcher.get("")


def test_mirror_fallback_tries_the_next_host() -> None:
    session = FakeSession({"gh-proxy": "<html>ok</html>"})
    fetcher = Fetcher(
        session=session,
        interval=0.0,
        auto_mirror=True,
        mirrors=["{url}", "https://gh-proxy.com/{url}"],
    )
    body = fetcher.get("https://raw.githubusercontent.com/a/b/main/x.json", use_mirror=True)
    assert body == "<html>ok</html>"
    assert len(session.calls) == 2


def test_mirrors_are_not_used_for_ordinary_pages() -> None:
    session = FakeSession({"e.com": "<html>ok</html>"})
    fetcher = Fetcher(session=session, interval=0.0, auto_mirror=True)
    fetcher.get("https://e.com/page")
    assert len(session.calls) == 1


def test_fetch_rules_parses_json() -> None:
    session = FakeSession({"x.json": '{"name": "A"}'})
    fetcher = Fetcher(session=session, interval=0.0)
    assert fetcher.fetch_rules("https://e.com/x.json") == {"name": "A"}


def test_fetch_rules_rejects_broken_json() -> None:
    session = FakeSession({"x.json": "{not json"})
    fetcher = Fetcher(session=session, interval=0.0)
    with pytest.raises(FetchError):
        fetcher.fetch_rules("https://e.com/x.json")


def test_pacing_waits_between_requests() -> None:
    slept: list[float] = []
    session = FakeSession({"e.com": "<html></html>"})
    fetcher = Fetcher(session=session, interval=0.5, sleep=slept.append)
    fetcher.get("https://e.com/a")
    fetcher.get("https://e.com/b")
    assert slept, "第二次请求应该等一会儿"


def test_the_user_agent_goes_out() -> None:
    fetcher, session = make_fetcher({"e.com": "<html></html>"})
    fetcher.get("https://e.com/a")
    assert session.calls[0]["headers"]["User-Agent"]


def test_encoding_is_corrected_when_guessed_wrong() -> None:
    """猜错编码时中文会成乱码，而乱码照样能跑 XPath，只是文本全错。"""
    session = FakeSession({"e.com": FakeResponse("<html>中文</html>")})
    response = session.pages["e.com"]
    response.encoding = "iso-8859-1"
    response.apparent_encoding = "utf-8"
    fetcher = Fetcher(session=session, interval=0.0)
    fetcher.get("https://e.com/a")
    assert response.encoding == "utf-8"
