"""Smart crop: Gemini 3 Flash guided landscape-to-portrait image cropping.

Uses Gemini 3 Flash to identify the focal point of a landscape image, then
crops to 9:16 portrait preserving the most important content. Compares against
a naive center-crop for A/B visual inspection.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_smart_crop

    # Also generate Veo videos from cropped images (costs ~$0.80/video):
    python -m tvd_pipeline.dev_scripts.unit_tests.test_smart_crop --veo
"""

import argparse
import base64
import io
import json
import os
import sys
import time

from PIL import Image

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.providers.vertex import VertexAIProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ASSETS_DIR = os.path.normpath(os.path.join(
    script_dir, "..", "..", "..", "..", "api_pipeline", "documents",
    "test_scripts", "flymore_assets",
))

OUTPUT_DIR = os.path.join(script_dir, "test_output", "smart_crop")

TARGET_AR = 9 / 16  # 0.5625 — portrait

GEMINI_MODEL = "gemini-3-flash-preview"

SMART_CROP_SCHEMA = {
    "type": "object",
    "properties": {
        "focus_x": {
            "type": "number",
            "description": (
                "Horizontal center of the most important region "
                "(0.0=left edge, 1.0=right edge)"
            ),
        },
        "focus_y": {
            "type": "number",
            "description": (
                "Vertical center of the most important region "
                "(0.0=top edge, 1.0=bottom edge)"
            ),
        },
        "description": {
            "type": "string",
            "description": "Brief description of what the important region contains",
        },
    },
    "required": ["focus_x", "focus_y", "description"],
    "additionalProperties": False,
}

SMART_CROP_PROMPT = """\
Look at this image carefully. I need to crop it to a 9:16 portrait (vertical) \
format for a social media video.

Identify the FOCAL POINT — the most visually important and interesting region \
that should be preserved in the crop. Consider:
- People (especially faces, actions, interactions)
- Main subjects or products
- Key visual elements that tell the story
- Areas with the most visual interest or motion potential

Return the center coordinates of the most important region as normalized values:
- focus_x: 0.0 = left edge, 0.5 = center, 1.0 = right edge
- focus_y: 0.0 = top edge, 0.5 = center, 1.0 = bottom edge
- description: Brief description of what's at the focal point

The crop window will be positioned around your focal point. For a wide \
landscape image being cropped to portrait, the horizontal position (focus_x) \
is most critical — it determines which vertical strip of the image is kept."""


# ---------------------------------------------------------------------------
# Smart crop functions (will be extracted to tasks/smart_crop.py later)
# ---------------------------------------------------------------------------

def smart_crop_focal_point(vertex: VertexAIProvider, image_bytes: bytes,
                           model: str = GEMINI_MODEL):
    """Use Gemini 3 Flash to find the optimal 9:16 crop focal point.

    Args:
        vertex: VertexAIProvider instance.
        image_bytes: Raw image bytes (JPEG/PNG/WebP).
        model: Gemini model to use.

    Returns:
        dict with keys: focus_x (float 0-1), focus_y (float 0-1), description (str).
    """
    # Detect mime type from bytes header
    if image_bytes[:4] == b'\x89PNG':
        mime = "image/png"
    elif image_bytes[:4] == b'RIFF':
        mime = "image/webp"
    else:
        mime = "image/jpeg"

    b64 = base64.b64encode(image_bytes).decode("utf-8")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
                {"type": "text", "text": SMART_CROP_PROMPT},
            ],
        }
    ]

    result = vertex.call(
        model,
        messages,
        temperature=0.1,
        max_tokens=1000,
    )

    raw = (result.get("text") or "").strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]  # remove ```json line
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    parsed = json.loads(raw)

    # Clamp to valid range
    parsed["focus_x"] = max(0.0, min(1.0, float(parsed["focus_x"])))
    parsed["focus_y"] = max(0.0, min(1.0, float(parsed["focus_y"])))

    return parsed


