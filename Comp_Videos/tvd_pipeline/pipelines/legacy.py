"""Legacy video pipeline extracted from VideoSceneProcessor.
This pipeline takes an existing video URL as input, runs scene detection,
frame analysis via OpenAI, and regenerates each scene with new visuals.
Only used in Google Sheets mode (not available via API).
"""
import logging
import os
import time
import re
import tempfile
import shutil
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from tvd_pipeline.config import Config
from tvd_pipeline.services.ffmpeg_processor import FFmpegProcessor
from tvd_pipeline.services.tasks.video_analysis import (
    analyze_video_comprehensive, detect_product_in_frames,
    analyze_video_style, analyze_video_structure,
    analyze_full_video, analyze_scene_frames,
)
from tvd_pipeline.services.tasks.prompt_enhancement import enhance_prompt_with_product, enhance_motion_prompt_with_product
from tvd_pipeline.services.tasks.music import generate_music_description
from tvd_pipeline.services.tasks.voiceover import generate_vo_script_from_article
from tvd_pipeline.services.tasks.text_generation import generate_opening_text

logger = logging.getLogger(__name__)

# Module-level config singleton (same as monolith)
config = Config()

# Globals that may or may not be available at runtime
try:
    from scenedetect import detect, ContentDetector, AdaptiveDetector
    PYSCENEDETECT_AVAILABLE = True
except ImportError:
    PYSCENEDETECT_AVAILABLE = False

try:
    from tvd_pipeline.services.kie import remove_green_background
except ImportError:
    remove_green_background = None


def _detect_language(text: str) -> str:
    """Detect language from text. Thin wrapper around langdetect."""
    if not text or len(text.strip()) < 10:
        return "en"
    try:
        from langdetect import detect as langdetect_detect
        return langdetect_detect(text)
    except ImportError:
        if re.search(r"[֐-׿]", text):
            return "he"
        if re.search(r"[؀-ۿ]", text):
            return "ar"
        return "en"
    except Exception:
        return "en"


