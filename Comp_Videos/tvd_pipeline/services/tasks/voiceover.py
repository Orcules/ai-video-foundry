"""Provider-agnostic voice-over generation tasks.

Functions
---------
generate_influencer_vo_script
    First-person, scene-structured VO for influencer / personal-brand videos.
    Single LLM pass with explicit length instructions.

generate_vo_script_from_article
    Product-video VO based on article content and scene visuals.
    Consolidated from OpenAIService.
"""

import json
import logging
import re
from typing import Callable, Dict, List, Optional

from tvd_pipeline.data_loader import get_language_name, get_speech_rate
from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.utils import _word_count_for_duration

logger = logging.getLogger(__name__)

# Hebrew nikud spec: minimal nikud only where needed for pronunciation.
# See hebrew_nikud_spec_for_developer.md for full spec.
HEBREW_NIKUD_SYSTEM = (
    "HEBREW — YOU MUST ADD MINIMAL NIKUD: Do not output fully unpointed text. "
    "Add nikud (dagesh and vowel points) where the spec requires: (1) Letters that "
    "can be read two ways — use dagesh for בּ, כּ, פּ when disambiguation is needed; "
    "(2) Rare, ambiguous, or foreign words where pronunciation is not obvious. "
    "Most of the script stays unpointed (כתיב ללא ניקוד); but you MUST add nikud "
    "in those cases so TTS pronounces correctly. No full nikud on every word."
)
HEBREW_NIKUD_REMINDER = (
    "HEBREW: You MUST add minimal nikud where needed: בּ/כּ/פּ for disambiguation, "
    "and on rare/foreign words. Do not output completely unpointed text. Rest: unpointed."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trim_to_word_budget_at_sentence_boundaries(text: str, max_words: int) -> str:
    """Trim text to at most max_words, cutting only at sentence boundaries.

    Preserves sentence integrity: we never cut mid-sentence. If the last
    sentence would exceed max_words, we keep it whole (script may exceed
    max_words). Handles Hebrew and Latin punctuation (., !, ?).
    """
    text = text.strip()
    if not text:
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    # Find sentence boundaries: word index after which a sentence ends.
    # A word ends a sentence if it ends with . ! ? (Latin or common in Hebrew).
    sentence_ends: List[int] = []
    for i, w in enumerate(words):
        if re.search(r"[.!?]\s*$", w) or re.search(r"[.!?]$", w):
            sentence_ends.append(i + 1)
    if not sentence_ends:
        # No sentence boundaries: keep whole text (sentence integrity over length)
        logger.debug(
            "No sentence boundary in segment (%d words), keeping whole (max %d)",
            len(words), max_words,
        )
        return text
    # Take the last sentence end that is <= max_words
    best = 0
    for end in sentence_ends:
        if end <= max_words:
            best = end
        else:
            break
    if best == 0:
        # First sentence is longer than max_words; keep it whole
        return text
    return " ".join(words[:best]).strip()


def _merge_excess_segments(segments: List[str], target_count: int) -> str:
    """Merge adjacent short segments until we have exactly *target_count*.

    Repeatedly finds the shortest segment and merges it with its shorter
    neighbour until the segment list reaches the target size.
    """
    merged = list(segments)
    while len(merged) > target_count and len(merged) > 1:
        shortest_idx = min(
            range(len(merged)),
            key=lambda i: len(merged[i].split()),
        )
        if shortest_idx == 0:
            merge_with = 1
        elif shortest_idx == len(merged) - 1:
            merge_with = shortest_idx - 1
        else:
            left_len = len(merged[shortest_idx - 1].split())
            right_len = len(merged[shortest_idx + 1].split())
            merge_with = shortest_idx - 1 if left_len <= right_len else shortest_idx + 1
        lo, hi = sorted([shortest_idx, merge_with])
        merged[lo] = merged[lo].rstrip() + " " + merged[hi].lstrip()
        del merged[hi]
    return " ||| ".join(merged)


# ---------------------------------------------------------------------------
# VO validation schema + helpers
# ---------------------------------------------------------------------------

VO_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["missing"],
    "additionalProperties": False,
}


