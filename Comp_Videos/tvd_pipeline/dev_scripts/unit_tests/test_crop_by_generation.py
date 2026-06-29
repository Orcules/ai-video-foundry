"""Crop by generation: smart crop with black-bar threshold instead of width>height.

Instead of the binary "is landscape?" check, this calculates how much black
bar area would appear if the image were placed as-is in a 9:16 frame.
If the bars are negligible (below a configurable threshold), skip cropping.
Otherwise, call Gemini for focal point and crop to 9:16.

This avoids cropping images that are only a few pixels off from portrait,
while still cropping images where the bars would be noticeable.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation

    # Custom threshold (default 5%):
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation --threshold 10
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import time

from PIL import Image, ImageDraw

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
OISHI_DIR = os.path.normpath(os.path.join(
    script_dir, "..", "..", "..", "..", "api_pipeline", "documents",
    "test_scripts", "oishi_assets",
))
FLYMORE_DIR = os.path.normpath(os.path.join(
    script_dir, "..", "..", "..", "..", "api_pipeline", "documents",
    "test_scripts", "flymore_assets",
))

OUTPUT_DIR = os.path.join(script_dir, "test_output", "crop_by_generation")

TARGET_AR = 9 / 16  # 0.5625
DEFAULT_BAR_THRESHOLD = 5.0  # percent
GEMINI_MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 5

# ---------------------------------------------------------------------------
# Schema + Prompt (simple focal point, no safety/validation)
# ---------------------------------------------------------------------------
FOCAL_SCHEMA = {
    "type": "object",
    "properties": {
        "focus_x": {
            "type": "number",
            "description": "Horizontal center of the most important region (0.0=left, 1.0=right)",
        },
        "focus_y": {
            "type": "number",
            "description": "Vertical center of the most important region (0.0=top, 1.0=bottom)",
        },
        "description": {
            "type": "string",
            "description": "What content is at the focal point",
        },
    },
    "required": ["focus_x", "focus_y", "description"],
    "additionalProperties": False,
}

FOCAL_PROMPT = """\
This image MUST be cropped to 9:16 portrait format for a social media video. \
Your job is to find the BEST focal point for the crop.

Think about what makes the most compelling 9:16 crop:
- Where are the PEOPLE? Especially faces, actions, expressions
- Where is the ACTION or MOTION happening?
- What is the most visually dynamic or interesting part?
- Prefer human activity over empty structures or backgrounds

Return the center of the best crop region as normalized coordinates:
- focus_x: 0.0 = left edge, 0.5 = center, 1.0 = right edge
- focus_y: 0.0 = top edge, 0.5 = center, 1.0 = bottom edge
- description: what you chose and why

IMPORTANT: Do NOT default to center (0.5, 0.5). Look at the actual image \
and find where the most interesting content is."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


def black_bar_pct(ar: float, target_ar: float = TARGET_AR) -> float:
    """Calculate % of a 9:16 frame that would be black bars if image is placed as-is.

    The image is scaled to fit inside the frame (no cropping), and the
    remaining area is black bars.

    Returns a value 0-100.
    """
    if ar <= target_ar:
        # Taller than target: fit by height, bars on left/right
        return (1.0 - ar / target_ar) * 100
    else:
        # Wider than target: fit by width, bars on top/bottom
        return (1.0 - target_ar / ar) * 100


