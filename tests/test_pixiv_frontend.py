"""Pixiv 前端的契约：id、文案、桥方法三处对不上都是静默坏掉。

和 ``test_frontend_assets.py`` 管 app.js 的方式一样，只是这一份专门盯
``pixiv.js``：它的字面量 id 少一个不会报错，只会在点下去的时候什么都不
发生；桥方法名写错会在控制台留一条 rejection，界面上只是一片空白。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
PIXIV_JS = (ASSETS / "pixiv.js").read_text(encoding="utf-8")
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")
BRIDGE = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")
I18N = ROOT / "src" / "i18n"


def _flat(name: str) -> dict[str, str]:
    data = json.loads((I18N / f"{name}.json").read_text(encoding="utf-8"))
    out: dict[str, str] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, path + ".")
            else:
                out[path] = value

    walk(data)
    return out


def test_every_element_the_script_looks_up_exists():
    """``pixivEl('x')`` 里的 x 必须在页面上有对应元素。"""
    ids = set(re.findall(r"pixivEl\('([^']+)'\)", PIXIV_JS))
    ids |= set(re.findall(r"getElementById\('([^']+)'\)", PIXIV_JS))

    missing = sorted(i for i in ids if f'id="{i}"' not in HTML)

    assert ids, "脚本应该按 id 找元素"
    assert missing == [], f"pixiv.js 引用了不存在的元素：{missing}"


def test_every_text_key_exists_in_both_languages():
    # \b 是必须的：`createElement('div')` 的尾巴也长得像 t('div')。
    used = set(re.findall(r"\bt\('([a-z0-9_.]+)'", PIXIV_JS))
    used |= {key for key in re.findall(r'data-i18n(?:-title|-placeholder)?="([^"]+)"', HTML)
             if key.startswith("pixiv.")}
    zh = _flat("zh_cn")
    en = _flat("en_us")

    missing_zh = sorted(k for k in used if k not in zh)
    missing_en = sorted(k for k in used if k not in en)

    assert used, "应该有文案"
    assert missing_zh == [], f"中文缺这些键：{missing_zh}"
    assert missing_en == [], f"英文缺这些键：{missing_en}"


def test_the_placeholders_match_between_languages():
    """``{count}`` 只在一门语言里出现，那门语言就会把花括号原样显示出来。"""
    zh = _flat("zh_cn")
    en = _flat("en_us")
    broken = []
    for key, text in zh.items():
        if not key.startswith("pixiv."):
            continue
        if set(re.findall(r"\{(\w+)\}", text)) != set(re.findall(r"\{(\w+)\}", en.get(key, ""))):
            broken.append(key)

    assert broken == [], f"占位符不一致：{broken}"


def test_every_bridge_method_the_page_calls_exists():
    methods = set(re.findall(r"^    def (pixiv_\w+)", BRIDGE, re.M))
    called = set(re.findall(r"api\.(pixiv_\w+)", PIXIV_JS))

    missing = sorted(m for m in called if m not in methods)

    assert called, "应该调用后端"
    assert missing == [], f"pixiv.js 调用了不存在的桥方法：{missing}"


def test_the_page_is_reachable_from_the_home_screen():
    """一张首页卡片、一个顶栏标签、一个 section，三样缺一样就进不去。"""
    assert 'data-entry="pixiv"' in HTML
    assert 'data-view="pixiv"' in HTML
    assert 'id="view-pixiv"' in HTML


def test_the_script_loads_before_the_one_that_calls_it():
    """pixiv.js 在 app.js 之前：app.js 的 boot 会叫 ``window.PixivUI``。"""
    pixiv_at = HTML.index('src="pixiv.js"')
    app_at = HTML.index('src="app.js"')

    assert pixiv_at < app_at


def test_every_tab_has_a_segment_to_show():
    """标签和它的内容块要一一对上，不然点了就是一片空白。"""
    tabs = set(re.findall(r'data-pixiv-tab="([a-z]+)"', HTML))

    missing = [tab for tab in sorted(tabs) if f'id="pixiv-seg-{tab}"' not in HTML]

    assert tabs, "应该有标签"
    assert missing == [], f"这些标签没有对应的内容块：{missing}"


def test_the_detail_panel_closes_on_escape():
    assert "'Escape'" in PIXIV_JS
    assert "closeDetail()" in PIXIV_JS


def test_a_like_does_not_open_a_confirm_box():
    """点赞不再弹确认框。

    用户原话：「点赞时也不要向用户确认是否点赞，弹窗确认很麻烦」。
    一个「点赞」按钮弹一次确认，比按错一次还费事；状态改由按钮本身说。
    """
    assert "window.confirm" not in PIXIV_JS
    assert "pixiv.like_confirm" not in PIXIV_JS


def test_a_liked_heart_is_marked_on_the_button():
    """点过赞心要变红——靠颜色说状态，不靠一闪而过的提示。"""
    assert "work.is_liked = true" in PIXIV_JS
    assert "classList.toggle('is-active', !!work.is_liked)" in PIXIV_JS
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    assert "#btn-pixiv-detail-like.is-active" in css


def test_the_description_is_reduced_to_text():
    """作品简介是 HTML，直接当标记挂进 DOM 等于让外面决定我们的结构。"""
    assert "replace(/<[^>]*>/g" in PIXIV_JS


def test_the_pixiv_styles_are_present():
    css = (ASSETS / "style.css").read_text(encoding="utf-8")

    for selector in (".pixiv-detail", ".pixiv-stage", ".pixiv-curtain",
                     ".pixiv-tag", ".pixiv-account", ".pixiv-nsfw-badge",
                     ".pixiv-follow-row", ".page-state"):
        assert selector in css, f"缺样式：{selector}"


def test_the_views_positioning_is_explicit():
    """详情面板用 absolute 盖在页面上，页面本身必须是定位容器。"""
    css = (ASSETS / "style.css").read_text(encoding="utf-8")

    assert "#view-pixiv { position: relative; }" in css
