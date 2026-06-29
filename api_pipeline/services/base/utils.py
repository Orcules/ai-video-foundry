"""Utility functions extracted from the monolith.

Verbatim copies from Comp_Videos/video_scene_processor.py lines 490-559.
"""

import json
import re

from api_pipeline.services.base.config import config


def is_valid_voice_id(voice_id: str) -> bool:
    """Check if a voice_id is valid (not empty, not #N/A, etc.).

    Args:
        voice_id: The voice ID to validate.

    Returns:
        True if valid, False otherwise.
    """
    if not voice_id:
        return False

    # Check for common invalid values from spreadsheets
    invalid_values = [
        '#n/a', '#na', 'n/a', 'na', '#ref!', '#error!', '#value!',
        'null', 'none', 'undefined', '-', ''
    ]

    normalized = voice_id.strip().lower()
    return normalized not in invalid_values and len(normalized) > 3


def script_only_for_tts(vo_cell_value: str) -> str:
    """Extract only the spoken script from a VO cell value (strip metadata/timing/tags).

    Ensures TTS never reads aloud [Scene N] tags, JSON, break/pause tags, or timestamp lines.
    """
    if not vo_cell_value or not isinstance(vo_cell_value, str):
        return ""
    text = vo_cell_value.strip()
    if not text:
        return ""
    # If it looks like JSON with a script field, extract it
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("script", "text", "full_script", "vo_text"):
                    if key in data and data[key]:
                        return str(data[key]).strip()
        except (json.JSONDecodeError, TypeError):
            pass
    # Strip SSML/break tags so TTS doesn't read "break time 0.5s" etc. aloud
    text = re.sub(r"<break\s+[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?break\s*>?", " ", text, flags=re.IGNORECASE)
    # Strip any "pause 0.5", "meta pause" or similar
    text = re.sub(r"\bmeta\s*pause\s*[\d.]*\s*s?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpause\s*[\d.]*\s*s\b", " ", text, flags=re.IGNORECASE)
    # Strip [Scene N] and [scene n] tags — but KEEP ElevenLabs v3 Audio Tags like [excited], [whispers], [laughs]
    text = re.sub(r"\[Scene\s*\d+\]\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_validated_voice_id(voice_id: str, default_voice_id: str = None) -> str:
    """Get a validated voice_id, falling back to default if invalid.

    Args:
        voice_id: The voice ID to validate.
        default_voice_id: Default to use if voice_id is invalid.

    Returns:
        Valid voice_id or default.
    """
    if is_valid_voice_id(voice_id):
        return voice_id
    return default_voice_id or config.DEFAULT_VOICE_ID
