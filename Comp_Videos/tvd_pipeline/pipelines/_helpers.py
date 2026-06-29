"""Shared helper functions used by both product and UGC pipelines.

These were originally private methods on VideoSceneProcessor.  They are
pure functions (or close to it) that accept a *processor* instance when
they need access to services.

Imported by::

    from tvd_pipeline.pipelines._helpers import ...
"""

import base64
import json
import math
import re
import time
import logging
from io import BytesIO
from typing import Dict, Any, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont

from tvd_pipeline.config import Config, get_pipeline_defaults
from tvd_pipeline.data_loader import get_speech_rate, get_language_name
from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.services.local_ffmpeg import LocalFFmpegFallback

config = Config()
logger = logging.getLogger(__name__)


def unified_scene_image_motion_prompts(scene: dict) -> Tuple[str, str]:
    """Resolve image + motion text for scene generation.

    Product scene LLM uses ``image_prompt`` / ``motion_prompt``. Studio and UGC
    flows often persist ``first_prompt`` / ``second_prompt`` only — without this
    merge, product ``generate_scene_visual`` would skip Kie/Vertex entirely.
    """
    raw_img = scene.get("image_prompt") or scene.get("first_prompt") or ""
    image_prompt = raw_img.strip() if isinstance(raw_img, str) else ""
    raw_mot = scene.get("motion_prompt") or scene.get("second_prompt") or ""
    motion_prompt = raw_mot.strip() if isinstance(raw_mot, str) else ""
    if not motion_prompt:
        motion_prompt = "Slow, subtle movement"
    return image_prompt, motion_prompt


def emit_llm_usage_events(processor, on_progress, usage_list, step_label, category="text"):
    """Emit one usage event per model from accumulated _call_llm entries.

    Groups entries by (model, provider) and emits separate on_progress("usage")
    events for each group. Replaces the old pattern of get_usage() + a single
    event with a hardcoded model name.

    Args:
        processor: VideoSceneProcessor with get_usage_by_model().
        on_progress: Callback (may be None — caller should guard).
        usage_list: List to append usage dicts to (may be None).
        step_label: Step name for the usage event (e.g. "scene_prompts").
        category: Cost category (default "text").
    """
    entries = processor.get_usage_by_model()
    emit_llm_usage_events_from_entries(entries, on_progress, usage_list, step_label, category)


def emit_llm_usage_events_from_entries(entries, on_progress, usage_list, step_label, category="text"):
    """Emit usage events from an explicit list of _call_llm usage entries.

    Use this when entries were accumulated in one window but should be
    attributed to different pipeline steps (e.g. parallel clean-product LLM vs VO).
    """
    if not entries:
        return

    # Group by (model, provider)
    grouped = {}
    for e in entries:
        key = (e["model"], e["provider"])
        if key not in grouped:
            grouped[key] = {"input_tokens": 0, "output_tokens": 0, "steps": []}
        grouped[key]["input_tokens"] += e["input_tokens"]
        grouped[key]["output_tokens"] += e["output_tokens"]
        if e["step_key"] not in grouped[key]["steps"]:
            grouped[key]["steps"].append(e["step_key"])

    for (model, provider), tokens in grouped.items():
        # Map provider to service name for cost tracker
        if provider == "openai":
            service = "openai"
        elif provider == "vercel":
            service = "vercel"
        else:
            service = "gemini_text"

        usage_data = {
            "service": service,
            "step": step_label,
            "model": model,
            "provider": provider,
            "input_tokens": tokens["input_tokens"],
            "output_tokens": tokens["output_tokens"],
            "label": f"{step_label} ({', '.join(tokens['steps'])})",
            "category": category,
            "success": True,
        }
        if on_progress:
            on_progress("usage", usage_data)
        if usage_list is not None:
            usage_list.append(usage_data)


