"""Product video pipeline -- extracted from VideoSceneProcessor.

This module contains the complete product video creation pipeline.
The main function ``process_product_video`` accepts a *processor* instance
(a ``VideoSceneProcessor``) so it can access all services.

Imported by the monolith via::

    from tvd_pipeline.pipelines.product import process_product_video
"""

import json
import math
import re
import time
import logging
import tempfile
import os
import threading
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from tvd_pipeline.runtime_callback import executor_submit_with_progress
from tvd_pipeline.config import Config, get_pipeline_defaults
from tvd_pipeline.services.veo3 import VeoRAIBlockedError
from tvd_pipeline.data_loader import get_speech_rate, get_language_name, get_elevenlabs_config
from tvd_pipeline.utils import (
    _SIM_IMAGE, _SIM_VIDEO, _SIM_AUDIO,
    script_only_for_tts,
)
from tvd_pipeline.services.ffmpeg_processor import FFmpegProcessor
from tvd_pipeline.services.local_ffmpeg import LocalFFmpegFallback
from tvd_pipeline.services.tasks.character import describe_characters
from tvd_pipeline.services.tasks.image_eval import evaluate_image_quality, evaluate_image_cleanliness
from tvd_pipeline.services.tasks.prompt_parsing import parse_product_prompt, generate_product_video_scenes
from tvd_pipeline.services.tasks.music import generate_music_description_from_text
from tvd_pipeline.services.tasks.video_analysis import analyze_reference_video_structure
from tvd_pipeline.services.tasks.subtitle_enrichment import enrich_transcript_for_subtitles

config = Config()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers (imported from their own modules)
# ---------------------------------------------------------------------------
from tvd_pipeline.pipelines._helpers import (  # noqa: E402
    _presplit_vo_into_scenes,
    _presplit_vo_at_sentences,
    _generate_vo_script_single,
    _estimate_scene_count_from_text4,
    _apply_phrase_start_strategy,
    _precision_trim_clip,
    emit_llm_usage_events,
    emit_llm_usage_events_from_entries,
    unified_scene_image_motion_prompts,
)


