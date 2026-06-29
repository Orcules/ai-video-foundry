import json, threading
from pathlib import Path
from typing import Optional

from api_pipeline.model_config import get_media_model
from api_pipeline.model_mappings_config import get_cost_fallback

PRICING_CONFIG = json.loads((Path(__file__).parent / "config" / "pricing.json").read_text())


def snap_duration(model: str, requested: float) -> int:
    """Round UP to nearest supported duration for the given model.

    Reads supportedDurations from pricing.json. Must round up because
    STEP 7.5 trims down — generated video must be >= requested duration.
    Falls back to defaultDurationSeconds if no supportedDurations defined.

    Looks up both ``model`` directly and ``model:*`` composite keys.
    """
    pricing = PRICING_CONFIG["models"].get(model, {})
    # If not found by bare model name, try to find any model:provider key
    if not pricing:
        for key, val in PRICING_CONFIG["models"].items():
            if key.startswith(f"{model}:") and isinstance(val, dict):
                pricing = val
                break
    supported = pricing.get("supportedDurations")
    if not supported:
        return int(pricing.get("defaultDurationSeconds", requested))
    for s in sorted(supported):
        if s >= requested:
            return s
    return max(supported)  # requested exceeds all — use largest


def _calculate_cost(model_provider_key: str, input_tokens=0, output_tokens=0,
                    media_duration_seconds=None, variant=None,
                    character_count=0, count=1) -> float:
    """Type-dispatched cost calculation using model:provider composite key.

    Falls back to bare model name lookup for backward compatibility.
    """
    pricing = PRICING_CONFIG["models"].get(model_provider_key)
    # Fallback: try bare model name (strip provider suffix)
    if not pricing:
        bare_model = model_provider_key.split(":")[0] if ":" in model_provider_key else model_provider_key
        pricing = PRICING_CONFIG["models"].get(bare_model)
    if not pricing:
        return 0.0
    t = pricing["type"]
    if t == "token":
        return (input_tokens / 1_000_000) * pricing.get("inputCostPer1M", 0) \
             + (output_tokens / 1_000_000) * pricing.get("outputCostPer1M", 0)
    elif t == "fixed":
        cost = pricing.get("costPerUnit", 0)
        if variant and "costVariants" in pricing:
            cost = pricing["costVariants"].get(variant, cost)
        return cost * count
    elif t == "duration":
        secs = media_duration_seconds or pricing.get("defaultDurationSeconds", 0)
        # Snap to the nearest supported duration (round UP) — the API generates
        # at a supported length and we get billed for that, even if the clip is
        # later trimmed down to the exact VO-synced duration.
        supported = pricing.get("supportedDurations")
        if supported and secs:
            for s in sorted(supported):
                if s >= secs:
                    secs = s
                    break
            else:
                secs = max(supported)
        cost_per_sec = pricing.get("costPerSecond", 0)
        if variant and "costVariants" in pricing:
            cost_per_sec = pricing["costVariants"].get(variant, cost_per_sec)
        return secs * cost_per_sec
    elif t == "character":
        return character_count * pricing.get("costPerCharacter", 0)
    return 0.0


