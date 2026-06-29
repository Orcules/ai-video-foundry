"""Base pipeline utilities — steps, abort checks, logging, timers."""

import json
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

from api_pipeline.supabase_client import SupabaseJobClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-job accumulated cost tracker for auto-computing step_cost_usd deltas
# ---------------------------------------------------------------------------
_last_cost: Dict[str, float] = {}  # job_id -> last logged accumulated cost


def _cleanup_cost_tracking(job_id: str) -> None:
    """Remove the cost tracking state for a finished job."""
    _last_cost.pop(job_id, None)


def _seed_cost_tracking(job_id: str, cost_usd: float) -> None:
    """Seed the cost tracker on resume so the first step doesn't show the full restored total."""
    _last_cost[job_id] = cost_usd


# ---------------------------------------------------------------------------
# Step-to-intermediates mapping (ordered — clearing step X clears X + after)
# ---------------------------------------------------------------------------
PRODUCT_STEPS = [
    ("step_0",    "Describe Character",     ["character_description"]),
    ("step_1",    "Parse Prompt",           ["parsed_texts"]),
    ("step_2",    "Clean Product Image",    ["clean_product_image"]),
    ("step_2.5",  "Reference Video",        ["reference_video_structure"]),
    ("step_2.7",  "Voiceover (VO)",         ["vo_audio_url", "vo_word_segments", "vo_script", "vo_duration"]),
    ("step_3",    "Scene Prompts",          ["scene_prompts", "music_style"]),
    ("steps_4_7", "Generate Assets",        ["scene_images", "scene_images_all", "scene_videos", "music_url"]),
    ("step_8",    "Concat + Audio",         ["concat_url", "audio_mix_url", "rendi_scene_voice_url"]),
    ("step_9",    "Subtitles",              ["subtitled_url"]),
]

INFLUENCER_STEPS = [
    ("step_0",    "Generate/Describe Influencer", ["influencer_image", "character_description"]),
    ("step_0.5",  "Analyze Media",                ["ref_image_analyses", "asset_analyses"]),
    ("step_1",    "Parse Prompt",                 ["parsed_texts"]),
    ("step_2.7",  "Generate VO Script + TTS",     ["vo_script", "vo_audio_url", "vo_word_segments", "vo_duration"]),
    ("step_3",    "Generate Scene Prompts",        ["scene_prompts", "music_style"]),
    ("steps_4_7", "Generate Scene Assets",         ["scene_images", "scene_images_all", "scene_videos", "asset_videos", "music_url"]),
    ("step_7.5",  "Beat-Sync Trim",               ["trimmed_scene_videos"]),
    ("step_8",    "Concatenate + Audio Mix",       ["concat_url", "audio_mix_url", "rendi_scene_voice_url"]),
    ("step_9",    "Add Subtitles",                 ["subtitled_url"]),
]

PERSONAL_BRAND_STEPS = [
    ("step_0",    "Describe Character",        ["character_descriptions"]),
    ("step_0.5",  "Analyze Reference Images",  ["ref_image_analyses"]),
    ("step_1",    "Parse Prompt",              ["parsed_texts"]),
    ("step_2.7",  "Generate VO Script + TTS",  ["vo_script", "vo_audio_url", "vo_word_segments", "vo_duration"]),
    ("step_3",    "Generate Scene Prompts",     ["scene_prompts", "music_style"]),
    ("steps_4_7", "Generate Scene Assets",      ["scene_images", "scene_images_all", "scene_videos", "music_url"]),
    ("step_7.5",  "Beat-Sync Trim",            ["trimmed_scene_videos"]),
    ("step_8",    "Concatenate + Audio Mix",    ["concat_url", "audio_mix_url", "rendi_scene_voice_url"]),
    ("step_9",    "Add Subtitles",              ["subtitled_url"]),
]

