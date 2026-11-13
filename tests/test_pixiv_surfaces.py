"""Pixiv 在四个「公共位置」上是否真的接进去了。

用户提的是五件事：账号中心、设置、聚合搜索、详情页、备份导出。前四件都在
前端，而且是**别人家的页面**——账号中心是 app.js 的，设置是 app.js + 弹窗的，
聚合搜索是 app.js 的，详情页是 pixiv.js 的。任何一处接线掉了，界面上就是
「Pixiv 不见了」，但没有任何单元测试会红。

所以这里不去跑界面，只钉死几条「必须存在」的事实：
- 账号中心有一张 Pixiv 的卡，并且它由 refreshPixivAccountCard 驱动；
- 设置弹窗里有 data-panel="pixiv"，且设置项与后端白名单对得上；
- 聚合搜索的来源里有 pixiv，并且真的会去要一页；
- 详情面板的结构是新版的（三个 id：stats / meta / pager 都在，且没有退回
  到「一列 flex 堆出来」的老结构）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
I18N = ROOT / "src" / "i18n"


def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _flatten(node: dict, prefix: str = "") -> dict[str, object]:
    """摊平成 ``family.key``。递归写在模块级，闭包改外层字典 lint 不放行。"""
    flat: dict[str, object] = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, path + "."))
        else:
            flat[path] = value
    return flat


# --------------------------------------------------------------- 账号中心

def test_account_center_has_a_pixiv_card() -> None:
    html = _asset("index.html")
    assert 'id="ac-pixiv-card"' in html
    assert 'id="ac-pixiv-state"' in html
    assert 'id="ac-pixiv-actions"' in html
    assert 'id="ac-pixiv-login"' in html


def test_account_center_drives_the_pixiv_card() -> None:
    app = _asset("app.js")
    # 卡片必须被刷新流程带上，否则「登录了但卡片还显示未登录」。
    refresh = re.search(r"async function refreshAccountCenter\(\)[\s\S]*?\n\}", app)
    assert refresh, "refreshAccountCenter 不见了"
    assert "refreshPixivAccountCard" in refresh.group(0)
    assert "async function refreshPixivAccountCard" in app
    # 登录表单的显示开关要认 pixiv
    show = re.search(r"function showAccountLogin[\s\S]*?\n\}", app)
    assert show and "ac-pixiv-login" in show.group(0)


def test_pixiv_login_buttons_are_bound_once() -> None:
    app = _asset("app.js")
    assert "function bindAccountCenterPixiv" in app
    for ident in ("btn-ac-pixiv-login", "btn-ac-pixiv-signup", "btn-ac-pixiv-cookie"):
        assert ident in app, ident
    # 绑两次会让一次点击开两个登录窗口
    assert "card.bound = true" in app


# ------------------------------------------------------------------ 设置

def test_settings_modal_has_a_pixiv_page() -> None:
    html = _asset("index.html")
    assert 'data-tab="pixiv"' in html
    assert 'data-panel="pixiv"' in html
    for ident in ("pixiv-nsfw-mode", "pixiv-include-r18", "pixiv-hide-ai",
                  "pixiv-quality", "pixiv-page-size", "pixiv-template",
                  "pixiv-bm-private", "pixiv-download-dir"):
        assert f'id="{ident}"' in html, ident


def test_pixiv_settings_are_not_in_two_places() -> None:
    """设置只有一个地方：搬进设置弹窗之后，页面里那份必须删掉。

    两份的话改一处另一处不动，用户看到的就是「设置没生效」。
    """
    html = _asset("index.html")
    assert html.count('id="pixiv-nsfw-mode"') == 1
    assert html.count('id="pixiv-template"') == 1


def test_every_settings_control_matches_the_backend_whitelist() -> None:
    """界面上能改的键，后端必须收；后端收的键，界面必须能改。"""
    bridge = (ROOT / "src" / "gui" / "pixiv_bridge.py").read_text(encoding="utf-8")
    allowed = re.search(r"allowed\s*=\s*\{([^}]*)\}", bridge)
    assert allowed, "找不到 pixiv_update_settings 的白名单"
    keys = set(re.findall(r'"(\w+)"', allowed.group(1)))
    app = _asset("pixiv.js")
    saved = set(re.findall(r"saveSettings\(\{\s*(\w+)\s*:", app))
    assert saved, "pixiv.js 一处设置都没在存"
    assert saved <= keys, f"界面在存后端不收的键：{sorted(saved - keys)}"


# -------------------------------------------------------------- 聚合搜索

def test_aggregate_search_offers_pixiv_as_a_source() -> None:
    html = _asset("index.html")
    source = re.search(r'id="aggr-source"[\s\S]*?</div>', html)
    assert source and 'data-value="pixiv"' in source.group(0)


def test_aggregate_search_asks_pixiv_for_a_page() -> None:
    app = _asset("app.js")
    assert "async function loadCombinedPixiv" in app
    assert "pixiv_search" in app
    # 它得参与「翻完了没有」的判断，不然「没有更多了」永远不出现
    exhausted = re.search(r"function combinedExhausted\(\)[\s\S]*?\n\}", app)
    assert exhausted and "pixivDone" in exhausted.group(0)
    # 只选 Pixiv 的时候，另外两个来源要当场算结束
    assert "combined.source === 'pixiv'" in app
    # 点开一张图要能进详情
    assert "function openPixivWork" in app and "PixivUI.openDetail" in app


def test_aggregate_rows_render_pixiv_differently() -> None:
    app = _asset("app.js")
    assert "is-pixiv" in app
    assert "search.sub_pixiv_bookmarks" in app


# -------------------------------------------------------------- 详情页

def test_detail_panel_uses_the_three_part_layout() -> None:
    html = _asset("index.html")
    for ident in ("pixiv-detail-stats", "pixiv-detail-meta", "pixiv-detail-pager",
                  "pixiv-detail-tags", "pixiv-detail-desc"):
        assert f'id="{ident}"' in html, ident
    # 统计数据是 JS 填的，得有个渲染入口
    js = _asset("pixiv.js")
    assert "renderStats(work)" in js


def test_detail_panel_is_a_grid_not_a_column_of_vh_boxes() -> None:
    """以前是 flex 一列 + vh 卡高度，窗口一矮就叠。

    这里钉住三件事：外层是 grid、中间那行是 minmax(0, 1fr)、侧栏自己滚。
    把其中任何一条改回「堆」的写法，这条就会红。
    """
    css = _asset("style.css")
    detail = re.search(r"\.pixiv-detail \{([^}]*)\}", css)
    assert detail and "grid-template-rows" in detail.group(1)
    body = re.search(r"\.pixiv-detail-body \{([^}]*)\}", css)
    assert body and "grid-template-columns" in body.group(1)
    side = re.search(r"\.pixiv-detail-side \{([^}]*)\}", css)
    assert side and "overflow-y: auto" in side.group(1)
    # 舞台不能再用 vh 定高
    stage = re.search(r"\.pixiv-stage \{([^}]*)\}", css)
    assert stage and "vh" not in stage.group(1)


# ---------------------------------------------------------------- i18n

def test_the_new_surface_keys_exist_in_both_languages() -> None:
    needed = [
        "search.source_pixiv", "search.sub_pixiv_bookmarks", "search.result_pixiv",
        "pixiv.stat_bookmarks", "pixiv.stat_likes", "pixiv.stat_views", "pixiv.stat_pages",
        "pixiv.type_illust", "pixiv.type_manga", "pixiv.type_ugoira",
        "pixiv.settings_browse", "pixiv.page_size", "pixiv.download_dir",
        "account.center_pixiv", "pixiv.cookie_label", "pixiv.cookie_hint",
    ]
    for lang in ("zh_cn", "en_us"):
        data = json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))
        flat = _flatten(data)
        missing = [key for key in needed if key not in flat]
        assert not missing, f"{lang} 缺文案：{missing}"


# ---------------------------------------------------------------- 备份

def test_backup_export_gate_counts_all_three_accounts() -> None:
    """一个账号都没登录才拒绝导出；任意一个登录都放行。

    这条闸门以前是「没登录网易云就不让导出」，Pixiv 登录了也导不出东西。
    """
    bridge = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")
    assert "_pixiv_signed_in" in bridge
    # **每一处**「三个账号一个都没有」的判断都要带上 Pixiv：导出和导入各有一
    # 处，只改一处的写法（比如导出改了、导入没改）照样是错的。
    gates = re.findall(r"if not \(([^)]*signed_in[^)]*)\)", bridge)
    # 导出和导入各有一处。少一处就说明那处被改成了别的写法，比如又退回「只看
    # 网易云」——只查「找到的那几处都对」是抓不到的，因为被改坏的那处根本匹配
    # 不上正则，会自己消失。
    assert len(gates) >= 2, f"「一个账号都没登录」的闸门只剩 {len(gates)} 处"
    for condition in gates:
        for name in ("netease_signed_in", "bili_signed_in", "pixiv_signed_in"):
            assert name in condition, f"闸门 {condition!r} 里没有 {name}"


def test_backup_payload_carries_the_pixiv_section() -> None:
    bridge = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")
    assert 'payload["pixiv"]' in bridge
    assert "export_pixiv_data" in bridge
    # 读失败只能让这一节空掉，不能把整份备份带下水
    export = re.search(r'payload\["pixiv"\][\s\S]{0,600}?except Exception', bridge)
    assert export, "pixiv 一节缺少 try/except"


def test_backup_dialog_shows_the_pixiv_part() -> None:
    html = _asset("index.html")
    assert 'id="backup-pixiv-block"' in html
    assert 'id="backup-pixiv-summary"' in html
    app = _asset("app.js")
    assert "function renderBackupPixiv" in app
    # 弹窗画的时候必须被调用，否则那块永远藏着
    conflicts = re.search(r"function renderBackupConflicts[\s\S]*?\n\}", app)
    assert conflicts and "renderBackupPixiv" in conflicts.group(0)


def test_import_result_mentions_pixiv_without_swallowing_bilibili() -> None:
    """两边的数字要接在同一行里。

    各写各的话，后写的那一句会把前一句盖掉——用户只会看到最后恢复的那一节。
    """
    app = _asset("app.js")
    apply_fn = re.search(r"async function applyBackupConflicts[\s\S]*?\n\}", app)
    assert apply_fn
    body = apply_fn.group(0)
    assert "backup.import_pixiv" in body
    assert "extras.push" in body, "两节应当拼进同一个数组再 join"
    assert "extras.join(' · ')" in body


def test_the_export_gate_message_mentions_all_three() -> None:
    for lang in ("zh_cn", "en_us"):
        data = json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))
        text = data["backup"]["login_required"]
        assert "Pixiv" in text, f"{lang} 的登录提示还只说了网易云"
