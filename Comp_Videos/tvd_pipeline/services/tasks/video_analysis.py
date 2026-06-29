"""Video analysis tasks -- scene detection, product detection, style analysis.

Free functions that analyse video frames or full videos.

Functions that need native Vertex video upload take ``vertex_provider`` as
their first parameter.  Functions that use standard chat completions take
``call_fn`` instead.  Pure data-extraction helpers take neither.
"""

import base64
import json
import logging
import os
import time as _time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from tvd_pipeline.config import Config
from tvd_pipeline.data_loader import get_language_name
from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.utils import get_cultural_adaptation_instructions

config = Config()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _get_empty_analysis() -> Dict[str, Any]:
    """Return empty analysis structure when Gemini is unavailable."""
    return {
        "scenes": [],
        "product": {
            "detected": False,
            "type": "",
            "visual_description": "",
            "purpose": "",
            "usage_method": "",
            "application_rules": "",
            "best_frame_timestamps": [],
        },
        "video_story": {
            "type": "unknown",
            "one_sentence_summary": "",
            "subject_changes": {
                "has_visible_change": False,
                "start_state": "",
                "end_state": "",
            },
        },
        "new_voiceover": {
            "full_script": "",
            "word_count": 0,
            "style": "",
        },
        "cta": {
            "needs_cta": False,
            "button_text": "",
            "scene_number": 0,
        },
        "style": {
            "aesthetic": "modern",
            "lighting": "",
            "mood": "",
            "style_prefix": "",
        },
        "audio": {
            "original_has_vo": False,
            "original_vo_style": "",
            "original_vo_gender": "unknown",
            "music_mood": "",
        },
    }


def _empty_video_analysis_result(
    pyscenedetect_timestamps: List[float],
    video_duration: float,
) -> Dict[str, Any]:
    """Create empty/fallback result using original PySceneDetect timestamps."""
    corrected_scenes = []
    scene_prompts = []

    for i, ts in enumerate(pyscenedetect_timestamps):
        if i + 1 < len(pyscenedetect_timestamps):
            end_ts = pyscenedetect_timestamps[i + 1]
        else:
            end_ts = video_duration

        corrected_scenes.append({
            "scene_num": i + 1,
            "start": ts,
            "end": end_ts,
            "reason": "Fallback - using original PySceneDetect timing",
        })
        scene_prompts.append({
            "scene_num": i + 1,
            "image_prompt": "",
            "motion_prompt": "",
        })

    return {
        "corrected_scenes": corrected_scenes,
        "scene_prompts": scene_prompts,
    }


# ---------------------------------------------------------------------------
# Internal helpers for analyze_scene_frames
# ---------------------------------------------------------------------------

def _generate_image_prompt(
    call_fn: Callable,
    image_contents: List[Dict],
    manual_instructions: str = "",
) -> Dict[str, Any]:
    """Generate image recreation prompt using an LLM.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        image_contents: List of base64 encoded images (OpenAI content parts).
        manual_instructions: Optional custom instructions.

    Returns:
        Dict with analysis, text_content, and first_prompt.
    """
    try:
        analysis_prompt = get_prompt_loader().get("shared_image_analysis_system")
        base_system_prompt = get_prompt_loader().get("shared_image_prompt_system")

        if manual_instructions:
            system_prompt = (
                f"**\U0001f6a8 USER INSTRUCTIONS (HIGHEST PRIORITY - MUST FOLLOW):**\n"
                f"{manual_instructions}\n\n"
                "These instructions OVERRIDE any conflicting default guidelines below. "
                "Apply them consistently to your entire output, especially the 'first_prompt'.\n\n"
                "---\n\n"
                f"{base_system_prompt}"
            )
        else:
            system_prompt = base_system_prompt

        instruction_text = "Analyze these frames from a video scene and generate an image recreation prompt:"

        user_content = [
            {"type": "text", "text": instruction_text},
            {"type": "text", "text": analysis_prompt},
        ] + image_contents

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        result = call_fn(messages, response_format={"type": "json_object"})
        text = result.get("text", "")
        if not text:
            return {"analysis": "", "text_content": {}, "first_prompt": ""}
        parsed = json.loads(text.strip())
        logger.info("Image prompt generated successfully")
        return parsed

    except Exception as e:
        logger.error(f"Error generating image prompt: {e}")
        return {"analysis": "", "text_content": {}, "first_prompt": ""}


def _generate_motion_prompt(
    call_fn: Callable,
    image_contents: List[Dict],
    manual_instructions: str = "",
) -> Dict[str, Any]:
    """Generate motion/animation prompt using an LLM.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        image_contents: List of base64 encoded images (OpenAI content parts).
        manual_instructions: Optional custom instructions.

    Returns:
        Dict with second_prompt (motion prompt).
    """
    try:
        motion_analysis_prompt = (
            "Analyze these video frames to understand the motion and animation.\n\n"
            "Focus ONLY on:\n"
            "1. Camera movement (pan left/right, tilt up/down, zoom in/out, dolly, crane, static)\n"
            "2. Subject movement (walking, running, gesturing, facial expressions)\n"
            "3. Object movement (falling, flying, rotating, scaling)\n"
            "4. Transitions and effects between frames\n"
            "5. Speed and timing of movements\n"
            "6. Direction of movement"
        )

        system_prompt = get_prompt_loader().get("shared_motion_gen_system")

        instruction_text = "Analyze the motion in these frames and generate a Runway motion prompt:"
        if manual_instructions:
            instruction_text += f"\n\n**SPECIAL INSTRUCTIONS FROM USER:**\n{manual_instructions}"

        user_content = [
            {"type": "text", "text": instruction_text},
            {"type": "text", "text": motion_analysis_prompt},
        ] + image_contents

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        result = call_fn(messages, temperature=0.3, response_format={"type": "json_object"})
        text = result.get("text", "")
        if not text:
            return {"second_prompt": ""}
        parsed = json.loads(text.strip())
        logger.info("Motion prompt generated successfully")
        return parsed

    except Exception as e:
        logger.error(f"Error generating motion prompt: {e}")
        return {"second_prompt": ""}