def _presplit_vo_into_scenes(
    processor,
    structured_script: str,
    word_segments: List[Dict],
    total_duration: float
) -> List[Dict[str, Any]]:
    """Pre-split VO word segments into scene groups using '|||' markers from the structured script.
    
    Matches the scene segments in the structured script (separated by '|||') to the
    ElevenLabs word-level timestamps, producing per-scene groups with exact timing.
    
    Args:
        structured_script: VO script with '|||' separators between scenes
        word_segments: ElevenLabs word segments with start_time/end_time
        total_duration: Total VO duration in seconds
        
    Returns:
        List of dicts, each with: text, start_time, end_time, word_count, word_start_idx, word_end_idx
    """
    try:
        if not structured_script or "|||" not in structured_script or not word_segments:
            return []
        
        # Split script by ||| and clean each segment (remove audio tags for matching)
        raw_segments = [seg.strip() for seg in structured_script.split("|||") if seg.strip()]
        if len(raw_segments) < 2:
            return []
        
        # Build the full TTS text (without audio tags) for matching
        def strip_audio_tags(text: str) -> str:
            return re.sub(r'\[(?:excited|happily|awe|sorrowful|nervously|whispers|shouts|softly|dramatically|laughs|sighs|gasps|clears throat|light chuckle|pause)\]', '', text).strip()
        
        clean_segments = [strip_audio_tags(seg) for seg in raw_segments]
        
        # Build the full word list from ElevenLabs segments
        all_words = [ws["text"] for ws in word_segments]
        num_words = len(all_words)
        
        # For each scene segment, find the approximate word range by counting words
        # We match by word count proportion since ElevenLabs may slightly alter text
        scene_results = []
        current_word_idx = 0
        
        for seg_idx, clean_seg in enumerate(clean_segments):
            seg_words = clean_seg.split()
            seg_word_count = len(seg_words)
            
            if seg_word_count == 0:
                continue
            
            # Determine end word index for this segment
            if seg_idx == len(clean_segments) - 1:
                # Last segment gets all remaining words
                end_word_idx = num_words - 1
            else:
                end_word_idx = min(current_word_idx + seg_word_count - 1, num_words - 1)
            
            # Get timestamps from word segments
            start_time = word_segments[current_word_idx]["start_time"] if current_word_idx < num_words else 0.0
            end_time = word_segments[end_word_idx]["end_time"] if end_word_idx < num_words else total_duration
            
            # Collect the actual spoken text for this range
            actual_text = " ".join(all_words[current_word_idx:end_word_idx + 1])
            actual_word_count = end_word_idx - current_word_idx + 1
            
            scene_results.append({
                "scene_num": seg_idx + 1,
                "text": actual_text,
                "start_time": round(start_time, 3),
                "end_time": round(end_time, 3),
                "duration": round(end_time - start_time, 3),
                "word_count": actual_word_count,
                "word_start_idx": current_word_idx,
                "word_end_idx": end_word_idx
            })
            
            current_word_idx = end_word_idx + 1
        
        return scene_results if len(scene_results) >= 2 else []
        
    except Exception as e:
        logger.warning(f"Error pre-splitting VO into scenes: {e}")
        return []



def _rebalance_oversized_segments(
    scene_segments: List[Dict[str, Any]],
    word_segments: List[Dict],
    max_segment_duration: float = 12.0,
) -> List[Dict[str, Any]]:
    """Split oversized VO scene segments at sentence boundaries.

    If any segment is longer than *max_segment_duration*, it is subdivided
    into roughly equal sub-segments snapped to the nearest sentence
    boundary (`.`, `?`, `!`).  This prevents scenes that are too long for
    a single visual clip and ensures VO-visual sync throughout.

    Returns a new list (the original is not mutated).
    """
    if not scene_segments or not word_segments:
        return scene_segments

    result: List[Dict[str, Any]] = []
    for seg in scene_segments:
        dur = seg.get("duration", 0)
        if dur <= max_segment_duration:
            result.append(seg)
            continue

        w_start = seg["word_start_idx"]
        w_end = seg["word_end_idx"]
        n_words = w_end - w_start + 1
        if n_words < 6:
            result.append(seg)
            continue

        n_sub = max(2, round(dur / max_segment_duration))
        words_per_sub = n_words / n_sub

        sentence_ends = []
        for i in range(w_start, w_end + 1):
            w = word_segments[i]["text"].strip()
            if w and w[-1] in ".?!:" and i < w_end:
                sentence_ends.append(i)

        ideal_splits = [
            w_start + int(round((k + 1) * words_per_sub))
            for k in range(n_sub - 1)
        ]
        actual_splits: List[int] = []
        used = set()
        for ideal in ideal_splits:
            best = ideal
            best_dist = n_words
            for se in sentence_ends:
                d = abs(se - ideal)
                if d < best_dist and se not in used and w_start < se < w_end:
                    best = se
                    best_dist = d
            actual_splits.append(best)
            used.add(best)
        actual_splits = sorted(set(actual_splits))

        prev = w_start
        for sp in actual_splits:
            end_idx = sp
            if end_idx <= prev:
                end_idx = min(prev + max(3, int(words_per_sub)), w_end)
            st = word_segments[prev]["start_time"]
            et = word_segments[end_idx]["end_time"]
            result.append({
                "scene_num": len(result) + 1,
                "text": " ".join(ws["text"] for ws in word_segments[prev:end_idx + 1]),
                "start_time": round(st, 3),
                "end_time": round(et, 3),
                "duration": round(et - st, 3),
                "word_count": end_idx - prev + 1,
                "word_start_idx": prev,
                "word_end_idx": end_idx,
            })
            prev = end_idx + 1

        if prev <= w_end:
            st = word_segments[prev]["start_time"]
            et = word_segments[w_end]["end_time"]
            result.append({
                "scene_num": len(result) + 1,
                "text": " ".join(ws["text"] for ws in word_segments[prev:w_end + 1]),
                "start_time": round(st, 3),
                "end_time": round(et, 3),
                "duration": round(et - st, 3),
                "word_count": w_end - prev + 1,
                "word_start_idx": prev,
                "word_end_idx": w_end,
            })

    for i, s in enumerate(result):
        s["scene_num"] = i + 1
    logger.info(f"Rebalanced VO segments: {len(scene_segments)} -> {len(result)} (max_dur={max_segment_duration}s)")
    return result


