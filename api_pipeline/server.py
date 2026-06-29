"""FastAPI server for the Video Generation API."""

import asyncio
import json
import math
import os
import logging
import mimetypes
import re
import requests as dl_requests
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv

# Load env: monolith defaults, optional cwd .env, then api_pipeline/.env (wins on conflicts).
# Without override on the last step, an empty SUPABASE_* in Comp_Videos/.env or a root .env
# could block keys from api_pipeline/.env and break Studio cloud sign-in.
_API_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_API_DIR)
_COMP_VIDEOS = os.path.join(_REPO_ROOT, "Comp_Videos")
_MONOLITH_ENV = os.path.join(_COMP_VIDEOS, ".env")
if os.path.isfile(_MONOLITH_ENV):
    load_dotenv(_MONOLITH_ENV)
load_dotenv()  # optional cwd .env (repo root when running uvicorn from there)
_env_file = os.path.join(_API_DIR, ".env")
if os.path.isfile(_env_file):
    load_dotenv(_env_file, override=True)

# Set service account path for monolith before any tvd_pipeline import (so Config finds it)
_SA_CANDIDATES = [
    os.path.join(_COMP_VIDEOS, "service_account.json"),
    os.path.join(_API_DIR, "service_account.json"),
]
for _p in _SA_CANDIDATES:
    if os.path.isfile(_p):
        _abs = os.path.abspath(_p)
        os.environ.setdefault("SERVICE_ACCOUNT_FILE", _abs)
        os.environ.setdefault("GCS_CREDENTIALS_FILE", _abs)
        os.environ.setdefault("GCS_UPLOAD_CREDENTIALS_FILE", _abs)
        # Also set GOOGLE_APPLICATION_CREDENTIALS so Google SDK libraries
        # (including Veo 3.1 google-genai and Vertex AI client libs) pick up
        # the service account automatically via ADC.
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _abs)
        break

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from api_pipeline.models import (
    GenerateVideoRequest,
    GenerateVideoResponse,
    GenerateMusicRequest,
    GenerateMusicResponse,
    GenerateSceneImageRequest,
    GenerateSceneImageResponse,
    AnimateSceneRequest,
    AnimateSceneResponse,
    GenerateCharacterRequest,
    GenerateCharacterResponse,
    SuggestCharacterBriefsRequest,
    SuggestCharacterBriefsResponse,
    CharacterRecord,
    CreateCharacterRequest,
    UpdateCharacterRequest,
    VoiceOption,
    GenerateVoRequest,
    GenerateVoResponse,
    VoiceDesignRequest,
    VoiceDesignResponse,
    VoiceDesignPreview,
    VoiceSaveRequest,
    VoiceSaveResponse,
    PatchIntermediatesRequest,
    PatchIntermediatesResponse,
    JobStatusResponse,
    JobListResponse,
    HealthResponse,
    ServiceHealthResponse,
    ServiceStatus,
)
from api_pipeline.auth import Tenant, init_auth, require_tenant, require_tenant_or_token
from api_pipeline.supabase_client import (
    SupabaseJobClient,
    is_supabase_env_configured,
    resolve_supabase_public_credentials,
)
from api_pipeline.event_store import event_store, fallback_store, fallback_handler
from api_pipeline.server_log_stream import (
    install_server_log_capture,
    get_recent_lines,
    register_subscriber,
    unregister_subscriber,
)
from api_pipeline.pipeline_runner import (
    ServiceRegistry,
    run_product_pipeline,
    run_influencer_pipeline,
    run_personal_brand_pipeline,
    run_ugc_real_pipeline,
    run_custom_pipeline,
    _is_transient_error,
    JobAbortedError,
    JobPausedError,
    get_steps_for_type,
    clear_intermediates_from_step,
    _cleanup_cost_tracking,
    PRODUCT_STEPS,
    INFLUENCER_STEPS,
    PERSONAL_BRAND_STEPS,
)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
# Monolith outbound calls (Gemini/Vertex, Kie, OpenAI, etc.) — search logs for [ExternalAPI]
logging.getLogger("tvd.external_api").setLevel(logging.INFO)


def _is_errno_22_style(exc: BaseException) -> bool:
    seen = set()
    chain: Optional[BaseException] = exc
    for _ in range(12):
        if chain is None or id(chain) in seen:
            break
        seen.add(id(chain))
        if isinstance(chain, OSError) and getattr(chain, "errno", None) == 22:
            return True
        s = str(chain)
        if "Errno 22" in s or "WinError 10022" in s:
            return True
        chain = getattr(chain, "__cause__", None) or getattr(chain, "__context__", None)
    return False


def _try_recover_product_phase1_errno22(
    job_id: str, video_type: str, params: dict, sb: SupabaseJobClient
) -> bool:
    """If Windows EINVAL killed the job after parse, mark paused when TEXT data exists."""
    if (video_type or "").lower() != "product video":
        return False
    if params.get("pause_after_step") != "step_1":
        return False
    try:
        job = sb.get_job(job_id)
    except Exception:
        return False
    if not job:
        return False
    pt = (job.get("intermediates") or {}).get("parsed_texts") or {}

    def _txt(v):
        if v is None:
            return ""
        return str(v).strip()

    if not any(_txt(pt.get(k)) for k in ("text_1", "text_2", "text_3")):
        return False
    try:
        sb.mark_paused(job_id)
        event_store.push(
            job_id,
            "SERVER",
            "Paused after parse (recovered from Windows I/O error) — review Preferences",
            progress=5,
            event_type="pause",
        )
        return True
    except Exception as ex:
        logger.warning("errno22 recovery mark_paused failed: %s", ex)
        return False
logging.getLogger().addHandler(fallback_handler)

# ---------------------------------------------------------------------------
# Globals (populated in lifespan)
# ---------------------------------------------------------------------------
services: Optional[ServiceRegistry] = None
supabase: Optional[SupabaseJobClient] = None
executor: Optional[ThreadPoolExecutor] = None

# Guard against duplicate resume/restart spawning two threads for the same job
_running_jobs_lock = threading.Lock()
_running_jobs: set = set()


def _try_claim_job(job_id: str) -> bool:
    """Try to claim a job for execution. Returns False if already running."""
    with _running_jobs_lock:
        if job_id in _running_jobs:
            return False
        _running_jobs.add(job_id)
        return True


def _release_job(job_id: str) -> None:
    """Release a job claim after execution completes."""
    with _running_jobs_lock:
        _running_jobs.discard(job_id)

HEALTH_CACHE_TTL = 300  # 5 minutes for /health/services
ACTIVE_JOBS_CACHE_TTL = 10  # 10 seconds for /health active_jobs count
_health_cache: Dict[str, Any] = {"result": None, "expires_at": 0}
_active_jobs_cache: Dict[str, Any] = {"count": 0, "expires_at": 0}

# ---------------------------------------------------------------------------
# Server config — loaded from config/server.json, env vars override
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config" / "server.json"

def load_server_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}

server_config: dict = load_server_config()

MAX_WORKERS = int(os.environ.get("MAX_PIPELINE_WORKERS", "0")) or server_config.get("max_workers", 20)

UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

JOB_ARTIFACTS_DIR = Path(__file__).parent / "job_artifacts"
JOB_ARTIFACTS_DIR.mkdir(exist_ok=True)
JOB_ARTIFACTS_RETENTION_DAYS = server_config.get("job_artifacts_retention_days", 7)

UPLOAD_MAX_SIZE = 50 * 1024 * 1024  # 50 MB
UPLOAD_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm"}


# ---------------------------------------------------------------------------
# Mux fallback background task — polls for stuck async uploads
# ---------------------------------------------------------------------------
MUX_API_URL = "https://api.mux.com/video/v1"


async def _mux_fallback_loop():
    """Safety-net loop that runs every 60s to catch Mux uploads the webhook missed."""
    while True:
        try:
            await asyncio.sleep(60)
            if not supabase or not services or not services.mux:
                continue

            # Find completed jobs with mux_status='uploading' older than 10 minutes
            ten_min_ago = datetime.now(timezone.utc).isoformat()
            try:
                result = (
                    supabase.client.table(supabase.table)
                    .select("id, output, input_params, completed_at")
                    .eq("status", "completed")
                    .execute()
                )
                rows = result.data or []
            except Exception:
                continue

            now = datetime.now(timezone.utc)
            for row in rows:
                try:
                    output = row.get("output") or {}
                    if output.get("mux_status") != "uploading":
                        continue

                    completed_at_str = row.get("completed_at")
                    if not completed_at_str:
                        continue

                    # Parse completed_at and check if >10 min ago
                    completed_at = datetime.fromisoformat(completed_at_str.replace("Z", "+00:00"))
                    age_minutes = (now - completed_at).total_seconds() / 60
                    if age_minutes < 10:
                        continue

                    job_id = row["id"]
                    upload_id = output.get("mux_upload_id")
                    if not upload_id:
                        continue

                    # Poll Mux API for upload status
                    resp = dl_requests.get(
                        f"{MUX_API_URL}/uploads/{upload_id}",
                        auth=services.mux.auth,
                        timeout=15,
                    )
                    if not resp.ok:
                        continue

                    upload_data = resp.json().get("data", {})
                    asset_id = upload_data.get("asset_id")

                    if asset_id:
                        # Check asset status
                        asset_resp = dl_requests.get(
                            f"{MUX_API_URL}/assets/{asset_id}",
                            auth=services.mux.auth,
                            timeout=15,
                        )
                        if not asset_resp.ok:
                            continue

                        asset_data = asset_resp.json().get("data", {})
                        asset_status = asset_data.get("status")

                        if asset_status == "ready":
                            # Check all static rendition files are ready (MP4 availability)
                            static_rend = asset_data.get("static_renditions") or {}
                            rend_files = static_rend.get("files", []) if isinstance(static_rend, dict) else []
                            if not (rend_files and all(f.get("status") == "ready" for f in rend_files)):
                                continue  # keep polling — asset streams but MP4 not ready yet

                            playback_ids = asset_data.get("playback_ids", [])
                            if playback_ids:
                                playback_id = playback_ids[0]["id"]
                                mp4_name = "highest.mp4"

                                output["mux_status"] = "ready"
                                output["final_asset_id"] = asset_id
                                output["final_playback_id"] = playback_id
                                output["final_stream_url"] = f"https://stream.mux.com/{playback_id}.m3u8"
                                output["final_mp4_url"] = f"https://stream.mux.com/{playback_id}/{mp4_name}"
                                supabase.client.table(supabase.table).update({
                                    "output": output,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                }).eq("id", job_id).execute()
                                logger.info(f"Mux fallback: job {job_id} asset ready — playback_id={playback_id}")

                        elif asset_status == "errored":
                            output["mux_status"] = "failed"
                            supabase.client.table(supabase.table).update({
                                "output": output,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }).eq("id", job_id).execute()
                            logger.warning(f"Mux fallback: job {job_id} asset errored")

                    # Timeout: >30 min and still no asset or asset not ready
                    elif age_minutes > 30:
                        output["mux_status"] = "timeout"
                        supabase.client.table(supabase.table).update({
                            "output": output,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }).eq("id", job_id).execute()
                        logger.warning(f"Mux fallback: job {job_id} timed out after {age_minutes:.0f}min")

                except Exception:
                    pass  # Per-row errors silently caught

        except asyncio.CancelledError:
            return
        except Exception:
            pass  # Entire loop iteration errors silently caught


# ---------------------------------------------------------------------------
# Queue drain background task — submits queued jobs when slots open
# ---------------------------------------------------------------------------
def get_tenant_limits(tenant_id: str) -> dict:
    """Get rate limits for a tenant. DB values override config defaults."""
    defaults = server_config.get("defaults", {})
    row = supabase.get_tenant_limits(tenant_id)
    return {
        "max_concurrent_jobs": row.get("max_concurrent_jobs") or defaults.get("max_concurrent_jobs", 10),
        "max_concurrent_per_customer": row.get("max_concurrent_per_customer") or defaults.get("max_concurrent_per_customer", 5),
        "max_queued_jobs": row.get("max_queued_jobs") or defaults.get("max_queued_jobs", 50),
    }


async def _queue_drain_loop():
    """Periodically submit queued jobs when slots become available.

    Round-robin across tenants for fairness — one job per tenant per cycle.
    Queued jobs are persisted in Supabase, so they survive server restarts.
    Uses adaptive backoff: polls every 15s when queue is active, backs off to
    120s after 4 consecutive empty cycles to avoid hammering Supabase at idle.
    """
    base_interval = server_config.get("queue_drain_interval_seconds", 15)
    max_idle_interval = server_config.get("queue_drain_idle_interval_seconds", 120)
    idle_backoff_after = 4  # empty cycles before slowing down
    consecutive_empty = 0
    while True:
        try:
            interval = base_interval if consecutive_empty < idle_backoff_after else max_idle_interval
            await asyncio.sleep(interval)
            if not supabase or not executor:
                continue

            tenant_ids = supabase.get_tenants_with_queued_jobs()
            if not tenant_ids:
                consecutive_empty += 1
                continue

            consecutive_empty = 0
            for tenant_id in tenant_ids:
                try:
                    limits = get_tenant_limits(tenant_id)

                    tenant_active = supabase.count_active_jobs_for_tenant(tenant_id)
                    if tenant_active >= limits["max_concurrent_jobs"]:
                        continue

                    job = supabase.get_next_queued_job(tenant_id)
                    if not job:
                        continue

                    # Check per-customer limit
                    cid = job.get("customer_id")
                    if cid:
                        cust_active = supabase.count_active_jobs_for_customer(tenant_id, cid)
                        if cust_active >= limits["max_concurrent_per_customer"]:
                            continue

                    supabase.mark_pending(job["id"])
                    vt = job["video_type"]
                    params = job.get("input_params", {})
                    executor.submit(_run_job, job["id"], vt, params)
                    logger.info(f"Queue drain: submitted job {job['id']} (tenant={tenant_id})")
                except Exception as inner_err:
                    logger.warning(f"Queue drain error for tenant {tenant_id}: {inner_err}")

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"Queue drain loop error: {e}")


# ---------------------------------------------------------------------------
# Periodic artifact cleanup — removes old job folders daily
# ---------------------------------------------------------------------------
async def _artifact_cleanup_loop():
    """Periodically clean up old job artifact folders."""
    import shutil
    check_interval = 24 * 60 * 60  # check once per day
    while True:
        await asyncio.sleep(check_interval)
        try:
            now = time.time()
            max_age = JOB_ARTIFACTS_RETENTION_DAYS * 24 * 60 * 60
            count = 0
            for d in JOB_ARTIFACTS_DIR.iterdir():
                if d.is_dir() and (now - d.stat().st_mtime) > max_age:
                    shutil.rmtree(d, ignore_errors=True)
                    count += 1
            if count:
                logger.info(f"Artifact cleanup: removed {count} folder(s) older than {JOB_ARTIFACTS_RETENTION_DAYS} days")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Artifact cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Lifespan — init services on startup, cleanup on shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global services, supabase, executor
    logger.info("Starting up — initializing services...")
    try:
        install_server_log_capture()
        services = ServiceRegistry()
        if is_supabase_env_configured():
            supabase = SupabaseJobClient()
            init_auth(supabase)
        else:
            from api_pipeline.in_memory_job_store import InMemoryJobClient
            supabase = InMemoryJobClient()
            init_auth(None)  # auth accepts any API key when _supabase is None
            _u, _k = resolve_supabase_public_credentials()
            logger.warning(
                "Supabase not configured — using in-memory job store (no cloud jobs / Studio sign-in). "
                "Set SUPABASE_URL and SUPABASE_ANON_KEY or SUPABASE_PUBLISHABLE_KEY in api_pipeline/.env "
                "(Dashboard → Settings → API). URL set=%s, public key set=%s.",
                bool(_u),
                bool(_k),
            )
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        logger.info(f"Ready — {MAX_WORKERS} pipeline workers")

        # Stale job recovery — any job left as "processing" from a previous server run has no live
        # thread. Auto-resume by re-submitting to the executor; the pipeline's checkpoint mechanism
        # picks up from saved intermediates so the user never has to click Resume manually.
        # If submitting fails we fall back to marking paused so the job isn't lost.
        try:
            stale = (
                supabase.client.table(supabase.table)
                .select("id,video_type,input_params,current_step")
                .eq("status", "processing")
                .execute()
            )
            stale_rows = stale.data or []
            if stale_rows:
                logger.warning(
                    "Auto-resuming %d stale 'processing' job(s): %s",
                    len(stale_rows),
                    [r["id"] for r in stale_rows],
                )
                for r in stale_rows:
                    jid = r["id"]
                    try:
                        vt = r.get("video_type") or ""
                        params = dict(r.get("input_params") or {})
                        executor.submit(_run_job, jid, vt, params)
                        event_store.push(
                            jid,
                            "SERVER",
                            f"Auto-resumed after server restart (was at step={r.get('current_step') or '?'})",
                            event_type="start",
                        )
                    except Exception as _resume_err:
                        logger.warning(
                            "Auto-resume submit failed for %s (%s) — marking paused for manual recovery",
                            jid,
                            _resume_err,
                        )
                        try:
                            supabase.mark_paused(jid, current_step=r.get("current_step"))
                        except Exception:
                            pass
        except Exception as _stale_err:
            logger.warning("Stale job recovery failed (non-fatal): %s", _stale_err)

        # Orphan upload cleanup — delete files older than 24 hours
        try:
            now = time.time()
            max_age = 24 * 60 * 60  # 24 hours in seconds
            orphan_count = 0
            for f in UPLOADS_DIR.iterdir():
                if f.is_file() and (now - f.stat().st_mtime) > max_age:
                    f.unlink(missing_ok=True)
                    orphan_count += 1
            if orphan_count:
                logger.info(f"Cleaned up {orphan_count} orphaned upload(s) older than 24h")
        except Exception as cleanup_err:
            logger.warning(f"Upload orphan cleanup failed: {cleanup_err}")

        # Job artifact cleanup — delete folders older than retention period
        try:
            artifact_count = 0
            max_artifact_age = JOB_ARTIFACTS_RETENTION_DAYS * 24 * 60 * 60
            for d in JOB_ARTIFACTS_DIR.iterdir():
                if d.is_dir() and (now - d.stat().st_mtime) > max_artifact_age:
                    import shutil
                    shutil.rmtree(d, ignore_errors=True)
                    artifact_count += 1
            if artifact_count:
                logger.info(f"Cleaned up {artifact_count} artifact folder(s) older than {JOB_ARTIFACTS_RETENTION_DAYS} days")
        except Exception as artifact_err:
            logger.warning(f"Artifact cleanup failed: {artifact_err}")

        # Mux fallback background task — polls for stuck uploads
        mux_fallback_task = asyncio.create_task(_mux_fallback_loop())

        # Queue drain background task — submits queued jobs when slots open
        queue_drain_task = asyncio.create_task(_queue_drain_loop())

        # Periodic artifact cleanup — removes old job folders daily
        artifact_cleanup_task = asyncio.create_task(_artifact_cleanup_loop())

    except Exception as e:
        logger.error(f"Startup failed: {e}")
        raise
    yield
    # Shutdown
    mux_fallback_task.cancel()
    queue_drain_task.cancel()
    artifact_cleanup_task.cancel()
    if executor:
        executor.shutdown(wait=False)
    logger.info("Shut down")


app = FastAPI(
    title="Video Generation API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request logging middleware — log every API call so you can see what happens
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request, call_next):
    import time as _t
    method = request.method
    path = request.url.path
    query = str(request.url.query) if request.url.query else ""
    cl = request.headers.get("content-length", "")
    start = _t.perf_counter()
    logger.info("[API] %s %s%s%s", method, path, "?" + query if query else "", " body=" + cl + "b" if cl else "")
    response = await call_next(request)
    elapsed = _t.perf_counter() - start
    logger.info("[API] %s %s -> %s %.2fs", method, path, response.status_code, elapsed)
    return response


from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder


