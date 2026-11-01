"""The frozen build has to contain every module the app loads by name.

PyInstaller only ships what it can see in the source.  The Bilibili helper
modules are loaded with ``importlib.import_module("src.core.<name>")``, so
the static analysis misses them and the packaged app loses the danmaku, the
private messages and the upload -- while the source tree works fine.

The test reads the module list out of the bridge and the list out of
``main.spec`` and compares the two.
"""
from __future__ import annotations

import re
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / "main.spec"


def _helper_modules() -> set[str]:
    """The modules the bridge loads by name."""
    from src.gui.bilibili_bridge import _HELPER_MODULES

    return {f"src.core.{name}" for name in _HELPER_MODULES.values()}


def _spec_hidden_imports() -> set[str]:
    """The hidden imports of the PyInstaller spec."""
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(r"hiddenimports\s*=\s*\[(.*?)\]", text, re.S)
    assert match, "main.spec has no hiddenimports list"
    return set(re.findall(r"[\"']([A-Za-z0-9_.]+)[\"']", match.group(1)))


def test_the_spec_ships_every_module_the_bridge_loads_by_name():
    """A helper module that is not in the spec is missing from the bundle."""
    missing = sorted(_helper_modules() - _spec_hidden_imports())

    assert missing == [], (
        f"main.spec does not ship {missing}: the packaged app would answer "
        "\"模块不可用\" for them"
    )


def test_the_spec_ships_the_helper_modules_by_their_full_name():
    """The name in the spec has to be the importable one."""
    shipped = _spec_hidden_imports()

    for module in _helper_modules():
        assert module in shipped