def _presplit_vo_at_sentences(
    processor,
    word_segments: List[Dict],
    target_scene_count: int,
    total_duration: float
) -> List[Dict[str, Any]]:
    """Pre-split VO word segments into scene groups using ||| mandatory splits
    and sentence boundaries.

    Strategy:
    1. Find ||| positions in the word stream and record them as mandatory
       split points.
    2. Remove ||| entries from the working word list.
    3. Find sentence boundaries (words ending with . ? !).
    4. Build initial segments by splitting at the mandatory ||| positions.
    5. If fewer segments than target_scene_count, subdivide the longest
       segment at its best sentence boundary (nearest midpoint) until
       reaching the target or running out of sentence boundaries.
    6. Never split mid-sentence — accept fewer scenes rather than
       cutting inside a sentence.

    Args:
        word_segments: ElevenLabs word segments with start_time/end_time
        target_scene_count: Desired number of scene segments
        total_duration: Total VO duration in seconds

    Returns:
        List of dicts, each with: scene_num, text, start_time, end_time,
        duration, word_count, word_start_idx, word_end_idx
    """
    try:
        if not word_segments or target_scene_count < 2:
            return []

        # ------------------------------------------------------------------
        # 1. Identify ||| positions (before removing them)
        # ------------------------------------------------------------------
        # An entry counts as a ||| marker if its text is exactly "|||" or
        # contains "|||" (ElevenLabs may have it as spoken word or fused
        # with adjacent punctuation).
        marker_original_indices = set()
        for i, ws in enumerate(word_segments):
            if "|||" in ws.get("text", ""):
                marker_original_indices.add(i)

        # ------------------------------------------------------------------
        # 2. Build a clean word list (no ||| entries)
        # ------------------------------------------------------------------
        clean_words: List[Dict] = []
        # Track which clean indices sit right after a ||| marker.  These
        # become mandatory split boundaries (the segment starts here).
        mandatory_split_after: List[int] = []  # clean indices where a new segment starts

        for orig_idx, ws in enumerate(word_segments):
            if orig_idx in marker_original_indices:
                # Mark the *next* clean word as a mandatory split start
                mandatory_split_after.append(len(clean_words))
                continue
            clean_words.append(ws)

        num_words = len(clean_words)
        if num_words < target_scene_count * 2:
            # Too few real words to meaningfully split
            return []

        # Deduplicate and remove 0 (splitting at index 0 is a no-op)
        mandatory_split_starts = sorted(set(s for s in mandatory_split_after if 0 < s < num_words))

        # ------------------------------------------------------------------
        # 3. Find sentence boundary indices (clean indices)
        #    A sentence ends at a word whose text ends with . ? or !
        #    (not : or ; alone — those are weak boundaries).
        # ------------------------------------------------------------------
        sentence_end_indices: List[int] = []
        for i, ws in enumerate(clean_words):
            word = ws["text"].rstrip()
            if word and word[-1] in '.?!' and i < num_words - 1:
                sentence_end_indices.append(i)

        # ------------------------------------------------------------------
        # 4. Build initial segment boundaries from ||| mandatory splits
        #    Each boundary is the clean index where a new segment begins.
        # ------------------------------------------------------------------
        segment_starts = [0] + mandatory_split_starts
        # segment_starts is sorted and deduplicated; each value is the
        # first word index of a segment.

        # ------------------------------------------------------------------
        # 5. Subdivide until we reach target_scene_count
        #    Pick the segment with the most words, find the best sentence
        #    boundary near its midpoint, and split there.
        # ------------------------------------------------------------------
        # Track segments that cannot be subdivided (no internal sentence
        # boundary) so we don't re-attempt them on every iteration.
        unsplittable_starts: set = set()

        while len(segment_starts) < target_scene_count:
            # Build (start, end_exclusive) pairs for current segments
            seg_ranges = []
            for idx_s in range(len(segment_starts)):
                s = segment_starts[idx_s]
                e = segment_starts[idx_s + 1] if idx_s + 1 < len(segment_starts) else num_words
                seg_ranges.append((s, e))

            # Find the longest splittable segment (skip those already
            # proven unsplittable or too small)
            candidates = [
                (k, seg_ranges[k][1] - seg_ranges[k][0])
                for k in range(len(seg_ranges))
                if seg_ranges[k][0] not in unsplittable_starts
                and seg_ranges[k][1] - seg_ranges[k][0] >= 4
            ]
            if not candidates:
                break  # nothing left to split

            longest_idx = max(candidates, key=lambda c: c[1])[0]
            seg_s, seg_e = seg_ranges[longest_idx]
            seg_len = seg_e - seg_s

            # Find sentence boundaries strictly inside this segment
            # (after at least 1 word and before the last word)
            mid = seg_s + seg_len // 2
            best_boundary = None
            best_dist = num_words
            for se in sentence_end_indices:
                # se is the index of the last word of a sentence.
                # Splitting means the next segment starts at se+1.
                if se < seg_s + 1 or se >= seg_e - 1:
                    continue  # boundary not inside segment
                dist = abs(se - mid)
                if dist < best_dist:
                    best_dist = dist
                    best_boundary = se

            if best_boundary is None:
                # No sentence boundary inside this segment — mark it
                # unsplittable and try the next-longest segment.
                unsplittable_starts.add(seg_s)
                continue

            # Insert new segment start right after the sentence boundary
            new_start = best_boundary + 1
            segment_starts.append(new_start)
            segment_starts = sorted(set(segment_starts))

        # ------------------------------------------------------------------
        # 6. Build output dicts
        # ------------------------------------------------------------------
        scene_results = []
        for seg_idx in range(len(segment_starts)):
            seg_s = segment_starts[seg_idx]
            seg_e = (segment_starts[seg_idx + 1] if seg_idx + 1 < len(segment_starts) else num_words) - 1
            # seg_e is the index of the last word in this segment (inclusive)

            if seg_s > seg_e:
                continue

            start_time = clean_words[seg_s]["start_time"]
            end_time = clean_words[seg_e]["end_time"]
            text = " ".join(ws["text"] for ws in clean_words[seg_s:seg_e + 1])
            word_count = seg_e - seg_s + 1

            scene_results.append({
                "scene_num": len(scene_results) + 1,
                "text": text,
                "start_time": round(start_time, 3),
                "end_time": round(end_time, 3),
                "duration": round(end_time - start_time, 3),
                "word_count": word_count,
                "word_start_idx": seg_s,
                "word_end_idx": seg_e,
            })

        return scene_results if len(scene_results) >= 2 else []

    except Exception as e:
        logger.warning(f"Error pre-splitting VO at sentence boundaries: {e}")
        return []



