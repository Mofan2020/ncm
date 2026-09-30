"""Translation catalogue must stay complete and consistent between languages."""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.i18n import FALLBACK_LANGUAGE, LANGUAGE_NAMES, flatten_translations

I18N_DIR = Path(__file__).resolve().parent.parent / "src" / "i18n"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _load(code: str) -> dict:
    return json.loads((I18N_DIR / f"{code}.json").read_text(encoding="utf-8"))


def _codes() -> list[str]:
    return sorted(path.stem for path in I18N_DIR.glob("*.json"))


def test_languages_registered():
    codes = _codes()
    assert FALLBACK_LANGUAGE in codes
    for code in codes:
        assert code in LANGUAGE_NAMES, f"{code} is missing a display name"


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
