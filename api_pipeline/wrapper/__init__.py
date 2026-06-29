"""Wrapper module — thin orchestration layer that delegates pipeline execution to the monolith."""

from api_pipeline.wrapper.monolith_bridge import run_monolith_pipeline
from api_pipeline.wrapper.input_translator import translate_params, resolve_animation_model
from api_pipeline.wrapper.progress_callback import create_progress_callback

__all__ = [
    "run_monolith_pipeline",
    "translate_params",
    "resolve_animation_model",
    "create_progress_callback",
]
