"""Test Veo 3.0 Fast image-to-video with pre-crop + aspectRatio fix.

For each landscape test image, generates video with:
  1. aspectRatio="9:16" ONLY (no pre-crop — shows letterboxing/black bars)
  2. aspectRatio="9:16" + center-crop to 9:16 (proposed full fix)

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_veo30_aspect_ratio
"""

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

ASSETS_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "..", "api_pipeline", "documents", "test_scripts", "flymore_assets",
))

TEST_IMAGES = ["legoland_1.jpg", "legoland_2.jpg"]
MOTION_PROMPT = "Subtle slow zoom in, very slight movement"
DURATION = 5
TARGET_AR = 9 / 16  # 0.5625 — portrait


def center_crop_to_portrait(image_bytes: bytes) -> bytes:
    """Center-crop a landscape image to 9:16 portrait. Returns JPEG bytes."""
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size

    if w <= h:
        # Already portrait or square — no crop needed
        return image_bytes

    # Target width for 9:16 given this height
    target_w = int(h * TARGET_AR)
    if target_w >= w:
        return image_bytes

    left = (w - target_w) // 2
    img = img.crop((left, 0, left + target_w, h))
    print(f"  Cropped: {w}x{h} -> {img.size[0]}x{img.size[1]} (center-crop to 9:16)")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def get_video_resolution(video_url: str) -> tuple:
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


def run_test(label, gcs, svc, image_url, prompt, duration):
    """Generate a video and return (url, width, height, elapsed)."""
    print(f"  Generating video...")
    start = time.time()
    url = None
    try:
        url = svc.generate_video(
            prompt=prompt,
            image_url=image_url,
            duration=duration,
            resolution="720p",
        )
    except (VeoPromptBlockedError, VeoRAIBlockedError) as e:
        print(f"  Safety blocked: {e}")
    elapsed = time.time() - start

    if url:
        w, h = get_video_resolution(url)
        orient = "PORTRAIT" if h and h > w else "LANDSCAPE"
        print(f"  Result:     {url}")
        print(f"  Resolution: {w}x{h} ({orient})")
        print(f"  Time:       {elapsed:.1f}s")
        return url, w, h, elapsed
    else:
        print(f"  FAILED or BLOCKED ({elapsed:.1f}s)")
        return None, None, None, elapsed


def main():
    from PIL import Image
    import io

    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )

    # Patch veo config with aspectRatio for all tests
    veo_cfg = get_veo3_config()
    veo_cfg["veo3_0"]["aspectRatio"] = "9:16"
    print(f"veo3_0 config: {veo_cfg['veo3_0']}")
    print()

    svc = Veo3Service(gcs_storage_service=gcs, model=config.VEO3_FAST_MODEL)
    if not svc.initialized:
        print("ERROR: Veo 3.0 Fast service not initialized")
        sys.exit(1)

    results = []

    for image_name in TEST_IMAGES:
        image_path = os.path.join(ASSETS_DIR, image_name)
        if not os.path.exists(image_path):
            print(f"SKIP: {image_name} not found")
            continue

        with open(image_path, "rb") as f:
            original_bytes = f.read()

        img = Image.open(io.BytesIO(original_bytes))
        print(f"{'=' * 60}")
        print(f"IMAGE: {image_name} ({img.width}x{img.height})")
        print(f"{'=' * 60}")
        print()

        # --- Test A: aspectRatio only (no pre-crop) — expect letterboxing ---
        print(f"  TEST A: aspectRatio='9:16' only (no crop)")
        ts = int(time.time())
        url_a = gcs.upload_image_bytes(original_bytes, f"test_ar_{image_name}_{ts}.jpg")
        url_a_vid, wa, ha, ta = run_test("AR only", gcs, svc, url_a, MOTION_PROMPT, DURATION)
        print()

        # --- Test B: aspectRatio + center-crop to 9:16 ---
        print(f"  TEST B: aspectRatio='9:16' + center-crop to 9:16")
        cropped_bytes = center_crop_to_portrait(original_bytes)
        crop_img = Image.open(io.BytesIO(cropped_bytes))
        print(f"  Cropped size: {crop_img.width}x{crop_img.height}")
        url_b = gcs.upload_image_bytes(cropped_bytes, f"test_crop_{image_name}_{ts}.jpg")
        url_b_vid, wb, hb, tb = run_test("AR+crop", gcs, svc, url_b, MOTION_PROMPT, DURATION)
        print()

        results.append({
            "image": image_name,
            "original": f"{img.width}x{img.height}",
            "cropped": f"{crop_img.width}x{crop_img.height}",
            "test_a": {"url": url_a_vid, "res": f"{wa}x{ha}", "time": ta},
            "test_b": {"url": url_b_vid, "res": f"{wb}x{hb}", "time": tb},
        })

    # --- Summary ---
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"\n  {r['image']} (original {r['original']}, cropped {r['cropped']})")
        print(f"    Test A (AR only):    {r['test_a']['res']}  {r['test_a']['url'] or 'FAILED'}")
        print(f"    Test B (AR + crop):  {r['test_b']['res']}  {r['test_b']['url'] or 'FAILED'}")

    print()
    print("Compare Test A vs Test B videos — Test B should have no black bars.")


if __name__ == "__main__":
    main()
