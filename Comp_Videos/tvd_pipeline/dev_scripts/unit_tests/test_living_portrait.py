"""Test: Living portrait — 3 original selfies → Veo 3.1 living avatar.

Uploads original selfies to GCS, sends to Veo 3.1 reference-to-video with minimal prompt.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_living_portrait
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
# Original selfies
# ---------------------------------------------------------------------------
IMAGE_DIR = os.path.join(script_dir, "test_output", "living_portrait")
FACE_1 = os.path.join(IMAGE_DIR, "face1.jpg")
FACE_2 = os.path.join(IMAGE_DIR, "face2.jpg")
FACE_3 = os.path.join(IMAGE_DIR, "face3.jpg")

OUTPUT_DIR = os.path.join(script_dir, "test_output", "living_portrait")

# ---------------------------------------------------------------------------
# fal.ai Veo 3.1 config
# ---------------------------------------------------------------------------
FAL_REF_TO_VIDEO = "https://queue.fal.run/fal-ai/veo3.1/reference-to-video"

VEO_PROMPT = (
    "Do not make the person speak — mouth stays closed. "
    "Only subtle micro-movements: soft breathing and an occasional very small, barely noticeable closed-mouth smile — no teeth showing. "
    "The person stays still — no head turns, no camera movement. "
    "CRITICAL: This video must be a perfect seamless loop. The very last frame must be "
    "identical to the very first frame so the video can repeat infinitely with no visible "
    "cut or jump. All motion must naturally return to the exact starting position and "
    "expression by the end of the clip."
)


# ---------------------------------------------------------------------------
# fal.ai Veo 3.1 reference-to-video
# ---------------------------------------------------------------------------
def veo_living_portrait(fal_key: str, image_urls: list) -> str | None:
    """Send bg-removed portraits to Veo 3.1, get living avatar video."""
    headers = {
        "Authorization": f"Key {fal_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": VEO_PROMPT,
        "image_urls": image_urls,
        "duration": "8s",
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "generate_audio": False,
        "safety_tolerance": "5",
    }

    print(f"\nSubmitting to Veo 3.1 reference-to-video...")
    print(f"  Images: {len(image_urls)}")

    resp = requests.post(FAL_REF_TO_VIDEO, headers=headers, json=payload, timeout=30)
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

    faces = [
        (FACE_1, "face1"),
        (FACE_2, "face2"),
        (FACE_3, "face3"),
    ]
    for path, label in faces:
        if not os.path.isfile(path):
            print(f"ERROR: {label} not found: {path}")
            sys.exit(1)
        print(f"  {label}: {os.path.getsize(path) / 1024:.0f} KB")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = int(time.time())

    # Upload original selfies to GCS (Veo needs URLs)
    print("\nUploading 3 selfies to GCS...")
    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )

    image_urls = []
    for i, (path, label) in enumerate(faces, 1):
        with open(path, "rb") as f:
            img_bytes = f.read()
        url = gcs.upload_image_bytes(img_bytes, f"experiment/portrait_orig_{i}_{ts}.jpg")
        if not url:
            print(f"ERROR: GCS upload failed for {label}"); sys.exit(1)
        print(f"  {label}: {url}")
        image_urls.append(url)

    # ===================================================================
    # Veo 3.1 — animate into living portrait
    # ===================================================================
    print(f"\n{'='*60}")
    print("Veo 3.1 — living avatar animation (original selfies)")
    print(f"{'='*60}")

    video_url = veo_living_portrait(fal_key, image_urls)

    if video_url:
        out_path = os.path.join(OUTPUT_DIR, f"living_portrait_{ts}.mp4")
        download_file(video_url, out_path)
        print(f"\n{'='*60}")
        print("RESULT")
        print(f"{'='*60}")
        print(f"  Video: {out_path}")
        print(f"  URL:   {video_url}")
        print(f"  Cost:  ~$1.60 (Veo 8s, no audio)")
    else:
        print("\nFAILED — no video returned")


if __name__ == "__main__":
    main()
