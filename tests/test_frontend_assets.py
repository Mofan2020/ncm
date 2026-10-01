"""The frontend/backend contract: ids, i18n keys and bridge method names.

None of this shows up as a Python error at runtime -- a missing key renders a raw
`player.play` on screen and a missing id silently kills a button -- so the three
links are checked here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parent.parent / "src" / "gui" / "assets"
I18N = Path(__file__).resolve().parent.parent / "src" / "i18n"
BRIDGE = Path(__file__).resolve().parent.parent / "src" / "gui" / "bridge.py"
JS = (ASSETS / "app.js").read_text(encoding="utf-8")
HTML = (ASSETS / "index.html").read_text(encoding="utf-8")


def _flat_catalogue(name: str) -> dict[str, str]:
    data = json.loads((I18N / f"{name}.json").read_text(encoding="utf-8"))
    flat: dict[str, str] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(value, path + ".")
            else:
                flat[path] = value

    walk(data)
    return flat


def _used_keys() -> set[str]:
    keys = set(re.findall(r"\bt\(['\"]([A-Za-z0-9_.]+)['\"]", JS))
    keys |= set(re.findall(r'data-i18n(?:-title|-placeholder)?="([^"]+)"', HTML))
    return keys


def _dynamic_prefixes() -> set[str]:
    """``t('player.mode_' + mode)`` -> ``player.mode_``."""
    return set(re.findall(r"\bt\(['\"]([A-Za-z0-9_.]+_)['\"]\s*\+", JS))


def test_every_element_id_the_script_uses_exists():
    ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", JS))

    missing = sorted(i for i in ids if f'id="{i}"' not in HTML)

    assert ids, "the script should look up elements by id"
    assert missing == [], f"app.js refers to missing elements: {missing}"


def test_every_i18n_key_the_frontend_uses_exists():
    catalogue = _flat_catalogue("zh_cn")
    used = _used_keys() | _dynamic_prefixes()
    # A trailing "_" or "." means "built by concatenation", checked per family.
    literal = {key for key in used if not key.endswith(("_", "."))}

    missing = sorted(k for k in literal if k not in catalogue)

    assert missing == [], f"frontend uses undefined i18n keys: {missing}"


def test_dynamic_i18n_families_are_complete():
    """The concatenated keys must exist for every value the code can produce."""
    catalogue = _flat_catalogue("zh_cn")
    families = {
        # `none` has no label: the UI clears the line instead (and an empty
        # translation is rejected by the i18n test), so it is not a key here.
        "lyrics.source_": ["netease", "sidecar", "embedded"],
        "player.mode_": re.findall(r"mode: '([a-z_]+)'", JS),
        "settings.theme_": ["light", "dark", "system"],
        "player.badge_": ["online", "local"],
        "download.status_": re.findall(r"status: '([a-z_]+)'", JS),
        # updateStatus() builds 'status.<key>' at the call site.
        "status.": re.findall(r"updateStatus\('([a-z_]+)'", JS),
    }
    for prefix, values in families.items():
        for value in sorted(set(values)):
            key = prefix + value
            if key not in catalogue:
                pytest.fail(f"missing i18n key {key}")

    assert "error.reason.no_local_copy" in catalogue


def test_every_bridge_method_the_frontend_calls_exists():
    methods = set(re.findall(r"^    def (\w+)", BRIDGE.read_text(encoding="utf-8"), re.M))
    called = set(re.findall(r"state\.api\.(\w+)\(", JS))

    missing = sorted(m for m in called if m not in methods)

    assert called, "the script should call the bridge"
    assert missing == [], f"app.js calls undefined bridge methods: {missing}"


def test_backend_pushes_have_a_matching_frontend_handler():
    """`_call_js('onFoo')` must land on a global `onFoo` in app.js."""
    pushed = set(re.findall(r'_call_js\(\s*["\'](\w+)["\']', BRIDGE.read_text(encoding="utf-8")))

    missing = sorted(name for name in pushed if f"function {name}(" not in JS)

    assert pushed, "the bridge should push at least one event"
    assert missing == [], f"app.js has no handler for: {missing}"
