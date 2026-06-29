"""Configuration dataclass for the TVD X1 video pipeline."""

import os
import json
import logging
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline defaults loaded once from pipeline_defaults.json
# ---------------------------------------------------------------------------
_PIPELINE_DEFAULTS_PATH = Path(__file__).parent / "pipeline_defaults.json"
_PIPELINE_DEFAULTS: dict = {}

def _load_pipeline_defaults() -> dict:
    """Load pipeline defaults from pipeline_defaults.json (next to this file)."""
    global _PIPELINE_DEFAULTS
    if _PIPELINE_DEFAULTS:
        return _PIPELINE_DEFAULTS
    try:
        with open(_PIPELINE_DEFAULTS_PATH, "r", encoding="utf-8") as f:
            _PIPELINE_DEFAULTS = json.load(f)
        _logger.debug(f"Loaded pipeline defaults: {_PIPELINE_DEFAULTS}")
    except Exception as e:
        _logger.warning(f"Could not load pipeline_defaults.json: {e}")
        _PIPELINE_DEFAULTS = {
            "sync_method": "standard",
            "sync_strategy": "continuous",
            "add_subtitles": True,
            "dissolve_seconds": 0.075,
            "generate_vo": True,
            "last_scene_buffer": 1.0,
            "min_scene_duration": 1.0,
        }
    return _PIPELINE_DEFAULTS


def get_pipeline_defaults() -> dict:
    """Return pipeline defaults dict (cached after first load)."""
    return _load_pipeline_defaults()


def _resolve_service_account_default() -> str:
    """Resolve default path for service_account.json so API runs from api_pipeline/ find it in Comp_Videos/.

    Handles two layouts:
    - Local (Comp_Videos/tvd_pipeline/...): _comp_videos resolves to Comp_Videos/ directly.
    - Docker (tvd_pipeline mounted at /app/tvd_pipeline): _comp_videos resolves to /app, so
      service_account.json lives at /app/Comp_Videos/service_account.json (sibling of tvd_pipeline).
    """
    env_path = os.environ.get("SERVICE_ACCOUNT_FILE")
    if env_path:
        return env_path
    _config_dir = Path(__file__).resolve().parent  # tvd_pipeline/config
    _comp_videos = _config_dir.parent.parent       # Comp_Videos (local) or /app (Docker)
    _repo_root = _comp_videos.parent               # repo root
    for candidate in [
        _comp_videos / "service_account.json",
        _comp_videos / "Comp_Videos" / "service_account.json",  # Docker: /app/Comp_Videos/
        _repo_root / "api_pipeline" / "service_account.json",
    ]:
        if candidate.is_file():
            return str(candidate)
    return "service_account.json"


# Resolved once at import so all credential path defaults use the same path
_RESOLVED_SERVICE_ACCOUNT_FILE: str = _resolve_service_account_default()