@app.exception_handler(RequestValidationError)
async def _log_422_detail(request, exc: RequestValidationError):
    # Default FastAPI 422 swallows the detail in server logs — make it visible.
    try:
        errors = exc.errors()
    except Exception:
        errors = []
    summary = "; ".join(
        f"{'.'.join(str(p) for p in (e.get('loc') or []))}: {e.get('msg', '')}"
        for e in errors
    ) or str(exc)
    logger.warning("[422] %s %s — %s", request.method, request.url.path, summary)
    # jsonable_encoder handles ValueError/Exception objects in ctx fields.
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _json_dict_or_empty(v: Any) -> dict:
    """Supabase JSONB columns may be NULL; dict.get(key, {}) still returns None if key exists with null."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    return {}


def _json_list_of_dicts(v: Any) -> list:
    """step_timings must be a list for JobStatusResponse; DB NULL or wrong shape used to 500 GET /api/jobs."""
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


def _sanitize_json_floats(v: Any) -> Any:
    """Replace NaN/Inf in nested structures — json.dumps emits invalid JSON for them and json.loads then raises."""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, dict):
        return {k: _sanitize_json_floats(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_sanitize_json_floats(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_sanitize_json_floats(x) for x in v)
    return v


def _json_response_safe(v: Any, fallback: Any):
    """Deep-normalize JSONB / nested values so FastAPI can encode GET /api/jobs (UUID/datetime/Decimal)."""
    try:
        v = _sanitize_json_floats(v)
        out = json.loads(json.dumps(v, default=str))
        return out
    except (TypeError, ValueError) as ex:
        logger.warning("JSON response coerce failed (%s), using fallback", ex)
        return fallback


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float_opt(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _job_to_response(j: dict) -> JobStatusResponse:
    """Convert a Supabase job row dict to a JobStatusResponse."""
    out = _json_response_safe(_json_dict_or_empty(j.get("output")), {})
    im = _json_response_safe(_json_dict_or_empty(j.get("intermediates")), {})
    params = _json_response_safe(_json_dict_or_empty(j.get("input_params")), {})
    if not isinstance(out, dict):
        out = {}
    if not isinstance(im, dict):
        im = {}
    if not isinstance(params, dict):
        params = {}

    err_det = j.get("error_details")
    if err_det is not None and not isinstance(err_det, dict):
        err_det = None
    elif isinstance(err_det, dict) and err_det:
        err_det = _json_response_safe(err_det, err_det)
        if not isinstance(err_det, dict):
            err_det = None

    st_raw = _json_list_of_dicts(j.get("step_timings"))
    st = _json_response_safe(st_raw, st_raw)
    if not isinstance(st, list):
        st = []
    st = [x for x in st if isinstance(x, dict)]

    err_msg = j.get("error")
    if err_msg is not None and not isinstance(err_msg, str):
        err_msg = str(err_msg)

    cust = j.get("customer_id")
    if cust is not None and not isinstance(cust, str):
        cust = str(cust)

    failed_step = j.get("failed_at_step")
    if failed_step is not None and not isinstance(failed_step, str):
        failed_step = str(failed_step)

    jid = j.get("id")
    if jid is None:
        logger.error("Job row missing id: keys=%s", list(j.keys())[:20])
        raise HTTPException(status_code=500, detail="Corrupt job row (missing id)")

    c_out = _safe_float_opt(out.get("cost_usd"))
    c_im = _safe_float_opt(im.get("cost_usd"))
    cost_usd = c_out if c_out is not None else c_im
    return JobStatusResponse(
        id=str(jid),
        customer_id=cust,
        status=str(j.get("status") or "unknown"),
        video_type=str(j.get("video_type") or ""),
        progress=_safe_int(j.get("progress"), 0),
        current_step=str(j.get("current_step") or "unknown"),
        input_params=params,
        intermediates=im,
        output=out,
        error=err_msg,
        error_details=err_det,
        retry_count=_safe_int(j.get("retry_count"), 0),
        max_retries=_safe_int(j.get("max_retries"), 3),
        failed_at_step=failed_step,
        step_timings=st,
        cost_usd=cost_usd,
        created_at=str(j["created_at"]) if j.get("created_at") else None,
        started_at=str(j["started_at"]) if j.get("started_at") else None,
        completed_at=str(j["completed_at"]) if j.get("completed_at") else None,
        updated_at=str(j["updated_at"]) if j.get("updated_at") else None,
    )


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------
def _cleanup_uploaded_files(job_id: str, params: dict):
    """Delete uploaded files referenced in a job's input params (local + GCS)."""
    url_fields = ["character_url", "logo_url", "video_reference_url"]
    list_fields = ["product_image_urls", "reference_image_urls", "asset_urls"]
    urls = []
    for f in url_fields:
        v = params.get(f)
        if v:
            urls.append(v)
    for f in list_fields:
        v = params.get(f)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    u = item.get("url")
                    if u:
                        urls.append(u)
                elif isinstance(item, str):
                    urls.append(item)

    gcs_marker = "storage.googleapis.com/automatiq/uploads/"
    local_marker = "/api/uploads/"

    for url in urls:
        # Clean up GCS object
        if gcs_marker in url:
            blob_path = "uploads/" + url.split(gcs_marker)[-1].split("?")[0]
            try:
                if services and services.gcs_storage and services.gcs_storage._initialized:
                    blob = services.gcs_storage.bucket.blob(blob_path)
                    blob.delete()
                    logger.info(f"Job {job_id} cleaned up GCS upload: {blob_path}")
            except Exception as del_err:
                logger.warning(f"Job {job_id} failed to delete GCS upload {blob_path}: {del_err}")
            # Also delete the local cache copy (filename is last part of blob_path)
            filename = blob_path.split("/")[-1]
            local_path = UPLOADS_DIR / filename
            try:
                if local_path.exists():
                    local_path.unlink()
                    logger.info(f"Job {job_id} cleaned up local cache: {filename}")
            except Exception:
                pass
        # Clean up local-only uploads (fallback case)
        elif local_marker in url:
            filename = url.split(local_marker)[-1].split("?")[0]
            fpath = UPLOADS_DIR / filename
            try:
                if fpath.exists():
                    fpath.unlink()
                    logger.info(f"Job {job_id} cleaned up uploaded file: {filename}")
            except Exception as del_err:
                logger.warning(f"Job {job_id} failed to delete upload {filename}: {del_err}")


def _run_job(job_id: str, video_type: str, params: dict):
    """Runs in a thread — executes the pipeline and updates Supabase.

    On transient failures, auto-retries up to max_retries with exponential
    backoff. The pipeline resumes from checkpoints saved in intermediates.
    """
    logger.info(
        "[RUN_JOB] job_id=%s video_type=%s pause_after_step=%s simulation=%s",
        job_id, video_type, params.get("pause_after_step") or "(none)", params.get("simulation", False),
    )
    if not _try_claim_job(job_id):
        logger.warning(f"Job {job_id} is already running — skipping duplicate submission")
        return

    _needs_upload_cleanup = True
    try:
        # Only mark processing if not already set (resume/restart set it before submitting)
        job_state = supabase.get_job(job_id)
        if not job_state or job_state.get("status") != "processing":
            supabase.mark_processing(job_id)

        # Retry loop (replaces recursive _run_job calls)
        while True:
            try:
                # Simulation mode: use mock services (Type 1) or real services with simulation flag (Type 2)
                sim_type = params.get("simulation_type", "wrapper")
                if params.get("is_simulation") and sim_type == "wrapper":
                    from api_pipeline.services.simulation import SimServiceRegistry
                    job_services = SimServiceRegistry(
                        simulation_duration=params.get("simulation_duration", "none"),
                        job_id=job_id,
                        supabase=supabase,
                        video_type=video_type,
                    )
                else:
                    job_services = services

                if video_type == "product video":
                    output = run_product_pipeline(job_id, params, job_services, supabase)
                elif video_type == "influencer":
                    output = run_influencer_pipeline(job_id, params, job_services, supabase)
                elif video_type == "personal-brand":
                    output = run_personal_brand_pipeline(job_id, params, job_services, supabase)
                elif video_type == "ugc-real":
                    output = run_ugc_real_pipeline(job_id, params, job_services, supabase)
                elif video_type == "custom":
                    output = run_custom_pipeline(job_id, params, job_services, supabase)
                else:
                    raise ValueError(f"Unsupported video type: {video_type}")

                # --- Success path ---
                fb_logs = fallback_store.get_logs(job_id)
                if fb_logs:
                    output["fallback_logs"] = fb_logs

                supabase.mark_completed(job_id, output)

                # Download final video into job_artifacts/{job_id}/ (skip for simulation — placeholder URLs)
                final_url = output.get("final_mp4_url") if not params.get("is_simulation") else None
                if final_url:
                    try:
                        short_id = job_id[:8]
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        if video_type == "product video":
                            vt_tag = "product"
                        elif video_type == "influencer":
                            vt_tag = "influencer"
                        elif video_type == "personal-brand":
                            vt_tag = "personal_brand"
                        else:
                            vt_tag = "ugc_real"
                        filename = f"{vt_tag}_{ts}_{short_id}.mp4"
                        artifacts_dir = JOB_ARTIFACTS_DIR / job_id
                        artifacts_dir.mkdir(parents=True, exist_ok=True)
                        output["artifacts_folder"] = str(artifacts_dir)
                        local_path = artifacts_dir / filename
                        resp = dl_requests.get(final_url, timeout=120)
                        resp.raise_for_status()
                        local_path.write_bytes(resp.content)
                        output["local_path"] = str(local_path)
                        supabase.mark_completed(job_id, output)
                        logger.info(f"Job {job_id} final video saved to {local_path} ({len(resp.content)} bytes)")
                        event_store.push(job_id, "SERVER", f"Video saved to {filename}", event_type="info")
                    except Exception as dl_err:
                        logger.warning(f"Job {job_id} video download failed: {dl_err}")
                        event_store.push(job_id, "SERVER", f"Local download failed: {dl_err}", event_type="warn")

                event_store.push(job_id, "SERVER", "Job completed successfully", progress=100, event_type="complete")
                logger.info(f"Job {job_id} completed successfully")

                try:
                    jr = supabase.get_job(job_id)
                    uid = (jr or {}).get("user_id")
                    if uid and callable(getattr(supabase, "create_user_video", None)):
                        im = (jr or {}).get("intermediates") or {}
                        out = (jr or {}).get("output") or {}
                        scene_imgs = im.get("scene_images") or []
                        thumb = None
                        for s in scene_imgs:
                            if s and isinstance(s, str) and len(str(s).strip()) > 5:
                                thumb = str(s).strip()
                                break
                        title = (params.get("prompt") or "Untitled video")[:80]
                        vurl = out.get("final_mp4_url") or out.get("subtitled_video_url")
                        vno = out.get("video_before_subtitles_url") or out.get("rendi_scene_voice_url")
                        if not vurl and not vno:
                            logger.warning(
                                "user_videos for job %s: no final_mp4_url / subtitled_video_url on output — "
                                "gallery row may have empty player links (keys present: %s)",
                                job_id,
                                list(out.keys())[:25],
                            )
                        supabase.create_user_video(
                            user_id=uid,
                            job_id=job_id,
                            title=title,
                            video_type=video_type,
                            thumbnail_url=thumb,
                            video_url=vurl,
                            video_no_subs_url=vno,
                            session_id=(jr or {}).get("studio_session_id"),
                            duration_s=float(params.get("duration") or 0) or None,
                        )
                    elif not uid:
                        logger.warning(
                            "user_videos skipped for job %s: job has no user_id. "
                            "Sign in via Video Studio (Account) before starting generation so the browser "
                            "sends X-Studio-User-Token on POST /api/generate; otherwise My videos stays empty.",
                            job_id,
                        )
                except Exception as uv_err:
                    logger.warning("user_videos insert skipped: %s", uv_err)

                break  # exit retry loop

            except (JobAbortedError, JobPausedError):
                raise  # propagate to outer handler

            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"Job {job_id} failed: {e}\n{tb}")

                if _is_errno_22_style(e) and _try_recover_product_phase1_errno22(
                    job_id, video_type, params, supabase
                ):
                    logger.info(
                        "Job %s: Errno 22 after product parse — marked paused (parsed_texts present)",
                        job_id,
                    )
                    raise JobPausedError(f"Recovered Errno 22 for job {job_id}")

                job = supabase.get_job(job_id)
                current_step = job.get("current_step", "unknown") if job else "unknown"
                retry_count = job.get("retry_count", 0) if job else 0
                max_retries = job.get("max_retries", 3) if job else 3

                if _is_transient_error(e) and retry_count < max_retries:
                    new_retry = retry_count + 1
                    backoff = 30 * (2 ** retry_count)  # 30s, 60s, 120s
                    logger.info(f"Job {job_id} transient error at '{current_step}', auto-retrying in {backoff}s (attempt {new_retry}/{max_retries})")
                    event_store.push(job_id, "SERVER", f"Transient error at '{current_step}', retrying in {backoff}s (attempt {new_retry}/{max_retries})", event_type="warn")
                    time.sleep(backoff)
                    supabase.mark_retrying(job_id, new_retry)
                    continue  # retry loop
                else:
                    fb_logs = fallback_store.get_logs(job_id)
                    error_details = {"traceback": tb}
                    if fb_logs:
                        error_details["fallback_logs"] = fb_logs
                    supabase.mark_failed(
                        job_id,
                        error=str(e),
                        error_details=error_details,
                        failed_at_step=current_step,
                    )
                    event_store.push(job_id, "SERVER", f"Job failed at '{current_step}': {str(e)[:200]}", progress=-1, event_type="error")
                    break  # exit retry loop

    except JobAbortedError:
        event_store.push(job_id, "SERVER", "Job aborted by user", progress=-1, event_type="abort")
        logger.info(f"Job {job_id} aborted by user")
        return

    except JobPausedError as e:
        try:
            jr = supabase.get_job(job_id)
            st = (jr or {}).get("status")
            if jr and st not in ("paused", "completed", "failed", "aborted"):
                pstep = getattr(e, "pause_monolith_step", None) or (jr.get("current_step") or "").strip()
                if not pstep:
                    pstep = "animations_review"
                for attempt in range(5):
                    try:
                        supabase.mark_paused(job_id, pstep)
                        logger.warning(
                            "[PAUSED] job %s DB was %s after pause gate — normalized to paused (%s)",
                            job_id,
                            st,
                            pstep,
                        )
                        break
                    except Exception as mp_ex:
                        if attempt == 4:
                            logger.error("[PAUSED] job %s mark_paused normalize failed: %s", job_id, mp_ex)
                        time.sleep(0.35 * (attempt + 1))
        except Exception as norm_ex:
            logger.warning("[PAUSED] job %s could not normalize status: %s", job_id, norm_ex)
        job = supabase.get_job(job_id)
        if job and job.get("status") == "paused":
            event_store.push(job_id, "SERVER", "Job paused by user — resume when ready", progress=-1, event_type="pause")
        logger.info("[PAUSED] Pipeline stopped for job %s (step-by-step). No further steps will run until Resume. %s", job_id, str(e))
        _needs_upload_cleanup = False
        return

    finally:
        _cleanup_cost_tracking(job_id)
        _release_job(job_id)
        if _needs_upload_cleanup:
            try:
                _cleanup_uploaded_files(job_id, params)
            except Exception as cleanup_err:
                logger.warning(f"Job {job_id} upload cleanup failed: {cleanup_err}")


# ---------------------------------------------------------------------------
# File upload endpoints
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def upload_file(
    file: UploadFile,
    classify: bool = False,
    slot: str = "",
    tenant: Tenant = Depends(require_tenant),
):
    """Upload a file for use in a video generation job.

    Returns a public GCS URL that can be used in product_image_urls,
    reference_image_urls, asset_urls, character_url, logo_url, or
    video_reference_url fields. Files are uploaded to GCS so that external
    APIs (Kie.ai, etc.) can access them directly.
    Uploaded files are auto-deleted after the job completes, fails, or is aborted.

    Optional ``?classify=true`` runs a cheap Gemini Vision classifier and
    returns ``classification = {type, confidence, reason}``. When ``slot`` is
    also given (e.g. ``uploads_character``), the response includes a
    ``warning`` field when the upload doesn't match the expected asset type.
    """
    # Validate extension
    ext = Path(file.filename or "").suffix.lower()
    if ext not in UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(UPLOAD_ALLOWED_EXTENSIONS))}",
        )

    # Read file content with size check
    content = await file.read()
    if len(content) > UPLOAD_MAX_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(content) / 1024 / 1024:.1f} MB). Maximum: {UPLOAD_MAX_SIZE / 1024 / 1024:.0f} MB",
        )

    # Generate unique filename
    safe_name = Path(file.filename or "upload").name.replace(" ", "_")
    unique_name = f"{uuid.uuid4().hex[:12]}_{safe_name}"

    # Save locally (cache / fallback serving)
    dest = UPLOADS_DIR / unique_name
    dest.write_bytes(content)

    # Upload to GCS for a publicly-accessible URL
    gcs_url = None
    if services and services.gcs_storage and services.gcs_storage._initialized:
        try:
            content_type = mimetypes.guess_type(unique_name)[0] or "application/octet-stream"
            blob_path = f"uploads/{unique_name}"
            gcs_svc = services.gcs_storage
            blob = gcs_svc.bucket.blob(blob_path)
            blob.upload_from_string(content, content_type=content_type)
            try:
                blob.make_public()
            except Exception:
                pass  # bucket may already be public
            gcs_url = f"https://storage.googleapis.com/{gcs_svc.bucket_name}/{blob_path}"
            logger.info(f"Uploaded file to GCS: {gcs_url} ({len(content)} bytes)")
        except Exception as gcs_err:
            logger.warning(f"GCS upload failed, falling back to local URL: {gcs_err}")

    final_url = gcs_url if gcs_url else f"/api/uploads/{unique_name}"
    if not gcs_url:
        logger.info(f"Uploaded file locally: {unique_name} ({len(content)} bytes)")

    response = {"url": final_url, "filename": unique_name}

    # P0.2 / F: optional Gemini Vision classification of the upload.
    # Only triggers when the caller passed ?classify=true. We classify only
    # images (videos return type=video without a Vertex call). Surface any
    # slot-mismatch warning so the UI can confirm before storing.
    if classify and gcs_url:  # only GCS URLs are publicly reachable for classification
        try:
            from api_pipeline.asset_classifier import classify_asset, slot_mismatch_warning
            classification = classify_asset(final_url)
            response["classification"] = classification
            if slot:
                warning = slot_mismatch_warning(slot, classification)
                if warning:
                    response["warning"] = warning
        except Exception as cls_err:
            logger.warning("Asset classification failed (non-fatal): %s", cls_err)

    return response


@app.get("/api/uploads/{filename}")
async def serve_upload(filename: str):
    """Serve an uploaded file."""
    fpath = UPLOADS_DIR / filename
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Prevent path traversal
    if fpath.resolve().parent != UPLOADS_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename")

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(fpath, media_type=media_type)


@app.delete("/api/upload")
async def delete_upload(url: str, tenant: Tenant = Depends(require_tenant)):
    """Delete a previously uploaded file from both local storage and GCS."""
    gcs_marker = "storage.googleapis.com/automatiq/uploads/"
    local_marker = "/api/uploads/"

    filename = None
    if gcs_marker in url:
        filename = url.split(gcs_marker)[-1].split("?")[0]
        blob_path = f"uploads/{filename}"
        try:
            if services and services.gcs_storage and services.gcs_storage._initialized:
                blob = services.gcs_storage.bucket.blob(blob_path)
                blob.delete()
                logger.info(f"Deleted GCS upload: {blob_path}")
        except Exception as e:
            logger.warning(f"Failed to delete GCS upload {blob_path}: {e}")
    elif local_marker in url:
        filename = url.split(local_marker)[-1].split("?")[0]
    else:
        raise HTTPException(status_code=400, detail="URL is not a recognized upload")

    if filename:
        # Prevent path traversal
        local_path = UPLOADS_DIR / filename
        if local_path.resolve().parent != UPLOADS_DIR.resolve():
            raise HTTPException(status_code=400, detail="Invalid filename")
        try:
            if local_path.exists():
                local_path.unlink()
                logger.info(f"Deleted local upload: {filename}")
        except Exception as e:
            logger.warning(f"Failed to delete local upload {filename}: {e}")

    return {"ok": True}


