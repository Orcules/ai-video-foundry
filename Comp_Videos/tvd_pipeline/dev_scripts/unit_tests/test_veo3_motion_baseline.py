"""Animate the origami butterfly image with the hardcoded fallback motion prompt.

This is the exact prompt that ugc.py:2034 sends for ALL reference images today:
    "Subtle slow zoom in, very slight movement"

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.unit_tests.test_veo3_motion_baseline
"""

import os
import sys
import time

# Ensure Comp_Videos is on the path
script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.veo3 import Veo3Service

MOTION_PROMPT = (
    "The stuffed cat in the Santa hat slowly turns its head toward the sushi, "
    "its paw rising from its lap and reaching for a piece of salmon roll. "
    "The green avocado plushie tilts slightly, its embroidered smile seeming "
    "to widen as it hugs its red heart tighter. A pair of chopsticks on the "
    "wooden bowl shift slightly as if nudged by an invisible hand."
)

# Same model used in the Oishi job (720p_low tier → veo-3.0-fast → REST API model ID)
VEO_MODEL = "veo-3.0-fast-generate-001"


def main():
    image_path = os.path.join(comp_videos_dir, "temp", "14.PNG")
    if not os.path.exists(image_path):
        print(f"ERROR: Image not found: {image_path}")
        sys.exit(1)

    file_size_kb = os.path.getsize(image_path) / 1024
    print(f"Image: {image_path}")
    print(f"Size: {file_size_kb:.0f} KB")

    # --- Init services ---
    config = Config()
    print(f"\nInitializing GCS storage...")
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name="automatiq",
        folder_path="Comp/Final_Video",
    )

    print(f"Initializing Veo 3.0...")
    veo = Veo3Service(gcs_storage_service=gcs, model=VEO_MODEL)
    if not veo.initialized:
        print("ERROR: Veo service failed to initialize.")
        sys.exit(1)
    print(f"Veo ready (model: {veo.model})")

    # --- Upload image to GCS to get a URL ---
    print(f"\nUploading image to GCS...")
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    image_key = f"temp_analysis/test_plush_{int(time.time())}.png"
    image_url = gcs.upload_video_bytes(
        video_data=image_bytes,
        key_name=image_key,
        make_public=True,
    )
    if not image_url:
        print("ERROR: Failed to upload image to GCS")
        sys.exit(1)
    print(f"Image URL: {image_url}")

    # --- Generate video ---
    print(f"\nMotion prompt: \"{MOTION_PROMPT}\"")
    print(f"Starting Veo 3.0 image-to-video generation...")
    t0 = time.time()

    video_url = veo.generate_video_from_image(
        image_url=image_url,
        motion_prompt=MOTION_PROMPT,
        duration=4.0,
    )

    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.0f}s")

    if video_url:
        print(f"\n{'='*60}")
        print(f"VIDEO URL: {video_url}")
        print(f"{'='*60}")
    else:
        print("\nERROR: Veo returned no video.")

    # --- Cleanup temp image from GCS ---
    try:
        blob_name = image_key
        gcs._bucket.blob(blob_name).delete()
        print(f"\nCleaned up temp image from GCS.")
    except Exception:
        pass

    print("Done.")


if __name__ == "__main__":
    main()
