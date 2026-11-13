"""前端调后端时参数个数必须对得上。

pywebview 是按位置把 JS 的参数送进 Python 函数的：多传一个就是
``TypeError: get_bilibili_video_streams() takes from 1 to 3 positional
arguments but 4 were given``，而这个异常在 JS 那边只是一次 promise 拒绝，
界面只知道「取播放地址失败」。

v3.22.0 就是这样：分P 的 ``cid`` 加到了前端和 ``BilibiliBridge``，中间的
``GuiBridge`` 包装漏了，于是**每一个**视频都播不了。名字对得上，参数个数
对不上——这层检查补的就是这个洞。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "gui" / "assets"
BRIDGE = (ROOT / "src" / "gui" / "bridge.py").read_text(encoding="utf-8")


def _python_methods() -> dict[str, dict[str, int | bool]]:
    """``GuiBridge`` 上每个方法能收几个位置参数。"""
    methods: dict[str, dict[str, int | bool]] = {}
    for match in re.finditer(r"^    def (\w+)\(([^)]*)\)", BRIDGE, re.M):
        name = match.group(1)
        params = [part.strip() for part in match.group(2).split(",") if part.strip()]
        # self 不算
        params = [part for part in params if not part.startswith("self")]
        var_positional = any(part.startswith("*") and not part.startswith("**")
                             for part in params)
        var_keyword = any(part.startswith("**") for part in params)
        plain = [part for part in params if not part.startswith("*")]
        methods[name] = {
            "max": len(plain),
            "required": len([part for part in plain if "=" not in part]),
            "var_positional": var_positional,
            "var_keyword": var_keyword,
        }
    return methods


def _strip_strings(text: str) -> str:
    """把字符串里的括号抹掉，免得它们参与括号配对。"""
    text = re.sub(r"'[^'\n]*'", "''", text)
    text = re.sub(r'"[^"\n]*"', '""', text)
    text = re.sub(r"`[^`]*`", "``", text, flags=re.S)
    return text


def _call_arguments(source: str, start: int) -> int:
    """从 ``(`` 之后的位置数出这次调用的顶层参数个数。"""
    depth = 1
    index = start
    commas = 0
    has_content = False
    while index < len(source) and depth > 0:
        char = source[index]
        if char in "([{":
            depth += 1
            has_content = True
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif char == "," and depth == 1:
            commas += 1
        elif not char.isspace():
            has_content = True
        index += 1
    if not has_content:
        return 0
    return commas + 1


def _frontend_calls() -> list[tuple[str, str, int]]:
    """``(文件, 方法名, 参数个数)``。"""
    calls: list[tuple[str, str, int]] = []
    for path in sorted(ASSETS.glob("*.js")):
        source = _strip_strings(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"\bapi\.(\w+)\(", source):
            calls.append((path.name, match.group(1), _call_arguments(source, match.end())))
    return calls


def test_no_call_passes_more_arguments_than_the_bridge_accepts():
    methods = _python_methods()
    problems: list[str] = []

    for filename, name, count in _frontend_calls():
        info = methods.get(name)
        if info is None:
            # 名字不存在由 test_every_bridge_method_the_frontend_calls_exists 管
            continue
        if info["var_positional"]:
            continue
        if count > info["max"]:
            problems.append(f"{filename}: {name}(…) 传了 {count} 个参数，后端只收 {info['max']} 个")

    assert problems == [], "前端多传参数会让整个调用抛 TypeError：\n" + "\n".join(problems)


def test_the_stream_call_carries_the_page_number():
    """分P 的 cid 从播放窗口一路传到后端，中间任何一处漏了都会全片失败。"""
    info = _python_methods()["get_bilibili_video_streams"]

    assert info["max"] == 3, "bvid / quality / cid 三个"
    assert info["required"] <= 1
