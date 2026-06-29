"""Prompt-parsing and scene-generation task functions.

Extracts structured briefs (TEXT 1-4), influencer scene prompts, and product
scene prompts from free-form user input via an LLM.

Every public function takes ``call_fn`` as its first argument (see package
docstring in ``__init__.py`` for the protocol).
"""

import base64
import json
import logging
from typing import Any, Dict, List, Optional

import requests

from tvd_pipeline.config import get_pipeline_defaults
from tvd_pipeline.data_loader import get_language_name, get_style_prompts
from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.services.tasks._helpers import (
    extract_json_from_response,
    fix_truncated_scene_json,
    get_empty_prompt_parse_result,
    get_empty_scene_result,
    get_scene_count_for_duration,
    get_scene_duration_range,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response schemas for structured output
# ---------------------------------------------------------------------------

PARSE_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "text_1": {"type": "string"},
        "text_2": {"type": "string"},
        "text_3": {"type": "string"},
        "text_4": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene": {"type": "integer"},
                    "purpose": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["scene", "purpose", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["text_1", "text_2", "text_3", "text_4"],
    "additionalProperties": False,
}

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "missing": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["missing"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Convergence validation helper
# ---------------------------------------------------------------------------

def _validate_parse_completeness(
    call_fn,
    original_prompt: str,
    parsed: Dict[str, Any],
    language: str,
) -> List[str]:
    """Check if the parsed output captures all details from the original prompt.

    Returns a list of missing details (empty list = converged).
    """
    try:
        loader = get_prompt_loader()
        validation_prompt = loader.get(
            "shared_parse_prompt_validation_system",
            original_prompt=original_prompt,
            parsed_output=json.dumps(parsed, ensure_ascii=False, indent=2),
        )
        messages = [
            {"role": "user", "content": validation_prompt},
        ]
        result = call_fn(messages, temperature=0.3, max_tokens=2000, responseSchema=VALIDATION_SCHEMA)
        content = result.get("text", "")
        data = json.loads(content)
        return data.get("missing", [])
    except Exception as e:
        logger.warning(f"Parse prompt validation failed: {e}")
        return []  # On error, assume converged to avoid blocking


# ---------------------------------------------------------------------------
# 1. parse_product_prompt
# ---------------------------------------------------------------------------

def parse_product_prompt(
    call_fn,
    prompt: str,
    image_urls: List[str] = None,
    image_descriptions: List[Dict[str, Any]] = None,
    asset_descriptions: List[Dict[str, Any]] = None,
    video_type_context: str = "product",
    language: str = "en",
    on_progress=None,
) -> Dict[str, str]:
    """Parse a product/service description into 4 structured outputs.

    Breaks a free-form description down into:
    - TEXT 1: What is the video about (topic/description)
    - TEXT 2: What is the goal of the video (purpose/objective)
    - TEXT 3: Content and style requirements (tone, visual style, what to avoid)
    - TEXT 4: Video structure (scene-by-scene breakdown as structured JSON array)

    Uses ``responseSchema`` for consistent JSON output, explicit language
    instruction, and a convergence loop that validates completeness and
    retries with corrective feedback when details are missing.

    Args:
        call_fn: LLM call function ``(messages, **kw) -> dict``.
        prompt: Free-form description from user.
        image_urls: Optional list of reference image URLs.
        image_descriptions: Optional pre-analyzed image descriptions (smart asset
            mode). When provided, text descriptions are used instead of fetching
            and base64-encoding images.  Each dict should have ``index`` (int)
            and ``description`` (str).
        asset_descriptions: Optional pre-analyzed asset video descriptions (smart
            asset mode). Each dict should have ``asset_index``, ``content_summary``,
            ``duration_seconds``, and ``key_moments`` list.  Always additive text.
        video_type_context: For logging only -- ``"product"`` or ``"UGC"``.
        language: ISO 639-1 language code (e.g. ``"he"``, ``"en"``).
        on_progress: Optional callback for warnings (``on_progress("warning", {...})``).

    Returns:
        Dict with keys ``text_1``, ``text_2``, ``text_3``, ``text_4``.
    """
    language_name = get_language_name(language)
    logger.info(f"Parsing {video_type_context} prompt (language={language_name})...")

    # -- system prompt (loaded from template) --------------------------------
    loader = get_prompt_loader()
    system_prompt = loader.get(
        "shared_parse_prompt_system",
        language_name=language_name,
    )

    # -- build OpenAI-format user content ------------------------------------
    user_content: List[Dict[str, Any]] = []

    # Smart path: use pre-analyzed descriptions as text (no base64, no fetch)
    if image_descriptions:
        desc_text = "Reference images available for this video:\n"
        for desc in image_descriptions:
            _img_desc = desc.get("description_variant_regular") or desc.get("description") or f"Reference image {desc.get('index', '?')}"
            desc_text += f"- Image {desc['index']}: {_img_desc}\n"
        user_content.append({"type": "text", "text": desc_text})
        logger.info(f"   Including {len(image_descriptions)} pre-analyzed image descriptions (smart path)")
    elif image_urls:
        # Legacy path: fetch and base64-encode images
        valid_images = [u for u in image_urls if u and u.strip()]
        for url in valid_images:
            url = url.strip()
            if url.startswith("gs://"):
                user_content.append(
                    {"type": "image_url", "image_url": {"url": url}}
                )
            elif url.startswith("data:image"):
                user_content.append({"type": "image_url", "image_url": {"url": url}})
            else:
                # External URL: fetch and base64-encode
                try:
                    fetch_headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                    resp = requests.get(url, headers=fetch_headers, timeout=30)
                    resp.raise_for_status()
                    ct = resp.headers.get("Content-Type", "").lower()
                    mime = (
                        "image/png" if "png" in ct or url.lower().endswith(".png")
                        else "image/webp" if "webp" in ct
                        else "image/jpeg"
                    )
                    b64 = base64.b64encode(resp.content).decode("utf-8")
                    user_content.append(
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                    )
                except Exception as e:
                    logger.warning(f"   Could not fetch image for parse prompt {url[:50]}...: {e}, skipping")
        if valid_images:
            logger.info(f"   Including {len(valid_images)} reference images")

    # Asset video descriptions (always additive text when provided)
    if asset_descriptions:
        asset_text = "Video assets available for this video:\n"
        for desc in asset_descriptions:
            moments_str = ", ".join(
                f"[{m['index']}] {m['description']} ({m.get('duration_seconds', 0)}s)"
                for m in desc.get("key_moments", [])
            )
            asset_text += f"- Video {desc['asset_index']}: {desc['content_summary']} ({desc['duration_seconds']}s). Key moments: {moments_str}\n"
        user_content.append({"type": "text", "text": asset_text})
        logger.info(f"   Including {len(asset_descriptions)} pre-analyzed asset video descriptions")

    user_text = loader.get("shared_parse_prompt_user", prompt=prompt)
    user_content.append({"type": "text", "text": user_text})

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        llm_result = call_fn(messages, temperature=0.7, max_tokens=4000, responseSchema=PARSE_PROMPT_SCHEMA)
        content = llm_result.get("text", "")

        # With responseSchema the LLM returns valid JSON directly
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            # Fallback: try extract_json_from_response for non-schema providers
            parsed = extract_json_from_response(content)

        if not parsed:
            logger.warning("Could not parse JSON from response")
            logger.warning(f"   Raw response (first 1000 chars): {content[:1000]}")
            return get_empty_prompt_parse_result()

        logger.info("Successfully parsed product prompt into 4 sections")

        # -- Convergence loop: validate completeness and retry ---------------
        _p_defaults = get_pipeline_defaults()
        max_retries = _p_defaults.get("parse_prompt_max_retries", 3)
        missing = []

        for attempt in range(max_retries):
            missing = _validate_parse_completeness(call_fn, prompt, parsed, language)
            if not missing:
                logger.info(f"Parse prompt converged after {attempt + 1} validation(s)")
                break
            logger.warning(
                f"Parse prompt attempt {attempt + 1}/{max_retries}: "
                f"missing {len(missing)} details: {missing}"
            )
            # Build corrective retry
            missing_list = "\n".join(f"- {item}" for item in missing)
            retry_user = loader.get(
                "shared_parse_prompt_retry_user",
                original_prompt=prompt,
                missing_list=missing_list,
            )
            retry_messages = messages + [
                {"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)},
                {"role": "user", "content": retry_user},
            ]
            llm_result = call_fn(
                retry_messages, temperature=0.7, max_tokens=4000,
                responseSchema=PARSE_PROMPT_SCHEMA,
            )
            retry_content = llm_result.get("text", "")
            try:
                parsed = json.loads(retry_content)
            except (json.JSONDecodeError, TypeError):
                parsed_fallback = extract_json_from_response(retry_content)
                if parsed_fallback:
                    parsed = parsed_fallback
        else:
            if missing:
                logger.warning(
                    f"Parse prompt did not fully converge after {max_retries} retries. "
                    f"Still missing: {missing}"
                )
                if on_progress:
                    on_progress("warning", {
                        "message": f"Parse prompt did not fully converge after {max_retries} retries. "
                                   f"Still missing: {', '.join(missing)}. Proceeding with best result."
                    })

        return {
            "text_1": parsed.get("text_1", ""),
            "text_2": parsed.get("text_2", ""),
            "text_3": parsed.get("text_3", ""),
            "text_4": parsed.get("text_4", ""),
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Error calling LLM API: {e}")
        return get_empty_prompt_parse_result()
    except Exception as e:
        logger.error(f"Error parsing product prompt: {e}")
        return get_empty_prompt_parse_result()


# ---------------------------------------------------------------------------
# Venue DNA helper
# ---------------------------------------------------------------------------

def _build_venue_dna_block(venue_dna: str) -> str:
    """Format the venue DNA string into an injection block for LLM prompts.

    Returns an empty string when no DNA is available, so template variables
    resolve cleanly without adding blank lines.
    """
    if not venue_dna or not venue_dna.strip():
        return ""
    return (
        "\n═══ VENUE DNA — USE IN EVERY INDOOR SCENE ═══\n"
        f"{venue_dna.strip()}\n"
        "Every scene that takes place INSIDE this venue MUST include 1–2 of these specific "
        "visual details verbatim (surface colors, furniture, lighting, signature decor) to "
        "guarantee all indoor scenes look like the SAME location.\n"
        "═══════════════════════════════════════════════\n"
    )


# ---------------------------------------------------------------------------
# 2. generate_influencer_prompts
# ---------------------------------------------------------------------------

def generate_influencer_prompts(
    call_fn,
    free_text: str,
    reference_images: List[Dict[str, Any]],
    scene_count: int,
    manual_instructions: str = "",
    cta_text: str = "",
    language: str = "en",
    existing_influencer_description: str = "",
    vo_timing: Dict[str, Any] = None,
    visual_style: str = "Auto",
    video_subtype: str = "influencer",
    asset_descriptions: List[Dict[str, Any]] = None,
    venue_dna: str = "",
) -> Dict[str, Any]:
    """Generate influencer or personal-brand video prompts for each scene.

    Creates prompts for an influencer recommendation video where:
    - Scene 1: Influencer with strong hook
    - Scene 4, 7, 10...: Influencer appears again (identical appearance)
    - Last scene: Influencer with CTA
    - Other scenes: Product/experience with cycling reference images

    When *vo_timing* is provided, scene prompts are aligned to the VO text so
    that what the viewer SEES matches what they HEAR at every moment.

    Args:
        call_fn: LLM call function ``(messages, **kw) -> dict``.
        free_text: Content describing the product/experience to promote.
        reference_images: List of dicts with ``url`` and optional ``base64``/``analysis``.
        scene_count: Number of scenes to generate.
        manual_instructions: Optional custom instructions.
        cta_text: Call-to-action text for the last scene.
        language: ISO 639-1 language code.
        existing_influencer_description: If provided, use this instead of generating one.
        vo_timing: Optional dict with pre-split VO scene segments.
        visual_style: Visual style name (e.g. ``"Auto"``, ``"Modern flat 2d"``).
        video_subtype: ``"influencer"`` or ``"personal_brand"``.

    Returns:
        Dict with ``influencer_description``, ``scene_prompts`` list.
    """
    try:
        mode_label = "personal brand" if video_subtype == "personal_brand" else "influencer"
        logger.info(f"Generating {mode_label} prompts for {scene_count} scenes (via Gemini)...")

        language_name = get_language_name(language)

        # Style-specific prompt prefix and forbidden words for scene generation.
        _style_scene_config = get_style_prompts("scene_config")

        style_key = visual_style if visual_style in _style_scene_config else "Auto"
        style_cfg = _style_scene_config[style_key]
        style_prompt_prefix = style_cfg["prefix"]
        style_forbidden_csv = ", ".join(f'"{w}"' for w in style_cfg["forbidden"])
        style_instruction = style_cfg["instruction"]

        # Build reference image descriptions
        ref_image_descriptions = []
        for i, img in enumerate(reference_images):
            if img.get("analysis"):
                ref_image_descriptions.append(f"Reference Image {i+1}: {img['analysis'][:500]}")
            else:
                ref_image_descriptions.append(
                    f"Reference Image {i+1}: [Content not analyzed -- use for scenes that "
                    f"match the video topic when this image can support the story]"
                )

        ref_images_text = "\n".join(ref_image_descriptions) if ref_image_descriptions else "No reference images provided."
        ref_image_count = len(reference_images)

        # Build the system prompt (loaded from template)
        system_prompt = get_prompt_loader().get(
            "ugc_scene_gen_system",
            language_name=language_name,
            style_key=style_key,
            style_instruction=style_instruction,
            style_prompt_prefix=style_prompt_prefix,
            style_forbidden_csv=style_forbidden_csv,
        )

        # Manual instructions addition
        manual_section = ""
        if manual_instructions:
            manual_section = f"\n\nADDITIONAL INSTRUCTIONS FROM USER:\n{manual_instructions}"

        # CTA section
        cta_section = ""
        if cta_text:
            cta_section = f"\n\nCTA TEXT (for conceptual reference in last scene): {cta_text}"

        # Existing influencer description section
        influencer_section = ""
        if existing_influencer_description:
            multi_note = ""
            if "Person 2:" in existing_influencer_description or "Person 2 :" in existing_influencer_description:
                multi_note = (
                    "\nWhen multiple persons are described (Person 1, Person 2, ...), "
                    "include ALL of them in scenes where shows_influencer is true; "
                    "describe each in the scene prompt."
                )
            influencer_section = f"""

IMPORTANT - USE THIS EXACT INFLUENCER DESCRIPTION:
The influencer(s) have been pre-defined. You MUST use this exact description in all influencer scenes:
"{existing_influencer_description}"
{multi_note}

Do NOT create a new influencer appearance. Copy this description exactly into the "influencer_description" field and use it in all scene prompts where shows_influencer is true."""

        # Build VO timing block for visual-audio coherence
        vo_timing_block = ""
        if vo_timing and vo_timing.get("scene_segments"):
            scene_segments = vo_timing["scene_segments"]
            vo_total = vo_timing.get("total_duration", 0)
            scene_vo_lines = []
            for seg in scene_segments:
                scene_vo_lines.append(
                    f"  SCENE {seg['scene_num']} VO ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s, "
                    f"~{seg['duration']:.1f}s):\n"
                    f"    \"{seg['text']}\""
                )
            scene_vo_str = "\n\n".join(scene_vo_lines)
            vo_timing_block = f"""

=== VOICE-OVER AUDIO (ALREADY RECORDED - PRECISE TIMING REQUIRED) ===
A VO audio track ({vo_total:.1f}s) has ALREADY been recorded. Your output MUST have exactly {len(scene_segments)} scenes.
Each scene has EXACT timestamps. The image for scene N must depict ONLY what is said in that scene's time window--no other content.

PER-SCENE VO TEXT WITH EXACT TIMESTAMPS (match first_prompt 1:1 to each block):
{scene_vo_str}

ABSOLUTE RULE -- WHAT YOU SEE = WHAT YOU HEAR:
For each scene number above, your first_prompt MUST depict EXACTLY what the VO says in that scene's time window:
- If the VO says "families struggling to find a home" -> the image shows a family in a small apartment. NOT the influencer.
- If the VO says "I decided to help them" -> the image shows the influencer in a helping/advisory moment.
- If the VO says "they found their dream home" -> the image shows a happy family in a beautiful new home. NOT the influencer.
- If the VO says "call me today" -> the image shows the influencer with a warm CTA gesture.

ALSO: shows_influencer must match the VO content. If the VO talks about OTHER people -> shows_influencer = false (show those people/situation). If the VO talks about THE INFLUENCER -> shows_influencer = true.

Do NOT use generic beauty shots that ignore the VO. The viewer must SEE what they HEAR at EVERY SECOND. This is the #1 priority.
"""
        elif vo_timing and vo_timing.get("full_text"):
            vo_timing_block = f"""

=== VOICE-OVER AUDIO (ALREADY RECORDED - YOUR SCENES MUST MATCH IT) ===
A VO audio track ({vo_timing.get('total_duration', 0):.1f}s, {vo_timing.get('word_count', 0)} words) has ALREADY been recorded.

VO TRANSCRIPT:
\"{vo_timing['full_text']}\"

CRITICAL: Split this VO text across your {scene_count} scenes. Each scene's first_prompt MUST visually
illustrate what the VO says during that scene. The viewer must SEE what they HEAR.
"""

        extra_image_label = ", Image 5" if ref_image_count >= 5 else ""
        last_body_scene = scene_count - 1

        # Build asset matching context (timestamps + durations for scene matching)
        asset_matching_context = "No video assets provided."
        if asset_descriptions:
            _amc_lines = []
            for vi, ad in enumerate(asset_descriptions):
                moments_str = ", ".join(
                    f"[{m['index']}] {m['description']} ({m['start_seconds']:.1f}-{m['end_seconds']:.1f}s, {m.get('duration_seconds', m['end_seconds'] - m['start_seconds']):.1f}s)"
                    for m in ad.get("key_moments", [])
                )
                _amc_lines.append(f"- Video {vi}: {ad['content_summary']} ({ad['duration_seconds']:.1f}s). Key moments: {moments_str}")
            asset_matching_context = "You have the following video assets available. For each scene, decide whether to use a video asset (set video_asset_index + best_moment_index) or generate AI content (set both to null).\n" + "\n".join(_amc_lines)

        user_prompt = get_prompt_loader().get(
            "ugc_influencer_prompts_user",
            scene_count=scene_count,
            influencer_section=influencer_section,
            free_text=free_text[:3000],
            vo_timing_block=vo_timing_block,
            manual_section=manual_section,
            cta_section=cta_section,
            ref_image_count=ref_image_count,
            extra_image_label=extra_image_label,
            last_body_scene=last_body_scene,
            language_name=language_name,
            style_prompt_prefix=style_prompt_prefix,
            style_key=style_key,
            style_forbidden_csv=style_forbidden_csv,
            ref_images_text=ref_images_text,
            asset_matching_context=asset_matching_context,
            venue_dna=_build_venue_dna_block(venue_dna),
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        llm_result = call_fn(messages, temperature=0.7, max_tokens=16000)
        result_text = (llm_result.get("text") or "").strip()
        if not result_text:
            logger.error("call_fn returned no text for influencer prompts")
            return {"influencer_description": "", "scene_prompts": []}

        # Strip markdown code fences if present
        if result_text.startswith("```"):
            result_text = result_text.split("\n", 1)[1] if "\n" in result_text else result_text[3:]
        if result_text.rstrip().endswith("```"):
            result_text = result_text.rstrip()[:-3].rstrip()

        # Try to parse JSON, with fallback for truncated responses
        try:
            result = json.loads(result_text)
        except json.JSONDecodeError as je:
            logger.warning(f"JSON parse error, attempting to fix truncated response: {je}")
            result = fix_truncated_scene_json(result_text)
            if not result:
                logger.error(f"Could not fix JSON response. Raw text (first 500 chars): {result_text[:500]}")
                return {"influencer_description": "", "scene_prompts": []}

        # Validate and enhance prompts with influencer description
        influencer_desc = (
            existing_influencer_description
            if existing_influencer_description
            else result.get("influencer_description", "")
        )
        scene_prompts = result.get("scene_prompts", [])

        # Ensure influencer description is embedded in all influencer scenes
        for scene_prompt in scene_prompts:
            if scene_prompt.get("shows_influencer", False) and influencer_desc:
                original_prompt = scene_prompt.get("first_prompt", "")
                scene_prompt["first_prompt"] = f"INFLUENCER: {influencer_desc}. SCENE: {original_prompt}"

        logger.info(f"Generated {len(scene_prompts)} influencer scene prompts (via Gemini)")
        return {
            "influencer_description": influencer_desc,
            "scene_prompts": scene_prompts,
        }

    except Exception as e:
        logger.error(f"Failed to generate influencer prompts (Gemini): {e}")
        return {"influencer_description": "", "scene_prompts": []}


# ---------------------------------------------------------------------------
# 3. generate_product_video_scenes
# ---------------------------------------------------------------------------

def generate_product_video_scenes(
    call_fn,
    text_1: str,
    text_2: str,
    text_3: str,
    text_4: str,
    prompt: str = "",
    image_urls: List[str] = None,
    target_duration: int = 30,
    character_description: str = None,
    character_urls: List[str] = None,
    logo_url: str = None,
    slogan_text: str = None,
    reference_video_structure: Dict[str, Any] = None,
    language: str = "en",
    country: str = "",
    vo_timing: Dict[str, Any] = None,
    no_on_screen_character: bool = False,
) -> Dict[str, Any]:
    """Generate detailed scene-by-scene prompts for product video creation.

    Uses TEXT 1-4 (strongly) and the original *prompt* (weakly) to create
    detailed image and motion prompts for each scene.

    Args:
        call_fn: LLM call function ``(messages, **kw) -> dict``.
        text_1: What the video is about (topic/description).
        text_2: What is the goal of the video (purpose/objective).
        text_3: Content and style requirements (tone, visual style).
        text_4: Video structure (scene-by-scene breakdown).
        prompt: Original product prompt (used weakly for context).
        image_urls: Product reference image URLs.
        target_duration: Target video duration in seconds.
        character_description: Optional character description text.
        character_urls: Optional list of character reference image URLs.
        logo_url: Optional logo URL for CTA scene.
        slogan_text: Optional slogan for CTA scene.
        reference_video_structure: Optional reference video analysis result.
        language: ISO 639-1 language code.
        country: Target country for locale-specific visuals.
        vo_timing: Optional dict with pre-generated VO timing data.
        no_on_screen_character: When True (product video), forbid recurring on-screen person/spokesperson.

    Returns:
        Dict with ``scenes`` list, ``total_duration``, and ``music_style``.
    """
    logger.info("Generating product video scene prompts with Gemini...")

    # Build the system prompt for scene generation (loaded from template)
    system_prompt = get_prompt_loader().get("product_scene_gen_system")

    # Vertex AI generateContent expects gs:// URIs; convert public GCS URLs
    def to_gs_uri(url: str) -> str:
        u = url.strip()
        if u.startswith("gs://"):
            return u
        if u.startswith("https://storage.googleapis.com/"):
            return "gs://" + u.replace("https://storage.googleapis.com/", "")
        return u

    def _gcs_object_exists(url: str) -> bool:
        """HEAD-check a GCS public URL to verify the object exists."""
        https_url = url.strip()
        if https_url.startswith("gs://"):
            https_url = "https://storage.googleapis.com/" + https_url[len("gs://"):]
        if not https_url.startswith("https://storage.googleapis.com/"):
            return True  # not GCS, assume ok
        try:
            r = requests.head(https_url, timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    # -- build user content parts (OpenAI-format for call_fn) ----------------
    user_content: List[Dict[str, Any]] = []

    # Add product images if provided
    if image_urls:
        valid_images = [u for u in image_urls if u and u.strip()]
        included = 0
        for url in valid_images:
            uri = to_gs_uri(url)
            if uri.startswith("gs://") and not _gcs_object_exists(uri):
                logger.warning(f"   GCS object not found for scene prompts: {url}, skipping")
                continue
            if uri.startswith("gs://"):
                user_content.append(
                    {"fileData": {"mimeType": "image/jpeg", "fileUri": uri}}
                )
            else:
                # Non-GCS URL: keep as fileData (call_fn handles passthrough)
                user_content.append(
                    {"fileData": {"mimeType": "image/jpeg", "fileUri": uri}}
                )
            included += 1
        if included:
            logger.info(f"   Including {included} product reference images")

    # Add character image(s) if provided (skipped when no_on_screen_character — URLs should already be empty)
    for character_url in (character_urls or []):
        char_uri = to_gs_uri(character_url)
        if char_uri.startswith("gs://"):
            user_content.append(
                {"fileData": {"mimeType": "image/jpeg", "fileUri": char_uri}}
            )
        else:
            try:
                fetch_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "image/*,*/*;q=0.8",
                }
                img_resp = requests.get(character_url.strip(), headers=fetch_headers, timeout=30)
                img_resp.raise_for_status()
                ct = img_resp.headers.get("Content-Type", "").lower()
                mime = "image/png" if "png" in ct else "image/webp" if "webp" in ct else "image/jpeg"
                b64 = base64.b64encode(img_resp.content).decode("utf-8")
                user_content.append({"inlineData": {"mimeType": mime, "data": b64}})
            except Exception as fetch_err:
                logger.warning(f"   Could not fetch character image for scene prompts: {fetch_err}, skipping character image")
    if character_urls:
        logger.info(f"   Including {len(character_urls)} character reference image(s) for scene generation")

    # -- build the text prompt -----------------------------------------------
    text_prompt = f"""Generate a detailed scene-by-scene breakdown for a product video.

=== VIDEO BRIEF (USE STRONGLY) ===

TEXT 1 - WHAT THE VIDEO IS ABOUT:
{text_1}

TEXT 2 - VIDEO GOAL:
{text_2}

TEXT 3 - CONTENT AND STYLE REQUIREMENTS:
{text_3}

TEXT 4 - VIDEO STRUCTURE:
{text_4}

=== ADDITIONAL CONTEXT (USE WEAKLY) ===
Original product description:
{prompt[:500] if prompt else "Not provided"}

=== CHARACTER INFORMATION ===
{'''NO ON-SCREEN PERSON OR SPOKESPERSON: Product-focused ad only. Do NOT introduce a recurring character, influencer, or identifiable presenter. Prefer product hero shots, packshots, environments without recognizable faces, anonymous hands-only usage, or abstract/motion graphics. Set has_character to false for every scene unless TEXT 1–3 explicitly require a generic non-identifying human element (e.g. hands holding the product). The VO is disembodied narration — visuals must not show someone "delivering" the pitch as a character.''' if no_on_screen_character else (f"CHARACTER(S) PROVIDED - Reference image(s) of the character(s) are attached. Include in relevant scenes:" if (character_description or character_urls) else "No character provided - skip has_character field or set to false")}
{"" if no_on_screen_character else (character_description if character_description else "")}
{"" if no_on_screen_character else (f"When has_character=true, reference 'the character(s) from the reference image(s)' in the image_prompt. The reference image(s) are attached." if (character_description or character_urls) else "")}

=== LOGO/BRANDING/SLOGAN ===
{f"Logo provided for ending scene - design CTA scene with clean space for logo overlay" if logo_url else "No logo provided - create generic CTA ending scene"}
{f"SLOGAN PROVIDED: '{slogan_text}' - Include this slogan ONLY in the ending/CTA scene image_prompt, as one short phrase in the TARGET LANGUAGE (correct script). No text in other scenes." if slogan_text else "No slogan provided - Optionally one short phrase (3-6 words) in the TARGET LANGUAGE in the CTA scene only; no text in body scenes."}

=== TARGET COUNTRY & LANGUAGE ===
{f"TARGET COUNTRY: {country}. People, characters, environments, and settings MUST look authentic to {country} (ethnicity, architecture, landscape, indoor style, climate, clothing). Make the visuals feel like they were shot in {country}." if country else "No specific country - use generic/neutral settings."}
{f"TARGET LANGUAGE: {language}. Write ALL vo_text in this language. The VO script must be entirely in this language, NOT in English." if language and language != "en" else "Language: English (default)."}
"""

    # Reference video structure block
    ref_block = ""
    ref_has_content = False
    if (
        reference_video_structure
        and isinstance(reference_video_structure.get("scenes"), list)
        and reference_video_structure["scenes"]
    ):
        ref_scenes = reference_video_structure["scenes"]
        ref_has_content = any(s.get("content_summary") or s.get("vo_snippet") for s in ref_scenes)
        lines = [
            "REFERENCE VIDEO (MANDATORY - IGNORE TEXT 4 for structure and flow):",
            "Your output MUST have exactly the same number of scenes, in the same order. "
            "For each scene: same narrative_role and approximate duration.",
            "ADAPT each reference scene to the NEW product: same story beat, same type of "
            "message and shot, same pacing. Only the product/topic changes. Do NOT create a "
            "different storyline.",
            "",
        ]
        for i, s in enumerate(ref_scenes, 1):
            role = s.get("narrative_role", "transition")
            dur = s.get("duration_seconds", s.get("duration", 3))
            line = f"Scene {i}: {role}, {dur}s"
            if s.get("content_summary"):
                line += f" | Content: {s.get('content_summary', '')}"
            if s.get("vo_snippet"):
                line += f' | VO: "{s.get("vo_snippet", "")}"'
            lines.append(line)
        lines.append("")
        if ref_has_content:
            lines.append(
                "For each scene, generate image_prompt AND vo_text that ADAPT the reference "
                "content and VO above to the new product (same beat and tone, new product)."
            )
        ref_block = "\n".join(lines) + "\n\n"

    if reference_video_structure and reference_video_structure.get("scenes"):
        scene_count_requirement = str(len(reference_video_structure["scenes"]))
    else:
        scene_count_requirement = get_scene_count_for_duration(target_duration)

    # Build VO timing block if VO was generated first
    vo_timing_block = ""
    if vo_timing and vo_timing.get("segments") and vo_timing.get("total_duration", 0) > 0:
        vo_total = vo_timing["total_duration"]
        vo_text_full = vo_timing.get("full_text", "")
        vo_segs = vo_timing["segments"]
        num_words = len(vo_segs)
        scene_segments = vo_timing.get("scene_segments", [])

        if scene_segments:
            # FULL per-scene VO text with exact timestamps (from pre-splitting)
            scene_vo_lines = []
            for seg in scene_segments:
                scene_vo_lines.append(
                    f"  SCENE {seg['scene_num']} VO ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s, "
                    f"~{seg['duration']:.1f}s, words [{seg['word_start_idx']}-{seg['word_end_idx']}]):\n"
                    f'    "{seg["text"]}"'
                )
            scene_vo_str = "\n\n".join(scene_vo_lines)

            vo_timing_block = f"""
=== VOICE-OVER AUDIO (ALREADY GENERATED - YOUR SCENES MUST MATCH IT) ===
A VO audio track ({vo_total:.1f}s, {num_words} words) has ALREADY been recorded.
The VO has been pre-split into {len(scene_segments)} scene segments below.
Your output MUST have exactly {len(scene_segments)} scenes, one for each VO segment.

=== PER-SCENE VO TEXT WITH EXACT TIMESTAMPS ===
{scene_vo_str}

=== CRITICAL VISUAL-AUDIO MATCHING RULES ===
1. Each scene's image_prompt MUST directly illustrate what the VO SAYS during that scene.
   - If the VO says "struggling with back pain" -> image shows a person with back pain
   - If the VO says "this product changed everything" -> image shows the product in use
   - If the VO says "imagine waking up refreshed" -> image shows a person waking up happy
2. Use the pre-split word ranges as "vo_word_start" and "vo_word_end" for each scene.
3. Scene durations are automatically calculated from timestamps. Set approximate durations based on the timestamps above.
4. Do NOT write new vo_text - the VO is already recorded.
5. The viewer must FEEL that the visuals and audio tell the same story at the same moment.

"""
        else:
            # Fallback: full word list with timestamps (no pre-splitting available)
            word_list_lines = []
            for i in range(0, num_words, max(1, num_words // 30)):
                ws = vo_segs[i]
                word_list_lines.append(f'  [{i}] {ws["start_time"]:.1f}s "{ws["text"]}"')
            word_list_lines.append(
                f'  [{num_words - 1}] {vo_segs[-1]["end_time"]:.1f}s "{vo_segs[-1]["text"]}" (last word)'
            )
            word_list_str = "\n".join(word_list_lines)

            vo_timing_block = f"""
=== VOICE-OVER AUDIO (ALREADY GENERATED - YOUR SCENES MUST MATCH IT) ===
A VO audio track ({vo_total:.1f}s, {num_words} words) has ALREADY been recorded. Your scenes MUST be timed to match.

VO TRANSCRIPT:
\"{vo_text_full}\"

WORD INDEX LANDMARKS (word_index -> timestamp):
{word_list_str}

=== CRITICAL VISUAL-AUDIO MATCHING RULES ===
1. Split the transcript into your scenes so each scene's visuals MATCH what is being SAID.
   - If the VO says "struggling with back pain" -> image shows a person with back pain
   - If the VO says "this product changed everything" -> image shows the product in use
2. For each scene, set "vo_word_start" and "vo_word_end" (0-indexed word positions).
   - All {num_words} words must be covered. No gaps, no overlaps.
3. Scene durations are calculated automatically from word timestamps.
4. The image_prompt must visually depict what the VO describes in that word range.
5. Do NOT write new vo_text - the VO is already recorded.

"""

    text_prompt += ref_block + vo_timing_block + f"""
=== REQUIREMENTS ===
- Target duration: {target_duration} seconds{f" (VO already recorded: {vo_timing['total_duration']:.1f}s - the video MUST be at least this long to fit the VO)" if vo_timing and vo_timing.get('total_duration') else ""}
- Generate {scene_count_requirement} scenes that tell a compelling product story with real depth and interest (emotional arc, concrete moments, relatable hook, varied pacing--see STORY DEPTH AND INTEREST in instructions).
- Each scene should be {get_scene_duration_range(target_duration)} seconds (CRITICAL: keep each scene between 3-8 seconds so animation clips can cover it. NEVER make a single scene longer than 10 seconds.)
- Each image_prompt = ONE image only: describe a single moment/frame, never two shots or a transition ("then", "transitions to") in one prompt.
- Include a strong, relatable hook in the first scene (a moment the viewer recognizes).
- End with a clear, satisfying call-to-action after a visible emotional or practical payoff.
- Ensure scenes flow naturally and build interest; avoid flat or repetitive tone.
- Reference the product images provided for accurate visual details.
- vo_text: Write like spoken copy--conversational, varied rhythm, one memorable line when possible; not a bullet list.{" (NOTE: VO is already recorded - vo_text is for reference only)" if vo_timing else ""}
- IMPORTANT: The total duration of all scenes MUST sum to approximately {target_duration} seconds. If a VO is provided, the total MUST equal the VO duration + 1-2s buffer.

=== IMPORTANT: PRODUCT VISIBILITY ===
Use SMART narrative logic for product visibility:
- If scene shows a PROBLEM (pain, frustration, discomfort BEFORE using product) -> product_visible: false
- If scene shows the SOLUTION, BENEFIT, or RESULT of using the product -> product_visible: true
- First scene often shows the problem -> product usually NOT visible yet
- Product is revealed when transitioning from problem to solution

Example: For an ergonomic chair video:
- Scene 1 (hook/problem): Person with back pain at desk -> product_visible: FALSE
- Scene 2 (solution): New chair is introduced/revealed -> product_visible: TRUE
- Scene 3+ (benefits): Comfortable sitting, happy user -> product_visible: TRUE

Output ONLY valid JSON with the scenes array, total_duration, and music_style."""

    user_content.append({"type": "text", "text": text_prompt})

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        llm_result = call_fn(messages, temperature=0.7, max_tokens=32000)
        content = llm_result.get("text", "")

        # Parse the JSON response
        parsed = extract_json_from_response(content)

        if parsed and "scenes" in parsed:
            scenes = parsed.get("scenes", [])
            logger.info(f"Generated {len(scenes)} scene prompts")
            return {
                "scenes": scenes,
                "total_duration": parsed.get("total_duration", target_duration),
                "music_style": parsed.get("music_style", "Upbeat, modern, corporate background music"),
            }
        else:
            logger.error("Could not parse scene prompts from Gemini response")
            logger.warning(f"Raw response (first 2000 chars): {content[:2000]}")
            return get_empty_scene_result()

    except Exception as e:
        logger.error(f"Error generating scene prompts: {e}")
        return get_empty_scene_result()


# ---------------------------------------------------------------------------
# 4. generate_influencer_prompts_smart (2-step: Director → Writer)
#    with flat clip list and gap-fill convergence loop
# ---------------------------------------------------------------------------

# -- Response schemas for enhanced Director --

_CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clip_index": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "type": {"type": "string", "enum": ["existing", "generate"]},
        "duration": {"type": "number"},
        "shows_influencer": {"type": "boolean"},
        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "motion_prompt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "first_prompt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "second_prompt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "reason": {"type": "string"},
    },
    "required": ["clip_index", "type", "duration", "shows_influencer", "description", "motion_prompt", "first_prompt", "second_prompt", "reason"],
    "additionalProperties": False,
}

# Merged Director+Writer schema for single-call smart mode
DIRECTOR_WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "beat_number": {"type": "integer"},
                    "total_duration": {"type": "number"},
                    "clips": {"type": "array", "items": _CLIP_SCHEMA},
                    "backup_clips": {"type": "array", "items": _CLIP_SCHEMA},
                },
                "required": ["beat_number", "total_duration", "clips"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["beats"],
    "additionalProperties": False,
}

# Legacy Director-only schema (still used as internal fallback)
_CLIP_SCHEMA_LEGACY = {
    "type": "object",
    "properties": {
        "clip_index": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "type": {"type": "string", "enum": ["existing", "generate"]},
        "duration": {"type": "number"},
        "shows_influencer": {"type": "boolean"},
        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "motion_prompt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "reason": {"type": "string"},
    },
    "required": ["clip_index", "type", "duration", "shows_influencer", "description", "motion_prompt", "reason"],
    "additionalProperties": False,
}

DIRECTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "beat_number": {"type": "integer"},
                    "total_duration": {"type": "number"},
                    "clips": {"type": "array", "items": _CLIP_SCHEMA_LEGACY},
                    "backup_clips": {"type": "array", "items": _CLIP_SCHEMA_LEGACY},
                },
                "required": ["beat_number", "total_duration", "clips", "backup_clips"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["beats"],
    "additionalProperties": False,
}

DIRECTOR_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "beat_number": {"type": "integer"},
                    "total_duration": {"type": "number"},
                    "clips": {"type": "array", "items": _CLIP_SCHEMA_LEGACY},
                    "backup_clips": {"type": "array", "items": _CLIP_SCHEMA_LEGACY},
                },
                "required": ["beat_number", "total_duration", "clips", "backup_clips"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fixes"],
    "additionalProperties": False,
}


def _build_clip_list(
    asset_descriptions: List[Dict[str, Any]],
    ref_images: List[Dict[str, Any]],
    surprise_mode=2,
) -> List[Dict[str, Any]]:
    """Build a flat numbered clip list from video moments + reference images.

    Returns a single list where each clip dict contains both the Director-facing
    fields (``clip_index``, ``description``, ``uniqueness``, ``motion_intensity``)
    and the resolution fields (``source_type``, ``asset_index``, ``moment_index``,
    ``start_seconds``, ``end_seconds``, ``real_duration``, ``asset_url`` for video
    moments; ``image_index`` for ref images).
    """
    clip_list = []
    idx = 0

    # Video moments first
    for vi, ad in enumerate(asset_descriptions or []):
        for moment in ad.get("key_moments", []):
            dur = moment.get("duration_seconds",
                             moment.get("end_seconds", 0) - moment.get("start_seconds", 0))
            motion = moment.get("motion_intensity", "")
            desc = moment.get("description", "Video clip")
            clip_list.append({
                "clip_index": idx,
                "description": desc,
                "uniqueness": moment.get("uniqueness", "medium"),
                "motion_intensity": motion or None,
                "source_type": "video_moment",
                "asset_index": vi,
                "moment_index": moment.get("index", 0),
                "start_seconds": moment.get("start_seconds", 0),
                "end_seconds": moment.get("end_seconds", 0),
                "real_duration": round(dur, 2),
                "asset_url": ad.get("url", ""),
            })
            idx += 1

    # Reference images (described as potential clips)
    # surprise_mode controls regular vs surprise behavior:
    #   "none"  — regular only, no surprises
    #   "all"   — surprise replaces regular (when available), regular as fallback
    #   int N   — both regular + surprise with variant_group pairing (Director picks)
    # venue is independent — always added when venue data exists, regardless of surprise_mode
    for ii, img in enumerate(ref_images or []):
        desc_regular = img.get("description_variant_regular") or img.get("analysis") or img.get("description") or "Reference image"
        if len(desc_regular) > 300:
            desc_regular = desc_regular[:300] + "..."

        motion_regular = img.get("motion_prompt_regular", "Subtle slow zoom in, very slight movement")
        uniq = img.get("uniqueness", "medium")

        has_surprise_data = bool(img.get("motion_prompt_surprise"))
        has_venue_data = bool(img.get("motion_prompt_venue"))

        # --- Regular / Surprise clips (controlled by surprise_mode) ---
        if surprise_mode == "none" or not has_surprise_data:
            # Regular only
            clip_list.append({
                "clip_index": idx,
                "description": desc_regular,
                "uniqueness": uniq,
                "motion_intensity": None,
                "source_type": "ref_image",
                "image_index": ii,
                "real_duration": None,
                "motion_prompt": motion_regular,
                "variant": "regular",
            })
            idx += 1

        elif surprise_mode == "all":
            # "all" — surprise replaces regular (no variant_group, single entry)
            desc_surprise = img.get("description_variant_surprise") or "Surprise animation"
            if len(desc_surprise) > 300:
                desc_surprise = desc_surprise[:300] + "..."
            clip_list.append({
                "clip_index": idx,
                "description": desc_surprise,
                "uniqueness": uniq,
                "motion_intensity": None,
                "source_type": "ref_image",
                "image_index": ii,
                "real_duration": None,
                "motion_prompt": img["motion_prompt_surprise"],
                "variant": "surprise",
            })
            idx += 1

        else:
            # int N — both regular + surprise with variant_group pairing
            clip_list.append({
                "clip_index": idx,
                "description": desc_regular,
                "uniqueness": uniq,
                "motion_intensity": None,
                "source_type": "ref_image",
                "image_index": ii,
                "real_duration": None,
                "motion_prompt": motion_regular,
                "variant": "regular",
                "variant_group": ii,
            })
            idx += 1

            desc_surprise = img.get("description_variant_surprise") or "Surprise animation"
            if len(desc_surprise) > 300:
                desc_surprise = desc_surprise[:300] + "..."
            clip_list.append({
                "clip_index": idx,
                "description": desc_surprise,
                "uniqueness": uniq,
                "motion_intensity": None,
                "source_type": "ref_image",
                "image_index": ii,
                "real_duration": None,
                "motion_prompt": img["motion_prompt_surprise"],
                "variant": "surprise",
                "variant_group": ii,
            })
            idx += 1

        # --- Venue clip (always independent of surprise_mode) ---
        if has_venue_data:
            desc_venue = (img.get("description_variant_venue") or "Influencer placed in venue")[:300]
            clip_list.append({
                "clip_index": idx,
                "description": desc_venue,
                "uniqueness": uniq,
                "motion_intensity": None,
                "source_type": "ref_image",
                "image_index": ii,
                "real_duration": None,
                "motion_prompt": img["motion_prompt_venue"],
                "variant": "influencer_in_venue",
            })
            idx += 1

    return clip_list


def _validate_director_output(
    beats: List[Dict[str, Any]],
    clip_list: List[Dict[str, Any]],
    vo_segments: List[Dict[str, Any]],
    tolerance: float = 0.3,
    min_influencer_clip_ratio: float = None,
    max_influencer_clip_ratio: float = None,
    dissolve_seconds: float = 0.0,
) -> List[Dict[str, Any]]:
    """Validate Director beat assignments: timing math, duplicate clips, duration overrides.

    Returns a list of error dicts (empty = all valid). Each error has
    ``beat_number``, ``issue``, ``detail``.
    """
    defaults = get_pipeline_defaults()
    max_gen_dur = defaults.get("max_generate_clip_duration", 4.0)
    max_gen_inf_dur = defaults.get("max_generate_influencer_clip_duration", 2.0)

    defaults = get_pipeline_defaults()
    min_inf_ratio = min_influencer_clip_ratio if min_influencer_clip_ratio is not None else defaults.get("min_influencer_clip_ratio", 0.10)
    max_inf_ratio = max_influencer_clip_ratio if max_influencer_clip_ratio is not None else defaults.get("max_influencer_clip_ratio", 0.20)

    errors = []
    used_clips: Dict[int, int] = {}  # clip_index -> beat_number
    total_clip_count = 0
    influencer_clip_count = 0

    for beat in beats:
        bn = beat.get("beat_number", 0)
        target_dur = beat.get("total_duration", 0)
        clips = beat.get("clips", [])
        total = 0.0

        for clip in clips:
            ci = clip.get("clip_index")
            ctype = clip.get("type", "generate")
            cdur = clip.get("duration", 0)

            # Resolve "existing" clips to actual source type from clip_list
            if ci is not None and 0 <= ci < len(clip_list):
                m = clip_list[ci]
                real_dur = m.get("real_duration")
                if real_dur is not None:
                    # Video moment — enforce real duration
                    if cdur > real_dur + 0.1:
                        clip["duration"] = real_dur
                        cdur = real_dur
                    clip["type"] = "video"
                else:
                    # Reference image — flexible duration (will be animated)
                    clip["type"] = "image"

                # Check duplicates
                if ci in used_clips:
                    errors.append({
                        "beat_number": bn,
                        "issue": "duplicate_clip",
                        "detail": f"Clip {ci} already used in beat {used_clips[ci]}",
                    })
                used_clips[ci] = bn

            # Validate generate clip durations
            if ctype == "generate":
                is_inf = clip.get("shows_influencer", False)
                max_dur = max_gen_inf_dur if is_inf else max_gen_dur
                label = "influencer" if is_inf else "non-influencer"
                if cdur > max_dur:
                    errors.append({
                        "beat_number": bn,
                        "issue": "generate_too_long",
                        "detail": f"Generate {label} clip is {cdur:.1f}s — max {max_dur:.0f}s. Split into multiple shorter clips with different actions/angles.",
                    })

            total += cdur
            total_clip_count += 1
            if clip.get("shows_influencer", False):
                influencer_clip_count += 1

        # Check timing
        diff = total - target_dur
        if abs(diff) > tolerance:
            direction = "OVER" if diff > 0 else "SHORT"
            errors.append({
                "beat_number": bn,
                "issue": "timing",
                "detail": f"needs {target_dur:.1f}s, filled {total:.1f}s ({direction} {abs(diff):.1f}s)",
            })

    # Check influencer clip count ratio
    if total_clip_count > 0:
        inf_ratio = influencer_clip_count / total_clip_count
        if inf_ratio < min_inf_ratio:
            errors.append({
                "beat_number": 0,
                "issue": "influencer_ratio",
                "detail": f"Influencer in only {influencer_clip_count}/{total_clip_count} clips ({inf_ratio:.0%}). Must be at least {min_inf_ratio:.0%}.",
            })
        elif inf_ratio > max_inf_ratio:
            errors.append({
                "beat_number": 0,
                "issue": "influencer_ratio_high",
                "detail": f"Influencer in {influencer_clip_count}/{total_clip_count} clips ({inf_ratio:.0%}). Must be at most {max_inf_ratio:.0%}. Replace some influencer clips with existing or atmosphere clips.",
            })

    # Check total planned vs VO with dissolve loss
    if dissolve_seconds > 0 and vo_segments:
        total_clips = sum(len(b.get("clips", [])) for b in beats)
        dissolve_loss = (total_clips - 1) * dissolve_seconds
        total_planned = sum(b.get("total_duration", 0) for b in beats)
        total_vo = sum(s.get("duration", 0) for s in vo_segments)
        effective = total_planned - dissolve_loss
        if effective < total_vo - tolerance:
            shortfall = total_vo - effective
            errors.append({
                "beat_number": 0,
                "issue": "dissolve_shortfall",
                "detail": f"Total {total_planned:.1f}s - dissolve {dissolve_loss:.1f}s = "
                          f"{effective:.1f}s effective, but VO needs {total_vo:.1f}s. "
                          f"Extend beats by {shortfall:.1f}s total.",
            })

    # Check variant group exclusivity — same group = same source image, pick only one
    used_groups: Dict[int, int] = {}  # variant_group -> clip_index of first usage
    for ci_used in used_clips:
        if 0 <= ci_used < len(clip_list):
            vg = clip_list[ci_used].get("variant_group")
            if vg is not None:
                if vg in used_groups:
                    other_ci = used_groups[vg]
                    errors.append({
                        "beat_number": 0,
                        "issue": "variant_group_conflict",
                        "detail": f"Clips {other_ci} and {ci_used} are from the same variant group {vg} (same source image). Pick only ONE variant per group.",
                    })
                else:
                    used_groups[vg] = ci_used

    # Check same-source adjacency — clips from same source image must not be consecutive
    all_clips_flat = []
    for b in beats:
        for c in b.get("clips", []):
            ci = c.get("clip_index")
            if ci is not None and 0 <= ci < len(clip_list):
                all_clips_flat.append((b["beat_number"], ci))
    for i in range(len(all_clips_flat) - 1):
        bn1, ci1 = all_clips_flat[i]
        bn2, ci2 = all_clips_flat[i + 1]
        img1 = clip_list[ci1].get("image_index")
        img2 = clip_list[ci2].get("image_index")
        if img1 is not None and img1 == img2:
            errors.append({
                "beat_number": bn2,
                "issue": "same_source_adjacent",
                "detail": f"Clips {ci1} and {ci2} are from the same source image (source={img1}). "
                          f"Move them apart — same-source clips must not be consecutive.",
            })

    return errors


def _build_correction_message(
    errors: List[Dict[str, Any]],
    beats: List[Dict[str, Any]],
    clip_list: List[Dict[str, Any]],
) -> str:
    """Build a correction message for the Director based on validation errors."""
    lines = [
        "Your assignments have timing issues. Fix ONLY the beats listed below.",
        "All other beats are confirmed — do not change them.",
        "",
    ]

    beat_map = {b["beat_number"]: b for b in beats}

    for err in errors:
        bn = err["beat_number"]

        if err["issue"] == "influencer_ratio":
            lines.append(f"INFLUENCER RATIO TOO LOW — {err['detail']}")
            lines.append(f"  -> Replace some existing/asset clips with generate clips that have shows_influencer=true.")
            lines.append(f"  -> Ensure hook and closing beats each have at least one influencer generate clip.")
            lines.append("")
            continue

        if err["issue"] == "influencer_ratio_high":
            lines.append(f"INFLUENCER RATIO TOO HIGH — {err['detail']}")
            lines.append(f"  -> Replace some influencer generate clips with existing/asset clips or atmosphere generate clips.")
            lines.append("")
            continue

        if err["issue"] == "dissolve_shortfall":
            lines.append(f"DISSOLVE SHORTFALL — {err['detail']}")
            lines.append(f"  -> Make some beats longer (add a few tenths of a second to flexible clips) so total planned time compensates for dissolve loss.")
            lines.append("")
            continue

        if err["issue"] == "variant_group_conflict":
            lines.append(f"VARIANT GROUP CONFLICT — {err['detail']}")
            lines.append(f"  -> Same-group clips are the SAME source image with different animations. Pick only ONE from each group.")
            lines.append("")
            continue

        if err["issue"] == "same_source_adjacent":
            lines.append(f"SAME-SOURCE ADJACENCY — {err['detail']}")
            lines.append(f"  -> Reorder clips so that same-source clips are separated by at least one clip from a different source.")
            lines.append("")
            continue

        if err["issue"] == "generate_too_long":
            lines.append(f"Beat {bn} — {err['detail']}")
            lines.append(f"  -> Split into 2+ shorter clips with different actions/angles.")
            lines.append("")
            continue

        beat = beat_map.get(bn, {})
        clips_desc = []
        for c in beat.get("clips", []):
            ci = c.get("clip_index")
            if ci is not None:
                desc = next((cl["description"][:60] for cl in clip_list if cl["clip_index"] == ci), f"Clip {ci}")
                real_dur = clip_list[ci].get("real_duration") if 0 <= ci < len(clip_list) else None
                dur_label = f"{real_dur:.1f}s FIXED" if real_dur else f"{c['duration']:.1f}s flexible"
                clips_desc.append(f"[Clip {ci}: {desc} {dur_label}]")
            else:
                clips_desc.append(f"[generate: {c.get('description', '?')[:40]} {c['duration']:.1f}s]")

        lines.append(f"Beat {bn} — {err['detail']}")
        lines.append(f"  Current: {' + '.join(clips_desc)}")
        if err["issue"] == "timing":
            lines.append(f"  -> Adjust clip durations or add/remove clips to match exactly {beat.get('total_duration', 0):.1f}s")
        elif err["issue"] == "duplicate_clip":
            lines.append(f"  -> {err['detail']}. Use a different clip or generate.")
        lines.append("")

    # List unused clips
    used = set()
    for beat in beats:
        for c in beat.get("clips", []):
            ci = c.get("clip_index")
            if ci is not None:
                used.add(ci)
    unused = [c for c in clip_list if c["clip_index"] not in used]
    if unused:
        lines.append("UNUSED CLIPS (consider using these):")
        for c in unused:
            real_dur = c.get("real_duration")
            dur_label = f"{real_dur:.1f}s, video" if real_dur else "flexible, image"
            lines.append(f"  Clip {c['clip_index']}: {c['description'][:80]} ({dur_label})")
        lines.append("")

    lines.append("ALL ORIGINAL RULES STILL APPLY.")
    return "\n".join(lines)


def _force_fix_durations(
    beats: List[Dict[str, Any]],
    clip_list: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Safety net: adjust flexible clips (image/generate) to match beat durations."""
    for beat in beats:
        target = beat.get("total_duration", 0)
        clips = beat.get("clips", [])
        if not clips:
            continue

        # Find total of fixed clips and identify flexible ones
        fixed_total = 0.0
        flexible_indices = []
        for i, c in enumerate(clips):
            ci = c.get("clip_index")
            is_fixed = False
            if ci is not None and 0 <= ci < len(clip_list):
                if clip_list[ci].get("real_duration") is not None:
                    is_fixed = True
            if is_fixed:
                fixed_total += c["duration"]
            else:
                flexible_indices.append(i)

        if not flexible_indices:
            continue

        remaining = target - fixed_total
        if remaining <= 0:
            # Remove flexible clips — fixed clips already fill the beat
            beat["clips"] = [c for i, c in enumerate(clips) if i not in flexible_indices]
            continue

        # Distribute remaining time evenly among flexible clips
        per_flex = remaining / len(flexible_indices)
        for i in flexible_indices:
            clips[i]["duration"] = round(max(1.0, min(8.0, per_flex)), 2)

    return beats


def _direct_media_enhanced(
    fn_director,
    vo_segments: List[Dict[str, Any]],
    clip_list: List[Dict[str, Any]],
    influencer_desc: str,
    max_rounds: int = 3,
    min_influencer_clip_ratio: float = None,
    max_influencer_clip_ratio: float = None,
    highlights_text: str = "",
    surprise_mode=2,
    visual_location: str = "",
) -> List[Dict[str, Any]]:
    """Enhanced Director: multi-clip beats with durations and correction loop.

    Returns list of beat dicts with resolved clips.
    """
    loader = get_prompt_loader()

    # Build beats text with durations
    beat_lines = []
    for seg in vo_segments:
        sn = seg.get("scene_num", seg.get("beat_number", 0))
        dur = seg.get("duration", 4.0)
        text = seg.get("text", "")[:120]
        role = seg.get("role", "")
        role_tag = f" [{role.upper()}]" if role else ""
        beat_lines.append(f"  Beat {sn}{role_tag} ({dur:.1f}s): \"{text}\"")
    beats_text = "\n".join(beat_lines) if beat_lines else "No beats provided."

    # Build clip list text with durations and types
    clip_lines = []
    for c in clip_list:
        ci = c["clip_index"]
        desc = c["description"]
        real_dur = c.get("real_duration")
        uniq = c.get("uniqueness", "medium")
        uniq_tag = f" ★{uniq}" if uniq == "high" else f" {uniq}"
        motion = c.get("motion_intensity")
        variant = c.get("variant")
        vgroup = c.get("variant_group")
        img_idx = c.get("image_index")
        if real_dur is not None:
            motion_tag = f", motion: {motion}" if motion else ""
            clip_lines.append(f"  Clip {ci}: {desc} ({real_dur:.1f}s, video,{uniq_tag} uniqueness{motion_tag})")
        else:
            variant_tag = ""
            if variant:
                if vgroup is not None:
                    variant_tag = f", {variant.upper()}, group={vgroup}"
                else:
                    variant_tag = f", {variant.upper()}"
            source_tag = f", source={img_idx}" if img_idx is not None else ""
            clip_lines.append(f"  Clip {ci}: {desc} (flexible, image,{uniq_tag} uniqueness{variant_tag}{source_tag})")
    clip_list_text = "\n".join(clip_lines) if clip_lines else "No clips available — generate all."

    defaults = get_pipeline_defaults()
    _md_rounds = defaults.get("media_director_max_rounds")
    if _md_rounds is not None:
        try:
            max_rounds = max(1, min(int(_md_rounds), 5))
        except (TypeError, ValueError):
            pass
    max_gen_dur = defaults.get("max_generate_clip_duration", 4.0)
    max_gen_inf_dur = defaults.get("max_generate_influencer_clip_duration", 2.0)
    min_gen_dur = defaults.get("min_generate_clip_duration", 2.0)
    max_flex_dur = 8  # max animation duration from SUPPORTED_DURATIONS
    dissolve_sec = defaults.get("dissolve_seconds", 0.075)

    _min_inf = min_influencer_clip_ratio if min_influencer_clip_ratio is not None else defaults.get("min_influencer_clip_ratio", 0.10)
    _max_inf = max_influencer_clip_ratio if max_influencer_clip_ratio is not None else defaults.get("max_influencer_clip_ratio", 0.20)

    # Pre-compute dissolve example values for the prompt template
    _example_clips = 12
    _example_loss = round((_example_clips - 1) * dissolve_sec, 2)
    _example_target = round(25.5 + _example_loss, 1)

    # Build surprise_instructions based on surprise_mode
    if isinstance(surprise_mode, int) and surprise_mode >= 1:
        surprise_instructions = (
            "VARIANT CLIPS (REGULAR / SURPRISE / INFLUENCER_IN_VENUE):\n"
            "- Some image clips come in groups sharing the same group number — REGULAR and SURPRISE variants of the same source image.\n"
            "- REGULAR: natural camera animation (pan, zoom, environmental motion).\n"
            "- SURPRISE: a whimsical element in the image comes alive (doll moves, sticker winks, origami flies). These create viral, share-worthy moments.\n"
            "- INFLUENCER_IN_VENUE: the influencer is composited INTO the venue/location in the image. Creates authentic 'I was there' shots. These count as shows_influencer=true. NOT part of a variant group — can be used alongside a REGULAR or SURPRISE variant from the same source.\n"
            "- From each variant group, pick ONLY ONE (they are the same source image, animated differently).\n"
            "- For INTERMEDIATE beats (not hook, not closing): if you choose a SURPRISE variant from a group, the REGULAR variant of that same group is OFF LIMITS — they are the same photo with very similar motion, so using both makes the video look repetitive.\n"
            "- SAME-SOURCE ADJACENCY: Clips sharing the same `source` number come from the SAME photo. Never place two clips with the same `source` in consecutive positions (back-to-back within a beat or across adjacent beats). Spread them apart for visual variety.\n"
            f"- Use at least {surprise_mode} SURPRISE variant(s) in the video if {surprise_mode} or more are available. "
            "Place them in high-impact beats (hook, discovery, or outcome). If fewer surprise variants exist, use as many as you can.\n"
            "- INFLUENCER_IN_VENUE variants are great for hook or discovery beats — they show the influencer naturally in the real venue.\n"
            "\n"
            "Example:\n"
            '  Clip 4: "Cozy table with cat doll and sushi boat" (flexible, image, ★high, REGULAR, group=2, source=0)\n'
            '  Clip 5: "Cat doll\'s paw reaches toward a sushi piece" (flexible, image, ★high, SURPRISE, group=2, source=0)\n'
            '  Clip 6: "Young woman sitting at the table, smiling at the camera" (flexible, image, ★high, INFLUENCER_IN_VENUE, source=0)\n'
            "  → 4 and 5 are the SAME group — pick one (e.g., 5 for surprise).\n"
            "  → 6 is a different type (venue) but SAME source — can be used alongside 5, but NOT in the next/previous clip position.\n\n"
        )
    elif surprise_mode == "all":
        surprise_instructions = (
            "All image clips have been pre-selected for maximum visual impact. Use them as provided.\n\n"
        )
    else:
        surprise_instructions = ""

    dur_vars = dict(
        min_clip_dur=int(min_gen_dur),
        max_clip_dur=int(max_flex_dur),
        max_influencer_dur=int(max_gen_inf_dur),
        max_non_influencer_dur=int(max_gen_dur),
        min_influencer_pct=int(_min_inf * 100),
        max_influencer_pct=int(_max_inf * 100),
        dissolve_seconds=dissolve_sec,
        example_loss=_example_loss,
        example_target=_example_target,
        surprise_instructions=surprise_instructions,
    )

    if highlights_text:
        highlights_section = (
            "BUSINESS HIGHLIGHTS (prefer clips showing these):\n"
            f"{highlights_text}\n\n"
            "When choosing between clips of similar uniqueness, PREFER clips whose description matches a highlight."
        )
    else:
        highlights_section = ""

    if visual_location:
        location_context = (
            f"LOCATION CONTEXT: This business/product is in {visual_location}. "
            "When writing descriptions for generate clips (streets, exteriors, atmosphere), "
            f"describe the city character of {visual_location} — local architecture style, "
            "street patterns, typical signage, lighting, and cultural atmosphere. "
            "You still must NOT name specific storefronts or addresses, but the scene must LOOK like "
            f"it was filmed in {visual_location}, not in a generic American city. "
            "Example: for Wroclaw, Poland → 'cobblestone European street with warm streetlights and low-rise "
            "historic buildings' NOT 'bustling city street with skyscrapers'."
        )
    else:
        location_context = ""

    system_msg = loader.get("ugc_director_system", **dur_vars)
    user_msg = loader.get(
        "ugc_director_user",
        beats_text=beats_text,
        clip_list_text=clip_list_text,
        influencer_description=influencer_desc or "No influencer description provided.",
        highlights_section=highlights_section,
        location_context=location_context,
        **dur_vars,
    )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    for rnd in range(max_rounds):
        schema = DIRECTOR_SCHEMA if rnd == 0 else DIRECTOR_FIX_SCHEMA
        logger.info("Director round %d/%d starting (schema=%s)...",
                     rnd + 1, max_rounds, "initial" if rnd == 0 else "fix")
        llm_result = fn_director(messages, temperature=0.3, responseSchema=schema)
        result_text = (llm_result.get("text") or "").strip()

        # Strip markdown fences
        if result_text.startswith("```"):
            result_text = result_text.split("\n", 1)[1] if "\n" in result_text else result_text[3:]
        if result_text.rstrip().endswith("```"):
            result_text = result_text.rstrip()[:-3].rstrip()

        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError:
            parsed = extract_json_from_response(result_text)

        if not parsed:
            logger.error("Director round %d returned unparseable response", rnd + 1)
            break

        if rnd == 0:
            beats = parsed.get("beats", [])
        else:
            # Merge fixes into existing beats
            fixes = parsed.get("fixes", parsed.get("beats", []))
            fix_map = {f["beat_number"]: f for f in fixes}
            fixed_beat_nums = list(fix_map.keys())
            for i, b in enumerate(beats):
                if b["beat_number"] in fix_map:
                    beats[i] = fix_map[b["beat_number"]]
            logger.info("Director round %d: merged fixes for beats %s", rnd + 1, fixed_beat_nums)

        if not beats:
            logger.error("Director returned empty beats")
            break

        # Log per-beat summary
        total_clips = 0
        total_dur = 0.0
        inf_clips = 0
        for b in beats:
            clips = b.get("clips", [])
            total_clips += len(clips)
            beat_dur = sum(c.get("duration", 0) for c in clips)
            total_dur += beat_dur
            inf_clips += sum(1 for c in clips if c.get("shows_influencer", False))
        logger.info("Director round %d result: %d beats, %d clips, %.1fs total, %d influencer clips (%.0f%%)",
                     rnd + 1, len(beats), total_clips, total_dur,
                     inf_clips, (inf_clips / total_clips * 100) if total_clips else 0)

        # Validate
        errors = _validate_director_output(beats, clip_list, vo_segments,
            min_influencer_clip_ratio=_min_inf, max_influencer_clip_ratio=_max_inf,
            dissolve_seconds=dissolve_sec)
        if not errors:
            if rnd > 0:
                logger.info("Director converged after %d correction round(s)", rnd)
            else:
                logger.info("Director assignments valid on first round")
            return beats

        # Log each error
        for err in errors:
            logger.info("Director round %d error [beat %s]: %s — %s",
                        rnd + 1, err.get("beat_number", "?"), err["issue"], err["detail"])

        logger.info(
            "Director round %d: %d error(s), sending corrections",
            rnd + 1, len(errors),
        )

        # Build correction and append to conversation
        messages.append({"role": "assistant", "content": result_text})
        correction = _build_correction_message(errors, beats, clip_list)
        messages.append({"role": "user", "content": correction})

    # Exhausted rounds — force-fix
    logger.warning("Director did not converge after %d rounds, force-fixing", max_rounds)
    beats = _force_fix_durations(beats, clip_list)
    return beats


def _resolve_clips(
    assignments: List[Dict[str, Any]],
    clip_list: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Resolve clip_index references in assignments to actual source types.

    Enriches each assignment with resolved source info:
    - video_asset_index, best_moment_index, start_seconds, end_seconds, asset_url
    - reference_image_index
    - Or left as generate (clip_index=null)

    Also resolves fillers the same way.
    """
    for a in assignments:
        ci = a.get("clip_index")
        if ci is not None and 0 <= ci < len(clip_list):
            m = clip_list[ci]
            if m["source_type"] == "video_moment":
                a["video_asset_index"] = m["asset_index"]
                a["best_moment_index"] = m["moment_index"]
                a["_start_seconds"] = m["start_seconds"]
                a["_end_seconds"] = m["end_seconds"]
                a["_asset_url"] = m.get("asset_url", "")
            elif m["source_type"] == "ref_image":
                a["reference_image_index"] = m["image_index"]
        else:
            # generate scene
            a["video_asset_index"] = None
            a["reference_image_index"] = None

        # Resolve fillers (legacy format)
        for f in a.get("fillers", []):
            fi = f.get("filler_clip_index")
            if fi is not None and 0 <= fi < len(clip_list):
                fm = clip_list[fi]
                if fm["source_type"] == "video_moment":
                    f["video_asset_index"] = fm["asset_index"]
                    f["best_moment_index"] = fm["moment_index"]
                    f["_start_seconds"] = fm["start_seconds"]
                    f["_end_seconds"] = fm["end_seconds"]
                    f["_asset_url"] = fm.get("asset_url", "")
                elif fm["source_type"] == "ref_image":
                    f["reference_image_index"] = fm["image_index"]

    return assignments


def _resolve_beat_clips(
    beats: List[Dict[str, Any]],
    clip_list: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Resolve clip_index references in beat clips to actual source types.

    Each beat contains a ``clips`` list. Each clip with a ``clip_index``
    gets enriched with source info; clips with ``type: "generate"`` get
    generate-specific fields.
    """
    for beat in beats:
        for clip in beat.get("clips", []):
            ci = clip.get("clip_index")
            if ci is not None and 0 <= ci < len(clip_list):
                m = clip_list[ci]
                if m["source_type"] == "video_moment":
                    clip["video_asset_index"] = m["asset_index"]
                    clip["best_moment_index"] = m["moment_index"]
                    clip["_start_seconds"] = m["start_seconds"]
                    clip["_end_seconds"] = m["end_seconds"]
                    clip["_asset_url"] = m.get("asset_url", "")
                elif m["source_type"] == "ref_image":
                    clip["reference_image_index"] = m["image_index"]
                    # Carry motion_prompt and variant from clip list
                    # Skip overwrite for venue clips — Director wrote the complete motion
                    if m.get("motion_prompt") and m.get("variant") != "influencer_in_venue":
                        clip["motion_prompt"] = m["motion_prompt"]
                    if m.get("variant"):
                        clip["variant"] = m["variant"]
            else:
                clip["video_asset_index"] = None
                clip["reference_image_index"] = None
    return beats


def _write_prompts(
    call_fn,
    scene_plan: List[Dict[str, Any]],
    media_assignments: List[Dict[str, Any]],
    ref_images: List[Dict[str, Any]],
    influencer_desc: str,
    style_cfg: Dict[str, Any],
    language: str,
    visual_location: str = "",
    venue_dna: str = "",
) -> List[Dict[str, Any]]:
    """Step C: Prompt Writer — write first_prompt/second_prompt for non-asset scenes."""
    language_name = get_language_name(language)
    loader = get_prompt_loader()

    style_key = style_cfg.get("key", "Auto")
    style_prompt_prefix = style_cfg.get("prefix", "")
    style_forbidden_csv = style_cfg.get("forbidden_csv", "")
    style_instruction = style_cfg.get("instruction", "")

    # Build ref images text
    ref_image_descriptions = []
    for i, img in enumerate(ref_images):
        if img.get("analysis"):
            ref_image_descriptions.append(f"Reference Image {i}: {img['analysis'][:500]}")
        else:
            ref_image_descriptions.append(f"Reference Image {i}: [Content not analyzed]")
    ref_images_text = "\n".join(ref_image_descriptions) if ref_image_descriptions else "No reference images provided."

    system_prompt = loader.get(
        "ugc_writer_system",
        style_key=style_key,
        style_instruction=style_instruction,
        style_prompt_prefix=style_prompt_prefix,
        style_forbidden_csv=style_forbidden_csv,
        language_name=language_name,
    )

    if visual_location:
        location_context = (
            f"LOCATION CONTEXT: This business/product is in {visual_location}. "
            "When writing scene descriptions for generate clips (streets, exteriors, atmosphere), "
            f"describe the city character of {visual_location} — local architecture style, "
            "street patterns, typical signage, lighting, and cultural atmosphere. "
            "You still must NOT name specific storefronts or addresses, but the scene must LOOK like "
            f"it was filmed in {visual_location}, not in a generic American city."
        )
    else:
        location_context = ""

    user_prompt = loader.get(
        "ugc_writer_user",
        scene_plan_json=json.dumps(scene_plan, ensure_ascii=False, indent=2),
        media_assignments_json=json.dumps(media_assignments, ensure_ascii=False, indent=2),
        ref_images_text=ref_images_text,
        influencer_description=influencer_desc or "No influencer description provided.",
        location_context=location_context,
        venue_dna=_build_venue_dna_block(venue_dna),
        style_key=style_key,
        style_prompt_prefix=style_prompt_prefix,
        style_forbidden_csv=style_forbidden_csv,
        language_name=language_name,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    llm_result = call_fn(messages, temperature=0.7)
    result_text = (llm_result.get("text") or "").strip()

    # Strip markdown fences
    if result_text.startswith("```"):
        result_text = result_text.split("\n", 1)[1] if "\n" in result_text else result_text[3:]
    if result_text.rstrip().endswith("```"):
        result_text = result_text.rstrip()[:-3].rstrip()

    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        parsed = extract_json_from_response(result_text)

    if not parsed:
        return {"scene_prompts": [], "filler_prompts": []}

    return {
        "scene_prompts": parsed.get("scene_prompts", []),
        "filler_prompts": parsed.get("filler_prompts", []),
    }


def generate_influencer_prompts_smart(
    call_fn,
    free_text: str,
    reference_images: List[Dict[str, Any]],
    scene_count: int,
    manual_instructions: str = "",
    cta_text: str = "",
    language: str = "en",
    existing_influencer_description: str = "",
    vo_timing: Dict[str, Any] = None,
    visual_style: str = "Auto",
    video_subtype: str = "influencer",
    asset_descriptions: List[Dict[str, Any]] = None,
    on_progress=None,
    call_fn_director=None,
    target_duration: int = 30,
    # Deprecated — kept for backward compatibility, ignored
    call_fn_plan=None,
    min_influencer_clip_ratio: float = None,
    max_influencer_clip_ratio: float = None,
    highlights_text: str = "",
    surprise_mode=2,
    visual_location: str = "",
    venue_dna: str = "",
    logo_url: str = "",
) -> Dict[str, Any]:
    """Generate influencer prompts via a single merged Director+Writer LLM call.

    Combines clip assignment (beat planning) and visual prompt writing into one
    Gemini call using the ``ugc_director_writer_system`` / ``ugc_director_writer_user``
    templates.  The legacy two-step flow (``_direct_media_enhanced`` →
    ``_write_prompts``) is kept intact but is no longer called here.

    Same return format as ``generate_influencer_prompts``:
    ``{"influencer_description": ..., "scene_prompts": [...]}``.
    """
    try:
        mode_label = "personal brand" if video_subtype == "personal_brand" else "influencer"
        logger.info(f"Generating {mode_label} prompts (smart single-call) for {scene_count} scenes...")

        influencer_desc = existing_influencer_description or ""

        # Use call_fn for the merged call (call_fn_director kept for compat but ignored)
        fn_merged = call_fn

        # -- Build flat clip list --
        clip_list = _build_clip_list(
            asset_descriptions or [],
            reference_images,
            surprise_mode=surprise_mode,
        )
        logger.info(f"  Built flat clip list: {len(clip_list)} clips "
                     f"({sum(1 for c in clip_list if c['source_type'] == 'video_moment')} video moments, "
                     f"{sum(1 for c in clip_list if c['source_type'] == 'ref_image')} ref images)")

        if on_progress:
            on_progress("artifact", {
                "name": "smart_clip_list",
                "data": clip_list,
            })

        # -- Extract VO segments --
        vo_segments = []
        if vo_timing and vo_timing.get("scene_segments"):
            vo_segments = vo_timing["scene_segments"]
        elif vo_timing and vo_timing.get("full_text"):
            words = vo_timing["full_text"].split()
            words_per_scene = max(1, len(words) // scene_count)
            total_dur = vo_timing.get("total_duration", target_duration)
            dur_per_scene = total_dur / scene_count
            for i in range(scene_count):
                start = i * words_per_scene
                end = start + words_per_scene if i < scene_count - 1 else len(words)
                vo_segments.append({
                    "scene_num": i + 1,
                    "text": " ".join(words[start:end]),
                    "duration": dur_per_scene,
                    "start_time": i * dur_per_scene,
                    "end_time": (i + 1) * dur_per_scene,
                })

        # -- Prepare template variables shared with Director --
        defaults = get_pipeline_defaults()
        _md_rounds = defaults.get("media_director_max_rounds")
        max_gen_dur = defaults.get("max_generate_clip_duration", 4.0)
        max_gen_inf_dur = defaults.get("max_generate_influencer_clip_duration", 2.0)
        min_gen_dur = defaults.get("min_generate_clip_duration", 2.0)
        max_flex_dur = 8
        dissolve_sec = defaults.get("dissolve_seconds", 0.075)

        _min_inf = min_influencer_clip_ratio if min_influencer_clip_ratio is not None else defaults.get("min_influencer_clip_ratio", 0.10)
        _max_inf = max_influencer_clip_ratio if max_influencer_clip_ratio is not None else defaults.get("max_influencer_clip_ratio", 0.20)

        _example_clips = 12
        _example_loss = round((_example_clips - 1) * dissolve_sec, 2)
        _example_target = round(25.5 + _example_loss, 1)

        if isinstance(surprise_mode, int) and surprise_mode >= 1:
            surprise_instructions = (
                "VARIANT CLIPS (REGULAR / SURPRISE / INFLUENCER_IN_VENUE):\n"
                "- Some image clips come in groups sharing the same group number — REGULAR and SURPRISE variants of the same source image.\n"
                "- REGULAR: natural camera animation (pan, zoom, environmental motion).\n"
                "- SURPRISE: a whimsical element in the image comes alive (doll moves, sticker winks, origami flies). These create viral, share-worthy moments.\n"
                "- INFLUENCER_IN_VENUE: the influencer is composited INTO the venue/location in the image. Creates authentic 'I was there' shots. These count as shows_influencer=true. NOT part of a variant group — can be used alongside a REGULAR or SURPRISE variant from the same source.\n"
                "- From each variant group, pick ONLY ONE (they are the same source image, animated differently).\n"
                "- For INTERMEDIATE beats (not hook, not closing): if you choose a SURPRISE variant from a group, the REGULAR variant of that same group is OFF LIMITS — they are the same photo with very similar motion, so using both makes the video look repetitive.\n"
                "- SAME-SOURCE ADJACENCY: Clips sharing the same `source` number come from the SAME photo. Never place two clips with the same `source` in consecutive positions (back-to-back within a beat or across adjacent beats). Spread them apart for visual variety.\n"
                f"- Use at least {surprise_mode} SURPRISE variant(s) in the video if {surprise_mode} or more are available. "
                "Place them in high-impact beats (hook, discovery, or outcome). If fewer surprise variants exist, use as many as you can.\n"
                "- INFLUENCER_IN_VENUE variants are great for hook or discovery beats — they show the influencer naturally in the real venue.\n"
                "\n"
                "Example:\n"
                '  Clip 4: "Cozy table with cat doll and sushi boat" (flexible, image, ★high, REGULAR, group=2, source=0)\n'
                '  Clip 5: "Cat doll\'s paw reaches toward a sushi piece" (flexible, image, ★high, SURPRISE, group=2, source=0)\n'
                '  Clip 6: "Young woman sitting at the table, smiling at the camera" (flexible, image, ★high, INFLUENCER_IN_VENUE, source=0)\n'
                "  → 4 and 5 are the SAME group — pick one (e.g., 5 for surprise).\n"
                "  → 6 is a different type (venue) but SAME source — can be used alongside 5, but NOT in the next/previous clip position.\n\n"
            )
        elif surprise_mode == "all":
            surprise_instructions = (
                "All image clips have been pre-selected for maximum visual impact. Use them as provided.\n\n"
            )
        else:
            surprise_instructions = ""

        dur_vars = dict(
            min_clip_dur=int(min_gen_dur),
            max_clip_dur=int(max_flex_dur),
            max_influencer_dur=int(max_gen_inf_dur),
            max_non_influencer_dur=int(max_gen_dur),
            min_influencer_pct=int(_min_inf * 100),
            max_influencer_pct=int(_max_inf * 100),
            dissolve_seconds=dissolve_sec,
            example_loss=_example_loss,
            example_target=_example_target,
            surprise_instructions=surprise_instructions,
        )

        if highlights_text:
            highlights_section = (
                "BUSINESS HIGHLIGHTS (prefer clips showing these):\n"
                f"{highlights_text}\n\n"
                "When choosing between clips of similar uniqueness, PREFER clips whose description matches a highlight."
            )
        else:
            highlights_section = ""

        if visual_location:
            location_context = (
                f"LOCATION CONTEXT: This business/product is in {visual_location}. "
                "When writing descriptions for generate clips (streets, exteriors, atmosphere), "
                f"describe the city character of {visual_location} — local architecture style, "
                "street patterns, typical signage, lighting, and cultural atmosphere. "
                "You still must NOT name specific storefronts or addresses, but the scene must LOOK like "
                f"it was filmed in {visual_location}, not in a generic American city. "
                "Example: for Wroclaw, Poland → 'cobblestone European street with warm streetlights and low-rise "
                "historic buildings' NOT 'bustling city street with skyscrapers'."
            )
        else:
            location_context = ""

        # -- Style config for Writer rules embedded in system prompt --
        _style_scene_config = get_style_prompts("scene_config")
        style_key = visual_style if visual_style in _style_scene_config else "Auto"
        style_cfg_raw = _style_scene_config[style_key]
        style_prefix = style_cfg_raw["prefix"]
        style_forbidden_csv = ", ".join(f'"{w}"' for w in style_cfg_raw["forbidden"])
        style_instruction = style_cfg_raw["instruction"]
        language_name = get_language_name(language)

        # -- Build beats text for prompt --
        beat_lines = []
        for seg in vo_segments:
            sn = seg.get("scene_num", seg.get("beat_number", 0))
            dur = seg.get("duration", 4.0)
            text = seg.get("text", "")[:120]
            role = seg.get("role", "")
            role_tag = f" [{role.upper()}]" if role else ""
            beat_lines.append(f"  Beat {sn}{role_tag} ({dur:.1f}s): \"{text}\"")
        beats_text = "\n".join(beat_lines) if beat_lines else "No beats provided."

        # -- Build clip list text --
        clip_lines = []
        for c in clip_list:
            ci = c["clip_index"]
            desc = c["description"]
            real_dur = c.get("real_duration")
            uniq = c.get("uniqueness", "medium")
            uniq_tag = f" ★{uniq}" if uniq == "high" else f" {uniq}"
            motion = c.get("motion_intensity")
            variant = c.get("variant")
            vgroup = c.get("variant_group")
            img_idx = c.get("image_index")
            if real_dur is not None:
                motion_tag = f", motion: {motion}" if motion else ""
                clip_lines.append(f"  Clip {ci}: {desc} ({real_dur:.1f}s, video,{uniq_tag} uniqueness{motion_tag})")
            else:
                variant_tag = ""
                if variant:
                    if vgroup is not None:
                        variant_tag = f", {variant.upper()}, group={vgroup}"
                    else:
                        variant_tag = f", {variant.upper()}"
                source_tag = f", source={img_idx}" if img_idx is not None else ""
                clip_lines.append(f"  Clip {ci}: {desc} (flexible, image,{uniq_tag} uniqueness{variant_tag}{source_tag})")
        clip_list_text = "\n".join(clip_lines) if clip_lines else "No clips available — generate all."

        # -- Build reference images text --
        ref_image_descriptions = []
        for i, img in enumerate(reference_images or []):
            if img.get("analysis"):
                ref_image_descriptions.append(f"Reference Image {i}: {img['analysis'][:500]}")
            else:
                ref_image_descriptions.append(f"Reference Image {i}: [Content not analyzed]")
        ref_images_text = "\n".join(ref_image_descriptions) if ref_image_descriptions else "No reference images provided."

        # -- Build CTA branding block --
        _has_logo = bool(logo_url)
        _has_slogan = bool(cta_text)
        if _has_logo and _has_slogan:
            cta_branding = (
                f"CTA BRANDING (closing beat only):\n"
                f"- A logo image IS provided — the closing beat's generated clip must include a clean, prominent space "
                f"for the logo (centered or lower-third). Write the image prompt with a simple, elegant background "
                f"(soft bokeh, gradient, or brand-connected colors) that would make the logo stand out.\n"
                f"- SLOGAN: \"{cta_text}\" — embed this exact text in the closing clip's first_prompt as a visible "
                f"on-screen caption in {language_name} script."
            )
        elif _has_slogan:
            cta_branding = (
                f"CTA BRANDING (closing beat only):\n"
                f"- SLOGAN: \"{cta_text}\" — embed this exact text in the closing beat's generated clip first_prompt "
                f"as a visible on-screen caption in {language_name} script. "
                f"Use a clean, elegant background (soft bokeh or gradient) for the closing card."
            )
        elif _has_logo:
            cta_branding = (
                f"CTA BRANDING (closing beat only):\n"
                f"- A logo image IS provided — the closing beat's generated clip must have a clean, prominent space "
                f"for the logo overlay. Write a simple, elegant background (soft bokeh, gradient, or warm colors) "
                f"that makes a logo stand out."
            )
        else:
            cta_branding = ""

        # -- Build the merged system + user messages --
        loader = get_prompt_loader()
        system_msg = loader.get(
            "ugc_director_writer_system",
            style_key=style_key,
            style_instruction=style_instruction,
            style_prompt_prefix=style_prefix,
            style_forbidden_csv=style_forbidden_csv,
            language_name=language_name,
            **dur_vars,
        )
        user_msg = loader.get(
            "ugc_director_writer_user",
            beats_text=beats_text,
            clip_list_text=clip_list_text,
            ref_images_text=ref_images_text,
            influencer_description=influencer_desc or "No influencer description provided.",
            highlights_section=highlights_section,
            location_context=location_context,
            venue_dna=_build_venue_dna_block(venue_dna),
            cta_branding=cta_branding,
            style_key=style_key,
            style_prompt_prefix=style_prefix,
            style_forbidden_csv=style_forbidden_csv,
            language_name=language_name,
            **dur_vars,
        )

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        logger.info("  Smart single-call: Director+Writer merged prompt...")
        llm_result = fn_merged(messages, temperature=0.5, responseSchema=DIRECTOR_WRITER_SCHEMA)
        result_text = (llm_result.get("text") or "").strip()

        # Strip markdown fences
        if result_text.startswith("```"):
            result_text = result_text.split("\n", 1)[1] if "\n" in result_text else result_text[3:]
        if result_text.rstrip().endswith("```"):
            result_text = result_text.rstrip()[:-3].rstrip()

        try:
            parsed = json.loads(result_text)
        except json.JSONDecodeError:
            parsed = extract_json_from_response(result_text)

        if not parsed:
            logger.error("  Merged Director+Writer returned unparseable response")
            if on_progress:
                on_progress("artifact", {"name": "smart_director_writer_result", "data": result_text[:2000], "format": "txt"})
            return {"influencer_description": influencer_desc, "scene_prompts": []}

        beats = parsed.get("beats", [])
        if not beats:
            logger.error("  Merged Director+Writer returned empty beats")
            return {"influencer_description": influencer_desc, "scene_prompts": []}

        if on_progress:
            on_progress("artifact", {"name": "smart_director_writer_result", "data": parsed})

        logger.info(f"  Merged call complete: {len(beats)} beats")

        # Validate timing — force-fix without extra LLM round
        errors = _validate_director_output(
            beats, clip_list, vo_segments,
            min_influencer_clip_ratio=_min_inf,
            max_influencer_clip_ratio=_max_inf,
            dissolve_seconds=dissolve_sec,
        )
        if errors:
            logger.warning(f"  Merged result: {len(errors)} timing/ratio error(s) — force-fixing in code...")
            for err in errors:
                logger.info("  [beat %s]: %s — %s", err.get("beat_number", "?"), err["issue"], err["detail"])
            beats = _force_fix_durations(beats, clip_list)

        # Resolve clip indices to source types
        beats = _resolve_beat_clips(beats, clip_list)

        if on_progress:
            on_progress("artifact", {"name": "smart_resolved_beats", "data": beats})

        # -- Build final scene list from beats --
        merged = []
        for beat in beats:
            bn = beat["beat_number"]
            clips = beat.get("clips", [])

            # Collect first_prompt / second_prompt from the primary generate clip
            # (the one that drives the beat-level image generation)
            first_prompt = ""
            second_prompt = ""
            for clip in clips:
                if clip.get("type") == "generate":
                    fp = clip.get("first_prompt") or ""
                    sp = clip.get("second_prompt") or ""
                    if fp:
                        first_prompt = fp
                        second_prompt = sp
                        break

            shows_inf = any(c.get("shows_influencer", False) for c in clips)

            primary_vid_idx = None
            primary_moment_idx = None
            primary_ref_idx = None
            for clip in clips:
                if clip.get("video_asset_index") is not None:
                    primary_vid_idx = clip["video_asset_index"]
                    primary_moment_idx = clip.get("best_moment_index")
                    break
            if primary_vid_idx is None:
                for clip in clips:
                    if clip.get("reference_image_index") is not None:
                        primary_ref_idx = clip["reference_image_index"]
                        break

            scene = {
                "scene_number": bn,
                "narrative_role": "",
                "shows_influencer": shows_inf,
                "reference_image_index": primary_ref_idx,
                "video_asset_index": primary_vid_idx,
                "best_moment_index": primary_moment_idx,
                "first_prompt": first_prompt,
                "second_prompt": second_prompt,
                "duration": beat.get("total_duration", 4.0),
                "beat_clips": clips,
            }

            if shows_inf and first_prompt:
                has_beat_clips = bool(clips and any(c.get("type") == "generate" for c in clips))
                if has_beat_clips:
                    scene["first_prompt"] = f"INFLUENCER scene. {first_prompt}"
                elif influencer_desc:
                    scene["first_prompt"] = f"INFLUENCER: {influencer_desc}. SCENE: {first_prompt}"

            merged.append(scene)

        if on_progress:
            on_progress("artifact", {"name": "smart_merged_scenes", "data": merged})

        logger.info(f"Generated {len(merged)} {mode_label} scene prompts (smart single-call)")
        return {
            "influencer_description": influencer_desc,
            "scene_prompts": merged,
            "beats": beats,
        }

    except Exception as e:
        logger.error(f"Failed to generate {mode_label} prompts (smart single-call): {e}")
        return {"influencer_description": "", "scene_prompts": []}


# ---------------------------------------------------------------------------
# Highlight extraction
# ---------------------------------------------------------------------------

HIGHLIGHTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "highlight": {"type": "string"},
            "visual_cue": {"type": "string"},
        },
        "required": ["highlight", "visual_cue"],
    },
}


def extract_highlights(
    call_fn,
    free_text: str,
    business_category: str = "general",
) -> List[Dict[str, str]]:
    """Extract unique business highlights from content via LLM.

    Returns list of dicts with 'highlight' and 'visual_cue' keys,
    or empty list on failure (non-blocking).
    """
    try:
        loader = get_prompt_loader()
        system_msg = loader.get("shared_extract_highlights_system")
        user_msg = loader.get(
            "shared_extract_highlights_user",
            business_category=business_category,
            free_text=free_text,
        )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        result = call_fn(messages, temperature=0.3, responseSchema=HIGHLIGHTS_SCHEMA)
        text = (result.get("text") or "").strip()
        # Strip markdown fences
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            parsed = parsed.get("highlights", [])
        return parsed[:5]
    except Exception as e:
        logger.warning("Highlights extraction failed: %s", e)
        return []


def format_highlights_text(highlights: List[Dict[str, str]]) -> str:
    """Format highlights list into numbered text for prompt injection."""
    if not highlights:
        return ""
    lines = []
    for i, h in enumerate(highlights, 1):
        text = h.get("highlight", "")
        cue = h.get("visual_cue")
        if cue:
            lines.append(f"{i}. {text} — look for: {cue}")
        else:
            lines.append(f"{i}. {text}")
    return "\n".join(lines)
