"""Test Runway Gen 4.5 image-to-video with a reference image.

Tests with the wide image (2.39:1 ratio) that previously failed on both
Veo (safety block) and Runway (aspect ratio rejection). The _prepare_image
method should auto-crop it before sending.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_runway45
"""

import os
import sys
import time

script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.services.runway_direct import RunwayDirectService
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.config import Config

# Wide image (2.39:1) — triggers Veo safety block AND Runway aspect ratio rejection
# _prepare_image() should auto-crop it to 2:1 before sending
IMAGE_LOCAL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "..", "api_pipeline", "documents", "test_scripts",
    "flymore_assets", "legoland_dubai_5.jpg",
)
MOTION_PROMPT = "Subtle slow zoom in, very slight movement"
DURATION = 5


def main():
    config = Config()

    runway_key = os.environ.get("RUNWAYML_API_SECRET", "")
    if not runway_key:
        print("ERROR: RUNWAYML_API_SECRET not set in environment")
        sys.exit(1)

    image_path = os.path.normpath(IMAGE_LOCAL)
    if not os.path.exists(image_path):
        print(f"ERROR: Image not found: {image_path}")
        sys.exit(1)

    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )
    service = RunwayDirectService(api_key=runway_key, gcs_storage_service=gcs)

    # Upload image to GCS so Runway can fetch it
    print(f"Uploading {os.path.basename(image_path)} to GCS...")
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_url = gcs.upload_image_bytes(image_bytes, f"test_runway45_{int(time.time())}.jpg")
    if not image_url:
        print("ERROR: GCS upload failed")
        sys.exit(1)

    from PIL import Image
    import io
    img = Image.open(io.BytesIO(image_bytes))
    ratio = img.width / img.height
    print(f"Image:    {image_url}")
    print(f"Size:     {img.width}x{img.height} (ratio {ratio:.2f}:1)")
    print(f"Prompt:   {MOTION_PROMPT}")
    print(f"Duration: {DURATION}s")
    print(f"Model:    gen4.5")
    if ratio > 2.0:
        print(f"NOTE:     Ratio {ratio:.2f} > 2.0 — _prepare_image will auto-crop")
    print()

    start = time.time()
    print("Generating video with Runway Gen 4.5...")
    result = service.generate_video(
        image_url=image_url,
        prompt=MOTION_PROMPT,
        duration=DURATION,
        model="gen4.5",
        resolution=720,
    )
    elapsed = time.time() - start

    if result:
        print(f"\nSUCCESS ({elapsed:.1f}s)")
        print(f"Video URL: {result}")
    else:
        print(f"\nFAILED ({elapsed:.1f}s)")
        sys.exit(1)


if __name__ == "__main__":
    main()
