"""Structured INFO logs for outbound third-party API calls.

- **Server / Docker stdout:** search for ``[ExternalAPI]`` (logger ``tvd.external_api``).
- **Studio API Log:** lines with method **EXT** (from SSE / ``/api/jobs/{id}/pipeline-events``)
  when a pipeline job is running — requires ``pipeline_progress_scoped`` and, for thread-pool
  work, ``executor_submit_with_progress`` so workers inherit the progress callback.

Disable file + SSE forwarding with env ``TVD_EXTERNAL_API_LOG=0``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

_LOG = logging.getLogger("tvd.external_api")

_ENABLED = os.environ.get("TVD_EXTERNAL_API_LOG", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


def log_external_api_call(
    provider: str,
    operation: str,
    *,
    method: str = "POST",
    model: str = "",
    url_hint: str = "",
    detail: str = "",
) -> None:
    """Emit one line per outbound provider request (before the HTTP call)."""
    if _ENABLED:
        parts = [f"provider={provider}", f"op={operation}", f"method={method}"]
        if model:
            parts.append(f"model={model}")
        if url_hint:
            parts.append(f"path={url_hint.strip()[:120]}")
        if detail:
            parts.append(detail.strip()[:200])
        _LOG.info("[ExternalAPI] " + " | ".join(parts))

    try:
        from tvd_pipeline.runtime_callback import emit_progress_external_api

        emit_progress_external_api(
            {
                "phase": "start",
                "provider": provider,
                "operation": operation,
                "method": method,
                "model": model or "",
                "url_hint": (url_hint or "")[:200],
                "detail": (detail or "")[:400],
            }
        )
    except Exception:
        pass


def log_external_api_result(
    provider: str,
    operation: str,
    *,
    duration_ms: int,
    method: str = "POST",
    model: str = "",
    http_status: Optional[int] = None,
    ok: bool = True,
    error: str = "",
    detail: str = "",
) -> None:
    """Emit after an outbound HTTP/API call completes (duration + status)."""
    err_short = (error or "").replace("\n", " ")[:300]
    if _ENABLED:
        parts = [
            f"provider={provider}",
            f"op={operation}",
            f"method={method}",
            f"duration_ms={duration_ms}",
            f"ok={ok}",
        ]
        if http_status is not None:
            parts.append(f"http={http_status}")
        if model:
            parts.append(f"model={model}")
        if err_short:
            parts.append(f"err={err_short[:120]}")
        if detail:
            parts.append(detail.strip()[:160])
        _LOG.info("[ExternalAPI] done | " + " | ".join(parts))

    try:
        from tvd_pipeline.runtime_callback import emit_progress_external_api

        emit_progress_external_api(
            {
                "phase": "done",
                "provider": provider,
                "operation": operation,
                "method": method,
                "model": model or "",
                "duration_ms": int(duration_ms),
                "http_status": http_status,
                "ok": bool(ok),
                "error": err_short,
                "detail": (detail or "")[:400],
            }
        )
    except Exception:
        pass


def log_usage_event(job_id: str, data: Dict[str, Any]) -> None:
    """Log a completed billed call (from monolith usage / cost tracker)."""
    if not _ENABLED:
        return
    provider = data.get("provider") or "?"
    label = (data.get("label") or "").replace("\n", " ")[:80]
    model = data.get("model") or ""
    cat = data.get("category") or ""
    extra = []
    for k in (
        "input_tokens",
        "output_tokens",
        "duration_seconds",
        "character_count",
        "count",
        "resolution",
        "has_audio",
    ):
        v = data.get(k)
        if v is not None:
            extra.append(f"{k}={v}")
    tail = " | ".join(extra) if extra else ""
    _LOG.info(
        "[ExternalAPI] usage_complete | job=%s | provider=%s | model=%s | category=%s | label=%s%s%s",
        job_id,
        provider,
        model,
        cat,
        label,
        " | " if tail else "",
        tail,
    )
