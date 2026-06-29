"""Attach the wrapper's on_progress callback for the duration of a monolith pipeline run.

Used to forward structured external-API traces to the API server's SSE / Studio log.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import Executor
from typing import Any, Callable, Dict, Iterator, Optional, TypeVar

_pipeline_progress: ContextVar[Optional[Callable[[str, Dict[str, Any]], None]]] = ContextVar(
    "pipeline_progress", default=None
)

_T = TypeVar("_T")


def executor_submit_with_progress(executor: Executor, fn: Callable[..., _T], *args, **kwargs):
    """Submit ``fn(*args, **kwargs)`` on a thread pool with caller ``ContextVar`` values in the worker.

    Python does not copy ``ContextVar`` values into ``ThreadPoolExecutor`` workers. Without this,
    ``emit_progress_external_api`` is a no-op inside parallel work (scene images, clean-product+VO, etc.),
    so Studio never shows ``EXT`` / ``[ExternalAPI]`` lines for those calls.

    We use ``copy_context()`` so *all* context vars active on the submitting thread (not only
    ``pipeline_progress``) are visible inside the worker.
    """
    ctx = copy_context()

    def _runner() -> _T:
        return ctx.run(lambda: fn(*args, **kwargs))

    return executor.submit(_runner)


@contextmanager
def pipeline_progress_scoped(
    callback: Optional[Callable[[str, Dict[str, Any]], None]],
) -> Iterator[None]:
    """Set the active progress callback for the current thread/async context."""
    if callback is None:
        yield
        return
    token = _pipeline_progress.set(callback)
    try:
        yield
    finally:
        _pipeline_progress.reset(token)


def emit_progress_external_api(data: Dict[str, Any]) -> None:
    """If a pipeline run is active, send an ``external_api`` progress event to the wrapper."""
    cb = _pipeline_progress.get()
    if cb is None:
        return
    try:
        cb("external_api", data)
    except Exception:
        pass
