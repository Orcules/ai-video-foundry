"""Input sanitization & normalization layer.

Auto-corrects common input mistakes (typos, casing, aliases) before the
pipeline starts, and returns structured warnings so the caller knows what
was fixed.  Hard validation (422 rejections) lives in models.py; this
module handles soft corrections only.

Zero external dependencies — uses stdlib ``difflib.get_close_matches``.
"""

import difflib
import logging
from typing import Any, Dict, List, Tuple

from api_pipeline.data_config import (
    get_country_ethnicity_map,
    get_all_language_codes,
    get_reverse_language_map,
    get_style_prompts,
)

logger = logging.getLogger(__name__)

# ── Type alias for the warnings list ────────────────────────────────────
Warning = Dict[str, str]  # {field, original, normalized, message}


# ── Country aliases ─────────────────────────────────────────────────────
_COUNTRY_ALIASES: Dict[str, str] = {
    "us": "usa",
    "il": "israel",
    "england": "uk",
    "great britain": "uk",
    "britain": "uk",
    "united states of america": "usa",
    "united states": "usa",
    "united kingdom": "uk",
    "jp": "japan",
    "kr": "south korea",
    "korea": "south korea",
    "de": "germany",
    "fr": "france",
    "es": "spain",
    "it": "italy",
    "br": "brazil",
    "cn": "china",
    "in": "india",
    "tr": "turkey",
    "ru": "russia",
    "pl": "poland",
    "th": "thailand",
    "vn": "vietnam",
    "mx": "mexico",
    "co": "colombia",
    "ar": "argentina",
    "ae": "uae",
    "sa": "saudi arabia",
    "eg": "egypt",
    "ma": "morocco",
    "pt": "portugal",
    "nl": "netherlands",
    "se": "sweden",
    "au": "australia",
    "ca": "canada",       # NOTE: "ca" also means Catalan (ISO 639-1) — here it maps to country Canada
    "no": "norway",       # NOTE: "no" also means Norwegian (ISO 639-1) — here it maps to country Norway
    "ng": "nigeria",
    "gb": "uk",
}

# ── Language aliases ────────────────────────────────────────────────────
# Language normalization order:
#   (1) exact match in data_maps.json language_names (ISO code)
#   (2) reverse_map (language name → code, e.g. "english" → "en")
#   (3) _LANGUAGE_ALIASES below
#   (4) fuzzy match against language names
# Aliases here catch common alternative names not in the reverse map
# (e.g. "farsi" → "fa", "mandarin" → "zh-CN", "brazilian" → "pt-BR").
_LANGUAGE_ALIASES: Dict[str, str] = {
    "chinese": "zh",
    "mandarin": "zh-CN",
    "farsi": "fa",
    "persian": "fa",
    "brazilian portuguese": "pt-BR",
    "brazilian": "pt-BR",
    "simplified chinese": "zh-CN",
    "traditional chinese": "zh-TW",
    "american english": "en-US",
    "british english": "en-GB",
    "australian english": "en-AU",
    "mexican spanish": "es-MX",
    "canadian french": "fr-CA",
    # ISO 639-2/B three-letter codes (common from API integrators)
    "eng": "en",
    "heb": "he",
    "spa": "es",
    "fre": "fr",
    "ger": "de",
    "ita": "it",
    "por": "pt",
    "rus": "ru",
    "jpn": "ja",
    "kor": "ko",
    "ara": "ar",
    "hin": "hi",
    "tur": "tr",
    "pol": "pl",
    "dut": "nl",
    "swe": "sv",
    "nor": "no",
    "dan": "da",
    "fin": "fi",
    "tha": "th",
    "vie": "vi",
    "ind": "id",
    "chi": "zh",
    "zho": "zh",
}

# ── Gender sets ─────────────────────────────────────────────────────────
_FEMALE_SET = {"f", "female", "woman", "w", "girl"}
_MALE_SET = {"m", "male", "man", "boy"}

# ── Image API aliases ──────────────────────────────────────────────────
_IMAGE_API_ALIASES: Dict[str, str] = {
    "flash": "kie-flash",
    "kie flash": "kie-flash",
    "gemini flash": "kie-flash",
    "gemini-flash": "kie-flash",
    "gemini": "google",
}

# ── Video type aliases ──────────────────────────────────────────────────
_VIDEO_TYPE_ALIASES: Dict[str, str] = {
    # Product
    "product": "product video",
    "product video": "product video",
    # Influencer
    "influencer": "influencer",
    "influencer video": "influencer",
    "ugc": "influencer",
    "ugc video": "influencer",
    "ugc-style": "influencer",
    "ugc style video": "influencer",
    "ugc-style video": "influencer",
    # UGC Real
    "ugc-real": "ugc-real",
    "ugc real": "ugc-real",
    "ugc real video": "ugc-real",
    # Personal Brand
    "personal-brand": "personal-brand",
    "personal brand": "personal-brand",
    "personal brand video": "personal-brand",
    "personal-service": "personal-brand",
    "personal service": "personal-brand",
    "personal": "personal-brand",
    "brand": "personal-brand",
    # Custom (chat-built storyboard)
    "custom": "custom",
    "custom video": "custom",
    "storyboard": "custom",
}


