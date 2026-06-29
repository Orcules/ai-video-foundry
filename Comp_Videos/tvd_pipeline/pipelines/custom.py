"""Custom storyboard pipeline.

Takes a user-approved Storyboard JSON (built by the chat agent) and runs it
through the existing `process_ugc_video` infrastructure by pre-loading the
storyboard's scenes / VO / music into the `existing_intermediates` slot. This
way no carefully-tuned scene-generation logic is duplicated — the storyboard
is the single source of truth for what visuals run, and ugc.py just executes
them.

Entry point: `process_custom_video(processor, storyboard, **kwargs)`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from tvd_pipeline.pipelines._composer import compose  # run_side_channel reserved for D5/D7
from tvd_pipeline.pipelines._storyboard import (
    storyboard_to_ugc_kwargs,
    validate_storyboard,
)
from tvd_pipeline.pipelines.ugc import process_ugc_video

logger = logging.getLogger(__name__)


def _strip_script_markers(script: str) -> str:
    """Remove ||| segment markers used by the storyboard schema before TTS."""
    if not script:
        return ""
    return " ".join(part.strip() for part in script.split("|||") if part.strip())


def _synthesize_voiceover(
    processor,
    *,
    script: str,
    voice_id: Optional[str],
    language: str,
    row_num: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Run ElevenLabs TTS + upload to GCS. Returns dict with audio_url + word_segments."""
    text = _strip_script_markers(script)
    if not text:
        return None
    try:
        result = processor.elevenlabs_service.text_to_speech_with_timestamps(
            text=text,
            voice_id=voice_id,
            language=language,
        )
        if not result:
            logger.warning("[custom] ElevenLabs TTS returned no result")
            return None
        audio_bytes, word_segments = result
        key_name = f"custom_videos/row_{row_num or 'cli'}_vo_{int(time.time())}.mp3"
        audio_url = processor.gcs_storage_service.upload_audio_bytes(
            audio_data=audio_bytes, key_name=key_name
        )
        if not audio_url:
            logger.warning("[custom] Failed to upload VO audio to GCS")
            return None
        return {
            "audio_url": audio_url,
            "word_segments": word_segments or [],
        }
    except Exception as e:
        logger.exception("[custom] VO synthesis failed: %s", e)
        return None


