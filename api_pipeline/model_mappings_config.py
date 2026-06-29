"""Lazy-load singleton for model alias mappings.

Reads api_pipeline/config/model_mappings.json once and exposes typed
accessors for animation model maps, image API maps, and cost tracker
fallback model names.

Follows the same lazy-load pattern as data_config.py.
"""

import json
import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "model_mappings.json")
_data: Optional[dict] = None


def _load() -> None:
    global _data
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _data = json.load(f)
        logger.debug(f"Loaded model_mappings.json")
    except Exception as e:
        logger.warning(f"Could not load model_mappings.json ({e}), using hardcoded fallbacks")
        _data = {}


def _ensure_loaded() -> dict:
    global _data
    if _data is None:
        _load()
    return _data


def get_animation_model_map() -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Return animation_model alias → (video_model, video_provider) mapping.

    Tuple format matches the existing _ANIMATION_MODEL_MAP usage.
    """
    data = _ensure_loaded()
    raw = data.get("animation_model_map", {})
    result = {}
    for alias, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        result[alias] = (entry.get("video_model"), entry.get("video_provider"))
    return result


def get_image_api_map() -> Dict[str, Tuple[str, str]]:
    """Return image_api alias → (image_model, image_provider) mapping."""
    data = _ensure_loaded()
    raw = data.get("image_api_map", {})
    result = {}
    for alias, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        result[alias] = (entry.get("image_model"), entry.get("image_provider"))
    return result


def get_cost_fallback(step_key: str, fallback: str) -> str:
    """Return the fallback model name for a cost tracker step key."""
    data = _ensure_loaded()
    return data.get("cost_tracker_fallbacks", {}).get(step_key, fallback)


def get_text_fallback_model() -> str:
    """Return the fallback text model name (e.g. 'gemini-2.5-flash')."""
    data = _ensure_loaded()
    return data.get("text_model_fallbacks", {}).get("model", "gemini-2.5-flash")


def get_text_fallback_provider() -> str:
    """Return the fallback text provider name (e.g. 'gemini')."""
    data = _ensure_loaded()
    return data.get("text_model_fallbacks", {}).get("provider", "gemini")


def reload():
    """Force re-read of model_mappings.json."""
    global _data
    _data = None
    _load()
