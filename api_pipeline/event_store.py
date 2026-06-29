"""Thread-safe event store bridging pipeline threads to SSE endpoints."""

import logging
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass
class PipelineEvent:
    """A single pipeline event."""
    timestamp: str
    step: str
    message: str
    epoch: float = 0.0
    progress: int = -1  # -1 means no progress update
    event_type: str = "info"  # info, start, complete, error, warn, abort, pause
    elapsed: Optional[float] = None
    cost_usd: Optional[float] = None
    step_cost_usd: Optional[float] = None
    asset_url: Optional[str] = None    # URL to a generated asset (image or video)
    asset_type: Optional[str] = None   # "image", "video", "audio", or "text"

    def to_dict(self) -> dict:
        d = asdict(self)
        # Omit optional fields when None to keep events lean
        for key in ("epoch", "elapsed", "cost_usd", "step_cost_usd", "asset_url", "asset_type"):
            if key == "epoch" or d.get(key) is None:
                d.pop(key, None)
        return d


class JobEventStore:
    """Thread-safe store for pipeline events, consumed by SSE endpoints."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: Dict[str, List[PipelineEvent]] = {}
        self._waiters: Dict[str, threading.Event] = {}

    def push(
        self,
        job_id: str,
        step: str,
        message: str,
        progress: int = -1,
        event_type: str = "info",
        elapsed: Optional[float] = None,
        cost_usd: Optional[float] = None,
        step_cost_usd: Optional[float] = None,
        asset_url: Optional[str] = None,
        asset_type: Optional[str] = None,
    ) -> None:
        """Append an event and wake any SSE waiters."""
        ev = PipelineEvent(
            timestamp=time.strftime("%H:%M:%S"),
            epoch=time.time(),
            step=step,
            message=message,
            progress=progress,
            event_type=event_type,
            elapsed=elapsed,
            cost_usd=cost_usd,
            step_cost_usd=step_cost_usd,
            asset_url=asset_url,
            asset_type=asset_type,
        )
        with self._lock:
            if job_id not in self._events:
                self._events[job_id] = []
            self._events[job_id].append(ev)
            # Wake any SSE waiter
            waiter = self._waiters.get(job_id)
            if waiter:
                waiter.set()

    def get_events(self, job_id: str, after_index: int = 0) -> List[PipelineEvent]:
        """Return events from cursor position onward."""
        with self._lock:
            events = self._events.get(job_id, [])
            return events[after_index:]

    def get_events_page(
        self, job_id: str, after_index: int = 0, limit: int = 80
    ) -> tuple:
        """Return up to ``limit`` events from ``after_index`` (avoids huge payloads)."""
        with self._lock:
            evs = self._events.get(job_id, [])
            chunk = evs[after_index : after_index + limit]
            return chunk, len(evs)

    def get_waiter(self, job_id: str) -> threading.Event:
        """Get or create a threading.Event for SSE to wait on."""
        with self._lock:
            if job_id not in self._waiters:
                self._waiters[job_id] = threading.Event()
            return self._waiters[job_id]

    def clear_waiter(self, job_id: str) -> None:
        """Reset the waiter after consuming events."""
        with self._lock:
            waiter = self._waiters.get(job_id)
            if waiter:
                waiter.clear()

    def event_count(self, job_id: str) -> int:
        """Return total event count for a job."""
        with self._lock:
            return len(self._events.get(job_id, []))

    def cleanup_job(self, job_id: str) -> None:
        """Remove all events and waiters for a job."""
        with self._lock:
            self._events.pop(job_id, None)
            self._waiters.pop(job_id, None)

    def cleanup_old(self, max_age_seconds: int = 3600) -> None:
        """Remove events for jobs older than max_age_seconds.
        Uses the timestamp of the last event as proxy for job age."""
        with self._lock:
            if len(self._events) > 100:
                # Keep the 50 most recent jobs (by last event timestamp)
                sorted_jobs = sorted(
                    self._events.keys(),
                    key=lambda jid: self._events[jid][-1].epoch if self._events[jid] else 0,
                    reverse=True,
                )
                for jid in sorted_jobs[50:]:
                    del self._events[jid]
                    self._waiters.pop(jid, None)


class FallbackLogStore:
    """Captures FALLBACK warning logs per job_id for the dashboard."""

    _JOB_ID_RE = re.compile(r"\[([a-f0-9-]{36})\]")

    def __init__(self, max_per_job: int = 200):
        self._lock = threading.Lock()
        self._logs: Dict[str, List[dict]] = {}
        self._max = max_per_job

    def append(self, job_id: str, entry: dict) -> None:
        with self._lock:
            if job_id not in self._logs:
                self._logs[job_id] = []
            if len(self._logs[job_id]) < self._max:
                self._logs[job_id].append(entry)

    def get_logs(self, job_id: str) -> List[dict]:
        with self._lock:
            return list(self._logs.get(job_id, []))

    def cleanup_job(self, job_id: str) -> None:
        with self._lock:
            self._logs.pop(job_id, None)

    def cleanup_old(self) -> None:
        with self._lock:
            if len(self._logs) > 100:
                oldest = sorted(self._logs.keys())[:len(self._logs) - 50]
                for jid in oldest:
                    del self._logs[jid]


class FallbackLogHandler(logging.Handler):
    """Python logging handler that intercepts fallback/failure logs and stores them per job."""

    _FALLBACK_PATTERNS = (
        re.compile(r"FALLBACK:", re.IGNORECASE),
        re.compile(r"falling back", re.IGNORECASE),
        re.compile(r"also failed", re.IGNORECASE),
    )

    def __init__(self, store: FallbackLogStore):
        super().__init__(level=logging.WARNING)
        self._store = store

    _ISSUE_KEYWORDS = (
        "error", "failed", "skipped", "quota", "timeout",
        "none", "missing", "fallback", "not applied",
        "returned none", "also failed", "default",
    )

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        is_error = record.levelno >= logging.ERROR
        matches_pattern = any(p.search(msg) for p in self._FALLBACK_PATTERNS)
        m = FallbackLogStore._JOB_ID_RE.search(msg)

        if matches_pattern:
            pass  # Always capture pattern matches
        elif m:
            # Has job UUID — capture ERRORs always, WARNINGs with issue keywords
            if not is_error:
                lower = msg.lower()
                if not any(kw in lower for kw in self._ISSUE_KEYWORDS):
                    return  # Benign job warning — skip
        else:
            return  # No pattern, no UUID — skip

        job_id = m.group(1) if m else "__global__"

        fallback_msg = msg
        for kw in ("FALLBACK:", "falling back", "also failed"):
            idx = msg.lower().find(kw.lower())
            if idx >= 0:
                fallback_msg = msg[idx:]
                break
        if not matches_pattern and m:
            fallback_msg = msg[m.end():].strip().lstrip("] ").strip()

        self._store.append(job_id, {
            "timestamp": time.strftime("%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": fallback_msg,
        })


# Module-level singletons
event_store = JobEventStore()
fallback_store = FallbackLogStore()
fallback_handler = FallbackLogHandler(fallback_store)
