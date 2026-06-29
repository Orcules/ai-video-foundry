"""Simulated pipeline runner — runs the full step flow with mock services.

Short-circuits the monolith bridge so simulation mode works without
the tvd_pipeline package installed. Uses SimServiceRegistry mock services
to produce placeholder outputs while emitting the same SSE events the
dashboard expects.

Supports checkpoint/resume: on resume or restart-from-step, reads
existing intermediates from Supabase and skips steps whose outputs
are already cached.
"""

import logging
import os
import time
import uuid
from typing import Dict, Any

from api_pipeline.cost_tracker import CostTracker
from api_pipeline.defaults_config import get_default
from api_pipeline.event_store import event_store
from api_pipeline.pipelines.base import (
    _step_log, _check_abort, _cleanup_cost_tracking, _seed_cost_tracking,
    get_steps_for_type, validate_pipeline_output,
    save_pipeline_log, StepTimer,
    JobAbortedError, JobPausedError,
    PRODUCT_STEPS,
)

logger = logging.getLogger(__name__)

# Real asset URLs from config (fallback to old placeholders)
from api_pipeline.services.simulation import (
    _ref_image, _ref_video, _ref_product, _ref_vo, _ref_music, _ref_final,
    _SIM_IMAGE, _SIM_VIDEO, _SIM_AUDIO,
)


