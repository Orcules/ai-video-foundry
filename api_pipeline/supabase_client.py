"""Supabase client for video_jobs table CRUD operations."""

import os
import logging
import time
import uuid
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from supabase import create_client, Client

logger = logging.getLogger(__name__)

_TENANT_LIMITS_TTL = 300  # 5 minutes


def resolve_supabase_public_credentials() -> tuple[Optional[str], Optional[str]]:
    """Project URL + anon (public) API key from env.

    Accepts either ``SUPABASE_PUBLISHABLE_KEY`` or ``SUPABASE_ANON_KEY`` so local
    setups match the Supabase dashboard naming and stay aligned with ``/api/config``.
    """
    url = (os.environ.get("SUPABASE_URL") or "").strip() or None
    key = (
        (os.environ.get("SUPABASE_PUBLISHABLE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or "").strip()
        or None
    )
    return url, key


def is_supabase_env_configured() -> bool:
    """True when URL and at least one public (anon/publishable) key are set."""
    u, k = resolve_supabase_public_credentials()
    return bool(u and k)


class SupabaseJobClient:
    """Manages video_jobs table in Supabase."""

    def __init__(self):
        url, key = resolve_supabase_public_credentials()
        if not url or not key:
            raise ValueError(
                "SUPABASE_URL and (SUPABASE_ANON_KEY or SUPABASE_PUBLISHABLE_KEY) must be set"
            )
        self.client: Client = create_client(url, key)
        self.table = "video_jobs"
        service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        self._admin_client: Optional[Client] = (
            create_client(url, service_key) if service_key else None
        )
        if not self._admin_client:
            logger.warning(
                "SUPABASE_SERVICE_ROLE_KEY not set — user_videos rows cannot be created on job completion"
            )
        # Short TTL cache for Studio auth (see auth._implicit_studio_tenant_api_key).
        self._studio_single_tenant_cache_until: float = 0.0
        self._studio_single_tenant_cache_value: Optional[str] = None
        # Per-job read cache: avoids hitting Supabase on every 2s poll.
        # Entries: {job_id: (expires_at, data)}. Invalidated on any write to that job.
        self._job_cache: Dict[str, tuple] = {}
        self._JOB_CACHE_TTL: float = 1.5  # seconds
        logger.info("Supabase client initialized")

    def resolve_single_tenant_api_key_for_studio(self) -> Optional[str]:
        """If exactly one active ``api_tenants`` row exists, return its ``api_key``.

        Used when a signed-in Studio user has no ``Authorization`` header and
        ``STUDIO_FALLBACK_API_KEY`` is unset. Requires ``SUPABASE_SERVICE_ROLE_KEY``.
        Cached ~60s. Returns ``None`` if zero or multiple active tenants.
        """
        now = time.time()
        if self._studio_single_tenant_cache_until > now:
            return self._studio_single_tenant_cache_value

        self._studio_single_tenant_cache_until = now + 60.0
        self._studio_single_tenant_cache_value = None

        if not self._admin_client:
            return None
        try:
            r = (
                self._admin_client.table("api_tenants")
                .select("api_key,is_active")
                .limit(25)
                .execute()
            )
            raw = r.data or []
            # Treat NULL/missing is_active as active (matches DB default); only explicit False is inactive.
            rows = [x for x in raw if x.get("is_active", True) is not False]
            if len(rows) != 1:
                if len(rows) > 1:
                    logger.info(
                        "Studio auto-tenant skipped: %s eligible api_tenants rows (need exactly 1, or set STUDIO_FALLBACK_API_KEY)",
                        len(rows),
                    )
                elif len(raw) == 0:
                    logger.info(
                        "Studio auto-tenant skipped: api_tenants is empty (insert a row with api_key, or set STUDIO_FALLBACK_API_KEY)"
                    )
                return None
            key = (rows[0].get("api_key") or "").strip()
            if not key:
                return None
            self._studio_single_tenant_cache_value = key
            return key
        except Exception as e:
            logger.warning("resolve_single_tenant_api_key_for_studio failed: %s", e)
            return None

    def fetch_tenant_row_by_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        """Load one api_tenants row by api_key.

        Uses the service-role client when configured so lookup still works if RLS
        was enabled on api_tenants (anon key would see zero rows → false Invalid API key).
        """
        key = (api_key or "").strip()
        if not key:
            return None
        db = self._admin_client or self.client
        try:
            r = (
                db.table("api_tenants")
                .select("id, name, api_key, is_active")
                .eq("api_key", key)
                .limit(1)
                .execute()
            )
            rows = r.data or []
            if not rows:
                return None
            return rows[0]
        except Exception as e:
            logger.warning("fetch_tenant_row_by_api_key failed: %s", e)
            return None

    def create_job(
        self,
        video_type: str,
        input_params: Dict[str, Any],
        customer_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        studio_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new job row, returns the full row."""
        data = {
            "video_type": video_type,
            "input_params": input_params,
            "status": "pending",
            "progress": 0,
            "current_step": "queued",
            "intermediates": {},
            "output": {},
        }
        if customer_id:
            data["customer_id"] = customer_id
        if tenant_id:
            data["tenant_id"] = tenant_id
        if user_id:
            data["user_id"] = user_id
        if studio_session_id:
            data["studio_session_id"] = studio_session_id

        result = self.client.table(self.table).insert(data).execute()
        row = result.data[0]
        logger.info(f"Created job {row['id']} (type={video_type}, tenant={tenant_id})")
        return row

    def studio_session_belongs_to_user(
        self, session_id: str, user_id: str
    ) -> bool:
        """Return True if user_sessions row exists for this id and user."""
        if not self._admin_client or not session_id or not user_id:
            return False
        try:
            r = (
                self._admin_client.table("user_sessions")
                .select("id")
                .eq("id", session_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            return bool(r.data)
        except Exception as e:
            logger.warning("studio_session_belongs_to_user failed: %s", e)
            return False

    def create_user_video(
        self,
        user_id: str,
        job_id: str,
        title: str,
        video_type: str,
        thumbnail_url: Optional[str],
        video_url: Optional[str],
        video_no_subs_url: Optional[str],
        session_id: Optional[str] = None,
        duration_s: Optional[float] = None,
    ) -> None:
        """Insert gallery row (service role; bypasses RLS)."""
        if not self._admin_client:
            logger.warning("create_user_video skipped: no service role client")
            return
        row = {
            "user_id": user_id,
            "job_id": job_id,
            "title": (title or "Video")[:500],
            "video_type": video_type,
            "thumbnail_url": thumbnail_url,
            "video_url": video_url,
            "video_no_subs_url": video_no_subs_url,
        }
        if session_id:
            row["session_id"] = session_id
        if duration_s is not None:
            row["duration_s"] = duration_s
        try:
            self._admin_client.table("user_videos").insert(row).execute()
            logger.info("Created user_videos row for job %s user %s", job_id, user_id)
        except Exception as e:
            logger.error("create_user_video failed: %s", e)

    # ------------------------------------------------------------------
    # Studio character library (service role; scoped by verified user_id)
    # ------------------------------------------------------------------

    def list_studio_characters(self, user_id: str) -> List[Dict[str, Any]]:
        """Return active character rows for a Supabase Auth user (newest first)."""
        uid = (user_id or "").strip()
        if not uid or not self._admin_client:
            return []
        try:
            r = (
                self._admin_client.table("studio_characters")
                .select("*")
                .eq("user_id", uid)
                .neq("status", "deleted")
                .order("updated_at", desc=True)
                .limit(200)
                .execute()
            )
            return list(r.data or [])
        except Exception as e:
            logger.warning("list_studio_characters failed: %s", e)
            return []

    def get_studio_character(self, user_id: str, character_id: str) -> Optional[Dict[str, Any]]:
        uid = (user_id or "").strip()
        cid = (character_id or "").strip()
        if not uid or not cid or not self._admin_client:
            return None
        try:
            r = (
                self._admin_client.table("studio_characters")
                .select("*")
                .eq("id", cid)
                .eq("user_id", uid)
                .limit(1)
                .execute()
            )
            row = (r.data or [None])[0]
            if not row or row.get("status") == "deleted":
                return None
            return row
        except Exception as e:
            logger.warning("get_studio_character failed: %s", e)
            return None

    def create_studio_character(self, user_id: str, row: Dict[str, Any]) -> tuple:
        """Insert a studio_characters row. Returns (row_dict_or_None, error_message_or_None)."""
        uid = (user_id or "").strip()
        if not uid or not self._admin_client:
            logger.warning("create_studio_character skipped: missing user_id or service role client")
            return None, "missing user_id or Supabase service role client"
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "id": str(uuid.uuid4()),
            "user_id": uid,
            "name": (row.get("name") or "Character")[:500],
            "source_type": (row.get("source_type") or "uploaded")[:64],
            "status": (row.get("status") or "active")[:32],
            "tags": row.get("tags") if row.get("tags") is not None else [],
            "thumbnail": row.get("thumbnail"),
            "reference_images": row.get("reference_images") if row.get("reference_images") is not None else [],
            "voice_reference": row.get("voice_reference"),
            "default_language": row.get("default_language"),
            "preferred_formats": row.get("preferred_formats") if row.get("preferred_formats") is not None else [],
            "character_dna": row.get("character_dna") if row.get("character_dna") is not None else {},
            "style_json": row.get("style_json") if row.get("style_json") is not None else {},
            "voice_profile": row.get("voice_profile") if row.get("voice_profile") is not None else {},
            "created_at": now,
            "updated_at": now,
        }
        new_id = data["id"]
        try:
            ins = self._admin_client.table("studio_characters").insert(data).execute()
            out = (ins.data or [None])[0] if ins else None
            # Some PostgREST / proxy setups return 201 with an empty body; treat as success and load by id.
            if not out:
                out = self.get_studio_character(uid, new_id)
            if not out:
                logger.error(
                    "create_studio_character: insert OK but no returned row and get_studio_character empty (id=%s)",
                    new_id,
                )
                return (
                    None,
                    "insert returned no row (check table studio_characters exists, migration 002_studio_characters.sql, and API logs)",
                )
            return out, None
        except Exception as e:
            logger.error("create_studio_character failed: %s", e, exc_info=True)
            err_text = str(e)[:800]
            # postgrest / httpx wrappers sometimes hide JSON error body
            details = getattr(e, "details", None) or getattr(e, "message", None)
            if details and str(details) not in err_text:
                err_text = (err_text + " | " + str(details))[:800]
            return None, err_text

    def update_studio_character(
        self, user_id: str, character_id: str, patch: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        uid = (user_id or "").strip()
        cid = (character_id or "").strip()
        if not uid or not cid or not self._admin_client or not patch:
            return None
        allowed = {
            "name",
            "status",
            "tags",
            "thumbnail",
            "reference_images",
            "voice_reference",
            "default_language",
            "preferred_formats",
            "character_dna",
            "style_json",
            "voice_profile",
            "last_used_at",
        }
        data = {k: v for k, v in patch.items() if k in allowed and v is not None}
        if not data:
            return self.get_studio_character(uid, cid)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            r = (
                self._admin_client.table("studio_characters")
                .update(data)
                .eq("id", cid)
                .eq("user_id", uid)
                .execute()
            )
            row = (r.data or [None])[0]
            return row or self.get_studio_character(uid, cid)
        except Exception as e:
            logger.error("update_studio_character failed: %s", e)
            return None

    def delete_studio_character(self, user_id: str, character_id: str) -> bool:
        """Soft-delete: status = deleted."""
        uid = (user_id or "").strip()
        cid = (character_id or "").strip()
        if not uid or not cid or not self._admin_client:
            return False
        try:
            self._admin_client.table("studio_characters").update(
                {
                    "status": "deleted",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", cid).eq("user_id", uid).execute()
            return True
        except Exception as e:
            logger.error("delete_studio_character failed: %s", e)
            return False

    def mark_processing(self, job_id: str) -> None:
        """Mark job as processing with started_at timestamp."""
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "status": "processing",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

    def update_progress(
        self,
        job_id: str,
        progress: int,
        current_step: str,
        intermediates: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update job progress (0-100), current step, and optionally merge intermediates."""
        data: Dict[str, Any] = {
            "progress": progress,
            "current_step": current_step,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if intermediates is not None:
            # Merge new intermediates with existing ones
            existing = self.get_job(job_id)
            if existing:
                merged = existing.get("intermediates", {})
                merged.update(intermediates)
                data["intermediates"] = merged
            else:
                data["intermediates"] = intermediates

        self._cache_invalidate(job_id)
        self.client.table(self.table).update(data).eq("id", job_id).execute()

    def merge_intermediates(self, job_id: str, data: Dict[str, Any]) -> None:
        """Merge keys into job intermediates without changing progress or current_step."""
        if not data:
            return
        existing = self.get_job(job_id)
        if not existing:
            return
        merged = dict(existing.get("intermediates") or {})
        merged.update(data)
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "intermediates": merged,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

    def merge_input_params(self, job_id: str, patch: Dict[str, Any]) -> None:
        """Shallow-merge keys into job input_params without changing progress or current_step."""
        if not patch:
            return
        existing = self.get_job(job_id)
        if not existing:
            return
        merged = dict(existing.get("input_params") or {})
        merged.update(patch)
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "input_params": merged,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

    def mark_completed(
        self,
        job_id: str,
        output: Dict[str, Any],
        intermediates: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark job as completed with output URLs."""
        data: Dict[str, Any] = {
            "status": "completed",
            "progress": 100,
            "current_step": "done",
            "output": output,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if intermediates is not None:
            data["intermediates"] = intermediates

        self._cache_invalidate(job_id)
        self.client.table(self.table).update(data).eq("id", job_id).execute()
        logger.info(f"Job {job_id} completed")

    def mark_failed(
        self,
        job_id: str,
        error: str,
        error_details: Optional[Dict[str, Any]] = None,
        failed_at_step: Optional[str] = None,
    ) -> None:
        """Mark job as failed with error info."""
        data: Dict[str, Any] = {
            "status": "failed",
            "current_step": "failed",
            "error": error,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if error_details:
            data["error_details"] = error_details
        if failed_at_step:
            data["failed_at_step"] = failed_at_step

        self._cache_invalidate(job_id)
        self.client.table(self.table).update(data).eq("id", job_id).execute()
        logger.error(f"Job {job_id} failed at step '{failed_at_step}': {error}")

    def mark_aborted(self, job_id: str) -> None:
        """Mark a job as aborted by the user."""
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "status": "aborted",
            "current_step": "aborted",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
        logger.info(f"Job {job_id} aborted by user")

    def mark_paused(self, job_id: str, current_step: Optional[str] = None) -> None:
        """Mark a job as paused. Preserves intermediates and progress.

        When the pipeline pauses at a gate, pass ``current_step`` (monolith step id) so resume
        inference (e.g. product video + animations_review) stays correct if a prior progress update failed.
        """
        data: Dict[str, Any] = {
            "status": "paused",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if current_step is not None and str(current_step).strip():
            data["current_step"] = str(current_step).strip()
        self._cache_invalidate(job_id)
        self.client.table(self.table).update(data).eq("id", job_id).execute()
        logger.info("Job %s marked paused%s", job_id, f" at step {data['current_step']}" if "current_step" in data else "")

    def mark_retrying(self, job_id: str, retry_count: int) -> None:
        """Reset a failed job back to processing for retry. Preserves intermediates."""
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "status": "processing",
            "error": None,
            "error_details": None,
            "failed_at_step": None,
            "completed_at": None,
            "retry_count": retry_count,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
        logger.info(f"Job {job_id} retrying (attempt {retry_count})")

    def _cache_get(self, job_id: str) -> Optional[Dict[str, Any]]:
        entry = self._job_cache.get(job_id)
        if entry and time.monotonic() < entry[0]:
            return entry[1]
        return None

    def _cache_set(self, job_id: str, data: Dict[str, Any]) -> None:
        self._job_cache[job_id] = (time.monotonic() + self._JOB_CACHE_TTL, data)

    def _cache_invalidate(self, job_id: str) -> None:
        self._job_cache.pop(job_id, None)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get a single job by ID. Results are cached for up to 1.5s to reduce Supabase round-trips."""
        cached = self._cache_get(job_id)
        if cached is not None:
            return cached
        try:
            result = self.client.table(self.table).select("*").eq("id", job_id).execute()
        except Exception as e:
            from fastapi import HTTPException
            logger.error("Supabase error fetching job %s: %s", job_id, e)
            raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please retry.")
        if result.data:
            self._cache_set(job_id, result.data[0])
            return result.data[0]
        return None

    def list_jobs(
        self,
        status: Optional[str] = None,
        customer_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List jobs with optional filters. When tenant_id is set, only that tenant's jobs are returned."""
        query = self.client.table(self.table).select("*")

        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        if status:
            query = query.eq("status", status)
        if customer_id:
            query = query.eq("customer_id", customer_id)

        query = query.order("created_at", desc=True).range(offset, offset + limit - 1)
        try:
            result = query.execute()
        except Exception as e:
            from fastapi import HTTPException
            logger.error("Supabase error listing jobs: %s", e)
            raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please retry.")
        return result.data

    def verify_job_ownership(self, job_id: str, tenant_id: str) -> Dict[str, Any]:
        """Get a job and verify it belongs to the tenant.

        Returns the job dict if ownership matches.
        Raises HTTPException(404) if not found or tenant mismatch
        (returning 404 instead of 403 prevents job ID enumeration).
        """
        from fastapi import HTTPException

        job = self.get_job(job_id)
        jt = job.get("tenant_id") if job else None
        # PostgREST may return UUID as string; Tenant.id is str — normalize so poll does not 500/404 mismatch.
        if not job or str(jt) != str(tenant_id):
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def update_cost(self, job_id: str, cost_usd: float) -> None:
        """Lightweight update of intermediates.cost_usd only.

        Uses PostgreSQL jsonb_set so no SELECT is needed — avoids an extra Supabase
        round-trip on every parallel scene generation cost tick.
        """
        self._cache_invalidate(job_id)
        try:
            self.client.rpc(
                "jsonb_set_cost",
                {"job_id": job_id, "cost": cost_usd},
            ).execute()
        except Exception:
            # RPC not available — fall back to read-modify-write
            existing = self.get_job(job_id)
            if not existing:
                return
            intermediates = dict(existing.get("intermediates") or {})
            intermediates["cost_usd"] = cost_usd
            self._cache_invalidate(job_id)
            self.client.table(self.table).update({
                "intermediates": intermediates,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()

    def update_step_timings(self, job_id: str, timings: List[Dict[str, Any]]) -> None:
        """Write step timing data to the step_timings column."""
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "step_timings": timings,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

    def count_active_jobs(self) -> int:
        """Count jobs currently processing."""
        result = (
            self.client.table(self.table)
            .select("id", count="exact")
            .eq("status", "processing")
            .execute()
        )
        return result.count or 0

    def store_usage(self, job_id: str, cost_summary: dict) -> None:
        """Insert a row into generation_usage with the full cost breakdown."""
        data = {
            "job_id": job_id,
            "total_cost": cost_summary.get("total_usd", 0),
            "pricing_version": cost_summary.get("pricing_version"),
            "total_input_tokens": cost_summary.get("total_input_tokens", 0),
            "total_output_tokens": cost_summary.get("total_output_tokens", 0),
            "total_video_seconds": cost_summary.get("total_video_seconds", 0),
            "total_image_count": cost_summary.get("total_image_count", 0),
            "breakdown": cost_summary.get("breakdown", {}),
            "entries": cost_summary.get("entries", []),
        }
        self.client.table("generation_usage").insert(data).execute()
        logger.info(f"Stored usage for job {job_id}: ${cost_summary.get('total_usd', 0):.4f}")

    # ------------------------------------------------------------------
    # Queue / rate-limit methods
    # ------------------------------------------------------------------
    _tenant_limits_cache: Dict[str, Any] = {}  # tenant_id -> (row_dict, expires_at)

    def get_tenant_limits(self, tenant_id: str) -> dict:
        """Get rate limit columns from api_tenants (cached 5 min)."""
        now = time.time()
        cached = self._tenant_limits_cache.get(tenant_id)
        if cached:
            row, expires_at = cached
            if now < expires_at:
                return row

        try:
            result = (
                self.client.table("api_tenants")
                .select("max_concurrent_jobs, max_concurrent_per_customer, max_queued_jobs")
                .eq("id", tenant_id)
                .execute()
            )
            row = result.data[0] if result.data else {}
        except Exception as e:
            logger.warning(f"Failed to fetch tenant limits for {tenant_id}: {e}")
            row = {}

        self._tenant_limits_cache[tenant_id] = (row, now + _TENANT_LIMITS_TTL)
        return row

    def count_active_jobs_for_tenant(self, tenant_id: str) -> int:
        """Count processing+pending jobs for a tenant."""
        result = (
            self.client.table(self.table)
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .in_("status", ["processing", "pending"])
            .execute()
        )
        return result.count or 0

    def count_active_jobs_for_customer(self, tenant_id: str, customer_id: str) -> int:
        """Count processing+pending jobs for a customer within a tenant."""
        result = (
            self.client.table(self.table)
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .eq("customer_id", customer_id)
            .in_("status", ["processing", "pending"])
            .execute()
        )
        return result.count or 0

    def count_queued_jobs_for_tenant(self, tenant_id: str) -> int:
        """Count queued jobs for a tenant (for queue depth check)."""
        result = (
            self.client.table(self.table)
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
            .eq("status", "queued")
            .execute()
        )
        return result.count or 0

    def get_next_queued_job(self, tenant_id: str) -> Optional[Dict]:
        """Get oldest queued job for a tenant (FIFO)."""
        result = (
            self.client.table(self.table)
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("status", "queued")
            .order("created_at")
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_tenants_with_queued_jobs(self) -> list:
        """Get distinct tenant_ids that have queued jobs."""
        result = (
            self.client.table(self.table)
            .select("tenant_id")
            .eq("status", "queued")
            .execute()
        )
        seen = set()
        out = []
        for r in (result.data or []):
            tid = r["tenant_id"]
            if tid not in seen:
                seen.add(tid)
                out.append(tid)
        return out

    def get_queue_position(self, job_id: str, tenant_id: str) -> int:
        """Get 1-based position of a job in the tenant's queue."""
        result = (
            self.client.table(self.table)
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("status", "queued")
            .order("created_at")
            .execute()
        )
        for i, row in enumerate(result.data or []):
            if row["id"] == job_id:
                return i + 1
        return 0

    def mark_queued(self, job_id: str) -> None:
        """Mark job as queued (accepted, waiting for a slot)."""
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "status": "queued",
            "current_step": "queued",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

    def mark_pending(self, job_id: str) -> None:
        """Transition queued → pending (about to submit to worker)."""
        self._cache_invalidate(job_id)
        self.client.table(self.table).update({
            "status": "pending",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
