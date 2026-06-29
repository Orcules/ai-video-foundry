"""Per-provider concurrency for scene images and animations (Sheets Image API / Animation model)."""

import os
from typing import Optional, Tuple

from tvd_pipeline.data_loader import get_provider_rate_limits


def resolve_scene_video_limits(cfg, video_model, video_provider, veo_retry_cfg) -> Tuple[int, float]:
    """Return (max_concurrent, delay_sec_after_each_animation)."""
    sv = get_provider_rate_limits().get("scene_video", {})
    vm_s = (video_model or "").lower()
    vp = video_provider

    def _int_env(k: str, default: int) -> int:
        v = os.environ.get(k, "").strip()
        return int(v) if v.isdigit() else default

    def _float_env(k: str, default: float) -> float:
        v = os.environ.get(k, "").strip()
        try:
            return float(v) if v != "" else default
        except ValueError:
            return default

    if not video_model or video_model == "none" or vp is None:
        b = sv.get("none", {"max_concurrent": 32, "delay_after_each_sec": 0})
        return _int_env("SCENE_VIDEO_NONE_MAX_CONCURRENT", int(b["max_concurrent"])), _float_env(
            "SCENE_VIDEO_NONE_DELAY_SEC", float(b.get("delay_after_each_sec") or 0)
        )

    if vp == "direct" and vm_s.startswith("veo"):
        b = sv.get("veo_vertex", {"max_concurrent": 2, "delay_after_each_sec": 6})
        mc = int(veo_retry_cfg.get("max_concurrent_requests") or b["max_concurrent"])
        mc = _int_env("SCENE_VIDEO_VEO_MAX_CONCURRENT", mc)
        delay = _float_env("SCENE_VIDEO_VEO_DELAY_SEC", float(getattr(cfg, "SCENE_VIDEO_RATE_LIMIT_DELAY", 6) or 0))
        return mc, delay

    if vp == "kie" and "kling" in vm_s:
        b = sv.get("kling_kie", {"max_concurrent": 4, "delay_after_each_sec": 3})
        return _int_env("SCENE_VIDEO_KLING_MAX_CONCURRENT", int(b["max_concurrent"])), _float_env(
            "SCENE_VIDEO_KLING_DELAY_SEC", float(b.get("delay_after_each_sec") or 0)
        )

    if vp == "kie":
        b = sv.get("runway_kie", {"max_concurrent": 4, "delay_after_each_sec": 3})
        return _int_env("SCENE_VIDEO_RUNWAY_MAX_CONCURRENT", int(b["max_concurrent"])), _float_env(
            "SCENE_VIDEO_RUNWAY_DELAY_SEC", float(b.get("delay_after_each_sec") or 0)
        )

    b = sv.get("kie_default", {"max_concurrent": 4, "delay_after_each_sec": 2})
    return int(b["max_concurrent"]), float(b.get("delay_after_each_sec") or 0)


def resolve_scene_image_workers(cfg, use_google_image: bool, use_kie_flash: bool, image_model: Optional[str]) -> Tuple[int, str]:
    """Return (parallel_workers, label)."""
    si = get_provider_rate_limits().get("scene_image", {})
    im = image_model or ""
    g31 = getattr(cfg, "GEMINI_31_FLASH_IMAGE_MODEL", "") or ""

    def _pw(key: str, fallback: int) -> int:
        b = si.get(key, {})
        base = int(b.get("parallel_workers", fallback))
        envk = "SCENE_IMAGE_" + key.upper() + "_WORKERS"
        v = os.environ.get(envk, "").strip()
        return int(v) if v.isdigit() else base

    if use_google_image:
        if im == "gemini-25-flash-image":
            return _pw("gemini_25_flash_vertex", getattr(cfg, "GEMINI_25_FLASH_IMAGE_PARALLEL_WORKERS", 12)), "Vertex Gemini 2.5 Flash image"
        if g31 and g31 in im:
            return _pw("gemini_31_flash_vertex", getattr(cfg, "GEMINI_31_FLASH_IMAGE_PARALLEL_WORKERS", 4)), "Vertex Gemini 3.1 Flash image"
        if im == "gemini-3-pro-image":
            return _pw("gemini_3_pro_vertex", getattr(cfg, "GEMINI_SCENE_IMAGE_PARALLEL_WORKERS", 1)), "Vertex Gemini 3 Pro image"
        if im == "nano-banana-2":
            return _pw("nano_banana_2_vertex", getattr(cfg, "GEMINI_RATE_LIMITED_IMAGE_PARALLEL_WORKERS", 1)), "Vertex NANO BANANA 2"
        return _pw("gemini_3_pro_vertex", 1), "Vertex Gemini image (default)"
    if use_kie_flash:
        return _pw("gemini_3_flash_kie", 8), "Kie Gemini 3 Flash image"
    return _pw("nano_banana_pro_kie", getattr(cfg, "KIE_SCENE_IMAGE_PARALLEL_WORKERS", 12)), "Kie Nano Banana Pro"


def get_scene_image_stagger_seconds(use_google_image: bool, use_kie_flash: bool, image_model: Optional[str]) -> float:
    """Return stagger_seconds for scene image (delay before starting each image by index). 0 = no stagger."""
    if use_google_image:
        return 0.0
    si = get_provider_rate_limits().get("scene_image", {})
    if use_kie_flash:
        b = si.get("gemini_3_flash_kie", {})
    else:
        b = si.get("nano_banana_pro_kie", {})
    return float(b.get("stagger_seconds") or 0)
