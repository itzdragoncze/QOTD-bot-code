import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("translations")

LOCALES_DIR = Path(__file__).parent / "locales"

DEFAULT_LANGUAGE: str = "en"

# Load supported languages
_lang_file = LOCALES_DIR / "languages.json"
if _lang_file.exists():
    try:
        with open(_lang_file, "r", encoding="utf-8") as f:
            SUPPORTED_LANGUAGES: Dict[str, str] = json.load(f)
    except Exception as err:
        logger.error("Failed to load languages.json: %s", err)
        SUPPORTED_LANGUAGES = {"en": "English 🇬🇧"}
else:
    SUPPORTED_LANGUAGES = {"en": "English 🇬🇧"}

# Load default initial questions per language
_dq_file = LOCALES_DIR / "default_questions.json"
if _dq_file.exists():
    try:
        with open(_dq_file, "r", encoding="utf-8") as f:
            DEFAULT_QUESTIONS: Dict[str, List[str]] = json.load(f)
    except Exception as err:
        logger.error("Failed to load default_questions.json: %s", err)
        DEFAULT_QUESTIONS = {"en": []}
else:
    DEFAULT_QUESTIONS = {"en": []}

# Load translation dictionaries for each supported language
STRINGS: Dict[str, Dict[str, str]] = {}
for code in SUPPORTED_LANGUAGES:
    locale_file = LOCALES_DIR / f"{code}.json"
    if locale_file.exists():
        try:
            with open(locale_file, "r", encoding="utf-8") as f:
                STRINGS[code] = json.load(f)
        except Exception as err:
            logger.error("Failed to load %s.json: %s", code, err)
            STRINGS[code] = {}
    else:
        STRINGS[code] = {}


def t(lang: Optional[str], key: str, **kwargs: Any) -> str:
    """Translates a key into the specified language, falling back to English."""
    lang_code = (lang or DEFAULT_LANGUAGE).lower()
    if lang_code not in STRINGS:
        lang_code = DEFAULT_LANGUAGE

    template = STRINGS.get(lang_code, {}).get(key)
    if template is None:
        template = STRINGS.get(DEFAULT_LANGUAGE, {}).get(key, key)

    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError, IndexError):
            return template
    return template


def normalize_locale(locale: Optional[str]) -> str:
    """Normalizes an arbitrary locale string to a supported 2-letter language code."""
    if not locale:
        return DEFAULT_LANGUAGE
    loc = str(locale).lower().replace("_", "-")
    lang_code = loc.split("-")[0]
    if lang_code in SUPPORTED_LANGUAGES:
        return lang_code
    return DEFAULT_LANGUAGE


def resolve_user_locale(locale: Any) -> Optional[str]:
    """Resolves a Discord client locale to a supported language code."""
    if not locale:
        return None
    if hasattr(locale, "locale") and locale.locale:
        locale = locale.locale
    val = getattr(locale, "value", locale)
    if not isinstance(val, str):
        val = str(val)
    if not val:
        return None
    loc = val.lower().replace("_", "-")
    lang_code = loc.split("-")[0]
    if lang_code in SUPPORTED_LANGUAGES:
        return lang_code
    return None
