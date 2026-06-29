"""In-memory job store for local development when Supabase is not configured.

Implements the same interface as SupabaseJobClient so the server and
progress callback work without a database. Jobs are lost on server restart.
"""

import copy
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _FakeTable:
    """Minimal fake Supabase table for .select().eq().execute() and .update().eq().execute()."""

    def __init__(self, store: "InMemoryJobClient", table_name: str):
        self._store = store
        self._table = table_name
        self._select_cols = "*"
        self._filters: Dict[str, Any] = {}
        self._update_data: Optional[Dict[str, Any]] = None

    def select(self, *cols: str) -> "_FakeTable":
        self._select_cols = ",".join(cs.strip() for c in cols) if cols else "*"
        return self

    def eq(self, key: str, value: Any) -> "_FakeTable":
        self._filters[key] = value
        return self

    def in_(self, key: str, values: list) -> "_FakeTable":
        self._filters[key] = values
        return self

    def order(self, key: str, desc: bool = False) -> "_FakeTable":
        self._order_key = key
        self._order_desc = desc
        return self

    def limit(self, n: int) -> "_FakeTable":
        self._limit = n
        return self

    def range(self, start: int, end: int) -> "_FakeTable":
        self._range = (start, end)
        return self

    def update(self, data: Dict[str, Any]) -> "_FakeTable":
        self._update_data = data
        return self

    def execute(self):
        jobs = list(self._store._jobs.values())
        for k, v in self._filters.items():
            if isinstance(v, list):
                jobs = [j for j in jobs if j.get(k) in v]
            else:
                jobs = [j for j in jobs if j.get(k) == v]
        if self._update_data is not None:
            for j in jobs:
                j.update(self._update_data)
            return type("Result", (), {"data": None})()
        if getattr(self, "_order_key", None):
            jobs.sort(key=lambda x: x.get(self._order_key) or "", reverse=getattr(self, "_order_desc", False))
        if getattr(self, "_limit", None):
            jobs = jobs[: self._limit]
        if getattr(self, "_range", None):
            s, e = self._range
            jobs = jobs[s : e + 1]
        return type("Result", (), {"data": jobs, "count": len(jobs)})()


