"""Minimal API-focused Config dataclass.

Extracted from Comp_Videos/video_scene_processor.py Config class.
Only includes fields used by the extracted service classes and ServiceRegistry.
All Google Sheets column names and unused fields are stripped.
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Configuration settings for the API pipeline services."""

    # Service account (GCS / Vertex AI)
    SERVICE_ACCOUNT_FILE: str = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")

    # API Keys
    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
    KIE_API_KEY: str = os.environ.get("KIE_API", "")
    RENDI_API_KEY: str = os.environ.get("RENDI_API_KEY", "")
    ELEVENLABS_API_KEY: str = os.environ.get("ELEVEN_LABS_API_KEY", "")
    GOOGLE_API_KEY: str = os.environ.get("GOOGLE_API_KEY", "")

    # Vertex AI Gemini API
    VERTEX_AI_PROJECT_ID: str = os.environ.get(
        "VERTEX_AI_PROJECT_ID",
        os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project"),
    )
    VERTEX_AI_LOCATION: str = os.environ.get("VERTEX_AI_LOCATION", "global")
    VERTEX_AI_API_KEY: str = os.environ.get("VERTEX_AI_API_KEY", "")
    VERTEX_AI_MODEL: str = os.environ.get("VERTEX_AI_MODEL", "gemini-2.5-flash")

    # Gemini Image Generation — Vertex AI REST API
    GEMINI_IMAGE_PROJECT_ID: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project")
    GEMINI_IMAGE_MAX_REFERENCE_IMAGES: int = 3
    GEMINI_IMAGE_MAX_CALLS_PER_MINUTE: int = 10
    GEMINI_IMAGE_RATE_LIMIT_DELAY: int = 12
    GEMINI_IMAGE_INITIAL_DELAY_SEC: int = 65

    # Model for PRODUCT images
    GEMINI_PRODUCT_IMAGE_MODEL: str = "gemini-3-pro-image-preview"
    GEMINI_PRODUCT_IMAGE_RATE_LIMIT_DELAY: int = 12
    GEMINI_PRODUCT_IMAGE_RETRY_DELAY: int = 20
    GEMINI_PRODUCT_IMAGE_MAX_RETRIES: int = 8

    # Model for SCENE images
    GEMINI_SCENE_IMAGE_MODEL: str = "gemini-3-pro-image-preview"
    GEMINI_SCENE_IMAGE_PARALLEL_WORKERS: int = 1
    GEMINI_SCENE_IMAGE_RATE_LIMIT_DELAY: int = 12
    SCENE_IMAGE_RETRY_WAIT_SEC: int = 20
    GEMINI_SCENE_IMAGE_RETRY_DELAY: int = 20
    GEMINI_SCENE_IMAGE_MAX_RETRIES: int = 8

    # Kie (Nano Banana) image gen parallel workers
    KIE_SCENE_IMAGE_PARALLEL_WORKERS: int = 6

    # Veo Video Generation
    VEO3_MODEL: str = "veo-3.0-generate-001"
    VEO31_FAST_MODEL: str = "veo-3.1-fast-generate-001"
    VEO3_PROJECT_ID: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project")
    SCENE_VIDEO_PARALLEL_WORKERS: int = 4
    SCENE_VIDEO_RATE_LIMIT_DELAY: int = 0
    VEO3_GENERATE_ENDPOINT: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models/{model}:predictLongRunning"
    VEO3_POLL_ENDPOINT: str = "https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models/{model}:fetchPredictOperation"
    VEO3_DEFAULT_RESOLUTION: str = "720p"
    VEO3_POLL_INTERVAL: int = 5
    VEO3_MAX_POLL_TIME: int = 600

    # GCS Storage Configuration
    GCS_UPLOAD_BUCKET_NAME: str = os.environ.get("GCS_UPLOAD_BUCKET_NAME", "automatiq")
    GCS_UPLOAD_FOLDER: str = "Comp/Final_Video/"
    GCS_UPLOAD_CREDENTIALS_FILE: str = os.environ.get("GCS_CREDENTIALS_FILE", "service_account.json")
    GCS_BUCKET_NAME: str = os.environ.get("GCS_BUCKET_NAME", "automatiq")
    GCS_FOLDER_NAME: str = os.environ.get("GCS_FOLDER_NAME", "articles2025")
    GCS_CREDENTIALS_FILE: str = os.environ.get("GCS_CREDENTIALS_FILE", "service_account.json")

    # API Endpoints
    KIE_BASE_URL: str = "https://api.kie.ai"
    RENDI_BASE_URL: str = "https://api.rendi.dev"
    RENDI_DISSOLVE_TRANSITION: str = os.environ.get("RENDI_DISSOLVE_TRANSITION", "dissolve")
    CONCAT_DISSOLVE_SECONDS: float = float(os.environ.get("CONCAT_DISSOLVE_SECONDS", "0.4"))
    VIDEO_CRF: int = int(os.environ.get("VIDEO_CRF", "23"))
    ELEVENLABS_BASE_URL: str = "https://api.elevenlabs.io/v1"

    # Processing settings
    MAX_SCENES: int = 20
    FRAMES_PER_SECOND: int = 3
    SCENE_BUFFER_SECONDS: float = 0.5
    SCENE_ALLOW_KB_FILLER: bool = False

    # PySceneDetect settings
    PYSCENEDETECT_THRESHOLD: float = 2.5
    PYSCENEDETECT_MIN_SCENE_DURATION: float = 1
    PYSCENEDETECT_MAX_SCENE_DURATION: float = 10
    PYSCENEDETECT_USE_ADAPTIVE: bool = True

    # ElevenLabs Voice IDs
    DEFAULT_VOICE_ID: str = "JBFqnCBsd6RMkjVDRZzb"
    DEFAULT_FEMALE_VOICE_ID: str = "EXAVITQu4vr4xnSDxMaL"
    ELEVENLABS_TTS_DELAY_BETWEEN_CALLS: float = 2.5
    ELEVENLABS_TTS_RATE_LIMIT_WAIT: int = 30

    # Gemini video analysis settings
    ENABLE_GEMINI_VIDEO_ANALYSIS: bool = True
    GEMINI_MODEL: str = "gemini-1.5-flash"
    GEMINI_VIDEO_ANALYSIS_MODEL: str = os.environ.get("GEMINI_VIDEO_ANALYSIS_MODEL", "gemini-2.5-flash")
    GEMINI_MAX_VIDEO_DURATION: int = 3600

    # Duration settings
    DEFAULT_VIDEO_DURATION: int = 30
    MIN_VIDEO_DURATION: int = 10
    MAX_VIDEO_DURATION: int = 120

    # ZapCap settings
    ZAPCAP_API_KEY: str = os.environ.get("ZAPCAP_API_KEY", "")
    ZAPCAP_BASE_URL: str = "https://api.zapcap.ai"
    ZAPCAP_TEMPLATE_ID: str = os.environ.get("ZAPCAP_TEMPLATE_ID", "your-zapcap-template-id")

    # Available visual styles
    STYLE_OPTIONS: tuple = (
        "Auto",
        "Modern flat 2d",
        "Minimal line art",
        "Futuristic isometric Tech Glow",
        "Modern semi flat 2d",
        "Cinematic photography",
        "Soft 3d clay",
        "isometric soft vector",
        "Paper Cut",
    )

    # Cultural / regional adaptation (initialized in __post_init__)
    REGION_MAPPING: dict = None
    CULTURAL_STYLES: dict = None
    HOOK_STYLES: dict = None

    def __post_init__(self):
        """Initialize complex dict fields after dataclass creation."""
        self.REGION_MAPPING = {
            'es': 'latam', 'pt': 'latam', 'pt-BR': 'latam',
            'de': 'western_europe', 'fr': 'western_europe', 'it': 'western_europe',
            'nl': 'western_europe', 'da': 'western_europe', 'sv': 'western_europe',
            'no': 'western_europe', 'fi': 'western_europe',
            'hu': 'eastern_europe', 'pl': 'eastern_europe', 'cs': 'eastern_europe',
            'sk': 'eastern_europe', 'ro': 'eastern_europe', 'bg': 'eastern_europe',
            'uk': 'eastern_europe', 'ru': 'eastern_europe',
            'zh': 'east_asia', 'ja': 'east_asia', 'ko': 'east_asia',
            'zh-TW': 'east_asia', 'zh-CN': 'east_asia',
            'th': 'southeast_asia', 'vi': 'southeast_asia', 'id': 'southeast_asia',
            'ms': 'southeast_asia', 'tl': 'southeast_asia',
            'ar': 'metap', 'tr': 'metap', 'he': 'metap', 'fa': 'metap',
            'hi': 'metap', 'ur': 'metap', 'bn': 'metap',
            'en': 'namer', 'en-US': 'namer', 'en-GB': 'western_europe',
            'en-AU': 'namer',
        }
        self.CULTURAL_STYLES = {
            'latam': {
                'ethnicity': 'Latin American/Hispanic features, warm skin tones, dark hair',
                'environment': 'vibrant colors, colonial architecture, tropical or urban Latin settings',
                'style': 'warm, family-oriented, emotional, passionate',
                'clothing': 'casual modern Latin American fashion, bright colors',
                'names': 'names like Sofia, Diego, Isabella, Carlos, Maria, Juan',
            },
            'western_europe': {
                'ethnicity': 'diverse Western European features, mix of skin tones',
                'environment': 'modern European cities, clean architecture, historic buildings',
                'style': 'professional, sophisticated, understated elegance',
                'clothing': 'smart casual European fashion, neutral and earth tones',
                'names': 'names like Emma, Liam, Sophie, Felix, Anna, Max',
            },
            'eastern_europe': {
                'ethnicity': 'Eastern European/Slavic features, fair to medium skin tones',
                'environment': 'Eastern European cities, mix of historic and Soviet-era architecture',
                'style': 'practical, direct, resilient, no-nonsense',
                'clothing': 'practical European fashion, darker colors, layered outfits',
                'names': 'names like Katya, Ivan, Marta, Pavel, Olga, Dmitri',
            },
            'east_asia': {
                'ethnicity': 'East Asian features, Chinese/Japanese/Korean appearance',
                'environment': 'modern Asian cities, blend of traditional and ultra-modern',
                'style': 'refined, tech-savvy, minimalist, respectful',
                'clothing': 'modern Asian fashion, clean lines, often monochromatic',
                'names': 'names like Wei, Yuki, Min-ji, Kenji, Mei, Hiroshi',
            },
            'southeast_asia': {
                'ethnicity': 'Southeast Asian features, warm skin tones',
                'environment': 'tropical settings, bustling markets, modern Asian cities',
                'style': 'friendly, community-oriented, vibrant',
                'clothing': 'light fabrics, bright colors, tropical-appropriate fashion',
                'names': 'names like Anh, Putri, Somchai, Maria, Budi, Linh',
            },
            'metap': {
                'ethnicity': 'Middle Eastern, South Asian, or African features as appropriate',
                'environment': 'diverse - from modern Gulf cities to traditional markets',
                'style': 'respectful, family-values, hospitable',
                'clothing': 'modest modern fashion, appropriate for the specific culture',
                'names': 'names like Ahmed, Fatima, Priya, Rahul, Amina, Yusuf',
            },
            'namer': {
                'ethnicity': 'diverse North American features, multicultural mix',
                'environment': 'American suburbs, modern offices, diverse urban settings',
                'style': 'confident, aspirational, diverse, inclusive',
                'clothing': 'casual American fashion, athleisure, diverse styles',
                'names': 'names like Jessica, Michael, Ashley, Brandon, Emily, Tyler',
            },
        }
        self.HOOK_STYLES = {
            'latam': 'emotional appeal, family benefits, passionate language, urgency',
            'western_europe': 'factual benefits, professional tone, quality focus',
            'eastern_europe': 'practical advantages, value proposition, direct approach',
            'east_asia': 'social proof, technology benefits, quality assurance',
            'southeast_asia': 'community benefits, friendly tone, accessible language',
            'metap': 'family benefits, trust-building, respectful tone',
            'namer': 'aspirational messaging, personal success, opportunity focus',
        }


config = Config()