def process_product_video(
    processor,
    row_num: int = None,
    row_data: List[str] = None,
    headers: List[str] = None,
    prompt: str = "",
    image_urls: List[str] = None,
    text_1_col: int = None,
    text_2_col: int = None,
    text_3_col: int = None,
    text_4_col: int = None,
    vo_script_col: int = None,
    animation_model: str = "auto",
    generate_vo: bool = True,
    visual_style: str = "Auto",
    target_duration: int = 30,
    character_urls: List[str] = None,
    logo_url: str = None,
    slogan_text: str = None,
    add_subtitles: bool = True,
    subtitle_language: str = "en",
    video_reference_url: str = None,
    country: str = "",
    # --- Unified model+provider params ---
    video_model: str = None,
    video_provider: str = None,
    video_resolution: str = None,
    image_model: str = None,
    image_provider: str = None,
    image_resolution: str = "1K",
    text_model: str = None,
    text_provider: str = None,
    # --- Other new params ---
    output_resolution: str = None,
    product_image_mode: str = "auto",
    product_image_urls: List[str] = None,
    dissolve_seconds: float = None,
    voice_id: str = None,
    sync_method: str = "standard",
    sync_strategy: str = "continuous",
    on_progress: callable = None,
    existing_intermediates: dict = None,
    language: str = None,
    quality_check: bool = True,
    character_description: str = None,
    gender: str = "f",
    reference_image_urls: List[str] = None,
    asset_urls: list = None,
    enrich_cta_with_influencer: bool = False,
    film_grain: bool = False,
    subtitle_emoji: bool = True,
    subtitle_position: str = "middle",
    simulation: bool = False,
    run_only_parse_prompt: bool = False,
    # Studio Phase 2 (seed_job + pause_after_step=step_2.7): skip heavy character work; optional VO script without TTS until resume.
    skip_character_and_analyze_media: bool = False,
    vo_script_only: bool = False,
    product_explain: str = None,
    product_no_on_screen_character: bool = False,
    **kwargs
) -> Dict[str, Any]:
    """Process a product video row - FULL PIPELINE.
    
    This method implements the complete product video creation workflow:
    1. Parse prompt into TEXT 1-4 (if not already done)
    2. Generate clean product image from reference images
    3. Generate scene-by-scene prompts using Gemini
    
    Args:
        visual_style: Visual style for image generation. Options:
            - "Auto" (default): Use photorealistic/cinematic style
            - "Modern flat 2d", "Minimal line art", "Futuristic isometric Tech Glow",
              "Modern semi flat 2d", "Cinematic photography", "Soft 3d clay",
              "isometric soft vector", "Paper Cut"
        target_duration: Target video duration in seconds (10-40). Affects:
            - Number of scenes (more scenes for longer videos)
            - VO script length (more words for longer videos)
            - Individual scene durations
    4. Generate images for each scene using Nano Banana
    5. Generate animations for each scene using Runway/Kling
    6. Generate background music using Suno
    7. Generate voice over using ElevenLabs (optional)
    8. Combine everything into final video using Rendi
    9. Update Google Sheet with all asset URLs
    
    Args:
        row_num: Row number in the sheet (1-indexed)
        row_data: List of cell values for the row
        headers: List of column headers
        prompt: The product description prompt from the Prompt column
        image_urls: Optional list of product reference image URLs
        text_1_col: Column index for TEXT 1
        text_2_col: Column index for TEXT 2
        text_3_col: Column index for TEXT 3
        text_4_col: Column index for TEXT 4
        animation_model: "runway" or "kling" for video generation
        generate_vo: Whether to generate voice over
        character_urls: Optional list of character image URLs to include in scenes (supports multiple people)
        logo_url: Optional URL to logo for ending/CTA scene
        
    Returns:
        Dict with processing results including all generated asset URLs
    """
    # Handle param aliases
    image_urls = image_urls or product_image_urls
    subtitle_language = language or subtitle_language

    _p_defaults = get_pipeline_defaults()
    image_quality_threshold = _p_defaults.get("image_quality_threshold", 5)

    # Resolve unified model params from legacy animation_model
    if video_model is None and animation_model:
        mapped = processor.SHEET_ANIMATION_MAP.get(animation_model, ("runway", "kie"))
        video_model, video_provider = mapped
    elif video_model is None:
        video_model, video_provider = "runway", "kie"

    # Wire text_model/text_provider to _call_llm() for runtime overrides
    processor._text_model = text_model
    processor._text_provider = text_provider

    # Initialize intermediates cache and usage tracking for wrapper integration
    intermediates = existing_intermediates or {}
    usage_list = []

    if product_no_on_screen_character:
        character_urls = []
        character_description = None
        intermediates.pop("character_description", None)
        logger.info(
            f"   [Row {row_num}] product_no_on_screen_character: skipping on-screen character (URLs/description cleared)"
        )

    logger.info(f"🎬 [Row {row_num}] Processing product video - FULL PIPELINE...")
    logger.info(f"   Prompt: {prompt[:100]}..." if len(prompt) > 100 else f"   Prompt: {prompt}")

    result = {
        "row": row_num,
        "video_type": "product video",
        "success": False,
        "parsed_texts": {},
        "clean_product_image": None,
        "scene_prompts": [],
        "scene_images": [],
        "scene_videos": [],
        "music_url": None,
        "vo_script": None,
        "vo_audio_url": None,
        "final_video_url": None,
        "errors": []
    }
    
    if not processor.gemini_service or not processor.gemini_service.initialized:
        error = "Gemini service not available"
        logger.error(f"   [Row {row_num}] {error}")
        result["errors"].append(error)
        return result
    
    if not prompt:
        error = "No prompt provided in Prompt column"
        logger.error(f"   [Row {row_num}] {error}")
        result["errors"].append(error)
        return result
    
    # Filter out empty image URLs
    valid_image_urls = [url for url in (image_urls or []) if url and url.strip()]
    if valid_image_urls:
        logger.info(f"   Including {len(valid_image_urls)} product reference images")

    _pex = (product_explain or "").strip()
    parse_prompt_text = prompt.strip()
    if _pex:
        parse_prompt_text = f"{parse_prompt_text}\n\n--- Product details (from user) ---\n{_pex}"

    # API / wrapper path: row_data is None — avoid len(None) and bad indexing
    _row = row_data if isinstance(row_data, (list, tuple)) else []
    
    # =====================================================================
    # STEP 0: Describe character(s) if character URL(s) provided
    # Studio phase 1 (run_only_parse_prompt): skip — character runs on resume/phase 2.
    # =====================================================================
    _char_desc_param = character_description
    character_description = None
    if run_only_parse_prompt or skip_character_and_analyze_media:
        character_description = intermediates.get("character_description") or _char_desc_param
        if character_description:
            intermediates["character_description"] = character_description
            logger.info(
                f"   [Row {row_num}] "
                f"{'run_only_parse_prompt' if run_only_parse_prompt else 'skip_character_and_analyze_media'}: "
                f"reusing character_description from seed / params"
            )
    else:
        if on_progress:
            on_progress("step_start", {
                "step": "character_description",
                "label": "Character Description",
                "message": "Generating character description...",
            })
        if "character_description" in intermediates:
            character_description = intermediates["character_description"]
            logger.info(f"   [Row {row_num}] Using existing intermediate: character_description")
        elif _char_desc_param:
            character_description = _char_desc_param
            logger.info("Using provided character_description, skipping AI analysis")
            intermediates["character_description"] = character_description
        elif character_urls:
            character_description = None
            logger.info(f"   [Row {row_num}] Describing {len(character_urls)} character(s) from image(s)...")
            try:
                if simulation:
                    character_description = "A person with professional appearance, well-groomed, wearing business attire"
                    logger.info(f"   [Row {row_num}] [SIM] Character(s) described: {character_description}")
                else:
                    processor.reset_usage()
                    character_description = describe_characters(
                        lambda msgs, **kw: processor._call_llm("describe_character", msgs, **kw),
                        image_urls=character_urls,
                    )
                if character_description:
                    intermediates["character_description"] = character_description
                    if not simulation:
                        logger.info(f"   [Row {row_num}] Character(s) described: {character_description[:100]}...")
                else:
                    logger.warning(f"   [Row {row_num}] Failed to describe character(s), will skip character integration")
                    character_urls = []
            except Exception as e:
                logger.warning(f"   [Row {row_num}] Error describing character(s): {e}")
                character_urls = []

        if character_description:
            if on_progress:
                on_progress("step_complete", {
                    "step": "character_description",
                    "label": "Character Description",
                    "progress": 3,
                    "message": "Character described",
                })
                on_progress("intermediate", {"key": "character_description", "value": character_description})
                emit_llm_usage_events(processor, on_progress, usage_list, "character_description")

    # =====================================================================
    # STEP 1: Parse prompt into TEXT 1-4 (if not already present in sheet)
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "parse_prompt",
            "label": "Parse Prompt",
            "message": "Parsing prompt into scenes...",
        })
    logger.info(f"   [Row {row_num}] Step 1: Parsing product prompt...")

    # Check if TEXT columns already have data
    text_1 = _row[text_1_col].strip() if text_1_col is not None and text_1_col < len(_row) else ""
    text_2 = _row[text_2_col].strip() if text_2_col is not None and text_2_col < len(_row) else ""
    text_3 = _row[text_3_col].strip() if text_3_col is not None and text_3_col < len(_row) else ""
    text_4 = _row[text_4_col].strip() if text_4_col is not None and text_4_col < len(_row) else ""

    # Use existing intermediate if available (checkpoint/resume). Ignore placeholder dicts where
    # all of text_1–3 are empty — otherwise we skip the LLM and never fill TEXT 1–3 (Studio/API).
    def _parsed_text_nonempty(pt: Any) -> bool:
        if not isinstance(pt, dict):
            return False
        for k in ("text_1", "text_2", "text_3"):
            v = pt.get(k)
            if v is None:
                continue
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, list) and v:
                return True
            if isinstance(v, dict) and v:
                return True
        return False

    _cached_pt = intermediates.get("parsed_texts")
    if _parsed_text_nonempty(_cached_pt):
        parsed = intermediates["parsed_texts"]
        text_1 = parsed.get("text_1", text_1)
        text_2 = parsed.get("text_2", text_2)
        text_3 = parsed.get("text_3", text_3)
        text_4 = parsed.get("text_4", text_4)
        result["parsed_texts"] = parsed
        logger.info(f"   [Row {row_num}] Using existing intermediate: parsed_texts")
    elif not text_1 or not text_2 or not text_3:
        # Need to parse the prompt
        try:
            if simulation:
                parsed = {
                    "text_1": f"[Sim] Overview: {prompt[:80]}",
                    "text_2": "[Sim] Benefits and key differentiators",
                    "text_3": "[Sim] Visual narrative with scene descriptions",
                    "text_4": "[Sim] Scene structure and timing",
                }
            else:
                processor.reset_usage()
                parsed = parse_product_prompt(
                    lambda msgs, **kw: processor._call_llm("parse_prompt", msgs, **kw),
                    prompt=parse_prompt_text,
                    image_urls=valid_image_urls,
                    language=subtitle_language,
                    on_progress=on_progress,
                )

            text_1 = parsed.get("text_1", "")
            text_2 = parsed.get("text_2", "")
            text_3_raw = parsed.get("text_3", "")
            # Convert text_3 to string if it's a list or dict
            if isinstance(text_3_raw, list):
                text_3 = "\n".join(str(item) for item in text_3_raw)
            elif isinstance(text_3_raw, dict):
                text_3 = json.dumps(text_3_raw, indent=2)
            else:
                text_3 = str(text_3_raw) if text_3_raw else ""

            text_4_raw = parsed.get("text_4", "")
            # Convert text_4 to readable string for downstream consumers
            if isinstance(text_4_raw, list):
                text_4 = "\n".join(
                    f"Scene {s.get('scene', i+1)} ({s.get('purpose', '')}): {s.get('description', '')}"
                    for i, s in enumerate(text_4_raw)
                )
                text_4_list = text_4_raw  # keep original list for scene counting
            elif isinstance(text_4_raw, dict):
                text_4 = json.dumps(text_4_raw, indent=2)
                text_4_list = None
            else:
                text_4 = str(text_4_raw) if text_4_raw else ""
                text_4_list = None

            result["parsed_texts"] = parsed
            intermediates["parsed_texts"] = parsed

            # Persist parsed_texts to the job before usage/cost events (Windows Errno 22 can
            # break later Supabase calls; early save keeps Preferences data recoverable).
            if on_progress:
                try:
                    on_progress(
                        "intermediate",
                        {"key": "parsed_texts", "value": dict(result["parsed_texts"])},
                    )
                except Exception as _early_im:
                    logger.warning(
                        "   [Row %s] Early parsed_texts intermediate failed: %s",
                        row_num,
                        _early_im,
                    )

            # Write to sheet
            updates = []
            if text_1_col is not None and text_1:
                updates.append((config.TEXT_1_COLUMN, text_1))
            if text_2_col is not None and text_2:
                updates.append((config.TEXT_2_COLUMN, text_2))
            if text_3_col is not None and text_3:
                updates.append((config.TEXT_3_COLUMN, text_3))
            if text_4_col is not None and text_4:
                updates.append((config.TEXT_4_COLUMN, text_4))

            _sheet_ctx = (
                headers
                and row_num is not None
                and isinstance(row_num, int)
                and row_num >= 1
            )
            if _sheet_ctx and updates:
                for column_name, value in updates:
                    try:
                        processor.sheets_service.update_cell(
                            config.GOOGLE_SHEET_ID,
                            config.GOOGLE_SHEET_TAB,
                            row_num,
                            column_name,
                            value,
                            headers
                        )
                    except OSError as oe:
                        logger.warning(
                            "   [Row %s] Sheet write skipped after parse (OSError): %s",
                            row_num,
                            oe,
                        )
                logger.info(f"   [Row {row_num}] Parsed and wrote TEXT 1-4 to sheet")
            elif not _sheet_ctx:
                logger.info(f"   [Row {row_num}] Parsed prompt (no sheet row — TEXT 1-4 in job only)")

            # --- Callback: parse_prompt usage ---
            if on_progress:
                try:
                    emit_llm_usage_events(processor, on_progress, usage_list, "parse_prompt")
                except OSError as oe:
                    logger.warning(
                        "   [Row %s] emit_llm_usage_events after parse skipped: %s",
                        row_num,
                        oe,
                    )

        except Exception as e:
            error = f"Error parsing prompt: {str(e)}"
            logger.error(f"   [Row {row_num}] {error}")
            result["errors"].append(error)
            return result
    else:
        logger.info(f"   [Row {row_num}] TEXT 1-4 already present in sheet")
        result["parsed_texts"] = {
            "text_1": text_1,
            "text_2": text_2,
            "text_3": text_3,
            "text_4": text_4
        }

    # --- Callback: emit intermediate first so wrapper saves before pause (step_complete triggers pause) ---
    if on_progress:
        on_progress("intermediate", {"key": "parsed_texts", "value": result["parsed_texts"]})
        on_progress("step_complete", {
            "step": "parse_prompt",
            "label": "Parse Prompt",
            "progress": 5,
            "message": "Prompt parsed into TEXT 1-4",
        })

    # Image API: "Google" = Vertex Gemini; "kie flash" = Kie Gemini 3 Flash; "kie" or empty = Kie (Nano Banana)
    image_api_col = processor._get_col_safe(headers, config.IMAGE_API_COLUMN) if headers else None
    image_api_val = (_row[image_api_col].strip().lower() if image_api_col is not None and image_api_col < len(_row) else "")
    # Resolve unified image model params
    if image_model is None:
        if image_api_val:
            mapped = processor.SHEET_IMAGE_API_MAP.get(image_api_val.lower().strip(), ("nano-banana-pro", "kie"))
            image_model, image_provider = mapped
        else:
            image_model, image_provider = "nano-banana-pro", "kie"
    # Derive image API flags from either Sheet column or resolved model params (API path)
    if image_api_val:
        use_google_image = (image_api_val in ["gemini 3 pro (vertex ai)", "gemini 3.1 flash (vertex ai)", "gemini 2.5 pro (vertex ai)", "gemini 2.5 flash (vertex ai)", "nano banana 2 (vertex ai)", "gemini pro (vertex)", "google"])
        use_kie_flash = (image_api_val in ["gemini 3 flash (kie.ai)", "gemini flash", "kie flash", "kie-flash", "flash", "gemini-flash"])
    else:
        # API path: derive from image_model/image_provider (Vertex = direct; Kie Flash = kie + flash)
        use_google_image = (image_provider == "direct" and image_model)
        use_kie_flash = (image_provider == "kie" and image_model and "flash" in image_model)
    if use_google_image:
        logger.info(f"   [Row {row_num}] Image API: Vertex Gemini")
    elif use_kie_flash:
        logger.info(f"   [Row {row_num}] Image API: Kie Flash (Gemini 3 Flash)")
    else:
        logger.info(f"   [Row {row_num}] Image API: Kie (Nano Banana)")

    # Route clean product image generation to the correct service based on image_api
    def _gen_clean_product(ref_urls, description, res):
        if use_google_image:
            return processor.gemini_image_service.generate_clean_product_image(
                reference_image_urls=ref_urls, product_description=description, resolution=res,
                image_model=image_model)
        else:
            return processor.kie_service.generate_clean_product_image(
                reference_image_urls=ref_urls, product_description=description, resolution=res)

    _clean_restored_early = bool(intermediates.get("clean_product_image"))
    _vo_full_intermediates_early = (
        "vo_script" in intermediates
        and "vo_audio_url" in intermediates
        and "vo_word_segments" in intermediates
    )
    _vo_script_resume_early = bool(
        generate_vo
        and not _vo_full_intermediates_early
        and (intermediates.get("vo_script") or "").strip()
        and not intermediates.get("vo_audio_url")
    )
    _existing_vo_script_early = ""
    if vo_script_col is not None and vo_script_col < len(_row):
        _existing_vo_script_early = (_row[vo_script_col] or "").strip()

    _need_new_vo_llm = (
        generate_vo
        and not simulation
        and not _vo_full_intermediates_early
        and not _vo_script_resume_early
        and not _existing_vo_script_early
    )
    _need_clean_product_work = (not _clean_restored_early) and bool(valid_image_urls)
    prefetched_vo_script = None
    prefetched_product_voice = None
    prefetched_calibrated_wps = None
    prefetched_calib_sample_len = 0

    def _resolve_product_voice_for_vo():
        if voice_id:
            logger.info(f"Using provided voice_id: {voice_id}")
            return voice_id
        try:
            _vid_col = processor._get_col_safe(headers, config.VOICE_ID_COLUMN) if headers else None
            if _vid_col is not None and _vid_col < len(_row):
                _sheet_vid = _row[_vid_col].strip()
                if _sheet_vid:
                    return _sheet_vid
        except (ValueError, Exception):
            pass
        from tvd_pipeline.data_loader import get_language_voice
        lang_voice = get_language_voice(subtitle_language, "male")
        if lang_voice:
            logger.info(f"   Using language-specific voice: {lang_voice}")
            return lang_voice
        if not simulation:
            return processor.elevenlabs_service.pick_random_voice(gender="male", language=subtitle_language) or config.DEFAULT_VOICE_ID
        return config.DEFAULT_VOICE_ID

    def _execute_clean_product_core():
        """STEP 2 body without step_start/step_complete. Returns (clean_product_url, restored_from_intermediates)."""
        url = intermediates.get("clean_product_image")
        restored = bool(url)
        if url:
            result["clean_product_image"] = url
            logger.info(f"   [Row {row_num}] Using existing intermediate: clean_product_image")
            return url, True
        if simulation and valid_image_urls:
            url = _SIM_IMAGE
            result["clean_product_image"] = url
            logger.info(f"   [Row {row_num}] [SIM] Clean product image: {url}")
            return url, False
        if product_image_mode in ("force_clean", "clean") and valid_image_urls:
            try:
                url = _gen_clean_product(valid_image_urls, text_1, image_resolution)
            except Exception as e:
                logger.warning(f"   [Row {row_num}] Clean product image generation failed: {e}")
            return url, False
        if product_image_mode == "auto" and valid_image_urls:
            try:
                is_clean = evaluate_image_cleanliness(
                    lambda msgs, **kw: processor._call_llm("image_cleanliness_check", msgs, **kw),
                    image_url=valid_image_urls[0],
                )
                if not is_clean:
                    url = _gen_clean_product(valid_image_urls, text_1, image_resolution)
                else:
                    url = valid_image_urls[0]
            except Exception as e:
                logger.warning(f"   [Row {row_num}] Auto product image mode failed: {e}")
            return url, False
        if valid_image_urls:
            try:
                url = _gen_clean_product(valid_image_urls, text_1, image_resolution)
                if url:
                    result["clean_product_image"] = url
                    logger.info(f"   [Row {row_num}] Clean product image: {url[:60]}...")

                    for col_name in (config.CLEAN_PRODUCT_IMAGE_COLUMN, "Clean Product Image"):
                        try:
                            processor.sheets_service.update_cell(
                                config.GOOGLE_SHEET_ID,
                                config.GOOGLE_SHEET_TAB,
                                row_num,
                                col_name,
                                url,
                                headers
                            )
                            break
                        except (ValueError, Exception):
                            continue
                else:
                    logger.warning(f"   [Row {row_num}] Could not generate clean product image")
            except Exception as e:
                logger.warning(f"   [Row {row_num}] Error generating clean product: {e}")
            return url, False
        logger.warning(f"   [Row {row_num}] No reference images for clean product generation")
        return None, False

    def _emit_clean_product_step_callbacks(c_url, c_restored):
        if on_progress:
            # Emit intermediate BEFORE step_complete so the URL is persisted even when
            # pause_after_step='step_2' causes step_complete to raise JobPausedError.
            if c_url:
                on_progress("intermediate", {"key": "clean_product_image", "value": c_url})
            if c_url and not c_restored:
                usage_data = {
                    "service": "gemini_image" if use_google_image else ("kie_flash" if use_kie_flash else "nano_banana"),
                    "step": "clean_product_image",
                    "model": image_model or "nano-banana-pro",
                    "provider": image_provider or "kie",
                    "count": 1, "resolution": image_resolution or "1K",
                    "label": "Clean product image", "category": "images",
                    "success": bool(c_url),
                }
                on_progress("usage", usage_data)
                usage_list.append(usage_data)
            on_progress("step_complete", {
                "step": "clean_product_image",
                "label": "Clean Product Image",
                "progress": 10,
                "message": "Product image cleaned",
            })

    _parallel_clean_and_vo_llm = _need_new_vo_llm and _need_clean_product_work

    if _parallel_clean_and_vo_llm:
        prefetched_product_voice = _resolve_product_voice_for_vo()

        def _vo_llm_parallel_worker():
            calibrated_wps = None
            sample_len = 0
            if not simulation and prefetched_product_voice and not bool(vo_script_only):
                from tvd_pipeline.data_loader import prepare_wps_sample_text

                sample = prepare_wps_sample_text(prompt)
                sample_len = len(sample)
                calibrated_wps = processor.elevenlabs_service.calibrate_voice_wps(
                    sample_text=sample, voice_id=prefetched_product_voice, language=subtitle_language
                )
            script = _generate_vo_script_single(
                processor,
                text_1, text_2, text_3, [], target_duration,
                language=subtitle_language, country=country,
                raw_prompt=prompt, text_4=text_4,
                wps_override=calibrated_wps,
                on_progress=None,
            )
            return script, calibrated_wps, sample_len

        processor.reset_usage()
        if on_progress:
            on_progress("step_start", {
                "step": "clean_product_image",
                "label": "Clean product image + VO script (parallel)",
                "message": "Cleaning product image and generating VO script in parallel — script appears here as soon as the LLM finishes (image may still be running).",
            })
        logger.info(f"   [Row {row_num}] Step 2 (parallel): clean product image + VO script LLM...")
        with ThreadPoolExecutor(max_workers=2) as _tp_ex:
            _f_clean = executor_submit_with_progress(_tp_ex, _execute_clean_product_core)
            _f_vo = executor_submit_with_progress(_tp_ex, _vo_llm_parallel_worker)
            clean_product_url, _clean_product_restored = None, None
            for _fut in as_completed([_f_clean, _f_vo]):
                if _fut is _f_vo:
                    _pv_script, _pv_wps, _pv_calib_len = _fut.result()
                    prefetched_vo_script = _pv_script
                    prefetched_calibrated_wps = _pv_wps
                    prefetched_calib_sample_len = _pv_calib_len
                    # Push script immediately; do not wait for clean_product_image (often slower).
                    if on_progress and prefetched_vo_script:
                        try:
                            on_progress("intermediate", {"key": "vo_script", "value": prefetched_vo_script})
                        except Exception:
                            pass
                else:
                    clean_product_url, _clean_product_restored = _fut.result()

        parallel_entries = processor.get_usage_by_model()
        emit_llm_usage_events_from_entries(
            [e for e in parallel_entries if e.get("step_key") == "image_cleanliness_check"],
            on_progress, usage_list, "clean_product_image",
        )
        emit_llm_usage_events_from_entries(
            [e for e in parallel_entries if e.get("step_key") == "generate_vo"],
            on_progress, usage_list, "vo_script",
        )
        processor.reset_usage()

        if (
            on_progress
            and prefetched_calibrated_wps is not None
            and prefetched_calib_sample_len > 0
            and not bool(vo_script_only)
        ):
            on_progress("usage", {
                "service": "elevenlabs", "step": "tts_calibration",
                "model": get_elevenlabs_config()["tts_model"], "provider": "elevenlabs",
                "character_count": prefetched_calib_sample_len,
                "label": "Voice WPS calibration", "category": "tts", "success": True,
            })

        _emit_clean_product_step_callbacks(clean_product_url, _clean_product_restored)

        if prefetched_vo_script:
            try:
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                    config.VO_SCRIPT_COLUMN, prefetched_vo_script, headers
                )
            except Exception:
                pass
            # vo_script intermediate was already emitted when the VO future completed (parallel path).
    else:
        # =====================================================================
        # STEP 2: Generate clean product image (sequential)
        # =====================================================================
        if on_progress:
            on_progress("step_start", {
                "step": "clean_product_image",
                "label": "Clean Product Image",
                "message": "Evaluating product image...",
            })
        logger.info(f"   [Row {row_num}] Step 2: Generating clean product image...")
        clean_product_url, _clean_product_restored = _execute_clean_product_core()
        _emit_clean_product_step_callbacks(clean_product_url, _clean_product_restored)

    # =====================================================================
    # STEP 2.5: Optional - analyze reference video for structure (when Video reference URL provided)
    # =====================================================================
    reference_video_structure = None
    if video_reference_url and video_reference_url.strip() and not skip_character_and_analyze_media:
        if on_progress:
            on_progress("step_start", {
                "step": "analyze_reference",
                "label": "Analyze Reference Video",
                "message": "Analyzing reference video...",
            })
        if simulation:
            reference_video_structure = {"scenes": 5, "transitions": "dissolve", "scene_count": 5}
            logger.info(f"   [Row {row_num}] [SIM] Reference video structure: 5 scenes")
        else:
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(suffix=".mp4")
                os.close(fd)
                if FFmpegProcessor.download_video(video_reference_url.strip(), temp_path):
                    logger.info(f"   [Row {row_num}] Analyzing reference video structure...")
                    processor.reset_usage()
                    reference_video_structure = analyze_reference_video_structure(processor.gemini_service._provider, temp_path, llm_logger=processor.llm_logger)
                    if reference_video_structure:
                        logger.info(f"   [Row {row_num}] Reference structure: {reference_video_structure.get('scene_count', 0)} scenes")
                    else:
                        logger.info(f"   [Row {row_num}] Reference structure empty or failed, continuing without")
                else:
                    logger.warning(f"   [Row {row_num}] Could not download reference video, continuing without structure")
            except Exception as e:
                logger.warning(f"   [Row {row_num}] Error with reference video: {e}, continuing without structure")
            finally:
                if temp_path and os.path.isfile(temp_path):
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
    elif video_reference_url and video_reference_url.strip() and skip_character_and_analyze_media:
        logger.info(f"   [Row {row_num}] Skipping reference video analysis (Studio fast path)")

    # --- Callback: analyze_reference step_complete + usage ---
    if video_reference_url and video_reference_url.strip() and not skip_character_and_analyze_media:
        if on_progress:
            on_progress("step_complete", {
                "step": "analyze_reference",
                "label": "Analyze Reference Video",
                "progress": 12,
                "message": "Reference video analyzed",
            })
            emit_llm_usage_events(processor, on_progress, usage_list, "analyze_reference")

    # =====================================================================
    # STEP 2.7: Generate VO FIRST (before scene prompts) so scene timing matches audio
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "vo_generation",
            "label": "Voice Over Generation",
            "message": "Generating voiceover...",
        })
    vo_result = {"script": None, "audio_url": None, "segments": None, "audio_urls": None, "word_segments": None}
    vo_audio_url_early = None
    vo_word_segments_early = []
    vo_duration_seconds = 0.0

    # Check for existing VO intermediates (checkpoint/resume)
    _vo_from_intermediates = False
    if "vo_script" in intermediates and "vo_audio_url" in intermediates and "vo_word_segments" in intermediates:
        vo_result["script"] = intermediates["vo_script"]
        vo_audio_url_early = intermediates["vo_audio_url"]
        vo_word_segments_early = intermediates["vo_word_segments"] or []
        vo_result["audio_url"] = vo_audio_url_early
        vo_result["word_segments"] = vo_word_segments_early
        if vo_word_segments_early:
            vo_duration_seconds = max((ws["end_time"] for ws in vo_word_segments_early), default=0)
        logger.info(f"   [Row {row_num}] Using existing intermediates: vo_script, vo_audio_url, vo_word_segments (VO duration={vo_duration_seconds:.1f}s)")
        _vo_from_intermediates = True

    combined_script = ""  # Defined outside try so it's accessible for VO pre-splitting later
    if _vo_from_intermediates:
        combined_script = vo_result["script"] or ""

    # Resume after Studio pause: vo_script in job but no audio yet — run ElevenLabs only.
    _vo_script_resume_tts = bool(
        generate_vo
        and not _vo_from_intermediates
        and (intermediates.get("vo_script") or "").strip()
        and not intermediates.get("vo_audio_url")
    )

    if generate_vo and (not _vo_from_intermediates or _vo_script_resume_tts):
        logger.info(f"   [Row {row_num}] Step 2.7: Generating VO FIRST (target ~{target_duration}s)...")
        try:
            existing_vo_script = ""
            if vo_script_col is not None and vo_script_col < len(_row):
                existing_vo_script = _row[vo_script_col].strip()

            # Voice selection (before VO generation / TTS)
            _product_voice = None
            if prefetched_product_voice is not None:
                _product_voice = prefetched_product_voice
            elif voice_id:
                _product_voice = voice_id
                logger.info(f"Using provided voice_id: {voice_id}")
            else:
                try:
                    _vid_col = processor._get_col_safe(headers, config.VOICE_ID_COLUMN) if headers else None
                    if _vid_col is not None and _vid_col < len(_row):
                        _sheet_vid = _row[_vid_col].strip()
                        if _sheet_vid:
                            _product_voice = _sheet_vid
                except (ValueError, Exception):
                    pass
                if not _product_voice:
                    from tvd_pipeline.data_loader import get_language_voice
                    lang_voice = get_language_voice(subtitle_language, "male")
                    if lang_voice:
                        _product_voice = lang_voice
                        logger.info(f"   Using language-specific voice: {_product_voice}")
                if not _product_voice:
                    if not simulation:
                        _product_voice = processor.elevenlabs_service.pick_random_voice(gender="male", language=subtitle_language) or config.DEFAULT_VOICE_ID
                    else:
                        _product_voice = config.DEFAULT_VOICE_ID

            calibrated_wps = None
            # Skip ElevenLabs WPS calibration when Studio defers TTS, on resume-TTS, or already done in parallel prefetch.
            _skip_wps_calibration = bool(
                vo_script_only or _vo_script_resume_tts or (prefetched_vo_script is not None)
            )
            if not simulation and _product_voice and not existing_vo_script and not _skip_wps_calibration:
                from tvd_pipeline.data_loader import prepare_wps_sample_text
                sample = prepare_wps_sample_text(prompt)
                calibrated_wps = processor.elevenlabs_service.calibrate_voice_wps(
                    sample_text=sample, voice_id=_product_voice, language=subtitle_language
                )
                if on_progress and calibrated_wps:
                    on_progress("usage", {
                        "service": "elevenlabs", "step": "tts_calibration",
                        "model": get_elevenlabs_config()["tts_model"], "provider": "elevenlabs",
                        "character_count": len(sample),
                        "label": "Voice WPS calibration", "category": "tts", "success": True,
                    })

            combined_script = ""
            if _vo_script_resume_tts:
                combined_script = str(intermediates.get("vo_script") or "").strip()
                logger.info(f"   [Row {row_num}] Resume: using vo_script from job intermediates ({len(combined_script)} chars) → TTS only")
            elif existing_vo_script:
                combined_script = existing_vo_script
                logger.info(f"   [Row {row_num}] Using existing VO script ({len(combined_script)} chars)")
            elif simulation:
                combined_script = (
                    f"[Sim] This is a {target_duration} second voiceover script for the product. "
                    f"||| It highlights key features and benefits. "
                    f"||| The visual narrative brings the product to life. "
                    f"||| And concludes with a compelling call to action."
                )
                logger.info(f"   [Row {row_num}] [SIM] Generated VO script ({len(combined_script.split())} words)")
            elif prefetched_vo_script:
                combined_script = prefetched_vo_script
                logger.info(
                    f"   [Row {row_num}] Using parallel-prefetched VO script "
                    f"({len(combined_script.split())} words, targeting {target_duration}s)"
                )
            else:
                processor.reset_usage()
                combined_script = _generate_vo_script_single(processor,
                    text_1, text_2, text_3, [], target_duration,
                    language=subtitle_language, country=country,
                    raw_prompt=prompt, text_4=text_4,
                    wps_override=calibrated_wps,
                    on_progress=on_progress,
                )
                if combined_script:
                    logger.info(f"   [Row {row_num}] Generated VO script from prompt ({len(combined_script.split())} words, targeting {target_duration}s)")
                    try:
                        processor.sheets_service.update_cell(
                            config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                            config.VO_SCRIPT_COLUMN, combined_script, headers
                        )
                    except Exception:
                        pass

            if combined_script:
                vo_result["script"] = combined_script

            # Studio: show script first; TTS after user approves / resume (matches wrapper vo_script_only).
            _defer_tts = bool(vo_script_only and not _vo_script_resume_tts)
            if _defer_tts and combined_script:
                logger.info(f"   [Row {row_num}] vo_script_only=True — script saved; TTS runs after user resume or Generate VO in Studio.")

            if combined_script and not _defer_tts:
                script_for_tts = script_only_for_tts(combined_script) or combined_script

                if simulation:
                    words = script_for_tts.replace("|||", "").split()
                    wps = get_speech_rate(subtitle_language)
                    vo_word_segments_early = [{"text": w, "start_time": i / wps, "end_time": (i + 1) / wps} for i, w in enumerate(words)]
                    vo_audio_url_early = _SIM_AUDIO
                    vo_result["audio_url"] = vo_audio_url_early
                    vo_result["word_segments"] = vo_word_segments_early
                    vo_duration_seconds = len(words) / wps
                    logger.info(f"   [Row {row_num}] [SIM] VO ready: {len(words)} words, duration={vo_duration_seconds:.1f}s")
                else:
                    tts_result = processor.elevenlabs_service.text_to_speech_with_timestamps(
                        text=script_for_tts,
                        voice_id=_product_voice,
                        language=subtitle_language
                    )
                    if tts_result:
                        vo_audio_data, vo_word_segments_early = tts_result
                        vo_key = f"product_videos/row_{row_num}_vo_{int(time.time())}.mp3"
                        vo_audio_url_early = processor.gcs_storage_service.upload_audio_bytes(
                            audio_data=vo_audio_data, key_name=vo_key
                        )
                        if vo_audio_url_early:
                            vo_result["audio_url"] = vo_audio_url_early
                            if vo_word_segments_early:
                                vo_result["word_segments"] = vo_word_segments_early
                                vo_duration_seconds = max((ws["end_time"] for ws in vo_word_segments_early), default=0)
                                logger.info(f"   [Row {row_num}] VO ready: {len(vo_word_segments_early)} words, duration={vo_duration_seconds:.1f}s")
                            try:
                                processor.sheets_service.update_cell(
                                    config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                                    config.NEW_VOICE_COLUMN, vo_audio_url_early, headers
                                )
                            except Exception:
                                pass
                    else:
                        logger.warning(f"   [Row {row_num}] TTS generation failed")
        except Exception as e:
            logger.warning(f"   [Row {row_num}] VO generation error: {e}")

    # Store VO intermediates for checkpoint/resume
    if vo_result.get("script"):
        intermediates["vo_script"] = vo_result["script"]
    if vo_audio_url_early:
        intermediates["vo_audio_url"] = vo_audio_url_early
    if vo_word_segments_early:
        intermediates["vo_word_segments"] = vo_word_segments_early

    # --- Callback: emit intermediates first so wrapper saves before pause (step_complete triggers pause) ---
    if on_progress:
        if vo_result.get("script"):
            on_progress("intermediate", {"key": "vo_script", "value": vo_result["script"]})
        if vo_audio_url_early:
            on_progress("intermediate", {"key": "vo_audio_url", "value": vo_audio_url_early})
        if vo_word_segments_early:
            on_progress("intermediate", {"key": "vo_word_segments", "value": vo_word_segments_early})
        if bool(vo_script_only) and not (vo_result.get("script") or "").strip():
            err = (
                "VO script is empty after the generation step (check TEXT 1/2/3, LLM logs, or generate_vo). "
                "Cannot pause for Studio review without a script."
            )
            logger.error("   [Row %s] %s", row_num, err)
            raise RuntimeError(err)
        on_progress("step_complete", {
            "step": "vo_generation",
            "label": "Voice Over Generation",
            "progress": 20,
            "message": "Voice over generated",
        })
        # Usage: VO script generation (per-model attribution)
        if vo_result.get("script"):
            emit_llm_usage_events(processor, on_progress, usage_list, "vo_script")
        # Usage: TTS (ElevenLabs)
        if vo_audio_url_early:
            usage_data = {
                "service": "elevenlabs", "step": "tts",
                "model": get_elevenlabs_config()["tts_model"], "provider": "elevenlabs",
                "character_count": len(vo_result.get("script", "")),
                "label": "Text-to-speech", "category": "tts", "success": True,
            }
            on_progress("usage", usage_data)
            usage_list.append(usage_data)

    # =====================================================================
    # CRITICAL: Override target_duration to match VO when VO is longer
    # If the VO is longer than the target_duration from the sheet, the video
    # MUST be at least as long as the VO. Otherwise scenes get too few/too long
    # and animation clips can't be stretched enough (max 2x slow-motion).
    # =====================================================================
    if vo_duration_seconds > 0 and vo_duration_seconds > target_duration:
        old_target = target_duration
        target_duration = int(vo_duration_seconds) + 2  # VO + small buffer
        logger.info(f"   [Row {row_num}] ⚠️ VO ({vo_duration_seconds:.1f}s) is longer than target ({old_target}s) → target_duration updated to {target_duration}s")
    
    # Build VO timing info for scene generation (so Gemini can match visuals to audio)
    # Pre-split the VO into scene groups using '|||' separators from the structured VO script
    vo_timing_for_scenes = None
    if vo_word_segments_early and vo_duration_seconds > 0:
        full_vo_text = " ".join(ws["text"] for ws in vo_word_segments_early)
        
        # Try to pre-split VO into scenes using '|||' markers from the structured script
        vo_scene_segments = []
        target_scene_count = _estimate_scene_count_from_text4(processor, text_4, target_duration)
        # Require at least 60% of target scene count, minimum 5 for videos > 25s
        min_acceptable_segments = max(5 if vo_duration_seconds > 25 else 3, int(target_scene_count * 0.6))
        if combined_script and "|||" in combined_script:
            vo_scene_segments = _presplit_vo_into_scenes(processor, 
                combined_script, vo_word_segments_early, vo_duration_seconds
            )
            if vo_scene_segments:
                if len(vo_scene_segments) < min_acceptable_segments:
                    # Too few ||| segments for this video length - fall back to sentence splitting
                    logger.warning(f"   [Row {row_num}] VO has only {len(vo_scene_segments)} ||| segments but need ~{target_scene_count} scenes → falling back to sentence splitting")
                    vo_scene_segments = []  # Reset so fallback triggers
                else:
                    logger.info(f"   [Row {row_num}] Pre-split VO into {len(vo_scene_segments)} scene segments using ||| markers")
                    for i, seg in enumerate(vo_scene_segments):
                        logger.info(f"   [Row {row_num}]   Scene {i+1}: '{seg['text'][:50]}...' ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s, {seg['word_count']} words)")
        
        # Fallback: split at sentence boundaries if no ||| markers or too few ||| segments
        if not vo_scene_segments:
            vo_scene_segments = _presplit_vo_at_sentences(processor, 
                vo_word_segments_early, target_scene_count, vo_duration_seconds
            )
            if vo_scene_segments:
                logger.info(f"   [Row {row_num}] Pre-split VO into {len(vo_scene_segments)} scene segments at sentence boundaries")
        
        vo_timing_for_scenes = {
            "total_duration": round(vo_duration_seconds, 2),
            "word_count": len(vo_word_segments_early),
            "full_text": full_vo_text,
            "segments": vo_word_segments_early,
            "scene_segments": vo_scene_segments  # Pre-split scene groups with text + timestamps
        }
    
    # =====================================================================
    # STEP 3: Generate scene prompts with Gemini (using VO timing)
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "scene_prompts",
            "label": "Scene Prompts",
            "message": "Generating scene prompts...",
        })
    logger.info(f"   [Row {row_num}] Step 3: Generating scene prompts (with VO timing)...")

    if "scene_prompts" in intermediates and intermediates["scene_prompts"]:
        scenes = intermediates["scene_prompts"]
        music_style = intermediates.get("music_style", "")
        result["scene_prompts"] = scenes
        logger.info(f"   [Row {row_num}] Using existing intermediate: scene_prompts ({len(scenes)} scenes)")
    else:
        scenes = None
        music_style = ""

    if scenes is None:
        try:
            if simulation:
                num_scenes = max(3, target_duration // 5)
                scenes_list = []
                for i in range(num_scenes):
                    scenes_list.append({
                        "scene_number": i + 1,
                        "image_prompt": f"[Sim] Scene {i + 1} image prompt for product video",
                        "motion_prompt": f"[Sim] Scene {i + 1} motion prompt with smooth camera movement",
                        "duration": round(target_duration / num_scenes, 1),
                    })
                scene_data = {"scenes": scenes_list, "music_style": "Upbeat corporate background music"}
                logger.info(f"   [Row {row_num}] [SIM] Generated {num_scenes} scene prompts")
            else:
                processor.reset_usage()
                scene_data = generate_product_video_scenes(
                    lambda msgs, **kw: processor._call_llm("generate_scenes", msgs, **kw),
                    text_1=text_1,
                    text_2=text_2,
                    text_3=text_3,
                    text_4=text_4,
                    prompt=prompt,
                    image_urls=valid_image_urls,
                    target_duration=target_duration,
                    character_description=character_description,
                    character_urls=character_urls,
                    logo_url=logo_url,
                    slogan_text=slogan_text,
                    reference_video_structure=reference_video_structure,
                    language=subtitle_language,
                    country=country,
                    vo_timing=vo_timing_for_scenes,
                    no_on_screen_character=bool(product_no_on_screen_character),
                )

            scenes = scene_data.get("scenes", [])
            music_style = scene_data.get("music_style", "")
        
            if not scenes:
                error = "No scenes generated from prompts"
                logger.error(f"   [Row {row_num}] {error}")
                result["errors"].append(error)
                return result
        
            # CRITICAL: Cap scenes to VO segment count so video length matches VO.
            # If Gemini returns more scenes than VO segments, only the first N get VO-synced
            # durations; the rest would be dropped or misaligned, producing a shorter video.
            expected_count = None
            if vo_timing_for_scenes and vo_timing_for_scenes.get("scene_segments"):
                expected_count = len(vo_timing_for_scenes["scene_segments"])
            if expected_count is not None and len(scenes) > expected_count:
                logger.warning(f"   [Row {row_num}] Gemini returned {len(scenes)} scene prompts but VO has {expected_count} segments → using first {expected_count} only (video length will match VO)")
                scenes = scenes[:expected_count]
        
            result["scene_prompts"] = scenes
            logger.info(f"   [Row {row_num}] Generated {len(scenes)} scene prompts (aligned with VO segments)")
        
            # POST-PROCESS: Assign exact durations from ElevenLabs VO timestamps.
            # Strategy:
            # 1. Try Gemini's vo_word_start/vo_word_end → map to ElevenLabs times
            # 2. If Gemini mapping is broken/missing → auto-distribute words evenly across scenes
            # 3. Tile scenes continuously (no gaps); last scene extends to VO end + buffer
            VO_END_BUFFER = 1.0  # extra seconds after VO ends so video outlasts audio
        
            if vo_word_segments_early and len(vo_word_segments_early) > 0:
                ws = vo_word_segments_early
                num_words = len(ws)
                vo_end = vo_duration_seconds
                n_scenes = len(scenes)
            
                # --- Step A: Try Gemini word indices ---
                gemini_indices = []
                gemini_valid = True
                for scene in scenes:
                    w_start = scene.get("vo_word_start")
                    w_end = scene.get("vo_word_end")
                    if w_start is not None and w_end is not None:
                        try:
                            w_s = max(0, min(int(w_start), num_words - 1))
                            w_e = max(0, min(int(w_end), num_words - 1))
                            if w_e < w_s:
                                w_e = w_s
                            gemini_indices.append((w_s, w_e))
                        except (TypeError, ValueError):
                            gemini_indices.append(None)
                            gemini_valid = False
                    else:
                        gemini_indices.append(None)
                        gemini_valid = False
            
                # Validate Gemini indices: must be sequential, cover all words, no big gaps
                if gemini_valid and all(g is not None for g in gemini_indices):
                    # Check sequential order
                    prev_w = -1
                    for (ws_idx, we_idx) in gemini_indices:
                        if ws_idx < prev_w:
                            gemini_valid = False
                            break
                        prev_w = we_idx
                    # Check coverage: first scene starts near 0, last scene ends near last word
                    if gemini_valid:
                        first_start = gemini_indices[0][0]
                        last_end = gemini_indices[-1][1]
                        if first_start > 5 or last_end < num_words - 10:
                            gemini_valid = False
                            logger.warning(f"   [Row {row_num}] Gemini word indices don't cover full VO (first={first_start}, last={last_end}/{num_words-1})")
            
                # --- Step B: If Gemini indices are bad, auto-distribute words evenly ---
                if not gemini_valid or any(g is None for g in gemini_indices):
                    logger.info(f"   [Row {row_num}] Auto-distributing {num_words} words across {n_scenes} scenes (Gemini mapping incomplete)")
                    words_per_scene = num_words / n_scenes
                    gemini_indices = []
                    for i in range(n_scenes):
                        w_s = int(round(i * words_per_scene))
                        w_e = int(round((i + 1) * words_per_scene)) - 1
                        w_e = min(w_e, num_words - 1)
                        w_s = min(w_s, num_words - 1)
                        gemini_indices.append((w_s, w_e))
            
                # --- Step C: Assign durations based on sync_strategy ---
                _min_scene_dur = _p_defaults.get("min_scene_duration", 1.0)

                if sync_strategy == "phrase_start":
                    # Phase 12: Each scene extends to start_time of next scene's first word
                    logger.info(f"   [Row {row_num}] Using phrase_start sync strategy")
                    _apply_phrase_start_strategy(
                        scenes=scenes,
                        word_timestamps=ws,
                        gemini_indices=gemini_indices,
                        vo_duration=vo_end,
                        last_scene_buffer=VO_END_BUFFER,
                        min_scene_duration=_min_scene_dur,
                    )
                    for i, scene in enumerate(scenes):
                        w_s, w_e = gemini_indices[i]
                        logger.info(f"   [Row {row_num}] Scene {scene.get('scene_num', '?')}: "
                                  f"words [{w_s}-{w_e}] → {scene.get('vo_start_time', '?')}s-{scene.get('vo_end_time', '?')}s ({scene.get('duration', '?')}s) [phrase_start]")
                else:
                    # Default continuous strategy: tile scenes back-to-back
                    prev_end = 0.0
                    for i, scene in enumerate(scenes):
                        w_s, w_e = gemini_indices[i]
                        actual_start = prev_end
                        # Scene ends at the end_time of its last word
                        raw_end = ws[w_e]["end_time"]
                        if i == n_scenes - 1:
                            # Last scene: extend to VO end + buffer so video always outlasts VO
                            actual_end = vo_end + VO_END_BUFFER
                        else:
                            actual_end = raw_end

                        # Ensure minimum duration (at least 1s per scene)
                        if actual_end - actual_start < _min_scene_dur:
                            actual_end = actual_start + max(_min_scene_dur, scene.get("duration", 3.0))

                        scene_dur = round(actual_end - actual_start, 2)
                        scene["duration"] = scene_dur
                        scene["vo_start_time"] = round(actual_start, 3)
                        scene["vo_end_time"] = round(actual_end, 3)
                        logger.info(f"   [Row {row_num}] Scene {scene.get('scene_num', '?')}: "
                                  f"words [{w_s}-{w_e}] → {actual_start:.1f}s-{actual_end:.1f}s ({scene_dur:.1f}s)")
                        prev_end = actual_end

                total_scene_dur = sum(s.get("duration", 0) for s in scenes)
                logger.info(f"   [Row {row_num}] Scene durations (VO-synced, {sync_strategy}, no gaps): {total_scene_dur:.1f}s "
                          f"(VO: {vo_duration_seconds:.1f}s + {VO_END_BUFFER}s buffer = video ends after VO)")

                # Phase 12: When precision sync is active, compute exact_duration
                # and overgenerate_duration for each scene so the animation request
                # uses ceil(duration) and the clip is later trimmed to exact ms.
                if sync_method == "precision":
                    for _s in scenes:
                        _dur = _s.get("duration", 3.0)
                        if "exact_duration" not in _s:
                            _s["exact_duration"] = round(_dur, 3)
                        if "overgenerate_duration" not in _s:
                            _s["overgenerate_duration"] = math.ceil(_dur)
                    logger.info(f"   [Row {row_num}] Precision sync: overgenerate durations = {[s['overgenerate_duration'] for s in scenes]}")

            # Write scene prompts to individual columns (Scene 1 - First prompt, Scene 1 - Second prompt, etc.)
            for i, scene in enumerate(scenes[:config.MAX_SCENES]):
                scene_num = i + 1
                image_prompt = scene.get("image_prompt", "")
                motion_prompt = scene.get("motion_prompt", "")
            
                # Write First prompt (image prompt)
                first_prompt_col = config.SCENE_FIRST_PROMPT_PREFIX.format(n=scene_num)
                try:
                    processor.sheets_service.update_cell(
                        config.GOOGLE_SHEET_ID,
                        config.GOOGLE_SHEET_TAB,
                        row_num,
                        first_prompt_col,
                        image_prompt,
                        headers
                    )
                except Exception:
                    pass
            
                # Write Second prompt (motion prompt)
                second_prompt_col = config.SCENE_SECOND_PROMPT_PREFIX.format(n=scene_num)
                try:
                    processor.sheets_service.update_cell(
                        config.GOOGLE_SHEET_ID,
                        config.GOOGLE_SHEET_TAB,
                        row_num,
                        second_prompt_col,
                        motion_prompt,
                        headers
                    )
                except Exception:
                    pass
        
            logger.info(f"   [Row {row_num}] Wrote prompts to Scene columns")

            # --- Callback: emit intermediate first so wrapper saves before pause (step_complete triggers pause) ---
            if on_progress:
                on_progress("intermediate", {"key": "scene_prompts", "value": scenes})
                on_progress("step_complete", {
                    "step": "scene_prompts",
                    "label": "Scene Prompts",
                    "progress": 25,
                    "message": "Scene prompts generated",
                })
                emit_llm_usage_events(processor, on_progress, usage_list, "scene_prompts")

        except Exception as e:
            error = f"Error generating scene prompts: {str(e)}"
            logger.error(f"   [Row {row_num}] {error}")
            result["errors"].append(error)
            return result
    else:
        # Loaded from existing_intermediates (seed job / resume). The fresh-generation path emits
        # step_complete here so the wrapper can pause at pause_after_step=step_3 (Studio phase 3a).
        # Without this event, pause never matches and the pipeline runs through to the final video.
        if on_progress:
            on_progress("intermediate", {"key": "scene_prompts", "value": scenes})
            on_progress("step_complete", {
                "step": "scene_prompts",
                "label": "Scene Prompts",
                "progress": 25,
                "message": "Scene prompts loaded from checkpoint",
            })

    # =====================================================================
    # PRE-COMPENSATE SCENE DURATIONS FOR DISSOLVE OVERLAP
    # =====================================================================
    # Use provided dissolve or config default
    effective_dissolve = dissolve_seconds if dissolve_seconds is not None else _p_defaults.get("dissolve_seconds", 0.075)
    dissolve_sec = effective_dissolve
    total_clips = len(scenes)
    num_dissolves = max(0, total_clips - 1)
    dissolve_loss = round(num_dissolves * dissolve_sec, 2)
    if dissolve_loss > 0 and total_clips > 0:
        per_clip_add = round(dissolve_loss / total_clips, 3)
        for scene in scenes:
            old_dur = scene.get("duration", 4.0)
            scene["duration"] = round(old_dur + per_clip_add, 2)
        logger.info(f"   [Row {row_num}] Dissolve pre-compensation: {num_dissolves} transitions × {dissolve_sec}s = {dissolve_loss:.1f}s → +{per_clip_add:.2f}s/scene")

    # =====================================================================
    # STEPS 4-7: PARALLEL ASSET GENERATION
    # - Track 1: Image → Video pipeline (each scene: generate image, then video)
    # - Track 2: Music generation (Suno)
    # - Track 3: Voice over generation (ElevenLabs)
    # All three tracks run in parallel for maximum speed
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "scene_generation",
            "label": "Scene Generation",
            "message": "Generating scene images and videos...",
        })
    style_info = f", style={visual_style}" if visual_style != "Auto" else ""
    logger.info(f"   [Row {row_num}] Steps 4-7: Starting PARALLEL asset generation...{style_info}")
    
    # Helper function to get reference URLs for a scene
    def _ref_urls_for_scene(product_visible_flag):
        if not product_visible_flag:
            return None
        if clean_product_url:
            return [clean_product_url]
        if valid_image_urls:
            return valid_image_urls[:2]
        return None
    
    # Shared results containers (thread-safe via GIL for simple assignments)
    scene_images = [None] * len(scenes)
    scene_videos = [None] * len(scenes)
    scene_vo_audios = [None] * len(scenes)  # Legacy; not used with single VO
    music_result = {"url": None, "description": None}

    # --- existing_intermediates skip logic for scene_images/scene_videos/music_url ---
    _skip_images = False
    _skip_videos = False
    if "scene_images" in intermediates and intermediates["scene_images"]:
        cached_imgs = intermediates["scene_images"]
        for idx, url in enumerate(cached_imgs):
            if idx < len(scene_images):
                scene_images[idx] = url
        _skip_images = True
        logger.info(f"Using cached scene_images from existing_intermediates ({len(cached_imgs)} images)")
    if "scene_videos" in intermediates and intermediates["scene_videos"]:
        cached_vids = intermediates["scene_videos"]
        for idx, url in enumerate(cached_vids):
            if idx < len(scene_videos):
                scene_videos[idx] = url
        _skip_videos = True
        logger.info(f"Using cached scene_videos from existing_intermediates ({len(cached_vids)} videos)")
    if "music_url" in intermediates and intermediates["music_url"]:
        music_result["url"] = intermediates["music_url"]
        logger.info("Using cached music_url from existing_intermediates")
    # vo_result already populated in Step 2.7 above
    
    from tvd_pipeline.data_loader import get_veo3_config as _get_veo3_cfg
    from tvd_pipeline.pipelines._provider_limits import resolve_scene_video_limits, resolve_scene_image_workers, get_scene_image_stagger_seconds
    _veo_retry_cfg = _get_veo3_cfg().get("retry", {})
    _vid_conc, _scene_video_delay_sec = resolve_scene_video_limits(
        config, video_model, video_provider, _veo_retry_cfg
    )
    video_semaphore = threading.Semaphore(max(1, _vid_conc))
    logger.info(
        f"   [Row {row_num}] Scene video limits: {_vid_conc} concurrent, {_scene_video_delay_sec}s after each "
        f"({video_model!r} / {video_provider!r})"
    )
    image_workers, _img_api_label = resolve_scene_image_workers(
        config, use_google_image, use_kie_flash, image_model
    )
    scene_image_semaphore = threading.Semaphore(max(1, image_workers))
    scene_image_stagger_seconds = get_scene_image_stagger_seconds(use_google_image, use_kie_flash, image_model)
    if scene_image_stagger_seconds > 0:
        logger.info(f"   [Row {row_num}] Scene image stagger: {scene_image_stagger_seconds}s between start of each (Kie)")
    logger.info(f"   [Row {row_num}] Scene image parallelism: {image_workers} worker(s) ({_img_api_label})")
    
    # =====================================================================
    # TRACK 1: Image → Video Pipeline (per scene)
    # =====================================================================
    def generate_scene_visual(scene_idx, scene, is_last_scene=False):
        """Generate image then immediately send to video generation."""
        if scene_image_stagger_seconds > 0 and scene_idx > 0:
            time.sleep(scene_idx * scene_image_stagger_seconds)
        scene_num = scene.get("scene_num", scene_idx + 1)
        image_prompt, motion_prompt = unified_scene_image_motion_prompts(scene)
        duration = scene.get("duration", 3.0)
        # Phase 12: precision sync — over-generate with ceil(duration), trim later
        _exact_dur = scene.get("exact_duration")
        _use_precision = sync_method == "precision" and _exact_dur is not None
        anim_duration = scene.get("overgenerate_duration", math.ceil(duration)) if _use_precision else duration
        product_visible = scene.get("product_visible", False)
        has_character = scene.get("has_character", False)
        narrative_role = scene.get("narrative_role", "")

        if not image_prompt:
            return (scene_idx, None, None)

        # Simulation: skip real image + video generation
        if simulation:
            scene_images[scene_idx] = _SIM_IMAGE
            scene_videos[scene_idx] = _SIM_VIDEO
            logger.info(f"   [Row {row_num}] Scene {scene_num}: [SIM] Image + Video generated")
            return (scene_idx, _SIM_IMAGE, _SIM_VIDEO)

        ref_urls = _ref_urls_for_scene(product_visible)

        # Only the actual last scene is CTA (logo/slogan). Do NOT use narrative_role "cta" for middle scenes.
        is_cta_scene = is_last_scene
        scene_logo_url = logo_url if is_cta_scene else None

        # If logo is provided for CTA scene, integrate logo INTO the image
        if scene_logo_url and is_cta_scene:
            image_prompt = f"{image_prompt}\n\nThis is the call-to-action ending scene. IMPORTANT: Integrate the provided logo image naturally into this scene. Place the logo prominently but elegantly - it can be on a product, wall, screen, or floating with a subtle glow effect. Add a short catchy slogan text near the logo. Make the logo the focal point of this closing scene while maintaining professional aesthetics."

        # Step 1: Generate image (or use cached from Studio / existing_intermediates)
        image_url = None
        if _skip_images and scene_images[scene_idx]:
            image_url = scene_images[scene_idx]
            logger.info(f"   [Row {row_num}] Scene {scene_num}: Using cached image from existing_intermediates — animating")
        last_image_error = None
        scene_image_retry_wait = getattr(config, "SCENE_IMAGE_RETRY_WAIT_SEC", 45)
        if not image_url:
            for scene_attempt in range(2):
                try:
                    scene_image_kw = dict(
                        image_prompt=image_prompt,
                        product_reference_urls=ref_urls,
                        product_description=text_1 if product_visible else None,
                        product_visible=product_visible,
                        visual_style=visual_style,
                        character_reference_urls=character_urls if (has_character and character_urls) else None,
                        has_character=has_character,
                        logo_reference_url=scene_logo_url,
                        is_cta_scene=is_cta_scene
                    )
                    with scene_image_semaphore:
                        image_url = processor._generate_image(
                            image_model=image_model,
                            image_provider=image_provider,
                            resolution=image_resolution,
                            **scene_image_kw,
                        )
                    if image_url:
                        scene_images[scene_idx] = image_url
                        logger.info(f"   [Row {row_num}] Scene {scene_num}: Image generated")
                        # Quality gate: evaluate and retry once if below threshold
                        if quality_check and not simulation:
                            try:
                                quality_score = evaluate_image_quality(
                                    lambda msgs, **kw: processor._call_llm("image_quality_check", msgs, **kw),
                                    image_url=image_url, original_prompt=image_prompt,
                                )
                                if quality_score is not None and quality_score < image_quality_threshold:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: Low quality image (score {quality_score}/10), regenerating...")
                                    with scene_image_semaphore:
                                        retry_url = processor._generate_image(
                                            image_model=image_model,
                                            image_provider=image_provider,
                                            resolution=image_resolution,
                                            **scene_image_kw,
                                        )
                                    if retry_url:
                                        image_url = retry_url
                                        scene_images[scene_idx] = image_url
                                        logger.info(f"   [Row {row_num}] Scene {scene_num}: Quality gate retry succeeded")
                                    else:
                                        logger.info(f"   [Row {row_num}] Scene {scene_num}: Quality gate retry failed, keeping original")
                            except Exception as qe:
                                logger.debug(f"   [Row {row_num}] Scene {scene_num}: Quality check failed: {qe}")
                        # Update sheet
                        if scene_num <= config.MAX_SCENES:
                            try:
                                processor.sheets_service.update_cell(
                                    config.GOOGLE_SHEET_ID,
                                    config.GOOGLE_SHEET_TAB,
                                    row_num,
                                    config.SCENE_NEW_IMAGE_PREFIX.format(n=scene_num),
                                    image_url,
                                    headers
                                )
                            except Exception:
                                pass
                        if on_progress:
                            try:
                                on_progress(
                                    "intermediate",
                                    {"key": "scene_images", "value": [u if u else None for u in scene_images]},
                                )
                            except Exception as _ie:
                                logger.debug(f"   [Row {row_num}] scene_images partial emit: {_ie}")
                        break
                except Exception as e:
                    last_image_error = str(e)
                    logger.error(f"   [Row {row_num}] Scene {scene_num}: Image error - {e}")
                    if scene_attempt == 1:
                        return (scene_idx, None, None)
                if not image_url and scene_attempt == 0:
                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: Image failed, retrying once after {scene_image_retry_wait}s...")
                    time.sleep(scene_image_retry_wait)

        if not image_url:
            if last_image_error:
                reason = f" (last error: {last_image_error[:200]})"
            elif use_google_image:
                reason = " (" + (getattr(processor.gemini_image_service, "last_failure_reason", "") or "possible rate limit 429 or content/safety block") + ")"
            else:
                reason = " (Kie/Nano Banana)"
            logger.error(f"   [Row {row_num}] Scene {scene_num}: Image generation failed after 2 attempts - scene will be skipped{reason}")
            return (scene_idx, None, None)
        
        # Step 2: Generate video via unified _generate_video() dispatch
        video_url = None
        with video_semaphore:
            try:
                try:
                    video_url = processor._generate_video(
                        video_model=video_model,
                        video_provider=video_provider,
                        image_url=image_url,
                        motion_prompt=motion_prompt,
                        duration=anim_duration,
                        resolution=video_resolution,
                    )
                except VeoRAIBlockedError as rai_err:
                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: RAI blocked ({rai_err.reason}), retrying with softened prompt...")
                    softened_prompt = (
                        "Safe for all audiences. No violence, weapons, drugs, or explicit content. "
                        "Family-friendly commercial style. " + motion_prompt
                    )
                    video_url = processor._generate_video(
                        video_model=video_model,
                        video_provider=video_provider,
                        image_url=image_url,
                        motion_prompt=softened_prompt,
                        duration=anim_duration,
                        resolution=video_resolution,
                    )
                    if video_url:
                        logger.info(f"   [Row {row_num}] Scene {scene_num}: RAI retry succeeded")

                if video_url and video_model != "none":
                    # Trim the first 1 second from every animation (removes initial static/glitch frame)
                    try:
                        trim_url = f"{processor.rendi_service.base_url}/v1/run-ffmpeg-command"
                        trim_payload = {
                            "input_files": {"in_1": video_url},
                            "output_files": {"out_1": "trimmed_start.mp4"},
                            "ffmpeg_command": "-i {{in_1}} -ss 1.0 -c:v libx264 -preset fast -crf " + str(config.VIDEO_CRF) + " -an -movflags +faststart {{out_1}}",
                            "max_command_run_seconds": 60
                        }
                        trim_resp = requests.post(trim_url, headers=processor.rendi_service.headers, json=trim_payload, timeout=30)
                        if trim_resp.ok and "command_id" in trim_resp.json():
                            trimmed = processor.rendi_service._wait_for_command(trim_resp.json()["command_id"])
                            if trimmed:
                                video_url = trimmed
                                logger.info(f"   [Row {row_num}] Scene {scene_num}: Trimmed first 1s from animation")
                    except Exception as trim_err:
                        logger.warning(f"   [Row {row_num}] Scene {scene_num}: Could not trim first 1s: {trim_err}")

                    # Phase 12: Precision sync — trim to exact millisecond locally
                    if _use_precision and _exact_dur and video_url:
                        precision_url = _precision_trim_clip(
                            processor.gcs_storage_service,
                            video_url,
                            _exact_dur,
                            row_num=row_num,
                            scene_num=scene_num,
                        )
                        if precision_url:
                            video_url = precision_url
                            scene["_precision_trimmed"] = True

                if video_url:
                    scene_videos[scene_idx] = video_url
                    logger.info(f"   [Row {row_num}] Scene {scene_num}: Animation generated")
                    # Update sheet
                    if scene_num <= config.MAX_SCENES:
                        try:
                            processor.sheets_service.update_cell(
                                config.GOOGLE_SHEET_ID,
                                config.GOOGLE_SHEET_TAB,
                                row_num,
                                config.SCENE_NEW_VIDEO_PREFIX.format(n=scene_num),
                                video_url,
                                headers
                            )
                        except Exception:
                            pass
                elif video_model != "none":
                    logger.error(f"   [Row {row_num}] Scene {scene_num}: Animation generation failed")
            except Exception as e:
                logger.error(f"   [Row {row_num}] Scene {scene_num}: Animation error - {e}")
        # Release semaphore before spacing sleep so other scenes can use Vertex while this thread waits.
        if _scene_video_delay_sec > 0:
            time.sleep(_scene_video_delay_sec)

        return (scene_idx, image_url, video_url)

    # =====================================================================
    # TRACK 2: Music Generation
    # =====================================================================
    def generate_music_track():
        """Generate background music with Suno (music mood matches VO)."""
        logger.info(f"   [Row {row_num}] [Parallel] Starting music generation...")
        try:
            if simulation:
                music_result["url"] = _SIM_AUDIO
                logger.info(f"   [Row {row_num}] [SIM] Music generated")
                return
            music_description = music_style if music_style else generate_music_description_from_text(
                lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw),
                content_text=f"{text_1}\n{text_2}\n{text_3}",
                vo_script=vo_result.get("script", "") or "",
            )
            music_result["description"] = music_description
            music_url = processor.suno_service.generate_pure_music(
                style_description=music_description
            )
            if music_url:
                music_result["url"] = music_url
                logger.info(f"   [Row {row_num}] [Parallel] Music generated: {music_url[:60]}...")
                # Update sheet
                try:
                    processor.sheets_service.update_cell(
                        config.GOOGLE_SHEET_ID,
                        config.GOOGLE_SHEET_TAB,
                        row_num,
                        config.NEW_MUSIC_COLUMN,
                        music_url,
                        headers
                    )
                except Exception:
                    pass
            else:
                logger.warning(f"   [Row {row_num}] [Parallel] Music generation failed")
        except Exception as e:
            logger.warning(f"   [Row {row_num}] [Parallel] Music error: {e}")
    
    # =====================================================================
    # VO already generated in Step 2.7 – no TRACK 3 needed
    # =====================================================================
    
    # =====================================================================
    # RUN SCENES + MUSIC IN PARALLEL (VO already done in Step 2.7)
    # =====================================================================
    if _skip_images and _skip_videos and music_result["url"]:
        logger.info(
            f"   [Row {row_num}] Skipping parallel generation — using cached images, videos, "
            f"and music from intermediates"
        )
    elif _skip_images and _skip_videos and not music_result["url"]:
        logger.warning(
            f"   [Row {row_num}] Cached scene images/videos in intermediates but music_url is missing — "
            f"generating background music only (skipping scene re-generation)"
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            music_future = executor_submit_with_progress(executor, generate_music_track)
            music_future.result()
    else:
        if use_google_image:
            initial_delay = getattr(config, "GEMINI_IMAGE_INITIAL_DELAY_SEC", 65)
            logger.info(f"   [Row {row_num}] Waiting {initial_delay}s for Vertex image quota to reset after text generation...")
            time.sleep(initial_delay)

        logger.info(f"   [Row {row_num}] Launching parallel tracks: {len(scenes)} scenes + music (VO already generated)")

        with ThreadPoolExecutor(max_workers=len(scenes) + 1) as executor:
            # Always run generate_scene_visual; when _skip_images we use cached scene_images and only animate
            visual_futures = [
                executor_submit_with_progress(executor, generate_scene_visual, i, scene, i == len(scenes) - 1)
                for i, scene in enumerate(scenes)
            ]
            if not music_result["url"]:
                music_future = executor_submit_with_progress(executor, generate_music_track)
            else:
                music_future = None

            for future in as_completed(visual_futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"   [Row {row_num}] Visual task error: {e}")
                if on_progress and visual_futures:
                    try:
                        _sv_snap = [u if u else None for u in scene_videos]
                        on_progress("intermediate", {"key": "scene_videos", "value": _sv_snap})
                    except Exception as _pe:
                        logger.debug(f"   [Row {row_num}] scene_videos partial emit: {_pe}")

            if music_future:
                music_future.result()
    
    # =====================================================================
    # BARRIER: All parallel tasks (images, animations, music) are now
    # complete.  The ThreadPoolExecutor context-manager guarantees all
    # submitted threads have finished (shutdown(wait=True)).
    # =====================================================================
    animated_count = sum(1 for v in scene_videos if v)
    logger.info(f"   [Row {row_num}] ✅ All parallel tasks finished — {animated_count}/{len(scenes)} scenes animated, "
                 f"music={'✅' if music_result['url'] else '❌'}")
    
    # =====================================================================
    # FALLBACK: Create Ken Burns videos from static images for failed scenes
    # =====================================================================
    missing_video_indices = [i for i in range(len(scenes)) if scene_images[i] and not scene_videos[i]]
    if missing_video_indices:
        logger.warning(f"   [Row {row_num}] ⚠️ {len(missing_video_indices)}/{len(scenes)} scenes missing video — creating Ken Burns fallback from static images...")
        for i in missing_video_indices:
            scene_num = scenes[i].get("scene_number", scenes[i].get("scene_num", i + 1))
            dur = scenes[i].get("duration", 4.0)
            logger.info(f"   [Row {row_num}] Scene {scene_num}: Creating Ken Burns video from image ({dur:.1f}s)...")
            try:
                fallback_video = processor.rendi_service.create_video_from_image(
                    image_url=scene_images[i],
                    duration=dur
                )
                if fallback_video:
                    scene_videos[i] = fallback_video
                    logger.info(f"   [Row {row_num}] Scene {scene_num}: Fallback video created ✅")
                    if scene_num <= config.MAX_SCENES:
                        try:
                            processor.sheets_service.update_cell(
                                config.GOOGLE_SHEET_ID,
                                config.GOOGLE_SHEET_TAB,
                                row_num,
                                config.SCENE_NEW_VIDEO_PREFIX.format(n=scene_num),
                                fallback_video,
                                headers
                            )
                        except Exception:
                            pass
                else:
                    logger.error(f"   [Row {row_num}] Scene {scene_num}: Fallback video creation also failed")
            except Exception as e:
                logger.error(f"   [Row {row_num}] Scene {scene_num}: Fallback video error - {e}")
        # Notify the API wrapper that scenes fell back to Ken Burns
        if on_progress:
            on_progress("intermediate", {
                "key": "fallback_scenes",
                "value": [scenes[i].get("scene_number", scenes[i].get("scene_num", i + 1)) for i in missing_video_indices]
            })

    # Collect results — one slot per scene (nulls preserved) for wrapper/Studio index alignment
    result["scene_images"] = [u if u else None for u in scene_images]
    result["scene_videos"] = [u if u else None for u in scene_videos]
    skipped = [i + 1 for i in range(len(scenes)) if not scene_videos[i]]
    if skipped:
        logger.warning(f"   [Row {row_num}] Skipped scenes (no video): {skipped}")
    _n_img_done = sum(1 for u in scene_images if u)
    _n_vid_done = sum(1 for u in scene_videos if u)
    logger.info(
        f"   [Row {row_num}] Generated {_n_img_done}/{len(scenes)} images, {_n_vid_done}/{len(scenes)} videos"
    )

    # --- Callback: per-scene image step_complete + intermediate + usage ---
    if on_progress:
        _img_valid = [i for i, u in enumerate(scene_images) if u]
        for _idx, _si in enumerate(_img_valid):
            _img_progress = 30 + int(10 * (_idx + 1) / max(len(_img_valid), 1))
            on_progress("step_complete", {
                "step": f"scene_{_si + 1}_image",
                "label": f"Scene {_si + 1} Image",
                "progress": _img_progress,
                "message": f"Scene {_si + 1} image generated",
                "asset_url": scene_images[_si],
                "asset_type": "image",
            })
            usage_data = {
                "service": "gemini_image" if use_google_image else ("kie_flash" if use_kie_flash else "nano_banana"),
                "step": f"scene_{_si + 1}_image",
                "model": image_model or "nano-banana-pro",
                "provider": image_provider or "kie",
                "count": 1, "resolution": image_resolution or "1K",
                "label": f"Scene {_si + 1} image", "category": "images",
                "success": True,
            }
            on_progress("usage", usage_data)
            usage_list.append(usage_data)
        on_progress("intermediate", {"key": "scene_images", "value": result["scene_images"]})

    # --- Callback: per-scene video step_complete + intermediate + usage ---
    if on_progress:
        _vid_valid = [i for i, u in enumerate(scene_videos) if u]
        for _idx, _si in enumerate(_vid_valid):
            _vid_progress = 40 + int(25 * (_idx + 1) / max(len(_vid_valid), 1))
            _scene_dur = scenes[_si].get("duration", 4.0) if _si < len(scenes) else 4.0
            on_progress("step_complete", {
                "step": f"scene_{_si + 1}_video",
                "label": f"Scene {_si + 1} Video",
                "progress": _vid_progress,
                "message": f"Scene {_si + 1} video generated",
                "asset_url": scene_videos[_si],
                "asset_type": "video",
            })
            usage_data = {
                "service": "veo" if video_model and "veo" in video_model else ("kling" if video_model and "kling" in video_model else "runway"),
                "step": f"scene_{_si + 1}_video",
                "model": video_model or "runway",
                "provider": video_provider or "kie",
                "duration_seconds": _scene_dur,
                "resolution": video_resolution or "720p",
                "label": f"Scene {_si + 1} video ({_scene_dur:.0f}s)",
                "category": "videos", "success": True,
            }
            on_progress("usage", usage_data)
            usage_list.append(usage_data)
        on_progress("intermediate", {"key": "scene_videos", "value": result["scene_videos"]})

    # --- Callback: music step_complete + intermediate + usage ---
    if on_progress:
        on_progress("step_complete", {
            "step": "music",
            "label": "Background Music",
            "progress": 68,
            "message": "Background music generated",
            "asset_url": music_result.get("url"),
            "asset_type": "audio" if music_result.get("url") else None,
        })
        if music_result["url"]:
            on_progress("intermediate", {"key": "music_url", "value": music_result["url"]})
            if music_result.get("description"):
                on_progress("intermediate", {"key": "music_description", "value": music_result["description"]})
            usage_data = {
                "service": "suno", "step": "music",
                "model": "suno-v5", "provider": "kie",
                "count": 1,
                "label": "Background music", "category": "music", "success": True,
            }
            on_progress("usage", usage_data)
            usage_list.append(usage_data)

    if not result["scene_videos"]:
        error = "No scene videos generated"
        logger.error(f"   [Row {row_num}] {error}")
        result["errors"].append(error)
        return result

    # Store music and VO results
    if music_result["url"]:
        result["music_url"] = music_result["url"]

    vo_audio_url = vo_result.get("audio_url")
    vo_script = vo_result.get("script")
    if vo_script:
        result["vo_script"] = vo_script
    if vo_audio_url:
        result["vo_audio_url"] = vo_audio_url

    logger.info(f"   [Row {row_num}] Generation complete: music={bool(music_result['url'])}, VO={bool(vo_audio_url)} (VO generated in Step 2.7)")

    # Studio: pause after animations before Rendi concat (pause_after_step=step_12 maps here).
    if on_progress:
        on_progress("step_complete", {
            "step": "animations_review",
            "label": "Review scene animations",
            "progress": 72,
            "message": "Scene animations ready — approve in Studio to continue to final assembly",
        })
    
    # =====================================================================
    # STEP 8: Combine everything into final video using Rendi
    # Per-scene VO: Add VO to each scene BEFORE concatenation
    # =====================================================================

    # --- existing_intermediates skip logic for final_video_url ---
    if "final_video_url" in intermediates and intermediates["final_video_url"]:
        logger.info(f"   [Row {row_num}] Using cached final_video_url from existing_intermediates — skipping Steps 8-9")
        result["final_video_url"] = intermediates["final_video_url"]
        result["success"] = True
        result["usage"] = usage_list
        return result

    if on_progress:
        on_progress("step_start", {
            "step": "concat",
            "label": "Concatenate Videos",
            "message": "Concatenating video clips...",
        })
    logger.info(f"   [Row {row_num}] Step 8: Combining into final video...")

    try:
        # Check if we have per-scene VO audio
        has_per_scene_vo = bool(scene_vo_audios) and any(scene_vo_audios)
        has_single_vo = bool(vo_audio_url) and not has_per_scene_vo
        has_music = bool(result.get("music_url"))

        logger.info(f"   [Row {row_num}] Audio status: music={has_music}, per-scene VO={has_per_scene_vo}, single VO={has_single_vo}")
        
        # Build list by ORIGINAL scene index so VO and video stay paired when some scenes are skipped
        # Each item: (video_url, vo_url, scene_idx) for scenes that have a video
        scene_video_pairs = []
        for i in range(len(scenes)):
            if scene_videos[i]:
                vo_url = scene_vo_audios[i] if i < len(scene_vo_audios) else None
                scene_video_pairs.append((scene_videos[i], vo_url, i))
        
        if not scene_video_pairs:
            raise ValueError("No scene videos to concat")
        
        video_urls_for_concat = []
        if has_per_scene_vo:
            logger.info(f"   [Row {row_num}] Adding per-scene VO to each scene...")
            for video_url, scene_vo_url, scene_idx in scene_video_pairs:
                scene_num = scene_idx + 1
                if scene_vo_url:
                    try:
                        scene_with_vo = processor.rendi_service.add_audio_to_video(
                            video_url=video_url,
                            audio_url=scene_vo_url
                        )
                        if not scene_with_vo and FFmpegProcessor.check_ffmpeg_installed():
                            scene_with_vo = LocalFFmpegFallback.add_audio_to_video(
                                processor.gcs_storage_service, video_url, scene_vo_url
                            )
                            if scene_with_vo:
                                logger.info(f"   [Row {row_num}] Scene {scene_num}: VO added (local ffmpeg fallback)")
                        if scene_with_vo:
                            video_urls_for_concat.append(scene_with_vo)
                            logger.info(f"   [Row {row_num}] Scene {scene_num}: VO added")
                        else:
                            video_urls_for_concat.append(video_url)
                            logger.warning(f"   [Row {row_num}] Scene {scene_num}: VO add failed, using original")
                    except Exception as e:
                        video_urls_for_concat.append(video_url)
                        logger.warning(f"   [Row {row_num}] Scene {scene_num}: VO error: {e}")
                else:
                    video_urls_for_concat.append(video_url)
        else:
            video_urls_for_concat = [v for v, _, _ in scene_video_pairs]
        
        buffer_sec = getattr(config, "SCENE_BUFFER_SECONDS", 0.0) or 0.0
        n_scenes = len(video_urls_for_concat)
        # When per-scene VO: set each scene duration to VO length + 0.5s (use vo from scene_video_pairs)
        scene_durations_for_trim = None
        if has_per_scene_vo and scene_video_pairs and n_scenes > 0:
            vo_extra_sec = 0.5
            scene_durations_for_trim = []
            for idx, (_, vo_url, scene_idx) in enumerate(scene_video_pairs):
                base_d = scenes[scene_idx].get("duration", 3.0)
                if vo_url:
                    vo_dur = processor.rendi_service.get_audio_duration_cloud(vo_url)
                    if vo_dur <= 0 and FFmpegProcessor.check_ffmpeg_installed():
                        vo_dur = FFmpegProcessor.get_audio_duration(vo_url)
                    if vo_dur > 0:
                        base_d = max(vo_dur + vo_extra_sec, 1.0)
                        logger.info(f"   [Row {row_num}] Scene {scene_idx + 1}: duration from VO = {base_d:.2f}s (VO {vo_dur:.2f}s + {vo_extra_sec}s)")
                scene_durations_for_trim.append(base_d + (buffer_sec if idx < n_scenes - 1 and buffer_sec > 0 else 0))
        
        # Build video data with durations from VO-mapped timing (set in post-processing)
        # Uses EXACT ElevenLabs timestamps from vo_start_time/vo_end_time calculated in Step 3 post-processing.
        # IMPORTANT: If some scenes failed (no video), redistribute their VO time to remaining scenes
        # so the total video duration still matches the VO duration.
        video_data = []
        raw_durations = []
        
        # Log timing sync verification: confirm we use ElevenLabs-derived durations
        if vo_duration_seconds > 0:
            logger.info(f"   [Row {row_num}] TIMING SYNC: Using ElevenLabs-derived scene durations (VO total={vo_duration_seconds:.1f}s)")
            for i, scene in enumerate(scenes):
                vo_start = scene.get("vo_start_time", "?")
                vo_end = scene.get("vo_end_time", "?")
                dur = scene.get("duration", "?")
                logger.info(f"   [Row {row_num}]   Scene {i+1}: VO window={vo_start}s-{vo_end}s, duration={dur}s")
        
        for i, (video_url, _, scene_idx) in enumerate(scene_video_pairs):
            if scene_durations_for_trim and i < len(scene_durations_for_trim):
                duration = scene_durations_for_trim[i]
            else:
                duration = scenes[scene_idx].get("duration", 3.0) if scene_idx < len(scenes) else 3.0
            raw_durations.append(duration)
            video_data.append({
                "video_url": video_url,
                "duration": duration
            })
        
        # Check if scenes were skipped (failed) and compensate
        total_planned = sum(s.get("duration", 0) for s in scenes)
        total_actual = sum(raw_durations)
        missing_time = total_planned - total_actual
        if missing_time > 1.0 and len(video_data) > 0:
            # Distribute missing time across remaining scenes proportionally
            extra_per_scene = missing_time / len(video_data)
            logger.info(f"   [Row {row_num}] Compensating for skipped scenes: distributing {missing_time:.1f}s across {len(video_data)} scenes (+{extra_per_scene:.1f}s each)")
            for d in video_data:
                d["duration"] = round(d["duration"] + extra_per_scene, 2)
        
        # Ensure last scene covers at least until VO end + buffer
        if vo_duration_seconds > 0 and video_data:
            target_total = vo_duration_seconds + 1.5  # VO + buffer
            current_total = sum(d["duration"] for d in video_data)
            if current_total < target_total:
                shortfall = target_total - current_total
                video_data[-1]["duration"] = round(video_data[-1]["duration"] + shortfall, 2)
                logger.info(f"   [Row {row_num}] Extended last scene by {shortfall:.1f}s to reach {target_total:.1f}s (VO+buffer)")
        
        # Final timing verification log
        final_total = sum(d["duration"] for d in video_data)
        timing_drift = abs(final_total - (vo_duration_seconds + 1.5)) if vo_duration_seconds > 0 else 0
        logger.info(f"   [Row {row_num}] Scene durations for concat: {[round(d['duration'], 1) for d in video_data]} "
                   f"(total={final_total:.1f}s, VO={vo_duration_seconds:.1f}s, drift={timing_drift:.1f}s)")
        
        if simulation:
            # Skip trim/concat/VO mixing in simulation
            concat_video_url = _SIM_VIDEO
            logger.info(f"   [Row {row_num}] [SIM] Videos concatenated")
        else:
            # Phase 12: Check if ALL scenes were precision-trimmed locally — skip Rendi trim
            _all_precision = sync_method == "precision" and all(
                scenes[si].get("_precision_trimmed", False)
                for _, _, si in scene_video_pairs
                if si < len(scenes)
            )
            if _all_precision:
                logger.info(f"   [Row {row_num}] Precision sync: all clips pre-trimmed locally — skipping Rendi trim_videos_batch")
            else:
                # ALWAYS trim/slow-motion each clip to match its VO-based target duration
                # trim_videos_batch handles: longer clips → trim, shorter clips → slow motion (up to 2x)
                trim_durations = [d["duration"] for d in video_data]
                logger.info(f"   [Row {row_num}] Adjusting clips to VO-based durations (trim or slow-motion)...")
                adjusted_data = processor.rendi_service.trim_videos_batch(
                    video_data,
                    add_buffer_except_last=False,
                    videos_have_audio=has_per_scene_vo  # only keep audio if per-scene VO (usually False now)
                )
                if adjusted_data:
                    video_data = adjusted_data
                    actual_total = sum(d.get("duration", 0) for d in video_data)
                    logger.info(f"   [Row {row_num}] Clips adjusted: {[round(d['duration'], 1) for d in video_data]} (total={actual_total:.1f}s)")
                else:
                    logger.warning(f"   [Row {row_num}] trim_videos_batch failed, using raw clips")

            # Concatenate all scene videos (with VO already embedded per-scene)
            concat_video_url = processor.rendi_service.concatenate_videos(
                video_data=video_data,
                assume_clips_have_audio=has_per_scene_vo,
                dissolve_seconds=effective_dissolve,
            )
            if not concat_video_url and FFmpegProcessor.check_ffmpeg_installed():
                logger.info(f"   [Row {row_num}] Rendi concat failed, trying local ffmpeg...")
                concat_video_url = LocalFFmpegFallback.concat_video_only(processor.gcs_storage_service, video_data)
                if concat_video_url:
                    logger.info(f"   [Row {row_num}] Concat done via local ffmpeg")

        if concat_video_url:
            logger.info(f"   [Row {row_num}] Scenes concatenated")

            # --- Callback: concat step_complete + intermediate + usage ---
            if on_progress:
                on_progress("step_complete", {
                    "step": "concat",
                    "label": "Concatenate Videos",
                    "progress": 80,
                    "message": "Videos concatenated",
                })
                on_progress("intermediate", {"key": "concat_url", "value": concat_video_url})
                usage_data = {
                    "service": "rendi", "step": "concat",
                    "model": "rendi", "provider": "rendi",
                    "count": 1,
                    "label": "Concatenate videos", "category": "ffmpeg",
                    "success": True,
                }
                on_progress("usage", usage_data)
                usage_list.append(usage_data)

            # Film grain post-processing (applied to video track only, before VO+music)
            if film_grain and concat_video_url and not simulation:
                _fg_intensity = _p_defaults.get("film_grain_intensity", 3)
                logger.info(f"   [Row {row_num}] Applying film grain (intensity={_fg_intensity})...")
                try:
                    grain_url = processor.rendi_service.apply_ffmpeg_filter(
                        video_url=concat_video_url,
                        filter_string=f"noise=c0s={_fg_intensity}:c0f=t",
                    )
                    if grain_url:
                        concat_video_url = grain_url
                        logger.info(f"   [Row {row_num}] Film grain applied")
                        if on_progress:
                            usage_data = {
                                "service": "rendi", "step": "film_grain",
                                "model": "rendi", "provider": "rendi",
                                "count": 1,
                                "label": "Film grain", "category": "ffmpeg",
                                "success": True,
                            }
                            on_progress("usage", usage_data)
                            usage_list.append(usage_data)
                    else:
                        logger.warning(f"   [Row {row_num}] Film grain failed (Rendi returned None)")
                except Exception as fg_err:
                    logger.warning(f"   [Row {row_num}] Film grain error: {fg_err}")

            # Write to RENDI Scene column
            try:
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID,
                    config.GOOGLE_SHEET_TAB,
                    row_num,
                    config.RENDI_SCENE_COLUMN,
                    concat_video_url,
                    headers
                )
            except Exception:
                pass

            rendi_scene_voice_url = concat_video_url

            if on_progress:
                on_progress("step_start", {
                    "step": "audio_mix",
                    "label": "Audio Mix",
                    "message": "Mixing voiceover and music...",
                })
            if simulation:
                rendi_scene_voice_url = _SIM_VIDEO
                logger.info(f"   [Row {row_num}] [SIM] VO + music mixed")
            # If we used per-scene VO, now just add music (mix VO + music; do not replace)
            elif has_per_scene_vo:
                if has_music:
                    video_with_music = processor.rendi_service.add_background_music_to_video(
                        video_url=concat_video_url,
                        music_url=result["music_url"],
                        music_volume=0.2,
                        assume_has_audio=True
                    )
                    if not video_with_music and FFmpegProcessor.check_ffmpeg_installed():
                        video_with_music = LocalFFmpegFallback.add_music_to_video(
                            processor.gcs_storage_service, concat_video_url, result["music_url"], 0.2, assume_has_audio=True
                        )
                        if video_with_music:
                            logger.info(f"   [Row {row_num}] Music added (local ffmpeg fallback)")
                    if video_with_music:
                        rendi_scene_voice_url = video_with_music
                        logger.info(f"   [Row {row_num}] Music added to per-scene VO video")
                    else:
                        logger.warning(f"   [Row {row_num}] Failed to add music")
            
            # If we have single VO (fallback/legacy), add it with music
            elif has_single_vo:
                if has_music:
                    # Mix VO (loud) with music (quiet background)
                    video_with_both = processor.rendi_service.add_vo_and_music_to_video(
                        video_url=concat_video_url,
                        vo_url=vo_audio_url,
                        music_url=result["music_url"],
                        vo_volume=1.0,
                        music_volume=0.2
                    )
                    if video_with_both:
                        rendi_scene_voice_url = video_with_both
                        logger.info(f"   [Row {row_num}] Music + Voice over added")
                    else:
                        logger.warning(f"   [Row {row_num}] Failed to mix music + VO")
                else:
                    video_with_vo = processor.rendi_service.add_audio_to_video(
                        video_url=concat_video_url,
                        audio_url=vo_audio_url
                    )
                    if not video_with_vo and FFmpegProcessor.check_ffmpeg_installed():
                        video_with_vo = LocalFFmpegFallback.add_audio_to_video(
                            processor.gcs_storage_service, concat_video_url, vo_audio_url
                        )
                    if video_with_vo:
                        rendi_scene_voice_url = video_with_vo
                        logger.info(f"   [Row {row_num}] Voice over added")
            
            elif has_music:
                video_with_music = processor.rendi_service.add_background_music_to_video(
                    video_url=concat_video_url,
                    music_url=result["music_url"],
                    music_volume=0.3
                )
                if not video_with_music and FFmpegProcessor.check_ffmpeg_installed():
                    video_with_music = LocalFFmpegFallback.add_music_to_video(
                        processor.gcs_storage_service, concat_video_url, result["music_url"], 0.3
                    )
                if video_with_music:
                    rendi_scene_voice_url = video_with_music
                    logger.info(f"   [Row {row_num}] Music added")
            
            # Write to RENDI Scene & Voice column
            try:
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID,
                    config.GOOGLE_SHEET_TAB,
                    row_num,
                    config.RENDI_SCENE_VOICE_COLUMN,
                    rendi_scene_voice_url,
                    headers
                )
            except Exception:
                pass

            # --- Callback: audio_mix step_complete + intermediate + usage ---
            if on_progress:
                on_progress("step_complete", {
                    "step": "audio_mix",
                    "label": "Audio Mix",
                    "progress": 85,
                    "message": "Audio mixed",
                })
                on_progress("intermediate", {"key": "audio_mix_url", "value": rendi_scene_voice_url})
                usage_data = {
                    "service": "rendi", "step": "audio_mix",
                    "model": "rendi", "provider": "rendi",
                    "count": 1,
                    "label": "Mix VO and music", "category": "ffmpeg",
                    "success": bool(rendi_scene_voice_url),
                }
                on_progress("usage", usage_data)
                usage_list.append(usage_data)

            result["vo_audio_urls"] = [u for u in scene_vo_audios if u] if has_per_scene_vo else None
            
            logger.info(f"   [Row {row_num}] Video with audio: {rendi_scene_voice_url[:60]}...")
            
            # =====================================================================
            # STEP 8.5: TRIM FINAL VIDEO TO VO LENGTH + BUFFER
            # Ensures VO always finishes before the video ends (no trailing silence)
            # but never trims below the user's requested target_duration.
            # =====================================================================
            if vo_duration_seconds > 0 and rendi_scene_voice_url and not simulation:
                trim_target = max(vo_duration_seconds + 1.5, target_duration)  # VO + buffer, but never below requested duration
                # Get actual video duration to decide what to do
                actual_video_dur = processor.rendi_service.get_video_duration_cloud(rendi_scene_voice_url)
                if actual_video_dur <= 0:
                    logger.info(f"   [Row {row_num}] Step 8.5: Could not probe video duration, skipping adjustment")
                elif actual_video_dur > trim_target + 1.0:
                    # Video is LONGER than VO+buffer → trim it
                    logger.info(f"   [Row {row_num}] Step 8.5: Video too long ({actual_video_dur:.1f}s > {trim_target:.1f}s) → trimming")
                    try:
                        trimmed_final = processor.rendi_service.trim_video(
                            video_url=rendi_scene_voice_url,
                            duration=trim_target,
                            has_audio=True
                        )
                        if not trimmed_final and FFmpegProcessor.check_ffmpeg_installed():
                            trimmed_final = LocalFFmpegFallback.trim_video(
                                processor.gcs_storage_service, rendi_scene_voice_url, trim_target
                            )
                        if trimmed_final:
                            rendi_scene_voice_url = trimmed_final
                            logger.info(f"   [Row {row_num}] Final video trimmed to ~{trim_target:.1f}s")
                    except Exception as e:
                        logger.warning(f"   [Row {row_num}] Trim error: {e}")
                elif actual_video_dur < vo_duration_seconds - 1.0:
                    # Video is SHORTER than VO → slow down the whole video to match
                    speed_factor = actual_video_dur / trim_target  # e.g. 40s/56s = 0.71
                    speed_factor = max(0.5, speed_factor)  # Don't go below 0.5x (2x slow)
                    logger.info(f"   [Row {row_num}] Step 8.5: Video too short ({actual_video_dur:.1f}s < VO {vo_duration_seconds:.1f}s) → slowing to {speed_factor:.2f}x to reach ~{trim_target:.1f}s")
                    try:
                        slowed = processor.rendi_service.slow_motion_video(
                            video_url=rendi_scene_voice_url,
                            speed_factor=speed_factor,
                            target_duration=trim_target,
                            keep_audio=False  # VO will be re-added or is already there but we want video to match
                        )
                        if slowed:
                            # Re-add VO + music to the slowed video (original audio is distorted by slow-mo)
                            if has_single_vo and has_music:
                                slowed_with_audio = processor.rendi_service.add_vo_and_music_to_video(
                                    video_url=slowed, vo_url=vo_audio_url,
                                    music_url=result.get("music_url", ""), vo_volume=1.0, music_volume=0.2
                                )
                                if slowed_with_audio:
                                    rendi_scene_voice_url = slowed_with_audio
                                    logger.info(f"   [Row {row_num}] Video slowed + VO+music re-added → ~{trim_target:.1f}s")
                                else:
                                    rendi_scene_voice_url = slowed
                                    logger.warning(f"   [Row {row_num}] Slowed but could not re-add audio")
                            elif has_single_vo:
                                slowed_with_vo = processor.rendi_service.add_audio_to_video(
                                    video_url=slowed, audio_url=vo_audio_url
                                )
                                rendi_scene_voice_url = slowed_with_vo or slowed
                            else:
                                rendi_scene_voice_url = slowed
                            logger.info(f"   [Row {row_num}] Video extended to match VO duration")
                        else:
                            logger.warning(f"   [Row {row_num}] Slow-motion failed, VO may be cut off")
                    except Exception as e:
                        logger.warning(f"   [Row {row_num}] Slow-motion error: {e}")
                else:
                    logger.info(f"   [Row {row_num}] Step 8.5: Video={actual_video_dur:.1f}s, VO+buffer={trim_target:.1f}s → good match")

            # --- Callback: trim step_complete + usage ---
            if on_progress:
                on_progress("step_complete", {
                    "step": "trim",
                    "label": "Trim Video",
                    "progress": 90,
                    "message": "Video trimmed",
                })
                if vo_duration_seconds > 0:
                    usage_data = {
                        "service": "rendi", "step": "trim",
                        "model": "rendi", "provider": "rendi",
                        "count": 1,
                        "label": "Trim to VO length", "category": "ffmpeg",
                        "success": True,
                    }
                    on_progress("usage", usage_data)
                    usage_list.append(usage_data)

            # =====================================================================
            # STEP 9: ADD SUBTITLES WITH ZAPCAP (if requested)
            # Note: Logo is now integrated into the CTA scene image by Gemini
            # =====================================================================
            result["video_before_subtitles_url"] = rendi_scene_voice_url
            if on_progress:
                on_progress("intermediate", {"key": "video_before_subtitles_url", "value": rendi_scene_voice_url})
            if on_progress:
                on_progress("step_start", {
                    "step": "subtitles",
                    "label": "Subtitles",
                    "message": "Adding subtitles...",
                })
            final_video_for_output = rendi_scene_voice_url
            
            if add_subtitles and simulation:
                final_video_for_output = _SIM_VIDEO
                result["subtitled_video_url"] = _SIM_VIDEO
                logger.info(f"   [Row {row_num}] [SIM] Subtitles added")
            elif add_subtitles and processor.zapcap_service:
                logger.info(f"   [Row {row_num}] Step 9: Adding subtitles with ZapCap...")

                try:
                    # Use word segments from ElevenLabs TTS (Bring Your Own Transcript)
                    _zapcap_transcript = vo_result.get("word_segments") if vo_result else None
                    if _zapcap_transcript:
                        logger.info(f"   [Row {row_num}] Using ElevenLabs word segments for ZapCap BYOT ({len(_zapcap_transcript)} words)")
                    else:
                        logger.info(f"   [Row {row_num}] No word segments available, ZapCap will auto-transcribe")

                    # Enrich transcript with emoji + importance markers via LLM
                    _subtitle_enrichments = None
                    if subtitle_emoji and _zapcap_transcript:
                        try:
                            _normalized_for_enrich = processor.zapcap_service._normalize_transcript_for_zapcap(_zapcap_transcript)
                            if _normalized_for_enrich:
                                processor.reset_usage()
                                _subtitle_enrichments = enrich_transcript_for_subtitles(
                                    lambda msgs, **kw: processor._call_llm("enrich_subtitles", msgs, **kw),
                                    word_segments=_normalized_for_enrich,
                                    vo_script=vo_result.get("script", "") or "",
                                    language=subtitle_language,
                                    fallback_call_fn=lambda msgs, **kw: processor._call_llm("enrich_subtitles_fallback", msgs, **kw),
                                )
                                if on_progress:
                                    emit_llm_usage_events(processor, on_progress, usage_list, "enrich_subtitles")
                        except Exception as _enrich_err:
                            logger.warning(f"   [Row {row_num}] Subtitle enrichment failed (non-blocking): {_enrich_err}")

                    subtitled_video_url = processor.zapcap_service.add_subtitles(
                        video_url=rendi_scene_voice_url,
                        language=subtitle_language,
                        transcript=_zapcap_transcript,
                        enrichments=_subtitle_enrichments,
                        subtitle_position=subtitle_position,
                    )
                    if subtitled_video_url:
                        subtitled_video_url = processor.rendi_service.transcode_social_sharing_mp4(subtitled_video_url)
                    
                    if subtitled_video_url:
                        # Upload subtitled video to GCS for permanent storage
                        gcs_key = f"Comp/Final_Video/product_videos/row_{row_num}_subtitled_{int(time.time())}.mp4"
                        gcs_subtitled_url = processor.gcs_storage_service.upload_video_from_url(
                            source_url=subtitled_video_url, key_name=gcs_key
                        )
                        if gcs_subtitled_url:
                            logger.info(f"   [Row {row_num}] Subtitled video uploaded to GCS: {gcs_subtitled_url[:60]}...")
                            final_video_for_output = gcs_subtitled_url
                            result["subtitled_video_url"] = gcs_subtitled_url
                            if on_progress:
                                on_progress(
                                    "intermediate",
                                    {"key": "subtitled_video_url", "value": gcs_subtitled_url},
                                )
                        else:
                            logger.warning(f"   [Row {row_num}] Could not upload subtitled video to GCS, using ZapCap URL")
                            final_video_for_output = subtitled_video_url
                            result["subtitled_video_url"] = subtitled_video_url
                            if on_progress and subtitled_video_url:
                                on_progress(
                                    "intermediate",
                                    {"key": "subtitled_video_url", "value": subtitled_video_url},
                                )
                        
                        # Update Subtitled Video column
                        try:
                            processor.sheets_service.update_cell(
                                config.GOOGLE_SHEET_ID,
                                config.GOOGLE_SHEET_TAB,
                                row_num,
                                config.SUBTITLED_VIDEO_COLUMN,
                                subtitled_video_url,
                                headers
                            )
                        except Exception:
                            pass
                    else:
                        logger.warning(f"   [Row {row_num}] Failed to add subtitles, using video without subtitles")
                except Exception as e:
                    logger.warning(f"   [Row {row_num}] Error adding subtitles: {e}, using video without subtitles")
            
            elif add_subtitles and not processor.zapcap_service:
                logger.warning(f"   [Row {row_num}] Subtitles requested but ZapCap service not available")

            # --- Callback: subtitles step_complete + usage ---
            if on_progress:
                on_progress("step_complete", {
                    "step": "subtitles",
                    "label": "Subtitles",
                    "progress": 95,
                    "message": "Subtitles added",
                })
                if add_subtitles and result.get("subtitled_video_url"):
                    usage_data = {
                        "service": "zapcap", "step": "subtitles",
                        "model": "zapcap", "provider": "zapcap",
                        "duration_seconds": vo_duration_seconds,
                        "label": "Add subtitles", "category": "subtitles",
                        "success": True,
                    }
                    on_progress("usage", usage_data)
                    usage_list.append(usage_data)

            # Update final video URL to include subtitles if added
            result["final_video_url"] = final_video_for_output

            # --- Callback: final_video_url intermediate ---
            if on_progress:
                on_progress("intermediate", {"key": "final_video_url", "value": final_video_for_output})

            # Update sheet with final video (may include subtitles)
            try:
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID,
                    config.GOOGLE_SHEET_TAB,
                    row_num,
                    config.FINAL_VIDEO_COLUMN,
                    final_video_for_output,
                    headers
                )
            except Exception:
                pass

            result["success"] = True
        else:
            error = "Failed to concatenate scene videos"
            logger.error(f"   [Row {row_num}] {error}")
            result["errors"].append(error)
            
    except Exception as e:
        error = f"Error creating final video: {str(e)}"
        logger.error(f"   [Row {row_num}] {error}")
        result["errors"].append(error)

    result["usage"] = usage_list
    return result

