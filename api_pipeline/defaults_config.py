"""Lazy-load singleton for API request defaults.

Reads api_pipeline/config/api_defaults.json once and exposes get_default()
so Pydantic Field defaults and pipeline code can look up values from config.

Follows the same lazy-load pattern as data_config.py.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "api_defaults.json")
_data: Optional[dict] = None


def _load() -> None:
    global _data
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _data = json.load(f)
        logger.debug(f"Loaded api_defaults.json ({len(_data)} keys)")
    except Exception as e:
        logger.warning(f"Could not load api_defaults.json ({e}), using hardcoded fallbacks")
        _data = {}


def _ensure_loaded() -> dict:
    global _data
    if _data is None:
        _load()
    return _data


def get_default(key: str, fallback=None):
    """Return a config default value, or fallback if missing.

    Safe to call at import time (Pydantic Field defaults evaluate during
    class definition). The JSON file is loaded lazily on first access.
    """
    data = _ensure_loaded()
    return data.get(key, fallback)


def reload():
    """Force re-read of api_defaults.json."""
    global _data
    _data = None
    _load()
