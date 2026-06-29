"""Centralized loader for data_maps.json and arc templates — provides language
names, cultural data, brand/talking replacements, style prompts, speech rates,
text translations, and narrative arc beat maps.

All data is loaded once from ``tvd_pipeline/config/data_maps.json`` and cached in
module-level globals so repeated calls are free.
"""

import json
import os
import re as _re
import logging

logger = logging.getLogger(__name__)

_DATA_MAPS = None
_MODELS_CONFIG = None
_SUPPORTED_DURATIONS_CACHE = None
_ELEVENLABS_CONFIG = None
_VEO3_CONFIG = None
_PROVIDER_RATE_LIMITS = None
_KIE_CONFIG = None
_SUNO_CONFIG = None
_ZAPCAP_CONFIG = None
_FAL_CONFIG = None


def get_data_maps() -> dict:
    """Return the full data_maps dict (cached after first load)."""
    global _DATA_MAPS
    if _DATA_MAPS is None:
        path = os.path.join(os.path.dirname(__file__), "config", "data_maps.json")
        with open(path, encoding="utf-8") as f:
            _DATA_MAPS = json.load(f)
        logger.debug("Loaded data_maps.json (%d top-level keys)", len(_DATA_MAPS))
    return _DATA_MAPS


def get_models_config() -> dict:
    """Return the full models.json dict (cached after first load)."""
    global _MODELS_CONFIG
    if _MODELS_CONFIG is None:
        path = os.path.join(os.path.dirname(__file__), "config", "models.json")
        with open(path, encoding="utf-8") as f:
            _MODELS_CONFIG = json.load(f)
        logger.debug("Loaded models.json (%d top-level keys)", len(_MODELS_CONFIG))
    return _MODELS_CONFIG


def get_supported_durations() -> dict:
    """Build {version_name: [durations]} from models.json media_models."""
    global _SUPPORTED_DURATIONS_CACHE
    if _SUPPORTED_DURATIONS_CACHE is None:
        cfg = get_models_config().get("media_models", {})
        result = {}
        for group in cfg.values():
            if not isinstance(group, dict):
                continue
            for version_name, version_cfg in group.get("versions", {}).items():
                durations = version_cfg.get("supported_durations")
                if durations:
                    result[version_name] = durations
        _SUPPORTED_DURATIONS_CACHE = result
    return _SUPPORTED_DURATIONS_CACHE


def get_language_names() -> dict:
    """Return the language_names mapping (language code -> full name)."""
    return get_data_maps()["language_names"]


def get_language_name(code: str, default: str = "English") -> str:
    """Return the full language name for *code*, falling back to *default*."""
    return get_language_names().get(code, default)


def get_region_mapping() -> dict:
    """Return the region_mapping dict (language code -> region key)."""
    return get_data_maps()["region_mapping"]


def get_cultural_styles() -> dict:
    """Return the cultural_styles dict (region -> style info)."""
    return get_data_maps()["cultural_styles"]


def get_hook_styles() -> dict:
    """Return the hook_styles dict (region -> hook description)."""
    return get_data_maps()["hook_styles"]


def get_brand_replacements() -> dict:
    """Return the brand_replacements dict (regex pattern -> replacement text)."""
    return get_data_maps()["brand_replacements"]


def get_talking_replacements() -> dict:
    """Return the talking_replacements dict (regex pattern -> replacement text)."""
    return get_data_maps()["talking_replacements"]


def get_style_prompts(variant: str = "gemini_image") -> dict:
    """Return the style_prompts dict for *variant* ('gemini_image', 'kie', or 'scene_config').

    Each key is a style name; for gemini_image/kie the value is [prefix, suffix].
    For scene_config the value is {prefix, forbidden, instruction}.
    """
    return get_data_maps()["style_prompts"][variant]


def get_speech_rate(language: str, default: float = 2.5) -> float:
    """Return the words-per-second rate for *language*, falling back to *default*."""
    return get_data_maps()["speech_rates_wps"].get(language, default)


def get_text_translations() -> dict:
    """Return the text_translations dict used for brand replacement CTA text."""
    return get_data_maps()["text_translations"]


def get_context_detection_keywords() -> dict:
    """Return the context_detection_keywords dict."""
    return get_data_maps()["context_detection_keywords"]


def get_elevenlabs_config() -> dict:
    """Return the ElevenLabs config dict (cached after first load).

    Loaded from ``tvd_pipeline/config/11_labs.json``.  Contains TTS/STS
    model IDs and voice_settings presets (normal, expressive).
    """
    global _ELEVENLABS_CONFIG
    if _ELEVENLABS_CONFIG is None:
        path = os.path.join(os.path.dirname(__file__), "config", "11_labs.json")
        with open(path, encoding="utf-8") as f:
            _ELEVENLABS_CONFIG = json.load(f)
        logger.debug("Loaded 11_labs.json")
    return _ELEVENLABS_CONFIG


