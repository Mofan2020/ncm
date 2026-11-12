"""播放源面板的契约检查。

三个东西必须对得上，任何一处错都是"点了没反应"：

1. app.js 调用的每个 ``state.api.xxx`` 在 ``GuiBridge`` 上真的存在；
2. app.js 取的每个元素 id 在 index.html 里真的存在；
3. app.js 用的每个 i18n key 在两份译文里都有。

这三类错误的共同点是**不报错**——方法不存在是 JS 的 TypeError 被 try/catch
吞掉，id 不存在是 ``null.hidden`` 静默失败，键不存在是把键名原样显示出来。
所以只能靠静态比对。
"""

from __future__ import annotations

import json
import pathlib
import re

BASE = pathlib.Path(__file__).parent.parent
APP_JS = BASE / "src" / "gui" / "assets" / "app.js"
INDEX_HTML = BASE / "src" / "gui" / "assets" / "index.html"
BRIDGE = BASE / "src" / "gui" / "bridge.py"
ZH = BASE / "src" / "i18n" / "zh_cn.json"
EN = BASE / "src" / "i18n" / "en_us.json"

#: 这个面板的范围：从状态对象那一行开始到文件结尾（它是最后追加的一段）。
#: 用代码而不是注释当锚点——全角括号跟半角括号在中文里太容易打错一个。
SECTION_MARKER = "const bangumiSource = {"

#: 面板里出现的元素 id。
PANEL_IDS = (
    "bangumi-seg-source",
    "bgm-source-enabled",
    "bgm-source-auto-mirror",
    "bgm-source-skip-ssl",
    "bgm-source-repo",
    "btn-bgm-source-refresh",
    "bgm-source-status",
    "bgm-source-rules",
    "bgm-source-keyword",
    "btn-bgm-source-search",
    "bgm-source-hits",
    "bgm-source-chapters",
    "bgm-source-player",
    "bgm-source-video",
    "bgm-source-now",
    "bgm-source-msg",
)

#: 面板调用的桥方法。
BRIDGE_METHODS = (
    "get_bangumi_source_settings",
    "save_bangumi_source_settings",
    "get_bangumi_rules",
    "search_bangumi_by_rule",
    "get_bangumi_rule_chapters",
    "get_bangumi_rule_streams",
)

#: 面板用到的 i18n key。
I18N_KEYS = (
    "bangumi.tab_source",
    "bangumi.source_hint",
    "bangumi.source_enabled",
    "bangumi.source_auto_mirror",
    "bangumi.source_skip_ssl",
    "bangumi.source_repo_placeholder",
    "bangumi.source_refresh",
    "bangumi.source_keyword_placeholder",
    "bangumi.source_search",
    "bangumi.source_rule_count",
    "bangumi.source_no_rules",
    "bangumi.source_rule_deprecated",
    "bangumi.source_pick_rule_first",
    "bangumi.rule_keyword_empty",
    "bangumi.source_no_hits",
    "bangumi.source_no_chapters",
    "bangumi.source_looking_stream",
    "bangumi.source_no_stream",
    "bangumi.source_stream_count",
)


def _section() -> str:
    text = APP_JS.read_text(encoding="utf-8")
    at = text.find(SECTION_MARKER)
    assert at >= 0, "app.js 里找不到播放源那一段"
    return text[at:]


def _flatten(node: object, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(value, name))
    else:
        out[prefix] = str(node)
    return out


def _translations(path: pathlib.Path) -> dict[str, str]:
    return _flatten(json.loads(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------------ 元素


def test_the_panel_has_a_tab() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-bangumi-tab="source"' in html


def test_every_panel_id_exists_in_the_markup() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    missing = [name for name in PANEL_IDS if f'id="{name}"' not in html]
    assert not missing, f"HTML 里没有这些 id：{missing}"


def test_every_id_the_script_touches_is_declared() -> None:
    """真实存在的 id 之外，脚本不该去摸别的东西。"""
    section = _section()
    used = set(re.findall(r"bgmSrc\('([^']+)'\)", section))
    used |= set(re.findall(r'getElementById\(\'([^\']+)\'\)', section))
    used |= set(re.findall(r"getElementById\('([^']+)'\)", section))
    declared = set(PANEL_IDS)
    # bangumi-seg-<tab> 是切标签那段在用，属于已知范围
    extra = {name for name in used if name not in declared and not name.startswith("bangumi-seg-")}
    assert not extra, f"脚本摸了没声明的 id：{sorted(extra)}"


# ------------------------------------------------------------------ 桥


def test_every_bridge_method_is_declared() -> None:
    text = BRIDGE.read_text(encoding="utf-8")
    missing = [
        name for name in BRIDGE_METHODS
        if not re.search(rf"def {name}\(", text)
    ]
    assert not missing, f"GuiBridge 上没有这些方法：{missing}"


def test_the_script_only_calls_declared_methods() -> None:
    section = _section()
    called = set(re.findall(r"state\.api\.([a-z_][a-z0-9_]*)\(", section))
    known = set(BRIDGE_METHODS) | {"get_bangumi_subject", "get_bangumi_episodes"}
    extra = called - known
    assert not extra, f"脚本调了没登记的方法：{sorted(extra)}"


def test_unknown_bridge_is_guarded() -> None:
    """桥方法可能还没注入（pywebview 的时序），至少入口要挡一下。"""
    section = _section()
    assert "bridgeHas(" in section


# ------------------------------------------------------------------ 译文


def test_every_key_exists_in_both_languages() -> None:
    zh = _translations(ZH)
    en = _translations(EN)
    for lang, table in (("zh_cn", zh), ("en_us", en)):
        missing = [key for key in I18N_KEYS if key not in table]
        assert not missing, f"{lang} 缺这些键：{missing}"


def test_placeholders_match_between_languages() -> None:
    """占位符对不上，界面里会直接显示 {count} 这种字面量。"""
    zh = _translations(ZH)
    en = _translations(EN)

    def holders(text: str) -> set[str]:
        return set(re.findall(r"\{(\w+)\}", text))

    for key in I18N_KEYS:
        if key in zh and key in en:
            assert holders(zh[key]) == holders(en[key]), f"{key} 的占位符不一致"


def test_every_key_the_script_uses_is_translated() -> None:
    section = _section()
    used = set(re.findall(r"t\('([a-z][a-z0-9_.]*)'", section))
    used |= set(re.findall(r"t\(\(result && result\.error_key\) \|\| '([^']+)'", section))
    zh = _translations(ZH)
    # error.* 是通用键，本来就存在；只检查 bangumi.* 和 common.*
    missing = [
        key for key in used
        if key.startswith(("bangumi.", "common.")) and key not in zh
    ]
    assert not missing, f"脚本用了没翻译的键：{missing}"
