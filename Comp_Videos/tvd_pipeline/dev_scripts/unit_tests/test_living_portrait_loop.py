"""Test: Living portrait loop — same image as first & last frame → seamless loop.

Uses Veo 3.1 first-last-frame-to-video with the same image for both frames,
guaranteeing a perfect seamless loop.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_living_portrait_loop
"""

import os
import sys
import time

import requests

script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService

# ---------------------------------------------------------------------------
# Source image (front-facing selfie)
# ---------------------------------------------------------------------------
IMAGE_DIR = os.path.join(script_dir, "test_output", "living_portrait")
SELFIE = os.path.join(IMAGE_DIR, "face4.jpg")

OUTPUT_DIR = IMAGE_DIR

# ---------------------------------------------------------------------------
# fal.ai Veo 3.1 first-last-frame config
# ---------------------------------------------------------------------------
FAL_FLF_ENDPOINT = "https://queue.fal.run/fal-ai/veo3.1/first-last-frame-to-video"

VEO_PROMPT = (
    "Animate subtle life into this portrait. "
    "Do not make the person speak — mouth stays closed. "
    "The person naturally alternates between a neutral resting expression and a very small closed-mouth smile — no teeth showing. "
    "Most of the time the face is relaxed and neutral, with brief moments of a gentle smile. "
    "Allow gentle natural micro-movements: a slight head tilt, small weight shift, or subtle eye movement. "
    "No large movements, no camera movement."
)


def veo_first_last_frame(fal_key: str, image_url: str) -> str | None:
    """Same image as first & last frame → guaranteed seamless loop."""
    headers = {
        "Authorization": f"Key {fal_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": VEO_PROMPT,
        "first_frame_url": image_url,
        "last_frame_url": image_url,
        "duration": "6s",
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "generate_audio": False,
        "safety_tolerance": "5",
    }

    print(f"\nSubmitting to Veo 3.1 first-last-frame-to-video...")
    print(f"  Same image for first & last frame (seamless loop)")

    resp = requests.post(FAL_FLF_ENDPOINT, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        print(f"  Submit FAILED ({resp.status_code}): {resp.text[:500]}")
        return None
    data = resp.json()
    request_id = data.get("request_id", "unknown")
    status_url = data.get("status_url")
    response_url = data.get("response_url")
    print(f"  Submitted — request_id: {request_id}")

    if not status_url or not response_url:
        print(f"  ERROR: missing status_url/response_url")
        return None

    start = time.time()
    while time.time() - start < 300:
        time.sleep(3)
        try:
            status_resp = requests.get(status_url, headers=headers, timeout=15)
            status_resp.raise_for_status()
            status = status_resp.json()
            state = status.get("status")
            elapsed = int(time.time() - start)
            if state == "COMPLETED":
                print(f"  Completed in {elapsed}s")
                break
            elif state in ("FAILED", "CANCELLED"):
                print(f"  {state}: {status}")
                return None
            print(f"  ... {state} ({elapsed}s)")
        except Exception as e:
            print(f"  Poll error: {e}")
    else:
        print(f"  Timed out after 300s")
        return None

    result_resp = requests.get(response_url, headers=headers, timeout=30)
    if result_resp.status_code != 200:
        print(f"  Result fetch FAILED ({result_resp.status_code})")
        return None
    result = result_resp.json()
    video_url = result.get("video", {}).get("url")
    if not video_url:
        print(f"  ERROR: no video URL in result: {list(result.keys())}")
    return video_url


def download_file(url: str, local_path: str):
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    print(f"  Saved: {local_path} ({len(resp.content) / 1024:.0f} KB)")


def main():
    fal_key = os.environ.get("FAL_KEY")
    if not fal_key:
        print("ERROR: FAL_KEY not set"); sys.exit(1)

    if not os.path.isfile(SELFIE):
        print(f"ERROR: selfie not found: {SELFIE}"); sys.exit(1)
    print(f"  Source: {SELFIE} ({os.path.getsize(SELFIE) / 1024:.0f} KB)")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = int(time.time())

    # Upload to GCS
    print("\nUploading selfie to GCS...")
    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )
    with open(SELFIE, "rb") as f:
        img_bytes = f.read()
    image_url = gcs.upload_image_bytes(img_bytes, f"experiment/portrait_flf_{ts}.jpg")
    if not image_url:
        print("ERROR: GCS upload failed"); sys.exit(1)
    print(f"  URL: {image_url}")

    # Veo 3.1 first-last-frame
    print(f"\n{'='*60}")
    print("Veo 3.1 — first/last frame seamless loop")
    print(f"{'='*60}")

    video_url = veo_first_last_frame(fal_key, image_url)

    if video_url:
        out_path = os.path.join(OUTPUT_DIR, f"living_loop_{ts}.mp4")
        download_file(video_url, out_path)
        print(f"\n{'='*60}")
        print("RESULT")
        print(f"{'='*60}")
        print(f"  Video: {out_path}")
        print(f"  URL:   {video_url}")
        print(f"  Cost:  ~$1.20 (Veo 6s, no audio)")
    else:
        print("\nFAILED — no video returned")


if __name__ == "__main__":
    main()
