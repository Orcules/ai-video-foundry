"""Test: Veo 3 resizeMode comparison — landscape sushi image to 9:16 portrait.

Runs TWO generations with the same landscape image:
  A) resizeMode="pad"  (default) — expect black bars / letterboxing
  B) resizeMode="crop" — expect Veo to center-crop the image, no black bars

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_veo3_landscape_to_portrait
"""

import copy
import os
import sys
import time
import subprocess

script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.services.veo3 import Veo3Service, VeoPromptBlockedError, VeoRAIBlockedError
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.config import Config
from tvd_pipeline.data_loader import get_veo3_config

# Original landscape sushi image
IMAGE_PATH = os.path.normpath(os.path.join(
    script_dir, "..", "..", "..", "..",
    "api_pipeline", "documents", "test_scripts", "oishi_assets",
    "WhatsApp Image 2026-03-04 at 01.12.52 (1).jpeg",
))

MOTION_PROMPT = "Subtle slow zoom in on the sushi plate, very slight movement"
DURATION = 5
OUTPUT_DIR = os.path.join(script_dir, "test_output")


def get_video_resolution(video_url: str):
    """Get (width, height) of a video URL using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                video_url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        parts = result.stdout.strip().split(",")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception as e:
        print(f"  ffprobe error: {e}")
    return None, None


def download_video(url: str, local_path: str):
    """Download a video from URL to local file."""
    import requests
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    print(f"  Saved: {local_path}")


def run_test(label, svc, image_url, resize_mode=None):
    """Patch veo config, generate video, return (url, w, h, elapsed)."""
    veo_cfg = get_veo3_config()
    original_0 = copy.deepcopy(veo_cfg["veo3_0"])
    if resize_mode:
        veo_cfg["veo3_0"]["resizeMode"] = resize_mode
    print(f"  veo3_0 config: {veo_cfg['veo3_0']}")
    print(f"  veo3_1 config: {veo_cfg['veo3_1']}")
    print(f"  Generating video...")

    start = time.time()
    url = None
    try:
        url = svc.generate_video(
            prompt=MOTION_PROMPT,
            image_url=image_url,
            duration=DURATION,
            resolution="720p",
        )
    except (VeoPromptBlockedError, VeoRAIBlockedError) as e:
        print(f"  Safety blocked: {e}")
    elapsed = time.time() - start

    # Restore original config
    veo_cfg["veo3_0"] = original_0

    if url:
        w, h = get_video_resolution(url)
        if w and h:
            orient = "PORTRAIT" if h > w else "LANDSCAPE"
            print(f"  Output: {w}x{h} ({orient}, ~{w/h:.3f})")
        else:
            w, h = None, None
            print(f"  Could not read resolution")
        print(f"  URL:  {url}")
        print(f"  Time: {elapsed:.1f}s")
        return url, w, h, elapsed
    else:
        print(f"  FAILED or BLOCKED ({elapsed:.1f}s)")
        return None, None, None, elapsed


def main():
    from PIL import Image
    import io

    if not os.path.exists(IMAGE_PATH):
        print(f"ERROR: Image not found: {IMAGE_PATH}")
        sys.exit(1)

    with open(IMAGE_PATH, "rb") as f:
        image_bytes = f.read()

    img = Image.open(io.BytesIO(image_bytes))
    print(f"Input image: {img.width}x{img.height} ({'LANDSCAPE' if img.width > img.height else 'PORTRAIT'})")
    print(f"Image path:  {IMAGE_PATH}")
    print()

    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )

    svc = Veo3Service(gcs_storage_service=gcs, model=config.VEO3_FAST_MODEL)
    print(f"Model: {config.VEO3_FAST_MODEL}")
    if not svc.initialized:
        print("ERROR: Veo 3.0 Fast service not initialized")
        sys.exit(1)

    ts = int(time.time())
    image_url = gcs.upload_image_bytes(image_bytes, f"test_resize_sushi_{ts}.jpg")
    print(f"Uploaded image: {image_url}")
    print()

    # --- Test A: resizeMode="pad" (default — expect black bars) ---
    print("=" * 60)
    print('TEST A: resizeMode="pad" (default — expect black bars)')
    print("=" * 60)
    url_a, wa, ha, ta = run_test("pad", svc, image_url, "pad")
    print()

    # --- Test B: resizeMode="crop" (expect Veo to crop, no black bars) ---
    print("=" * 60)
    print('TEST B: resizeMode="crop" (expect Veo to crop, no bars)')
    print("=" * 60)
    url_b, wb, hb, tb = run_test("crop", svc, image_url, "crop")
    print()

    # Download both
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if url_a:
        download_video(url_a, os.path.join(OUTPUT_DIR, "veo3_resizeMode_pad.mp4"))
    if url_b:
        download_video(url_b, os.path.join(OUTPUT_DIR, "veo3_resizeMode_crop.mp4"))

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Input:         {img.width}x{img.height} (landscape)")
    print(f"  Target:        9:16 portrait")
    print(f"  Test A (pad):  {wa}x{ha}  {ta:.0f}s")
    print(f"  Test B (crop): {wb}x{hb}  {tb:.0f}s")
    print()
    print("Compare the two videos — crop should have no black bars.")


if __name__ == "__main__":
    main()