@dataclass
class Config:
    """Configuration settings for the video processor."""

    # Google Sheets
    GOOGLE_SHEET_ID: str = ""
    GOOGLE_SHEET_TAB: str = "Sheet1"
    SERVICE_ACCOUNT_FILE: str = _RESOLVED_SERVICE_ACCOUNT_FILE

    # API Keys
    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
    KIE_API_KEY: str = os.environ.get("KIE_API", "")
    RENDI_API_KEY: str = os.environ.get("RENDI_API_KEY", "")
    ELEVENLABS_API_KEY: str = os.environ.get("ELEVEN_LABS_API_KEY", "")
    GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", "")  # For Gemini video analysis
    FAL_KEY: str = os.environ.get("FAL_KEY", "")

    # Vertex AI Gemini API (REST: project + location + Bearer token)
    VERTEX_AI_PROJECT_ID: str = os.environ.get("VERTEX_AI_PROJECT_ID", os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project"))
    VERTEX_AI_LOCATION: str = os.environ.get("VERTEX_AI_LOCATION", "global")
    VERTEX_AI_API_KEY: str = os.environ.get("VERTEX_AI_API_KEY", "")  # Bearer token for Vertex AI
    VERTEX_AI_MODEL: str = os.environ.get("VERTEX_AI_MODEL", "gemini-3-flash-preview")  # or gemini-2.5-flash for reasoning tasks

    # Gemini Image Generation - Vertex AI REST API
    # Two models: Pro for high-quality product images, Flash for fast scene images
    GEMINI_IMAGE_PROJECT_ID: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project")
    GEMINI_IMAGE_MAX_REFERENCE_IMAGES: int = 3  # Maximum reference images supported

    # --- Vertex Image Rate Limits (minimal concurrency to avoid 429) ---
    GEMINI_IMAGE_INITIAL_DELAY_SEC: int = int(os.environ.get("GEMINI_IMAGE_INITIAL_DELAY_SEC", "30"))

    # Model for PRODUCT images (gemini-3-pro-image-preview)
    GEMINI_PRODUCT_IMAGE_MODEL: str = "gemini-3-pro-image-preview"
    GEMINI_PRODUCT_IMAGE_RATE_LIMIT_DELAY: int = 3
    GEMINI_PRODUCT_IMAGE_RETRY_DELAY: int = 20  # Base retry on 429
    GEMINI_PRODUCT_IMAGE_MAX_RETRIES: int = 8

    # Scene images: 1 worker = no concurrent Gemini Image calls, avoids 429 (Vertex quota is strict)
    GEMINI_SCENE_IMAGE_MODEL: str = "gemini-3-pro-image-preview"
    GEMINI_SCENE_IMAGE_PARALLEL_WORKERS: int = int(os.environ.get("GEMINI_SCENE_IMAGE_PARALLEL_WORKERS", "1"))
    GEMINI_SCENE_IMAGE_RATE_LIMIT_DELAY: int = int(os.environ.get("GEMINI_SCENE_IMAGE_RATE_LIMIT_DELAY", "4"))

    # Gemini 3.1 Flash Image — moderate parallelism
    GEMINI_31_FLASH_IMAGE_MODEL: str = "gemini-3.1-flash-image-preview"

    # Nano Banana 2 (Vertex AI) — image model; set to actual model ID when published
    GEMINI_NANO_BANANA_2_IMAGE_MODEL: str = os.environ.get("GEMINI_NANO_BANANA_2_IMAGE_MODEL", "gemini-3.1-flash-image-preview")

    # Gemini 2.5 Flash Image (Vertex AI) — high quota (e.g. 3.4M RPM), allow many parallel requests
    GEMINI_25_FLASH_IMAGE_MODEL: str = os.environ.get("GEMINI_25_FLASH_IMAGE_MODEL", "gemini-2.5-flash-image")
    GEMINI_25_FLASH_IMAGE_PARALLEL_WORKERS: int = int(os.environ.get("GEMINI_25_FLASH_IMAGE_PARALLEL_WORKERS", "12"))
    GEMINI_25_FLASH_IMAGE_RATE_LIMIT_DELAY: int = int(os.environ.get("GEMINI_25_FLASH_IMAGE_RATE_LIMIT_DELAY", "0"))

    # Nano Banana 2 (Vertex) — tested OK at 10 parallel; 8 is safe headroom
    GEMINI_RATE_LIMITED_IMAGE_PARALLEL_WORKERS: int = int(os.environ.get("GEMINI_RATE_LIMITED_IMAGE_PARALLEL_WORKERS", "8"))
    GEMINI_31_FLASH_IMAGE_PARALLEL_WORKERS: int = int(os.environ.get("GEMINI_31_FLASH_IMAGE_PARALLEL_WORKERS", "4"))
    GEMINI_31_FLASH_IMAGE_RATE_LIMIT_DELAY: int = int(os.environ.get("GEMINI_31_FLASH_IMAGE_RATE_LIMIT_DELAY", "2"))
    SCENE_IMAGE_RETRY_WAIT_SEC: int = 20  # Wait before scene-level retry
    GEMINI_SCENE_IMAGE_RETRY_DELAY: int = 20  # Base retry on 429 (wait for quota reset)
    GEMINI_SCENE_IMAGE_MAX_RETRIES: int = 8  # More retries for reliability
    # Single-request timeout; under load or after 429 Vertex can be slow (default 8 min)
    GEMINI_IMAGE_REQUEST_TIMEOUT: int = int(os.environ.get("GEMINI_IMAGE_REQUEST_TIMEOUT", "480"))

    # Kie (Nano Banana) image gen — can handle a request every few seconds; run many in parallel
    KIE_SCENE_IMAGE_PARALLEL_WORKERS: int = int(os.environ.get("KIE_SCENE_IMAGE_PARALLEL_WORKERS", "12"))  # Up to 12 images at once

    # Veo Video Generation (Google's video generation models)
    VEO3_MODEL: str = "veo-3.0-generate-001"           # Veo 3.0 standard
    VEO3_FAST_MODEL: str = "veo-3.0-fast-generate-001"  # Veo 3.0 fast
    VEO31_MODEL: str = "veo-3.1-generate-001"           # Veo 3.1 full quality
    VEO31_FAST_MODEL: str = "veo-3.1-fast-generate-001"  # Veo 3.1 fast
    VEO31_LOCATION: str = "us-central1"  # Region for Veo 3.1 genai SDK
    VEO3_PROJECT_ID: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project")
    # --- Vertex Video Rate Limits (conservative for daily usage) ---
    SCENE_VIDEO_PARALLEL_WORKERS: int = int(os.environ.get("SCENE_VIDEO_PARALLEL_WORKERS", "5"))
    SCENE_VIDEO_RATE_LIMIT_DELAY: int = int(os.environ.get("SCENE_VIDEO_RATE_LIMIT_DELAY", "2"))
    VEO3_GENERATE_ENDPOINT: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models/{model}:predictLongRunning"
    VEO3_POLL_ENDPOINT: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models/{model}:fetchPredictOperation"
    VEO3_DEFAULT_RESOLUTION: str = "720p"  # 720p or 1080p
    VEO3_POLL_INTERVAL: int = 3  # Seconds between status checks (adaptive polling starts at 3s, grows to 8s after 30s)
    VEO3_MAX_POLL_TIME: int = 600  # Maximum seconds to wait for video generation (10 minutes)

    # GCS Storage Configuration (unified bucket for all uploads)
    GCS_UPLOAD_BUCKET_NAME: str = os.environ.get("GCS_UPLOAD_BUCKET_NAME", "automatiq")
    GCS_UPLOAD_FOLDER: str = "Comp/Final_Video/"
    GCS_UPLOAD_CREDENTIALS_FILE: str = os.environ.get("GCS_CREDENTIALS_FILE", _RESOLVED_SERVICE_ACCOUNT_FILE)

    # API Endpoints
    KIE_BASE_URL: str = "https://api.kie.ai"
    RENDI_BASE_URL: str = "https://api.rendi.dev"
    RENDI_DISSOLVE_TRANSITION: str = os.environ.get("RENDI_DISSOLVE_TRANSITION", "dissolve")  # xfade type: dissolve, fade, wipeleft, radial, etc.
    RENDI_STRETCH_PARALLEL_WORKERS: int = int(os.environ.get("RENDI_STRETCH_PARALLEL_WORKERS", "4"))  # Parallel workers for VO-sync stretch (probe + slow_motion)
    # Faster Rendi: veryfast/superfast = quicker encode (slightly larger file). fast = default balance.
    RENDI_X264_PRESET: str = os.environ.get("RENDI_X264_PRESET", "fast")  # fast | veryfast | superfast | ultrafast
    # H.264 profile/pixel format for mobile in-app players (WhatsApp expects MP4 + AVC + yuv420p + AAC in practice).
    RENDI_X264_PROFILE: str = os.environ.get("RENDI_X264_PROFILE", "main")  # main | baseline | high
    RENDI_X264_LEVEL: str = os.environ.get("RENDI_X264_LEVEL", "")  # e.g. 4.2 — empty lets libx264 pick level from resolution
    # Re-encode ZapCap downloads so unknown codecs/containers from the subtitle service still play on phones.
    RENDI_TRANSCODE_AFTER_ZAPCAP: bool = os.environ.get("RENDI_TRANSCODE_AFTER_ZAPCAP", "true").lower() in ("1", "true", "yes")
    RENDI_POLL_INTERVAL: int = int(os.environ.get("RENDI_POLL_INTERVAL", "5"))  # Seconds between status checks (5 = faster completion detection)
    CONCAT_DISSOLVE_SECONDS: float = float(os.environ.get("CONCAT_DISSOLVE_SECONDS", "0.25"))  # Transition length; lower = tighter VO–visual sync (0.2–0.4)
    VIDEO_CRF: int = int(os.environ.get("VIDEO_CRF", "23"))  # H.264 quality: 18=near-lossless/huge, 23=excellent/small, 26=good for social media. Default 23.
    ELEVENLABS_BASE_URL: str = "https://api.elevenlabs.io/v1"

    # Processing settings (match sheet columns: Scene 1-20 First/Second prompt, new image, new video)
    MAX_SCENES: int = 20
    FRAMES_PER_SECOND: int = 3  # Frames to extract per second of scene duration
    SCENE_BUFFER_SECONDS: float = 0.5  # Breathing room: animation continues without VO at end of each scene (except last). Set 0 to disable.
    # When a clip is shorter than the VO segment: prefer slow-motion to extend; do not add static/Ken Burns filler (looks bad).
    SCENE_ALLOW_KB_FILLER: bool = False  # If True, allow Ken Burns filler when clip is short (legacy). If False, use slow-motion only; no static filler.

    # PySceneDetect settings (more accurate than FFmpeg)
    # Threshold: 20-35 typical, higher = less sensitive, lower = more sensitive
    # For videos with ~8 scenes in ~16 seconds, try 25-30
    PYSCENEDETECT_THRESHOLD: float = 2.5
    PYSCENEDETECT_MIN_SCENE_DURATION: float = 1  # Minimum scene length in seconds
    PYSCENEDETECT_MAX_SCENE_DURATION: float = 10  # Maximum scene length in seconds (will be split if longer)
    PYSCENEDETECT_USE_ADAPTIVE: bool = True  # AdaptiveDetector adjusts to video content

    # VO words-per-second for duration estimation (TODO: per-language table e.g. Hebrew ~2.2, Spanish ~3.0)
    DEFAULT_VO_WORDS_PER_SECOND: float = 2.5
    # ElevenLabs Voice ID (default)
    DEFAULT_VOICE_ID: str = "JBFqnCBsd6RMkjVDRZzb"
    DEFAULT_FEMALE_VOICE_ID: str = "EXAVITQu4vr4xnSDxMaL"  # Sarah voice for influencer mode
    # Delay (seconds) between consecutive ElevenLabs TTS calls to avoid 429 rate limit
    ELEVENLABS_TTS_DELAY_BETWEEN_CALLS: float = 2.5
    # Wait (seconds) when ElevenLabs returns 429 before retry
    ELEVENLABS_TTS_RATE_LIMIT_WAIT: int = 30

    # Influencer Mode settings
    DEFAULT_INFLUENCER_SCENES: int = 6  # Default number of scenes when Time column is empty
    INFLUENCER_SCENE_DURATION: float = 5.0  # Duration of each scene in seconds

    # Column mappings
    INPUT_VIDEO_COLUMN: str = "Input Videos"
    MANUAL_INSTRUCTIONS_COLUMN: str = "Manual instructions"

    # CTA Button columns
    ADD_CTA_BUTTON_COLUMN: str = "Add CTA button"
    CTA_TEXT_COLUMN: str = "CTA Text"
    CTA_DURATION_COLUMN: str = "CTA Duration"  # "Whole Video" or "At the End"

    # Opening Text columns
    ADD_OPENING_TEXT_COLUMN: str = "Opening Text?"
    OPENING_TEXT_COLUMN: str = "Opening Text"

    # Subtitles column
    ADD_SUBTITLES_COLUMN: str = "Add subtitles"

    # Article adaptation columns (optional)
    ARTICLE_COLUMN: str = "Article"
    VERTICAL_COLUMN: str = "Vertical"

    # Article-Video relationship column
    # "Yes" = Article is similar to video content, adapt video for new offer/language
    # "No" = Article is fundamentally different, keep video style but create new content
    ARTICLE_RELATED_TO_VIDEO_COLUMN: str = "Article related to Video"

    # Language column (for ZapCap subtitles language and VO)
    LANGUAGE_COLUMN: str = "Language"

    # Country column (for influencer ethnicity and cultural adaptation)
    COUNTRY_COLUMN: str = "Country"

    # Manual override columns (optional)
    MANUAL_VO_TEXT_COLUMN: str = "Manual text for VO"
    MANUAL_MUSIC_LINK_COLUMN: str = "Manual music link"
    FREE_TEXT_COLUMN: str = "Free text"  # Overrides Title, 1stP, Rest of Content if provided

    # Influencer Mode columns (used when Input Videos is empty)
    IMAGE_1_COLUMN: str = "Image 1"
    IMAGE_2_COLUMN: str = "Image 2"
    IMAGE_3_COLUMN: str = "Image 3"
    IMAGE_4_COLUMN: str = "Image 4"
    IMAGE_5_COLUMN: str = "Image 5"

    # Asset columns (images/videos to insert as-is without editing)
    ASSET_1_COLUMN: str = "Asset 1"
    ASSET_2_COLUMN: str = "Asset 2"
    ASSET_3_COLUMN: str = "Asset 3"

    TIME_COLUMN: str = "Time"  # Number of scenes to generate (default 6)
    VOICE_ID_COLUMN: str = "Voice id"  # Custom ElevenLabs voice ID (optional)
    IMAGE_API_COLUMN: str = "Image api"  # "Google" = Vertex Gemini for images; "kie" = Kie (Nano Banana)

    # Article data columns (populated from GCS when Article contains a URL)
    TITLE_COLUMN: str = "Title"
    FIRST_PARAGRAPH_COLUMN: str = "1stP"
    REST_CONTENT_COLUMN: str = "Rest of Content"

    # GCS Configuration (for fetching article data from URLs)
    GCS_CREDENTIALS_FILE: str = os.environ.get("GCS_CREDENTIALS_FILE", _RESOLVED_SERVICE_ACCOUNT_FILE)
    GCS_BUCKET_NAME: str = os.environ.get("GCS_BUCKET_NAME", "automatiq")
    GCS_FOLDER_NAME: str = os.environ.get("GCS_FOLDER_NAME", "articles2025")

    # ZapCap settings
    ZAPCAP_API_KEY: str = os.environ.get("ZAPCAP_API_KEY", "")
    ZAPCAP_BASE_URL: str = "https://api.zapcap.ai"
    ZAPCAP_TEMPLATE_ID: str = os.environ.get("ZAPCAP_TEMPLATE_ID", "your-zapcap-template-id")

    # ==========================================================================
    # GEMINI VIDEO ANALYSIS SETTINGS
    # ==========================================================================
    ENABLE_GEMINI_VIDEO_ANALYSIS: bool = True  # Use Gemini for comprehensive video analysis
    GEMINI_MODEL: str = "gemini-1.5-flash"     # Model to use (flash is faster/cheaper, pro is more detailed)
    # Model for reference-video structure analysis (must support video input in Vertex AI).
    # Vertex video-understanding supported: gemini-3-flash-preview, gemini-2.5-flash, gemini-2.0-flash, etc.
    GEMINI_VIDEO_ANALYSIS_MODEL: str = os.environ.get("GEMINI_VIDEO_ANALYSIS_MODEL", "gemini-3-flash-preview")
    GEMINI_MAX_VIDEO_DURATION: int = 3600      # Max video duration in seconds (1 hour)

    # ==========================================================================
    # PRODUCT DETECTION SETTINGS
    # ==========================================================================
    ENABLE_PRODUCT_DETECTION: bool = True  # Feature flag to enable/disable product detection
    PRODUCT_MIN_CONFIDENCE: float = 0.7    # Minimum confidence score (0-1) to consider product detected
    PRODUCT_DETECTION_FRAMES: int = 60     # Number of frames to analyze for comprehensive video understanding
    PRODUCT_REFERENCE_FOLDER: str = "product_references"  # GCS folder for reference images

    # Output columns (Scene 1-8)
    SCENE_FIRST_PROMPT_PREFIX: str = "Scene {n} - First prompt"
    SCENE_SECOND_PROMPT_PREFIX: str = "Scene {n} - Second prompt"
    SCENE_NEW_IMAGE_PREFIX: str = "Scene {n} - new image"
    SCENE_NEW_VIDEO_PREFIX: str = "Scene {n} - new video"
    RENDI_SCENE_COLUMN: str = "RENDI Scene"
    NEW_VOICE_COLUMN: str = "New Voice"
    NEW_MUSIC_COLUMN: str = "New music"
    RENDI_SCENE_VOICE_COLUMN: str = "RENDI Scene & Voice"
    SUBTITLED_VIDEO_COLUMN: str = "Subtitled Video"
    FINAL_VIDEO_COLUMN: str = "Final Video"

    # Gender detection column (m for male, f for female)
    GENDER_COLUMN: str = "Gender"

    # Animation model column - "runway" (default) or "kling"
    ANIMATION_MODEL_COLUMN: str = "Animation model"

    # Product detection output columns (ENHANCED)
    PRODUCT_DETECTED_COLUMN: str = "Product Detected"
    PRODUCT_REFERENCE_COLUMN: str = "Product Reference"
    PRODUCT_CONFIDENCE_COLUMN: str = "Product Confidence"
    PRODUCT_PURPOSE_COLUMN: str = "Product Purpose"  # What the product does
    PRODUCT_USAGE_COLUMN: str = "Product Usage"      # How it's applied/used
    PRODUCT_CONTEXTS_COLUMN: str = "Usage Contexts"  # How it appears in different scenes

    # ==========================================================================
    # NEW VIDEO TYPE WORKFLOW COLUMNS
    # ==========================================================================
    # Video type determines the processing workflow
    VIDEO_TYPE_COLUMN: str = "Video type"
    PROMPT_COLUMN: str = "Prompt"
    STYLE_COLUMN: str = "Style"  # Visual style for image generation
    DURATION_COLUMN: str = "Duration"  # Target video duration in seconds (10-40)
    CHARACTER_COLUMN: str = "Character"  # Optional character image URL for scenes
    CHARACTER_EXTRA_COLUMNS: tuple = ("Character 2", "Character 3")  # Additional character columns (multiple people)
    LOGO_COLUMN: str = "Logo"  # Optional logo URL for ending scene
    SLOGAN_COLUMN: str = "Slogan"  # Optional slogan text for ending scene (if empty, will be generated)
    VIDEO_REFERENCE_COLUMN: str = "Video reference"  # Optional URL: Gemini analyzes video structure for product video pipeline

    # Duration settings
    DEFAULT_VIDEO_DURATION: int = 30  # Default duration if not specified
    MIN_VIDEO_DURATION: int = 10
    MAX_VIDEO_DURATION: int = 120  # Support up to 2 minutes

    # Available visual styles (Auto = default behavior, specific style = apply to all images)
    STYLE_OPTIONS: tuple = (
        "Auto",
        "Modern flat 2d",
        "Minimal line art",
        "Futuristic isometric Tech Glow",
        "Modern semi flat 2d",
        "Cinematic photography",
        "Soft 3d clay",
        "isometric soft vector",
        "Paper Cut"
    )

    # Output columns for parsed prompt (populated by Gemini)
    TEXT_1_COLUMN: str = "TEXT 1"  # What is the video about
    TEXT_2_COLUMN: str = "TEXT 2"  # What is the goal of the video
    TEXT_3_COLUMN: str = "TEXT 3"  # Content and style requirements
    TEXT_4_COLUMN: str = "TEXT 4"  # Video structure/scenes

    # Product reference images for video generation
    PRODUCT_IMAGE_1_COLUMN: str = "image 1"
    PRODUCT_IMAGE_2_COLUMN: str = "image 2"
    PRODUCT_IMAGE_3_COLUMN: str = "image 3"
    PRODUCT_IMAGE_4_COLUMN: str = "image 4"
    PRODUCT_IMAGE_5_COLUMN: str = "image 5"

    # Product video pipeline output columns
    CLEAN_PRODUCT_IMAGE_COLUMN: str = "Clean Product image"  # Column where clean product image URL is written
    SCENE_PROMPTS_COLUMN: str = "Scene Prompts"
    SCENE_VIDEOS_COLUMN: str = "Scene Videos"
    GENERATED_MUSIC_COLUMN: str = "Generated Music"
    VO_SCRIPT_COLUMN: str = "VO"
    VO_AUDIO_COLUMN: str = "VO Audio"

    # Valid video types (influencer/personal brand use same UGC pipeline with different subtype)
    VIDEO_TYPES: tuple = ("product video", "influencer", "personal-brand", "UGC-style video", "personal-service")

    # ==========================================================================
    # CULTURAL AND REGIONAL ADAPTATION SETTINGS
    # ==========================================================================

    # Map language codes to cultural regions
    REGION_MAPPING: dict = None  # Will be initialized in __post_init__

    # Cultural styles for each region (used in image/video generation prompts)
    CULTURAL_STYLES: dict = None  # Will be initialized in __post_init__

    # Hook styles for opening text by region
    HOOK_STYLES: dict = None  # Will be initialized in __post_init__

    def __post_init__(self):
        """Initialize complex dict fields after dataclass creation.

        Cultural/regional data is loaded from data_maps.json via the data_loader
        module so that it is maintained in a single place.
        """
        from tvd_pipeline.data_loader import (
            get_region_mapping,
            get_cultural_styles,
            get_hook_styles,
        )

        self.REGION_MAPPING = get_region_mapping()
        self.CULTURAL_STYLES = get_cultural_styles()
        self.HOOK_STYLES = get_hook_styles()
