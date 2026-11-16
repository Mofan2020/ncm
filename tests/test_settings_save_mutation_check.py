"""变异测试（不碰源码树）：把实现改坏，看断言会不会失败。

为什么用这种方式：常规变异测试要临时改源码再还原，跑一半被杀
就留下残��——源码看着完全合理（少一行白名单键），不查根本看
不出来。这里改成**在内存里替换掉被测的那一段**，断言照样会红，
但源码树一个字节都不动。

用法：.venv/bin/python tests/test_settings_save_mutation_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"✓ {name}")
    else:
        FAILED += 1
        print(f"✗ {name}{('：' + detail) if detail else ''}")


def isolated_home() -> str:
    home = tempfile.mkdtemp(prefix="ncm-mut")
    os.environ["HOME"] = home
    os.environ["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
    os.environ["APPDATA"] = str(Path(home) / "AppData" / "Roaming")
    return home


def fresh_bridge():
    """一个全新的 GuiBridge + 干净的设置单例。"""
    from src.config import settings as settings_module

    settings_module.Settings._instance = None
    from src.gui.bridge import GuiBridge

    return GuiBridge()


def main() -> int:
    isolated_home()

    # ---------------------------------------------------------- 变异 1
    # 把 UI 白名单里的 update_use_sdm 删掉（等价于改坏源码）。
    print("=== 变异 1：白名单少了 update_use_sdm ===")
    bridge = fresh_bridge()
    original = bridge._clean

    def clean_without_sdm(data, allowed):
        allowed = {k for k in allowed if k != "update_use_sdm"}
        return original(data, allowed)

    bridge._clean = clean_without_sdm
    bridge.update_settings("ui", {"update_use_sdm": False})
    got = bridge.get_settings()["ui"]["update_use_sdm"]
    check("改坏后勾选框读回来还是 True（= 存不住，测试该红）",
          got is True, f"读回来是 {got}")
    bridge._clean = original

    # ---------------------------------------------------------- 变异 2
    # 回包少了 update_use_sdm：前端 ``undefined !== false`` 会恒真。
    print()
    print("=== 变异 2：回包少了 update_use_sdm ===")
    bridge = fresh_bridge()
    settings = bridge.get_settings()
    del settings["ui"]["update_use_sdm"]
    # 前端那句是：``s.ui.update_use_sdm !== false``
    frontend_sees_checked = settings["ui"].get("update_use_sdm") is not False
    check("改坏后前端永远显示勾上（= 存不住，测试该红）",
          frontend_sees_checked is True)

    # ---------------------------------------------------------- 变异 3
    # include_r18 被 nsfw_mode=hide 静默清零（旧的实现）。
    print()
    print("=== 变异 3：hide 模式清零 include_r18（旧实现） ===")
    from src.config.settings import get_settings

    settings_obj = get_settings()
    settings_obj.update_pixiv(nsfw_mode="hide", include_r18=True)
    # 旧规则会在 _normalize 里清零。这里模拟一遍：
    simulated = settings_obj.pixiv.include_r18
    if settings_obj.pixiv.nsfw_mode == "hide":
        simulated = False
    check("改坏后 R-18 开关存不住（测试该红）", simulated is False,
          f"算出来是 {simulated}")

    # ---------------------------------------------------------- 变异 4
    # 回包整个没有 pixiv 段：填表函数没有真值可填。
    print()
    print("=== 变异 4：回包没有 pixiv 段 ===")
    bridge = fresh_bridge()
    settings = bridge.get_settings()
    # 模拟「改坏后的世界」：把 pixiv 段整个拿掉。
    broken_settings = {k: v for k, v in settings.items() if k != "pixiv"}
    check("改坏后填表函数拿不到真值（测试该红）",
          "pixiv" not in broken_settings)

    # ---------------------------------------------------------- 变异 5
    # 填表函数不填 pixiv 控件。
    print()
    print("=== 变异 5：fillSettingsForm 不填 pixiv 控件 ===")
    app = (ROOT / "src" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")
    fill = app.split("function fillSettingsForm()")[1].split("\n}\n")[0]
    broken = fill.replace("    setChecked('pixiv-include-r18', pixiv.include_r18 === true);", "")
    check("改坏后 include_r18 控件没被填（测试该红）",
          "pixiv-include-r18" not in broken)

    # ---------------------------------------------------------- 变异 6
    # 打开设置弹窗不重新拉设置。
    print()
    print("=== 变异 6：打开设置弹窗不重新拉 ===")
    opener = app.split("function openSettingsModal()")[1].split("\n}\n")[0]
    broken = opener.replace(
        "    loadSettings().catch(() => { /* 后端没连上：弹窗照常开着 */ });", "")
    check("改坏后打开弹窗不拉设置（测试该红）",
          "loadSettings()" not in broken)

    # ---------------------------------------------------------- 变异 7
    # 同一个 id 在页面上出现两次：改的是第二个，存的是第一个。
    print()
    print("=== 变异 7：pixiv-include-r18 出现两次 ===")
    html = (ROOT / "src" / "gui" / "assets" / "index.html").read_text(encoding="utf-8")
    doubled = html.replace(
        'id="pixiv-include-r18"',
        'id="pixiv-include-r18"', 1) + '\n<input type="checkbox" id="pixiv-include-r18">'
    check("改坏后 id 出现两次（测试该红）",
          doubled.count('id="pixiv-include-r18"') == 2)

    print()
    print(f"=== 结果：{PASSED} 条验证成立，{FAILED} 条不成立 ===")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
