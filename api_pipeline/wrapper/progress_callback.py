"""Factory for the on_progress callback passed to the monolith.

The callback bridges monolith progress events to the API's SSE event store,
Supabase progress tracking, and cost accumulation.

When ``simulation_duration`` is provided (monolith simulation mode), the
callback injects proportional delays after each step_complete event, reusing
the same timing model as the wrapper simulation (``simulation_timings.json``).
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from api_pipeline.pipelines.base import JobAbortedError, JobPausedError
from api_pipeline.services.simulation import _parse_simulation_duration

logger = logging.getLogger(__name__)


def _exception_chain_errno_22(exc: BaseException) -> bool:
    """True if exc or any __cause__/__context__ is OSError with errno 22 (Windows EINVAL)."""
    seen = set()
    chain: Optional[BaseException] = exc
    for _ in range(12):
        if chain is None or id(chain) in seen:
            break
        seen.add(id(chain))
        if isinstance(chain, OSError) and getattr(chain, "errno", None) == 22:
            return True
        msg = str(chain)
        if "Errno 22" in msg or "WinError 10022" in msg:
            return True
        chain = getattr(chain, "__cause__", None) or getattr(chain, "__context__", None)
    return False


# Map monolith step names (sent in step_complete) to wrapper step IDs (pause_after_step from API).
# The Studio sends pause_after_step="step_1" etc.; the monolith sends step="parse_prompt".
MONOLITH_TO_WRAPPER_STEP = {
    "character_description": "step_0",
    "analyze_media": "step_0.5",
    "ref_image_analyses": "step_0.5",
    "parse_prompt": "step_1",
    "clean_product_image": "step_2",
    "analyze_reference": "step_2.5",
    "vo_generation": "step_2.7",
    "scene_prompts": "step_3",
    "steps_4_7": "steps_4_7",
    "step_7.5": "step_7.5",
    "music": "step_8",
    "step_8": "step_8",
    "step_9": "step_9",
    "animations_review": "step_12",
}

# Load timing baselines (same file used by SimServiceRegistry)
_TIMINGS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "simulation_timings.json")
with open(_TIMINGS_PATH) as _f:
    _TIMINGS = json.load(_f)


def create_progress_callback(
    job_id: str,
    supabase,
    event_store,
    cost_tracker,
    simulation_duration: Optional[str] = None,
    is_simulation: bool = False,
    video_type: str = None,
    pause_after_step: Optional[str] = None,
) -> Callable[[str, Dict[str, Any]], None]:
    """Create the on_progress callback for a monolith pipeline run.

    The monolith calls ``on_progress(event_type, data)`` at various points.
    This factory returns a callback that handles three event types:

    - ``"usage"`` — the monolith reports an API usage event. We compute
      the delta cost, emit an SSE cost_update event, and update Supabase.
    - ``"step_start"`` — a pipeline step is about to begin. We emit an
      SSE "start" event (blue in the dashboard) and begin timing.
    - ``"step_complete"`` — a pipeline step finished. We update Supabase
      progress/current_step, emit an SSE event, and track timing.
    - ``"intermediate"`` — an intermediate result is available. We merge
      it into the Supabase intermediates JSONB field.
    - ``"warning"`` — a non-fatal warning from the monolith.
    - ``"artifact"`` — a debug artifact (JSON or text) to persist to disk.
    - ``"external_api"`` — outbound third-party call (start or done); forwarded to SSE / Studio.

    The callback also checks for abort/pause before processing each event.

    When ``simulation_duration`` is set, proportional delays are injected
    after each step_complete event so the monolith simulation is paced
    similarly to the wrapper simulation.

    Args:
        job_id: The Supabase job ID.
        supabase: SupabaseJobClient instance.
        event_store: The SSE JobEventStore singleton.
        cost_tracker: CostTracker instance for the current job.
        simulation_duration: Optional duration string (e.g. ``"30s"``,
            ``"1m"``, ``"real"``, ``"none"``). Only used for monolith
            simulation runs.
        is_simulation: Whether this is a simulation run (Type 2 monolith sim).
            When True, placeholder asset URLs in SSE events are swapped for
            real GCS URLs.

    Returns:
        A callable ``on_progress(event_type: str, data: dict) -> None``.
    """
    # --- Simulation delay setup ---
    _sim_mode, _sim_target = _parse_simulation_duration(simulation_duration)
    _fixed_steps = _TIMINGS["fixed_steps"]
    _per_scene_seconds = _TIMINGS["per_scene_seconds"]
    _baseline_scene_count = _TIMINGS["baseline_scene_count"]

    # Compute scale factor (same formula as SimServiceRegistry)
    _asset_total = _per_scene_seconds * _baseline_scene_count
    _fixed_total = sum(_fixed_steps.values())
    _real_total = _fixed_total + _asset_total

    if _sim_mode == "none":
        _sim_scale = 0.0
    elif _sim_mode == "real":
        _sim_scale = 1.0
    else:  # scaled
        _sim_scale = _sim_target / _real_total if _real_total > 0 else 0.0

    _is_sim = is_simulation
    _video_type = video_type

    if _sim_scale > 0:
        logger.info(f"[{job_id}] Monolith sim delay enabled — mode={_sim_mode}, "
                     f"scale={_sim_scale:.3f}, target={_sim_target}s")

    # Step timing state
    _step_timings = []
    _current_step_start: Dict[str, Any] = {}
    _accumulated_intermediates: Dict[str, Any] = {}

    def _flush_accumulated_intermediates_before_pause(
        canonical_step: str, monolith_step: str
    ) -> None:
        """Persist in-memory intermediates before pausing (merge may have failed earlier)."""
        if not _accumulated_intermediates:
            return
        try:
            supabase.merge_intermediates(job_id, dict(_accumulated_intermediates))
        except Exception as e:
            logger.error(
                "[%s] Failed to flush accumulated intermediates before pause: %s",
                job_id,
                e,
            )
            is_vo_pause = (
                canonical_step == "step_2.7"
                or monolith_step == "vo_generation"
                or pause_after_step in ("step_2.7", "vo_generation")
            )
            if is_vo_pause:
                raise

    def _check_job_status():
        """Check if the job has been aborted or paused. Raises accordingly."""
        try:
            job = supabase.get_job(job_id)
        except OSError as e:
            # Windows can raise EINVAL (Errno 22) on some HTTP/socket paths; do not fail the whole job.
            logger.warning("[%s] job status check OSError (continuing): %s", job_id, e)
            return
        except Exception as e:
            # Transient Supabase errors (HTTP/2 ConnectionTerminated, 503) must not crash the pipeline.
            logger.warning("[%s] job status check transient error (continuing): %s", job_id, e)
            return
        if not job:
            return
        status = job.get("status")
        if status == "aborted":
            raise JobAbortedError(f"Job {job_id} was aborted by user")
        if status == "paused":
            raise JobPausedError(f"Job {job_id} was paused by user")

    def _finish_current_step(detail: Optional[str] = None):
        """Close out the current step timing entry if one is active."""
        nonlocal _current_step_start
        if not _current_step_start:
            return
        now = datetime.now(timezone.utc)
        start_iso = _current_step_start.get("started_at")
        duration = None
        if start_iso:
            try:
                start_dt = datetime.fromisoformat(start_iso)
                duration = round((now - start_dt).total_seconds(), 2)
            except Exception:
                pass
        _step_timings.append({
            "step": _current_step_start.get("step"),
            "label": _current_step_start.get("label"),
            "started_at": start_iso,
            "ended_at": now.isoformat(),
            "duration_sec": duration,
            "status": "completed",
            "detail": detail,
        })
        _current_step_start = {}

    def on_progress(event_type: str, data: Dict[str, Any]) -> None:
        """Handle a progress event from the monolith.

        Args:
            event_type: One of "usage", "step_start", "step_complete",
                "intermediate", "warning", "artifact", "external_api".
            data: Event-specific payload dict.
        """
        nonlocal _current_step_start

        # Always check for abort/pause before processing
        _check_job_status()

        try:
            if event_type == "usage":
                _handle_usage(data)
            elif event_type == "step_start":
                _handle_step_start(data)
            elif event_type == "step_complete":
                _handle_step_complete(data)
            elif event_type == "intermediate":
                _handle_intermediate(data)
            elif event_type == "warning":
                _handle_warning(data)
            elif event_type == "artifact":
                _handle_artifact(data)
            elif event_type == "external_api":
                _handle_external_api(data)
            else:
                logger.warning(f"[{job_id}] Unknown progress event type: {event_type}")
        except JobAbortedError:
            raise
        except JobPausedError:
            raise
        except Exception as e:
            if not _exception_chain_errno_22(e):
                raise
            logger.warning(
                "[%s] on_progress Errno-22-style error (event=%s): %s",
                job_id,
                event_type,
                e,
            )
            if event_type == "step_complete" and pause_after_step:
                step = data.get("step", "")
                canonical = MONOLITH_TO_WRAPPER_STEP.get(step, step)
                if step == pause_after_step or canonical == pause_after_step:
                    _flush_accumulated_intermediates_before_pause(canonical, step)
                    for _mp_attempt in range(4):
                        try:
                            supabase.mark_paused(job_id, step)
                            break
                        except Exception as _mp_e:
                            if _mp_attempt == 3:
                                logger.warning("[%s] mark_paused failed after Errno-22 recovery: %s", job_id, _mp_e)
                            else:
                                time.sleep(0.35 * (_mp_attempt + 1))
                    try:
                        pr = data.get("progress")
                        pr_int = int(pr) if pr is not None and int(pr) >= 0 else -1
                        event_store.push(
                            job_id,
                            "SERVER",
                            f"Paused after {data.get('label', step)} — resume when ready",
                            progress=pr_int,
                            event_type="pause",
                        )
                    except Exception:
                        pass
                    raise JobPausedError(
                        f"Paused after {canonical} for job {job_id} (recovered from Errno 22)",
                        pause_monolith_step=step,
                    )
            # Do not swallow Errno-22 for vo_script intermediates — otherwise the job can pause
            # with an empty vo_script in Supabase and Studio shows a misleading empty textarea.
            if event_type == "intermediate" and data.get("key") == "vo_script":
                raise
            if event_type in ("usage", "step_start", "intermediate", "warning", "artifact"):
                return
            raise

    def _handle_external_api(data: Dict[str, Any]):
        """Forward outbound provider calls to SSE (Studio API log / pipeline-events)."""
        phase = (data.get("phase") or "start").strip().lower()
        prov = (data.get("provider") or "?").strip()
        op = (data.get("operation") or "?").strip()
        method = (data.get("method") or "").strip() or "?"
        model = (data.get("model") or "").strip()
        url_hint = (data.get("url_hint") or "").strip()
        detail = (data.get("detail") or "").strip()
        duration_ms = data.get("duration_ms")
        http_status = data.get("http_status")
        ok = data.get("ok")
        err = (data.get("error") or "").strip()

        parts = [f"→ OUT [{phase}]", prov, method, op]
        if model:
            parts.append(f"model={model}")
        if url_hint:
            parts.append(url_hint[:100])
        if detail and phase == "start":
            parts.append(detail[:180])
        if duration_ms is not None:
            try:
                parts.append(f"{int(duration_ms)}ms")
            except (TypeError, ValueError):
                parts.append(f"{duration_ms}ms")
        if http_status is not None:
            parts.append(f"HTTP {http_status}")
        if ok is not None:
            parts.append("ok" if ok else "FAIL")
        if err:
            parts.append(err[:160])

        msg = " ".join(str(p) for p in parts if p)
        try:
            event_store.push(job_id, "EXTERNAL_API", msg, progress=-1, event_type="info")
        except Exception as e:
            logger.warning("[%s] external_api event_store.push failed: %s", job_id, e)
        logger.info("[%s] %s", job_id, msg)

    def _handle_usage(data: Dict[str, Any]):
        """Process a usage/cost event from the monolith.

        Expected data keys (model:provider style):
            model: str — model identifier (e.g. "gemini-2.5-flash")
            provider: str — provider name (e.g. "vertex", "kie", "openai")
            category: str — cost category (e.g. "gemini_text", "videos")
            label: str — human-readable description
            actual_cost_usd: float — optional real billing cost from provider
            (plus service-specific keys: input_tokens, output_tokens,
             duration_seconds, character_count, count, resolution, has_audio, etc.)

        The monolith sends usage events WITHOUT a pre-computed cost_usd.
        We use cost_tracker.record_usage() to compute cost from the
        model:provider composite key in pricing.json.

        If data contains actual_cost_usd (e.g. from Vercel), that value is
        used directly instead of estimating.
        """
        # Skip cost tracking for failed/blocked calls — providers don't bill for these
        if data.get("success") is False:
            logger.info(
                f"[{job_id}] Skipping cost for failed call: {data.get('label', '')} "
                f"({data.get('model', '?')}:{data.get('provider', '?')})"
            )
            return

        # Re-bucket category based on provider/service for accurate cost breakdown.
        # The monolith sends category="text" for all LLM calls regardless of
        # provider, which causes OpenAI/Vercel costs to be lumped into gemini_text
        # in the cost summary.  Re-map using the provider field (authoritative).
        provider = data.get("provider", "")
        service = data.get("service", "")
        category = data.get("category", "unknown")
        if category in ("text",):
            # Use provider field first (most reliable), then service name as fallback
            if provider in ("openai",) or service in ("openai",):
                data["category"] = "openai"
            elif provider in ("vercel",) or service in ("vercel",):
                data["category"] = "vercel"
            else:
                data["category"] = "gemini_text"
        elif category == "image":
            data["category"] = "images"
        elif category == "video":
            data["category"] = "videos"

        # Use the unified record_usage method to compute cost from model:provider
        cost = cost_tracker.record_usage(data)
        total = cost_tracker.total_usd

        # Emit SSE cost update
        event_store.push(
            job_id, "COST",
            f"Cost: ${total:.4f} (+${cost:.4f} {data.get('label', '')})",
            progress=-1, event_type="info",
            cost_usd=round(total, 4),
            step_cost_usd=round(cost, 4),
        )

        # Update Supabase cost
        try:
            supabase.update_cost(job_id, round(total, 4))
        except Exception as e:
            logger.warning(f"[{job_id}] Failed to update Supabase cost: {e}")

    def _handle_step_start(data: Dict[str, Any]):
        """Process a step_start event — emits an SSE 'start' event and begins timing.

        Expected data keys:
            step: str — step identifier
            label: str — human-readable step name
            message: str — status message (e.g. "Generating scene images...")
        """
        nonlocal _current_step_start
        step = data.get("step", "unknown")
        label = data.get("label", step)
        message = data.get("message", f"Starting {label}...")

        # Start timing for this step (will be closed by step_complete)
        _current_step_start = {
            "step": step,
            "label": label,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        # Emit SSE event with event_type="start" (dashboard already styles this blue+bold)
        event_store.push(
            job_id, step, message,
            progress=-1, event_type="start",
        )

        # Keep Supabase current_step in sync with the step that is *running* (not only after
        # step_complete). Otherwise the UI can show e.g. parse_prompt for minutes while
        # highlights + VO generation are actually executing (step_complete lags behind).
        try:
            job = supabase.get_job(job_id)
            existing_progress = int(job.get("progress") or 0) if job else 0
            if existing_progress < 0:
                existing_progress = 0
            supabase.update_progress(job_id, existing_progress, step)
        except Exception as e:
            logger.warning(f"[{job_id}] step_start: could not update current_step: {e}")

        logger.info(f"[{job_id}] [{step}] {message}")

    def _handle_step_complete(data: Dict[str, Any]):
        """Process a step_complete event.

        Expected data keys:
            step: str — step identifier (e.g. "step_1", "steps_4_7")
            label: str — human-readable step name
            progress: int — overall progress percentage (0-100)
            message: str — status message
            detail: str — optional detail for step timing
        """
        step = data.get("step", "unknown")
        label = data.get("label", step)
        progress = data.get("progress", -1)
        message = data.get("message", f"Step '{label}' complete")
        detail = data.get("detail")

        # Close out current step timing and start tracking the next step
        _finish_current_step(detail)

        # Monotonic progress guard — never let progress go backwards
        if progress >= 0:
            if not hasattr(on_progress, '_max_progress'):
                on_progress._max_progress = 0
            progress = max(progress, on_progress._max_progress)
            on_progress._max_progress = progress

        # Update Supabase progress
        try:
            supabase.update_progress(job_id, progress, step)
        except Exception as e:
            logger.warning(f"[{job_id}] Failed to update Supabase progress: {e}")

        # Compute per-step cost delta
        step_cost = None
        if cost_tracker:
            current_total = cost_tracker.total_usd
            if not hasattr(on_progress, '_prev_total'):
                on_progress._prev_total = 0.0
            delta = current_total - on_progress._prev_total
            if delta > 0:
                step_cost = round(delta, 4)
            on_progress._prev_total = current_total

        # Emit SSE event (with optional inline asset)
        asset_url = data.get("asset_url")
        asset_type = data.get("asset_type")

        # For Type 2 simulation, replace placeholder asset URLs
        if _is_sim and asset_url and "/simulation/placeholder" in asset_url:
            from api_pipeline.services.simulation import _swap_sim_asset_url
            asset_url = _swap_sim_asset_url(step, asset_url, video_type=_video_type)
        extra = {}
        if cost_tracker:
            extra["cost_usd"] = round(cost_tracker.total_usd, 4)
        if step_cost:
            extra["step_cost_usd"] = step_cost
        event_store.push(
            job_id, step, message,
            progress=progress, event_type="info",
            asset_url=asset_url, asset_type=asset_type,
            **extra,
        )

        # Start timing the next step (will be closed by the next step_complete or on_progress_done)
        _current_step_start = {
            "step": step,
            "label": label,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(f"[{job_id}] [STEP_COMPLETE] {step} — {label}: {message} (progress={progress}%)")

        # Pause after this step if requested (step-by-step flow: pipeline stops, user reviews, then resume).
        # Map monolith step name (e.g. parse_prompt) to wrapper step ID (e.g. step_1) so pause_after_step matches.
        canonical_step = MONOLITH_TO_WRAPPER_STEP.get(step, step)
        if pause_after_step and (step == pause_after_step or canonical_step == pause_after_step):
            _flush_accumulated_intermediates_before_pause(canonical_step, step)
            _pause_ok = False
            for _mp_attempt in range(5):
                try:
                    supabase.mark_paused(job_id, step)
                    _pause_ok = True
                    break
                except Exception as e:
                    if _mp_attempt == 4:
                        logger.error(f"[{job_id}] mark_paused failed after retries: {e}")
                    else:
                        time.sleep(0.4 * (_mp_attempt + 1))
            if _pause_ok:
                try:
                    event_store.push(
                        job_id, "SERVER",
                        f"Paused after {label} — continue when ready (Studio: Approve and continue)",
                        progress=progress, event_type="pause",
                    )
                except Exception as es:
                    logger.warning(f"[{job_id}] event_store pause push failed: {es}")
                logger.info(f"[{job_id}] Paused after step {canonical_step} (pause_after_step); raising JobPausedError to stop pipeline")
            else:
                logger.warning(
                    "[%s] Proceeding to JobPausedError without confirmed DB pause — server will normalize status",
                    job_id,
                )
            raise JobPausedError(
                f"Paused after {canonical_step} for job {job_id} (step-by-step flow)",
                pause_monolith_step=step,
            )

        # --- Simulation delay: sleep proportionally after each step ---
        if _sim_scale > 0:
            if step == "steps_4_7":
                base = _per_scene_seconds
            elif step in _fixed_steps:
                base = _fixed_steps[step]
            else:
                base = 5.0  # fallback for unknown steps
            remaining = base * _sim_scale
            while remaining > 0:
                chunk = min(2.0, remaining)
                time.sleep(chunk)
                remaining -= chunk
                _check_job_status()  # abort/pause check between chunks

    def _handle_warning(data: Dict[str, Any]):
        """Process a warning event from the monolith.

        Logs with [job_id] so FallbackLogHandler captures it for the
        dashboard's 'Issues, Warnings, Fallbacks' section, and pushes
        an SSE warn event for real-time display.
        """
        msg = data.get("message", "Unknown warning")
        logger.warning(f"[{job_id}] {msg}")
        event_store.push(job_id, "WARNING", msg, event_type="warn")

    def _handle_artifact(data: Dict[str, Any]):
        """Save a debug artifact from the monolith to the job's artifact dir."""
        name = data.get("name")
        payload = data.get("data")
        fmt = data.get("format", "json")
        if not name or payload is None:
            logger.warning(f"[{job_id}] Artifact event missing 'name' or 'data'")
            return
        try:
            from pathlib import Path
            artifacts_dir = Path(__file__).resolve().parent.parent / "job_artifacts" / job_id
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            ext = ".json" if fmt == "json" else ".txt"
            if fmt == "json":
                (artifacts_dir / f"{name}{ext}").write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
            else:
                (artifacts_dir / f"{name}{ext}").write_text(str(payload), encoding="utf-8")
            logger.info(f"[{job_id}] Saved artifact: {name}{ext} -> {artifacts_dir / f'{name}{ext}'}")
        except Exception as e:
            logger.warning(f"[{job_id}] Failed to save artifact '{name}': {e}")

    def _handle_intermediate(data: Dict[str, Any]):
        """Process an intermediate result event.

        Expected data keys:
            key: str — intermediate field name (e.g. "scene_images", "vo_script")
            value: Any — the intermediate value
            (OR a flat dict of key/value pairs to merge)
        """
        try:
            intermediates = {}
            if "key" in data and "value" in data:
                intermediates[data["key"]] = data["value"]
            else:
                intermediates = {k: v for k, v in data.items() if k not in ("event_type",)}

            if not intermediates:
                return

            keys_str = ", ".join(intermediates.keys())
            logger.info(f"[{job_id}] [INTERMEDIATE] saved keys: {keys_str}")

            _accumulated_intermediates.update(intermediates)

            try:
                supabase.merge_intermediates(job_id, intermediates)
            except Exception as e:
                logger.error(f"[{job_id}] Failed to merge intermediates keys [{keys_str}]: {e}")
                if "vo_script" in intermediates:
                    raise
            review_messages = {
                "offer_profile": "Offer analysis is ready for review.",
                "creative_strategy": "Creative strategy is ready.",
                "narrative_plan": "Narrative plan is ready.",
                "nine_cell_plan": "Nine-cell plan is ready for review.",
                "scene_plan": "Scene plan is ready for review.",
                "scene_grids": "Scene grids are ready for review.",
                "frame_classifications": "Frame routing is ready for review.",
                "scene_video_plan": "Render routing plan was updated.",
            }
            for key in intermediates.keys():
                if key in review_messages:
                    try:
                        event_store.push(job_id, "REVIEW", review_messages[key], event_type="info")
                    except Exception:
                        pass
        except Exception as e:
            if data.get("key") == "vo_script" or "vo_script" in data:
                raise
            logger.warning(f"[{job_id}] _handle_intermediate failed (non-fatal): {e}")

    # Attach helper methods to the callback for the bridge to use
    on_progress.finish = lambda detail=None: _finish_current_step(detail)
    on_progress.get_timings = lambda: list(_step_timings)
    on_progress.get_intermediates = lambda: dict(_accumulated_intermediates)

    # Seed _prev_total from the restored tracker so checkpoint cost doesn't
    # appear as a phantom delta on the first step_complete event.
    on_progress._prev_total = cost_tracker.total_usd

    return on_progress
