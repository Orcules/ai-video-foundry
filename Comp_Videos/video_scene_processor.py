#!/usr/bin/env python3
"""Video Scene Processor - TVD X1 Pipeline.

Thin orchestrator that wires up services from tvd_pipeline and delegates
all pipeline logic to extracted modules. See tvd_pipeline/ for real code.
"""

import os
import json
import time
import logging
from typing import Dict, Any, List, Optional
import threading

from dotenv import load_dotenv

# Load .env before any config import (Config reads os.environ at class definition time)
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, ".env"))
load_dotenv()

# Service classes from tvd_pipeline (used by __init__ to wire up services)
from tvd_pipeline.config import Config
from tvd_pipeline.services.google_sheets import GoogleSheetsService, NoOpSheetsService
from tvd_pipeline.services.gemini_text import GeminiService
from tvd_pipeline.services.gemini_image import GeminiImageService
from tvd_pipeline.services.veo3 import Veo3Service, VeoRAIBlockedError, VeoPromptBlockedError
from tvd_pipeline.services.openai_service import OpenAIService
from tvd_pipeline.services.kie import KieAIService
from tvd_pipeline.services.rendi import RendiService
from tvd_pipeline.services.elevenlabs import ElevenLabsService
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.zapcap import ZapCapService
from tvd_pipeline.services.suno_music import SunoMusicService
from tvd_pipeline.services.gcs_video_upload import GCSVideoUploadService
from tvd_pipeline.services.gcs_article import GCSArticleService

# Pipeline functions from tvd_pipeline (extracted pipeline logic)
from tvd_pipeline.pipelines.product import process_product_video as _product_pipeline
from tvd_pipeline.pipelines.ugc import (
    process_ugc_video as _ugc_pipeline,
    _generate_influencer_image as _ugc_generate_influencer_image,
    _get_cultural_info_for_language as _ugc_get_cultural_info,
    _pick_reference_image_index_for_scene as _ugc_pick_ref_image,
    _analyze_reference_images as _ugc_analyze_ref_images,
    _insert_asset_as_scene as _ugc_insert_asset,
)
from tvd_pipeline.pipelines.ugc_real import process_ugc_real_video as _ugc_real_pipeline
from tvd_pipeline.pipelines.custom import process_custom_video as _custom_pipeline
from tvd_pipeline.pipelines._helpers import (
    _presplit_vo_into_scenes as _h_presplit_vo_into_scenes,
    _presplit_vo_at_sentences as _h_presplit_vo_at_sentences,
    _generate_vo_script_single as _h_generate_vo_script_single,
    _estimate_scene_count_from_text4 as _h_estimate_scene_count,
    _evaluate_image_quality as _h_evaluate_image_quality,
    _tpad_video as _h_tpad_video,
)
from tvd_pipeline.pipelines._sheet_helpers import (
    run_rendi_zapcap_only as _sh_run_rendi_zapcap_only,
    run_rendi_voice_subtitles_for_row as _sh_run_rendi_voice_subtitles_for_row,
    add_subtitles_to_row_from_rendi_voice as _sh_add_subtitles_to_row_from_rendi_voice,
    process_influencer_row as _sh_process_influencer_row,
    _process_influencer_scene as _sh_process_influencer_scene,
)
from tvd_pipeline.pipelines.legacy import (
    process_single_video as _legacy_process_single_video,
    _process_single_scene as _legacy_process_single_scene,
    _process_cta_button as _legacy_process_cta_button,
)
from tvd_pipeline.pipelines._sheet_orchestrator import process_all_videos as _sheet_process_all_videos

# Utility functions from tvd_pipeline (were formerly top-level in monolith)
from tvd_pipeline.utils import (
    parse_character_urls,
    is_valid_voice_id,
    get_validated_voice_id,
    detect_language,
    script_only_for_tts,
    _word_count_for_duration,
    _normalize_animation_model_value,
    snap_duration,
)

# Load environment variables
load_dotenv()

# Configure logging with immediate flush for real-time output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Flushes immediately to console
    ]
)
logger = logging.getLogger(__name__)
# Ensure immediate flush
for handler in logging.root.handlers:
    handler.flush()


config = Config()


def _merge_reference_image_urls_into_scene_kw(scene_image_kw: Dict[str, Any]) -> None:
    """Pop API ``reference_image_urls`` and merge into product/character ref lists.

    The wrapper passes ``reference_image_urls``; older ``KieAIService.generate_scene_image`` (and
    ``GeminiImageService.generate_scene_image``) signatures omitted this name, causing
    ``unexpected keyword argument 'reference_image_urls'``. Refs are preserved via
    ``product_reference_urls`` / ``character_reference_urls`` / ``product_visible``.
    """
    extra = scene_image_kw.pop("reference_image_urls", None)
    if not extra:
        return
    urls = [
        u.strip()
        for u in (extra if isinstance(extra, (list, tuple)) else [extra])
        if u and isinstance(u, str) and u.strip()
    ]
    if not urls:
        return

    def _extend_list(key: str) -> None:
        cur = scene_image_kw.get(key)
        merged: List[str] = []
        if isinstance(cur, (list, tuple)):
            merged.extend(str(x).strip() for x in cur if x and str(x).strip())
        elif cur:
            merged.append(str(cur).strip())
        for u in urls:
            if u not in merged:
                merged.append(u)
        scene_image_kw[key] = merged or None

    if scene_image_kw.get("product_visible", True):
        _extend_list("product_reference_urls")
        return
    if scene_image_kw.get("has_character"):
        _extend_list("character_reference_urls")
        return
    scene_image_kw["product_reference_urls"] = urls
    scene_image_kw["product_visible"] = True


