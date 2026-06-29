"""Mock service classes for simulation mode.

Runs the real pipeline code with placeholder data instead of calling
external APIs.  Every mock returns the exact shapes the pipeline expects
so that SSE events, intermediates, cost tracking, and step flow are
identical to a real run — just instant and free.

Timing model:
  simulation_duration="none"  → instant (no delays)
  simulation_duration="real"  → production-speed delays (~698s for 6-scene product)
  simulation_duration="25s"   → scale real timings proportionally to fit 25s total
  simulation_duration="1.5m"  → scale real timings proportionally to fit 90s total
"""

import json
import os
import re
import time
import uuid
import logging
from typing import Dict, Any, Optional, List, Tuple

from api_pipeline.model_config import get_text_model, get_media_model
from api_pipeline.model_mappings_config import get_cost_fallback, get_text_fallback_model
from api_pipeline.data_config import get_speech_rate
from api_pipeline.pipelines.base import _check_abort

logger = logging.getLogger(__name__)

# Placeholder URLs
_SIM_IMAGE = "https://storage.googleapis.com/automatiq/simulation/placeholder.jpg"
_SIM_VIDEO = "https://storage.googleapis.com/automatiq/simulation/placeholder.mp4"
_SIM_VIDEO_MIXED = "https://storage.googleapis.com/automatiq/simulation/placeholder_mixed.mp4"
_SIM_AUDIO = "https://storage.googleapis.com/automatiq/simulation/placeholder.mp3"

# Fake audio bytes (minimal valid MP3 frame — enough to not crash any len() checks)
_FAKE_AUDIO_BYTES = b"\xff\xfb\x90\x00" + b"\x00" * 417

# Load real-world timing baselines
_TIMINGS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "simulation_timings.json")
with open(_TIMINGS_PATH) as _f:
    _TIMINGS = json.load(_f)

# Load per-video-type asset pools for realistic simulation
_ASSETS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "simulation_assets.json")
try:
    with open(_ASSETS_PATH) as _f:
        _SIM_ASSETS = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    _SIM_ASSETS = {}


def _get_asset_pool(video_type: str = None) -> dict:
    """Return the asset pool for a video type, merging with default for missing keys."""
    default = _SIM_ASSETS.get("default", {})
    if video_type:
        pool = _SIM_ASSETS.get(video_type.lower(), {})
        if pool:
            merged = dict(default)
            merged.update(pool)
            return merged
    return default


def _ref_image(scene_idx: int = 0, video_type: str = None) -> str:
    """Return a real image URL for the given scene index, cycling through the pool."""
    images = _get_asset_pool(video_type).get("scene_images", [])
    return images[scene_idx % len(images)] if images else _SIM_IMAGE


def _ref_video(scene_idx: int = 0, video_type: str = None) -> str:
    """Return a real video URL for the given scene index, cycling through the pool."""
    videos = _get_asset_pool(video_type).get("scene_videos", [])
    return videos[scene_idx % len(videos)] if videos else _SIM_VIDEO


def _ref_music(video_type: str = None) -> str:
    return _get_asset_pool(video_type).get("music_url") or _SIM_AUDIO


def _ref_vo(video_type: str = None) -> str:
    return _get_asset_pool(video_type).get("vo_audio_url") or _SIM_AUDIO


def _ref_product(video_type: str = None) -> str:
    return _get_asset_pool(video_type).get("product_image") or _SIM_IMAGE


def _ref_final(video_type: str = None) -> str:
    return _get_asset_pool(video_type).get("final_video_url") or _SIM_VIDEO


# Backward-compat constants (resolve to default pool)
_REF_IMAGES = _get_asset_pool().get("scene_images", [])
_REF_VIDEOS = _get_asset_pool().get("scene_videos", [])
_REF_MUSIC = _ref_music()
_REF_VO = _ref_vo()
_REF_PRODUCT = _ref_product()
_REF_FINAL = _ref_final()


# ---------------------------------------------------------------------------
# Type 2 (monolith) simulation: replace placeholder URLs with real GCS assets
# ---------------------------------------------------------------------------
_PLACEHOLDER_MARKER = "/simulation/placeholder"


