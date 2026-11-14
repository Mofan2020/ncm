"""发现页的「更多」和 Pixiv 设置的「保存」。

用户报的两件事：

1. **发现页点「更多」没反应。** 根因不在界面：pixiv 的推荐流**没有分页**
   （同一个地址每次都给一批新的随机推荐，``offset`` 会被直接忽略，实测
   offset=30 和第一页的 id 完全一样）。所以「更多」只能是再要一批，然后
   把已经显示过的挑掉。
2. **Pixiv 设置存不上。** 设置弹窗的保存列表里本来没有 Pixiv 这一类，
   用户在弹窗里改完点「保存」，那些值根本没进后端；页签里又只有
   「改完立刻存」，控件没失焦就不会触发 change，弹窗一关就丢了。

这里钉住：更多会去掉已经显示过的、设置弹窗会带上 Pixiv、页签里有
一个明确的保存按钮。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.gui.pixiv_bridge import PixivBridge

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
PIXIV_JS = (ASSETS / "pixiv.js").read_text(encoding="utf-8")
APP_JS = (ASSETS / "app.js").read_text(encoding="utf-8")
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")
BRIDGE_PY = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")


class _FakeSettings:
    nsfw_mode = "hide"
    include_r18 = False
    hide_ai = False
    quality = "original"
    download_dir = ""
    template = "{artist} - {title}"
    bookmark_private = False
    page_size = 48


class _FakeClient:
    def __init__(self, works: list[dict]) -> None:
        self.works = works
        self.calls: list[dict] = []
        self.last_error = ""

    def discovery(self, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        return list(self.works)


def _work(pid: str) -> dict:
    return {"id": pid, "title": "t", "x_restrict": 0, "sl": 2,
            "thumb": f"https://i.pximg.net/thumb/{pid}.jpg",
            "user_id": "7", "user_name": "a", "ai_type": 0, "nsfw": False}


def _bridge(monkeypatch, works: list[dict]) -> tuple[PixivBridge, _FakeClient]:
    client = _FakeClient(works)
    session = type("S", (), {"client": client, "is_logged_in": True})()
    bridge = PixivBridge(session=session)
    import src.config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings",
                        lambda: type("S", (), {"pixiv": _FakeSettings()})())
    return bridge, client


def test_more_drops_what_is_already_on_screen(monkeypatch) -> None:
    """已经显示过的作品不该再出现一遍。"""
    works = [_work("1"), _work("2"), _work("3")]
    bridge, _client = _bridge(monkeypatch, works)
    result = bridge.discover_more(["1", "3"])
    assert [w["id"] for w in result["works"]] == ["2"]


def test_more_asks_pixiv_again_instead_of_reading_the_cache(monkeypatch) -> None:
    """推荐流每次都是新的一批，走缓存就永远还是那几十张。"""
    bridge, client = _bridge(monkeypatch, [_work("9")])
    bridge.discover_more([])
    assert client.calls, "「更多」应该真的去问一次 pixiv"


def test_the_page_has_a_more_button() -> None:
    assert 'id="btn-pixiv-discover-more"' in HTML
    assert "api.pixiv_discover_more" in PIXIV_JS
    # 结果是**接在**原来那批后面，不是替换掉。
    assert "pixivState.discover.concat(works)" in PIXIV_JS


def test_the_settings_panel_has_its_own_save_button() -> None:
    """光靠「改完立刻存」不够：控件不失焦就不会触发 change。"""
    assert 'id="btn-pixiv-settings-save"' in HTML
    assert "saveAllSettings" in PIXIV_JS
    for field in ("nsfw_mode", "include_r18", "hide_ai", "quality",
                  "bookmark_private", "template", "page_size"):
        assert field in PIXIV_JS, field


def test_the_settings_modal_saves_the_pixiv_panel_too() -> None:
    """设置弹窗的「保存」以前不管 Pixiv：改了等于没改。"""
    match = re.search(
        r"for \(const category of \[([^\]]+)\]\)", APP_JS)
    assert match, "设置弹窗应该有一个分类列表"
    categories = [item.strip().strip("'\"") for item in match.group(1).split(",")]
    assert "pixiv" in categories
    assert 'category == "pixiv"' in BRIDGE_PY


def test_the_pixiv_panel_values_are_collected_for_the_modal() -> None:
    """弹窗保存时要读 Pixiv 那一页的控件。"""
    match = re.search(r"pixiv: \{(.*?)\n        \},", APP_JS, re.S)
    assert match, "app.js 里应该有一份 Pixiv 的表单数据"
    block = match.group(1)
    for field in ("nsfw_mode", "include_r18", "hide_ai", "quality",
                  "bookmark_private", "template", "page_size"):
        assert field in block, field


def test_the_i18n_keys_the_new_buttons_use_exist() -> None:
    for name in ("zh_cn", "en_us"):
        data = json.loads((ROOT / "src" / "i18n" / f"{name}.json").read_text(encoding="utf-8"))
        assert data["common"]["save"]
        assert data["pixiv"]["load_more"]
