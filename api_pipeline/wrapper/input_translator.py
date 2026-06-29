"""Translate API request params into monolith method kwargs.

Handles resolution tier resolution, animation model selection, and
parameter name mapping between the API schema and the monolith's
VideoSceneProcessor methods.
"""

import logging
from typing import Dict, Any

from api_pipeline.resolution_tiers import get_tier
from api_pipeline.defaults_config import get_default
from api_pipeline.model_mappings_config import get_animation_model_map, get_image_api_map
from tvd_pipeline.config import get_pipeline_defaults

logger = logging.getLogger(__name__)

# Passed through the API for ugc-real only. process_ugc_video() (influencer / personal-brand) has no **kwargs —
# leaving these in translated causes "unexpected keyword argument" in the monolith bridge.
_UGC_REAL_ONLY_MONOLITH_KEYS = (
    "offer_type",
    "offer_category",
    "delivery_format",
    "device_type",
    "target_audience",
    "main_problem",
    "key_benefits",
    "cta_text",
    "variation_count",
    "ad_format",
    "pace",
    "realism_level",
    "drama_level",
)


def _video_type_to_pipeline(video_type: str) -> str:
    """Map API video_type to pipeline section key for resolution tiers."""
    vt = video_type.lower()
    if vt == "influencer":
        return "influencer"
    elif vt == "personal-brand":
        return "personal_brand"
    elif vt == "ugc-real":
        return "ugc_real"
    elif vt == "custom":
        return "custom"
    return "product"


def resolve_animation_model(animation_model: str, tier_config: dict) -> str:
    """Resolve 'auto' animation model from tier config; pass through explicit values.

    Args:
        animation_model: The requested model ("auto" or an explicit value like "google", "kling", "runway").
        tier_config: The resolved tier dict from get_tier().

    Returns:
        The concrete animation model string.
    """
    if animation_model != "auto":
        return animation_model

    # Infer from tier config — new format uses video_model, legacy uses veo_model
    if "video_model" in tier_config:
        vm = tier_config["video_model"]
        if vm.startswith("veo"):
            return "google"
        elif vm.startswith("kling"):
            return "kling"
        elif vm.startswith("runway"):
            return "runway"
    elif "veo_model" in tier_config:
        return "google"

    return "google"  # safe default


# ── Unified param name mapping ─────────────────────────────────────────
_VEO_MODEL_MAP = {
    "veo-3.1-fast-generate-preview": "veo-3.1-fast",
    "veo-3.1-fast-generate-001": "veo-3.1-fast",
    "veo-3.1-generate-preview": "veo-3.1",
    "veo-3.1-generate-001": "veo-3.1",
    "veo-3.0-generate-001": "veo-3.0",
}


def _map_video_model(veo_model: str) -> str:
    """Map internal veo model names to unified short names."""
    return _VEO_MODEL_MAP.get(veo_model, veo_model)


# ── animation_model → video_model+video_provider mapping ──────────────
# Loaded from config/model_mappings.json; tuple format: (video_model, video_provider)
_ANIMATION_MODEL_MAP = get_animation_model_map()

# ── image_api → image_model+image_provider mapping ────────────────────
# NOTE: "gemini-3-pro-image" is a short name used by the wrapper and monolith.
# The actual Vertex AI model ID is "gemini-3-pro-image-preview" (appended by
# GeminiImageService). The dispatch works because it checks
# model.startswith("gemini"), so the short name routes correctly.
# Loaded from config/model_mappings.json; tuple format: (image_model, image_provider)
_IMAGE_API_MAP = get_image_api_map()