@app.get("/api/jobs/{job_id}/artifacts/{filename}")
async def serve_artifact(job_id: str, filename: str, tenant: Tenant = Depends(require_tenant)):
    """Serve a job artifact file (persisted image/video)."""
    if supabase:
        supabase.verify_job_ownership(job_id, tenant.id)

    artifacts_dir = JOB_ARTIFACTS_DIR / job_id
    fpath = artifacts_dir / filename
    if not fpath.exists() or not fpath.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Prevent path traversal
    if fpath.resolve().parent != artifacts_dir.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename")

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(fpath, media_type=media_type)


def _get_studio_user_id_from_request(request: Request) -> Optional[str]:
    """Validate Supabase access token from Video Studio; returns auth user id or None."""
    from api_pipeline.studio_auth import get_supabase_user_id_from_access_token

    token = (request.headers.get("X-Studio-User-Token") or "").strip()
    if not token:
        return None
    return get_supabase_user_id_from_access_token(token)


def require_studio_authenticated_user(request: Request) -> str:
    """Require a valid Studio Supabase JWT (character library and other per-user features)."""
    uid = _get_studio_user_id_from_request(request)
    if not uid:
        raise HTTPException(
            status_code=401,
            detail="Sign in via Video Studio (Account) so the server receives X-Studio-User-Token, or use endpoints without the character library.",
        )
    return uid


def _character_row_to_record(row: Dict[str, Any]) -> CharacterRecord:
    """Map a studio_characters DB row to CharacterRecord."""

    def _as_str_list(key: str) -> List[str]:
        v = row.get(key)
        if isinstance(v, list):
            return [str(x) for x in v if x is not None]
        return []

    def _as_dict(key: str) -> Dict[str, Any]:
        v = row.get(key)
        return dict(v) if isinstance(v, dict) else {}

    return CharacterRecord(
        character_id=str(row.get("id", "")),
        user_id=str(row.get("user_id", "")),
        name=str(row.get("name") or ""),
        source_type=str(row.get("source_type") or "uploaded"),
        status=str(row.get("status") or "active"),
        tags=_as_str_list("tags"),
        thumbnail=(str(row["thumbnail"]).strip() if row.get("thumbnail") else None),
        reference_images=_as_str_list("reference_images"),
        voice_reference=(str(row["voice_reference"]).strip() if row.get("voice_reference") else None),
        default_language=(str(row["default_language"]).strip() if row.get("default_language") else None),
        preferred_formats=_as_str_list("preferred_formats"),
        character_dna=_as_dict("character_dna"),
        style_json=_as_dict("style_json"),
        voice_profile=_as_dict("voice_profile"),
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/api/generate", response_model=GenerateVideoResponse)
async def generate_video(
    req: GenerateVideoRequest,
    request: Request,
    tenant: Tenant = Depends(require_tenant),
):
    """Submit a video generation job. Returns immediately with job_id."""
    if not supabase or not executor:
        raise HTTPException(status_code=503, detail="Services not initialized")
    if not req.simulation and not services:
        raise HTTPException(status_code=503, detail="Services not initialized")

    # Build params dict
    params = req.model_dump(exclude_none=True)
    if req.simulation:
        params["is_simulation"] = True
        params["simulation_type"] = req.simulation_type  # "wrapper" or "monolith"

    # ── Input normalization (auto-correct common mistakes) ──
    from api_pipeline.input_normalizer import normalize_inputs
    params, input_warnings = normalize_inputs(params)
    if input_warnings:
        params["_warnings"] = input_warnings

    # Use normalized video_type for routing
    vt = params.get("video_type", "").lower().strip()
    if vt not in ("product video", "influencer", "personal-brand", "ugc-real"):
        raise HTTPException(status_code=400, detail=f"Invalid video_type: {params.get('video_type', '')}")

    # ugc-real intake validation is handled by GenerateVideoRequest (prompt or structured fields).

    # ── LLM asset gate (influencer: reject if assets required but missing) ──
    if vt == "influencer" and not params.get("is_simulation"):
        assets = params.get("asset_urls") or []
        if not assets:
            try:
                from api_pipeline.llm import call_llm
                result = call_llm(
                    "asset_gate",
                    business_category=params.get("business_category", "general"),
                    prompt=params.get("prompt", ""),
                )
                gate = json.loads(result["text"])
                if gate.get("assets_required"):
                    reason = gate.get("reason", "Real photos/videos are needed for this business type")
                    raise HTTPException(
                        status_code=422,
                        detail=f"asset_urls required: {reason}",
                    )
            except HTTPException:
                raise
            except Exception as e:
                # LLM gate failure should NOT block the job — log and proceed
                logger.warning(f"Asset gate LLM check failed (proceeding): {e}")

    # ── LLM location extraction (if product_location not explicit) ──
    if not params.get("product_location") and not params.get("is_simulation"):
        try:
            from api_pipeline.llm import call_llm
            loc_result = call_llm(
                "location_extract",
                business_category=params.get("business_category", "general"),
                prompt=params.get("prompt", ""),
            )
            loc_data = json.loads(loc_result["text"])
            extracted = loc_data.get("product_location", "")
            if extracted:
                params["product_location"] = extracted
                logger.info(f"Location extracted from prompt: {extracted}")
        except Exception as e:
            logger.warning(f"Location extraction failed (proceeding without): {e}")

    # Rate limit checks — all counts BEFORE creating the job row
    limits = get_tenant_limits(tenant.id)

    # Check queue depth limit first (reject if queue is full)
    queued_count = supabase.count_queued_jobs_for_tenant(tenant.id)
    if queued_count >= limits["max_queued_jobs"]:
        raise HTTPException(
            status_code=429,
            detail=f"Queue full: {queued_count} jobs waiting (max {limits['max_queued_jobs']}). Try again later.",
        )

    # Check concurrent limits BEFORE creating the job (so the new row doesn't count itself)
    tenant_active = supabase.count_active_jobs_for_tenant(tenant.id)
    customer_active = 0
    if req.customer_id:
        customer_active = supabase.count_active_jobs_for_customer(tenant.id, req.customer_id)

    over_limit = (
        tenant_active >= limits["max_concurrent_jobs"]
        or (req.customer_id and customer_active >= limits["max_concurrent_per_customer"])
    )

    studio_uid = _get_studio_user_id_from_request(request)
    if req.user_id and studio_uid and str(req.user_id).strip() != str(studio_uid):
        raise HTTPException(
            status_code=400,
            detail="user_id does not match authenticated Studio user",
        )
    studio_session_id = None
    sid = (req.session_id or "").strip()
    if studio_uid and sid:
        if supabase.studio_session_belongs_to_user(sid, studio_uid):
            studio_session_id = sid
        else:
            logger.warning("Studio session_id not owned by user — ignoring")
    params.pop("session_id", None)
    params.pop("user_id", None)

    # Seeded product Phase 2 must pause after VO (step_2.7), not after clean product (step_2).
    if (
        vt == "product video"
        and params.get("seed_job_id")
        and params.get("pause_after_step") == "step_2"
    ):
        params["pause_after_step"] = "step_2.7"
        logger.info(
            "[GENERATE] Coerced pause_after_step step_2 → step_2.7 for seeded product video job"
        )

    # Create Supabase row
    job = supabase.create_job(
        video_type=vt,
        input_params=params,
        customer_id=req.customer_id,
        tenant_id=tenant.id,
        user_id=studio_uid,
        studio_session_id=studio_session_id,
    )
    job_id = job["id"]

    # Copy intermediates from seed job when continuing from a previous phase/job
    seed_job_id = params.get("seed_job_id")
    if seed_job_id:
        try:
            seed_job = supabase.verify_job_ownership(seed_job_id, tenant.id)
            seed_intermediates = dict(seed_job.get("intermediates") or {})
            # Phase 2 (pause_after_step=step_2.7) must generate a fresh VO based on user-edited
            # texts from Step 6.  Strip any VO data that Phase 1 produced in parallel so the
            # monolith doesn't treat them as a checkpoint and skip VO generation entirely.
            if params.get("pause_after_step") == "step_2.7":
                for _vo_key in ("vo_script", "vo_audio_url", "vo_word_segments", "vo_duration"):
                    seed_intermediates.pop(_vo_key, None)
            if seed_intermediates:
                supabase.update_progress(job_id, -1, "", intermediates=seed_intermediates)
                logger.info(f"Seeded job {job_id} with intermediates from job {seed_job_id}")
        except HTTPException as e:
            if e.status_code == 404:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Seed job not found. It may have expired (e.g. server was restarted and jobs are in memory). "
                        "Please run the previous step again (e.g. Generate VO for scene prompts) and then continue."
                    ),
                ) from e
            raise
        except Exception as e:
            logger.warning(f"Failed to seed from job {seed_job_id}: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid or inaccessible seed_job_id: {seed_job_id}")

    # Log normalization warnings
    if input_warnings:
        for w in input_warnings:
            fallback_store.append(job_id, {
                "timestamp": time.strftime("%H:%M:%S"),
                "level": "WARNING",
                "logger": "input_normalizer",
                "message": f"FALLBACK: {w['message']}",
            })
            event_store.push(job_id, "INPUT", f"Auto-corrected {w['field']}: '{w['original']}' \u2192 '{w['normalized']}'",
                             progress=-1, event_type="warn")

    if not over_limit:
        # Submit immediately
        pause_after = params.get("pause_after_step")
        logger.info(
            "[GENERATE] job_id=%s video_type=%s pause_after_step=%s seed_job_id=%s",
            job_id, vt, pause_after or "(none)", params.get("seed_job_id") or "(none)",
        )
        executor.submit(_run_job, job_id, vt, params)
        return GenerateVideoResponse(
            job_id=job_id,
            status="pending",
            message="Job submitted successfully",
            active_jobs=tenant_active + 1,
            max_concurrent=limits["max_concurrent_jobs"],
            warnings=input_warnings or None,
        )
    else:
        # Queue for later — drain loop will pick it up
        supabase.mark_queued(job_id)
        queue_pos = supabase.get_queue_position(job_id, tenant.id)
        return GenerateVideoResponse(
            job_id=job_id,
            status="queued",
            message=f"Queued at position {queue_pos}. Will start automatically when a slot opens.",
            queue_position=queue_pos,
            active_jobs=tenant_active,
            max_concurrent=limits["max_concurrent_jobs"],
            warnings=input_warnings or None,
        )


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str, tenant: Tenant = Depends(require_tenant)):
    """Get job status and results."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)
    return _job_to_response(job)


@app.get("/api/jobs", response_model=JobListResponse)
async def list_jobs(
    status: Optional[str] = None,
    customer_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    tenant: Tenant = Depends(require_tenant),
):
    """List jobs with optional filters. Scoped to the authenticated tenant."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")

    jobs = supabase.list_jobs(status=status, customer_id=customer_id, tenant_id=tenant.id, limit=limit, offset=offset)
    items = [_job_to_response(j) for j in jobs]
    return JobListResponse(jobs=items, total=len(items))


@app.post("/api/jobs/{job_id}/retry", response_model=GenerateVideoResponse)
async def retry_job(job_id: str, tenant: Tenant = Depends(require_tenant)):
    """Retry a failed job. Resumes from checkpoints stored in intermediates."""
    if not supabase or not executor:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)

    if job["status"] != "failed":
        raise HTTPException(status_code=400, detail=f"Job status is '{job['status']}', must be 'failed' to retry")

    retry_count = job.get("retry_count", 0)
    max_retries = job.get("max_retries", 3)
    if retry_count >= max_retries:
        raise HTTPException(status_code=400, detail=f"Max retries reached ({retry_count}/{max_retries})")

    new_retry = retry_count + 1
    supabase.mark_retrying(job_id, new_retry)

    vt = job["video_type"]
    params = dict(job.get("input_params", {}))
    # Failed during Rendi/ZapCap after all scene clips exist — do not pause again at
    # animations_review (step_12), or the monolith would stop before final assembly.
    im = job.get("intermediates") or {}
    sv = im.get("scene_videos") or []
    if isinstance(sv, list) and sv:
        http_n = sum(
            1 for u in sv if isinstance(u, str) and u.strip().lower().startswith("http")
        )
        sp = im.get("scene_prompts") or []
        si = im.get("scene_images") or []
        need = max(len(sv), len(sp), len(si))
        if need > 0 and http_n >= need:
            params["pause_after_step"] = None
            logger.info(
                "Job %s retry: full scene_videos in intermediates (%s/%s) — pause_after_step cleared for final assembly",
                job_id,
                http_n,
                need,
            )
    executor.submit(_run_job, job_id, vt, params)

    return GenerateVideoResponse(
        job_id=job_id,
        status="processing",
        message=f"Job retrying (attempt {new_retry}/{max_retries}), resuming from checkpoints",
    )


@app.post("/api/jobs/{job_id}/abort", response_model=GenerateVideoResponse)
async def abort_job(job_id: str, tenant: Tenant = Depends(require_tenant)):
    """Abort a running job. The pipeline checks for abort status before each step."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)

    status = job["status"]
    if status in ("completed", "aborted"):
        raise HTTPException(status_code=400, detail=f"Job is already '{status}', cannot abort")
    if status == "failed":
        raise HTTPException(status_code=400, detail="Job already failed, cannot abort")
    if status not in ("processing", "paused", "pending", "queued"):
        raise HTTPException(status_code=400, detail=f"Job status is '{status}', cannot abort")

    supabase.mark_aborted(job_id)

    # If the job was paused or queued (no pipeline thread running), push the
    # terminal SSE event directly so the client transitions immediately.
    if status in ("paused", "queued"):
        event_store.push(job_id, "SERVER", "Job aborted by user", progress=-1, event_type="abort")
    else:
        event_store.push(job_id, "SERVER", "Abort requested — pipeline will stop at next step boundary", event_type="warn")

    return GenerateVideoResponse(
        job_id=job_id,
        status="aborted",
        message="Job abort requested. Pipeline will stop before the next step.",
    )


@app.post("/api/jobs/{job_id}/pause", response_model=GenerateVideoResponse)
async def pause_job(job_id: str, tenant: Tenant = Depends(require_tenant)):
    """Pause a running job. The pipeline stops at the next step boundary."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)

    if job["status"] != "processing":
        raise HTTPException(status_code=400, detail=f"Job status is '{job['status']}', must be 'processing' to pause")

    supabase.mark_paused(job_id)
    event_store.push(job_id, "SERVER", "Pause requested — pipeline will stop at next step boundary", event_type="warn")
    return GenerateVideoResponse(
        job_id=job_id,
        status="paused",
        message="Job pause requested. Pipeline will stop before the next step.",
    )


def _ugc_real_scene_plan_in_intermediates(im: Optional[Dict[str, Any]]) -> bool:
    if not im or not isinstance(im, dict):
        return False
    plan = im.get("nine_cell_plan")
    if not isinstance(plan, dict):
        return False
    cells = plan.get("cells")
    if not isinstance(cells, list) or len(cells) != 9:
        return False
    filled = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        visual_prompt = str(cell.get("visual_prompt") or "").strip()
        voice_line = str(cell.get("voice_line") or "").strip()
        if visual_prompt and voice_line:
            filled += 1
    return filled >= 7


def _ugc_real_grid_review_in_intermediates(im: Optional[Dict[str, Any]]) -> bool:
    if not im or not isinstance(im, dict):
        return False
    if im.get("grid_image_url"):
        return True
    sg = im.get("scene_grids")
    if isinstance(sg, list) and len(sg) > 0:
        return True
    gm = im.get("grid_manifests")
    if isinstance(gm, list) and len(gm) > 0:
        return True
    # UGC Real persists grid review data under these keys (see ugc_real pipeline); older checks
    # missed them so jobs stayed "processing" and Studio resume returned 400 with no visible UX.
    fr = im.get("frame_routing")
    if isinstance(fr, list) and len(fr) > 0:
        return True
    gcells = im.get("grid_cells")
    if isinstance(gcells, list) and len(gcells) > 0:
        return True
    return False


def _intermediates_ready_for_final_assembly(im: Optional[Dict[str, Any]]) -> bool:
    """True when all scene clip URLs exist in intermediates and concat has not started."""
    if not im or not isinstance(im, dict):
        return False
    if im.get("concat_url"):
        return False
    sv = im.get("scene_videos") or []
    if not isinstance(sv, list) or not sv:
        return False
    sp = im.get("scene_prompts") or []
    si = im.get("scene_images") or []
    need = max(
        len(sv),
        len(sp) if isinstance(sp, list) else 0,
        len(si) if isinstance(si, list) else 0,
    )
    if need <= 0:
        return False
    have = sum(
        1 for u in sv if isinstance(u, str) and u.strip().lower().startswith("http")
    )
    return have >= need


def _ugc_real_default_pause_after(current_step: str, explicit_stop_after: Optional[bool]) -> Optional[str]:
    """Resume pause targets for UGC Real Studio review flow.

    Pause chain: step_parse (offer fields) → step_1 (nine-cell plan) → step_2 (style DNA; Studio: approve
    character + patch job before master grid) → step_5 (grid review) → step_8 → end.
    """
    step = (current_step or "").strip()
    if explicit_stop_after is False:
        return None
    if step == "step_parse":
        return "step_1"
    if step == "step_1":
        return "step_2"
    if step == "step_2":
        return "step_5"
    if step == "step_5":
        return "step_8"
    if step in ("step_8", "animations_review"):
        return None
    return "step_1" if explicit_stop_after is None else "step_8"


def _restart_pause_target(video_type: str, from_step: str) -> Optional[str]:
    """Default restart pause targets so Studio review screens remain deterministic."""
    vt = (video_type or "").strip().lower()
    if vt != "ugc-real":
        return None
    if from_step == "step_parse":
        return "step_parse"
    if from_step in ("step_0", "step_0.5"):
        return "step_1"
    if from_step == "step_1":
        return "step_2"
    if from_step in ("step_2", "step_3", "step_4"):
        return "step_5"
    if from_step == "step_8":
        return "step_8"
    return None