def _warn(field: str, original: str, normalized: str) -> Warning:
    return {
        "field": field,
        "original": original,
        "normalized": normalized,
        "message": f"{field} '{original}' auto-corrected to '{normalized}'",
    }


# ── Individual normalizers ──────────────────────────────────────────────

def _normalize_country(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip().lower()
    if not v:
        return value, warnings

    country_map = get_country_ethnicity_map()  # keys are lowercase
    valid_countries = set(country_map.keys())

    # Exact match against known countries
    if v in valid_countries:
        if v != raw:
            warnings.append(_warn("country", raw, v))
        return v, warnings

    # Check alias table
    if v in _COUNTRY_ALIASES:
        resolved = _COUNTRY_ALIASES[v]
        warnings.append(_warn("country", raw, resolved))
        return resolved, warnings

    # Fuzzy match
    matches = difflib.get_close_matches(v, valid_countries, n=1, cutoff=0.6)
    if matches:
        warnings.append(_warn("country", raw, matches[0]))
        return matches[0], warnings

    # Unrecognizable — clear to empty
    warnings.append({
        "field": "country",
        "original": raw,
        "normalized": "(cleared)",
        "message": f"country '{raw}' not recognized — cleared to empty (pipeline will use language-based cultural fallback)",
    })
    return "", warnings


def _normalize_language(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip().lower()
    if not v:
        return value, warnings

    valid_codes = get_all_language_codes()

    # Already a valid code (case-insensitive match for codes like pt-BR)
    # Check exact lowercase first
    if v in valid_codes:
        if v != raw:
            warnings.append(_warn("language", raw, v))
        return v, warnings

    # Check case-preserving (e.g. "pt-BR" matches "pt-BR")
    code_map = {c.lower(): c for c in valid_codes}
    if v in code_map:
        resolved = code_map[v]
        if resolved != raw:
            warnings.append(_warn("language", raw, resolved))
        return resolved, warnings

    # Check reverse name map (e.g. "english" → "en")
    reverse_map = get_reverse_language_map()
    if v in reverse_map:
        resolved = reverse_map[v]
        warnings.append(_warn("language", raw, resolved))
        return resolved, warnings

    # Check language aliases
    if v in _LANGUAGE_ALIASES:
        resolved = _LANGUAGE_ALIASES[v]
        warnings.append(_warn("language", raw, resolved))
        return resolved, warnings

    # Fuzzy match against language names
    name_list = list(reverse_map.keys())
    matches = difflib.get_close_matches(v, name_list, n=1, cutoff=0.6)
    if matches:
        resolved = reverse_map[matches[0]]
        warnings.append(_warn("language", raw, resolved))
        return resolved, warnings

    # No match — leave as-is (pipeline has its own fallbacks)
    return value, warnings


def _normalize_style(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip()
    if not v:
        return value, warnings

    style_map = get_style_prompts()
    valid_styles = list(style_map.keys()) + ["Auto"]

    # Case-insensitive exact match
    lower_map = {s.lower(): s for s in valid_styles}
    vl = v.lower()
    if vl in lower_map:
        resolved = lower_map[vl]
        if resolved != raw:
            warnings.append(_warn("style", raw, resolved))
        return resolved, warnings

    # Substring match — if lowered input is a substring of exactly one style
    substring_matches = [s for s in valid_styles if vl in s.lower()]
    if len(substring_matches) == 1:
        resolved = substring_matches[0]
        warnings.append(_warn("style", raw, resolved))
        return resolved, warnings

    # Fuzzy match against style names
    matches = difflib.get_close_matches(vl, [s.lower() for s in valid_styles], n=1, cutoff=0.6)
    if matches:
        resolved = lower_map[matches[0]]
        warnings.append(_warn("style", raw, resolved))
        return resolved, warnings

    # No match — leave as-is (pipeline handles unknown styles gracefully)
    return value, warnings


def _normalize_gender(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip().lower()
    if not v:
        return "f", warnings

    if v in _FEMALE_SET:
        if raw != "f":
            warnings.append(_warn("gender", raw, "f"))
        return "f", warnings

    if v in _MALE_SET:
        if raw != "m":
            warnings.append(_warn("gender", raw, "m"))
        return "m", warnings

    # Unknown — default to "f"
    warnings.append({
        "field": "gender",
        "original": raw,
        "normalized": "f",
        "message": f"gender '{raw}' not recognized — defaulting to 'f'",
    })
    return "f", warnings


_ANIMATION_MODEL_ALIASES: Dict[str, str] = {
    "veo": "google", "veo3": "google", "veo 3": "google", "veo3.1": "google",
    "google31": "google",
    "kling2.5": "kling", "kling 2.5": "kling", "kling-2.5": "kling",
    "kling2.6": "kling", "kling 2.6": "kling", "kling-2.6": "kling",
}


def _normalize_animation_model(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip().lower()
    if not v:
        return value, warnings

    valid = {"auto", "google", "kling", "runway", "none"}
    if v in valid:
        if v != raw:
            warnings.append(_warn("animation_model", raw, v))
        return v, warnings

    # Check aliases
    if v in _ANIMATION_MODEL_ALIASES:
        resolved = _ANIMATION_MODEL_ALIASES[v]
        warnings.append(_warn("animation_model", raw, resolved))
        return resolved, warnings

    # Fuzzy match
    matches = difflib.get_close_matches(v, list(valid) + list(_ANIMATION_MODEL_ALIASES.keys()), n=1, cutoff=0.6)
    if matches:
        resolved = _ANIMATION_MODEL_ALIASES.get(matches[0], matches[0])
        if resolved in valid:
            warnings.append(_warn("animation_model", raw, resolved))
            return resolved, warnings

    # No match — leave as-is (models.py validator will reject)
    return value, warnings


def _normalize_image_api(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip().lower()
    if not v:
        return value, warnings

    v = _IMAGE_API_ALIASES.get(v, v)
    valid = {"google", "google-31-flash", "kie-flash", "kie", "nano-banana-2", "gemini-25-flash-image"}
    if v in valid:
        if v != raw:
            warnings.append(_warn("image_api", raw, v))
        return v, warnings

    # No match — leave as-is (models.py validator will reject)
    return value, warnings


_OUTPUT_RESOLUTION_ALIASES: Dict[str, str] = {
    "720p": "720p_low",
    "720": "720p_low",
    "1080p": "1080p_low",
    "1080": "1080p_low",
    "4k": "4k_low",
}


def _normalize_output_resolution(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip().lower()
    if not v:
        return value, warnings

    valid = {"720p_low", "720p_high", "1080p_low", "1080p_high", "4k_low", "4k_high"}
    if v in valid:
        if v != raw:
            warnings.append(_warn("output_resolution", raw, v))
        return v, warnings

    # Check aliases
    if v in _OUTPUT_RESOLUTION_ALIASES:
        resolved = _OUTPUT_RESOLUTION_ALIASES[v]
        warnings.append(_warn("output_resolution", raw, resolved))
        return resolved, warnings

    # No match — leave as-is (models.py validator will reject)
    return value, warnings


def _normalize_video_type(value: str) -> Tuple[str, List[Warning]]:
    warnings: List[Warning] = []
    raw = value
    v = value.strip().lower()
    if not v:
        return value, warnings

    if v in _VIDEO_TYPE_ALIASES:
        resolved = _VIDEO_TYPE_ALIASES[v]
        if resolved != raw.strip():
            warnings.append(_warn("video_type", raw, resolved))
        return resolved, warnings

    # No match — leave as-is (server.py still rejects 400)
    return value, warnings


# ── Public API ──────────────────────────────────────────────────────────

def normalize_inputs(params: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Warning]]:
    """Normalize and auto-correct common input mistakes.

    Returns:
        (params, warnings) — params dict with corrected values,
        plus a list of warning dicts describing what was changed.
    """
    all_warnings: List[Warning] = []

    if "country" in params:
        params["country"], w = _normalize_country(params["country"])
        all_warnings.extend(w)

    if "language" in params:
        params["language"], w = _normalize_language(params["language"])
        all_warnings.extend(w)

    if "style" in params:
        params["style"], w = _normalize_style(params["style"])
        all_warnings.extend(w)

    if "gender" in params:
        params["gender"], w = _normalize_gender(params["gender"])
        all_warnings.extend(w)

    if "video_type" in params:
        params["video_type"], w = _normalize_video_type(params["video_type"])
        all_warnings.extend(w)

    if "animation_model" in params:
        params["animation_model"], w = _normalize_animation_model(params["animation_model"])
        all_warnings.extend(w)

    if "image_api" in params:
        params["image_api"], w = _normalize_image_api(params["image_api"])
        all_warnings.extend(w)

    if "output_resolution" in params:
        params["output_resolution"], w = _normalize_output_resolution(params["output_resolution"])
        all_warnings.extend(w)

    if all_warnings:
        logger.info(f"Input normalization: {len(all_warnings)} correction(s) applied")

    return params, all_warnings
