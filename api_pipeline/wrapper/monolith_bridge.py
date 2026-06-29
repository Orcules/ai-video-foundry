"""Bridge between the API server and the monolith VideoSceneProcessor.

This module is the main entry point for running pipelines through the monolith.
It handles: param translation, progress callback setup, monolith invocation,
Mux upload, Supabase finalization, and cost storage.

ZERO pipeline logic lives here — it is purely orchestration.
"""

import logging
import time
from typing import Dict, Any

from api_pipeline.wrapper.input_translator import translate_params
from api_pipeline.wrapper.progress_callback import create_progress_callback
from api_pipeline.wrapper.gcs_intermediates import upload_intermediates_to_gcs
from api_pipeline.event_store import event_store
from api_pipeline.cost_tracker import CostTracker
from api_pipeline.pipelines.base import (
    JobAbortedError, JobPausedError, _step_log,
    StepTimer, save_pipeline_log, validate_pipeline_output,
    JOB_ARTIFACTS_DIR,
)

logger = logging.getLogger(__name__)

# Monolith pipelines use slightly different keys; try all sensible fallbacks before failing the job.
_FINAL_VIDEO_URL_KEYS = (
    "final_video_url",
    "subtitled_video_url",
    "subtitled_url",
    "video_before_subtitles_url",
    "rendi_scene_voice_url",
    "concat_url",
)


def _coerce_http_url(val) -> str:
    if not val or not isinstance(val, str):
        return ""
    v = val.strip()
    return v if len(v) > 10 and v.lower().startswith("http") else ""


def _pick_final_video_url(monolith_result: Dict[str, Any]) -> str:
    for key in _FINAL_VIDEO_URL_KEYS:
        url = _coerce_http_url(monolith_result.get(key))
        if url:
            return url
    return ""


def _format_missing_final_video_error(job_id: str, monolith_result: Dict[str, Any]) -> str:
    """Human-readable failure when the monolith returned without a usable final URL."""
    errors = monolith_result.get("errors") or []
    err_text = "; ".join(str(e) for e in errors if e) if errors else ""
    sv = monolith_result.get("scene_videos") or []
    si = monolith_result.get("scene_images") or []
    n_vid = len([x for x in sv if _coerce_http_url(x)]) if isinstance(sv, list) else 0
    n_img = len([x for x in si if _coerce_http_url(x)]) if isinstance(si, list) else 0
    parts = [
        f"Monolith did not produce a final video URL for job {job_id}.",
    ]
    if err_text:
        parts.append(f"Monolith reported: {err_text}")
    else:
        parts.append("The monolith returned no errors[] detail — check server logs and job intermediates.")
    parts.append(f"Counts: scene_images_with_url={n_img}, scene_videos_with_url={n_vid}.")
    if n_img and not n_vid:
        parts.append("Images exist but no scene videos — animation step likely failed for every scene.")
    elif not n_img and not n_vid:
        parts.append("No scene images — image generation likely failed or prompts were empty.")
    return " ".join(parts)


def _save_cost_checkpoint(job_id: str, cost_tracker, supabase) -> None:
    """Persist the cost tracker state so resumed/restarted runs pick up accumulated cost."""
    try:
        checkpoint = cost_tracker.to_checkpoint()
        if checkpoint.get("entries"):
            supabase.update_progress(job_id, progress=-1, current_step="",
                intermediates={"cost_checkpoint": checkpoint})
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to save cost checkpoint: {e}")