# ---------------------------------------------------------------------------
# Vertex-provider video analysis (native Vertex video upload)
# ---------------------------------------------------------------------------

def analyze_video_comprehensive(
    vertex_provider,
    video_path: str,
    article_content: Optional[Dict[str, str]] = None,
    manual_instructions: str = "",
    original_transcript: str = "",
    target_language: str = "en",
    article_related_to_video: bool = True,
) -> Dict[str, Any]:
    """Analyze entire video with Gemini via Vertex AI.

    Uploads the video to GCS, sends it alongside a comprehensive analysis
    prompt, and returns the structured JSON result.

    Args:
        vertex_provider: ``VertexAIProvider`` instance (provides GCS upload,
            auth headers, and endpoint URLs).
        video_path: Path to the video file.
        article_content: Optional article content for context.
        manual_instructions: Optional manual instructions.
        original_transcript: Transcript of what is said in the video.
        target_language: Target language code for VO script.
        article_related_to_video: True if article is similar to video content.

    Returns:
        Comprehensive analysis dict (scenes, product, style, etc.).
    """
    if not vertex_provider.initialized:
        logger.warning("Gemini not initialized, returning empty analysis")
        return _get_empty_analysis()

    video_url = None

    try:
        # Upload video to GCS to get public URL
        video_url = vertex_provider._upload_video_to_gcs(video_path)
        if not video_url:
            logger.warning("Could not upload video to GCS, falling back to GPT-4o")
            return _get_empty_analysis()

        # Build the comprehensive analysis prompt
        article_context = ""
        if article_content:
            title = article_content.get("title", "")
            first_p = article_content.get("first_paragraph", "")
            free_text = article_content.get("free_text", "")
            article_text_combined = free_text or f"{title}\n{first_p}"

            if title or first_p or free_text:
                cultural_instructions = get_cultural_adaptation_instructions(target_language)

                if article_related_to_video:
                    article_context = get_prompt_loader().get(
                        "shared_article_adaptation_similar_gemini",
                        title=title,
                        first_p=first_p[:500] if first_p else "N/A",
                        free_text=free_text[:1000] if free_text else "N/A",
                        cultural_instructions=cultural_instructions,
                    )
                else:
                    article_context = get_prompt_loader().get(
                        "shared_article_adaptation_different_gemini",
                        title=title,
                        first_p=first_p[:500] if first_p else "N/A",
                        free_text=free_text[:1000] if free_text else "N/A",
                        cultural_instructions=cultural_instructions,
                    )

        # Language context for VO script
        language_context = (
            f"\n\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f CRITICAL - LANGUAGE REQUIREMENT \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n"
            f"TARGET LANGUAGE: {target_language.upper()}\n"
            f"The new voiceover script (full_script) MUST be written ENTIRELY in {target_language.upper()}.\n"
            f"Do NOT use any other language. The script will be read by a TTS system in {target_language}.\n"
        )

        instructions_context = ""
        if manual_instructions:
            instructions_context = f"\nMANUAL INSTRUCTIONS:\n{manual_instructions}\n"

        transcript_context = ""
        if original_transcript:
            transcript_context = (
                f'\nAUDIO TRANSCRIPT (what is being said in the video):\n'
                f'"""{original_transcript}"""\n\n'
                f'IMPORTANT: Analyze how the audio/voiceover relates to what\'s shown visually in each scene.\n'
            )

        # Build goal statement based on article-video relationship
        if article_related_to_video:
            goal_statement = (
                "You are an expert video director and storyteller. Your job is to DEEPLY UNDERSTAND "
                "this video's story and create PRECISE, ACCURATE prompts that recreate the ORIGINAL "
                "video's visuals and story exactly.\n\n"
                "\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f YOUR GOAL: ADAPT the video for a NEW OFFER while keeping SIMILAR visuals \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n"
                "- Watch the ORIGINAL video carefully - understand its visual style\n"
                "- The new video should LOOK SIMILAR to the original\n"
                "- But adapt the product/offer and messaging to match the ARTICLE content\n"
                "- Your prompts should recreate the visual style while adapting the content"
            )
        else:
            goal_statement = (
                "You are an expert video director and storyteller. Your job is to understand this "
                "video's VISUAL STYLE and create NEW content that matches the article while keeping "
                "the same STYLE and ATMOSPHERE.\n\n"
                "\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f YOUR GOAL: CREATE NEW CONTENT with the SAME VISUAL STYLE \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n"
                "- Watch the ORIGINAL video to understand its STYLE (lighting, camera work, mood, energy, quality)\n"
                "- DO NOT copy the original video's content/product - it's COMPLETELY DIFFERENT from the article\n"
                "- CREATE NEW visuals that are appropriate for the ARTICLE content\n"
                "- The new video should FEEL LIKE the original (same style/mood) but SHOW the article's content\n"
                "- Your prompts should describe NEW scenes for the article content, using the original's style"
            )

        # Build workflow steps based on article-video relationship
        if article_related_to_video:
            workflow_steps = (
                "\U0001f3ac YOUR MISSION: Understand the VIDEO'S COMPLETE STORY and generate prompts "
                "that ADAPT it for the new offer.\n\n"
                "\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f CRITICAL WORKFLOW - FOLLOW THIS EXACTLY \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n\n"
                "STEP 1: UNDERSTAND THE COMPLETE STORY (DO THIS FIRST!)\n"
                "1. **WATCH THE ENTIRE VIDEO** - Don't just analyze frames, watch the complete narrative\n"
                "2. **IDENTIFY THE STORY TYPE** - Is it transformation? Demo? Testimonial? Problem-solution? Before/after?\n"
                "3. **UNDERSTAND THE NARRATIVE ARC** - Beginning -> Middle -> End. What's the journey?\n"
                "4. **UNDERSTAND SCENE CONNECTIONS** - How do scenes connect? What changes between scenes? Why?\n"
                "5. **TRACK SUBJECT CHANGES** - Does the subject look different in different scenes? Why? "
                "(e.g., weight loss, mood change, clothing change)\n"
                "6. **UNDERSTAND PRODUCT ROLE** - When does the product appear? What's its role in the story? "
                "How does it connect to the narrative?\n\n"
                "STEP 2: ANALYZE EACH SCENE INDIVIDUALLY\n"
                "For EACH scene, watch the ORIGINAL video at that scene's timestamp:\n"
                "1. What do you ACTUALLY see? (subject appearance, clothing, setting, lighting, camera angle)\n"
                "2. What's the EXACT visual state? (match the original exactly)\n"
                "3. Is the product visible? (set product_visible accurately)\n"
                "4. How does this scene connect to the previous scene? (what changed?)\n"
                "5. What's the subject's state in THIS scene? (match the original exactly)\n\n"
                "STEP 3: CREATE ADAPTED PROMPTS\n"
                "Only AFTER understanding the complete story AND analyzing each scene, create prompts that:\n"
                "- KEEP the ORIGINAL video's visual style (camera angles, lighting, mood)\n"
                "- ADAPT the product to match the ARTICLE's product/offer\n"
                "- ADAPT the messaging to match the ARTICLE content\n"
                "- Match the scene structure of the original (same number of scenes, similar durations)\n"
                "- Include the ARTICLE's product when appropriate (replacing the original product)"
            )
        else:
            workflow_steps = (
                "\U0001f3ac YOUR MISSION: Extract the video's VISUAL STYLE and create NEW content for the article.\n\n"
                "\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f CRITICAL WORKFLOW FOR DIFFERENT CONTENT - FOLLOW THIS EXACTLY \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n\n"
                "STEP 1: EXTRACT THE VIDEO'S VISUAL STYLE (DO THIS FIRST!)\n"
                "1. **WATCH THE ENTIRE VIDEO** - Focus on HOW it looks, not WHAT it shows\n"
                "2. **IDENTIFY THE STYLE ELEMENTS:**\n"
                "   - Lighting style (natural, studio, dramatic, soft, etc.)\n"
                "   - Camera work (static, handheld, smooth movements, etc.)\n"
                "   - Color palette (warm, cool, vibrant, muted, etc.)\n"
                "   - Mood/energy (energetic, calm, professional, casual, etc.)\n"
                "   - Production quality (UGC style, professional, cinematic, etc.)\n"
                "   - Framing preferences (close-ups, wide shots, etc.)\n"
                "3. **IDENTIFY THE PACING** - How long are scenes? What's the rhythm?\n"
                "4. **IDENTIFY THE NARRATIVE STRUCTURE** - Hook -> Problem -> Solution -> CTA?\n"
                "5. **DO NOT FOCUS ON THE PRODUCT** - The original product is IRRELEVANT for this task\n\n"
                "STEP 2: UNDERSTAND THE ARTICLE CONTENT\n"
                "For EACH piece of information in the article:\n"
                "1. What is the product/offer? (This is what we're advertising)\n"
                "2. What are the benefits? (These should be shown in the video)\n"
                "3. Who is the target audience? (People like this should appear in scenes)\n"
                "4. What emotions should the video evoke? (Match the article's tone)\n"
                "5. What call-to-action is needed? (What should viewers do?)\n\n"
                "STEP 3: CREATE NEW PROMPTS WITH ORIGINAL STYLE\n"
                "Create prompts for a NEW video that:\n"
                "- HAS THE SAME STYLE as the original (lighting, camera, mood, quality, pacing)\n"
                "- SHOWS NEW CONTENT appropriate for the ARTICLE\n"
                "- DOES NOT include the original video's product AT ALL\n"
                "- Features people, settings, and actions relevant to the ARTICLE\n"
                "- Uses the same narrative structure (hook, problem, solution, CTA) but for the NEW topic\n"
                "- Has the same number of scenes with similar durations as the original"
            )

        analysis_prompt = get_prompt_loader().get(
            "shared_gemini_comprehensive_analysis",
            goal_statement=goal_statement,
            language_context=language_context,
            article_context=article_context,
            instructions_context=instructions_context,
            transcript_context=transcript_context,
            workflow_steps=workflow_steps,
        )

        logger.info("Analyzing video with Gemini 3 Pro (via Kie.ai)...")

        # Build request payload for Kie.ai Gemini 3 Pro endpoint
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": analysis_prompt},
                        {"type": "image_url", "image_url": {"url": video_url}},
                    ],
                }
            ],
            "stream": False,
            "include_thoughts": False,
            "reasoning_effort": "low",
        }

        # Send request to Vertex AI Gemini endpoint
        response = requests.post(
            vertex_provider._get_vertex_url(vertex_provider.model),
            headers=vertex_provider._get_vertex_headers(),
            json=payload,
            timeout=300,
        )
        response.raise_for_status()

        result = response.json()

        logger.info(f"Gemini response status: {response.status_code}")
        logger.info(f"Gemini response keys: {list(result.keys())}")

        if "error" in result:
            logger.error(f"Gemini API error: {result.get('error')}")
            return _get_empty_analysis()

        # Extract content from response
        if "choices" in result and len(result["choices"]) > 0:
            choice = result["choices"][0]
            logger.info(f"Choice keys: {list(choice.keys())}")
            message = choice.get("message", {})
            logger.info(f"Message keys: {list(message.keys())}")
            response_text = message.get("content", "")
            logger.info(f"Content length: {len(response_text)} chars")
        else:
            logger.error(f"No choices in Gemini response. Keys: {list(result.keys())}")
            logger.error(f"Full response: {json.dumps(result)[:1000]}")
            return _get_empty_analysis()

        # Clean up response if needed
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        logger.info(f"Gemini raw response (first 500 chars): {response_text[:500]}")

        analysis = json.loads(response_text.strip())

        logger.info("Gemini video analysis complete:")
        logger.info(f"   - Scenes detected: {len(analysis.get('scenes', []))}")
        logger.info(f"   - Product detected: {analysis.get('product', {}).get('detected', False)}")
        logger.info(f"   - Video type: {analysis.get('video_story', {}).get('type', 'unknown')}")
        logger.info(f"   - Style: {analysis.get('style', {}).get('aesthetic', 'unknown')}")

        scenes = analysis.get("scenes", [])
        if scenes:
            logger.info(f"   - First scene image prompt: {scenes[0].get('prompts', {}).get('image_prompt', 'N/A')[:60]}...")
        if analysis.get("new_voiceover", {}).get("full_script"):
            logger.info(f"   - New VO script: {analysis['new_voiceover']['full_script'][:60]}...")

        # Clean up the uploaded video from GCS
        vertex_provider._cleanup_gcs_video(video_url)

        return analysis

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Gemini response as JSON: {e}")
        if video_url:
            vertex_provider._cleanup_gcs_video(video_url)
        return _get_empty_analysis()
    except Exception as e:
        logger.error(f"Error in Gemini video analysis: {e}")
        traceback.print_exc()
        if video_url:
            vertex_provider._cleanup_gcs_video(video_url)
        return _get_empty_analysis()


