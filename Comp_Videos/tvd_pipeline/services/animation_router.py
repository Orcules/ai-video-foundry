"""Per-clip animation tool selector for the custom storyboard pipeline.

Given a clip from a storyboard, picks the most appropriate animation tool
(Veo / Seedance / Kling / Runway / Ken Burns / Rendi trim) using deterministic
rules. No LLM call.

This is currently consumed by:
  - `pipelines/custom.py` (to attach a `_tool_hint` to each beat_clip for downstream readers)
  - `api_pipeline/cost_tracker.py` (to walk a storyboard and estimate total cost)

The function is intentionally pure (no I/O) and easy to test.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Phrase heuristics
_DIALOG_HINTS = ("talking", "speaking", "speech", "dialog", "dialogue", "lip", "voice over scene")
_HEAVY_MOTION_HINTS = ("dolly", "tracking", "orbit", "pan fast", "whip", "crash zoom", "shake")
_MULTI_SHOT_HINTS = ("multi-shot", "multishot", "cut to", "then", "next shot", "intercut", "montage")
_CINEMATIC_STYLES = {"cinematic photography", "cinematic", "film noir", "epic"}

# Full set of tools the router can return. Storyboard's tool_hint must be in
# this set (validator in _storyboard.py).
ALL_TOOLS = ("veo", "seedance", "kling", "runway", "kenburns", "trim")


def _motion_is_simple(motion_prompt: str) -> bool:
    """Subtle/simple motion (ok for Ken Burns) — heuristic on length and keywords."""
    if not motion_prompt:
        return True
    mp = motion_prompt.lower()
    if any(h in mp for h in _HEAVY_MOTION_HINTS):
        return False
    if any(h in mp for h in _DIALOG_HINTS):
        return False
    return len(mp.split()) <= 12


def pick_tool(
    clip: Dict[str, Any],
    storyboard_meta: Optional[Dict[str, Any]] = None,
    available_tools: Optional[List[str]] = None,
) -> str:
    """Return one of: 'veo' | 'seedance' | 'kling' | 'runway' | 'kenburns' | 'trim'.

    Args:
        clip: a storyboard clip dict (`type`, `duration`, `motion_prompt`,
              `tool_hint`, `shows_character`/`shows_influencer`, `ingredients`,
              `source` with possible ref counts, ...).
        storyboard_meta: the storyboard's `meta` block (for `style`).
        available_tools: optional whitelist (e.g. the resolved resolution tier's
                         supported models). Defaults to all tools.
    """
    meta = storyboard_meta or {}
    available = set(available_tools or list(ALL_TOOLS))

    # Rule 1: explicit user override wins (subject to availability).
    hint = (clip.get("tool_hint") or "auto").lower()
    if hint != "auto" and hint in available:
        return hint

    ctype = clip.get("type") or "generate"
    duration = float(clip.get("duration") or 0.0)
    motion = (clip.get("motion_prompt") or clip.get("second_prompt") or "").lower()
    # Accept both new (shows_character) and legacy (shows_influencer) field names.
    shows_character = bool(clip.get("shows_character") or clip.get("shows_influencer"))
    style = (meta.get("style") or "").lower()
    ingredients = clip.get("ingredients") or {}

    # Rule 2: asset_video always trims with Rendi.
    if ctype == "asset_video":
        return "trim" if "trim" in available else next(iter(available))

    # Rule 3: ken_burns type, or very short clips with subtle motion.
    if ctype == "ken_burns":
        return "kenburns" if "kenburns" in available else "veo"
    if duration < 2.5 and _motion_is_simple(motion) and "kenburns" in available:
        return "kenburns"

    # Rule 4: explicit seedance_multishot type — that's exactly what Seedance is for.
    if ctype == "seedance_multishot" and "seedance" in available:
        return "seedance"

    # Rule 5: motion_graphic — text/UI animation is best handled by Kling (best
    # at text legibility) or Runway (clean composition). Skip Seedance/Veo for
    # text since they tend to garble fonts.
    if ctype == "motion_graphic":
        for fallback in ("kling", "runway", "kenburns"):
            if fallback in available:
                return fallback

    # Rule 6: multi-shot prompts (Director used phrases like "cut to" / "then")
    # are best handled by Seedance 2.0 (multi-shot consistency in one call).
    if any(h in motion for h in _MULTI_SHOT_HINTS) and "seedance" in available:
        return "seedance"

    # Rule 7: character-heavy clips with locked ingredients prefer Seedance.
    # Seedance accepts up to 9 reference images, making it the strongest tool
    # for "same character + same venue + same style across multiple shots".
    locked_refs_count = sum(1 for k in ("use_character_sheet", "use_venue_sheet", "use_style_sheet")
                            if ingredients.get(k))
    if shows_character and locked_refs_count >= 2 and "seedance" in available:
        return "seedance"

    # Rule 8: dialog / lip sync prefers Veo (still the best for spoken faces).
    if shows_character and any(h in motion for h in _DIALOG_HINTS):
        if "veo" in available:
            return "veo"

    # Rule 9: heavy camera motion prefers Kling.
    if any(h in motion for h in _HEAVY_MOTION_HINTS):
        if "kling" in available:
            return "kling"

    # Rule 10: cinematic subtle motion prefers Runway when available.
    if style in _CINEMATIC_STYLES and _motion_is_simple(motion) and "runway" in available:
        return "runway"

    # Rule 11: default — prefer Veo (best quality for I2V generally), else first available.
    for fallback in ("veo", "seedance", "kling", "runway", "kenburns", "trim"):
        if fallback in available:
            return fallback
    return "veo"  # ultimate fallback (shouldn't be reached)


def tool_to_model_provider(
    tool: str, tier: Optional[Dict[str, Any]] = None
) -> Tuple[str, str]:
    """Translate a tool name to `(video_model, video_provider)` strings the
    existing `_generate_video()` dispatcher understands.

    `tier` is the resolved resolution tier dict from `resolution_tiers.json`
    (its `video_model` / `video_provider` are honoured for `veo`).
    """
    tier = tier or {}
    if tool == "veo":
        return (
            tier.get("video_model", "veo-3.0-fast"),
            tier.get("video_provider", "direct"),
        )
    if tool == "kling":
        return ("kling-2.5", "kie")
    if tool == "seedance":
        return ("seedance-2", "kie")
    if tool == "runway":
        return ("runway-gen4-turbo", "kie")
    if tool == "kenburns":
        return ("kenburns", "ffmpeg")
    if tool == "trim":
        return ("rendi", "rendi")
    return (tier.get("video_model", "veo-3.0-fast"), tier.get("video_provider", "direct"))


def annotate_storyboard_with_tools(
    storyboard: Dict[str, Any], available_tools: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Walk every clip and stamp `_resolved_tool` so cost-estimators and UI can
    show which tool will run. Mutates and returns the storyboard."""
    meta = storyboard.get("meta") or {}
    for scene in storyboard.get("scenes") or []:
        for clip in scene.get("clips") or []:
            clip["_resolved_tool"] = pick_tool(clip, meta, available_tools)
    return storyboard