UGC_REAL_STEPS = [
    ("step_parse", "Parse UGC Real Brief",       ["parsed_texts", "ugc_real_intake"]),
    ("step_0",    "Offer Analysis",             ["offer_profile"]),
    ("step_0.5",  "Creative Strategy",          ["creative_strategy"]),
    ("step_1",    "Nine-Cell Plan",             ["nine_cell_plan"]),
    ("step_2",    "Style DNA",                  ["style_dna"]),
    ("step_3",    "Grid Generation",            ["master_grid_prompt", "grid_image_url"]),
    ("step_4",    "Grid Cutting",               ["grid_cells"]),
    ("step_5",    "Frame Routing",              ["frame_routing"]),
    ("step_6",    "Voiceover",                  ["vo_script", "vo_audio_url", "cell_vo_audio", "vo_word_segments", "vo_duration"]),
    ("step_7",    "Lip Sync Generation",        ["lip_sync_videos"]),
    ("step_8",    "Animation Generation",       ["scene_videos", "scene_video_plan"]),
    ("step_9",    "Concatenate + Audio Mix",    ["music_url", "concat_url", "audio_mix_url", "rendi_scene_voice_url"]),
    ("step_10",   "Add Subtitles",              ["subtitled_url", "final_video_url"]),
]


def get_steps_for_type(video_type: str) -> list:
    """Return the step list for a given video type."""
    vt = video_type.lower()
    if vt == "influencer":
        return INFLUENCER_STEPS
    if vt == "personal-brand":
        return PERSONAL_BRAND_STEPS
    if vt == "ugc-real":
        return UGC_REAL_STEPS
    return PRODUCT_STEPS


def clear_intermediates_from_step(intermediates: dict, video_type: str, from_step: str) -> dict:
    """Remove intermediate keys from `from_step` onward. Returns the cleaned dict."""
    steps = get_steps_for_type(video_type)
    found = False
    for step_id, _label, keys in steps:
        if step_id == from_step:
            found = True
        if found:
            for k in keys:
                intermediates.pop(k, None)
    return intermediates


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class JobAbortedError(BaseException):
    """Raised when a job has been aborted by the user."""
    pass


class JobPausedError(BaseException):
    """Raised when the pipeline hit a user/Studio pause gate (or user clicked Pause)."""

    def __init__(self, message: str, *, pause_monolith_step: Optional[str] = None):
        super().__init__(message)
        self.pause_monolith_step = pause_monolith_step


def _check_abort(supabase: SupabaseJobClient, job_id: str) -> None:
    """Check if the job has been aborted or paused. Raises accordingly."""
    job = supabase.get_job(job_id)
    if job:
        status = job.get("status")
        if status == "aborted":
            raise JobAbortedError(f"Job {job_id} was aborted by user")
        if status == "paused":
            raise JobPausedError(f"Job {job_id} was paused by user")


# ---------------------------------------------------------------------------
# Logging + SSE push
# ---------------------------------------------------------------------------
def _step_log(job_id: str, step: str, msg: str, t0: float = 0, progress: int = -1, event_type: str = "info", cost_usd: float = None, step_cost_usd: float = None, asset_url: str = None, asset_type: str = None) -> None:
    """Log a pipeline step with optional elapsed time and push to SSE event store."""
    from api_pipeline.event_store import event_store
    elapsed_sec = (time.time() - t0) if t0 else None
    elapsed_str = f" ({elapsed_sec:.1f}s)" if elapsed_sec else ""
    logger.info(f"[{job_id}] [{step}] {msg}{elapsed_str}")

    # Auto-compute step_cost_usd for sequential steps (parallel steps set it explicitly)
    if cost_usd is not None and step_cost_usd is None:
        prev = _last_cost.get(job_id, 0.0)
        step_cost_usd = max(0.0, cost_usd - prev)
    if cost_usd is not None:
        _last_cost[job_id] = cost_usd

    event_store.push(
        job_id, step, msg, progress, event_type,
        round(elapsed_sec, 2) if elapsed_sec else None,
        cost_usd=round(cost_usd, 4) if cost_usd is not None else None,
        step_cost_usd=round(step_cost_usd, 4) if step_cost_usd is not None else None,
        asset_url=asset_url,
        asset_type=asset_type,
    )