def process_single_video(
    processor, 
    video_url: str, 
    row_num: int,
    headers: List[str],
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
) -> Dict[str, Any]:
    """Process a single video through the entire pipeline (unified OpenAI flow).
    
    NEW FLOW:
    1. Download video
    2. Run PySceneDetect for initial scene timestamps
    3. Extract frames for ENTIRE video (1/sec)
    4. Send ALL frames + timestamps to OpenAI (single unified call)
    5. OpenAI returns corrected timestamps + scene prompts
    6. Process each scene with Nano Banana + Runway
    7. Concatenate and finalize
    8. Generate new music with Suno (if original has background music)
    9. Add subtitles with ZapCap (if requested)
    
    If article_text is provided, the pipeline adapts content to match the article.
    
    Args:
        video_url: URL of the video to process.
        row_num: Row number in the Google Sheet (1-based).
        headers: List of column headers.
        manual_instructions: Optional custom instructions for OpenAI analysis.
        cta_button: Whether to include a CTA button in image prompts.
        cta_text: Text for the CTA button.
        add_subtitles: Whether to add subtitles to the final video.
        article_text: Optional article content for content adaptation.
        vertical: Optional vertical/offer name for content adaptation.
        subtitle_language: Optional language code for ZapCap subtitles (e.g., 'de', 'en').
        manual_vo_text: Optional manual text for voice-over (overrides generated VO).
        manual_music_link: Optional manual music URL (overrides Suno generation).
        voice_id: Optional custom ElevenLabs voice ID (uses default if empty).
        
    Returns:
        Dict with processing results.
    """
    result = {
        "row": row_num,
        "video_url": video_url,
        "success": False,
        "scenes_processed": 0,
        "errors": [],
        "manual_instructions": manual_instructions,
        "cta_button": cta_button,
        "cta_text": cta_text,
        "add_subtitles": add_subtitles,
        "article_text": article_text,
        "vertical": vertical,
        "subtitle_language": subtitle_language,
        "manual_vo_text": manual_vo_text,
        "manual_music_link": manual_music_link
    }
    
    # =================================================================
    # ARTICLE ADAPTATION SETUP
    # =================================================================
    has_article_adaptation = bool(article_text.strip())
    article_language = "en"  # Default to English
    
    if has_article_adaptation:
        logger.info(f"📰 [Row {row_num}] Article adaptation mode enabled")
        # Priority: 1) subtitle_language from sheet, 2) detect from article text
        if subtitle_language:
            article_language = subtitle_language
            logger.info(f"🌍 Detected language: {article_language} (from sheet Language column)")
        else:
            article_language = _detect_language(article_text)
            logger.info(f"🌍 Detected language: {article_language}")
        logger.info(f"   Detected article language: {article_language}")
        if vertical:
            logger.info(f"   Vertical/Offer: {vertical}")
    
    # Set Rendi API key for cloud fallback
    FFmpegProcessor.set_rendi_api_key(config.RENDI_API_KEY)
    
    # Check if FFmpeg is available
    ffmpeg_available = FFmpegProcessor.check_ffmpeg_installed()
    
    # Create temp directory for processing (using mkdtemp for manual control)
    temp_dir = tempfile.mkdtemp()
    
    try:
        video_path = None
        
        # =================================================================
        # STEP 1: Download video
        # =================================================================
        logger.info(f"📥 [Row {row_num}] Step 1: Downloading video...")
        video_path = os.path.join(temp_dir, "input_video.mp4")
        download_success = FFmpegProcessor.download_video(video_url, video_path)
        
        if not download_success:
            logger.error(f"❌ [Row {row_num}] Failed to download video")
            result["errors"].append("Failed to download video")
            return result
        
        # =================================================================
        # STEP 2: Get video duration and run PySceneDetect
        # =================================================================
        logger.info(f"🎬 [Row {row_num}] Step 2: Detecting scenes with PySceneDetect...")
        
        # Get video duration using OpenCV (no FFmpeg needed)
        video_duration = 0
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                if fps > 0 and frame_count > 0:
                    video_duration = frame_count / fps
                cap.release()
        except Exception as e:
            logger.warning(f"⚠️ Could not get duration via OpenCV: {e}")
        
        if video_duration <= 0:
            video_duration = processor.rendi_service.get_video_duration_cloud(video_url)
        
        logger.info(f"   Video duration: {video_duration:.2f}s")
        
        # Run PySceneDetect for initial scene timestamps
        if PYSCENEDETECT_AVAILABLE:
            pyscenedetect_timestamps = FFmpegProcessor.detect_scenes(
                video_path,
                threshold=config.PYSCENEDETECT_THRESHOLD,
                min_scene_duration=config.PYSCENEDETECT_MIN_SCENE_DURATION,
                use_adaptive=config.PYSCENEDETECT_USE_ADAPTIVE
            )
        else:
            # Fallback: divide into equal segments
            num_segments = min(5, int(video_duration / 3))
            segment_duration = video_duration / num_segments
            pyscenedetect_timestamps = [i * segment_duration for i in range(num_segments)]
        
        logger.info(f"   PySceneDetect found {len(pyscenedetect_timestamps)} initial scenes")
        
        # =================================================================
        # STEP 3: Extract frames for ENTIRE video (1 per second)
        # =================================================================
        logger.info(f"🎬 [Row {row_num}] Step 3: Extracting frames for entire video...")
        
        frames_dir = os.path.join(temp_dir, "all_frames")
        os.makedirs(frames_dir, exist_ok=True)
        
        # Extract frames (1 per second)
        frames_with_timestamps = FFmpegProcessor.extract_frames_entire_video(
            video_path=video_path,
            video_duration=video_duration,
            output_dir=frames_dir,
            fps=5  # 1 frame per second
        )
        
        if not frames_with_timestamps:
            # Fallback to cloud extraction
            logger.info("🌐 Falling back to cloud frame extraction...")
            frames_with_timestamps = FFmpegProcessor.extract_frames_entire_video_cloud(
                video_url=video_url,
                video_duration=video_duration,
                output_dir=frames_dir,
                rendi_api_key=config.RENDI_API_KEY,
                fps=5
            )
        
        if not frames_with_timestamps:
            result["errors"].append("Failed to extract frames")
            return result
        
        logger.info(f"   Extracted {len(frames_with_timestamps)} frames")
        
        # =================================================================
        # STEP 3.35: EARLY AUDIO EXTRACTION & TRANSCRIPTION (NEW)
        # =================================================================
        # Extract and transcribe audio BEFORE Gemini analysis so we can
        # understand the audio-visual relationship and what's being said
        # =================================================================
        original_transcript = ""
        audio_path = os.path.join(temp_dir, "original_audio.mp3")
        
        logger.info(f"🎤 [Row {row_num}] Step 3.35: Extracting and transcribing audio...")
        
        try:
            # Try local extraction first
            audio_extracted = FFmpegProcessor.extract_audio(video_path, audio_path)
            
            if not audio_extracted:
                # Fallback to cloud extraction
                logger.info("🌐 Extracting audio via cloud...")
                original_audio_url = FFmpegProcessor.extract_audio_from_url(
                    video_url=video_url,
                    output_path=audio_path,
                    rendi_api_key=config.RENDI_API_KEY
                )
                
                if original_audio_url:
                    try:
                        response = requests.get(original_audio_url, timeout=60)
                        response.raise_for_status()
                        with open(audio_path, 'wb') as f:
                            f.write(response.content)
                        audio_extracted = True
                    except Exception:
                        pass
            
            # Transcribe if we have audio
            if audio_extracted and os.path.exists(audio_path):
                original_transcript = processor.elevenlabs_service.get_transcript_from_audio(audio_path) or ""
                if original_transcript:
                    logger.info(f"✅ [Row {row_num}] Transcribed: {original_transcript[:100]}...")
                else:
                    logger.info(f"ℹ️ [Row {row_num}] No speech detected in video")
            else:
                logger.warning(f"⚠️ [Row {row_num}] Could not extract audio for transcription")
                
        except Exception as audio_err:
            logger.warning(f"⚠️ [Row {row_num}] Audio extraction error: {audio_err}")
        
        # =================================================================
        # STEP 3.4: GEMINI COMPREHENSIVE VIDEO ANALYSIS (Native Video)
        # =================================================================
        # Gemini analyzes the ENTIRE video including:
        # - What's shown visually in each scene
        # - What's being said (from transcript)
        # - How audio relates to visuals
        # - Product appearance and usage
        # - Style, tone, and mood
        # =================================================================
        gemini_analysis = None
        
        if processor.gemini_service and processor.gemini_service.initialized:
            logger.info(f"🔮 [Row {row_num}] Step 3.4: Running Gemini comprehensive video analysis...")
            
            try:
                # Prepare article content from the article_text parameter
                article_content_for_gemini = {
                    'title': vertical or "",  # Use vertical as context
                    'first_paragraph': article_text[:500] if article_text else "",
                    'free_text': article_text or ""
                }
                
                # Run comprehensive video analysis WITH TRANSCRIPT
                # Pass subtitle_language to ensure VO script is in correct language
                # Pass article_related_to_video to control prompt generation strategy:
                # - True (Yes): Article is similar to video - adapt video for new offer/language
                # - False (No): Article is different - keep video style but create new content
                gemini_analysis = analyze_video_comprehensive(
                    processor.gemini_service._provider,
                    video_path=video_path,
                    article_content=article_content_for_gemini,
                    manual_instructions=manual_instructions,
                    target_language=subtitle_language or "en",  # Use Language column or default to English
                    original_transcript=original_transcript,  # Pass transcript to Gemini
                    article_related_to_video=article_related_to_video  # Controls prompt generation strategy
                )
                
                if gemini_analysis and gemini_analysis.get("scenes"):
                    logger.info(f"✅ [Row {row_num}] Gemini analysis complete (NEW FORMAT):")
                    logger.info(f"   - Video type: {gemini_analysis.get('video_story', {}).get('type', 'unknown')}")
                    logger.info(f"   - Scenes: {len(gemini_analysis.get('scenes', []))}")
                    logger.info(f"   - Product detected: {gemini_analysis.get('product', {}).get('detected', False)}")
                    logger.info(f"   - Style: {gemini_analysis.get('style', {}).get('aesthetic', 'modern')}")
                    
                    # Log new VO script
                    new_vo = gemini_analysis.get("new_voiceover", {})
                    if new_vo.get("full_script"):
                        logger.info(f"   - New VO: {new_vo.get('full_script', '')[:60]}...")
                        logger.info(f"   - VO style: {new_vo.get('style', 'unknown')}")
                    
                    # Log first scene prompt
                    scenes = gemini_analysis.get("scenes", [])
                    if scenes:
                        first_prompt = scenes[0].get('prompts', {}).get('image_prompt', '')
                        logger.info(f"   - First scene prompt: {first_prompt[:80]}...")
                    
                    # Store style prefix for later use
                    style_prefix = gemini_analysis.get("style", {}).get("style_prefix", "")
                    if style_prefix:
                        logger.info(f"   - Style prefix: {style_prefix[:100]}...")
                    
                    # =============================================================
                    # Extract and save product frames based on Gemini recommendations
                    # =============================================================
                    product_frames_urls = []
                    product = gemini_analysis.get("product", {})
                    recommended_timestamps = product.get("best_frame_timestamps", [])
                    
                    if recommended_timestamps and product.get("detected"):
                        logger.info(f"📸 [Row {row_num}] Extracting {len(recommended_timestamps)} product frames...")
                        
                        for i, timestamp in enumerate(recommended_timestamps[:5]):  # Max 5 frames
                            try:
                                # Parse timestamp (format: "0:02" or "1:30")
                                parts = timestamp.replace("s", "").split(":")
                                if len(parts) == 2:
                                    seconds = int(parts[0]) * 60 + float(parts[1])
                                else:
                                    seconds = float(parts[0])
                                
                                # Find the closest frame
                                # frames_with_timestamps is a list of tuples (timestamp, path)
                                frame_idx = int(seconds * 5)  # 5 fps
                                if frame_idx < len(frames_with_timestamps):
                                    # Access tuple: (timestamp, path)
                                    frame_timestamp, frame_path = frames_with_timestamps[frame_idx]
                                    
                                    # Read frame file and upload to GCS
                                    if frame_path and os.path.exists(frame_path):
                                        with open(frame_path, 'rb') as f:
                                            frame_data = f.read()
                                        
                                        product_frame_key = f"product_references/row_{row_num}_product_{i+1}.jpg"
                                        frame_url = processor.gcs_storage_service.upload_image_bytes(
                                            image_data=frame_data,
                                            key_name=product_frame_key,
                                            make_public=True
                                        )
                                        
                                        if frame_url:
                                            product_frames_urls.append(frame_url)
                                            logger.info(f"   ✅ Product frame {i+1} saved: {frame_url[:60]}...")
                            except Exception as frame_err:
                                logger.warning(f"   ⚠️ Could not extract frame at {timestamp}: {frame_err}")
                        
                        if product_frames_urls:
                            gemini_analysis["product_frame_urls"] = product_frames_urls
                            logger.info(f"✅ [Row {row_num}] Saved {len(product_frames_urls)} product reference frames")
                else:
                    logger.warning(f"⚠️ [Row {row_num}] Gemini analysis returned empty results")
                    
            except Exception as gemini_error:
                logger.warning(f"⚠️ [Row {row_num}] Gemini analysis failed: {gemini_error}")
                gemini_analysis = None
        else:
            logger.info(f"ℹ️ [Row {row_num}] Gemini not available, using GPT-4o for analysis")
        
        # =================================================================
        # STEP 3.5: Product Detection with Context Understanding (ENHANCED)
        # =================================================================
        # This step scans frames from across the ENTIRE video to:
        # 1. Identify the product (type, brand, visual details)
        # 2. Understand what the product DOES (purpose, usage method)
        # 3. Analyze HOW the product appears in different scenes (static, being applied, etc.)
        # NOTE: If Gemini analysis succeeded, we use its product_info instead
        # =================================================================
        product_info = {"has_product": False}
        product_reference_url = None
        
        # Use Gemini product info if available, otherwise use GPT-4o
        gemini_product = gemini_analysis.get("product", {}) if gemini_analysis else {}
        if gemini_product.get("detected"):
            logger.info(f"🔍 [Row {row_num}] Step 3.5: Using Gemini product analysis (skipping GPT)...")
            
            # Convert NEW Gemini format to our internal format
            product_info = {
                "has_product": True,
                "product_detected": gemini_product.get("type", "product"),
                "overall_confidence": 0.95,  # Gemini provides high confidence
                "product_description": gemini_product.get("visual_description", ""),
                "product_purpose": gemini_product.get("purpose", ""),
                "product_usage_method": gemini_product.get("usage_method", ""),
                "product_details": {
                    "application_rules": gemini_product.get("application_rules", ""),
                },
                "usage_contexts": [],  # Will be inferred from scenes
                "best_frame_index": 0
            }
            
            logger.info(f"   Product type: {product_info.get('product_detected')}")
            logger.info(f"   Description: {product_info.get('product_description', '')[:100]}...")
            logger.info(f"   Purpose: {product_info.get('product_purpose', '')[:100]}...")
            logger.info(f"   Usage method: {product_info.get('product_usage_method', '')[:100]}...")
            logger.info(f"   Application rules: {product_info.get('product_details', {}).get('application_rules', '')[:100]}...")
            
            # Generate clean product image from reference frames
            product_frame_urls = gemini_analysis.get("product_frame_urls", [])
            if product_frame_urls:
                logger.info(f"🧹 [Row {row_num}] Generating clean product image from {len(product_frame_urls)} reference frames...")
                
                # Get product description for the clean image generation
                product_desc = product_info.get("product_description", "")
                
                # Generate clean, isolated product image using Nano Banana
                clean_product_url = processor.kie_service.generate_clean_product_image(
                    reference_image_urls=product_frame_urls[:3],  # Use up to 3 reference frames
                    product_description=product_desc
                )
                
                if clean_product_url:
                    product_reference_url = clean_product_url  # Use clean image as reference
                    logger.info(f"   ✅ Clean product image generated: {product_reference_url[:60]}...")
                    
                    # Upload clean product image to GCS for persistence
                    try:
                        clean_product_key = f"product_references/row_{row_num}_clean_product.png"
                        # Download the clean image and re-upload to our GCS
                        import requests as req
                        clean_response = req.get(clean_product_url, timeout=30)
                        if clean_response.status_code == 200:
                            gcs_clean_url = processor.gcs_storage_service.upload_image_bytes(
                                image_data=clean_response.content,
                                key_name=clean_product_key,
                                make_public=True
                            )
                            if gcs_clean_url:
                                product_reference_url = gcs_clean_url
                                logger.info(f"   ✅ Clean product image saved to GCS: {gcs_clean_url[:60]}...")
                    except Exception as upload_err:
                        logger.warning(f"   ⚠️ Could not save clean product to GCS: {upload_err}")
                else:
                    # Fallback to first raw frame if clean generation failed
                    product_reference_url = product_frame_urls[0]
                    logger.warning(f"   ⚠️ Clean product generation failed, using raw frame: {product_reference_url[:60]}...")
            
        elif config.ENABLE_PRODUCT_DETECTION and frames_with_timestamps:
            logger.info(f"🔍 [Row {row_num}] Step 3.5: Detecting product and analyzing usage context...")
            
            try:
                # Select frames spread across the entire video for context understanding
                # Instead of just first N frames, sample frames from beginning, middle, and end
                total_frames = len(frames_with_timestamps)
                detection_frame_count = min(config.PRODUCT_DETECTION_FRAMES, total_frames)
                
                if total_frames >= 10:
                    # Evenly distribute frames across the entire video
                    # For 60 frames from 136 total: sample every ~2.3 frames
                    step = max(1, total_frames / detection_frame_count)
                    indices = []
                    for i in range(detection_frame_count):
                        idx = min(int(i * step), total_frames - 1)
                        if idx not in indices:
                            indices.append(idx)
                    
                    # Ensure we always include first and last frame
                    if 0 not in indices:
                        indices[0] = 0
                    if total_frames - 1 not in indices:
                        indices[-1] = total_frames - 1
                    
                    indices = sorted(set(indices))
                    detection_frames = [frames_with_timestamps[i][1] for i in indices]
                    logger.info(f"   Analyzing {len(detection_frames)} frames spread across entire video (every ~{step:.1f} frames)")
                else:
                    # Not enough frames, use all available
                    detection_frames = [f[1] for f in frames_with_timestamps[:detection_frame_count]]
                    logger.info(f"   Using first {len(detection_frames)} frames")
                
                # Comprehensive video analysis: product + narrative + audio correlation
                logger.info(f"   🎬 Running comprehensive video analysis with {len(detection_frames)} frames + audio transcript...")
                call_fn_detect = lambda msgs, **kw: processor._call_llm("detect_product", msgs, **kw)
                product_info = detect_product_in_frames(
                    call_fn_detect,
                    frame_paths=detection_frames,
                    min_confidence=config.PRODUCT_MIN_CONFIDENCE,
                    audio_transcript=original_transcript,
                    video_duration=video_duration
                )
                
                # Log comprehensive video analysis results
                if product_info.get("has_product"):
                    # Log video narrative
                    video_narrative = product_info.get("video_narrative", {})
                    if video_narrative:
                        logger.info(f"   🎬 VIDEO NARRATIVE:")
                        logger.info(f"      Type: {video_narrative.get('video_type', 'unknown')}")
                        logger.info(f"      Hook: {video_narrative.get('opening_hook', '')[:80]}...")
                        logger.info(f"      Story: {video_narrative.get('main_story', '')[:80]}...")
                        logger.info(f"      Style: {video_narrative.get('style', 'unknown')}")
                    
                    # Log sequential breakdown
                    sequential = product_info.get("sequential_breakdown", [])
                    if sequential:
                        logger.info(f"   📊 SEQUENTIAL BREAKDOWN ({len(sequential)} segments):")
                        for seg in sequential[:3]:
                            logger.info(f"      {seg.get('segment', '?')}: {seg.get('what_happens', '')[:60]}...")
                    
                    # Log audio-visual sync
                    av_sync = product_info.get("audio_visual_sync", [])
                    if av_sync:
                        logger.info(f"   🔊 AUDIO-VISUAL SYNC ({len(av_sync)} segments):")
                        for sync in av_sync[:2]:
                            logger.info(f"      VO: \"{sync.get('vo_text', '')[:50]}...\"")
                            logger.info(f"      Visual: {sync.get('visual_description', '')[:50]}...")
                    
                    # Log usage contexts
                    usage_contexts = product_info.get("usage_contexts", [])
                    if usage_contexts:
                        context_types = [c.get("context_type") for c in usage_contexts]
                        logger.info(f"   📋 Usage contexts found: {', '.join(context_types)}")
                    
                    # Log detailed product info
                    product_desc = product_info.get("product_description", "")
                    if product_desc:
                        logger.info(f"   📝 Product description ({len(product_desc)} chars): {product_desc[:150]}...")
                    
                    product_purpose = product_info.get("product_purpose", "")
                    if product_purpose:
                        logger.info(f"   🎯 Product purpose: {product_purpose[:150]}...")
                    
                    product_usage = product_info.get("product_usage_method", "")
                    if product_usage:
                        logger.info(f"   🔧 Usage method: {product_usage[:150]}...")
                    
                    # Log detailed product details
                    product_details = product_info.get("product_details", {})
                    if product_details:
                        shape = product_details.get("shape", "")
                        dims = product_details.get("dimensions", "")
                        if shape or dims:
                            logger.info(f"   📐 Shape/Size: {shape} | {dims}")
                    
                    # Log recreation notes
                    recreation_notes = product_info.get("recreation_notes", "")
                    if recreation_notes:
                        logger.info(f"   💡 Recreation notes: {recreation_notes[:150]}...")
                
                # Upload reference frames and generate clean product image if product detected
                if product_info.get("has_product"):
                    # Get up to 3 best frames for reference
                    best_frame_index = product_info.get("best_frame_index", 0)
                    
                    # Collect reference frames (best frame + adjacent frames)
                    ref_frame_paths = []
                    if best_frame_index is not None and 0 <= best_frame_index < len(detection_frames):
                        # Add best frame
                        ref_frame_paths.append(detection_frames[best_frame_index])
                        # Add adjacent frames if available
                        if best_frame_index > 0:
                            ref_frame_paths.append(detection_frames[best_frame_index - 1])
                        if best_frame_index < len(detection_frames) - 1:
                            ref_frame_paths.append(detection_frames[best_frame_index + 1])
                    
                    if ref_frame_paths:
                        # Upload reference frames to GCS
                        ref_frame_urls = []
                        for i, frame_path in enumerate(ref_frame_paths[:3]):
                            try:
                                with open(frame_path, 'rb') as f:
                                    frame_data = f.read()
                                frame_key = f"product_references/row_{row_num}_gpt_ref_{i+1}.jpg"
                                frame_url = processor.gcs_storage_service.upload_image_bytes(
                                    image_data=frame_data,
                                    key_name=frame_key,
                                    make_public=True
                                )
                                if frame_url:
                                    ref_frame_urls.append(frame_url)
                            except Exception as frame_err:
                                logger.warning(f"   ⚠️ Could not upload frame {i+1}: {frame_err}")
                        
                        if ref_frame_urls:
                            logger.info(f"🧹 [Row {row_num}] Generating clean product image from {len(ref_frame_urls)} GPT reference frames...")
                            
                            # Generate clean, isolated product image
                            product_desc = product_info.get("product_description", "")
                            clean_product_url = processor.kie_service.generate_clean_product_image(
                                reference_image_urls=ref_frame_urls,
                                product_description=product_desc
                            )
                            
                            if clean_product_url:
                                # Upload clean product to GCS
                                try:
                                    import requests as req
                                    clean_response = req.get(clean_product_url, timeout=30)
                                    if clean_response.status_code == 200:
                                        clean_key = f"product_references/row_{row_num}_clean_product.png"
                                        gcs_clean_url = processor.gcs_storage_service.upload_image_bytes(
                                            image_data=clean_response.content,
                                            key_name=clean_key,
                                            make_public=True
                                        )
                                        if gcs_clean_url:
                                            product_reference_url = gcs_clean_url
                                            logger.info(f"   ✅ Clean product image saved: {product_reference_url[:60]}...")
                                except Exception as upload_err:
                                    product_reference_url = clean_product_url
                                    logger.warning(f"   ⚠️ Could not save to GCS, using original: {upload_err}")
                            else:
                                # Fallback to first raw frame
                                product_reference_url = ref_frame_urls[0]
                                logger.warning(f"   ⚠️ Clean product generation failed, using raw frame")
                        else:
                            logger.warning(f"⚠️ [Row {row_num}] Failed to upload reference frames")
                    else:
                        logger.warning(f"⚠️ [Row {row_num}] No valid reference frames found")
                    
                    # Store product info in result for tracking
                    result["product_detected"] = True
                    result["product_type"] = product_info.get("product_detected", "unknown")
                    result["product_confidence"] = product_info.get("overall_confidence", 0)
                    
                    # Write product detection results to Google Sheet (ENHANCED)
                    try:
                        # Basic product info
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.PRODUCT_DETECTED_COLUMN,
                            value=product_info.get("product_detected", "unknown"),
                            headers=headers
                        )
                        if product_reference_url:
                            processor._update_sheet_cell(
                                row_num=row_num,
                                column=config.PRODUCT_REFERENCE_COLUMN,
                                value=product_reference_url,
                                headers=headers
                            )
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.PRODUCT_CONFIDENCE_COLUMN,
                            value=f"{product_info.get('overall_confidence', 0):.2f}",
                            headers=headers
                        )
                        
                        # NEW: Write product purpose (what the product does)
                        product_purpose = product_info.get("product_purpose", "")
                        if product_purpose:
                            processor._update_sheet_cell(
                                row_num=row_num,
                                column=config.PRODUCT_PURPOSE_COLUMN,
                                value=product_purpose[:500],  # Truncate if too long
                                headers=headers
                            )
                        
                        # NEW: Write product usage method (how it's applied)
                        product_usage = product_info.get("product_usage_method", "")
                        if product_usage:
                            processor._update_sheet_cell(
                                row_num=row_num,
                                column=config.PRODUCT_USAGE_COLUMN,
                                value=product_usage[:500],
                                headers=headers
                            )
                        
                        # NEW: Write usage contexts (how product appears in different scenes)
                        usage_contexts = product_info.get("usage_contexts", [])
                        if usage_contexts:
                            context_summary = ", ".join([
                                f"{c.get('context_type')}: {c.get('description', '')[:50]}"
                                for c in usage_contexts[:5]
                            ])
                            processor._update_sheet_cell(
                                row_num=row_num,
                                column=config.PRODUCT_CONTEXTS_COLUMN,
                                value=context_summary[:500],
                                headers=headers
                            )
                        
                        logger.info(f"✅ [Row {row_num}] Product detection results (incl. context) written to sheet")
                    except Exception as sheet_error:
                        logger.warning(f"⚠️ [Row {row_num}] Failed to write product info to sheet: {sheet_error}")
                else:
                    logger.info(f"ℹ️ [Row {row_num}] No product detected, continuing with standard flow")
                    
            except Exception as e:
                logger.error(f"❌ [Row {row_num}] Product detection failed: {e}")
                logger.info(f"   Continuing with standard flow...")
                product_info = {"has_product": False, "error": str(e)}
        
        # =================================================================
        # STEP 3.55: Video Style Analysis
        # =================================================================
        # Comprehensive visual style analysis to match the original video:
        # - Color palette, lighting, composition
        # - Camera style, mood, atmosphere
        # - Creates style guide for prompt generation
        # NOTE: Uses Gemini analysis if available, otherwise falls back to GPT-4o
        # =================================================================
        video_style = {}
        
        if gemini_analysis and gemini_analysis.get("style"):
            # Use Gemini's NEW visual style analysis
            logger.info(f"🎨 [Row {row_num}] Step 3.55: Using Gemini video style (skipping GPT)...")
            
            gemini_style = gemini_analysis.get("style", {})
            video_style = {
                "mood_atmosphere": {
                    "overall_mood": gemini_style.get("mood", "professional")
                },
                "overall_aesthetic": gemini_style.get("aesthetic", "modern"),
                "lighting": gemini_style.get("lighting", "natural"),
                "style_prompt_prefix": gemini_style.get("style_prefix", "")
            }
            
            # Store key style elements in result for reference
            result["video_style"] = {
                "aesthetic": gemini_style.get("aesthetic", "modern"),
                "lighting": gemini_style.get("lighting", "natural"),
                "mood": gemini_style.get("mood", "professional"),
                "style_prefix": gemini_style.get("style_prefix", "")
            }
            
            logger.info(f"✅ [Row {row_num}] Video style from Gemini:")
            logger.info(f"   - Aesthetic: {gemini_style.get('aesthetic', 'modern')}")
            logger.info(f"   - Lighting: {gemini_style.get('lighting', 'natural')}")
            logger.info(f"   - Mood: {gemini_style.get('mood', 'professional')}")
            logger.info(f"   - Style prefix: {gemini_style.get('style_prefix', 'N/A')[:60]}...")
            
        elif frames_with_timestamps:
            # Fallback to GPT-4o frame analysis
            logger.info(f"🎨 [Row {row_num}] Step 3.55: Analyzing video visual style with GPT-4o...")
            
            try:
                all_frame_paths = [f[1] for f in frames_with_timestamps]
                call_fn_style = lambda msgs, **kw: processor._call_llm("analyze_video_style", msgs, **kw)
                video_style = analyze_video_style(
                    call_fn_style,
                    frame_paths=all_frame_paths,
                    video_duration=video_duration
                )
                
                if video_style and not video_style.get("error"):
                    logger.info(f"✅ [Row {row_num}] Video style analyzed successfully")
                    
                    # Store key style elements in result for reference
                    result["video_style"] = {
                        "color_temperature": video_style.get("color_palette", {}).get("color_temperature", "neutral"),
                        "lighting": video_style.get("lighting", {}).get("type", "natural"),
                        "framing": video_style.get("composition", {}).get("primary_framing", "medium"),
                        "mood": video_style.get("mood_atmosphere", {}).get("overall_mood", "professional")
                    }
                else:
                    logger.warning(f"⚠️ [Row {row_num}] Style analysis failed, using defaults")
                    
            except Exception as style_error:
                logger.warning(f"⚠️ [Row {row_num}] Style analysis error: {style_error}")
                video_style = {}
        
        # =================================================================
        # STEP 3.6: Video Structure Analysis
        # =================================================================
        # Analyze video narrative structure considering:
        # - Article content (Free text, Title, 1stP, Rest of Content)
        # - Manual instructions
        # - Product information (if detected)
        # NOTE: Uses Gemini analysis if available, otherwise falls back to GPT-4o
        # =================================================================
        video_structure = {"video_structure": "unknown", "scene_plan": []}
        
        if gemini_analysis and gemini_analysis.get("scenes"):
            # Use Gemini's NEW scene structure (skipping GPT)
            logger.info(f"📊 [Row {row_num}] Step 3.6: Using Gemini scenes (skipping GPT)...")
            
            gemini_scenes = gemini_analysis.get("scenes", [])
            video_story = gemini_analysis.get("video_story", {})
            
            # Convert Gemini scenes to our scene_plan format
            scene_plan = []
            for scene in gemini_scenes:
                understanding = scene.get("understanding", {})
                prompts = scene.get("prompts", {})
                scene_plan.append({
                    "scene_number": scene.get("scene_number", 0),
                    "narrative_role": understanding.get("narrative_role", "content"),
                    "key_message": understanding.get("what_happens", "")[:100],
                    "visual_suggestion": prompts.get("image_prompt", ""),
                    "motion_prompt": prompts.get("motion_prompt", ""),
                    "product_visible": understanding.get("product_visible", False),
                    "product_action": understanding.get("product_action", ""),
                    "subject_appearance": understanding.get("subject_appearance", "")
                })
            
            video_structure = {
                "video_structure": video_story.get("type", "advertisement"),
                "narrative_summary": video_story.get("one_sentence_summary", ""),
                "scene_plan": scene_plan,
                "has_subject_changes": video_story.get("subject_changes", {}).get("has_visible_change", False),
                "start_state": video_story.get("subject_changes", {}).get("start_state", ""),
                "end_state": video_story.get("subject_changes", {}).get("end_state", "")
            }
            
            logger.info(f"✅ [Row {row_num}] Video structure from Gemini:")
            logger.info(f"   - Type: {video_structure.get('video_structure')}")
            logger.info(f"   - Scenes: {len(scene_plan)}")
            logger.info(f"   - Subject changes: {video_structure.get('has_subject_changes')}")
            if scene_plan:
                for sp in scene_plan[:3]:
                    logger.info(f"      Scene {sp.get('scene_number')}: {sp.get('narrative_role')} - {sp.get('key_message', '')[:40]}...")
                if len(scene_plan) > 3:
                    logger.info(f"      ... and {len(scene_plan) - 3} more scenes")
            
        elif config.ENABLE_PRODUCT_DETECTION and frames_with_timestamps:
            # Fallback to GPT-4o analysis
            logger.info(f"📊 [Row {row_num}] Step 3.6: Analyzing video structure with GPT-4o...")
            
            try:
                # Prepare article content dict from available parameters
                article_content = {
                    'free_text': article_text or "",
                    'title': vertical or "",
                    'first_paragraph': article_text[:500] if article_text else "",
                    'rest_content': article_text[500:] if article_text and len(article_text) > 500 else ""
                }
                
                # Get frame paths for analysis
                all_frame_paths = [f[1] for f in frames_with_timestamps]
                
                # Analyze video structure
                call_fn_struct = lambda msgs, **kw: processor._call_llm("analyze_video_structure", msgs, **kw)
                video_structure = analyze_video_structure(
                    call_fn_struct,
                    frame_paths=all_frame_paths,
                    article_content=article_content,
                    manual_instructions=manual_instructions,
                    product_info=product_info
                )
                
                # Log structure results
                if video_structure.get("video_structure") != "unknown":
                    logger.info(f"✅ [Row {row_num}] Video structure: {video_structure.get('video_structure')}")
                    logger.info(f"   Narrative: {video_structure.get('narrative_summary', '')[:80]}...")
                    scene_plan = video_structure.get("scene_plan", [])
                    if scene_plan:
                        logger.info(f"   Scene plan ({len(scene_plan)} scenes):")
                        for sp in scene_plan[:3]:
                            logger.info(f"      Scene {sp.get('scene_number')}: {sp.get('narrative_role')} - {sp.get('key_message', '')[:40]}...")
                        if len(scene_plan) > 3:
                            logger.info(f"      ... and {len(scene_plan) - 3} more scenes")
                else:
                    logger.info(f"ℹ️ [Row {row_num}] Could not determine video structure")
                    
            except Exception as e:
                logger.error(f"❌ [Row {row_num}] Video structure analysis failed: {e}")
                logger.info(f"   Continuing with standard flow...")
        
        # =================================================================
        # STEP 4: Scene Analysis (Gemini or OpenAI fallback)
        # =================================================================
        
        # Check if Gemini already provided all prompts
        gemini_scenes = gemini_analysis.get("scenes", []) if gemini_analysis else []
        gemini_has_prompts = gemini_scenes and all(
            s.get("prompts", {}).get("image_prompt") for s in gemini_scenes
        )
        
        if gemini_has_prompts:
            # USE GEMINI PROMPTS DIRECTLY - Skip OpenAI!
            logger.info(f"🎯 [Row {row_num}] Step 4: Using Gemini prompts directly (skipping OpenAI)...")
            
            # Convert Gemini scenes to our format
            corrected_scenes = []
            scene_prompts = []
            
            for gs in gemini_scenes:
                scene_num = gs.get("scene_number", 0)
                understanding = gs.get("understanding", {})
                prompts = gs.get("prompts", {})
                
                # Calculate approximate timestamps based on scene number
                # Use video_structure's scene_plan if available for better timing
                corrected_scenes.append({
                    "scene_num": scene_num,
                    "duration": gs.get("duration_seconds", 3)
                })
                
                scene_prompts.append({
                    "scene_num": scene_num,
                    "image_prompt": prompts.get("image_prompt", ""),
                    "motion_prompt": prompts.get("motion_prompt", ""),
                    "narrative_role": understanding.get("narrative_role", "content")
                })
            
            logger.info(f"   ✅ Using {len(gemini_scenes)} Gemini scene prompts")
            for sp in scene_prompts[:3]:
                logger.info(f"      Scene {sp['scene_num']}: {sp.get('image_prompt', '')[:50]}...")
        else:
            # FALLBACK: Use OpenAI to generate prompts
            logger.info(f"🤖 [Row {row_num}] Step 4: Fallback to OpenAI analysis...")
            if cta_button and cta_text:
                logger.info(f"   🔘 [Row {row_num}] Including CTA button in prompts: '{cta_text}'")
            
            call_fn_full = lambda msgs, **kw: processor._call_llm("analyze_full_video", msgs, **kw)
            openai_result = analyze_full_video(
                call_fn_full,
                frame_paths_with_timestamps=frames_with_timestamps,
                pyscenedetect_timestamps=pyscenedetect_timestamps,
                video_duration=video_duration,
                manual_instructions=manual_instructions,
                cta_button=cta_button,
                cta_text=cta_text,
                row_num=row_num,
                article_text=article_text,
                vertical=vertical,
                article_language=article_language,
                article_related_to_video=article_related_to_video
            )
            
            corrected_scenes = openai_result.get("corrected_scenes", [])
            scene_prompts = openai_result.get("scene_prompts", [])
        
        if not corrected_scenes:
            logger.error(f"❌ [Row {row_num}] No corrected scenes available")
            result["errors"].append("No corrected scenes available")
            return result
        
        logger.info(f"   [Row {row_num}] Using {len(corrected_scenes)} scenes with prompts")
        
        # Write prompts to Google Sheet
        for prompt_data in scene_prompts[:config.MAX_SCENES]:
            scene_num = prompt_data.get("scene_num", 0)
            if scene_num > 0:
                # Write image prompt
                first_prompt = prompt_data.get("image_prompt", "")
                if first_prompt:
                    processor._update_sheet_cell(
                        row_num=row_num,
                        column=config.SCENE_FIRST_PROMPT_PREFIX.replace("{n}", str(scene_num)),
                        value=first_prompt[:4000],  # Truncate if needed
                        headers=headers
                    )
                
                # Write motion prompt
                second_prompt = prompt_data.get("motion_prompt", "")
                if second_prompt:
                    processor._update_sheet_cell(
                        row_num=row_num,
                        column=config.SCENE_SECOND_PROMPT_PREFIX.replace("{n}", str(scene_num)),
                        value=second_prompt,
                        headers=headers
                    )
        
        # =================================================================
        # STEP 5: Process SCENES + AUDIO in PARALLEL
        # =================================================================
        # We run two parallel workflows:
        # A) Scene processing: Nano Banana → Runway for each scene
        # B) Audio processing: Extract → Detect speech → ElevenLabs + Suno
        # This saves significant time as audio takes ~2 minutes
        # =================================================================
        logger.info(f"🚀 [Row {row_num}] Step 5: Processing SCENES + AUDIO in PARALLEL...")
        
        # Build scene data from OpenAI results or Gemini scenes
        scene_data = []
        cumulative_time = 0.0  # Track cumulative time for Gemini scenes
        
        for i, scene in enumerate(corrected_scenes[:config.MAX_SCENES]):
            scene_num = scene.get("scene_num", i + 1)
            
            # Find matching prompts
            prompt_data = next(
                (p for p in scene_prompts if p.get("scene_num") == scene_num),
                {"image_prompt": "", "motion_prompt": ""}
            )
            
            # Determine duration: use from scene if available (Gemini), otherwise calculate from start/end
            if "duration" in scene:
                # Gemini scenes have duration directly
                scene_duration = scene.get("duration", 3.0)
                start_time = cumulative_time
                end_time = cumulative_time + scene_duration
                cumulative_time = end_time  # Update for next scene
            else:
                # OpenAI scenes have start/end
                start_time = scene.get("start", 0)
                end_time = scene.get("end", video_duration)
                scene_duration = end_time - start_time
            
            scene_data.append({
                "scene_num": scene_num,
                "start_time": start_time,
                "end_time": end_time,
                "duration": scene_duration,
                "image_prompt": prompt_data.get("image_prompt", ""),
                "motion_prompt": prompt_data.get("motion_prompt", "")
            })
            
            logger.info(f"   Scene {scene_num}: duration = {scene_duration:.2f}s (start: {start_time:.2f}s, end: {end_time:.2f}s)")
        
        # =================================================================
        # SCALE SCENE DURATIONS TO MATCH EXPECTED VO DURATION
        # =================================================================
        # This ensures the video is created at the correct length from the start,
        # so we don't need to apply slow motion or cut the video later
        if gemini_analysis:
            new_vo = gemini_analysis.get("new_voiceover", {})
            vo_script = new_vo.get("full_script", "")
            vo_word_count = new_vo.get("word_count", 0)
            
            if vo_script and not vo_word_count:
                vo_word_count = len(vo_script.split())
            
            if vo_word_count > 0:
                # Estimate VO duration: ~2.5 words per second for natural speech
                # Add a small buffer (0.5s) to ensure video is slightly longer than VO
                estimated_vo_duration = (vo_word_count / 2.5) + 0.5
                
                # Calculate current total scene duration
                current_total_duration = sum(s["duration"] for s in scene_data)
                
                if current_total_duration > 0 and abs(estimated_vo_duration - current_total_duration) > 1.0:
                    # Scale scene durations proportionally to match VO
                    scale_factor = estimated_vo_duration / current_total_duration
                    
                    # Only scale if within reasonable bounds (0.7x to 1.5x)
                    if 0.7 <= scale_factor <= 1.5:
                        logger.info(f"📐 Scaling scene durations to match expected VO duration...")
                        logger.info(f"   VO word count: {vo_word_count} words")
                        logger.info(f"   Estimated VO duration: {estimated_vo_duration:.2f}s")
                        logger.info(f"   Current total scene duration: {current_total_duration:.2f}s")
                        logger.info(f"   Scale factor: {scale_factor:.2f}x")
                        
                        cumulative_time = 0.0
                        for scene in scene_data:
                            old_duration = scene["duration"]
                            new_duration = old_duration * scale_factor
                            # Ensure minimum duration of 2s (Kling/Runway minimum)
                            new_duration = max(2.0, new_duration)
                            scene["duration"] = new_duration
                            scene["start_time"] = cumulative_time
                            scene["end_time"] = cumulative_time + new_duration
                            cumulative_time += new_duration
                            logger.info(f"   Scene {scene['scene_num']}: {old_duration:.2f}s → {new_duration:.2f}s")
                        
                        new_total_duration = sum(s["duration"] for s in scene_data)
                        logger.info(f"   ✅ New total scene duration: {new_total_duration:.2f}s (target VO: {estimated_vo_duration:.2f}s)")
                    else:
                        logger.info(f"ℹ️ Scale factor {scale_factor:.2f}x out of range (0.7-1.5), keeping original durations")
        
        scene_results = {}
        audio_result = {"new_voice_url": None, "new_music_url": None, "final_audio_url": None, "has_speech": False}
        
        def process_scene_with_prompts(scene_info):
            """Process a single scene with pre-generated prompts.
            
            If a product was detected, enhances the prompt to maintain product accuracy
            AND uses the appropriate usage context for the scene.
            """
            scene_num = scene_info["scene_num"]
            image_prompt = scene_info["image_prompt"]
            motion_prompt = scene_info["motion_prompt"]
            duration = scene_info["duration"]
            scene_start_time = scene_info.get("start_time", 0)
            
            result_data = {
                "scene_num": scene_num,
                "duration": duration,
                "image_url": None,
                "video_url": None
            }
            
            try:
                # Step 1: Generate image with Nano Banana
                if image_prompt:
                    # Check if Gemini provided ready-made prompts for this scene
                    gemini_scene_prompts = None
                    if gemini_analysis:
                        gemini_scenes = gemini_analysis.get("scenes", [])
                        for gs in gemini_scenes:
                            if gs.get("scene_number") == scene_num:
                                gemini_scene_prompts = gs.get("prompts", {})
                                break
                    
                    # USE GEMINI PROMPTS DIRECTLY if available
                    if gemini_scene_prompts and gemini_scene_prompts.get("image_prompt"):
                        final_image_prompt = gemini_scene_prompts.get("image_prompt")
                        final_motion_prompt = gemini_scene_prompts.get("motion_prompt", motion_prompt)
                        
                        # Check if product is visible in THIS specific scene
                        gemini_scene_info = None
                        for gs in gemini_scenes:
                            if gs.get("scene_number") == scene_num:
                                gemini_scene_info = gs.get("understanding", {})
                                break
                        
                        product_visible_in_scene = gemini_scene_info.get("product_visible", False) if gemini_scene_info else False
                        
                        # Check if Manual Instructions say to remove text
                        manual_instructions_lower = manual_instructions.lower() if manual_instructions else ""
                        should_remove_text = any(phrase in manual_instructions_lower for phrase in [
                            "remove text", "remove any text", "no text", "without text", 
                            "remove all text", "delete text", "הסר טקסט", "ללא טקסט"
                        ])
                        
                        if should_remove_text:
                            logger.info(f"📝 [Scene {scene_num}] Skipping text overlay - Manual Instructions say to remove text")
                        else:
                            # Check if original video has NO VO - if so, add text to image prompt
                            # Default to False - if Gemini didn't detect VO, assume there's no VO
                            original_has_vo = gemini_analysis.get("audio", {}).get("original_has_vo", False)
                            
                            # Also check if original video had text overlays
                            original_has_text = gemini_scene_info.get("has_branding_overlay", False) if gemini_scene_info else False
                            
                            # Only add text if: original had text AND original has no VO (text shown instead of spoken)
                            if original_has_text and not original_has_vo:
                                # Get text for this scene from Gemini analysis
                                scene_text = gemini_scene_info.get("text_on_screen", "") if gemini_scene_info else ""
                                
                                # Clean scene_text - remove "none", "None", "NONE", "no text", etc.
                                if scene_text:
                                    scene_text_lower = scene_text.lower().strip()
                                    # Remove common negative text indicators
                                    if scene_text_lower in ["none", "no text", "no", "n/a", "na", ""]:
                                        scene_text = ""
                                    # Remove "none" if it appears as a word
                                    scene_text = re.sub(r'\b(none|no text|no)\b', '', scene_text, flags=re.IGNORECASE).strip()
                                
                                # Add text to image prompt if we have valid text (not empty, not "none")
                                if scene_text and scene_text.lower().strip() not in ["none", "no text", "no", "n/a", "na", ""]:
                                    # Add text overlay instruction to prompt
                                    final_image_prompt += f" | Text overlay on image: '{scene_text}' - The text should be prominently displayed, styled, and clearly readable as part of the image composition."
                                    logger.info(f"📝 [Scene {scene_num}] Added text to image prompt (original had text, no VO): '{scene_text[:50]}...'")
                                elif scene_text:
                                    logger.warning(f"⚠️ [Scene {scene_num}] Skipping invalid text (contains 'none' or empty): '{scene_text}'")
                        
                        # Only include product reference if product is visible in this scene
                        if product_visible_in_scene and gemini_analysis.get("product", {}).get("detected"):
                            ref_url = product_reference_url
                            
                            # Build comprehensive product description for better accuracy
                            product_info_gemini = gemini_analysis.get("product", {})
                            visual_desc = product_info_gemini.get("visual_description", "")
                            application_rules = product_info_gemini.get("application_rules", "")
                            usage_method = product_info_gemini.get("usage_method", "")
                            product_image_details = product_info_gemini.get("product_image_details", "")
                            
                            # Combine all product details for comprehensive reference
                            # Include EXTREMELY DETAILED description for pixel-perfect accuracy
                            ref_desc_parts = []
                            if visual_desc:
                                ref_desc_parts.append(f"VISUAL DESCRIPTION (CRITICAL - MATCH EXACTLY): {visual_desc}")
                            if product_image_details:
                                ref_desc_parts.append(f"PRODUCT IMAGE DETAILS (FROM REFERENCE FRAME): {product_image_details}")
                            if application_rules:
                                ref_desc_parts.append(f"APPLICATION RULES: {application_rules}")
                            if usage_method:
                                ref_desc_parts.append(f"USAGE METHOD: {usage_method}")
                            
                            ref_desc = "\n\n".join(ref_desc_parts) if ref_desc_parts else visual_desc
                            
                            # Add emphasis on accuracy
                            if ref_desc:
                                ref_desc = f"""PRODUCT REFERENCE - EXTREMELY DETAILED DESCRIPTION FOR PIXEL-PERFECT ACCURACY:

{ref_desc}

CRITICAL INSTRUCTIONS:
- The reference image shows the EXACT product appearance
- Match the product's EXACT shape, colors (with specific hex codes), size, materials, textures, and text/logos from the reference image
- Use the reference image to ensure pixel-perfect product accuracy
- The product in the generated image MUST match the reference image exactly in appearance
- Pay special attention to: exact colors (use hex codes if specified), exact shape and dimensions, exact materials and textures, exact text/logos if visible, exact lighting and shadows
- The product placement and usage should be logical based on the reference image and the scene context"""
                            
                            logger.info(f"🎯 [Scene {scene_num}] Using Gemini-generated prompts (product VISIBLE in this scene)")
                            logger.info(f"   📸 Product reference: {ref_url[:60] if ref_url else 'None'}...")
                            logger.info(f"   📝 Product description: {ref_desc[:100] if ref_desc else 'None'}...")
                        else:
                            ref_url = None
                            ref_desc = None
                            logger.info(f"🎯 [Scene {scene_num}] Using Gemini-generated prompts (product NOT visible in this scene)")
                        
                        logger.info(f"   Image prompt: {final_image_prompt[:80]}...")
                        if final_motion_prompt:
                            logger.info(f"   Motion prompt: {final_motion_prompt[:80]}...")
                    else:
                        # FALLBACK: Use existing GPT enhancement logic
                        final_image_prompt = image_prompt
                        ref_url = None
                        ref_desc = None
                        
                        # Check if Manual Instructions say to remove text
                        manual_instructions_lower = manual_instructions.lower() if manual_instructions else ""
                        should_remove_text = any(phrase in manual_instructions_lower for phrase in [
                            "remove text", "remove any text", "no text", "without text", 
                            "remove all text", "delete text", "הסר טקסט", "ללא טקסט"
                        ])
                        
                        # Check if original video has NO VO - if so, add text to image prompt
                        if gemini_analysis and not should_remove_text:
                            # Default to False - if Gemini didn't detect VO, assume there's no VO
                            original_has_vo = gemini_analysis.get("audio", {}).get("original_has_vo", False)
                            
                            # Get scene info for branding check
                            gemini_scene_info = None
                            for gs in gemini_analysis.get("scenes", []):
                                if gs.get("scene_number") == scene_num:
                                    gemini_scene_info = gs.get("understanding", {})
                                    break
                            
                            # Check if original had text overlays
                            original_has_text = gemini_scene_info.get("has_branding_overlay", False) if gemini_scene_info else False
                            
                            # Only add text if: original had text AND original has no VO
                            if original_has_text and not original_has_vo:
                                scene_text = gemini_scene_info.get("text_on_screen", "") if gemini_scene_info else ""
                                
                                # Clean scene_text - remove "none", "None", "NONE", "no text", etc.
                                if scene_text:
                                    scene_text_lower = scene_text.lower().strip()
                                    # Remove common negative text indicators
                                    if scene_text_lower in ["none", "no text", "no", "n/a", "na", ""]:
                                        scene_text = ""
                                    # Remove "none" if it appears as a word
                                    scene_text = re.sub(r'\b(none|no text|no)\b', '', scene_text, flags=re.IGNORECASE).strip()
                                
                                # Add text to image prompt if we have valid text (not empty, not "none")
                                if scene_text and scene_text.lower().strip() not in ["none", "no text", "no", "n/a", "na", ""]:
                                    final_image_prompt += f" | Text overlay on image: '{scene_text}' - The text should be prominently displayed, styled, and clearly readable as part of the image composition."
                                    logger.info(f"📝 [Scene {scene_num}] Added text to image prompt (original had text, no VO, fallback): '{scene_text[:50]}...'")
                                elif scene_text:
                                    logger.warning(f"⚠️ [Scene {scene_num}] Skipping invalid text (contains 'none' or empty): '{scene_text}'")
                        
                        if product_info.get("has_product"):
                            # Determine the usage context for this scene from multiple sources:
                            # 1. Video structure analysis (scene_plan)
                            # 2. Product detection frame analysis
                            # 3. Default from usage_contexts
                            scene_context = None
                            scene_plan_entry = None
                            narrative_role = None
                            article_content_for_scene = None
                            
                            # First, check video structure scene plan
                            scene_plan = video_structure.get("scene_plan", [])
                            for sp in scene_plan:
                                if sp.get("scene_number") == scene_num:
                                    scene_plan_entry = sp
                                    scene_context = sp.get("product_appearance")
                                    narrative_role = sp.get("narrative_role")
                                    article_content_for_scene = sp.get("article_content_to_use")
                                    if scene_context:
                                        logger.info(f"   [Scene {scene_num}] Context from structure analysis: {scene_context} (role: {narrative_role})")
                                    break
                            
                            # If no context from structure, try frame analysis
                            if not scene_context:
                                frame_analysis = product_info.get("frame_analysis", [])
                                usage_contexts = product_info.get("usage_contexts", [])
                                
                                # Try to find context from frame analysis based on scene timing
                                if frame_analysis:
                                    for frame_info in frame_analysis:
                                        frame_idx = frame_info.get("frame_index", 0)
                                        estimated_time = frame_idx / 5.0
                                        if scene_start_time <= estimated_time < scene_start_time + duration:
                                            scene_context = frame_info.get("usage_context")
                                            if scene_context:
                                                logger.info(f"   [Scene {scene_num}] Context from frame analysis: {scene_context}")
                                                break
                                
                                # If no context found from frames, use the first usage context
                                if not scene_context and usage_contexts:
                                    scene_context = usage_contexts[0].get("context_type", "static_display")
                            
                            # Log scene planning info
                            if scene_plan_entry:
                                logger.info(f"   [Scene {scene_num}] Narrative role: {narrative_role}")
                                if article_content_for_scene:
                                    logger.info(f"   [Scene {scene_num}] Content to use: {article_content_for_scene[:50]}...")
                            
                            logger.info(f"🎨 [Scene {scene_num}] Enhancing prompt with product details (context: {scene_context})...")
                            try:
                                # Build enhanced article context if scene plan has specific content
                                enhanced_article_text = article_text
                                if article_content_for_scene:
                                    enhanced_article_text = f"{article_content_for_scene}\n\n{article_text}"
                                
                                # Add scene plan info to product_info for enhancement
                                enhanced_product_info = product_info.copy() if product_info else {}
                                if scene_plan_entry:
                                    enhanced_product_info["scene_plan"] = {
                                        "narrative_role": narrative_role,
                                        "key_message": scene_plan_entry.get("key_message", ""),
                                        "visual_suggestion": scene_plan_entry.get("visual_suggestion", "")
                                    }
                                
                                # Add video story context from Gemini analysis
                                if gemini_analysis:
                                    video_story = gemini_analysis.get("video_story", {})
                                    scene_breakdown = gemini_analysis.get("scene_breakdown", [])
                                    
                                    # Find this specific scene's info from Gemini
                                    scene_gemini_info = None
                                    for s in scene_breakdown:
                                        if s.get("scene_number") == scene_num:
                                            scene_gemini_info = s
                                            break
                                    
                                    # Get subject appearance for this scene from recreation blueprint
                                    blueprint = video_story.get("recreation_blueprint", {})
                                    subject_appearances = blueprint.get("subject_appearance_per_scene", {})
                                    scene_subject_appearance = subject_appearances.get(str(scene_num), "")
                                    
                                    # Get story arc info
                                    story_arc = video_story.get("story_arc", {})
                                    subject_journey = video_story.get("subject_journey", {})
                                    
                                    # Determine if subject changes and how
                                    enhanced_product_info["story_context"] = {
                                        "story_type": video_story.get("story_type", ""),
                                        "story_summary": video_story.get("one_sentence_summary", ""),
                                        "scene_subject_appearance": scene_subject_appearance,
                                        "has_visible_change": subject_journey.get("has_visible_change", False),
                                        "change_type": subject_journey.get("change_type", ""),
                                        "start_state": subject_journey.get("start_state", ""),
                                        "end_state": subject_journey.get("end_state", ""),
                                        "essential_story_beats": blueprint.get("essential_story_beats", []),
                                        "must_preserve": blueprint.get("must_preserve", [])
                                    }
                                    
                                    # Add scene-specific visual info
                                    if scene_gemini_info:
                                        visual_content = scene_gemini_info.get("visual_content", {})
                                        subjects = visual_content.get("subjects", {})
                                        enhanced_product_info["story_context"]["scene_details"] = {
                                            "physical_state": subjects.get("physical_state", ""),
                                            "action": subjects.get("action", ""),
                                            "what_changed": scene_gemini_info.get("scene_changes", {}).get("what_changed_from_previous", ""),
                                            "purpose": scene_gemini_info.get("story_element", {}).get("purpose", ""),
                                            "emotional_beat": scene_gemini_info.get("story_element", {}).get("emotional_beat", "")
                                        }
                                        
                                        if scene_subject_appearance:
                                            logger.info(f"   [Scene {scene_num}] Subject appearance: {scene_subject_appearance[:60]}...")
                                        if subjects.get("physical_state"):
                                            logger.info(f"   [Scene {scene_num}] Physical state: {subjects.get('physical_state', '')[:60]}...")
                                
                                call_fn_enhance = lambda msgs, **kw: processor._call_llm("enhance_prompt_product", msgs, **kw)
                                final_image_prompt = enhance_prompt_with_product(
                                    call_fn_enhance,
                                    original_prompt=image_prompt,
                                    product_description=product_info.get("product_description", ""),
                                    article_text=enhanced_article_text,
                                    product_info=enhanced_product_info,
                                    scene_context=scene_context,
                                    video_style=video_style
                                )
                                ref_url = product_reference_url
                                ref_desc = product_info.get("product_description")
                                logger.info(f"✅ [Scene {scene_num}] Prompt enhanced with product + style matching")
                            except Exception as e:
                                logger.warning(f"⚠️ [Scene {scene_num}] Failed to enhance prompt: {e}")
                                # Continue with original prompt
                    
                    logger.info(f"🎨 [Scene {scene_num}] Generating image...")
                    image_url = processor.kie_service.generate_image_nano_banana(
                        prompt=final_image_prompt,
                        reference_image_url=ref_url,
                        reference_description=ref_desc,
                        target_language=subtitle_language or "en",
                        article_text=article_text
                    )
                    if image_url:
                        result_data["image_url"] = image_url
                        
                        # Write to sheet
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.SCENE_NEW_IMAGE_PREFIX.replace("{n}", str(scene_num)),
                            value=image_url,
                            headers=headers
                        )
                        
                        # Step 2: Generate video with animation model (Runway or Kling)
                        if motion_prompt:
                            # Enhance motion prompt with product usage details AND video style
                            final_motion_prompt = motion_prompt
                            if product_info.get("has_product"):
                                try:
                                    final_motion_prompt = enhance_motion_prompt_with_product(
                                        product_info=product_info,
                                        original_motion_prompt=motion_prompt,
                                        scene_context=scene_context,
                                        video_style=video_style
                                    )
                                    logger.info(f"✅ [Scene {scene_num}] Motion prompt enhanced for product usage")
                                except Exception as me:
                                    logger.warning(f"⚠️ [Scene {scene_num}] Motion enhancement failed: {me}")
                            
                            logger.info(f"🎬 [Scene {scene_num}] Generating video with {animation_model.upper()}...")
                            if animation_model == "kling":
                                video_url = processor.kie_service.generate_video_kling(
                                    prompt=final_motion_prompt,
                                    image_url=image_url,
                                    duration=duration
                                )
                            else:
                                video_url = processor.kie_service.generate_video_runway(
                                    prompt=final_motion_prompt,
                                    image_url=image_url,
                                    duration=duration
                                )
                            if video_url:
                                # Upload to GCS immediately to avoid temp URL expiration
                                try:
                                    gcs_video_url = processor._upload_video_to_gcs_from_url(
                                        video_url=video_url,
                                        row_num=row_num,
                                        scene_num=scene_num
                                    )
                                    if gcs_video_url:
                                        video_url = gcs_video_url
                                        logger.info(f"✅ [Scene {scene_num}] Video uploaded to GCS (permanent URL)")
                                except Exception as upload_err:
                                    logger.warning(f"⚠️ [Scene {scene_num}] GCS upload failed, using temp URL: {upload_err}")
                                
                                result_data["video_url"] = video_url
                                
                                processor._update_sheet_cell(
                                    row_num=row_num,
                                    column=config.SCENE_NEW_VIDEO_PREFIX.replace("{n}", str(scene_num)),
                                    value=video_url,
                                    headers=headers
                                )
                
                return result_data
                
            except Exception as e:
                logger.error(f"❌ [Scene {scene_num}] Error: {e}")
                return result_data
        
        def process_audio_pipeline():
            """Process audio: Extract → Detect speech → ElevenLabs + Suno.
            
            If article_text is provided, generates NEW VO from article using TTS.
            """
            nonlocal audio_result, voice_id
            
            # Voice selection: Voice id column value (already read) → random → default
            if not voice_id or not voice_id.strip():
                # Try random voice from catalog
                random_voice = processor.elevenlabs_service.pick_random_voice(gender="male", language=subtitle_language)
                if random_voice:
                    voice_id = random_voice
                else:
                    voice_id = config.DEFAULT_VOICE_ID
                    logger.info(f"🎤 [AUDIO] Fallback to default voice ID: {voice_id}")
            
            logger.info("🎤 [AUDIO] Starting audio pipeline in parallel...")
            audio_path = os.path.join(temp_dir, "original_audio.mp3")
            
            # Try local extraction first, then cloud
            audio_extracted = False
            if ffmpeg_available and video_path:
                audio_extracted = FFmpegProcessor.extract_audio(video_path, audio_path)
            
            if not audio_extracted:
                logger.info("🌐 [AUDIO] Extracting audio via Rendi.dev cloud...")
                original_audio_url = FFmpegProcessor.extract_audio_from_url(
                    video_url=video_url,
                    output_path=audio_path,
                    rendi_api_key=config.RENDI_API_KEY
                )
                
                if original_audio_url:
                    try:
                        response = requests.get(original_audio_url, timeout=60)
                        response.raise_for_status()
                        with open(audio_path, 'wb') as f:
                            f.write(response.content)
                        audio_extracted = True
                        logger.info("✅ [AUDIO] Audio downloaded from cloud extraction")
                    except Exception as e:
                        logger.error(f"❌ [AUDIO] Failed to download audio: {e}")
            
            if not audio_extracted or not os.path.exists(audio_path):
                logger.warning("⚠️ [AUDIO] Could not extract audio")
                return
            
            # Upload original audio to GCS for Suno (needed in all paths)
            timestamp = int(time.time())
            temp_audio_key = f"temp_audio_for_suno_row_{row_num}_{timestamp}.mp3"
            with open(audio_path, 'rb') as f:
                audio_data = f.read()
            audio_url_for_suno = processor.gcs_storage_service.upload_audio_bytes(
                audio_data=audio_data,
                key_name=temp_audio_key
            )
            
            # =================================================================
            # DETECT VO PRESENCE AND GENDER FROM ORIGINAL VIDEO
            # =================================================================
            # Check if original video has voice-over narration
            detected_gender, original_transcript = processor.elevenlabs_service.detect_vo_gender(audio_path)
            original_has_vo = detected_gender is not None
            
            if original_has_vo:
                logger.info(f"🎤 [AUDIO] Original video HAS VO (gender: {detected_gender})")
                # Write detected gender (m/f) to Gender column
                logger.info(f"📝 [Row {row_num}] Writing gender '{detected_gender}' to column '{config.GENDER_COLUMN}'")
                update_success = processor._update_sheet_cell(
                    row_num=row_num,
                    column=config.GENDER_COLUMN,
                    value=detected_gender,
                    headers=headers
                )
                if update_success:
                    logger.info(f"✅ [Row {row_num}] Gender '{detected_gender}' written successfully")
                    
                    # ================================================================
                    # RE-READ VOICE ID FROM SHEET (formula depends on Gender)
                    # ================================================================
                    time.sleep(1.5)  # Wait for sheet formula to recalculate
                    try:
                        updated_row_data = processor.sheets_service.get_row(
                            sheet_id=config.GOOGLE_SHEET_ID,
                            worksheet_name=config.GOOGLE_SHEET_TAB,
                            row_num=row_num
                        )
                        if updated_row_data:
                            voice_id_col = processor.sheets_service.get_column_index(headers, config.VOICE_ID_COLUMN)
                            if voice_id_col is not None and voice_id_col < len(updated_row_data):
                                new_voice_id = updated_row_data[voice_id_col].strip()
                                if new_voice_id:  # Use directly without validation
                                    voice_id = new_voice_id
                                    logger.info(f"🎤 [Row {row_num}] Updated Voice ID from sheet: {voice_id}")
                                else:
                                    # Voice id column empty → use random voice by detected gender
                                    random_voice = processor.elevenlabs_service.pick_random_voice(
                                        gender="female" if detected_gender == "f" else "male",
                                        language=subtitle_language
                                    )
                                    if random_voice:
                                        voice_id = random_voice
                                    elif not voice_id or not voice_id.strip():
                                        voice_id = config.DEFAULT_VOICE_ID
                                        logger.info(f"🎤 [Row {row_num}] Voice ID empty, fallback to default: {voice_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ [Row {row_num}] Failed to re-read Voice ID: {e}")
                else:
                    logger.warning(f"⚠️ [Row {row_num}] Failed to write gender to sheet")
            else:
                logger.info("🔇 [AUDIO] Original video has NO VO - will generate music only")
            
            # =================================================================
            # MANUAL VO TEXT PATH - Use provided text instead of generating
            # =================================================================
            if manual_vo_text:
                logger.info(f"🎤 [AUDIO] Using MANUAL VO text ({len(manual_vo_text)} chars)...")
                
                # Use manual text directly for TTS WITH TIMESTAMPS
                vo_language = subtitle_language or article_language or "en"
                
                tts_result = processor.elevenlabs_service.text_to_speech_with_timestamps(
                    text=manual_vo_text,
                    voice_id=voice_id,  # Validated inside function
                    language=vo_language
                )
                
                if tts_result:
                    tts_audio_data, word_segments = tts_result
                    logger.info(f"📝 [AUDIO] Got {len(word_segments)} word segments from Manual TTS")
                    
                    # Store word segments for ZapCap
                    audio_result["tts_word_segments"] = word_segments
                    audio_result["tts_generated"] = True
                    
                    ts = int(time.time())
                    voice_key = f"manual_vo_row_{row_num}_{ts}.mp3"
                    voice_url = processor.gcs_storage_service.upload_audio_bytes(
                        audio_data=tts_audio_data,
                        key_name=voice_key
                    )
                    
                    if voice_url:
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_VOICE_COLUMN,
                            value=voice_url,
                            headers=headers
                        )
                        audio_result["new_voice_url"] = voice_url
                        audio_result["has_speech"] = True
                        logger.info(f"✅ [AUDIO] Manual VO TTS generated: {voice_url}")
                else:
                    logger.error("❌ [AUDIO] Failed to generate TTS from manual VO text")
                
                # Handle music: use manual link or generate
                if manual_music_link:
                    logger.info(f"🎵 [AUDIO] Using MANUAL music link: {manual_music_link[:50]}...")
                    audio_result["new_music_url"] = manual_music_link
                    processor._update_sheet_cell(
                        row_num=row_num,
                        column=config.NEW_MUSIC_COLUMN,
                        value=manual_music_link,
                        headers=headers
                    )
                elif audio_url_for_suno:
                    # Generate music with Suno
                    call_fn_music = lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw)
                    music_description = generate_music_description(
                        call_fn_music,
                        scene_prompts=scene_prompts
                    )
                    logger.info(f"🎵 [AUDIO] Generating music: {music_description[:80]}...")
                    
                    music_url = processor.suno_service.generate_instrumental_background(
                        audio_url=audio_url_for_suno,
                        style=music_description,
                        fallback_style=music_description
                    )
                    
                    if music_url:
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_MUSIC_COLUMN,
                            value=music_url,
                            headers=headers
                        )
                        audio_result["new_music_url"] = music_url
                
                # Set final audio
                if audio_result.get("new_voice_url"):
                    audio_result["final_audio_url"] = audio_result["new_voice_url"]
                    logger.info("✅ [AUDIO] Manual VO processing complete")
                
                return  # Exit - manual VO complete
            
            # =================================================================
            # ARTICLE ADAPTATION PATH - Generate NEW VO from article via TTS
            # (Only if original video has VO)
            # =================================================================
            if has_article_adaptation:
                logger.info("📰 [AUDIO] Article adaptation mode...")
                
                # Check if original video has VO - if not, skip VO generation
                if not original_has_vo:
                    logger.info("🔇 [AUDIO] Original video has NO VO - skipping VO generation, creating music only")
                    
                    # Generate music only (no VO)
                    if manual_music_link:
                        logger.info(f"🎵 [AUDIO] Using MANUAL music link: {manual_music_link[:50]}...")
                        audio_result["new_music_url"] = manual_music_link
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_MUSIC_COLUMN,
                            value=manual_music_link,
                            headers=headers
                        )
                    elif audio_url_for_suno:
                        call_fn_music = lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw)
                        music_description = generate_music_description(
                            call_fn_music,
                            scene_prompts=scene_prompts
                        )
                        logger.info(f"🎵 [AUDIO] Generating music (no VO): {music_description[:80]}...")
                        
                        music_url = processor.suno_service.generate_instrumental_background(
                            audio_url=audio_url_for_suno,
                            style=music_description,
                            fallback_style=music_description
                        )
                        
                        if music_url:
                            processor._update_sheet_cell(
                                row_num=row_num,
                                column=config.NEW_MUSIC_COLUMN,
                                value=music_url,
                                headers=headers
                            )
                            audio_result["new_music_url"] = music_url
                            audio_result["final_audio_url"] = music_url
                            logger.info(f"✅ [AUDIO] Music only (no VO) generated: {music_url}")
                    
                    logger.info("✅ [AUDIO] No-VO mode complete (music only)")
                    return  # Exit - no VO mode complete
                
                # Original has VO - proceed with VO generation
                logger.info("🎤 [AUDIO] Original video HAS VO - generating new VO from article...")
                
                # Check if Gemini provided a ready VO script
                vo_script = None
                if gemini_analysis and gemini_analysis.get("new_voiceover", {}).get("full_script"):
                    vo_script = gemini_analysis["new_voiceover"]["full_script"]
                    vo_style = gemini_analysis["new_voiceover"].get("style", "")
                    vo_word_count = gemini_analysis["new_voiceover"].get("word_count", len(vo_script.split()))
                    logger.info(f"🎯 [AUDIO] Using Gemini-generated VO script ({vo_word_count} words, style: {vo_style})")
                    logger.info(f"   Script preview: {vo_script[:100]}...")
                else:
                    # FALLBACK: Use GPT to generate VO script
                    logger.info("📝 [AUDIO] Generating VO script with GPT (Gemini didn't provide one)...")
                    call_fn_vo = lambda msgs, **kw: processor._call_llm("generate_vo_from_article", msgs, **kw)
                    vo_script = generate_vo_script_from_article(
                        call_fn_vo,
                        article_text=article_text,
                        vertical=vertical,
                        target_duration=video_duration,
                        target_language=article_language,
                        original_vo_transcript=original_transcript,
                        scene_prompts=scene_prompts,  # Pass scene prompts so VO matches visuals
                        gemini_vo_recommendations=gemini_analysis  # Pass Gemini analysis for VO style matching
                    )
                
                if vo_script:
                    logger.info(f"✅ [AUDIO] Generated VO script ({len(vo_script.split())} words)")
                    
                    # Generate TTS audio WITH TIMESTAMPS for ZapCap subtitles
                    tts_result = processor.elevenlabs_service.text_to_speech_with_timestamps(
                        text=vo_script,
                        voice_id=voice_id,  # Validated inside function
                        language=article_language
                    )
                    
                    if tts_result:
                        tts_audio_data, word_segments = tts_result
                        logger.info(f"📝 [AUDIO] Got {len(word_segments)} word segments from TTS")
                        
                        # Store word segments for ZapCap (will be used in Step 11)
                        audio_result["tts_word_segments"] = word_segments
                        audio_result["tts_generated"] = True
                        
                        ts = int(time.time())
                        voice_key = f"tts_voice_row_{row_num}_{ts}.mp3"
                        voice_url = processor.gcs_storage_service.upload_audio_bytes(
                            audio_data=tts_audio_data,
                            key_name=voice_key
                        )
                        
                        if voice_url:
                            processor._update_sheet_cell(
                                row_num=row_num,
                                column=config.NEW_VOICE_COLUMN,
                                value=voice_url,
                                headers=headers
                            )
                            audio_result["new_voice_url"] = voice_url
                            logger.info(f"✅ [AUDIO] TTS voice generated: {voice_url}")
                    else:
                        logger.error("❌ [AUDIO] Failed to generate TTS audio")
                else:
                    logger.error("❌ [AUDIO] Failed to generate VO script from article")
                
                # Handle music: use manual link or generate
                if manual_music_link:
                    logger.info(f"🎵 [AUDIO] Using MANUAL music link: {manual_music_link[:50]}...")
                    audio_result["new_music_url"] = manual_music_link
                    processor._update_sheet_cell(
                        row_num=row_num,
                        column=config.NEW_MUSIC_COLUMN,
                        value=manual_music_link,
                        headers=headers
                    )
                elif audio_url_for_suno:
                    call_fn_music = lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw)
                    music_description = generate_music_description(
                        call_fn_music,
                        scene_prompts=scene_prompts
                    )
                    logger.info(f"🎵 [AUDIO] Generating music for article: {music_description[:80]}...")
                    
                    music_url = processor.suno_service.generate_instrumental_background(
                        audio_url=audio_url_for_suno,
                        style=music_description,
                        fallback_style=music_description
                    )
                    
                    if music_url:
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_MUSIC_COLUMN,
                            value=music_url,
                            headers=headers
                        )
                        audio_result["new_music_url"] = music_url
                        logger.info(f"✅ [AUDIO] New music generated: {music_url}")
                
                # Set final audio
                if audio_result.get("new_voice_url"):
                    audio_result["final_audio_url"] = audio_result["new_voice_url"]
                    audio_result["has_speech"] = True
                    logger.info("✅ [AUDIO] Article adaptation complete")
                
                return  # Exit - article adaptation complete
            
            # =================================================================
            # NORMAL PATH - Use existing speech detection and voice changer
            # =================================================================
            # Use the VO detection result from earlier (original_has_vo)
            audio_result["has_speech"] = original_has_vo
            
            if original_has_vo:
                # =========================================================
                # PATH A: Speech detected - Stem Separation → Voice Changer → New Music
                # =========================================================
                logger.info("🎤 [AUDIO] Speech detected - separating stems first...")
                
                # Step 1: Separate stems to get clean vocals
                clean_vocals_path = processor.elevenlabs_service.separate_stems(
                    audio_path=audio_path,
                    output_dir=temp_dir
                )
                
                # Use clean vocals if available, otherwise fallback to original audio
                voice_source_path = clean_vocals_path if clean_vocals_path else audio_path
                if clean_vocals_path:
                    logger.info("✅ [AUDIO] Using clean vocals for voice changer")
                else:
                    logger.warning("⚠️ [AUDIO] Stem separation failed, using original audio")
                
                def run_elevenlabs():
                    """Apply voice changer on clean vocals."""
                    new_voice_data = processor.elevenlabs_service.voice_changer(voice_source_path)
                    if new_voice_data:
                        ts = int(time.time())
                        voice_key = f"voice_row_{row_num}_{ts}.mp3"
                        voice_url = processor.gcs_storage_service.upload_audio_bytes(
                            audio_data=new_voice_data,
                            key_name=voice_key
                        )
                        if voice_url:
                            processor._update_sheet_cell(
                                row_num=row_num,
                                column=config.NEW_VOICE_COLUMN,
                                value=voice_url,
                                headers=headers
                            )
                            logger.info(f"✅ [AUDIO] Voice changed: {voice_url}")
                            return voice_url
                    return None
                
                def run_suno_new_music():
                    """Generate NEW background music with Suno (or use manual link)."""
                    # Check for manual music link first
                    if manual_music_link:
                        logger.info(f"🎵 [AUDIO] Using MANUAL music link: {manual_music_link[:50]}...")
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_MUSIC_COLUMN,
                            value=manual_music_link,
                            headers=headers
                        )
                        return manual_music_link
                    
                    if not audio_url_for_suno:
                        return None
                    
                    # Generate dynamic music description based on video content
                    call_fn_music = lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw)
                    music_description = generate_music_description(
                        call_fn_music,
                        scene_prompts=scene_prompts
                    )
                    logger.info(f"🎵 [AUDIO] Using AI-generated music description: {music_description[:80]}...")
                    
                    music_url = processor.suno_service.generate_instrumental_background(
                        audio_url=audio_url_for_suno,
                        style=music_description,
                        fallback_style=music_description  # Use same for pure generation fallback
                    )
                    if music_url:
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_MUSIC_COLUMN,
                            value=music_url,
                            headers=headers
                        )
                        logger.info(f"✅ [AUDIO] New music generated: {music_url}")
                        return music_url
                    return None
                
                # Run Voice Changer and Suno in parallel
                with ThreadPoolExecutor(max_workers=10) as audio_executor:
                    voice_future = audio_executor.submit(run_elevenlabs)
                    music_future = audio_executor.submit(run_suno_new_music)
                    
                    audio_result["new_voice_url"] = voice_future.result()
                    audio_result["new_music_url"] = music_future.result()
                
                # Final audio will combine: New Voice + New Suno Music
                # (original audio is discarded)
                if audio_result["new_voice_url"]:
                    audio_result["final_audio_url"] = audio_result["new_voice_url"]
                    logger.info("✅ [AUDIO] Voice ready, new Suno music will be added after video combination")
                
            else:
                # =========================================================
                # PATH B: No speech - use manual music or generate cover music
                # =========================================================
                logger.info("🎵 [AUDIO] No speech detected - setting up music...")
                
                # Check for manual music link first
                if manual_music_link:
                    logger.info(f"🎵 [AUDIO] Using MANUAL music link: {manual_music_link[:50]}...")
                    processor._update_sheet_cell(
                        row_num=row_num,
                        column=config.NEW_MUSIC_COLUMN,
                        value=manual_music_link,
                        headers=headers
                    )
                    processor._update_sheet_cell(
                        row_num=row_num,
                        column=config.NEW_VOICE_COLUMN,
                        value=f"[NO VOICE - Manual Music] {manual_music_link}",
                        headers=headers
                    )
                    audio_result["new_music_url"] = manual_music_link
                    audio_result["final_audio_url"] = manual_music_link
                elif audio_url_for_suno:
                    music_url = processor.suno_service.generate_cover_music(
                        audio_url=audio_url_for_suno,
                        audio_path=audio_path
                    )
                    
                    if music_url:
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_MUSIC_COLUMN,
                            value=music_url,
                            headers=headers
                        )
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.NEW_VOICE_COLUMN,
                            value=f"[NO VOICE - Music Only] {music_url}",
                            headers=headers
                        )
                        audio_result["new_music_url"] = music_url
                        audio_result["final_audio_url"] = music_url
                        logger.info(f"✅ [AUDIO] Cover music generated: {music_url}")
                    else:
                        # Fallback: use original audio
                        logger.warning("⚠️ [AUDIO] Could not generate cover music, using original")
                        original_audio_key = f"original_audio_row_{row_num}_{timestamp}.mp3"
                        original_url = processor.gcs_storage_service.upload_audio_bytes(
                            audio_data=audio_data,
                            key_name=original_audio_key
                        )
                        audio_result["final_audio_url"] = original_url
        
        # =================================================================
        # Run SCENE PROCESSING + AUDIO in PARALLEL (independent; rows stay sequential)
        # =================================================================
        with ThreadPoolExecutor(max_workers=12) as executor:
            audio_future = executor.submit(process_audio_pipeline)
            scene_futures = {
                executor.submit(process_scene_with_prompts, scene): scene["scene_num"]
                for scene in scene_data
            }
            
            for future in as_completed(scene_futures):
                scene_num = scene_futures[future]
                try:
                    scene_result = future.result()
                    scene_results[scene_num] = scene_result
                    if scene_result.get("video_url"):
                        result["scenes_processed"] += 1
                        logger.info(f"✅ Scene {scene_num} completed")
                except Exception as e:
                    logger.error(f"❌ Scene {scene_num} failed: {e}")
            
            try:
                audio_future.result()
                logger.info("✅ Audio pipeline completed")
            except Exception as e:
                logger.error(f"❌ Audio pipeline failed: {e}")
        
        # Collect videos for concatenation - ONLY videos, NOT images
        # Use a dict to prevent duplicates by scene_num
        scene_videos_dict = {}
        missing_videos = []
        
        for scene in scene_data:
            scene_num = scene["scene_num"]
            # Skip if we already have this scene (prevent duplicates)
            if scene_num in scene_videos_dict:
                logger.warning(f"   ⚠️ Scene {scene_num}: Already in list, skipping duplicate")
                continue
                
            if scene_num in scene_results:
                scene_result = scene_results[scene_num]
                # Only include if it has video_url (not just image_url)
                if scene_result.get("video_url"):
                    scene_videos_dict[scene_num] = {
                        "video_url": scene_result["video_url"],
                        "duration": scene["duration"]
                    }
                    logger.info(f"   ✅ Scene {scene_num}: video ready (duration: {scene['duration']:.2f}s)")
                elif scene_result.get("image_url"):
                    # Skip scenes that only have images (no video/animation)
                    logger.warning(f"   ⚠️ Scene {scene_num}: Only image available, no video/animation - skipping from concatenation")
                    missing_videos.append(scene_num)
                else:
                    missing_videos.append(scene_num)
                    logger.warning(f"   ⚠️ Scene {scene_num}: video missing or failed")
            else:
                missing_videos.append(scene_num)
                logger.warning(f"   ⚠️ Scene {scene_num}: video missing or failed")
        
        # Convert dict to list, sorted by scene_num to maintain order
        scene_videos_with_durations = [scene_videos_dict[num] for num in sorted(scene_videos_dict.keys())]
        
        if missing_videos:
            logger.warning(f"⚠️ {len(missing_videos)} scenes missing videos: {missing_videos}")
        
        logger.info(f"✅ Parallel processing complete: {len(scene_videos_with_durations)}/{len(scene_data)} videos generated")
        
        # Extract audio results
        new_voice_url = audio_result.get("new_voice_url")
        new_music_url = audio_result.get("new_music_url")
        final_audio_url = audio_result.get("final_audio_url")
        result["new_music_url"] = new_music_url
        
        # Step 6: Trim and concatenate all scene videos with Rendi
        logger.info(f"🎬 [Row {row_num}] Step 6: Trimming videos to original scene durations and concatenating...")
        combined_video_url = None
        if scene_videos_with_durations:
            # First, trim each Runway video to match original scene duration
            logger.info(f"✂️ Trimming {len(scene_videos_with_durations)} videos to their original scene durations...")
            for i, item in enumerate(scene_videos_with_durations):
                logger.info(f"   Scene {i+1}: target duration = {item['duration']:.2f}s")
            
            trimmed_videos = processor.rendi_service.trim_videos_batch(scene_videos_with_durations)
            
            logger.info(f"✅ Trimmed {len(trimmed_videos)} videos, now uploading to GCS if needed...")
            
            # Upload any Rendi storage URLs to GCS to prevent download failures
            for i, video_item in enumerate(trimmed_videos):
                video_url = video_item.get("video_url") if isinstance(video_item, dict) else video_item
                if video_url and "storage.rendi.dev" in video_url:
                    # This is a Rendi storage URL - upload to GCS to prevent download failures
                    logger.info(f"   📤 Uploading Rendi storage video {i+1} to GCS...")
                    gcs_key = f"rendi_videos/row_{row_num}_scene_{i+1}_{int(time.time())}.mp4"
                    uploaded_url = processor.gcs_storage_service.upload_video_from_url(video_url, gcs_key)
                    if uploaded_url:
                        logger.info(f"   ✅ Uploaded to GCS: {uploaded_url[:60]}...")
                        if isinstance(video_item, dict):
                            video_item["video_url"] = uploaded_url
                        else:
                            trimmed_videos[i] = uploaded_url
                    else:
                        logger.warning(f"   ⚠️ Failed to upload Rendi video to GCS, using original URL")
            
            logger.info(f"✅ Videos ready for concatenation...")
            
            # =============================================================
            # OPENING TEXT OVERLAY (on first scene only)
            # =============================================================
            if add_opening_text and trimmed_videos:
                logger.info(f"🎬 [Row {row_num}] Adding opening text to first scene...")
                try:
                    # If no opening text provided, generate one based on the article/content
                    actual_opening_text = opening_text
                    if not actual_opening_text:
                        # Generate opening text based on VIDEO content (first scene description)
                        # Get video description from scene_data
                        video_description = None
                        if scene_data:
                            # Combine first few scene prompts to understand video content
                            scene_descriptions = []
                            for scene in scene_data[:3]:  # Use first 3 scenes for context
                                if scene.get("image_prompt"):
                                    scene_descriptions.append(scene["image_prompt"])
                            if scene_descriptions:
                                video_description = " | ".join(scene_descriptions)
                                logger.info(f"🎬 Video description for opening text: {video_description[:100]}...")
                        
                        # Use OpenAI to generate short, compelling opening text
                        opening_lang = subtitle_language or article_language or "en"
                        call_fn_text = lambda msgs, **kw: processor._call_llm("generate_opening_text", msgs, **kw)
                        generated_text = generate_opening_text(
                            call_fn_text,
                            article_text=article_text[:1000] if article_text else "",
                            language=opening_lang,
                            video_description=video_description
                        )
                        if generated_text:
                            actual_opening_text = generated_text
                            logger.info(f"✅ Generated opening text: '{actual_opening_text}'")
                    
                    if actual_opening_text:
                        # Step 1: Generate opening text image with Nano Banana
                        opening_image_url = processor.kie_service.generate_opening_text(actual_opening_text)
                        
                        if opening_image_url:
                            # Step 2: Download, process (remove bg), and re-upload
                            opening_processed_url = _process_cta_button(
                                processor,
                                cta_image_url=opening_image_url,
                                temp_dir=temp_dir,
                                row_num=row_num
                            )
                            
                            if opening_processed_url:
                                # Step 3: Overlay on the first scene video
                                first_scene_video = trimmed_videos[0]
                                first_video_url = first_scene_video.get("video_url") if isinstance(first_scene_video, dict) else first_scene_video
                                
                                video_with_opening = processor.rendi_service.overlay_cta_on_video(
                                    video_url=first_video_url,
                                    cta_image_url=opening_processed_url,
                                    position="center"
                                )
                                
                                if video_with_opening:
                                    # Update the first scene with the opening text version
                                    if isinstance(trimmed_videos[0], dict):
                                        trimmed_videos[0]["video_url"] = video_with_opening
                                    else:
                                        trimmed_videos[0] = video_with_opening
                                    logger.info(f"✅ Opening text added to first scene: '{actual_opening_text}'")
                                else:
                                    logger.warning("⚠️ Failed to overlay opening text on video")
                            else:
                                logger.warning("⚠️ Failed to process opening text image")
                        else:
                            logger.warning("⚠️ Failed to generate opening text image")
                    else:
                        logger.warning("⚠️ No opening text available (empty and couldn't generate)")
                except Exception as e:
                    logger.error(f"❌ Opening text overlay error: {e}, continuing without opening text")
            
            # =============================================================
            # CTA BUTTON OVERLAY (on last scene only - for "at_the_end" mode)
            # For "whole_video" mode, CTA is applied after concatenation
            # =============================================================
            if cta_button and cta_text and trimmed_videos and cta_duration == "at_the_end":
                logger.info(f"🔘 [Row {row_num}] Adding CTA button to last scene: '{cta_text}'")
                try:
                    # Step 1: Generate CTA button image with Nano Banana
                    cta_image_url = processor.kie_service.generate_cta_button(cta_text)
                    
                    if cta_image_url:
                        # Step 2: Download, process (remove bg + add glow), and re-upload
                        cta_processed_url = _process_cta_button(
                            processor,
                            cta_image_url=cta_image_url,
                            temp_dir=temp_dir,
                            row_num=row_num
                        )

                        if cta_processed_url:
                            # Step 3: Overlay on the last scene video
                            last_scene_idx = len(trimmed_videos) - 1
                            last_scene_video = trimmed_videos[last_scene_idx]
                            last_video_url = last_scene_video.get("video_url") if isinstance(last_scene_video, dict) else last_scene_video
                            
                            video_with_cta = processor.rendi_service.overlay_cta_on_video(
                                video_url=last_video_url,
                                cta_image_url=cta_processed_url,
                                position="center"
                            )
                            
                            if video_with_cta:
                                # Update the last scene with the CTA version
                                if isinstance(trimmed_videos[last_scene_idx], dict):
                                    trimmed_videos[last_scene_idx]["video_url"] = video_with_cta
                                else:
                                    trimmed_videos[last_scene_idx] = video_with_cta
                                logger.info(f"✅ CTA button added to last scene")
                            else:
                                logger.warning("⚠️ Failed to overlay CTA on video, continuing without CTA")
                        else:
                            logger.warning("⚠️ Failed to process CTA button image, continuing without CTA")
                    else:
                        logger.warning("⚠️ Failed to generate CTA button, continuing without CTA")
                except Exception as e:
                    logger.error(f"❌ CTA overlay error: {e}, continuing without CTA")
            
            # =================================================================
            # STEP 6b: Adjust video duration to match VO or original video length
            # =================================================================
            # If we have VO: ensure video ends when VO ends
            # If no VO: ensure video is approximately original video length
            if trimmed_videos:
                try:
                    # Calculate current total video duration
                    total_video_duration = sum(
                        v.get("duration", 0) if isinstance(v, dict) else 0 
                        for v in trimmed_videos
                    )
                    
                    target_duration = None
                    
                    if new_voice_url:
                        # CASE 1: We have VO - video should end when VO ends
                        logger.info("🔄 Adjusting video duration to match VO...")
                        vo_duration = processor.rendi_service.get_audio_duration_cloud(new_voice_url)
                        
                        if vo_duration > 0:
                            logger.info(f"   VO duration: {vo_duration:.2f}s")
                            logger.info(f"   Current video duration: {total_video_duration:.2f}s")
                            
                            if abs(vo_duration - total_video_duration) > 0.5:  # More than 0.5s difference
                                target_duration = vo_duration
                                logger.info(f"   Target duration: {target_duration:.2f}s (matching VO)")
                    else:
                        # CASE 2: No VO - video should be approximately original video length
                        logger.info("🔄 Adjusting video duration to match original video length...")
                        logger.info(f"   Original video duration: {video_duration:.2f}s")
                        logger.info(f"   Current video duration: {total_video_duration:.2f}s")
                        
                        # Allow ±10% tolerance
                        tolerance = video_duration * 0.1
                        if abs(total_video_duration - video_duration) > tolerance:
                            target_duration = video_duration
                            logger.info(f"   Target duration: {target_duration:.2f}s (matching original)")
                    
                    # Adjust video if needed
                    if target_duration:
                        if target_duration > total_video_duration:
                            # Video is shorter than VO - use slow motion on individual scenes if within bounds
                            duration_ratio = target_duration / total_video_duration
                            max_slowdown = 2.0  # Maximum 100% slower (2x duration)
                            
                            if duration_ratio <= max_slowdown:
                                # Apply slow motion to each scene proportionally
                                speed_factor = total_video_duration / target_duration
                                logger.info(f"⏸️ Video ({total_video_duration:.2f}s) is shorter than VO ({target_duration:.2f}s)")
                                logger.info(f"   Will apply slow motion ({(1-speed_factor)*100:.0f}% slower) after concatenation...")
                                # Note: Slow motion will be applied to the combined video after concatenation
                                # This is more efficient than slowing each scene individually
                            else:
                                logger.info(f"ℹ️ Video ({total_video_duration:.2f}s) is too short for slow motion ({duration_ratio:.2f}x needed > {max_slowdown:.1f}x max)")
                                logger.info(f"   VO may extend slightly past video end.")
                        elif target_duration < total_video_duration:
                            # Only trim if video is significantly longer than VO (more than 10% longer)
                            # This ensures VO never cuts off
                            excess_ratio = (total_video_duration - target_duration) / target_duration
                            
                            if excess_ratio > 0.1:  # More than 10% longer
                                trim_amount = total_video_duration - target_duration
                                logger.info(f"✂️ Video is {excess_ratio*100:.1f}% longer than VO, trimming by {trim_amount:.2f}s...")
                                
                                last_scene_idx = len(trimmed_videos) - 1
                                last_scene = trimmed_videos[last_scene_idx]
                                last_video_url = last_scene.get("video_url") if isinstance(last_scene, dict) else last_scene
                                last_scene_duration = last_scene.get("duration", 5.0) if isinstance(last_scene, dict) else 5.0
                                
                                new_last_scene_duration = max(1.0, last_scene_duration - trim_amount)  # Min 1 second
                                
                                trimmed_last_scene = processor.rendi_service.trim_video(
                                    video_url=last_video_url,
                                    duration=new_last_scene_duration
                                )
                                
                                if trimmed_last_scene:
                                    if isinstance(trimmed_videos[last_scene_idx], dict):
                                        trimmed_videos[last_scene_idx]["video_url"] = trimmed_last_scene
                                        trimmed_videos[last_scene_idx]["duration"] = new_last_scene_duration
                                    else:
                                        trimmed_videos[last_scene_idx] = {
                                            "video_url": trimmed_last_scene,
                                            "duration": new_last_scene_duration
                                        }
                                    logger.info(f"✅ Video trimmed to {target_duration:.2f}s")
                                else:
                                    logger.warning("⚠️ Failed to trim video, continuing with current duration")
                            else:
                                # Video is only slightly longer - keep it to ensure VO doesn't cut off
                                logger.info(f"✅ Video is only {excess_ratio*100:.1f}% longer than VO, keeping extra time to ensure VO doesn't cut off")
                        else:
                            logger.info("✅ Video duration already matches target")
                    else:
                        logger.info("✅ Video duration is appropriate")
                except Exception as e:
                    logger.error(f"❌ Error adjusting video duration: {e}, continuing with current duration")
            
            # Remove any duplicates before concatenation to prevent same video appearing twice
            seen_video_urls = set()
            unique_trimmed_videos = []
            for video_item in trimmed_videos:
                video_url = video_item.get("video_url") if isinstance(video_item, dict) else video_item
                if video_url and video_url not in seen_video_urls:
                    seen_video_urls.add(video_url)
                    unique_trimmed_videos.append(video_item)
                elif video_url in seen_video_urls:
                    logger.warning(f"⚠️ Duplicate video URL detected before concatenation: {video_url[:60]}... - removing duplicate")
            
            if len(unique_trimmed_videos) < len(trimmed_videos):
                logger.warning(f"⚠️ Removed {len(trimmed_videos) - len(unique_trimmed_videos)} duplicate videos before concatenation")
                trimmed_videos = unique_trimmed_videos
            
            # Concatenate the trimmed videos with simple concat (more reliable, no repetition)
            # Using simple concat instead of transitions to avoid weird cuts and repetition
            combined_video_url = processor.rendi_service.concatenate_videos(
                trimmed_videos, 
                use_transitions=False  # Use simple concat for clean cuts without repetition
            )
            if combined_video_url:
                processor._update_sheet_cell(
                    row_num=row_num,
                    column=config.RENDI_SCENE_COLUMN,
                    value=combined_video_url,
                    headers=headers
                )
                
                # Final check: Log if video is shorter than VO (but do NOT loop to avoid jumps)
                if new_voice_url:
                    try:
                        vo_duration = processor.rendi_service.get_audio_duration_cloud(new_voice_url)
                        if vo_duration > 0:
                            combined_duration = processor.rendi_service.get_video_duration_cloud(combined_video_url)
                            if combined_duration > 0 and combined_duration < vo_duration:
                                # Use slow motion to extend video to match VO (up to 2x duration)
                                duration_ratio = vo_duration / combined_duration
                                max_slowdown = 2.0  # Maximum 100% slower (2x duration)
                                
                                if duration_ratio <= max_slowdown:
                                    speed_factor = combined_duration / vo_duration
                                    logger.info(f"⏸️ Combined video ({combined_duration:.2f}s) is shorter than VO ({vo_duration:.2f}s)")
                                    logger.info(f"   Applying slow motion ({(1-speed_factor)*100:.0f}% slower) to match VO...")
                                    
                                    slowmo_combined = processor.rendi_service.slow_motion_video(
                                        video_url=combined_video_url,
                                        speed_factor=speed_factor,
                                        target_duration=vo_duration + 0.3  # Small buffer
                                    )
                                    
                                    if slowmo_combined:
                                        # Upload to GCS if it's a Rendi storage URL
                                        if "storage.rendi.dev" in slowmo_combined:
                                            logger.info(f"   📤 Uploading slow-mo video to GCS...")
                                            gcs_key = f"rendi_videos/row_{row_num}_combined_slowmo_{int(time.time())}.mp4"
                                            uploaded_url = processor.gcs_storage_service.upload_video_from_url(slowmo_combined, gcs_key)
                                            if uploaded_url:
                                                slowmo_combined = uploaded_url
                                                logger.info(f"   ✅ Uploaded to GCS: {uploaded_url[:60]}...")
                                        
                                        combined_video_url = slowmo_combined
                                        logger.info(f"✅ Combined video extended with slow motion to match VO")
                                    else:
                                        logger.warning("⚠️ Slow motion failed, VO may extend past video end")
                                else:
                                    # Too much slowdown needed, keep as-is
                                    logger.info(f"ℹ️ Combined video ({combined_duration:.2f}s) is too short for slow motion ({duration_ratio:.2f}x needed > {max_slowdown:.1f}x max)")
                                    logger.info(f"   Keeping video as-is. VO may extend past video end.")
                            else:
                                logger.info(f"✅ Video duration ({combined_duration:.2f}s) matches or exceeds VO ({vo_duration:.2f}s)")
                    except Exception as e:
                        logger.warning(f"⚠️ Could not verify video duration vs VO: {e}")
        
        # =================================================================
        # STEP 7: Combine video with audio (TWO-STEP process)
        # =================================================================
        # If we have both voice AND music:
        #   Step 7a: Add voice to video
        #   Step 7b: Add background music to video (overlaid on voice)
        # If we have only voice OR only music:
        #   Single step: Add the available audio
        # =================================================================
        logger.info(f"🎬 [Row {row_num}] Step 7: Combining video with audio...")
        final_video_with_voice = None
        final_video_with_music = None
        
        if combined_video_url and final_audio_url:
            # Step 7a: Add voice/primary audio to video WITH RETRY LOGIC
            logger.info("🎬 Step 7a: Adding primary audio (voice) to video...")
            MAX_AUDIO_RETRIES = 3
            
            for audio_attempt in range(MAX_AUDIO_RETRIES):
                final_video_with_voice = processor.rendi_service.add_audio_to_video(
                    video_url=combined_video_url,
                    audio_url=final_audio_url
                )
                if final_video_with_voice:
                    # Validate that the video actually has audio
                    has_audio = processor.rendi_service.validate_video_has_audio(final_video_with_voice)
                    if has_audio:
                        logger.info(f"✅ Audio combination successful (attempt {audio_attempt + 1})")
                        break
                    else:
                        logger.warning(f"⚠️ Video has no audio track (attempt {audio_attempt + 1}/{MAX_AUDIO_RETRIES})")
                        final_video_with_voice = None
                else:
                    logger.warning(f"⚠️ Audio combination failed (attempt {audio_attempt + 1}/{MAX_AUDIO_RETRIES})")
                
                if audio_attempt < MAX_AUDIO_RETRIES - 1:
                    logger.info(f"   Retrying in 5 seconds...")
                    time.sleep(5)
            
            # Fallback: If audio combination failed, try using original audio from the video
            if not final_video_with_voice and combined_video_url:
                logger.warning("⚠️ All audio combination attempts failed, trying fallback...")
                # Try to extract and re-add original audio as fallback
                try:
                    original_audio_fallback = FFmpegProcessor.extract_audio_from_url(
                        video_url=video_url,
                        output_path=os.path.join(temp_dir, "fallback_audio.mp3"),
                        rendi_api_key=config.RENDI_API_KEY
                    )
                    if original_audio_fallback:
                        logger.info("🔄 Attempting fallback with original video audio...")
                        final_video_with_voice = processor.rendi_service.add_audio_to_video(
                            video_url=combined_video_url,
                            audio_url=original_audio_fallback
                        )
                        if final_video_with_voice:
                            logger.info("✅ Fallback audio combination successful")
                        else:
                            logger.error("❌ Fallback audio combination also failed")
                except Exception as fallback_error:
                    logger.error(f"❌ Fallback audio extraction failed: {fallback_error}")
            
            if final_video_with_voice:
                processor._update_sheet_cell(
                    row_num=row_num,
                    column=config.RENDI_SCENE_VOICE_COLUMN,
                    value=final_video_with_voice,
                    headers=headers
                )
                
                # Step 7b: If we have background music, add it as overlay (with retry)
                if new_music_url:
                    # We have music - add it as background (with or without voice)
                    if new_voice_url:
                        logger.info("🎵 Step 7b: Adding background music overlay to video with voice...")
                        base_video = final_video_with_voice
                        music_volume = 0.25  # Music at 25% to not overpower voice
                    else:
                        logger.info("🎵 Step 7b: Adding background music to video (no voice)...")
                        base_video = combined_video_url
                        music_volume = 0.5  # Music at 50% if no voice
                    
                    final_video_with_music = None
                    for music_attempt in range(MAX_AUDIO_RETRIES):
                        if base_video:
                            final_video_with_music = processor.rendi_service.add_background_music_to_video(
                                video_url=base_video,
                                music_url=new_music_url,
                                music_volume=music_volume
                            )
                            if final_video_with_music:
                                logger.info(f"✅ Background music added successfully (attempt {music_attempt + 1}): {final_video_with_music[:80]}...")
                                # Update the reference to use the version with music
                                if new_voice_url:
                                    final_video_with_voice = final_video_with_music
                                break
                            else:
                                logger.warning(f"⚠️ Music overlay failed (attempt {music_attempt + 1}/{MAX_AUDIO_RETRIES})")
                                if music_attempt < MAX_AUDIO_RETRIES - 1:
                                    logger.info(f"   Retrying in 3 seconds...")
                                    time.sleep(3)
                        else:
                            logger.error("❌ No base video available for music overlay")
                            break
                    
                    if final_video_with_music:
                        logger.info(f"✅ Step 7b complete: Video with music ready")
                        # Update final_video_with_voice to include music
                        final_video_with_voice = final_video_with_music
                    else:
                        logger.error("❌ CRITICAL: Could not add background music after all retries!")
                        result["errors"].append("Failed to add background music to video")
            else:
                # Mark as error - don't upload silent video
                logger.error("❌ CRITICAL: Could not add audio to video after all attempts!")
                result["errors"].append("Failed to add audio to video - video would be silent")
        
        # Handle case where we have music but no voice
        elif combined_video_url and new_music_url and not final_audio_url:
            logger.info("🎵 Step 7: Adding background music to video (no voice)...")
            final_video_with_music = None
            for music_attempt in range(MAX_AUDIO_RETRIES):
                final_video_with_music = processor.rendi_service.add_background_music_to_video(
                    video_url=combined_video_url,
                    music_url=new_music_url,
                    music_volume=0.5  # Music at 50% if no voice
                )
                if final_video_with_music:
                    logger.info(f"✅ Background music added successfully (attempt {music_attempt + 1})")
                    final_video_with_voice = final_video_with_music  # Use this as final video
                    break
                else:
                    logger.warning(f"⚠️ Music overlay failed (attempt {music_attempt + 1}/{MAX_AUDIO_RETRIES})")
                    if music_attempt < MAX_AUDIO_RETRIES - 1:
                        time.sleep(3)
            
            if not final_video_with_music:
                logger.error("❌ CRITICAL: Could not add background music after all retries!")
                result["errors"].append("Failed to add background music to video")
        
        # Step 11: Add subtitles with ZapCap (if requested AND video has speech)
        subtitled_video_url = None
        source_for_subtitles = final_video_with_voice or combined_video_url
        has_speech = audio_result.get("has_speech", False)
        
        # Debug logging for subtitle decision
        logger.info(f"📝 Step 11: Subtitle check - add_subtitles={add_subtitles}, has_speech={has_speech}, source_exists={bool(source_for_subtitles)}, zapcap_available={bool(processor.zapcap_service)}")
        
        if add_subtitles and source_for_subtitles:
            # Only send to ZapCap if video has speech (VO)
            if not has_speech:
                logger.info("📝 Step 11: Skipping ZapCap - no speech/VO in video (music only)")
                # No subtitles needed, will upload directly to GCS
            elif processor.zapcap_service:
                logger.info("📝 Step 11: Adding subtitles with ZapCap...")
                
                # Use subtitle_language if provided, else fall back to article_language, else "en"
                zapcap_language = subtitle_language or article_language or "en"
                
                # Check if we have TTS word segments (from article adaptation TTS generation)
                tts_word_segments = audio_result.get("tts_word_segments", [])
                tts_generated = audio_result.get("tts_generated", False)
                
                if tts_generated and tts_word_segments:
                    # TTS path: Use timestamped transcript from ElevenLabs
                    logger.info(f"   Using TTS transcript for subtitles ({len(tts_word_segments)} words, language: {zapcap_language})")
                    subtitled_video_url = processor.zapcap_service.add_subtitles(
                        video_url=source_for_subtitles,
                        language=zapcap_language,
                        transcript=tts_word_segments
                    )
                else:
                    # Voice change path: ZapCap will auto-transcribe using the Language column
                    logger.info(f"   Using auto-transcription for subtitles (language: {zapcap_language})")
                    subtitled_video_url = processor.zapcap_service.add_subtitles(
                        video_url=source_for_subtitles,
                        language=zapcap_language
                    )
                
                if subtitled_video_url:
                    subtitled_video_url = processor.rendi_service.transcode_social_sharing_mp4(subtitled_video_url)
                    processor._update_sheet_cell(
                        row_num=row_num,
                        column=config.SUBTITLED_VIDEO_COLUMN,
                        value=subtitled_video_url,
                        headers=headers
                    )
                    result["subtitled_video_url"] = subtitled_video_url
                    logger.info(f"✅ Subtitles added: {subtitled_video_url}")
                else:
                    logger.warning("⚠️ Could not add subtitles with ZapCap")
            else:
                logger.warning("⚠️ ZapCap service not available (no API key)")
        elif not add_subtitles:
            logger.info("📝 Step 11: Subtitles not requested (Add subtitles column is not 'yes')")
        elif not source_for_subtitles:
            logger.warning("⚠️ Step 11: No source video available for subtitles")
        
        # Step 11.5: Add CTA button overlay for "whole_video" mode
        # This is done after all processing so CTA appears throughout the entire video
        if cta_button and cta_text and cta_duration == "whole_video":
            source_for_cta = subtitled_video_url or final_video_with_voice or combined_video_url
            if source_for_cta:
                logger.info(f"🔘 [Row {row_num}] Adding CTA button for WHOLE VIDEO: '{cta_text}'...")
                try:
                    # Use the existing temp_dir from the function
                    cta_temp_dir = temp_dir  # Reuse existing temp directory
                    # Generate CTA button image
                    cta_image_url = processor.kie_service.generate_cta_button(cta_text)
                    
                    if cta_image_url:
                        # Process CTA button (remove green background)
                        cta_processed_url = _process_cta_button(
                            processor,
                            cta_image_url=cta_image_url,
                            temp_dir=cta_temp_dir,
                            row_num=row_num
                        )
                        
                        if cta_processed_url:
                            # Overlay CTA for entire video (start_time=0, end_time=None means whole video)
                            video_with_cta = processor.rendi_service.overlay_cta_on_video_timed(
                                video_url=source_for_cta,
                                cta_image_url=cta_processed_url,
                                position="center",
                                start_time=0.0,
                                end_time=None  # None means until end of video
                            )
                            
                            if video_with_cta:
                                # Update the source video for GCS upload
                                if subtitled_video_url:
                                    subtitled_video_url = video_with_cta
                                elif final_video_with_voice:
                                    final_video_with_voice = video_with_cta
                                else:
                                    combined_video_url = video_with_cta
                                logger.info(f"✅ [Row {row_num}] CTA button added for whole video")
                            else:
                                logger.warning(f"⚠️ [Row {row_num}] Failed to overlay CTA button")
                        else:
                            logger.warning(f"⚠️ [Row {row_num}] Failed to process CTA button image")
                    else:
                        logger.warning(f"⚠️ [Row {row_num}] Failed to generate CTA button image")
                except Exception as e:
                    logger.warning(f"⚠️ [Row {row_num}] CTA button overlay failed: {e}")
        
        # Step 12: Upload final video to GCS
        logger.info("📤 Step 12: Uploading final video to GCS...")
        
        # Choose the best available source video (prioritize subtitled if available)
        source_video = subtitled_video_url or final_video_with_voice or combined_video_url
        
        if source_video:
            timestamp = int(time.time())
            final_key = f"final_video_row_{row_num}_{timestamp}.mp4"
            final_gcs_url = processor.gcs_storage_service.upload_video_from_url(
                source_url=source_video,
                key_name=final_key
            )
            if final_gcs_url:
                processor._update_sheet_cell(
                    row_num=row_num,
                    column=config.FINAL_VIDEO_COLUMN,
                    value=final_gcs_url,
                    headers=headers
                )
                result["final_video_url"] = final_gcs_url
                result["success"] = True
    
    except Exception as e:
        logger.error(f"❌ [Row {row_num}] UNHANDLED EXCEPTION in process_single_video: {e}")
        logger.error(f"   [Row {row_num}] Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"   [Row {row_num}] Traceback:\n{traceback.format_exc()}")
        result["errors"].append(f"Unhandled exception: {str(e)}")
    
    finally:
        # Clean up temp directory
        try:
            import shutil
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass  # Ignore cleanup errors
        
    return result



def _process_single_scene(
    processor,
    scene: Dict[str, Any],
    row_num: int,
    headers: List[str],
    manual_instructions: str = "",
    animation_model: str = "runway",
    target_language: str = "en"
) -> Dict[str, Any]:
    """Process a single scene: OpenAI analysis → Nano Banana → Runway/Kling.
    
    This method is designed to run in parallel with other scenes.
    
    Args:
        scene: Scene data with scene_num, start_time, end_time, duration, frame_paths.
        row_num: Row number in the Google Sheet (1-based).
        headers: List of column headers.
        manual_instructions: Optional custom instructions for OpenAI analysis.
        animation_model: Video generation model - "runway" (default) or "kling".
        target_language: Target language for text on images (e.g., 'en', 'he', 'da').
        
    Returns:
        Dict with image_url, video_url, and any errors.
    """
    scene_num = scene["scene_num"]
    scene_duration = scene.get("duration", 5.0)  # Default 5 seconds if not specified
    
    result = {
        "scene_num": scene_num,
        "duration": scene_duration,
        "image_url": None,
        "video_url": None,
        "first_prompt": None,
        "second_prompt": None,
        "error": None
    }
    
    try:
        logger.info(f"🔍 Scene {scene_num}: Analyzing {len(scene['frame_paths'])} frames with OpenAI...")
        
        # Step 1: Analyze frames with OpenAI (with manual instructions if provided)
        call_fn_scene = lambda msgs, **kw: processor._call_llm("analyze_scene_frames", msgs, **kw)
        analysis = analyze_scene_frames(
            call_fn_scene,
            scene["frame_paths"],
            manual_instructions=manual_instructions
        )
        
        first_prompt = analysis.get("first_prompt", "")
        second_prompt = analysis.get("second_prompt", "")
        result["first_prompt"] = first_prompt
        result["second_prompt"] = second_prompt
        
        # Update Google Sheet with prompts (thread-safe with gspread)
        processor._update_sheet_cell(
            row_num=row_num,
            column=config.SCENE_FIRST_PROMPT_PREFIX.format(n=scene_num),
            value=first_prompt,
            headers=headers
        )
        processor._update_sheet_cell(
            row_num=row_num,
            column=config.SCENE_SECOND_PROMPT_PREFIX.format(n=scene_num),
            value=second_prompt,
            headers=headers
        )
        
        # Step 2: Generate image with Nano Banana
        if first_prompt:
            logger.info(f"🍌 Scene {scene_num}: Generating image with Nano Banana...")
            image_url = processor.kie_service.generate_image_nano_banana(
                prompt=first_prompt,
                target_language=target_language
            )
            
            if image_url:
                result["image_url"] = image_url
                processor._update_sheet_cell(
                    row_num=row_num,
                    column=config.SCENE_NEW_IMAGE_PREFIX.format(n=scene_num),
                    value=image_url,
                    headers=headers
                )
                
                # Step 3: Generate video with Runway or Kling (using scene duration)
                if second_prompt:
                    logger.info(f"🎬 Scene {scene_num}: Generating video with {animation_model.upper()} (target: {scene_duration:.1f}s)...")
                    if animation_model == "kling":
                        video_url = processor.kie_service.generate_video_kling(
                            prompt=second_prompt,
                            image_url=image_url,
                            duration=scene_duration
                        )
                    else:
                        video_url = processor.kie_service.generate_video_runway(
                            prompt=second_prompt,
                            image_url=image_url,
                            duration=scene_duration
                        )
                    
                    if video_url:
                        # Upload to GCS immediately to avoid temp URL expiration
                        try:
                            gcs_video_url = processor._upload_video_to_gcs_from_url(
                                video_url=video_url,
                                row_num=row_num,
                                scene_num=scene_num
                            )
                            if gcs_video_url:
                                video_url = gcs_video_url
                                logger.info(f"✅ Scene {scene_num}: Video uploaded to GCS (permanent URL)")
                        except Exception as upload_err:
                            logger.warning(f"⚠️ Scene {scene_num}: GCS upload failed, using temp URL: {upload_err}")
                        
                        result["video_url"] = video_url
                        processor._update_sheet_cell(
                            row_num=row_num,
                            column=config.SCENE_NEW_VIDEO_PREFIX.format(n=scene_num),
                            value=video_url,
                            headers=headers
                        )
                        logger.info(f"✅ Scene {scene_num}: Video generated successfully!")
                    else:
                        logger.warning(f"⚠️ Scene {scene_num}: {animation_model.upper()} video generation failed")
            else:
                logger.warning(f"⚠️ Scene {scene_num}: Nano Banana image generation failed")
        else:
            logger.warning(f"⚠️ Scene {scene_num}: No prompt generated by OpenAI")
            
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"❌ Scene {scene_num}: Error during processing: {e}")
    
    return result



