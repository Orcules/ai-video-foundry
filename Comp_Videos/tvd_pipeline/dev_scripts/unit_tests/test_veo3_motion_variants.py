"""Compare regular vs surprise motion prompts on the origami butterfly image.

Runs 2 Veo 3.0-fast generations in parallel with the same image but different prompts:
  1. REGULAR  — thoughtful camera motion (what the Motion Writer would produce)
  2. SURPRISE — the butterfly comes alive (what the Motion Writer would produce)

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.unit_tests.test_veo3_motion_variants
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure Comp_Videos is on the path
script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.veo3 import Veo3Service

# Same model + resolution as the Oishi job (720p_low tier)
VEO_MODEL = "veo-3.0-fast-generate-001"

PROMPTS = {
    "REGULAR": (
        "Camera gently tilts down across the origami instruction page, "
        "soft focus shifting between the numbered folding steps. "
        "The person's thumb subtly adjusts its grip on the booklet. "
        "Warm ambient light catches the glossy pink butterfly illustration "
        "as a faint shadow drifts across the paper."
    ),
    "SURPRISE": (
        "The pink paper butterfly illustration on the page slowly peels off "
        "the paper surface, its wings beginning to flutter with delicate, "
        "lifelike movements. It lifts upward from the booklet, hovering just "
        "above the page as the person's fingers loosen in surprise. "
        "Tiny paper creases are visible on the wings as they beat gently."
    ),
}


def generate(veo, image_url, label, prompt):
    """Generate one video, return (label, url, elapsed)."""
    t0 = time.time()
    url = veo.generate_video_from_image(
        image_url=image_url,
        motion_prompt=prompt,
        duration=4.0,
    )
    return label, url, time.time() - t0


def main():
    image_path = os.path.join(comp_videos_dir, "temp", "15.jpg")
    if not os.path.exists(image_path):
        print(f"ERROR: Image not found: {image_path}")
        sys.exit(1)

    print(f"Image: {image_path}")
    print(f"Size: {os.path.getsize(image_path) / 1024:.0f} KB")

    # --- Init services ---
    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name="automatiq",
        folder_path="Comp/Final_Video",
    )
    veo = Veo3Service(gcs_storage_service=gcs, model=VEO_MODEL)
    if not veo.initialized:
        print("ERROR: Veo service failed to initialize.")
        sys.exit(1)
    print(f"Veo ready ({veo.model})")

    # --- Upload image once ---
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_key = f"temp_analysis/test_origami_{int(time.time())}.jpg"
    image_url = gcs.upload_video_bytes(
        video_data=image_bytes, key_name=image_key, make_public=True,
    )
    if not image_url:
        print("ERROR: Failed to upload image to GCS")
        sys.exit(1)
    print(f"Image URL: {image_url}\n")

    # --- Run both in parallel ---
    print("Generating 2 videos in parallel...\n")
    for label, prompt in PROMPTS.items():
        print(f"  {label}: {prompt[:80]}...")

    print()
    t0 = time.time()

    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(generate, veo, image_url, label, prompt): label
            for label, prompt in PROMPTS.items()
        }
        for future in as_completed(futures):
            label, url, elapsed = future.result()
            results[label] = url
            status = url[:80] + "..." if url else "FAILED"
            print(f"  {label} done in {elapsed:.0f}s — {status}")

    total = time.time() - t0
    print(f"\nTotal wall time: {total:.0f}s")

    # --- Results ---
    print(f"\n{'='*60}")
    print("RESULTS:")
    print(f"{'='*60}")
    for label in ["REGULAR", "SURPRISE"]:
        url = results.get(label)
        print(f"  {label}: {url or 'FAILED'}")
    print(f"{'='*60}")

    # --- Cleanup ---
    try:
        gcs._bucket.blob(image_key).delete()
    except Exception:
        pass
    print("\nDone.")


if __name__ == "__main__":
    main()
