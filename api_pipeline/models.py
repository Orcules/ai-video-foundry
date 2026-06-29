"""Pydantic models for the Video Generation API."""

from enum import Enum
import re
from typing import ClassVar, Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime

from api_pipeline.defaults_config import get_default


class SoundSyncMethod(str, Enum):
    NONE = "none"
    BEAT_SYNC = "beat_sync"


class GenerateVideoRequest(BaseModel):
    """Request body for POST /api/generate."""
    video_type: str = Field(..., description="'product video', 'influencer', 'personal-brand', or 'ugc-real' (legacy: 'UGC-style video', 'personal-service')")
    prompt: str = Field(default="", description="Product/video description prompt")
    simulation: bool = Field(default=False, description="Run in simulation mode (Type 1: wrapper mock, or Type 2: monolith with mock API calls)")
    simulation_type: str = Field(default=get_default("simulation_type", "wrapper"), description="Simulation type: 'wrapper' (Type 1 — mock services, no monolith) or 'monolith' (Type 2 — real monolith pipeline with mock API calls)")
    sim_duration_seconds: Optional[int] = Field(default=None, ge=0, le=300, description="DEPRECATED — use simulation_duration instead. Kept for backward compatibility.")
    simulation_duration: Optional[str] = Field(default=None, description="Simulation pacing: 'none' (instant), 'real' (~12 min, production-speed delays), or a duration string like '25s', '1m', '1.5m' (scale real timings proportionally). Max 1200s (20m). Type 1 only.")
    duration: int = Field(default=get_default("duration", 20), ge=10, le=120, description="Target video duration in seconds")
    style: str = Field(default=get_default("style", "Auto"), description="Visual style (Auto, Modern flat 2d, Cinematic photography, etc.)")
    animation_model: str = Field(default=get_default("animation_model", "auto"), description="Animation model: 'auto' (resolved from output_resolution tier), or explicit override: google, runway, kling")
    language: str = Field(default=get_default("language", "en"), description="Language code for VO and subtitles")
    country: str = Field(default=get_default("country", ""), description="Target country for cultural adaptation")
    add_subtitles: Optional[bool] = Field(default=None, description="Whether to add ZapCap subtitles. Default from pipeline_defaults.json: true")
    sound_sync_method: Optional[str] = Field(default=None, description="Audio-video sync: 'none' (VO overlay only) or 'beat_sync' (VO-driven per-scene timing with frame-perfect trim). Default from pipeline_defaults.json: none")
    beat_sync_strategy: Optional[str] = Field(default=None, description="Beat-sync tiling strategy: 'phrase_start' (cut at next phrase start, +0.5s buffer) or 'continuous' (monolith-style gapless tiling from prev_end, +1.0s buffer). Default from pipeline_defaults.json: phrase_start")
    dissolve_seconds: Optional[float] = Field(default=None, ge=0, le=2.0, description="Dissolve transition duration between scenes in seconds (0 = hard cut). Applies to all pipelines. Default from pipeline_defaults.json: 0.4")
    generate_vo: Optional[bool] = Field(default=None, description="Whether to generate voiceover. Works for all pipeline types. Default from pipeline_defaults.json: true")
    generate_assets: Optional[bool] = Field(
        default=None,
        description="When false, pipeline stops after Director output (no image/video generation). "
                    "Useful for dry-run testing of analysis + Director flow without cost. Default: true."
    )
    pause_after_step: Optional[str] = Field(
        default=None,
        description="Step id after which to pause (e.g. 'step_2.7' = pause after VO). Job stays paused until resumed. Same checkpoint/resume flow as manual Pause."
    )
    seed_job_id: Optional[str] = Field(
        default=None,
        description="Copy intermediates from this job into the new job (e.g. continue from a paused/phase job). New job must be same tenant."
    )
    output_resolution: str = Field(default=get_default("output_resolution", "720p_low"), description="Output quality tier: '720p_low', '720p_high', '1080p_low', '1080p_high', '4k_low', '4k_high'")
    # UGC Real intake
    offer_type: Optional[str] = Field(default=None, description="UGC Real offer type: 'physical_product', 'digital_product', or 'service'")
    offer_category: Optional[str] = Field(default=None, description="UGC Real offer category")
    delivery_format: Optional[str] = Field(default=None, description="UGC Real service delivery format: local, remote, hybrid")
    device_type: Optional[str] = Field(default=None, description="UGC Real digital product device type: mobile, desktop, both")
    target_audience: Optional[str] = Field(default=None, description="UGC Real target audience")
    main_problem: Optional[str] = Field(default=None, description="UGC Real main problem solved")
    key_benefits: Optional[str] = Field(default=None, description="UGC Real key benefits")
    cta_text: Optional[str] = Field(default=None, description="UGC Real CTA text")
    variation_count: Optional[int] = Field(default=None, ge=1, le=10, description="UGC Real number of ad variations")
    ad_format: Optional[str] = Field(default=None, description="UGC Real ad format (talking_head, podcast, car_selfie, etc.)")
    pace: Optional[str] = Field(default=None, description="UGC Real pacing preference")
    realism_level: Optional[int] = Field(default=None, ge=1, le=10, description="UGC Real realism intensity (1-10)")
    drama_level: Optional[int] = Field(default=None, ge=1, le=10, description="UGC Real drama intensity (1-10)")

    # Model/provider overrides (optional — override resolution tier defaults)
    video_model: Optional[str] = Field(default=None, description="Override video model (e.g., 'veo-3.1-fast', 'kling-2.5', 'kling-2.6', 'runway')")
    video_provider: Optional[str] = Field(default=None, description="Override video provider ('direct' or 'kie')")
    video_resolution: Optional[str] = Field(default=None, description="Override video resolution ('720p', '1080p', '4k')")
    image_model: Optional[str] = Field(default=None, description="Override image model ('nano-banana-pro', 'gemini-3-pro-image', 'gemini-3-flash')")
    image_provider: Optional[str] = Field(default=None, description="Override image provider ('direct' or 'kie')")
    image_resolution: Optional[str] = Field(default=None, description="Override image resolution ('1K', '2K', '4K')")
    text_model: Optional[str] = Field(default=None, description="Override text LLM model")
    text_provider: Optional[str] = Field(default=None, description="Override text LLM provider ('vertex', 'openai')")

    # Optional pre-filled or edited TEXT 1–3 (from prompt parse). If provided, pipeline uses these instead of re-parsing.
    text_1: Optional[str] = Field(default=None, description="Headline / Hook. When provided, used as parsed_texts.text_1 (skip parse or use edited value).")
    text_2: Optional[str] = Field(default=None, description="Key message / Body. When provided, used as parsed_texts.text_2.")
    text_3: Optional[str] = Field(default=None, description="Call to action / Closing. When provided, used as parsed_texts.text_3.")

    # Product video specific
    product_image_urls: Optional[List[str]] = Field(default=None, description="Product reference image URLs (product video)")
    clean_product_image_urls: Optional[List[str]] = Field(default=None, description="Additional clean product image URLs (image 2, 3). Merged with product_image_urls when passed to monolith.")
    product_explain: Optional[str] = Field(default=None, description="Optional text explaining the product (product video).")
    product_no_on_screen_character: Optional[bool] = Field(
        default=None,
        description="Product video only. When true, omit on-screen spokesperson/recurring character; product-focused visuals (no character URLs/description).",
    )
    product_image_mode: str = Field(default=get_default("product_image_mode", "auto"), description="Product image handling: 'auto' (Gemini evaluates each image, only regenerates dirty ones), 'clean' (images already clean — skip regeneration), 'force_clean' (always regenerate via Nano Banana)")

    # Influencer / Personal-Brand specific
    gender: str = Field(default=get_default("gender", "f"), description="Influencer gender: m or f (influencer and personal-brand)")
    reference_image_urls: Optional[List[str]] = Field(default=None, description="Reference image URLs (UGC)")
    asset_urls: Optional[List[Any]] = Field(default=None, description="UGC: assets to insert. Each item is a URL string or {\"url\": \"...\", \"type\": \"image\"|\"video\"}")
    enrich_cta_with_influencer: bool = Field(default=get_default("enrich_cta_with_influencer", False), description="Influencer/personal-brand: apply influencer enrichment to the CTA/ending scene (default: clean logo+slogan only)")
    asset_mode: Optional[str] = Field(default=None, description="Asset handling: 'smart' (LLM-analyzed, content-aware placement) or 'legacy' (hardcoded 3s trim, even spacing). Default from pipeline_defaults.json")
    vo_duration_hints: Optional[bool] = Field(default=None, description="When asset_mode='smart': pass qualitative duration tags to the VO LLM. Nudges longer narration for long assets. Default from pipeline_defaults.json")
    film_grain: Optional[bool] = Field(default=None, description="Add subtle film grain to final video for organic look. Default: true for influencer/personal-brand, false for product. From pipeline_defaults.json")
    subtitle_emoji: Optional[bool] = Field(default=None, description="Enable emoji + keyword highlighting in subtitles (default: true)")
    vertical_option: Optional[str] = Field(
        default=None,
        description="How to convert reference images to portrait (9:16) for video generation. "
                    "'none': use as-is. 'crop_by_code': AI focal-point crop (existing). "
                    "'crop_by_generation': re-generate in portrait (not yet implemented). "
                    "'auto': auto-select best strategy (not yet implemented). "
                    "Influencer/personal-brand only. Default from pipeline_defaults.json: 'none'."
    )
    subtitle_position: Optional[str] = Field(default=get_default("subtitle_position", "middle"), description="Subtitle vertical position: 'top', 'middle', or 'bottom'. All pipelines.")
    business_category: Optional[str] = Field(default=None, description="Business category for arc template selection (influencer smart mode). E.g. 'restaurant', 'hotel', 'fitness'. Falls back to 'general' if not set or unrecognized.")
    highlights: Optional[List[str]] = Field(
        default=None,
        description="Business highlights — unique/special things to emphasize in VO and visuals. "
                    "E.g. ['Cat-themed decor everywhere', 'Interactive origami crafting']. "
                    "If not provided, extracted automatically from the prompt (influencer smart mode only)."
    )
    min_influencer_clip_ratio: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Minimum fraction of clips showing the influencer (0.0-1.0). Default from pipeline_defaults.json: 0.10. Influencer pipeline only."
    )
    max_influencer_clip_ratio: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Maximum fraction of clips showing the influencer (0.0-1.0). Default from pipeline_defaults.json: 0.20. Increase for known-figure influencers."
    )
    surprise_mode: Optional[Any] = Field(
        default=None,
        description="Surprise animation mode (smart asset mode only): integer >= 1 (minimum number of surprise variants), 'all' (prefer surprise variants), or 'none' (no surprises). Default from pipeline_defaults.json: 2."
    )

    # Extended version (influencer smart mode only)
    generate_extended: Optional[bool] = Field(default=None, description="Influencer smart mode only. Generate a second extended version using full-length raw clips with a new longer VO. Stored as GCS URL in intermediates (no Mux upload). Extra cost ~$0.12-0.17.")

    # Background removal (influencer only)
    remove_character_bg: bool = Field(default=False, description="Remove character image background before NB2 compositing (influencer only). Uses fal.ai birefnet.")

    # Location awareness (influencer / personal-brand)
    product_location: Optional[str] = Field(default=None, description="Where the product/venue is physically located — controls visual style of generated scenes. E.g. 'Wroclaw, Poland'. Auto-extracted from prompt via LLM if not provided.")

    # End card params (influencer only)
    business_name: Optional[str] = Field(default=None, description="Business name for end card overlay (influencer only). E.g. 'OISHI HOUSE'")
    business_address: Optional[str] = Field(default=None, description="Business address for end card overlay (influencer only). E.g. 'Spalena St, Prague'")
    business_phone: Optional[str] = Field(default=None, description="Business phone for end card overlay (influencer only). E.g. '+420 123 456 789'")
    business_website: Optional[str] = Field(default=None, description="Business website for end card overlay (influencer only). E.g. 'www.oishi-house.com'")
    end_card_color: Optional[str] = Field(default="white", description="End card name color: preset name (pink, gold, cyan, coral, lime, violet) or hex (#FF6B9D). Influencer only.")
    end_card_detail_color: Optional[str] = Field(default="white", description="End card address/phone color: preset name or hex. Influencer only.")
    end_card_position: Optional[str] = Field(default=get_default("end_card_position", "middle"), description="End card text position: 'bottom', 'top', or 'middle'. Influencer only.")

    # Shared optional
    character_url: Optional[str] = Field(default=None, description="Character/influencer image URL (single)")
    character_urls: Optional[List[str]] = Field(default=None, description="Character/influencer image URLs (multiple people, comma-separated in sheet)")
    character_description: Optional[str] = Field(default=None, description="Character/influencer text description (bypasses Gemini AI image analysis)")
    logo_url: Optional[str] = Field(default=None, description="Logo URL for CTA scene")
    slogan_text: Optional[str] = Field(default=None, description="Slogan text for CTA scene")
    voice_id: Optional[str] = Field(default=None, description="Custom ElevenLabs voice ID")
    video_reference_url: Optional[str] = Field(default=None, description="Reference video URL for structure analysis")
    image_api: str = Field(default=get_default("image_api", "kie"), description="Image generation API: 'google' (Vertex Gemini, parallelism=1), 'kie-flash' (Kie Flash, parallelism=6), 'kie' (Nano Banana, default, parallelism=6)")

    # Custom pipeline: full storyboard JSON built by the chat agent.
    # Required when video_type == "custom"; ignored for other pipelines.
    storyboard: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Chat-built storyboard JSON (custom pipeline only). See _storyboard.py for schema.",
    )

    customer_id: Optional[str] = Field(default=None, description="Customer identifier")
    session_id: Optional[str] = Field(
        default=None,
        description="Video Studio: UUID of user_sessions row (owner verified via X-Studio-User-Token). Links completed video to session for re-edit.",
    )
    user_id: Optional[str] = Field(
        default=None,
        description="Deprecated — ignored. Studio user is taken from X-Studio-User-Token only.",
    )
    quality_check: bool = Field(default=get_default("quality_check", True), description="Enable image quality gate (regenerate low-scoring images)")

    @field_validator("subtitle_position")
    @classmethod
    def validate_subtitle_position(cls, v):
        if v is None:
            return "middle"
        v = v.strip().lower()
        allowed = ("top", "middle", "bottom")
        if v not in allowed:
            raise ValueError(f"subtitle_position must be one of {allowed}, got '{v}'")
        return v

    @field_validator("end_card_position")
    @classmethod
    def validate_end_card_position(cls, v):
        if v is None:
            return "middle"
        v = v.strip().lower()
        allowed = ("bottom", "top", "middle")
        if v not in allowed:
            raise ValueError(f"end_card_position must be one of {allowed}, got '{v}'")
        return v

    @field_validator("simulation_type")
    @classmethod
    def validate_simulation_type(cls, v):
        v = v.strip().lower()
        allowed = ("wrapper", "monolith")
        if v not in allowed:
            raise ValueError(f"simulation_type must be one of {allowed}, got '{v}'")
        return v

    @field_validator("simulation_duration")
    @classmethod
    def validate_simulation_duration(cls, v):
        if v is None:
            return v
        v = v.strip().lower()
        if v in ("none", "real"):
            return v
        m = re.match(r"^(\d+\.?\d*)(s|m)$", v)
        if not m:
            raise ValueError(
                "simulation_duration must be 'none', 'real', or a duration like '25s', '1m', '1.5m'"
            )
        amount = float(m.group(1))
        unit = m.group(2)
        seconds = amount if unit == "s" else amount * 60
        if seconds > 1200:
            raise ValueError("simulation_duration max is 1200s (20m)")
        return v

    @field_validator("offer_type")
    @classmethod
    def validate_offer_type(cls, v):
        if v is None:
            return v
        vv = str(v).strip().lower()
        allowed = ("physical_product", "digital_product", "service")
        if vv not in allowed:
            raise ValueError(f"offer_type must be one of {allowed}, got '{v}'")
        return vv

    @model_validator(mode="after")
    def migrate_sim_duration(self):
        """Backward compat: convert legacy sim_duration_seconds to simulation_duration."""
        if self.simulation_duration is None and self.sim_duration_seconds is not None:
            if self.sim_duration_seconds == 0:
                self.simulation_duration = "none"
            else:
                self.simulation_duration = f"{self.sim_duration_seconds}s"
        if self.simulation_duration is None:
            self.simulation_duration = "none"
        return self

    @model_validator(mode="after")
    def fill_simulation_defaults(self):
        if self.simulation and not self.prompt:
            self.prompt = "Simulation mode — no real prompt needed"
        if not self.simulation and not self.prompt.strip():
            raise ValueError("prompt is required for non-simulation requests")
        return self

    @model_validator(mode="after")
    def validate_animation_model_per_pipeline(self):
        """Validate and remap animation_model based on video_type."""
        # "auto" is resolved at pipeline runtime from the resolution tier
        # Normalize casing before validation (fixes Pydantic-before-normalizer issue)
        if self.animation_model:
            self.animation_model = self.animation_model.strip().lower()

        # Apply alias resolution inline (same pattern as image_api) so aliases
        # like "veo3", "kling2.5" work even though normalizer runs after Pydantic
        from api_pipeline.input_normalizer import _ANIMATION_MODEL_ALIASES
        self.animation_model = _ANIMATION_MODEL_ALIASES.get(self.animation_model, self.animation_model)

        if self.animation_model == "auto":
            return self

        from api_pipeline.model_config import get_allowed_animation_values

        vt = (self.video_type or "").lower().strip()
        from api_pipeline.input_normalizer import _VIDEO_TYPE_ALIASES
        canonical = _VIDEO_TYPE_ALIASES.get(vt)
        if canonical == "influencer":
            pipeline = "influencer"
        elif canonical == "personal-brand":
            pipeline = "personal_brand"
        elif canonical == "ugc-real":
            pipeline = "ugc_real"
        else:
            pipeline = "product"

        allowed = get_allowed_animation_values(pipeline)
        if not allowed:
            allowed = ("google", "runway", "kling", "none")

        if self.animation_model not in allowed:
            raise ValueError(f"animation_model for {pipeline} must be one of {allowed}, got '{self.animation_model}'")
        return self

    @field_validator("vertical_option")
    @classmethod
    def validate_vertical_option(cls, v):
        if v is None:
            return v
        valid = ("none", "crop_by_code", "crop_by_generation", "auto")
        if v.lower() not in valid:
            raise ValueError(f"vertical_option must be one of {valid}, got '{v}'")
        return v.lower()

    @field_validator("asset_mode")
    @classmethod
    def validate_asset_mode(cls, v):
        if v is None:
            return v
        v = v.strip().lower()
        allowed = ("legacy", "smart")
        if v not in allowed:
            raise ValueError(f"asset_mode must be one of {allowed}, got '{v}'")
        return v

    @field_validator("product_image_mode")
    @classmethod
    def validate_product_image_mode(cls, v):
        v = v.strip().lower()
        allowed = ("auto", "clean", "force_clean", "none")
        if v not in allowed:
            raise ValueError(f"product_image_mode must be one of {allowed}")
        return v

    @field_validator("output_resolution")
    @classmethod
    def validate_output_resolution(cls, v):
        v = v.strip().lower()
        # Apply alias resolution inline (same pattern as image_api) so aliases
        # like "720p" work even though normalizer runs after Pydantic validation
        from api_pipeline.input_normalizer import _OUTPUT_RESOLUTION_ALIASES
        v = _OUTPUT_RESOLUTION_ALIASES.get(v, v)
        allowed = ("720p_low", "720p_high", "1080p_low", "1080p_high", "4k_low", "4k_high")
        if v not in allowed:
            raise ValueError(f"output_resolution must be one of {allowed}")
        return v

    @field_validator("image_api")
    @classmethod
    def validate_image_api(cls, v):
        v = v.strip().lower()
        from api_pipeline.input_normalizer import _IMAGE_API_ALIASES
        v = _IMAGE_API_ALIASES.get(v, v)
        allowed = ("google", "google-31-flash", "kie-flash", "kie", "nano-banana-2", "gemini-25-flash-image")
        if v not in allowed:
            raise ValueError(f"image_api must be one of {allowed}, got '{v}'")
        return v

    @field_validator("asset_urls", mode="before")
    @classmethod
    def normalize_asset_urls(cls, v):
        """Normalize asset_urls to List[Dict] with 'url' and 'type' keys.

        Accepts plain URL strings and {url, type} objects in any mix.
        """
        if v is None:
            return v
        normalized = []
        for item in v:
            if isinstance(item, str):
                normalized.append({"url": item, "type": None, "keep_audio": False})
            elif isinstance(item, dict):
                if "url" not in item:
                    raise ValueError("Asset object must have a 'url' key")
                asset_type = item.get("type")
                if asset_type is not None and asset_type not in ("image", "video"):
                    raise ValueError(f"Asset type must be 'image', 'video', or null — got '{asset_type}'")
                normalized.append({"url": item["url"], "type": asset_type, "keep_audio": bool(item.get("keep_audio", False))})
            else:
                raise ValueError(f"Each asset must be a URL string or {{url, type}} object — got {type(item).__name__}")
        return normalized

    @field_validator("sound_sync_method")
    @classmethod
    def validate_sound_sync_method(cls, v):
        if v is None:
            return v
        v = v.strip().lower()
        allowed = ("beat_sync", "none")
        if v not in allowed:
            raise ValueError(f"sound_sync_method must be one of {allowed}, got '{v}'")
        return v

    @field_validator("beat_sync_strategy")
    @classmethod
    def validate_beat_sync_strategy(cls, v):
        if v is None:
            return v
        v = v.strip().lower()
        allowed = ("phrase_start", "continuous")
        if v not in allowed:
            raise ValueError(f"beat_sync_strategy must be one of {allowed}, got '{v}'")
        return v

    @field_validator("surprise_mode", mode="before")
    @classmethod
    def validate_surprise_mode(cls, v):
        if v is None:
            return v
        v_str = str(v).strip().lower()
        if v_str in ("all", "none"):
            return v_str
        try:
            n = int(v_str)
            if n < 1:
                raise ValueError("surprise_mode integer must be >= 1")
            return n
        except ValueError:
            raise ValueError(f"surprise_mode must be 'all', 'none', or an integer >= 1, got '{v}'")

    @field_validator("voice_id", mode="before")
    @classmethod
    def validate_voice_id(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        stripped = v.strip()
        if not stripped:
            return None
        sentinels = {"#n/a", "n/a", "na", "#ref!", "null", "none", "undefined", "-"}
        if stripped.lower() in sentinels:
            return None
        if len(stripped) < 4:
            raise ValueError("voice_id too short — ElevenLabs IDs are 20+ characters")
        return stripped

    # Per-pipeline required fields (checked after normalization of video_type aliases)
    PIPELINE_REQUIRED_FIELDS: ClassVar[Dict[str, List[str]]] = {
        "influencer": ["business_name"],
    }

    @model_validator(mode="after")
    def check_ugc_real_prompt_or_structured(self):
        from api_pipeline.input_normalizer import _VIDEO_TYPE_ALIASES

        vt = (self.video_type or "").lower().strip()
        vt = _VIDEO_TYPE_ALIASES.get(vt, vt)
        if vt != "ugc-real":
            return self
        prompt_ok = bool((self.prompt or "").strip())
        o = (self.offer_type or "").strip().lower()
        legacy_texts = all(
            bool((getattr(self, k, None) or "").strip())
            for k in ("target_audience", "main_problem", "key_benefits", "cta_text")
        )
        legacy_ok = o in ("physical_product", "digital_product", "service") and legacy_texts
        t123 = all(bool((getattr(self, f"text_{i}", None) or "").strip()) for i in (1, 2, 3))
        if not (prompt_ok or legacy_ok or t123):
            raise ValueError(
                "ugc-real requires a non-empty prompt, or offer_type with target_audience, main_problem, "
                "key_benefits, and cta_text, or non-empty text_1, text_2, and text_3"
            )
        return self

    @model_validator(mode="after")
    def check_pipeline_required_fields(self):
        from api_pipeline.input_normalizer import _VIDEO_TYPE_ALIASES
        vt = (self.video_type or "").lower().strip()
        vt = _VIDEO_TYPE_ALIASES.get(vt, vt)
        required = self.PIPELINE_REQUIRED_FIELDS.get(vt, [])
        missing = [f for f in required if not getattr(self, f, None)]
        if missing:
            raise ValueError(f"{vt} pipeline requires: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def validate_ugc_real_offer_assets(self):
        from api_pipeline.input_normalizer import _VIDEO_TYPE_ALIASES

        vt = (self.video_type or "").lower().strip()
        vt = _VIDEO_TYPE_ALIASES.get(vt, vt)
        if vt != "ugc-real":
            return self

        offer_type = (self.offer_type or "").strip().lower()
        if offer_type not in ("physical_product", "digital_product", "service"):
            return self
        if offer_type == "physical_product" and not (self.product_image_urls or self.clean_product_image_urls):
            raise ValueError("ugc-real physical_product requires at least one product image")
        if offer_type == "digital_product" and not self.reference_image_urls:
            raise ValueError("ugc-real digital_product requires at least one reference image / UI screenshot")
        return self

    @model_validator(mode="after")
    def validate_url_fields(self):
        """Reject values that are not valid URLs in URL fields."""
        valid_prefixes = ("http://", "https://", "/api/uploads/")

        def check_url(value: str, field_name: str):
            if value and not value.startswith(valid_prefixes):
                raise ValueError(f"{field_name} must be a valid URL (http://, https://, or /api/uploads/), got '{value}'")

        if self.character_url is not None:
            check_url(self.character_url, "character_url")
        if self.character_urls:
            for i, url in enumerate(self.character_urls):
                check_url(url, f"character_urls[{i}]")
        if self.logo_url is not None:
            check_url(self.logo_url, "logo_url")
        if self.video_reference_url is not None:
            check_url(self.video_reference_url, "video_reference_url")

        if self.product_image_urls:
            for i, url in enumerate(self.product_image_urls):
                if url is not None:
                    check_url(url, f"product_image_urls[{i}]")

        if self.clean_product_image_urls:
            for i, url in enumerate(self.clean_product_image_urls):
                if url is not None:
                    check_url(url, f"clean_product_image_urls[{i}]")

        if self.reference_image_urls:
            for i, url in enumerate(self.reference_image_urls):
                if url is not None:
                    check_url(url, f"reference_image_urls[{i}]")

        if self.asset_urls:
            for i, item in enumerate(self.asset_urls):
                if isinstance(item, dict) and item.get("url") is not None:
                    check_url(item["url"], f"asset_urls[{i}].url")

        return self


class GenerateVideoResponse(BaseModel):
    """Response for POST /api/generate."""
    job_id: str
    status: str = "pending"
    message: str = "Job submitted successfully"
    event_cursor: int = 0  # SSE cursor — pass as ?after= to skip already-seen events
    queue_position: Optional[int] = None  # 1-based position in tenant queue (only when status="queued")
    active_jobs: Optional[int] = None  # current active (processing+pending) jobs for the tenant
    max_concurrent: Optional[int] = None  # tenant's max concurrent job limit
    warnings: Optional[List[Dict[str, str]]] = None


class GenerateMusicRequest(BaseModel):
    """Request body for POST /api/generate-music (standalone music generation)."""
    text_1: str = Field(default="", description="Headline / hook text")
    text_2: str = Field(default="", description="Body text")
    text_3: str = Field(default="", description="CTA / closing text")
    vo_script: str = Field(default="", description="Voiceover script (for mood)")
    language: str = Field(default="en", description="Language code")
    video_type: str = Field(default="influencer", description="'product video', 'influencer', or 'personal-brand'")
    music_description_override: Optional[str] = Field(default=None, description="User-edited description; skips LLM when set")


class GenerateMusicResponse(BaseModel):
    """Response for POST /api/generate-music."""
    music_description: str
    music_url: str


class GenerateSceneImageRequest(BaseModel):
    """Request body for POST /api/generate-scene-image (single scene image with optional correction)."""
    image_prompt: str = Field(..., description="Scene image prompt")
    correction_text: Optional[str] = Field(default=None, description="User correction; prepended to prompt for regeneration")
    image_to_fix_url: Optional[str] = Field(default=None, description="When set (Fix this image): send this image as reference to Nano Banana with correction_text as instructions")
    visual_style: str = Field(default="Auto", description="Visual style name")
    video_type: str = Field(default="influencer", description="Video type for provider resolution")
    image_api: str = Field(default="kie", description="'google', 'kie', 'kie-flash'")
    reference_image_urls: Optional[List[str]] = Field(default=None, description="Product or reference image URLs")
    character_reference_urls: Optional[List[str]] = Field(default=None, description="Character/influencer reference URLs")
    has_character: bool = Field(default=False, description="Whether character(s) appear in scene")
    product_description: Optional[str] = Field(default=None, description="Product or context description (e.g. text_1)")
    is_cta_scene: bool = Field(default=False, description="True if CTA/ending scene")
    logo_reference_url: Optional[str] = Field(default=None, description="Logo URL for CTA scene")


class GenerateSceneImageResponse(BaseModel):
    """Response for POST /api/generate-scene-image."""
    image_url: str


class AnimateSceneRequest(BaseModel):
    """Request body for POST /api/animate-scene (per-scene re-animate from Studio Step 13)."""
    job_id: str = Field(..., description="Job ID owning the scene")
    scene_index: int = Field(..., ge=0, description="0-based scene index in scene_images / scene_videos")
    motion_prompt: Optional[str] = Field(
        default=None,
        description="Camera/motion instructions for this scene; falls back to scene_prompts[idx].second_prompt when omitted.",
    )
    image_url: Optional[str] = Field(
        default=None,
        description="Image to animate; falls back to intermediates.scene_images[idx] when omitted.",
    )
    duration: Optional[float] = Field(
        default=None, gt=0, le=30,
        description="Scene duration in seconds; falls back to scene_prompts[idx].duration_seconds (default 5).",
    )


class AnimateSceneResponse(BaseModel):
    """Response for POST /api/animate-scene."""
    scene_index: int
    video_url: str


class GenerateCharacterRequest(BaseModel):
    """Request body for POST /api/generate-character (Studio: auto character before scene prompts)."""
    prompt: str = Field(default="", description="Main video prompt / product context")
    character_description: Optional[str] = Field(
        default=None,
        description="Optional appearance brief (age, outfit, setting). Merged ahead of prompt for portrait context.",
    )
    video_type: str = Field(default="influencer", description="product video | influencer | personal-brand | ugc-real")
    gender: str = Field(default="f", description="m or f")
    country: Optional[str] = Field(default=None, description="Target country for appearance")
    language: str = Field(default="en", description="Language code")
    visual_style: str = Field(default="Auto", description="Visual style from Studio")
    correction_text: Optional[str] = Field(
        default=None,
        description="User feedback when regenerating the character portrait",
    )

    @model_validator(mode="after")
    def require_prompt_or_character_brief(self):
        p = (self.prompt or "").strip()
        c = (self.character_description or "").strip()
        if not p and not c:
            raise ValueError("Provide a non-empty prompt and/or character_description")
        return self


class GenerateCharacterResponse(BaseModel):
    """Response for POST /api/generate-character."""
    image_url: Optional[str] = None
    description: Optional[str] = None
    portrait_image_prompt: Optional[str] = Field(
        default=None,
        description="Full image-generation prompt sent to Nano Banana; use for ElevenLabs voice design.",
    )


class SuggestCharacterBriefsRequest(BaseModel):
    """Request for AI character-look suggestions from the main video prompt (Studio step 3)."""
    prompt: str = Field(
        ...,
        min_length=3,
        description="Exact main video prompt from Studio step 3; server sends this text to Vertex Gemini for suggestions",
    )
    language: str = Field(default="en", description="Target language code (for cultural tone hints)")
    country: Optional[str] = Field(default=None, description="Target country if known")
    video_type: str = Field(
        default="influencer",
        description="product video | influencer | personal-brand | ugc-real — adjusts on-camera persona style",
    )
    gender: Optional[str] = Field(
        default=None,
        description="Wizard gender 'm' or 'f' — all suggestions must match (on-screen / voice gender)",
    )

    @field_validator("gender")
    @classmethod
    def normalize_suggest_gender(cls, v: Optional[str]) -> Optional[str]:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return None
        g = str(v).strip().lower()[:1]
        if g not in ("m", "f"):
            return None
        return g


class SuggestCharacterBriefsResponse(BaseModel):
    """Short character look lines the user can paste into Character look."""
    suggestions: List[str]


class CharacterRecord(BaseModel):
    """Character library record."""
    character_id: str
    user_id: str
    name: str
    source_type: str
    status: str = "active"
    tags: List[str] = []
    thumbnail: Optional[str] = None
    reference_images: List[str] = []
    voice_reference: Optional[str] = None
    default_language: Optional[str] = None
    preferred_formats: List[str] = []
    character_dna: Dict[str, Any] = {}
    style_json: Dict[str, Any] = {}
    voice_profile: Dict[str, Any] = {}
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_used_at: Optional[str] = None


class CreateCharacterRequest(BaseModel):
    """Request body for POST /api/characters."""
    name: str = Field(..., min_length=1, max_length=120)
    source_type: str = Field(default="uploaded", description="uploaded or generated")
    status: str = Field(default="active")
    tags: Optional[List[str]] = Field(default=None)
    thumbnail: Optional[str] = None
    reference_images: Optional[List[str]] = Field(default=None)
    voice_reference: Optional[str] = None
    default_language: Optional[str] = None
    preferred_formats: Optional[List[str]] = Field(default=None)
    character_dna: Optional[Dict[str, Any]] = Field(default=None)
    style_json: Optional[Dict[str, Any]] = Field(default=None)
    voice_profile: Optional[Dict[str, Any]] = Field(default=None)

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, v):
        vv = (v or "").strip().lower()
        if vv not in {"uploaded", "generated"}:
            raise ValueError("source_type must be 'uploaded' or 'generated'")
        return vv


class UpdateCharacterRequest(BaseModel):
    """Request body for PUT /api/characters/{character_id}."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    status: Optional[str] = None
    tags: Optional[List[str]] = None
    thumbnail: Optional[str] = None
    reference_images: Optional[List[str]] = None
    voice_reference: Optional[str] = None
    default_language: Optional[str] = None
    preferred_formats: Optional[List[str]] = None
    character_dna: Optional[Dict[str, Any]] = None
    style_json: Optional[Dict[str, Any]] = None
    voice_profile: Optional[Dict[str, Any]] = None
    last_used_at: Optional[str] = None


class VoiceOption(BaseModel):
    """Single voice option for GET /api/voices."""
    voice_id: str
    label: str


class VoiceDesignPreview(BaseModel):
    """Single preview returned by POST /api/voice-design."""
    generated_voice_id: str
    audio_base_64: str
    media_type: str
    duration_secs: float
    language: Optional[str] = None


class VoiceDesignRequest(BaseModel):
    """Request body for POST /api/voice-design."""
    language: str = Field(default="en", description="ISO 639-1 language code — used for voice description context")
    portrait_image_prompt: Optional[str] = Field(
        default=None,
        description="Nano Banana portrait prompt from generate-character; used only when character_description "
        "is empty and character_image_url is absent or Gemini describe fails (appearance slice only; rules stripped)",
    )
    character_description: Optional[str] = Field(
        default=None,
        description="Character look text (step 4) or library character_dna.character_brief — primary source for ElevenLabs",
    )
    character_image_url: Optional[str] = Field(
        default=None,
        description="Hosted influencer/portrait image URL; when character_description is empty the server uses Gemini (≤20 words)",
    )
    gender: str = Field(default="f", description="m or f — included in voice description context")
    seed: Optional[int] = Field(default=None, description="Random seed for reproducibility")
    loudness: float = Field(default=0.5, description="Volume level -1..1")
    guidance_scale: float = Field(default=5.0, description="How closely AI follows the prompt")
    auto_generate_text: bool = Field(default=True, description="Let ElevenLabs pick sample text for the preview")


class VoiceDesignResponse(BaseModel):
    """Response for POST /api/voice-design."""
    previews: List[VoiceDesignPreview]
    text: str
    voice_description: str


class VoiceSaveRequest(BaseModel):
    """Request body for POST /api/voice-save."""
    generated_voice_id: str = Field(..., description="Temporary generated_voice_id from /api/voice-design")
    voice_name: str = Field(default="Studio Custom Voice", description="Name to save the voice as in ElevenLabs library")
    voice_description: str = Field(default="", description="Optional description for the saved voice")


class VoiceSaveResponse(BaseModel):
    """Response for POST /api/voice-save."""
    voice_id: str
    voice_name: str


class GenerateVoRequest(BaseModel):
    """Request body for POST /api/generate-vo (standalone VO TTS via ElevenLabs)."""
    vo_script: str = Field(..., description="Voiceover script text")
    language: str = Field(default="en", description="ISO 639-1 language code")
    voice_id: str = Field(..., description="ElevenLabs voice ID")
    video_type: str = Field(default="influencer", description="'influencer' or 'personal-brand' use expressive TTS")
    job_id: Optional[str] = Field(default=None, description="Optional job ID for GCS path")
    with_word_timestamps: bool = Field(
        default=False,
        description="If true, use ElevenLabs /with-timestamps (slower; returns word segments). "
        "Studio preview defaults to false for faster TTS.",
    )


class GenerateVoResponse(BaseModel):
    """Response for POST /api/generate-vo."""
    vo_audio_url: str
    vo_duration: Optional[float] = None
    vo_word_segments: Optional[List[Dict[str, Any]]] = None


class PatchIntermediatesRequest(BaseModel):
    """Request body for PATCH /api/jobs/{id}/intermediates. Single key-value or batch."""
    key: Optional[str] = Field(default=None, description="Single intermediate key")
    value: Optional[Any] = Field(default=None, description="Value for key (when key is set)")
    intermediates: Optional[Dict[str, Any]] = Field(default=None, description="Batch: merge these keys into job intermediates")
    input_params_patch: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Shallow-merge these keys into job input_params (e.g. text_1–3 before UGC Real resume)",
    )

    @model_validator(mode="after")
    def require_key_or_intermediates(self):
        if self.key is not None:
            return self  # value may be None (clear key)
        if self.intermediates:
            return self
        if self.input_params_patch:
            return self
        raise ValueError("Provide either key (and optional value), intermediates, or input_params_patch")

    def to_intermediates_dict(self) -> Dict[str, Any]:
        """Return the intermediates dict to merge (single key or batch)."""
        if self.key is not None:
            return {self.key: self.value}
        return self.intermediates or {}


class PatchIntermediatesResponse(BaseModel):
    """Response for PATCH /api/jobs/{id}/intermediates."""
    ok: bool = True


class JobStatusResponse(BaseModel):
    """Response for GET /api/jobs/{job_id}."""
    id: str
    customer_id: Optional[str] = None
    status: str
    video_type: str
    progress: int = 0
    current_step: str = "queued"
    input_params: Dict[str, Any] = {}
    intermediates: Dict[str, Any] = {}
    output: Dict[str, Any] = {}
    error: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    retry_count: int = 0
    max_retries: int = get_default("max_retries", 3)
    failed_at_step: Optional[str] = None
    step_timings: List[Dict[str, Any]] = []
    cost_usd: Optional[float] = Field(default=None, description="Total generation cost in USD")
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    updated_at: Optional[str] = None


class JobListResponse(BaseModel):
    """Response for GET /api/jobs."""
    jobs: List[JobStatusResponse]
    total: int


class HealthResponse(BaseModel):
    """Response for GET /api/health."""
    status: str = "ok"
    services_initialized: bool = False
    active_jobs: int = 0


class ServiceStatus(BaseModel):
    """Status of a single external service."""
    name: str
    status: str  # "healthy", "unhealthy", "unknown"
    latency_ms: Optional[float] = None
    error: Optional[str] = None


class ServiceHealthResponse(BaseModel):
    """Response for GET /api/health/services."""
    overall: str = "healthy"  # "healthy", "degraded", "unhealthy"
    services: List[ServiceStatus] = []
    cached: bool = False
    checked_at: Optional[str] = None