def crop_around_focus(img_w, img_h, focus_x, focus_y, target_ar=TARGET_AR):
    crop_w = int(img_h * target_ar)
    crop_h = img_h
    if crop_w > img_w:
        crop_w = img_w
        crop_h = int(img_w / target_ar)
    cx = int(focus_x * img_w)
    cy = int(focus_y * img_h)
    left = max(0, min(cx - crop_w // 2, img_w - crop_w))
    top = max(0, min(cy - crop_h // 2, img_h - crop_h))
    return (left, top, left + crop_w, top + crop_h)


def _parse_regex(raw: str) -> dict | None:
    fx = re.search(r'"focus_x"\s*:\s*([\d.]+)', raw)
    fy = re.search(r'"focus_y"\s*:\s*([\d.]+)', raw)
    desc = re.search(r'"description"\s*:\s*"([^"]*)', raw)
    if fx and fy:
        return {
            "focus_x": max(0.0, min(1.0, float(fx.group(1)))),
            "focus_y": max(0.0, min(1.0, float(fy.group(1)))),
            "description": desc.group(1) if desc else "",
        }
    return None


def call_focal_llm(vertex: VertexAIProvider, image_bytes: bytes) -> dict:
    """Call Gemini for focal point. Retries up to MAX_RETRIES with regex fallback."""
    mime = detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "text": FOCAL_PROMPT},
    ]}]

    last_err = None
    last_raw = ""

    for attempt in range(MAX_RETRIES):
        result = vertex.call(
            GEMINI_MODEL, messages,
            temperature=0.1, max_tokens=1000,
            responseSchema=FOCAL_SCHEMA,
        )
        raw = (result.get("text") or "").strip()
        last_raw = raw
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            parsed = json.loads(raw)
            parsed["focus_x"] = max(0.0, min(1.0, float(parsed["focus_x"])))
            parsed["focus_y"] = max(0.0, min(1.0, float(parsed["focus_y"])))
            return parsed
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            last_err = e
            print(f"      Parse attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

    parsed = _parse_regex(last_raw)
    if parsed:
        print("      Recovered via regex fallback")
        return parsed
    raise ValueError(f"Could not parse after {MAX_RETRIES} attempts: {last_err}")


def make_comparison(original_bytes, crop_box, header_text):
    """Side-by-side: original with crop overlay + cropped result."""
    img = Image.open(io.BytesIO(original_bytes)).convert("RGB")
    w, h = img.size
    cropped = img.crop(crop_box)
    cw, ch = cropped.size

    target_h = 600
    orig_scale = target_h / h
    crop_scale = target_h / ch

    orig_r = img.resize((int(w * orig_scale), target_h), Image.LANCZOS)
    crop_r = cropped.resize((int(cw * crop_scale), target_h), Image.LANCZOS)

    sb = (
        int(crop_box[0] * orig_scale), int(crop_box[1] * orig_scale),
        int(crop_box[2] * orig_scale), int(crop_box[3] * orig_scale),
    )

    # Dim outside crop
    overlay = Image.new("RGBA", orig_r.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([(0, 0), (sb[0], target_h)], fill=(0, 0, 0, 120))
    od.rectangle([(sb[2], 0), (orig_r.width, target_h)], fill=(0, 0, 0, 120))
    od.rectangle([(sb[0], 0), (sb[2], sb[1])], fill=(0, 0, 0, 120))
    od.rectangle([(sb[0], sb[3]), (sb[2], target_h)], fill=(0, 0, 0, 120))
    orig_r = Image.alpha_composite(orig_r.convert("RGBA"), overlay).convert("RGB")
    ImageDraw.Draw(orig_r).rectangle(sb, outline=(0, 255, 0), width=3)

    gap, hdr_h = 20, 40
    canvas = Image.new("RGB", (orig_r.width + gap + crop_r.width, target_h + hdr_h), (30, 30, 30))
    canvas.paste(orig_r, (0, hdr_h))
    canvas.paste(crop_r, (orig_r.width + gap, hdr_h))
    ImageDraw.Draw(canvas).text((10, 10), header_text, fill=(255, 255, 255))
    return canvas


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crop by generation test")
    parser.add_argument("--threshold", type=float, default=DEFAULT_BAR_THRESHOLD,
                        help=f"Black bar %% threshold to skip crop (default: {DEFAULT_BAR_THRESHOLD}%%)")
    args = parser.parse_args()
    threshold = args.threshold

    os.makedirs(OUTPUT_DIR, exist_ok=True)

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

    # Collect images
    image_entries = []
    for label, dir_path in [("oishi", OISHI_DIR), ("flymore", FLYMORE_DIR)]:
        if not os.path.isdir(dir_path):
            print(f"WARNING: {label} dir not found: {dir_path}")
            continue
        for f in sorted(os.listdir(dir_path)):
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                image_entries.append((label, f, os.path.join(dir_path, f)))

    if not image_entries:
        print("ERROR: No test images found")
        sys.exit(1)

    print(f"Found {len(image_entries)} test images")
    print(f"Output: {OUTPUT_DIR}")
    print(f"Model: {GEMINI_MODEL}")
    print(f"Black bar threshold: {threshold}% (below this = skip crop)")
    print()

    # Show threshold examples
    print("  Example black bar percentages:")
    for name, ar in [("9:16", 0.5625), ("2:3", 0.667), ("3:4", 0.75),
                     ("1:1", 1.0), ("4:3", 1.333), ("16:9", 1.778)]:
        bp = black_bar_pct(ar)
        action = "SKIP" if bp < threshold else "CROP"
        print(f"    {name:>5} (AR={ar:.3f}): {bp:5.1f}% bars -> {action}")
    print()

    results = []

    for source, image_name, image_path in image_entries:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        ar = w / h if h > 0 else 1.0
        bars = black_bar_pct(ar)
        stem = os.path.splitext(image_name)[0]
        prefix = f"{source}_{stem}"

        print(f"{'=' * 70}")
        print(f"[{source}] {image_name}  {w}x{h}  AR={ar:.3f}  Bars={bars:.1f}%")
        print(f"{'=' * 70}")

        # Save original copy
        orig_out = os.path.join(OUTPUT_DIR, f"{prefix}_original.jpg")
        img_rgb = img.convert("RGB") if img.mode in ("RGBA", "P") else img
        img_rgb.save(orig_out, format="JPEG", quality=92)

        if bars < threshold:
            print(f"  SKIP -- {bars:.1f}% bars < {threshold}% threshold (negligible)")
            print(f"  Original: {orig_out}")
            results.append({
                "source": source, "image": image_name, "size": f"{w}x{h}",
                "ar": ar, "bars": bars, "action": "SKIP",
                "original_path": image_path, "original_copy": orig_out,
            })
            print()
            continue

        # Crop
        print(f"  CROP -- {bars:.1f}% bars >= {threshold}% threshold")
        print(f"  Calling Gemini for focal point...")
        t0 = time.time()
        try:
            resp = call_focal_llm(vertex, image_bytes)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({
                "source": source, "image": image_name, "size": f"{w}x{h}",
                "ar": ar, "bars": bars, "action": "ERROR",
                "error": str(e), "original_path": image_path,
                "original_copy": orig_out,
            })
            print()
            continue
        fx, fy = resp["focus_x"], resp["focus_y"]
        elapsed = time.time() - t0
        print(f"  Focal: ({fx:.3f}, {fy:.3f})  [{elapsed:.1f}s]")
        print(f"  Desc: {resp['description']}")

        box = crop_around_focus(w, h, fx, fy)
        cropped_img = img.crop(box)
        if cropped_img.mode in ("RGBA", "P"):
            cropped_img = cropped_img.convert("RGB")

        crop_path = os.path.join(OUTPUT_DIR, f"{prefix}_cropped.jpg")
        cropped_img.save(crop_path, format="JPEG", quality=92)

        header = f"Bars={bars:.1f}% | Focal=({fx:.2f},{fy:.2f}) | CROP"
        comp = make_comparison(image_bytes, box, header)
        comp_path = os.path.join(OUTPUT_DIR, f"{prefix}_comparison.jpg")
        comp.save(comp_path, format="JPEG", quality=92)

        print(f"  Original:   {orig_out}")
        print(f"  Cropped:    {crop_path}")
        print(f"  Comparison: {comp_path}")

        results.append({
            "source": source, "image": image_name, "size": f"{w}x{h}",
            "ar": ar, "bars": bars, "action": "CROP",
            "focus_x": fx, "focus_y": fy,
            "original_path": image_path, "original_copy": orig_out,
            "cropped_path": crop_path, "comparison_path": comp_path,
        })
        print()

    # --- Summary ---
    print()
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"{'Source':<8} {'Image':<35} {'Size':<12} {'AR':>6} {'Bars':>6} {'Action':<6}")
    print("-" * 90)

    for r in results:
        print(
            f"{r['source']:<8} {r['image']:<35} {r['size']:<12} "
            f"{r['ar']:>6.3f} {r['bars']:>5.1f}% {r['action']:<6}"
        )

    actions = {}
    for r in results:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
    print(f"\nActions: {dict(actions)}")
    print(f"Threshold: {threshold}%")
    print(f"Output: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
