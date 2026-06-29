"""Gentle crop: trim edges minimally so AI video model has context.

Instead of cropping all the way to 9:16 (loses ~58% width on landscape),
do a minimal crop — just trim the unimportant edges. The result is NOT 9:16,
but much closer to portrait. The AI video model then only needs to
fill/crop a small amount, and it has edge context to avoid hallucination.

The LLM decides how much to crop from each side — the goal is to remove
only clearly unimportant peripheral content while keeping 85-95% of the image.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_gentle_crop
"""

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

OUTPUT_DIR = os.path.join(script_dir, "test_output", "gentle_crop")

GEMINI_MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Prompt & Schema
# ---------------------------------------------------------------------------
GENTLE_CROP_PROMPT = """\
This landscape image will be used as input for an AI video generator that \
outputs 9:16 portrait video. The video model works best when the input is \
closer to portrait format.

Your job: decide how much to GENTLY trim from the LEFT and RIGHT edges. \
The goal is NOT to crop to 9:16 — just remove clearly unimportant peripheral \
content (empty table, background wall, edge clutter) so the image is closer \
to portrait while keeping ALL the important content.

Rules:
- KEEP 85-95% of the original width — this is a gentle trim, not aggressive
- NEVER cut into the main subject (food, person, product, action)
- Leave a small hint of edge content so the AI knows what was there
- If both sides have equal importance, trim equally from both
- If one side has more clutter, trim more from that side
- If the image is already close to portrait or all content is important, \
  set trim_left and trim_right to 0

Return trim amounts as percentages of total image width:
- trim_left: percentage to trim from left edge (0-15)
- trim_right: percentage to trim from right edge (0-15)
- description: what you're trimming and why"""