def get_language_voice(language: str, gender: str) -> str | None:
    """Return an ElevenLabs voice ID for TTS.

    Uses ``language_voices`` in ``11_labs.json``; falls back to
    ``default_voices`` (multilingual premade) when the language has no entry.
    *gender* should be ``"male"`` or ``"female"``.
    """
    cfg = get_elevenlabs_config()
    voices = cfg.get("language_voices", {})
    lang = (language or "en").strip()
    lang_entry = voices.get(lang)
    if not lang_entry and "-" in lang:
        lang_entry = voices.get(lang.split("-")[0].strip())
    g = "male" if str(gender).lower().startswith("m") else "female"
    if lang_entry:
        vid = lang_entry.get(g) or lang_entry.get("female") or lang_entry.get("male")
        if vid:
            return vid
    dv = cfg.get("default_voices") or {}
    vid = dv.get(g) or dv.get("female") or dv.get("male")
    if vid:
        return vid
    # Last-resort premade IDs (Rachel / Adam)
    return "pNInz6obpgDQGcFmaJgB" if g == "male" else "21m00Tcm4TlvDq8ikWAM"


def get_veo3_config() -> dict:
    """Return the Veo 3 config dict (cached after first load).

    Loaded from ``tvd_pipeline/config/veo3.json``.  Contains API parameters
    for Veo 3.0 (REST) and Veo 3.1 (google-genai SDK).
    """
    global _VEO3_CONFIG
    if _VEO3_CONFIG is None:
        path = os.path.join(os.path.dirname(__file__), "config", "veo3.json")
        with open(path, encoding="utf-8") as f:
            _VEO3_CONFIG = json.load(f)
        logger.debug("Loaded veo3.json")
    return _VEO3_CONFIG


def get_provider_rate_limits() -> dict:
    """Per-tool concurrency for scene video (Veo / Kling / Runway) and scene image APIs.

    Loaded from ``tvd_pipeline/config/provider_rate_limits.json``. Keys:
    ``scene_video`` (veo_vertex, kling_kie, runway_kie, kie_default, none),
    ``scene_image`` (nano_banana_pro_kie, gemini_3_flash_kie, vertex variants).
    """
    global _PROVIDER_RATE_LIMITS
    if _PROVIDER_RATE_LIMITS is None:
        path = os.path.join(os.path.dirname(__file__), "config", "provider_rate_limits.json")
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            _PROVIDER_RATE_LIMITS = {
                "scene_video": raw.get("scene_video") or {},
                "scene_image": raw.get("scene_image") or {},
            }
            logger.debug("Loaded provider_rate_limits.json")
        except Exception as e:
            logger.warning("Could not load provider_rate_limits.json: %s — using built-in defaults", e)
            _PROVIDER_RATE_LIMITS = {
                "scene_video": {
                    "veo_vertex": {"max_concurrent": 2, "delay_after_each_sec": 6},
                    "kling_kie": {"max_concurrent": 4, "delay_after_each_sec": 3},
                    "runway_kie": {"max_concurrent": 4, "delay_after_each_sec": 3},
                    "kie_default": {"max_concurrent": 4, "delay_after_each_sec": 2},
                    "none": {"max_concurrent": 32, "delay_after_each_sec": 0},
                },
                "scene_image": {
                    "nano_banana_pro_kie": {"parallel_workers": 12},
                    "gemini_3_flash_kie": {"parallel_workers": 8},
                    "gemini_3_pro_vertex": {"parallel_workers": 1},
                    "nano_banana_2_vertex": {"parallel_workers": 8},
                    "gemini_31_flash_vertex": {"parallel_workers": 4},
                    "gemini_25_flash_vertex": {"parallel_workers": 12},
                },
            }
    return _PROVIDER_RATE_LIMITS


def get_kie_config() -> dict:
    """Return the Kie.ai config dict (cached after first load).

    Loaded from ``tvd_pipeline/config/kie.json``.  Contains API parameters
    for Nano Banana, Runway, Kling, and Flash sub-services.
    """
    global _KIE_CONFIG
    if _KIE_CONFIG is None:
        path = os.path.join(os.path.dirname(__file__), "config", "kie.json")
        with open(path, encoding="utf-8") as f:
            _KIE_CONFIG = json.load(f)
        logger.debug("Loaded kie.json")
    return _KIE_CONFIG


def get_suno_config() -> dict:
    """Return the Suno config dict (cached after first load).

    Loaded from ``tvd_pipeline/config/suno.json``.  Contains API parameters
    for each generation mode (pure, upload-cover, cover vocal/instrumental).
    """
    global _SUNO_CONFIG
    if _SUNO_CONFIG is None:
        path = os.path.join(os.path.dirname(__file__), "config", "suno.json")
        with open(path, encoding="utf-8") as f:
            _SUNO_CONFIG = json.load(f)
        logger.debug("Loaded suno.json")
    return _SUNO_CONFIG


