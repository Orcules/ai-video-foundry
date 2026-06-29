"""Pipeline routing — dispatches to monolith bridge or simulation runner.

The wrapper branch replaces the old pipeline implementations with two paths:

  1. Real pipelines → wrapper/monolith_bridge.py (calls the monolith)
  2. Simulation  → services/sim_pipeline_runner.py (mock services, no monolith)
"""

import logging

from api_pipeline.wrapper.monolith_bridge import run_monolith_pipeline
from api_pipeline.services.registry import ServiceRegistry
from api_pipeline.pipelines.base import (
    get_steps_for_type, clear_intermediates_from_step,
    PRODUCT_STEPS, INFLUENCER_STEPS, PERSONAL_BRAND_STEPS,
    JobAbortedError, JobPausedError, StepTimer, _step_log, _check_abort,
    _cleanup_cost_tracking, _seed_cost_tracking,
    save_pipeline_log, validate_pipeline_output,
    _is_transient_error,
)

logger = logging.getLogger(__name__)


def _is_simulation(services) -> bool:
    """Check if the services object is a simulation registry."""
    return type(services).__name__ == "SimServiceRegistry"


def run_product_pipeline(job_id, params, services, supabase):
    """Run product video pipeline."""
    if _is_simulation(services):
        from api_pipeline.services.sim_pipeline_runner import run_simulated_pipeline
        return run_simulated_pipeline(job_id, "product video", params, services, supabase)
    return run_monolith_pipeline(job_id, "product video", params, services, supabase)



def run_influencer_pipeline(job_id, params, services, supabase):
    """Run influencer pipeline (maps to monolith's process_ugc_video with video_subtype='influencer')."""
    if _is_simulation(services):
        from api_pipeline.services.sim_pipeline_runner import run_simulated_pipeline
        return run_simulated_pipeline(job_id, "influencer", params, services, supabase)
    return run_monolith_pipeline(job_id, "influencer", params, services, supabase)


def run_personal_brand_pipeline(job_id, params, services, supabase):
    """Run personal-brand pipeline (maps to monolith's process_ugc_video with video_subtype='personal_brand')."""
    if _is_simulation(services):
        from api_pipeline.services.sim_pipeline_runner import run_simulated_pipeline
        return run_simulated_pipeline(job_id, "personal-brand", params, services, supabase)
    return run_monolith_pipeline(job_id, "personal-brand", params, services, supabase)


def run_ugc_real_pipeline(job_id, params, services, supabase):
    """Run UGC Real pipeline."""
    if _is_simulation(services):
        from api_pipeline.services.sim_pipeline_runner import run_simulated_pipeline
        return run_simulated_pipeline(job_id, "ugc-real", params, services, supabase)
    return run_monolith_pipeline(job_id, "ugc-real", params, services, supabase)


def run_custom_pipeline(job_id, params, services, supabase):
    """Run custom storyboard pipeline (chat-built JSON consumed by process_custom_video).

    Wrapper-mode simulation (Type 1) is not supported — the mock pipeline doesn't
    know how to interpret a storyboard. For simulation, use monolith mode
    (simulation=True passed through to the monolith via simulation_type='monolith').
    """
    if _is_simulation(services):
        # Force monolith path even in wrapper-sim mode — the storyboard executor
        # itself handles simulation via the `simulation=True` flag inside ugc.py.
        logger.info(
            "[%s] Custom pipeline forced to monolith path (wrapper-sim does not support storyboards)",
            job_id,
        )
    return run_monolith_pipeline(job_id, "custom", params, services, supabase)


__all__ = [
    "run_product_pipeline",
    "run_influencer_pipeline", "run_personal_brand_pipeline", "run_ugc_real_pipeline",
    "run_custom_pipeline",
    "run_monolith_pipeline",
    "ServiceRegistry",
    "get_steps_for_type", "clear_intermediates_from_step",
    "PRODUCT_STEPS", "INFLUENCER_STEPS", "PERSONAL_BRAND_STEPS",
    "JobAbortedError", "JobPausedError", "StepTimer", "_step_log", "_check_abort",
    "_cleanup_cost_tracking", "_seed_cost_tracking",
    "save_pipeline_log", "validate_pipeline_output",
    "_is_transient_error",
]