def process_custom_video(
    processor,
    storyboard: Dict[str, Any],
    *,
    on_progress: Optional[Callable] = None,
    existing_intermediates: Optional[Dict[str, Any]] = None,
    simulation: bool = False,
    row_num: Optional[int] = None,
    **extra_ugc_kwargs: Any,
) -> Dict[str, Any]:
    """Execute a custom storyboard.

    Args:
        processor: VideoSceneProcessor instance (services container).
        storyboard: validated storyboard dict (see `_storyboard.py` for schema).
        on_progress: optional event callback (forwarded to process_ugc_video).
        existing_intermediates: extra intermediates from API resume/checkpoint —
            merged with storyboard-derived intermediates (storyboard wins on conflicts).
        simulation: skip real API calls if True.
        row_num: optional Sheet-mode row index for logging consistency.
        **extra_ugc_kwargs: passed through to process_ugc_video (e.g. video_model,
            image_model, output_resolution, asset URLs that were normalized by the API).

    Returns:
        Same shape as process_ugc_video — dict with "final_video_url" etc.
    """
    # ---- 1. Validate ----
    errors = validate_storyboard(storyboard)
    if errors:
        raise ValueError("Invalid storyboard: " + "; ".join(errors))

    meta = storyboard.get("meta") or {}
    vo = storyboard.get("voiceover") or {}

    if on_progress:
        on_progress("step_start", {
            "step": "custom_validate",
            "label": "Storyboard validation",
            "message": f"Approved storyboard: {len(storyboard.get('scenes') or [])} scenes, "
                       f"~{meta.get('target_duration_seconds', '?')}s",
        })

    # ---- 1.5. Compose: annotate clips with resolved tools + packaged refs + camera ----
    # This is the new D2 step. The Composer is deterministic — no LLM call.
    # After compose() each clip carries _resolved_tool, _resolved_refs, and
    # _motion_with_camera metadata that the side-channel executor below reads.
    compose(storyboard)

    # ---- 2. Convert storyboard -> ugc_kwargs + intermediates ----
    ugc_kwargs, sb_intermediates = storyboard_to_ugc_kwargs(storyboard)

    # ---- 3. Synthesize VO up-front (so ugc.py skips its own VO step) ----
    if not simulation and vo.get("script") and not (
        vo.get("audio_url") and vo.get("word_segments")
    ):
        if on_progress:
            on_progress("step_start", {
                "step": "custom_vo",
                "label": "Voice over",
                "message": "Synthesizing voiceover from storyboard script...",
            })
        synth = _synthesize_voiceover(
            processor,
            script=vo.get("script", ""),
            voice_id=vo.get("voice_id"),
            language=vo.get("language") or meta.get("language", "en"),
            row_num=row_num,
        )
        if synth:
            sb_intermediates["vo_script"] = vo.get("script", "")
            sb_intermediates["vo_audio_url"] = synth["audio_url"]
            sb_intermediates["vo_word_segments"] = synth["word_segments"]
            if on_progress:
                on_progress("intermediate", {"key": "vo_script", "value": vo.get("script", "")})
                on_progress("intermediate", {"key": "vo_audio_url", "value": synth["audio_url"]})
                on_progress("intermediate", {"key": "vo_word_segments", "value": synth["word_segments"]})
                # Emit usage so cost tracking picks it up
                from tvd_pipeline.config.data_loader import get_elevenlabs_config
                on_progress("usage", {
                    "service": "elevenlabs", "step": "tts",
                    "model": get_elevenlabs_config()["tts_model"], "provider": "elevenlabs",
                    "character_count": len(_strip_script_markers(vo.get("script", ""))),
                    "label": "Text-to-speech (storyboard VO)",
                    "category": "tts", "success": True,
                })

    # ---- 4. Merge with any caller-provided intermediates (storyboard wins) ----
    merged_intermediates: Dict[str, Any] = dict(existing_intermediates or {})
    merged_intermediates.update(sb_intermediates)

    # ---- 4.5 E1/E2 — preview_image_url per scene → cached scene_images ----
    # If the user already approved image previews (rendered by storyboard_previews.py),
    # those URLs become the I2V start frames so ugc.py's smart-mode skips T2I.
    # Build a list of the same length as scenes, with URL where available and None
    # where the executor still needs to generate the image.
    sb_scenes = storyboard.get("scenes") or []
    preview_urls = [s.get("preview_image_url") for s in sb_scenes]
    if any(u for u in preview_urls):
        # Merge with any existing scene_images already in intermediates
        existing_imgs = merged_intermediates.get("scene_images") or []
        seeded = []
        for i in range(len(sb_scenes)):
            from_existing = existing_imgs[i] if i < len(existing_imgs) else None
            from_preview = preview_urls[i]
            # Existing intermediate (e.g. from a paused job) wins over a fresh preview
            seeded.append(from_existing or from_preview or None)
        merged_intermediates["scene_images"] = seeded
        logger.info(
            "[custom] Seeded scene_images from approved storyboard previews: "
            "%d/%d slots populated",
            sum(1 for u in seeded if u), len(seeded),
        )

    # ---- 4.6 Per-scene rerolled video URLs → cached scene_videos ----
    # When the user rerolls an individual scene's video in the Studio (e.g. via
    # the per-scene re-animate endpoint), the new video URL is stashed on the
    # first clip of the scene as `_reroll_video_url`. On commit-custom, we must
    # NOT regenerate from scratch — feed those URLs into ugc.py's existing
    # `intermediates["scene_videos"]` cache slot (list-shaped, mirrors the
    # scene_images pattern above) so the smart-mode loop skips animation for
    # those slots. The cache-hit path in ugc.py requires a list, not a dict.
    reroll_urls = []
    has_any_reroll = False
    for scene in sb_scenes:
        clips = scene.get("clips") or []
        url = None
        if clips and isinstance(clips[0], dict):
            url = clips[0].get("_reroll_video_url")
        if url:
            has_any_reroll = True
        reroll_urls.append(url)
    if has_any_reroll:
        existing_vids = merged_intermediates.get("scene_videos") or []
        seeded_vids = []
        for i in range(len(sb_scenes)):
            from_existing = existing_vids[i] if i < len(existing_vids) else None
            from_reroll = reroll_urls[i]
            # Existing intermediate (e.g. from a paused job) wins over a fresh reroll
            seeded_vids.append(from_existing or from_reroll or None)
        merged_intermediates["scene_videos"] = seeded_vids
        logger.info(
            "[custom] Seeded scene_videos from per-scene rerolls: "
            "%d/%d slots populated",
            sum(1 for u in seeded_vids if u), len(seeded_vids),
        )

    # ---- 4.7 Manim codegen for framework_render clips (best-effort, no render) ----
    # For each scene whose first clip is a Manim framework_render, ask the LLM to
    # generate the Manim Python source NOW. The code is stashed on the clip as
    # `_manim_code` (or `_manim_error` if codegen failed). We do NOT invoke the
    # Manim CLI here — the container may not have it installed, and ugc.py still
    # renders a Ken Burns placeholder for these clips. A future executor (when
    # the Manim binary is in the image) can read `_manim_code` off the clip and
    # call render_math_scene() to substitute the real animation.
    #
    # Cross-package import note: api_pipeline imports from Comp_Videos via the
    # wrapper, but Comp_Videos must NOT depend on api_pipeline at module-import
    # time. We therefore lazy-import inside the try/except so the monolith stays
    # usable in pure Google-Sheets mode (where api_pipeline is not on sys.path).
    for i, scene in enumerate(sb_scenes):
        clips = scene.get("clips") or []
        if not clips or not isinstance(clips[0], dict):
            continue
        clip0 = clips[0]
        if clip0.get("type") != "framework_render":
            continue
        if (clip0.get("framework") or "").lower() != "manim":
            continue
        prompt_text = clip0.get("first_prompt") or ""
        if not prompt_text.strip():
            continue
        ok = False
        try:
            from api_pipeline.manim_service import generate_manim_code  # lazy
            duration_target = float(
                clip0.get("duration_seconds")
                or scene.get("duration_seconds")
                or 5.0
            )
            result = generate_manim_code(prompt_text, duration_target=duration_target) or {}
            if "error" in result:
                clip0["_manim_error"] = str(result.get("error"))
                # Keep partial code if the LLM produced something usable for debugging
                if result.get("code"):
                    clip0["_manim_code"] = result["code"]
            else:
                clip0["_manim_code"] = result.get("code", "")
                ok = bool(clip0.get("_manim_code"))
        except ImportError:
            # api_pipeline not on sys.path (Sheets-only mode) — silently skip.
            continue
        except Exception as exc:  # best-effort; never block the pipeline
            clip0["_manim_error"] = f"codegen exception: {exc}"
        logger.info("[manim] codegen for scene %d: %s", i, "ok" if ok else "failed")

    # ---- 4.5. (Reserved) Side-channel pre-execution disabled until ugc.py
    # gains a per-scene cache hook. Today ugc.py's smart_mode loop overwrites
    # scene_videos[scene_idx] when it runs the beat-clip generator, so a
    # Composer-pre-populated URL gets discarded. The fix is a tiny per-scene
    # cache check inside generate_scene_visual, which is out of scope for D2.
    #
    # For now, per-clip Seedance routing works only when the GLOBAL video_model
    # is set to "seedance-2" (via API animation_model="seedance" or storyboard's
    # tier override). The Composer's compose() above still annotates each clip
    # with _resolved_tool / _resolved_refs / _motion_with_camera so the UI and
    # cost estimator see the right per-clip plan even if ugc.py executes a
    # single-model path. Full per-clip routing is on the D5/D7 roadmap.

    # ---- 5. Allow caller overrides for resolved model/provider/resolution ----
    # These are normalized by the API's input_translator from output_resolution etc.
    for k, v in extra_ugc_kwargs.items():
        if v is not None:
            ugc_kwargs[k] = v

    if on_progress:
        on_progress("step_complete", {
            "step": "custom_setup",
            "label": "Storyboard ready",
            "progress": 8,
            "message": f"Handing off to executor ({len(merged_intermediates.get('scene_prompts') or [])} pre-built scenes)",
        })

    # ---- 6. Hand off to process_ugc_video with everything pre-loaded ----
    return process_ugc_video(
        processor,
        on_progress=on_progress,
        existing_intermediates=merged_intermediates,
        simulation=simulation,
        row_num=row_num,
        **ugc_kwargs,
    )