@app.post("/api/jobs/{job_id}/resume", response_model=GenerateVideoResponse)
async def resume_job(job_id: str, request: Request, tenant: Tenant = Depends(require_tenant)):
    """Resume a paused job from where it stopped.

    JSON body (optional):

    - ``{"stop_after_scene_animations": true}`` — pause again after ``animations_review``
      (before Rendi concat) so the user can approve scene clips.
    - ``{"stop_after_scene_animations": false}`` — run through final assembly in one go.

    If the key is **omitted**: for **product video** jobs, defaults to the same as ``true`` unless
    ``current_step`` is already ``animations_review`` (then runs to completion). Other video
    types default to running to completion when the key is omitted (legacy dashboard behavior).
    """
    if not supabase or not executor:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_stop_after = body.get("stop_after_scene_animations")

    # Studio: Supabase mark_paused can fail (HTTP/2 flakes) while the worker already stopped —
    # DB stays "processing" with all scene_videos filled. Approve sends resume with false; fix state.
    if job["status"] == "processing" and raw_stop_after is False:
        im_fix = job.get("intermediates") or {}
        if _intermediates_ready_for_final_assembly(im_fix):
            logger.warning(
                "Job %s resume: status was processing with full scene_videos — syncing paused(animations_review)",
                job_id,
            )
            for attempt in range(5):
                try:
                    supabase.mark_paused(job_id, "animations_review")
                    job = supabase.verify_job_ownership(job_id, tenant.id)
                    if job.get("status") == "paused":
                        break
                except Exception as sync_e:
                    if attempt == 4:
                        logger.error("Job %s could not sync paused for resume: %s", job_id, sync_e)
                time.sleep(0.35 * (attempt + 1))

    # UGC Real: worker raised the review pause but Supabase stayed "processing" (same flake class as above).
    # Only sync when no executor thread holds the job — otherwise the pipeline is genuinely still running.
    vt_resume = (job.get("video_type") or "").strip().lower()
    if job["status"] == "processing" and vt_resume == "ugc-real":
        with _running_jobs_lock:
            ugc_worker_in_flight = job_id in _running_jobs
        if not ugc_worker_in_flight:
            im_u = job.get("intermediates") or {}
            cs_u = (job.get("current_step") or "").strip()
            pause_at: Optional[str] = None
            if cs_u in ("step_1", "step_2") and _ugc_real_scene_plan_in_intermediates(im_u):
                pause_at = cs_u
            elif cs_u in ("step_3", "step_4") and _ugc_real_grid_review_in_intermediates(im_u):
                # Master grid / cut finished in the worker but Supabase stayed "processing" (same flake class).
                pause_at = "step_5"
            elif cs_u == "step_5" and _ugc_real_grid_review_in_intermediates(im_u):
                pause_at = "step_5"
            if pause_at:
                logger.warning(
                    "Job %s resume: UGC Real was processing at %s with review data but not running — syncing paused",
                    job_id,
                    pause_at,
                )
                for attempt in range(5):
                    try:
                        supabase.mark_paused(job_id, pause_at)
                        job = supabase.verify_job_ownership(job_id, tenant.id)
                        if job.get("status") == "paused":
                            break
                    except Exception as sync_u:
                        if attempt == 4:
                            logger.error("Job %s UGC review pause sync failed: %s", job_id, sync_u)
                    time.sleep(0.35 * (attempt + 1))

    # Clearer than 400 "must be paused" when the worker is genuinely in flight (e.g. Nano Banana / Kie).
    if job["status"] == "processing":
        with _running_jobs_lock:
            if job_id in _running_jobs:
                raise HTTPException(
                    status_code=409,
                    detail="Job is already running; wait for the current step to finish or for the job to pause before resuming.",
                )

    if job["status"] != "paused":
        raise HTTPException(status_code=400, detail=f"Job status is '{job['status']}', must be 'paused' to resume")

    # Prevent duplicate resume if job is already running
    with _running_jobs_lock:
        if job_id in _running_jobs:
            raise HTTPException(status_code=409, detail="Job is already running")

    # Record cursor so SSE reconnect skips old events (including the terminal "pause")
    cursor = event_store.event_count(job_id)

    supabase.mark_processing(job_id)
    vt = job["video_type"]
    params = dict(job.get("input_params", {}))
    # Three-way flag: True = pause after animations_review (step_12) in most cases; False = run to final video;
    # omitted = product defaults (see below).
    _cs = (job.get("current_step") or "").strip()
    _vt_lower = (vt or "").strip().lower()
    if _vt_lower == "ugc-real":
        params["pause_after_step"] = _ugc_real_default_pause_after(_cs, raw_stop_after)
    elif raw_stop_after is True:
        # Studio step 7 "Resume" after VO script review: must stop after scene *prompts* (step_3),
        # not jump straight to scene_generation. "stop_after_scene_animations" still means
        # "insert a pause before final assembly" — the first such pause after VO is step_3.
        if _vt_lower == "product video" and _cs == "vo_generation":
            params["pause_after_step"] = "step_3"
        else:
            params["pause_after_step"] = "step_12"
    elif raw_stop_after is False:
        params["pause_after_step"] = None
    else:
        if _cs == "animations_review":
            # Paused for animation review — user is approving clips / final assembly.
            params["pause_after_step"] = None
        elif _vt_lower == "product video":
            # Resume with no body: same as legacy Studio — after VO gate, stop for scene prompts next.
            if _cs == "vo_generation":
                params["pause_after_step"] = "step_3"
            else:
                params["pause_after_step"] = "step_12"
        else:
            # Influencer / personal-brand / etc.: never silently run all the way to final video on
            # a no-body resume. The user's normal Studio flow inserts pauses at scene-image and
            # animation review; an empty-body resume (manual curl, watchdog auto-resume) used to
            # default to "no pause" → final video assembled before approval. Default to step_12
            # (animation review) so the user always gets the approval gate.
            params["pause_after_step"] = "step_12"
    executor.submit(_run_job, job_id, vt, params)

    event_store.push(job_id, "SERVER", "Job resumed — continuing from checkpoints", event_type="start")
    return GenerateVideoResponse(
        job_id=job_id,
        status="processing",
        message="Job resumed, continuing from checkpoints.",
        event_cursor=cursor,
    )


@app.post("/api/jobs/{job_id}/retry-scene-animations")
async def retry_scene_animations(job_id: str, tenant: Tenant = Depends(require_tenant)):
    """Clear scene video slots and re-run the animation phase (Studio step 12).

    - **processing**: requests pause. Call again once status is **paused** (poll every few seconds).
    - **paused**: clears ``scene_videos`` / trim cache, resumes with ``pause_after_step=step_12``.
    - **failed**: if the job had scene images, clears videos and restarts the pipeline with the same pause.
    """
    if not supabase or not executor:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)
    status = job.get("status") or ""

    def _scene_slot_count(im: dict) -> int:
        sp = im.get("scene_prompts") or []
        si = im.get("scene_images") or []
        if (job.get("video_type") or "").strip().lower() == "ugc-real":
            scenes = ((im.get("nine_cell_plan") or {}).get("cells") or [])
            sg = im.get("scene_grids") or []
            return max(len(scenes), len(sg), len(si), 0)
        return max(len(sp), len(si), 0)

    def _clear_animation_outputs(im: dict) -> dict:
        out = dict(im or {})
        n = _scene_slot_count(out)
        if n == 0:
            raise HTTPException(
                status_code=400,
                detail="No scene_prompts or scene_images — run step 9 and generate images first.",
            )
        out["scene_videos"] = [None] * n
        out.pop("trimmed_scene_videos", None)
        # Also clear smart-mode beat clip cache so retry regenerates clips via Veo
        out.pop("scene_beat_clips", None)
        return out

    if status == "processing":
        with _running_jobs_lock:
            in_flight = job_id in _running_jobs
        if in_flight:
            supabase.mark_paused(job_id)
            event_store.push(
                job_id,
                "SERVER",
                "Pause requested — retry scene animations after status is paused",
                event_type="warn",
            )
            logger.info(f"[retry-scene-animations] job {job_id}: pause requested (was processing)")
            return {
                "job_id": job_id,
                "phase": "pause_requested",
                "status": "paused",
                "message": "Pause requested. Wait until the job status is paused (up to a few minutes if a video API call is in flight), then call this endpoint again.",
                "event_cursor": event_store.event_count(job_id),
            }
        raise HTTPException(
            status_code=409,
            detail="Job shows processing but no worker is registered — refresh job status or contact support.",
        )

    if status == "failed":
        im = dict(job.get("intermediates") or {})
        n = _scene_slot_count(im)
        if n == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot retry animations: no scene data in failed job. Start from step 9.",
            )
        cleared = _clear_animation_outputs(im)
        with _running_jobs_lock:
            if job_id in _running_jobs:
                raise HTTPException(status_code=409, detail="Job is already running")
        cursor = event_store.event_count(job_id)
        vt = job["video_type"]
        params = dict(job.get("input_params", {}))
        params["pause_after_step"] = "step_8" if (vt or "").strip().lower() == "ugc-real" else "step_12"
        now = datetime.now(timezone.utc).isoformat()
        supabase.client.table(supabase.table).update({
            "status": "processing",
            "intermediates": cleared,
            "error": None,
            "error_details": None,
            "failed_at_step": None,
            "current_step": "retry_scene_animations",
            "progress": max(int(job.get("progress") or 0), 1),
            "updated_at": now,
        }).eq("id", job_id).execute()
        executor.submit(_run_job, job_id, vt, params)
        event_store.push(job_id, "SERVER", "Retrying scene animations (cleared previous clips)", event_type="start")
        logger.info(f"[retry-scene-animations] job {job_id}: restarted from failed")
        return {
            "job_id": job_id,
            "phase": "resumed",
            "status": "processing",
            "message": "Scene videos cleared; animation phase restarted. Stops again after clips for review.",
            "event_cursor": cursor,
        }

    if status != "paused":
        raise HTTPException(
            status_code=400,
            detail=f"Job status is '{status}'. For a running job, call this endpoint once to pause, wait until paused, then call again.",
        )

    with _running_jobs_lock:
        if job_id in _running_jobs:
            raise HTTPException(status_code=409, detail="Job worker still active — wait a few seconds and try again")

    im = dict(job.get("intermediates") or {})
    cleared = _clear_animation_outputs(im)
    supabase.update_progress(job_id, -1, "", intermediates=cleared)

    cursor = event_store.event_count(job_id)
    vt = job["video_type"]
    params = dict(job.get("input_params", {}))
    params["pause_after_step"] = "step_8" if (vt or "").strip().lower() == "ugc-real" else "step_12"
    supabase.mark_processing(job_id)
    executor.submit(_run_job, job_id, vt, params)
    event_store.push(job_id, "SERVER", "Scene animations re-run started", event_type="start")
    logger.info(f"[retry-scene-animations] job {job_id}: cleared scene_videos and resumed")
    return {
        "job_id": job_id,
        "phase": "resumed",
        "status": "processing",
        "message": "Previous scene clips cleared; animations running again. Review when the job pauses.",
        "event_cursor": cursor,
    }


@app.patch("/api/jobs/{job_id}/intermediates", response_model=PatchIntermediatesResponse)
async def patch_job_intermediates(
    job_id: str,
    req: PatchIntermediatesRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Merge intermediate key(s) into a job (e.g. before resume). Does not change progress/step."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")
    supabase.verify_job_ownership(job_id, tenant.id)
    to_merge = req.to_intermediates_dict()
    if to_merge:
        supabase.merge_intermediates(job_id, to_merge)
        logger.info(f"Patched job {job_id} intermediates: {list(to_merge.keys())}")
    ipp = req.input_params_patch or {}
    if ipp:
        supabase.merge_input_params(job_id, ipp)
        logger.info(f"Patched job {job_id} input_params: {list(ipp.keys())}")
    if not to_merge and not ipp:
        return PatchIntermediatesResponse(ok=True)
    return PatchIntermediatesResponse(ok=True)


@app.post("/api/generate-music", response_model=GenerateMusicResponse)
async def generate_music(req: GenerateMusicRequest, tenant: Tenant = Depends(require_tenant)):
    """Standalone music generation (description + Suno). Uses monolith processor when available."""
    try:
        from tvd_pipeline.processor import VideoSceneProcessor
        from tvd_pipeline.services.tasks.music import generate_music_description_from_text
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="Music generation requires tvd_pipeline (monolith). Not available.",
        ) from e
    processor = VideoSceneProcessor()
    if req.music_description_override:
        music_description = req.music_description_override.strip()
    else:
        video_subtype = None
        if req.video_type.lower() in ("influencer", "personal-brand"):
            video_subtype = "influencer" if req.video_type.lower() == "influencer" else "personal_brand"
        music_description = generate_music_description_from_text(
            lambda msgs, **kw: processor._call_llm("generate_music_description", msgs, **kw),
            content_text=f"{req.text_1}\n{req.text_2}\n{req.text_3}",
            vo_script=req.vo_script or "",
            video_subtype=video_subtype,
        )
    music_url = processor.suno_service.generate_pure_music(style_description=music_description)
    if not music_url:
        raise HTTPException(status_code=502, detail="Suno did not return a music URL")
    return GenerateMusicResponse(music_description=music_description, music_url=music_url)


# Stagger Kie scene-image requests so parallel calls (e.g. Studio "Generate images") are sent a few seconds apart
_KIE_SCENE_IMAGE_STAGGER_LOCK = asyncio.Lock()
_KIE_SCENE_IMAGE_SLOT_COUNTER = 0
_KIE_SCENE_IMAGE_LAST_START = 0.0
_KIE_SCENE_IMAGE_STAGGER_IDLE_RESET_SEC = 30


def _get_kie_scene_image_stagger_seconds(image_provider: str, image_model: str) -> float:
    """Return stagger_seconds when using Kie for scene images; 0 otherwise."""
    if (image_provider or "").lower() != "kie":
        return 0.0
    try:
        from tvd_pipeline.pipelines._provider_limits import get_scene_image_stagger_seconds
        use_kie_flash = "flash" in (image_model or "").lower()
        return get_scene_image_stagger_seconds(
            use_google_image=False,
            use_kie_flash=use_kie_flash,
            image_model=image_model,
        )
    except Exception:
        return 3.0


@app.post("/api/generate-scene-image", response_model=GenerateSceneImageResponse)
async def generate_scene_image(req: GenerateSceneImageRequest, tenant: Tenant = Depends(require_tenant)):
    """Generate a single scene image (with optional correction text). Uses monolith processor."""
    try:
        from tvd_pipeline.processor import VideoSceneProcessor
        from api_pipeline.resolution_tiers import get_tier
        from api_pipeline.model_mappings_config import get_image_api_map
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="Scene image generation requires tvd_pipeline. Not available.",
        ) from e
    full_scene_prompt = (req.image_prompt or "").strip()
    if req.image_to_fix_url and req.correction_text:
        image_prompt = req.correction_text.strip()
    elif req.correction_text:
        image_prompt = (req.correction_text + "\n" + req.image_prompt).strip()
    else:
        image_prompt = req.image_prompt or ""
    if not (image_prompt or "").strip():
        raise HTTPException(
            status_code=400,
            detail="image_prompt is empty — add scene text on step 10 or correction notes for Regenerate/Fix.",
        )
    # Resolve image_model + image_provider from image_api (same as input_translator)
    output_resolution = "720p_low"
    vt = (req.video_type or "influencer").lower().strip()
    pipeline = "influencer" if vt == "influencer" else ("personal_brand" if vt == "personal-brand" else "product")
    tier = get_tier(output_resolution, pipeline)
    tier_image_model = tier.get("image_model", "nano-banana-pro")
    tier_image_provider = tier.get("image_provider", "kie")
    tier_image_resolution = tier.get("image_resolution") or "1K"
    image_api_map = get_image_api_map()
    client_image_api = (req.image_api or "").strip() or None
    api_key = (client_image_api or "").lower()
    # Studio dropdown historically used "flash"; model_mappings.json key is "kie-flash"
    if api_key == "flash":
        api_key = "kie-flash"
    if api_key:
        mapped = image_api_map.get(api_key)
        if mapped:
            tier_image_model, tier_image_provider = mapped
    processor = VideoSceneProcessor()
    ref_urls = (req.reference_image_urls or []) if isinstance(req.reference_image_urls, list) else []
    char_urls = (req.character_reference_urls or []) if isinstance(req.character_reference_urls, list) else []
    fix_ref_urls = [req.image_to_fix_url] if req.image_to_fix_url else ref_urls
    image_edit_mode = bool(req.image_to_fix_url and str(req.image_to_fix_url).strip())
    scene_image_kw = dict(
        image_prompt=image_prompt,
        product_visible=bool(fix_ref_urls),
        visual_style=req.visual_style or "Auto",
        character_reference_urls=char_urls or None,
        has_character=req.has_character,
        logo_reference_url=req.logo_reference_url or None,
        is_cta_scene=req.is_cta_scene,
        image_edit_mode=image_edit_mode,
        scene_context_for_edit=full_scene_prompt if image_edit_mode else None,
    )
    if fix_ref_urls:
        scene_image_kw["product_reference_urls"] = fix_ref_urls
        if not image_edit_mode:
            if req.image_to_fix_url and req.correction_text:
                scene_image_kw["product_description"] = "Apply these corrections to the reference image: " + req.correction_text.strip()
            elif req.image_to_fix_url:
                scene_image_kw["product_description"] = "Refine or improve this image based on the original scene. " + (req.image_prompt or "")
            else:
                scene_image_kw["product_description"] = req.product_description or ""

    stagger_sec = _get_kie_scene_image_stagger_seconds(tier_image_provider, tier_image_model)
    logger.info(
        "generate-scene-image: client_image_api=%s resolved image_provider=%s image_model=%s image_resolution=%s "
        "kie_stagger_sec=%s",
        client_image_api or "(tier default)",
        tier_image_provider,
        tier_image_model,
        tier_image_resolution,
        stagger_sec,
    )
    if stagger_sec > 0:
        global _KIE_SCENE_IMAGE_SLOT_COUNTER, _KIE_SCENE_IMAGE_LAST_START
        now = time.time()
        async with _KIE_SCENE_IMAGE_STAGGER_LOCK:
            if now - _KIE_SCENE_IMAGE_LAST_START > _KIE_SCENE_IMAGE_STAGGER_IDLE_RESET_SEC:
                _KIE_SCENE_IMAGE_SLOT_COUNTER = 0
            slot = _KIE_SCENE_IMAGE_SLOT_COUNTER
            _KIE_SCENE_IMAGE_SLOT_COUNTER += 1
            _KIE_SCENE_IMAGE_LAST_START = now
        delay = slot * stagger_sec
        if delay > 0:
            await asyncio.sleep(delay)

    loop = asyncio.get_event_loop()
    try:
        image_url = await loop.run_in_executor(
            None,
            lambda: processor._generate_image(
                image_model=tier_image_model,
                image_provider=tier_image_provider,
                resolution=tier_image_resolution,
                **scene_image_kw,
            ),
        )
    except Exception as gen_exc:
        logger.exception("generate-scene-image: _generate_image raised")
        raise HTTPException(
            status_code=502,
            detail=f"Image generation failed: {gen_exc}",
        ) from gen_exc

    # If the first attempt failed because the model refused the content (Vertex
    # IMAGE_PROHIBITED_CONTENT, SAFETY, RECITATION, etc.), try ONE softer rephrase via LLM
    # and retry on the same provider/model. This catches genuine swimwear / body-content false
    # positives where Vertex hard-refuses but a softer phrasing slips through.
    rephrase_attempted = False
    rephrased_prompt_used: Optional[str] = None
    if not image_url or not str(image_url).strip():
        gem_svc = getattr(processor, "gemini_image_service", None)
        first_failure = (getattr(gem_svc, "last_failure_reason", None) or "").strip()
        looks_like_safety = bool(first_failure) and any(
            marker in first_failure.upper()
            for marker in (
                "PROHIBITED",
                "SAFETY",
                "BLOCKED",
                "RECITATION",
                "FILTERED",
                "FINISH_REASON=SAFETY",
                "IMAGE_PROHIBITED",
            )
        )
        if looks_like_safety:
            try:
                from tvd_pipeline.prompt_loader import get_prompt_loader  # type: ignore
                _loader = get_prompt_loader()
                rephrase_system = _loader.get("shared_rephrase_blocked_prompt_system")
                rephrase_user = _loader.get(
                    "shared_rephrase_blocked_prompt_user",
                    original_prompt=image_prompt,
                    error_message=first_failure,
                )
                rephrase_messages = [
                    {"role": "system", "content": rephrase_system},
                    {"role": "user", "content": rephrase_user},
                ]
                rephrase_attempted = True
                logger.warning(
                    "generate-scene-image: first attempt blocked (%s) — calling LLM to soften prompt and retrying once",
                    (first_failure or "")[:200],
                )
                rephrased_text = await loop.run_in_executor(
                    None,
                    lambda: (processor._call_llm("rephrase_blocked_prompt", rephrase_messages).get("text") or "").strip(),
                )
                if rephrased_text and len(rephrased_text) > 5:
                    rephrased_prompt_used = rephrased_text
                    softer_kw = dict(scene_image_kw)
                    softer_kw["image_prompt"] = rephrased_text
                    image_url = await loop.run_in_executor(
                        None,
                        lambda: processor._generate_image(
                            image_model=tier_image_model,
                            image_provider=tier_image_provider,
                            resolution=tier_image_resolution,
                            **softer_kw,
                        ),
                    )
                    if image_url and str(image_url).strip():
                        logger.info(
                            "generate-scene-image: softer rephrase succeeded after %s",
                            (first_failure or "")[:80],
                        )
                else:
                    logger.warning("generate-scene-image: rephrase LLM returned empty text — skipping retry")
            except Exception as _rep_exc:
                logger.warning("generate-scene-image: rephrase-and-retry failed (%s) — falling through to 502", _rep_exc)

    if not image_url or not str(image_url).strip():
        hints = []
        kie_svc = getattr(processor, "kie_service", None)
        kr = (getattr(kie_svc, "last_failure_reason", None) or "").strip()
        if kr:
            hints.append(f"Kie: {kr}")
        gem_svc = getattr(processor, "gemini_image_service", None)
        gr = (getattr(gem_svc, "last_failure_reason", None) or "").strip()
        if gr:
            hints.append(f"Vertex/Gemini image: {gr}")
        if rephrase_attempted:
            hints.append("Soft-rephrase retry was attempted and also failed — model is hard-refusing this content. Try switching the Image model dropdown to 'Nano Banana Pro (Kie.ai)'.")
        detail = "Image generation did not return a URL."
        if hints:
            detail += " " + " ".join(hints)
        else:
            detail += (
                " Check KIE_API key, server logs, and that reference image URLs are reachable (not expired or 403)."
            )
        logger.warning(
            "generate-scene-image: no URL (provider=%s model=%s rephrase_attempted=%s) %s",
            tier_image_provider,
            tier_image_model,
            rephrase_attempted,
            " | ".join(hints) if hints else "(no provider last_failure_reason)",
        )
        raise HTTPException(status_code=502, detail=detail)
    return GenerateSceneImageResponse(image_url=str(image_url).strip())


