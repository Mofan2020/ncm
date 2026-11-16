"""设置存不下去的三个坑：借 SDM、R-18 开关、Pixiv 那段读不回来。

用户报的现象是「有两个设置无法保存」：
- 自动更新里的「借 SDM 下载更新包」
- pixiv 设置里的「搜索时也向服务器要素材（R-18）」

根因不在一处，但形状一样：**键没有进允许名单，或者读回的时候那一段
整个是空的**。界面读不到真值，就只能拿默认值写回去，看着像保存了，
其实什么都没存。

所以这里钉三件事：
1. ``update_use_sdm`` / ``update_threads`` 能存进去，也能读回来；
2. ``include_r18`` 存进去就是 True，**不会被 nsfw_mode 静默清零**；
3. ``get_settings()`` 里有 pixiv 段，弹窗填表才有真值可填。
"""
from __future__ import annotations

import pytest

# --------------------------------------------------------------- 借 SDM


def test_update_use_sdm_can_be_turned_off(bridge):
    """关掉「借 SDM 下载更新包」要能存住。

    以前这个键不在 ``update_settings('ui', ...)`` 的白名单里，前端发了
    也被丢掉：用户取消勾选，保存后重开还是勾着的。
    """
    assert bridge.update_settings("ui", {"update_use_sdm": False})["success"] is True

    assert bridge.get_settings()["ui"]["update_use_sdm"] is False


def test_update_use_sdm_can_be_turned_on_again(bridge):
    """反向也要能存：白名单不能只挡 False。"""
    bridge.update_settings("ui", {"update_use_sdm": False})
    bridge.update_settings("ui", {"update_use_sdm": True})

    assert bridge.get_settings()["ui"]["update_use_sdm"] is True


def test_update_threads_survives_the_round_trip(bridge):
    """分片数也是一个键，漏了它就永远停在 8。"""
    bridge.update_settings("ui", {"update_threads": 16})

    assert bridge.get_settings()["ui"]["update_threads"] == 16


def test_update_use_sdm_is_written_to_disk(bridge):
    """不只是内存里对：重启之后也得在。"""
    from src.config import settings as settings_module

    bridge.update_settings("ui", {"update_use_sdm": False, "update_threads": 20})
    settings_module.Settings._instance = None
    reloaded = settings_module.get_settings()

    assert reloaded.ui.update_use_sdm is False
    assert reloaded.ui.update_threads == 20


# ----------------------------------------------------------- pixiv R-18


def test_include_r18_is_stored_as_asked(bridge):
    """勾上就是勾上，不许被别的设置顶掉。"""
    bridge.update_settings("pixiv", {"nsfw_mode": "blur", "include_r18": True})

    assert bridge.get_settings()["pixiv"]["include_r18"] is True


def test_include_r18_is_not_silently_cleared_by_hide_mode(bridge):
    """「隐藏」模式下勾 R-18，也要留住用户的勾选。

    以前这里有一条「hide 就把 include_r18 清零」的规则。清零之后请求
    层照样按 safe 发（见 ``_search_mode``），所以清零没改变任何行为，
    只是把用户在设置里亲手勾的东西弄丢了——用户看到的就是「存不住」。
    """
    bridge.update_settings("pixiv", {"nsfw_mode": "hide", "include_r18": True})

    assert bridge.get_settings()["pixiv"]["include_r18"] is True


def test_include_r18_survives_a_restart(bridge):
    from src.config import settings as settings_module

    bridge.update_settings("pixiv", {"nsfw_mode": "blur", "include_r18": True})
    settings_module.Settings._instance = None
    reloaded = settings_module.get_settings()

    assert reloaded.pixiv.include_r18 is True


def test_include_r18_can_be_turned_off_again(bridge):
    """反向也要能存，不然勾上就下不来了。"""
    bridge.update_settings("pixiv", {"nsfw_mode": "blur", "include_r18": True})
    bridge.update_settings("pixiv", {"include_r18": False})

    assert bridge.get_settings()["pixiv"]["include_r18"] is False


# --------------------------------------------------------- 读回那一段


def test_get_settings_has_a_pixiv_section(bridge):
    """回包里必须有 pixiv 段。

    以前整个段是空的：设置弹窗填表时没有真值可填，控件停在 HTML 的
    默认值上，用户点保存就把默认值写回去。
    """
    settings = bridge.get_settings()

    assert "pixiv" in settings
    for key in ("nsfw_mode", "include_r18", "hide_ai", "quality",
                "bookmark_private", "template", "page_size"):
        assert key in settings["pixiv"], f"pixiv 段缺 {key}"


def test_get_settings_pixiv_reflects_what_was_stored(bridge):
    bridge.update_settings("pixiv", {"nsfw_mode": "blur", "include_r18": True,
                                     "hide_ai": True, "page_size": 24})

    pixiv = bridge.get_settings()["pixiv"]

    assert pixiv["nsfw_mode"] == "blur"
    assert pixiv["include_r18"] is True
    assert pixiv["hide_ai"] is True
    assert pixiv["page_size"] == 24


def test_get_settings_has_a_bangumi_section(bridge):
    """Bangumi 同理：那一段以前也不在回包里。"""
    settings = bridge.get_settings()

    assert "bangumi" in settings
    for key in ("api_host", "web_host", "request_interval",
                "skip_ssl_verify", "mirrors", "resource_sites"):
        assert key in settings["bangumi"], f"bangumi 段缺 {key}"


# ------------------------------------------------------ 界面填表那一层


def test_the_settings_form_fills_the_pixiv_controls():
    """填表函数必须填 Pixiv 那几个控件。

    不填 = 控件停在 HTML 默认值上 = 用户点保存就把默认值写回去。
    """
    from pathlib import Path

    app = (Path(__file__).resolve().parent.parent
           / "src" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")
    fill = app.split("function fillSettingsForm()")[1].split("\n}\n")[0]

    for ident in ("pixiv-nsfw-mode", "pixiv-include-r18", "pixiv-hide-ai",
                  "pixiv-quality", "pixiv-bm-private", "pixiv-page-size"):
        assert ident in fill, f"fillSettingsForm() 没填 {ident}"
    assert "s.pixiv" in fill, "fillSettingsForm() 没读 state.settings.pixiv"


def test_the_settings_modal_reloads_before_opening():
    """打开设置弹窗要重新拉一次设置。

    不拉就填，弹窗里显示的是上一次的残留值，用户看到的勾选框状态
    和后端存的不一样。
    """
    from pathlib import Path

    app = (Path(__file__).resolve().parent.parent
           / "src" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")

    assert "function openSettingsModal()" in app
    opener = app.split("function openSettingsModal()")[1].split("\n}\n")[0]
    assert "loadSettings()" in opener, "打开设置弹窗前没有重新拉设置"


@pytest.mark.parametrize("ident", [
    "pixiv-nsfw-mode", "pixiv-include-r18", "pixiv-hide-ai",
    "pixiv-quality", "pixiv-bm-private", "pixiv-page-size",
    "pixiv-template",
])
def test_every_pixiv_control_has_exactly_one_id(ident):
    """同一份设置不能在页面上出现两次。

    出现两次的话 ``getElementById`` 只会拿到第一个，用户改的是第二个，
    保存的是第一个——「设置没生效」的经典来源。
    """
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent
            / "src" / "gui" / "assets" / "index.html").read_text(encoding="utf-8")

    assert html.count(f'id="{ident}"') == 1, f"{ident} 出现了不止一次"
