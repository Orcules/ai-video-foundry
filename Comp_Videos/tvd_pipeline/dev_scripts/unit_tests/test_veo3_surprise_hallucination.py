"""Test whether Veo 3.0-fast hallucinate new characters when animating surprise clips.

Background:
  Job 30275caa had a surprise clip (poke bowl with cartoon cat food pick) where Veo
  replaced the original brown cat with a large white kawaii cat. The previous run
  (job f3b21749) used a different crop of the same image and preserved the cat design.

This test sends the EXACT same image + motion prompt from the failing job to Veo
3.0-fast and downloads the result for manual inspection.

We also test the PREVIOUS run's image (tighter crop, cat is larger) to compare.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_veo3_surprise_hallucination
"""

import os
import sys
import time
import subprocess

# Ensure Comp_Videos is on the path
script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.veo3 import Veo3Service

# === Test data from the two jobs ===

# Current run (hallucinated white cat) - wider crop, cat is small
CURR_IMAGE_URL = "https://storage.googleapis.com/automatiq/Comp/Final_Video/crop_by_generation/208a18ad464a_img4.jpg"
CURR_MOTION_PROMPT = (
    "The small cartoon cat on the toothpick wiggles its ears and gives a cheerful "
    "wink to the camera. It then raises its tiny paw to wave while the surrounding "
    "salmon and avocado remain perfectly still. The camera slowly zooms in on the "
    "character's expressive face."
)

# Previous run (preserved cat design) - tighter crop, cat is larger
PREV_IMAGE_URL = "https://storage.googleapis.com/automatiq/Comp/Final_Video/crop_by_generation/44f62182d09b_img4.jpg"
PREV_MOTION_PROMPT = (
    "The illustrated cat character on the topper becomes animated, its eyes winking "
    "playfully and its tail twitching behind the jar. It moves its small paws in a "
    "digging motion inside the orange jar while the white sprouts beneath it sway "
    "slightly. The camera performs a very tight, slow zoom on the character's "
    "expressive face to emphasize the magical movement."
)

VEO_MODEL = "veo-3.0-fast-generate-001"
DURATION = 4.0

OUTPUT_DIR = os.path.join(script_dir, "test_output", "surprise_hallucination")


def extract_frames(video_path, output_dir, prefix):
    """Extract 4 evenly-spaced frames from a video."""
    os.makedirs(output_dir, exist_ok=True)
    frame_path = os.path.join(output_dir, f"{prefix}_f%02d.jpg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-vf", "select=eq(n\\,0)+eq(n\\,24)+eq(n\\,48)+eq(n\\,72)",
            "-vsync", "vfr",
            frame_path,
        ],
        capture_output=True,
    )
    frames = sorted(
        f for f in os.listdir(output_dir) if f.startswith(prefix) and f.endswith(".jpg")
    )
    return [os.path.join(output_dir, f) for f in frames]


def run_single_test(veo, label, image_url, motion_prompt, output_dir):
    """Generate one video and extract frames."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")
    print(f"Image: {image_url}")
    print(f"Motion: {motion_prompt[:80]}...")
    print(f"Model: {VEO_MODEL}")
    print(f"Duration: {DURATION}s")

    t0 = time.time()
    video_url = veo.generate_video_from_image(
        image_url=image_url,
        motion_prompt=motion_prompt,
        duration=DURATION,
    )
    elapsed = time.time() - t0

    if not video_url:
        print(f"\nERROR: Veo returned no video for {label}")
        return None

    print(f"Completed in {elapsed:.0f}s")
    print(f"Video URL: {video_url}")

    # Download video
    video_path = os.path.join(output_dir, f"{label}.mp4")
    import urllib.request
    urllib.request.urlretrieve(video_url, video_path)
    print(f"Downloaded to: {video_path}")

    # Extract frames
    frames = extract_frames(video_path, output_dir, label)
    print(f"Extracted {len(frames)} frames")
    for f in frames:
        print(f"  {f}")

    return video_url


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Output dir: {OUTPUT_DIR}")

    # Init services
    config = Config()
    print("Initializing GCS storage...")
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name="automatiq",
        folder_path="Comp/Final_Video",
    )

    print(f"Initializing Veo ({VEO_MODEL})...")
    veo = Veo3Service(gcs_storage_service=gcs, model=VEO_MODEL)
    if not veo.initialized:
        print("ERROR: Veo service failed to initialize.")
        sys.exit(1)
    print(f"Veo ready (model: {veo.model})")

    # Test 1: Current run's image (wider crop, small cat) - the one that hallucinated
    run_single_test(
        veo,
        label="curr_crop_curr_prompt",
        image_url=CURR_IMAGE_URL,
        motion_prompt=CURR_MOTION_PROMPT,
        output_dir=OUTPUT_DIR,
    )

    # Test 2: Previous run's image (tighter crop, larger cat) - the one that was fine
    run_single_test(
        veo,
        label="prev_crop_prev_prompt",
        image_url=PREV_IMAGE_URL,
        motion_prompt=PREV_MOTION_PROMPT,
        output_dir=OUTPUT_DIR,
    )

    # Test 3: Current image with previous prompt (to isolate crop vs prompt effect)
    run_single_test(
        veo,
        label="curr_crop_prev_prompt",
        image_url=CURR_IMAGE_URL,
        motion_prompt=PREV_MOTION_PROMPT,
        output_dir=OUTPUT_DIR,
    )

    print(f"\n{'='*60}")
    print("ALL TESTS COMPLETE")
    print(f"Check frames in: {OUTPUT_DIR}")
    print(f"{'='*60}")
    print("\nCompare:")
    print("  curr_crop_curr_prompt  = same inputs as the hallucinated job")
    print("  prev_crop_prev_prompt  = same inputs as the good job")
    print("  curr_crop_prev_prompt  = isolates whether the crop or prompt matters")


if __name__ == "__main__":
    main()