@app.post("/api/animate-scene", response_model=AnimateSceneResponse)
async def animate_scene(req: AnimateSceneRequest, tenant: Tenant = Depends(require_tenant)):
    """Re-animate a single scene (Studio Step 13 Re-animate button).

    Loads the job, picks up the image (request override → intermediates.scene_images[idx]) and
    motion prompt (request override → scene_prompts[idx].second_prompt), runs the monolith's
    ``_generate_video`` for one scene, patches ``intermediates.scene_videos[scene_index]`` with
    the new URL, and returns it. Resolution tier + animation provider are taken from the job's
    ``input_params`` so the new clip matches the rest of the video.
    """
    try:
        from tvd_pipeline.processor import VideoSceneProcessor
        from api_pipeline.resolution_tiers import get_tier
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="Per-scene re-animate requires tvd_pipeline. Not available.",
        ) from e
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(req.job_id, tenant.id)
    ints = job.get("intermediates") or {}
    sp = ints.get("scene_prompts") or []
    si = ints.get("scene_images") or []
    sv = ints.get("scene_videos") or []
    if not sp:
        raise HTTPException(status_code=400, detail="Job has no scene_prompts — generate them first.")
    if req.scene_index >= len(sp):
        raise HTTPException(
            status_code=400,
            detail=f"scene_index {req.scene_index} out of range (have {len(sp)} scenes).",
        )

    scene = sp[req.scene_index] if isinstance(sp[req.scene_index], dict) else {}
    image_url = (req.image_url or "").strip()
    if not image_url:
        candidate = si[req.scene_index] if req.scene_index < len(si) else None
        image_url = (candidate or "").strip() if isinstance(candidate, str) else ""
    if not image_url:
        raise HTTPException(
            status_code=400,
            detail=f"No image URL for scene {req.scene_index} (request omitted image_url and intermediates.scene_images[{req.scene_index}] is empty).",
        )

    motion_prompt = (req.motion_prompt or "").strip()
    if not motion_prompt:
        motion_prompt = (scene.get("second_prompt") or scene.get("motion_prompt") or scene.get("first_prompt") or "").strip()
    if not motion_prompt:
        raise HTTPException(status_code=400, detail="motion_prompt is empty and no fallback in scene_prompts.")

    duration = float(req.duration) if req.duration is not None else float(scene.get("duration_seconds") or scene.get("duration") or 5.0)

    ip = job.get("input_params") or {}
    output_resolution = (ip.get("output_resolution") or "720p_low")
    vt = (job.get("video_type") or "influencer").lower().strip()
    pipeline = "influencer" if vt == "influencer" else ("personal_brand" if vt == "personal-brand" else "product")
    tier = get_tier(output_resolution, pipeline)
    video_model = ip.get("video_model") or tier.get("video_model") or "veo-3.1-fast"
    video_provider = ip.get("video_provider") or tier.get("video_provider") or "direct"
    video_resolution = ip.get("video_resolution") or tier.get("video_resolution") or "720p"

    processor = VideoSceneProcessor()
    loop = asyncio.get_event_loop()
    try:
        video_url = await loop.run_in_executor(
            None,
            lambda: processor._generate_video(
                video_model=video_model,
                video_provider=video_provider,
                image_url=image_url,
                motion_prompt=motion_prompt,
                duration=duration,
                resolution=video_resolution,
            ),
        )
    except Exception as gen_exc:
        logger.exception("animate-scene: _generate_video raised for job=%s scene=%s", req.job_id, req.scene_index)
        raise HTTPException(status_code=502, detail=f"Per-scene animation failed: {gen_exc}") from gen_exc

    if not video_url or not str(video_url).strip():
        raise HTTPException(
            status_code=502,
            detail=f"Animation produced no URL (provider={video_provider}, model={video_model}).",
        )

    # Patch intermediates.scene_videos[scene_index] = new_url, preserving siblings.
    new_sv = list(sv) if isinstance(sv, list) else []
    while len(new_sv) <= req.scene_index:
        new_sv.append(None)
    new_sv[req.scene_index] = str(video_url).strip()
    try:
        supabase.merge_intermediates(req.job_id, {"scene_videos": new_sv})
    except Exception as patch_exc:
        logger.warning("animate-scene: merge_intermediates failed (returning URL anyway): %s", patch_exc)

    return AnimateSceneResponse(scene_index=req.scene_index, video_url=str(video_url).strip())


@app.post("/api/generate-character", response_model=GenerateCharacterResponse)
async def generate_character(req: GenerateCharacterRequest, tenant: Tenant = Depends(require_tenant)):
    """Generate a character / spokesperson portrait when the Studio user did not upload one (step 9, before scene prompts)."""
    try:
        from tvd_pipeline.processor import VideoSceneProcessor
        from tvd_pipeline.pipelines.ugc import _generate_influencer_image
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="Character generation requires tvd_pipeline. Not available.",
        ) from e
    try:
        char_d = (req.character_description or "").strip()
        pr = (req.prompt or "").strip()
        # Topic/persona block in the monolith uses product_context only — keep it to the MAIN SCRIPT (step 3),
        # not the character brief, so scene/narrative in "Character look" does not leak into topic_section.
        topic_ctx = (pr[:800] if pr else (char_d[:800] if char_d else "")).strip()
        correction = (str(req.correction_text).strip() if req.correction_text else None) or None
        vt_raw = (req.video_type or "influencer").lower().strip()
        if vt_raw in ("product video", "product-video", "product_video"):
            subtype = "personal_brand"
        elif vt_raw in ("personal-brand", "personal_brand", "personal service"):
            subtype = "personal_brand"
        else:
            subtype = "influencer"
        g = (req.gender or "f").strip().lower()
        if g not in ("m", "f"):
            g = "f"
        try:
            processor = VideoSceneProcessor()
        except Exception as e:
            logger.exception("generate-character: VideoSceneProcessor init failed")
            raise HTTPException(
                status_code=502,
                detail=f"Character portrait: server could not initialize pipeline (env / credentials). {e}",
            ) from e

        def _run():
            return _generate_influencer_image(
                processor,
                gender=g,
                product_context=topic_ctx,
                visual_style=req.visual_style or "Auto",
                country=(req.country or "") or "",
                language=(req.language or "en") or "en",
                video_subtype=subtype,
                portrait_correction=correction,
                studio_character_look=char_d if char_d else None,
            )

        loop = asyncio.get_running_loop()
        image_url, description, portrait_prompt = await loop.run_in_executor(None, _run)

        def _as_opt_str(v):
            if v is None:
                return None
            s = str(v).strip()
            return s if s else None

        return GenerateCharacterResponse(
            image_url=_as_opt_str(image_url),
            description=_as_opt_str(description),
            portrait_image_prompt=_as_opt_str(portrait_prompt),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("generate-character: unhandled exception")
        raise HTTPException(
            status_code=502,
            detail=f"Character portrait generation failed: {type(e).__name__}: {e}",
        ) from e


def _parse_character_suggestions_json(raw: str) -> List[str]:
    """Extract {"suggestions": [...]} from model text; tolerate markdown fences."""
    import json
    import re

    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\"suggestions\"[\s\S]*\}", text)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []
    raw_list = data.get("suggestions")
    if not isinstance(raw_list, list):
        return []
    out: List[str] = []
    for item in raw_list[:8]:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s[:400])
    return out


def _vertex_suggest_character_briefs_sync(
    prompt: str,
    language: str,
    country: Optional[str],
    video_type: str,
    gender: Optional[str] = None,
) -> List[str]:
    """Call Vertex generateContent with a cheap Gemini model; return 1–4 character-look strings."""
    from api_pipeline.services.base.config import config as _cfg

    project = _cfg.VERTEX_AI_PROJECT_ID
    location = _cfg.VERTEX_AI_LOCATION
    api_key = (_cfg.VERTEX_AI_API_KEY or "").strip()

    # Fallback to monolith Config when wrapper config has no VERTEX_AI_API_KEY
    # (monolith Config has a default key baked in; wrapper .env leaves it blank)
    if not api_key:
        try:
            from tvd_pipeline.config import Config as _MonolithConfig
            _mc = _MonolithConfig()
            api_key = (_mc.VERTEX_AI_API_KEY or "").strip()
        except Exception:
            pass
    template = (
        f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
        "/publishers/google/models"
    )

    def _headers() -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            return h
        try:
            from google.auth import default
            from google.auth.transport.requests import Request

            creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(Request())
            if creds.token:
                h["Authorization"] = f"Bearer {creds.token}"
                return h
        except Exception:
            pass
        raise ValueError(
            "Vertex AI not configured for character suggestions: set VERTEX_AI_API_KEY "
            "or use gcloud application-default login with cloud-platform scope."
        )

    models_csv = (os.environ.get("VERTEX_CHARACTER_SUGGEST_MODEL") or "").strip()
    if models_csv:
        models = [m.strip() for m in models_csv.split(",") if m.strip()]
    else:
        models = ["gemini-2.0-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash-001"]

    vt = (video_type or "influencer").lower().strip()
    co = (country or "").strip() or "unspecified"
    lang = (language or "en").strip() or "en"
    script = (prompt or "").strip()
    if len(script) > 12000:
        script = script[:12000].rstrip() + "\n[…]"

    g = (gender or "").strip().lower()[:1] if gender else ""
    gender_rule = ""
    if g == "f":
        gender_rule = (
            "MANDATORY GENDER (wizard): The user chose FEMALE for this video. "
            "All four suggestions MUST describe women or clearly female-presenting adults only. "
            "Do not suggest men, boys, or male-presenting characters. "
            "If the script mentions a male role, still output female-presenting creator looks that could host the same topic.\n\n"
        )
    elif g == "m":
        gender_rule = (
            "MANDATORY GENDER (wizard): The user chose MALE for this video. "
            "All four suggestions MUST describe men or clearly male-presenting adults only. "
            "Do not suggest women, girls, or female-presenting characters. "
            "If the script mentions a female role, still output male-presenting creator looks that could host the same topic.\n\n"
        )

    user_text = (
        gender_rule
        + "You are a casting assistant for short-form UGC and ad videos.\n\n"
        "The user wrote the block below as their MAIN VIDEO PROMPT (Studio step 3). It is the ONLY primary source. "
        "Do not invent a different product, story, or audience than what this text implies.\n\n"
        "Task: propose exactly 4 distinct CHARACTER LOOK lines for a REFERENCE PORTRAIT (image generation).\n\n"
        "Each string must be ONE concise paragraph (at most ~280 characters) describing ONLY:\n"
        "- Visible appearance: approximate age, face, hair, skin, expression, modest upper-body clothing, optional cultural/religious head covering if fitting the brief.\n"
        "- End every suggestion by stating clearly that this is a chest-up shot on a plain neutral white (or off-white) studio background — no environment.\n\n"
        "STORY-LINK REQUIREMENT: Each of the four lines must be clearly rooted in THIS script — not generic stock influencers. "
        "Name or imply 1–2 concrete cues from the story (topic, stakes, mood, audience) that justify that look's energy, wardrobe level, or facial seriousness. "
        "Examples: urgent news-like script → sharper, tired eyes and restrained clothing; playful beauty script → warmer expression and softer styling. "
        "Do not describe locations or actions to be painted; only use the script to choose **who** could credibly host it on camera.\n\n"
        "Do NOT include in any suggestion: streets, cities, weather, debris, documentary or selfie/camera language, "
        "phones or props, walking/actions, lighting moods that imply a location, or 'camera vibe' as a scene. "
        "Story and setting from the script will be shot later; this field is only the person's studio headshot look.\n\n"
        "Rules:\n"
        "- All four looks must feel like alternate casting choices for the **same** video idea in the script, not four unrelated defaults.\n"
        "- If the script gives no person cues, offer four diverse on-brand creator looks for the offer — still appearance + white studio only.\n"
        "- No celebrity or copyrighted character names.\n"
        f"- Write each suggestion in the same language as the main prompt when obvious; language hint: {lang}.\n\n"
        f"MAIN VIDEO PROMPT (step 3 — verbatim):\n---\n{script}\n---\n\n"
        f"Target language code (hint): {lang}\n"
        f"Country/market hint: {co}\n"
        f"Video type: {vt}\n"
        f"Wizard gender selection: {g if g in ('m', 'f') else 'not specified (infer from script if obvious, else stay neutral)'}\n\n"
        'Return ONLY valid JSON: {"suggestions":["s1","s2","s3","s4"]} with exactly four strings. '
        "No markdown fences, no other keys."
    )
    body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.35,
            "responseMimeType": "application/json",
        },
    }

    headers = _headers()

    last_err: Optional[str] = None
    for model in models:
        base = f"{template}/{model}:generateContent"
        url = f"{base}?key={api_key}" if api_key else base
        try:
            r = dl_requests.post(url, headers=headers, json=body, timeout=45)
            if r.status_code == 404:
                last_err = f"{model}: model not found (404)"
                continue
            if r.status_code >= 400:
                last_err = f"{model}: HTTP {r.status_code} {r.text[:240]}"
                continue
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                last_err = f"{model}: no candidates"
                continue
            text = (
                (cands[0].get("content") or {}).get("parts") or [{}]
            )[0].get("text", "")
            text = (text or "").strip()
            if not text:
                last_err = f"{model}: empty text"
                continue
            out = _parse_character_suggestions_json(text)
            if out:
                return out[:4]
            last_err = f"{model}: could not parse suggestions JSON"
        except Exception as exc:
            last_err = f"{model}: {exc}"
            continue

    raise ValueError(
        f"Character brief suggestions failed after trying {models!r}. Last error: {last_err or 'unknown'}"
    )


