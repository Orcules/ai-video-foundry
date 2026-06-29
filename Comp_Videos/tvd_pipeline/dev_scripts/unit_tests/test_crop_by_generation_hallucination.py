"""Crop-by-generation with hallucination check via GPT 5.4.

Flow:
  1. Generate 9:16 portrait via NB2 (Kie API)
  2. GPT 5.4 compares original vs regen for major hallucinations
  3. If hallucinated → retry NB2 (up to MAX_RETRIES)
  4. Save all attempts + final result

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation_hallucination path/to/image.jpg
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation_hallucination https://storage.googleapis.com/...jpg
"""

import argparse
import base64
import io
import json
import os
import sys
import time

import requests
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
from tvd_pipeline.services.kie import KieAIService
from tvd_pipeline.services.providers.openai_provider import OpenAIProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUTPUT_DIR = os.environ.get("OUTPUT_DIR_OVERRIDE") or os.path.join(
    script_dir, "test_output", "crop_by_generation_debug"
)

GPT_MODEL = "gpt-5.4"
MAX_RETRIES = 3
TARGET_AR = 9 / 16  # 0.5625
AR_TOLERANCE = 0.05

REGEN_PROMPT = (
    "Recreate this image in 9:16 portrait. use the same details, "
    "just zoom in little bit, using the same angle of the camera."
)

# ---------------------------------------------------------------------------
# Hallucination check prompt
# ---------------------------------------------------------------------------
HALLUCINATION_CHECK_PROMPT = """\
You are given two images:
- Image 1: the ORIGINAL photo
- Image 2: an AI-regenerated version in 9:16 portrait

The AI was asked to recreate this image in portrait format. To fit 9:16, \
the AI may have extended or changed content at the edges.

YOUR JOB: Compare the two images carefully. Look at what is visible in Image 2 \
that was NOT visible in Image 1 — especially at the top, bottom, left, and right \
edges where the AI had to add content.

Step 1: For each edge (top, bottom, left, right), describe what new content \
appears in Image 2 that was NOT in Image 1.

Step 2: Decide if any of that new content is a MAJOR hallucination.

MAJOR hallucination (mark as TRUE):
- A NEW door, doorway, or window that was not in the original at all
- A NEW room or space that opens up beyond the original frame
- NEW people, characters, or body parts that were not in the original
- NEW furniture (tables, chairs, shelves) that was not in the original
- NEW large objects (vehicles, appliances, signs)

IMPORTANT: These images are used for real business venues. An invented door \
or new person misleads viewers. Even small invented architectural elements \
(a door, a window) count as major.

NOT a hallucination (mark as FALSE):
- Seeing MORE of something that was already partially visible in the original \
  (e.g. stairs that were already in the original now show a few more steps — \
  this is just a wider view, NOT a hallucination)
- Same content slightly rearranged or zoomed differently
- More of an existing surface continuing naturally (same table, floor, wall)
- Existing railings, stairs, or walls continuing naturally
- Blurred or dark background continuation
- Minor food/plate/cup differences
- Color or lighting shifts
- A glass, napkin, or small tableware item appearing at the edge

KEY DISTINCTION: The question is whether the AI had to INVENT what is beyond \
the original frame. Continuing a flat, generic surface (table, floor, plain wall, \
blurred background) is fine because there is nothing specific to get wrong. \
But if the AI extends structured content (architecture, interiors, furniture \
arrangements) it is guessing what the real place looks like — and that guess \
could mislead viewers. If the extended content is specific enough that a viewer \
might think "oh that place has X" — and X was invented — that is a hallucination.

Respond with JSON."""