def _clean_vo(llm_result):
    """Clean LLM result: remove stage directions, keep ElevenLabs audio tags."""
    script = (llm_result.get("text") or "").strip()
    if not script:
        return ""
    script = re.sub(r'\[Scene\s*\d+\]', '', script, flags=re.IGNORECASE)
    script = re.sub(r'\(.*?\)', '', script)
    return script.strip()


def _validate_vo_highlights(call_fn, highlights_data, vo_script):
    """LLM checks if VO mentions the curated business highlights.
    Returns list of missing highlight texts (empty = all covered).
    Returns [] on error (non-blocking).
    """
    try:
        loader = get_prompt_loader()
        highlights_list = "\n".join(f"- {h['highlight']}" for h in highlights_data)
        prompt = loader.get(
            "shared_vo_highlights_validation_system",
            highlights_list=highlights_list,
            vo_script=vo_script,
        )
        result = call_fn(
            [{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=2000,
            responseSchema=VO_VALIDATION_SCHEMA,
        )
        data = json.loads(result.get("text", "{}"))
        return data.get("missing", [])
    except Exception as e:
        logger.warning("VO highlights validation failed: %s", e)
        return []


def _validate_vo_content(call_fn, raw_prompt, vo_script):
    """LLM checks if VO captures key selling messages from the raw prompt.
    Returns list of missing messages (empty = all covered).
    Returns [] on error (non-blocking).
    """
    try:
        loader = get_prompt_loader()
        prompt = loader.get(
            "shared_vo_content_validation_system",
            original_prompt=raw_prompt[:3000],
            vo_script=vo_script,
        )
        result = call_fn(
            [{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=2000,
            responseSchema=VO_VALIDATION_SCHEMA,
        )
        data = json.loads(result.get("text", "{}"))
        return data.get("missing", [])
    except Exception as e:
        logger.warning("VO content validation failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Influencer / Personal-Brand VO
# ---------------------------------------------------------------------------

def generate_influencer_vo_script(
    call_fn: Callable,
    free_text: str,
    target_duration: float,
    manual_instructions: str = "",
    language: str = "en",
    original_vo_transcript: str = "",
    raw_prompt: str = "",
    text_4: str = "",
    video_subtype: str = "influencer",
    wps_override: float = None,
    cta_slogan: str = "",
    media_descriptions: str = "",
    arc_beats: str = "",
    highlights_text: str = "",
    highlights_data: List[Dict[str, str]] = None,
    on_progress=None,
    max_words_override: int = None,
    # Deprecated — kept for backward compatibility, ignored
    scene_count: int = 0,
) -> str:
    """Generate a first-person voice-over script in a single LLM call.

    Produces VO text with ``|||`` separating narrative beats. Word-count
    convergence and corrective retries are disabled — one generation only.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` LLM dispatch.
        free_text: Content describing the product / experience.
        target_duration: Target duration in seconds.
        manual_instructions: Optional custom instructions.
        language: ISO 639-1 language code.
        original_vo_transcript: Original video VO for style matching.
        raw_prompt: Raw user prompt (extra context).
        text_4: Video structure / scene-by-scene breakdown.
        video_subtype: ``"influencer"`` or ``"personal_brand"``.
        wps_override: Override words-per-second rate.
        media_descriptions: Pre-formatted asset context text.
        arc_beats: Formatted beat map text from arc template.
        scene_count: Deprecated, ignored.

    Returns:
        Voice-over script text with ``|||`` separating beats,
        or ``""`` on failure.
    """
    try:
        logger.info(
            "Generating influencer VO script (target: %.1fs, language: %s)...",
            target_duration, language,
        )

        wps = wps_override or get_speech_rate(language)
        target_words = int(target_duration * wps)
        if max_words_override is not None:
            target_words = max_words_override
        # Slight prompt headroom so undershoot still lands near duration target (no retries).
        target_words_prompt = target_words + max(10, int(target_words * 0.08))
        max_words_prompt = int(target_words * 1.15)

        language_name = get_language_name(language)

        # -- style guidance ------------------------------------------------
        if original_vo_transcript and len(original_vo_transcript) > 20:
            style_guidance = (
                f'\nSTYLE REFERENCE (match tone and energy):\n'
                f'"{original_vo_transcript[:1000]}"\n'
            )
        else:
            style_guidance = (
                "\nSTYLE: Authentic influencer voice - the speaker is the "
                "influencer sharing their personal experience in first person. "
                'Genuine, relatable, conversational, as if recommending to a '
                'friend ("I went there", "I tried it", "my favorite").\n'
            )

        # -- voice style override ------------------------------------------
        voice_style = ""
        if manual_instructions:
            if "third person" in manual_instructions.lower():
                voice_style = "Override: Use third person narrator style."
            elif "narrator" in manual_instructions.lower():
                voice_style = "Override: Use professional narrator style."

        # -- raw prompt section --------------------------------------------
        raw_prompt_section = ""
        if raw_prompt and raw_prompt.strip():
            raw_prompt_section = (
                f'\nORIGINAL USER PROMPT (the raw input about what this video '
                f'is about):\n"{raw_prompt.strip()[:2000]}"\n'
            )

        # -- Hebrew: per hebrew_nikud_spec — minimal nikud, unpointed rest ----
        hebrew_nikud_note = (
            HEBREW_NIKUD_SYSTEM
            if language and language.lower().startswith("he") else ""
        )

        # -- highlights section ---------------------------------------------
        highlights_section = ""
        if highlights_text:
            highlights_section = (
                "--- BUSINESS HIGHLIGHTS (weave these into the story naturally) ---\n"
                f"{highlights_text}\n"
            )

        # -- arc beats section ---------------------------------------------
        arc_beats_section = arc_beats if arc_beats else (
            "No specific beat map provided. Use natural narrative flow: "
            "Hook -> Story build -> Peak moment -> Payoff/CTA."
        )

        loader = get_prompt_loader()

        system_prompt = loader.get(
            "ugc_influencer_vo_system",
            target_duration=target_duration,
            target_words=target_words_prompt,
            max_words=max_words_prompt,
            language_name=language_name,
            hebrew_nikud_note=hebrew_nikud_note,
            cta_slogan=cta_slogan or "(none provided — use a short CTA that fits the story)",
            arc_beats=arc_beats_section,
        )

        # -- TEXT 4 scene structure ----------------------------------------
        text4_section = ""
        if text_4:
            if isinstance(text_4, list):
                lines = [
                    f"Scene {s.get('scene', '?')} ({s.get('purpose', '')}): {s.get('description', '')}"
                    for s in text_4
                ]
                text4_formatted = "\n".join(lines)
            else:
                text4_formatted = text_4.strip() if text_4 else ""
            if text4_formatted:
                text4_section = (
                    f"\n--- VIDEO SCENE STRUCTURE (use as context for your VO) "
                    f"---\n{text4_formatted}\n"
                )

        hebrew_nikud_reminder = (
            HEBREW_NIKUD_REMINDER
            if language and language.lower().startswith("he") else ""
        )
        special_instructions = (
            f"SPECIAL INSTRUCTIONS: {manual_instructions}"
            if manual_instructions else ""
        )

        user_prompt = loader.get(
            "ugc_influencer_vo_user",
            target_duration=target_duration,
            target_words=target_words_prompt,
            max_words=max_words_prompt,
            language_name=language_name,
            raw_prompt_section=raw_prompt_section,
            free_text=free_text[:2000],
            text4_section=text4_section,
            style_guidance=style_guidance,
            special_instructions=special_instructions,
            voice_style=voice_style,
            hebrew_nikud_reminder=hebrew_nikud_reminder,
            asset_context=media_descriptions,
            arc_beats=arc_beats_section,
            highlights_section=highlights_section,
        )

        user_prompt += (
            "\n\n=== LENGTH — SINGLE RESPONSE (no follow-up) ===\n"
            f"Deliver exactly ONE complete script. Spoken-word budget: {target_words}–{max_words_prompt} words "
            f"(~{target_duration:.0f}s at ~{wps:.2f} words/sec). "
            f"HARD CEILING: Do NOT exceed {max_words_prompt} spoken words — a longer script "
            f"produces a video that overshoots the {target_duration:.0f}s target. "
            "Do not count [bracket audio tags] or ||| toward spoken words. "
            "End the last beat with a full sentence (. ! or ?)."
        )
        if target_duration >= 45:
            _floor = max(int(target_words * 0.82), int(target_duration * wps * 0.78))
            user_prompt += (
                f"\n\n=== HARD FLOOR (long-form) ===\n"
                f"Target is a ~{target_duration:.0f}s video: include {_floor}–{max_words_prompt} spoken words "
                "(not counting [brackets] or |||). Add beats, sensory detail, and natural pacing — "
                "do not stop at a short teaser; the final runtime must justify the requested length. "
                f"But do NOT exceed {max_words_prompt} words — staying within budget is equally critical."
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        llm_result = call_fn(messages, temperature=0.7, max_tokens=8192)
        script = _clean_vo(llm_result)
        if not script:
            logger.error("call_fn returned no text for VO script")
            return ""

        word_count = _word_count_for_duration(script)
        segments = [s.strip() for s in script.split("|||") if s.strip()]
        logger.info(
            "Generated influencer VO script (pass 1): %d words, %d beats "
            "(target ~%d words, %.0fs)",
            word_count, len(segments), target_words, target_duration,
        )

        # Retry once if word count is below 70% of target — the LLM cut short.
        retry_floor = int(target_words * 0.70)
        if word_count < retry_floor:
            logger.warning(
                "VO word count %d < retry floor %d (70%% of %d) — retrying with stricter prompt",
                word_count, retry_floor, target_words,
            )
            retry_suffix = (
                f"\n\n=== RETRY — YOUR PREVIOUS SCRIPT WAS TOO SHORT ===\n"
                f"You wrote only ~{word_count} words. The minimum is {target_words} words "
                f"for a {target_duration:.0f}-second video. "
                f"Write the complete script again — add vivid scenes, sensory details, "
                f"and emotional moments to reach AT LEAST {target_words} spoken words. "
                f"Do NOT stop at a short teaser or summary."
            )
            retry_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt + retry_suffix},
            ]
            retry_result = call_fn(retry_messages, temperature=0.75, max_tokens=8192)
            retry_script = _clean_vo(retry_result)
            if retry_script:
                retry_wc = _word_count_for_duration(retry_script)
                logger.info(
                    "VO retry result: %d words (was %d, target %d)",
                    retry_wc, word_count, target_words,
                )
                if retry_wc > word_count:
                    script = retry_script
                    word_count = retry_wc
                    segments = [s.strip() for s in script.split("|||") if s.strip()]

        # Trim excess beats if script significantly exceeds ceiling (>130% of target)
        hard_max = int(target_words * 1.30)
        if word_count > hard_max and len(segments) > 3:
            logger.warning(
                "VO script %d words exceeds hard max %d (130%% of %d) — trimming middle beats",
                word_count, hard_max, target_words,
            )
            while word_count > max_words_prompt and len(segments) > 3:
                # Remove the longest middle beat (preserve first hook + last CTA)
                middle = segments[1:-1]
                longest_idx = max(range(len(middle)), key=lambda j: len(middle[j].split()))
                removed = middle.pop(longest_idx)
                logger.info("  Trimmed beat %d (%d words): %s...", longest_idx + 1, len(removed.split()), removed[:60])
                segments = [segments[0]] + middle + [segments[-1]]
                script = " ||| ".join(segments)
                word_count = _word_count_for_duration(script)
            logger.info("  After trim: %d words, %d beats", word_count, len(segments))

        logger.info(
            "Final influencer VO script: %d words, %d beats (target ~%d words, max ~%d, %.0fs)",
            word_count, len(segments), target_words, max_words_prompt, target_duration,
        )
        logger.info("VO Script preview: %s...", script[:200])
        return script

    except Exception as e:
        logger.error("Failed to generate influencer VO script: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Product-video VO from article
# ---------------------------------------------------------------------------

def generate_vo_script_from_article(
    call_fn: Callable,
    article_text: str,
    vertical: str,
    target_duration: float,
    target_language: str = "en",
    original_vo_transcript: Optional[str] = None,
    scene_prompts: Optional[List[Dict]] = None,
    gemini_vo_recommendations: Optional[Dict] = None,
) -> str:
    """Generate a voice-over script from article content and scene visuals.

    Creates a script suitable for TTS that matches the visuals AND content.
    The VO MUST match what is shown in the generated images.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` LLM dispatch.
        article_text: Full article text content.
        vertical: The vertical / offer name (headline).
        target_duration: Target duration in seconds.
        target_language: ISO 639-1 language code.
        original_vo_transcript: Optional original VO for style reference.
        scene_prompts: Optional list of scene dicts (``image_prompt`` key).
        gemini_vo_recommendations: Optional Gemini analysis with VO recs.

    Returns:
        Voice-over script text, or a fallback string on failure.
    """
    try:
        logger.info(
            "Generating VO script from article (target: %.1fs, language: %s)...",
            target_duration, target_language,
        )

        target_words = int(target_duration * get_speech_rate(target_language))
        language_name = get_language_name(target_language)

        # -- scene visuals section -----------------------------------------
        visuals_section = ""
        if scene_prompts:
            visuals_section = (
                "\nVIDEO SCENES (THE VISUALS THE VIEWER WILL SEE):\n"
                "The VO you write MUST match these visuals EXACTLY. The "
                "viewer will see these scenes while hearing your script.\n\n"
            )
            for i, prompt in enumerate(scene_prompts[:8], 1):
                image_prompt = prompt.get("image_prompt", "")[:300]
                if image_prompt:
                    visuals_section += f"Scene {i}: {image_prompt}\n\n"

            visuals_section += (
                "\nABSOLUTE RULE - VO MUST MATCH WHAT'S SHOWN!\n"
                "- The scenes above show what the viewer will SEE\n"
                "- Your VO must talk about EXACTLY what's shown\n"
                "- If scenes show garbage collection worker -> VO talks "
                "about waste collection jobs\n"
                "- If scenes show office work -> VO talks about office "
                "careers\n"
                "- If scenes show delivery driver -> VO talks about "
                "delivery/logistics jobs\n"
                "- NEVER write VO about Topic A when the video shows "
                "Topic B!\n"
                "- The article text is just REFERENCE - if it doesn't "
                "match the video, IGNORE IT!\n\n"
                "Example of WRONG: Video shows nurse in hospital, VO "
                'talks about "babysitting jobs"\n'
                "Example of RIGHT: Video shows nurse in hospital, VO "
                'talks about "healthcare careers"\n'
            )

        # -- Gemini recommendations ----------------------------------------
        gemini_guidance = ""
        if gemini_vo_recommendations:
            audio_analysis = gemini_vo_recommendations.get(
                "audio_analysis", {}
            )
            recommended_vo = gemini_vo_recommendations.get(
                "recommended_new_vo", {}
            )

            if audio_analysis or recommended_vo:
                gemini_guidance = "\nAI ANALYSIS OF ORIGINAL VIDEO:\n"
                if audio_analysis.get("voiceover_style"):
                    gemini_guidance += (
                        f"- VO Style: {audio_analysis['voiceover_style']}\n"
                    )
                if audio_analysis.get("voiceover_tone"):
                    gemini_guidance += (
                        f"- VO Tone: {audio_analysis['voiceover_tone']}\n"
                    )
                if audio_analysis.get("selling_approach"):
                    gemini_guidance += (
                        f"- Selling Approach: "
                        f"{audio_analysis['selling_approach']}\n"
                    )
                if audio_analysis.get("speaking_pace"):
                    gemini_guidance += (
                        f"- Speaking Pace: "
                        f"{audio_analysis['speaking_pace']}\n"
                    )
                key_phrases = audio_analysis.get("key_phrases", [])[:5]
                if key_phrases:
                    gemini_guidance += (
                        f"- Key Phrases to Include: "
                        f"{', '.join(key_phrases)}\n"
                    )

                if recommended_vo:
                    gemini_guidance += (
                        "\nRECOMMENDED NEW VO STRUCTURE:\n"
                    )
                    if recommended_vo.get("style_to_match"):
                        gemini_guidance += (
                            f"- Style: {recommended_vo['style_to_match']}\n"
                        )
                    if recommended_vo.get("tone_to_match"):
                        gemini_guidance += (
                            f"- Tone: {recommended_vo['tone_to_match']}\n"
                        )
                    messages_list = recommended_vo.get(
                        "key_messages_to_include", []
                    )[:3]
                    if messages_list:
                        gemini_guidance += (
                            f"- Key Messages: "
                            f"{', '.join(messages_list)}\n"
                        )
                    avoid_list = recommended_vo.get("avoid", [])[:3]
                    if avoid_list:
                        gemini_guidance += (
                            f"- AVOID: {', '.join(avoid_list)}\n"
                        )
                    if recommended_vo.get("suggested_structure"):
                        gemini_guidance += (
                            f"- Structure: "
                            f"{recommended_vo['suggested_structure']}\n"
                        )

            scene_breakdown = gemini_vo_recommendations.get(
                "scene_breakdown", []
            )
            if scene_breakdown:
                gemini_guidance += "\nPER-SCENE VO SUGGESTIONS:\n"
                for scene in scene_breakdown[:6]:
                    scene_num = scene.get("scene_number", "?")
                    rec_vo = scene.get(
                        "recommended_vo_for_new_video", ""
                    )
                    if rec_vo:
                        gemini_guidance += (
                            f"Scene {scene_num}: {rec_vo[:100]}...\n"
                        )

        # -- style guidance ------------------------------------------------
        if original_vo_transcript and len(original_vo_transcript) > 20:
            style_guidance = (
                f'\nORIGINAL VIDEO VO (MATCH THIS STYLE EXACTLY):\n'
                f'"{original_vo_transcript[:800]}"\n'
                f'{gemini_guidance}\n'
                f'\nANALYZE THE ORIGINAL VO AND MATCH:\n'
                f'- Tone: Is it energetic? Calm? Urgent? Friendly? '
                f'Professional?\n'
                f'- Structure: How does it flow? Hook -> Story -> CTA? '
                f'Question -> Answer?\n'
                f'- Pacing: Short punchy sentences? Longer flowing '
                f'narrative?\n'
                f'- Voice: First person "I"? Second person "You"? Third '
                f'person narrator?\n'
                f'- Language style: Casual? Formal? Conversational? '
                f'Dramatic?\n\n'
                f'Your new VO MUST feel like it belongs to the SAME video. '
                f'Same energy. Same rhythm. Same vibe. Just new content.\n'
            )
        else:
            _default_style = (
                "- Professional, engaging product advertisement tone\n"
                "- Direct and benefit-focused\n"
                "- Clear call-to-action at the end\n"
            )
            style_guidance = (
                f'\nNO ORIGINAL VO DETECTED - USE AI-ANALYZED STYLE:\n'
                f'{gemini_guidance if gemini_guidance else _default_style}'
            )

        # -- Hebrew: per hebrew_nikud_spec — minimal nikud, unpointed rest ----
        hebrew_nikud_note = ""
        if target_language and target_language.lower().startswith("he"):
            hebrew_nikud_note = "\n\n" + HEBREW_NIKUD_SYSTEM

        prompt = get_prompt_loader().get(
            "shared_vo_style_matching",
            style_guidance=style_guidance,
            visuals_section=visuals_section,
            article_text=article_text[:1500],
            target_words=target_words,
            target_duration=target_duration,
            language_name=language_name,
            hebrew_nikud_note=hebrew_nikud_note,
        )
        _wps_art = get_speech_rate(target_language)
        prompt += (
            "\n\n=== LENGTH — SINGLE RESPONSE (no follow-up) ===\n"
            f"Deliver exactly ONE complete script. Spoken-word budget: ~{target_words} words "
            f"(~{target_duration:.0f}s at ~{_wps_art:.2f} words/sec). "
            "End with a full sentence (. ! or ?)."
        )

        system_content = (
            f"You are an expert voice-over writer who can perfectly match "
            f"any style. When given an original VO, you analyze its tone, "
            f"rhythm, and structure and create new content that feels like "
            f"it belongs to the same video. You write in {language_name} "
            f"and your scripts sound natural when spoken."
        )

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ]
        result = call_fn(messages, temperature=0.7, max_tokens=8192)
        script = (result.get("text") or "").strip()

        if not script:
            logger.error("call_fn returned no text for article VO script")
            return "Discover something amazing today. Click to learn more."

        # Clean up stage directions, keep ElevenLabs v3 audio tags
        script = re.sub(r'\[Scene\s*\d+\]', '', script, flags=re.IGNORECASE)
        script = re.sub(r'\(.*?\)', '', script)
        script = script.strip()

        word_count = _word_count_for_duration(script)
        logger.info(
            "Generated VO script: %d words (target: %d)", word_count,
            target_words,
        )

        return script

    except Exception as e:
        logger.error("Failed to generate VO script: %s", e)
        return "Discover something amazing today. Click to learn more."