def run_monolith_pipeline(
    job_id: str,
    video_type: str,
    params: Dict[str, Any],
    services,
    supabase,
) -> Dict[str, Any]:
    """Run a video pipeline via the monolith's VideoSceneProcessor.

    This is the main entry point called by _run_job() in server.py for
    all pipeline types (product video, influencer, personal-brand).

    Flow:
        1. Translate API params to monolith kwargs
        2. Create progress callback
        3. Import and instantiate the monolith processor
        4. Call the appropriate pipeline method
        5. Upload final video to Mux
        6. Store output + cost in Supabase
        7. Return output dict

    Args:
        job_id: Supabase job ID.
        video_type: API video type string ("product video", "influencer", "personal-brand").
        params: Raw API request params dict.
        services: ServiceRegistry instance (for Mux, GCS, etc.).
        supabase: SupabaseJobClient instance.

    Returns:
        Output dict with final video URLs, cost, etc.
    """
    t_start = time.time()
    cost_tracker = CostTracker()

    # Restore cost tracker from checkpoint if resuming
    intermediates = {}
    try:
        job = supabase.get_job(job_id)
        if job:
            intermediates = job.get("intermediates", {})
            cost_checkpoint = intermediates.get("cost_checkpoint")
            if cost_checkpoint:
                cost_tracker = CostTracker.from_checkpoint(cost_checkpoint)
                logger.info(f"[{job_id}] Restored cost tracker from checkpoint: ${cost_tracker.total_usd:.4f}")
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to restore cost checkpoint: {e}")

    # If the request provides non-empty text_1/text_2/text_3 (user-edited in Studio Step 6), inject
    # into parsed_texts so the monolith uses the latest texts — not Phase 1's copy from seed_job_id.
    # Use .strip() for the emptiness check: whitespace-only must NOT inject an "empty" parsed_texts
    # dict, or product.py would skip the parse_prompt LLM and never persist real TEXT 1–3.
    _t1 = (params.get("text_1") or "").strip()
    _t2 = (params.get("text_2") or "").strip()
    _t3 = (params.get("text_3") or "").strip()
    _t4 = (params.get("text_4") or "").strip()
    if _t1 or _t2 or _t3 or _t4:
        intermediates = dict(intermediates)
        intermediates["parsed_texts"] = {
            "text_1": _t1,
            "text_2": _t2,
            "text_3": _t3,
            "text_4": _t4,
        }

    # Step 1: Translate params
    translated = translate_params(video_type, params)
    logger.info(
        f"[{job_id}] Params translated — video_model={translated.get('video_model')}, "
        f"sync_method={translated.get('sync_method')}, resolution={translated.get('output_resolution')}, "
        f"target_duration={translated.get('target_duration')}"
    )

    # Wire checkpoint/resume intermediates to the monolith
    if intermediates:
        translated["existing_intermediates"] = intermediates

    # Step 2: Create progress callback
    # Pass simulation_duration only for simulation runs so the callback
    # can inject proportional pacing delays between monolith steps.
    sim_duration = (
        params.get("simulation_duration")
        if params.get("is_simulation")
        else None
    )
    callback = create_progress_callback(
        job_id=job_id,
        supabase=supabase,
        event_store=event_store,
        cost_tracker=cost_tracker,
        simulation_duration=sim_duration,
        is_simulation=bool(params.get("is_simulation")),
        video_type=video_type,
        pause_after_step=params.get("pause_after_step"),
    )

    # Step 3: Import monolith
    try:
        from tvd_pipeline.processor import VideoSceneProcessor
    except ImportError as e:
        raise RuntimeError(
            f"Cannot import monolith (tvd_pipeline.processor.VideoSceneProcessor). "
            f"The tvd_pipeline package is not yet available.\n"
            f"\n"
            f"To fix this:\n"
            f"  1. Docker: uncomment the tvd_pipeline volume mount in docker-compose.yml:\n"
            f"       - ../Comp_Videos/tvd_pipeline:/app/tvd_pipeline\n"
            f"  2. Local: set PYTHONPATH to the parent of tvd_pipeline/:\n"
            f"       export PYTHONPATH=/path/to/repo\n"
            f"  3. Simulation mode works without tvd_pipeline — use simulation=true.\n"
            f"\n"
            f"Original error: {e}"
        ) from e

    from tvd_pipeline.runtime_callback import pipeline_progress_scoped

    # Step 4: Instantiate processor and run pipeline
    _step_log(job_id, "WRAPPER", f"Starting {video_type} pipeline via monolith", progress=0, event_type="start")

    processor = VideoSceneProcessor()
    processor._llm_log_dir = str(JOB_ARTIFACTS_DIR / job_id / "llm_logs")
    monolith_result = None

    vt = video_type.lower()
    try:
        with pipeline_progress_scoped(callback):
            if vt == "product video":
                logger.info("[MONOLITH] job_id=%s calling process_product_video", job_id)
                monolith_result = processor.process_product_video(
                    **translated,
                    on_progress=callback,
                )
            elif vt == "influencer":
                logger.info("[MONOLITH] job_id=%s calling process_ugc_video (subtype=influencer)", job_id)
                translated["video_subtype"] = "influencer"
                monolith_result = processor.process_ugc_video(
                    **translated,
                    on_progress=callback,
                )
            elif vt == "personal-brand":
                logger.info("[MONOLITH] job_id=%s calling process_ugc_video (subtype=personal_brand)", job_id)
                translated["video_subtype"] = "personal_brand"
                monolith_result = processor.process_ugc_video(
                    **translated,
                    on_progress=callback,
                )
            elif vt == "ugc-real":
                logger.info("[MONOLITH] job_id=%s calling process_ugc_real_video", job_id)
                monolith_result = processor.process_ugc_real_video(
                    **translated,
                    on_progress=callback,
                )
            elif vt == "custom":
                storyboard = translated.pop("storyboard", None)
                if not storyboard:
                    raise ValueError("video_type=custom requires 'storyboard' in input_params")
                logger.info(
                    "[MONOLITH] job_id=%s calling process_custom_video (%d scenes)",
                    job_id, len((storyboard or {}).get("scenes") or []),
                )
                monolith_result = processor.process_custom_video(
                    storyboard,
                    **translated,
                    on_progress=callback,
                )
            else:
                raise ValueError(f"Unsupported video type for monolith bridge: {video_type}")
    except (JobPausedError, JobAbortedError):
        _save_cost_checkpoint(job_id, cost_tracker, supabase)
        raise
    except Exception:
        _save_cost_checkpoint(job_id, cost_tracker, supabase)
        raise

    # Finalize step timings
    callback.finish(detail="pipeline complete")

    # Save cost checkpoint after successful completion (before Mux upload)
    _save_cost_checkpoint(job_id, cost_tracker, supabase)

    if monolith_result is None:
        raise RuntimeError(f"Monolith returned None for job {job_id}")

    # For Type 2 simulation, replace placeholder URLs with real GCS assets
    if params.get("is_simulation") and params.get("simulation_type") == "monolith":
        from api_pipeline.services.simulation import _inject_real_sim_assets
        monolith_result = _inject_real_sim_assets(monolith_result, video_type=video_type)
        logger.info(f"[{job_id}] Injected real simulation assets into monolith result")

    # Step 5: Extract final video URL from monolith result
    final_video_url = _pick_final_video_url(monolith_result)
    if final_video_url and not _coerce_http_url(monolith_result.get("final_video_url")):
        logger.info(
            "[%s] Using fallback final URL key (final_video_url missing): first usable among %s",
            job_id,
            _FINAL_VIDEO_URL_KEYS,
        )

    if not final_video_url:
        # Dry-run mode: monolith returned successfully but no video was generated
        if monolith_result.get("dry_run"):
            logger.info(f"[{job_id}] Dry-run complete — no final video (asset generation skipped)")
            return {
                "final_video_url": None,
                "dry_run": True,
                "scenes": monolith_result.get("scenes"),
                "vo_script": monolith_result.get("vo_script"),
            }
        logger.warning("[%s] Monolith result keys (no final URL): %s", job_id, list(monolith_result.keys()))
        raise RuntimeError(_format_missing_final_video_error(job_id, monolith_result))

    # Step 5.5: Upload intermediates to GCS for permanent storage
    try:
        if services and services.gcs_storage:
            _step_log(job_id, "GCS", "Uploading intermediate assets to GCS...", progress=93)
            monolith_result = upload_intermediates_to_gcs(
                job_id=job_id,
                result=monolith_result,
                gcs=services.gcs_storage,
            )
            # If GCS uploaded the final video, use the permanent URL for Mux
            gcs_final = monolith_result.get("final_video_url")
            if gcs_final and gcs_final != final_video_url:
                logger.info(f"[{job_id}] Using GCS URL for Mux upload: {gcs_final}")
                final_video_url = gcs_final
    except Exception as e:
        logger.warning(f"[{job_id}] GCS intermediate upload failed, continuing with original URLs: {e}")

    # Step 6: Upload to Mux
    output = dict(monolith_result)
    output_resolution = params.get("output_resolution", "720p_low")

    if services and services.mux:
        _step_log(job_id, "MUX", "Uploading final video to Mux CDN...", progress=95)
        try:
            mux_result = services.mux.upload_video_async(
                final_video_url,
                job_id=job_id,
                output_resolution=output_resolution,
            )
            output["mux_upload_id"] = mux_result.get("upload_id")
            output["mux_status"] = mux_result.get("status", "uploading")
            _step_log(job_id, "MUX", f"Mux upload started (upload_id={mux_result.get('upload_id')})")
        except Exception as mux_err:
            logger.warning(f"[{job_id}] Mux upload failed: {mux_err}")
            _step_log(job_id, "MUX", f"Mux upload failed: {mux_err}", event_type="warn")

    # Use the original final URL as the MP4 URL (Mux will override later via webhook/fallback)
    output["final_mp4_url"] = final_video_url
    # Studio: expose pre/post ZapCap URLs explicitly on the job output
    if monolith_result.get("video_before_subtitles_url"):
        output["video_before_subtitles_url"] = monolith_result["video_before_subtitles_url"]
    if monolith_result.get("subtitled_video_url"):
        output["subtitled_video_url"] = monolith_result["subtitled_video_url"]
    if monolith_result.get("rendi_scene_voice_url"):
        output["rendi_scene_voice_url"] = monolith_result["rendi_scene_voice_url"]

    # Step 7: Cost summary
    cost_summary = cost_tracker.build_summary()
    output["cost_usd"] = cost_summary["total_usd"]
    output["cost_breakdown"] = cost_summary

    # Store usage in generation_usage table
    try:
        supabase.store_usage(job_id, cost_summary)
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to store usage: {e}")

    # Step 8: Persist GCS-upgraded URLs to Supabase intermediates
    try:
        gcs_urls = {k: v for k, v in output.items()
                    if isinstance(v, str) and "storage.googleapis.com/automatiq" in v}
        gcs_lists = {k: v for k, v in output.items()
                     if isinstance(v, list) and any(
                         isinstance(u, str) and "storage.googleapis.com/automatiq" in u
                         for u in v)}
        if gcs_urls or gcs_lists:
            supabase.update_progress(job_id, progress=-1, current_step="",
                                       intermediates={**gcs_urls, **gcs_lists})
            logger.info(f"[{job_id}] Persisted {len(gcs_urls) + len(gcs_lists)} GCS URLs to Supabase intermediates")
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to persist GCS URLs to Supabase: {e}")

    # Validate output (use output dict as intermediates — it contains the monolith's results)
    issues = validate_pipeline_output(job_id, video_type, output, params, output)
    if issues:
        output["issues"] = issues

    # Save pipeline log to artifacts (merge callback intermediates so scene_beat_clips etc. are included)
    try:
        _log_intermediates = dict(output)
        _log_intermediates.update(callback.get_intermediates())
        save_pipeline_log(job_id, video_type, params, _log_intermediates, output, type("Timer", (), {"entries": callback.get_timings()})())
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to save pipeline log: {e}")

    # Persist step timings
    try:
        supabase.update_step_timings(job_id, callback.get_timings())
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to persist step timings: {e}")

    elapsed = time.time() - t_start
    _step_log(job_id, "WRAPPER", f"Pipeline complete in {elapsed:.1f}s — cost ${cost_summary['total_usd']:.4f}",
              progress=98, event_type="info", cost_usd=cost_summary["total_usd"])

    return output