def _run_ugc_real_simulation(job_id: str, params: Dict[str, Any], services, supabase) -> Dict[str, Any]:
    """Dedicated simulation flow for ugc-real pipeline."""
    t_start = time.time()
    cost_tracker = CostTracker()
    timer = StepTimer(job_id, supabase)
    steps = get_steps_for_type("ugc-real")
    duration = int(params.get("duration") or 30)
    offer_type = (params.get("offer_type") or "service").strip().lower() or "service"
    prompt = params.get("prompt", "Simulated UGC Real prompt")
    language = params.get("language", get_default("language", "en"))

    intermediates: Dict[str, Any] = {}
    _step_log(job_id, "WRAPPER", "Starting ugc-real pipeline (SIMULATION)", progress=0, event_type="start")

    def _progress(step_idx: int) -> int:
        return min(95, int((step_idx + 1) / max(len(steps), 1) * 95))

    def _emit(step_idx: int, step_id: str, label: str, msg: str):
        timer.start(step_id, label)
        _check_abort(supabase, job_id)
        _step_log(job_id, step_id, msg, progress=_progress(step_idx), event_type="info", cost_usd=cost_tracker.total_usd)
        try:
            supabase.update_progress(job_id, _progress(step_idx), step_id)
        except Exception:
            pass

    def _end(detail: str = "simulated"):
        timer.end(detail)
        try:
            supabase.update_progress(job_id, progress=-1, current_step="", intermediates=intermediates)
        except Exception:
            pass

    # step_parse + step_0 offer analysis
    _emit(0, steps[0][0], steps[0][1], "Parsing UGC Real brief...")
    intermediates["parsed_texts"] = {
        "text_1": "Simulated target audience",
        "text_2": "Simulated main problem",
        "text_3": "Simulated benefits\n\nCTA: Try it today",
    }
    intermediates["ugc_real_intake"] = {
        "offer_type": offer_type,
        "offer_category": "simulation",
        "target_audience": intermediates["parsed_texts"]["text_1"],
        "main_problem": intermediates["parsed_texts"]["text_2"],
        "key_benefits": "Simulated benefits",
        "cta_text": "Try it today",
        "ad_format": "talking_head",
    }
    cost_tracker.record_gemini_text("gemini-2.5-flash", 900, 220, "ugc_real_parse_prompt")
    _end()

    _emit(1, steps[1][0], steps[1][1], "Analyzing offer...")
    intermediates["offer_profile"] = {
        "offer_type": offer_type,
        "visual_requirements": ["ugc_creator_presence", "native_platform_style"],
        "recommended_ad_patterns": ["hook_first", "problem_solution"],
    }
    cost_tracker.record_gemini_text("gemini-2.5-flash", 1200, 300, "ugc_real_offer_analysis")
    _end()

    # step_0.5 strategy
    _emit(2, steps[2][0], steps[2][1], "Building creative strategy...")
    intermediates["creative_strategy"] = {
        "creative_angle": "pain_point",
        "hook_style": "confession_hook",
        "narrative_mode": "ugc_story",
        "cta_strategy": "direct",
    }
    cost_tracker.record_gemini_text("gemini-2.5-flash", 1000, 250, "ugc_real_creative_strategy")
    _end()

    # step_1 nine-cell plan
    _emit(3, steps[3][0], steps[3][1], "Planning 9-cell storyboard...")
    roles = [
        "character_talking",
        "product_only",
        "character_with_product",
        "service_ui",
        "character_talking",
        "product_only",
        "b_roll",
        "character_with_product",
        "cta",
    ]
    avg_dur = round(max(2.0, float(duration) / 9.0), 2)
    nine_cells = []
    for i in range(9):
        lip = i in (0, 4, 8)
        nine_cells.append({
            "cell_index": i + 1,
            "visual_prompt": f"Simulated cell {i+1}",
            "voice_line": f"Cell {i+1} voice line",
            "lipsync": lip,
            "shot_role": roles[i],
            "duration_seconds": avg_dur,
        })
    intermediates["nine_cell_plan"] = {"cells": nine_cells}
    cost_tracker.record_gemini_text("gemini-2.5-flash", 1400, 350, "ugc_real_nine_cell_plan")
    _end()

    # step_2 style DNA
    _emit(4, steps[4][0], steps[4][1], "Extracting visual DNA...")
    intermediates["style_dna"] = {
        "color_palette": "warm natural tones",
        "lighting": "soft studio high-key",
        "camera_lens": "85mm f/1.8",
        "character_details": "natural UGC creator",
        "background": "clean light grey",
    }
    cost_tracker.record_gemini_text("gemini-2.5-flash", 800, 200, "ugc_real_style_dna")
    _end()

    # step_3 grid generation
    _emit(5, steps[5][0], steps[5][1], "Generating 3x3 grid image...")
    grid_image_url = _ref_image(0, "influencer")
    intermediates["master_grid_prompt"] = "Simulated 3x3 grid prompt"
    intermediates["grid_image_url"] = grid_image_url
    cost_tracker.record_nano_banana(1, model="nano-banana-pro")
    cost_tracker.record_gemini_text("gemini-2.5-flash", 1000, 300, "ugc_real_master_grid")
    _end("simulated — 1 grid")

    # step_4 grid cutting
    _emit(6, steps[6][0], steps[6][1], "Cutting grid into 9 cells...")
    grid_cells = []
    for c in range(9):
        grid_cells.append({
            "cell_index": c + 1,
            "image_url": _ref_image(c % 10, "influencer"),
            "visual_prompt": nine_cells[c]["visual_prompt"],
            "voice_line": nine_cells[c]["voice_line"],
            "lipsync": nine_cells[c]["lipsync"],
            "shot_role": nine_cells[c]["shot_role"],
        })
    intermediates["grid_cells"] = grid_cells
    _end()

    # step_5 frame routing
    _emit(7, steps[7][0], steps[7][1], "Routing cells to Avatar vs I2V...")
    frame_routing = []
    for c in nine_cells:
        frame_routing.append({
            "cell_index": c["cell_index"],
            "route": "kling_avatar" if c["lipsync"] else "i2v_animation",
            "shot_role": c["shot_role"],
            "voice_line": c["voice_line"],
            "lipsync": c["lipsync"],
        })
    intermediates["frame_routing"] = frame_routing
    _end()

    # step_6 VO
    _emit(8, steps[8][0], steps[8][1], "Generating voiceover...")
    vo_script = " ||| ".join([c["voice_line"] for c in nine_cells]) or "Simulated script"
    intermediates["vo_script"] = vo_script
    intermediates["vo_audio_url"] = _ref_vo("influencer")
    intermediates["vo_duration"] = duration
    intermediates["vo_word_segments"] = []
    intermediates["cell_vo_audio"] = {str(c["cell_index"]): _ref_vo("influencer") for c in nine_cells if c["lipsync"]}
    cost_tracker.record_elevenlabs(len(vo_script))
    _end()

    # step_7 lip sync (Kling avatar pro simulated)
    _emit(9, steps[9][0], steps[9][1], "Generating lip-sync clips...")
    lip_sync_videos = {}
    for c in nine_cells:
        if c["lipsync"]:
            lip_sync_videos[str(c["cell_index"])] = _ref_video(c["cell_index"] - 1, "influencer")
    intermediates["lip_sync_videos"] = lip_sync_videos
    if lip_sync_videos:
        cost_tracker.record_usage({
            "model": "kling/ai-avatar-pro",
            "provider": "kie",
            "category": "videos",
            "count": len(lip_sync_videos),
            "label": "avatar lip-sync clips",
        })
    _end()

    # step_8 animation
    _emit(10, steps[10][0], steps[10][1], "Generating animation clips...")
    scene_videos = [_ref_video(i, "influencer") for i in range(9)]
    intermediates["scene_videos"] = scene_videos
    non_lip = sum(1 for c in nine_cells if not c["lipsync"])
    cost_tracker.record_veo3(non_lip * avg_dur, model="veo-3.1-fast")
    _end()

    # step_9 assembly
    _emit(11, steps[11][0], steps[11][1], "Assembling final video...")
    concat_url = services.rendi.concatenate_videos(video_data=scene_videos)
    final_audio_mix = services.rendi.add_vo_and_music_to_video(
        video_url=concat_url,
        vo_url=intermediates["vo_audio_url"],
        music_url=_ref_music("influencer"),
    )
    intermediates["concat_url"] = concat_url
    intermediates["rendi_scene_voice_url"] = final_audio_mix
    cost_tracker.record_rendi("concat")
    cost_tracker.record_rendi("add_vo_music")
    _end()

    # step_10 subtitles
    _emit(12, steps[12][0], steps[12][1], "Adding subtitles...")
    subtitled_url = services.zapcap.add_subtitles(intermediates["rendi_scene_voice_url"], language=language)
    intermediates["subtitled_url"] = subtitled_url
    cost_tracker.record_zapcap(duration)
    _end()

    output = dict(intermediates)
    output["final_video_url"] = subtitled_url
    output["final_mp4_url"] = subtitled_url
    mux_result = services.mux.upload_video_async(subtitled_url, job_id)
    output["mux_upload_id"] = mux_result.get("upload_id")
    output["mux_status"] = "ready"
    cost_summary = cost_tracker.build_summary()
    output["cost_usd"] = cost_summary["total_usd"]
    output["cost_breakdown"] = cost_summary
    timer.persist()
    _step_log(
        job_id,
        "WRAPPER",
        f"UGC Real simulation complete in {time.time() - t_start:.1f}s — cost ${cost_summary['total_usd']:.4f}",
        progress=98,
        event_type="info",
        cost_usd=cost_summary["total_usd"],
    )
    return output


