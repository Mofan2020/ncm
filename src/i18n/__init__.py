"""Internationalization (i18n) module."""
import os
import json
import threading
from pathlib import Path
from typing import Dict, Optional, Any
from src.config import get_settings


class I18n:
    """Internationalization manager with JSON-based translations."""
    
    _instance: Optional['I18n'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._translations: Dict[str, Dict[str, str]] = {}
        self._current_lang = 'zh_cn'
        self._load_translations()
        self._apply_saved_language()
    
    def _load_translations(self) -> None:
        """Load all translation files."""
        i18n_dir = Path(__file__).parent
        for lang_file in i18n_dir.glob('*.json'):
            lang_code = lang_file.stem
            try:
                with open(lang_file, 'r', encoding='utf-8') as f:
                    self._translations[lang_code] = json.load(f)
            except Exception as e:
                print(f"Failed to load translation {lang_file}: {e}")
        
        # Ensure fallback language exists
        if 'zh_cn' not in self._translations:
            self._translations['zh_cn'] = {}
        if 'en_us' not in self._translations:
            self._translations['en_us'] = {}
    
    def _apply_saved_language(self) -> None:
        """Apply saved language from settings."""
        settings = get_settings()
        lang = settings.ui.language
        if lang in self._translations:
            self._current_lang = lang
    
    @property
    def current_language(self) -> str:
        return self._current_lang
    
    @property
    def available_languages(self) -> Dict[str, str]:
        """Return dict of language_code -> display_name."""
        return {
            'zh_cn': '简体中文',
            'en_us': 'English'
        }
    
    def set_language(self, lang_code: str) -> bool:
        """Set current language."""
        if lang_code in self._translations:
            self._current_lang = lang_code
            settings = get_settings()
            settings.update_ui(language=lang_code)
            return True
        return False
    
    def t(self, key: str, **kwargs) -> str:
        """Translate a key with optional formatting."""
        # Try current language
        translation = self._translations.get(self._current_lang, {}).get(key)
        if translation is None:
            # Fallback to Chinese
            translation = self._translations.get('zh_cn', {}).get(key)
        if translation is None:
            # Fallback to key itself
            translation = key
        
        # Format with kwargs if provided
        if kwargs:
            try:
                translation = translation.format(**kwargs)
            except Exception:
                pass
        return translation
    
    def get_all_translations(self, lang_code: str) -> Dict[str, str]:
        """Get all translations for a language (for frontend)."""
        return self._translations.get(lang_code, {}).copy()


def t(key: str, **kwargs) -> str:
    """Global translation function."""
    return I18n().t(key, **kwargs)


def get_i18n() -> I18n:
    """Get global i18n instance."""
    return I18n()