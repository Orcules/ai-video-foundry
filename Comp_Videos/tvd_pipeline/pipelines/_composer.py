"""Composer — deterministic translator from Director storyboard to executor plan.

Stage between the Director (LLM that emits a storyboard) and the Executor
(`ugc.py:generate_scene_visual` closure that actually runs the API calls).

Responsibilities:
  1. Resolve per-clip animation tool via `animation_router.pick_tool()`.
  2. Package per-clip reference URLs from the storyboard's character_sheet /
     venue_sheet / style_sheet, capped per target tool (Veo: 3, Seedance: 9).
  3. Fold cinematic intent (per-scene `camera` dict) into each clip's
     motion_prompt as a short prefix.
  4. Decide which clips should be SIDE-CHANNEL pre-executed (Seedance multi-shot,
     motion graphics with Kling). These are run upfront and their URLs are
     written into `intermediates["scene_videos"][scene_idx]` so ugc.py skips
     re-generating them.

No LLM calls. Pure Python.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from tvd_pipeline.pipelines._storyboard import (
    cinematic_motion_words,
    package_ingredient_refs,
)
from tvd_pipeline.services.animation_router import pick_tool

logger = logging.getLogger(__name__)


# Per-tool maximum reference image count (input cap for the model).
_TOOL_MAX_REFS = {
    "veo": 3,         # Veo 3.1 Ingredients-to-Video: up to 3 reference images
    "seedance": 9,    # Seedance 2.0: up to 9 reference images
    "kling": 4,       # Kling: practical cap (varies by version)
    "runway": 3,      # Runway Gen-4.5 uses style + character refs
    "kenburns": 0,    # No refs
    "trim": 0,        # No refs
}


def compose(
    storyboard: Dict[str, Any],
    *,
    tier: Optional[Dict[str, Any]] = None,
    available_tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Annotate the storyboard with executor-ready fields (mutates and returns).

    After compose() each clip has:
      - `_resolved_tool`  — the tool the executor should use ('veo' | 'seedance' | ...)
      - `_resolved_refs`  — list of reference image URLs packaged per the clip's
                            `ingredients` flags + storyboard sheets, trimmed to
                            the resolved tool's max.
      - `_motion_with_camera` — motion_prompt prefixed with cinematic intent
                                (when the parent scene has a `camera` dict).

    The original `motion_prompt`, `first_prompt`, etc. are NOT replaced — these
    are additive metadata the executor can consume opportunistically.
    """
    meta = storyboard.get("meta") or {}
    scenes = storyboard.get("scenes") or []
    available = available_tools  # passed through to pick_tool

    for scene in scenes:
        camera = scene.get("camera") if isinstance(scene.get("camera"), dict) else None
        cam_prefix = cinematic_motion_words(camera) if camera else ""

        for clip in scene.get("clips") or []:
            # NEW: framework_render clips don't go through the AI-video tool router.
            # Until each framework's executor is wired (Manim / Remotion / HyperFrames /
            # Lottie), the Composer maps them to the FFmpeg path so they render as a
            # styled placeholder (Ken Burns over a generated still). This keeps the
            # pipeline working end-to-end and surfaces the Director's intent.
            if clip.get("type") == "framework_render":
                clip["_resolved_tool"] = "kenburns"
                clip["_resolved_video_model"] = "kenburns"
                clip["_resolved_video_provider"] = "ffmpeg"
                clip["_framework_placeholder"] = True  # executor / docs can highlight this
                # Fall through to camera / refs handling below
                continue

            # Resolve tool. E1: video_model_override takes precedence over tool_hint
            # and over the router's heuristics. The Composer back-translates the
            # explicit model into the tool name so downstream code paths work.
            override_model = clip.get("video_model_override")
            if override_model:
                # Map model name → tool name (inverse of tool_to_model_provider)
                if override_model.startswith("veo"):
                    tool = "veo"
                elif override_model.startswith("seedance"):
                    tool = "seedance"
                elif override_model.startswith("kling"):
                    tool = "kling"
                elif override_model.startswith("runway"):
                    tool = "runway"
                elif override_model == "kenburns":
                    tool = "kenburns"
                else:
                    tool = pick_tool(clip, meta, available)
                clip["_resolved_tool"] = tool
                clip["_resolved_video_model"] = override_model
                if clip.get("video_provider_override"):
                    clip["_resolved_video_provider"] = clip["video_provider_override"]
            else:
                tool = pick_tool(clip, meta, available)
                clip["_resolved_tool"] = tool

            # Package refs
            ing = clip.get("ingredients") or {}
            if ing.get("use_character_sheet") or ing.get("use_venue_sheet") or ing.get("use_style_sheet"):
                max_refs = _TOOL_MAX_REFS.get(tool, 3)
                refs = package_ingredient_refs(
                    storyboard,
                    use_character=bool(ing.get("use_character_sheet")),
                    use_venue=bool(ing.get("use_venue_sheet")),
                    use_style=bool(ing.get("use_style_sheet")),
                    max_total=max_refs,
                )
                if refs:
                    clip["_resolved_refs"] = refs

            # Fold camera into motion_prompt as a prefix (additive)
            base_motion = clip.get("motion_prompt") or clip.get("second_prompt") or ""
            if cam_prefix:
                if base_motion:
                    clip["_motion_with_camera"] = f"{cam_prefix}. {base_motion}"
                else:
                    clip["_motion_with_camera"] = cam_prefix

    return storyboard


