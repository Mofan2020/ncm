"""Translation catalogue must stay complete and consistent between languages."""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.i18n import FALLBACK_LANGUAGE, LANGUAGE_NAMES, flatten_translations

I18N_DIR = Path(__file__).resolve().parent.parent / "src" / "i18n"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "src" / "gui" / "assets"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

# A translation key that is written out in the front end: a dotted name
# whose parts are all lower case.  `t('download.status_' + state)` is
# not one of these, because the name is only half written down.
KEY_LITERAL = re.compile(r"""['"]([a-z][a-z0-9]*(?:\.[a-z0-9_]+)+)['"]""")

# Names that look like a key but are not one: file names that end in
# `.js` or `.css`, the example host of an input field, and the half of
# a key that is put together at run time (`download.status_` plus the
# state, which is why they end in an underscore).
NOT_A_KEY = re.compile(
    r"^(?:[\w.-]+\.(?:js|css|png|svg|jpg|webp)|api\.bangumi\.lat|[\w.]+_)$"
)


def _load(code: str) -> dict:
    return json.loads((I18N_DIR / f"{code}.json").read_text(encoding="utf-8"))


def _codes() -> list[str]:
    return sorted(path.stem for path in I18N_DIR.glob("*.json"))


def test_languages_registered():
    codes = _codes()
    assert FALLBACK_LANGUAGE in codes
    for code in codes:
        assert code in LANGUAGE_NAMES, f"{code} is missing a display name"


def test_no_dotted_keys_at_the_top_level():
    """键名里的点只能是「层级」，不能是字面量。

    顶层写成 ``"pixiv.page_size": "每页数量"`` 的时候，``flatten_translations``
    摊平后跟嵌套写法长得一模一样，键位对比全过——但运行时 ``t()`` 是顺着
    层级找的，找不到就原样把 ``pixiv.page_size`` 显示到界面上。这类错误
    只能靠「顶层键不许带点」这条来挡。
    """
    for code in _codes():
        stray = sorted(key for key in _load(code) if "." in key)
        assert not stray, f"{code} has dotted top-level keys: {stray}"


def test_no_missing_keys_between_languages():
    reference = set(flatten_translations(_load(FALLBACK_LANGUAGE)))
    for code in _codes():
        missing = reference - set(flatten_translations(_load(code)))
        assert not missing, f"{code} is missing keys: {sorted(missing)}"


def test_no_extra_keys_between_languages():
    reference = set(flatten_translations(_load(FALLBACK_LANGUAGE)))
    for code in _codes():
        extra = set(flatten_translations(_load(code))) - reference
        assert not extra, f"{code} has unexpected keys: {sorted(extra)}"


def test_no_empty_values():
    for code in _codes():
        for key, value in flatten_translations(_load(code)).items():
            assert isinstance(value, str) and value.strip(), f"{code}:{key} is empty"


def test_placeholders_match_across_languages():
    reference = {key: set(PLACEHOLDER.findall(text))
                 for key, text in flatten_translations(_load(FALLBACK_LANGUAGE)).items()}
    for code in _codes():
        for key, text in flatten_translations(_load(code)).items():
            placeholders = set(PLACEHOLDER.findall(text))
            assert placeholders == reference[key], (
                f"{code}:{key} placeholders {placeholders} != {reference[key]}")


def test_status_and_queue_keys_exist_for_every_task_state():
    """The UI renders `download.status_<state>` for each downloader state."""
    states = ("waiting", "resolving", "downloading", "completed", "failed",
              "skipped", "cancelled", "paused")
    for code in _codes():
        flat = flatten_translations(_load(code))
        for state in states:
            assert f"download.status_{state}" in flat, f"{code}: missing state {state}"


def test_failure_reasons_cover_api_codes():
    from src.core.api import PLAY_URL_CODES

    expected = set(PLAY_URL_CODES.values())
    expected |= {"no_url", "no_response", "incomplete", "unknown"}
    for code in _codes():
        flat = flatten_translations(_load(code))
        missing = [name for name in expected if f"error.reason.{name}" not in flat]
        assert not missing, f"{code}: missing failure reasons {missing}"


def test_every_key_the_front_end_uses_exists():
    """前端用到的每一个键，目录里都得有。

    缺键的时候界面不会报错：`t()` 找不到就把键名本身原样显示出来，
    于是确认框里会写着 `settings.update_install_confirm`。这种错只有
    把前端引用的键跟目录对一遍才看得出来。

    键名是拼出来的（`t('download.status_' + state)`）不在这一条里，
    因为那一半不是写死的。
    """
    flat = flatten_translations(_load(FALLBACK_LANGUAGE))
    missing: dict[str, list[str]] = {}
    for path in sorted(ASSETS_DIR.rglob("*")):
        if path.suffix not in (".js", ".html"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in set(KEY_LITERAL.findall(text)):
            if NOT_A_KEY.match(key) or key in flat:
                continue
            missing.setdefault(key, []).append(path.name)
    assert not missing, f"the catalogue is missing keys: {missing}"


def test_i18n_lookup_and_fallback(fresh_i18n):
    i18n = fresh_i18n
    assert i18n.available_languages == {"en_us": "English", "zh_cn": "简体中文"}
    assert i18n.set_language("en_us") is True
    assert i18n.t("common.ok") == "OK"
    assert i18n.t("status.download_complete", success=1, fail=2, skipped=3) == \
        "Done: 1 succeeded, 2 failed, 3 skipped"
    assert i18n.set_language("zh_cn") is True
    assert i18n.t("common.ok") == "确定"
    # unknown keys degrade to the key itself instead of raising
    assert i18n.t("does.not.exist") == "does.not.exist"
    assert i18n.set_language("xx_yy") is False
    assert i18n.missing_keys("en_us") == []


def test_language_persisted_to_settings(fresh_i18n):
    from src.config import get_settings

    fresh_i18n.set_language("en_us")
    assert get_settings().ui.language == "en_us"
