"""Pixiv 全屏查看器的界面契约：id、文案、样式、接线。

查看器的**行为**（缩放、拖动、换页）在
``scripts/check-pixiv-viewer.mjs`` 里测——那部分住在前端，只有把
``pixiv.js`` 加载起来驱动才是诚实的测法。

这一份盯的是那些「少一个也不报错，只在点下去的时候什么都不发生」
的东西：元素 id、文案键、样式、事件接线。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
PIXIV_JS = (ASSETS / "pixiv.js").read_text(encoding="utf-8")
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")
CSS = (ASSETS / "style.css").read_text(encoding="utf-8")
I18N = ROOT / "src" / "i18n"

#: 查看器用到的元素。少一个，界面上就少一块。
VIEWER_IDS = (
    "pixiv-viewer",
    "pixiv-viewer-bg",
    "pixiv-viewer-stage",
    "pixiv-viewer-image",
    "pixiv-viewer-loading",
    "pixiv-viewer-pager",
    "pixiv-viewer-zoom",
    "btn-pixiv-viewer-close",
    "btn-pixiv-viewer-prev",
    "btn-pixiv-viewer-next",
    "btn-pixiv-viewer-reset",
)


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


def test_the_viewer_has_all_its_elements():
    for ident in VIEWER_IDS:
        assert f'id="{ident}"' in HTML, f"缺元素：{ident}"


def test_the_viewer_elements_exist_only_once():
    """同一个 id 出现两次的话，getElementById 只会拿到第一个。"""
    for ident in VIEWER_IDS:
        assert HTML.count(f'id="{ident}"') == 1, f"{ident} 出现了不止一次"


def test_the_viewer_is_outside_the_views():
    """查看器要盖住整个窗口，所以不能在某个 view 里面。

    放在 view 里面的话它会被祖先的 ``overflow`` 裁掉，也会被 view 的
    定位带走——「全屏」就变成了「那个面板里的全屏」。
    """
    viewer_at = HTML.index('id="pixiv-viewer"')
    # 所有 view 的 section 都在查看器之前开始。
    for view in ('id="view-pixiv"', 'id="view-bili"', 'id="view-home"'):
        if view in HTML:
            assert HTML.index(view) < viewer_at, f"{view} 在查看器之后"


def test_the_viewer_sits_over_the_settings_modal():
    """查看器的 z-index 比设置弹窗高。

    用户在设置弹窗里点开一张图也要能进全屏——弹窗盖着图就点不到。
    """
    viewer_rule = CSS.split(".pixiv-viewer {")[1].split("}")[0]
    assert "z-index" in viewer_rule
    viewer_z = int(re.search(r"z-index:\s*(\d+)", viewer_rule).group(1))

    modal_rules = re.findall(r"\.modal\s*\{[^}]*\}", CSS)
    modal_z = 0
    for rule in modal_rules:
        found = re.search(r"z-index:\s*(\d+)", rule)
        if found:
            modal_z = max(modal_z, int(found.group(1)))
    assert viewer_z > modal_z, (
        f"查看器的 z-index ({viewer_z}) 不比设置弹窗 ({modal_z}) 高")


def test_the_viewer_is_hidden_by_default():
    """默认必须藏起来：不然一启动就盖住整个界面。"""
    at = HTML.index('id="pixiv-viewer"')
    tag = HTML[at:at + 200]
    assert "hidden" in tag.split(">")[0], "查看器默认没有 hidden"


def test_the_viewer_has_the_styles_it_needs():
    for selector in (".pixiv-viewer", ".pixiv-viewer-stage",
                     ".pixiv-viewer-loading", ".pixiv-viewer-bg",
                     ".pixiv-viewer-bar", ".pixiv-viewer-nav"):
        assert selector in CSS, f"缺样式：{selector}"


def test_the_image_is_scaled_with_a_transform():
    """缩放必须走 transform。

    改 width/height 会让浏览器每帧重排，拖动和滚轮都会掉帧。
    """
    assert "transform" in PIXIV_JS
    assert "scale(" in PIXIV_JS
    # CSS 里也要允许 transform 生效（不能有 transform: none 覆盖）。
    assert ".pixiv-viewer-stage img" in CSS


def test_the_viewer_keeps_the_aspect_ratio():
    """等比放大：图用 max-width/max-height 铺满，缩放只改 transform。

    改了宽高就等于拉伸了，长宽比不对。
    """
    rule = CSS.split(".pixiv-viewer-stage img")[1].split("}")[0]
    assert "max-width" in rule and "max-height" in rule


def test_switching_pages_takes_the_old_image_down():
    """换页时旧图必须先撤下来。

    留着旧图的话，新图没加载出来之前屏幕上还是上一张，用户会以为
    翻页没生效。
    """
    show = PIXIV_JS.split("function pixivViewerShow(index)")[1].split("\n}\n")[0]
    assert "removeAttribute('src')" in show, "换页时没有撤掉旧图"
    assert "loading.hidden = false" in show, "换页时没有显示「加载中」"


def test_the_new_image_hides_the_loading_hint():
    """新图到了，「加载中」要消失。"""
    assert "image.onload" in PIXIV_JS
    assert "loading.hidden = true" in PIXIV_JS


def test_the_zoom_accepts_any_ratio():
    """任意比例：滚轮给的是连续的值，不是几个固定档位。"""
    assert "Math.exp(" in PIXIV_JS, "缩放不是连续值"
    assert "VIEWER_MIN_SCALE" in PIXIV_JS and "VIEWER_MAX_SCALE" in PIXIV_JS


def test_the_zoom_works_with_the_wheel_and_the_touchpad():
    """滚轮和触控板捏合都要能缩放。

    触控板捏合在浏览器里是 ctrl+wheel，两条路都要接。
    """
    bind = PIXIV_JS.split("function pixivViewerBind()")[1]
    assert "'wheel'" in bind
    assert "ctrlKey" in bind, "触控板捏合（ctrl+wheel）没有接"
    # 步长要按来源分开：滚轮一格约 120，捏合只有几。
    assert "event.ctrlKey" in bind, "滚轮和捏合没有分开算步长"


def test_a_single_zoom_step_is_capped():
    """单次缩放的幅度必须封顶。

    触控板甩一下的 delta 能到几百，不封顶的话一甩就直接顶到最大倍率。
    """
    bind = PIXIV_JS.split("function pixivViewerBind()")[1]
    assert "Math.max(" in bind and "Math.min(" in bind, "单次步长没有封顶"


def test_the_image_can_be_dragged():
    assert "'pointerdown'" in PIXIV_JS
    assert "'pointermove'" in PIXIV_JS
    assert "'pointerup'" in PIXIV_JS


def test_the_viewer_is_opened_by_clicking_the_big_image():
    """点详情里的大图进全屏。"""
    assert "PixivViewer.open(" in PIXIV_JS
    assert "pixiv-detail-image" in PIXIV_JS
    # 大图要有「可以点」的提示。
    assert "is-zoomable" in PIXIV_JS
    assert "is-zoomable" in CSS


def test_the_viewer_has_its_own_keyboard_handling():
    """方向键翻页、Esc 关闭。"""
    assert "'ArrowLeft'" in PIXIV_JS
    assert "'ArrowRight'" in PIXIV_JS
    assert "'Escape'" in PIXIV_JS


def test_the_detail_panel_does_not_steal_the_keys_while_the_viewer_is_open():
    """查看器开着的时候，方向键归它管。

    不然一次按键既翻查看器又翻详情页。
    """
    assert "PixivViewer.state.open" in PIXIV_JS


def test_every_viewer_text_key_exists_in_both_languages():
    used = set(re.findall(r"\bt\('(pixiv\.viewer_[a-z_]+)'", PIXIV_JS))
    used |= {key for key in re.findall(r'data-i18n="([^"]+)"', HTML)
             if key.startswith("pixiv.viewer_")}
    zh = _flat("zh_cn")
    en = _flat("en_us")

    assert used, "应该有文案"
    assert sorted(k for k in used if k not in zh) == [], "中文缺键"
    assert sorted(k for k in used if k not in en) == [], "英文缺键"


def test_the_node_check_script_exists():
    """行为检查的脚本必须在：没有脚本就没有行为测试。"""
    assert (ROOT / "scripts" / "check-pixiv-viewer.mjs").exists()