@app.post("/api/suggest-character-briefs", response_model=SuggestCharacterBriefsResponse)
async def suggest_character_briefs(
    req: SuggestCharacterBriefsRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Suggest short character-look lines from the main video prompt (cheap Vertex Gemini Flash / Flash-Lite)."""
    try:
        loop = asyncio.get_running_loop()
        suggestions = await loop.run_in_executor(
            None,
            lambda: _vertex_suggest_character_briefs_sync(
                req.prompt.strip(),
                req.language or "en",
                (req.country or "").strip() or None,
                req.video_type or "influencer",
                req.gender,
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if not suggestions:
        raise HTTPException(
            status_code=502,
            detail="Model returned no character suggestions. Try again or edit your script on step 3.",
        )
    return SuggestCharacterBriefsResponse(suggestions=suggestions)


def _require_character_library_backend():
    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    admin = getattr(supabase, "_admin_client", None)
    if not admin:
        raise HTTPException(
            status_code=503,
            detail="Character library requires SUPABASE_SERVICE_ROLE_KEY on the API server (same as user_videos inserts).",
        )


@app.get("/api/characters", response_model=List[CharacterRecord])
async def list_characters(
    tenant: Tenant = Depends(require_tenant),
    _studio_uid: str = Depends(require_studio_authenticated_user),
):
    """List saved characters for the signed-in Studio user."""
    _require_character_library_backend()
    rows = supabase.list_studio_characters(_studio_uid)
    return [_character_row_to_record(r) for r in rows]


@app.post("/api/characters", response_model=CharacterRecord)
async def create_character_entry(
    body: CreateCharacterRequest,
    tenant: Tenant = Depends(require_tenant),
    studio_uid: str = Depends(require_studio_authenticated_user),
):
    """Create a character library row for the signed-in Studio user."""
    _require_character_library_backend()
    payload = {
        "name": body.name.strip(),
        "source_type": body.source_type,
        "status": body.status or "active",
        "tags": body.tags or [],
        "thumbnail": body.thumbnail,
        "reference_images": body.reference_images or [],
        "voice_reference": body.voice_reference,
        "default_language": body.default_language,
        "preferred_formats": body.preferred_formats or [],
        "character_dna": body.character_dna or {},
        "style_json": body.style_json or {},
        "voice_profile": body.voice_profile or {},
    }
    row, create_err = supabase.create_studio_character(studio_uid, payload)
    if not row:
        detail_parts = []
        if create_err:
            detail_parts.append(f"DB error: {create_err[:700]}")
        detail_parts.extend(
            [
                "Could not save the character library row.",
                "Checklist: (1) Run migration `api_pipeline/migrations/002_studio_characters.sql` in your Supabase project.",
                "(2) SUPABASE_SERVICE_ROLE_KEY must be set on the API server.",
                "(3) JWT project mismatch: the Studio sign-in Supabase project must be the same project as SUPABASE_URL on the API server (user UUID must exist in auth.users of that project).",
            ]
        )
        raise HTTPException(status_code=502, detail=" | ".join(detail_parts))
    return _character_row_to_record(row)


@app.get("/api/characters/{character_id}", response_model=CharacterRecord)
async def get_character_entry(
    character_id: str,
    tenant: Tenant = Depends(require_tenant),
    studio_uid: str = Depends(require_studio_authenticated_user),
):
    _require_character_library_backend()
    row = supabase.get_studio_character(studio_uid, character_id)
    if not row:
        raise HTTPException(status_code=404, detail="Character not found")
    return _character_row_to_record(row)


@app.put("/api/characters/{character_id}", response_model=CharacterRecord)
async def update_character_entry(
    character_id: str,
    body: UpdateCharacterRequest,
    tenant: Tenant = Depends(require_tenant),
    studio_uid: str = Depends(require_studio_authenticated_user),
):
    _require_character_library_backend()
    patch = body.model_dump(exclude_none=True)
    row = supabase.update_studio_character(studio_uid, character_id, patch)
    if not row:
        raise HTTPException(status_code=404, detail="Character not found or nothing to update")
    return _character_row_to_record(row)


@app.delete("/api/characters/{character_id}")
async def delete_character_entry(
    character_id: str,
    tenant: Tenant = Depends(require_tenant),
    studio_uid: str = Depends(require_studio_authenticated_user),
):
    _require_character_library_backend()
    ok = supabase.delete_studio_character(studio_uid, character_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Character not found")
    return {"ok": True, "character_id": character_id}


@app.get("/api/voices", response_model=List[VoiceOption])
async def get_voices(language: str = "en", gender: Optional[str] = None, tenant: Tenant = Depends(require_tenant)):
    """Return ElevenLabs voice IDs from 11_labs.json; generic defaults if language not listed."""
    try:
        from tvd_pipeline.data_loader import get_elevenlabs_config
    except ImportError:
        return []
    cfg = get_elevenlabs_config()
    lang_voices = cfg.get("language_voices") or {}
    lang_key = (language or "en").strip()
    lang_entry = lang_voices.get(lang_key)
    if not lang_entry and "-" in lang_key:
        lang_entry = lang_voices.get(lang_key.split("-")[0].strip())
    defaults = cfg.get("default_voices") or {
        "female": "21m00Tcm4TlvDq8ikWAM",
        "male": "pNInz6obpgDQGcFmaJgB",
    }
    if not lang_entry:
        lang_entry = defaults
    result: List[VoiceOption] = []
    if gender is None:
        for g, voice_id in (("female", lang_entry.get("female")), ("male", lang_entry.get("male"))):
            if voice_id:
                label = "Male" if g == "male" else "Female"
                if lang_entry is defaults and lang_key not in lang_voices:
                    label = f"{label} (default multilingual)"
                result.append(VoiceOption(voice_id=voice_id, label=label))
    else:
        g = "male" if str(gender).lower() in ("m", "male") else "female"
        voice_id = lang_entry.get(g) if isinstance(lang_entry, dict) else None
        if not voice_id:
            voice_id = defaults.get(g)
        if voice_id:
            label = "Male" if g == "male" else "Female"
            if lang_key not in lang_voices and lang_entry is defaults:
                label = f"{label} (default multilingual)"
            elif lang_key not in lang_voices:
                label = f"{label} (default)"
            result.append(VoiceOption(voice_id=voice_id, label=label))
    return result


@app.post("/api/generate-vo", response_model=GenerateVoResponse)
async def generate_vo(req: GenerateVoRequest, tenant: Tenant = Depends(require_tenant)):
    """Generate voiceover audio via ElevenLabs and return a GCS URL. Uses lightweight TTS only (no full VideoSceneProcessor)."""
    try:
        from tvd_pipeline.config import Config as _TvdConfig
        from tvd_pipeline.services.elevenlabs import ElevenLabsService
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail="VO generation requires tvd_pipeline. Not available.",
        ) from e
    _el = ElevenLabsService(_TvdConfig().ELEVENLABS_API_KEY, openai_client=None)
    vt = str(req.video_type or "").strip().lower()
    expressive = vt in ("influencer", "personal-brand", "ugc-real", "ugc style video", "personal-service")
    word_segments = None
    if req.with_word_timestamps:
        tts_result = _el.text_to_speech_with_timestamps(
            text=req.vo_script,
            voice_id=req.voice_id,
            language=req.language,
        )
        if not tts_result or not tts_result[0]:
            raise HTTPException(status_code=502, detail="ElevenLabs TTS did not return audio")
        audio_bytes, word_segments = tts_result[0], tts_result[1]
    else:
        pause = 0.5 if expressive else 0.0
        audio_bytes = _el.text_to_speech(
            req.vo_script,
            req.voice_id,
            req.language,
            expressive=expressive,
            sentence_pause_seconds=pause,
        )
        if not audio_bytes:
            raise HTTPException(status_code=502, detail="ElevenLabs TTS did not return audio")
    vo_duration = None
    if word_segments:
        last = word_segments[-1]
        if isinstance(last, dict) and "end_time" in last:
            vo_duration = float(last["end_time"])
    elif req.vo_script:
        # Rough duration when timestamps are skipped (Studio preview path).
        wc = max(1, len(str(req.vo_script).split()))
        vo_duration = float(wc) / 2.5
    key_name = f"jobs/{req.job_id}/vo_audio.mp3" if req.job_id else f"studio_vo/{uuid.uuid4().hex}.mp3"
    gcs_url = None
    if services and services.gcs_storage and getattr(services.gcs_storage, "_initialized", False):
        gcs_url = services.gcs_storage.upload_audio_bytes(audio_data=audio_bytes, key_name=key_name)
    if not gcs_url:
        raise HTTPException(status_code=502, detail="Failed to upload VO audio to storage")
    return GenerateVoResponse(
        vo_audio_url=gcs_url,
        vo_duration=vo_duration,
        vo_word_segments=word_segments if req.with_word_timestamps else None,
    )


# ElevenLabs text-to-voice API: schema says max 1000 chars; stay under aggressively (unicodem / proxy quirks).
_ELEVENLABS_VOICE_DESCRIPTION_CHAR_MAX = 960


def _limit_words(text: str, max_words: int = 20) -> str:
    """Trim description to at most ``max_words`` words (ElevenLabs / casting blurbs)."""
    words = (text or "").strip().split()
    if len(words) <= max_words:
        return " ".join(words) if words else ""
    return " ".join(words[:max_words])


def _clamp_voice_description_for_elevenlabs(text: str, max_len: int = _ELEVENLABS_VOICE_DESCRIPTION_CHAR_MAX) -> str:
    """Ensure voice_description fits ElevenLabs limits; keep a trailing \"The voice should be …\" line when present."""
    text = (text or "").strip().replace("\r\n", "\n")
    if len(text) <= max_len:
        return text
    tail_marker = "\n\nThe voice should be "
    ti = text.rfind(tail_marker)
    if ti >= 0:
        tail = text[ti:]
        if len(tail) >= max_len:
            out = text[: max_len - 1].rstrip() + "…"
            return out if len(out) <= max_len else out[:max_len]
        head_room = max_len - len(tail)
        head = text[:ti].rstrip()
        if len(head) > head_room:
            head = head[: max(0, head_room - 1)].rstrip() + "…"
        combined = head + tail
        if len(combined) > max_len:
            combined = combined[: max_len - 1].rstrip() + "…"
        text = combined
    else:
        text = text[: max_len - 1].rstrip() + "…"
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text[:max_len] if len(text) > max_len else text


def _portrait_prompt_appearance_only(
    portrait_image_prompt: str, max_core_chars: int = 720
) -> str:
    """Strip Nano Banana / pipeline boilerplate; keep appearance lines for voice casting only.

    Full portrait prompts often append \"=== GLOBAL PIPELINE RULES ===\" and exceed ElevenLabs' voice-description
    limit even after naive truncation. Voice design only needs how the *person* looks and feels.
    """
    t = (portrait_image_prompt or "").strip().replace("\r\n", "\n")
    if not t:
        return ""
    # Portrait templates use "=== SECTION ===" blocks; voice casting must not include GLOBAL PIPELINE RULES etc.
    sec = re.search(r"(?m)^\s*={3,}", t)
    if sec:
        t = t[: sec.start()].strip()
    if not t:
        return ""
    lower = t.lower()
    cut_needles = (
        "\n=== ",
        "\n===",
        "\n---\n",
        "\nglobal pipeline",
        "=== global",
        "non-negotiable",
        "must never appear",
        "\nimportant:",
        "\ncritical rules",
    )
    cut_at = len(t)
    for needle in cut_needles:
        idx = lower.find(needle.lower())
        if idx >= 0:
            cut_at = min(cut_at, idx)
    t = t[:cut_at].strip()
    if len(t) > max_core_chars:
        t = t[: max_core_chars - 1].rstrip() + "…"
    return t


def _normalize_character_look_text_for_voice(raw: str) -> str:
    """Character look / library brief — user text only; strip pipeline boilerplate (\"=== …\") never sent to ElevenLabs."""
    t = _portrait_prompt_appearance_only((raw or "").strip(), max_core_chars=_ELEVENLABS_VOICE_DESCRIPTION_CHAR_MAX - 80)
    if not t:
        return ""
    t = re.sub(
        r"^Voice casting for this on-screen person[^:]{0,200}:\s*",
        "",
        t,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    return t


def _build_voice_description(
    language: str,
    gender: str,
    portrait_image_prompt: Optional[str],
    character_description: Optional[str],
    character_image_url: Optional[str],
) -> str:
    """Build ``voice_description`` for ElevenLabs design (hard cap ``_ELEVENLABS_VOICE_DESCRIPTION_CHAR_MAX``).

    Priority (Studio):
    1. **Character look** text (textarea or ``character_dna.character_brief`` from saved library) — as entered,
       minus any pasted ``===`` / GLOBAL PIPELINE blocks.
    2. **Reference / influencer image URL** — short Gemini caption (≤20 words), never the full Nano Banana prompt.
    3. **portrait_image_prompt** (AI-generated portrait only, no user image) — appearance slice before ``===``.
    4. Generic narrator fallback.
    """
    gender_word = "female" if str(gender).lower() in ("f", "female") else "male"
    lang_tail = f", {language} narration" if language and str(language).lower() not in ("en", "english", "") else ""

    if character_description and character_description.strip():
        raw_cd = character_description.strip().replace("\r\n", "\n")
        core = _normalize_character_look_text_for_voice(raw_cd)
        if not core:
            # Pasted-only pipeline template / empty after stripping — take any prose before first === block.
            cut = raw_cd.find("\n===")
            if cut < 0:
                cut = raw_cd.find("\n---\n")
            if cut < 0:
                cut = raw_cd.find("===")
            frag = raw_cd[:cut].strip() if cut > 0 else raw_cd.strip()
            core = (frag[:400].rstrip() + "…") if len(frag) > 400 else frag
        if not core:
            core = f"Clear, engaging {gender_word} narrator for short-form video"
        if lang_tail and lang_tail not in core.lower():
            core = f"{core}{lang_tail}"
        return _clamp_voice_description_for_elevenlabs(core)

    img_u = (character_image_url or "").strip()
    if img_u:
        try:
            description = _gemini_describe_character_image(img_u)
            if description:
                core = _limit_words(description.strip(), max_words=20)
                if lang_tail and lang_tail not in core.lower():
                    core = f"{core}{lang_tail}"
                return _clamp_voice_description_for_elevenlabs(core)
        except Exception as exc:
            logger.warning("Gemini image description for voice design failed: %s", exc)

    if portrait_image_prompt and portrait_image_prompt.strip():
        raw_pp = portrait_image_prompt.strip().replace("\r\n", "\n")
        # Strip "Voice casting for this on-screen person…" wrapper that some older prompt templates inject.
        raw_pp = re.sub(
            r"^Voice casting for this on-screen person[^:]{0,300}:\s*",
            "",
            raw_pp,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        core = _portrait_prompt_appearance_only(raw_pp, max_core_chars=420)
        if not core:
            core = raw_pp[:320].rstrip() + "…" if len(raw_pp) > 320 else raw_pp
        if lang_tail and lang_tail not in core.lower():
            spare = _ELEVENLABS_VOICE_DESCRIPTION_CHAR_MAX - len(core) - 1
            if spare > len(lang_tail):
                core = core + lang_tail
        return _clamp_voice_description_for_elevenlabs(core)

    return _clamp_voice_description_for_elevenlabs(
        f"Clear, engaging {gender_word} narrator for short-form video{lang_tail}."
    )


def _gemini_describe_character_image(image_url: str) -> Optional[str]:
    """Use a cheap Gemini Flash call (Vertex REST API) to describe a character image for voice casting."""
    try:
        from tvd_pipeline.config import Config as _TvdConfig
        cfg = _TvdConfig()
        api_key = cfg.VERTEX_AI_API_KEY or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return None
        import base64
        img_bytes = dl_requests.get(image_url, timeout=15)
        img_bytes.raise_for_status()
        img_b64 = base64.b64encode(img_bytes.content).decode()
        mime = img_bytes.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        model = "gemini-2.0-flash-lite"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        body = {
            "contents": [{
                "parts": [
                    {
                        "text": (
                            "For voice casting only. Reply with at most 20 words: age range, apparent gender, "
                            "energy (e.g. warm, calm, upbeat), and speaking vibe. No camera, lens, lighting, "
                            "or background description. No bullet points."
                        ),
                    },
                    {"inline_data": {"mime_type": mime, "data": img_b64}},
                ]
            }],
            "generationConfig": {"maxOutputTokens": 80, "temperature": 0.25},
        }
        resp = dl_requests.post(url, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        ).strip()
        if not text:
            return None
        return _limit_words(text, 20) or None
    except Exception as exc:
        logger.warning("_gemini_describe_character_image failed: %s", exc)
        return None


@app.post("/api/voice-design", response_model=VoiceDesignResponse)
async def voice_design(req: VoiceDesignRequest, tenant: Tenant = Depends(require_tenant)):
    """Call ElevenLabs Design-a-Voice, returning preview audio + generated_voice_ids.

    The voice_description is built automatically from character text (or a cheap Gemini image
    description when only an image URL is provided).  All previews are streamed back as
    base64 audio so the Studio player can audition them without an extra round-trip.
    """
    try:
        from tvd_pipeline.config import Config as _TvdConfig
        from tvd_pipeline.services.elevenlabs import ElevenLabsService
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="tvd_pipeline not available") from exc

    voice_description = _build_voice_description(
        language=req.language,
        gender=req.gender,
        portrait_image_prompt=req.portrait_image_prompt,
        character_description=req.character_description,
        character_image_url=req.character_image_url,
    )

    _cfg = _TvdConfig()
    _el_key = (_cfg.ELEVENLABS_API_KEY or "").strip()
    if not _el_key:
        raise HTTPException(
            status_code=503,
            detail="ELEVENLABS_API_KEY is not configured on the server (set env ELEVEN_LABS_API_KEY).",
        )
    _el = ElevenLabsService(_el_key, openai_client=None)
    result, el_err = _el.design_voice(
        voice_description=voice_description,
        auto_generate_text=req.auto_generate_text,
        loudness=req.loudness,
        guidance_scale=req.guidance_scale,
        seed=req.seed,
    )
    if not result:
        raise HTTPException(
            status_code=502,
            detail=el_err or "ElevenLabs voice design returned no previews",
        )

    previews = [
        VoiceDesignPreview(
            generated_voice_id=p.get("generated_voice_id", ""),
            audio_base_64=p.get("audio_base_64", ""),
            media_type=p.get("media_type", "audio/mpeg"),
            duration_secs=float(p.get("duration_secs", 0)),
            language=p.get("language"),
        )
        for p in result.get("previews", [])
    ]
    return VoiceDesignResponse(
        previews=previews,
        text=result.get("text", ""),
        voice_description=voice_description,
    )


@app.get("/api/voice-preview/{generated_voice_id}/stream")
async def voice_preview_stream(
    generated_voice_id: str,
    tenant: Tenant = Depends(require_tenant_or_token),
):
    """Proxy-stream preview audio for a generated_voice_id from ElevenLabs.

    Returns the raw audio bytes so the browser <audio> element can play it directly.
    """
    try:
        from tvd_pipeline.config import Config as _TvdConfig
        from tvd_pipeline.services.elevenlabs import ElevenLabsService
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="tvd_pipeline not available") from exc

    _el = ElevenLabsService(_TvdConfig().ELEVENLABS_API_KEY, openai_client=None)
    audio_bytes = _el.stream_voice_preview(generated_voice_id)
    if not audio_bytes:
        raise HTTPException(status_code=502, detail="ElevenLabs preview stream returned no data")

    return StreamingResponse(
        iter([audio_bytes]),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'inline; filename="{generated_voice_id}.mp3"'},
    )


@app.post("/api/voice-save", response_model=VoiceSaveResponse)
async def voice_save(req: VoiceSaveRequest, tenant: Tenant = Depends(require_tenant)):
    """Save a designed voice (generated_voice_id) to the ElevenLabs voice library.

    Converts a temporary generated_voice_id (from /api/voice-design) into a permanent
    voice_id that can be used for standard TTS calls in any pipeline.
    """
    try:
        from tvd_pipeline.config import Config as _TvdConfig
        from tvd_pipeline.services.elevenlabs import ElevenLabsService
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="tvd_pipeline not available") from exc

    _el = ElevenLabsService(_TvdConfig().ELEVENLABS_API_KEY, openai_client=None)
    _voice_desc = (req.voice_description or "").strip()
    if len(_voice_desc) < 20:
        _voice_desc = (_voice_desc + " Custom AI-designed studio voice for video")[:500]
    voice_id, save_err = _el.save_designed_voice(
        generated_voice_id=req.generated_voice_id,
        voice_name=req.voice_name,
        voice_description=_voice_desc,
    )
    if not voice_id:
        raise HTTPException(
            status_code=502,
            detail=save_err or "ElevenLabs could not save the designed voice",
        )

    return VoiceSaveResponse(voice_id=voice_id, voice_name=req.voice_name)