def _count_vo_words(text: str) -> int:
    """Count spoken words in a VO script, excluding ||| markers and [audio tags]."""
    cleaned = text.replace("|||", " ")
    cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)
    return len(cleaned.split())


def _generate_vo_script_single(
    processor,
    text_1: str,
    text_2: str,
    text_3: str,
    scenes: List[Dict],
    target_duration: int = 30,
    language: str = "en",
    country: str = "",
    raw_prompt: str = "",
    text_4: str = "",
    wps_override: float = None,
    on_progress=None,
) -> str:
    """Generate a scene-structured voice over script for a product video.
    
    Generates VO text organized by scenes based on TEXT 4 (video structure),
    so each scene's VO segment has a natural break point aligned with the
    visual narrative. Uses '|||' as scene separator in the output.
    
    If a raw_prompt (original Prompt column) is provided with detailed script content,
    uses it as the primary source and adapts it for the target duration.
    Otherwise falls back to generating from TEXT 1-4.
    
    Args:
        text_1: What the video is about
        text_2: Goal of the video
        text_3: Content and style
        scenes: List of scene data
        target_duration: Target video duration in seconds
        language: Language code for VO script (e.g. 'he', 'en', 'es')
        country: Country code for cultural context (e.g. 'IL', 'US')
        raw_prompt: The original Prompt column text (may contain a full script)
        text_4: Video structure / scene-by-scene breakdown (used to structure VO by scenes)
        
    Returns:
        Voice over script text as single string, with '|||' separating scene segments.
    """
    try:
        total_duration = target_duration if target_duration else sum(s.get("duration", 3) for s in scenes)
        _speech_rate = wps_override or get_speech_rate(language)
        target_words = int(total_duration * _speech_rate)

        # Build language instruction
        lang_instruction = ""
        if language and language != "en":
            lang_name = get_language_name(language, default=language)
            lang_instruction = f"\n- CRITICAL: Write the ENTIRE script in {lang_name} ({language})."
            if language.lower().startswith("he"):
                from tvd_pipeline.services.tasks.voiceover import HEBREW_NIKUD_REMINDER
                lang_instruction += f"\n- {HEBREW_NIKUD_REMINDER}"
        
        country_instruction = ""
        if country:
            country_instruction = f"\n- Target audience is in {country}. Adapt cultural references, tone, and style accordingly."
        
        # Determine expected scene count from TEXT 4 or fallback
        scene_count = processor._estimate_scene_count_from_text4(text_4, target_duration)
        words_per_scene = max(5, target_words // scene_count) if scene_count > 0 else target_words
        
        # Scene structure instruction block
        scene_structure_block = ""
        if text_4 and text_4.strip():
            scene_structure_block = f"""
=== VIDEO SCENE STRUCTURE (TEXT 4) - STRUCTURE YOUR VO AROUND THESE SCENES ===
{text_4.strip()}

CRITICAL: Your VO script must follow this scene structure. Write VO text for EACH scene described above.
Separate each scene's VO text with '|||' (three pipe characters).
Each scene segment should have approximately {words_per_scene} words (~{_speech_rate} words per second).
The VO for each scene MUST match what that scene is about visually - this is essential for visual-audio coherence.
"""
        else:
            scene_structure_block = f"""
=== SCENE STRUCTURE ===
Structure the VO as {scene_count} distinct segments, separated by '|||' (three pipe characters).
Each segment corresponds to one visual scene (~{words_per_scene} words per scene, ~{_speech_rate} words per second).
Follow a natural narrative arc: hook → problem → solution → benefits → CTA.
"""
        
        # If raw_prompt contains a detailed script (long enough to be a real script), use it as primary source
        has_detailed_script = raw_prompt and len(raw_prompt.strip()) > 200
        
        if has_detailed_script:
            prompt = get_prompt_loader().get(
                "product_vo_script_from_brief",
                total_duration=total_duration,
                target_words=target_words,
                raw_prompt=raw_prompt.strip(),
                text_1=text_1,
                text_2=text_2,
                scene_structure_block=scene_structure_block,
                scene_count=scene_count,
                lang_instruction=lang_instruction,
                country_instruction=country_instruction,
            )
        else:
            prompt = get_prompt_loader().get(
                "product_vo_script_from_scratch",
                text_1=text_1,
                text_2=text_2,
                text_3=text_3,
                scene_structure_block=scene_structure_block,
                total_duration=total_duration,
                target_words=target_words,
                scene_count=scene_count,
                lang_instruction=lang_instruction,
                country_instruction=country_instruction,
            )

        # Single LLM call — no word-count retry loop (pipeline policy).
        prompt += (
            "\n\n=== LENGTH — SINGLE RESPONSE (no follow-up) ===\n"
            f"Produce exactly ONE complete script in this reply. Spoken-word budget: ~{target_words} words "
            f"for ~{total_duration}s at ~{_speech_rate:.2f} words/sec. "
            "Spoken words exclude [bracket audio tags] and the ||| separators themselves. "
            "Every scene segment must be fully written; the last segment must end with . ! or ? (not mid-sentence)."
        )

        call_fn = lambda msgs, **kw: processor._call_llm("generate_vo", msgs, **kw)
        messages = [
            {"role": "system", "content": "You are an expert copywriter for product videos. You write VO scripts structured by scenes, using '|||' to separate scene segments. Each segment's text corresponds to what the viewer sees in that scene."},
            {"role": "user", "content": prompt}
        ]

        _vo_max_out_tokens = 8192
        result = call_fn(messages, max_tokens=_vo_max_out_tokens, temperature=0.7)
        text = (result.get("text", "") or "").strip()
        if not text:
            return None
        actual_words = _count_vo_words(text)
        logger.info(
            "  Product VO script (single pass): %d spoken words (target ~%d for %ds)",
            actual_words, target_words, total_duration,
        )
        fr = (result.get("finish_reason") or "").lower()
        if fr in ("max_tokens", "length"):
            logger.warning(
                "  Product VO may be truncated (finish_reason=%s); consider shorter scenes or duration.",
                fr,
            )
        return text

    except Exception as e:
        logger.error(f"Error generating VO script: {e}")
        return None



def _estimate_scene_count_from_text4(processor, text_4, target_duration: int = 30) -> int:
    """Estimate the number of scenes from TEXT 4 content and target duration.

    Uses the LARGER of: (1) scene count inferred from TEXT 4, (2) duration-based
    estimate (~7s per scene).  Capped at MAX_SCENES.

    Args:
        text_4: Video structure -- may be a list (structured schema), plain text,
                numbered list, or JSON string.
        target_duration: Target video duration in seconds

    Returns:
        Estimated number of scenes (minimum 3, maximum MAX_SCENES=20)
    """
    _p_defaults = get_pipeline_defaults()
    _secs_per_scene = _p_defaults.get("target_seconds_per_scene", 7.0)
    count = 0
    duration_based = max(3, min(config.MAX_SCENES, int(target_duration // _secs_per_scene)))

    # Structured list from responseSchema -- just count it
    if isinstance(text_4, list):
        count = len(text_4)
        if count >= 3:
            return min(max(count, duration_based), config.MAX_SCENES)

    if text_4 and isinstance(text_4, str) and text_4.strip():
        t4 = text_4.strip()
        # Try JSON array
        try:
            parsed = json.loads(t4)
            if isinstance(parsed, list):
                count = len(parsed)
            elif isinstance(parsed, dict) and "scenes" in parsed:
                count = len(parsed["scenes"])
        except (json.JSONDecodeError, TypeError):
            pass

        if count == 0:
            # Count numbered items like "1.", "2.", "Scene 1", "scene 2", bullet points
            scene_patterns = re.findall(r'(?:^|\n)\s*(?:\d+[\.\):]|scene\s*\d+|[-*•])\s', t4, re.IGNORECASE)
            count = len(scene_patterns)

    if count < 3:
        count = duration_based
    else:
        # Ensure at least duration-based count so long videos get enough scenes
        count = max(count, duration_based)

    return min(count, config.MAX_SCENES)

# ------------------------------------------------------------------
# Phase 12: Advanced Sync — phrase_start strategy & precision trim
# ------------------------------------------------------------------

def _apply_phrase_start_strategy(
    scenes: List[Dict],
    word_timestamps: List[Dict],
    gemini_indices: List[Tuple[int, int]],
    vo_duration: float,
    last_scene_buffer: float = 1.0,
    min_scene_duration: float = 1.0,
) -> None:
    """Apply phrase-start sync strategy: each scene extends to the
    start_time of the next scene's first VO word, producing more
    natural transitions.

    Mutates *scenes* in-place, setting ``exact_duration``,
    ``overgenerate_duration``, ``vo_start_time`` and ``vo_end_time``.

    Args:
        scenes: Scene dicts (must have at least ``scene_num``).
        word_timestamps: ElevenLabs word segments (each has
            ``start_time`` and ``end_time``).
        gemini_indices: List of (word_start_idx, word_end_idx)
            tuples, one per scene.
        vo_duration: Total VO duration in seconds.
        last_scene_buffer: Extra seconds after VO end for the last
            scene (default 1.0).
        min_scene_duration: Minimum per-scene duration (default 1.0).
    """
    n = len(scenes)
    if n == 0 or not word_timestamps or not gemini_indices or len(gemini_indices) != n:
        return

    for i, scene in enumerate(scenes):
        w_s, _w_e = gemini_indices[i]
        # Scene starts at start_time of its first word (scene 0 always starts at 0)
        scene_start = word_timestamps[w_s]["start_time"] if i > 0 else 0.0

        if i < n - 1:
            # Extend to start of next scene's first word
            next_w_s = gemini_indices[i + 1][0]
            scene_end = word_timestamps[next_w_s]["start_time"]
        else:
            # Last scene: extend to VO end + buffer
            scene_end = vo_duration + last_scene_buffer

        exact_dur = max(min_scene_duration, scene_end - scene_start)
        scene["exact_duration"] = round(exact_dur, 3)
        scene["overgenerate_duration"] = math.ceil(exact_dur)
        scene["vo_start_time"] = round(scene_start, 3)
        scene["vo_end_time"] = round(scene_end, 3)
        # Keep "duration" in sync for downstream code that reads it
        scene["duration"] = round(exact_dur, 2)

def _precision_trim_clip(
    gcs_storage_service,
    video_url: str,
    exact_duration: float,
    row_num: int = 0,
    scene_num: int = 0,
) -> Optional[str]:
    """Locally trim a video clip to exact millisecond duration and
    upload to GCS.  Returns the trimmed GCS URL, or *None* on
    failure (caller should fall back to the original clip).

    Uses ``LocalFFmpegFallback.trim_video`` which runs FFmpeg
    locally: ``ffmpeg -i input -t <exact> -c:v libx264 -crf 18
    -preset fast -an output.mp4``.
    """
    try:
        trimmed_url = LocalFFmpegFallback.trim_video(
            gcs_storage_service, video_url, exact_duration
        )
        if trimmed_url:
            logger.info(
                f"   [Row {row_num}] Scene {scene_num}: Precision-trimmed to "
                f"{exact_duration:.3f}s (local FFmpeg)"
            )
            return trimmed_url
        else:
            logger.warning(
                f"   [Row {row_num}] Scene {scene_num}: Precision trim returned "
                f"None — keeping original clip"
            )
            return None
    except Exception as e:
        logger.warning(
            f"   [Row {row_num}] Scene {scene_num}: Precision trim error: {e}"
        )
        return None


def _evaluate_image_quality(processor, image_url: str, original_prompt: str) -> int:
    """Rate image quality 1-10 using Gemini vision. Returns 7 on error."""
    try:
        eval_prompt = (
            "Rate this image from 1-10 for quality, composition, and relevance to the following prompt. "
            "Consider: sharpness, artifacts, color accuracy, and how well it matches the description. "
            f"Reply with ONLY a single integer.\n\nPrompt: {original_prompt}"
        )
        if not processor.gemini_service or not processor.gemini_service.initialized:
            return 7

        # Build Vertex AI multimodal request with image
        image_part = None
        if image_url.startswith("gs://"):
            image_part = {"fileData": {"mimeType": "image/jpeg", "fileUri": image_url}}
        else:
            try:
                fetch_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "image/*,*/*;q=0.8",
                }
                img_resp = requests.get(image_url.strip(), headers=fetch_headers, timeout=30)
                img_resp.raise_for_status()
                ct = img_resp.headers.get("Content-Type", "").lower()
                mime = "image/png" if "png" in ct else "image/webp" if "webp" in ct else "image/jpeg"
                b64 = base64.b64encode(img_resp.content).decode("utf-8")
                image_part = {"inlineData": {"mimeType": mime, "data": b64}}
            except Exception:
                return 7  # Cannot fetch image, default pass

        payload = {
            "contents": [{"role": "user", "parts": [image_part, {"text": eval_prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 10},
        }
        model = getattr(config, "VERTEX_AI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash"
        result = processor.gemini_service._provider.raw_generate_content(payload, model=model)
        text = result.get("text", "")
        if text:
            match = re.search(r'\d+', text.strip())
            if match:
                return max(1, min(10, int(match.group())))
        return 7
    except Exception as e:
        logger.warning(f"Image quality evaluation failed: {e}")
        return 7


def _tpad_video(
    processor,
    video_url: str,
    pad_seconds: float,
    row_num: int = 0,
) -> Optional[str]:
    """Extend a video by freezing its last frame using FFmpeg tpad.

    This is a fallback when the Ken Burns filler approach fails.
    Uses Rendi's run-ffmpeg-command endpoint with the ``tpad`` filter.

    Args:
        processor: VideoSceneProcessor instance.
        video_url: Public URL of the video to extend.
        pad_seconds: Number of seconds to pad (freeze last frame).
        row_num: Row number for logging context.

    Returns:
        Public URL of the padded video, or None on failure.
    """
    try:
        pad_ms = int(pad_seconds * 1000)
        if pad_ms <= 0:
            return video_url

        payload = {
            "input_url": video_url,
            "ffmpeg_command": (
                f'-i {{input}} -vf "tpad=stop_mode=clone:stop_duration={pad_seconds:.2f}" '
                f"-c:a copy {{output}}"
            ),
            "output_extension": "mp4",
            "vcpu_count": 2,
            "max_command_run_seconds": 120,
        }

        url = f"{processor.rendi_service.base_url}/v1/run-ffmpeg-command"
        resp = requests.post(
            url,
            headers=processor.rendi_service.headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        result_url = processor.rendi_service._wait_for_command(resp.json()["command_id"])
        if result_url:
            logger.info(f"   [Row {row_num}] Video padded with tpad (+{pad_seconds:.1f}s)")
            return result_url
        else:
            logger.warning(f"   [Row {row_num}] tpad command returned no URL")
            return None
    except Exception as e:
        logger.warning(f"   [Row {row_num}] _tpad_video failed: {e}")
        return None


def _resolve_end_card_color(color_str: str) -> tuple:
    """Resolve an end card color string to an (R, G, B) tuple.

    Accepts hex (#FF6B9D) or a preset name (pink, gold, etc.).
    Falls back to white if unrecognized.
    """
    if color_str and color_str.startswith("#"):
        hex_str = color_str.lstrip("#")
        if len(hex_str) == 6:
            return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    # Lookup in presets
    defaults = get_pipeline_defaults()
    presets = defaults.get("end_card_color_presets", {})
    hex_val = presets.get(color_str, presets.get("white", "#FFFFFF"))
    hex_str = hex_val.lstrip("#")
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))


def _create_end_card_overlay_png(
    business_name: str,
    business_address: str,
    business_phone: str,
    width: int = 1080,
    height: int = 1920,
    end_card_color: str = "white",
    end_card_detail_color: str = "white",
    end_card_position: str = "middle",
    business_website: str = None,
) -> bytes:
    """Generate a transparent PNG overlay with minimal-style business text.

    Returns PNG bytes of a full-frame RGBA image with text near the bottom.
    No background box — text floats directly on the video with heavy drop shadow.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Resolve colors
    name_rgb = _resolve_end_card_color(end_card_color)
    detail_rgb = _resolve_end_card_color(end_card_detail_color)

    # --- Load fonts (Arial Bold for name, Arial Regular for details) ---
    def _load_font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
        paths = (
            ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"]
            if bold
            else ["C:/Windows/Fonts/arial.ttf"]
        )
        for p in paths:
            try:
                return ImageFont.truetype(p, size)
            except (OSError, IOError):
                continue
        # Linux / container fallback
        for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            try:
                return ImageFont.truetype(p, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    font_name = _load_font(bold=True, size=80)
    font_addr = _load_font(bold=False, size=46)
    font_phone = _load_font(bold=False, size=42)

    # --- Build text lines: (text, font, fill_rgba, stroke_radius, is_bold) ---
    lines = []
    if business_name:
        lines.append((business_name.upper(), font_name, (*name_rgb, 255), 3, True))
    if business_address:
        lines.append((business_address, font_addr, (*detail_rgb, 217), 2, False))  # 85%
    if business_phone:
        lines.append((business_phone, font_phone, (*detail_rgb, 191), 2, False))   # 75%
    if business_website:
        lines.append((business_website, font_phone, (*detail_rgb, 191), 2, False))  # 75%

    if not lines:
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    line_spacing = 20
    h_margin = 60  # px padding each side
    max_text_width = width - 2 * h_margin

    # Measure each line width & height, scale font down if text overflows
    measurements = []
    scaled_fonts = []
    for text, font, fill, stroke_r, is_bold in lines:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        visual_w = tw + 2 * stroke_r
        if visual_w > max_text_width:
            new_size = max(20, int(font.size * max_text_width / visual_w))
            font = _load_font(bold=is_bold, size=new_size)
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        measurements.append((tw, th))
        scaled_fonts.append(font)

    total_text_h = sum(th for _, th in measurements) + line_spacing * (len(lines) - 1)

    # Position: centered horizontally, vertical position based on end_card_position
    margin = int(height * 0.08)
    if end_card_position == "top":
        block_y = margin
    elif end_card_position == "middle":
        block_y = (height - total_text_h) // 2
    else:  # "bottom" (default)
        block_y = height - total_text_h - margin

    # --- Draw text lines with drop shadow ---
    cursor_y = block_y
    stroke_color = (0, 0, 0, 200)
    for i, (text, _orig_font, fill, stroke_r, _bold) in enumerate(lines):
        font = scaled_fonts[i]
        tw, th = measurements[i]
        tx = (width - tw) // 2

        # Draw dark stroke (loop offsets for readability on any background)
        for dx in range(-stroke_r, stroke_r + 1):
            for dy in range(-stroke_r, stroke_r + 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((tx + dx, cursor_y + dy), text, font=font,
                          fill=stroke_color)
        # Draw text in fill color
        draw.text((tx, cursor_y), text, font=font, fill=fill)

        cursor_y += th + line_spacing

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _add_end_card_text_overlay(
    processor,
    video_url: str,
    business_name: str,
    business_address: str,
    business_phone: str,
    row_num: int = 0,
    end_card_color: str = "white",
    end_card_detail_color: str = "white",
    end_card_position: str = "middle",
    business_website: str = None,
) -> Optional[str]:
    """Add a minimal-style text overlay to an end card clip.

    Generates a transparent PNG with Pillow, uploads to GCS, then
    composites onto the video via Rendi's FFmpeg overlay filter.

    Args:
        processor: VideoSceneProcessor instance (needs ``rendi_service``
            and ``gcs_storage_service``).
        video_url: Public URL of the end card clip.
        business_name: Business name (line 1, large).
        business_address: Address (line 2, medium).
        business_phone: Phone number (line 3, medium).
        row_num: Row number for logging context.
        end_card_color: Accent color for business name (preset or hex).
        end_card_detail_color: Color for address/phone (preset or hex).

    Returns:
        Public URL of the clip with overlay, or *None* on failure.
    """
    try:
        # 0. Probe video dimensions via Rendi remux (returns metadata)
        vid_w, vid_h = 1080, 1920  # safe fallback (portrait 9:16)
        try:
            probe_payload = {
                "ffmpeg_command": "-i {{in_1}} -c copy {{out_1}}",
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": "probe.mp4"},
                "vcpu_count": 1,
                "max_command_run_seconds": 30,
            }
            probe_url = f"{processor.rendi_service.base_url}/v1/run-ffmpeg-command"
            probe_resp = requests.post(
                probe_url, headers=processor.rendi_service.headers,
                json=probe_payload, timeout=30,
            )
            if probe_resp.status_code == 200:
                cmd_id = probe_resp.json().get("command_id")
                if cmd_id:
                    start_t = time.time()
                    while time.time() - start_t < 60:
                        check_url = f"{processor.rendi_service.base_url}/v1/commands/{cmd_id}"
                        check_resp = requests.get(check_url, headers=processor.rendi_service.headers, timeout=30)
                        check_resp.raise_for_status()
                        check_result = check_resp.json()
                        status = check_result.get("status", "").upper()
                        if status == "SUCCESS":
                            out_meta = check_result.get("output_files", {}).get("out_1", {})
                            w = out_meta.get("width")
                            h = out_meta.get("height")
                            if w and h:
                                vid_w, vid_h = int(w), int(h)
                                logger.info(f"   [Row {row_num}] End card clip dimensions: {vid_w}x{vid_h}")
                            break
                        elif status == "FAILED":
                            break
                        time.sleep(3)
        except Exception as probe_err:
            logger.warning(f"   [Row {row_num}] End card probe failed ({probe_err}), using 1080x1920 fallback")

        # 1. Generate the overlay PNG at the video's actual resolution
        png_bytes = _create_end_card_overlay_png(
            business_name, business_address, business_phone,
            width=vid_w, height=vid_h,
            end_card_color=end_card_color,
            end_card_detail_color=end_card_detail_color,
            end_card_position=end_card_position,
            business_website=business_website,
        )

        # 2. Upload to GCS
        overlay_key = f"end_card_overlay_{row_num}_{int(time.time())}.png"
        overlay_url = processor.gcs_storage_service.upload_image_bytes(
            png_bytes, overlay_key,
        )
        if not overlay_url:
            logger.warning(f"   [Row {row_num}] End card overlay: GCS upload failed")
            return None

        # 3. Composite via Rendi overlay (same pattern as overlay_logo_on_video)
        ffmpeg_command = (
            f"-i {{{{in_1}}}} -i {{{{in_2}}}} "
            f"-filter_complex \"[1:v][0:v]scale2ref[ovr][base];[base][ovr]overlay=0:0[out]\" "
            f"-map \"[out]\" -map 0:a? "
            f"-c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
            f"-c:a copy -movflags +faststart "
            f"{{{{out_1}}}}"
        )

        payload = {
            "ffmpeg_command": ffmpeg_command,
            "input_files": {
                "in_1": video_url,
                "in_2": overlay_url,
            },
            "output_files": {"out_1": "end_card_overlay.mp4"},
            "vcpu_count": 4,
            "max_command_run_seconds": 300,
        }

        url = f"{processor.rendi_service.base_url}/v1/run-ffmpeg-command"
        resp = requests.post(
            url,
            headers=processor.rendi_service.headers,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        result_url = processor.rendi_service._wait_for_command(resp.json()["command_id"])
        if result_url:
            logger.info(f"   [Row {row_num}] End card styled overlay applied")
            return result_url
        else:
            logger.warning(f"   [Row {row_num}] End card overlay: no URL returned")
            return None
    except Exception as e:
        logger.warning(f"   [Row {row_num}] End card overlay failed: {e}")
        return None