class VideoSceneProcessor:
    """Main processor for the video scene processing pipeline."""

    # Maps Sheet "Animation model" column values to (video_model, video_provider)
    SHEET_ANIMATION_MAP = {
        "veo 3.1 fast (vertex ai)":   ("veo-3.1-fast", "direct"),
        "kling 2.5 (kie.ai)":         ("kling-2.5", "kie"),
        "kling 2.6 (kie.ai)":         ("kling-2.6", "kie"),
        "runway gen4 turbo (kie.ai)":  ("runway", "kie"),
        "none":                        ("none", None),
        # Legacy aliases (backward compat)
        "google":   ("veo-3.1-fast", "direct"),
        "google31": ("veo-3.1-fast", "direct"),
        "google veo 3.1": ("veo-3.1-fast", "direct"),
        "kling":    ("kling-2.5", "kie"),
        "kling 2.5": ("kling-2.5", "kie"),
        "runway":   ("runway", "kie"),
        "runway gen4": ("runway", "kie"),
    }

    # Sheet convenience aliases — operators type varied names in the "Image API" column.
    # Multiple aliases map to the same (model, provider) pair intentionally.
    SHEET_IMAGE_API_MAP = {
        "nano banana pro (kie.ai)":   ("nano-banana-pro", "kie"),
        "gemini 3 pro (vertex ai)":   ("gemini-3-pro-image", "direct"),
        "gemini 3.1 flash (vertex ai)": ("gemini-3.1-flash-image-preview", "direct"),
        "gemini 2.5 pro (vertex ai)": ("gemini-25-flash-image", "direct"),
        "gemini 2.5 flash (vertex ai)": ("gemini-25-flash-image", "direct"),
        "nano banana 2 (vertex ai)":   ("nano-banana-2", "direct"),
        "gemini 3 flash (kie.ai)":    ("gemini-3-flash", "kie"),
        # Legacy aliases (backward compat)
        "gemini 3.1 flash": ("gemini-3.1-flash-image-preview", "direct"),
        "google":       ("gemini-3-pro-image", "direct"),
        "gemini pro (vertex)": ("gemini-3-pro-image", "direct"),
        "nano banana pro": ("nano-banana-pro", "kie"),
        "gemini 2.5 pro": ("gemini-25-flash-image", "direct"),
        "nano banana 2": ("nano-banana-2", "direct"),
        "kie flash":    ("gemini-3-flash", "kie"),
        "kie-flash":    ("gemini-3-flash", "kie"),
        "flash":        ("gemini-3-flash", "kie"),
        "gemini flash": ("gemini-3-flash", "kie"),
        "gemini-flash": ("gemini-3-flash", "kie"),
        "kie":          ("nano-banana-pro", "kie"),
        "":             ("nano-banana-pro", "kie"),
    }

    def __init__(self):
        """Initialize the video scene processor with all services."""
        # Validate required API keys
        self._validate_config()

        # Runtime overrides set by pipeline methods (API mode)
        self._text_model = None
        self._text_provider = None

        # Usage accumulator — _call_llm() accumulates token counts here so
        # pipeline code can read them after calling task functions.
        self._usage_accumulator = {"input_tokens": 0, "output_tokens": 0}
        self._usage_entries = []  # per-call entries: {step_key, model, provider, input_tokens, output_tokens}
        self._usage_lock = threading.Lock()

        # LLM debug logging — when set, _call_llm saves input/output to JSON files
        self._llm_log_dir = None
        self._llm_logger = None  # lazy LLMLogger instance (created via property)

        # Thread lock for Google Sheets updates (prevents race conditions in parallel mode)
        self._sheets_lock = threading.Lock()
        
        # Load per-step model config for _call_llm() dispatch
        try:
            _models_json_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "tvd_pipeline", "config", "models.json",
            )
            with open(_models_json_path, "r", encoding="utf-8") as f:
                self.model_config = json.load(f)
        except Exception:
            self.model_config = {"text_defaults": {}}

        # Initialize services (Sheets optional — use no-op when running from API/UI without credentials)
        try:
            self.sheets_service = GoogleSheetsService(config.SERVICE_ACCOUNT_FILE)
        except Exception as e:
            logger.warning("Sheets unavailable (API/UI mode, no credentials). Sheet writes disabled: %s", e)
            self.sheets_service = NoOpSheetsService()
        self.openai_service = OpenAIService(config.OPENAI_API_KEY)
        
        # Initialize GCS Storage service (unified storage for all uploads)
        self.gcs_storage_service = GCSStorageService(
            credentials_file=config.GCS_UPLOAD_CREDENTIALS_FILE,
            bucket_name=config.GCS_UPLOAD_BUCKET_NAME,
            folder_path=config.GCS_UPLOAD_FOLDER
        )
        
        # Pass GCS storage service to KieAIService for CTA button uploads
        self.kie_service = KieAIService(config.KIE_API_KEY, gcs_storage_service=self.gcs_storage_service)
        self.rendi_service = RendiService(config.RENDI_API_KEY)
        
        # Gemini service for native video analysis (via Kie.ai - uses same API key)
        if config.ENABLE_GEMINI_VIDEO_ANALYSIS and config.KIE_API_KEY:
            self.gemini_service = GeminiService(config.KIE_API_KEY, gcs_storage_service=self.gcs_storage_service)
        else:
            self.gemini_service = None
            if config.ENABLE_GEMINI_VIDEO_ANALYSIS:
                logger.warning("⚠️ Gemini video analysis enabled but KIE_API_KEY not set")
        
        # Pass OpenAI client to ElevenLabs for speech detection
        self.elevenlabs_service = ElevenLabsService(
            config.ELEVENLABS_API_KEY,
            openai_client=self.openai_service.client
        )
        
        # ZapCap service for subtitles (optional - only if API key is set)
        if config.ZAPCAP_API_KEY:
            self.zapcap_service = ZapCapService(
                api_key=config.ZAPCAP_API_KEY,
                template_id=config.ZAPCAP_TEMPLATE_ID
            )
            logger.info("   ✅ ZapCap service initialized")
        else:
            self.zapcap_service = None
            logger.info("   ⚠️ ZapCap service not available (no API key)")
        
        # Suno Music service for music generation (uses Kie.ai API key)
        self.suno_service = SunoMusicService(
            api_key=config.KIE_API_KEY,
            openai_client=self.openai_service.client
        )
        
        # Gemini Image service for image generation (replaces Kie.ai Nano Banana)
        self.gemini_image_service = GeminiImageService(
            gcs_storage_service=self.gcs_storage_service
        )
        
        # Veo 3.0 service for video generation (Google's video model)
        self.veo3_service = Veo3Service(
            gcs_storage_service=self.gcs_storage_service,
            model=config.VEO3_MODEL
        )
        # Veo 3.0 Fast service
        self.veo3_fast_service = Veo3Service(
            gcs_storage_service=self.gcs_storage_service,
            model=config.VEO3_FAST_MODEL
        )
        # Veo 3.1 service (full quality, slower)
        self.veo31_service = Veo3Service(
            gcs_storage_service=self.gcs_storage_service,
            model=config.VEO31_MODEL
        )
        # Veo 3.1 Fast service (faster generation, GA)
        self.veo31_fast_service = Veo3Service(
            gcs_storage_service=self.gcs_storage_service,
            model=config.VEO31_FAST_MODEL
        )
        
        # GCS Article service for fetching article data from URLs
        self.gcs_article_service = GCSArticleService(
            credentials_file=config.GCS_CREDENTIALS_FILE,
            bucket_name=config.GCS_BUCKET_NAME,
            folder_name=config.GCS_FOLDER_NAME
        )
        
        # GCS Video Upload service for final influencer videos
        self.gcs_video_service = GCSVideoUploadService(
            credentials_file=config.GCS_UPLOAD_CREDENTIALS_FILE,
            bucket_name=config.GCS_UPLOAD_BUCKET_NAME
        )

        # Runway Direct (Gen4 Turbo + Gen 4.5) — optional, requires RUNWAYML_API_SECRET
        runway_key = os.environ.get("RUNWAYML_API_SECRET", "")
        if runway_key:
            try:
                from tvd_pipeline.services.runway_direct import RunwayDirectService
                self.runway_direct_service = RunwayDirectService(
                    api_key=runway_key, gcs_storage_service=self.gcs_storage_service
                )
                logger.info("   ✅ Runway Direct service initialized")
            except Exception as e:
                self.runway_direct_service = None
                logger.warning(f"   ⚠️ Runway Direct service failed to initialize: {e}")
        else:
            self.runway_direct_service = None
            logger.info("   ⚠️ Runway Direct service not available (no RUNWAYML_API_SECRET)")

        # fal.ai Video — optional, requires FAL_KEY
        fal_key = os.environ.get("FAL_KEY", "")
        if fal_key:
            try:
                from tvd_pipeline.services.fal_video import FalVideoService
                self.fal_service = FalVideoService(
                    api_key=fal_key, gcs_storage_service=self.gcs_storage_service
                )
                logger.info("   OK fal.ai Video service initialized")
            except Exception as e:
                self.fal_service = None
                logger.warning(f"   fal.ai Video service failed to initialize: {e}")
        else:
            self.fal_service = None
            logger.info("   fal.ai Video service not available (no FAL_KEY)")

        # Vercel AI Hub — optional, requires VERCEL_AI_HUB_KEY
        vercel_key = os.environ.get("VERCEL_AI_HUB_KEY", "")
        if vercel_key:
            try:
                from tvd_pipeline.services.vercel_hub import VercelAIHubService
                self.vercel_service = VercelAIHubService(api_key=vercel_key)
                logger.info("   ✅ Vercel AI Hub service initialized")
            except Exception as e:
                self.vercel_service = None
                logger.warning(f"   ⚠️ Vercel AI Hub service failed to initialize: {e}")
        else:
            self.vercel_service = None

        logger.info("✅ VideoSceneProcessor initialized successfully")
    
    @property
    def llm_logger(self):
        """Lazy LLMLogger — created when _llm_log_dir is set by the wrapper."""
        if self._llm_log_dir and self._llm_logger is None:
            from tvd_pipeline.llm_logger import LLMLogger
            self._llm_logger = LLMLogger(self._llm_log_dir)
        return self._llm_logger

    def _validate_config(self) -> None:
        """Validate that all required configuration is present."""
        # OPENAI_API_KEY is optional — all text generation uses Gemini (vertex provider) by default.
        # Only KIE (image/video gen), Rendi (FFmpeg mixing), and ElevenLabs (TTS) are strictly required.
        required_keys = {
            "KIE_API_KEY": config.KIE_API_KEY,
            "RENDI_API_KEY": config.RENDI_API_KEY,
            "ELEVENLABS_API_KEY": config.ELEVENLABS_API_KEY
        }

        missing = [key for key, value in required_keys.items() if not value]

        if missing:
            raise ValueError(f"Missing required API keys: {', '.join(missing)}")

    def _get_col_safe(self, headers: List[str], col_name: str) -> Optional[int]:
        """Get column index by name, returning None (not raising) if not found."""
        try:
            return self.sheets_service.get_column_index(headers, col_name)
        except ValueError:
            return None

    # =================================================================
    # Unified dispatch helpers (called by both product & UGC pipelines)
    # =================================================================

    def _generate_video(
        self,
        video_model: str,
        video_provider: str,
        image_url: str,
        motion_prompt: str,
        duration: float,
        animation_model: str = None,
        resolution: str = None,
        _original_duration: float = None,
        _failover_depth: int = 0,
        result_metadata: dict = None,
        reference_image_urls: list = None,
        api_method: str = None,
    ) -> Optional[str]:
        """Route video generation to the correct service.

        Supports Veo 3.0, Veo 3.1, Kling, and Runway. Falls back to the
        legacy ``animation_model`` string when ``video_model`` is not set
        (Google Sheets mode).

        When the primary provider fails (returns None) and failover is enabled
        in pipeline_defaults.json, automatically tries backup providers before
        returning None. VeoRAIBlockedError / VeoPromptBlockedError are NOT
        caught — they propagate so callers can handle content-specific retries.

        Args:
            _original_duration: Pre-snap duration for re-snapping on failover.
            _failover_depth: Prevents recursive failover chains (0=primary, 1=backup).

        Returns:
            Public URL of the generated video, or None on failure.
        """
        # Resolve model + provider from legacy animation_model if needed
        vm = video_model
        vp = video_provider
        if not vm and animation_model:
            legacy_map = {
                "google":   ("veo-3.1-fast", "direct"),
                "google31": ("veo-3.1-fast", "direct"),
                "kling":    ("kling-2.5", "kie"),
                "runway":   ("runway", "kie"),
            }
            vm, vp = legacy_map.get(animation_model, (None, None))

        if vm == "none" or vm is None:
            return self.rendi_service.create_video_from_image(image_url, duration)

        result = None
        try:
            if vm.startswith("veo-3.1-ref"):
                # Reference-to-video (fal.ai or Vertex) — must be checked before generic "veo"
                if vp == "fal" and self.fal_service:
                    result = self.fal_service.generate_video(
                        prompt=motion_prompt,
                        image_urls=reference_image_urls or [image_url],
                        video_model=vm,
                        duration=duration,
                        resolution=resolution,
                    )
                elif vp == "fal" and not self.fal_service:
                    logger.warning("FalVideoService not available for ref-to-video, falling back")
                    result = None
                else:
                    # Vertex reference-to-video (default provider: google)
                    svc = self.veo31_fast_service if "fast" in vm else self.veo31_service
                    result = svc.generate_video(
                        prompt=motion_prompt,
                        reference_image_urls=reference_image_urls or [image_url],
                        duration=duration,
                        resolution=resolution,
                        api_method=api_method,
                    )
            elif vm.startswith("veo"):
                if vp == "kie":
                    logger.warning(f"Veo via Kie not yet supported, falling back to direct")
                # Pick the right Veo service instance
                if "3.1" in vm:
                    svc = self.veo31_fast_service if "fast" in vm else self.veo31_service
                else:
                    svc = self.veo3_fast_service if "fast" in vm else self.veo3_service
                result = svc.generate_video(
                    prompt=motion_prompt,
                    image_url=image_url,
                    duration=duration,
                    resolution=resolution,
                    api_method=api_method,
                )
            elif vm.startswith("kling"):
                result = self.kie_service.generate_video_kling(
                    prompt=motion_prompt,
                    image_url=image_url,
                    duration=duration,
                    video_model=vm,
                )
            elif vm.startswith("seedance"):
                # Seedance 2.0 via Kie. If reference_image_urls were supplied,
                # send them through the multimodal-refs mode. Otherwise use
                # the single image_url as first_frame_url.
                _refs = list(reference_image_urls or [])
                # Ensure image_url is at the front of refs so character/style locks first
                if image_url and image_url not in _refs:
                    _refs.insert(0, image_url)
                if _refs:
                    result = self.kie_service.generate_video_seedance(
                        prompt=motion_prompt,
                        reference_image_urls=_refs,
                        duration=int(round(duration)),
                        resolution=resolution,
                    )
                else:
                    result = self.kie_service.generate_video_seedance(
                        prompt=motion_prompt,
                        first_frame_url=image_url,
                        duration=int(round(duration)),
                        resolution=resolution,
                    )
            elif vm.startswith("runway-gen"):
                # Runway Gen4 Turbo / Gen 4.5 via direct API
                model = "gen4.5" if "4.5" in vm else "gen4_turbo"
                _res = int(str(resolution).replace("p", "").replace("P", "")) if resolution else 720
                if self.runway_direct_service:
                    result = self.runway_direct_service.generate_video(
                        image_url=image_url,
                        prompt=motion_prompt,
                        duration=duration,
                        model=model,
                        resolution=_res,
                        video_model=vm,
                    )
                else:
                    logger.warning("RunwayDirectService not available, falling back to Kie Runway")
                    result = self.kie_service.generate_video_runway(
                        prompt=motion_prompt,
                        image_url=image_url,
                        duration=duration,
                    )
            else:
                # runway (legacy via Kie) or any other model
                result = self.kie_service.generate_video_runway(
                    prompt=motion_prompt,
                    image_url=image_url,
                    duration=duration,
                )
        except (VeoRAIBlockedError, VeoPromptBlockedError):
            raise  # Let caller handle content-specific retry
        except Exception as e:
            logger.error(f"_generate_video({vm}): {e}")
            result = None

        # Validate that the returned file actually has a video stream
        if result:
            try:
                if not self.rendi_service.validate_video_has_video_stream(result):
                    logger.warning(f"_generate_video({vm}): returned file has no video stream (likely blocked by safety filter): {result[:80]}...")
                    raise VeoRAIBlockedError(reason="no_video_stream")
            except VeoRAIBlockedError:
                raise
            except Exception as val_err:
                logger.warning(f"_generate_video({vm}): video stream validation error ({val_err}), proceeding")

        # Record which model/provider actually produced the result
        if result and result_metadata is not None:
            result_metadata["model"] = vm
            result_metadata["provider"] = vp or "direct"

        # --- Failover: if primary failed and we haven't already failed over ---
        if result is None and _failover_depth == 0:
            from tvd_pipeline.config import get_pipeline_defaults
            _p_defaults = get_pipeline_defaults()
            if _p_defaults.get("video_failover_enabled", False):
                chain = _p_defaults.get("video_failover_chain", {})
                # Find chain by prefix match
                fb_list = None
                for prefix, models in chain.items():
                    if vm and vm.startswith(prefix):
                        fb_list = models
                        break
                if fb_list:
                    raw_dur = _original_duration if _original_duration is not None else duration
                    for fb_model in fb_list:
                        # Check service availability
                        if fb_model.startswith("kling") and not self.kie_service:
                            continue
                        if fb_model.startswith("runway") and not self.kie_service:
                            continue
                        if fb_model.startswith("veo") and not (self.veo31_fast_service or self.veo3_service):
                            continue

                        fb_dur = snap_duration(fb_model, raw_dur)
                        # Resolve provider from model prefix
                        if fb_model.startswith("veo"):
                            fb_provider = "direct"
                        else:
                            fb_provider = "kie"

                        logger.warning(
                            f"Video failover: {vm} failed -> trying {fb_model} "
                            f"(duration {duration}s -> {fb_dur}s)"
                        )
                        fb_result = self._generate_video(
                            video_model=fb_model,
                            video_provider=fb_provider,
                            image_url=image_url,
                            motion_prompt=motion_prompt,
                            duration=fb_dur,
                            resolution=resolution,
                            _failover_depth=1,
                            result_metadata=result_metadata,
                        )
                        if fb_result:
                            logger.info(f"Video failover succeeded: {vm} -> {fb_model}")
                            return fb_result
                        logger.warning(f"Video failover {fb_model} also failed")

        return result

    def _generate_image(
        self,
        image_model: str,
        image_provider: str,
        use_google_image: bool = False,
        use_kie_flash: bool = False,
        resolution: str = None,
        **scene_image_kw,
    ) -> Optional[str]:
        """Route image generation to the correct service.

        When ``image_model``/``image_provider`` are set (API mode), they take
        priority. Otherwise falls back to the legacy boolean flags
        ``use_google_image`` and ``use_kie_flash`` (Sheets mode).

        The ``resolution`` param is forwarded to Kie services so the API
        tier can control output image resolution.

        Returns:
            Public URL of the generated image, or None on failure.
        """
        _merge_reference_image_urls_into_scene_kw(scene_image_kw)
        # Forward tier resolution to Vertex Nano Banana 2 (imageConfig.imageSize) when applicable.
        _scene_kw = dict(scene_image_kw)
        if resolution:
            _scene_kw["resolution"] = resolution
        # Resolve which service to use
        if image_model:
            # API-mode: direct = Vertex (Gemini Pro or Gemini 3.1 Flash); kie + flash = Kie Flash
            if image_provider == "direct" and "gemini" in image_model:
                return self.gemini_image_service.generate_scene_image(image_model=image_model, **_scene_kw)
            if "flash" in image_model and image_provider == "kie":
                return self.kie_service.generate_scene_image_flash(**scene_image_kw)
            if image_model.startswith("gemini") or image_provider == "direct":
                return self.gemini_image_service.generate_scene_image(image_model=image_model, **_scene_kw)
            return self.kie_service.generate_scene_image(resolution=resolution, **scene_image_kw)
        # Legacy Sheets mode
        if use_google_image:
            return self.gemini_image_service.generate_scene_image(**scene_image_kw)
        if use_kie_flash:
            return self.kie_service.generate_scene_image_flash(**scene_image_kw)
        return self.kie_service.generate_scene_image(resolution=resolution, **scene_image_kw)

    def _evaluate_image_quality(self, image_url: str, original_prompt: str) -> int:
        """Rate image quality 1-10 using Gemini vision. Returns 7 on error."""
        return _h_evaluate_image_quality(self, image_url, original_prompt)

    def reset_usage(self):
        """Reset the token usage accumulator. Call before a task function."""
        with self._usage_lock:
            self._usage_accumulator = {"input_tokens": 0, "output_tokens": 0}
            self._usage_entries = []

    def get_usage(self) -> Dict[str, int]:
        """Return accumulated token counts since last reset_usage()."""
        with self._usage_lock:
            return dict(self._usage_accumulator)

    def get_usage_by_model(self) -> list:
        """Return per-call usage entries since last reset_usage().

        Each entry: {step_key, model, provider, input_tokens, output_tokens}
        """
        with self._usage_lock:
            return list(self._usage_entries)

    def _call_llm(self, step_key: str, messages: list, **kwargs):
        """Unified 3-tier text LLM dispatch.

        Resolution order:
        1. Runtime override (self._text_provider / self._text_model, set from API params)
        2. Per-step config from tvd_pipeline/config/models.json text_defaults
        3. Fallback to vertex / gemini-2.5-flash

        Args:
            step_key: Key matching text_defaults in models.json (e.g. "parse_prompt").
            messages: List of message dicts for the LLM.
            **kwargs: Passed through to the underlying service call.

        Returns:
            The LLM response (string or structured, depending on service).
        """
        # Tier 1: Runtime override from API params
        if self._text_provider and self._text_model:
            provider, model = self._text_provider, self._text_model
        else:
            # Tier 2: Per-step config defaults
            step_config = self.model_config.get("text_defaults", {}).get(step_key, {})
            provider = step_config.get("provider", "vertex")
            model = step_config.get("model", "gemini-2.5-flash")
            # Forward extra config keys (e.g. reasoning_effort) as kwargs defaults
            for k, v in step_config.items():
                if k not in ("provider", "model", "_note") and k not in kwargs:
                    kwargs[k] = v

        t0 = time.perf_counter()
        result = None
        _llm_exc: Optional[BaseException] = None
        try:
            if provider in ("vertex", "gemini"):
                result = self.gemini_service.call(model, messages, **kwargs)
            elif provider == "openai":
                result = self.openai_service.call(model, messages, **kwargs)
            elif provider == "vercel":
                if hasattr(self, "vercel_service") and self.vercel_service:
                    result = self.vercel_service.call(model, messages, **kwargs)
                else:
                    logger.warning(f"_call_llm: vercel provider requested but VercelService not available, falling back to vertex")
                    result = self.gemini_service.call(model, messages, **kwargs)
            else:
                raise ValueError(f"Unknown text_provider: {provider}")
        except BaseException as e:
            _llm_exc = e
            raise
        finally:
            try:
                from tvd_pipeline.external_api_log import log_external_api_result

                dt_ms = max(0, int((time.perf_counter() - t0) * 1000))
                detail_parts = [f"step_key={step_key}"]
                if isinstance(result, dict):
                    if result.get("input_tokens") is not None:
                        detail_parts.append(f"in_tok={result.get('input_tokens')}")
                    if result.get("output_tokens") is not None:
                        detail_parts.append(f"out_tok={result.get('output_tokens')}")
                log_external_api_result(
                    str(provider),
                    str(step_key),
                    duration_ms=dt_ms,
                    method="LLM",
                    model=str(model),
                    ok=_llm_exc is None,
                    error=str(_llm_exc)[:400] if _llm_exc else "",
                    detail=" ".join(detail_parts)[:400],
                )
            except Exception:
                pass

        # Accumulate token usage (providers return input_tokens/output_tokens in their response dict)
        if isinstance(result, dict):
            _in_tok = result.get("input_tokens", 0)
            _out_tok = result.get("output_tokens", 0)
            with self._usage_lock:
                self._usage_accumulator["input_tokens"] += _in_tok
                self._usage_accumulator["output_tokens"] += _out_tok
                self._usage_entries.append({
                    "step_key": step_key,
                    "model": model,
                    "provider": provider,
                    "input_tokens": _in_tok,
                    "output_tokens": _out_tok,
                })

        # Debug logging — delegate to LLMLogger (handles sequencing, sanitisation)
        if self.llm_logger:
            self.llm_logger.log(
                step_key, provider, model, messages, result,
                **{k: v for k, v in kwargs.items() if k != "responseSchema"},
            )

        return result

    def _tpad_video(self, video_url: str, pad_seconds: float, row_num: int = 0) -> Optional[str]:
        """Extend a video by freezing its last frame using FFmpeg tpad."""
        return _h_tpad_video(self, video_url, pad_seconds, row_num=row_num)

    def process_product_video(self, **kwargs) -> Dict[str, Any]:
        """Delegated to tvd_pipeline.pipelines.product."""
        return _product_pipeline(self, **kwargs)


    def run_rendi_zapcap_only(self, row_num, headers, scene_videos, scene_durations, music_url=None, vo_audio_urls=None, vo_audio_url=None, add_subtitles=False, subtitle_language="en", buffer_seconds=0.5):
        """Run only Rendi (concat + audio) and ZapCap for existing assets. No generation."""
        return _sh_run_rendi_zapcap_only(self, row_num, headers, scene_videos, scene_durations, music_url=music_url, vo_audio_urls=vo_audio_urls, vo_audio_url=vo_audio_url, add_subtitles=add_subtitles, subtitle_language=subtitle_language, buffer_seconds=buffer_seconds)
    
    def process_ugc_video(self, **kwargs) -> Dict[str, Any]:
        """Delegated to tvd_pipeline.pipelines.ugc."""
        return _ugc_pipeline(self, **kwargs)

    def process_ugc_real_video(self, **kwargs) -> Dict[str, Any]:
        """Delegated to tvd_pipeline.pipelines.ugc_real."""
        return _ugc_real_pipeline(self, **kwargs)

    def process_custom_video(self, storyboard, **kwargs) -> Dict[str, Any]:
        """Delegated to tvd_pipeline.pipelines.custom — chat-built storyboard executor."""
        return _custom_pipeline(self, storyboard, **kwargs)


    def _generate_influencer_image(self, **kwargs):
        return _ugc_generate_influencer_image(self, **kwargs)

    def _get_cultural_info_for_language(self, language):
        return _ugc_get_cultural_info(self, language=language)

    def _pick_reference_image_index_for_scene(self, **kwargs):
        return _ugc_pick_ref_image(self, **kwargs)

    def _analyze_reference_images(self, image_urls):
        return _ugc_analyze_ref_images(self, image_urls=image_urls)

    def _insert_asset_as_scene(self, asset_url, target_duration=4.0):
        return _ugc_insert_asset(self, asset_url=asset_url, target_duration=target_duration)

    def _presplit_vo_into_scenes(self, structured_script, word_segments, total_duration):
        return _h_presplit_vo_into_scenes(self, structured_script=structured_script, word_segments=word_segments, total_duration=total_duration)

    def _presplit_vo_at_sentences(self, word_segments, target_scene_count, total_duration):
        return _h_presplit_vo_at_sentences(self, word_segments=word_segments, target_scene_count=target_scene_count, total_duration=total_duration)

    def _generate_vo_script_single(self, **kwargs):
        return _h_generate_vo_script_single(self, **kwargs)

    def _estimate_scene_count_from_text4(self, text_4, target_duration=30):
        return _h_estimate_scene_count(self, text_4=text_4, target_duration=target_duration)

    def run_rendi_voice_subtitles_for_row(self, row_num: int) -> Optional[str]:
        """One-off: read scene videos, VO, music from sheet for one row; produce RENDI Scene, RENDI Scene & Voice, and Subtitled Video."""
        return _sh_run_rendi_voice_subtitles_for_row(self, row_num)

    def add_subtitles_to_row_from_rendi_voice(self, row_num: int) -> Optional[str]:
        """Take the existing 'RENDI Scene & Voice' video for one row, add subtitles via ZapCap, upload to GCS, and write Final Video."""
        return _sh_add_subtitles_to_row_from_rendi_voice(self, row_num)
    
    def process_all_videos(self) -> Dict[str, Any]:
        """Main sheet orchestrator. Delegated to tvd_pipeline."""
        return _sheet_process_all_videos(self)

    def process_single_video(
        self, 
        video_url: str, 
        row_num: int,
        headers,
        manual_instructions: str = "",
        cta_button: bool = False,
        cta_text: str = "",
        cta_duration: str = "at_the_end",
        add_subtitles: bool = False,
        article_text: str = "",
        vertical: str = "",
        subtitle_language: str = "",
        manual_vo_text: str = "",
        manual_music_link: str = "",
        voice_id: str = "",
        add_opening_text: bool = False,
        opening_text: str = "",
        animation_model: str = "runway",
        article_related_to_video: bool = True
    ):
        """Legacy pipeline: process existing video via scene detection + regeneration. Delegated to tvd_pipeline."""
        return _legacy_process_single_video(
            self, video_url=video_url, row_num=row_num, headers=headers,
            manual_instructions=manual_instructions, cta_button=cta_button,
            cta_text=cta_text, cta_duration=cta_duration, add_subtitles=add_subtitles,
            article_text=article_text, vertical=vertical, subtitle_language=subtitle_language,
            manual_vo_text=manual_vo_text, manual_music_link=manual_music_link,
            voice_id=voice_id, add_opening_text=add_opening_text, opening_text=opening_text,
            animation_model=animation_model, article_related_to_video=article_related_to_video,
        )

    def _process_single_scene(self, scene, row_num, headers, manual_instructions="", animation_model="runway", target_language="en"):
        """Process a single scene. Delegated to tvd_pipeline."""
        return _legacy_process_single_scene(
            self, scene=scene, row_num=row_num, headers=headers,
            manual_instructions=manual_instructions, animation_model=animation_model,
            target_language=target_language,
        )

    def _process_cta_button(self, cta_image_url, temp_dir, row_num):
        """Process CTA button image. Delegated to tvd_pipeline."""
        return _legacy_process_cta_button(self, cta_image_url=cta_image_url, temp_dir=temp_dir, row_num=row_num)

    def _upload_video_to_gcs_from_url(self, video_url, row_num, scene_num):
        """Upload video from temp URL to GCS. Returns GCS URL or None."""
        try:
            # Use the GCS storage service method for uploading from URL
            gcs_key = f"scene_video_row_{row_num}_scene_{scene_num}_{int(time.time())}.mp4"
            
            gcs_url = self.gcs_storage_service.upload_video_from_url(
                source_url=video_url,
                key_name=gcs_key
            )
            
            return gcs_url
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to upload video to GCS: {e}")
            return None
    
    def _update_sheet_cell(self, row_num, column, value, headers):
        """Update a cell in the Google Sheet (thread-safe, throttled)."""
        with self._sheets_lock:  # Thread-safe lock for parallel scene processing
            try:
                self.sheets_service.update_cell(
                    sheet_id=config.GOOGLE_SHEET_ID,
                    worksheet_name=config.GOOGLE_SHEET_TAB,
                    row=row_num,
                    column_name=column,
                    value=value,
                    headers=headers
                )
                # Throttle: Small delay to avoid rate limits (60 req/min = 1 req/sec)
                # With multiple parallel rows, we need ~1.5s gap between updates
                time.sleep(0.6)
                return True
                
            except ValueError as e:
                logger.warning(f"⚠️ Column '{column}' not found, skipping update")
                return False
                
            except Exception as e:
                # Service already retried, this is the final failure
                logger.error(f"❌ Failed to update cell ({row_num}, {column}) after retries: {e}")
                return False

    def process_influencer_row(self, row_num, row_data, headers, free_text, manual_instructions="", language="", cta_button=False, cta_text="", cta_duration="at_the_end", add_subtitles=False, manual_vo_text="", manual_music_link="", image_urls=None, scene_count=None, voice_id="", gender="f"):
        """Process a row in influencer mode (no input video, generate from Free text)."""
        return _sh_process_influencer_row(self, row_num, row_data, headers, free_text, manual_instructions=manual_instructions, language=language, cta_button=cta_button, cta_text=cta_text, cta_duration=cta_duration, add_subtitles=add_subtitles, manual_vo_text=manual_vo_text, manual_music_link=manual_music_link, image_urls=image_urls, scene_count=scene_count, voice_id=voice_id, gender=gender)

    def _process_influencer_scene(self, scene_num, first_prompt, second_prompt, reference_image_url, reference_description, row_num, headers, animation_model="runway", target_language="en"):
        """Process a single influencer scene (image + video generation)."""
        return _sh_process_influencer_scene(self, scene_num, first_prompt, second_prompt, reference_image_url, reference_description, row_num, headers, animation_model=animation_model, target_language=target_language)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