@app.post("/api/jobs/{job_id}/restart", response_model=GenerateVideoResponse)
async def restart_job(job_id: str, from_step: str, tenant: Tenant = Depends(require_tenant)):
    """Restart a job from a specific step. Clears intermediates from that step onward."""
    if not supabase or not executor:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)

    if job["status"] not in ("paused", "failed", "completed"):
        raise HTTPException(status_code=400, detail=f"Job status is '{job['status']}', must be 'paused', 'failed', or 'completed' to restart")

    # Prevent duplicate restart if job is already running
    with _running_jobs_lock:
        if job_id in _running_jobs:
            raise HTTPException(status_code=409, detail="Job is already running")

    # Record cursor so SSE reconnect skips old events
    cursor = event_store.event_count(job_id)

    vt = job["video_type"]
    steps = get_steps_for_type(vt)
    valid_step_ids = [s[0] for s in steps]
    if from_step not in valid_step_ids:
        raise HTTPException(status_code=400, detail=f"Invalid from_step '{from_step}'. Valid: {valid_step_ids}")

    # Clear intermediates from the restart step onward
    intermediates = dict(job.get("intermediates", {}))
    cleared = clear_intermediates_from_step(intermediates, vt, from_step)

    # Clear cost checkpoint entirely — re-run starts fresh cost tracking.
    # Previous run cost is preserved in generation_usage table.
    cleared.pop("cost_checkpoint", None)
    cleared.pop("cost_usd", None)

    # Find the step label for logging
    step_label = next((s[1] for s in steps if s[0] == from_step), from_step)

    # Update DB: reset intermediates, clear error, set processing
    from datetime import datetime, timezone
    supabase.client.table(supabase.table).update({
        "status": "processing",
        "intermediates": cleared,
        "output": None,
        "progress": 0,
        "current_step": f"restarting_from_{from_step}",
        "error": None,
        "error_details": None,
        "failed_at_step": None,
        "completed_at": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    params = dict(job.get("input_params", {}))
    pause_after_restart = _restart_pause_target(vt, from_step)
    if pause_after_restart is not None:
        params["pause_after_step"] = pause_after_restart
    executor.submit(_run_job, job_id, vt, params)

    event_store.push(job_id, "SERVER", f"Restarting from '{step_label}' — earlier steps will use cached results", event_type="start")
    return GenerateVideoResponse(
        job_id=job_id,
        status="processing",
        message=f"Job restarting from step '{step_label}'. Earlier results preserved.",
        event_cursor=cursor,
    )


@app.get("/api/jobs/{job_id}/steps")
async def get_job_steps(job_id: str, tenant: Tenant = Depends(require_tenant)):
    """Return the step definitions for a job's video type."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")

    job = supabase.verify_job_ownership(job_id, tenant.id)

    steps = get_steps_for_type(job["video_type"])
    return {"steps": [{"id": s[0], "label": s[1], "keys": s[2]} for s in steps]}


@app.get("/api/jobs/{job_id}/pipeline-events")
async def get_job_pipeline_events(
    job_id: str,
    after: int = 0,
    tenant: Tenant = Depends(require_tenant),
):
    """Recent pipeline events (same stream as SSE) for Studio log — poll with ?after=N."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")
    supabase.verify_job_ownership(job_id, tenant.id)
    from api_pipeline.event_store import event_store

    chunk, total = event_store.get_events_page(job_id, max(0, after), limit=80)
    return {
        "job_id": job_id,
        "after": after,
        "next_after": after + len(chunk),
        "total_events": total,
        "events": [e.to_dict() for e in chunk],
    }


@app.get("/api/jobs/{job_id}/logs")
async def get_job_logs(job_id: str, tenant: Tenant = Depends(require_tenant)):
    """Return fallback/warning logs captured for a job."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Services not initialized")
    supabase.verify_job_ownership(job_id, tenant.id)
    logs = fallback_store.get_logs(job_id)
    return {"job_id": job_id, "logs": logs, "count": len(logs)}


@app.get("/api/admin/server-logs/recent")
async def get_server_logs_recent(
    limit: int = Query(500, ge=1, le=5000),
    tenant: Tenant = Depends(require_tenant_or_token),
):
    """Last N lines of this API process logs (JSON). Same auth as the rest of the API (Bearer or ?token= sk-tvd).

    Logs are process-wide (all jobs), not filtered by tenant — intended for debugging."""
    _ = tenant  # auth only
    lines = get_recent_lines(limit)
    return {"lines": lines, "count": len(lines)}


@app.get("/api/admin/server-logs/stream")
async def stream_server_logs(
    tenant: Tenant = Depends(require_tenant_or_token),
):
    """SSE: server log lines in real time, plus an initial backlog.

    Use the same ``Authorization: Bearer`` or ``?token=`` API key as other endpoints (EventSource).
    Example: curl -N -H \"Authorization: Bearer sk-tvd-...\" http://localhost:8000/api/admin/server-logs/stream
    """
    _ = tenant  # auth only

    def _dequeue_line_or_none(subscriber_q, timeout: float):
        import queue as _queue

        try:
            return subscriber_q.get(timeout=timeout)
        except _queue.Empty:
            return None

    async def _event_generator():
        sub_q = register_subscriber()
        try:
            recent = get_recent_lines(400)
            yield f"data: {json.dumps({'kind': 'backlog', 'lines': recent}, ensure_ascii=False)}\n\n"
            while True:
                item = await asyncio.to_thread(_dequeue_line_or_none, sub_q, 25.0)
                if item is None:
                    yield ": keepalive\n\n"
                else:
                    yield f"data: {json.dumps({'kind': 'line', 'line': item}, ensure_ascii=False)}\n\n"
        finally:
            unregister_subscriber(sub_q)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _studio_sign_in_tenant_auth_ready() -> tuple[bool, str]:
    """Whether signed-in Studio users can omit Bearer (JWT + server-side tenant resolution).

    Returns (ready, short_english_hint_for_ui). Empty hint when ready.
    """
    if (os.environ.get("STUDIO_FALLBACK_API_KEY") or "").strip():
        return True, ""
    sr = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not sr:
        return (
            False,
            "Server: set SUPABASE_SERVICE_ROLE_KEY in api_pipeline/.env (Supabase secret), restart the API, then refresh.",
        )
    global supabase
    if supabase is None:
        return False, "API services not initialized; restart the video API server."
    resolver = getattr(supabase, "resolve_single_tenant_api_key_for_studio", None)
    if not callable(resolver):
        return (
            False,
            "Server is not using Supabase job store: paste a non-empty API key (e.g. dev) in the header.",
        )
    try:
        if resolver():
            return True, ""
        return (
            False,
            "Supabase: add exactly one active api_tenants row (sk-tvd-...), or set STUDIO_FALLBACK_API_KEY in .env, restart API.",
        )
    except Exception as e:
        logger.warning("_studio_sign_in_tenant_auth_ready: %s", e)
        return (
            False,
            "Server could not read api_tenants (check SUPABASE_SERVICE_ROLE_KEY and DB). See API server log.",
        )


@app.get("/api/config")
async def get_config():
    """Serve Supabase connection info, animation model config, and tier data for dashboard."""
    from api_pipeline.model_config import get_animation_models
    from api_pipeline.resolution_tiers import _TIERS
    # Strip _valid_values (reference-only, not needed by client)
    tiers = {k: v for k, v in _TIERS.items() if not k.startswith("_")}
    _sb_url, _anon = resolve_supabase_public_credentials()
    _studio_cloud = bool(_sb_url and _anon)
    _sign_ok, _sign_hint = _studio_sign_in_tenant_auth_ready()
    return {
        "supabase_url": _sb_url,
        # Browser client key (legacy anon or new publishable — same value from resolve_supabase_public_credentials)
        "supabase_anon_key": _anon,
        "supabase_publishable_key": _anon,
        "studio_cloud_available": _studio_cloud,
        "studio_auth_enabled": _studio_cloud
        and os.environ.get("STUDIO_REQUIRE_LOGIN", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        # True when cloud sign-in can replace pasted sk-tvd (service role + single tenant or STUDIO_FALLBACK_API_KEY)
        "studio_sign_in_only_ready": _sign_ok,
        "studio_tenant_auth_hint": _sign_hint if _studio_cloud else "",
        "animation_models": get_animation_models(),
        "resolution_tiers": tiers,
    }


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check."""
    global _active_jobs_cache
    now = time.time()
    if now < _active_jobs_cache["expires_at"]:
        active = _active_jobs_cache["count"]
    else:
        active = 0
        if supabase:
            try:
                active = supabase.count_active_jobs()
                _active_jobs_cache = {"count": active, "expires_at": now + ACTIVE_JOBS_CACHE_TTL}
            except Exception:
                pass

    return HealthResponse(
        status="ok",
        services_initialized=services is not None,
        active_jobs=active,
    )


@app.get("/api/health/services", response_model=ServiceHealthResponse)
async def health_services():
    """Check health of all external services. Results cached for 5 minutes."""
    global _health_cache

    now = time.time()
    if _health_cache["result"] and now < _health_cache["expires_at"]:
        cached = _health_cache["result"]
        cached.cached = True
        return cached

    if not services:
        raise HTTPException(status_code=503, detail="Services not initialized")

    import requests as _req
    cfg = services.config

    checks: List[ServiceStatus] = []

    # ElevenLabs
    t0 = time.time()
    try:
        r = _req.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": cfg.ELEVENLABS_API_KEY}, timeout=10)
        checks.append(ServiceStatus(name="elevenlabs", status="healthy" if r.ok else "unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=None if r.ok else f"HTTP {r.status_code}"))
    except Exception as e:
        checks.append(ServiceStatus(name="elevenlabs", status="unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=str(e)))

    # Rendi
    t0 = time.time()
    try:
        rendi_key = cfg.RENDI_API_KEY
        r = _req.get(f"{cfg.RENDI_BASE_URL}/health", headers={"x-api-key": rendi_key}, timeout=10)
        checks.append(ServiceStatus(name="rendi", status="healthy" if r.ok else "unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=None if r.ok else f"HTTP {r.status_code}"))
    except Exception as e:
        checks.append(ServiceStatus(name="rendi", status="unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=str(e)))

    # Kie.ai
    t0 = time.time()
    try:
        r = _req.get("https://api.kie.ai/v1/balance", headers={"api-key": cfg.KIE_API_KEY}, timeout=10)
        checks.append(ServiceStatus(name="kie_ai", status="healthy" if r.ok else "unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=None if r.ok else f"HTTP {r.status_code}"))
    except Exception as e:
        checks.append(ServiceStatus(name="kie_ai", status="unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=str(e)))

    # ZapCap
    t0 = time.time()
    try:
        zapcap_key = cfg.ZAPCAP_API_KEY
        if zapcap_key:
            r = _req.get("https://api.zapcap.ai/api/v1/videos", headers={"x-api-key": zapcap_key}, timeout=10)
            checks.append(ServiceStatus(name="zapcap", status="healthy" if r.ok else "unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=None if r.ok else f"HTTP {r.status_code}"))
        else:
            checks.append(ServiceStatus(name="zapcap", status="unknown", error="Not configured"))
    except Exception as e:
        checks.append(ServiceStatus(name="zapcap", status="unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=str(e)))

    # OpenAI
    t0 = time.time()
    try:
        from openai import OpenAI as _OAI
        _oai = _OAI(api_key=cfg.OPENAI_API_KEY)
        _oai.models.list()
        checks.append(ServiceStatus(name="openai", status="healthy", latency_ms=round((time.time() - t0) * 1000, 1)))
    except Exception as e:
        checks.append(ServiceStatus(name="openai", status="unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=str(e)))

    # Local FFmpeg
    t0 = time.time()
    try:
        import subprocess
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        installed = result.returncode == 0
        checks.append(ServiceStatus(name="local_ffmpeg", status="healthy" if installed else "unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=None if installed else "FFmpeg not found"))
    except Exception as e:
        checks.append(ServiceStatus(name="local_ffmpeg", status="unhealthy", latency_ms=round((time.time() - t0) * 1000, 1), error=str(e)))

    # Determine overall
    statuses = [c.status for c in checks]
    unhealthy_count = statuses.count("unhealthy")
    if unhealthy_count == 0:
        overall = "healthy"
    elif unhealthy_count <= 2:
        overall = "degraded"
    else:
        overall = "unhealthy"

    result = ServiceHealthResponse(
        overall=overall,
        services=checks,
        cached=False,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )

    _health_cache["result"] = result
    _health_cache["expires_at"] = now + HEALTH_CACHE_TTL

    return result


# ---------------------------------------------------------------------------
# SSE endpoint — real-time event streaming
# ---------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, after: int = 0, tenant: Tenant = Depends(require_tenant_or_token)):
    """Stream pipeline events via Server-Sent Events (SSE).

    Query params:
        after: Skip events before this index (used on resume/restart to avoid replay).
        token: API key for SSE (EventSource API can't set custom headers).
    """
    # Verify ownership before streaming
    if supabase:
        supabase.verify_job_ownership(job_id, tenant.id)

    async def _event_generator():
        cursor = after
        # 'pause' is not terminal — the job will resume. Closing on pause caused EventSource
        # to reconnect every ~1s, which woke pollJob() and produced ~8 req/s polling storms.
        terminal_types = {"complete", "error", "abort"}
        notify_types = terminal_types | {"pause"}

        while True:
            # Get new events from cursor
            events = event_store.get_events(job_id, cursor)
            if events:
                for ev in events:
                    data = json.dumps(ev.to_dict())
                    yield f"data: {data}\n\n"
                    cursor += 1
                    # If terminal event, send done and close
                    if ev.event_type in terminal_types:
                        yield f"event: done\ndata: {data}\n\n"
                        return

            # Wait for new events (with timeout for keepalive)
            waiter = event_store.get_waiter(job_id)
            try:
                got_event = await asyncio.to_thread(waiter.wait, 25)
            except Exception:
                got_event = False

            if got_event:
                event_store.clear_waiter(job_id)
            else:
                # Keepalive comment
                yield ": keepalive\n\n"

                # Fallback: check DB status in case event was missed
                if supabase:
                    try:
                        job = supabase.get_job(job_id)
                        if job and job.get("status") in ("completed", "failed", "aborted", "paused"):
                            # Push a synthetic notification event if none exists
                            db_status = job["status"]
                            latest = event_store.get_events(job_id, max(0, cursor - 1))
                            already_notified = any(e.event_type in notify_types for e in latest)
                            if not already_notified:
                                etype = {"completed": "complete", "failed": "error", "aborted": "abort", "paused": "pause"}.get(db_status, "error")
                                msg = job.get("error", f"Job {db_status}") if db_status == "failed" else f"Job {db_status}"
                                event_store.push(job_id, "SERVER", msg, event_type=etype,
                                                 progress=100 if db_status == "completed" else -1)
                    except Exception:
                        pass

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Studio chat agent
# ---------------------------------------------------------------------------
from pydantic import BaseModel as _ChatBaseModel
from api_pipeline import chat_agent as _chat_agent
from api_pipeline.cost_tracker import estimate_cost as _estimate_cost


class _ChatStartRequest(_ChatBaseModel):
    initial_message: Optional[str] = None


class _ChatMessageRequest(_ChatBaseModel):
    session_id: str
    message: str = ""
    attachments: Optional[List[Dict[str, Any]]] = None


class _ChatCommitRequest(_ChatBaseModel):
    session_id: str
    simulation: bool = False
    simulation_duration: Optional[str] = None


class _ChatBuildStoryboardRequest(_ChatBaseModel):
    session_id: str


class _ChatDirectStoryboardRequest(_ChatBaseModel):
    session_id: str


class _ChatSetModeRequest(_ChatBaseModel):
    session_id: str
    mode: str  # "concierge" | "director"


class _ChatRenderPreviewsRequest(_ChatBaseModel):
    session_id: str


class _ChatRerollPreviewRequest(_ChatBaseModel):
    session_id: str
    scene_idx: int
    preview_image_model: Optional[str] = None  # optional model swap before re-roll
    first_prompt: Optional[str] = None  # optional prompt edit before re-roll


class _ChatRerollSceneVideoOverrides(_ChatBaseModel):
    first_prompt: Optional[str] = None
    motion_prompt: Optional[str] = None
    video_model_override: Optional[str] = None
    video_provider_override: Optional[str] = None


class _ChatRerollSceneVideoRequest(_ChatBaseModel):
    session_id: str
    scene_idx: int
    # Single-clip assumption: overrides apply to clips[0] of the scene.
    overrides: Optional[_ChatRerollSceneVideoOverrides] = None


class _ChatPatchStoryboardRequest(_ChatBaseModel):
    session_id: str
    storyboard: Dict[str, Any]


class _ChatCommitCustomRequest(_ChatBaseModel):
    session_id: str
    simulation: bool = False
    simulation_duration: Optional[str] = None
    output_resolution: Optional[str] = None


@app.post("/api/studio-chat/start")
async def studio_chat_start(
    req: _ChatStartRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Open a new chat session. Returns the session id and the agent's first reply."""
    session = _chat_agent.create_session(tenant.id, initial_message=req.initial_message)

    if req.initial_message:
        # Drive one turn so the agent greets in context of the user's first message
        # (create_session already appended the user message; chat_turn would double-append)
        # — instead, pop it and let chat_turn re-add via its own path.
        first = session.messages.pop()
        envelope = _chat_agent.chat_turn(session, first.get("content", ""), attachments=None)
    else:
        # Open with a friendly default greeting (no LLM call)
        envelope = {
            "reply": "Hey — I'm your VidBuddy concierge. Tell me about the video you want to make.",
            "detected_language": "en",
            "slots_update": {},
            "ui_action": {"type": "none", "panel": "none"},
            "needs_more_info": True,
            "missing_fields": [],
        }
        session.messages.append({"role": "assistant", "content": envelope["reply"]})

    return {
        "session_id": session.session_id,
        "envelope": envelope,
        "slots": dict(session.slots),
    }


@app.post("/api/studio-chat/message")
async def studio_chat_message(
    req: _ChatMessageRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Send a message in an existing chat session. Returns the agent's structured envelope."""
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if not (req.message or req.attachments):
        raise HTTPException(status_code=400, detail="message or attachments required")

    envelope = _chat_agent.chat_turn(session, req.message or "", attachments=req.attachments)

    # If the agent wants to show the summary card, enrich it with a real cost estimate
    ui_action = envelope.get("ui_action") or {}
    if ui_action.get("type") == "show_summary":
        summary = ui_action.get("summary") or {}
        try:
            est = _estimate_cost(
                video_type=session.slots.get("video_type", "product video"),
                duration=int(session.slots.get("duration") or 20),
                animation_model=session.slots.get("animation_model", "auto"),
            )
            summary.update(est)
            ui_action["summary"] = summary
            envelope["ui_action"] = ui_action
        except Exception as e:
            logger.warning("Cost estimate failed: %s", e)

    return {
        "envelope": envelope,
        "slots": dict(session.slots),
        "job_id": session.job_id,
    }


@app.get("/api/studio-chat/session/{session_id}")
async def studio_chat_get_session(
    session_id: str,
    tenant: Tenant = Depends(require_tenant),
):
    """Restore a chat session (full history + current slots)."""
    session = _chat_agent.get_session(session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    return _chat_agent.session_to_dict(session)


_REQUIRED_SLOTS_PER_PIPELINE: Dict[str, List[str]] = {
    "product video": ["prompt"],
    "influencer": ["prompt", "gender", "business_name", "character_urls"],
    "personal-brand": ["prompt", "gender", "character_urls"],
    "ugc-real": ["prompt"],
}


def _missing_required_slots(slots: Dict[str, Any]) -> List[str]:
    """Return the list of required slots that are missing for the picked video_type."""
    vt = (slots.get("video_type") or "").lower().strip()
    required = _REQUIRED_SLOTS_PER_PIPELINE.get(vt, [])
    missing = []
    for f in required:
        v = slots.get(f)
        if v is None:
            missing.append(f)
        elif isinstance(v, str) and not v.strip():
            missing.append(f)
        elif isinstance(v, (list, tuple)) and len(v) == 0:
            missing.append(f)
    return missing


@app.post("/api/studio-chat/commit", response_model=GenerateVideoResponse)
async def studio_chat_commit(
    req: _ChatCommitRequest,
    request: Request,
    tenant: Tenant = Depends(require_tenant),
):
    """Validate the gathered slots and kick off /api/generate.

    This is the only path that turns a chat conversation into real billing.
    The chat UI calls it after the user clicks the Generate button on the
    summary card.
    """
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if session.job_id:
        raise HTTPException(status_code=409, detail=f"Session already has job {session.job_id}")

    slots = dict(session.slots or {})
    if not slots.get("video_type"):
        raise HTTPException(status_code=422, detail="Cannot generate yet — agent has not picked a video_type")
    if not slots.get("prompt"):
        raise HTTPException(status_code=422, detail="Cannot generate yet — prompt is empty")

    # Pre-flight required-slot validation. If anything's missing, push the gap
    # back through the chat so the agent collects it (instead of letting Pydantic
    # blow up with a wall of error JSON the user can't act on).
    missing = _missing_required_slots(slots)
    if missing:
        # Inject a synthetic system note so the agent knows what to ask next,
        # then run one chat turn so the user gets a friendly question.
        nudge = (
            "[SYSTEM NOTE] The user just clicked Generate, but these required "
            f"fields are still missing for the {slots.get('video_type')} pipeline: "
            f"{', '.join(missing)}. Apologize briefly (in the user's language), "
            "then ask for the FIRST missing field only. Do NOT show summary again "
            "until every required field is filled."
        )
        envelope = _chat_agent.chat_turn(session, nudge, attachments=None)
        # Hide the system note from the user's transcript by removing it.
        try:
            for i in range(len(session.messages) - 1, -1, -1):
                if session.messages[i].get("content", "").startswith("[SYSTEM NOTE]"):
                    session.messages.pop(i)
                    break
        except Exception:
            pass
        raise HTTPException(
            status_code=422,
            detail={
                "error": "missing_required_slots",
                "missing_fields": missing,
                "agent_reply": envelope.get("reply", ""),
                "ui_action": envelope.get("ui_action", {"type": "none", "panel": "none"}),
            },
        )

    # Map list slots to the field names GenerateVideoRequest expects
    payload: Dict[str, Any] = {
        "video_type": slots["video_type"],
        "prompt": slots["prompt"],
        "duration": int(slots.get("duration") or 20),
        "language": slots.get("language") or "en",
        "country": slots.get("country") or "",
        "style": slots.get("style") or "Auto",
        "simulation": bool(req.simulation),
    }
    if req.simulation_duration:
        payload["simulation_duration"] = req.simulation_duration
    for opt in ("gender", "business_name", "slogan_text", "logo_url", "voice_id"):
        if slots.get(opt):
            payload[opt] = slots[opt]
    for list_field in ("character_urls", "product_image_urls", "reference_image_urls"):
        if slots.get(list_field):
            payload[list_field] = list(slots[list_field])
    # Single-character convenience: if character_urls has exactly one, also fill character_url
    if slots.get("character_urls") and not slots.get("character_url"):
        urls = slots["character_urls"]
        if isinstance(urls, list) and urls:
            payload["character_url"] = urls[0]
    # asset_urls expects [{url, type}] objects — wrap raw strings
    if slots.get("asset_urls"):
        wrapped = []
        for a in slots["asset_urls"]:
            if isinstance(a, dict):
                wrapped.append(a)
            elif isinstance(a, str):
                wrapped.append({"url": a, "type": "image"})
        payload["asset_urls"] = wrapped

    try:
        gen_req = GenerateVideoRequest(**payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Slot validation failed: {e}")

    # Reuse the real generate endpoint — auth, normalization, queueing, everything
    response = await generate_video(req=gen_req, request=request, tenant=tenant)

    # Stash the job_id on the session so future chat turns can reference it
    job_id = getattr(response, "job_id", None) or (response.get("job_id") if isinstance(response, dict) else None)
    if job_id:
        _chat_agent.attach_job(req.session_id, tenant.id, job_id)

    return response


# ---------------------------------------------------------------------------
# Custom storyboard routes (chat → storyboard → custom pipeline)
# ---------------------------------------------------------------------------

# P1.5: convert raw validator messages into plain-English summaries the UI can show
def _friendly_storyboard_errors(errors: list) -> list:
    """Translate validator messages into one-line plain-English summaries."""
    if not errors:
        return []
    out = []
    seen = set()
    for err in errors:
        msg = str(err)
        # Common patterns → friendlier hint
        if "voiceover.script" in msg and "required" in msg.lower():
            hint = "Missing voiceover script — the agent forgot to write the narration."
        elif "scenes must be a non-empty" in msg:
            hint = "Storyboard has no scenes — the agent's plan was empty."
        elif "meta.target_duration_seconds" in msg:
            hint = "Video duration is missing or invalid."
        elif "preview_image_model" in msg and "must be one of" in msg:
            hint = "An invalid image model name was picked for one scene."
        elif "video_model_override" in msg and "must be one of" in msg:
            hint = "An invalid video model name was picked for one clip."
        elif "type=asset_video requires source.asset_video_index" in msg:
            hint = "A clip wants to use uploaded video but doesn't say which one."
        elif "type=asset_image_animate requires source.reference_image_index" in msg:
            hint = "A clip wants to animate an uploaded photo but doesn't say which one."
        elif "type=generate requires first_prompt" in msg:
            hint = "An AI-generated scene is missing its visual description."
        elif "type=motion_graphic requires first_prompt" in msg:
            hint = "A motion-graphic scene is missing its visual description."
        elif "clip durations sum to" in msg and "off by" in msg:
            # Extract scene index for clarity
            import re as _re
            m = _re.search(r"scenes\[(\d+)\]", msg)
            sc = (int(m.group(1)) + 1) if m else "?"
            hint = f"Scene {sc}: clip durations don't add up to the scene duration."
        elif "duration must be positive" in msg:
            hint = "One of the scenes or clips has an invalid duration."
        elif "tool_hint must be one of" in msg:
            hint = "An invalid animation tool was picked for one clip."
        else:
            # Generic — drop the deep paths so the user sees readable text
            hint = msg.replace("scenes[", "Scene ").replace(".clips[", " clip ").replace("]", "")
        if hint not in seen:
            seen.add(hint)
            out.append(hint)
    return out


@app.post("/api/studio-chat/mode")
async def studio_chat_set_mode(
    req: _ChatSetModeRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Switch a chat session between 'concierge' (slot-filling wizard) and
    'director' (Gemini 3 Pro plans the whole storyboard) modes."""
    ok = _chat_agent.set_mode(req.session_id, tenant.id, req.mode)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid mode or session not found")
    return {"session_id": req.session_id, "mode": req.mode}


@app.post("/api/studio-chat/direct-storyboard")
async def studio_chat_direct_storyboard(
    req: _ChatDirectStoryboardRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Run the Director (Gemini 3 Pro) on the session's gathered slots and
    return a full storyboard JSON. Replaces any prior storyboard on the
    session. Caller (UI) renders the storyboard for review/edit, then PATCHes
    edits and POSTs to /commit-custom to execute.

    Unlike /build-storyboard (the Concierge's lighter builder), this uses
    Gemini 3 Pro with the richer Director schema (character_sheet, venue_sheet,
    style_sheet, per-scene camera intent, seedance/motion_graphic clip types).
    """
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if not session.slots.get("prompt") and not (session.messages and len(session.messages) > 0):
        raise HTTPException(
            status_code=422,
            detail="Cannot direct yet — gather at least a brief or one user message first",
        )

    from api_pipeline.director import direct_storyboard
    storyboard = direct_storyboard(session)
    if not storyboard:
        raise HTTPException(status_code=502, detail="Director returned no storyboard — try refining the brief")

    # P1.3: Director may return a needs_assets payload INSTEAD of a storyboard,
    # asking the user to upload key media first. Surface to chat as a message
    # + UI action so the user can upload, then re-trigger Director.
    if storyboard.get("_is_needs_assets"):
        needs = storyboard.get("needs_assets") or []
        reply = storyboard.get("reply") or "I need a few uploads from you to make this video."
        # Append the reply to chat history so the user sees it on next poll
        try:
            session.messages.append({"role": "assistant", "content": reply})
        except Exception:
            pass
        # Pick the FIRST asset's panel as the UI action (multi-asset support: UI will iterate)
        first_panel = needs[0].get("panel") if needs else "uploads_assets"
        return {
            "needs_assets": needs,
            "reply": reply,
            "ui_action": {"type": "request_upload", "panel": first_panel},
            "mode": "director",
            "phase": "preflight",
        }

    # Strict validation now so the caller fails fast on schema issues.
    from tvd_pipeline.pipelines._storyboard import validate_storyboard
    errors = validate_storyboard(storyboard)
    if errors:
        # P1.5: surface friendly summaries of the validation errors so the UI
        # can show "missing voiceover" instead of "voiceover.script is required".
        friendly = _friendly_storyboard_errors(errors)
        return {
            "storyboard": storyboard,
            "validation_errors": errors,
            "friendly_errors": friendly,
            "valid": False,
        }

    from api_pipeline.cost_tracker import estimate_storyboard_cost
    cost = estimate_storyboard_cost(storyboard)

    return {
        "storyboard": storyboard,
        "validation_errors": [],
        "valid": True,
        "cost_estimate": cost,
        "mode": "director",
    }


@app.post("/api/studio-chat/render-storyboard-previews")
async def studio_chat_render_storyboard_previews(
    req: _ChatRenderPreviewsRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Render image previews for every scene of the session's storyboard.

    Each scene's chosen `preview_image_model` renders ONE first-frame image.
    Results are written back to `session.storyboard.scenes[i].preview_image_url`
    so the same storyboard can be retrieved later with previews populated.
    """
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if not session.storyboard:
        raise HTTPException(
            status_code=422,
            detail="No storyboard on session — call /direct-storyboard (Director) or /build-storyboard (Concierge) first",
        )

    # Lazy-import to keep server startup fast and avoid pulling tvd_pipeline
    # at import time when api_pipeline is loaded standalone.
    from tvd_pipeline.processor import VideoSceneProcessor
    from api_pipeline.storyboard_previews import render_storyboard_previews
    processor = VideoSceneProcessor()
    summary = render_storyboard_previews(
        processor,
        session.storyboard,
        max_workers=4,
    )

    # Mark session as touched so it doesn't expire mid-review
    session.last_active = time.time()

    return {
        "session_id": session.session_id,
        "previews": summary["previews"],
        "rendered": summary["rendered"],
        "failed": summary["failed"],
        "elapsed_seconds": summary["elapsed_seconds"],
        "model_used": summary["model_used"],
        "storyboard": session.storyboard,  # echo back so UI gets the URLs in one round-trip
    }


@app.post("/api/studio-chat/reroll-scene-preview")
async def studio_chat_reroll_scene_preview(
    req: _ChatRerollPreviewRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Re-roll a single scene's preview image. The user can optionally swap the
    model or edit the visual prompt before re-rolling."""
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if not session.storyboard:
        raise HTTPException(status_code=422, detail="No storyboard on session")
    scenes = session.storyboard.get("scenes") or []
    if req.scene_idx < 0 or req.scene_idx >= len(scenes):
        raise HTTPException(
            status_code=400,
            detail=f"scene_idx {req.scene_idx} out of range (storyboard has {len(scenes)} scenes)",
        )

    scene = scenes[req.scene_idx]
    if req.preview_image_model:
        # Validate against the same enum the storyboard validator uses
        from tvd_pipeline.pipelines._storyboard import IMAGE_MODEL_NAMES
        if req.preview_image_model not in IMAGE_MODEL_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"preview_image_model must be one of {sorted(IMAGE_MODEL_NAMES)}",
            )
        scene["preview_image_model"] = req.preview_image_model
    if req.first_prompt and scene.get("clips"):
        scene["clips"][0]["first_prompt"] = req.first_prompt

    # Clear any prior preview_image_url so the executor regenerates
    scene.pop("preview_image_url", None)

    from tvd_pipeline.processor import VideoSceneProcessor
    from api_pipeline.storyboard_previews import render_storyboard_previews
    processor = VideoSceneProcessor()
    summary = render_storyboard_previews(
        processor,
        session.storyboard,
        only_scenes=[req.scene_idx],
        max_workers=1,
    )

    session.last_active = time.time()

    return {
        "session_id": session.session_id,
        "scene_idx": req.scene_idx,
        "preview_url": summary["previews"].get(req.scene_idx),
        "model_used": summary["model_used"].get(req.scene_idx),
        "elapsed_seconds": summary["elapsed_seconds"],
    }


@app.post("/api/studio-chat/reroll-scene-video")
async def studio_chat_reroll_scene_video(
    req: _ChatRerollSceneVideoRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Re-roll a single scene's VIDEO (I2V from its preview image).

    KNOWN GAP: This endpoint assumes ONE clip per scene (operates on
    ``clips[0]``). Multi-clip scenes are not supported here — they will only
    have their first clip rerolled and the others left untouched. The full
    pipeline (``ugc.py:generate_scene_visual``) handles multi-clip scenes;
    this reroll path is intentionally scoped to the storyboard-preview UI.

    Flow:
      1. Validate session + scene_idx.
      2. Apply caller overrides to ``scenes[scene_idx].clips[0]``.
      3. Call ``render_scene_video`` which:
         - Uses ``scene.preview_image_url`` as the I2V start frame.
         - Resolves video_model/provider (override > Composer stamp > tool fallback).
         - Calls ``processor._generate_video(...)`` directly.
         - Rehosts the result to GCS for a stable URL.
      4. Write the resulting URL back to
         ``scenes[scene_idx].clips[0]._reroll_video_url`` (in-memory; not
         persisted to DB, same as /reroll-scene-preview).
      5. Append a director_note explaining what was rerolled so the UI badge
         updates.

    UI wiring is left as a documented next step — this endpoint just exposes
    the capability.
    """
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if not session.storyboard:
        raise HTTPException(status_code=422, detail="No storyboard on session")
    scenes = session.storyboard.get("scenes") or []
    if req.scene_idx < 0 or req.scene_idx >= len(scenes):
        raise HTTPException(
            status_code=400,
            detail=f"scene_idx {req.scene_idx} out of range (storyboard has {len(scenes)} scenes)",
        )

    scene = scenes[req.scene_idx]
    if not (scene.get("clips") or []):
        raise HTTPException(
            status_code=422,
            detail=f"scene {req.scene_idx} has no clips — cannot reroll video",
        )
    if not scene.get("preview_image_url"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"scene {req.scene_idx} has no preview_image_url — call "
                "/api/studio-chat/reroll-scene-preview (or /render-storyboard-previews) first"
            ),
        )

    # Reject framework_render clips per the investigation note (no executor yet).
    first_clip = scene["clips"][0]
    if first_clip.get("type") == "framework_render":
        raise HTTPException(
            status_code=422,
            detail=(
                "framework_render clips are not supported by /reroll-scene-video yet "
                "(blocked on D5/D7 roadmap). The composer currently maps them to a "
                "Ken Burns placeholder during full pipeline runs."
            ),
        )

    overrides_payload: Optional[Dict[str, Any]] = None
    if req.overrides is not None:
        overrides_payload = req.overrides.model_dump(exclude_none=True)

    from tvd_pipeline.processor import VideoSceneProcessor
    from api_pipeline.storyboard_previews import render_scene_video
    from api_pipeline.cost_tracker import estimate_scene_video_cost

    processor = VideoSceneProcessor()
    result = render_scene_video(
        processor,
        scene,
        storyboard=session.storyboard,
        overrides=overrides_payload,
    )

    if not result.get("video_url"):
        # Bubble up the failure but keep the session intact so the user can retry.
        raise HTTPException(
            status_code=502,
            detail=result.get("error") or "scene video reroll failed (no URL returned)",
        )

    # Write URL back to the session's storyboard so subsequent calls (commit,
    # render, etc.) see the rerolled output. Same ephemeral-memory contract as
    # /reroll-scene-preview — not persisted to DB until commit-custom.
    first_clip["_reroll_video_url"] = result["video_url"]
    first_clip["_reroll_video_model"] = result["model_used"]
    first_clip["_reroll_video_provider"] = result["provider_used"]

    # Append a director_note so the UI badge updates with the rationale.
    overrides_summary_parts: List[str] = []
    if overrides_payload:
        if overrides_payload.get("video_model_override"):
            overrides_summary_parts.append(f"model={overrides_payload['video_model_override']}")
        if overrides_payload.get("motion_prompt"):
            overrides_summary_parts.append("motion edited")
        if overrides_payload.get("first_prompt"):
            overrides_summary_parts.append("prompt edited")
    overrides_summary = (", " + ", ".join(overrides_summary_parts)) if overrides_summary_parts else ""
    first_clip["director_note"] = (
        f"Rerolled video via {result['model_used']} ({result['provider_used']}{overrides_summary})."
    )[:100]

    # Per-clip cost estimate (separate from full storyboard estimate so the UI
    # can show "this reroll cost ~$X" without double-counting).
    cost_estimate = estimate_scene_video_cost(
        first_clip,
        storyboard_meta=(session.storyboard.get("meta") or {}),
    )

    session.last_active = time.time()

    return {
        "session_id": session.session_id,
        "scene_idx": req.scene_idx,
        "video_url": result["video_url"],
        "raw_url": result["raw_url"],
        "model_used": result["model_used"],
        "provider_used": result["provider_used"],
        "duration": result["duration"],
        "elapsed_seconds": result["elapsed_seconds"],
        "cost_estimate": cost_estimate,
        "director_note": first_clip["director_note"],
        "known_gaps": [
            "single-clip assumption: only scenes[scene_idx].clips[0] is rerolled",
            "framework_render clip types rejected with 422 (no executor wired yet)",
            "session storyboard mutations are in-memory only until commit-custom",
        ],
    }


@app.post("/api/studio-chat/build-storyboard")
async def studio_chat_build_storyboard(
    req: _ChatBuildStoryboardRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Call the storyboard_builder LLM and return a full storyboard JSON.

    Idempotent: re-runs from the session's current slots. Replaces any prior
    storyboard on the session. The chat UI typically calls this after the
    chat agent has gathered enough info and the user opts into the custom flow.
    """
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if not session.slots.get("prompt"):
        raise HTTPException(status_code=422, detail="Cannot build storyboard yet — gather a prompt first")

    storyboard = _chat_agent.build_storyboard(session)
    if not storyboard:
        raise HTTPException(status_code=502, detail="Storyboard builder returned no result; try again")

    # Validate now so the caller fails fast on schema errors.
    from tvd_pipeline.pipelines._storyboard import validate_storyboard
    errors = validate_storyboard(storyboard)
    if errors:
        # Keep the draft on the session so the UI can show it for manual fixes,
        # but surface the issues to the caller.
        return {
            "storyboard": storyboard,
            "validation_errors": errors,
            "valid": False,
        }

    # Honest cost estimate based on the actual scene + clip plan.
    from api_pipeline.cost_tracker import estimate_storyboard_cost
    cost = estimate_storyboard_cost(storyboard)

    return {
        "storyboard": storyboard,
        "validation_errors": [],
        "valid": True,
        "cost_estimate": cost,
    }


@app.patch("/api/studio-chat/storyboard")
async def studio_chat_patch_storyboard(
    req: _ChatPatchStoryboardRequest,
    tenant: Tenant = Depends(require_tenant),
):
    """Replace the session's storyboard with a user-edited version.

    The UI sends back the full edited storyboard (after the user reorders scenes,
    swaps clip types, pins tool_hints, etc.). Server validates and stores it.
    """
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")

    from tvd_pipeline.pipelines._storyboard import validate_storyboard
    errors = validate_storyboard(req.storyboard)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_storyboard", "validation_errors": errors},
        )

    session.storyboard = req.storyboard
    session.last_active = time.time()

    from api_pipeline.cost_tracker import estimate_storyboard_cost
    return {
        "storyboard": req.storyboard,
        "valid": True,
        "cost_estimate": estimate_storyboard_cost(req.storyboard),
    }


@app.post("/api/studio-chat/commit-custom", response_model=GenerateVideoResponse)
async def studio_chat_commit_custom(
    req: _ChatCommitCustomRequest,
    request: Request,
    tenant: Tenant = Depends(require_tenant),
):
    """Submit the session's approved storyboard for execution via the custom pipeline.

    Unlike `/commit`, this routes to `video_type="custom"` and embeds the
    storyboard as `input_params.storyboard`. The chat slots provide the rest
    (duration, language, etc.).
    """
    session = _chat_agent.get_session(req.session_id, tenant.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found or expired")
    if session.job_id:
        raise HTTPException(status_code=409, detail=f"Session already has job {session.job_id}")
    if not session.storyboard:
        raise HTTPException(
            status_code=422,
            detail="No storyboard on session — call POST /api/studio-chat/build-storyboard first",
        )

    from tvd_pipeline.pipelines._storyboard import validate_storyboard
    errors = validate_storyboard(session.storyboard)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_storyboard", "validation_errors": errors},
        )

    meta = session.storyboard.get("meta") or {}
    slots = session.slots or {}

    payload: Dict[str, Any] = {
        "video_type": "custom",
        "prompt": meta.get("title") or slots.get("prompt") or "Custom video",
        "duration": int(meta.get("target_duration_seconds") or slots.get("duration") or 20),
        "language": meta.get("language") or slots.get("language") or "en",
        "country": meta.get("country") or slots.get("country") or "",
        "style": meta.get("style") or slots.get("style") or "Auto",
        "simulation": bool(req.simulation),
        "storyboard": session.storyboard,
    }
    if req.simulation_duration:
        payload["simulation_duration"] = req.simulation_duration
    if req.output_resolution:
        payload["output_resolution"] = req.output_resolution
    if slots.get("voice_id"):
        payload["voice_id"] = slots["voice_id"]

    try:
        gen_req = GenerateVideoRequest(**payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Storyboard payload failed validation: {e}")

    response = await generate_video(req=gen_req, request=request, tenant=tenant)

    job_id = getattr(response, "job_id", None) or (response.get("job_id") if isinstance(response, dict) else None)
    if job_id:
        _chat_agent.attach_job(req.session_id, tenant.id, job_id)

    return response


# ---------------------------------------------------------------------------
# Root → VidBuddy landing page
# ---------------------------------------------------------------------------
from fastapi.responses import RedirectResponse as _RedirectResponse


@app.get("/", include_in_schema=False)
async def root_redirect():
    return _RedirectResponse(url="/studio/home.html", status_code=302)


# ---------------------------------------------------------------------------
# Dashboard route
# ---------------------------------------------------------------------------
app.mount(
    "/dashboard",
    StaticFiles(
        directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "playgrounds", "dashboard"),
        html=True,
    ),
    name="dashboard",
)
app.mount(
    "/studio",
    StaticFiles(
        directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "playgrounds", "studio"),
        html=True,
    ),
    name="studio",
)


# ---------------------------------------------------------------------------
# Playground static files (ES modules need proper MIME types via HTTP)
# ---------------------------------------------------------------------------
app.mount(
    "/playground",
    StaticFiles(
        directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "playgrounds", "playground"),
        html=True,
    ),
    name="playground",
)

# System overview docs — single-file HTML at /system-docs/
# (Not at /docs because FastAPI reserves that for Swagger UI.)
app.mount(
    "/system-docs",
    StaticFiles(
        directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "documents", "system_overview"),
        html=True,
    ),
    name="system-docs",
)