def translate_params(video_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Map API request params to monolith VideoSceneProcessor method kwargs.

    This is the central translation point. The monolith expects arguments
    matching its Google Sheets column structure. We map from the clean API
    schema to those kwargs.

    Override priority (highest wins):
        1. Explicit model/provider overrides (video_model, image_model, etc.)
        2. animation_model / image_api convenience overrides
        3. Resolution tier defaults

    Args:
        video_type: The API video type string.
        params: The raw API request params dict.

    Returns:
        Dict of kwargs ready to pass to the monolith's process_*_video() method.
    """
    output_resolution = params.get("output_resolution", get_default("output_resolution", "720p_low"))
    _raw_duration = params.get("duration")
    logger.info("[translate_params] video_type=%s  raw duration param=%s", video_type, _raw_duration)
    tier = get_tier(output_resolution, _video_type_to_pipeline(video_type))

    # Load pipeline defaults for any params the API caller didn't send
    _defaults = get_pipeline_defaults()

    # Sound sync mapping: API → monolith
    raw_sync = params.get("sound_sync_method")
    if raw_sync is not None:
        if raw_sync == "beat_sync":
            sync_method = "precision"
        elif raw_sync == "none":
            sync_method = "standard"
        else:
            sync_method = raw_sync
    else:
        # API caller didn't send it — use monolith-native value from config
        sync_method = _defaults.get("sync_method", "standard")

    # Build character URLs list (monolith expects a list)
    character_urls = []
    char_url = params.get("character_url")
    if char_url:
        character_urls.append(char_url)
    for u in (params.get("character_urls") or []):
        if u and isinstance(u, str) and u.strip() and u.strip() not in character_urls:
            character_urls.append(u.strip())

    # Normalize asset URLs to list of dicts
    raw_assets = params.get("asset_urls") or []
    assets = []
    for a in raw_assets:
        if isinstance(a, dict):
            if a.get("url") and a["url"].strip():
                assets.append({"url": a["url"].strip(), "type": a.get("type"), "keep_audio": a.get("keep_audio", False)})
        elif isinstance(a, str) and a.strip():
            assets.append({"url": a.strip(), "type": None, "keep_audio": False})

    # Product image URLs: main list + optional clean product images 2 & 3
    product_images = [u for u in (params.get("product_image_urls") or []) if u and u.strip()]
    clean_extra = [u for u in (params.get("clean_product_image_urls") or []) if u and u.strip()]
    if clean_extra:
        product_images = product_images + clean_extra

    # Reference image URLs
    ref_images = [u for u in (params.get("reference_image_urls") or []) if u and u.strip()]

    # ── Resolve tier defaults ────────────────────────────────────────
    if "video_model" in tier:
        tier_video_model = tier.get("video_model", "veo-3.1-fast")
        tier_video_provider = tier.get("video_provider", "direct")
        tier_video_resolution = tier.get("video_resolution", "720p")
        tier_image_model = tier.get("image_model", "nano-banana-pro")
        tier_image_provider = tier.get("image_provider", "kie")
        tier_image_resolution = tier.get("image_resolution", "1K")
    else:
        # Legacy tier format — convert to unified names
        legacy_veo_model = tier.get("veo_model", "veo-3.1-fast-generate-preview")
        tier_video_model = _map_video_model(legacy_veo_model)
        tier_video_provider = "direct" if legacy_veo_model else None
        tier_video_resolution = tier.get("veo_res", "720p")
        tier_image_model = None
        tier_image_provider = None
        tier_image_resolution = tier.get("nb_res", "1K")

    # ── Apply user overrides (tier < convenience < explicit) ─────────

    # Start with tier defaults
    final_video_model = tier_video_model
    final_video_provider = tier_video_provider
    final_video_resolution = tier_video_resolution
    final_image_model = tier_image_model
    final_image_provider = tier_image_provider
    final_image_resolution = tier_image_resolution

    # Convenience override: animation_model → video_model + video_provider
    animation_model_raw = params.get("animation_model", get_default("animation_model", "auto"))
    animation_model = resolve_animation_model(animation_model_raw, tier)
    if animation_model_raw != "auto" and not params.get("video_model"):
        mapped = _ANIMATION_MODEL_MAP.get(animation_model)
        if mapped:
            mapped_model, mapped_provider = mapped
            if mapped_model is None:
                # "none" — skip animation entirely, let monolith handle
                final_video_model = "none"
                final_video_provider = None
            elif animation_model == "google" and final_video_model.startswith("veo"):
                # For "google": preserve tier's veo model version if already a veo model
                final_video_provider = mapped_provider
            else:
                final_video_model, final_video_provider = mapped_model, mapped_provider
        else:
            logger.warning(f"Unrecognized animation_model '{animation_model_raw}', using tier defaults")

    # Convenience override: image_api → image_model + image_provider
    if not params.get("image_model"):
        img_api = params.get("image_api")
        if img_api:
            mapped = _IMAGE_API_MAP.get(img_api)
            if mapped:
                final_image_model, final_image_provider = mapped

    # Explicit overrides always win
    if params.get("video_model"):
        final_video_model = params["video_model"]
    if params.get("video_provider"):
        final_video_provider = params["video_provider"]
    if params.get("video_resolution"):
        final_video_resolution = params["video_resolution"]
    if params.get("image_model"):
        final_image_model = params["image_model"]
    if params.get("image_provider"):
        final_image_provider = params["image_provider"]
    if params.get("image_resolution"):
        final_image_resolution = params["image_resolution"]

    # Auto-infer image_provider from image_model when only image_model is set.
    # Without this, a user setting image_model="gemini-3-pro-image" but not
    # image_provider gets the tier default ("kie"), producing broken composite
    # pricing keys like "gemini-3-pro-image:kie".
    if params.get("image_model") and not params.get("image_provider"):
        _im = params["image_model"]
        if "gemini" in _im and ("3.1-flash" in _im or "flash" not in _im):
            final_image_provider = "direct"  # Vertex: Pro or 3.1 Flash
        elif "flash" in _im:
            final_image_provider = "kie"  # Kie Flash
        # nano-banana and other kie models keep the tier default ("kie")

    # Text model/provider (None = use monolith per-step defaults)
    final_text_model = params.get("text_model")
    final_text_provider = params.get("text_provider")

    translated = {
        # Core prompt
        "prompt": params.get("prompt", ""),

        # Edited TEXT 1–3 (all pipelines; UGC Real uses for intake overrides / Phase 2)
        "text_1": params.get("text_1"),
        "text_2": params.get("text_2"),
        "text_3": params.get("text_3"),

        # Unified model+provider params (final resolved values)
        "video_model": final_video_model,
        "video_provider": final_video_provider,
        "video_resolution": final_video_resolution,
        "image_model": final_image_model,
        "image_provider": final_image_provider,
        "image_resolution": final_image_resolution,
        "output_resolution": output_resolution,

        # Text model+provider (optional API overrides)
        "text_model": final_text_model,
        "text_provider": final_text_provider,

        # Sync
        "sync_method": sync_method,
        "sync_strategy": params.get("beat_sync_strategy") or _defaults.get("sync_strategy", "phrase_start"),

        # Duration and style
        "target_duration": params.get("duration", get_default("duration", 20)),  # NOTE: sourced from API "duration" param
        "visual_style": params.get("style", get_default("style", "Auto")),
        "language": params.get("language", get_default("language", "en")),
        "country": params.get("country", get_default("country", "")),

        # Audio and subtitles
        "add_subtitles": params["add_subtitles"] if params.get("add_subtitles") is not None else _defaults.get("add_subtitles", True),
        "subtitle_language": params.get("language", get_default("language", "en")),
        "dissolve_seconds": params["dissolve_seconds"] if params.get("dissolve_seconds") is not None else _defaults.get("dissolve_seconds", 0.4),

        # Character
        "character_urls": character_urls,
        "character_description": params.get("character_description"),
        "gender": params.get("gender", get_default("gender", "f")),

        # Product-specific (product_explain only for product video; UGC does not accept it)
        "product_image_urls": product_images,
        "product_image_mode": params.get("product_image_mode", get_default("product_image_mode", "auto")),

        # UGC / influencer / personal-brand specifics
        "reference_image_urls": ref_images,
        "asset_urls": assets,

        # Shared optional
        "logo_url": params.get("logo_url"),
        "slogan_text": params.get("slogan_text"),
        "voice_id": params.get("voice_id"),
        "video_reference_url": params.get("video_reference_url"),
        "quality_check": params.get("quality_check", get_default("quality_check", True)),
        "enrich_cta_with_influencer": params.get("enrich_cta_with_influencer", get_default("enrich_cta_with_influencer", False)),

        # VO generation (all pipelines; defaults from config)
        "generate_vo": params["generate_vo"] if params.get("generate_vo") is not None else _defaults.get("generate_vo", True),

        # When pausing after VO step: generate script only (no TTS); TTS runs after user approves (Studio or resume).
        "vo_script_only": params.get("pause_after_step") == "step_2.7",

        # When pausing after step_1 (Preferences): run only parse_prompt (skip character + analyze_media for speed).
        "run_only_parse_prompt": params.get("pause_after_step") == "step_1",
        # When Phase 2 (seed + pause after VO): skip character + analyze_media so we get to VO fast.
        "skip_character_and_analyze_media": bool(params.get("seed_job_id")) and params.get("pause_after_step") == "step_2.7",

        # Dry-run mode: stop after Director output (no asset generation)
        "generate_assets": params["generate_assets"] if params.get("generate_assets") is not None else True,

        # Asset mode (influencer only, defaults to "smart")
        "asset_mode": params.get("asset_mode") if params.get("asset_mode") else _defaults.get("asset_mode", "smart"),

        # Business category for arc template selection (influencer smart mode)
        "business_category": params.get("business_category", "general"),

        # Business highlights (influencer smart mode, optional)
        "highlights": params.get("highlights"),

        # Influencer clip ratio (influencer smart mode)
        "min_influencer_clip_ratio": params.get("min_influencer_clip_ratio") if params.get("min_influencer_clip_ratio") is not None else _defaults.get("min_influencer_clip_ratio", 0.10),
        "max_influencer_clip_ratio": params.get("max_influencer_clip_ratio") if params.get("max_influencer_clip_ratio") is not None else _defaults.get("max_influencer_clip_ratio", 0.20),

        # VO duration hints (only meaningful with smart mode)
        "vo_duration_hints": params.get("vo_duration_hints") if params.get("vo_duration_hints") is not None else _defaults.get("vo_duration_hints", False),

        # Surprise mode (influencer smart mode)
        "surprise_mode": params.get("surprise_mode") if params.get("surprise_mode") is not None else _defaults.get("surprise_mode", 2),

        # Extended version (influencer smart mode only)
        "generate_extended": params["generate_extended"] if params.get("generate_extended") is not None else _defaults.get("generate_extended", False),

        # Background removal (influencer only)
        "remove_character_bg": params.get("remove_character_bg", False),

        # Location awareness (influencer / personal-brand)
        "product_location": params.get("product_location"),

        # End card params (influencer only)
        "business_name": params.get("business_name"),
        "business_address": params.get("business_address"),
        "business_phone": params.get("business_phone"),
        "business_website": params.get("business_website"),
        "end_card_color": params.get("end_card_color", "white"),
        "end_card_detail_color": params.get("end_card_detail_color", "white"),
        "end_card_position": params.get("end_card_position", _defaults.get("end_card_position", "middle")),

        # Subtitle emoji enrichment
        "subtitle_emoji": params["subtitle_emoji"] if params.get("subtitle_emoji") is not None else _defaults.get("subtitle_emoji", True),

        # Vertical option (reference image portrait conversion)
        "vertical_option": params["vertical_option"] if params.get("vertical_option") is not None else _defaults.get("vertical_option", "none"),

        # Subtitle position
        "subtitle_position": params.get("subtitle_position", _defaults.get("subtitle_position", "middle")),

        # Film grain (per-pipeline defaults resolved below)
        "film_grain": None,  # placeholder, resolved after dict creation
        # UGC Real offer-aware fields
        "offer_type": params.get("offer_type"),
        "offer_category": params.get("offer_category"),
        "delivery_format": params.get("delivery_format"),
        "device_type": params.get("device_type"),
        "target_audience": params.get("target_audience"),
        "main_problem": params.get("main_problem"),
        "key_benefits": params.get("key_benefits"),
        "cta_text": params.get("cta_text"),
        "variation_count": params.get("variation_count"),
        "ad_format": params.get("ad_format"),
        "pace": params.get("pace"),
        "realism_level": params.get("realism_level"),
        "drama_level": params.get("drama_level"),
    }

    # Resolve film_grain per-pipeline defaults
    raw_film_grain = params.get("film_grain")
    if raw_film_grain is not None:
        translated["film_grain"] = raw_film_grain
    else:
        vt = video_type.lower()
        if vt in ("influencer", "personal-brand", "personal-service", "ugc-style video", "ugc-real"):
            translated["film_grain"] = _defaults.get("film_grain_ugc", True)
        else:
            translated["film_grain"] = _defaults.get("film_grain_product", False)

    # Type 2 (monolith) simulation: pass simulation=True to the monolith
    if params.get("is_simulation") and params.get("simulation_type") == "monolith":
        translated["simulation"] = True

    # product_explain is only accepted by process_product_video; UGC pipeline rejects it
    if video_type and video_type.lower() == "product video":
        translated["product_explain"] = params.get("product_explain")
        pnoc = bool(params.get("product_no_on_screen_character"))
        translated["product_no_on_screen_character"] = pnoc
        if pnoc:
            translated["character_urls"] = []
            translated["character_description"] = None

    vt_out = (video_type or "").strip().lower()
    if vt_out != "ugc-real":
        for _k in _UGC_REAL_ONLY_MONOLITH_KEYS:
            translated.pop(_k, None)

    # Custom pipeline: pass storyboard JSON through. The bridge pops it before
    # calling process_custom_video so it is not also spread as a kwarg twice.
    if vt_out == "custom":
        translated["storyboard"] = params.get("storyboard")

    return translated
