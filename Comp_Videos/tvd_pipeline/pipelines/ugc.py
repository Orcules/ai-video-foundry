"""UGC (influencer / personal-brand) video pipeline -- extracted from VideoSceneProcessor.

This module contains the UGC-style video creation pipeline and its
dedicated helper functions.  The main function ``process_ugc_video``
accepts a *processor* instance (a ``VideoSceneProcessor``) so it can
access all services.

Imported by the monolith via::

    from tvd_pipeline.pipelines.ugc import process_ugc_video
"""

import json
import math
import re
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from tvd_pipeline.runtime_callback import executor_submit_with_progress
from tvd_pipeline.config import Config, get_pipeline_defaults
from tvd_pipeline.services.veo3 import VeoRAIBlockedError, VeoPromptBlockedError
from tvd_pipeline.data_loader import get_speech_rate, get_language_name, get_elevenlabs_config, get_arc_template, format_arc_beats, get_kie_config, get_fal_config
from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.utils import (
    _SIM_IMAGE, _SIM_VIDEO, _SIM_AUDIO,
    _word_count_for_duration,
    script_only_for_tts,
    snap_duration,
)
from tvd_pipeline.services.ffmpeg_processor import FFmpegProcessor
from tvd_pipeline.services.local_ffmpeg import LocalFFmpegFallback
from tvd_pipeline.services.tasks.character import describe_characters
from tvd_pipeline.services.tasks.image_eval import evaluate_image_quality, evaluate_image_cleanliness
from tvd_pipeline.services.tasks.prompt_parsing import parse_product_prompt, generate_influencer_prompts, generate_influencer_prompts_smart, extract_highlights, format_highlights_text
from tvd_pipeline.services.tasks.voiceover import generate_influencer_vo_script
from tvd_pipeline.services.tasks.music import generate_music_description_from_text
from tvd_pipeline.services.tasks.video_analysis import analyze_asset_videos
from tvd_pipeline.services.tasks.subtitle_enrichment import enrich_transcript_for_subtitles

config = Config()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers (imported from their own modules)
# ---------------------------------------------------------------------------
from tvd_pipeline.pipelines._provider_limits import resolve_scene_video_limits, resolve_scene_image_workers, get_scene_image_stagger_seconds  # noqa: E402
from tvd_pipeline.pipelines._helpers import (  # noqa: E402
    _presplit_vo_into_scenes,
    _presplit_vo_at_sentences,
    _rebalance_oversized_segments,
    _estimate_scene_count_from_text4,
    _apply_phrase_start_strategy,
    _precision_trim_clip,
    _tpad_video,
    _add_end_card_text_overlay,
    emit_llm_usage_events,
)


# ============================================================================
# UGC-SPECIFIC HELPERS
# ============================================================================

def _get_warmup_skip(processor, video_model: str) -> float:
    """Look up warmup_skip for the current video model from models.json."""
    media = processor.model_config.get("media_models", {})
    for _key, cfg in media.items():
        versions = cfg.get("versions", {})
        if video_model in versions:
            return cfg.get("warmup_skip", 0)
    return 0


def _generate_influencer_image(
    processor,
    gender: str,
    product_context: str,
    visual_style: str = "Ultra photorealistic",
    country: str = "",
    language: str = "en",
    video_subtype: str = "influencer",
    portrait_correction: Optional[str] = None,
    studio_character_look: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Generate an influencer or personal-brand creator image when Character column is empty.
    
    Creates a realistic portrait: professional/LinkedIn for personal_brand, Instagram-style for influencer.
    
    Args:
        gender: "m" for male or "f" for female
        product_context: Brief description of the product for context
        visual_style: Visual style for the image
        country: Target country for ethnic appearance (e.g., "Israel", "Japan")
        language: Language code for cultural adaptation fallback
        video_subtype: "influencer" (Instagram-style) or "personal_brand" (professional/LinkedIn)
        portrait_correction: Studio/user feedback for regeneration; injected into the image prompt in full
            (not subject to the short product_context slice used for topic fit).
        studio_character_look: Free-text character appearance description from Studio "Character look" field.
            Injected faithfully so Nano Banana follows user-specified appearance instructions.
        
    Returns:
        Tuple of (image_url, description, portrait_image_prompt) or (None, None, None) on failure.
        ``portrait_image_prompt`` is the full Nano Banana prompt (for Studio voice design).
    """
    try:
        if video_subtype == "personal_brand":
            logger.info(f"?????? Generating {'female' if gender == 'f' else 'male'} professional / personal brand image...")
        else:
            logger.info(f"?????? Generating {'female' if gender == 'f' else 'male'} influencer image...")
        
        # Build prompt for influencer portrait
        look_trim = (str(studio_character_look).strip() if studio_character_look else "")
        has_studio_look = bool(look_trim)

        gender_desc = "woman" if gender == "f" else "man"
        age_range = "25-35" if gender == "f" else "28-38"
        
        # Determine ethnicity based on country or language
        ethnicity_desc = ""
        if country:
            # Load from shared data_maps.json (single source of truth)
            from tvd_pipeline.data_loader import get_data_maps
            country_ethnicity_map = get_data_maps().get("country_ethnicity_map", {})
            ethnicity_desc = country_ethnicity_map.get(country.lower(), "")
        
        # Fallback to language-based ethnicity if country didn't match
        if not ethnicity_desc and language:
            cultural_info = _get_cultural_info_for_language(processor, language)
            if cultural_info:
                ethnicity_desc = cultural_info.get('ethnicity', '')
        
        # Build ethnicity instruction
        ethnicity_section = ""
        if ethnicity_desc:
            ethnicity_section = f"""
CRITICAL - ETHNIC APPEARANCE:
- This person MUST look like they are from {country or 'the target region'}: {ethnicity_desc}
- Their facial features, skin tone, hair type, and overall look must match this ethnicity
- They should look like a REAL local influencer from {country or 'that region'}"""
            if has_studio_look:
                ethnicity_section += """

Exception: If USER-SPECIFIED CHARACTER APPEARANCE (above) clearly implies a different ethnic or regional look, age, or features than this country hint, obey the user's appearance text for those traits."""
        
        # Studio "Character look": appearance only. User text may accidentally include video scene / action — strip at prompt level.
        character_look_section = ""
        if has_studio_look:
            safe_look = look_trim[:1500].replace('"', "'")
            character_look_section = f"""

=== PRIMARY — STUDIO "CHARACTER LOOK" (THIS IS THE MAIN BRIEF FOR THIS PORTRAIT) ===
The block below is what the user typed in Character look. The generated image MUST visibly match it for face, hair, age, skin, expression, head covering (if any), and upper-body clothing. Treat every specific adjective and trait here as mandatory unless it clearly demands a forbidden background or held object (then adapt only the pose/wardrobe, still on white).

Technical guardrails (white studio, chest-up, no props) still apply from GLOBAL PIPELINE RULES above, but creative direction for **who this person is** comes from this block first.

Use ONLY appearance-relevant lines from the user text. Ignore pure scene/street/action wording — do not paint locations; still reflect **mood and social energy** implied by the user's words (e.g. somber, urgent, warm) in face and styling.
User text:
{safe_look}

PRECEDENCE: This block wins over the generic age line ({age_range}), "relatable influencer" defaults, topic-fit wardrobe hints, and country/ethnicity hints whenever they conflict with explicit user appearance text.
"""

        # User-requested edits (Studio "Changes") — must not be appended only after a long prompt:
        # topic_section below uses only the first N chars of product_context, which would drop trailing feedback.
        correction_section = ""
        if portrait_correction and str(portrait_correction).strip():
            safe_corr = str(portrait_correction).strip()[:2000].replace('"', "'")
            correction_section = f"""

=== USER-REQUESTED CHANGES FOR THIS PORTRAIT (MANDATORY) ===
The user asked to regenerate this portrait. Apply EVERY point below in a clearly visible way.
If any instruction below conflicts with generic portrait defaults, OBEY THE USER.
{safe_corr}
"""

        # Topic fit: light hints from main script; weaker when user supplied Character look (avoid overriding their brief).
        topic_section = ""
        if product_context and product_context.strip():
            topic_snip = product_context[:400].replace('"', "'").strip()
            if has_studio_look:
                topic_section = f"""

SECONDARY — STORY / TOPIC (persona & wardrobe hints only; Character look above is PRIMARY for appearance):
Use the excerpt only to nudge formality or energy if it does NOT contradict the user's Character look.
Never render locations, weather, streets, conflict, phones, or actions as visible scene — white studio only.
---
{topic_snip[:280]}{'…' if len(topic_snip) > 280 else ''}
---
If anything here conflicts with PRIMARY Character look, ignore this excerpt for that trait."""
            else:
                topic_section = """
CRITICAL — TOPIC FIT (PERSONA AND WARDROBE ONLY, NOT A SCENE):
The text below is the MAIN VIDEO PROMPT / topic. Use it ONLY to infer what kind of on-camera creator would credibly speak about this subject (e.g. B2B vs lifestyle) and how formal their top should look.
Do NOT depict any place, object, weather, street, phone, camera angle, conflict, or action from this text. The portrait is ALWAYS a neutral white-studio chest-up shot with empty hands — kitchens, gyms, offices, and documentary locations appear in later pipeline steps, never here.
---
""" + topic_snip + """
---
Remember: white seamless backdrop only; chest-up; no props."""
        
        # Personal brand = professional/LinkedIn; influencer = Instagram-style
        # When Studio sends Character look, soften default "attractive / polished" lines — they fight user intent (e.g. somber, plain, older).
        if video_subtype == "personal_brand":
            style_line = (
                "Professional spokesperson reference: credible expert or leader energy, approachable, plain top or subtle business-casual "
                "(no suit props, no desk ? white studio only). Same global chest-up / no-accessories rules."
            )
            if has_studio_look:
                beauty_line = (
                    "Follow USER-SPECIFIED CHARACTER APPEARANCE for credibility level, expression, and grooming. "
                    "Do not upgrade to a generic polished executive headshot unless the user described that."
                )
            else:
                beauty_line = "Polished, trustworthy on-camera presence. Natural skin, clear eyes, well-groomed hair. Executive or specialist credibility without glamour-shot styling."
        else:
            if has_studio_look:
                style_line = (
                    "Neutral white-studio chest-up reference for later compositing. Match the user's described creator — not a default 'influencer pretty' archetype."
                )
                beauty_line = (
                    "Apply USER-SPECIFIED CHARACTER APPEARANCE for expression, age cues, skin texture, and hair exactly as written "
                    "(including somber, tired, plain, weathered, or unconventional looks). Do not substitute a smiling, generically attractive host."
                )
            else:
                style_line = (
                    "Relatable creator / influencer reference: authentic, engaging face, modern and well-groomed ? still a neutral white-studio chest-up shot "
                    "for pipeline consistency (not a lifestyle location portrait)."
                )
                beauty_line = (
                    "Attractive, natural on-camera presence; healthy real skin texture; expressive eyes; well-groomed hair ? believable UGC host, not an exaggerated fashion model."
                )
        
        prompt = get_prompt_loader().get(
            "ugc_influencer_portrait",
            gender_desc=gender_desc,
            age_range=age_range,
            style_line=style_line,
            character_look_section=character_look_section,
            correction_section=correction_section,
            topic_section=topic_section,
            ethnicity_section=ethnicity_section,
            beauty_line=beauty_line,
            visual_style=visual_style,
        )
        
        # Generate the image using Nano Banana
        image_url = processor.kie_service.generate_scene_image(
            image_prompt=prompt,
            product_reference_urls=None,
            product_description=None,
            product_visible=False,
            visual_style=visual_style,
            character_reference_url=None,
            has_character=True
        )
        
        if image_url:
            logger.info(f"   Influencer image generated: {image_url[:60]}...")
            
            # Generate a brief description for consistency (include topic fit when we had product_context)
            description = (
                f"A photorealistic {'female' if gender == 'f' else 'male'} reference portrait ({age_range}), chest-up on a plain white studio background, "
                f"no accessories, suitable as a consistent spokesperson for video scenes."
            )
            if portrait_correction and str(portrait_correction).strip():
                fb = str(portrait_correction).strip()[:200].replace("\n", " ")
                description += f" User-requested adjustments: {fb}."
            elif product_context and product_context.strip():
                topic_brief = product_context[:120].replace("\n", " ").strip()
                description += f" Appearance and style fit the video topic: {topic_brief}."
            
            return image_url, description, prompt
        else:
            logger.warning(f"   Failed to generate influencer image")
            return None, None, None
            
    except Exception as e:
        logger.error(f"   Error generating influencer image: {e}")
        return None, None, None



def _get_cultural_info_for_language(processor, language: str) -> Optional[Dict]:
    """Get cultural info dict for a language code. Used as fallback when Country is not specified."""
    cultural_mapping = {
        'en': {'ethnicity': 'diverse American population', 'country': 'United States'},
        'he': {'ethnicity': 'Israeli/Jewish - diverse including Ashkenazi, Sephardi, Mizrahi', 'country': 'Israel'},
        'ar': {'ethnicity': 'Arab/Middle Eastern - olive to brown skin tones, dark hair', 'country': 'Arab World'},
        'es': {'ethnicity': 'Hispanic/Latino - warm skin tones, dark hair', 'country': 'Latin America'},
        'de': {'ethnicity': 'German/Central European - fair to light skin', 'country': 'Germany'},
        'fr': {'ethnicity': 'French - diverse population', 'country': 'France'},
        'it': {'ethnicity': 'Italian/Mediterranean - olive skin, dark hair', 'country': 'Italy'},
        'pt': {'ethnicity': 'Brazilian - very diverse, mixed race', 'country': 'Brazil'},
        'ja': {'ethnicity': 'Japanese - East Asian features', 'country': 'Japan'},
        'ko': {'ethnicity': 'Korean - East Asian features, K-beauty aesthetic', 'country': 'South Korea'},
        'zh': {'ethnicity': 'Chinese - East Asian features', 'country': 'China'},
        'ru': {'ethnicity': 'Russian/Slavic - fair skin, Eastern European features', 'country': 'Russia'},
        'hi': {'ethnicity': 'Indian/South Asian - brown skin tones, dark hair', 'country': 'India'},
        'tr': {'ethnicity': 'Turkish - Mediterranean to Middle Eastern, olive skin', 'country': 'Turkey'},
        'pl': {'ethnicity': 'Polish/Slavic - fair skin, Eastern European features', 'country': 'Poland'},
        'th': {'ethnicity': 'Thai/Southeast Asian - tan skin, dark hair', 'country': 'Thailand'},
        'vi': {'ethnicity': 'Vietnamese - Southeast Asian features', 'country': 'Vietnam'},
    }
    return cultural_mapping.get(language.lower(), None)



def _pick_reference_image_index_for_scene(
    processor,
    scene: Dict[str, Any],
    ref_image_analyses: List[Dict[str, str]],
    fallback_position: int = 0
) -> Optional[int]:
    """Pick the best reference image index for a scene using text similarity.
    
    Matches scene text (first_prompt + optional vo_text) against each analyzed
    reference image description. This is used when the model does not provide
    a valid ``reference_image_index`` so scenes still align with VO/story.
    
    Type-aware: product screenshots are blocked from early/problem scenes
    and preferred for solution/discovery scenes.
    
    Args:
        scene: Scene prompt dict.
        ref_image_analyses: List from _analyze_reference_images().
        fallback_position: Position-based fallback index (for stable ordering).
        
    Returns:
        Best matching 0-based index, or fallback if no strong match.
    """
    if not ref_image_analyses:
        return None
    
    scene_text = " ".join([
        str(scene.get("first_prompt", "")),
        str(scene.get("vo_text", "")),
        str(scene.get("narrative_role", "")),
    ]).lower()
    
    scene_num = scene.get("scene_number", scene.get("scene_num", fallback_position + 1))
    is_early_scene = (scene_num <= 2 or fallback_position <= 1)
    
    problem_keywords = {"problem", "struggle", "difficult", "hard", "frustrated",
                        "worried", "stress", "stuck", "lost", "confused", "pain",
                        "fail", "wrong", "bad", "worse", "crisis", "challenge"}
    solution_keywords = {"solution", "discover", "found", "answer", "help",
                         "solve", "result", "transform", "success", "better",
                         "easy", "simple", "product", "app", "platform", "service",
                         "tool", "method", "system", "technology"}
    
    scene_tokens_raw = set(re.findall(r"[a-z0-9]+", scene_text))
    is_problem_scene = bool(scene_tokens_raw & problem_keywords) and not bool(scene_tokens_raw & solution_keywords)
    is_solution_scene = bool(scene_tokens_raw & solution_keywords)
    
    stop_words = {
        "the", "and", "for", "with", "that", "this", "from", "into", "your",
        "scene", "shot", "show", "shows", "video", "image", "ultra",
        "photorealistic", "professional", "canon", "lens", "light", "lighting"
    }
    scene_tokens = {t for t in scene_tokens_raw if len(t) > 2 and t not in stop_words}
    
    product_type_tags = {"product_screenshot", "product_photo"}
    
    best_idx = None
    best_score = -1
    
    for item in ref_image_analyses:
        idx = item.get("index")
        desc = str(item.get("description_variant_regular") or item.get("description") or "").lower()
        desc_tokens = set(re.findall(r"[a-z0-9]+", desc))
        desc_tokens = {t for t in desc_tokens if len(t) > 2 and t not in stop_words}
        if not desc_tokens:
            continue
        
        is_product_image = bool(desc_tokens & product_type_tags) or "[product_screenshot]" in desc or "[product_photo]" in desc
        
        if is_product_image and (is_early_scene or is_problem_scene):
            continue
        
        score = len(scene_tokens.intersection(desc_tokens))
        
        if is_product_image and is_solution_scene:
            score += 3
        
        if score > best_score:
            best_score = score
            best_idx = idx
    
    if best_idx is None or best_score <= 0:
        non_product_indices = []
        for item in ref_image_analyses:
            desc = str(item.get("description_variant_regular") or item.get("description") or "").lower()
            if "[product_screenshot]" not in desc and "[product_photo]" not in desc:
                non_product_indices.append(item.get("index"))
        
        if non_product_indices and (is_early_scene or is_problem_scene):
            return non_product_indices[fallback_position % len(non_product_indices)]
        return fallback_position % len(ref_image_analyses)
    
    return int(best_idx)



def _is_likely_image_url(url: str) -> bool:
    """Return True if *url* looks like it points to an actual image file
    (as opposed to an HTML page, a domain root, etc.).  Used to avoid
    passing non-image URLs to Gemini which would return 400.
    """
    if not url:
        return False
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff")):
        return True
    # Cloud storage URLs (GCS, S3, CloudFlare R2) are usually images
    host = parsed.hostname or ""
    if any(h in host for h in ("storage.googleapis.com", "s3.amazonaws.com",
                                "cloudfront.net", "r2.cloudflarestorage.com",
                                "tempfile.aiquickdraw.com", "kie.ai")):
        return True
    # If path is just "/" or empty, it's likely a website
    if not path or path == "":
        return False
    # If the path has an image-like segment, accept it
    if any(seg in path for seg in ("/image", "/photo", "/img", "/media", "/upload")):
        return True
    # Fallback: accept if path has a non-empty last segment without common web extensions
    last_seg = path.split("/")[-1]
    if last_seg and "." not in last_seg:
        return True  # e.g. /abc123 (CDN hash)
    if last_seg and last_seg.endswith((".html", ".htm", ".php", ".aspx", ".app")):
        return False
    return True


IMAGE_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "type": {"type": "string"},
                    "uniqueness": {"type": "string", "enum": ["high", "medium", "low"]},
                    "uniqueness_reason": {"type": "string"},
                    "surprise_candidate": {"type": "boolean"},
                    "venue_candidate": {"type": "boolean"},
                },
                "required": ["index", "type", "uniqueness", "uniqueness_reason", "surprise_candidate", "venue_candidate"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["images"],
    "additionalProperties": False,
}


def _compress_image_for_analysis(image_bytes: bytes, max_dim: int = 1024) -> tuple:
    """Resize + JPEG-compress an image for LLM analysis. Returns (compressed_bytes, mime_type)."""
    from PIL import Image as _PILImage
    import io as _io
    img = _PILImage.open(_io.BytesIO(image_bytes))
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), _PILImage.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), "image/jpeg"


_VENUE_TRIGGER_KEYWORDS = [
    # English
    "restaurant", "cafe", "café", "coffee shop", "bistro", "diner", "eatery",
    "bar", "pub", "lounge", "club",
    "barbershop", "barber", "salon", "beauty salon", "nail salon", "spa",
    "gym", "fitness", "yoga studio", "pilates",
    "store", "shop", "boutique", "market", "pharmacy", "florist",
    "office", "clinic", "dental", "hospital", "studio",
    "hotel", "resort", "airbnb",
    # Hebrew
    "מסעדה", "בית קפה", "קפה", "ספרה", "מספרה", "סלון", "ניל", "ספא",
    "חדר כושר", "פיטנס", "יוגה", "חנות", "בוטיק", "מרפאה", "קליניקה",
    "אולפן", "סטודיו", "מלון",
]


def _should_extract_venue_dna(prompt_text: str, ref_image_data: list) -> bool:
    """Return True if venue DNA extraction is warranted."""
    lower = prompt_text.lower()
    if any(kw in lower for kw in _VENUE_TRIGGER_KEYWORDS):
        return True
    for img in ref_image_data:
        desc = (
            img.get("description_variant_regular") or
            img.get("analysis") or
            img.get("description") or ""
        ).lower()
        if any(kw in desc for kw in _VENUE_TRIGGER_KEYWORDS):
            return True
    return False


def _extract_venue_dna(
    processor,
    prompt_text: str,
    ref_image_data: list,
    row_num: int = None,
) -> str:
    """Extract a locked venue visual DNA from reference images + prompt text.

    Runs one LLM call (text-only, low-cost) to distill a consistent environment
    description from the already-analyzed reference image descriptions and the
    free-text prompt.  The result is injected verbatim into every scene-generation
    call so all indoor scenes look like the same location.

    Returns:
        Venue DNA string (1 paragraph), or empty string if no venue context found.
    """
    if not _should_extract_venue_dna(prompt_text, ref_image_data):
        return ""

    loader = get_prompt_loader()

    image_desc_lines = []
    for i, img in enumerate(ref_image_data):
        desc = (
            img.get("description_variant_regular") or
            img.get("analysis") or
            img.get("description") or ""
        )
        if desc:
            image_desc_lines.append(f"Image {i + 1}: {desc[:400]}")
    image_descriptions = "\n".join(image_desc_lines) if image_desc_lines else "No image descriptions available."

    system_prompt = loader.get("shared_venue_dna_system")
    user_prompt = loader.get(
        "shared_venue_dna_user",
        prompt_text=prompt_text[:1500],
        image_descriptions=image_descriptions,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response_schema = {
        "type": "object",
        "properties": {"venue_dna": {"type": "string"}},
        "required": ["venue_dna"],
    }

    try:
        processor.reset_usage()
        raw = processor._call_llm(
            "extract_venue_dna",
            messages,
            response_schema=response_schema,
        )
        if not raw:
            return ""
        if isinstance(raw, str):
            import json as _json
            parsed = _json.loads(raw)
        else:
            parsed = raw
        dna = parsed.get("venue_dna", "").strip()
        if dna:
            logger.info(f"   [Row {row_num}] Venue DNA extracted: {dna[:100]}...")
        return dna
    except Exception as _e:
        logger.warning(f"   [Row {row_num}] Venue DNA extraction failed: {_e}")
        return ""


def _analyze_venue_and_influencer(processor, venue_url: str, influencer_url: str, row_num: int = None) -> Optional[Dict[str, str]]:
    """Joint analysis of venue + influencer images for NB2 compositing.

    Sends both images to the LLM so the NB2 prompt can describe which image
    is the venue and which is the person. This helps NB2 composite correctly
    instead of blending both venue perspectives.

    Returns:
        Dict with 'venue_description' and 'influencer_description', or None on failure.
    """
    import base64 as _b64

    _p_defaults = get_pipeline_defaults()
    max_dim = _p_defaults.get("max_image_dimension", 1024)
    loader = get_prompt_loader()

    system_prompt = loader.get("shared_venue_influencer_analysis_system")
    user_prompt_text = loader.get("shared_venue_influencer_analysis_user")

    user_parts = []
    for label, url in [("venue", venue_url), ("influencer", influencer_url)]:
        clean_url = url.strip()
        image_part = None
        try:
            if clean_url.startswith("gs://"):
                image_part = {"type": "image_url", "image_url": {"url": clean_url}}
            else:
                img_fetch_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "image/*,*/*;q=0.8",
                }
                img_response = requests.get(clean_url, headers=img_fetch_headers, timeout=15)
                if img_response.status_code == 200:
                    compressed_bytes, mime = _compress_image_for_analysis(img_response.content, max_dim)
                    img_b64 = _b64.b64encode(compressed_bytes).decode("utf-8")
                    image_part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}
        except Exception as fetch_err:
            logger.warning(f"   [Row {row_num}] Could not fetch {label} image for venue+influencer analysis: {fetch_err}")

        if image_part:
            user_parts.append(image_part)

    if len(user_parts) < 2:
        logger.warning(f"   [Row {row_num}] Venue+influencer analysis: could not fetch both images")
        return None

    user_parts.append({"type": "text", "text": user_prompt_text})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_parts},
    ]

    response_schema = {
        "type": "object",
        "properties": {
            "venue_description": {"type": "string"},
            "influencer_description": {"type": "string"},
        },
        "required": ["venue_description", "influencer_description"],
    }

    processor.reset_usage()
    raw = processor._call_llm("analyze_venue_and_influencer", messages, response_schema=response_schema)
    if not raw:
        return None

    try:
        if isinstance(raw, str):
            parsed = json.loads(raw)
        else:
            parsed = raw
        if "venue_description" in parsed and "influencer_description" in parsed:
            return parsed
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"   [Row {row_num}] Venue+influencer analysis: could not parse response")

    return None


def _analyze_reference_images(processor, image_urls: List[str]) -> List[Dict[str, str]]:
    """Analyze ALL reference images in a single batched LLM call.

    Sends all images (compressed to max 1024px JPEG) as base64 parts in one
    Gemini call with responseSchema for structured JSON output.

    Args:
        processor: VideoSceneProcessor instance.
        image_urls: List of reference image URLs to analyze.

    Returns:
        List of dicts with 'url', 'index', 'type', and 'description' for each image.
    """
    import base64 as _b64

    _p_defaults = get_pipeline_defaults()
    max_images = _p_defaults.get("max_reference_images", 50)
    max_dim = _p_defaults.get("max_image_dimension", 1024)
    loader = get_prompt_loader()

    if len(image_urls) > max_images:
        dropped = list(range(max_images, len(image_urls)))
        logger.warning(f"   Capped at {max_images} reference images: dropped indices {dropped}")
        image_urls = image_urls[:max_images]

    call_fn = lambda msgs, **kw: processor._call_llm("analyze_reference_images", msgs, **kw)
    system_prompt = loader.get("shared_image_analysis_system")

    # Build multi-image user content
    user_parts = []
    url_map = {}  # index -> original url
    for i, url in enumerate(image_urls):
        clean_url = url.strip()
        image_part = None
        try:
            if clean_url.startswith("gs://"):
                image_part = {"type": "image_url", "image_url": {"url": clean_url}}
            else:
                img_fetch_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "image/*,*/*;q=0.8",
                }
                img_response = requests.get(clean_url, headers=img_fetch_headers, timeout=15)
                if img_response.status_code == 200:
                    compressed_bytes, mime = _compress_image_for_analysis(img_response.content, max_dim)
                    img_b64 = _b64.b64encode(compressed_bytes).decode("utf-8")
                    image_part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}
        except Exception as fetch_err:
            logger.warning(f"   Could not fetch image {i+1} for analysis: {fetch_err}")

        if image_part:
            user_parts.append(image_part)
            url_map[i] = url
        else:
            logger.warning(f"   Could not build image part for image {i+1}")

    if not user_parts:
        return [{"url": url, "index": i, "type": "OTHER", "uniqueness": "medium", "uniqueness_reason": "", "surprise_candidate": False} for i, url in enumerate(image_urls)]

    user_parts.append({"type": "text", "text": f"Analyze all {len(user_parts)} images above. Return a JSON object with an 'images' array."})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_parts},
    ]

    try:
        llm_result = call_fn(messages, temperature=0.3, responseSchema=IMAGE_ANALYSIS_SCHEMA)
        parsed = json.loads(llm_result.get("text", "{}"))
        llm_images = parsed.get("images", [])
    except Exception as e:
        logger.warning(f"   Batched image analysis failed: {e} ????? returning placeholders")
        return [{"url": url, "index": i, "type": "OTHER", "uniqueness": "medium", "uniqueness_reason": "", "surprise_candidate": False} for i, url in enumerate(image_urls)]

    # Build result aligned with original image_urls order
    analyzed = []
    llm_by_index = {img["index"]: img for img in llm_images if isinstance(img.get("index"), int)}
    for i, url in enumerate(image_urls):
        if i in llm_by_index:
            item = llm_by_index[i]
            uniq = item.get("uniqueness", "medium")
            analyzed.append({
                "url": url, "index": i,
                "type": item.get("type", "OTHER"),
                "uniqueness": uniq,
                "uniqueness_reason": item.get("uniqueness_reason", ""),
                "surprise_candidate": item.get("surprise_candidate", False),
                "venue_candidate": item.get("venue_candidate", False),
            })
            logger.info(f"   Image {i+1} [{uniq}]: type={item.get('type', 'OTHER')}"
                        f"{' [surprise_candidate]' if item.get('surprise_candidate') else ''}"
                        f"{' [venue_candidate]' if item.get('venue_candidate') else ''}")
        else:
            analyzed.append({"url": url, "index": i, "type": "OTHER", "uniqueness": "medium", "uniqueness_reason": ""})
            logger.warning(f"   Image {i+1}: not in LLM response, using placeholder")

    return analyzed


MOTION_VARIANTS_SCHEMA = {
    "type": "object",
    "properties": {
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "description_variant_regular": {"type": "string"},
                    "motion_prompt_regular": {"type": "string"},
                    "description_variant_surprise": {"type": "string", "nullable": True},
                    "motion_prompt_surprise": {"type": "string", "nullable": True},
                    "description_variant_venue": {"type": "string", "nullable": True},
                    "motion_prompt_venue": {"type": "string", "nullable": True},
                },
                "required": ["index", "description_variant_regular", "motion_prompt_regular",
                             "description_variant_surprise", "motion_prompt_surprise",
                             "description_variant_venue", "motion_prompt_venue"],
            },
        },
    },
    "required": ["images"],
}


