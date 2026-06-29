"""Storyboard preview rendering — image-first storyboard step.

After the Director builds a storyboard, this module renders ONE preview image
per scene (the first frame) via the scene's chosen `preview_image_model`. The
user reviews the resulting image storyboard before committing to video gen.

Each preview later becomes the I2V start frame for that scene's video
generation. No throwaway work.

Public entry: :func:`render_storyboard_previews(processor, storyboard, ...)`.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default model when scene.preview_image_model is missing.
_DEFAULT_PREVIEW_MODEL = "nano-banana-pro"

# Image models routed via the Kie service. The Kie service holds the
# nano-banana model id in kie.json — we just pick the right pricing key here.
_KIE_IMAGE_MODELS = {"nano-banana-2", "nano-banana-pro"}

# Image models routed via Vertex (GeminiImageService).
_GEMINI_IMAGE_MODELS = {
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
}


def _build_scene_prompt(scene: Dict[str, Any], storyboard: Dict[str, Any]) -> str:
    """Compose a single image prompt from the scene's first generate-like clip
    plus any locked ingredient descriptions from the sheets."""
    clips = scene.get("clips") or []
    base = ""
    for c in clips:
        fp = (c.get("first_prompt") or "").strip()
        if fp:
            base = fp
            break
    if not base:
        # Fallback to vo_text if no visual prompt was authored
        base = (scene.get("vo_text") or "").strip()

    # Camera intent prefix (mirrors Composer's logic at low cost)
    cam = scene.get("camera") or {}
    cam_parts: List[str] = []
    if cam.get("shot_type"):
        cam_parts.append(f"{cam['shot_type'].replace('_', ' ')} framing")
    if cam.get("lens_feel"):
        cam_parts.append(f"{cam['lens_feel'].replace('_', ' ')} lens")
    cam_prefix = ", ".join(cam_parts)

    # Ingredient descriptions (text only — image refs are passed separately as URLs)
    parts: List[str] = []
    if cam_prefix:
        parts.append(cam_prefix.capitalize() + ".")
    if base:
        parts.append(base)

    # If any clip wants the character/venue/style sheet, fold the sheet's
    # description into the prompt so the model understands intent even when
    # we can't pass all refs (some image models cap at 1 ref).
    used_sheets: set = set()
    for c in clips:
        ing = c.get("ingredients") or {}
        if ing.get("use_character_sheet"):
            used_sheets.add("character")
        if ing.get("use_venue_sheet"):
            used_sheets.add("venue")
        if ing.get("use_style_sheet"):
            used_sheets.add("style")

    if "character" in used_sheets:
        cd = ((storyboard.get("character_sheet") or {}).get("subject_description") or "").strip()
        if cd:
            parts.append(f"Subject: {cd}.")
    if "venue" in used_sheets:
        vd = ((storyboard.get("venue_sheet") or {}).get("description") or "").strip()
        if vd:
            parts.append(f"Environment: {vd}.")
    if "style" in used_sheets:
        sd = ((storyboard.get("style_sheet") or {}).get("description") or "").strip()
        if sd:
            parts.append(f"Visual treatment: {sd}.")

    style_global = ((storyboard.get("meta") or {}).get("style") or "").strip()
    if style_global and style_global.lower() != "auto":
        parts.append(f"Style: {style_global}.")

    return " ".join(parts).strip() or "Cinematic still."


def _pick_reference_image(scene: Dict[str, Any], storyboard: Dict[str, Any]) -> Optional[str]:
    """Pick the most relevant single reference image to ground the model.

    Kie's Nano Banana family accepts up to one `reference_image_url` per call
    (multi-ref needs a different code path). Priority: character > venue > style
    > assets.reference_image_urls.
    """
    clips = scene.get("clips") or []
    use_char = use_venue = use_style = False
    for c in clips:
        ing = c.get("ingredients") or {}
        use_char = use_char or bool(ing.get("use_character_sheet"))
        use_venue = use_venue or bool(ing.get("use_venue_sheet"))
        use_style = use_style or bool(ing.get("use_style_sheet"))

    if use_char:
        urls = ((storyboard.get("character_sheet") or {}).get("reference_image_urls") or [])
        if urls:
            return urls[0]
    if use_venue:
        urls = ((storyboard.get("venue_sheet") or {}).get("reference_image_urls") or [])
        if urls:
            return urls[0]
    if use_style:
        urls = ((storyboard.get("style_sheet") or {}).get("reference_image_urls") or [])
        if urls:
            return urls[0]

    # Fall back to first reference_image referenced by a clip
    clips_in = scene.get("clips") or []
    for c in clips_in:
        src = c.get("source") or {}
        ri = src.get("reference_image_index")
        if isinstance(ri, int):
            ref_urls = ((storyboard.get("assets") or {}).get("reference_image_urls") or [])
            if 0 <= ri < len(ref_urls):
                return ref_urls[ri]

    return None


def _render_one_scene(
    processor,
    scene: Dict[str, Any],
    storyboard: Dict[str, Any],
    *,
    aspect_ratio: str,
    on_progress: Optional[Callable] = None,
) -> Optional[str]:
    """Render exactly one preview image for one scene. Returns URL or None."""
    model = scene.get("preview_image_model") or _DEFAULT_PREVIEW_MODEL
    prompt = _build_scene_prompt(scene, storyboard)
    ref_url = _pick_reference_image(scene, storyboard)
    scene_num = scene.get("scene_number", "?")

    t0 = time.time()
    try:
        if model in _KIE_IMAGE_MODELS:
            kie = getattr(processor, "kie_service", None)
            if kie is None:
                logger.warning("[previews] scene %s: kie_service unavailable", scene_num)
                return None
            # The Kie config holds the actual NB model name; we override per scene
            # by temporarily setting it (the service reads kie.json each call).
            # Simpler: pass the model via the prompt-adjacent kwargs that the
            # service already accepts. For nano-banana-2 vs Pro we'll switch by
            # setting kie's config.nano_banana.model at the service level — but
            # since both are nano-banana family and the difference is the URL the
            # Kie API picks, we can also just call with the right model name in
            # the payload. The existing generate_image_nano_banana() reads
            # kie.json's `nano_banana.model`. For "nano-banana-2" vs "nano-banana-pro"
            # we monkey-set the config temporarily inside the lock.
            url = _render_kie_with_model_override(
                kie, model, prompt, ref_url, aspect_ratio,
            )
        elif model in _GEMINI_IMAGE_MODELS:
            gim = getattr(processor, "gemini_image_service", None)
            if gim is None:
                logger.warning("[previews] scene %s: gemini_image_service unavailable", scene_num)
                return None
            # Pass ref as a list (Gemini supports inline base64 refs)
            refs = [ref_url] if ref_url else None
            url = gim.generate_image(
                prompt=prompt,
                reference_image_urls=refs,
                aspect_ratio=aspect_ratio,
                use_flash=("flash" in model),
                model_override=model,
            )
        else:
            logger.warning("[previews] scene %s: unknown model %s — skipping", scene_num, model)
            return None

        elapsed = time.time() - t0
        if url:
            logger.info(
                "[previews] scene %s rendered (%s, %.1fs): %s",
                scene_num, model, elapsed, url[:80],
            )
            if on_progress:
                try:
                    on_progress("usage", {
                        "service": "kie" if model in _KIE_IMAGE_MODELS else "vertex",
                        "step": f"preview_scene_{scene_num}",
                        "model": model,
                        "provider": "kie" if model in _KIE_IMAGE_MODELS else "direct",
                        "count": 1,
                        "label": f"Preview render scene {scene_num}",
                        "category": "image", "success": True,
                    })
                except Exception:
                    pass
        else:
            logger.warning("[previews] scene %s: model returned no URL", scene_num)
        return url
    except Exception as e:
        logger.exception("[previews] scene %s render failed: %s", scene_num, e)
        return None


def _render_kie_with_model_override(
    kie, model: str, prompt: str, ref_url: Optional[str], aspect_ratio: str,
) -> Optional[str]:
    """Call the Kie Nano Banana service with the right model name.

    The service reads `kie.json:nano_banana.model` at call time. To get
    nano-banana-2 vs nano-banana-pro, we temporarily swap the loaded config's
    model field, call, and restore. This is safe because each preview request
    is single-threaded per worker and the config is process-wide.
    """
    try:
        from tvd_pipeline.data_loader import get_kie_config
    except Exception:
        get_kie_config = None

    original_model = None
    if get_kie_config is not None:
        try:
            cfg = get_kie_config()
            nb = cfg.get("nano_banana") or {}
            original_model = nb.get("model")
            nb["model"] = model
            cfg["nano_banana"] = nb
        except Exception:
            pass

    try:
        return kie.generate_image_nano_banana(
            prompt=prompt,
            reference_image_url=ref_url,
            aspect_ratio=aspect_ratio,
        )
    finally:
        if original_model is not None and get_kie_config is not None:
            try:
                cfg = get_kie_config()
                nb = cfg.get("nano_banana") or {}
                nb["model"] = original_model
                cfg["nano_banana"] = nb
            except Exception:
                pass


def render_storyboard_previews(
    processor,
    storyboard: Dict[str, Any],
    *,
    only_scenes: Optional[List[int]] = None,
    aspect_ratio: Optional[str] = None,
    max_workers: int = 4,
    on_progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Render preview images for every scene (or only_scenes) in parallel.

    Mutates the storyboard in place, setting ``scene.preview_image_url`` on
    each scene that rendered successfully. Returns a summary dict.

    Args:
        processor: A VideoSceneProcessor instance (provides kie_service +
            gemini_image_service).
        storyboard: The full storyboard JSON. Must have ``scenes`` list.
        only_scenes: Optional list of 0-indexed scene indices to render. When
            None, renders every scene.
        aspect_ratio: Default aspect ratio for previews. Defaults to the
            storyboard's meta.aspect_ratio or "9:16".
        max_workers: Parallel worker count (default 4 — Kie/Vertex tolerate
            this well; raise carefully).
        on_progress: Optional callback for usage events.

    Returns:
        ``{
            "previews": {scene_idx: image_url_or_None},
            "rendered": int,
            "failed":   int,
            "elapsed_seconds": float,
            "model_used": {scene_idx: model_name},
        }``
    """
    scenes = storyboard.get("scenes") or []
    ar = aspect_ratio or ((storyboard.get("meta") or {}).get("aspect_ratio") or "9:16")
    indices = list(only_scenes) if only_scenes is not None else list(range(len(scenes)))

    previews: Dict[int, Optional[str]] = {}
    models_used: Dict[int, str] = {}
    t0 = time.time()

    def _job(i: int) -> tuple:
        if i < 0 or i >= len(scenes):
            return (i, None, None)
        scene = scenes[i]
        url = _render_one_scene(
            processor, scene, storyboard,
            aspect_ratio=ar,
            on_progress=on_progress,
        )
        if url:
            scene["preview_image_url"] = url
        return (i, url, scene.get("preview_image_model") or _DEFAULT_PREVIEW_MODEL)

    if not indices:
        return {"previews": {}, "rendered": 0, "failed": 0, "elapsed_seconds": 0.0, "model_used": {}}

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(indices)))) as pool:
        futures = [pool.submit(_job, i) for i in indices]
        for fut in as_completed(futures):
            try:
                i, url, model = fut.result()
                previews[i] = url
                if model:
                    models_used[i] = model
            except Exception as e:
                logger.exception("[previews] worker failed: %s", e)

    elapsed = time.time() - t0
    rendered = sum(1 for u in previews.values() if u)
    failed = sum(1 for u in previews.values() if not u)
    logger.info(
        "[previews] %d/%d rendered in %.1fs (%d failed)",
        rendered, len(indices), elapsed, failed,
    )

    return {
        "previews": previews,
        "rendered": rendered,
        "failed": failed,
        "elapsed_seconds": round(elapsed, 1),
        "model_used": models_used,
    }


