"""Test the INFLUENCER_IN_VENUE pipeline in isolation.

Runs the 3-step flow that the pipeline uses for venue compositing:
  Step 1: Joint venue + influencer LLM analysis (image descriptions)
  Step 2: NB2 compositing (place influencer in venue image)
  Step 3: Reference-to-video animation (animate the composite)

Inputs: local venue + influencer images (uploaded to GCS at runtime)
and a mock Director output following the exact Director prompt rules.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_influencer_in_venue

Optional flags:
    --skip-analysis     Skip Step 1 (LLM analysis) — use a generic prompt
    --skip-video        Skip Step 3 (ref-to-video) — only test NB2 compositing
    --venue URL         Override venue image URL (skips local upload)
    --influencer URL    Override influencer image URL (skips local upload)
    --motion "prompt"   Override the Director motion prompt
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.kie import KieAIService
from tvd_pipeline.data_loader import get_kie_config
from tvd_pipeline.config import get_pipeline_defaults
from tvd_pipeline.prompt_loader import get_prompt_loader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = os.path.join(script_dir, "test_output", "influencer_in_venue")

# ---------------------------------------------------------------------------
# Local test images (relative to this script's directory)
# ---------------------------------------------------------------------------
VENUE_IMAGE_PATH = os.path.join(script_dir, "exoeriment", "venue.webp")
INFLUENCER_IMAGE_PATH = os.path.join(script_dir, "exoeriment", "inflcunecer2.jpg")

# ---------------------------------------------------------------------------
# Director output (mock) — follows ugc_director_system.md rules:
#   description = first frame pose, ends with "use the exact same venue..."
#   motion_prompt = body actions + camera, NO facial expressions
# ---------------------------------------------------------------------------
# Director's description (first frame — static pose for NB2 compositing)
DIRECTOR_DESCRIPTION = (
    "Close-up shot. She is sitting at the nearest table in the foreground of the Japanese restaurant, "
    "facing the camera. She fills the lower half of the frame, visible from the waist up. "
    "Use the exact same venue, don't change venue."
)

# Director's motion prompt (how video continues — NO facial expressions per rules)
DIRECTOR_MOTION_PROMPT = (
    "She leans in slightly and gives an inviting open-handed gesture toward the room. "
    "She turns back to camera with bright energy. "
    "The camera makes a gentle push-in from a medium shot to a slightly tighter framing."
)

# Full mock director clip — mirrors what _resolve_beat_clips() produces
MOCK_DIRECTOR_CLIP = {
    "type": "image",
    "clip_index": 0,
    "reference_image_index": 0,
    "variant": "influencer_in_venue",
    "description": DIRECTOR_DESCRIPTION,
    "duration": 4.0,
    "motion_prompt": DIRECTOR_MOTION_PROMPT,
}


def upload_local_image(gcs: GCSStorageService, image_path: str, label: str, ts: int) -> str | None:
    """Upload a local image to GCS and return the public URL."""
    if not os.path.isfile(image_path):
        print(f"  ERROR: {label} image not found: {image_path}")
        return None

    ext = os.path.splitext(image_path)[1].lower()
    content_type_map = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    content_type = content_type_map.get(ext, "image/jpeg")

    with open(image_path, "rb") as f:
        data = f.read()

    key = f"experiment/venue_test_{label}_{ts}{ext}"
    url = gcs.upload_image_bytes(data, key_name=key, content_type=content_type)
    print(f"  {label}: {image_path} ({len(data)//1024}KB) -> {url}")
    return url


# ====================================================================
# Step 1: Joint venue + influencer analysis (LLM)
# ====================================================================
def step1_analyze(processor, venue_url: str, influencer_url: str) -> dict | None:
    """Call the same LLM analysis that ugc.py uses before NB2."""
    import base64 as _b64
    from tvd_pipeline.pipelines.ugc import _compress_image_for_analysis

    print(f"\n{'='*60}")
    print("STEP 1: Joint venue + influencer LLM analysis")
    print(f"{'='*60}")

    defaults = get_pipeline_defaults()
    max_dim = defaults.get("max_image_dimension", 1024)
    loader = get_prompt_loader()

    system_prompt = loader.get("shared_venue_influencer_analysis_system")
    user_prompt_text = loader.get("shared_venue_influencer_analysis_user")

    user_parts = []
    for label, url in [("venue", venue_url), ("influencer", influencer_url)]:
        print(f"  Fetching {label} image: {url[:80]}...")
        try:
            img_resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "image/*,*/*;q=0.8",
            }, timeout=15)
            if img_resp.status_code != 200:
                print(f"  ERROR: HTTP {img_resp.status_code} for {label}")
                return None
            compressed, mime = _compress_image_for_analysis(img_resp.content, max_dim)
            b64 = _b64.b64encode(compressed).decode("utf-8")
            user_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            print(f"  {label}: {len(img_resp.content)//1024}KB -> {len(compressed)//1024}KB ({mime})")
        except Exception as e:
            print(f"  ERROR fetching {label}: {e}")
            return None

    if len(user_parts) < 2:
        print("  ERROR: Could not fetch both images")
        return None

    user_parts.append({"type": "text", "text": user_prompt_text})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_parts},
    ]

    response_schema = {
        "type": "object",
        "properties": {
            "venue_description": {"type": "string"},
            "influencer_description": {"type": "string"},
        },
        "required": ["venue_description", "influencer_description"],
    }

    print("  Calling LLM (analyze_venue_and_influencer)...")
    t0 = time.time()
    processor.reset_usage()
    raw = processor._call_llm("analyze_venue_and_influencer", messages, response_schema=response_schema)
    elapsed = time.time() - t0
    print(f"  LLM responded in {elapsed:.1f}s")

    if not raw:
        print("  ERROR: LLM returned empty response")
        return None

    # _call_llm returns {"text": "...", "input_tokens": ..., ...} — extract text
    try:
        if isinstance(raw, dict) and "text" in raw:
            text_content = raw["text"]
        else:
            text_content = raw
        if isinstance(text_content, str):
            # Strip markdown code fences if present (```json ... ```)
            cleaned = text_content.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3].strip()
            parsed = json.loads(cleaned)
        else:
            parsed = text_content
    except (json.JSONDecodeError, TypeError) as parse_err:
        print(f"  ERROR: Could not parse LLM response ({parse_err}): {str(raw)[:200]}")
        return None

    if "venue_description" not in parsed or "influencer_description" not in parsed:
        print(f"  ERROR: Missing keys in response: {list(parsed.keys())}")
        return None

    print(f"  venue_description:      {parsed['venue_description']}")
    print(f"  influencer_description: {parsed['influencer_description']}")
    return parsed


# ====================================================================
# Step 2: NB2 compositing (Nano Banana 2)
# ====================================================================
def step2_nb2_composite(
    kie_service: KieAIService,
    venue_url: str,
    influencer_url: str,
    analysis: dict | None,
    clip_description: str = "",
) -> str | None:
    """Call NB2 to composite the influencer into the venue image."""
    print(f"\n{'='*60}")
    print("STEP 2: NB2 compositing (influencer in venue)")
    print(f"{'='*60}")

    # Build prompt using the same templates as ugc.py
    loader = get_prompt_loader()
    if analysis:
        venue_prompt = loader.get(
            "shared_venue_nb2_composite",
            venue_description=analysis.get('venue_description', ''),
            influencer_description=analysis.get('influencer_description', ''),
            director_description=clip_description,
        )
    else:
        venue_prompt = loader.get(
            "shared_venue_nb2_composite_fallback",
            director_description=clip_description,
        )

    print(f"  Prompt: {venue_prompt[:120]}...")
    print(f"  Venue:      {venue_url[:80]}...")
    print(f"  Influencer: {influencer_url[:80]}...")

    t0 = time.time()
    result_url = kie_service.composite_influencer_in_venue(
        venue_image_url=venue_url,
        influencer_image_urls=[influencer_url],
        prompt=venue_prompt,
        resolution="1K",
    )
    elapsed = time.time() - t0

    if result_url:
        print(f"  NB2 composite generated in {elapsed:.1f}s")
        print(f"  Result: {result_url}")
    else:
        print(f"  ERROR: NB2 compositing failed after {elapsed:.1f}s")

    return result_url


# ====================================================================
# Step 3: Reference-to-video animation
# ====================================================================
def step3_ref_to_video(
    processor,
    composite_url: str,
    influencer_url: str,
    motion_prompt: str,
    duration: float = 4.0,
) -> str | None:
    """Animate the composite image using reference-to-video (Veo 3.1)."""
    print(f"\n{'='*60}")
    print("STEP 3: Reference-to-video animation")
    print(f"{'='*60}")

    # Read ref_to_video config (same as ugc.py line ~2863)
    ref_model_cfg = processor.model_config.get("media_models", {}).get("ref_to_video", {})
    ref_version = ref_model_cfg.get("selected", "")
    ref_provider = ref_model_cfg.get("provider", "google")
    version_cfg = ref_model_cfg.get("versions", {}).get(ref_version, {})
    ref_provider = version_cfg.get("provider", ref_provider)
    api_method = version_cfg.get("api_method")

    # Build ref-to-video prompt (same template as ugc.py)
    ref_prompt = get_prompt_loader().get(
        "shared_venue_ref_to_video",
        motion_prompt=motion_prompt,
    )

    print(f"  Model:    {ref_version} ({ref_provider})")
    print(f"  API:      {api_method}")
    print(f"  Duration: {duration}s")
    print(f"  Prompt:   {ref_prompt[:120]}...")
    print(f"  Images:   composite + influencer ref")

    t0 = time.time()
    try:
        video_url = processor._generate_video(
            video_model=ref_version,
            video_provider=ref_provider,
            image_url=None,  # not used for R2V
            reference_image_urls=[composite_url, influencer_url],
            motion_prompt=ref_prompt,
            duration=duration,
            resolution="720p",
            api_method=api_method,
        )
    except Exception as e:
        print(f"  ERROR: ref-to-video failed: {e}")
        return None

    elapsed = time.time() - t0

    if video_url:
        print(f"  Video generated in {elapsed:.0f}s")
        print(f"  Result: {video_url}")
    else:
        print(f"  ERROR: ref-to-video returned None after {elapsed:.0f}s")

    return video_url


# ====================================================================
# Main
# ====================================================================
def main():
    parser = argparse.ArgumentParser(description="Test INFLUENCER_IN_VENUE pipeline in isolation")
    parser.add_argument("--skip-analysis", action="store_true", help="Skip LLM analysis step")
    parser.add_argument("--skip-video", action="store_true", help="Skip ref-to-video step")
    parser.add_argument("--venue", default=None, help="Venue image URL (skips local upload)")
    parser.add_argument("--influencer", default=None, help="Influencer image URL (skips local upload)")
    parser.add_argument("--motion", default=DIRECTOR_MOTION_PROMPT, help="Director motion prompt")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = int(time.time())

    # --- Init processor (for _call_llm and _generate_video) ---
    print("Initializing VideoSceneProcessor...")
    from video_scene_processor import VideoSceneProcessor
    processor = VideoSceneProcessor()
    print("Services initialized.\n")

    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )

    # Upload local images to GCS (or use provided URLs)
    print("Uploading test images to GCS...")
    if args.venue:
        venue_url = args.venue
    else:
        venue_url = upload_local_image(gcs, VENUE_IMAGE_PATH, "venue", ts)
        if not venue_url:
            print("ERROR: Failed to upload venue image")
            return

    if args.influencer:
        influencer_url = args.influencer
    else:
        influencer_url = upload_local_image(gcs, INFLUENCER_IMAGE_PATH, "influencer", ts)
        if not influencer_url:
            print("ERROR: Failed to upload influencer image")
            return

    motion_prompt = args.motion

    print(f"\nVenue:      {venue_url}")
    print(f"Influencer: {influencer_url}")
    print(f"\nDirector clip (mock):")
    print(f"  description:   {MOCK_DIRECTOR_CLIP['description']}")
    print(f"  motion_prompt: {MOCK_DIRECTOR_CLIP['motion_prompt']}")
    print(f"  duration:      {MOCK_DIRECTOR_CLIP['duration']}s")

    results = {}

    # --- Step 1: LLM analysis ---
    analysis = None
    if not args.skip_analysis:
        analysis = step1_analyze(processor, venue_url, influencer_url)
        results["step1_analysis"] = analysis
        if not analysis:
            print("\n  WARNING: Analysis failed — continuing with generic NB2 prompt")
    else:
        print("\n  [Skipping Step 1: LLM analysis]")

    # --- Step 2: NB2 compositing ---
    composite_url = step2_nb2_composite(
        processor.kie_service,
        venue_url,
        influencer_url,
        analysis,
        clip_description=MOCK_DIRECTOR_CLIP.get("description", ""),
    )
    results["step2_nb2_composite"] = composite_url

    if not composite_url:
        print("\nSTOPPING: NB2 compositing failed — cannot proceed to ref-to-video")
        _save_results(results, ts)
        return

    # Download composite image
    try:
        img_resp = requests.get(composite_url, timeout=30)
        if img_resp.status_code == 200:
            img_path = os.path.join(OUTPUT_DIR, f"nb2_composite_{ts}.jpg")
            with open(img_path, "wb") as f:
                f.write(img_resp.content)
            print(f"  Saved composite: {img_path} ({len(img_resp.content)//1024}KB)")
    except Exception as e:
        print(f"  Could not save composite image: {e}")

    # --- Step 3: Ref-to-video ---
    if not args.skip_video:
        video_url = step3_ref_to_video(
            processor,
            composite_url,
            influencer_url,
            motion_prompt,
            duration=MOCK_DIRECTOR_CLIP.get("duration", 4.0),
        )
        results["step3_ref_to_video"] = video_url

        # Download video
        if video_url:
            try:
                vid_resp = requests.get(video_url, timeout=120)
                if vid_resp.status_code == 200:
                    vid_path = os.path.join(OUTPUT_DIR, f"ref_to_video_{ts}.mp4")
                    with open(vid_path, "wb") as f:
                        f.write(vid_resp.content)
                    print(f"  Saved video: {vid_path} ({len(vid_resp.content)//1024}KB)")
            except Exception as e:
                print(f"  Could not save video: {e}")
    else:
        print("\n  [Skipping Step 3: ref-to-video]")

    # --- Summary ---
    _save_results(results, ts)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for key, val in results.items():
        if val is None:
            status = "FAILED"
        elif isinstance(val, dict):
            status = "OK"
        elif isinstance(val, str) and val.startswith("http"):
            status = f"OK -> {val[:80]}"
        else:
            status = str(val)[:80]
        print(f"  {key}: {status}")
    print(f"\nOutput dir: {OUTPUT_DIR}")


def _save_results(results: dict, ts: int):
    """Persist results JSON."""
    # Convert non-serializable values
    safe = {}
    for k, v in results.items():
        if v is None or isinstance(v, (str, int, float, bool, dict, list)):
            safe[k] = v
        else:
            safe[k] = str(v)
    out = os.path.join(OUTPUT_DIR, f"results_{ts}.json")
    with open(out, "w") as f:
        json.dump(safe, f, indent=2)
    print(f"  Results saved: {out}")


if __name__ == "__main__":
    main()
