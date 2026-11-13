"""设置页里「勾选框那一行」的布局守卫。

用户报的原话：「文本跑右边去了，复选框之类的全去左边了」。

根因是一条跟着勾选框走的样式：为了让「说明在左、勾选框在右」的那几行横排
起来，给 ``.option:has(> input[type="checkbox"])`` 加了
``justify-content: space-between``。可设置页里还有另一种行——``.option.checkbox``，
它是勾选框在前、说明在后，本来就该「框在左、字紧跟着」。同一条 space-between
套上去，框被顶到最左、字被推到最右，整页看下来就是文字全在右边、框全在左边。

这个文件钉两件事：
1. ``:has()`` 那条只认「勾选框排在最后」的行。
2. 任何规则都不许把 ``.option.checkbox`` 拆成两端对齐。
"""
from __future__ import annotations

import re
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "src" / "gui" / "assets"
CSS = (ASSETS / "style.css").read_text(encoding="utf-8")

#: 一条规则：选择器 + 花括号里的声明。
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)


def _rules() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _RULE.finditer(CSS):
        selector = " ".join(match.group(1).split())
        if selector.startswith("/*"):
            end = selector.rfind("*/")
            if end >= 0:
                selector = " ".join(selector[end + 2:].split())
        if selector:
            out.append((selector, match.group(2)))
    return out


def _checkbox_row_rules() -> list[tuple[str, str]]:
    return [(s, b) for s, b in _rules()
            if "option" in s and ":has(" in s and "checkbox" in s]


def test_the_row_treatment_only_touches_rows_whose_box_comes_last() -> None:
    rules = _checkbox_row_rules()
    assert rules, "那条让勾选框行横排的规则不见了——设置页会退回竖排"
    for selector, _body in rules:
        assert ':last-child' in selector, (
            f"{selector} 会连 .option.checkbox（框在前、字在后）一起改成两端对齐，"
            "文字会被推到最右边"
        )


def test_a_checkbox_first_row_never_gets_justified_apart() -> None:
    """框在前的行，只要出现 space-between，字就飞到右边去了。"""
    guilty = [selector for selector, body in _rules()
              if "space-between" in body
              and "option" in selector
              and ":last-child" not in selector]
    assert guilty == [], f"这些规则把勾选框和它的说明推到了两端：{guilty}"


def test_the_checkbox_itself_keeps_its_size() -> None:
    """勾选框被拉成一条的不算少：设了 width 又设 flex: 1 就没了。"""
    for selector, body in _rules():
        if "option" not in selector or "checkbox" not in selector:
            continue
        if re.search(r"flex\s*:\s*1\b", body):
            raise AssertionError(f"{selector} 会把勾选框拉长")