def get_zapcap_config() -> dict:
    """Return the ZapCap config dict (cached after first load).

    Loaded from ``tvd_pipeline/config/zapcap.json``.  Contains template IDs,
    render options, and the style_override flag.
    """
    global _ZAPCAP_CONFIG
    if _ZAPCAP_CONFIG is None:
        path = os.path.join(os.path.dirname(__file__), "config", "zapcap.json")
        with open(path, encoding="utf-8") as f:
            _ZAPCAP_CONFIG = json.load(f)
        logger.debug("Loaded zapcap.json")
    return _ZAPCAP_CONFIG


def get_fal_config() -> dict:
    """Return the fal.ai config dict (cached after first load).

    Loaded from ``tvd_pipeline/config/fal.json``.  Contains API parameters
    for fal.ai reference-to-video and other fal services.
    """
    global _FAL_CONFIG
    if _FAL_CONFIG is None:
        path = os.path.join(os.path.dirname(__file__), "config", "fal.json")
        with open(path, encoding="utf-8") as f:
            _FAL_CONFIG = json.load(f)
        logger.debug("Loaded fal.json")
    return _FAL_CONFIG


_ARC_CACHE: dict = {}
_ARC_BEAT_RE = _re.compile(r'^\d+\.\s*\[(\w+)\]\s*(.+)$')


def get_arc_template(business_category: str, duration: int) -> list:
    """Load arc beat map for *business_category* + *duration*.

    Returns a list of ``{"role": "hook", "guidance": "..."}`` dicts.
    Falls back to ``"general"`` if the category file is not found.
    """
    from tvd_pipeline.config import get_pipeline_defaults
    tiers = get_pipeline_defaults().get("arc_duration_tiers", {})
    tier_name = "medium"
    for name, (lo, hi) in tiers.items():
        if lo <= duration <= hi:
            tier_name = name
            break

    cat = (business_category or "general").strip().lower().replace(" ", "_").replace("-", "_")
    cache_key = f"{cat}:{tier_name}"
    if cache_key in _ARC_CACHE:
        return _ARC_CACHE[cache_key]

    arcs_dir = os.path.join(os.path.dirname(__file__), "config", "arcs")
    path = os.path.join(arcs_dir, f"{cat}.md")
    if not os.path.isfile(path):
        logger.warning("Arc template '%s' not found, falling back to general", cat)
        path = os.path.join(arcs_dir, "general.md")
    if not os.path.isfile(path):
        logger.error("General arc template not found at %s", path)
        _ARC_CACHE[cache_key] = []
        return []

    with open(path, encoding="utf-8") as f:
        content = f.read()

    # Parse the requested tier section
    beats = _parse_arc_section(content, tier_name)
    if not beats:
        # Fallback: try medium
        beats = _parse_arc_section(content, "medium")
    logger.info("Arc template loaded: %s / %s -> %d beats", cat, tier_name, len(beats))
    _ARC_CACHE[cache_key] = beats
    return beats


def _parse_arc_section(content: str, tier_name: str) -> list:
    """Parse a single tier section from an arc template file."""
    beats = []
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower() == f"## {tier_name}":
            in_section = True
            continue
        if stripped.startswith("## ") and in_section:
            break  # next section
        if in_section:
            m = _ARC_BEAT_RE.match(stripped)
            if m:
                beats.append({"role": m.group(1), "guidance": m.group(2)})
    return beats


def format_arc_beats(beats: list) -> str:
    """Format arc beats into a text block for prompt injection."""
    if not beats:
        return ""
    lines = []
    for i, b in enumerate(beats, 1):
        lines.append(f"Beat {i} [{b['role'].upper()}]: {b['guidance']}")
    return "\n".join(lines)


def prepare_wps_sample_text(prompt: str, min_words: int = 8, target_words: int = 20) -> str:
    """Extract a short sample from the user's prompt for voice WPS calibration.

    Strips URLs and special characters, takes the first *target_words* words.
    If the cleaned text has fewer than *min_words*, appends a generic padding
    sentence so the calibration TTS call has enough content.

    Args:
        prompt: The user's raw prompt text.
        min_words: Minimum acceptable word count before padding.
        target_words: How many words to aim for.

    Returns:
        A cleaned sample string suitable for a short TTS calibration call.
    """
    import re as _re

    text = (prompt or "").strip()
    # Strip URLs
    text = _re.sub(r'https?://\S+', '', text)
    # Strip special characters (keep letters, digits, spaces, basic punctuation)
    text = _re.sub(r'[^\w\s.,!?\'-]', '', text)
    # Collapse whitespace
    text = " ".join(text.split())

    words = text.split()[:target_words]
    sample = " ".join(words)

    if len(words) < min_words:
        padding = "This is a sample text for voice speed calibration and testing purposes."
        sample = (sample + " " + padding).strip()

    return sample