def _analyze_motion_variants(processor, image_urls: List[str], candidate_indices: List[int] = None, venue_candidate_indices: List[int] = None, gender: str = "f") -> Dict[int, Dict]:
    """Analyze images for motion variants (regular + surprise + venue) in a single LLM call.

    Args:
        processor: VideoSceneProcessor instance.
        image_urls: List of reference image URLs.
        candidate_indices: 0-based indices of images pre-approved as surprise candidates
                          by the image analysis LLM. Only these get surprise prompts.
        venue_candidate_indices: 0-based indices of images flagged as venue candidates.
                                Only these get venue variant prompts.

    Returns dict keyed by image index with variant data.
    """
    import base64 as _b64

    _p_defaults = get_pipeline_defaults()
    max_images = _p_defaults.get("max_reference_images", 50)
    max_dim = _p_defaults.get("max_image_dimension", 1024)
    loader = get_prompt_loader()

    if len(image_urls) > max_images:
        image_urls = image_urls[:max_images]

    system_prompt = loader.get("shared_motion_variants_system")

    # Build multi-image user content (same pattern as _analyze_reference_images)
    user_parts = []
    for i, url in enumerate(image_urls):
        clean_url = url.strip()
        image_part = None
        try:
            if clean_url.startswith("gs://"):
                image_part = {"type": "image_url", "image_url": {"url": clean_url}}
            else:
                img_fetch_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "image/*,*/*;q=0.8",
                }
                img_response = requests.get(clean_url, headers=img_fetch_headers, timeout=15)
                if img_response.status_code == 200:
                    compressed_bytes, mime = _compress_image_for_analysis(img_response.content, max_dim)
                    img_b64 = _b64.b64encode(compressed_bytes).decode("utf-8")
                    image_part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}
        except Exception as fetch_err:
            logger.warning(f"   Motion variants: could not fetch image {i+1}: {fetch_err}")

        if image_part:
            user_parts.append(image_part)

    if not user_parts:
        return {}

    # Format candidate indices for the prompt (e.g. "1, 3, 5" or "none")
    if candidate_indices:
        _ci_str = ", ".join(str(i) for i in sorted(candidate_indices))
    else:
        _ci_str = "none (no images are surprise candidates)"
    if venue_candidate_indices:
        _vi_str = ", ".join(str(i) for i in sorted(venue_candidate_indices))
    else:
        _vi_str = "none (no images are venue candidates)"
    _gender_word = "female" if gender == "f" else "male"
    _gender_pronoun = "she" if gender == "f" else "he"
    _gender_possessive = "her" if gender == "f" else "his"
    user_text = loader.get("shared_motion_variants_user", count=len(user_parts), candidate_indices=_ci_str, venue_candidate_indices=_vi_str, gender_word=_gender_word, gender_pronoun=_gender_pronoun, gender_possessive=_gender_possessive)
    user_parts.append({"type": "text", "text": user_text})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_parts},
    ]

    llm_result = processor._call_llm(
        "analyze_motion_variants", messages,
        temperature=0.7,
        responseSchema=MOTION_VARIANTS_SCHEMA,
    )
    parsed = json.loads(llm_result.get("text", "{}"))
    llm_images = parsed.get("images", [])

    # Detect and fix 1-based indexing (LLM sometimes returns 1-7 instead of 0-6)
    if llm_images and all(isinstance(img.get("index"), int) for img in llm_images):
        min_idx = min(img["index"] for img in llm_images)
        if min_idx == 1:
            logger.info("   Motion variants: detected 1-based indexing, normalizing to 0-based")
            for img in llm_images:
                img["index"] -= 1

    result = {}
    for item in llm_images:
        idx = item.get("index")
        if isinstance(idx, int):
            result[idx] = {
                "description_variant_regular": item.get("description_variant_regular", ""),
                "motion_prompt_regular": item.get("motion_prompt_regular", "Subtle slow zoom in, very slight movement"),
                "description_variant_surprise": item.get("description_variant_surprise"),
                "motion_prompt_surprise": item.get("motion_prompt_surprise"),
                "description_variant_venue": item.get("description_variant_venue"),
                "motion_prompt_venue": item.get("motion_prompt_venue"),
            }

    _n_surprise = sum(1 for v in result.values() if v.get('motion_prompt_surprise'))
    _n_venue = sum(1 for v in result.values() if v.get('motion_prompt_venue'))
    logger.info(f"   Motion variants: {len(result)} images analyzed, "
                f"{_n_surprise} with surprise, {_n_venue} with venue")
    return result


def _validate_scene_media_assignments(scene_prompts, asset_descriptions, image_descriptions):
    """Check for ALL media assignment violations. Returns list of violation strings, empty if clean."""
    violations = []
    n_assets = len(asset_descriptions)
    n_images = len(image_descriptions)

    for sp in scene_prompts:
        scene_num = sp.get("scene_number", sp.get("scene_num", "?"))
        vid_idx = sp.get("video_asset_index")
        moment_idx = sp.get("best_moment_index")
        img_idx = sp.get("reference_image_index")

        if vid_idx is not None:
            if not isinstance(vid_idx, int) or vid_idx < 0 or vid_idx >= n_assets:
                violations.append(f"Scene {scene_num}: video_asset_index={vid_idx} invalid (valid: 0-{n_assets-1} or null).")
            elif moment_idx is not None and moment_idx != -1:
                n_moments = len(asset_descriptions[vid_idx].get("key_moments", []))
                if not isinstance(moment_idx, int) or moment_idx < 0 or moment_idx >= n_moments:
                    violations.append(f"Scene {scene_num}: best_moment_index={moment_idx} invalid for video {vid_idx} ({n_moments} moments, valid: -1 or 0-{n_moments-1}).")

        if img_idx is not None:
            if not isinstance(img_idx, int) or img_idx < 0 or img_idx >= n_images:
                violations.append(f"Scene {scene_num}: reference_image_index={img_idx} invalid (valid: 0-{n_images-1} or null).")

        if vid_idx is not None and img_idx is not None:
            violations.append(f"Scene {scene_num}: has both video ({vid_idx}) and image ({img_idx}). Pick one.")
        if vid_idx is None and moment_idx is not None:
            violations.append(f"Scene {scene_num}: video=null but moment={moment_idx}. Set moment to null.")

    # Duplicate detection
    image_assignments = {}
    for sp in scene_prompts:
        img_idx = sp.get("reference_image_index")
        if img_idx is not None and isinstance(img_idx, int):
            image_assignments.setdefault(img_idx, []).append(sp.get("scene_number", sp.get("scene_num", "?")))
    for idx, scenes_list in image_assignments.items():
        if len(scenes_list) > 1:
            violations.append(f"Image {idx} used in scenes {scenes_list}. Each image may appear in at most ONE scene.")

    video_moment_pairs = {}
    for sp in scene_prompts:
        vid_idx = sp.get("video_asset_index")
        moment_idx = sp.get("best_moment_index")
        if vid_idx is not None and isinstance(vid_idx, int):
            pair = (vid_idx, moment_idx)
            video_moment_pairs.setdefault(pair, []).append(sp.get("scene_number", sp.get("scene_num", "?")))
    for (vid_idx, moment_idx), scenes_list in video_moment_pairs.items():
        if len(scenes_list) > 1:
            violations.append(f"Video {vid_idx} moment {moment_idx} used in scenes {scenes_list}. Each (video, moment) pair must be unique.")

    return violations


def _force_fix_scene_assignments(scene_prompts, asset_descriptions, image_descriptions):
    """Last-resort fallback: fix violations by nullifying duplicates and invalid indices."""
    seen_pairs = set()
    seen_images = set()
    n_assets = len(asset_descriptions)
    n_images = len(image_descriptions)
    for sp in scene_prompts:
        vid_idx = sp.get("video_asset_index")
        moment_idx = sp.get("best_moment_index")
        img_idx = sp.get("reference_image_index")

        if vid_idx is not None:
            if not isinstance(vid_idx, int) or vid_idx < 0 or vid_idx >= n_assets:
                sp["video_asset_index"] = None
                sp["best_moment_index"] = None
                continue
            if moment_idx is not None and moment_idx != -1:
                n_moments = len(asset_descriptions[vid_idx].get("key_moments", []))
                if not isinstance(moment_idx, int) or moment_idx < 0 or moment_idx >= n_moments:
                    sp["best_moment_index"] = -1
            pair = (sp["video_asset_index"], sp["best_moment_index"])
            if pair in seen_pairs:
                sp["video_asset_index"] = None
                sp["best_moment_index"] = None
            else:
                seen_pairs.add(pair)

        if img_idx is not None:
            if not isinstance(img_idx, int) or img_idx < 0 or img_idx >= n_images:
                sp["reference_image_index"] = None
            elif img_idx in seen_images:
                sp["reference_image_index"] = None
            else:
                seen_images.add(img_idx)
    return scene_prompts


