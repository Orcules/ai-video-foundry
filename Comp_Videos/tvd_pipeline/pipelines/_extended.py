"""Extended version generation — self-contained module for creating a longer
video from full-length raw clips with a new VO.

ISOLATION PRINCIPLE: This module does NOT import from, call, or modify
``voiceover.py`` or any existing prompt ``.md`` files. Extended VO uses a
single LLM call here so it stays isolated from influencer VO changes.
"""

import re
import logging
from typing import Any, Dict, List, Optional

from tvd_pipeline.data_loader import get_speech_rate
from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.utils import _word_count_for_duration

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VO cleanup (duplicated from voiceover.py for isolation)
# ---------------------------------------------------------------------------

def _clean_vo(llm_result) -> str:
    """Clean LLM result: remove stage directions, keep ElevenLabs audio tags."""
    script = (llm_result.get("text") or "").strip()
    if not script:
        return ""
    script = re.sub(r'\[Scene\s*\d+\]', '', script, flags=re.IGNORECASE)
    script = re.sub(r'\(.*?\)', '', script)
    return script.strip()


# ---------------------------------------------------------------------------
# Clip analysis
# ---------------------------------------------------------------------------

def analyze_extended_clips(
    processor,
    clips: List[Dict[str, Any]],
    on_progress: Optional[callable],
    usage_list: list,
    row_num: int,
) -> List[Dict[str, Any]]:
    """Analyze each clip with a vision LLM to get actual descriptions.

    Parameters
    ----------
    processor : VideoSceneProcessor
        Provides ``_call_llm()`` for LLM calls.
    clips : list[dict]
        Each dict has ``url``, ``duration``, ``type``.
    on_progress : callable or None
        Progress callback.
    usage_list : list
        Accumulated usage events.
    row_num : int
        Row number for logging.

    Returns
    -------
    list[dict]
        Same list with ``description`` added to each clip.
    """
    from tvd_pipeline.pipelines._helpers import emit_llm_usage_events

    loader = get_prompt_loader()
    system_prompt = loader.get("ugc_extended_analyze_clip_system")
    user_prompt = loader.get("ugc_extended_analyze_clip_user")

    for i, clip in enumerate(clips):
        clip_type = clip.get("type", "unknown")
        # For real footage / asset clips, use a generic description
        if clip_type in ("unknown", "video"):
            clip["description"] = "[real footage clip]"
            continue

        try:
            processor.reset_usage()
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "video_url", "video_url": clip["url"]},
                    ],
                },
            ]
            result = processor._call_llm("extended_analyze_clip", messages, temperature=0.3, max_tokens=256)
            desc = (result.get("text") or "").strip()
            clip["description"] = desc or "Visual clip"
            logger.info(f"   [Row {row_num}] Extended clip {i+1}: {desc[:80]}")
        except Exception as e:
            clip["description"] = "Visual clip"
            logger.warning(f"   [Row {row_num}] Extended clip {i+1} analysis failed: {e}")

        if on_progress:
            emit_llm_usage_events(processor, on_progress, usage_list, "extended_analyze_clip")

    return clips


# ---------------------------------------------------------------------------
# Extended VO generation (single LLM call)
# ---------------------------------------------------------------------------

def generate_extended_vo(
    processor,
    original_vo: str,
    clips: List[Dict[str, Any]],
    target_duration: float,
    language: str,
    on_progress: Optional[callable],
    usage_list: list,
    row_num: int,
) -> str:
    """Generate a new VO script for the extended version.

    Single LLM call — does NOT call voiceover.py and does not retry on word count.

    Parameters
    ----------
    processor : VideoSceneProcessor
        Provides ``_call_llm()``.
    original_vo : str
        The original VO script (``combined_script``).
    clips : list[dict]
        Clips with ``duration`` and ``description`` fields.
    target_duration : float
        Total extended video duration in seconds.
    language : str
        Language code (e.g. ``"en"``).
    on_progress : callable or None
        Progress callback.
    usage_list : list
        Accumulated usage events.
    row_num : int
        Row number for logging.

    Returns
    -------
    str
        The extended VO script text.
    """
    from tvd_pipeline.pipelines._helpers import emit_llm_usage_events

    wps = get_speech_rate(language)
    target_words = int(round(target_duration * wps))

    # Build clip sequence text
    clip_lines = []
    for i, clip in enumerate(clips):
        dur = clip.get("duration", 0)
        desc = clip.get("description", "Visual clip")
        clip_lines.append(f"Clip {i+1} ({dur:.1f}s): {desc}")
    clip_sequence = "\n".join(clip_lines)

    loader = get_prompt_loader()
    system_prompt = loader.get(
        "ugc_extended_vo_system",
        target_words=target_words,
        wps=wps,
        target_duration=target_duration,
    )
    user_prompt = loader.get(
        "ugc_extended_vo_user",
        original_vo=original_vo,
        clip_sequence=clip_sequence,
        target_duration=target_duration,
        target_words=target_words,
        wps=wps,
    )
    user_prompt += (
        "\n\n=== LENGTH — SINGLE RESPONSE (no follow-up) ===\n"
        f"One complete script only. Aim for ~{target_words} spoken words for "
        f"~{target_duration:.0f}s at ~{wps:.2f} words/sec. "
        "End the last segment with . ! or ?"
    )

    processor.reset_usage()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    llm_result = processor._call_llm("extended_vo", messages, temperature=0.7, max_tokens=8192)
    script = _clean_vo(llm_result)
    if on_progress:
        emit_llm_usage_events(processor, on_progress, usage_list, "extended_vo")

    if not script:
        logger.error(f"   [Row {row_num}] Extended VO: LLM returned no text")
        return ""

    word_count = _word_count_for_duration(script)
    segments = [s.strip() for s in script.split("|||") if s.strip()]
    logger.info(
        f"   [Row {row_num}] Extended VO (single pass): {word_count} words, "
        f"{len(segments)} beats (target ~{target_words}, {target_duration:.0f}s)"
    )
    return script