HALLUCINATION_SCHEMA = {
    "type": "object",
    "properties": {
        "has_major_hallucination": {
            "type": "boolean",
            "description": "True if the AI invented major new content not in the original",
        },
        "description": {
            "type": "string",
            "description": "What major content was hallucinated, or 'clean' if no major issues",
        },
    },
    "required": ["has_major_hallucination", "description"],
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


def download_image(url: str) -> bytes:
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    return resp.content


def upload_local_image(gcs, image_path: str) -> str:
    with open(image_path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    content_type = mime_map.get(ext, "image/jpeg")
    basename = os.path.basename(image_path).replace(" ", "_")
    gcs_path = f"test/crop_by_generation/{basename}"
    return gcs.upload_image_bytes(data, gcs_path, content_type=content_type)


def pil_to_jpeg_bytes(img: Image.Image) -> bytes:
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Step 1: NB2 regen via Kie
# ---------------------------------------------------------------------------

def nb2_regen_kie(kie: KieAIService, image_url: str, prompt: str) -> str | None:
    """Generate 9:16 portrait via NB2 on Kie. Returns temp URL or None."""
    return kie.generate_image_nano_banana(
        prompt,
        reference_image_url=image_url,
        aspect_ratio="9:16",
    )


# ---------------------------------------------------------------------------
# Step 2: GPT 5.4 hallucination check
# ---------------------------------------------------------------------------

def check_hallucination(openai_provider: OpenAIProvider, orig_bytes: bytes, regen_bytes: bytes) -> dict:
    """Ask GPT 5.4 to compare original vs regen for major hallucinations."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Image 1 (ORIGINAL):"},
        _b64_image_part(orig_bytes),
        {"type": "text", "text": "Image 2 (AI REGEN):"},
        _b64_image_part(regen_bytes),
        {"type": "text", "text": HALLUCINATION_CHECK_PROMPT},
    ]}]

    result = openai_provider.call(
        GPT_MODEL, messages, temperature=0.1,
        responseSchema=HALLUCINATION_SCHEMA,
    )
    raw = (result.get("text") or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    try:
        parsed = json.loads(raw)
        return {
            "has_major_hallucination": bool(parsed.get("has_major_hallucination", False)),
            "description": parsed.get("description", ""),
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"    Warning: could not parse GPT response, assuming clean: {raw[:200]}")
        return {"has_major_hallucination": False, "description": "parse failed"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crop-by-generation with hallucination check")
    parser.add_argument("image", help="Local file path or HTTP(S) URL")
    parser.add_argument("--prompt", default=REGEN_PROMPT, help="Override regen prompt")
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES, help="Max regen attempts")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Init services
    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )
    kie = KieAIService(api_key=config.KIE_API_KEY, gcs_storage_service=gcs)
    openai_provider = OpenAIProvider(api_key=os.environ.get("OPENAI_API_KEY", ""))

    # Load image
    is_url = args.image.startswith("http://") or args.image.startswith("https://")
    if is_url:
        image_bytes = download_image(args.image)
        ref_url = args.image
        image_name = args.image.split("/")[-1].split("?")[0] or "image"
    else:
        image_path = os.path.abspath(args.image)
        if not os.path.isfile(image_path):
            sys.exit(f"ERROR: File not found: {image_path}")
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        image_name = os.path.basename(image_path)
        print("Uploading to GCS...")
        ref_url = upload_local_image(gcs, image_path)
        print(f"Reference URL: {ref_url}")

    orig_img = Image.open(io.BytesIO(image_bytes))
    orig_w, orig_h = orig_img.size
    orig_ar = orig_w / orig_h
    stem = os.path.splitext(image_name)[0].replace(" ", "_")
    run_id = time.strftime("%H%M%S")

    print(f"\nImage: {image_name}  {orig_w}x{orig_h}  AR={orig_ar:.3f}")
    print(f"Target AR: {TARGET_AR:.4f} (9:16)")
    print(f"Prompt: {args.prompt}")
    print(f"Max retries: {args.max_retries}")
    print(f"Output: {OUTPUT_DIR}\n")

    # Check if already portrait
    if orig_ar <= TARGET_AR * (1 + AR_TOLERANCE):
        print(f"Image is already portrait (AR={orig_ar:.3f} <= {TARGET_AR * (1 + AR_TOLERANCE):.3f}). Nothing to do.")
        return

    # --- Convergence loop ---
    for attempt in range(1, args.max_retries + 1):
        print(f"{'=' * 60}")
        print(f"Attempt {attempt}/{args.max_retries}")
        print(f"{'=' * 60}")

        # Step 1: NB2 regen
        print(f"\n  Step 1: NB2 regen via Kie...")
        t0 = time.time()
        regen_url = nb2_regen_kie(kie, ref_url, args.prompt)
        elapsed = time.time() - t0
        print(f"    Time: {elapsed:.1f}s")

        if not regen_url:
            print(f"    NB2 returned None. Retrying...")
            continue

        # Download and validate
        try:
            regen_bytes = download_image(regen_url)
            regen_img = Image.open(io.BytesIO(regen_bytes))
            rw, rh = regen_img.size
            print(f"    Result: {rw}x{rh}  AR={rw/rh:.3f}")
        except Exception as e:
            print(f"    Failed to download/open regen image: {e}. Retrying...")
            continue

        # Save attempt
        attempt_path = os.path.join(OUTPUT_DIR, f"{stem}_attempt{attempt}_{run_id}.jpg")
        regen_img.convert("RGB").save(attempt_path, format="JPEG", quality=92)
        print(f"    Saved: {attempt_path}")

        # Step 2: GPT 5.4 hallucination check
        print(f"\n  Step 2: GPT 5.4 hallucination check...")
        t0 = time.time()
        hall = check_hallucination(openai_provider, image_bytes, pil_to_jpeg_bytes(regen_img))
        elapsed = time.time() - t0
        print(f"    Time: {elapsed:.1f}s")
        print(f"    Has major hallucination: {hall['has_major_hallucination']}")
        print(f"    Description: {hall['description']}")

        if not hall["has_major_hallucination"]:
            # Clean — save as final and done
            final_path = os.path.join(OUTPUT_DIR, f"{stem}_final_{run_id}.jpg")
            regen_img.convert("RGB").save(final_path, format="JPEG", quality=92)
            print(f"\n  PASS — no major hallucination. Final: {final_path}")
            return

        print(f"\n  FAIL — hallucination detected: {hall['description']}")
        if attempt < args.max_retries:
            print(f"  Retrying...")

    # All attempts failed
    print(f"\n{'=' * 60}")
    print(f"All {args.max_retries} attempts had hallucinations. Using last result anyway.")
    print(f"{'=' * 60}")
    final_path = os.path.join(OUTPUT_DIR, f"{stem}_final_fallback_{run_id}.jpg")
    regen_img.convert("RGB").save(final_path, format="JPEG", quality=92)
    print(f"Final (fallback): {final_path}")


if __name__ == "__main__":
    main()