def _insert_asset_as_scene(
    processor,
    asset_url: str,
    target_duration: float = 4.0
) -> Optional[str]:
    """Process an asset (image or video) for insertion as-is into the video.
    
    For images: Creates a video with subtle Ken Burns effect (slow zoom)
    For videos: Trims if needed, otherwise passes through
    
    Args:
        asset_url: URL to the asset (image or video)
        target_duration: Target duration in seconds
        
    Returns:
        URL to the processed video ready for concatenation, or None on failure
    """
    try:
        logger.info(f"   Processing asset: {asset_url[:60]}...")
        
        # Determine if it's an image or video based on URL
        is_image = any(ext in asset_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp'])
        is_video = any(ext in asset_url.lower() for ext in ['.mp4', '.mov', '.avi', '.webm', '.mkv'])
        
        if is_image:
            # For images: Create a video with subtle zoom effect
            logger.info(f"   Asset is an image, creating video with Ken Burns effect...")
            
            # Generate a subtle motion video from the image
            # Use Runway or Kling with minimal motion prompt
            motion_prompt = "Subtle slow zoom in, very slight movement, professional Ken Burns style"
            
            try:
                # Try using Runway for the Ken Burns effect
                video_url = processor.kie_service.generate_video_runway(
                    prompt=motion_prompt,
                    image_url=asset_url,
                    duration=target_duration
                )
                if video_url:
                    logger.info(f"   Asset image converted to video with zoom")
                    return video_url
            except Exception as e:
                logger.warning(f"   Could not animate asset image: {e}")
            
            # Fallback: If animation fails, try to just use the image as-is
            # (would need a different approach like ffmpeg to create a static video)
            logger.warning(f"   Using asset image as-is (no animation)")
            return None
            
        elif is_video:
            # For videos: Trim to target duration if longer
            logger.info(f"   Asset is a video, processing...")
            
            try:
                # Use Rendi to trim the video to target duration
                trimmed_url = processor.rendi_service.trim_video(
                    video_url=asset_url,
                    duration=target_duration
                )
                if trimmed_url:
                    logger.info(f"   Asset video trimmed to {target_duration}s")
                    return trimmed_url
                else:
                    # If trimming fails, return original
                    logger.info(f"   Using asset video as-is")
                    return asset_url
            except Exception as e:
                logger.warning(f"   Could not trim asset video: {e}")
                return asset_url  # Return original on error
        else:
            # Unknown format, try to process as video
            logger.warning(f"   Unknown asset format, treating as video")
            return asset_url
            
    except Exception as e:
        logger.error(f"   Error processing asset: {e}")
        return None



# ============================================================================
# MAIN UGC PIPELINE
# ============================================================================

def process_ugc_video(
    processor,
    row_num: int = None,
    row_data: List[str] = None,
    headers: List[str] = None,
    prompt: str = "",
    gender: str = "f",
    reference_images: List[str] = None,
    image_explanations: List[Optional[str]] = None,
    image_explain_cols: List[Optional[int]] = None,
    assets: List[str] = None,
    character_urls: List[str] = None,
    text_1_col: int = None,
    text_2_col: int = None,
    text_3_col: int = None,
    text_4_col: int = None,
    vo_script_col: int = None,
    animation_model: str = "auto",
    visual_style: str = "Auto",
    target_duration: int = 30,
    logo_url: str = None,
    slogan_text: str = None,
    add_subtitles: bool = True,
    subtitle_language: str = "en",
    character_col: int = None,
    country: str = "",
    video_subtype: str = "influencer",
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
    product_image_mode: str = "none",
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
    reference_image_urls: List[str] = None,
    asset_urls: list = None,
    video_reference_url: str = None,
    generate_vo: bool = True,
    vo_script_only: bool = False,
    run_only_parse_prompt: bool = False,
    skip_character_and_analyze_media: bool = False,
    generate_assets: bool = True,
    enrich_cta_with_influencer: bool = False,
    simulation: bool = False,
    # --- Smart asset + film grain params (API-only, defaults from pipeline_defaults.json) ---
    asset_mode: str = "smart",
    vo_duration_hints: bool = False,
    film_grain: bool = True,
    business_category: str = "general",
    highlights: List[str] = None,
    min_influencer_clip_ratio: float = None,
    max_influencer_clip_ratio: float = None,
    surprise_mode=None,
    # --- Subtitle enrichment ---
    subtitle_emoji: bool = True,
    subtitle_position: str = "middle",
    # --- End card params (influencer only, API-only) ---
    business_name: str = None,
    business_address: str = None,
    business_phone: str = None,
    business_website: str = None,
    end_card_color: str = "white",
    end_card_detail_color: str = "white",
    end_card_position: str = "middle",
    vertical_option: str = None,
    remove_character_bg: bool = False,
    product_location: str = None,
    generate_extended: bool = False,
    # --- Video Studio / API: user-edited TEXT 1?4 from Preferences (input_translator keys text_1..4) ---
    text_1: Optional[str] = None,
    text_2: Optional[str] = None,
    text_3: Optional[str] = None,
    text_4: Optional[str] = None,
) -> Dict[str, Any]:
    """Process a UGC-style video row - influencer or personal brand style.

    Note: video_reference_url is accepted but ignored by UGC pipelines.

    This method implements the UGC-style video creation workflow:
    1. Check for Character image(s), generate one influencer if missing
    2. Parse prompt into TEXT 1-4 
    3. Generate scene prompts with influencer logic
    4. Generate images for each scene (use reference images if provided)
    5. Handle assets (insert as-is with subtle zoom)
    6. Generate animations for each scene
    7. Generate background music using Suno
    8. Generate voice over using ElevenLabs (first-person, authentic style)
    9. Combine everything into final video using Rendi
    10. Update Google Sheet with all asset URLs
    
    Args:
        row_num: Row number in the sheet (1-indexed)
        row_data: List of cell values for the row
        headers: List of column headers
        prompt: The product description prompt from the Prompt column
        gender: "m" for male or "f" for female influencer
        reference_images: Optional list of reference image URLs (Image 1-5)
        assets: Optional list of asset URLs (Asset 1-3) to insert as-is
        character_urls: Optional list of influencer/character image URLs (supports multiple people)
        text_1_col: Column index for TEXT 1
        text_2_col: Column index for TEXT 2
        text_3_col: Column index for TEXT 3
        text_4_col: Column index for TEXT 4
        vo_script_col: Column index for VO Script
        animation_model: "runway", "kling", or "google" for video generation
        visual_style: Visual style or "Auto" for ultra-realistic UGC style
        target_duration: Target video duration in seconds (API/Studio typically 10–120; VO word budget scales with this)
        logo_url: Optional URL to logo for ending/CTA scene
        slogan_text: Optional slogan text for CTA scene
        add_subtitles: Whether to add ZapCap subtitles
        subtitle_language: Language code for subtitles
        character_col: Column index for Character column (to save generated influencer)
        
    Returns:
        Dict with processing results including all generated asset URLs
    """
    # Handle param aliases
    reference_images = reference_images or reference_image_urls
    assets = assets or asset_urls
    subtitle_language = language or subtitle_language

    _p_defaults = get_pipeline_defaults()
    image_quality_threshold = _p_defaults.get("image_quality_threshold", 5)

    # Resolve surprise_mode: int >= 1, "all", "none"
    _surprise_mode = surprise_mode if surprise_mode is not None else _p_defaults.get("surprise_mode", 2)
    if isinstance(_surprise_mode, str) and _surprise_mode.isdigit():
        _surprise_mode = int(_surprise_mode)
    if isinstance(_surprise_mode, int) and _surprise_mode < 1:
        _surprise_mode = "none"

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

    if video_reference_url:
        logger.warning(f"[Row {row_num}] video_reference_url provided but ignored by UGC pipelines")

    logger.info(f"?????? [Row {row_num}] Processing UGC-style video...")
    logger.info(f"   Prompt: {prompt[:100]}..." if len(prompt) > 100 else f"   Prompt: {prompt}")
    logger.info(f"   Gender: {'Female' if gender == 'f' else 'Male'}")
    logger.info(f"   target_duration={target_duration}s  video_subtype={video_subtype}  video_model={video_model}")

    result = {
        "row": row_num,
        "video_type": "personal-brand" if video_subtype == "personal_brand" else "influencer",
        "success": False,
        "parsed_texts": {},
        "influencer_image": None,
        "scene_prompts": [],
        "scene_images": [],
        "scene_videos": [],
        "asset_videos": [],
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
    
    # Filter out empty reference image URLs
    valid_ref_images = [url for url in (reference_images or []) if url and url.strip()]
    if valid_ref_images:
        logger.info(f"   Including {len(valid_ref_images)} reference images")
    
    # Filter out empty asset URLs ????? handle both string and dict formats
    # (API sends dicts with {"url": ..., "type": ..., "keep_audio": ...}, Sheets sends plain strings)
    # Preserve metadata (type, keep_audio) as dicts for downstream use
    _raw_assets = assets or []
    valid_assets = []
    for _a in _raw_assets:
        if isinstance(_a, dict):
            _url = (_a.get("url") or "").strip()
            if _url:
                valid_assets.append({"url": _url, "type": _a.get("type"), "keep_audio": bool(_a.get("keep_audio", False))})
        elif isinstance(_a, str) and _a.strip():
            valid_assets.append({"url": _a.strip(), "type": None, "keep_audio": False})
    if valid_assets:
        logger.info(f"   Including {len(valid_assets)} assets to insert as-is")
    
    # Select voice ID: provided param ????? Voice id column ????? random from catalog ????? default
    if voice_id:
        logger.info(f"Using provided voice_id: {voice_id}")
    else:
        voice_id = None
        try:
            voice_id_col = processor._get_col_safe(headers, config.VOICE_ID_COLUMN) if headers else None
            if voice_id_col is not None and row_data and voice_id_col < len(row_data):
                sheet_voice = row_data[voice_id_col].strip()
                if sheet_voice:  # If Voice id column has any value, use it directly (no validation)
                    voice_id = sheet_voice
                    logger.info(f"   Using Voice ID from sheet: {voice_id}")
        except (ValueError, Exception):
            pass

        if not voice_id:
            from tvd_pipeline.data_loader import get_language_voice
            lang_voice = get_language_voice(subtitle_language, "female" if gender == "f" else "male")
            if lang_voice:
                voice_id = lang_voice
                logger.info(f"   Using language-specific voice: {voice_id}")

        if not voice_id:
            if not simulation:
                random_voice = processor.elevenlabs_service.pick_random_voice(
                    gender="female" if gender == "f" else "male",
                    language=subtitle_language,
                    country=country
                )
                if random_voice:
                    voice_id = random_voice
            if not voice_id:
                if gender == "f":
                    voice_id = config.DEFAULT_FEMALE_VOICE_ID
                    logger.info(f"   Fallback to default female voice: {voice_id}")
                else:
                    voice_id = config.DEFAULT_VOICE_ID
                    logger.info(f"   Fallback to default male voice: {voice_id}")

    # Calibrate voice WPS for accurate VO word count targeting (optional: skip to save ~10-30s)
    # Skip when: simulation, no VO needed, intermediates have cached VO, or sheet has existing VO script
    calibrated_wps = None
    _has_cached_vo = ("vo_script" in intermediates and "vo_audio_url" in intermediates)
    _has_sheet_vo = (vo_script_col is not None and row_data and vo_script_col < len(row_data)
                     and row_data[vo_script_col].strip())
    _skip_calibration = _p_defaults.get("skip_tts_calibration", False)
    if not simulation and generate_vo and voice_id and not _has_cached_vo and not _has_sheet_vo:
        if _skip_calibration:
            calibrated_wps = get_speech_rate(subtitle_language)
            logger.info(f"   Using default WPS (skip_tts_calibration=true): {calibrated_wps:.2f}")
        else:
            from tvd_pipeline.data_loader import prepare_wps_sample_text
            sample = prepare_wps_sample_text(prompt)
            calibrated_wps = processor.elevenlabs_service.calibrate_voice_wps(
                sample_text=sample, voice_id=voice_id, language=subtitle_language
            )
            if on_progress and calibrated_wps:
                on_progress("usage", {
                    "service": "elevenlabs", "step": "tts_calibration",
                    "model": get_elevenlabs_config()["tts_model"], "provider": "elevenlabs",
                    "character_count": len(sample),
                    "label": "Voice WPS calibration", "category": "tts", "success": True,
                })

    # Determine visual style - default by subtype (personal_brand = professional, influencer = UGC/Instagram)
    if visual_style == "Auto" or not visual_style:
        if video_subtype == "personal_brand":
            ugc_style = "Ultra photorealistic, professional brand style, clean modern lighting, studio quality"
        else:
            ugc_style = "Ultra photorealistic, authentic UGC style, natural lighting, shot on iPhone"
    else:
        ugc_style = visual_style
    logger.info(f"   Visual style: {ugc_style[:50]}...")
    
    # Image API: "Google" = Vertex Gemini; "kie flash" = Kie Gemini 3 Flash; "kie" or empty = Kie (Nano Banana)
    image_api_col = processor._get_col_safe(headers, config.IMAGE_API_COLUMN) if headers else None
    image_api_val = (row_data[image_api_col].strip().lower() if image_api_col is not None and row_data and image_api_col < len(row_data) else "")
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
        logger.info(f"   Image API: Vertex Gemini")
    elif use_kie_flash:
        logger.info(f"   Image API: Kie Flash (Gemini 3 Flash)")
    else:
        logger.info(f"   Image API: Kie (Nano Banana)")

    # When Phase 1 or Phase 2 (seed + pause after VO): skip character + analyze_media for speed.
    if run_only_parse_prompt or skip_character_and_analyze_media:
        ref_image_analyses = []
        asset_analyses = []
        _smart_mode = (asset_mode == "smart" and video_subtype == "influencer")
        # So downstream (e.g. scene prompts) don't get NameError when we skip step 0
        influencer_urls = list(character_urls) if character_urls else []
        influencer_url = influencer_urls[0] if influencer_urls else None
        influencer_description = intermediates.get("character_description") or ""
        # Studio Phase 1 uses run_only_parse_prompt (no describe step). Phase 2 skips media analysis
        # but still needs a character description for VO + scene prompts. Fill it here if missing.
        if (
            skip_character_and_analyze_media
            and video_subtype in ("influencer", "personal_brand")
            and influencer_urls
            and not (influencer_description or "").strip()
            and not simulation
        ):
            try:
                if on_progress:
                    on_progress("step_start", {
                        "step": "character_description",
                        "label": "Character Description",
                        "message": "Describing influencer for voiceover (Studio Phase 2)...",
                    })
                processor.reset_usage()
                influencer_description = describe_characters(
                    lambda msgs, **kw: processor._call_llm("describe_character", msgs, **kw),
                    image_urls=influencer_urls,
                ) or ""
                if influencer_description:
                    logger.info(
                        f"   [Row {row_num}] Phase 2: filled character_description from upload ({len(influencer_description)} chars)"
                    )
                    intermediates["character_description"] = influencer_description
                    if on_progress:
                        on_progress("intermediate", {
                            "key": "character_description",
                            "value": influencer_description,
                        })
                        on_progress("step_complete", {
                            "step": "character_description",
                            "label": "Character Description",
                            "progress": 4,
                            "message": "Influencer described (Phase 2)",
                        })
                        emit_llm_usage_events(processor, on_progress, usage_list, "character_description")
            except Exception as _desc_err:
                logger.warning(
                    f"   [Row {row_num}] Phase 2 describe_characters failed (continuing without): {_desc_err}"
                )
                influencer_description = intermediates.get("character_description") or ""
    if not run_only_parse_prompt and not skip_character_and_analyze_media:
        # =====================================================================
        # STEP 0: Generate influencer image if Character column is empty; else use provided URL(s)
        # =====================================================================
        if on_progress:
            on_progress("step_start", {
                "step": "character_description",
                "label": "Character Description",
                "message": "Generating character description...",
            })
        influencer_urls = list(character_urls) if character_urls else []
        influencer_url = influencer_urls[0] if influencer_urls else None  # primary URL (for saving when generated)
        influencer_description = None

        if "character_description" in intermediates and intermediates["character_description"]:
            influencer_description = intermediates["character_description"]
            logger.info("Using cached character_description from existing_intermediates")
        elif character_description:
            influencer_description = character_description
            logger.info("Using provided character_description, skipping AI analysis")
        elif not influencer_urls and simulation:
            influencer_url = _SIM_IMAGE
            influencer_urls = [influencer_url]
            influencer_description = "A person with professional appearance, well-groomed, wearing casual attire"
            result["influencer_image"] = influencer_url
            logger.info(f"   [Row {row_num}] [SIM] Influencer image generated")
        elif not influencer_urls:
            logger.info(f"   [Row {row_num}] Step 0: Generating influencer image...")
            try:
                influencer_url, influencer_description, _portrait_prompt_unused = _generate_influencer_image(processor, 
                    gender=gender,
                    product_context=prompt[:500],
                    visual_style=ugc_style,
                    country=country,
                    language=subtitle_language,
                    video_subtype=video_subtype
                )
                if influencer_url:
                    influencer_urls = [influencer_url]
                    result["influencer_image"] = influencer_url
                    logger.info(f"   [Row {row_num}] Influencer image generated: {influencer_url[:60]}...")

                    # Save to Character column
                    if character_col is not None:
                        try:
                            processor.sheets_service.update_cell(
                                config.GOOGLE_SHEET_ID,
                                config.GOOGLE_SHEET_TAB,
                                row_num,
                                config.CHARACTER_COLUMN,
                                influencer_url,
                                headers
                            )
                            logger.info(f"   [Row {row_num}] Saved influencer to Character column")
                        except Exception as e:
                            logger.warning(f"   [Row {row_num}] Could not save influencer to Character: {e}")
                else:
                    logger.warning(f"   [Row {row_num}] Could not generate influencer image")
            except Exception as e:
                logger.warning(f"   [Row {row_num}] Error generating influencer: {e}")
        else:
            logger.info(f"   [Row {row_num}] Using existing influencer(s) from Character column ({len(influencer_urls)} image(s))")
            try:
                if simulation:
                    influencer_description = "A person with professional appearance, well-groomed, wearing casual attire"
                    logger.info(f"   [Row {row_num}] [SIM] Influencer(s) described")
                else:
                    processor.reset_usage()
                    influencer_description = describe_characters(
                        lambda msgs, **kw: processor._call_llm("describe_character", msgs, **kw),
                        image_urls=influencer_urls,
                    )
                    if influencer_description:
                        logger.info(f"   [Row {row_num}] Influencer(s) described: {influencer_description[:100]}...")
            except Exception as e:
                logger.warning(f"   [Row {row_num}] Could not describe influencer(s): {e}")

        # --- Callback: character_description step_complete + intermediate + usage ---
        if influencer_description:
            if on_progress:
                on_progress("step_complete", {
                    "step": "character_description",
                    "label": "Character Description",
                    "progress": 1,
                    "message": "Character described",
                })
                on_progress("intermediate", {"key": "character_description", "value": influencer_description})
                emit_llm_usage_events(processor, on_progress, usage_list, "character_description")

        # =====================================================================
        # STEP 0.3: Background removal (optional, influencer only)
        # Removes the character image background before NB2 compositing so
        # only the person (not their original background) is composited.
        # =====================================================================
        if remove_character_bg and influencer_urls and not simulation and processor.fal_service:
            logger.info(f"   [Row {row_num}] Removing background from {len(influencer_urls)} character image(s)...")
            bg_removed_urls = []
            for _bg_i, _bg_url in enumerate(influencer_urls):
                try:
                    removed_url = processor.fal_service.remove_background(_bg_url)
                    if removed_url:
                        bg_removed_urls.append(removed_url)
                        logger.info(f"   [Row {row_num}] Character {_bg_i}: background removed")
                        if on_progress:
                            _fal_bg_cfg = get_fal_config().get("background_removal", {})
                            usage_data = {
                                "service": "fal", "step": f"background_removal_{_bg_i}",
                                "model": _fal_bg_cfg.get("endpoint", "fal-ai/birefnet"),
                                "provider": "fal", "count": 1,
                                "label": f"Background removal character {_bg_i}",
                                "category": "images", "success": True,
                            }
                            on_progress("usage", usage_data)
                            usage_list.append(usage_data)
                    else:
                        bg_removed_urls.append(_bg_url)  # fallback to original
                        logger.warning(f"   [Row {row_num}] Character {_bg_i}: background removal failed, using original")
                except Exception as _bg_err:
                    bg_removed_urls.append(_bg_url)  # fallback to original
                    logger.warning(f"   [Row {row_num}] Character {_bg_i}: background removal error ({_bg_err}), using original")
            influencer_urls = bg_removed_urls
            if on_progress:
                on_progress("intermediate", {"key": "bg_removed_character_urls", "value": influencer_urls})

        # =====================================================================
        # STEP 0.5: Analyze all media (images + videos) ????? IN PARALLEL
        # Both analyzed FIRST, before parse prompt. Descriptions flow downstream
        # as text ????? no base64 images sent to parse prompt or VO LLM.
        # =====================================================================
        if on_progress:
            on_progress("step_start", {
                "step": "analyze_media",
                "label": "Analyze Media",
                "message": "Analyzing reference images and asset videos...",
            })
        _smart_mode = (asset_mode == "smart" and video_subtype == "influencer")
        ref_image_analyses = []
        asset_analyses = []

        # A1: In smart mode, move image assets into the ref images pool.
        # After this, valid_assets contains ONLY videos.
        if _smart_mode and valid_assets:
            _IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
            _video_only_assets = []
            for a in valid_assets:
                _aurl = (a["url"] if isinstance(a, dict) else a)
                if any(_aurl.lower().split("?")[0].endswith(ext) for ext in _IMAGE_EXTS):
                    valid_ref_images.append(_aurl)
                else:
                    _video_only_assets.append(a)
            valid_assets = _video_only_assets

        # --- Vertical option: preprocess reference images for portrait (9:16) ---
        _vertical_option = (vertical_option or _p_defaults.get("vertical_option", "none")).lower()

        if valid_ref_images and not simulation and _vertical_option != "none":
            _influencer_url_set = set(influencer_urls) if influencer_urls else set()

            if _vertical_option == "crop_by_code":
                from tvd_pipeline.services.tasks.smart_crop import smart_crop_for_portrait
                _crop_call_fn = lambda msgs, **kw: processor._call_llm("smart_crop", msgs, **kw)
                processor.reset_usage()
                _smart_crop_log = []
                for _sc_i, _sc_url in enumerate(valid_ref_images):
                    if _sc_url in _influencer_url_set:
                        logger.info(f"   [Row {row_num}] Smart crop image {_sc_i}: influencer image, skipping")
                        _smart_crop_log.append({"index": _sc_i, "cropped": False, "skipped_reason": "influencer_image"})
                        continue
                    crop_result = smart_crop_for_portrait(
                        _crop_call_fn, _sc_url, processor.gcs_storage_service,
                    )
                    if crop_result["cropped"]:
                        valid_ref_images[_sc_i] = crop_result["url"]
                        logger.info(f"   [Row {row_num}] Smart crop image {_sc_i}: "
                                    f"({crop_result['focus_x']:.2f}, {crop_result['focus_y']:.2f}) "
                                    f"????? {crop_result['description']}")
                    _smart_crop_log.append(crop_result)
                emit_llm_usage_events(processor, on_progress, usage_list, "smart_crop")
                if on_progress:
                    on_progress("intermediate", {"key": "smart_crop_results", "value": _smart_crop_log})

            elif _vertical_option == "crop_by_generation":
                import base64 as _b64
                from io import BytesIO
                from PIL import Image as PILImage
                _cbg_prompt = get_prompt_loader().get("shared_crop_by_generation_user")
                _cbg_check_prompt = get_prompt_loader().get("shared_crop_by_generation_check_user")
                _cbg_tolerance = _p_defaults.get("portrait_ar_tolerance", 0.05)
                _cbg_max_workers = _p_defaults.get("crop_by_generation_max_concurrent", 4)
                _cbg_max_retries = _p_defaults.get("crop_by_generation_max_retries", 3)
                _cbg_target_ar = 9 / 16  # 0.5625
                _cbg_log = []

                _CBG_HALLUCINATION_SCHEMA = {
                    "type": "object",
                    "properties": {
                        "has_major_hallucination": {
                            "type": "boolean",
                            "description": "True if the AI invented major new content not in the original",
                        },
                        "description": {
                            "type": "string",
                            "description": "What major content was hallucinated, or 'clean' if no major issues",
                        },
                    },
                    "required": ["has_major_hallucination", "description"],
                    "additionalProperties": False,
                }

                def _cbg_process_one(idx, url):
                    """Download image, check AR, regenerate in portrait if needed with hallucination check, upload to GCS."""
                    entry = {"index": idx, "original_url": url, "skipped": False, "new_url": None, "error": None, "attempts": 0, "hallucination_checks": []}
                    if url in _influencer_url_set:
                        entry["skipped"] = True
                        entry["skipped_reason"] = "influencer_image"
                        logger.info(f"   [Row {row_num}] crop_by_generation: image {idx} is influencer image, skipping")
                        return idx, entry
                    orig_bytes = None
                    try:
                        resp = requests.get(url, timeout=30)
                        resp.raise_for_status()
                        orig_bytes = resp.content
                        img = PILImage.open(BytesIO(orig_bytes))
                        w, h = img.size
                        ar = w / h if h else 999
                        entry["original_ar"] = round(ar, 4)
                        entry["original_size"] = f"{w}x{h}"
                        logger.info(f"   [Row {row_num}] crop_by_generation: image {idx} original {w}x{h} AR={ar:.3f}")
                        if ar <= _cbg_target_ar * (1 + _cbg_tolerance):
                            entry["skipped"] = True
                            logger.info(f"   [Row {row_num}] crop_by_generation: image {idx} already portrait, skipping")
                            return idx, entry
                    except Exception as e:
                        entry["error"] = f"AR check failed: {e}"
                        logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} AR check failed ({e}), attempting regeneration anyway")

                    # --- Convergence loop: NB2 regen + hallucination check ---
                    last_temp_url = None
                    last_val_img = None
                    last_new_size = None
                    for attempt in range(1, _cbg_max_retries + 1):
                        entry["attempts"] = attempt
                        try:
                            temp_url = processor.kie_service.generate_image_nano_banana(
                                _cbg_prompt,
                                reference_image_url=url,
                                aspect_ratio="9:16",
                            )
                            # Emit NB2 usage for each attempt
                            if on_progress:
                                kie_cfg = get_kie_config()
                                nb_model = kie_cfg.get("nano_banana", {}).get("model", "nano-banana-2")
                                on_progress("usage", {
                                    "service": "nano_banana",
                                    "model": nb_model,
                                    "provider": "kie",
                                    "count": 1,
                                    "label": f"crop_by_generation image {idx} attempt {attempt}",
                                    "category": "images",
                                    "success": bool(temp_url),
                                })

                            if not temp_url:
                                logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} NB2 returned None")
                                continue

                            # Validate: download and check it's a real image
                            val_resp = requests.get(temp_url, timeout=30)
                            val_resp.raise_for_status()
                            content_type = val_resp.headers.get("content-type", "")
                            if "image" not in content_type:
                                logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} NB2 returned non-image ({content_type})")
                                continue
                            val_img = PILImage.open(BytesIO(val_resp.content))
                            nw, nh = val_img.size
                            logger.info(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} NB2 output {nw}x{nh}")

                            last_temp_url = temp_url
                            last_val_img = val_img
                            last_new_size = f"{nw}x{nh}"

                            # --- Hallucination check via LLM ---
                            if orig_bytes and _cbg_check_prompt:
                                try:
                                    # Compress both images for analysis
                                    orig_compressed, orig_mime = _compress_image_for_analysis(orig_bytes, 1024)
                                    regen_buf = BytesIO()
                                    _regen_rgb = val_img.convert("RGB") if val_img.mode in ("RGBA", "P") else val_img
                                    _regen_rgb.save(regen_buf, format="JPEG", quality=80)
                                    regen_compressed = regen_buf.getvalue()

                                    orig_b64 = _b64.b64encode(orig_compressed).decode("utf-8")
                                    regen_b64 = _b64.b64encode(regen_compressed).decode("utf-8")

                                    check_messages = [{"role": "user", "content": [
                                        {"type": "text", "text": "Image 1 (ORIGINAL):"},
                                        {"type": "image_url", "image_url": {"url": f"data:{orig_mime};base64,{orig_b64}"}},
                                        {"type": "text", "text": "Image 2 (AI REGEN):"},
                                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{regen_b64}"}},
                                        {"type": "text", "text": _cbg_check_prompt},
                                    ]}]

                                    check_result = processor._call_llm(
                                        "crop_by_generation_check", check_messages,
                                        temperature=0.1, responseSchema=_CBG_HALLUCINATION_SCHEMA,
                                    )

                                    # Emit LLM usage directly from result dict (thread-safe)
                                    if on_progress and isinstance(check_result, dict):
                                        _chk_in = check_result.get("input_tokens", 0)
                                        _chk_out = check_result.get("output_tokens", 0)
                                        # Read model/provider from models.json config
                                        _chk_step_cfg = processor.model_config.get("text_defaults", {}).get("crop_by_generation_check", {})
                                        _chk_model = _chk_step_cfg.get("model", "gpt-5.4")
                                        _chk_provider = _chk_step_cfg.get("provider", "openai")
                                        on_progress("usage", {
                                            "service": _chk_provider,
                                            "model": _chk_model,
                                            "provider": _chk_provider,
                                            "input_tokens": _chk_in,
                                            "output_tokens": _chk_out,
                                            "label": f"crop_by_generation_check image {idx} attempt {attempt}",
                                            "category": "text",
                                        })

                                    # Parse hallucination check response
                                    raw_text = (check_result.get("text") or "").strip()
                                    if raw_text.startswith("```"):
                                        raw_text = raw_text.split("\n", 1)[-1]
                                        if raw_text.endswith("```"):
                                            raw_text = raw_text[:-3].strip()
                                    try:
                                        parsed = json.loads(raw_text)
                                        has_hallucination = bool(parsed.get("has_major_hallucination", False))
                                        hall_desc = parsed.get("description", "")
                                    except (json.JSONDecodeError, KeyError, TypeError):
                                        logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} hallucination check parse failed, assuming clean")
                                        has_hallucination = False
                                        hall_desc = "parse failed"

                                    entry["hallucination_checks"].append({
                                        "attempt": attempt,
                                        "has_major_hallucination": has_hallucination,
                                        "description": hall_desc,
                                    })

                                    if has_hallucination:
                                        logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} hallucination check: HALLUCINATED ????? {hall_desc}")
                                        if attempt < _cbg_max_retries:
                                            continue  # retry NB2
                                        else:
                                            logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} all {_cbg_max_retries} attempts hallucinated, using last result as fallback")
                                            # Fall through to upload last result
                                    else:
                                        logger.info(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} hallucination check: CLEAN")
                                        # Fall through to upload
                                except Exception as hall_err:
                                    logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} hallucination check error ({hall_err}), accepting result")
                                    entry["hallucination_checks"].append({"attempt": attempt, "error": str(hall_err)})

                            # Upload to GCS so URL persists
                            import uuid
                            entry["new_size"] = last_new_size
                            gcs_key = f"crop_by_generation/{uuid.uuid4().hex[:12]}_img{idx}.jpg"
                            gcs_url = processor.gcs_storage_service.upload_image_from_url(
                                temp_url, key_name=gcs_key, timeout=30,
                            )
                            if gcs_url:
                                entry["new_url"] = gcs_url
                                logger.info(f"   [Row {row_num}] crop_by_generation: image {idx} uploaded to GCS ????? {gcs_url}")
                            else:
                                entry["new_url"] = temp_url
                                logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} GCS upload failed, using temp URL")
                            return idx, entry

                        except Exception as e:
                            logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} attempt {attempt} failed ({e})")
                            if attempt == _cbg_max_retries:
                                entry["error"] = str(e)

                    # All attempts exhausted without a successful upload ????? use last result if available
                    if last_temp_url and last_val_img:
                        import uuid
                        entry["new_size"] = last_new_size
                        try:
                            gcs_key = f"crop_by_generation/{uuid.uuid4().hex[:12]}_img{idx}.jpg"
                            gcs_url = processor.gcs_storage_service.upload_image_from_url(
                                last_temp_url, key_name=gcs_key, timeout=30,
                            )
                            entry["new_url"] = gcs_url or last_temp_url
                        except Exception:
                            entry["new_url"] = last_temp_url
                        logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} using fallback result after {_cbg_max_retries} attempts")
                    else:
                        entry["error"] = entry.get("error") or "All NB2 attempts failed"
                        logger.warning(f"   [Row {row_num}] crop_by_generation: image {idx} all attempts failed, keeping original")
                    return idx, entry

                with ThreadPoolExecutor(max_workers=min(_cbg_max_workers, len(valid_ref_images))) as pool:
                    futures = {
                        executor_submit_with_progress(pool, _cbg_process_one, i, u): i
                        for i, u in enumerate(valid_ref_images)
                    }
                    for fut in as_completed(futures):
                        idx, entry = fut.result()
                        _cbg_log.append(entry)
                        if entry["new_url"]:
                            valid_ref_images[idx] = entry["new_url"]

                _cbg_log.sort(key=lambda x: x["index"])
                _regen_count = sum(1 for e in _cbg_log if e["new_url"])
                _skip_ar_count = sum(1 for e in _cbg_log if e["skipped"] and e.get("skipped_reason") != "influencer_image")
                _skip_inf_count = sum(1 for e in _cbg_log if e.get("skipped_reason") == "influencer_image")
                _retry_count = sum(1 for e in _cbg_log if e.get("attempts", 0) > 1)
                logger.info(f"   [Row {row_num}] crop_by_generation done: {_regen_count} regenerated, {_skip_ar_count} already portrait, {_skip_inf_count} influencer skipped, {_retry_count} retried for hallucination, {len(_cbg_log) - _regen_count - _skip_ar_count - _skip_inf_count} failed")
                if on_progress:
                    _skip_parts = []
                    if _skip_ar_count:
                        _skip_parts.append(f"{_skip_ar_count} already portrait")
                    if _skip_inf_count:
                        _skip_parts.append(f"{_skip_inf_count} influencer skipped")
                    _skip_msg = ", ".join(_skip_parts) if _skip_parts else "0 skipped"
                    on_progress("step_complete", {
                        "step": "crop_by_generation",
                        "label": "Portrait Conversion",
                        "progress": 2,
                        "message": f"{_regen_count} regenerated, {_skip_msg}" + (f", {_retry_count} retried for hallucination" if _retry_count else ""),
                    })
                    on_progress("intermediate", {"key": "crop_by_generation_results", "value": _cbg_log})

            elif _vertical_option == "auto":
                # PLACEHOLDER ????? future: auto-select best strategy
                logger.info(f"   [Row {row_num}] vertical_option=auto not implemented yet, skipping")

            else:
                logger.warning(f"   [Row {row_num}] Unknown vertical_option '{_vertical_option}', skipping")

        if valid_ref_images or (_smart_mode and valid_assets):
            if simulation:
                ref_image_analyses = [{"url": url, "index": i, "type": "OTHER", "uniqueness": "medium", "description_variant_regular": f"[Sim] Reference image {i+1}", "motion_prompt_regular": "Subtle slow zoom in, very slight movement"} for i, url in enumerate(valid_ref_images)]
                if _smart_mode and valid_assets:
                    for i, a in enumerate(valid_assets):
                        _aurl = (a["url"] if isinstance(a, dict) else a)
                        asset_analyses.append({"asset_index": i, "url": _aurl, "type": "video", "duration_seconds": 5.0, "content_summary": f"[Sim] Asset video {i+1}", "key_moments": []})
                logger.info(f"   [Row {row_num}] [SIM] Media analysis complete")
            else:
                logger.info(f"   [Row {row_num}] Step 0.5: Analyzing media ({len(valid_ref_images)} ref images"
                            f"{', ' + str(len(valid_assets)) + ' assets' if _smart_mode and valid_assets else ''})...")

                # Step 1: Run image analysis FIRST (to get surprise_candidate flags)
                if valid_ref_images:
                    processor.reset_usage()
                    ref_image_analyses = _analyze_reference_images(processor, valid_ref_images)

                # Step 2: Extract surprise + venue candidate indices from LLM 1
                surprise_candidate_indices = [
                    a["index"] for a in ref_image_analyses
                    if a.get("surprise_candidate", False)
                ]
                if surprise_candidate_indices:
                    logger.info(f"   [Row {row_num}] Surprise candidates from image analysis: {surprise_candidate_indices}")
                venue_candidate_indices = [
                    a["index"] for a in ref_image_analyses
                    if a.get("venue_candidate", False)
                ]
                if venue_candidate_indices:
                    logger.info(f"   [Row {row_num}] Venue candidates from image analysis: {venue_candidate_indices}")

                # Step 3: Run motion variants + video analysis in PARALLEL
                motion_variants = {}
                with ThreadPoolExecutor(max_workers=2) as media_executor:
                    motion_future = None
                    vid_future = None

                    if valid_ref_images:
                        motion_future = executor_submit_with_progress(
                            media_executor,
                            _analyze_motion_variants,
                            processor,
                            valid_ref_images,
                            surprise_candidate_indices,
                            venue_candidate_indices,
                            gender,
                        )

                    if _smart_mode and valid_assets:
                        # valid_assets is video-only after A1 image extraction
                        asset_urls = []
                        for a in valid_assets:
                            _aurl = (a["url"] if isinstance(a, dict) else a)
                            asset_urls.append(_aurl)

                        if asset_urls:
                            vid_future = executor_submit_with_progress(
                                media_executor,
                                analyze_asset_videos,
                                vertex_provider=processor.gemini_service._provider,
                                asset_urls=asset_urls,
                                on_progress=on_progress,
                                llm_logger=processor.llm_logger,
                            )

                    # Collect motion variants
                    if motion_future:
                        try:
                            motion_variants = motion_future.result()
                        except Exception as e:
                            logger.warning(f"   [Row {row_num}] Motion variant analysis failed: {e} ????? continuing without")

                    if motion_variants:
                        for analysis in ref_image_analyses:
                            idx = analysis.get("index", -1)
                            if idx in motion_variants:
                                mv = motion_variants[idx]
                                analysis["description_variant_regular"] = mv["description_variant_regular"]
                                analysis["motion_prompt_regular"] = mv["motion_prompt_regular"]
                                analysis["description_variant_surprise"] = mv.get("description_variant_surprise")
                                analysis["motion_prompt_surprise"] = mv.get("motion_prompt_surprise")
                                analysis["description_variant_venue"] = mv.get("description_variant_venue")
                                analysis["motion_prompt_venue"] = mv.get("motion_prompt_venue")

                    # Collect video asset analyses (all videos, 0-indexed)
                    if _smart_mode and valid_assets:
                        _vid_results = vid_future.result() if vid_future else []
                        for vi, vid_analysis in enumerate(_vid_results):
                            vid_analysis["asset_index"] = vi
                            vid_analysis["type"] = "video"
                            asset_analyses.append(vid_analysis)
                        # Fill in any missing analyses as fallbacks
                        for vi in range(len(_vid_results), len(valid_assets)):
                            _aurl = (valid_assets[vi]["url"] if isinstance(valid_assets[vi], dict) else valid_assets[vi])
                            asset_analyses.append({
                                "asset_index": vi,
                                "url": _aurl,
                                "type": "video",
                                "content_summary": f"Asset video {vi+1}",
                                "duration_seconds": 0,
                                "key_moments": [],
                            })

                logger.info(f"   [Row {row_num}] Media analysis complete: {len(ref_image_analyses)} ref images, "
                            f"{len(asset_analyses)} asset videos")

                # --- Validate asset analyses: abort if any asset failed to analyze ---
                if _smart_mode and valid_assets and not simulation:
                    _failed_assets = []
                    for aa in asset_analyses:
                        _ai = aa.get("asset_index", "?")
                        if aa.get("duration_seconds", 0) == 0 or not aa.get("key_moments"):
                            _failed_assets.append(f"Asset {_ai} (video): duration={aa.get('duration_seconds', 0)}s, moments={len(aa.get('key_moments', []))}")
                    if len(asset_analyses) < len(valid_assets):
                        _failed_assets.append(f"Expected {len(valid_assets)} analyses but got {len(asset_analyses)} ????? some assets were dropped entirely")
                    if _failed_assets:
                        _err_msg = (
                            f"Asset analysis failed for {len(_failed_assets)} asset(s). "
                            f"This usually means the asset URLs are broken (404) or the files could not be downloaded. "
                            f"Details: {'; '.join(_failed_assets)}"
                        )
                        logger.error(f"   [Row {row_num}] ABORTING: {_err_msg}")
                        raise RuntimeError(_err_msg)

            # --- Callback: analyze_media step_complete + usage ---
            if on_progress:
                on_progress("step_complete", {
                    "step": "analyze_media",
                    "label": "Analyze Media",
                    "progress": 3,
                    "message": f"Analyzed {len(ref_image_analyses)} ref images, {len(asset_analyses)} asset videos",
                })
                if valid_ref_images and not simulation:
                    emit_llm_usage_events(processor, on_progress, usage_list, "analyze_media")
            # Asset video analysis usage is emitted per-video inside analyze_asset_videos()
            on_progress("intermediate", {"key": "ref_image_analyses", "value": ref_image_analyses})
            if asset_analyses:
                on_progress("intermediate", {"key": "asset_analyses", "value": asset_analyses})

    # =====================================================================
    # STEP 1: Parse prompt into TEXT 1-4 (if not already present in sheet)
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "parse_prompt",
            "label": "Parse Prompt",
            "message": "Parsing prompt into scenes...",
        })
    logger.info(f"   [Row {row_num}] Step 1: Parsing UGC prompt...")
    # Capture API-provided TEXT before locals shadow the parameter names.
    api_override_t1 = (text_1 or "").strip() if text_1 else ""
    api_override_t2 = (text_2 or "").strip() if text_2 else ""
    api_override_t3 = (text_3 or "").strip() if text_3 else ""
    api_override_t4 = (text_4 or "").strip() if text_4 else ""

    # Check intermediates first (checkpoint/resume)
    if "parsed_texts" in intermediates:
        parsed = intermediates["parsed_texts"]
        text_1 = parsed.get("text_1", "")
        text_2 = parsed.get("text_2", "")
        text_3 = parsed.get("text_3", "")
        text_4 = parsed.get("text_4", "")
        logger.info(f"   [Row {row_num}] Using existing intermediate: parsed_texts")
    else:
        # Check if TEXT columns already have data
        text_1 = row_data[text_1_col].strip() if text_1_col is not None and text_1_col < len(row_data) else ""
        text_2 = row_data[text_2_col].strip() if text_2_col is not None and text_2_col < len(row_data) else ""
        text_3 = row_data[text_3_col].strip() if text_3_col is not None and text_3_col < len(row_data) else ""
        text_4 = row_data[text_4_col].strip() if text_4_col is not None and text_4_col < len(row_data) else ""

    if api_override_t1:
        text_1 = api_override_t1
    if api_override_t2:
        text_2 = api_override_t2
    if api_override_t3:
        text_3 = api_override_t3
    if api_override_t4:
        text_4 = api_override_t4

    if "parsed_texts" not in intermediates and (not text_1 or not text_2 or not text_3):
        # Need to parse the prompt (same TEXT 1-4 structure; log reflects Video type)
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
                # Smart mode: pass text descriptions instead of base64 images
                _pp_kwargs = dict(
                    prompt=prompt,
                    video_type_context="UGC",
                    language=subtitle_language,
                    on_progress=on_progress,
                )
                if _smart_mode and ref_image_analyses:
                    _pp_kwargs["image_descriptions"] = ref_image_analyses
                else:
                    _pp_kwargs["image_urls"] = valid_ref_images
                if _smart_mode and asset_analyses:
                    _pp_kwargs["asset_descriptions"] = asset_analyses
                parsed = parse_product_prompt(
                    lambda msgs, **kw: processor._call_llm("parse_prompt", msgs, **kw),
                    **_pp_kwargs,
                )
            
            text_1 = parsed.get("text_1", "")
            text_2 = parsed.get("text_2", "")
            text_3_raw = parsed.get("text_3", "")
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
            
            for column_name, value in updates:
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID,
                    config.GOOGLE_SHEET_TAB,
                    row_num,
                    column_name,
                    value,
                    headers
                )
            logger.info(f"   [Row {row_num}] Parsed and wrote TEXT 1-4 to sheet (UGC)")

            # --- Callback: parse_prompt usage ---
            if on_progress:
                emit_llm_usage_events(processor, on_progress, usage_list, "parse_prompt")

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

    # =====================================================================
    # STEP 1.5: Extract business highlights (smart mode only)
    # =====================================================================
    if _smart_mode and not simulation:
        if highlights:
            # API-provided highlights ????? use directly, skip LLM
            highlights_data = [{"highlight": h, "visual_cue": None} for h in highlights]
            logger.info(f"   [Row {row_num}] Using {len(highlights_data)} API-provided highlights")
        else:
            # Extract from prompt via LLM
            if on_progress:
                on_progress("step_start", {
                    "step": "extract_highlights",
                    "label": "Talking points",
                    "message": "Extracting key talking points for the voiceover...",
                })
            processor.reset_usage()
            highlights_data = extract_highlights(
                lambda msgs, **kw: processor._call_llm("extract_highlights", msgs, **kw),
                free_text=f"{text_1}\n{text_2}\n{text_3}",
                business_category=business_category,
            )
            if highlights_data:
                logger.info(f"   [Row {row_num}] Extracted {len(highlights_data)} highlights via LLM")
            if on_progress:
                emit_llm_usage_events(processor, on_progress, usage_list, "extract_highlights")
        highlights_text = format_highlights_text(highlights_data)
        if on_progress:
            on_progress("artifact", {"name": "smart_highlights", "data": highlights_data})
    else:
        highlights_data = []
        highlights_text = ""

    # =====================================================================
    # STEP 2.7: Generate VO FIRST (before scene prompts) so scene timing matches audio
    # Same logic as product video: VO length drives scene count and durations.
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "vo_generation",
            "label": "Voice Over Generation",
            "message": "Generating voiceover script and audio...",
        })
    vo_result = {"script": None, "audio_url": None, "segments": None, "audio_urls": None, "word_segments": None}
    vo_duration_seconds = 0.0
    vo_audio_url_early = None
    ugc_word_segments = []

    # Check for existing VO intermediates (checkpoint/resume)
    _vo_from_intermediates = False
    if "vo_script" in intermediates and "vo_audio_url" in intermediates and "vo_word_segments" in intermediates:
        vo_result["script"] = intermediates["vo_script"]
        vo_audio_url_early = intermediates["vo_audio_url"]
        ugc_word_segments = intermediates["vo_word_segments"] or []
        vo_result["audio_url"] = vo_audio_url_early
        vo_result["word_segments"] = ugc_word_segments
        if ugc_word_segments:
            vo_duration_seconds = max((ws["end_time"] for ws in ugc_word_segments), default=0)
        logger.info(f"   [Row {row_num}] Using existing intermediates: vo_script, vo_audio_url, vo_word_segments (VO duration={vo_duration_seconds:.1f}s)")
        _vo_from_intermediates = True

    combined_script = ""  # Defined outside try so it's accessible for VO pre-splitting later
    if _vo_from_intermediates:
        combined_script = vo_result["script"] or ""
    if not _vo_from_intermediates:
        logger.info(f"   [Row {row_num}] Step 2.7: Generating VO FIRST (target ~{target_duration}s)...")
    try:
        existing_vo_script = ""
        if vo_script_col is not None and vo_script_col < len(row_data):
            existing_vo_script = row_data[vo_script_col].strip()
        if _vo_from_intermediates:
            pass  # VO already loaded from intermediates above; skip generation
        elif existing_vo_script:
            combined_script = existing_vo_script
            logger.info(f"   [Row {row_num}] Using existing VO script ({len(combined_script)} chars)")
        elif not generate_vo:
            logger.info(f"   [Row {row_num}] generate_vo=False ????? skipping VO generation")
            combined_script = None
        elif simulation:
            combined_script = (
                f"[Sim] Hey everyone! I want to share something amazing with you today. "
                f"||| This product has completely changed my routine and I am so excited. "
                f"||| The quality is incredible and the results speak for themselves. "
                f"||| Click the link below to check it out for yourself!"
            )
            logger.info(f"   [Row {row_num}] [SIM] Generated VO script ({len(combined_script.split())} words)")
        else:
            # NOTE: End card duration (~2s) is deliberately NOT subtracted from vo_target.
            # The end card is a post-VO outro that extends the video beyond VO ????? by design.
            vo_target = float(target_duration) - (len(valid_assets) * 1.5)
            vo_target = max(10.0, vo_target)
            _p_defaults = get_pipeline_defaults()
            _secs_per_scene = _p_defaults.get("target_seconds_per_scene", 7.0)
            _min_wps = _p_defaults.get("min_words_per_scene", 15)
            _wps = calibrated_wps or get_speech_rate(subtitle_language)
            _vo_words = int(vo_target * _wps)
            # Duration-based count drives scene count ????? this is what the user requested
            # Word-based count is a minimum floor (prevents too-long segments for fast speakers)
            # Use max so slow-speaking voices don't collapse scene count
            _dur_scenes = max(3, int(round(vo_target / _secs_per_scene)))
            _word_scenes = max(3, _vo_words // _min_wps)
            _scene_count = min(max(_dur_scenes, _word_scenes), config.MAX_SCENES)
            processor.reset_usage()
            # Load arc template for VO beat guidance
            _arc_template = get_arc_template(business_category, int(vo_target))
            _arc_beats_text = format_arc_beats(_arc_template)
            if _arc_template:
                logger.info(f"   [Row {row_num}] Arc template: {business_category} / {len(_arc_template)} beats")
            # Build media_descriptions for VO context (smart mode only)
            _vo_media_desc = ""
            if _smart_mode and asset_analyses:
                _vo_lines = []
                for ad in asset_analyses:
                    if vo_duration_hints:
                        moments_str = ", ".join(
                            f"{m['description']} [{m.get('duration_seconds', m.get('end_seconds', 0) - m.get('start_seconds', 0)):.1f}s]"
                            for m in ad.get("key_moments", [])
                        )
                    else:
                        moments_str = ", ".join(m["description"] for m in ad.get("key_moments", []))
                    _dur_tag = f"[{ad.get('duration_seconds', 0):.1f}s total] " if vo_duration_hints else ""
                    _vo_lines.append(f"- Video {ad['asset_index']}: {_dur_tag}{ad['content_summary']}. Key moments: {moments_str}")
                _vo_media_desc = "Media assets available:\n" + "\n".join(_vo_lines)
            combined_script = generate_influencer_vo_script(
                lambda msgs, **kw: processor._call_llm("generate_vo", msgs, **kw),
                free_text=f"{text_1}\n{text_2}\n{text_3}",
                target_duration=vo_target,
                manual_instructions=f"Gender: {'female' if gender == 'f' else 'male'} influencer",
                language=subtitle_language,
                raw_prompt=prompt,
                text_4=text_4,
                video_subtype=video_subtype,
                wps_override=calibrated_wps,
                cta_slogan=(slogan_text or "").strip(),
                media_descriptions=_vo_media_desc,
                arc_beats=_arc_beats_text,
                highlights_text=highlights_text,
                highlights_data=highlights_data,
                on_progress=on_progress,
            )
            if combined_script:
                logger.info(f"   [Row {row_num}] Generated VO script: {len(combined_script.split())} words, targeting ~{vo_target:.0f}s")
                try:
                    processor.sheets_service.update_cell(
                        config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                        config.VO_SCRIPT_COLUMN, combined_script, headers
                    )
                except Exception:
                    pass
            else:
                logger.error(f"   [Row {row_num}] VO script generation failed (Gemini returned empty or timed out). Row will have no voice-over.")
        
        if combined_script and not _vo_from_intermediates:
            vo_result["script"] = combined_script
            # Emit vo_script immediately so Studio shows it (and when vo_script_only, we stop before TTS)
            if on_progress:
                on_progress("intermediate", {"key": "vo_script", "value": combined_script})
            if vo_script_only:
                logger.info(f"   [Row {row_num}] vo_script_only=True ????? script ready; TTS will run after user approves (Studio or resume).")
            else:
                script_for_tts = script_only_for_tts(combined_script) or combined_script
                if simulation:
                    # Mock TTS: generate fake word segments from script
                    words = script_for_tts.replace("|||", "").split()
                    wps = get_speech_rate(subtitle_language)
                    ugc_word_segments = [{"text": w, "start_time": i / wps, "end_time": (i + 1) / wps} for i, w in enumerate(words)]
                    vo_audio_url_early = _SIM_AUDIO
                    vo_result["audio_url"] = vo_audio_url_early
                    vo_result["word_segments"] = ugc_word_segments
                    vo_duration_seconds = len(words) / wps
                    logger.info(f"   [Row {row_num}] [SIM] VO ready: {len(words)} words, duration={vo_duration_seconds:.1f}s")
                else:
                    # --- Parallel: TTS + music description+Suno run simultaneously ---
                    def _do_tts():
                        return processor.elevenlabs_service.text_to_speech_with_timestamps(
                            text=script_for_tts,
                            voice_id=voice_id,
                            language=subtitle_language,
                        )

                    _music_already_cached = "music_url" in intermediates and intermediates["music_url"]
                    def _do_music():
                        if _music_already_cached:
                            return None, None
                        try:
                            _mdesc = generate_music_description_from_text(
                                lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw),
                                content_text=f"{text_1}\n{text_2}\n{text_3}",
                                vo_script=combined_script,
                                video_subtype=video_subtype,
                            )
                            _murl = processor.suno_service.generate_pure_music(style_description=_mdesc)
                            return _mdesc, _murl
                        except Exception as _me:
                            logger.warning(f"   [Row {row_num}] [Parallel music] Failed: {_me}")
                            return None, None

                    if on_progress:
                        on_progress("step_start", {
                            "step": "music",
                            "label": "Background Music",
                            "message": "Generating background music in parallel with TTS...",
                        })

                    with ThreadPoolExecutor(max_workers=2) as _vo_pool:
                        _tts_future = _vo_pool.submit(_do_tts)
                        _music_future = _vo_pool.submit(_do_music)
                        tts_result = _tts_future.result()
                        _parallel_music_desc, _parallel_music_url = _music_future.result()

                    if tts_result:
                        vo_audio_data, ugc_word_segments = tts_result
                        vo_key = f"ugc_videos/row_{row_num}_vo_{int(time.time())}.mp3"
                        vo_audio_url_early = processor.gcs_storage_service.upload_audio_bytes(
                            audio_data=vo_audio_data, key_name=vo_key
                        )
                        if vo_audio_url_early:
                            vo_result["audio_url"] = vo_audio_url_early
                            if ugc_word_segments:
                                vo_result["word_segments"] = ugc_word_segments
                                vo_duration_seconds = max((ws["end_time"] for ws in ugc_word_segments), default=0)
                                logger.info(f"   [Row {row_num}] VO ready: {len(ugc_word_segments)} words, duration={vo_duration_seconds:.1f}s")
                            try:
                                processor.sheets_service.update_cell(
                                    config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB, row_num,
                                    config.NEW_VOICE_COLUMN, vo_audio_url_early, headers
                                )
                            except Exception:
                                pass
                        else:
                            logger.warning(f"   [Row {row_num}] VO upload to GCS failed ????? voice-over will be missing.")

                    # Store music results from parallel execution
                    if _parallel_music_url and not _music_already_cached:
                        intermediates["music_url"] = _parallel_music_url
                        intermediates["music_description"] = _parallel_music_desc or ""
                        logger.info(f"   [Row {row_num}] [Parallel music] Done: {_parallel_music_url[:60]}...")
                    else:
                        logger.warning(f"   [Row {row_num}] VO TTS failed (ElevenLabs returned no audio ????? check API key, voice_id, quota).")
        else:
            if not existing_vo_script:
                logger.warning(f"   [Row {row_num}] No VO text available ????? skipping TTS. Video will have no voice-over.")
    except Exception as e:
        logger.warning(f"   [Row {row_num}] VO generation error: {e}")

    # =====================================================================
    # Post-TTS VO duration note (no regeneration ????? single VO+TTS pass policy)
    # =====================================================================
    if (vo_duration_seconds > 0 and not _vo_from_intermediates
            and not simulation and combined_script and generate_vo):
        vo_target = float(target_duration) - (len(valid_assets) * 1.5)
        vo_target = max(10.0, vo_target)
        _max_dur_overshoot = _p_defaults.get("vo_max_duration_overshoot", 0.20)
        _max_vo_dur = vo_target * (1 + _max_dur_overshoot)
        if vo_duration_seconds > _max_vo_dur:
            logger.warning(
                f"   [Row {row_num}] VO audio {vo_duration_seconds:.1f}s exceeds soft cap "
                f"{_max_vo_dur:.1f}s (target {vo_target:.1f}s) ????? keeping first take (no VO retries)."
            )

    # Store VO intermediates for checkpoint/resume
    if vo_result.get("script") and not _vo_from_intermediates:
        intermediates["vo_script"] = vo_result["script"]
    if vo_result.get("audio_url") and not _vo_from_intermediates:
        intermediates["vo_audio_url"] = vo_result["audio_url"]
    if vo_result.get("word_segments") and not _vo_from_intermediates:
        intermediates["vo_word_segments"] = vo_result["word_segments"]

    # =====================================================================
    # CRITICAL: Override target_duration to match actual VO length.
    # The VO is already recorded and locked at this point; animations MUST
    # cover the full VO duration or the final video will end before the audio
    # (or rely on a capped Ken Burns filler that creates an obvious static tail).
    # =====================================================================
    if vo_duration_seconds > 0 and vo_duration_seconds > target_duration:
        old_target = target_duration
        target_duration = int(vo_duration_seconds) + 2
        logger.info(
            f"   [Row {row_num}] VO ({vo_duration_seconds:.1f}s) > target ({old_target}s) "
            f"-> target_duration={target_duration}s (matched to VO)"
        )

    # =====================================================================
    # EARLY MUSIC: Now handled in parallel with TTS above (non-simulation path).
    # Simulation path still needs separate handling here.
    _early_music_url = None
    _early_music_description = None
    _music_emitted_early = False
    if simulation and ("music_url" not in intermediates or not intermediates["music_url"]):
        _early_music_url = _SIM_AUDIO
        _early_music_description = "Upbeat modern background music, no vocals"
        intermediates["music_url"] = _early_music_url
        intermediates["music_description"] = _early_music_description
        _music_emitted_early = True
        logger.info(f"   [Row {row_num}] [SIM] Early music generated")
    elif "music_url" in intermediates and intermediates["music_url"]:
        _music_emitted_early = True
        logger.info(f"   [Row {row_num}] Music ready (parallel or cached)")

    # --- Callback: emit all intermediates (VO + music) then step_complete so wrapper saves before pause ---
    if on_progress:
        if vo_result.get("script"):
            on_progress("intermediate", {"key": "vo_script", "value": vo_result["script"]})
        if vo_result.get("audio_url"):
            on_progress("intermediate", {"key": "vo_audio_url", "value": vo_result["audio_url"]})
        if vo_result.get("word_segments"):
            on_progress("intermediate", {"key": "vo_word_segments", "value": vo_result["word_segments"]})
        if intermediates.get("music_url"):
            on_progress("intermediate", {"key": "music_url", "value": intermediates["music_url"]})
        if intermediates.get("music_description"):
            on_progress("intermediate", {"key": "music_description", "value": intermediates["music_description"]})
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
        if vo_result.get("audio_url"):
            usage_data = {
                "service": "elevenlabs", "step": "tts",
                "model": get_elevenlabs_config()["tts_model"], "provider": "elevenlabs",
                "character_count": len(vo_result.get("script", "")),
                "label": "Text-to-speech", "category": "tts", "success": True,
            }
            on_progress("usage", usage_data)
            usage_list.append(usage_data)
        # Usage: early music (Suno) when we generated it
        if _early_music_url and not simulation:
            usage_data = {
                "service": "suno", "step": "music",
                "model": "suno-v5", "provider": "kie",
                "count": 1, "label": "Background music",
                "category": "music", "success": True,
            }
            on_progress("usage", usage_data)
            usage_list.append(usage_data)
    
    # =====================================================================
    # STEP 2: Calculate scene count from VO structure
    # If the VO script has ||| markers, use the NUMBER OF SEGMENTS as
    # the scene count so every visual matches a VO segment 1-to-1.
    # Otherwise fall back to vo_duration / 4.
    # =====================================================================
    asset_time = len(valid_assets) * 3.0
    # Count ||| segments in the VO script (each ||| creates a new segment)
    vo_segment_count = 0
    if combined_script and "|||" in combined_script:
        vo_segment_count = len([s for s in combined_script.split("|||") if s.strip()])
    
    _target_sps = get_pipeline_defaults().get("target_seconds_per_scene", 6.0)
    if vo_segment_count >= 3:
        # Use the VO segment structure ????? one scene per segment
        num_generated_scenes = max(2, min(config.MAX_SCENES, vo_segment_count))
        logger.info(f"   [Row {row_num}] Scene count from VO segments (|||): {vo_segment_count} segments ????? {num_generated_scenes} scenes")
    elif vo_duration_seconds > 0:
        remaining_vo = max(8.0, vo_duration_seconds)
        num_generated_scenes = max(2, min(config.MAX_SCENES, int(round(remaining_vo / _target_sps))))
        logger.info(f"   [Row {row_num}] Scene count from VO duration: {vo_duration_seconds:.1f}s / {_target_sps}s = {num_generated_scenes} scenes")
    else:
        remaining_time = max(8.0, target_duration - asset_time)
        num_generated_scenes = max(2, min(config.MAX_SCENES, int(remaining_time / _target_sps)))
    
    total_scenes = num_generated_scenes + len(valid_assets)
    estimated_total = (num_generated_scenes * 4.0) + asset_time
    logger.info(f"   [Row {row_num}] Scene distribution: {num_generated_scenes} generated (~{num_generated_scenes*4}s) + {len(valid_assets)} assets (~{asset_time:.0f}s) = ~{estimated_total:.0f}s (target: {target_duration}s)")
    
    # =====================================================================
    # STEP 2.5: Analyze reference images to understand their content
    # Uses existing "Image X Explain" descriptions from the sheet when
    # available; otherwise generates via Gemini and writes back.
    # =====================================================================
    _explanations = image_explanations or []
    _explain_cols = image_explain_cols or []
    ref_image_analyses = []
    if valid_ref_images:
        if simulation:
            ref_image_analyses = [{"url": url, "description": f"[Sim] Reference image analysis for {url[:40]}"} for url in valid_ref_images]
            logger.info(f"   [Row {row_num}] [SIM] Reference image analysis complete")
        else:
            logger.info(f"   [Row {row_num}] Analyzing {len(valid_ref_images)} reference images...")
            processor.reset_usage()

            images_needing_analysis: List[int] = []
            for i, url in enumerate(valid_ref_images):
                existing = _explanations[i] if i < len(_explanations) else None
                if existing:
                    ref_image_analyses.append({
                        "url": url, "index": i, "description": existing,
                    })
                    logger.info(f"   Image {i+1}: using existing description from sheet")
                else:
                    ref_image_analyses.append(None)
                    images_needing_analysis.append(i)

            if images_needing_analysis:
                urls_to_analyze = [valid_ref_images[i] for i in images_needing_analysis]
                new_analyses = _analyze_reference_images(processor, urls_to_analyze)
                for j, orig_idx in enumerate(images_needing_analysis):
                    analysis = new_analyses[j] if j < len(new_analyses) else {
                        "url": valid_ref_images[orig_idx], "index": orig_idx,
                        "description": f"Reference image {orig_idx+1}",
                    }
                    analysis["index"] = orig_idx
                    ref_image_analyses[orig_idx] = analysis
                    logger.info(f"   Image {orig_idx+1}: {analysis['description'][:80]}...")

                    # Write description back to the "Image X Explain" column
                    ec = _explain_cols[orig_idx] if orig_idx < len(_explain_cols) else None
                    if ec is not None and headers and analysis.get("description"):
                        try:
                            processor.sheets_service.update_cell(
                                config.GOOGLE_SHEET_ID, config.GOOGLE_SHEET_TAB,
                                row_num, headers[ec],
                                analysis["description"], headers,
                            )
                            logger.info(f"   Wrote Image {orig_idx+1} description to sheet")
                        except Exception as e:
                            logger.warning(f"   Could not write Image {orig_idx+1} Explain: {e}")
            else:
                logger.info(f"   [Row {row_num}] All image descriptions loaded from sheet")

            ref_image_analyses = [a for a in ref_image_analyses if a is not None]
            logger.info(f"   [Row {row_num}] Reference image analysis complete")

    # --- Callback: analyze_reference step_complete + usage ---
    if valid_ref_images and on_progress:
        on_progress("step_complete", {
            "step": "analyze_reference",
            "label": "Analyze Reference Images",
            "progress": 22,
            "message": "Reference images analyzed",
        })
        _llm_usage = processor.get_usage()
        usage_data = {
            "service": "gemini_text", "step": "analyze_reference",
            "model": text_model or "gemini-2.5-flash",
            "provider": text_provider or "vertex",
            "input_tokens": _llm_usage["input_tokens"], "output_tokens": _llm_usage["output_tokens"],
            "label": "Analyze reference images", "category": "text",
            "success": bool(ref_image_analyses),
        }
        on_progress("usage", usage_data)
        usage_list.append(usage_data)

    # =====================================================================
    # STEP 2.8: Pre-split VO into scene segments for visual-audio coherence
    # =====================================================================
    ugc_word_segments = vo_result.get("word_segments") or []
    vo_timing_for_scenes = None
    if ugc_word_segments and vo_duration_seconds > 0:
        full_vo_text = " ".join(ws["text"] for ws in ugc_word_segments)
        
        # Try to pre-split VO into scenes using '|||' markers
        vo_scene_segments = []
        # Require at least 2 segments minimum (beats are natural, not forced)
        min_acceptable_segments = 2
        if combined_script and "|||" in combined_script:
            vo_scene_segments = _presplit_vo_into_scenes(processor, 
                combined_script, ugc_word_segments, vo_duration_seconds
            )
            if vo_scene_segments:
                if len(vo_scene_segments) < min_acceptable_segments:
                    # Too few ||| segments for this video length - fall back to sentence splitting
                    logger.warning(f"   [Row {row_num}] VO has only {len(vo_scene_segments)} ||| segments but need ~{num_generated_scenes} scenes ????? falling back to sentence splitting")
                    vo_scene_segments = []  # Reset so fallback triggers
                else:
                    logger.info(f"   [Row {row_num}] Pre-split VO into {len(vo_scene_segments)} scene segments using ||| markers")
                    for seg in vo_scene_segments:
                        logger.info(f"   [Row {row_num}]   Scene {seg['scene_num']}: '{seg['text'][:50]}...' ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s)")
        
        # Fallback: split at sentence boundaries (also used when ||| count is too low)
        if not vo_scene_segments:
            vo_scene_segments = _presplit_vo_at_sentences(processor, 
                ugc_word_segments, num_generated_scenes, vo_duration_seconds
            )
            if vo_scene_segments:
                logger.info(f"   [Row {row_num}] Pre-split VO into {len(vo_scene_segments)} scene segments at sentence boundaries")
                for seg in vo_scene_segments:
                    logger.info(f"   [Row {row_num}]   Scene {seg['scene_num']}: '{seg['text'][:50]}...' ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s)")
        
        # Rebalance: split any segment that exceeds the video model's usable clip
        # duration so every scene has a visual clip that fully covers its VO.
        # Cap the config value by (max_supported_duration - warmup_skip) for the
        # active video model — e.g. Veo max=8s, warmup=1s → effective cap = 7s.
        if vo_scene_segments:
            _max_seg_dur = _p_defaults.get("max_scene_segment_duration", 12.0)
            if video_model:
                from tvd_pipeline.data_loader import get_supported_durations as _gsd
                _dur_map = _gsd()
                _model_supported = _dur_map.get(video_model)
                if not _model_supported:
                    for _k in _dur_map:
                        if video_model.startswith(_k):
                            _model_supported = _dur_map[_k]
                            break
                if _model_supported:
                    _warmup = _get_warmup_skip(processor, video_model)
                    _model_cap = float(max(_model_supported)) - _warmup
                    if _model_cap < _max_seg_dur:
                        logger.info(
                            f"   [Row {row_num}] Rebalance cap: {_max_seg_dur}s -> {_model_cap}s "
                            f"({video_model} max={max(_model_supported)}s warmup={_warmup}s)"
                        )
                        _max_seg_dur = _model_cap
            old_count = len(vo_scene_segments)
            vo_scene_segments = _rebalance_oversized_segments(
                vo_scene_segments, ugc_word_segments, max_segment_duration=_max_seg_dur
            )
            if len(vo_scene_segments) != old_count:
                num_generated_scenes = len(vo_scene_segments)
                logger.info(f"   [Row {row_num}] After rebalance: {num_generated_scenes} scenes (was {old_count})")
                for seg in vo_scene_segments:
                    logger.info(f"   [Row {row_num}]   Scene {seg['scene_num']}: '{seg['text'][:50]}...' ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s, {seg['duration']:.1f}s)")

        vo_timing_for_scenes = {
            "total_duration": round(vo_duration_seconds, 2),
            "word_count": len(ugc_word_segments),
            "full_text": full_vo_text,
            "segments": ugc_word_segments,
            "scene_segments": vo_scene_segments
        }
    
    # =====================================================================
    # STEP 3: Generate scene prompts with influencer logic + VO timing
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "scene_prompts",
            "label": "Scene Prompts",
            "message": "Generating scene prompts...",
        })
    _scenes_from_intermediates = False
    if "scene_prompts" in intermediates and intermediates["scene_prompts"]:
        scenes = intermediates["scene_prompts"]
        music_style = intermediates.get("music_style", "")
        _scenes_from_intermediates = True
        logger.info(f"   [Row {row_num}] Using existing intermediate: scene_prompts ({len(scenes)} scenes)")

    if not _scenes_from_intermediates:
        logger.info(f"   [Row {row_num}] Step 3: Generating influencer scene prompts (with VO timing)...")

    try:
        if _scenes_from_intermediates:
            pass  # Scenes already loaded from intermediates
        elif simulation:
            scenes_list = []
            for i in range(num_generated_scenes):
                scenes_list.append({
                    "scene_number": i + 1,
                    "first_prompt": f"[Sim] UGC Scene {i + 1} with influencer in casual setting",
                    "second_prompt": f"[Sim] Natural camera movement, subtle pan",
                    "image_prompt": f"[Sim] UGC Scene {i + 1} image prompt",
                    "motion_prompt": f"[Sim] Scene {i + 1} motion prompt",
                    "shows_influencer": True,
                    "duration": round(target_duration / num_generated_scenes, 1),
                })
            scene_result = {"scene_prompts": scenes_list}
            logger.info(f"   [Row {row_num}] [SIM] Generated {num_generated_scenes} scene prompts")
        else:
            # Build reference image data with analysis + motion variants for scene generation
            ref_image_data = []
            for analysis in ref_image_analyses:
                entry = {
                    "url": analysis["url"],
                    "analysis": analysis.get("description_variant_regular") or analysis.get("description") or "Reference image",
                    "uniqueness": analysis.get("uniqueness", "medium"),
                    "description_variant_regular": analysis.get("description_variant_regular"),
                    "motion_prompt_regular": analysis.get("motion_prompt_regular"),
                    "description_variant_surprise": analysis.get("description_variant_surprise"),
                    "motion_prompt_surprise": analysis.get("motion_prompt_surprise"),
                    "venue_candidate": analysis.get("venue_candidate", False),
                    "description_variant_venue": analysis.get("description_variant_venue"),
                    "motion_prompt_venue": analysis.get("motion_prompt_venue"),
                }
                ref_image_data.append(entry)
            # If no analyses, just use URLs
            if not ref_image_data:
                for url in valid_ref_images:
                    ref_image_data.append({"url": url})

            # Extract venue DNA once — before scene generation.
            # Runs only when the prompt or image descriptions mention a specific
            # business location.  Result is injected into every scene-gen call
            # to enforce consistent indoor environments across all scenes.
            venue_dna_str = ""
            try:
                venue_dna_str = _extract_venue_dna(
                    processor,
                    prompt_text=f"{text_1}\n{text_2}\n{text_3}",
                    ref_image_data=ref_image_data,
                    row_num=row_num,
                )
                if venue_dna_str and on_progress:
                    on_progress("intermediate", {"key": "venue_dna", "value": venue_dna_str})
            except Exception as _vde:
                logger.warning(f"   [Row {row_num}] Venue DNA step skipped: {_vde}")

            processor.reset_usage()
            if _smart_mode:
                scene_result = generate_influencer_prompts_smart(
                    lambda msgs, **kw: processor._call_llm("generate_scenes", msgs, **kw),
                    free_text=f"{text_1}\n{text_2}\n{text_3}",
                    reference_images=ref_image_data,
                    scene_count=num_generated_scenes,
                    manual_instructions=f"Gender: {'female' if gender == 'f' else 'male'} influencer.",
                    cta_text=slogan_text or "",
                    language=subtitle_language,
                    existing_influencer_description=influencer_description or "",
                    vo_timing=vo_timing_for_scenes,
                    visual_style=ugc_style,
                    video_subtype=video_subtype,
                    asset_descriptions=asset_analyses,
                    on_progress=on_progress,
                    call_fn_director=lambda msgs, **kw: processor._call_llm("media_director", msgs, **kw),
                    target_duration=target_duration,
                    min_influencer_clip_ratio=min_influencer_clip_ratio,
                    max_influencer_clip_ratio=max_influencer_clip_ratio,
                    highlights_text=highlights_text,
                    surprise_mode=_surprise_mode,
                    visual_location=product_location or "",
                    venue_dna=venue_dna_str,
                    logo_url=logo_url or "",
                )
            else:
                scene_result = generate_influencer_prompts(
                    lambda msgs, **kw: processor._call_llm("generate_scenes", msgs, **kw),
                    free_text=f"{text_1}\n{text_2}\n{text_3}",
                    reference_images=ref_image_data,
                    scene_count=num_generated_scenes,
                    manual_instructions=f"Gender: {'female' if gender == 'f' else 'male'} influencer.",
                    cta_text=slogan_text or "",
                    language=subtitle_language,
                    existing_influencer_description=influencer_description or "",
                    vo_timing=vo_timing_for_scenes,
                    visual_style=ugc_style,
                    video_subtype=video_subtype,
                    asset_descriptions=asset_analyses if _smart_mode else None,
                    venue_dna=venue_dna_str,
                )

        if not _scenes_from_intermediates:
            scenes = scene_result.get("scene_prompts", [])

        if not scenes:
            error = "No scenes generated from prompts"
            logger.error(f"   [Row {row_num}] {error}")
            result["errors"].append(error)
            return result

        # Validate + fix media assignments (smart mode only)
        if _smart_mode and asset_analyses and not _scenes_from_intermediates and not simulation:
            max_retries = _p_defaults.get("scene_matching_max_retries", 2)
            for attempt in range(max_retries):
                violations = _validate_scene_media_assignments(scenes, asset_analyses, ref_image_analyses)
                if not violations:
                    if attempt > 0:
                        logger.info(f"   [Row {row_num}] Scene matching validation converged after {attempt} retry(s)")
                    break
                logger.warning(f"   [Row {row_num}] Scene matching validation attempt {attempt+1}/{max_retries}: {len(violations)} violation(s): {violations}")
                correction_text = "MEDIA ASSIGNMENT VIOLATIONS DETECTED ????? fix these:\n"
                correction_text += "\n".join(f"- {v}" for v in violations)
                correction_text += f"\n\nRegenerate ALL scene prompts. video_asset_index: null or 0-{len(asset_analyses)-1}."
                correction_text += " best_moment_index: -1 (full clip) or valid moment index."
                correction_text += f" reference_image_index: null or 0-{len(ref_image_analyses)-1}."
                correction_text += " Each image in at most ONE scene. Each (video, moment) pair unique."
                try:
                    retry_messages = [
                        {"role": "assistant", "content": json.dumps({"scene_prompts": scenes}, default=str)},
                        {"role": "user", "content": correction_text},
                    ]
                    retry_result = processor._call_llm("generate_scenes", retry_messages, temperature=0.5, max_tokens=16000)
                    retry_parsed = json.loads(retry_result.get("text", "{}"))
                    scenes = retry_parsed.get("scene_prompts", scenes)
                except Exception as retry_err:
                    logger.warning(f"   [Row {row_num}] Scene matching retry failed: {retry_err}")
                    break
            else:
                # Exhausted retries ????? force-fix
                remaining = _validate_scene_media_assignments(scenes, asset_analyses, ref_image_analyses)
                if remaining:
                    logger.warning(f"   [Row {row_num}] Scene matching did not converge after {max_retries} retries. Force-fixing.")
                    scenes = _force_fix_scene_assignments(scenes, asset_analyses, ref_image_analyses)

        # CRITICAL: Use only the first num_generated_scenes so video length matches VO.
        # If Gemini returns more scenes than requested, extra scenes have no VO segment
        # and would make the pipeline generate images/videos for them while the final
        # concat still uses VO-synced durations ????? resulting in a shorter video than VO.
        if not _scenes_from_intermediates and len(scenes) > num_generated_scenes:
            logger.warning(f"   [Row {row_num}] Gemini returned {len(scenes)} scene prompts but VO has {num_generated_scenes} segments ????? using first {num_generated_scenes} only (video length will match VO)")
            scenes = scenes[:num_generated_scenes]

        result["scene_prompts"] = scenes
        if not _scenes_from_intermediates:
            intermediates["scene_prompts"] = scenes
        logger.info(f"   [Row {row_num}] Generated {len(scenes)} scene prompts (aligned with VO segments)")

        # Attach VO text for each scene so image generation can enforce story match (works for both fresh and intermediates)
        _vo_segments = (vo_timing_for_scenes or {}).get("scene_segments") or []
        for _i, _sc in enumerate(scenes):
            if _i < len(_vo_segments) and _vo_segments[_i].get("text"):
                _snippet = re.sub(r"\[[^\]]*\]", "", _vo_segments[_i]["text"]).strip()
                if _snippet:
                    _sc["_vo_text_for_image"] = _snippet
        
        # Write scene prompts to sheet
        for i, scene in enumerate(scenes[:config.MAX_SCENES]):
            scene_num = i + 1
            image_prompt = scene.get("first_prompt", scene.get("image_prompt", ""))
            motion_prompt = scene.get("second_prompt", scene.get("motion_prompt", ""))
            
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

        # --- Early exit: dry-run mode (no asset generation) ---
        if not generate_assets:
            logger.info(f"   [Row {row_num}] generate_assets=False ????? stopping after Director convergence")
            if on_progress:
                on_progress("step_complete", {
                    "step": "dry_run_complete",
                    "label": "Dry Run Complete",
                    "progress": 100,
                    "message": "Director output ready (asset generation skipped)",
                })
            return {
                "final_video_url": None,
                "dry_run": True,
                "scenes": scenes,
                "vo_script": vo_result.get("vo_text") if vo_result else None,
            }

    except Exception as e:
        error = f"Error generating scene prompts: {str(e)}"
        logger.error(f"   [Row {row_num}] {error}")
        result["errors"].append(error)
        return result

    # Post-process: assign scene durations from ElevenLabs VO timings (SOURCE OF TRUTH for full VO?????scene sync).
    # Each scene must match the exact length of the speech segment the narrator speaks over it; no scene should be shorter or longer than its VO segment.
    # When we have pre-split scene_segments (from ||| or sentence boundaries), use their exact start/end from word_segments.
    vo_word_segments_early = vo_result.get("word_segments") or []
    VO_END_BUFFER = 1.0
    if vo_word_segments_early and vo_duration_seconds > 0 and len(scenes) > 0:
        n_scenes = len(scenes)
        scene_segments = (vo_timing_for_scenes or {}).get("scene_segments") or []
        
        if scene_segments and len(scene_segments) >= n_scenes:
            # Use exact pre-split segment timings for precise sync (1:1 match)
            for i, scene in enumerate(scenes):
                seg = scene_segments[i]
                actual_start = seg["start_time"]
                if i == n_scenes - 1:
                    actual_end = vo_duration_seconds + VO_END_BUFFER
                    scene_dur = round(actual_end - actual_start, 2)
                else:
                    actual_end = seg["end_time"]
                    scene_dur = round(seg["duration"], 2)
                scene["duration"] = scene_dur
                scene["vo_start_time"] = round(actual_start, 3)
                scene["vo_end_time"] = round(actual_end, 3)
                scene["vo_word_start"] = seg.get("word_start_idx")
                scene["vo_word_end"] = seg.get("word_end_idx")
                logger.info(f"   [Row {row_num}] Scene {scene.get('scene_number', scene.get('scene_num', i+1))}: pre-split -> {actual_start:.2f}s-{actual_end:.2f}s ({scene_dur:.2f}s)")
            total_scene_dur = sum(s.get("duration", 0) for s in scenes)
            logger.info(f"   [Row {row_num}] Scene durations from ElevenLabs (exact VO segment length): {total_scene_dur:.2f}s (VO: {vo_duration_seconds:.2f}s + {VO_END_BUFFER}s buffer)")
        elif scene_segments and len(scene_segments) < n_scenes:
            # More scenes than VO segments ????? redistribute so that the LAST segment (CTA) is
            # assigned ONLY to the last scene. This prevents the closing slide from appearing
            # while the VO is still on earlier segments (e.g. "Third: rebalancing").
            logger.warning(f"   [Row {row_num}] ??????? {n_scenes} scenes but only {len(scene_segments)} VO segments ????? redistributing (last segment = CTA for last scene only)")
            n_seg = len(scene_segments)
            last_seg_idx = n_seg - 1
            story_scenes_count = n_scenes - 1  # all but the final CTA scene
            story_segments_count = n_seg - 1   # all but the CTA segment
            if story_segments_count <= 0:
                # Only one VO segment: split it across all scenes (original behaviour)
                seg = scene_segments[0]
                seg_dur = (vo_duration_seconds + VO_END_BUFFER) - seg["start_time"]
                slice_dur = round(seg_dur / n_scenes, 2)
                for i, scene in enumerate(scenes):
                    actual_start = round(seg["start_time"] + i * slice_dur, 3)
                    actual_end = round(actual_start + slice_dur, 3)
                    scene["duration"] = round(slice_dur, 2)
                    scene["vo_start_time"] = actual_start
                    scene["vo_end_time"] = actual_end
                total_scene_dur = sum(s.get("duration", 0) for s in scenes)
                logger.info(f"   [Row {row_num}] Scene durations (single segment split): {total_scene_dur:.2f}s")
            else:
                for i, scene in enumerate(scenes):
                    if i == n_scenes - 1:
                        # Last scene: entire last VO segment (CTA) ????? so CTA slide only when VO says CTA
                        seg = scene_segments[last_seg_idx]
                        actual_start = seg["start_time"]
                        actual_end = vo_duration_seconds + VO_END_BUFFER
                        scene_dur = round(actual_end - actual_start, 2)
                        scene["duration"] = scene_dur
                        scene["vo_start_time"] = round(actual_start, 3)
                        scene["vo_end_time"] = round(actual_end, 3)
                        logger.info(f"   [Row {row_num}] Scene {i+1}: CTA only -> {actual_start:.2f}s-{actual_end:.2f}s ({scene_dur:.2f}s)")
                    else:
                        # Story scenes: distribute first (n_seg - 1) segments over first (n_scenes - 1) scenes
                        scenes_per_story_seg = story_scenes_count / story_segments_count if story_segments_count else 1
                        seg_idx = min(int(i / scenes_per_story_seg), story_segments_count - 1)
                        seg = scene_segments[seg_idx]
                        seg_scene_start = int(seg_idx * scenes_per_story_seg)
                        seg_scene_end = int((seg_idx + 1) * scenes_per_story_seg)
                        scenes_in_this_seg = max(1, seg_scene_end - seg_scene_start)
                        local_idx = i - seg_scene_start
                        seg_dur = seg["end_time"] - seg["start_time"]
                        slice_dur = round(seg_dur / scenes_in_this_seg, 2)
                        actual_start = round(seg["start_time"] + local_idx * slice_dur, 3)
                        actual_end = round(actual_start + slice_dur, 3)
                        scene["duration"] = round(slice_dur, 2)
                        scene["vo_start_time"] = actual_start
                        scene["vo_end_time"] = actual_end
                        logger.info(f"   [Row {row_num}] Scene {i+1}: redistributed -> {actual_start:.2f}s-{actual_end:.2f}s ({slice_dur:.2f}s)")
                total_scene_dur = sum(s.get("duration", 0) for s in scenes)
                logger.info(f"   [Row {row_num}] Scene durations (redistributed): {total_scene_dur:.2f}s (VO: {vo_duration_seconds:.2f}s)")
        else:
            ws = vo_word_segments_early
            num_words = len(ws)
            words_per_scene = num_words / n_scenes
            gemini_indices = []
            for i in range(n_scenes):
                w_s = int(round(i * words_per_scene))
                w_e = int(round((i + 1) * words_per_scene)) - 1
                w_e = min(w_e, num_words - 1)
                w_s = min(w_s, num_words - 1)
                gemini_indices.append((w_s, w_e))

            _min_scene_dur = _p_defaults.get("min_scene_duration", 1.0)

            if sync_strategy == "phrase_start":
                # Phase 12: Each scene extends to start_time of next scene's first word
                logger.info(f"   [Row {row_num}] Using phrase_start sync strategy (UGC)")
                _apply_phrase_start_strategy(
                    scenes=scenes,
                    word_timestamps=ws,
                    gemini_indices=gemini_indices,
                    vo_duration=vo_duration_seconds,
                    last_scene_buffer=VO_END_BUFFER,
                    min_scene_duration=_min_scene_dur,
                )
                for i, scene in enumerate(scenes):
                    w_s, w_e = gemini_indices[i]
                    logger.info(f"   [Row {row_num}] Scene {scene.get('scene_number', scene.get('scene_num', i+1))}: "
                              f"words [{w_s}-{w_e}] -> {scene.get('vo_start_time', '?')}s-{scene.get('vo_end_time', '?')}s ({scene.get('duration', '?')}s) [phrase_start]")
            else:
                prev_end = 0.0
                for i, scene in enumerate(scenes):
                    w_s, w_e = gemini_indices[i]
                    actual_start = prev_end
                    raw_end = ws[w_e]["end_time"]
                    if i == n_scenes - 1:
                        actual_end = vo_duration_seconds + VO_END_BUFFER
                    else:
                        actual_end = raw_end
                    if actual_end - actual_start < _min_scene_dur:
                        actual_end = actual_start + max(_min_scene_dur, scene.get("duration", 4.0))
                    scene_dur = round(actual_end - actual_start, 2)
                    scene["duration"] = scene_dur
                    scene["vo_start_time"] = round(actual_start, 3)
                    scene["vo_end_time"] = round(actual_end, 3)
                    logger.info(f"   [Row {row_num}] Scene {scene.get('scene_number', scene.get('scene_num', i+1))}: words [{w_s}-{w_e}] -> {actual_start:.2f}s-{actual_end:.2f}s ({scene_dur:.2f}s)")
                    prev_end = actual_end

            total_scene_dur = sum(s.get("duration", 0) for s in scenes)
            logger.info(f"   [Row {row_num}] Scene durations from ElevenLabs (word indices, {sync_strategy}): {total_scene_dur:.2f}s (VO: {vo_duration_seconds:.2f}s + {VO_END_BUFFER}s buffer)")

        # Phase 12: When precision sync is active, compute exact_duration
        # and overgenerate_duration for each scene (applies to all duration branches above).
        if sync_method == "precision":
            _warmup = _get_warmup_skip(processor, video_model)
            for _s in scenes:
                _dur = _s.get("duration", 4.0)
                if "exact_duration" not in _s:
                    _s["exact_duration"] = round(_dur, 3)
                if "overgenerate_duration" not in _s:
                    _s["overgenerate_duration"] = math.ceil(_dur + _warmup)
            logger.info(f"   [Row {row_num}] Precision sync (UGC): overgenerate durations = {[s['overgenerate_duration'] for s in scenes]} (warmup={_warmup}s)")

    # =====================================================================
    # PRE-COMPENSATE SCENE DURATIONS FOR DISSOLVE OVERLAP
    # Each dissolve transition eats ~dissolve_seconds from the total.
    # We add proportional time to each scene NOW so Ken Burns / animation
    # videos are long enough and the final concat matches the VO exactly.
    # =====================================================================
    # Use provided dissolve or config default
    effective_dissolve = dissolve_seconds if dissolve_seconds is not None else _p_defaults.get("dissolve_seconds", 0.075)
    dissolve_sec = effective_dissolve
    # In smart mode, assets are embedded in scenes (no separate asset clips)
    total_clips = len(scenes) if _smart_mode else len(scenes) + len(valid_assets)
    num_dissolves = max(0, total_clips - 1)
    dissolve_loss = round(num_dissolves * dissolve_sec, 2)
    if dissolve_loss > 0 and total_clips > 0:
        per_clip_add = round(dissolve_loss / total_clips, 3)
        for scene in scenes:
            old_dur = scene.get("duration", 4.0)
            scene["duration"] = round(old_dur + per_clip_add, 2)
        logger.info(f"   [Row {row_num}] Dissolve pre-compensation: {num_dissolves} transitions ???? {dissolve_sec}s = {dissolve_loss:.1f}s ????? +{per_clip_add:.2f}s/scene")
    
    # =====================================================================
    # STEPS 4-7: PARALLEL ASSET GENERATION (VO already done in Step 2.7)
    # =====================================================================
    if on_progress:
        on_progress("step_start", {
            "step": "scene_generation",
            "label": "Scene Generation",
            "message": "Generating scene images and videos...",
        })
    logger.info(f"   [Row {row_num}] Steps 4-7: Starting PARALLEL asset generation...")
    
    # Shared results containers (vo_result already populated in Step 2.7)
    scene_images = [None] * len(scenes)
    scene_videos = [None] * len(scenes)
    scene_filler_videos = [[] for _ in range(len(scenes))]  # Smart mode: list of filler clips per scene
    _surprise_clips_generated = []  # Track surprise clips for end card selection
    scene_vo_audios = [None] * (len(scenes) + len(valid_assets))
    asset_videos = [None] * len(valid_assets)
    music_result = {"url": None, "description": None}

    # --- existing_intermediates skip logic for scene_images/scene_videos/music_url ---
    # Treat skip flags as "we have real URLs to reuse", not merely a non-empty list:
    # [None] * N is truthy in Python but must not enable _skip_images / _skip_videos or the
    # pipeline would think caches are in use while every slot still needs generation (duplicate Kie billing).
    _skip_images = False
    _skip_videos = False
    _cached_imgs = intermediates.get("scene_images")
    if isinstance(_cached_imgs, list) and any(u for u in _cached_imgs):
        cached_imgs = _cached_imgs
        for idx, url in enumerate(cached_imgs):
            if idx < len(scene_images):
                scene_images[idx] = url
        _skip_images = True
        logger.info(
            f"Using cached scene_images from existing_intermediates "
            f"({sum(1 for u in cached_imgs if u)}/{len(cached_imgs)} non-empty)"
        )
    _cached_vids = intermediates.get("scene_videos")
    if isinstance(_cached_vids, list) and any(u for u in _cached_vids):
        cached_vids = _cached_vids
        for idx, url in enumerate(cached_vids):
            if idx < len(scene_videos):
                scene_videos[idx] = url
        _skip_videos = True
        logger.info(
            f"Using cached scene_videos from existing_intermediates "
            f"({sum(1 for u in cached_vids if u)}/{len(cached_vids)} non-empty)"
        )
    if "music_url" in intermediates and intermediates["music_url"]:
        music_result["url"] = intermediates["music_url"]
        music_result["description"] = intermediates.get("music_description") or music_result["description"]
        logger.info("Using cached music_url (and description) from existing_intermediates")
    
    from tvd_pipeline.data_loader import get_veo3_config as _get_veo3_cfg
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
    
    # Slogan: use ONLY if explicitly provided in the Slogan column. No default "Try it now!" ?????
    # if empty, the Gemini scene prompt handles CTA text naturally (or no text at all).
    _has_slogan = bool(slogan_text and slogan_text.strip())
    
    # Ensure Image 1 (index 0) is used in at least one scene when reference images exist.
    # Otherwise the first reference image can be skipped and the user sees no use of it.
    if valid_ref_images and len(valid_ref_images) >= 1:
        used_indices = {
            s.get("reference_image_index")
            for s in scenes
            if isinstance(s.get("reference_image_index"), int)
            and 0 <= s.get("reference_image_index") < len(valid_ref_images)
        }
        if 0 not in used_indices:
            for idx, s in enumerate(scenes):
                if idx < len(scenes) - 1:  # not the CTA scene (keep CTA free for logo)
                    s["reference_image_index"] = 0
                    logger.info(f"   [Row {row_num}] Ensuring Image 1 is used: assigned to scene {idx + 1}")
                    break

    # =====================================================================
    # TRACK 1: Image ????? Video Pipeline (per scene)
    # =====================================================================
    def generate_scene_visual(scene_idx, scene, is_last_scene=False):
        """Generate image then immediately send to video generation."""
        if scene_image_stagger_seconds > 0 and scene_idx > 0:
            time.sleep(scene_idx * scene_image_stagger_seconds)
        scene_num = scene.get("scene_number", scene.get("scene_num", scene_idx + 1))
        image_prompt = scene.get("first_prompt", scene.get("image_prompt", ""))
        motion_prompt = scene.get("second_prompt", scene.get("motion_prompt", "Slow, subtle movement"))
        duration = scene.get("duration", 4.0)
        # Phase 12: precision sync ????? over-generate with ceil(duration), trim later
        _exact_dur = scene.get("exact_duration")
        _use_precision = sync_method == "precision" and _exact_dur is not None
        anim_duration = scene.get("overgenerate_duration", math.ceil(duration)) if _use_precision else duration
        # Check both field names for influencer scene flag
        is_influencer_scene = scene.get("shows_influencer", scene.get("is_influencer_scene", False))

        # SMART MODE with beat_clips: process all clips in the beat
        beat_clips = scene.get("beat_clips", [])
        vid_ref = scene.get("video_asset_index")
        logger.info(f"   [Row {row_num}] Scene {scene_num}: smart_mode={_smart_mode}, vid_ref={vid_ref} (type={type(vid_ref).__name__}), beat_clips={len(beat_clips)}, asset_analyses_len={len(asset_analyses)}")
        if _smart_mode and beat_clips:
            if simulation:
                scene_videos[scene_idx] = _SIM_VIDEO
                scene_filler_videos[scene_idx] = [{"url": _SIM_VIDEO, "duration": duration}]
                logger.info(f"   [Row {row_num}] Scene {scene_num}: [SIM] Beat clips ({len(beat_clips)} clips)")
                return (scene_idx, None, _SIM_VIDEO)

            # Pre-merge consecutive video clips from the same asset into single trims
            # E.g. clips at 0-2s, 2-5.5s, 5.5-8s from asset 0 ????? one clip at 0-8s
            # This avoids dissolve transitions between continuous footage
            _merged_clips = []
            for _bc in beat_clips:
                if (_merged_clips
                    and _bc.get("type") == "video" and _merged_clips[-1].get("type") == "video"
                    and _bc.get("video_asset_index") is not None
                    and _bc["video_asset_index"] == _merged_clips[-1].get("video_asset_index")):
                    # Same source video ????? extend the boundaries (never shrink)
                    prev = _merged_clips[-1]
                    new_start = _bc.get("_start_seconds", prev.get("_start_seconds", 0))
                    new_end = _bc.get("_end_seconds", prev.get("_end_seconds", 0))
                    prev["_start_seconds"] = min(prev.get("_start_seconds", 0), new_start)
                    prev["_end_seconds"] = max(prev.get("_end_seconds", 0), new_end)
                    prev["duration"] = round(prev["_end_seconds"] - prev["_start_seconds"], 2)
                    prev["best_moment_index"] = None  # merged clip uses explicit boundaries, not a single moment
                    prev["_merged"] = True
                    logger.info(f"   [Row {row_num}] Scene {scene_num}: merged consecutive clip from asset {_bc['video_asset_index']} ????? {prev.get('_start_seconds',0):.1f}-{prev['_end_seconds']:.1f}s")
                else:
                    _merged_clips.append(dict(_bc))
            if len(_merged_clips) < len(beat_clips):
                logger.info(f"   [Row {row_num}] Scene {scene_num}: merged {len(beat_clips)} beat clips ????? {len(_merged_clips)} (consecutive same-source combined)")
            beat_clips = _merged_clips

            # Process each clip in beat order
            beat_clip_results = []
            for ci, clip in enumerate(beat_clips):
                ctype = clip.get("type", "generate")
                c_dur = clip.get("duration", 4.0)
                c_vid_idx = clip.get("video_asset_index")
                c_mi = clip.get("best_moment_index")
                c_img_idx = clip.get("reference_image_index")

                if ctype == "video" and c_vid_idx is not None and isinstance(c_vid_idx, int) and 0 <= c_vid_idx < len(asset_analyses):
                    # Video asset clip ????? trim to duration
                    c_asset = asset_analyses[c_vid_idx]
                    c_url = c_asset.get("url", "")
                    c_start = clip.get("_start_seconds", 0.0)
                    c_end = clip.get("_end_seconds", c_asset.get("duration_seconds", 0))
                    # Use moment boundaries if available
                    if isinstance(c_mi, int) and c_mi >= 0 and c_mi < len(c_asset.get("key_moments", [])):
                        moment = c_asset["key_moments"][c_mi]
                        c_start = moment["start_seconds"]
                        c_end = moment["end_seconds"]
                    # Trim duration to requested clip duration
                    trim_dur = min(round(c_end - c_start, 2), c_dur)
                    try:
                        trimmed_url = processor.rendi_service.trim_video(
                            video_url=c_url, duration=trim_dur,
                            start_time=round(c_start, 2), has_audio=False,
                        )
                        if trimmed_url:
                            beat_clip_results.append({"url": trimmed_url, "duration": trim_dur, "_pre_trimmed": True})
                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: video {c_vid_idx} trimmed [{c_start:.1f}-{c_start+trim_dur:.1f}s] ({trim_dur}s)")
                            if on_progress:
                                usage_data = {
                                    "service": "rendi", "step": f"smart_trim_scene_{scene_num}_clip_{ci}",
                                    "model": "rendi", "provider": "rendi", "count": 1,
                                    "label": f"Smart trim scene {scene_num} clip {ci}", "category": "ffmpeg", "success": True,
                                }
                                on_progress("usage", usage_data)
                                usage_list.append(usage_data)
                        else:
                            logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: video trim failed")
                    except Exception as te:
                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: video trim error ({te})")

                elif ctype == "image" and c_img_idx is not None and isinstance(c_img_idx, int) and 0 <= c_img_idx < len(ref_image_analyses):
                    # Reference image clip ????? AI animation (Veo/Kling), Ken Burns fallback
                    ref_img_url = ref_image_analyses[c_img_idx].get("url", "")
                    c_motion = (
                        clip.get("motion_prompt")       # From Motion Writer (regular or surprise variant)
                        or clip.get("second_prompt")    # Writer-generated (for generate clips)
                        or "Subtle slow zoom in, very slight movement"  # Ultimate fallback
                    )
                    if clip.get("variant") == "surprise":
                        logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: using SURPRISE motion for ref image {c_img_idx}")
                    _venue_ref_video = False
                    if clip.get("variant") == "influencer_in_venue" and influencer_urls:
                        logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: INFLUENCER_IN_VENUE (NB2_I2V) for ref image {c_img_idx}")
                        # --- NB2_I2V: Generate first frame with NB2, then ref-to-video ---

                        # Step 0: Joint venue + influencer analysis (cached per venue image)
                        if not hasattr(processor, '_venue_inf_analysis_cache'):
                            processor._venue_inf_analysis_cache = {}
                        _via_cache_key = ref_img_url
                        _via_result = processor._venue_inf_analysis_cache.get(_via_cache_key)
                        if _via_result is None and not simulation:
                            try:
                                _via_result = _analyze_venue_and_influencer(
                                    processor, ref_img_url, influencer_urls[0], row_num
                                )
                                if _via_result:
                                    processor._venue_inf_analysis_cache[_via_cache_key] = _via_result
                                    logger.info(f"   [Row {row_num}] Venue+influencer analysis: venue='{_via_result.get('venue_description', '')[:60]}', influencer='{_via_result.get('influencer_description', '')[:60]}'")
                                    if on_progress:
                                        on_progress("intermediate", {"key": f"venue_influencer_analysis_s{scene_num}_c{ci}", "value": _via_result})
                                        emit_llm_usage_events(processor, on_progress, usage_list, f"venue_influencer_analysis_s{scene_num}_c{ci}")
                            except Exception as _via_err:
                                logger.warning(f"   [Row {row_num}] Venue+influencer analysis failed: {_via_err}")

                        # Step 1: NB2 compositing to create first frame
                        venue_desc = clip.get("description") or ""
                        # Build prompt with analysis descriptions if available
                        if _via_result:
                            venue_prompt = get_prompt_loader().get(
                                "shared_venue_nb2_composite",
                                venue_description=_via_result.get('venue_description', ''),
                                influencer_description=_via_result.get('influencer_description', ''),
                                director_description=venue_desc,
                            )
                        else:
                            venue_prompt = get_prompt_loader().get(
                                "shared_venue_nb2_composite_fallback",
                                director_description=venue_desc,
                            )
                        first_frame_url = None
                        try:
                            first_frame_url = processor.kie_service.composite_influencer_in_venue(
                                venue_image_url=ref_img_url,
                                influencer_image_urls=influencer_urls,
                                prompt=venue_prompt,
                                resolution=image_resolution or "1K",
                            )
                            if first_frame_url:
                                logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: NB2 first frame generated")
                                if on_progress:
                                    on_progress("intermediate", {
                                        "key": f"nb2_venue_image_s{scene_num}_c{ci}",
                                        "value": first_frame_url,
                                    })
                                    _nb_model = get_kie_config().get("nano_banana", {}).get("model", "nano-banana-2")
                                    usage_data = {
                                        "service": "kie", "step": f"venue_composite_scene_{scene_num}_clip_{ci}",
                                        "model": _nb_model, "provider": "kie", "count": 1,
                                        "label": f"Venue composite scene {scene_num} clip {ci}",
                                        "category": "images", "success": True,
                                    }
                                    on_progress("usage", usage_data)
                                    usage_list.append(usage_data)
                            else:
                                logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: NB2 first frame failed")
                        except Exception as nb2_err:
                            logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: NB2 first frame error ({nb2_err})")

                        # Step 2: Ref-to-video with first frame + influencer reference
                        if first_frame_url:
                            _ref_model_cfg = processor.model_config.get("media_models", {}).get("ref_to_video", {})
                            _ref_version = _ref_model_cfg.get("selected", "")
                            _ref_provider = _ref_model_cfg.get("provider", "google")
                            _version_cfg = _ref_model_cfg.get("versions", {}).get(_ref_version, {})
                            _ref_provider = _version_cfg.get("provider", _ref_provider)
                            _api_method = _version_cfg.get("api_method")

                            if _ref_version:
                                _ref_versions = _ref_model_cfg.get("versions", {})
                                _ref_failover = []
                                if _ref_version in _ref_versions:
                                    _ref_failover.append(_ref_version)
                                for _vn in _ref_versions:
                                    if _vn not in _ref_failover:
                                        _ref_failover.append(_vn)

                                _REF_ATTEMPTS_PER_VERSION = 2
                                _ref_rephrased = False
                                _ref_prompt = get_prompt_loader().get(
                                    "shared_venue_ref_to_video",
                                    motion_prompt=c_motion,
                                )

                                for _ref_vi, _ref_ver in enumerate(_ref_failover):
                                    if _venue_ref_video:
                                        break
                                    _ver_cfg = _ref_versions.get(_ref_ver, {})
                                    _ver_provider = _ver_cfg.get("provider", _ref_provider)
                                    _ver_api_method = _ver_cfg.get("api_method", _api_method)

                                    for _ref_attempt in range(_REF_ATTEMPTS_PER_VERSION):
                                        if _venue_ref_video:
                                            break
                                        _is_first_overall = (_ref_vi == 0 and _ref_attempt == 0)
                                        _attempt_label = f"{_ref_ver} attempt {_ref_attempt + 1}/{_REF_ATTEMPTS_PER_VERSION}"

                                        if not _is_first_overall:
                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video {'retry' if _ref_attempt > 0 else 'failover'} -> {_attempt_label} ({_ver_provider})")

                                        try:
                                            _ref_meta = {}
                                            if not _is_first_overall:
                                                video_semaphore.acquire()
                                            try:
                                                c_vid_url = processor._generate_video(
                                                    video_model=_ref_ver, video_provider=_ver_provider,
                                                    image_url=ref_img_url,
                                                    reference_image_urls=[first_frame_url] + influencer_urls,
                                                    motion_prompt=_ref_prompt, duration=c_dur,
                                                    resolution=video_resolution, result_metadata=_ref_meta,
                                                    api_method=_ver_api_method,
                                                )
                                            finally:
                                                if not _is_first_overall:
                                                    video_semaphore.release()

                                            if c_vid_url:
                                                _venue_ref_video = True
                                                logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video succeeded ({_attempt_label})")
                                                if on_progress:
                                                    usage_data = {
                                                        "service": _ver_provider, "model": _ref_ver,
                                                        "provider": _ver_provider, "category": "video_generation",
                                                        "duration_seconds": c_dur,
                                                        "step": f"venue_ref_video_scene_{scene_num}_clip_{ci}",
                                                        "label": f"Venue ref-to-video scene {scene_num} clip {ci}",
                                                        "success": True,
                                                    }
                                                    on_progress("usage", usage_data)
                                                    usage_list.append(usage_data)
                                                    on_progress("intermediate", {
                                                        "key": f"ref_venue_video_s{scene_num}_c{ci}",
                                                        "value": c_vid_url,
                                                    })
                                            else:
                                                logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video returned None ({_attempt_label})")
                                        except (VeoRAIBlockedError, VeoPromptBlockedError) as block_err:
                                            logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video blocked ({_attempt_label}): {block_err}")
                                            c_vid_url = None
                                            if on_progress:
                                                usage_data = {
                                                    "service": _ver_provider, "model": _ref_ver,
                                                    "provider": _ver_provider, "category": "video_generation",
                                                    "duration_seconds": c_dur,
                                                    "step": f"venue_ref_video_scene_{scene_num}_clip_{ci}",
                                                    "label": f"Venue ref-to-video scene {scene_num} clip {ci} (blocked)",
                                                    "success": False,
                                                }
                                                on_progress("usage", usage_data)
                                                usage_list.append(usage_data)
                                        except Exception as ref_err:
                                            logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video error ({_attempt_label}): {ref_err}")
                                            c_vid_url = None

                                # ---- Rephrase and retry the full ref-to-video loop ----
                                if not _venue_ref_video and not _ref_rephrased:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: all ref-to-video versions failed, rephrasing and retrying full loop...")
                                    try:
                                        rephrase_system = get_prompt_loader().get("shared_rephrase_blocked_prompt_system")
                                        rephrase_user = get_prompt_loader().get(
                                            "shared_rephrase_blocked_prompt_user",
                                            original_prompt=_ref_prompt,
                                            error_message="Video generation produced content with no video stream, likely blocked by provider safety filter",
                                        )
                                        rephrased_result = processor._call_llm(
                                            "rephrase_blocked_prompt",
                                            [{"role": "system", "content": rephrase_system},
                                             {"role": "user", "content": rephrase_user}],
                                        )
                                        rephrased = (rephrased_result.get("text") or "") if isinstance(rephrased_result, dict) else (rephrased_result or "")
                                        if rephrased.strip():
                                            _ref_prompt = rephrased.strip().strip('"').strip("'")
                                            _ref_rephrased = True
                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: rephrased ref-to-video prompt: {_ref_prompt[:120]}...")
                                            # Re-run the full version loop with rephrased prompt
                                            for _ref_vi, _ref_ver in enumerate(_ref_failover):
                                                if _venue_ref_video:
                                                    break
                                                _ver_cfg = _ref_versions.get(_ref_ver, {})
                                                _ver_provider = _ver_cfg.get("provider", _ref_provider)
                                                _ver_api_method = _ver_cfg.get("api_method", _api_method)
                                                for _ref_attempt in range(_REF_ATTEMPTS_PER_VERSION):
                                                    if _venue_ref_video:
                                                        break
                                                    _attempt_label = f"{_ref_ver} attempt {_ref_attempt + 1}/{_REF_ATTEMPTS_PER_VERSION} (rephrased)"
                                                    logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video retry -> {_attempt_label} ({_ver_provider})")
                                                    try:
                                                        _ref_meta = {}
                                                        with video_semaphore:
                                                            c_vid_url = processor._generate_video(
                                                                video_model=_ref_ver, video_provider=_ver_provider,
                                                                image_url=ref_img_url,
                                                                reference_image_urls=[first_frame_url] + influencer_urls,
                                                                motion_prompt=_ref_prompt, duration=c_dur,
                                                                resolution=video_resolution, result_metadata=_ref_meta,
                                                                api_method=_ver_api_method,
                                                            )
                                                        if c_vid_url:
                                                            _venue_ref_video = True
                                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video rephrase succeeded ({_attempt_label})")
                                                            if on_progress:
                                                                usage_data = {
                                                                    "service": _ver_provider, "model": _ref_ver,
                                                                    "provider": _ver_provider, "category": "video_generation",
                                                                    "duration_seconds": c_dur,
                                                                    "step": f"venue_ref_video_scene_{scene_num}_clip_{ci}",
                                                                    "label": f"Venue ref-to-video scene {scene_num} clip {ci} (rephrased)",
                                                                    "success": True,
                                                                }
                                                                on_progress("usage", usage_data)
                                                                usage_list.append(usage_data)
                                                                on_progress("intermediate", {
                                                                    "key": f"ref_venue_video_s{scene_num}_c{ci}",
                                                                    "value": c_vid_url,
                                                                })
                                                        else:
                                                            logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video returned None ({_attempt_label})")
                                                    except (VeoRAIBlockedError, VeoPromptBlockedError) as block_err:
                                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video blocked ({_attempt_label}): {block_err}")
                                                        c_vid_url = None
                                                        if on_progress:
                                                            usage_data = {
                                                                "service": _ver_provider, "model": _ref_ver,
                                                                "provider": _ver_provider, "category": "video_generation",
                                                                "duration_seconds": c_dur,
                                                                "step": f"venue_ref_video_scene_{scene_num}_clip_{ci}",
                                                                "label": f"Venue ref-to-video scene {scene_num} clip {ci} (rephrased, blocked)",
                                                                "success": False,
                                                            }
                                                            on_progress("usage", usage_data)
                                                            usage_list.append(usage_data)
                                                    except Exception as ref_err:
                                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video error ({_attempt_label}): {ref_err}")
                                                        c_vid_url = None
                                    except Exception as rephrase_err:
                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video rephrase failed: {rephrase_err}")

                                if not _venue_ref_video:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: all ref-to-video versions failed after NB2 first frame, falling back to regular animation")

                        # If NB2 first frame succeeded but ref-to-video failed, use first frame as ref_img_url for regular animation
                        if first_frame_url and not _venue_ref_video:
                            ref_img_url = first_frame_url
                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: using NB2 first frame for regular animation fallback")

                    if _venue_ref_video and c_vid_url:
                        # Ref-to-video already produced the clip ????? probe duration and store
                        c_actual = c_dur
                        try:
                            c_actual = processor.rendi_service.get_video_duration_cloud(c_vid_url)
                        except Exception:
                            pass
                        beat_clip_results.append({"url": c_vid_url, "duration": c_dur, "type": "image", "_pre_trimmed": False})
                        logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref-to-video clip ({c_actual:.1f}s, will use {c_dur:.1f}s)")
                    elif ref_img_url and not _venue_ref_video:
                        _ref_animated = False
                        c_vid_url = None
                        _actual_meta = {}  # Tracks which model/provider actually produced the clip

                        # --- Primary attempt with content-block retry ---
                        with video_semaphore:
                            try:
                                c_vid_url = processor._generate_video(
                                    image_url=ref_img_url, motion_prompt=c_motion,
                                    video_model=video_model, video_provider=video_provider,
                                    duration=c_dur, resolution=video_resolution,
                                    result_metadata=_actual_meta,
                                )
                            except VeoPromptBlockedError as pb_err:
                                # Layer 1a: LLM rephrase
                                logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Prompt blocked, rephrasing via LLM...")
                                try:
                                    rephrase_system = get_prompt_loader().get("shared_rephrase_blocked_prompt_system")
                                    rephrase_user = get_prompt_loader().get(
                                        "shared_rephrase_blocked_prompt_user",
                                        original_prompt=c_motion,
                                        error_message=pb_err.original_message[:300],
                                    )
                                    rephrased_result = processor._call_llm(
                                        "rephrase_blocked_prompt",
                                        [{"role": "system", "content": rephrase_system},
                                         {"role": "user", "content": rephrase_user}],
                                    )
                                    rephrased = (rephrased_result.get("text") or "") if isinstance(rephrased_result, dict) else (rephrased_result or "")
                                    if rephrased.strip():
                                        rephrased = rephrased.strip().strip('"').strip("'")
                                        logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Rephrased: {rephrased[:120]}...")
                                        c_vid_url = processor._generate_video(
                                            image_url=ref_img_url, motion_prompt=rephrased,
                                            video_model=video_model, video_provider=video_provider,
                                            duration=c_dur, resolution=video_resolution,
                                            result_metadata=_actual_meta,
                                        )
                                        if c_vid_url:
                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Rephrase retry succeeded")
                                except Exception as rephrase_err:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Rephrase retry failed: {rephrase_err}")
                            except VeoRAIBlockedError as rai_err:
                                # Layer 1b: Softened prompt retry
                                logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: RAI blocked ({rai_err.reason}), retrying with softened prompt...")
                                softened = (
                                    "Safe for all audiences. No violence, weapons, drugs, or explicit content. "
                                    "Family-friendly commercial style. " + c_motion
                                )
                                try:
                                    c_vid_url = processor._generate_video(
                                        image_url=ref_img_url, motion_prompt=softened,
                                        video_model=video_model, video_provider=video_provider,
                                        duration=c_dur, resolution=video_resolution,
                                        result_metadata=_actual_meta,
                                    )
                                    if c_vid_url:
                                        logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: RAI softened retry succeeded")
                                except (VeoRAIBlockedError, VeoPromptBlockedError) as retry_err:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: softened retry also blocked ({retry_err})")
                                    # c_vid_url stays None ????? falls through to Layer 2
                            except Exception as ae:
                                logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref image animation failed ({ae})")

                        # --- Layer 2: Provider failover if primary failed ---
                        if not c_vid_url and video_model and video_model.startswith("veo"):
                            if _p_defaults.get("video_failover_enabled", False):
                                fb_chain = _p_defaults.get("video_failover_chain", {})
                                fb_list = None
                                for prefix, models in fb_chain.items():
                                    if video_model.startswith(prefix):
                                        fb_list = models
                                        break
                                if fb_list:
                                    for fb_model in fb_list:
                                        fb_dur = snap_duration(fb_model, c_dur)
                                        fb_provider = "direct" if fb_model.startswith("runway-gen") else "kie"
                                        logger.warning(
                                            f"   [Row {row_num}] Scene {scene_num} clip {ci}: "
                                            f"content-block failover {video_model} -> {fb_model}"
                                        )
                                        with video_semaphore:
                                            c_vid_url = processor._generate_video(
                                                image_url=ref_img_url, motion_prompt=c_motion,
                                                video_model=fb_model, video_provider=fb_provider,
                                                duration=fb_dur, resolution=video_resolution,
                                                result_metadata=_actual_meta,
                                            )
                                        if c_vid_url:
                                            _actual_meta = {"model": fb_model, "provider": fb_provider}
                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: content-block failover succeeded ({fb_model})")
                                            break

                        if c_vid_url:
                            _raw_vid_url = c_vid_url  # preserve pre-trim URL
                            c_actual = 0
                            try:
                                c_actual = processor.rendi_service.get_video_duration_cloud(c_vid_url)
                            except Exception:
                                c_actual = c_dur

                            # Skip static warmup if the model has one (e.g., Veo needs 1s skip)
                            _warmup = _get_warmup_skip(processor, video_model)
                            if _warmup > 0 and c_actual >= c_dur + _warmup:
                                try:
                                    _trimmed = processor.rendi_service.trim_video(
                                        video_url=c_vid_url, duration=round(c_dur, 2),
                                        start_time=_warmup, has_audio=False,
                                    )
                                    if _trimmed:
                                        logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: skipped {_warmup}s warmup ({video_model})")
                                        c_vid_url = _trimmed
                                except Exception as _we:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: warmup trim failed ({_we}), using raw video")

                            # Store Director's intended duration ????? Rendi concat will trim automatically
                            _is_pre_trimmed = (_warmup > 0 and c_actual >= c_dur + _warmup)
                            beat_clip_results.append({"url": c_vid_url, "raw_url": _raw_vid_url, "duration": c_dur, "type": "image", "_pre_trimmed": _is_pre_trimmed})
                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: ref image {c_img_idx} animated ({c_actual:.1f}s, will use {c_dur:.1f}s)")
                            _ref_animated = True
                            # Track surprise clips for end card
                            if clip.get("variant") == "surprise" and c_vid_url:
                                _surprise_clips_generated.append({
                                    "url": c_vid_url,
                                    "source_image_url": ref_img_url,
                                    "description": clip.get("description", ""),
                                    "motion_prompt": c_motion,
                                    "image_index": c_img_idx,
                                })
                            if on_progress:
                                _used_model = _actual_meta.get("model", video_model)
                                _used_provider = _actual_meta.get("provider", video_provider or "direct")
                                usage_data = {
                                    "service": _used_provider,
                                    "step": f"smart_animate_scene_{scene_num}_clip_{ci}",
                                    "model": _used_model, "provider": _used_provider, "count": 1,
                                    "label": f"Smart animate ref image scene {scene_num} clip {ci}",
                                    "category": "video_generation", "success": True,
                                }
                                on_progress("usage", usage_data)
                                usage_list.append(usage_data)
                                on_progress("artifact", {
                                    "name": f"smart_beat_clip_s{scene_num}_c{ci}",
                                    "data": {"url": c_vid_url, "raw_url": _raw_vid_url, "type": "image", "duration": c_dur,
                                             "source": c_img_idx},
                                })
                            # Motion check for animated ref images
                            _motion_check_enabled = _p_defaults.get("motion_check_enabled", False)
                            _motion_max_retries = _p_defaults.get("motion_check_max_retries", 1)
                            if _motion_check_enabled and not simulation:
                                has_motion = processor.rendi_service.detect_motion(c_vid_url)
                                if not has_motion:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: no motion detected, regenerating...")
                                    for retry in range(_motion_max_retries):
                                        with video_semaphore:
                                            retry_vid = processor._generate_video(
                                                image_url=ref_img_url, motion_prompt=c_motion,
                                                video_model=video_model, video_provider=video_provider,
                                                duration=c_dur, resolution=video_resolution,
                                            )
                                        if retry_vid:
                                            retry_trimmed = processor.rendi_service.trim_video(
                                                retry_vid, round(c_dur, 2), start_time=_warmup, has_audio=False,
                                            )
                                            if retry_trimmed and processor.rendi_service.detect_motion(retry_trimmed):
                                                beat_clip_results[-1]["url"] = retry_trimmed
                                                beat_clip_results[-1]["_pre_trimmed"] = True
                                                logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: motion retry {retry+1} succeeded")
                                                break
                                    else:
                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: all motion retries failed, using as-is")

                        # Fallback to Ken Burns if AI animation failed
                        if not _ref_animated:
                            try:
                                logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: falling back to Ken Burns for ref image {c_img_idx}")
                                kb_video = processor.rendi_service.create_video_from_image(
                                    image_url=ref_img_url, duration=c_dur
                                )
                                if kb_video:
                                    beat_clip_results.append({"url": kb_video, "duration": c_dur, "_pre_trimmed": True})
                            except Exception:
                                logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Ken Burns fallback also failed")

                elif ctype == "generate":
                    # Generated clip ????? create image + animate
                    c_prompt = clip.get("first_prompt", clip.get("description", ""))
                    c_motion = clip.get("second_prompt", "Slow, subtle movement")
                    if not c_prompt:
                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: generate clip has no prompt, skipping")
                        continue
                    try:
                        c_shows_inf = clip.get("shows_influencer", False)
                        c_img_kw = dict(
                            image_model=image_model, image_provider=image_provider,
                            resolution=image_resolution, image_prompt=c_prompt,
                        )
                        if c_shows_inf and influencer_urls:
                            c_img_kw["character_reference_urls"] = influencer_urls
                            c_img_kw["has_character"] = True
                        # Smart mode: pass logo as reference image for closing beat's generated clips
                        if is_last_scene and logo_url and _is_likely_image_url(logo_url) and not c_shows_inf:
                            c_img_kw["product_reference_urls"] = [logo_url]
                            logo_ref_desc = "This is a LOGO image. Recreate this exact logo prominently in the scene."
                            if _has_slogan:
                                logo_ref_desc += f" Include slogan text: '{slogan_text}'"
                            c_img_kw["product_description"] = logo_ref_desc
                            c_img_kw["has_character"] = False
                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: closing beat — injecting logo reference")
                        c_img_url = processor._generate_image(**c_img_kw)
                        if c_img_url:
                            c_vid_url = None
                            _actual_meta = {}  # Tracks which model/provider actually produced the clip

                            # --- Primary attempt (with content-block retry) ---
                            with video_semaphore:
                                try:
                                    c_vid_url = processor._generate_video(
                                        image_url=c_img_url, motion_prompt=c_motion,
                                        video_model=video_model, video_provider=video_provider,
                                        duration=c_dur, resolution=video_resolution,
                                        result_metadata=_actual_meta,
                                    )
                                except VeoPromptBlockedError as pb_err:
                                    # Layer 1: LLM rephrase (same logic as _generate_single_scene_video)
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Prompt blocked, rephrasing via LLM...")
                                    try:
                                        rephrase_system = get_prompt_loader().get("shared_rephrase_blocked_prompt_system")
                                        rephrase_user = get_prompt_loader().get(
                                            "shared_rephrase_blocked_prompt_user",
                                            original_prompt=c_motion,
                                            error_message=pb_err.original_message[:300],
                                        )
                                        rephrased_result = processor._call_llm(
                                            "rephrase_blocked_prompt",
                                            [{"role": "system", "content": rephrase_system},
                                             {"role": "user", "content": rephrase_user}],
                                        )
                                        rephrased = (rephrased_result.get("text") or "") if isinstance(rephrased_result, dict) else (rephrased_result or "")
                                        if rephrased.strip():
                                            rephrased = rephrased.strip().strip('"').strip("'")
                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Rephrased: {rephrased[:120]}...")
                                            c_vid_url = processor._generate_video(
                                                image_url=c_img_url, motion_prompt=rephrased,
                                                video_model=video_model, video_provider=video_provider,
                                                duration=c_dur, resolution=video_resolution,
                                                result_metadata=_actual_meta,
                                            )
                                            if c_vid_url:
                                                logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Rephrase retry succeeded")
                                    except Exception as rephrase_err:
                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: Rephrase retry failed: {rephrase_err}")

                                except VeoRAIBlockedError as rai_err:
                                    # Layer 1b: Softened prompt retry
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: RAI blocked ({rai_err.reason}), retrying with softened prompt...")
                                    softened = (
                                        "Safe for all audiences. No violence, weapons, drugs, or explicit content. "
                                        "Family-friendly commercial style. " + c_motion
                                    )
                                    try:
                                        c_vid_url = processor._generate_video(
                                            image_url=c_img_url, motion_prompt=softened,
                                            video_model=video_model, video_provider=video_provider,
                                            duration=c_dur, resolution=video_resolution,
                                            result_metadata=_actual_meta,
                                        )
                                        if c_vid_url:
                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: RAI softened retry succeeded")
                                    except (VeoRAIBlockedError, VeoPromptBlockedError) as retry_err:
                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: softened retry also blocked ({retry_err})")
                                        # c_vid_url stays None ????? falls through to Layer 2 (provider failover)

                            # --- Layer 2: Provider failover if primary failed ---
                            if not c_vid_url and video_model and video_model.startswith("veo"):
                                if _p_defaults.get("video_failover_enabled", False):
                                    fb_chain = _p_defaults.get("video_failover_chain", {})
                                    fb_list = None
                                    for prefix, models in fb_chain.items():
                                        if video_model.startswith(prefix):
                                            fb_list = models
                                            break
                                    if fb_list:
                                        for fb_model in fb_list:
                                            fb_dur = snap_duration(fb_model, c_dur)
                                            fb_provider = "direct" if fb_model.startswith("runway-gen") else "kie"
                                            logger.warning(
                                                f"   [Row {row_num}] Scene {scene_num} clip {ci}: "
                                                f"content-block failover {video_model} -> {fb_model}"
                                            )
                                            with video_semaphore:
                                                c_vid_url = processor._generate_video(
                                                    image_url=c_img_url, motion_prompt=c_motion,
                                                    video_model=fb_model, video_provider=fb_provider,
                                                    duration=fb_dur, resolution=video_resolution,
                                                    result_metadata=_actual_meta,
                                                )
                                            if c_vid_url:
                                                _actual_meta = {"model": fb_model, "provider": fb_provider}
                                                logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: content-block failover succeeded ({fb_model})")
                                                break

                            if c_vid_url:
                                _raw_vid_url = c_vid_url  # preserve pre-trim URL
                                c_actual = 0
                                try:
                                    c_actual = processor.rendi_service.get_video_duration_cloud(c_vid_url)
                                except Exception:
                                    c_actual = c_dur

                                # Skip static warmup if the model has one (e.g., Veo needs 1s skip)
                                _warmup = _get_warmup_skip(processor, video_model)
                                if _warmup > 0 and c_actual >= c_dur + _warmup:
                                    try:
                                        _trimmed = processor.rendi_service.trim_video(
                                            video_url=c_vid_url, duration=round(c_dur, 2),
                                            start_time=_warmup, has_audio=False,
                                        )
                                        if _trimmed:
                                            logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: skipped {_warmup}s warmup ({video_model})")
                                            c_vid_url = _trimmed
                                    except Exception as _we:
                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: warmup trim failed ({_we}), using raw video")

                                # Store Director's intended duration ????? Rendi concat will trim automatically
                                _is_pre_trimmed = False
                                beat_clip_results.append({"url": c_vid_url, "raw_url": _raw_vid_url, "duration": c_dur, "type": "generate", "_pre_trimmed": _is_pre_trimmed})
                                logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: generated ({c_actual:.1f}s, will use {c_dur:.1f}s)")
                                if on_progress:
                                    on_progress("artifact", {
                                        "name": f"smart_beat_clip_s{scene_num}_c{ci}",
                                        "data": {"url": c_vid_url, "raw_url": _raw_vid_url, "type": "generate", "duration": c_dur,
                                                 "source": None},
                                    })
                                # Motion check for generated clips
                                _motion_check_enabled = _p_defaults.get("motion_check_enabled", False)
                                _motion_max_retries = _p_defaults.get("motion_check_max_retries", 1)
                                if _motion_check_enabled and not simulation:
                                    has_motion = processor.rendi_service.detect_motion(c_vid_url)
                                    if not has_motion:
                                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: no motion detected, regenerating...")
                                        for retry in range(_motion_max_retries):
                                            with video_semaphore:
                                                retry_vid = processor._generate_video(
                                                    image_url=c_img_url, motion_prompt=c_motion,
                                                    video_model=video_model, video_provider=video_provider,
                                                    duration=c_dur, resolution=video_resolution,
                                                )
                                            if retry_vid:
                                                _warmup_gen = _get_warmup_skip(processor, video_model)
                                                retry_trimmed = retry_vid
                                                if _warmup_gen > 0:
                                                    try:
                                                        _rt = processor.rendi_service.trim_video(
                                                            retry_vid, round(c_dur, 2), start_time=_warmup_gen, has_audio=False,
                                                        )
                                                        if _rt:
                                                            retry_trimmed = _rt
                                                    except Exception:
                                                        pass
                                                if processor.rendi_service.detect_motion(retry_trimmed):
                                                    beat_clip_results[-1]["url"] = retry_trimmed
                                                    beat_clip_results[-1]["_pre_trimmed"] = True
                                                    logger.info(f"   [Row {row_num}] Scene {scene_num} clip {ci}: motion retry {retry+1} succeeded")
                                                    break
                                        else:
                                            logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: all motion retries failed, using as-is")
                            else:
                                logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: generate video failed (all retries exhausted)")
                        else:
                            logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: generate image failed")
                    except Exception as ge:
                        logger.warning(f"   [Row {row_num}] Scene {scene_num} clip {ci}: generate failed ({ge})")

            if beat_clip_results:
                scene_filler_videos[scene_idx] = beat_clip_results
                # Set scene_videos so the scene isn't considered "missing"
                scene_videos[scene_idx] = beat_clip_results[0]["url"]
                # Populate scene_images with last ref image so Ken Burns filler works if needed
                for bc in reversed(beat_clips):
                    if bc.get("type") == "image":
                        c_img_idx = bc.get("reference_image_index")
                        if c_img_idx is not None and 0 <= c_img_idx < len(ref_image_analyses):
                            scene_images[scene_idx] = ref_image_analyses[c_img_idx].get("url")
                            break
                logger.info(f"   [Row {row_num}] Scene {scene_num}: {len(beat_clip_results)} beat clips processed (total {sum(r['duration'] for r in beat_clip_results):.1f}s)")
                return (scene_idx, None, beat_clip_results[0]["url"])
            else:
                logger.warning(f"   [Row {row_num}] Scene {scene_num}: all beat clips failed, falling through to generation")
                # Fall through to standard image gen + animation below

        elif _smart_mode and vid_ref is not None and isinstance(vid_ref, int) and 0 <= vid_ref < len(asset_analyses):
            # Legacy smart mode path (no beat_clips but has video_asset_index)
            asset = asset_analyses[vid_ref]
            if simulation:
                scene_videos[scene_idx] = _SIM_VIDEO
                logger.info(f"   [Row {row_num}] Scene {scene_num}: [SIM] Smart trim (video {vid_ref})")
                return (scene_idx, None, _SIM_VIDEO)
            asset_url = asset.get("url", "")
            asset_dur = asset.get("duration_seconds", 0)
            best_mi = scene.get("best_moment_index")
            scene_dur = scene.get("planner_duration", duration)
            trim_start, trim_end = 0.0, asset_dur
            if best_mi == -1 or best_mi is None:
                if asset_dur > scene_dur:
                    trim_end = scene_dur
            elif isinstance(best_mi, int) and 0 <= best_mi < len(asset.get("key_moments", [])):
                moment = asset["key_moments"][best_mi]
                trim_start = moment["start_seconds"]
                trim_end = moment["end_seconds"]
            logger.info(f"   [Row {row_num}] Scene {scene_num}: smart trim video {vid_ref} moment={best_mi} [{trim_start:.1f}-{trim_end:.1f}s]")
            try:
                trimmed_url = processor.rendi_service.trim_video(
                    video_url=asset_url, duration=round(trim_end - trim_start, 2),
                    start_time=round(trim_start, 2), has_audio=False,
                )
                if trimmed_url:
                    scene_videos[scene_idx] = trimmed_url
                    if on_progress:
                        usage_data = {
                            "service": "rendi", "step": f"smart_trim_scene_{scene_num}",
                            "model": "rendi", "provider": "rendi", "count": 1,
                            "label": f"Smart trim scene {scene_num}", "category": "ffmpeg", "success": True,
                        }
                        on_progress("usage", usage_data)
                        usage_list.append(usage_data)
                    return (scene_idx, None, trimmed_url)
                else:
                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: smart trim failed, falling through to generation")
            except Exception as trim_err:
                logger.error(f"   [Row {row_num}] Scene {scene_num}: smart trim error ({trim_err}), falling through to generation")

        if not image_prompt:
            return (scene_idx, None, None)

        # Force image to match VO: prepend what the viewer hears so the image shows exactly that
        vo_snippet = scene.get("_vo_text_for_image") or scene.get("vo_text")
        if vo_snippet:
            vo_clean = (vo_snippet[:500] + "...") if len(vo_snippet) > 500 else vo_snippet
            image_prompt = f"""CRITICAL ????? In this scene the viewer HEARS: "{vo_clean}". The image MUST show exactly this moment and nothing else. No generic or decorative imagery. Visual must match the story being told.

{image_prompt}"""

        # Simulation: skip real image + video generation
        if simulation:
            scene_images[scene_idx] = _SIM_IMAGE
            scene_videos[scene_idx] = _SIM_VIDEO
            logger.info(f"   [Row {row_num}] Scene {scene_num}: [SIM] Image + Video generated")
            return (scene_idx, _SIM_IMAGE, _SIM_VIDEO)

        # Get reference image for this scene:
        # 1) If model set reference_image_index to a valid int ????? use that image
        # 2) If model explicitly set null ????? NO ref image (generate fresh from prompt)
        # 3) If key missing entirely ????? semantic fallback ????? cycle fallback
        ref_url = None
        ref_image_index = scene.get("reference_image_index", "__missing__")
        if ref_image_index is None:
            # Model explicitly chose null = no reference image fits this scene
            logger.debug(f"   Scene {scene_idx+1}: reference_image_index=null (model chose no ref image)")
        elif ref_image_index != "__missing__" and valid_ref_images and isinstance(ref_image_index, int) and 0 <= ref_image_index < len(valid_ref_images):
            ref_url = valid_ref_images[ref_image_index]
        elif valid_ref_images:
            best_idx = _pick_reference_image_index_for_scene(processor, 
                scene=scene,
                ref_image_analyses=ref_image_analyses,
                fallback_position=scene_idx
            )
            if best_idx is not None and 0 <= best_idx < len(valid_ref_images):
                ref_image_index = best_idx
                ref_url = valid_ref_images[best_idx]
                scene["reference_image_index"] = best_idx
            else:
                ref_url = valid_ref_images[scene_idx % len(valid_ref_images)]
        
        # Log which reference image was chosen for this scene (for transparency)
        if ref_url and valid_ref_images:
            chosen_idx = next((i for i, u in enumerate(valid_ref_images) if u == ref_url), None)
            if chosen_idx is not None:
                logger.info(f"   [Row {row_num}] Scene {scene_num}: using reference image {chosen_idx + 1} (Image {chosen_idx + 1})")
            else:
                logger.info(f"   [Row {row_num}] Scene {scene_num}: using reference image (fallback)")
        else:
            logger.info(f"   [Row {row_num}] Scene {scene_num}: no reference image (model chose null ????? generating from prompt only)")
        
        # For CTA scene in legacy mode: logo injected into image prompt + used as reference.
        # In smart mode: logo is passed via Director+Writer prompt context and as reference in beat clip gen.
        is_cta_scene = is_last_scene
        scene_logo_url = logo_url if (is_cta_scene and not _smart_mode) else None

        if is_cta_scene and not _smart_mode:
            logger.info(f"   [Row {row_num}] Scene {scene_num} is CTA scene. Logo URL: {scene_logo_url or 'None'}")
        
        if is_cta_scene and scene_logo_url:
            slogan_line = f"\nBelow or near the logo, add the slogan text: '{slogan_text}' in a clean, elegant font (display this exact text in the image)." if _has_slogan else ""
            image_prompt = f"""{image_prompt}
CLOSING/CTA SCENE: A logo image is provided as reference ????? recreate and integrate this EXACT logo prominently.{slogan_line}
The background should be beautiful and connected to the story ????? warm colors, soft bokeh lights, or a stylish gradient.
Make it look like a professional brand advertisement ending card. The logo must be CLEARLY VISIBLE and RECOGNIZABLE."""
        elif is_cta_scene and _has_slogan:
            image_prompt = f"""{image_prompt}
This is the CLOSING scene. Create a beautiful ending card with the slogan: '{slogan_text}' displayed prominently in an elegant font (show this exact text in the image). The background should be warm and inviting, connected to the story."""
        elif is_cta_scene and not is_influencer_scene:
            image_prompt = f"""Clean, professional CTA ending card ????? clearly connected to this video's story and offer. No people, no characters. Background and mood must fit the product/offer. {image_prompt}"""
        
        # Step 1: Generate image (or use cached from Studio / existing_intermediates)
        image_url = None
        if _skip_images and scene_images[scene_idx]:
            image_url = scene_images[scene_idx]
            logger.info(f"   [Row {row_num}] Scene {scene_num}: Using cached image from existing_intermediates ????? animating")
        if not image_url:
            with scene_image_semaphore:
                try:
                    # For influencer scenes in UGC, enhance prompt to place influencer IN the environment
                    scene_image_prompt = image_prompt
                    if is_influencer_scene:
                        env_instructions = ""
                        if ref_url:
                            env_instructions = """- Use the REFERENCE IMAGE as INSPIRATION for the environment/setting
- Match the reference's lighting, colors, textures, and atmosphere
- CREATE A BRAND NEW SCENE that places the influencer naturally INSIDE this type of environment"""
                        scene_image_prompt = get_prompt_loader().get(
                            "ugc_influencer_scene_instructions",
                            image_prompt=image_prompt,
                            env_instructions=env_instructions,
                        )

                    # For CTA scene with logo: send logo as the PRIMARY reference image
                    scene_image_kw = dict(
                        image_prompt=scene_image_prompt,
                        product_visible=True,
                        visual_style=ugc_style,
                        character_reference_urls=influencer_urls if is_influencer_scene else None,
                        has_character=is_influencer_scene,
                        logo_reference_url=scene_logo_url,
                        is_cta_scene=is_cta_scene
                    )
                    if is_cta_scene and scene_logo_url and _is_likely_image_url(scene_logo_url):
                        scene_image_kw["product_reference_urls"] = [scene_logo_url]
                        logo_desc = "This is a LOGO image. Recreate this exact logo prominently in the center of the scene."
                        if _has_slogan:
                            logo_desc += f" Slogan (display this text in the image): '{slogan_text}'"
                        scene_image_kw["product_description"] = logo_desc
                        scene_image_kw["character_reference_url"] = None
                        scene_image_kw["has_character"] = False
                        scene_image_kw["logo_reference_url"] = None
                    elif is_cta_scene and scene_logo_url:
                        logger.warning(f"   [Row {row_num}] Logo URL does not look like an image ({scene_logo_url[:60]}...), skipping as reference")
                        scene_image_kw["product_reference_urls"] = None
                    else:
                        _ref_urls = [ref_url] if ref_url and _is_likely_image_url(ref_url) else None
                        scene_image_kw["product_reference_urls"] = _ref_urls
                        scene_image_kw["product_description"] = text_1 if not is_influencer_scene else f"Environment/location reference. Place the influencer in this setting: {text_1[:200]}"
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
                                    image_url=image_url, original_prompt=scene_image_prompt,
                                )
                                if quality_score is not None and quality_score < image_quality_threshold:
                                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: Low quality image (score {quality_score}/10), regenerating...")
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
                except Exception as e:
                    logger.error(f"   [Row {row_num}] Scene {scene_num}: Image error - {e}")
                    return (scene_idx, None, None)

        if not image_url:
            if use_google_image:
                reason = getattr(processor.gemini_image_service, "last_failure_reason", "") or "no image returned (possible rate limit 429 or content/safety block)"
                logger.error(f"   [Row {row_num}] Scene {scene_num}: Image generation failed - scene will be skipped (Gemini: {reason})")
            else:
                reason = getattr(processor.kie_service, "last_failure_reason", "") or "no image returned"
                logger.error(f"   [Row {row_num}] Scene {scene_num}: Image generation failed - scene will be skipped (Kie/Nano Banana: {reason})")
            return (scene_idx, None, None)
        
        # Step 2: Generate video
        # Clean motion prompt to avoid Veo 3 content policy violations
        # AND remove any "talking/speaking" descriptions - VO is added separately
        clean_motion_prompt = motion_prompt
        # Remove phrases that trigger content filters
        for phrase in ["call to action", "call-to-action", "CTA", "click", "subscribe", "buy now", "order now", "book now"]:
            clean_motion_prompt = clean_motion_prompt.replace(phrase, "inviting gesture")
            clean_motion_prompt = clean_motion_prompt.replace(phrase.title(), "inviting gesture")
            clean_motion_prompt = clean_motion_prompt.replace(phrase.upper(), "inviting gesture")
        
        # Remove brand/character names that trigger third-party content blocks
        brand_replacements = {
            r'\bMickey Mouse\b': 'beloved cartoon mascot character',
            r'\bMickey\b': 'cartoon mascot',
            r'\bMinnie Mouse\b': 'cartoon character with polka dot bow',
            r'\bMinnie\b': 'cartoon character',
            r'\bDonald Duck\b': 'cartoon duck character',
            r'\bGoofy\b': 'tall cartoon dog character',
            r'\bDumbo\b': 'flying elephant',
            r'\bSleeping Beauty\b': 'fairytale princess',
            r'\bCinderella\b': 'fairytale princess',
            r'\bDisney\b': 'magical theme park',
            r'\bDisneyland\b': 'magical theme park',
            r'\bDisney World\b': 'magical theme park resort',
            r'\bMagic Kingdom\b': 'fantasy kingdom',
            r'\bEPCOT\b': 'futuristic theme park',
            r'\bUniversal Studios\b': 'movie theme park',
            r'\bHarry Potter\b': 'wizard character',
            r'\bHogwarts\b': 'magical castle school',
            r'\bMarvel\b': 'superhero',
            r'\bStar Wars\b': 'space fantasy',
            r'\bPixar\b': 'animated',
            r'\bSpace Mountain\b': 'indoor roller coaster in the dark',
            r'\bBig Thunder\b': 'wild west roller coaster',
            r'\bPirates of the Caribbean\b': 'pirate adventure boat ride',
            r'\bIt\'s a Small World\b': 'colorful boat ride through miniature scenes',
        }
        for pattern, replacement in brand_replacements.items():
            clean_motion_prompt = re.sub(pattern, replacement, clean_motion_prompt, flags=re.IGNORECASE)
        
        # Remove talking/speaking/phone descriptions - influencer should NOT appear to talk or hold phone
        talking_replacements = {
            r'\btalking\b': 'reacting with excitement',
            r'\bspeaking\b': 'looking amazed',
            r'\bspeaks\b': 'reacts with surprise',
            r'\btalks\b': 'shows excitement',
            r'\baddressing the camera\b': 'looking at the camera with a surprised expression',
            r'\baddresses the camera\b': 'gazes at the camera with delight',
            r'\bsaying\b': 'expressing excitement',
            r'\bsays\b': 'shows delight',
            r'\bnarrating\b': 'observing with wonder',
            r'\bnarrates\b': 'observes with wonder',
            r'\bher lips move\b': 'her expression shifts to amazement',
            r'\bhis lips move\b': 'his expression shifts to amazement',
            r'\blip movements\b': 'facial expressions',
            r'\bmouth moves\b': 'expression changes',
            r'\bmouth moving\b': 'expression shifting',
            r'\bstarts to speak\b': 'reacts with surprise',
            r'\bbegins to speak\b': 'shows amazement',
            r'\bvocal\b': 'expressive',
            # Phone/selfie related - NEVER hold a phone or any object
            r'\bholding a phone\b': 'gesturing with excitement',
            r'\bholding phone\b': 'gesturing with excitement',
            r'\bholding her phone\b': 'gesturing with her hands',
            r'\bholding his phone\b': 'gesturing with his hands',
            r'\bwith phone\b': 'with open hands',
            r'\bwith a phone\b': 'with open hands',
            r'\bphone in hand\b': 'hands free and expressive',
            r'\btaking a selfie\b': 'looking around with delight',
            r'\btaking selfie\b': 'looking around with delight',
            r'\bfilming\b': 'exploring the scene',
            r'\brecording\b': 'experiencing the moment',
            r'\bvlogging\b': 'enjoying the atmosphere',
            r'\bholding up\b': 'showing off',
            r'\bpulls out\b': 'reaches toward',
            r'\btakes out\b': 'reaches toward',
        }
        for pattern, replacement in talking_replacements.items():
            clean_motion_prompt = re.sub(pattern, replacement, clean_motion_prompt, flags=re.IGNORECASE)
        
        # For influencer scenes, ensure the motion prompt describes a SPECIFIC ACTION (not talking)
        if is_influencer_scene:
            # Add explicit action instruction if the prompt seems too vague
            action_keywords = ['eating', 'tasting', 'touching', 'walking', 'spinning', 'leaning', 
                               'pointing', 'looking', 'smelling', 'breathing', 'running', 'picking',
                               'sipping', 'reaching', 'exploring', 'dancing', 'laughing', 'smiling',
                               'turning', 'gazing', 'examining', 'reacting', 'waving', 'gesturing']
            has_action = any(kw in clean_motion_prompt.lower() for kw in action_keywords)
            if not has_action:
                clean_motion_prompt += ". The person reacts with genuine amazement, eyes widening, turning their head to take in the surroundings with a delighted expression."
            
            # Always prepend a "no talking" safety instruction
            clean_motion_prompt = f"The person in the scene is NOT talking or speaking at any point. {clean_motion_prompt}"
        
        # Step 2b: Generate video via unified _generate_video() dispatch
        video_url = None
        _is_veo = video_model and video_model.startswith("veo")
        with video_semaphore:
            try:
                try:
                    video_url = processor._generate_video(
                        video_model=video_model,
                        video_provider=video_provider,
                        image_url=image_url,
                        motion_prompt=clean_motion_prompt,
                        duration=anim_duration,
                        resolution=video_resolution,
                    )
                except VeoPromptBlockedError as pb_err:
                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: Prompt blocked by provider, rephrasing via LLM...")
                    try:
                        rephrase_system = get_prompt_loader().get("shared_rephrase_blocked_prompt_system")
                        rephrase_user = get_prompt_loader().get(
                            "shared_rephrase_blocked_prompt_user",
                            original_prompt=clean_motion_prompt,
                            error_message=pb_err.original_message[:300],
                        )
                        rephrased_result = processor._call_llm(
                            "rephrase_blocked_prompt",
                            [{"role": "system", "content": rephrase_system},
                             {"role": "user", "content": rephrase_user}],
                        )
                        rephrased = (rephrased_result.get("text") or "") if isinstance(rephrased_result, dict) else (rephrased_result or "")
                        if rephrased.strip():
                            rephrased = rephrased.strip().strip('"').strip("'")
                            logger.info(f"   [Row {row_num}] Scene {scene_num}: Rephrased prompt: {rephrased[:120]}...")
                            video_url = processor._generate_video(
                                video_model=video_model,
                                video_provider=video_provider,
                                image_url=image_url,
                                motion_prompt=rephrased,
                                duration=anim_duration,
                                resolution=video_resolution,
                            )
                            if video_url:
                                logger.info(f"   [Row {row_num}] Scene {scene_num}: Prompt rephrase retry succeeded")
                    except Exception as rephrase_err:
                        logger.warning(f"   [Row {row_num}] Scene {scene_num}: Rephrase retry failed: {rephrase_err}")
                except VeoRAIBlockedError as rai_err:
                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: RAI blocked ({rai_err.reason}), retrying with softened prompt...")
                    softened_prompt = (
                        "Safe for all audiences. No violence, weapons, drugs, or explicit content. "
                        "Family-friendly commercial style. " + clean_motion_prompt
                    )
                    try:
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
                    except (VeoRAIBlockedError, VeoPromptBlockedError) as retry_err:
                        logger.warning(f"   [Row {row_num}] Scene {scene_num}: softened retry also blocked ({retry_err})")
                        # video_url stays None ????? falls through to dynamic prompt retry

                # If Veo failed (returns None), retry with a more dynamic prompt
                if not video_url and _is_veo:
                    veo_label = video_model
                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: {veo_label} failed, retrying with dynamic prompt...")
                    dynamic_prompt = "Cinematic camera push-in with smooth dolly movement. Gentle parallax effect with foreground elements slightly shifting. Subtle environmental motion: hair, clothing, leaves, or light naturally moving. Professional cinematic feel."
                    video_url = processor._generate_video(
                        video_model=video_model,
                        video_provider=video_provider,
                        image_url=image_url,
                        motion_prompt=dynamic_prompt,
                        duration=anim_duration,
                        resolution=video_resolution,
                    )

                # If Veo failed on both attempts -> Ken Burns fallback (better than skipping the scene)
                is_ken_burns_fallback = False
                if not video_url and _is_veo:
                    logger.warning(f"   [Row {row_num}] Scene {scene_num}: {video_model} failed twice -> falling back to Ken Burns ({duration:.1f}s)...")
                    try:
                        video_url = processor.rendi_service.create_video_from_image(
                            image_url=image_url,
                            duration=duration,
                            subtle_for_last_scene=(scene_idx == len(scenes) - 1)
                        )
                        if video_url:
                            is_ken_burns_fallback = True
                            logger.info(f"   [Row {row_num}] Scene {scene_num}: Ken Burns fallback created")
                    except Exception as kb_err:
                        logger.error(f"   [Row {row_num}] Scene {scene_num}: Ken Burns fallback also failed: {kb_err}")

                if video_url and video_model != "none":
                    # Trim the first 1 second from AI animations only (removes initial static/glitch frame)
                    # Skip trim for Ken Burns fallback - they start moving immediately
                    if not is_ken_burns_fallback:
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

                    # Loop Veo clips to fill gap when anim_duration exceeds Veo's 8s max.
                    # Veo can only produce up to 8s; scenes in long videos may need 10-15s.
                    # After trimming the 1s head, the actual usable clip is ~7s at most.
                    # Looping keeps the visual alive without generating a second API call.
                    _veo_max = 8
                    if (not is_ken_burns_fallback and video_url and _is_veo
                            and anim_duration > _veo_max):
                        loop_target = anim_duration
                        logger.info(
                            f"   [Row {row_num}] Scene {scene_num}: Veo max {_veo_max}s < "
                            f"required {anim_duration:.1f}s -> looping clip to fill"
                        )
                        try:
                            looped = processor.rendi_service.loop_video_to_duration(
                                video_url=video_url,
                                target_duration=loop_target,
                            )
                            if looped:
                                video_url = looped
                                logger.info(
                                    f"   [Row {row_num}] Scene {scene_num}: "
                                    f"looped to {loop_target:.1f}s"
                                )
                        except Exception as _loop_err:
                            logger.warning(
                                f"   [Row {row_num}] Scene {scene_num}: "
                                f"loop_video_to_duration failed: {_loop_err}"
                            )

                    # Phase 12: Precision sync ????? trim to exact millisecond locally
                    if _use_precision and _exact_dur and video_url and not is_ken_burns_fallback:
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
                    logger.error(f"   [Row {row_num}] Scene {scene_num}: Animation failed completely")
            except Exception as e:
                logger.error(f"   [Row {row_num}] Scene {scene_num}: Animation error - {e}")
        # Release semaphore before spacing sleep so other scenes can start Veo while this thread waits.
        if _scene_video_delay_sec > 0:
            time.sleep(_scene_video_delay_sec)
        return (scene_idx, image_url, video_url)

    # =====================================================================
    # TRACK 2: Asset Processing (insert as-is with slight zoom)
    # =====================================================================
    def process_asset(asset_idx, asset_info):
        """Process an asset - insert as-is with subtle zoom.

        asset_info: dict with {url, type, keep_audio} or plain URL string (legacy).
        """
        if isinstance(asset_info, dict):
            asset_url = asset_info["url"]
        else:
            asset_url = asset_info
        logger.info(f"   [Row {row_num}] Processing asset {asset_idx + 1}: {asset_url[:60]}...")
        if simulation:
            asset_videos[asset_idx] = _SIM_VIDEO
            logger.info(f"   [Row {row_num}] Asset {asset_idx + 1}: [SIM] Processed")
            return
        try:
            asset_video_url = _insert_asset_as_scene(processor,
                asset_url=asset_url,
                target_duration=3.0  # Maximum 3 seconds for assets
            )
            if asset_video_url:
                asset_videos[asset_idx] = asset_video_url
                logger.info(f"   [Row {row_num}] Asset {asset_idx + 1}: Processed")
            else:
                logger.warning(f"   [Row {row_num}] Asset {asset_idx + 1}: Processing failed")
        except Exception as e:
            logger.error(f"   [Row {row_num}] Asset {asset_idx + 1}: Error - {e}")
    
    # =====================================================================
    # TRACK 3: Music Generation
    # =====================================================================
    def generate_music_track():
        """Generate background music with Suno (music mood matches VO)."""
        logger.info(f"   [Row {row_num}] [Parallel] Starting music generation...")
        try:
            if simulation:
                music_result["url"] = _SIM_AUDIO
                logger.info(f"   [Row {row_num}] [SIM] Music generated")
                return
            music_description = generate_music_description_from_text(
                lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw),
                content_text=f"{text_1}\n{text_2}\n{text_3}",
                vo_script=vo_result.get("script", "") or "",
                video_subtype=video_subtype,
            )
            music_result["description"] = music_description
            music_url = processor.suno_service.generate_pure_music(
                style_description=music_description
            )
            if music_url:
                music_result["url"] = music_url
                logger.info(f"   [Row {row_num}] [Parallel] Music generated: {music_url[:60]}...")
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
                logger.warning(f"   [Row {row_num}] [Parallel] Music generation failed (Suno returned no URL ????? check Kie/Suno API, quota, or endpoint above)")
        except Exception as e:
            logger.warning(f"   [Row {row_num}] [Parallel] Music error: {e}")
    
    # =====================================================================
    # RUN SCENES + ASSETS + MUSIC IN PARALLEL (VO already done in Step 2.7)
    # =====================================================================
    if _skip_images and _skip_videos and music_result["url"]:
        logger.info(f"   [Row {row_num}] Skipping parallel generation ? using cached images, videos, and music from intermediates")
    elif _skip_images and _skip_videos and not music_result["url"]:
        # Without this branch, missing music_url forces the full parallel block and re-bills
        # every scene image/video on resume (e.g. Studio final assembly after animation review).
        logger.warning(
            f"   [Row {row_num}] Cached scene images/videos in intermediates but music_url is missing ? "
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

        logger.info(f"   [Row {row_num}] Launching parallel tracks: {len(scenes)} scenes + {len(valid_assets)} assets + music")

        with ThreadPoolExecutor(max_workers=len(scenes) + len(valid_assets) + 2) as executor:
            # Always run generate_scene_visual (image + video per scene). When _skip_images we use
            # cached scene_images and only run the animation step so Studio "Animate all" works.
            visual_futures = [
                executor_submit_with_progress(executor, generate_scene_visual, i, scene, i == len(scenes) - 1)
                for i, scene in enumerate(scenes)
            ]
            # Legacy mode: process assets separately. Smart mode: assets handled in generate_scene_visual.
            if not _smart_mode:
                asset_futures = [
                    executor_submit_with_progress(executor, process_asset, i, asset_info)
                    for i, asset_info in enumerate(valid_assets)
                ]
            else:
                asset_futures = []
            if not music_result["url"]:
                music_future = executor_submit_with_progress(executor, generate_music_track)
            else:
                music_future = None

            _visual_future_set = set(visual_futures)
            for future in as_completed(visual_futures + asset_futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"   [Row {row_num}] Task error: {e}")
                if on_progress and future in _visual_future_set and visual_futures:
                    try:
                        _sv_snap = [u if u else None for u in scene_videos]
                        on_progress("intermediate", {"key": "scene_videos", "value": _sv_snap})
                    except Exception as _pe:
                        logger.debug(f"   [Row {row_num}] scene_videos partial emit: {_pe}")

            if music_future:
                music_future.result()
    
    # =====================================================================
    # BARRIER: All parallel tasks (images, animations, assets, music) are
    # now complete.  Only AFTER this point do we proceed to fallback /
    # combine.  The ThreadPoolExecutor context-manager guarantees all
    # submitted threads have finished (shutdown(wait=True)).
    # =====================================================================
    animated_count = sum(1 for v in scene_videos if v)
    has_music = bool(music_result.get("url"))
    logger.info(f"   [Row {row_num}] ???? All parallel tasks finished ????? {animated_count}/{len(scenes)} scenes animated, "
                 f"{sum(1 for v in asset_videos if v)}/{len(valid_assets)} assets, music={'????' if has_music else '?????'}")
    if not has_music:
        logger.warning(f"   [Row {row_num}] ??????? No background music was generated. Possible causes: Kie/Suno API error, Suno quota, or Suno not enabled for your Kie key. Check log above for 'Suno' or 'Music' messages.")
    
    # =====================================================================
    # FALLBACK: Create Ken Burns videos from static images for failed scenes
    # This ensures EVERY scene has a video clip matching its VO segment,
    # so the final video stays in sync with the voice-over.
    # =====================================================================
    missing_video_indices = [i for i in range(len(scenes)) if scene_images[i] and not scene_videos[i]]
    if missing_video_indices:
        logger.warning(f"   [Row {row_num}] ??????? {len(missing_video_indices)}/{len(scenes)} scenes missing video ????? creating Ken Burns fallback from static images...")
        for i in missing_video_indices:
            scene_num = scenes[i].get("scene_number", scenes[i].get("scene_num", i + 1))
            dur = scenes[i].get("duration", 4.0)
            is_last = (i == len(scenes) - 1)
            logger.info(f"   [Row {row_num}] Scene {scene_num}: Creating Ken Burns video from image ({dur:.1f}s){', last scene = subtle zoom' if is_last else ''}...")
            try:
                fallback_video = processor.rendi_service.create_video_from_image(
                    image_url=scene_images[i],
                    duration=dur,
                    subtle_for_last_scene=is_last
                )
                if fallback_video:
                    scene_videos[i] = fallback_video
                    logger.info(f"   [Row {row_num}] Scene {scene_num}: Fallback video created ????")
                    # Update sheet
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

    # Collect results ? one slot per scene (nulls preserved) for wrapper/Studio index alignment
    result["scene_images"] = [u if u else None for u in scene_images]
    result["scene_videos"] = [u if u else None for u in scene_videos]
    result["asset_videos"] = [url for url in asset_videos if url]

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

        # Send individual beat clips to dashboard
        for _si in range(len(scene_filler_videos)):
            for _ci, _clip in enumerate(scene_filler_videos[_si]):
                if _clip.get("url"):
                    on_progress("step_complete", {
                        "step": f"beat_clip_s{_si+1}_c{_ci}",
                        "label": f"Scene {_si+1} Clip {_ci}",
                        "progress": -1,
                        "message": f"Scene {_si+1} clip {_ci} ({_clip.get('duration', 0):.1f}s)",
                        "asset_url": _clip["url"],
                        "asset_type": "video",
                    })

        # Build complete beat clip map for debugging
        _beat_clip_map = {}
        for _si in range(len(scene_filler_videos)):
            clips_data = []
            for _ci, _clip in enumerate(scene_filler_videos[_si]):
                clips_data.append({
                    "url": _clip.get("url"),
                    "raw_url": _clip.get("raw_url"),
                    "duration": _clip.get("duration"),
                    "type": _clip.get("type", "unknown"),
                })
            if clips_data:
                _beat_clip_map[f"scene_{_si+1}"] = clips_data
        if _beat_clip_map:
            on_progress("intermediate", {"key": "scene_beat_clips", "value": _beat_clip_map})

    # --- Callback: music step_complete + intermediate + usage (skip if already emitted in early-music block) ---
    if on_progress and not _music_emitted_early:
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

    if not result["scene_videos"] and not result["asset_videos"]:
        error = "No scene videos or assets generated"
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
    
    # Optional API/Studio pause: after all scene animations, before Rendi concat (step_12).
    if on_progress:
        on_progress("step_complete", {
            "step": "animations_review",
            "label": "Review scene animations",
            "progress": 72,
            "message": "Scene animations ready ????? approve in Studio to continue to final assembly",
        })

    # =====================================================================
    # STEP 8: Combine ALL scenes + ALL assets into final video
    # ALL generated scenes and ALL assets MUST be included
    # CTA (closing) scene is ALWAYS last
    # =====================================================================

    # --- existing_intermediates skip logic for final_video_url ---
    if "final_video_url" in intermediates and intermediates["final_video_url"]:
        logger.info(f"   [Row {row_num}] Using cached final_video_url from existing_intermediates ????? skipping Steps 8-9")
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
        # Use the INDEXED scene_videos list (preserves ordering even with gaps)
        # so each video matches its VO-synced duration from scenes[i]
        asset_vids = result["asset_videos"]   # Already filtered for non-None
        
        # Build ordered list of (video_url, duration) for scenes ????? each scene MUST total exactly
        # the VO segment length from ElevenLabs (scene["duration"]) so visuals stay in sync with speech.
        ordered_scene_pairs = []
        for i in range(len(scenes)):
            if scene_videos[i]:
                vid_url = scene_videos[i]
                required_dur = round(scenes[i].get("duration", 4.0), 2)

                # Beat clips: Director already handled timing ????? skip stretch/slow-motion
                if _smart_mode and i < len(scene_filler_videos) and scene_filler_videos[i]:
                    # All clips for this beat are in scene_filler_videos[i]
                    # Just add a placeholder to keep indexing aligned
                    ordered_scene_pairs.append((vid_url, required_dur))
                    continue

                # Sanity check: if the URL looks like an image (not video), create Ken Burns for full duration
                is_image_url = any(vid_url.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"])
                if is_image_url:
                    logger.warning(f"   [Row {row_num}] Scene {i+1}: URL looks like an IMAGE, not video ????? creating Ken Burns to ensure motion")
                    try:
                        kb_video = processor.rendi_service.create_video_from_image(
                            image_url=vid_url, duration=required_dur,
                            subtle_for_last_scene=(i == len(scenes) - 1)
                        )
                        if kb_video:
                            vid_url = kb_video
                            scene_videos[i] = kb_video
                    except Exception:
                        pass
                # Ensure this scene's total duration matches VO segment exactly: probe actual clip length;
                # if clip is shorter than required, add Ken Burns filler from the same scene image.
                actual_dur = 0.0
                try:
                    actual_dur = processor.rendi_service.get_video_duration_cloud(vid_url)
                except Exception:
                    pass
                if actual_dur <= 0:
                    actual_dur = required_dur
                if actual_dur >= required_dur - 0.3:
                    ordered_scene_pairs.append((vid_url, required_dur))
                else:
                    # Prefer slow-motion to extend; avoid static/Ken Burns filler (looks like a frozen shot).
                    fill_dur = round(required_dur - actual_dur, 2)
                    duration_ratio = required_dur / actual_dur if actual_dur > 0 else 2.0
                    max_slowdown = 2.0
                    stretched_url = None
                    if duration_ratio <= max_slowdown and fill_dur > 0.3:
                        try:
                            speed_factor = actual_dur / required_dur
                            stretched_url = processor.rendi_service.slow_motion_video(
                                video_url=vid_url,
                                speed_factor=speed_factor,
                                target_duration=required_dur,
                                keep_audio=False
                            )
                            if stretched_url:
                                ordered_scene_pairs.append((stretched_url, required_dur))
                                logger.info(f"   [Row {row_num}] Scene {i+1}: stretched {actual_dur:.1f}s ????? {required_dur:.1f}s (slow-motion)")
                                continue
                        except Exception as slow_err:
                            logger.warning(f"   [Row {row_num}] Scene {i+1}: slow-motion failed ({slow_err})")
                    else:
                        logger.info(f"   [Row {row_num}] Scene {i+1}: clip too short for slow-motion ({duration_ratio:.1f}x > {max_slowdown}x max)")
                    ordered_scene_pairs.append((vid_url, round(actual_dur, 2)))
                    allow_kb = getattr(config, "SCENE_ALLOW_KB_FILLER", False)
                    if allow_kb and fill_dur >= 0.5:
                        img_url = scene_images[i] if i < len(scene_images) and scene_images[i] else None
                        if img_url:
                            try:
                                kb_filler = processor.rendi_service.create_video_from_image(
                                    image_url=img_url, duration=fill_dur,
                                    subtle_for_last_scene=(i == len(scenes) - 1)
                                )
                                if kb_filler:
                                    ordered_scene_pairs.append((kb_filler, fill_dur))
                                    logger.info(f"   [Row {row_num}] Scene {i+1}: clip {actual_dur:.1f}s + Ken Burns filler {fill_dur:.1f}s (SCENE_ALLOW_KB_FILLER=True)")
                            except Exception as kb_err:
                                logger.warning(f"   [Row {row_num}] Scene {i+1}: Ken Burns filler failed ({kb_err})")
                    else:
                        logger.info(f"   [Row {row_num}] Scene {i+1}: using clip as-is {actual_dur:.1f}s (target {required_dur:.1f}s; no static filler)")
        
        if not ordered_scene_pairs:
            error = "No scene videos available after fallback"
            logger.error(f"   [Row {row_num}] {error}")
            result["errors"].append(error)
            return result
        
        # =================================================================
        # SPLIT LONG SCENES: if a scene's VO duration > MAX_SINGLE_CLIP, use
        # first MAX_SINGLE_CLIP only ????? UNLESS the clip is already long enough
        # (e.g. Ken Burns was generated at the full required duration).  In
        # that case let it through so the visual covers all of the VO.
        # =================================================================
        MAX_SINGLE_CLIP = 10.0  # seconds
        allow_kb_long = getattr(config, "SCENE_ALLOW_KB_FILLER", False)
        split_pairs = []
        for idx, (vid_url, dur) in enumerate(ordered_scene_pairs):
            if dur <= MAX_SINGLE_CLIP:
                split_pairs.append((vid_url, dur))
            else:
                # Probe the actual clip length; if the clip already covers the
                # full duration (e.g. Ken Burns was rendered at full length),
                # use the clip in full instead of truncating to MAX_SINGLE_CLIP.
                try:
                    _probe_dur = processor.rendi_service.get_video_duration_cloud(vid_url)
                except Exception:
                    _probe_dur = 0
                if _probe_dur >= dur - 0.5:
                    split_pairs.append((vid_url, dur))
                    logger.info(f"   [Row {row_num}] Scene {idx+1}: clip already {_probe_dur:.1f}s (>= needed {dur:.1f}s) ????? using in full")
                    continue
                # Clip is shorter than required ????? truncate to MAX_SINGLE_CLIP
                split_pairs.append((vid_url, MAX_SINGLE_CLIP))
                remaining = round(dur - MAX_SINGLE_CLIP, 2)
                if not allow_kb_long:
                    logger.info(f"   [Row {row_num}] Scene {idx+1}: VO segment {dur:.1f}s ????? using first {MAX_SINGLE_CLIP:.0f}s only (no static filler)")
                    remaining = 0
                # Generate additional Ken Burns clip(s) from the same scene's image (only when allow_kb_long and remaining > 0)
                scene_idx_for_image = idx
                orig_scene_idx = None
                counter = 0
                for si in range(len(scenes)):
                    if scene_videos[si]:
                        if counter == idx:
                            orig_scene_idx = si
                            break
                        counter += 1
                img_url = scene_images[orig_scene_idx] if orig_scene_idx is not None and orig_scene_idx < len(scene_images) else None
                while remaining > 1.0 and img_url:
                    chunk = min(remaining, MAX_SINGLE_CLIP)
                    logger.info(f"   [Row {row_num}] Scene {idx+1}: VO segment >{MAX_SINGLE_CLIP:.0f}s ????? generating extra Ken Burns clip ({chunk:.1f}s) from same image")
                    try:
                        extra_video = processor.rendi_service.create_video_from_image(
                            image_url=img_url,
                            duration=chunk
                        )
                        if extra_video:
                            split_pairs.append((extra_video, chunk))
                            remaining = round(remaining - chunk, 2)
                            logger.info(f"   [Row {row_num}] Scene {idx+1}: Extra Ken Burns clip created ???? ({chunk:.1f}s)")
                        else:
                            split_pairs[-1] = (split_pairs[-1][0], round(split_pairs[-1][1] + remaining, 2))
                            logger.warning(f"   [Row {row_num}] Scene {idx+1}: Extra Ken Burns failed, extending previous clip by {remaining:.1f}s")
                            break
                    except Exception as kb_err:
                        split_pairs[-1] = (split_pairs[-1][0], round(split_pairs[-1][1] + remaining, 2))
                        logger.warning(f"   [Row {row_num}] Scene {idx+1}: Extra Ken Burns error ({kb_err}), extending previous clip")
                        break
        
        if len(split_pairs) > len(ordered_scene_pairs):
            logger.info(f"   [Row {row_num}] Split long scenes: {len(ordered_scene_pairs)} ????? {len(split_pairs)} clips")
        ordered_scene_pairs = split_pairs
        
        if _smart_mode:
            # SMART MODE: no CTA separation ????? all scenes are equal beats.
            # The Director controls content for every beat including the last one.
            cta_video = None
            cta_time = 0
            body_pairs = ordered_scene_pairs  # ALL scenes are body scenes
        else:
            # LEGACY MODE: last scene is CTA/closing scene ????? keep it separate
            cta_video, cta_time = ordered_scene_pairs[-1]
            body_pairs = ordered_scene_pairs[:-1]

        body_scenes = [p[0] for p in body_pairs]
        body_durations = [p[1] for p in body_pairs]

        # Build the video sequence
        all_videos = []

        num_body = len(body_scenes)
        num_assets = len(asset_vids)

        # Light clamp: min 2.5s per scene so clips are usable
        body_durations = [max(2.5, d) for d in body_durations]
        if cta_video:
            cta_time = max(2.5, cta_time)
        body_total = sum(body_durations)

        if _smart_mode:
            # SMART MODE: all clips (AI + real) are in scene_filler_videos (beat_clips)
            # No separate CTA, no asset interleaving ????? Director handles everything.
            asset_total_time = 0
            _beat_clip_count = sum(len(fv) for fv in scene_filler_videos)
            _beat_clip_total_dur = sum(f.get("duration", 0) for fv in scene_filler_videos for f in fv)
            # All scenes are body scenes ????? iterate all of them
            _body_clip_dur = 0
            for i in range(num_body):
                if i < len(scene_filler_videos) and scene_filler_videos[i]:
                    _body_clip_dur += sum(f.get("duration", 0) for f in scene_filler_videos[i])
                else:
                    _body_clip_dur += body_durations[i] if i < len(body_durations) else 4.0
            logger.info(f"   [Row {row_num}] Smart mode assembly: {num_body} beats, {_beat_clip_count} beat clips ({_beat_clip_total_dur:.1f}s), total={_body_clip_dur:.1f}s")
            for i in range(num_body):
                if i < len(scene_filler_videos) and scene_filler_videos[i]:
                    for clip_entry in scene_filler_videos[i]:
                        c_dur = round(clip_entry.get("duration", 4.0), 2)
                        all_videos.append({"video_url": clip_entry["url"], "duration": c_dur, "type": "beat_clip",
                                           "_pre_trimmed": clip_entry.get("_pre_trimmed", False)})
                elif i < len(body_scenes) and body_scenes[i]:
                    dur = body_durations[i] if i < len(body_durations) else 4.0
                    all_videos.append({"video_url": body_scenes[i], "duration": dur, "type": "scene"})
        else:
            # LEGACY MODE: interleave body scenes with asset clips
            asset_total_time = num_assets * 3.0
            vo_target_total = (vo_duration_seconds + 1.5) if vo_duration_seconds > 0 else 0
            total_so_far = body_total + asset_total_time + (cta_time if cta_video else 0)
            _cta_max = _p_defaults.get("cta_max_duration", 5.0)
            if vo_target_total > 0 and total_so_far < vo_target_total and cta_video:
                shortfall = round(vo_target_total - total_so_far, 2)
                cta_extension = min(shortfall, max(0, _cta_max - cta_time))
                cta_time = round(cta_time + cta_extension, 2)
                if cta_extension > 0:
                    logger.info(f"   [Row {row_num}] Extended CTA scene by +{cta_extension:.1f}s so video length >= VO+buffer ({vo_target_total:.1f}s)")
            elif vo_target_total > 0 and total_so_far < vo_target_total and num_body > 0:
                shortfall = round(vo_target_total - total_so_far, 2)
                body_durations[-1] = round(body_durations[-1] + shortfall, 2)
                logger.info(f"   [Row {row_num}] Extended last body scene by +{shortfall:.1f}s so video length >= VO+buffer ({vo_target_total:.1f}s)")

            logger.info(f"   [Row {row_num}] Duration: body VO-synced {[round(d,1) for d in body_durations]} total={body_total:.1f}s, assets={num_assets}x3s={asset_total_time:.1f}s, CTA={cta_time}s")

            if num_body > 0 and num_assets > 0:
                spacing = max(1, num_body // (num_assets + 1))
                asset_after_scene = []
                for a in range(num_assets):
                    insert_pos = min(spacing * (a + 1), num_body)
                    asset_after_scene.append(insert_pos)

                asset_idx = 0
                for i, scene_url in enumerate(body_scenes):
                    dur = body_durations[i] if i < len(body_durations) else 4.0
                    all_videos.append({"video_url": scene_url, "duration": dur, "type": "scene"})
                    if asset_idx < num_assets and (i + 1) in asset_after_scene:
                        all_videos.append({"video_url": asset_vids[asset_idx], "duration": 3.0, "type": "asset"})
                        asset_idx += 1
                while asset_idx < num_assets:
                    all_videos.append({"video_url": asset_vids[asset_idx], "duration": 3.0, "type": "asset"})
                    asset_idx += 1
            else:
                for i, scene_url in enumerate(body_scenes):
                    dur = body_durations[i] if i < len(body_durations) else 4.0
                    all_videos.append({"video_url": scene_url, "duration": dur, "type": "scene"})
                for asset_url in asset_vids:
                    all_videos.append({"video_url": asset_url, "duration": 3.0, "type": "asset"})

        # ALWAYS add the CTA/closing scene at the very end
        if cta_video:
            all_videos.append({"video_url": cta_video, "duration": cta_time, "type": "cta_scene"})

        # =====================================================================
        # END CARD: ~2s outro with business info overlay (influencer only)
        # Structurally prevents VO cutoff: video is always longer than VO.
        # =====================================================================
        _end_card_url = None
        END_CARD_DURATION = 2.0  # seconds ????? end card is always 2s
        _has_end_card_params = bool(business_name)
        if video_subtype == "influencer" and _has_end_card_params and not simulation:
            _end_card_clip_url = None

            # --- Pick the best surprise clip (or fallback to reference image) ---
            if len(_surprise_clips_generated) > 1:
                # LLM scoring: pick the best surprise clip for the outro
                try:
                    loader = get_prompt_loader()
                    ec_system = loader.get("shared_end_card_scoring_system")
                    clip_desc_lines = []
                    for ci_ec, sc in enumerate(_surprise_clips_generated):
                        clip_desc_lines.append(f"Clip {ci_ec}: {sc.get('description', 'No description')} | Motion: {sc.get('motion_prompt', 'N/A')}")
                    ec_user = loader.get(
                        "shared_end_card_scoring_user",
                        num_candidates=len(_surprise_clips_generated),
                        business_category=business_category or "general",
                        clip_descriptions="\n".join(clip_desc_lines),
                    )
                    ec_result = processor._call_llm(
                        "end_card_scoring",
                        [{"role": "system", "content": ec_system},
                         {"role": "user", "content": ec_user}],
                        temperature=0.3,
                    )
                    ec_text = (ec_result.get("text", "") or "").strip()
                    # Strip markdown fences
                    if ec_text.startswith("```"):
                        ec_text = re.sub(r"^```\w*\n?", "", ec_text)
                        ec_text = re.sub(r"\n?```$", "", ec_text).strip()
                    # Find JSON object
                    brace_start = ec_text.find("{")
                    brace_end = ec_text.rfind("}")
                    if brace_start >= 0 and brace_end > brace_start:
                        ec_text = ec_text[brace_start:brace_end + 1]
                    ec_parsed = json.loads(ec_text)
                    best_idx = int(ec_parsed.get("best_index", 0))
                    if 0 <= best_idx < len(_surprise_clips_generated):
                        _end_card_clip_url = _surprise_clips_generated[best_idx]["url"]
                        logger.info(f"   [Row {row_num}] End card: scored {len(_surprise_clips_generated)} surprise clips, selected index {best_idx} ????? {ec_parsed.get('reason', '')}")
                    else:
                        _end_card_clip_url = _surprise_clips_generated[0]["url"]
                        logger.warning(f"   [Row {row_num}] End card: LLM returned invalid index {best_idx}, using first surprise clip")
                    if on_progress:
                        usage_data = {
                            "service": "vertex", "step": "end_card_scoring",
                            "model": "gemini-3-flash-preview", "provider": "vertex",
                            "input_tokens": len(ec_user) // 4, "output_tokens": len(ec_text) // 4,
                            "label": "End card clip scoring", "category": "text_generation", "success": True,
                        }
                        on_progress("usage", usage_data)
                        usage_list.append(usage_data)
                except Exception as ec_err:
                    logger.warning(f"   [Row {row_num}] End card scoring failed ({ec_err}), using first surprise clip")
                    _end_card_clip_url = _surprise_clips_generated[0]["url"]
            elif len(_surprise_clips_generated) == 1:
                _end_card_clip_url = _surprise_clips_generated[0]["url"]
                logger.info(f"   [Row {row_num}] End card: single surprise clip, using it directly")
            else:
                # Fallback: use first reference image as Ken Burns still
                _fallback_img = None
                if ref_image_analyses:
                    for _ria in ref_image_analyses:
                        if _ria.get("url"):
                            _fallback_img = _ria["url"]
                            break
                if _fallback_img:
                    try:
                        _end_card_clip_url = processor.rendi_service.create_video_from_image(
                            image_url=_fallback_img, duration=END_CARD_DURATION
                        )
                        if _end_card_clip_url:
                            logger.info(f"   [Row {row_num}] End card: no surprise clips, using Ken Burns from reference image")
                    except Exception as kb_err:
                        logger.warning(f"   [Row {row_num}] End card Ken Burns fallback failed: {kb_err}")
                else:
                    logger.info(f"   [Row {row_num}] End card: no surprise clips or reference images, skipping end card")

            # --- Trim + text overlay + append ---
            if _end_card_clip_url:
                # 1. Try trim (independent)
                try:
                    trimmed_ec = processor.rendi_service.trim_video(
                        video_url=_end_card_clip_url, duration=END_CARD_DURATION, has_audio=False,
                    )
                    if trimmed_ec:
                        _end_card_clip_url = trimmed_ec
                except Exception as trim_err:
                    logger.warning(f"   [Row {row_num}] End card trim failed: {trim_err}")

                # 2. Try overlay (independent)
                overlaid = _add_end_card_text_overlay(
                    processor, _end_card_clip_url,
                    business_name, business_address, business_phone,
                    row_num=row_num,
                    end_card_color=end_card_color,
                    end_card_detail_color=end_card_detail_color,
                    end_card_position=end_card_position,
                    business_website=business_website,
                )
                if overlaid:
                    _end_card_url = overlaid
                else:
                    _end_card_url = _end_card_clip_url  # Use without text if overlay fails

                # End card will be appended AFTER Step 8.5 trim to guarantee visibility
                _overlay_status = "with text overlay" if overlaid else "WITHOUT text overlay (fallback)"
                logger.info(f"   [Row {row_num}] End card ready: {END_CARD_DURATION}s, {_overlay_status} ????? will append after trim step")
                try:
                    if on_progress:
                        on_progress("intermediate", {"key": "end_card_url", "value": _end_card_url})
                        usage_data = {
                            "service": "rendi", "step": "end_card_assembly",
                            "model": "rendi", "provider": "rendi", "count": 1,
                            "label": "End card trim + text overlay", "category": "ffmpeg", "success": True,
                        }
                        on_progress("usage", usage_data)
                        usage_list.append(usage_data)
                except Exception as progress_err:
                    logger.warning(f"   [Row {row_num}] End card progress reporting failed: {progress_err}")

        total_planned = sum(v["duration"] for v in all_videos)
        dissolve_total_loss = (len(all_videos) - 1) * effective_dissolve if len(all_videos) > 1 else 0
        effective_total = total_planned - dissolve_total_loss
        _end_card_label = " (end card will be appended after trim)" if _end_card_url else ""
        logger.info(f"   [Row {row_num}] Video sequence: {len(all_videos)} clips ({len(body_scenes)} body + {len(asset_vids)} assets + {'1 CTA' if cta_video else '0 CTA'}{_end_card_label}), total ~{total_planned:.1f}s - dissolve {dissolve_total_loss:.1f}s = ~{effective_total:.1f}s (VO={vo_duration_seconds:.1f}s)")
        
        # Safety: if effective total is shorter than VO, extend CTA (capped) or last clip to compensate
        _cta_max = _p_defaults.get("cta_max_duration", 5.0)
        if vo_duration_seconds > 0 and effective_total < vo_duration_seconds + 1.0:
            shortfall = round((vo_duration_seconds + 1.5) - effective_total, 2)
            if cta_video and all_videos and all_videos[-1].get("type") == "cta_scene":
                cta_extension = min(shortfall, max(0, _cta_max - all_videos[-1]["duration"]))
                if cta_extension > 0:
                    all_videos[-1]["duration"] = round(all_videos[-1]["duration"] + cta_extension, 2)
                    logger.info(f"   [Row {row_num}] ??????? Video shorter than VO after dissolve ????? extended CTA by +{cta_extension:.1f}s")
            elif all_videos:
                if all_videos[-1].get("_pre_trimmed"):
                    logger.info(f"   [Row {row_num}] Last clip is pre-trimmed ????? skipping extension (Ken Burns filler will handle gap if needed)")
                else:
                    all_videos[-1]["duration"] = round(all_videos[-1]["duration"] + shortfall, 2)
                    logger.info(f"   [Row {row_num}] ??????? Video shorter than VO after dissolve ????? extended last clip by +{shortfall:.1f}s")
            total_planned = sum(v["duration"] for v in all_videos)

        # Keep VO?????visual sync: each clip duration must match the VO segment length so speech and image stay aligned.
        # If a clip is shorter than requested, stretch it (slow_motion) so the visual stays in sync with the voice.
        # Phase 12: When precision sync is active and all scenes were pre-trimmed locally, skip the stretch pass
        _all_precision_ugc = sync_method == "precision" and all(
            scenes[i].get("_precision_trimmed", False)
            for i in range(len(scenes))
            if scene_videos[i]
        )
        if _all_precision_ugc:
            logger.info(f"   [Row {row_num}] Precision sync: all scene clips pre-trimmed locally ????? skipping VO-sync stretch pass")
        VO_STRETCH_MAX_FACTOR = 1.8  # Max stretch: requested/actual <= 1.8 (e.g. 5s clip ????? 9s for full sync)
        if all_videos and not _all_precision_ugc:
            def _probe_duration(entry: dict) -> float:
                try:
                    return processor.rendi_service.get_video_duration_cloud(entry["video_url"])
                except Exception:
                    return 0.0
            # First pass: probe actual duration for every clip
            with ThreadPoolExecutor(max_workers=min(getattr(config, "RENDI_STRETCH_PARALLEL_WORKERS", 4), len(all_videos))) as ex:
                futures = {executor_submit_with_progress(ex, _probe_duration, v): i for i, v in enumerate(all_videos)}
                for fut in as_completed(futures):
                    i = futures[fut]
                    entry = all_videos[i]
                    try:
                        entry["_actual_duration"] = fut.result()
                    except Exception:
                        entry["_actual_duration"] = 0.0
            stretched_count = 0
            capped_count = 0
            _PRETRIM_CAP_TOLERANCE = 0.1  # seconds ????? ignore differences smaller than this
            for i, entry in enumerate(all_videos):
                requested = entry.get("duration") or 5.0
                actual = entry.get("_actual_duration") or 0.0
                if actual <= 0 or requested <= actual:
                    entry.pop("_actual_duration", None)
                    continue
                if entry.get("_pre_trimmed"):
                    # Pre-trimmed clips shouldn't be stretched, but cap to actual duration
                    # to prevent Rendi from holding the last frame
                    if actual < requested and (requested - actual) > _PRETRIM_CAP_TOLERANCE:
                        delta = round(requested - actual, 2)
                        clip_type = entry.get("type", "clip")
                        entry["duration"] = round(actual, 2)
                        capped_count += 1
                        logger.info(f"   [Row {row_num}] Clip {i+1} ({clip_type}) pre-trimmed cap: {requested:.1f}s ????? {actual:.1f}s (delta={delta:.2f}s, prevents frozen frame)")
                    entry.pop("_actual_duration", None)
                    continue
                clip_type = entry.get("type", "clip")
                # When we have VO, stretch short clips so visual stays in sync with voice
                if vo_duration_seconds > 0 and (requested / actual) <= VO_STRETCH_MAX_FACTOR:
                    speed_factor = actual / requested
                    try:
                        stretched_url = processor.rendi_service.slow_motion_video(
                            entry["video_url"],
                            speed_factor=speed_factor,
                            target_duration=requested,
                            keep_audio=False
                        )
                        if stretched_url:
                            entry["video_url"] = stretched_url
                            stretched_count += 1
                            logger.info(f"   [Row {row_num}] Clip {i+1} ({clip_type}) stretched for VO sync: {actual:.1f}s ????? {requested:.1f}s (no desync)")
                        else:
                            entry["duration"] = round(actual, 2)
                            capped_count += 1
                            logger.info(f"   [Row {row_num}] Clip {i+1} ({clip_type}) stretch failed, capped: {requested:.1f}s ????? {actual:.1f}s")
                    except Exception as stretch_err:
                        entry["duration"] = round(actual, 2)
                        capped_count += 1
                        logger.warning(f"   [Row {row_num}] Clip {i+1} ({clip_type}) stretch error ({stretch_err}), capped to {actual:.1f}s")
                else:
                    entry["duration"] = round(actual, 2)
                    capped_count += 1
                    if vo_duration_seconds > 0:
                        logger.info(f"   [Row {row_num}] Clip {i+1} ({clip_type}) duration capped: {requested:.1f}s ????? {actual:.1f}s (stretch would be >{VO_STRETCH_MAX_FACTOR}x or no VO)")
                    else:
                        logger.info(f"   [Row {row_num}] Clip {i+1} ({clip_type}) duration capped: {requested:.1f}s ????? {actual:.1f}s (avoids frozen frame)")
                entry.pop("_actual_duration", None)
            if stretched_count:
                logger.info(f"   [Row {row_num}] Stretched {stretched_count} clip(s) for VO sync ????? image and voice stay aligned")
            if capped_count:
                logger.info(f"   [Row {row_num}] Capped {capped_count} clip(s) to actual file length")

        # Build video data for concatenation
        # Pre-trimmed clips: duration=None (Rendi skips re-trim), offset_duration has the actual value for dissolve calculations
        video_data = []
        for v in all_videos:
            entry = {"video_url": v["video_url"], "duration": v["duration"]}
            if v.get("_pre_trimmed"):
                entry["offset_duration"] = v["duration"]
            video_data.append(entry)
        
        # Concatenate all videos with gentle dissolve between shots (video_only for silent Veo/Kling output)
        if simulation:
            concat_video_url = _SIM_VIDEO
            logger.info(f"   [Row {row_num}] [SIM] Videos concatenated")
        else:
            concat_video_url = processor.rendi_service.concatenate_videos(
                video_data=video_data,
                video_only=True,
                dissolve_seconds=effective_dissolve
            )

        if concat_video_url:
            logger.info(f"   [Row {row_num}] Scenes concatenated")
            
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
                        logger.warning(f"   [Row {row_num}] Film grain failed (Rendi returned None), continuing without grain")
                except Exception as fg_err:
                    logger.warning(f"   [Row {row_num}] Film grain error: {fg_err}, continuing without grain")

            final_video_url = concat_video_url
            has_vo = bool(vo_audio_url)
            has_music = bool(result.get("music_url"))
            if not has_vo:
                logger.warning(f"   [Row {row_num}] No VO audio available (script or TTS may have failed) - will add music only if present")

            # Always probe concat duration (needed for audio mix -t flag to align streams)
            _concat_dur = 0
            if has_vo and vo_duration_seconds > 0 and not simulation:
                _concat_dur = processor.rendi_service.get_video_duration_cloud(concat_video_url)

            # EXTEND VIDEO TO VO LENGTH: instead of freezing the last frame (looks static/bad),
            # generate a Ken Burns clip from the last scene's image to fill the gap.
            # Skip when end card exists ????? the end card itself will fill the gap dynamically.
            if has_vo and vo_duration_seconds > 0 and not simulation and not _end_card_url:
                concat_dur = _concat_dur  # reuse probed duration (no extra API call)
                if concat_dur > 0 and concat_dur < vo_duration_seconds:
                    gap_seconds = round(vo_duration_seconds - concat_dur + 2.0, 1)
                    # Cap filler so we don't create a long "loop" of the same last-scene image (max 10s)
                    MAX_END_FILLER_SECONDS = 10.0
                    if gap_seconds > MAX_END_FILLER_SECONDS:
                        logger.info(f"   [Row {row_num}] ??????? Capping end filler to {MAX_END_FILLER_SECONDS:.0f}s (was {gap_seconds:.1f}s) to avoid long loop on last scene; video will end ~{gap_seconds - MAX_END_FILLER_SECONDS:.0f}s before VO")
                        gap_seconds = MAX_END_FILLER_SECONDS
                    logger.info(f"   [Row {row_num}] ??????? Concat video ({concat_dur:.1f}s) < VO ({vo_duration_seconds:.1f}s) ????? creating {gap_seconds:.1f}s Ken Burns filler")
                    # Find the last scene image for Ken Burns
                    last_image = None
                    for img in reversed(scene_images):
                        if img:
                            last_image = img
                            break
                    if last_image:
                        try:
                            filler_video = processor.rendi_service.create_video_from_image(
                                image_url=last_image, duration=gap_seconds
                            )
                            if filler_video:
                                # Append filler to concat video
                                filler_data = [
                                    {"video_url": concat_video_url, "duration": concat_dur},
                                    {"video_url": filler_video, "duration": gap_seconds}
                                ]
                                extended = processor.rendi_service.concatenate_videos(
                                    video_data=filler_data, video_only=True,
                                    dissolve_seconds=effective_dissolve,
                                )
                                if extended:
                                    concat_video_url = extended
                                    final_video_url = extended
                                    logger.info(f"   [Row {row_num}] Video extended with Ken Burns filler to ~{concat_dur + gap_seconds:.1f}s (no frozen frame)")
                                else:
                                    logger.warning(f"   [Row {row_num}] Could not append filler, falling back to tpad")
                                    tpad_result = _tpad_video(processor, concat_video_url, gap_seconds, row_num)
                                    if tpad_result:
                                        concat_video_url = tpad_result
                                        final_video_url = tpad_result
                        except Exception as fill_err:
                            logger.warning(f"   [Row {row_num}] Ken Burns filler error: {fill_err}")
                    else:
                        logger.warning(f"   [Row {row_num}] No scene image available for filler")
            
            # Add VO and music
            if on_progress:
                on_progress("step_start", {
                    "step": "audio_mix",
                    "label": "Audio Mix",
                    "message": "Mixing voiceover and music...",
                })
            if simulation:
                final_video_url = _SIM_VIDEO
                logger.info(f"   [Row {row_num}] [SIM] VO + music mixed")
            elif has_vo and has_music:
                video_with_both = processor.rendi_service.add_vo_and_music_to_video(
                    video_url=concat_video_url,
                    vo_url=vo_audio_url,
                    music_url=result["music_url"],
                    vo_volume=1.0,
                    music_volume=0.2,
                    video_duration=_concat_dur if _concat_dur > 0 else None,
                )
                if video_with_both:
                    final_video_url = video_with_both
                    logger.info(f"   [Row {row_num}] Music + Voice over added")
            elif has_vo:
                video_with_vo = processor.rendi_service.add_audio_to_video(
                    video_url=concat_video_url,
                    audio_url=vo_audio_url,
                    video_duration=_concat_dur if _concat_dur > 0 else None,
                )
                if video_with_vo:
                    final_video_url = video_with_vo
                    logger.info(f"   [Row {row_num}] Voice over added")
            elif has_music:
                video_with_music = processor.rendi_service.add_background_music_to_video(
                    video_url=concat_video_url,
                    music_url=result["music_url"],
                    music_volume=0.3
                )
                if video_with_music:
                    final_video_url = video_with_music
                    logger.info(f"   [Row {row_num}] Music added")
            
            # Write to RENDI Scene & Voice column
            try:
                processor.sheets_service.update_cell(
                    config.GOOGLE_SHEET_ID,
                    config.GOOGLE_SHEET_TAB,
                    row_num,
                    config.RENDI_SCENE_VOICE_COLUMN,
                    final_video_url,
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
                on_progress("intermediate", {"key": "audio_mix_url", "value": final_video_url})
                usage_data = {
                    "service": "rendi", "step": "audio_mix",
                    "model": "rendi", "provider": "rendi",
                    "count": 1,
                    "label": "Mix VO and music", "category": "ffmpeg",
                    "success": bool(final_video_url),
                }
                on_progress("usage", usage_data)
                usage_list.append(usage_data)

            # =====================================================================
            # STEP 8.5: TRIM FINAL VIDEO TO VO LENGTH + BUFFER
            # Ensures VO always finishes before the video ends (no trailing silence)
            # but never trims below the user's requested target_duration.
            # =====================================================================
            if vo_duration_seconds > 0 and final_video_url and not simulation:
                trim_target = max(vo_duration_seconds + 2.5, target_duration)  # VO + buffer, but never below requested duration
                _vo_gap_tolerance = -0.5 if _smart_mode else 1.0  # Smart mode: video must be >= VO + 0.5s
                # Get actual video duration to decide what to do
                actual_video_dur = processor.rendi_service.get_video_duration_cloud(final_video_url)
                if actual_video_dur <= 0:
                    logger.info(f"   [Row {row_num}] Step 8.5: Could not probe video duration, skipping adjustment")
                elif actual_video_dur > trim_target + 1.0:
                    # Video is LONGER than VO+buffer ????? trim it
                    logger.info(f"   [Row {row_num}] Step 8.5: Video too long ({actual_video_dur:.1f}s > {trim_target:.1f}s) ????? trimming")
                    try:
                        trimmed_final = processor.rendi_service.trim_video(
                            video_url=final_video_url,
                            duration=trim_target,
                            has_audio=True
                        )
                        if not trimmed_final and FFmpegProcessor.check_ffmpeg_installed():
                            trimmed_final = LocalFFmpegFallback.trim_video(
                                processor.gcs_storage_service, final_video_url, trim_target
                            )
                        if trimmed_final:
                            final_video_url = trimmed_final
                            logger.info(f"   [Row {row_num}] Final video trimmed to ~{trim_target:.1f}s")
                    except Exception as e:
                        logger.warning(f"   [Row {row_num}] Trim error: {e}")
                elif actual_video_dur < vo_duration_seconds - _vo_gap_tolerance:
                    # Video is SHORTER than VO -> slow the whole final video down to match.
                    # Aggressive whole-video slow-mo (>20%) was very noticeable and ugly.
                    # Cap at 0.85x (~18% slower) instead of the previous 0.5x (2x slow).
                    # When the shortfall would need a deeper stretch, accept that the final
                    # video will end slightly before VO rather than degrade the visuals.
                    # Real fix is more/longer pre-generated clips upstream.
                    raw_speed = actual_video_dur / trim_target  # e.g. 40s/56s = 0.71
                    speed_factor = max(0.85, raw_speed)
                    if raw_speed < 0.85:
                        logger.warning(
                            f"   [Row {row_num}] Step 8.5: Video too short ({actual_video_dur:.1f}s vs VO {vo_duration_seconds:.1f}s); "
                            f"raw stretch would be {raw_speed:.2f}x — capping at 0.85x to avoid ugly slow-mo. "
                            f"Final video will be ~{actual_video_dur / 0.85:.1f}s, ending before VO. "
                            f"Director should generate more or longer clips."
                        )
                    logger.info(f"   [Row {row_num}] Step 8.5: Video too short ({actual_video_dur:.1f}s < VO {vo_duration_seconds:.1f}s) -> slowing to {speed_factor:.2f}x to reach ~{trim_target:.1f}s")
                    try:
                        slowed = processor.rendi_service.slow_motion_video(
                            video_url=final_video_url,
                            speed_factor=speed_factor,
                            target_duration=trim_target,
                            keep_audio=False  # VO will be re-added (original audio distorted by slow-mo)
                        )
                        if slowed:
                            # Re-add VO + music to the slowed video
                            if has_vo and has_music:
                                slowed_with_audio = processor.rendi_service.add_vo_and_music_to_video(
                                    video_url=slowed, vo_url=vo_audio_url,
                                    music_url=result.get("music_url", ""), vo_volume=1.0, music_volume=0.2,
                                    video_duration=trim_target,
                                )
                                if slowed_with_audio:
                                    final_video_url = slowed_with_audio
                                    logger.info(f"   [Row {row_num}] Video slowed + VO+music re-added ????? ~{trim_target:.1f}s")
                                else:
                                    final_video_url = slowed
                                    logger.warning(f"   [Row {row_num}] Slowed but could not re-add audio")
                            elif has_vo:
                                slowed_with_vo = processor.rendi_service.add_audio_to_video(
                                    video_url=slowed, audio_url=vo_audio_url,
                                    video_duration=trim_target,
                                )
                                final_video_url = slowed_with_vo or slowed
                            else:
                                final_video_url = slowed
                            logger.info(f"   [Row {row_num}] Video extended to match VO duration")
                        else:
                            logger.warning(f"   [Row {row_num}] Slow-motion failed, VO may be cut off")
                    except Exception as e:
                        logger.warning(f"   [Row {row_num}] Slow-motion error: {e}")
                else:
                    logger.info(f"   [Row {row_num}] Step 8.5: Video={actual_video_dur:.1f}s, VO+buffer={trim_target:.1f}s ????? good match")

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
            # STEP 8.6: APPEND END CARD (after all trimming)
            # End card is appended last so it's never trimmed away by Step 8.5.
            # =====================================================================
            if _end_card_url and final_video_url and not simulation:
                try:
                    _pre_ec_dur = processor.rendi_service.get_video_duration_cloud(final_video_url)
                    if _pre_ec_dur > 0:
                        # Dynamic end card duration: fill VO gap + buffer, minimum END_CARD_DURATION
                        _ec_duration = END_CARD_DURATION
                        _end_card_buffer = _p_defaults.get("end_card_buffer", 2.0)
                        if vo_duration_seconds > 0 and _pre_ec_dur < vo_duration_seconds:
                            _vo_gap = vo_duration_seconds - _pre_ec_dur
                            _ec_duration = max(END_CARD_DURATION, round(_vo_gap + _end_card_buffer, 1))
                            logger.info(f"   [Row {row_num}] Step 8.6: VO gap={_vo_gap:.1f}s ????? end card extended to {_ec_duration:.1f}s (gap + {_end_card_buffer}s buffer)")
                        _ec_dissolve = min(effective_dissolve, 0.4)  # gentle dissolve into end card
                        _ec_result = processor.rendi_service.append_end_card(
                            video_url=final_video_url,
                            end_card_url=_end_card_url,
                            video_duration=_pre_ec_dur,
                            end_card_duration=_ec_duration,
                            dissolve=_ec_dissolve,
                            music_url=result.get("music_url") if has_music else None,
                            music_volume=0.2,
                        )
                        if _ec_result:
                            final_video_url = _ec_result
                            logger.info(f"   [Row {row_num}] Step 8.6: End card appended ({_ec_duration}s, dissolve={_ec_dissolve}s)")
                        else:
                            logger.warning(f"   [Row {row_num}] Step 8.6: End card append failed ????? end card will not appear")
                    else:
                        logger.warning(f"   [Row {row_num}] Step 8.6: Could not probe video duration, skipping end card")
                except Exception as ec_err:
                    logger.warning(f"   [Row {row_num}] Step 8.6: End card append error: {ec_err}")

                if on_progress:
                    usage_data = {
                        "service": "rendi", "step": "end_card_append",
                        "model": "rendi", "provider": "rendi", "count": 1,
                        "label": "Append end card after trim", "category": "ffmpeg",
                        "success": bool(_end_card_url and final_video_url),
                    }
                    on_progress("usage", usage_data)
                    usage_list.append(usage_data)

            # =====================================================================
            # STEP 9: ADD SUBTITLES WITH ZAPCAP (if requested)
            # Snapshot mixed video (VO+music) before burned-in subtitles ????? API/Studio can expose both.
            # =====================================================================
            result["video_before_subtitles_url"] = final_video_url
            if on_progress:
                on_progress("intermediate", {"key": "video_before_subtitles_url", "value": final_video_url})
            if on_progress:
                on_progress("step_start", {
                    "step": "subtitles",
                    "label": "Subtitles",
                    "message": "Adding subtitles...",
                })
            final_video_for_output = final_video_url
            
            if add_subtitles and simulation:
                final_video_for_output = _SIM_VIDEO
                result["subtitled_video_url"] = _SIM_VIDEO
                logger.info(f"   [Row {row_num}] [SIM] Subtitles added")
            elif add_subtitles and processor.zapcap_service:
                logger.info(f"   [Row {row_num}] Step 9: Adding subtitles with ZapCap...")
                try:
                    _ugc_transcript = vo_result.get("word_segments") if vo_result else None
                    if _ugc_transcript and final_video_url:
                        _final_dur = processor.rendi_service.get_video_duration_cloud(final_video_url)
                        if _final_dur > 0:
                            _pre_filter_count = len(_ugc_transcript)
                            _ugc_transcript = [w for w in _ugc_transcript if w.get("start_time", 0) < _final_dur - 0.1]
                            if len(_ugc_transcript) < _pre_filter_count:
                                logger.info(f"   [Row {row_num}] Filtered {_pre_filter_count - len(_ugc_transcript)} BYOT words past video end ({_final_dur:.1f}s)")
                    if _ugc_transcript:
                        logger.info(f"   [Row {row_num}] Using ElevenLabs word segments for ZapCap BYOT ({len(_ugc_transcript)} words)")
                    else:
                        logger.warning(f"   [Row {row_num}] No word_segments in vo_result - ZapCap will use auto-transcription (subtitles may be missing or wrong)")

                    # Enrich transcript with emoji + importance markers via LLM
                    _subtitle_enrichments = None
                    if subtitle_emoji and _ugc_transcript:
                        try:
                            _normalized_for_enrich = processor.zapcap_service._normalize_transcript_for_zapcap(_ugc_transcript)
                            if _normalized_for_enrich:
                                processor.reset_usage()
                                _subtitle_enrichments = enrich_transcript_for_subtitles(
                                    lambda msgs, **kw: processor._call_llm("enrich_subtitles", msgs, **kw),
                                    word_segments=_normalized_for_enrich,
                                    vo_script=vo_script or "",
                                    language=subtitle_language,
                                    fallback_call_fn=lambda msgs, **kw: processor._call_llm("enrich_subtitles_fallback", msgs, **kw),
                                )
                                if on_progress:
                                    emit_llm_usage_events(processor, on_progress, usage_list, "enrich_subtitles")
                        except Exception as _enrich_err:
                            logger.warning(f"   [Row {row_num}] Subtitle enrichment failed (non-blocking): {_enrich_err}")

                    subtitled_video_url = processor.zapcap_service.add_subtitles(
                        video_url=final_video_url,
                        language=subtitle_language,
                        transcript=_ugc_transcript,
                        enrichments=_subtitle_enrichments,
                        subtitle_position=subtitle_position,
                    )
                    if subtitled_video_url:
                        subtitled_video_url = processor.rendi_service.transcode_social_sharing_mp4(subtitled_video_url)
                    if subtitled_video_url:
                        # Upload subtitled video to GCS for permanent storage
                        gcs_key = f"Comp/Final_Video/ugc_videos/row_{row_num}_subtitled_{int(time.time())}.mp4"
                        gcs_subtitled_url = processor.gcs_storage_service.upload_video_from_url(
                            source_url=subtitled_video_url,
                            key_name=gcs_key
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
                        
                        try:
                            processor.sheets_service.update_cell(
                                config.GOOGLE_SHEET_ID,
                                config.GOOGLE_SHEET_TAB,
                                row_num,
                                config.SUBTITLED_VIDEO_COLUMN,
                                final_video_for_output,
                                headers
                            )
                        except Exception:
                            pass
                    else:
                        logger.warning(f"   [Row {row_num}] ????? ZapCap returned no URL ????? Final Video will be written WITHOUT subtitles (check ZapCap upload/task/timeout in logs above)")
                except Exception as e:
                    logger.warning(f"   [Row {row_num}] ????? Error adding subtitles: {e} ????? Final Video will be written WITHOUT subtitles")
            elif add_subtitles and not processor.zapcap_service:
                logger.warning(f"   [Row {row_num}] Subtitles requested but ZapCap not available (no API key) ????? Final Video without subtitles")

            # Safety: trim trailing black frame (VO+music -shortest edge case or ZapCap re-encode)
            if final_video_for_output and not simulation:
                try:
                    _final_dur = processor.rendi_service.get_video_duration_cloud(final_video_for_output)
                    if _final_dur > 1.0:
                        _safe_dur = round(_final_dur - 0.1, 3)
                        _trimmed = processor.rendi_service.trim_video(
                            video_url=final_video_for_output, duration=_safe_dur, has_audio=True,
                        )
                        if _trimmed:
                            final_video_for_output = _trimmed
                            if result.get("subtitled_video_url"):
                                result["subtitled_video_url"] = _trimmed
                            logger.info(f"   [Row {row_num}] Safety trim: {_final_dur:.2f}s -> {_safe_dur:.2f}s (remove trailing black frame)")
                except Exception as _st_err:
                    logger.warning(f"   [Row {row_num}] Safety trim skipped: {_st_err}")

            if on_progress and result.get("subtitled_video_url"):
                on_progress(
                    "intermediate",
                    {"key": "subtitled_video_url", "value": result["subtitled_video_url"]},
                )

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
                on_progress("intermediate", {"key": "final_video_url", "value": final_video_for_output})

            result["final_video_url"] = final_video_for_output
            
            # Update final video column
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
            _with_subs = "with subtitles" if (add_subtitles and result.get("subtitled_video_url")) else "without subtitles"
            logger.info(f"   [Row {row_num}] ???? UGC-style video completed ({_with_subs}): {final_video_for_output[:60]}...")

            # =================================================================
            # STEP 10: EXTENDED VERSION (full raw clips + new VO)
            # Only runs when generate_extended=True AND smart mode AND not sim
            # =================================================================
            if generate_extended and _smart_mode and not simulation:
                from tvd_pipeline.pipelines._extended import analyze_extended_clips, generate_extended_vo

                logger.info(f"   [Row {row_num}] ??????????????? Step 10: Generating extended version ???????????????")

                # Snapshot short version cost before extended work
                _short_cost = sum(u.get("cost_usd", 0) for u in usage_list)
                if on_progress:
                    on_progress("intermediate", {"key": "short_version_cost_usd", "value": round(_short_cost, 4)})

                _extended_usage_start = len(usage_list)  # track extended-only usage

                try:
                    # -----------------------------------------------------------
                    # Step 10.1: Collect raw clips & warmup-only trim
                    # -----------------------------------------------------------
                    if on_progress:
                        on_progress("step_start", {"step": "extended_collect_clips", "label": "Extended: Collecting full clips", "message": "Collecting full-length clips..."})

                    warmup = _get_warmup_skip(processor, video_model)
                    extended_clips = []

                    for si, beat_clips_list in enumerate(scene_filler_videos):
                        for ci, clip in enumerate(beat_clips_list):
                            clip_type = clip.get("type", "unknown")
                            raw_url = clip.get("raw_url") or clip.get("url")

                            if clip_type in ("image", "generate") and warmup > 0:
                                # AI-generated clip: trim warmup, keep rest
                                try:
                                    raw_dur = processor.rendi_service.get_video_duration_cloud(raw_url)
                                    if raw_dur > warmup + 0.5:
                                        keep_dur = round(raw_dur - warmup, 2)
                                        trimmed = processor.rendi_service.trim_video(
                                            video_url=raw_url, duration=keep_dur, start_time=warmup
                                        )
                                        if trimmed:
                                            actual_dur = processor.rendi_service.get_video_duration_cloud(trimmed)
                                            if actual_dur > 0:
                                                extended_clips.append({"url": trimmed, "raw_url": raw_url, "duration": round(actual_dur, 2), "type": clip_type})
                                            else:
                                                extended_clips.append({"url": trimmed, "raw_url": raw_url, "duration": keep_dur, "type": clip_type})
                                        else:
                                            extended_clips.append({"url": raw_url, "raw_url": raw_url, "duration": round(raw_dur, 2), "type": clip_type})
                                    else:
                                        extended_clips.append({"url": raw_url, "raw_url": raw_url, "duration": round(raw_dur, 2), "type": clip_type})
                                except Exception as e:
                                    logger.warning(f"   [Row {row_num}] Extended clip s{si}c{ci}: probe/trim failed ({e}), using original")
                                    extended_clips.append({"url": clip.get("url"), "raw_url": raw_url, "duration": clip.get("duration", 3.0), "type": clip_type})
                            else:
                                # Asset / unknown clip: use as-is at natural duration
                                try:
                                    actual_dur = processor.rendi_service.get_video_duration_cloud(raw_url)
                                    if actual_dur > 0:
                                        extended_clips.append({"url": raw_url, "raw_url": raw_url, "duration": round(actual_dur, 2), "type": clip_type})
                                    else:
                                        extended_clips.append({"url": raw_url, "raw_url": raw_url, "duration": clip.get("duration", 3.0), "type": clip_type})
                                except Exception:
                                    extended_clips.append({"url": raw_url, "raw_url": raw_url, "duration": clip.get("duration", 3.0), "type": clip_type})

                    total_extended_duration = sum(c["duration"] for c in extended_clips)
                    logger.info(f"   [Row {row_num}] Extended: {len(extended_clips)} clips, total {total_extended_duration:.1f}s (short was ~{vo_duration_seconds:.1f}s)")

                    if on_progress:
                        on_progress("step_complete", {
                            "step": "extended_collect_clips", "label": "Extended: Collecting full clips",
                            "progress": 96, "message": f"Collected {len(extended_clips)} clips ({total_extended_duration:.1f}s)",
                        })

                    # -----------------------------------------------------------
                    # Step 10.2: Analyze full clips with vision LLM
                    # -----------------------------------------------------------
                    if on_progress:
                        on_progress("step_start", {"step": "extended_analyze_clips", "label": "Extended: Analyzing clips", "message": "Analyzing clip content..."})

                    extended_clips = analyze_extended_clips(
                        processor=processor,
                        clips=extended_clips,
                        on_progress=on_progress,
                        usage_list=usage_list,
                        row_num=row_num,
                    )

                    if on_progress:
                        on_progress("step_complete", {
                            "step": "extended_analyze_clips", "label": "Extended: Analyzing clips",
                            "progress": 96, "message": f"Analyzed {len(extended_clips)} clips",
                        })
                        on_progress("artifact", {
                            "name": "extended_clips",
                            "data": [{"url": c["url"], "raw_url": c.get("raw_url"), "duration": c["duration"], "description": c.get("description", ""), "type": c.get("type", "")} for c in extended_clips],
                            "format": "json",
                        })
                        on_progress("intermediate", {"key": "extended_clips", "value": [{"duration": c["duration"], "description": c.get("description", ""), "type": c.get("type", "")} for c in extended_clips]})

                    # -----------------------------------------------------------
                    # Step 10.3: Generate extended VO
                    # -----------------------------------------------------------
                    if on_progress:
                        on_progress("step_start", {"step": "extended_vo", "label": "Extended: Generating VO", "message": "Generating extended voiceover..."})

                    _combined_script = combined_script or vo_script or ""
                    extended_vo_script = generate_extended_vo(
                        processor=processor,
                        original_vo=_combined_script,
                        clips=extended_clips,
                        target_duration=total_extended_duration,
                        language=subtitle_language,
                        on_progress=on_progress,
                        usage_list=usage_list,
                        row_num=row_num,
                    )

                    if on_progress:
                        on_progress("intermediate", {"key": "extended_vo_script", "value": extended_vo_script})
                        on_progress("artifact", {
                            "name": "extended_vo_convergence",
                            "data": {
                                "target_words": int(round(total_extended_duration * get_speech_rate(subtitle_language))),
                                "target_duration": round(total_extended_duration, 1),
                                "final_word_count": _word_count_for_duration(extended_vo_script),
                                "script": extended_vo_script,
                            },
                            "format": "json",
                        })

                    # TTS
                    _ext_tts_script = script_only_for_tts(extended_vo_script)
                    _ext_tts_result = processor.elevenlabs_service.text_to_speech_with_timestamps(
                        text=_ext_tts_script,
                        voice_id=voice_id,
                        language=subtitle_language,
                    )
                    _ext_vo_audio_url = _ext_tts_result.get("audio_url") if _ext_tts_result else None
                    _ext_word_segments = _ext_tts_result.get("word_segments", []) if _ext_tts_result else []
                    _ext_vo_duration = _ext_tts_result.get("duration_seconds", total_extended_duration) if _ext_tts_result else total_extended_duration

                    if on_progress and _ext_vo_audio_url:
                        on_progress("intermediate", {"key": "extended_vo_audio_url", "value": _ext_vo_audio_url})
                        _tts_chars = len(_ext_tts_script) if _ext_tts_script else 0
                        _tts_model = get_elevenlabs_config().get("tts_model", "eleven_v3")
                        usage_data = {
                            "service": "elevenlabs", "step": "extended_tts",
                            "model": _tts_model, "provider": "elevenlabs",
                            "character_count": _tts_chars,
                            "label": "Extended TTS", "category": "tts",
                            "success": True,
                        }
                        on_progress("usage", usage_data)
                        usage_list.append(usage_data)

                    if on_progress:
                        on_progress("step_complete", {
                            "step": "extended_vo", "label": "Extended: Generating VO",
                            "progress": 97, "message": f"Extended VO generated ({_ext_vo_duration:.1f}s)",
                        })

                    # -----------------------------------------------------------
                    # Step 10.4: Concat full clips
                    # -----------------------------------------------------------
                    if on_progress:
                        on_progress("step_start", {"step": "extended_concat", "label": "Extended: Concatenating", "message": "Concatenating full clips..."})

                    _ext_concat_data = [{"video_url": c["url"], "duration": c["duration"]} for c in extended_clips]
                    _ext_concat_url = processor.rendi_service.concatenate_videos(
                        video_data=_ext_concat_data,
                        dissolve_seconds=effective_dissolve,
                        video_only=True,
                    )

                    if on_progress:
                        on_progress("step_complete", {
                            "step": "extended_concat", "label": "Extended: Concatenating",
                            "progress": 97, "message": "Full clips concatenated",
                        })

                    if not _ext_concat_url:
                        logger.warning(f"   [Row {row_num}] Extended: concat failed, aborting extended version")
                        raise RuntimeError("Extended concat failed")

                    # -----------------------------------------------------------
                    # Step 10.5: Add VO + music
                    # -----------------------------------------------------------
                    if on_progress:
                        on_progress("step_start", {"step": "extended_audio_mix", "label": "Extended: Adding audio", "message": "Mixing VO and music..."})

                    _ext_music_url = result.get("music_url", "")
                    _ext_final = processor.rendi_service.add_vo_and_music_to_video(
                        video_url=_ext_concat_url,
                        vo_url=_ext_vo_audio_url or "",
                        music_url=_ext_music_url,
                        vo_volume=1.0,
                        music_volume=0.2,
                        video_duration=total_extended_duration,
                    )
                    if _ext_final:
                        logger.info(f"   [Row {row_num}] Extended: VO + music mixed")
                    else:
                        _ext_final = _ext_concat_url
                        logger.warning(f"   [Row {row_num}] Extended: VO+music mix failed, using video-only concat")

                    if on_progress:
                        on_progress("step_complete", {
                            "step": "extended_audio_mix", "label": "Extended: Adding audio",
                            "progress": 98, "message": "Audio mixed",
                        })

                    # -----------------------------------------------------------
                    # Step 10.6: Trim to extended VO length
                    # -----------------------------------------------------------
                    if _ext_vo_duration > 0 and _ext_final:
                        _ext_trim_target = _ext_vo_duration + 2.5
                        try:
                            _ext_actual_dur = processor.rendi_service.get_video_duration_cloud(_ext_final)
                            if _ext_actual_dur > _ext_trim_target + 1.0:
                                _ext_trimmed = processor.rendi_service.trim_video(
                                    video_url=_ext_final, duration=_ext_trim_target, has_audio=True,
                                )
                                if _ext_trimmed:
                                    _ext_final = _ext_trimmed
                                    logger.info(f"   [Row {row_num}] Extended: trimmed to {_ext_trim_target:.1f}s")
                        except Exception as e:
                            logger.warning(f"   [Row {row_num}] Extended: trim failed ({e})")

                    # -----------------------------------------------------------
                    # Step 10.7: Add subtitles (if enabled)
                    # -----------------------------------------------------------
                    if add_subtitles and processor.zapcap_service and _ext_final and _ext_word_segments:
                        try:
                            _ext_subtitled = processor.zapcap_service.add_subtitles(
                                video_url=_ext_final,
                                language=subtitle_language,
                                transcript=_ext_word_segments,
                                subtitle_position=subtitle_position,
                            )
                            if _ext_subtitled:
                                _ext_final = processor.rendi_service.transcode_social_sharing_mp4(_ext_subtitled) or _ext_subtitled
                                logger.info(f"   [Row {row_num}] Extended: subtitles added")
                            if on_progress:
                                usage_data = {
                                    "service": "zapcap", "step": "extended_subtitles",
                                    "model": "zapcap", "provider": "zapcap",
                                    "duration_seconds": _ext_vo_duration,
                                    "label": "Extended subtitles", "category": "subtitles",
                                    "success": bool(_ext_subtitled),
                                }
                                on_progress("usage", usage_data)
                                usage_list.append(usage_data)
                        except Exception as e:
                            logger.warning(f"   [Row {row_num}] Extended: subtitles failed ({e})")

                    # -----------------------------------------------------------
                    # Step 10.8: Append end card
                    # -----------------------------------------------------------
                    if _end_card_url and _ext_final:
                        try:
                            _ext_pre_ec_dur = processor.rendi_service.get_video_duration_cloud(_ext_final)
                            if _ext_pre_ec_dur > 0:
                                _ec_duration_ext = END_CARD_DURATION
                                _ec_dissolve_ext = min(effective_dissolve, 0.4)
                                _ec_result_ext = processor.rendi_service.append_end_card(
                                    video_url=_ext_final,
                                    end_card_url=_end_card_url,
                                    video_duration=_ext_pre_ec_dur,
                                    end_card_duration=_ec_duration_ext,
                                    dissolve=_ec_dissolve_ext,
                                    music_url=_ext_music_url if has_music else None,
                                    music_volume=0.2,
                                )
                                if _ec_result_ext:
                                    _ext_final = _ec_result_ext
                                    logger.info(f"   [Row {row_num}] Extended: end card appended")
                        except Exception as e:
                            logger.warning(f"   [Row {row_num}] Extended: end card append failed ({e})")

                    # -----------------------------------------------------------
                    # Step 10.9: Safety trim + store result
                    # -----------------------------------------------------------
                    # Safety trim trailing black frame
                    if _ext_final:
                        try:
                            _ext_final_dur = processor.rendi_service.get_video_duration_cloud(_ext_final)
                            if _ext_final_dur > 1.0:
                                _ext_safe_dur = round(_ext_final_dur - 0.1, 3)
                                _ext_safety_trimmed = processor.rendi_service.trim_video(
                                    video_url=_ext_final, duration=_ext_safe_dur, has_audio=True,
                                )
                                if _ext_safety_trimmed:
                                    _ext_final = _ext_safety_trimmed
                        except Exception:
                            pass

                    result["extended_video_url"] = _ext_final
                    if on_progress:
                        on_progress("intermediate", {"key": "extended_video_url", "value": _ext_final})

                    # Calculate extended-only cost
                    _extended_cost = sum(u.get("cost_usd", 0) for u in usage_list[_extended_usage_start:])
                    if on_progress:
                        on_progress("intermediate", {"key": "extended_extra_cost_usd", "value": round(_extended_cost, 4)})

                    if on_progress:
                        on_progress("step_complete", {
                            "step": "extended_complete", "label": "Extended version complete",
                            "progress": 99,
                            "message": f"Extended version ready ({total_extended_duration:.0f}s)",
                        })

                    logger.info(f"   [Row {row_num}] ???? Extended version complete: {_ext_final[:60]}... (extra cost: ${_extended_cost:.4f})")

                except Exception as ext_err:
                    logger.warning(f"   [Row {row_num}] Extended version failed (non-blocking): {ext_err}")
                    if on_progress:
                        on_progress("step_complete", {
                            "step": "extended_complete", "label": "Extended version",
                            "progress": 99, "message": f"Extended version failed: {ext_err}",
                        })

        else:
            error = "Failed to concatenate videos"
            logger.error(f"   [Row {row_num}] {error}")
            result["errors"].append(error)
            
    except Exception as e:
        error = f"Error creating final video: {str(e)}"
        logger.error(f"   [Row {row_num}] {error}")
        result["errors"].append(error)

    result["usage"] = usage_list
    return result

