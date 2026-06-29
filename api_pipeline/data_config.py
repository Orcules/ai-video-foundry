"""Configurable data-map loader.

Reads api_pipeline/config/data_maps.json once and exposes accessor functions
for language names, country/ethnicity maps, cultural mappings, brand/talking
replacement patterns, and style prompts.

Follows the same lazy-load singleton pattern as model_config.py.
"""

import json
import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "data_maps.json")
_data: Optional[dict] = None


def _load() -> None:
    global _data
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _data = json.load(f)
        logger.debug(f"Loaded data_maps.json ({len(_data.get('language_names', {}))} languages)")
    except Exception as e:
        logger.warning(f"Could not load data_maps.json ({e}), falling back to empty dicts")
        _data = {}


def _ensure_loaded() -> dict:
    global _data
    if _data is None:
        _load()
    return _data


def get_language_name(code: str, fallback: str = "English") -> str:
    """Return the human-readable language name for a language code.

    Args:
        code: ISO 639-1 code (e.g. "he", "pt-BR").
        fallback: Value to return if the code is unknown.
                  vo.py passes the raw code itself as fallback.
    """
    data = _ensure_loaded()
    lang_map = data.get("language_names", {})
    return lang_map.get(code, fallback)


def get_country_ethnicity_map() -> Dict[str, str]:
    """Return the country-to-ethnicity description map (keys are lowercase)."""
    data = _ensure_loaded()
    m = data.get("country_ethnicity_map", {})
    return {k: v for k, v in m.items() if k != "_comment"}


def get_cultural_mapping() -> Dict[str, Dict[str, str]]:
    """Return the language-code-to-cultural-info fallback map."""
    data = _ensure_loaded()
    m = data.get("cultural_mapping", {})
    return {k: v for k, v in m.items() if k != "_comment"}


def get_brand_replacements() -> Dict[str, str]:
    """Return brand-name regex patterns and their replacements."""
    data = _ensure_loaded()
    m = data.get("brand_replacements", {})
    return {k: v for k, v in m.items() if k != "_comment"}


def get_talking_replacements() -> Dict[str, str]:
    """Return talking/phone regex patterns and their replacements."""
    data = _ensure_loaded()
    m = data.get("talking_replacements", {})
    return {k: v for k, v in m.items() if k != "_comment"}


def get_style_prompts() -> Dict[str, Tuple[str, str]]:
    """Return style prompt prefix/suffix pairs keyed by style name.

    Returns:
        Dict mapping style name to (prefix, suffix) tuple.
    """
    data = _ensure_loaded()
    raw = data.get("style_prompts", {})
    result = {}
    for k, v in raw.items():
        if k == "_comment":
            continue
        if isinstance(v, dict):
            result[k] = (v.get("prefix", ""), v.get("suffix", ""))
        elif isinstance(v, list) and len(v) == 2:
            result[k] = (v[0], v[1])
    return result


def get_speech_rate(lang_code: str, default: float = 2.5) -> float:
    """Return the words-per-second speech rate for a language code.

    Used to calculate max_narration_words = target_duration * rate.
    """
    data = _ensure_loaded()
    rates = data.get("speech_rates_wps", {})
    return rates.get(lang_code, rates.get("_default", default))


def get_all_language_codes() -> set:
    """All known ISO language codes from data_maps."""
    data = _ensure_loaded()
    return set(k for k in data.get("language_names", {}) if k != "_comment")


def get_reverse_language_map() -> dict:
    """Lowercased language-name-to-code map. E.g. {'english': 'en', 'hebrew': 'he'}"""
    data = _ensure_loaded()
    return {v.lower(): k for k, v in data.get("language_names", {}).items() if k != "_comment"}


def reload():
    """Force re-read of data_maps.json (e.g. after editing at runtime)."""
    global _data
    _data = None
    _load()