def crop_around_focus(img_w: int, img_h: int, focus_x: float, focus_y: float,
                      target_ar: float = TARGET_AR):
    """Calculate crop box for target aspect ratio centered on focal point.

    Args:
        img_w: Image width in pixels.
        img_h: Image height in pixels.
        focus_x: Horizontal focal point (0.0-1.0).
        focus_y: Vertical focal point (0.0-1.0).
        target_ar: Target width/height ratio (default 9/16 = 0.5625).

    Returns:
        Tuple (left, top, right, bottom) in pixels.
    """
    # For landscape→portrait: use full height, calculate width
    crop_w = int(img_h * target_ar)
    crop_h = img_h

    if crop_w > img_w:
        # Image is already narrower than target — use full width, crop height
        crop_w = img_w
        crop_h = int(img_w / target_ar)

    # Position crop box centered on focal point, clamped to bounds
    cx = int(focus_x * img_w)
    cy = int(focus_y * img_h)

    left = max(0, min(cx - crop_w // 2, img_w - crop_w))
    top = max(0, min(cy - crop_h // 2, img_h - crop_h))

    return (left, top, left + crop_w, top + crop_h)


def center_crop_to_portrait(img_w: int, img_h: int, target_ar: float = TARGET_AR):
    """Calculate a naive center crop box for comparison.

    Returns:
        Tuple (left, top, right, bottom) in pixels.
    """
    return crop_around_focus(img_w, img_h, 0.5, 0.5, target_ar)


def apply_crop(image_bytes: bytes, box) -> bytes:
    """Crop image bytes using a (left, top, right, bottom) box. Returns JPEG bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    cropped = img.crop(box)
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Optional Veo video generation
# ---------------------------------------------------------------------------

def generate_veo_video(gcs, image_bytes: bytes, label: str):
    """Upload cropped image to GCS and generate a Veo video. Returns video URL."""
    from tvd_pipeline.services.veo3 import Veo3Service

    config = Config()
    svc = Veo3Service(gcs_storage_service=gcs, model=config.VEO3_FAST_MODEL)
    if not svc.initialized:
        print(f"    Veo not initialized, skipping {label}")
        return None

    ts = int(time.time())
    image_url = gcs.upload_image_bytes(image_bytes, f"smart_crop_test/{label}_{ts}.jpg")
    print(f"    Uploaded: {image_url[:60]}...")

    try:
        url = svc.generate_video(
            prompt="Subtle slow zoom in, very slight movement",
            image_url=image_url,
            duration=5,
            resolution="720p",
        )
        print(f"    Video: {url}")
        return url
    except Exception as e:
        print(f"    Veo error: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Smart crop test")
    parser.add_argument("--veo", action="store_true", help="Also generate Veo videos")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Init services
    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )
    vertex = VertexAIProvider(gcs_storage_service=gcs)
    if not vertex.initialized:
        print("ERROR: VertexAIProvider not initialized")
        sys.exit(1)

    # Discover test images
    if not os.path.isdir(ASSETS_DIR):
        print(f"ERROR: Assets dir not found: {ASSETS_DIR}")
        sys.exit(1)

    image_files = sorted([
        f for f in os.listdir(ASSETS_DIR)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ])
    if not image_files:
        print(f"ERROR: No images in {ASSETS_DIR}")
        sys.exit(1)

    print(f"Found {len(image_files)} test images in {ASSETS_DIR}")
    print(f"Output dir: {OUTPUT_DIR}")
    print(f"Model: {GEMINI_MODEL}")
    if args.veo:
        print("Veo video generation: ENABLED")
    print()

    results = []

    for image_name in image_files:
        image_path = os.path.join(ASSETS_DIR, image_name)
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        orientation = "LANDSCAPE" if w > h else "PORTRAIT" if h > w else "SQUARE"
        stem = os.path.splitext(image_name)[0]

        print(f"{'=' * 60}")
        print(f"IMAGE: {image_name} ({w}x{h}, {orientation})")
        print(f"{'=' * 60}")

        if w <= h:
            print(f"  Already portrait/square — skipping crop")
            results.append({
                "image": image_name, "size": f"{w}x{h}",
                "orientation": orientation, "skipped": True,
            })
            print()
            continue

        # --- Gemini focal point detection ---
        print(f"  Calling Gemini 3 Flash for focal point...")
        t0 = time.time()
        try:
            focal = smart_crop_focal_point(vertex, image_bytes)
        except Exception as e:
            print(f"  ERROR: Gemini call failed: {e}")
            results.append({
                "image": image_name, "size": f"{w}x{h}",
                "orientation": orientation, "error": str(e),
            })
            print()
            continue
        elapsed = time.time() - t0

        print(f"  Focal point: ({focal['focus_x']:.3f}, {focal['focus_y']:.3f})  [{elapsed:.1f}s]")
        print(f"  Description: {focal['description']}")

        # --- Calculate crop boxes ---
        smart_box = crop_around_focus(w, h, focal["focus_x"], focal["focus_y"])
        center_box = center_crop_to_portrait(w, h)

        print(f"  Smart crop:  {smart_box}  ({smart_box[2]-smart_box[0]}x{smart_box[3]-smart_box[1]})")
        print(f"  Center crop: {center_box}  ({center_box[2]-center_box[0]}x{center_box[3]-center_box[1]})")

        # --- Crop and save ---
        smart_bytes = apply_crop(image_bytes, smart_box)
        center_bytes = apply_crop(image_bytes, center_box)

        # Save original (as reference thumbnail)
        orig_path = os.path.join(OUTPUT_DIR, f"{stem}_original.jpg")
        img.save(orig_path, format="JPEG", quality=92)

        smart_path = os.path.join(OUTPUT_DIR, f"{stem}_smart_crop.jpg")
        with open(smart_path, "wb") as f:
            f.write(smart_bytes)

        center_path = os.path.join(OUTPUT_DIR, f"{stem}_center_crop.jpg")
        with open(center_path, "wb") as f:
            f.write(center_bytes)

        print(f"  Saved: {orig_path}")
        print(f"  Saved: {smart_path}")
        print(f"  Saved: {center_path}")

        result = {
            "image": image_name,
            "size": f"{w}x{h}",
            "orientation": orientation,
            "focus_x": focal["focus_x"],
            "focus_y": focal["focus_y"],
            "description": focal["description"],
            "smart_box": smart_box,
            "center_box": center_box,
            "smart_path": smart_path,
            "center_path": center_path,
        }

        # --- Optional Veo video generation ---
        if args.veo:
            print(f"  Generating Veo videos...")
            result["smart_video"] = generate_veo_video(gcs, smart_bytes, f"{stem}_smart")
            result["center_video"] = generate_veo_video(gcs, center_bytes, f"{stem}_center")

        results.append(result)
        print()

    # --- Summary ---
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"\n  {r['image']} ({r['size']}, {r['orientation']})")
        if r.get("skipped"):
            print(f"    SKIPPED (already portrait/square)")
        elif r.get("error"):
            print(f"    ERROR: {r['error']}")
        else:
            fx, fy = r["focus_x"], r["focus_y"]
            shift = abs(fx - 0.5)
            direction = "LEFT" if fx < 0.5 else "RIGHT" if fx > 0.5 else "CENTER"
            print(f"    Focal: ({fx:.3f}, {fy:.3f}) — {r['description']}")
            print(f"    Shift: {shift:.3f} {direction} of center")
            print(f"    Smart:  {r['smart_path']}")
            print(f"    Center: {r['center_path']}")
            if r.get("smart_video"):
                print(f"    Smart video:  {r['smart_video']}")
            if r.get("center_video"):
                print(f"    Center video: {r['center_video']}")

    print(f"\nOutput directory: {OUTPUT_DIR}")
    print("Compare *_smart_crop.jpg vs *_center_crop.jpg to evaluate quality.")


if __name__ == "__main__":
    main()