# ---------------------------------------------------------------------------
# Step timer
# ---------------------------------------------------------------------------
class StepTimer:
    """Accumulates per-step timing entries for a pipeline run."""

    def __init__(self, job_id: str, supabase):
        self.job_id = job_id
        self.supabase = supabase
        self.entries: List[Dict[str, Any]] = []
        self._active: Optional[Dict[str, Any]] = None

    def start(self, step: str, label: str) -> None:
        self._flush()
        self._active = {
            "step": step, "label": label,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": None, "duration_sec": None,
            "status": "in_progress", "detail": None,
        }

    def end(self, detail: str = None) -> None:
        if not self._active:
            return
        now = datetime.now(timezone.utc)
        self._active["ended_at"] = now.isoformat()
        start = datetime.fromisoformat(self._active["started_at"])
        self._active["duration_sec"] = round((now - start).total_seconds(), 2)
        self._active["status"] = "completed"
        self._active["detail"] = detail
        self.entries.append(self._active)
        self._active = None

    def skip(self, step: str, label: str, detail: str = None) -> None:
        self._flush()
        status = "skipped" if not detail or "checkpoint" not in detail.lower() else "restored"
        self.entries.append({
            "step": step, "label": label,
            "started_at": None, "ended_at": None,
            "duration_sec": None, "status": status, "detail": detail,
        })

    def _flush(self) -> None:
        if self._active:
            self._active["status"] = "incomplete"
            self.entries.append(self._active)
            self._active = None

    def persist(self) -> None:
        self._flush()
        try:
            self.supabase.update_step_timings(self.job_id, self.entries)
        except Exception as e:
            logger.warning(f"[{self.job_id}] Failed to persist step timings: {e}")


# ---------------------------------------------------------------------------
# Transient error detection
# ---------------------------------------------------------------------------
def _is_transient_error(error: Exception) -> bool:
    """Return True if the error looks transient (worth retrying)."""
    msg = str(error).lower()
    transient_patterns = [
        "timeout", "timed out", "429", "503", "502", "504",
        "connection reset", "connection refused", "connection aborted",
        "rate limit", "ratelimit", "too many requests",
        "temporarily unavailable", "service unavailable",
        "server error", "internal server error",
        "broken pipe", "eof occurred", "read timed out",
        "remotedisconnected", "connectionerror",
        "server disconnected", "remoteprotocolerror", "protocol error",
    ]
    return any(p in msg for p in transient_patterns)


# ---------------------------------------------------------------------------
# Per-job artifact persistence
# ---------------------------------------------------------------------------
JOB_ARTIFACTS_DIR = Path(__file__).parent.parent / "job_artifacts"


def _save_artifact(job_id: str, url: str, filename: str) -> Optional[str]:
    """Download an asset URL to the job's artifact folder. Returns local path or None."""
    try:
        import requests as _req
        artifacts_dir = JOB_ARTIFACTS_DIR / job_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        resp = _req.get(url, timeout=30)
        if resp.ok:
            local_path = artifacts_dir / filename
            local_path.write_bytes(resp.content)
            return str(local_path)
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to save artifact {filename}: {e}")
    return None


def _save_artifact_bg(job_id: str, url: str, filename: str) -> None:
    """Save artifact in a background thread (non-blocking)."""
    threading.Thread(target=_save_artifact, args=(job_id, url, filename), daemon=True).start()


def save_pipeline_log(job_id: str, video_type: str, params: dict, intermediates: dict, output: dict, timer) -> None:
    """Write a structured pipeline_log.json to the job's artifact folder."""
    try:
        artifacts_dir = JOB_ARTIFACTS_DIR / job_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "pipeline_log.json").write_text(json.dumps({
            "job_id": job_id,
            "video_type": video_type,
            "params": params,
            "intermediates": intermediates,
            "output": output,
            "step_timings": timer.entries if hasattr(timer, 'entries') else [],
        }, indent=2, default=str))
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to save pipeline log: {e}")