# --------------------------------------------------------------------------- #
# Per-scene video reroll
# --------------------------------------------------------------------------- #

# Default video model + provider when the storyboard/clip doesn't override.
_DEFAULT_VIDEO_MODEL = "veo-3.1-fast"
_DEFAULT_VIDEO_PROVIDER = "direct"
# Map _resolved_tool (from animation_router / Composer) back to a sensible
# concrete model:provider pair. Used when the storyboard only carries a tool
# name and not an explicit model.
_TOOL_TO_MODEL_PROVIDER = {
    "veo":      ("veo-3.1-fast", "direct"),
    "seedance": ("seedance-1.0-pro", "kie"),
    "kling":    ("kling-2.5", "kie"),
    "runway":   ("runway-gen4-turbo", "kie"),
    "kenburns": ("none", "ffmpeg"),
}


def render_scene_video(
    processor,
    scene: Dict[str, Any],
    *,
    storyboard: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    gcs_bucket: Optional[str] = None,
    on_progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Re-render a SINGLE scene's video (I2V from its preview image).

    This is the per-scene video equivalent of ``_render_one_scene`` for image
    previews. It does NOT touch the monolith's pipeline orchestration — it
    just calls ``processor._generate_video`` directly with parameters pulled
    from the scene (and any caller overrides), then optionally re-uploads the
    resulting URL to GCS so it lives in our bucket rather than the provider's
    short-lived storage.

    Args:
        processor: A VideoSceneProcessor instance (provides _generate_video and
            gcs_storage_service).
        scene: The scene dict from ``session.storyboard.scenes[scene_idx]``.
            Must have a ``preview_image_url`` (the I2V start frame) and at
            least one entry in ``clips``.
        storyboard: The parent storyboard (for meta lookups like aspect ratio).
            Optional; used only for fallbacks.
        overrides: Optional caller overrides, applied in-memory to clip[0]
            before generation. Recognized keys:
              - ``first_prompt``      — visual / first-frame prompt (informational)
              - ``motion_prompt``     — motion description (the I2V prompt)
              - ``video_model_override`` — concrete video model id
              - ``video_provider_override`` — provider for that model
        gcs_bucket: Optional bucket name override for re-upload. Defaults to
            ``processor.gcs_storage_service.bucket_name`` when present.
        on_progress: Optional usage callback (not currently used by callers,
            kept for parity with ``render_storyboard_previews``).

    Returns:
        ``{
            "video_url":         str | None,      # final URL (GCS-rehosted when possible)
            "raw_url":           str | None,      # original URL from the provider
            "model_used":        str,             # resolved video model id
            "provider_used":     str,             # resolved provider
            "duration":          float,           # clip duration sent to the API
            "elapsed_seconds":   float,
            "error":             str | None,      # failure reason if no URL
        }``
    """
    t0 = time.time()
    clips = scene.get("clips") or []
    if not clips:
        return {
            "video_url": None, "raw_url": None,
            "model_used": _DEFAULT_VIDEO_MODEL,
            "provider_used": _DEFAULT_VIDEO_PROVIDER,
            "duration": 0.0,
            "elapsed_seconds": 0.0,
            "error": "scene has no clips",
        }

    # We assume single-clip-per-scene for reroll (documented as a known gap).
    clip = clips[0]
    overrides = overrides or {}

    # Apply caller overrides in-memory (caller is expected to also persist them
    # on the session storyboard before/after calling us — same pattern as the
    # /reroll-scene-preview endpoint).
    if overrides.get("first_prompt"):
        clip["first_prompt"] = overrides["first_prompt"]
    if overrides.get("motion_prompt"):
        clip["motion_prompt"] = overrides["motion_prompt"]
    if overrides.get("video_model_override"):
        clip["video_model_override"] = overrides["video_model_override"]
    if overrides.get("video_provider_override"):
        clip["video_provider_override"] = overrides["video_provider_override"]

    # Pull the I2V start frame. /reroll-scene-preview is expected to have been
    # called first so this is populated.
    start_image_url = scene.get("preview_image_url")
    if not start_image_url:
        return {
            "video_url": None, "raw_url": None,
            "model_used": _DEFAULT_VIDEO_MODEL,
            "provider_used": _DEFAULT_VIDEO_PROVIDER,
            "duration": float(clip.get("duration") or 0.0),
            "elapsed_seconds": 0.0,
            "error": "scene has no preview_image_url — call /reroll-scene-preview first",
        }

    # Resolve motion prompt (compose-stamped > explicit > first_prompt fallback).
    motion_prompt = (
        clip.get("_motion_with_camera")
        or clip.get("motion_prompt")
        or clip.get("second_prompt")
        or clip.get("first_prompt")
        or ""
    )

    duration = float(clip.get("duration") or 5.0)

    # Resolve video model + provider. Priority:
    #   1. explicit clip override (video_model_override / video_provider_override)
    #   2. Composer-stamped _resolved_video_model / _resolved_video_provider
    #   3. _resolved_tool → known (model, provider) pair
    #   4. defaults (Veo 3.1 fast / direct)
    video_model = (
        clip.get("video_model_override")
        or clip.get("_resolved_video_model")
    )
    video_provider = (
        clip.get("video_provider_override")
        or clip.get("_resolved_video_provider")
    )
    if not video_model:
        tool = clip.get("_resolved_tool")
        if tool in _TOOL_TO_MODEL_PROVIDER:
            video_model, fallback_provider = _TOOL_TO_MODEL_PROVIDER[tool]
            video_provider = video_provider or fallback_provider
    video_model = video_model or _DEFAULT_VIDEO_MODEL
    video_provider = video_provider or _DEFAULT_VIDEO_PROVIDER

    reference_urls = clip.get("_resolved_refs") or None
    scene_num = scene.get("scene_number", "?")

    logger.info(
        "[reroll-video] scene %s — model=%s provider=%s duration=%.1fs",
        scene_num, video_model, video_provider, duration,
    )

    raw_url: Optional[str] = None
    error: Optional[str] = None
    try:
        gen = getattr(processor, "_generate_video", None)
        if gen is None:
            raise RuntimeError("processor has no _generate_video method")
        raw_url = gen(
            video_model=video_model,
            video_provider=video_provider,
            image_url=start_image_url,
            motion_prompt=motion_prompt,
            duration=duration,
            reference_image_urls=reference_urls,
        )
    except Exception as e:
        logger.exception("[reroll-video] scene %s _generate_video failed: %s", scene_num, e)
        error = f"_generate_video raised: {e}"

    final_url = raw_url
    # Re-host on GCS so the URL is stable (provider URLs often expire).
    if raw_url:
        gcs = getattr(processor, "gcs_storage_service", None)
        if gcs is not None:
            try:
                bucket = gcs_bucket or getattr(gcs, "bucket_name", None)
                key = f"reroll_scene_{scene_num}_{int(time.time())}.mp4"
                rehosted = gcs.upload_video_from_url(raw_url, key)
                if rehosted:
                    final_url = rehosted
                logger.info(
                    "[reroll-video] scene %s rehosted to GCS (bucket=%s)",
                    scene_num, bucket,
                )
            except Exception as upload_err:
                logger.warning(
                    "[reroll-video] scene %s GCS rehost failed (using raw URL): %s",
                    scene_num, upload_err,
                )

    elapsed = time.time() - t0

    if final_url and on_progress:
        try:
            on_progress("usage", {
                "service": "veo" if video_model.startswith("veo") else "kie",
                "step": f"reroll_scene_video_{scene_num}",
                "model": video_model,
                "provider": video_provider,
                "duration_seconds": duration,
                "label": f"Reroll scene {scene_num} video",
                "category": "videos",
                "success": True,
            })
        except Exception:
            pass

    return {
        "video_url": final_url,
        "raw_url": raw_url,
        "model_used": video_model,
        "provider_used": video_provider,
        "duration": duration,
        "elapsed_seconds": round(elapsed, 1),
        "error": error if not final_url else None,
    }
