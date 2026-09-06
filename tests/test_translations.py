import json
from pathlib import Path
import pytest
from translations import t, resolve_user_locale, normalize_locale, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, LOCALES_DIR


def test_supported_languages():
    assert "en" in SUPPORTED_LANGUAGES
    assert "cs" in SUPPORTED_LANGUAGES
    assert "sk" in SUPPORTED_LANGUAGES
    assert "de" in SUPPORTED_LANGUAGES
    assert "fr" in SUPPORTED_LANGUAGES
    assert "es" in SUPPORTED_LANGUAGES
    assert "pt" in SUPPORTED_LANGUAGES


def test_translations_basic():
    assert t("cs", "btn_suggest") == "Navrhnout otázku"
    assert t("en", "btn_suggest") == "Suggest Question"
    assert t("de", "btn_suggest") == "Frage vorschlagen"


def test_translations_interpolation():
    assert t("en", "ping_response", latency="45ms") == "Pong! 45ms"
    assert t("cs", "ping_response", latency="45ms") == "Pong! 45ms"


def test_translations_fallback():
    # Non-existent language falls back to default 'en'
    assert t("xx", "btn_suggest") == "Suggest Question"
    # Unknown key returns key itself
    assert t("en", "non_existent_key_12345") == "non_existent_key_12345"


def test_resolve_user_locale():
    assert resolve_user_locale("cs") == "cs"
    assert resolve_user_locale("en-US") == "en"
    assert resolve_user_locale("de-DE") == "de"
    assert resolve_user_locale("invalid-locale") is None
    assert resolve_user_locale(None) is None


def test_locales_json_files():
    for code in SUPPORTED_LANGUAGES:
        file_path = LOCALES_DIR / f"{code}.json"
        assert file_path.exists(), f"Missing locale file {file_path}"
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert isinstance(data, dict)
            assert len(data) > 0, f"Empty locale file for {code}"