def analyze_reference_video_structure(
    vertex_provider,
    video_path: str,
    llm_logger=None,
) -> Dict[str, Any]:
    """Analyze a reference video and return only its narrative structure.

    Used by the product video pipeline when Video reference column has a URL.

    Args:
        vertex_provider: ``VertexAIProvider`` instance.
        video_path: Path to local video file.

    Returns:
        Dict with ``scene_count`` and ``scenes`` list, or ``{}`` on failure.
    """
    if not vertex_provider.initialized:
        logger.warning("Gemini not initialized, returning empty reference structure")
        return {}

    video_url = None
    try:
        video_url = vertex_provider._upload_video_to_gcs(video_path)
        if not video_url:
            logger.warning("Could not upload reference video to GCS")
            return {}

        structure_prompt = get_prompt_loader().get("shared_reference_video_structure")

        # Vertex AI generateContent: need gs:// URI for fileData
        if "storage.googleapis.com/" in video_url:
            gs_uri = "gs://" + video_url.split("storage.googleapis.com/", 1)[1]
        else:
            gs_uri = video_url

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"fileData": {"mimeType": "video/mp4", "fileUri": gs_uri}},
                        {"text": structure_prompt},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 4096,
            },
        }

        # Use a video-analysis model
        video_model = (
            getattr(config, "GEMINI_VIDEO_ANALYSIS_MODEL", None)
            or getattr(config, "VERTEX_AI_MODEL", "gemini-2.5-flash")
            or "gemini-2.5-flash"
        )

        result = vertex_provider.raw_generate_content(payload, model=video_model)

        if llm_logger:
            llm_logger.log("analyze_reference_video_structure", "vertex", video_model, payload, result)

        text = result.get("text", "")

        if video_url:
            vertex_provider._cleanup_gcs_video(video_url)

        if not text:
            logger.warning("No content in Gemini reference structure response")
            return {}

        # Strip markdown code block if present
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        data = json.loads(text)
        scene_count = data.get("scene_count", 0)
        scenes = data.get("scenes", [])
        if not isinstance(scenes, list) or scene_count <= 0:
            return {}

        # Normalize each scene entry
        out_scenes = []
        for s in scenes:
            if not isinstance(s, dict):
                continue
            role = s.get("narrative_role") or "transition"
            dur = s.get("duration_seconds")
            if dur is None:
                dur = s.get("duration", 3)
            try:
                dur = float(dur) if dur is not None else 3.0
            except (TypeError, ValueError):
                dur = 3.0
            entry = {"narrative_role": str(role), "duration_seconds": dur}
            if s.get("content_summary"):
                entry["content_summary"] = str(s.get("content_summary", ""))[:500]
            if s.get("vo_snippet"):
                entry["vo_snippet"] = str(s.get("vo_snippet", ""))[:400]
            out_scenes.append(entry)

        if not out_scenes:
            return {}

        logger.info(f"Reference video structure: {len(out_scenes)} scenes extracted")
        return {"scene_count": len(out_scenes), "scenes": out_scenes}

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse reference structure JSON: {e}")
        if video_url:
            vertex_provider._cleanup_gcs_video(video_url)
        return {}
    except Exception as e:
        logger.warning(f"Error analyzing reference video structure: {e}")
        if video_url:
            vertex_provider._cleanup_gcs_video(video_url)
        return {}