def _inject_real_sim_assets(result: dict, video_type: str = None) -> dict:
    """Replace placeholder URLs in a monolith result dict with real GCS URLs.

    The monolith runs with simulation=True and produces placeholder URLs
    like ``/simulation/placeholder.mp4``.  This function walks the result
    dict and swaps them for real GCS asset URLs from simulation_assets.json
    so that Mux upload, dashboard preview, and SSE events all work.
    """
    result = dict(result)  # shallow copy

    # Replace list fields (scene images)
    for key in ("scene_images", "scene_images_all"):
        lst = result.get(key)
        if isinstance(lst, list):
            result[key] = [
                _ref_image(i, video_type) if isinstance(u, str) and _PLACEHOLDER_MARKER in u else u
                for i, u in enumerate(lst)
            ]

    # Replace list fields (scene videos)
    for key in ("scene_videos",):
        lst = result.get(key)
        if isinstance(lst, list):
            result[key] = [
                _ref_video(i, video_type) if isinstance(u, str) and _PLACEHOLDER_MARKER in u else u
                for i, u in enumerate(lst)
            ]

    # Replace single-value fields
    _singles = {
        "vo_audio_url": _ref_vo(video_type),
        "music_url": _ref_music(video_type),
        "clean_product_image": _ref_product(video_type),
        "influencer_image": _ref_image(0, video_type),
    }
    for key, replacement in _singles.items():
        val = result.get(key)
        if isinstance(val, str) and _PLACEHOLDER_MARKER in val:
            result[key] = replacement

    # Replace final video chain (covers both monolith key variants)
    _final = _ref_final(video_type)
    for key in ("final_video_url", "subtitled_url", "subtitled_video_url",
                "rendi_scene_voice_url", "concat_url", "audio_mix_url"):
        val = result.get(key)
        if isinstance(val, str) and _PLACEHOLDER_MARKER in val:
            result[key] = _final

    return result


def _swap_sim_asset_url(step: str, url: str, video_type: str = None) -> str:
    """Replace a placeholder asset_url in an SSE event based on the step name.

    Called from the progress callback for Type 2 (monolith) simulation runs
    so that SSE events carry real GCS URLs instead of placeholders.
    """
    if _PLACEHOLDER_MARKER not in url:
        return url

    m = re.match(r"scene_(\d+)_(image|video)", step)
    if m:
        idx = int(m.group(1)) - 1  # scene_1 → index 0
        return _ref_image(idx, video_type) if m.group(2) == "image" else _ref_video(idx, video_type)
    if step == "music":
        return _ref_music(video_type)

    # Fallback: swap by file extension
    if url.endswith(".mp4"):
        return _ref_final(video_type)
    if url.endswith(".mp3"):
        return _ref_music(video_type)
    return _ref_image(0, video_type)


def _parse_simulation_duration(value: str):
    """Parse simulation_duration string into (mode, target_seconds).

    Returns:
        ("none", 0)           — instant mode
        ("real", None)        — real-time mode (scale=1.0)
        ("scaled", seconds)   — proportionally scaled to target
    """
    value = (value or "none").strip().lower()
    if value == "none":
        return ("none", 0)
    if value == "real":
        return ("real", None)
    m = re.match(r"^(\d+\.?\d*)(s|m)$", value)
    if m:
        amount = float(m.group(1))
        unit = m.group(2)
        seconds = amount if unit == "s" else amount * 60
        return ("scaled", seconds)
    return ("none", 0)


