"""Configurable text + media model loader.

Reads the monolith's tvd_pipeline/config/models.json once and exposes
get_text_model() and get_media_model() so every pipeline step can look up
which model to use.
"""

import json
import logging
import os
from typing import Optional

from api_pipeline.model_mappings_config import get_text_fallback_model, get_text_fallback_provider

logger = logging.getLogger(__name__)

# Search order: Docker volume mount, then local dev relative path.
_CANDIDATE_PATHS = [
    os.path.join("/app", "tvd_pipeline", "config", "models.json"),                          # Docker
    os.path.join(os.path.dirname(__file__), "..", "Comp_Videos", "tvd_pipeline", "config", "models.json"),  # local dev
]

_text_models: Optional[dict] = None
_media_models: Optional[dict] = None
_animation_models: Optional[dict] = None


def _resolve_config_path() -> Optional[str]:
    for p in _CANDIDATE_PATHS:
        norm = os.path.normpath(p)
        if os.path.isfile(norm):
            return norm
    return None


def _load() -> None:
    global _text_models, _media_models, _animation_models
    path = _resolve_config_path()
    if not path:
        logger.warning("Could not find tvd_pipeline/config/models.json in any search path, falling back to defaults")
        _text_models = {}
        _media_models = {}
        _animation_models = {}
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _text_models = data.get("text_defaults", {})
        _media_models = data.get("media_models", {})
        _animation_models = data.get("animation_models", {})
        logger.debug(f"Loaded {len(_text_models)} text + {len(_media_models)} media model entries from {path}")
    except Exception as e:
        logger.warning(f"Could not load models.json ({e}), falling back to defaults")
        _text_models = {}
        _media_models = {}
        _animation_models = {}


def get_text_model(step_key: str, fallback: str = None) -> str:
    """Return the model name for a pipeline step key.

    Args:
        step_key: Key matching an entry in models.json text_defaults
                  (e.g. "parse_prompt", "image_quality_check").
        fallback: Value to return if the key is missing. If None, returns
                  "gemini-2.5-flash" as a safe default.

    Returns:
        Model name string (e.g. "gemini-2.5-flash", "gpt-4o").
    """
    global _text_models
    if _text_models is None:
        _load()
    entry = _text_models.get(step_key)
    if entry and isinstance(entry, dict):
        return entry.get("model", fallback or get_text_fallback_model())
    return fallback or get_text_fallback_model()


def get_text_provider(step_key: str, fallback: str = None) -> str:
    """Return the provider for a pipeline step key.

    Args:
        step_key: Key matching an entry in models.json text_defaults.
        fallback: Value to return if the key is missing. If None, uses
                  the text_model_fallbacks.provider from model_mappings.json.

    Returns:
        Provider string (e.g. "gemini", "openai").
    """
    if fallback is None:
        fallback = get_text_fallback_provider()
    global _text_models
    if _text_models is None:
        _load()
    entry = _text_models.get(step_key)
    if entry and isinstance(entry, dict):
        return entry.get("provider", fallback)
    return fallback


def get_media_model_config(step_key: str) -> Optional[dict]:
    """Return the full resolved config dict for a media model step.

    For version-aware entries (with ``selected`` + ``versions``), resolves
    the selected version and returns the version config dict (containing
    ``model``, and optionally ``image_field``, ``extra_params``, etc.).

    For simple entries (just ``{ "provider", "model" }``), returns a dict
    with at least a ``"model"`` key for backward compatibility.

    Returns:
        Dict with at least ``{"model": "..."}`` or None if the key is missing.
    """
    global _media_models
    if _media_models is None:
        _load()
    entry = _media_models.get(step_key)
    if not entry or not isinstance(entry, dict):
        return None

    # Version-aware format: { "selected": "...", "versions": { ... } }
    if "versions" in entry and "selected" in entry:
        selected = entry["selected"]
        version_config = entry["versions"].get(selected)
        if version_config:
            return dict(version_config)  # return a copy
        # If selected version not found, fall back to first available
        logger.warning(f"Selected version '{selected}' not found for {step_key}, using first available")
        first = next(iter(entry["versions"].values()), None)
        return dict(first) if first else None

    # Simple format: { "provider": "...", "model": "..." }
    if "model" in entry:
        return {"model": entry["model"]}

    return None


def get_media_model(step_key: str, fallback: str = None) -> str:
    """Return the model name for a media step key.

    Args:
        step_key: Key matching an entry in models.json media_models
                  (e.g. "nano_banana_image", "elevenlabs_tts").
        fallback: Value to return if the key is missing. If None, returns
                  None (caller should use its own default).

    Returns:
        Model name string, or fallback if the key is missing.
    """
    config = get_media_model_config(step_key)
    if config:
        return config.get("model", fallback)
    return fallback


def get_animation_models() -> dict:
    """Return animation_models config with labels enriched by current media_model versions."""
    global _animation_models, _media_models
    if _animation_models is None:
        _load()
    result = {}
    for pipeline, entries in _animation_models.items():
        if pipeline == "_comment":
            continue
        enriched = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            e = dict(entry)  # copy so we don't mutate the cached config
            media_key = e.pop("media_key", None)
            if media_key and _media_models:
                media_entry = _media_models.get(media_key)
                if media_entry and isinstance(media_entry, dict) and "selected" in media_entry:
                    e["label"] = f"{e['label']} ({media_entry['selected']})"
            enriched.append(e)
        result[pipeline] = enriched
    return result


def get_allowed_animation_values(pipeline: str) -> tuple:
    """Return tuple of allowed animation_model values for a pipeline."""
    models = get_animation_models()
    entries = models.get(pipeline, [])
    return tuple(e["value"] for e in entries if isinstance(e, dict))


def reload():
    """Force re-read of models.json (e.g. after editing the file at runtime)."""
    global _text_models, _media_models, _animation_models
    _text_models = None
    _media_models = None
    _animation_models = None
    _load()