# ---------------------------------------------------------------------------
# Pure data extraction (no LLM call)
# ---------------------------------------------------------------------------

def get_scene_prompt_context(
    analysis: Dict[str, Any],
    scene_number: int,
) -> Dict[str, Any]:
    """Extract relevant context for a specific scene from the comprehensive analysis.

    Args:
        analysis: Full video analysis from ``analyze_video_comprehensive()``.
        scene_number: 1-indexed scene number.

    Returns:
        Context dict with scene-specific and global style information.
    """
    scenes = analysis.get("scenes", [])
    product = analysis.get("product", {})
    style = analysis.get("style", {})

    scene_info = {}
    for scene in scenes:
        if scene.get("scene_number") == scene_number:
            scene_info = scene
            break

    prompts = scene_info.get("prompts", {})
    understanding = scene_info.get("understanding", {})

    return {
        "scene_info": scene_info,
        "understanding": understanding,
        "prompts": prompts,
        "product": product,
        "style": style,
        "style_prefix": style.get("style_prefix", ""),
        "narrative_role": understanding.get("narrative_role", ""),
        "image_prompt": prompts.get("image_prompt", ""),
        "motion_prompt": prompts.get("motion_prompt", ""),
        "product_visible": understanding.get("product_visible", False),
        "product_action": understanding.get("product_action", ""),
    }


# ---------------------------------------------------------------------------
# OpenAI-style call_fn functions (product/style/structure analysis)
# ---------------------------------------------------------------------------