def validate_pipeline_output(
    job_id: str,
    pipeline_type: str,
    output: Dict[str, Any],
    params: Dict[str, Any],
    intermediates: Dict[str, Any],
) -> list:
    """Validate pipeline output — flag missing/failed outputs.
    Returns list of issue dicts stored as output["issues"].
    """
    issues = []

    def _flag(field, severity, message):
        issues.append({"field": field, "severity": severity, "message": message})
        evt = "error" if severity == "critical" else "warn"
        _step_log(job_id, "VALIDATION", f"[{severity.upper()}] {message}", event_type=evt)
        logger.warning(f"[{job_id}] FALLBACK: output validation — {message}")

    if not output.get("final_mp4_url"):
        _flag("final_mp4_url", "critical", "Final video URL is missing")

    vo_attempted = intermediates.get("vo_script") is not None
    if vo_attempted and not output.get("vo_audio_url"):
        _flag("vo_audio_url", "warning", "Voiceover audio missing — TTS or upload failed")

    has_vo_url = bool(output.get("vo_audio_url"))
    concat_url = intermediates.get("concat_url")
    # Monolith emits "audio_mix_url"; check both keys for compatibility
    final_pre_sub = intermediates.get("audio_mix_url") or intermediates.get("rendi_scene_voice_url")
    if has_vo_url and final_pre_sub and concat_url and final_pre_sub == concat_url:
        _flag("audio_mix", "critical", "VO+music mixing BOTH failed (Rendi + FFmpeg) — video has NO audio")

    if not output.get("music_url"):
        _flag("music_url", "warning", "Background music missing — Suno failed or returned None")

    audio_mix_key = intermediates.get("audio_mix_url") or intermediates.get("rendi_scene_voice_url")
    if params.get("add_subtitles", True) and audio_mix_key and not intermediates.get("subtitled_url"):
        _flag("subtitled_url", "warning", "Subtitles requested but not applied — ZapCap may have failed")

    fallback_scenes = intermediates.get("fallback_scenes")
    if fallback_scenes:
        _flag("scene_videos", "warning",
              f"Video generation failed for {len(fallback_scenes)} scene(s) — Ken Burns zoom fallback used")

    if pipeline_type in ("influencer", "personal-brand", "ugc-real"):
        if pipeline_type == "ugc-real":
            char_list = params.get("character_urls") or output.get("character_urls") or intermediates.get("character_urls") or []
            if not params.get("character_url") and not char_list and not params.get("character_description"):
                if not intermediates.get("influencer_image") and not intermediates.get("character_description"):
                    _flag("character", "warning", "Character references missing — continuity may be weak")
            scenes = ((output.get("nine_cell_plan") or intermediates.get("nine_cell_plan") or {}).get("cells") or [])
            scene_videos = output.get("scene_videos") or intermediates.get("scene_videos") or []
            if scenes:
                missing = []
                for idx, scene in enumerate(scenes):
                    clip = scene_videos[idx] if idx < len(scene_videos) else None
                    if not clip:
                        missing.append(scene.get("scene_id") or scene.get("cell_index") or f"scene_{idx + 1}")
                if missing:
                    _flag("scene_videos", "critical", f"UGC Real missing rendered clip(s) for scenes: {missing}")
            if intermediates.get("grid_cells") and not intermediates.get("frame_classifications"):
                _flag("frame_classifications", "warning", "Grid cells exist but frame routing metadata is missing")
            if intermediates.get("vo_script") and not (output.get("vo_audio_url") or intermediates.get("vo_audio_url") or intermediates.get("scene_vo_audio")):
                _flag("scene_vo_audio", "warning", "Voice script exists but no lip-sync or full VO audio is available")
        elif not params.get("character_url") and not params.get("character_description"):
            if not intermediates.get("influencer_image") and not intermediates.get("character_description"):
                _flag("influencer", "warning", "Influencer image/description missing")

    return issues
