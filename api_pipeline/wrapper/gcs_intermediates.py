"""Parallel upload of intermediate pipeline assets to GCS for permanent storage.

External API services (Kie, Rendi, ElevenLabs, Suno, ZapCap) return temporary
URLs that expire in 24h-7 days. This module uploads those assets to GCS so they
can be reviewed, re-used, or evaluated after expiry.

Controlled by the `gcs_asset_persistence` setting in server.json:
  "all"              — upload all intermediates + final video
  "final_video_only" — upload only the final video
  "none"             — no GCS uploads (today's behavior)
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

from api_pipeline.services.registry import _upload_to_gcs_permanent

logger = logging.getLogger(__name__)

# Single-value asset keys → (GCS key template, extension)
_SINGLE_ASSETS = {
    "clean_product_image": ("clean_product_image", ".jpg"),
    "vo_audio_url":        ("vo_audio", ".mp3"),
    "music_url":           ("music", ".mp3"),
    "concat_url":          ("concat", ".mp4"),
    "audio_mix_url":       ("audio_mix", ".mp4"),
    "rendi_scene_voice_url": ("rendi_scene_voice", ".mp4"),
    "subtitled_url":       ("subtitled_video", ".mp4"),
    "final_video_url":     ("final_video", ".mp4"),
    "extended_video_url":  ("extended_video", ".mp4"),
    "extended_vo_audio_url": ("extended_vo_audio", ".mp3"),
}

# List-value asset keys → (GCS key template with {i} placeholder, extension)
_LIST_ASSETS = {
    "scene_images": ("scene_images/scene_{i:02d}", ".jpg"),
    "scene_videos": ("scene_videos/scene_{i:02d}", ".mp4"),
    "scene_grids": ("scene_grids/scene_{i:02d}", ".jpg"),
    "lip_sync_videos": ("lip_sync_videos/scene_{i:02d}", ".mp4"),
}

_FINAL_ONLY = {"final_video_url"}

_SERVER_JSON = os.path.join(os.path.dirname(__file__), "..", "config", "server.json")


def _load_persistence_mode() -> str:
    """Read gcs_asset_persistence from server.json. Defaults to 'all'."""
    try:
        with open(_SERVER_JSON, "r") as f:
            cfg = json.load(f)
        return cfg.get("gcs_asset_persistence", "all")
    except Exception:
        return "all"


def upload_intermediates_to_gcs(
    job_id: str,
    result: Dict[str, Any],
    gcs,
) -> Dict[str, Any]:
    """Upload intermediate assets from result dict to GCS.

    Args:
        job_id: Pipeline job ID (used for GCS key prefix).
        result: Monolith result dict containing asset URLs.
        gcs: GCSStorageService instance.

    Returns:
        Updated copy of result with GCS URLs replacing temporary URLs where
        upload succeeded. Original URLs are preserved on failure.
    """
    mode = _load_persistence_mode()
    if mode == "none":
        logger.info(f"[{job_id}] GCS asset persistence disabled (mode=none)")
        return result

    if not gcs or not gcs._initialized:
        logger.warning(f"[{job_id}] GCS not initialized, skipping asset persistence")
        return result

    # Determine which asset keys to upload
    if mode == "final_video_only":
        allowed_single = _FINAL_ONLY
        allowed_list = set()
    else:
        allowed_single = set(_SINGLE_ASSETS.keys())
        allowed_list = set(_LIST_ASSETS.keys())

    # Build list of (result_key, list_index_or_None, gcs_key, source_url)
    tasks = []
    for key, (template, ext) in _SINGLE_ASSETS.items():
        if key not in allowed_single:
            continue
        url = result.get(key)
        if not url or not isinstance(url, str):
            continue
        gcs_key = f"jobs/{job_id}/{template}{ext}"
        tasks.append((key, None, gcs_key, url))

    for key, (template, ext) in _LIST_ASSETS.items():
        if key not in allowed_list:
            continue
        urls = result.get(key)
        if not urls or not isinstance(urls, list):
            continue
        for i, url in enumerate(urls, start=1):
            if not url or not isinstance(url, str):
                continue
            gcs_key = f"jobs/{job_id}/{template.format(i=i)}{ext}"
            tasks.append((key, i - 1, gcs_key, url))

    # Handle nested scene_beat_clips (dict of scene_key -> list of clip dicts with urls)
    beat_clips = result.get("scene_beat_clips")
    if beat_clips and isinstance(beat_clips, dict) and mode == "all":
        for scene_key, clips in beat_clips.items():
            if not isinstance(clips, list):
                continue
            for ci, clip in enumerate(clips):
                if not isinstance(clip, dict):
                    continue
                for url_key in ("url", "raw_url"):
                    url = clip.get(url_key)
                    if url and isinstance(url, str):
                        suffix = "trimmed" if url_key == "url" else "raw"
                        gcs_key = f"jobs/{job_id}/beat_clips/{scene_key}_c{ci:02d}_{suffix}.mp4"
                        tasks.append((f"scene_beat_clips.{scene_key}[{ci}].{url_key}", None, gcs_key, url))

    # Handle dict value assets such as per-scene VO audio for ugc-real.
    for dict_key, ext in (("scene_vo_audio", ".mp3"), ("cell_vo_audio", ".mp3")):
        dict_value = result.get(dict_key)
        if not isinstance(dict_value, dict) or mode != "all":
            continue
        for child_key, url in dict_value.items():
            if url and isinstance(url, str):
                gcs_key = f"jobs/{job_id}/{dict_key}/{child_key}{ext}"
                tasks.append((f"{dict_key}.{child_key}", None, gcs_key, url))

    if not tasks:
        logger.info(f"[{job_id}] No assets to upload to GCS (mode={mode})")
        return result

    logger.info(f"[{job_id}] Uploading {len(tasks)} assets to GCS (mode={mode})")

    # Run uploads in parallel
    updated = dict(result)
    max_workers = min(len(tasks), 8)

    def _do_upload(task_info):
        result_key, list_idx, gcs_key, source_url = task_info
        gcs_url = _upload_to_gcs_permanent(gcs, source_url, gcs_key)
        return result_key, list_idx, gcs_url

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_do_upload, t): t for t in tasks}
        for future in as_completed(futures):
            task_info = futures[future]
            try:
                result_key, list_idx, gcs_url = future.result()
                if gcs_url and gcs_url != task_info[3]:
                    if list_idx is not None:
                        if result_key not in updated or not isinstance(updated[result_key], list):
                            continue
                        updated_list = list(updated[result_key])
                        updated_list[list_idx] = gcs_url
                        updated[result_key] = updated_list
                    else:
                        if "." in result_key and "[" not in result_key:
                            root, child = result_key.split(".", 1)
                            if isinstance(updated.get(root), dict):
                                updated_dict = dict(updated[root])
                                updated_dict[child] = gcs_url
                                updated[root] = updated_dict
                            else:
                                updated[result_key] = gcs_url
                        else:
                            updated[result_key] = gcs_url
            except Exception as e:
                logger.warning(
                    f"[{task_info[0]}] GCS upload failed for {task_info[2]}: {e}"
                )

    uploaded_count = sum(
        1
        for key, idx, gcs_key, orig_url in tasks
        if (
            (
                updated.get(key)
                if idx is None and "." not in key
                else (
                    (updated.get(key.split(".", 1)[0], {}) or {}).get(key.split(".", 1)[1])
                    if idx is None and "." in key and "[" not in key
                    else (updated.get(key, [None])[idx] if isinstance(updated.get(key), list) else None)
                )
            )
            != orig_url
        )
    )
    logger.info(f"[{job_id}] GCS upload complete: {uploaded_count}/{len(tasks)} assets persisted")

    return updated