GENTLE_CROP_SCHEMA = {
    "type": "object",
    "properties": {
        "trim_left": {
            "type": "number",
            "description": "Percentage to trim from left edge (0-15)",
        },
        "trim_right": {
            "type": "number",
            "description": "Percentage to trim from right edge (0-15)",
        },
        "description": {
            "type": "string",
            "description": "What content is being trimmed and reasoning",
        },
    },
    "required": ["trim_left", "trim_right", "description"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


def _b64_image_part(image_bytes: bytes) -> dict:
    mime = detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def pil_to_jpeg_bytes(img: Image.Image) -> bytes:
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def call_gentle_crop(vertex, image_bytes):
    """Ask LLM how much to trim from each side. Returns {trim_left, trim_right, description}."""
    messages = [{"role": "user", "content": [
        _b64_image_part(image_bytes),
        {"type": "text", "text": GENTLE_CROP_PROMPT},
    ]}]

    last_raw = ""
    for attempt in range(MAX_RETRIES):
        result = vertex.call(
            GEMINI_MODEL, messages,
            temperature=0.1, max_tokens=1000,
            responseSchema=GENTLE_CROP_SCHEMA,
        )
        raw = (result.get("text") or "").strip()
        last_raw = raw
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            parsed = json.loads(raw)
            parsed["trim_left"] = max(0.0, min(15.0, float(parsed["trim_left"])))
            parsed["trim_right"] = max(0.0, min(15.0, float(parsed["trim_right"])))
            return parsed
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"      Parse attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

    # Regex fallback
    tl = re.search(r'"trim_left"\s*:\s*([\d.]+)', last_raw)
    tr = re.search(r'"trim_right"\s*:\s*([\d.]+)', last_raw)
    if tl and tr:
        return {
            "trim_left": max(0.0, min(15.0, float(tl.group(1)))),
            "trim_right": max(0.0, min(15.0, float(tr.group(1)))),
            "description": "(regex fallback)",
        }
    raise ValueError(f"Could not parse after {MAX_RETRIES} attempts")


def apply_gentle_crop(img: Image.Image, trim_left_pct: float, trim_right_pct: float):
    """Crop image by trimming percentages from left/right. Returns (cropped_img, box)."""
    w, h = img.size
    left = int(w * trim_left_pct / 100)
    right = w - int(w * trim_right_pct / 100)
    box = (left, 0, right, h)
    return img.crop(box), box


def make_comparison(original_bytes, box, trim_left_pct, trim_right_pct, new_ar):
    """Side-by-side: original with trim lines + cropped result."""
    img = Image.open(io.BytesIO(original_bytes)).convert("RGB")
    w, h = img.size
    cropped = img.crop(box)
    cw, ch = cropped.size

    target_h = 600
    orig_scale = target_h / h
    crop_scale = target_h / ch

    orig_r = img.resize((int(w * orig_scale), target_h), Image.LANCZOS)
    crop_r = cropped.resize((int(cw * crop_scale), target_h), Image.LANCZOS)

    sb = (
        int(box[0] * orig_scale), 0,
        int(box[2] * orig_scale), target_h,
    )

    # Dim trimmed areas
    overlay = Image.new("RGBA", orig_r.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([(0, 0), (sb[0], target_h)], fill=(255, 0, 0, 80))
    od.rectangle([(sb[2], 0), (orig_r.width, target_h)], fill=(255, 0, 0, 80))
    orig_r = Image.alpha_composite(orig_r.convert("RGBA"), overlay).convert("RGB")

    # Draw trim lines
    draw = ImageDraw.Draw(orig_r)
    draw.line([(sb[0], 0), (sb[0], target_h)], fill=(0, 120, 255), width=3)
    draw.line([(sb[2], 0), (sb[2], target_h)], fill=(0, 120, 255), width=3)

    gap, hdr_h = 20, 40
    canvas = Image.new("RGB", (orig_r.width + gap + crop_r.width, target_h + hdr_h), (30, 30, 30))
    canvas.paste(orig_r, (0, hdr_h))
    canvas.paste(crop_r, (orig_r.width + gap, hdr_h))

    header = f"Gentle crop: L={trim_left_pct:.0f}% R={trim_right_pct:.0f}% | New AR={new_ar:.3f}"
    ImageDraw.Draw(canvas).text((10, 10), header, fill=(255, 255, 255))
    return canvas


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
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

    # Collect images from oishi assets
    image_entries = []
    if os.path.isdir(OISHI_DIR):
        for f in sorted(os.listdir(OISHI_DIR)):
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                image_entries.append((f, os.path.join(OISHI_DIR, f)))

    if not image_entries:
        print(f"ERROR: No images found in {OISHI_DIR}")
        sys.exit(1)

    print(f"Found {len(image_entries)} test images")
    print(f"Output: {OUTPUT_DIR}")
    print(f"Model: {GEMINI_MODEL}")
    print()

    results = []

    for image_name, image_path in image_entries:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        ar = w / h
        stem = os.path.splitext(image_name)[0]

        print(f"{'=' * 70}")
        print(f"{image_name}  {w}x{h}  AR={ar:.3f}")
        print(f"{'=' * 70}")

        if ar <= 9/16 + 0.01:
            print(f"  Already portrait — skipping")
            results.append({"image": image_name, "size": f"{w}x{h}", "ar": ar, "action": "SKIP"})
            print()
            continue

        # Ask LLM for gentle trim
        print(f"  Calling LLM for gentle crop advice...")
        t0 = time.time()
        try:
            resp = call_gentle_crop(vertex, image_bytes)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"image": image_name, "size": f"{w}x{h}", "ar": ar, "action": "ERROR", "error": str(e)})
            print()
            continue
        elapsed = time.time() - t0

        tl, tr = resp["trim_left"], resp["trim_right"]
        print(f"  Trim: left={tl:.1f}%, right={tr:.1f}%  [{elapsed:.1f}s]")
        print(f"  Reason: {resp['description']}")

        # Apply crop
        cropped_img, box = apply_gentle_crop(img, tl, tr)
        new_w, new_h = cropped_img.size
        new_ar = new_w / new_h
        kept_pct = new_w / w * 100

        print(f"  Result: {new_w}x{new_h} (AR={new_ar:.3f}, kept {kept_pct:.0f}% width)")

        # Save outputs
        orig_out = os.path.join(OUTPUT_DIR, f"{stem}_original.jpg")
        img_rgb = img.convert("RGB") if img.mode in ("RGBA", "P") else img
        img_rgb.save(orig_out, format="JPEG", quality=92)

        crop_path = os.path.join(OUTPUT_DIR, f"{stem}_gentle_crop.jpg")
        cropped_rgb = cropped_img.convert("RGB") if cropped_img.mode in ("RGBA", "P") else cropped_img
        cropped_rgb.save(crop_path, format="JPEG", quality=92)

        comp = make_comparison(image_bytes, box, tl, tr, new_ar)
        comp_path = os.path.join(OUTPUT_DIR, f"{stem}_comparison.jpg")
        comp.save(comp_path, format="JPEG", quality=92)

        print(f"  Original:   {orig_out}")
        print(f"  Cropped:    {crop_path}")
        print(f"  Comparison: {comp_path}")

        results.append({
            "image": image_name, "size": f"{w}x{h}", "ar": ar,
            "action": "GENTLE_CROP", "trim_left": tl, "trim_right": tr,
            "new_size": f"{new_w}x{new_h}", "new_ar": new_ar, "kept_pct": kept_pct,
            "description": resp["description"],
            "crop_path": crop_path, "comp_path": comp_path,
        })
        print()

    # --- Summary ---
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"{'Image':<45} {'Original':>12} {'AR':>6} {'Trim L':>7} {'Trim R':>7} {'Result':>12} {'New AR':>7} {'Kept':>5}")
    print("-" * 90)
    for r in results:
        if r["action"] == "SKIP":
            print(f"{r['image']:<45} {r['size']:>12} {r['ar']:>6.3f}   SKIP")
        elif r["action"] == "ERROR":
            print(f"{r['image']:<45} {r['size']:>12} {r['ar']:>6.3f}   ERROR: {r.get('error','')[:30]}")
        else:
            print(f"{r['image']:<45} {r['size']:>12} {r['ar']:>6.3f} "
                  f"{r['trim_left']:>6.1f}% {r['trim_right']:>6.1f}% "
                  f"{r['new_size']:>12} {r['new_ar']:>7.3f} {r['kept_pct']:>4.0f}%")

    print(f"\nOutput: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