class CostTracker:
    """Thread-safe cost accumulator with unified record_usage() and legacy wrappers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = []
        self._pricing_version = PRICING_CONFIG.get("version", "unknown")

    # ------------------------------------------------------------------
    # Unified recording method
    # ------------------------------------------------------------------

    def record_usage(self, data: dict) -> float:
        """Record a usage event using model:provider composite key.

        Args:
            data: Dict with keys:
                model: str — model name (e.g. "gemini-2.5-flash")
                provider: str — provider name (e.g. "vertex", "kie", "openai")
                category: str — cost category (e.g. "videos", "images", "gemini_text")
                label: str — human-readable description
                scene_id: optional scene identifier
                actual_cost_usd: optional float — if set, use directly instead of computing
                input_tokens: int — for token-based models
                output_tokens: int — for token-based models
                duration_seconds: float — for duration-based models
                resolution: str — cost variant key (e.g. "720p", "1k")
                has_audio: bool — appends "_audio" to resolution variant
                character_count: int — for character-based models (TTS)
                count: int — multiplier for fixed-cost models (default 1)

        Returns:
            The computed (or actual) cost in USD.
        """
        model = data.get("model", "unknown")
        provider = data.get("provider", "unknown")
        key = f"{model}:{provider}"

        # Prefer actual cost from provider (e.g. Vercel returns real billing amount)
        actual = data.get("actual_cost_usd")
        if actual is not None:
            cost = float(actual)
        else:
            variant = data.get("resolution")
            if data.get("has_audio") and variant:
                variant = f"{variant}_audio"
            cost = _calculate_cost(
                key,
                input_tokens=data.get("input_tokens", 0),
                output_tokens=data.get("output_tokens", 0),
                media_duration_seconds=data.get("duration_seconds"),
                variant=variant,
                character_count=data.get("character_count", 0),
                count=data.get("count", 1),
            )

        self._add(
            category=data.get("category", "unknown"),
            model=key,
            cost=cost,
            label=data.get("label", ""),
            scene_id=data.get("scene_id"),
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            media_duration_seconds=data.get("duration_seconds"),
            character_count=data.get("character_count"),
            image_count=data.get("count") if data.get("category") in ("images", "image") else None,
        )
        return cost

    # ------------------------------------------------------------------
    # Legacy record_* wrappers (used by sim_pipeline_runner and tests)
    # ------------------------------------------------------------------

    def record_gemini_text(self, model: str, prompt_tokens: int,
                           completion_tokens: int, label: str = "", scene_id=None) -> float:
        return self.record_usage({
            "model": model, "provider": "vertex",
            "category": "gemini_text", "label": label,
            "input_tokens": prompt_tokens, "output_tokens": completion_tokens,
            "scene_id": scene_id,
        })

    def record_gemini_image(self, model: str, count: int = 1, scene_id=None) -> float:
        return self.record_usage({
            "model": model, "provider": "direct",
            "category": "images", "label": f"{count} image(s)",
            "count": count, "scene_id": scene_id,
        })

    def record_veo3(self, duration_seconds: float, scene_id=None, model=None,
                    resolution: str = "720p", has_audio: bool = False) -> float:
        return self.record_usage({
            "model": model or get_media_model("veo3_video", get_cost_fallback("veo3_video", "veo-3.0-generate-001")),
            "provider": "direct",
            "category": "videos", "duration_seconds": duration_seconds,
            "resolution": resolution, "has_audio": has_audio,
            "scene_id": scene_id,
            "label": f"{duration_seconds}s video ({resolution}{'+audio' if has_audio else ''})",
        })

    def record_kling(self, duration_seconds: float, scene_id=None, model=None) -> float:
        return self.record_usage({
            "model": model or get_media_model("kling_video", get_cost_fallback("kling_video", "kling-2.5")),
            "provider": "kie",
            "category": "videos", "duration_seconds": duration_seconds,
            "scene_id": scene_id,
            "label": f"{duration_seconds}s video",
        })

    def record_runway(self, duration_seconds: float, scene_id=None, model=None) -> float:
        return self.record_usage({
            "model": model or "runway", "provider": "kie",
            "category": "videos", "duration_seconds": duration_seconds,
            "scene_id": scene_id,
            "label": f"{duration_seconds}s video",
        })

    def record_runway_direct(self, duration_seconds: int, scene_id=None,
                             model: str = None) -> float:
        return self.record_usage({
            "model": model or get_cost_fallback("runway_video", "runway-gen4-turbo"), "provider": "direct",
            "category": "videos", "duration_seconds": duration_seconds,
            "scene_id": scene_id,
            "label": f"{duration_seconds}s video",
        })

    def record_elevenlabs(self, character_count: int, model=None) -> float:
        return self.record_usage({
            "model": model or get_cost_fallback("elevenlabs_tts", "eleven_v3"), "provider": "elevenlabs",
            "category": "tts", "character_count": character_count,
            "label": f"{character_count} chars",
        })

    def record_nano_banana(self, count: int = 1, scene_id=None, model=None,
                           resolution: str = "1K") -> float:
        return self.record_usage({
            "model": model or get_cost_fallback("nano_banana_image", "nano-banana-pro"), "provider": "kie",
            "category": "images", "count": count,
            "resolution": resolution, "scene_id": scene_id,
            "label": f"{count} image(s) ({resolution})",
        })

    def record_suno(self, model=None) -> float:
        return self.record_usage({
            "model": model or get_cost_fallback("suno_music", "suno-v5"), "provider": "kie",
            "category": "music", "count": 1,
            "label": "music generation",
        })

    def record_rendi(self, operation: str = "") -> float:
        return self.record_usage({
            "model": "rendi", "provider": "rendi",
            "category": "rendi", "count": 1,
            "label": operation,
        })

    def record_zapcap(self, duration_seconds: float = 0, model=None) -> float:
        return self.record_usage({
            "model": "zapcap", "provider": "zapcap",
            "category": "subtitles", "duration_seconds": duration_seconds,
            "label": f"{duration_seconds}s subtitle burn-in",
        })

    def record_openai(self, model: str, prompt_tokens: int,
                      completion_tokens: int, label: str = "") -> float:
        return self.record_usage({
            "model": model, "provider": "openai",
            "category": "openai", "label": label,
            "input_tokens": prompt_tokens, "output_tokens": completion_tokens,
        })

    def record_vercel(self, model: str, prompt_tokens: int,
                      completion_tokens: int, label: str = "",
                      actual_cost_usd: float = None) -> float:
        """Record cost for a Vercel AI Hub call.

        If *actual_cost_usd* is provided (from Vercel's providerMetadata),
        that real billing amount is used directly.
        """
        return self.record_usage({
            "model": model, "provider": "vercel",
            "category": "vercel", "label": label,
            "input_tokens": prompt_tokens, "output_tokens": completion_tokens,
            "actual_cost_usd": actual_cost_usd,
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add(self, category, model, cost, label, scene_id=None, **extra):
        with self._lock:
            entry = {"category": category, "model": model, "cost_usd": round(cost, 6),
                     "label": label}
            if scene_id is not None:
                entry["scene_id"] = scene_id
            entry.update({k: v for k, v in extra.items() if v is not None})
            self._entries.append(entry)

    @property
    def total_usd(self) -> float:
        with self._lock:
            return round(sum(e["cost_usd"] for e in self._entries), 4)

    def build_summary(self) -> dict:
        with self._lock:
            entries = list(self._entries)

        total = sum(e["cost_usd"] for e in entries)

        def _cat(*names):
            return [e for e in entries if e["category"] in names]

        # Accept both wrapper names and monolith names for each category
        gemini_entries = _cat("gemini_text", "text")
        image_entries = _cat("images", "image")
        video_entries = _cat("videos", "video")
        tts_entries = _cat("tts")
        music_entries = _cat("music")
        rendi_entries = _cat("rendi", "ffmpeg")
        sub_entries = _cat("subtitles")
        openai_entries = _cat("openai")
        vercel_entries = _cat("vercel")

        return {
            "total_usd": round(total, 4),
            "pricing_version": self._pricing_version,
            "total_input_tokens": sum(e.get("input_tokens", 0) for e in entries),
            "total_output_tokens": sum(e.get("output_tokens", 0) for e in entries),
            "total_video_seconds": sum(e.get("media_duration_seconds", 0) for e in entries),
            "total_image_count": sum(e.get("image_count", 0) for e in entries),
            "breakdown": {
                "gemini_text": {
                    "input_tokens": sum(e.get("input_tokens", 0) for e in gemini_entries),
                    "output_tokens": sum(e.get("output_tokens", 0) for e in gemini_entries),
                    "total_cost": round(sum(e["cost_usd"] for e in gemini_entries), 4),
                },
                "openai": {
                    "input_tokens": sum(e.get("input_tokens", 0) for e in openai_entries),
                    "output_tokens": sum(e.get("output_tokens", 0) for e in openai_entries),
                    "total_cost": round(sum(e["cost_usd"] for e in openai_entries), 4),
                },
                "vercel": {
                    "input_tokens": sum(e.get("input_tokens", 0) for e in vercel_entries),
                    "output_tokens": sum(e.get("output_tokens", 0) for e in vercel_entries),
                    "total_cost": round(sum(e["cost_usd"] for e in vercel_entries), 4),
                },
                "images": {
                    "count": sum(e.get("image_count", 0) for e in image_entries),
                    "total_cost": round(sum(e["cost_usd"] for e in image_entries), 4),
                },
                "videos": {
                    "total_seconds": round(sum(e.get("media_duration_seconds", 0) for e in video_entries), 1),
                    "total_cost": round(sum(e["cost_usd"] for e in video_entries), 4),
                },
                "tts": {
                    "total_characters": sum(e.get("character_count", 0) for e in tts_entries),
                    "total_cost": round(sum(e["cost_usd"] for e in tts_entries), 4),
                },
                "music": {
                    "count": len(music_entries),
                    "total_cost": round(sum(e["cost_usd"] for e in music_entries), 4),
                },
                "rendi": {
                    "operation_count": len(rendi_entries),
                    "total_cost": round(sum(e["cost_usd"] for e in rendi_entries), 4),
                },
                "subtitles": {
                    "total_cost": round(sum(e["cost_usd"] for e in sub_entries), 4),
                },
            },
            "entries": entries,
        }

    def to_checkpoint(self) -> dict:
        with self._lock:
            return {"entries": list(self._entries), "pricing_version": self._pricing_version}

    @classmethod
    def from_checkpoint(cls, data: dict) -> "CostTracker":
        t = cls()
        t._entries = data.get("entries", [])
        t._pricing_version = data.get("pricing_version", t._pricing_version)
        return t


def estimate_cost(video_type: str, duration: int = 20, animation_model: str = "auto") -> dict:
    """Order-of-magnitude cost + wall-clock estimate for the chat summary card.

    Not for billing. Returns a low/high range so the summary can show "$2-4"
    instead of false precision.
    """
    duration = max(10, min(120, int(duration or 20)))
    scene_clip_len = 8  # Veo's typical clip length
    scene_count = max(1, -(-duration // scene_clip_len))  # ceil

    cost_per_sec_low, cost_per_sec_high = 0.10, 0.14
    am = (animation_model or "").lower()
    if am == "kling":
        cost_per_sec_low, cost_per_sec_high = 0.12, 0.18
    elif am == "runway":
        cost_per_sec_low, cost_per_sec_high = 0.15, 0.25

    video_low = scene_count * scene_clip_len * cost_per_sec_low
    video_high = scene_count * scene_clip_len * cost_per_sec_high

    image_count = scene_count + 1  # +1 for character/logo image
    image_cost = image_count * 0.04

    tts_chars = duration * 14
    tts_cost = tts_chars * 0.00018

    music_cost = 0.05
    llm_overhead = 0.03

    total_low = round(video_low + image_cost + tts_cost + music_cost + llm_overhead, 2)
    total_high = round(video_high + image_cost * 1.5 + tts_cost + music_cost + llm_overhead + 0.5, 2)

    wall_low_min = max(2, int(scene_count * 0.6))
    wall_high_min = max(4, int(scene_count * 1.7))

    return {
        "estimated_cost_usd_low": total_low,
        "estimated_cost_usd_high": total_high,
        "estimated_wall_clock_min_low": wall_low_min,
        "estimated_wall_clock_min_high": wall_high_min,
        "scene_count": scene_count,
    }


# Per-tool cost-per-second ranges (USD). Used by the storyboard estimator.
# These are deliberate over-estimates to avoid surprise bills in the chat summary.
_TOOL_COST_PER_SEC = {
    "veo":      (0.10, 0.14),
    "kling":    (0.12, 0.18),
    "runway":   (0.15, 0.25),
    "kenburns": (0.00, 0.001),    # Pure FFmpeg via Rendi — negligible
    "trim":     (0.00, 0.001),    # Same: just an FFmpeg trim
}


def estimate_storyboard_cost(storyboard: dict) -> dict:
    """Walk every clip in the storyboard, sum per-tool costs.

    Far more accurate than `estimate_cost(...)` because it knows which scenes
    reuse user assets (cheap), which use Ken Burns (free), and which spend on
    full video generation. The chat agent calls this before showing the summary
    card so the user sees an honest range.
    """
    if not isinstance(storyboard, dict):
        return estimate_cost("custom", 20, "auto")

    meta = storyboard.get("meta") or {}
    scenes = storyboard.get("scenes") or []
    target_duration = float(meta.get("target_duration_seconds", 20) or 20)

    # Lazy import to avoid pulling tvd_pipeline into wrapper-only environments.
    try:
        from tvd_pipeline.services.animation_router import pick_tool
    except Exception:
        pick_tool = None  # graceful degradation

    video_low = 0.0
    video_high = 0.0
    image_calls = 0      # T2I or composite calls — each ~$0.04
    composite_calls = 0  # NB2 composite — typically more expensive than plain T2I
    asset_seconds = 0.0  # cheap (just a trim)
    generated_seconds = 0.0

    for scene in scenes:
        for clip in scene.get("clips") or []:
            ctype = clip.get("type") or "generate"
            cdur = float(clip.get("duration") or 0.0)
            # framework_render clips fall back to Ken Burns + LLM codegen ~$0.01
            if ctype == "framework_render" or clip.get("_framework_placeholder"):
                video_low += 0.01
                video_high += 0.01
                generated_seconds += cdur
                continue
            tool = clip.get("_resolved_tool")
            if not tool and pick_tool is not None:
                try:
                    tool = pick_tool(clip, meta)
                except Exception:
                    tool = "veo"
            tool = tool or "veo"
            lo, hi = _TOOL_COST_PER_SEC.get(tool, _TOOL_COST_PER_SEC["veo"])
            video_low += cdur * lo
            video_high += cdur * hi
            if ctype == "asset_video":
                asset_seconds += cdur
            else:
                generated_seconds += cdur
            if ctype == "generate":
                image_calls += 1
            elif ctype == "composite":
                composite_calls += 1

    image_cost = image_calls * 0.04 + composite_calls * 0.06

    # E3: preview image rendering cost — one image per scene at the chosen model's rate.
    # Pro = $0.06, Banana 2 = $0.04, Gemini direct ≈ $0.04. If `preview_image_url`
    # is already set, the preview was already paid for (don't double-count).
    _PREVIEW_PRICE = {
        "nano-banana-pro": 0.06,
        "nano-banana-2": 0.04,
        "gemini-3-pro-image-preview": 0.04,
        "gemini-3.1-flash-image-preview": 0.02,
        "gemini-2.5-flash-image": 0.02,
    }
    preview_cost = 0.0
    for scene in scenes:
        if scene.get("preview_image_url"):
            continue  # already rendered
        model = scene.get("preview_image_model") or "nano-banana-pro"
        preview_cost += _PREVIEW_PRICE.get(model, 0.04)

    tts_chars = target_duration * 14
    tts_cost = tts_chars * 0.00018
    music_cost = 0.05 if not (storyboard.get("music") or {}).get("url") else 0.0
    llm_overhead = 0.05  # chat agent + storyboard builder

    total_low = round(video_low + image_cost + preview_cost + tts_cost + music_cost + llm_overhead, 2)
    total_high = round(
        video_high + image_cost * 1.5 + preview_cost + tts_cost + music_cost + llm_overhead + 0.5,
        2,
    )

    # Wall-clock: rough — ~30-90s per generated clip, instant for asset/kenburns.
    scene_count = sum(1 for _ in scenes)
    generated_clips = sum(
        1 for s in scenes for c in (s.get("clips") or [])
        if (c.get("type") or "generate") not in ("asset_video", "ken_burns")
    )
    wall_low_min = max(2, int(generated_clips * 0.5))
    wall_high_min = max(4, int(generated_clips * 1.5))

    return {
        "estimated_cost_usd_low": total_low,
        "estimated_cost_usd_high": total_high,
        "estimated_wall_clock_min_low": wall_low_min,
        "estimated_wall_clock_min_high": wall_high_min,
        "scene_count": scene_count,
        "generated_clip_count": generated_clips,
        "asset_seconds": round(asset_seconds, 1),
        "generated_seconds": round(generated_seconds, 1),
        "preview_cost_usd": round(preview_cost, 2),
    }


def estimate_scene_video_cost(
    clip: dict,
    storyboard_meta: Optional[dict] = None,
) -> dict:
    """Estimate cost for a SINGLE clip's video generation.

    Used by /reroll-scene-video so the UI can show the user the per-reroll cost
    before/after the call (separate from the full storyboard estimate).

    Returns ``{"estimated_cost_usd_low", "estimated_cost_usd_high", "tool",
    "duration"}``.
    """
    try:
        from tvd_pipeline.services.animation_router import pick_tool
    except Exception:
        pick_tool = None

    cdur = float((clip or {}).get("duration") or 0.0)
    tool = (clip or {}).get("_resolved_tool")
    if not tool and pick_tool is not None:
        try:
            tool = pick_tool(clip or {}, storyboard_meta or {})
        except Exception:
            tool = "veo"
    tool = tool or "veo"
    lo, hi = _TOOL_COST_PER_SEC.get(tool, _TOOL_COST_PER_SEC["veo"])
    return {
        "estimated_cost_usd_low": round(cdur * lo, 4),
        "estimated_cost_usd_high": round(cdur * hi, 4),
        "tool": tool,
        "duration": cdur,
    }
