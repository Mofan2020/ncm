"""Internationalisation (zh_cn / en_us).

Translations are plain JSON files next to this module.  They are loaded through
:func:`src.resources.resource_path` so the same lookup works from source and
from inside a PyInstaller bundle (where ``__file__`` points into the PYZ).

Adding a language = drop ``<code>.json`` into this directory and register the
display name in :data:`LANGUAGE_NAMES`.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.resources import resource_path

__all__ = ["I18n", "get_i18n", "t", "LANGUAGE_NAMES", "flatten_translations"]

#: Language code -> name shown in the settings dropdown.
LANGUAGE_NAMES: dict[str, str] = {
    "zh_cn": "简体中文",
    "en_us": "English",
}

FALLBACK_LANGUAGE = "zh_cn"


def _translations_dir() -> Path:
    """Directory holding the ``*.json`` translation files."""
    for candidate in (resource_path("src", "i18n"), Path(__file__).resolve().parent):
        try:
            if candidate.is_dir() and any(candidate.glob("*.json")):
                return candidate
        except OSError:  # pragma: no cover - defensive
            continue
    return Path(__file__).resolve().parent


def flatten_translations(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten ``{"a": {"b": "c"}}`` into ``{"a.b": "c"}`` for the webview."""
    flat: dict[str, str] = {}
    for key, value in (data or {}).items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_translations(value, full_key))
        else:
            flat[full_key] = value
    return flat


class I18n:
    """Translation registry (singleton)."""

    _instance: I18n | None = None
    _lock = threading.Lock()

    def __new__(cls) -> I18n:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.logger = logging.getLogger("ncm.i18n")
        self._translations: dict[str, dict[str, Any]] = {}
        self._flat_cache: dict[str, dict[str, str]] = {}
        self._current_lang = FALLBACK_LANGUAGE
        self.load()
        self._apply_saved_language()

    # ---------------------------------------------------------------- loading
    def load(self) -> None:
        directory = _translations_dir()
        for path in sorted(directory.glob("*.json")):
            code = path.stem
            try:
                with open(path, encoding="utf-8") as handle:
                    self._translations[code] = json.load(handle)
            except Exception as exc:
                self.logger.warning("cannot load translation %s: %s", path, exc)
        if FALLBACK_LANGUAGE not in self._translations:
            self.logger.error("fallback language %s is missing", FALLBACK_LANGUAGE)
            self._translations.setdefault(FALLBACK_LANGUAGE, {})
        self._flat_cache.clear()

    def _apply_saved_language(self) -> None:
        language = get_settings().ui.language
        if language in self._translations:
            self._current_lang = language
        elif self._translations:
            self._current_lang = FALLBACK_LANGUAGE

    # -------------------------------------------------------------- accessors
    @property
    def current_language(self) -> str:
        return self._current_lang

    @property
    def available_languages(self) -> dict[str, str]:
        """Loaded languages with their display names."""
        return {code: LANGUAGE_NAMES.get(code, code) for code in sorted(self._translations)}

    @property
    def languages(self) -> list[str]:
        return sorted(self._translations)

    def set_language(self, lang_code: str) -> bool:
        if lang_code not in self._translations:
            return False
        self._current_lang = lang_code
        get_settings().update_ui(language=lang_code)
        return True

    def t(self, key: str, **kwargs: Any) -> str:
        """Translate ``key``, falling back to zh_cn and then to the key itself."""
        translation: Any = None
        for language in (self._current_lang, FALLBACK_LANGUAGE):
            node: Any = self._translations.get(language, {})
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node[part]
            if isinstance(node, str):
                translation = node
                break
        if translation is None:
            return key
        if kwargs:
            try:
                translation = translation.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                pass
        return translation

    def get_all_translations(self, lang_code: str) -> dict[str, Any]:
        return dict(self._translations.get(lang_code, {}))

    def get_flat_translations(self, lang_code: str | None = None) -> dict[str, str]:
        """Flattened ``key -> text`` map handed to the frontend."""
        language = lang_code or self._current_lang
        if language not in self._flat_cache:
            self._flat_cache[language] = flatten_translations(self.get_all_translations(language))
        return dict(self._flat_cache[language])

    def missing_keys(self, lang_code: str) -> list[str]:
        """Keys present in the fallback language but absent from ``lang_code``."""
        reference = set(self.get_flat_translations(FALLBACK_LANGUAGE))
        current = set(self.get_flat_translations(lang_code))
        return sorted(reference - current)


def get_i18n() -> I18n:
    """Return the process-wide translation registry."""
    return I18n()


def t(key: str, **kwargs: Any) -> str:
    """Module level shortcut for :meth:`I18n.t`."""
    return I18n().t(key, **kwargs)
