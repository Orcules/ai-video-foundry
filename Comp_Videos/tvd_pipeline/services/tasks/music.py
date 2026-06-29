"""Provider-agnostic music description generation tasks.

Functions
---------
generate_music_description
    Product-video music description based on scene prompts.
    Consolidated from OpenAIService.

generate_music_description_from_text
    Influencer / personal-brand music description from content text and VO.
    Consolidated from GeminiService (has video_subtype support).
"""

import logging
from typing import Callable, Dict, List

from tvd_pipeline.prompt_loader import get_prompt_loader

logger = logging.getLogger(__name__)

# Default fallback descriptions
_PRODUCT_FALLBACK = (
    "upbeat corporate background music, modern electronic synths, "
    "professional and energetic, inspirational mood, no vocals"
)
_UGC_FALLBACK = (
    "upbeat trendy electronic music, modern synths with punchy drums, "
    "energetic and positive vibe, social media style, no vocals"
)


# ---------------------------------------------------------------------------
# Product-video music description (from scene prompts)
# ---------------------------------------------------------------------------

def generate_music_description(
    call_fn: Callable,
    scene_prompts: List[Dict],
) -> str:
    """Generate a music description based on video scene analysis.

    Analyses the video scenes and describes appropriate background music
    that matches the video's mood, style, and content.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` LLM dispatch.
        scene_prompts: List of scene prompt dicts with ``image_prompt`` key.

    Returns:
        Detailed music style description for Suno generation.
    """
    try:
        scenes_summary = "\n".join([
            f"Scene {i+1}: {sp.get('image_prompt', '')[:300]}"
            for i, sp in enumerate(scene_prompts[:6])
        ])

        if not scenes_summary.strip():
            logger.warning("No scene prompts available for music description")
            return _PRODUCT_FALLBACK

        prompt = get_prompt_loader().get(
            "product_music_description_user",
            scenes_summary=scenes_summary,
        )

        logger.info("Generating dynamic music description...")

        system_content = (
            "You are a professional music director who describes background "
            "music for videos. You always describe instrumental music only, "
            "no vocals."
        )

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ]
        result = call_fn(messages, max_tokens=250)
        description = (result.get("text") or "").strip()

        if not description:
            logger.warning("call_fn returned no text for music description")
            return _PRODUCT_FALLBACK

        logger.info("Generated music description: %s", description)
        return description

    except Exception as e:
        logger.warning("Could not generate music description: %s", e)
        return _PRODUCT_FALLBACK


# ---------------------------------------------------------------------------
# Influencer / Personal-Brand music description (from text + VO)
# ---------------------------------------------------------------------------

def generate_music_description_from_text(
    call_fn: Callable,
    content_text: str,
    vo_script: str = "",
    video_subtype: str = "influencer",
) -> str:
    """Generate a music description from content text and VO script.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` LLM dispatch.
        content_text: Free text content describing the product / experience.
        vo_script: Optional voice-over script. Music mood MUST match the
            VO tone and emotional arc.
        video_subtype: ``"influencer"`` (trendy/social) or
            ``"personal_brand"`` (professional/corporate).

    Returns:
        Detailed music style description for Suno generation.
    """
    try:
        mode_label = (
            "personal brand"
            if video_subtype == "personal_brand"
            else "influencer"
        )
        logger.info(
            "Generating music description for %s mode...", mode_label,
        )

        style_note = (
            "Professional, polished background music for thought-leadership "
            "or B2B content. Avoid overly casual or TikTok-style; lean "
            "corporate, inspiring, or documentary."
            if video_subtype == "personal_brand"
            else "Trendy, engaging background music for influencer/UGC videos."
        )

        system_prompt = (
            f"You are a professional music director for social media content. "
            f"You describe background music for {mode_label} videos. "
            f"{style_note} Always instrumental only, no vocals. The music "
            f"MUST match the emotional tone and arc of the voice-over script."
        )

        vo_section = ""
        if vo_script and len(vo_script.strip()) > 20:
            vo_section = (
                f"\n\nVOICE-OVER SCRIPT (the music plays BEHIND this "
                f"narration -- the mood MUST match):\n"
                f"{vo_script[:2000]}\n\n"
                f"CRITICAL: Read the VO above carefully. The music must "
                f"match its emotional arc:\n"
                f"- If the VO starts with a problem/tension -> music should "
                f"feel slightly tense or curious at first\n"
                f"- If the VO builds to a positive discovery -> music should "
                f"build and become uplifting\n"
                f"- If the VO is warm and personal -> music should be warm "
                f"and intimate\n"
                f"- If the VO is energetic and excited -> music should be "
                f"energetic\n"
                f"- The music mood must SUPPORT the VO, not contradict it. "
                f"If the VO is emotional and personal, do NOT describe "
                f'generic "upbeat pop" -- describe music that matches THAT '
                f"specific emotion."
            )

        user_prompt = get_prompt_loader().get(
            "ugc_music_description_user",
            content_text=content_text[:1500],
            vo_section=vo_section,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        llm_result = call_fn(messages, temperature=0.7, max_tokens=250)
        description = (llm_result.get("text") or "").strip()

        if not description:
            logger.warning(
                "call_fn returned no text for music description"
            )
            return _UGC_FALLBACK

        logger.info("Generated music description: %s", description)
        return description

    except Exception as e:
        logger.warning(
            "Could not generate music description (%s): %s", mode_label, e,
        )
        return _UGC_FALLBACK