class InMemoryJobClient:
    """In-memory implementation of the job store interface (SupabaseJobClient)."""

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self.table = "video_jobs"
        self._fake_client = type("Client", (), {"table": lambda _, name: _FakeTable(self, name)})()
        self.client = self._fake_client
        logger.warning("Using in-memory job store (no Supabase). Jobs are lost on restart.")

    def create_job(
        self,
        video_type: str,
        input_params: Dict[str, Any],
        customer_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        studio_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "id": job_id,
            "video_type": video_type,
            "input_params": input_params,
            "status": "pending",
            "progress": 0,
            "current_step": "queued",
            "intermediates": {},
            "output": {},
            "created_at": now,
            "updated_at": now,
        }
        if customer_id:
            row["customer_id"] = customer_id
        if tenant_id:
            row["tenant_id"] = tenant_id
        if user_id:
            row["user_id"] = user_id
        if studio_session_id:
            row["studio_session_id"] = studio_session_id
        self._jobs[job_id] = row
        logger.info(f"Created job {job_id} (type={video_type}, tenant={tenant_id})")
        return row

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self._jobs.get(job_id)
        return copy.deepcopy(job) if job is not None else None

    def mark_processing(self, job_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if job_id in self._jobs:
            self._jobs[job_id].update({
                "status": "processing",
                "started_at": now,
                "updated_at": now,
            })

    def update_progress(
        self,
        job_id: str,
        progress: int,
        current_step: str,
        intermediates: Optional[Dict[str, Any]] = None,
    ) -> None:
        if job_id not in self._jobs:
            return
        data = {
            "progress": progress,
            "current_step": current_step,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if intermediates is not None:
            merged = dict(self._jobs[job_id].get("intermediates", {}))
            merged.update(intermediates)
            data["intermediates"] = merged
        self._jobs[job_id].update(data)

    def merge_intermediates(self, job_id: str, data: Dict[str, Any]) -> None:
        """Merge keys into job intermediates without changing progress or current_step."""
        if not data or job_id not in self._jobs:
            return
        merged = dict(self._jobs[job_id].get("intermediates", {}))
        merged.update(data)
        self._jobs[job_id]["intermediates"] = merged
        self._jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    def mark_completed(
        self,
        job_id: str,
        output: Dict[str, Any],
        intermediates: Optional[Dict[str, Any]] = None,
    ) -> None:
        if job_id not in self._jobs:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._jobs[job_id].update({
            "status": "completed",
            "progress": 100,
            "current_step": "done",
            "output": output,
            "completed_at": now,
            "updated_at": now,
        })
        if intermediates is not None:
            self._jobs[job_id]["intermediates"] = intermediates
        logger.info(f"Job {job_id} completed")

    def mark_failed(
        self,
        job_id: str,
        error: str,
        error_details: Optional[Dict[str, Any]] = None,
        failed_at_step: Optional[str] = None,
    ) -> None:
        if job_id not in self._jobs:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._jobs[job_id].update({
            "status": "failed",
            "current_step": "failed",
            "error": error,
            "completed_at": now,
            "updated_at": now,
        })
        if error_details:
            self._jobs[job_id]["error_details"] = error_details
        if failed_at_step:
            self._jobs[job_id]["failed_at_step"] = failed_at_step
        logger.error(f"Job {job_id} failed at step '{failed_at_step}': {error}")

    def mark_aborted(self, job_id: str) -> None:
        if job_id not in self._jobs:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._jobs[job_id].update({
            "status": "aborted",
            "current_step": "aborted",
            "completed_at": now,
            "updated_at": now,
        })
        logger.info(f"Job {job_id} aborted by user")

    def mark_paused(self, job_id: str, current_step: Optional[str] = None) -> None:
        if job_id not in self._jobs:
            return
        now = datetime.now(timezone.utc).isoformat()
        upd: Dict[str, Any] = {
            "status": "paused",
            "updated_at": now,
        }
        if current_step is not None and str(current_step).strip():
            upd["current_step"] = str(current_step).strip()
        self._jobs[job_id].update(upd)
        logger.info("Job %s marked paused", job_id)

    def mark_retrying(self, job_id: str, retry_count: int) -> None:
        if job_id not in self._jobs:
            return
        self._jobs[job_id].update({
            "status": "processing",
            "error": None,
            "error_details": None,
            "failed_at_step": None,
            "completed_at": None,
            "retry_count": retry_count,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info(f"Job {job_id} retrying (attempt {retry_count})")

    def mark_queued(self, job_id: str) -> None:
        if job_id not in self._jobs:
            return
        self._jobs[job_id].update({
            "status": "queued",
            "current_step": "queued",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def mark_pending(self, job_id: str) -> None:
        if job_id not in self._jobs:
            return
        self._jobs[job_id].update({
            "status": "pending",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def list_jobs(
        self,
        status: Optional[str] = None,
        customer_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        jobs = list(self._jobs.values())
        if tenant_id:
            jobs = [j for j in jobs if j.get("tenant_id") == tenant_id]
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        if customer_id:
            jobs = [j for j in jobs if j.get("customer_id") == customer_id]
        jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
        return jobs[offset : offset + limit]

    def verify_job_ownership(self, job_id: str, tenant_id: str) -> Dict[str, Any]:
        from fastapi import HTTPException
        job = self.get_job(job_id)
        if not job or job.get("tenant_id") != tenant_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def get_tenant_limits(self, tenant_id: str) -> dict:
        return {
            "max_concurrent_jobs": 10,
            "max_concurrent_per_customer": 5,
            "max_queued_jobs": 50,
        }

    def count_active_jobs_for_tenant(self, tenant_id: str) -> int:
        return sum(
            1 for j in self._jobs.values()
            if j.get("tenant_id") == tenant_id and j.get("status") in ("processing", "pending")
        )

    def count_active_jobs_for_customer(self, tenant_id: str, customer_id: str) -> int:
        return sum(
            1 for j in self._jobs.values()
            if j.get("tenant_id") == tenant_id and j.get("customer_id") == customer_id
            and j.get("status") in ("processing", "pending")
        )

    def count_queued_jobs_for_tenant(self, tenant_id: str) -> int:
        return sum(
            1 for j in self._jobs.values()
            if j.get("tenant_id") == tenant_id and j.get("status") == "queued"
        )

    def get_next_queued_job(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        queued = [
            j for j in self._jobs.values()
            if j.get("tenant_id") == tenant_id and j.get("status") == "queued"
        ]
        queued.sort(key=lambda j: j.get("created_at") or "")
        return queued[0] if queued else None

    def get_tenants_with_queued_jobs(self) -> list:
        return list({
            j["tenant_id"] for j in self._jobs.values()
            if j.get("status") == "queued" and j.get("tenant_id")
        })

    def get_queue_position(self, job_id: str, tenant_id: str) -> int:
        queued = [
            j for j in self._jobs.values()
            if j.get("tenant_id") == tenant_id and j.get("status") == "queued"
        ]
        queued.sort(key=lambda j: j.get("created_at") or "")
        for i, j in enumerate(queued):
            if j.get("id") == job_id:
                return i + 1
        return 0

    def count_active_jobs(self) -> int:
        return sum(1 for j in self._jobs.values() if j.get("status") == "processing")

    def update_cost(self, job_id: str, cost_usd: float) -> None:
        if job_id not in self._jobs:
            return
        inter = dict(self._jobs[job_id].get("intermediates", {}))
        inter["cost_usd"] = cost_usd
        self._jobs[job_id]["intermediates"] = inter
        self._jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    def update_step_timings(self, job_id: str, timings: List[Dict[str, Any]]) -> None:
        if job_id not in self._jobs:
            return
        self._jobs[job_id]["step_timings"] = timings
        self._jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    def store_usage(self, job_id: str, cost_summary: dict) -> None:
        pass  # no-op for in-memory

    def studio_session_belongs_to_user(self, session_id: str, user_id: str) -> bool:
        """In-memory dev: accept any non-empty pair (no user_sessions table)."""
        return bool(session_id and user_id)

    def create_user_video(self, **kwargs) -> None:
        pass
