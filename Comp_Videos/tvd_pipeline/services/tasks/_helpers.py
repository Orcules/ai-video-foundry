"""Shared helpers for task functions — JSON extraction, scene math, etc.

These are pure utility functions with no LLM calls and no provider dependency.
Extracted from ``gemini_text.py`` so that all task modules can reuse them.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json_from_response(content: str) -> Optional[Dict]:
    """Extract a JSON object from an LLM response.

    Handles cases where JSON is wrapped in markdown code blocks,
    embedded in surrounding prose, or is the entire response.
    """
    if not content:
        return None

    # Try to find JSON in code blocks first (greedy match to capture nested braces)
    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*\})\s*```', content)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find the outermost JSON object (product prompt has text_1, scene generation has scenes)
    first_brace = content.find('{')
    last_brace = content.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        potential_json = content[first_brace:last_brace + 1]
        try:
            parsed = json.loads(potential_json)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Try parsing entire content as JSON
    try:
        # Clean up the content - remove code block markers if present
        cleaned = content.strip()
        if cleaned.startswith('```'):
            # Remove opening code block marker (```json or ```)
            lines = cleaned.split('\n')
            # Remove first line (```json) and last line (```)
            if lines[-1].strip() == '```':
                cleaned = '\n'.join(lines[1:-1])
            else:
                cleaned = '\n'.join(lines[1:])
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Log the content for debugging
    logger.debug(f"Could not parse JSON from response: {content[:500]}...")
    return None


def fix_truncated_scene_json(text: str) -> Optional[Dict]:
    """Fix a truncated JSON response for scene prompts.

    Robustly handles cases where the JSON is cut off mid-string, mid-object,
    or mid-array.  Tries multiple strategies to recover as many complete
    scenes as possible.
    """
    # Strategy 1: Find all complete scene objects and reconstruct
    try:
        # Extract influencer_description if present
        desc_match = re.search(r'"influencer_description"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        influencer_desc = desc_match.group(1) if desc_match else ""

        # Find all complete scene objects with regex (narrative_role optional)
        complete_scenes = []
        scene_pattern = re.compile(
            r'\{\s*"scene_number"\s*:\s*(\d+)\s*,\s*'
            r'(?:"narrative_role"\s*:\s*"([^"]*)"\s*,\s*)?'
            r'"shows_influencer"\s*:\s*(true|false)\s*,\s*'
            r'"reference_image_index"\s*:\s*(-?\d+|null)\s*,\s*'
            r'(?:"(?:reference_video_index|video_asset_index)"\s*:\s*(?:-?\d+|null)\s*,\s*)?'
            r'(?:"best_moment_index"\s*:\s*(?:-?\d+|null)\s*,\s*)?'
            r'"first_prompt"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*'
            r'"second_prompt"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
            re.DOTALL
        )

        for match in scene_pattern.finditer(text):
            narrative_role = match.group(2) if match.lastindex >= 2 and match.group(2) else ""
            scene = {
                "scene_number": int(match.group(1)),
                "shows_influencer": match.group(3) == "true",
                "reference_image_index": None if match.group(4) == "null" else int(match.group(4)),
                "first_prompt": match.group(5),
                "second_prompt": match.group(6)
            }
            if narrative_role:
                scene["narrative_role"] = narrative_role
            complete_scenes.append(scene)

        if complete_scenes:
            logger.info(f"Recovered {len(complete_scenes)} complete scenes from truncated JSON")
            return {
                "influencer_description": influencer_desc,
                "scene_prompts": complete_scenes
            }
    except Exception as e:
        logger.debug(f"Strategy 1 failed: {e}")

    # Strategy 2: Try brute-force closing brackets/braces
    try:
        fixed_text = text.rstrip()
        # Check if we're inside a string (odd number of unescaped quotes)
        in_string = False
        i = 0
        while i < len(fixed_text):
            if fixed_text[i] == '\\' and in_string:
                i += 2
                continue
            if fixed_text[i] == '"':
                in_string = not in_string
            i += 1

        if in_string:
            fixed_text += '"'

        open_braces = fixed_text.count('{') - fixed_text.count('}')
        open_brackets = fixed_text.count('[') - fixed_text.count(']')
        fixed_text += ']' * max(0, open_brackets) + '}' * max(0, open_braces)

        result = json.loads(fixed_text)
        logger.info("Fixed truncated JSON by closing brackets/braces")
        return result
    except json.JSONDecodeError:
        pass

    # Strategy 3: Truncate to last complete scene and close
    try:
        last_complete = text.rfind('"second_prompt"')
        if last_complete > 0:
            after_key = text.find(':', last_complete) + 1
            open_q = text.find('"', after_key)
            if open_q > 0:
                pos = open_q + 1
                while pos < len(text):
                    if text[pos] == '\\':
                        pos += 2
                        continue
                    if text[pos] == '"':
                        truncated = text[:pos + 1] + '}]}'
                        try:
                            result = json.loads(truncated)
                            logger.info("Fixed truncated JSON by finding last complete scene")
                            return result
                        except json.JSONDecodeError:
                            break
                    pos += 1
    except Exception as e:
        logger.debug(f"Strategy 3 failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Empty-result factories
# ---------------------------------------------------------------------------

def get_empty_prompt_parse_result() -> Dict[str, str]:
    """Return empty result structure for prompt parsing."""
    return {"text_1": "", "text_2": "", "text_3": "", "text_4": ""}


def get_empty_scene_result() -> Dict[str, Any]:
    """Return empty result structure for scene generation."""
    return {"scenes": [], "total_duration": 0, "music_style": ""}


# ---------------------------------------------------------------------------
# Scene count / duration helpers
# ---------------------------------------------------------------------------

def get_scene_count_for_duration(target_duration: int) -> str:
    """Get recommended scene count based on target video duration.

    Each scene should be ~4-6 seconds so that animation clips (5-10s) can
    cover the scene via trimming or mild slow-motion (max 2x).

    Args:
        target_duration: Target video duration in seconds (10-120).

    Returns:
        String describing the recommended number of scenes (e.g. ``"4-5"``).
    """
    if target_duration <= 12:
        return "3-4"
    elif target_duration <= 18:
        return "4-5"
    elif target_duration <= 25:
        return "5-7"
    elif target_duration <= 32:
        return "7-9"
    elif target_duration <= 45:
        return "9-12"
    elif target_duration <= 60:
        return "12-15"
    elif target_duration <= 80:
        return "15-18"
    elif target_duration <= 100:
        return "18-20"
    else:  # 100-120
        return "20"


def get_scene_duration_range(target_duration: int) -> str:
    """Get recommended scene duration range based on target video duration.

    Scene durations are kept in a range that animation APIs (Runway/Kling)
    can produce: 5s or 10s clips with up to 2x slow-motion = max ~10-20s
    per scene.  Shorter scenes (4-6s) are preferred because they're easier
    to match.

    Args:
        target_duration: Target video duration in seconds (10-120).

    Returns:
        String describing the recommended scene duration (e.g. ``"2-3"``).
    """
    if target_duration <= 15:
        return "2-3.5"
    elif target_duration <= 25:
        return "2.5-4"
    elif target_duration <= 40:
        return "3-5"
    else:  # 41-120: keep scenes manageable for animation clips
        return "4-6"