def detect_product_in_frames(
    call_fn: Callable,
    frame_paths: List[str],
    min_confidence: float = 0.7,
    audio_transcript: str = "",
    video_duration: float = 0,
) -> Dict[str, Any]:
    """Comprehensive video analysis: detect product, understand narrative, correlate with VO.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        frame_paths: List of paths to frame images.
        min_confidence: Minimum confidence threshold (0-1).
        audio_transcript: The transcribed VO/audio from the video.
        video_duration: Total video duration in seconds.

    Returns:
        Dict with comprehensive video understanding.
    """
    try:
        logger.info(f"[VIDEO ANALYSIS] Analyzing {len(frame_paths)} frames + audio for comprehensive understanding...")

        # Encode images to base64
        image_contents = []
        for frame_path in frame_paths:
            if not os.path.exists(frame_path):
                logger.warning(f"[PRODUCT] Frame not found: {frame_path}")
                continue
            with open(frame_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
                image_contents.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_data}",
                        "detail": "high",
                    },
                })

        if not image_contents:
            logger.warning("[PRODUCT] No valid frames to analyze")
            return {"has_product": False}

        # Build audio context if available
        audio_context = ""
        if audio_transcript and len(audio_transcript) > 10:
            frames_count = len(frame_paths)
            seconds_per_frame = video_duration / frames_count if video_duration > 0 and frames_count > 0 else 0.5
            audio_context = (
                f'\n=== AUDIO/VOICEOVER TRANSCRIPT ===\n'
                f'"{audio_transcript}"\n\n'
                f'Video duration: {video_duration:.1f} seconds\n'
                f'Frames analyzed: {frames_count} (1 frame every ~{seconds_per_frame:.2f} seconds)\n\n'
                f'IMPORTANT: Correlate what is SAID in the VO with what is SHOWN in frames.\n'
                f'Frame 0 = start of video (0:00), Frame {frames_count - 1} = end of video ({video_duration:.1f}s)\n'
                f'=================================\n'
            )

        system_prompt = get_prompt_loader().get("shared_video_detect_product_system")

        frame_count = len(frame_paths)
        user_prompt = get_prompt_loader().get(
            "shared_video_detect_product_user",
            frame_count=frame_count,
            audio_context=audio_context,
        )

        user_content = [{"type": "text", "text": user_prompt}] + image_contents

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        result = call_fn(messages, temperature=0.2, response_format={"type": "json_object"})
        text = result.get("text", "")
        if not text:
            logger.warning("[PRODUCT] LLM returned empty content")
            return {"has_product": False}

        parsed = json.loads(text.strip())

        # Check confidence threshold
        if parsed.get("has_product") and parsed.get("overall_confidence", 0) < min_confidence:
            logger.info(
                f"[PRODUCT] Product detected but confidence too low: "
                f"{parsed.get('overall_confidence'):.2f} < {min_confidence}"
            )
            parsed["has_product"] = False

        # Log result
        if parsed.get("has_product"):
            logger.info(f"[PRODUCT] Detected: {parsed.get('product_detected')}")
            logger.info(f"   Brand: {parsed.get('product_details', {}).get('brand', 'unknown')}")
            logger.info(f"   Purpose: {parsed.get('product_purpose', 'unknown')[:100]}...")
            logger.info(f"   Confidence: {parsed.get('overall_confidence', 0):.2f}")
            logger.info(f"   Best frame: {parsed.get('best_frame_index')}")
            usage_contexts = parsed.get("usage_contexts", [])
            if usage_contexts:
                context_types = [c.get("context_type") for c in usage_contexts]
                logger.info(f"   Usage contexts: {', '.join(context_types)}")
        else:
            logger.info("[PRODUCT] No product detected, continuing with standard flow")

        return parsed

    except Exception as e:
        logger.error(f"[PRODUCT] Detection error: {e}")
        return {"has_product": False, "error": str(e)}


