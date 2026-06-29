"""Test: Send landscape image directly to Veo 3 (no crop) requesting 9:16 vertical.

Sends the Legoland hotel landscape image to Veo 3.0 Fast with a prompt
describing a left-to-right camera pan, requesting full vertical 9:16 output.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_veo3_landscape_direct
"""

import os
import sys
import time
import subprocess

script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.services.veo3 import Veo3Service
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.config import Config

IMAGE_PATH = os.path.join(
    script_dir, "test_output", "smart_crop_safety",
    "flymore_legoland_3_original.jpg",
)

PROMPT = (
    "A photorealistic, wide-angle cinematic video of the Legoland Hotel entrance. "
    "The scene is bathed in clear daylight with a subtle lens flare under a blue sky. "
    "The video begins with a focus on the leftmost multicolored pillars and first set "
    "of windows, with the green dragon statue completely off-camera to the right. Then, "
    "the camera begins a slow, deliberate, strict horizontal side-scrolling pan towards "
    "the right. As the camera travels right, it passes the main entrance doors, revealing "
    "more of the massive colorful pillars. The green dragon statue slowly comes into full "
    "view from the right side of the frame and moves into the center, perfectly perched on "
    "the main yellow and red canopy. The final framing is a wider, balanced shot capturing "
    "the complete entrance facade, including both sides of the canopy and more of the "
    "building structure to the right. This is a pure horizontal camera move (like a dolly "
    "shot) with no zooming."
)

DURATION = 4
OUTPUT_DIR = os.path.join(script_dir, "test_output")


def get_video_resolution(video_url: str):
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
    import requests
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    print(f"  Saved: {local_path}")


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
    print(f"Prompt:      {PROMPT}")
    print(f"Duration:    {DURATION}s")
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
        print("ERROR: Veo 3 service not initialized")
        sys.exit(1)

    ts = int(time.time())
    image_url = gcs.upload_image_bytes(image_bytes, f"test_legoland_direct_{ts}.jpg")
    print(f"Uploaded image: {image_url}")
    print()

    print("=" * 60)
    print("Generating video: landscape image -> 9:16 vertical, no crop")
    print("=" * 60)

    start = time.time()
    url = None
    try:
        url = svc.generate_video(
            prompt=PROMPT,
            image_url=image_url,
            duration=DURATION,
            resolution="720p",
        )
    except Exception as e:
        print(f"  Error: {e}")

    elapsed = time.time() - start

    if url:
        w, h = get_video_resolution(url)
        if w and h:
            orient = "PORTRAIT" if h > w else "LANDSCAPE"
            print(f"  Output: {w}x{h} ({orient}, AR={w/h:.3f})")
        else:
            w, h = None, None
            print(f"  Could not read resolution")
        print(f"  URL:  {url}")
        print(f"  Time: {elapsed:.1f}s")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_DIR, f"veo3_legoland_dolly_pan_{ts}.mp4")
        download_video(url, out_path)

        print()
        print("=" * 60)
        print("RESULT")
        print("=" * 60)
        print(f"  Input:  {img.width}x{img.height} (landscape)")
        print(f"  Output: {w}x{h}")
        print(f"  Video:  {out_path}")
    else:
        print(f"  FAILED ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