def _save_sim_cost_checkpoint(job_id: str, cost_tracker, supabase) -> None:
    """Persist cost tracker state so resumed runs pick up accumulated cost."""
    try:
        checkpoint = cost_tracker.to_checkpoint()
        if checkpoint.get("entries"):
            supabase.update_progress(job_id, progress=-1, current_step="",
                intermediates={"cost_checkpoint": checkpoint})
    except Exception as e:
        logger.warning(f"[{job_id}] SIM: Failed to save cost checkpoint: {e}")


def run_simulated_pipeline(
    job_id: str,
    video_type: str,
    params: Dict[str, Any],
    services,
    supabase,
) -> Dict[str, Any]:
    """Run a simulated pipeline using mock services.

    Walks through the same steps as the real pipeline, calling mock services
    from SimServiceRegistry, emitting SSE events with correct progress
    percentages, and producing the same output dict shape.

    Supports checkpoint/resume: reads existing intermediates from Supabase
    and skips steps whose outputs are already cached.

    Args:
        job_id: Supabase job ID.
        video_type: API video type string.
        params: Raw API request params dict.
        services: SimServiceRegistry instance.
        supabase: SupabaseJobClient instance.

    Returns:
        Output dict with simulated final video URLs, cost, etc.
    """
    t_start = time.time()
    cost_tracker = CostTracker()
    timer = StepTimer(job_id, supabase)

    # Restore checkpoint state for resume/restart
    existing_intermediates = {}
    try:
        job = supabase.get_job(job_id)
        if job:
            existing_intermediates = job.get("intermediates", {}) or {}
            cost_checkpoint = existing_intermediates.get("cost_checkpoint")
            if cost_checkpoint:
                cost_tracker = CostTracker.from_checkpoint(cost_checkpoint)
                logger.info(f"[{job_id}] SIM: Restored cost tracker from checkpoint: ${cost_tracker.total_usd:.4f}")
    except Exception as e:
        logger.warning(f"[{job_id}] SIM: Failed to restore checkpoint: {e}")

    # Seed cost delta tracker so checkpoint cost doesn't appear as phantom delta
    _seed_cost_tracking(job_id, cost_tracker.total_usd)

    vt = video_type.lower()
    if vt == "ugc-real":
        return _run_ugc_real_simulation(job_id, params, services, supabase)

    is_product = vt == "product video"
    is_personal_brand = vt == "personal-brand"
    steps = get_steps_for_type(video_type)

    target_duration = params.get("duration", get_default("duration", 20))
    prompt = params.get("prompt", "Simulated product showcase")
    language = params.get("language", get_default("language", "en"))
    _image_api = params.get("image_api", get_default("image_api", "kie"))

    _step_log(job_id, "WRAPPER", f"Starting {video_type} pipeline (SIMULATION)", progress=0, event_type="start")

    # Start from existing intermediates (minus metadata keys)
    intermediates = {k: v for k, v in existing_intermediates.items()
                     if k not in ("cost_checkpoint", "cost_usd")}
    total_steps = len(steps)

    def _has_cached(*keys) -> bool:
        """Check if ALL keys are present and non-None in existing intermediates."""
        return all(k in existing_intermediates and existing_intermediates[k] is not None
                   for k in keys)

    def _progress(step_idx):
        """Calculate progress percentage for step index."""
        return min(95, int((step_idx + 1) / total_steps * 95))

    def _emit_step(step_idx, step_id, label, message, detail=None):
        """Emit a step_complete event and update Supabase."""
        prog = _progress(step_idx)
        timer.start(step_id, label)
        _check_abort(supabase, job_id)
        _step_log(job_id, step_id, message, progress=prog, event_type="info",
                  cost_usd=cost_tracker.total_usd)
        try:
            supabase.update_progress(job_id, prog, step_id)
        except Exception:
            pass

    def _emit_skip(step_idx, step_id, label):
        """Emit a skip/restored event for a cached step."""
        prog = _progress(step_idx)
        timer.skip(step_id, label, detail="restored from checkpoint")
        _check_abort(supabase, job_id)
        _step_log(job_id, step_id, f"{label} (restored from checkpoint)",
                  progress=prog, event_type="info", cost_usd=cost_tracker.total_usd)
        try:
            supabase.update_progress(job_id, prog, step_id)
        except Exception:
            pass

    def _emit_usage(label: str):
        """Emit a cost_update SSE event after recording cost."""
        total = cost_tracker.total_usd
        _step_log(job_id, "COST", f"Cost: ${total:.4f} ({label})",
                  progress=-1, event_type="info", cost_usd=round(total, 4))
        try:
            supabase.update_cost(job_id, round(total, 4))
        except Exception:
            pass

    def _end_step(detail=None):
        timer.end(detail)
        # Persist current intermediates to Supabase so resume can find them
        _persist_intermediates()

    def _persist_intermediates():
        """Save current intermediates to Supabase for checkpoint/resume."""
        try:
            supabase.update_progress(job_id, progress=-1, current_step="",
                                     intermediates=intermediates)
        except Exception as e:
            logger.warning(f"[{job_id}] SIM: Failed to persist intermediates: {e}")

    # Local variables set by steps and referenced by downstream steps.
    # Seed from cached intermediates so skipped steps still provide values.
    char_desc = intermediates.get("character_description", "")
    parsed = intermediates.get("parsed_texts", {})
    vo_script = intermediates.get("vo_script", "")
    scene_prompts = intermediates.get("scene_prompts", {})
    scene_images = intermediates.get("scene_images", [])
    scene_videos = intermediates.get("scene_videos", [])
    music_url = intermediates.get("music_url", "")
    concat_url = intermediates.get("concat_url", "")
    rendi_scene_voice_url = intermediates.get("rendi_scene_voice_url", "")
    subtitled_url = intermediates.get("subtitled_url", "")

    try:
        # ── Step 0: Describe Character ──
        step_idx = 0
        step_id, label, keys = steps[step_idx]
        if _has_cached(*keys):
            _emit_skip(step_idx, step_id, label)
            char_desc = intermediates.get("character_description", "")
        else:
            _emit_step(step_idx, step_id, label, f"Describing character...")
            char_desc = services.gemini.describe_character(_ref_image(0, video_type))
            cost_tracker.record_gemini_text("gemini-2.5-flash", 1500, 400, "describe_character")
            _emit_usage("describe_character")
            if is_personal_brand:
                intermediates["character_descriptions"] = [char_desc]
            elif not is_product:
                intermediates["influencer_image"] = _ref_image(0, video_type)
                intermediates["character_description"] = char_desc
            else:
                intermediates["character_description"] = char_desc
            _end_step("simulated")
        _check_abort(supabase, job_id)

        # ── Step 0.5: Analyze Media (influencer/personal-brand only, before parse prompt) ──
        if not is_product:
            # Find the analyze step (step_0.5) in the step list
            for _i, (_sid, _slabel, _skeys) in enumerate(steps):
                if _sid == "step_0.5":
                    step_idx = _i
                    break
            step_id, label, keys = steps[step_idx]
            if _has_cached(*keys):
                _emit_skip(step_idx, step_id, label)
            else:
                _emit_step(step_idx, step_id, label, f"{label}...")
                intermediates["ref_image_analyses"] = ["[Sim] Reference image analysis"]
                cost_tracker.record_gemini_text("gemini-2.5-flash", 1500, 400, "ref_image_analysis")
                # Simulate asset analysis cost (smart mode only)
                asset_urls = params.get("asset_urls") or []
                if params.get("asset_mode", "smart") == "smart" and asset_urls:
                    for i in range(len(asset_urls)):
                        cost_tracker.record_gemini_text("gemini-2.5-flash", 3000, 700, f"analyze_asset_video_{i+1}")
                    intermediates["asset_analyses"] = [f"[Sim] Asset video {i+1} analysis" for i in range(len(asset_urls))]
                _emit_usage("ref_image_analysis")
                _end_step("simulated")
            _check_abort(supabase, job_id)

        # ── Step 1: Parse Prompt ──
        for _i, (_sid, _slabel, _skeys) in enumerate(steps):
            if _sid == "step_1":
                step_idx = _i
                break
        step_id, label, keys = steps[step_idx]
        if _has_cached(*keys):
            _emit_skip(step_idx, step_id, label)
            parsed = intermediates.get("parsed_texts", {})
        else:
            _emit_step(step_idx, step_id, label, "Parsing prompt...")
            parsed = services.gemini.parse_product_prompt(prompt)
            cost_tracker.record_gemini_text("gemini-2.5-flash", 2000, 800, "parse_prompt")
            _emit_usage("parse_prompt")
            intermediates["parsed_texts"] = parsed
            _end_step("simulated")
        _check_abort(supabase, job_id)

        # ── Step 2: Clean Product Image (product only) ──
        if is_product:
            step_idx = 2
            step_id, label, keys = steps[step_idx]
            if _has_cached(*keys):
                _emit_skip(step_idx, step_id, label)
            else:
                _emit_step(step_idx, step_id, label, f"{label}...")
                intermediates["clean_product_image"] = _ref_product(video_type)
                if _image_api == "google":
                    cost_tracker.record_gemini_image("gemini-3-pro-image", 1)
                elif _image_api == "google-31-flash":
                    cost_tracker.record_gemini_image("gemini-3.1-flash-image-preview", 1)
                elif _image_api == "kie-flash":
                    cost_tracker.record_usage({
                        "model": "gemini-3-flash", "provider": "kie",
                        "category": "images", "count": 1, "label": "1 image(s)",
                    })
                else:
                    cost_tracker.record_nano_banana(1, model="nano-banana-pro")
                _emit_usage("clean_product_image")
                _end_step("simulated")
            _check_abort(supabase, job_id)

        # ── Step 2.5/2.7: Reference Video / Voiceover ──
        step_idx = 3
        step_id, label, keys = steps[step_idx]
        if _has_cached(*keys):
            _emit_skip(step_idx, step_id, label)
            vo_script = intermediates.get("vo_script", "")
        else:
            _emit_step(step_idx, step_id, label, f"{label}...")
            if step_id == "step_2.5":
                # Product: reference video
                intermediates["reference_video_structure"] = None
            else:
                # Personal-brand or influencer VO step
                vo_script = services.gemini.generate_influencer_vo_script(
                    text_1="[sim]", text_2="[sim]", text_3="[sim]",
                    free_text=prompt, target_duration=target_duration, language=language)
                cost_tracker.record_gemini_text("gemini-2.5-flash", 2000, 600, "vo_script")
                audio_bytes, segments = services.elevenlabs.text_to_speech_with_timestamps(vo_script)
                cost_tracker.record_elevenlabs(len(vo_script))
                _emit_usage("vo+tts")
                intermediates["vo_script"] = vo_script
                intermediates["vo_audio_url"] = _ref_vo(video_type)
                intermediates["vo_word_segments"] = segments
                intermediates["vo_duration"] = target_duration
            _end_step("simulated")
        _check_abort(supabase, job_id)

        # ── Step 3: Scene Prompts ──
        step_idx = 4 if len(steps) > 5 else 3
        for i, (sid, slabel, skeys) in enumerate(steps):
            if sid == "step_3":
                step_idx = i
                break
        step_id, label, keys = steps[step_idx]
        if _has_cached(*keys):
            _emit_skip(step_idx, step_id, label)
            scene_prompts = intermediates.get("scene_prompts", {})
        else:
            _emit_step(step_idx, step_id, label, "Generating scene prompts...")
            scene_count = max(3, min(8, int(target_duration / 4)))
            if hasattr(services, 'set_scene_count'):
                services.set_scene_count(scene_count)
            scene_prompts = services.gemini.generate_product_video_scenes(
                text_1=parsed.get("text_1", ""), text_2=parsed.get("text_2", ""),
                text_3=parsed.get("text_3", ""), text_4=parsed.get("text_4", ""),
                prompt=prompt, image_urls=[], target_duration=target_duration,
                character_description=char_desc, language=language,
            )
            cost_tracker.record_gemini_text("gemini-2.5-flash", 3000, 1500, "scene_prompts")
            _emit_usage("scene_prompts")
            intermediates["scene_prompts"] = scene_prompts
            intermediates["music_style"] = scene_prompts.get("music_style", "upbeat modern")
            _end_step("simulated")
        _check_abort(supabase, job_id)

        # If product pipeline has a VO step after scenes, handle it
        if is_product:
            for i, (sid, slabel, skeys) in enumerate(steps):
                if sid == "step_2.7":
                    if "vo_script" not in intermediates:
                        _emit_step(i, sid, slabel, "Generating voiceover script + TTS...")
                        vo_script = services.gemini.generate_influencer_vo_script(
                            text_1="[sim]", text_2="[sim]", text_3="[sim]",
                            free_text=prompt, target_duration=target_duration, language=language)
                        cost_tracker.record_openai("gpt-4o", 2000, 600, "vo_script")
                        cost_tracker.record_elevenlabs(len(vo_script))
                        _emit_usage("vo+tts")
                        intermediates["vo_script"] = vo_script
                        intermediates["vo_audio_url"] = _ref_vo(video_type)
                        intermediates["vo_duration"] = target_duration
                        _end_step("simulated")
                    break

        # ── Steps 4-7: Generate Assets (per-scene images, videos, music) ──
        for i, (sid, slabel, skeys) in enumerate(steps):
            if sid == "steps_4_7":
                step_idx = i
                break
        step_id, label, keys = steps[step_idx]

        if _has_cached("scene_images", "scene_videos", "music_url"):
            _emit_skip(step_idx, step_id, label)
            scene_images = intermediates.get("scene_images", [])
            scene_videos = intermediates.get("scene_videos", [])
            music_url = intermediates.get("music_url", "")
        else:
            timer.start(step_id, label)

            scenes = scene_prompts.get("scenes", [])
            scene_images = []
            scene_videos = []
            _num_scenes = max(len(scenes), 1)

            # Per-scene image generation with individual events
            for idx, s in enumerate(scenes):
                _check_abort(supabase, job_id)
                img = services.gemini_image.generate_image(s.get("image_prompt", "placeholder"), scene_idx=idx)
                scene_images.append(img)
                if _image_api == "google":
                    cost_tracker.record_gemini_image("gemini-3-pro-image", 1)
                elif _image_api == "google-31-flash":
                    cost_tracker.record_gemini_image("gemini-3.1-flash-image-preview", 1)
                elif _image_api == "kie-flash":
                    cost_tracker.record_usage({
                        "model": "gemini-3-flash", "provider": "kie",
                        "category": "images", "count": 1, "label": "1 image(s)",
                    })
                else:
                    cost_tracker.record_nano_banana(1)
                _img_progress = 30 + int(10 * (idx + 1) / _num_scenes)
                _step_log(job_id, f"scene_{idx+1}_image", f"Scene {idx+1} image generated",
                          progress=_img_progress, asset_url=img, asset_type="image",
                          cost_usd=cost_tracker.total_usd)

            # Per-scene video generation with individual events
            _video_model = params.get("video_model", "veo-3.1-fast") or "veo-3.1-fast"
            for idx, img in enumerate(scene_images):
                _check_abort(supabase, job_id)
                vid = services.veo3.generate_video_from_image(image_url=img, motion_prompt="zoom in", scene_idx=idx)
                scene_videos.append(vid)
                if "kling" in _video_model:
                    cost_tracker.record_kling(5.0, model=_video_model)
                elif "runway" in _video_model:
                    cost_tracker.record_runway(5.0, model=_video_model)
                else:
                    cost_tracker.record_veo3(5.0, model=_video_model)
                _vid_progress = 40 + int(25 * (idx + 1) / _num_scenes)
                _step_log(job_id, f"scene_{idx+1}_video", f"Scene {idx+1} video generated",
                          progress=_vid_progress, asset_url=vid, asset_type="video",
                          cost_usd=cost_tracker.total_usd)

            # Music generation with asset URL
            _check_abort(supabase, job_id)
            music_url = services.suno.generate_pure_music(style_description="upbeat modern")
            cost_tracker.record_suno()
            _step_log(job_id, "music", "Background music generated",
                      progress=68, asset_url=music_url, asset_type="audio",
                      cost_usd=cost_tracker.total_usd)

            intermediates["scene_images"] = scene_images
            intermediates["scene_images_all"] = scene_images
            intermediates["scene_videos"] = scene_videos
            intermediates["music_url"] = music_url
            _end_step(f"simulated — {len(scenes)} scenes")
        _check_abort(supabase, job_id)

        # ── Step 7.5: Beat-Sync Trim (only for pipelines that have this step) ──
        _has_7_5 = False
        for i, (sid, slabel, skeys) in enumerate(steps):
            if sid == "step_7.5":
                step_idx = i
                _has_7_5 = True
                break
        if _has_7_5:
            step_id, label, keys = steps[step_idx]
            if _has_cached(*keys):
                _emit_skip(step_idx, step_id, label)
            else:
                _emit_step(step_idx, step_id, label, "Trimming videos...")
                intermediates["trimmed_scene_videos"] = scene_videos
                cost_tracker.record_rendi("trim")
                _emit_usage("trim")
                _end_step("simulated")
            _check_abort(supabase, job_id)

        # ── Step 8: Concat + Audio ──
        for i, (sid, slabel, skeys) in enumerate(steps):
            if sid == "step_8":
                step_idx = i
                break
        step_id, label, keys = steps[step_idx]
        if _has_cached("concat_url", "rendi_scene_voice_url"):
            _emit_skip(step_idx, step_id, label)
            concat_url = intermediates.get("concat_url", "")
            rendi_scene_voice_url = intermediates.get("rendi_scene_voice_url", "")
        else:
            _emit_step(step_idx, step_id, label, "Concatenating video and adding audio...")
            concat_url = services.rendi.concatenate_videos(video_data=scene_videos)
            cost_tracker.record_rendi("concat")
            rendi_scene_voice_url = services.rendi.add_vo_and_music_to_video(
                video_url=concat_url, vo_url=_ref_vo(video_type), music_url=music_url)
            cost_tracker.record_rendi("add_vo_music")
            _emit_usage("concat+audio")
            intermediates["concat_url"] = concat_url
            intermediates["rendi_scene_voice_url"] = rendi_scene_voice_url
            _end_step("simulated")
        _check_abort(supabase, job_id)

        # ── Film grain (if enabled) ──
        film_grain = params.get("film_grain")
        if film_grain is None:
            vt_lower = params.get("video_type", "").lower()
            film_grain = vt_lower in ("influencer", "personal-brand")
        if film_grain:
            cost_tracker.record_rendi("film_grain")
            _emit_usage("film_grain")

        # ── Step 9: Subtitles ──
        for i, (sid, slabel, skeys) in enumerate(steps):
            if sid == "step_9":
                step_idx = i
                break
        step_id, label, keys = steps[step_idx]
        if _has_cached(*keys):
            _emit_skip(step_idx, step_id, label)
            subtitled_url = intermediates.get("subtitled_url", "")
        else:
            _emit_step(step_idx, step_id, label, "Adding subtitles...")
            subtitled_url = services.zapcap.add_subtitles(rendi_scene_voice_url, language=language)
            cost_tracker.record_zapcap(target_duration)
            _emit_usage("subtitles")
            intermediates["subtitled_url"] = subtitled_url
            _end_step("simulated")

    except (JobPausedError, JobAbortedError):
        _save_sim_cost_checkpoint(job_id, cost_tracker, supabase)
        raise

    # Save cost checkpoint on successful completion
    _save_sim_cost_checkpoint(job_id, cost_tracker, supabase)

    # ── Finalize ──
    final_video_url = subtitled_url or rendi_scene_voice_url or concat_url or _ref_final(video_type)

    # Build output dict
    output = dict(intermediates)
    output["final_video_url"] = final_video_url
    output["final_mp4_url"] = final_video_url

    # Mux upload (simulated)
    mux_result = services.mux.upload_video_async(final_video_url, job_id)
    output["mux_upload_id"] = mux_result.get("upload_id")
    output["mux_status"] = "ready"
    playback_id = mux_result.get("playback_id", f"sim_{uuid.uuid4().hex[:8]}")
    output["final_playback_id"] = playback_id
    output["final_stream_url"] = f"https://stream.mux.com/{playback_id}.m3u8"

    # Use real Mux playback ID from env if available (for realistic dashboard preview)
    sim_playback_id = os.environ.get("SIM_MUX_PLAYBACK_ID")
    if sim_playback_id:
        output["final_playback_id"] = sim_playback_id
        output["final_stream_url"] = f"https://stream.mux.com/{sim_playback_id}.m3u8"
        output["final_mp4_url"] = f"https://stream.mux.com/{sim_playback_id}/highest.mp4"

    # Cost summary
    cost_summary = cost_tracker.build_summary()
    output["cost_usd"] = cost_summary["total_usd"]
    output["cost_breakdown"] = cost_summary

    # Store usage
    try:
        supabase.store_usage(job_id, cost_summary)
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to store sim usage: {e}")

    # Validate output
    issues = validate_pipeline_output(job_id, video_type, output, params, intermediates)
    if issues:
        output["issues"] = issues

    # Save pipeline log
    try:
        save_pipeline_log(job_id, video_type, params, intermediates, output, timer)
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to save sim pipeline log: {e}")

    # Persist step timings
    timer.persist()

    elapsed = time.time() - t_start
    _step_log(job_id, "WRAPPER", f"Simulation complete in {elapsed:.1f}s — cost ${cost_summary['total_usd']:.4f}",
              progress=98, event_type="info", cost_usd=cost_summary["total_usd"])

    return output