def analyze_video_structure(
    call_fn: Callable,
    frame_paths: List[str],
    article_content: Optional[Dict[str, str]] = None,
    manual_instructions: str = "",
    product_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Analyze the video's narrative structure and plan scene content.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        frame_paths: List of frame paths from across the video.
        article_content: Dict with keys: free_text, title, first_paragraph, rest_content.
        manual_instructions: Optional manual instructions from the sheet.
        product_info: Product detection results (if available).

    Returns:
        Dict with video_structure, scene_plan, content_mapping.
    """
    try:
        logger.info("[STRUCTURE] Analyzing video structure with article context...")

        article = article_content or {}
        free_text = article.get("free_text", "")
        title = article.get("title", "")
        first_para = article.get("first_paragraph", "")
        rest_content = article.get("rest_content", "")

        full_article = ""
        if free_text:
            full_article = free_text
        else:
            parts = [p for p in [title, first_para, rest_content] if p]
            full_article = "\n\n".join(parts)

        product_context = ""
        if product_info and product_info.get("has_product"):
            product_context = (
                f"\nPRODUCT DETECTED:\n"
                f"- Type: {product_info.get('product_detected', 'unknown')}\n"
                f"- Purpose: {product_info.get('product_purpose', 'unknown')}\n"
                f"- Usage method: {product_info.get('product_usage_method', 'unknown')}\n"
                f"- Usage contexts in video: "
                f"{', '.join([c.get('context_type', '') for c in product_info.get('usage_contexts', [])])}\n"
            )

        # Encode sample frames (use 5 evenly distributed)
        image_contents = []
        sample_indices = [0, len(frame_paths) // 4, len(frame_paths) // 2, (len(frame_paths) * 3) // 4, len(frame_paths) - 1]
        sample_indices = list(set([min(i, len(frame_paths) - 1) for i in sample_indices]))

        for idx in sorted(sample_indices)[:5]:
            frame_path = frame_paths[idx] if idx < len(frame_paths) else frame_paths[-1]
            if os.path.exists(frame_path):
                with open(frame_path, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode("utf-8")
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}",
                            "detail": "low",
                        },
                    })

        if not image_contents:
            logger.warning("[STRUCTURE] No frames available for analysis")
            return {"video_structure": "unknown", "scene_plan": []}

        system_prompt = get_prompt_loader().get("shared_video_structure_openai_system")

        user_prompt = get_prompt_loader().get(
            "shared_video_structure_analysis_user",
            title=title if title else "[Not provided]",
            first_para=first_para[:500] if first_para else "[Not provided]",
            rest_content=rest_content[:500] if rest_content else "[Not provided]",
            free_text=free_text[:500] if free_text else "[Not provided]",
            manual_instructions=manual_instructions if manual_instructions else "[No manual instructions]",
            product_context=product_context if product_context else "**NO PRODUCT DETECTED**",
        )

        user_content = [{"type": "text", "text": user_prompt}] + image_contents

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        result = call_fn(messages, temperature=0.3, response_format={"type": "json_object"})
        text = result.get("text", "")
        if not text:
            logger.warning("[STRUCTURE] Analysis returned empty")
            return {"video_structure": "unknown", "scene_plan": []}

        parsed = json.loads(text.strip())

        logger.info(f"[STRUCTURE] Video type: {parsed.get('video_structure')}")
        logger.info(f"   Narrative: {parsed.get('narrative_summary', '')[:100]}...")
        scene_plan = parsed.get("scene_plan", [])
        logger.info(f"   Planned {len(scene_plan)} scenes")

        return parsed

    except Exception as e:
        logger.error(f"[STRUCTURE] Analysis error: {e}")
        return {"video_structure": "unknown", "scene_plan": [], "error": str(e)}


def analyze_video_style(
    call_fn: Callable,
    frame_paths: List[str],
    video_duration: float = 0,
) -> Dict[str, Any]:
    """Comprehensive video style analysis to match the original video's visual style.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        frame_paths: List of frame paths from across the video.
        video_duration: Total video duration in seconds.

    Returns:
        Dict with comprehensive style analysis.
    """
    try:
        logger.info("[STYLE] Analyzing video visual style for matching...")

        num_frames = min(8, len(frame_paths))
        if len(frame_paths) > num_frames:
            indices = [int(i * (len(frame_paths) - 1) / (num_frames - 1)) for i in range(num_frames)]
            sample_paths = [frame_paths[i] for i in indices]
        else:
            sample_paths = frame_paths

        image_contents = []
        for frame_path in sample_paths:
            try:
                with open(frame_path, "rb") as f:
                    img_data = base64.b64encode(f.read()).decode("utf-8")
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_data}", "detail": "low"},
                    })
            except Exception as e:
                logger.warning(f"Could not encode frame {frame_path}: {e}")

        if not image_contents:
            return {"error": "No frames to analyze"}

        system_prompt = get_prompt_loader().get("shared_video_style_analysis_system")
        user_prompt = get_prompt_loader().get(
            "shared_video_style_analysis_user",
            frame_count=len(image_contents),
        )

        user_content = [{"type": "text", "text": user_prompt}] + image_contents

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        result = call_fn(messages, temperature=0.3, response_format={"type": "json_object"})
        text = result.get("text", "")
        if not text:
            logger.warning("[STYLE] Analysis returned empty")
            return {}

        parsed = json.loads(text.strip())

        logger.info("[STYLE] Analysis complete:")
        logger.info(f"   Color temp: {parsed.get('color_palette', {}).get('color_temperature', 'unknown')}")
        logger.info(f"   Lighting: {parsed.get('lighting', {}).get('type', 'unknown')}")
        logger.info(f"   Composition: {parsed.get('composition', {}).get('primary_framing', 'unknown')}")
        logger.info(f"   Mood: {parsed.get('mood_atmosphere', {}).get('overall_mood', 'unknown')}")

        style_prefix = parsed.get("style_prompt_prefix", "")
        if style_prefix:
            logger.info(f"   Style prefix: {style_prefix[:80]}...")

        return parsed

    except Exception as e:
        logger.error(f"[STYLE] Analysis error: {e}")
        return {"error": str(e)}


def analyze_scene_frames(
    call_fn: Callable,
    frame_paths: List[str],
    manual_instructions: str = "",
) -> Dict[str, Any]:
    """Analyze scene frames and generate prompts using two separate LLM calls.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        frame_paths: List of paths to frame images (1 per second of scene).
        manual_instructions: Optional custom instructions from user.

    Returns:
        Dict containing analysis, first_prompt (image), second_prompt (motion).
    """
    try:
        logger.info(f"Analyzing {len(frame_paths)} frames (2 calls)...")

        # Encode images to base64
        image_contents = []
        for frame_path in frame_paths:
            with open(frame_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
                image_contents.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_data}",
                        "detail": "high",
                    },
                })

        # Call 1: Generate Image Prompt
        logger.info("Generating image prompt...")
        image_result = _generate_image_prompt(call_fn, image_contents, manual_instructions)

        # Call 2: Generate Motion Prompt
        logger.info("Generating motion prompt...")
        motion_result = _generate_motion_prompt(call_fn, image_contents, manual_instructions)

        result = {
            "analysis": image_result.get("analysis", ""),
            "text_content": image_result.get("text_content", {}),
            "first_prompt": image_result.get("first_prompt", ""),
            "second_prompt": motion_result.get("second_prompt", ""),
        }

        logger.info("Scene analysis complete (both prompts generated)")
        return result

    except Exception as e:
        logger.error(f"Error analyzing scene: {e}")
        return {
            "analysis": "Unable to analyze scene",
            "text_content": {"exact_text": "", "language": "", "position": "", "style": ""},
            "first_prompt": "",
            "second_prompt": "",
        }


def analyze_full_video(
    call_fn: Callable,
    frame_paths_with_timestamps: List[Tuple[float, str]],
    pyscenedetect_timestamps: List[float],
    video_duration: float,
    manual_instructions: str = "",
    cta_button: bool = False,
    cta_text: str = "",
    row_num: int = 0,
    article_text: str = "",
    vertical: str = "",
    article_language: str = "",
    article_related_to_video: bool = True,
) -> Dict[str, Any]:
    """Analyze entire video and generate scene timestamps + prompts in a single call.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        frame_paths_with_timestamps: List of (timestamp, frame_path) tuples.
        pyscenedetect_timestamps: Initial scene start times from PySceneDetect.
        video_duration: Total video duration in seconds.
        manual_instructions: Optional custom instructions from user.
        cta_button: Whether to include a CTA button in image prompts.
        cta_text: Text for the CTA button.
        row_num: Row number for logging purposes.
        article_text: Optional article content to adapt prompts to.
        vertical: Optional vertical/offer name for content adaptation.
        article_language: Optional language code for content adaptation.
        article_related_to_video: True if article is similar to video.

    Returns:
        Dict with corrected_scenes and scene_prompts.
    """
    row_prefix = f"[Row {row_num}] " if row_num > 0 else ""
    try:
        logger.info(f"{row_prefix}Analyzing full video (unified call)...")
        logger.info(f"   {row_prefix}Frames: {len(frame_paths_with_timestamps)}")
        logger.info(f"   {row_prefix}PySceneDetect scenes: {len(pyscenedetect_timestamps)}")
        logger.info(f"   {row_prefix}Video duration: {video_duration:.2f}s")

        # Encode images to base64 with timestamp labels
        image_contents = []
        for timestamp, frame_path in frame_paths_with_timestamps:
            try:
                with open(frame_path, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode("utf-8")
                    image_contents.append({"type": "text", "text": f"[Frame at {timestamp:.1f}s]"})
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}",
                            "detail": "high",
                        },
                    })
            except Exception as e:
                logger.warning(f"Could not read frame at {timestamp:.1f}s: {e}")

        if not image_contents:
            logger.error("No frames could be loaded")
            return _empty_video_analysis_result(pyscenedetect_timestamps, video_duration)

        # Format PySceneDetect timestamps for the prompt
        pyscene_info = "PySceneDetect detected scene changes at these timestamps:\n"
        for i, ts in enumerate(pyscenedetect_timestamps):
            if i + 1 < len(pyscenedetect_timestamps):
                end_ts = pyscenedetect_timestamps[i + 1]
            else:
                end_ts = video_duration
            duration = end_ts - ts
            pyscene_info += f"  Scene {i + 1}: {ts:.2f}s - {end_ts:.2f}s (duration: {duration:.2f}s)\n"

        # System prompt for unified video analysis
        system_prompt = get_prompt_loader().get(
            "shared_full_video_analysis_system",
            video_duration=video_duration,
            min_scene_duration=config.PYSCENEDETECT_MIN_SCENE_DURATION,
            max_scene_duration=config.PYSCENEDETECT_MAX_SCENE_DURATION,
            max_scenes=config.MAX_SCENES,
        )

        # Add article adaptation instructions if provided
        if article_text:
            article_summary = article_text[:2000]
            language_info = f"TARGET LANGUAGE: {article_language}" if article_language else ""
            cultural_instructions = get_cultural_adaptation_instructions(article_language)

            if article_related_to_video:
                article_section = get_prompt_loader().get(
                    "shared_article_adaptation_similar",
                    language_info=language_info,
                    article_summary=article_summary,
                    article_language=article_language,
                    cultural_instructions=cultural_instructions,
                )
            else:
                article_section = get_prompt_loader().get(
                    "shared_article_adaptation_different",
                    language_info=language_info,
                    article_summary=article_summary,
                    article_language=article_language,
                    cultural_instructions=cultural_instructions,
                )
            system_prompt = article_section + system_prompt

        # Add manual instructions if provided
        if manual_instructions:
            system_prompt = (
                f"**\U0001f6a8 USER INSTRUCTIONS (HIGHEST PRIORITY - MUST FOLLOW):**\n"
                f"{manual_instructions}\n\n"
                "Apply these instructions to ALL scene prompts consistently.\n\n"
                "---\n\n"
                f"{system_prompt}"
            )

        user_content = [
            {"type": "text", "text": pyscene_info},
            {"type": "text", "text": "\nHere are the video frames (1 per second):"},
        ] + image_contents

        logger.info(f"{row_prefix}Sending unified request...")

        start_time = _time.time()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        result = call_fn(messages, response_format={"type": "json_object"})

        elapsed = _time.time() - start_time
        logger.info(f"{row_prefix}LLM responded in {elapsed:.1f}s")

        text = result.get("text", "")
        if not text:
            logger.warning(f"{row_prefix}LLM returned empty content")
            return _empty_video_analysis_result(pyscenedetect_timestamps, video_duration)

        parsed = json.loads(text.strip())

        corrected_scenes = parsed.get("corrected_scenes", [])
        scene_prompts = parsed.get("scene_prompts", [])

        logger.info(f"{row_prefix}Analysis complete:")
        logger.info(f"   {row_prefix}Corrected scenes: {len(corrected_scenes)}")
        for scene in corrected_scenes:
            logger.info(f"     {row_prefix}Scene {scene.get('scene_num')}: {scene.get('start'):.2f}s - {scene.get('end'):.2f}s")
        logger.info(f"   {row_prefix}Prompts generated: {len(scene_prompts)}")

        return parsed

    except Exception as e:
        logger.error(f"{row_prefix}Error in unified video analysis: {e}")
        logger.error(f"   {row_prefix}Exception type: {type(e).__name__}")
        logger.error(f"   {row_prefix}Traceback:\n{traceback.format_exc()}")
        return _empty_video_analysis_result(pyscenedetect_timestamps, video_duration)


def _analyze_article_video_relevance(
    call_fn: Callable,
    article_text: str,
    video_description: str,
) -> Dict[str, Any]:
    """Analyze the relevance between article content and video content.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        article_text: The article content.
        video_description: Description of what is shown in the video.

    Returns:
        Dict with relevance_score, common_themes, blend_strategy.
    """
    try:
        if not article_text or not video_description:
            return {
                "relevance_score": 0.5,
                "common_themes": [],
                "blend_strategy": "video_priority",
                "blend_instructions": "Focus on video content, use article for general context only.",
            }

        messages = [
            {
                "role": "system",
                "content": get_prompt_loader().get("shared_article_relevance_system"),
            },
            {
                "role": "user",
                "content": (
                    f"ARTICLE CONTENT:\n{article_text[:1000]}\n\n"
                    f"VIDEO DESCRIPTION:\n{video_description[:500]}\n\n"
                    "Analyze the relevance and provide blending strategy."
                ),
            },
        ]
        result = call_fn(messages, temperature=0.3, max_tokens=300, response_format={"type": "json_object"})
        text = result.get("text", "")
        if not text:
            return {
                "relevance_score": 0.5,
                "common_themes": [],
                "blend_strategy": "partial_blend",
                "blend_instructions": "Try to find common ground between video and article content.",
            }

        parsed = json.loads(text)
        logger.info(
            f"Article-Video Relevance: {parsed.get('relevance_score', 0):.2f} - "
            f"Strategy: {parsed.get('blend_strategy', 'unknown')}"
        )
        return parsed

    except Exception as e:
        logger.warning(f"Could not analyze article-video relevance: {e}")
        return {
            "relevance_score": 0.5,
            "common_themes": [],
            "blend_strategy": "partial_blend",
            "blend_instructions": "Try to find common ground between video and article content.",
        }


# ---------------------------------------------------------------------------
# Asset video analysis (smart asset mode)
# ---------------------------------------------------------------------------

ASSET_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_index": {"type": "integer"},
        "duration_seconds": {"type": "number"},
        "content_summary": {"type": "string"},
        "key_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "description": {"type": "string"},
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "uniqueness": {"type": "string", "enum": ["high", "medium", "low"]},
                    "uniqueness_reason": {"type": "string"},
                    "motion_intensity": {"type": "string"},
                },
                "required": ["index", "description", "start_seconds", "end_seconds", "uniqueness", "uniqueness_reason", "motion_intensity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["asset_index", "duration_seconds", "content_summary", "key_moments"],
    "additionalProperties": False,
}


def _analyze_single_asset(
    vertex_provider,
    asset_url: str,
    asset_index: int,
    model: str = "gemini-2.5-flash",
    on_progress: Optional[Callable] = None,
    llm_logger=None,
) -> Optional[Dict[str, Any]]:
    """Analyze a single asset video via Vertex AI.

    Downloads the video, uploads to GCS, sends to Gemini with responseSchema,
    parses the result, and cleans up GCS. Returns enriched dict or None on failure.
    """
    try:
        logger.info(f"   Analyzing asset video {asset_index + 1}: {asset_url[:80]}...")

        # Download video to temp file
        import tempfile
        resp = requests.get(asset_url, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"   Asset {asset_index}: failed to download (HTTP {resp.status_code})")
            return None

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name

        try:
            # Upload to GCS
            video_gcs_url = vertex_provider._upload_video_to_gcs(tmp_path)
            if not video_gcs_url:
                logger.warning(f"   Asset {asset_index}: GCS upload failed")
                return None

            # Build gs:// URI
            if "storage.googleapis.com/" in video_gcs_url:
                gs_uri = "gs://" + video_gcs_url.split("storage.googleapis.com/", 1)[1]
            else:
                gs_uri = video_gcs_url

            # Load prompt template
            loader = get_prompt_loader()
            analysis_prompt = loader.get("shared_asset_analysis_system")

            # Build payload with responseSchema
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"fileData": {"mimeType": "video/mp4", "fileUri": gs_uri}},
                            {"text": analysis_prompt},
                        ],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.3,
                    "responseMimeType": "application/json",
                    "responseSchema": ASSET_ANALYSIS_SCHEMA,
                },
            }

            parsed = None
            _last_err = None
            for _attempt in range(3):
                result = vertex_provider.raw_generate_content(payload, model=model)
                try:
                    parsed = json.loads(result.get("text", "{}"))
                    break
                except json.JSONDecodeError as e:
                    _last_err = e
                    logger.warning(f"   Asset {asset_index}: JSON parse failed (attempt {_attempt+1}/3): {e}")
            if parsed is None:
                raise _last_err  # all retries failed

            if llm_logger:
                llm_logger.log("analyze_asset_video", "vertex", model, payload, result)

            # Enrich: set correct asset_index, clamp timestamps, compute duration_seconds per moment
            parsed["asset_index"] = asset_index
            parsed["url"] = asset_url
            asset_dur = parsed.get("duration_seconds", 0)
            for moment in parsed.get("key_moments", []):
                moment["start_seconds"] = max(moment.get("start_seconds", 0), 0.0)
                moment["end_seconds"] = min(moment.get("end_seconds", asset_dur), asset_dur)
                moment["duration_seconds"] = round(moment["end_seconds"] - moment["start_seconds"], 1)

            # Emit cost tracking
            if on_progress:
                on_progress("usage", {
                    "service": "gemini_text",
                    "step": "analyze_asset_video",
                    "model": model,
                    "provider": "vertex",
                    "category": "text",
                    "input_tokens": result.get("input_tokens", 0),
                    "output_tokens": result.get("output_tokens", 0),
                    "label": f"Analyze asset video {asset_index + 1}",
                })

            logger.info(f"   Asset {asset_index}: {parsed.get('content_summary', '')[:80]}...")
            return parsed

        finally:
            # Cleanup GCS
            try:
                vertex_provider._cleanup_gcs_video(video_gcs_url)
            except Exception:
                pass
            # Cleanup temp file
            try:
                import os as _os
                _os.unlink(tmp_path)
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"   Asset {asset_index} analysis failed, excluding from pool: {e}")
        return None


def analyze_asset_videos(
    vertex_provider,
    asset_urls: List[str],
    model: str = "gemini-2.5-flash",
    max_concurrent: int = 3,
    max_assets: int = 10,
    on_progress: Optional[Callable] = None,
    llm_logger=None,
) -> List[Dict[str, Any]]:
    """Analyze multiple asset videos in parallel via Vertex AI.

    Each video is analyzed separately (1 LLM call each) but all run concurrently
    via ThreadPoolExecutor. Failed analyses are dropped from the pool.

    Args:
        vertex_provider: VertexAIProvider instance with GCS access.
        asset_urls: List of video URLs to analyze.
        model: Gemini model to use.
        max_concurrent: Max parallel analysis threads.
        max_assets: Max asset videos to analyze (overflow truncated).
        on_progress: Optional callback for cost tracking.

    Returns:
        List of successfully analyzed asset descriptions (may be shorter than input).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not asset_urls:
        return []

    # Truncate to max
    if len(asset_urls) > max_assets:
        dropped = list(range(max_assets, len(asset_urls)))
        logger.warning(f"Capped at {max_assets}: dropped asset(s) {dropped}")
        asset_urls = asset_urls[:max_assets]

    workers = min(len(asset_urls), max_concurrent)
    results = [None] * len(asset_urls)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _analyze_single_asset, vertex_provider, url, idx, model, on_progress, llm_logger
            ): idx
            for idx, url in enumerate(asset_urls)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                if result is not None:
                    results[idx] = result
            except Exception as e:
                logger.warning(f"Asset {idx} analysis raised exception: {e}")

    # Filter out None (failed analyses), preserve order
    return [r for r in results if r is not None]