def main():
    """Main entry point for the video scene processor."""
    import sys
    # One-off: RENDI Scene + RENDI Scene & Voice + Subtitled Video for one row (row number from CLI, not hardcoded)
    for arg in sys.argv[1:]:
        if arg.startswith("--rendi-voice-subtitles-row="):
            try:
                row_num = int(arg.split("=", 1)[1].strip())
            except (IndexError, ValueError):
                logger.error("Usage: --rendi-voice-subtitles-row=4 (row number)")
                return None
            logger.info("="*60)
            logger.info("🎬 ONE-OFF: RENDI Scene → RENDI Scene & Voice → Subtitled Video")
            logger.info("="*60)
            try:
                processor = VideoSceneProcessor()
                url = processor.run_rendi_voice_subtitles_for_row(row_num)
                return {"row": row_num, "final_video_url": url}
            except Exception as e:
                logger.error(f"❌ Fatal error: {e}")
                raise
            break
        if arg.startswith("--subtitles-only-row="):
            try:
                row_num = int(arg.split("=", 1)[1].strip())
            except (IndexError, ValueError):
                logger.error("Usage: --subtitles-only-row=4 (row number)")
                return None
            logger.info("="*60)
            logger.info("🎬 SUBTITLES ONLY - from RENDI Scene & Voice")
            logger.info("="*60)
            try:
                processor = VideoSceneProcessor()
                url = processor.add_subtitles_to_row_from_rendi_voice(row_num)
                return {"row": row_num, "final_video_url": url}
            except Exception as e:
                logger.error(f"❌ Fatal error: {e}")
                raise
            break
    else:
        logger.info("="*60)
        logger.info("🎬 VIDEO SCENE PROCESSOR - TVD X1 PIPELINE")
        logger.info("="*60)
        
        try:
            processor = VideoSceneProcessor()
            results = processor.process_all_videos()
            
            logger.info("\n📊 Final Results:")
            logger.info(json.dumps(results, indent=2, default=str))
            
            return results
            
        except Exception as e:
            logger.error(f"❌ Fatal error: {e}")
            raise


if __name__ == "__main__":
    main()