def collect_side_channel_clips(
    storyboard: Dict[str, Any],
) -> List[Tuple[int, int, Dict[str, Any]]]:
    """Identify clips that should be pre-executed by the Composer (not ugc.py).

    These are scenes whose ONLY clip uses a tool that ugc.py's beat-clips path
    doesn't natively support — currently Seedance and Kling-motion-graphic
    when refs are involved.

    Returns a list of (scene_idx, clip_idx, clip_dict). Caller is responsible
    for executing each and stuffing the resulting URL into
    intermediates["scene_videos"][scene_idx].

    A scene is eligible only if it has exactly one clip whose _resolved_tool is
    in the side-channel set. Multi-clip scenes fall through to ugc.py normally.
    """
    side_channel_tools = {"seedance"}  # motion_graphic stays with ugc.py normal flow for D2
    out: List[Tuple[int, int, Dict[str, Any]]] = []
    for s_idx, scene in enumerate(storyboard.get("scenes") or []):
        clips = scene.get("clips") or []
        if len(clips) != 1:
            continue
        clip = clips[0]
        tool = clip.get("_resolved_tool")
        if tool in side_channel_tools:
            out.append((s_idx, 0, clip))
    return out


def execute_side_channel_clip(
    processor,
    storyboard: Dict[str, Any],
    scene_idx: int,
    clip: Dict[str, Any],
    *,
    simulation: bool = False,
    video_resolution: Optional[str] = None,
) -> Optional[str]:
    """Execute a single side-channel clip and return its video URL.

    Currently supports Seedance. Returns None on failure (caller should fall
    through to ugc.py's regular generation).
    """
    if simulation:
        # Match the simulation URL convention used by sim_pipeline_runner.
        return "https://storage.googleapis.com/automatiq/simulation/placeholder.mp4"

    tool = clip.get("_resolved_tool")
    if tool != "seedance":
        return None  # Only Seedance is wired right now

    motion_prompt = clip.get("_motion_with_camera") or clip.get("motion_prompt") or ""
    first_prompt = clip.get("first_prompt") or ""
    # Combine first_prompt + motion_prompt — Seedance benefits from both.
    full_prompt = (first_prompt + ". " + motion_prompt).strip(". ").strip() if first_prompt else motion_prompt
    if not full_prompt:
        logger.warning("[composer] Side-channel seedance clip has empty prompt — skipping")
        return None

    duration = int(round(float(clip.get("duration") or 5)))
    refs = clip.get("_resolved_refs") or []
    resolution = video_resolution or "720p"

    try:
        url = processor.kie_service.generate_video_seedance(
            prompt=full_prompt,
            reference_image_urls=refs if refs else None,
            duration=duration,
            resolution=resolution,
        )
        if url:
            logger.info(
                "[composer] Side-channel Seedance executed for scene %d "
                "(dur=%ss, refs=%d)", scene_idx + 1, duration, len(refs)
            )
        else:
            logger.warning(
                "[composer] Side-channel Seedance returned no URL for scene %d — "
                "falling back to ugc.py default path", scene_idx + 1
            )
        return url
    except Exception as e:
        logger.exception("[composer] Side-channel Seedance failed for scene %d: %s", scene_idx + 1, e)
        return None


def run_side_channel(
    processor,
    storyboard: Dict[str, Any],
    intermediates: Dict[str, Any],
    *,
    simulation: bool = False,
    video_resolution: Optional[str] = None,
    on_progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Walk the storyboard, execute side-channel clips, populate intermediates.

    Mutates and returns ``intermediates``. After this call,
    ``intermediates["scene_videos"]`` will have pre-generated URLs at the
    indices the side-channel handled; ugc.py's existing-intermediates skip
    logic will then bypass re-generation for those scenes.
    """
    pending = collect_side_channel_clips(storyboard)
    if not pending:
        return intermediates

    total_scenes = len(storyboard.get("scenes") or [])
    scene_videos: List[Any] = list(intermediates.get("scene_videos") or [None] * total_scenes)
    # Pad if shorter
    while len(scene_videos) < total_scenes:
        scene_videos.append(None)

    for s_idx, c_idx, clip in pending:
        if on_progress:
            on_progress("step_start", {
                "step": f"composer_seedance_s{s_idx + 1}",
                "label": f"Seedance multi-shot scene {s_idx + 1}",
                "message": "Side-channel execution via Composer (refs locked from sheets)",
            })

        url = execute_side_channel_clip(
            processor, storyboard, s_idx, clip,
            simulation=simulation, video_resolution=video_resolution,
        )
        if url:
            scene_videos[s_idx] = url
            if on_progress:
                on_progress("step_complete", {
                    "step": f"composer_seedance_s{s_idx + 1}",
                    "label": f"Seedance multi-shot scene {s_idx + 1}",
                    "message": f"Generated: {url[:80]}",
                })
                # Cost event so the tracker captures Seedance usage
                on_progress("usage", {
                    "service": "kie", "step": f"composer_seedance_s{s_idx + 1}",
                    "model": "seedance-2", "provider": "kie",
                    "duration_seconds": int(round(float(clip.get("duration") or 5))),
                    "label": "Seedance 2.0 (side-channel)",
                    "category": "video", "success": True,
                })

    intermediates["scene_videos"] = scene_videos
    return intermediates