# ---------------------------------------------------------------------------
# SimGeminiService
# ---------------------------------------------------------------------------
class SimGeminiService:
    def __init__(self, delay_fn=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self.model = get_text_model("parse_prompt", get_text_fallback_model())
        self.initialized = True
        self.last_usage_metadata = None

    def _set_usage(self, prompt_tokens: int = 500, completion_tokens: int = 200):
        self.last_usage_metadata = {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
        }

    # --- text methods ---

    def parse_product_prompt(self, prompt: str, image_urls=None):
        self._delay("step_1")
        self._set_usage(2000, 800)
        return {
            "text_1": f"[Sim] Product overview: {(prompt or '')[:120]}",
            "text_2": "[Sim] Marketing goal: increase brand awareness and drive conversions",
            "text_3": "[Sim] Visual style: modern, cinematic, premium feel with clean compositions",
            "text_4": "[Sim] Target audience: 25-40 year-old professionals who value quality",
        }

    def describe_character(self, image_url: str):
        self._delay("step_0")
        self._set_usage(1500, 400)
        return "[Sim] A confident person with warm features, bright eyes, and a natural smile. Wearing casual-professional attire."

    def describe_characters(self, image_urls):
        if not image_urls:
            return None
        if len(image_urls) == 1:
            return self.describe_character(image_urls[0])
        descriptions = []
        for i, url in enumerate(image_urls, 1):
            desc = self.describe_character(url)
            if desc:
                descriptions.append(f"Person {i}: {desc}")
        return " ".join(descriptions) if descriptions else None

    def generate_product_video_scenes(self, *, text_1, text_2, text_3, text_4,
                                       prompt, image_urls, target_duration,
                                       character_description=None, character_urls=None,
                                       logo_url=None, slogan_text=None,
                                       reference_video_structure=None,
                                       language=None, country=None,
                                       vo_timing=None):
        self._delay("step_3")
        self._set_usage(3000, 1500)
        scene_count = max(3, min(8, int(target_duration / 4)))
        per_scene = round(target_duration / scene_count, 1)
        scenes = []
        for i in range(scene_count):
            is_last = i == scene_count - 1
            scenes.append({
                "scene_num": i + 1,
                "image_prompt": f"[Sim] Scene {i + 1}: {'CTA — logo and slogan' if is_last else 'product showcase'}",
                "motion_prompt": "Slow zoom in with gentle parallax",
                "duration": per_scene,
                "product_visible": i < 3,
                "has_character": bool(character_description),
                "narrative_role": "cta" if is_last else "body",
            })
        return {
            "scenes": scenes,
            "total_duration": target_duration,
            "music_style": "upbeat corporate, modern and inspiring",
        }

    def generate_influencer_prompts(self, *, text_1, text_2, text_3, text_4,
                                     prompt, target_duration,
                                     influencer_description=None,
                                     ref_image_analyses=None,
                                     logo_url=None, slogan_text=None,
                                     language=None, country=None,
                                     vo_timing=None,
                                     enrich_cta_with_influencer=False,
                                     **kwargs):
        self._delay("step_3")
        self._set_usage(3000, 1500)
        scene_count = max(3, min(8, int(target_duration / 4)))
        per_scene = round(target_duration / scene_count, 1)
        scene_prompts = []
        for i in range(scene_count):
            scene_prompts.append({
                "scene_number": i + 1,
                "first_prompt": f"[Sim] UGC scene {i + 1} with influencer",
                "second_prompt": "Natural camera movement, person reacting genuinely",
                "duration": per_scene,
                "product_visible": True,
                "is_cta": i == scene_count - 1,
            })
        return {
            "influencer_description": influencer_description or "[Sim] A confident influencer with natural beauty",
            "scene_prompts": scene_prompts,
        }

    def generate_influencer_vo_script(self, *, text_1, text_2, text_3,
                                       target_duration, language=None,
                                       country=None, raw_prompt=None,
                                       influencer_description=None, **kwargs):
        self._delay("step_2.7", 0.4)
        self._set_usage(2000, 600)
        word_count = int(target_duration * get_speech_rate(language or "en"))
        words = ["[Sim]", "Hey", "everyone!", "Check", "out", "this", "amazing", "product!"]
        return " ".join((words * (word_count // len(words) + 1))[:word_count])

    def generate_music_description_from_text(self, text):
        self._delay("steps_4_7", 0.05)
        self._set_usage(800, 200)
        return "Upbeat modern pop with electronic elements, warm and inviting mood, 120 BPM"

    def analyze_reference_video_structure(self, path):
        self._delay("step_2.5")
        self._set_usage(5000, 1000)
        return {
            "scene_count": 5,
            "scenes": [
                {"index": i, "description": f"[Sim] Reference scene {i + 1}", "duration": 6.0}
                for i in range(5)
            ],
        }

    # Vertex AI helpers that the pipeline may touch
    def _get_vertex_url(self, model=None):
        return "https://sim.vertex.ai/unused"

    def _get_vertex_headers(self):
        return {"Authorization": "Bearer sim"}


# ---------------------------------------------------------------------------
# SimGeminiImageService
# ---------------------------------------------------------------------------
class SimGeminiImageService:
    def __init__(self, delay_fn=None, video_type=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self._vt = video_type
        self.initialized = True

    def generate_image(self, prompt, scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.15)
        return _ref_image(scene_idx, self._vt)

    def generate_scene_image(self, *, image_prompt, scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.15)
        return _ref_image(scene_idx, self._vt)


# ---------------------------------------------------------------------------
# SimKieService
# ---------------------------------------------------------------------------
class SimKieService:
    def __init__(self, delay_fn=None, video_type=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self._vt = video_type

    def generate_clean_product_image(self, *, reference_image_urls=None,
                                      product_description=None, resolution=None,
                                      **kwargs):
        self._delay("step_2")
        return _ref_product(self._vt)

    def generate_scene_image(self, *, image_prompt, product_reference_urls=None,
                              product_description=None, product_visible=False,
                              visual_style=None, character_reference_url=None,
                              has_character=False, logo_reference_url=None,
                              resolution=None, scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.15)
        return _ref_image(scene_idx, self._vt)

    def generate_scene_image_flash(self, *, image_prompt, scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.15)
        return _ref_image(scene_idx, self._vt)

    def generate_video_kling(self, *, prompt, image_url, duration=5, scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.80)
        return _ref_video(scene_idx, self._vt)

    def generate_video_runway(self, *, prompt, image_url, duration=5, scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.80)
        return _ref_video(scene_idx, self._vt)


# ---------------------------------------------------------------------------
# SimVeo3Service
# ---------------------------------------------------------------------------
class SimVeo3Service:
    def __init__(self, delay_fn=None, video_type=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self._vt = video_type
        self.model = get_media_model("veo3_video", get_cost_fallback("veo3_video", "veo-3.0-generate-001"))

    def generate_video_from_image(self, *, image_url, motion_prompt,
                                   duration=5, resolution=None, scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.80)
        return _ref_video(scene_idx, self._vt)


# ---------------------------------------------------------------------------
# SimElevenLabsService
# ---------------------------------------------------------------------------
class SimElevenLabsService:
    def __init__(self, delay_fn=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)

    def text_to_speech_with_timestamps(self, text, voice_id=None,
                                        language=None, **kwargs):
        self._delay("step_2.7", 0.6)
        words = text.split()
        segments = []
        t = 0.0
        for w in words:
            segments.append({"text": w, "start_time": round(t, 3), "end_time": round(t + 0.3, 3)})
            t += 0.35
        return (_FAKE_AUDIO_BYTES, segments)

    def text_to_speech(self, text, voice_id=None, language=None, **kwargs):
        self._delay("step_2.7", 0.6)
        return _FAKE_AUDIO_BYTES

    def pick_random_voice(self, gender=None, language=None):
        return "sim_voice_id"


# ---------------------------------------------------------------------------
# SimOpenAIService
# ---------------------------------------------------------------------------
class SimOpenAIService:
    class _FakeClient:
        class _Chat:
            class _Completions:
                def create(self, **kwargs):
                    class _FakeUsage:
                        prompt_tokens = 500
                        completion_tokens = 200
                    class _FakeMessage:
                        content = "[Sim] A warm, inspiring voiceover script describing the product..."
                    class _FakeChoice:
                        message = _FakeMessage()
                    class _FakeResp:
                        usage = _FakeUsage()
                        choices = [_FakeChoice()]
                    return _FakeResp()
            completions = _Completions()
        chat = _Chat()

    def __init__(self, delay_fn=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self.client = self._FakeClient()

    def generate_music_description_from_text(self, text, cost_tracker=None):
        self._delay("steps_4_7", 0.05)
        if cost_tracker:
            cost_tracker.record_openai("gpt-4o", 500, 100, "music_description")
        return "Upbeat modern electronic music, warm and inviting, 120 BPM"

    def generate_music_description(self, scene_prompts, cost_tracker=None):
        return self.generate_music_description_from_text(str(scene_prompts), cost_tracker)


# ---------------------------------------------------------------------------
# SimRendiService
# ---------------------------------------------------------------------------
class SimRendiService:
    def __init__(self, delay_fn=None, video_type=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self._vt = video_type
        self.api_key = "sim"
        self.base_url = "https://sim.rendi.dev"
        self.headers = {"Authorization": "Bearer sim"}

    def trim_videos_batch(self, video_data, add_buffer_except_last=False,
                          videos_have_audio=False, **kwargs):
        self._delay("step_7.5")
        return video_data

    def concatenate_videos(self, video_data=None, assume_clips_have_audio=False,
                           dissolve_seconds=None, **kwargs):
        self._delay("step_8", 0.5)
        return _ref_final(self._vt)

    def add_vo_and_music_to_video(self, *, video_url, vo_url, music_url,
                                   vo_volume=1.0, music_volume=0.2, **kwargs):
        self._delay("step_8", 0.5)
        return _SIM_VIDEO_MIXED

    def add_audio_to_video(self, *, video_url, audio_url, **kwargs):
        self._delay("step_8", 0.3)
        return _SIM_VIDEO_MIXED

    def add_background_music_to_video(self, *, video_url, music_url,
                                       music_volume=0.3, **kwargs):
        self._delay("step_8", 0.2)
        return _SIM_VIDEO_MIXED

    def get_video_duration_cloud(self, url):
        return 30.0

    def slow_motion_video(self, url, factor=2.0, **kwargs):
        self._delay("step_8", 0.1)
        return _SIM_VIDEO

    def trim_video(self, url, start=None, end=None, duration=None, **kwargs):
        self._delay("step_7.5", 0.3)
        return _SIM_VIDEO

    def _wait_for_command(self, cmd_id):
        return _SIM_VIDEO


# ---------------------------------------------------------------------------
# SimSunoService
# ---------------------------------------------------------------------------
class SimSunoService:
    def __init__(self, delay_fn=None, video_type=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self._vt = video_type

    def generate_pure_music(self, style_description=None, **kwargs):
        self._delay("steps_4_7", 0.05)
        return _ref_music(self._vt)


# ---------------------------------------------------------------------------
# SimZapCapService
# ---------------------------------------------------------------------------
class SimZapCapService:
    def __init__(self, delay_fn=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self.default_template_id = "sim"

    def add_subtitles(self, video_url, language=None, transcript=None, **kwargs):
        self._delay("step_9")
        return video_url  # passthrough


# ---------------------------------------------------------------------------
# SimGCSService
# ---------------------------------------------------------------------------
class SimGCSService:
    def __init__(self, delay_fn=None, video_type=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self._vt = video_type
        self._initialized = True

    def upload_bytes(self, content, key, content_type=None):
        return f"https://storage.googleapis.com/automatiq/simulation/{key}"

    def upload_audio_bytes(self, audio_data=None, key_name=None, **kwargs):
        return _ref_vo(self._vt)


# ---------------------------------------------------------------------------
# SimMuxService
# ---------------------------------------------------------------------------
class SimMuxService:
    def __init__(self, delay_fn=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)

    def upload_video_async(self, url, job_id, output_resolution="720p_low"):
        return {
            "upload_id": f"sim_{uuid.uuid4().hex[:8]}",
            "playback_id": f"sim_{uuid.uuid4().hex[:8]}",
        }


# ---------------------------------------------------------------------------
# SimRunwayDirectService
# ---------------------------------------------------------------------------
class SimRunwayDirectService:
    """Simulation mock for direct Runway API."""

    def __init__(self, delay_fn=None, video_type=None):
        self._delay = delay_fn or (lambda step_key, fraction=1.0: None)
        self._vt = video_type
        self.initialized = True

    def generate_video_from_image(self, image_url, prompt,
                                   duration=5, aspect_ratio="9:16",
                                   model="gen4_turbo", scene_idx=0, **kwargs):
        self._delay("steps_4_7", 0.80)
        return _ref_video(scene_idx, self._vt)


# ---------------------------------------------------------------------------
# SimServiceRegistry — drop-in replacement for ServiceRegistry
# ---------------------------------------------------------------------------
class SimServiceRegistry:
    """Registry of mock services for simulation mode.

    Exposes the same attributes as the real ServiceRegistry so the pipeline
    code runs unchanged.
    """

    def __init__(self, simulation_duration="none", job_id=None, supabase=None,
                 sim_duration_seconds=None, video_type=None):
        self.simulation = True
        self._job_id = job_id
        self._supabase = supabase
        self._video_type = video_type

        # Backward compat: convert legacy int param
        if simulation_duration == "none" and sim_duration_seconds is not None:
            if sim_duration_seconds == 0:
                simulation_duration = "none"
            else:
                simulation_duration = f"{sim_duration_seconds}s"

        self._simulation_duration = simulation_duration
        self._mode, self._target_seconds = _parse_simulation_duration(simulation_duration)

        # Load timing baselines
        self._fixed_steps = _TIMINGS["fixed_steps"]
        self._per_scene_seconds = _TIMINGS["per_scene_seconds"]
        self._baseline_scene_count = _TIMINGS["baseline_scene_count"]

        # Default scene count (updated by set_scene_count after pipeline computes it)
        self._scene_count = self._baseline_scene_count
        self._recalculate_scale()

        # Import Config to get voice IDs, durations, etc.
        from api_pipeline.services.base.config import Config
        self.config = Config()

        delay_fn = self._sim_delay
        vt = video_type
        self.gemini = SimGeminiService(delay_fn)
        self.gemini_image = SimGeminiImageService(delay_fn, video_type=vt)
        self.kie = SimKieService(delay_fn, video_type=vt)
        self.veo3 = SimVeo3Service(delay_fn, video_type=vt)
        self.veo3_full = SimVeo3Service(delay_fn, video_type=vt)
        self.openai = SimOpenAIService(delay_fn)
        self.elevenlabs = SimElevenLabsService(delay_fn)
        self.suno = SimSunoService(delay_fn, video_type=vt)
        self.rendi = SimRendiService(delay_fn, video_type=vt)
        self.zapcap = SimZapCapService(delay_fn)
        self.gcs_storage = SimGCSService(delay_fn, video_type=vt)
        self.gcs_video = SimGCSService(delay_fn, video_type=vt)
        self.mux = SimMuxService(delay_fn)
        self.runway_direct = SimRunwayDirectService(delay_fn, video_type=vt)

        logger.info("SimServiceRegistry initialized — simulation_duration=%s, mode=%s, scale=%.3f, video_type=%s",
                     simulation_duration, self._mode, self._scale, video_type)

    def _recalculate_scale(self):
        """Recalculate the time scale factor based on mode and scene count."""
        asset_total = self._per_scene_seconds * self._scene_count
        fixed_total = sum(self._fixed_steps.values())
        self._real_total = fixed_total + asset_total
        self._asset_total = asset_total

        if self._mode == "none":
            self._scale = 0.0
        elif self._mode == "real":
            self._scale = 1.0
        else:  # scaled
            self._scale = self._target_seconds / self._real_total if self._real_total > 0 else 0.0

    def set_scene_count(self, n: int):
        """Called by sim_pipeline_runner after scene count is computed.

        Recalculates the scale factor since asset generation time depends
        on scene count.
        """
        self._scene_count = n
        self._recalculate_scale()
        logger.debug("Scene count set to %d — real_total=%.1fs, scale=%.3f",
                     n, self._real_total, self._scale)

    def _sim_delay(self, step_key: str, fraction: float = 1.0):
        """Sleep for a duration proportional to the real-world timing of step_key.

        Args:
            step_key: Pipeline step identifier (e.g. "step_1", "steps_4_7").
            fraction: Sub-step fraction (e.g. 0.15 for image portion of asset gen).
        """
        if self._scale <= 0:
            return
        # Look up base seconds
        if step_key == "steps_4_7":
            base = self._per_scene_seconds
        elif step_key in self._fixed_steps:
            base = self._fixed_steps[step_key]
        else:
            base = 5.0  # fallback for unknown steps
        remaining = base * fraction * self._scale
        while remaining > 0:
            chunk = min(2.0, remaining)
            time.sleep(chunk)
            remaining -= chunk
            if self._supabase and self._job_id:
                _check_abort(self._supabase, self._job_id)