def _process_cta_button(
    processor,
    cta_image_url: str,
    temp_dir: str,
    row_num: int
) -> Optional[str]:
    """Process CTA button image: check if already processed, or download and process.
    
    If the image was generated by PIL (already has transparent background), 
    return it directly. Otherwise, download, remove green background, and upload.
    
    Args:
        cta_image_url: URL or path of the generated CTA button image.
        temp_dir: Temporary directory for processing.
        row_num: Row number for logging.
        
    Returns:
        URL of the processed CTA button image, or None if failed.
    """
    try:
        # Check if it's already uploaded to GCS (PIL-generated buttons are uploaded directly)
        # PIL-generated buttons have "cta_button_" in their GCS key
        if "cta_button_" in cta_image_url and "s3.amazonaws.com" in cta_image_url:
            logger.info(f"✅ CTA button already processed (PIL-generated): {cta_image_url}")
            return cta_image_url
        
        # Check if it's a local file path (fallback case)
        if os.path.isfile(cta_image_url):
            logger.info("🎨 Processing local CTA button image...")
            with open(cta_image_url, 'rb') as f:
                image_data = f.read()
            
            timestamp = int(time.time())
            cta_key = f"cta_button_row_{row_num}_{timestamp}.png"
            
            cta_url = processor.gcs_storage_service.upload_image_bytes(
                image_data=image_data,
                key_name=cta_key
            )
            
            if cta_url:
                logger.info(f"✅ CTA button uploaded: {cta_url}")
                return cta_url
            else:
                logger.error("❌ Failed to upload CTA button to GCS")
                return None
        
        # Old flow: download from URL and process (for Nano Banana generated buttons)
        logger.info("🎨 Processing CTA button image from URL...")
        
        # Download the CTA image
        response = requests.get(cta_image_url, timeout=60)
        response.raise_for_status()
        
        original_path = os.path.join(temp_dir, "cta_original.png")
        with open(original_path, 'wb') as f:
            f.write(response.content)
        logger.info(f"✅ Downloaded CTA image: {original_path}")
        
        # Remove green background (samples actual green from image)
        final_path = os.path.join(temp_dir, "cta_no_bg.png")
        if not remove_green_background(original_path, final_path):
            logger.warning("⚠️ Failed to remove background, using original")
            final_path = original_path
        
        # Upload to GCS
        with open(final_path, 'rb') as f:
            image_data = f.read()
        
        timestamp = int(time.time())
        cta_key = f"cta_button_row_{row_num}_{timestamp}.png"
        
        # Upload to GCS (using the GCS storage service)
        cta_url = processor.gcs_storage_service.upload_image_bytes(
            image_data=image_data,
            key_name=cta_key
        )
        
        if cta_url:
            logger.info(f"✅ CTA button processed and uploaded: {cta_url}")
            return cta_url
        else:
            logger.error("❌ Failed to upload CTA button to GCS")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error processing CTA button: {e}")
        return None

