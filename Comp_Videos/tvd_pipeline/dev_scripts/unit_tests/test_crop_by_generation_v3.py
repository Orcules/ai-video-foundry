"""Crop-by-generation v3: image regen + CV alignment + LLM hallucination check + crop.

Step 1: Regenerate the image as 9:16 portrait (via fal.ai NB2 or Vertex AI Gemini).
Step 2: CV template matching locates where the original content sits in the regen.
        This tells us exactly how many pixels NB2 added on each side.
Step 3: LLM compares original vs regen, tells us which added edges are hallucinated.
Step 4: Crop only the hallucinated edges (keep safe extensions like table surface).

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation_v3 path/to/image.jpg
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation_v3 --provider vertex path/to/image.jpg
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation_v3 --provider fal path/to/image.jpg
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import time

import cv2
import numpy as np
import requests
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
OUTPUT_DIR = os.environ.get("OUTPUT_DIR_OVERRIDE") or os.path.join(
    script_dir, "test_output", "crop_by_generation_v3"
)

FAL_ENDPOINT = "https://queue.fal.run/fal-ai/nano-banana-2/edit"
POLL_INTERVAL = 2
GEMINI_MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 5

# Vertex AI image regen models (Nano Banana = Gemini image models)
# NB2 = gemini-3.1-flash-image-preview, NB Pro = gemini-3-pro-image-preview
VERTEX_REGEN_MODEL = "gemini-3.1-flash-image-preview"

# ---------------------------------------------------------------------------
# Step 1: NB2 regen prompt
# ---------------------------------------------------------------------------
REGEN_PROMPT = (
    "Recreate this image in vertical 9:16 aspect ratio. "
    "Super realistic, as similar as possible to the original. "
    "Keep all subjects, composition intent, colors, lighting, and details. "
    "You may crop into the existing content instead of extending edges — "
    "cropping is preferred over inventing new content that wasn't in the original."
)

# ---------------------------------------------------------------------------
# Step 3: LLM hallucination check — which added edges are bad?
# ---------------------------------------------------------------------------
HALLUCINATION_PROMPT = """\
You are given two images:
- Image 1: the ORIGINAL photo
- Image 2: an AI-regenerated version in 9:16 portrait

The AI extended the image to fit portrait. I know how much was added:
- Top: {added_top}px added
- Bottom: {added_bottom}px added
- Left: {added_left}px added
- Right: {added_right}px added

YOUR JOB: For each edge where content was added, decide if the extension is \
SAFE or DANGEROUS.

SAFE means the AI filled with generic, continuable content: more of the same \
surface (table, floor, wall), blurred background, sky, dark/shadow area. \
These are harmless — they don't invent anything specific.

DANGEROUS means the AI invented specific content: stairs continuing upward, \
rooms or doorways, people or body parts, text or signage, buildings or \
architecture extending, furniture that wasn't there, any recognizable object \
that the AI fabricated. Even if it looks plausible, if the AI had to "guess" \
what is beyond the edge, it is dangerous.

Respond with which edges to CROP (only the dangerous ones)."""

HALLUCINATION_SCHEMA = {
    "type": "object",
    "properties": {
        "crop_top": {
            "type": "boolean",
            "description": "True if the top extension is hallucinated and should be cropped",
        },
        "crop_bottom": {
            "type": "boolean",
            "description": "True if the bottom extension is hallucinated and should be cropped",
        },
        "crop_left": {
            "type": "boolean",
            "description": "True if the left extension is hallucinated and should be cropped",
        },
        "crop_right": {
            "type": "boolean",
            "description": "True if the right extension is hallucinated and should be cropped",
        },
        "description": {
            "type": "string",
            "description": "What was hallucinated (or 'all extensions are clean')",
        },
    },
    "required": ["crop_top", "crop_bottom", "crop_left", "crop_right", "description"],
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
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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


def pil_to_cv2(pil_img: Image.Image) -> np.ndarray:
    rgb = np.array(pil_img.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def make_comparison(orig_bytes, result_bytes, header):
    orig = Image.open(io.BytesIO(orig_bytes)).convert("RGB")
    result = Image.open(io.BytesIO(result_bytes)).convert("RGB")
    target_h = 600
    orig_r = orig.resize((int(orig.width * (target_h / orig.height)), target_h), Image.LANCZOS)
    result_r = result.resize((int(result.width * (target_h / result.height)), target_h), Image.LANCZOS)
    gap, hdr_h = 20, 40
    canvas = Image.new("RGB", (orig_r.width + gap + result_r.width, target_h + hdr_h), (30, 30, 30))
    canvas.paste(orig_r, (0, hdr_h))
    canvas.paste(result_r, (orig_r.width + gap, hdr_h))
    ImageDraw.Draw(canvas).text((10, 10), header, fill=(255, 255, 255))
    return canvas


# ---------------------------------------------------------------------------
# Step 1: NB2 regen
# ---------------------------------------------------------------------------

def nb2_regen(fal_key: str, image_url: str, max_attempts: int = 3) -> str:
    headers = {"Authorization": f"Key {fal_key}", "Content-Type": "application/json"}
    payload = {
        "prompt": REGEN_PROMPT,
        "image_urls": [image_url],
        "aspect_ratio": "9:16",
        "output_format": "png",
    }

    for attempt in range(1, max_attempts + 1):
        resp = requests.post(FAL_ENDPOINT, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print(f"  request_id: {data['request_id']}")

        while True:
            time.sleep(POLL_INTERVAL)
            s = requests.get(data["status_url"], headers=headers, timeout=15).json()
            if s["status"] == "COMPLETED":
                break
            if s["status"] in ("FAILED", "CANCELLED"):
                raise RuntimeError(f"fal.ai job {s['status']}: {s}")

        result = requests.get(data["response_url"], headers=headers, timeout=15).json()
        images = result.get("images") or result.get("output", {}).get("images", [])
        if images:
            return images[0]["url"]

        if attempt < max_attempts:
            print(f"    fal.ai returned no images (attempt {attempt}), retrying...")
            time.sleep(3)
        else:
            raise RuntimeError(f"fal.ai returned no images after {max_attempts} attempts: {result}")


def vertex_regen(image_bytes: bytes, gcs: "GCSStorageService", config: "Config") -> bytes:
    """Regenerate image as 9:16 portrait via Vertex AI Gemini image generation.
    Returns raw image bytes (not a URL)."""
    import random

    api_key = config.VERTEX_AI_API_KEY
    project_id = config.GEMINI_IMAGE_PROJECT_ID
    model = VERTEX_REGEN_MODEL

    endpoint = (
        f"https://aiplatform.googleapis.com/v1/projects/{project_id}"
        f"/locations/global/publishers/google/models/{model}:generateContent"
    )
    url = f"{endpoint}?key={api_key}"

    mime = detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "contents": {
            "role": "user",
            "parts": [
                {"text": f"Generate an image: {REGEN_PROMPT}"},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ],
        },
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {"aspectRatio": "9:16"},
        },
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=300,
            )
            resp.raise_for_status()
            result = resp.json()

            candidates = result.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"Vertex returned no candidates: {result}")

            for part in candidates[0].get("content", {}).get("parts", []):
                if "inlineData" in part:
                    return base64.b64decode(part["inlineData"]["data"])

            raise RuntimeError("Vertex response had no image data")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429 and attempt < 2:
                delay = 10 * (attempt + 1)
                print(f"    Rate limit 429, waiting {delay}s...")
                time.sleep(delay)
                continue
            raise

    raise RuntimeError("Vertex regen failed after 3 attempts")


# ---------------------------------------------------------------------------
# Step 2: CV template matching — find where the original sits in the regen
# ---------------------------------------------------------------------------

def find_original_in_regen(orig_pil: Image.Image, regen_pil: Image.Image,
                           strip_ratio: float = 0.2) -> dict:
    """Use OpenCV template matching on a CENTER STRIP of the original to locate
    it in the regen. A center strip matches more reliably than the full image
    because NB2 preserves the center content best while edges get extended/changed.

    Returns dict with added_top, added_bottom, added_left, added_right (pixels)."""
    orig_cv = pil_to_cv2(orig_pil)
    regen_cv = pil_to_cv2(regen_pil)

    rh, rw = regen_cv.shape[:2]
    oh, ow = orig_cv.shape[:2]

    # Resize original to match regen width
    scale = rw / ow
    resized_h = int(oh * scale)
    orig_resized = cv2.resize(orig_cv, (rw, resized_h))

    # Extract a center strip (e.g. 20% of height) from the resized original
    strip_h = int(resized_h * strip_ratio)
    strip_top = (resized_h - strip_h) // 2
    strip = orig_resized[strip_top:strip_top + strip_h, :]

    # Template matching — find where this center strip sits in the regen
    result = cv2.matchTemplate(regen_cv, strip, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    strip_match_x, strip_match_y = max_loc

    # The strip was taken from strip_top within the resized original.
    # So the top of the full original in the regen is:
    match_y = strip_match_y - strip_top
    match_x = strip_match_x

    added_top = max(0, match_y)
    added_bottom = max(0, rh - (match_y + resized_h))
    added_left = max(0, match_x)
    added_right = max(0, rw - (match_x + rw))

    return {
        "match_score": max_val,
        "match_y": match_y,
        "resized_h": resized_h,
        "added_top": added_top,
        "added_bottom": added_bottom,
        "added_left": added_left,
        "added_right": added_right,
    }


# ---------------------------------------------------------------------------
# Step 3: LLM hallucination check (schema + retry + regex fallback)
# ---------------------------------------------------------------------------

def _parse_hallucination_regex(raw: str) -> dict | None:
    ct = re.search(r'"crop_top"\s*:\s*(true|false)', raw, re.IGNORECASE)
    cb = re.search(r'"crop_bottom"\s*:\s*(true|false)', raw, re.IGNORECASE)
    cl = re.search(r'"crop_left"\s*:\s*(true|false)', raw, re.IGNORECASE)
    cr = re.search(r'"crop_right"\s*:\s*(true|false)', raw, re.IGNORECASE)
    if not ct:
        return None
    desc = re.search(r'"description"\s*:\s*"([^"]*)', raw)
    return {
        "crop_top": ct.group(1).lower() == "true",
        "crop_bottom": cb.group(1).lower() == "true" if cb else False,
        "crop_left": cl.group(1).lower() == "true" if cl else False,
        "crop_right": cr.group(1).lower() == "true" if cr else False,
        "description": desc.group(1) if desc else "",
    }


def check_hallucination(vertex, orig_bytes: bytes, regen_bytes: bytes, alignment: dict) -> dict:
    """Ask LLM which added edges are hallucinated. Returns {crop_top, crop_bottom, ...} booleans."""
    # Skip LLM if nothing was added
    if all(alignment[k] == 0 for k in ("added_top", "added_bottom", "added_left", "added_right")):
        return {"crop_top": False, "crop_bottom": False, "crop_left": False,
                "crop_right": False, "description": "nothing added"}

    prompt_text = HALLUCINATION_PROMPT.format(
        added_top=alignment["added_top"],
        added_bottom=alignment["added_bottom"],
        added_left=alignment["added_left"],
        added_right=alignment["added_right"],
    )

    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Image 1 (ORIGINAL):"},
        _b64_image_part(orig_bytes),
        {"type": "text", "text": "Image 2 (AI REGEN):"},
        _b64_image_part(regen_bytes),
        {"type": "text", "text": prompt_text},
    ]}]

    last_raw = ""
    for attempt in range(MAX_RETRIES):
        result = vertex.call(
            GEMINI_MODEL, messages,
            temperature=0.1,
            responseSchema=HALLUCINATION_SCHEMA,
        )
        raw = (result.get("text") or "").strip()
        last_raw = raw
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            parsed = json.loads(raw)
            parsed["crop_top"] = bool(parsed.get("crop_top", False))
            parsed["crop_bottom"] = bool(parsed.get("crop_bottom", False))
            parsed["crop_left"] = bool(parsed.get("crop_left", False))
            parsed["crop_right"] = bool(parsed.get("crop_right", False))
            return parsed
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            print(f"    Parse attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")

    # Regex fallback
    parsed = _parse_hallucination_regex(last_raw)
    if parsed:
        print("    Recovered via regex fallback")
        return parsed

    # Final fallback: assume no hallucination
    print("    All parse attempts failed — assuming no hallucination")
    return {"crop_top": False, "crop_bottom": False, "crop_left": False,
            "crop_right": False, "description": "parse failed"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TARGET_AR = 9 / 16  # 0.5625
AR_TOLERANCE = 0.05  # accept if within 5% of 9:16
MAX_ITERATIONS = 3


def main():
    parser = argparse.ArgumentParser(description="Crop-by-generation v3")
    parser.add_argument("image", help="Local file path or HTTP(S) URL")
    parser.add_argument("--provider", choices=["fal", "vertex"], default="vertex",
                        help="Image regen provider: fal (NB2 via fal.ai) or vertex (Gemini via Vertex AI)")
    args = parser.parse_args()

    use_vertex = args.provider == "vertex"

    if not use_vertex:
        fal_key = os.environ.get("FAL_KEY")
        if not fal_key:
            sys.exit("ERROR: FAL_KEY not set")
    else:
        fal_key = None

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )
    vertex = VertexAIProvider(gcs_storage_service=gcs)

    # --- Load image ---
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
    stem = os.path.splitext(image_name)[0].replace(" ", "_")
    run_id = time.strftime("%H%M%S")

    provider_label = f"Vertex AI ({VERTEX_REGEN_MODEL})" if use_vertex else "fal.ai NB2"
    print(f"\nImage: {image_name}  {orig_w}x{orig_h}  AR={orig_w/orig_h:.3f}")
    print(f"Provider: {provider_label}")
    print(f"Target AR: {TARGET_AR:.4f} (9:16)")
    print(f"Output: {OUTPUT_DIR}\n")

    # Save original
    orig_path = os.path.join(OUTPUT_DIR, f"{stem}_original.jpg")
    orig_img.convert("RGB").save(orig_path, format="JPEG", quality=92)

    # --- Convergence loop ---
    # Each iteration: the "input" image gets closer to 9:16 without hallucination.
    # - First iteration: input = original image
    # - Subsequent iterations: input = cropped result from previous iteration
    current_img = orig_img
    current_bytes = image_bytes
    current_ref_url = ref_url

    for iteration in range(1, MAX_ITERATIONS + 1):
        cw, ch = current_img.size
        current_ar = cw / ch
        print(f"{'=' * 60}")
        print(f"Iteration {iteration}/{MAX_ITERATIONS}  |  Input: {cw}x{ch}  AR={current_ar:.3f}")
        print(f"{'=' * 60}")

        # Check if already close enough to 9:16
        if abs(current_ar - TARGET_AR) / TARGET_AR < AR_TOLERANCE:
            print(f"  AR {current_ar:.3f} is within {AR_TOLERANCE*100:.0f}% of {TARGET_AR:.4f} — done!")
            break

        # Step A: Regen to 9:16
        print(f"\n  Step A: Regen to 9:16 via {provider_label}...")
        t0 = time.time()
        if use_vertex:
            regen_bytes = vertex_regen(current_bytes, gcs, config)
            regen_img = Image.open(io.BytesIO(regen_bytes))
            elapsed = time.time() - t0
            print(f"    Done in {elapsed:.1f}s (Vertex AI direct)")
        else:
            regen_url = nb2_regen(fal_key, current_ref_url)
            elapsed = time.time() - t0
            print(f"    Done in {elapsed:.1f}s -> {regen_url}")
            regen_bytes = download_image(regen_url)
            regen_img = Image.open(io.BytesIO(regen_bytes))
        rw, rh = regen_img.size
        print(f"    Size: {rw}x{rh} AR={rw/rh:.3f}")

        regen_path = os.path.join(OUTPUT_DIR, f"{stem}_iter{iteration}_regen_{run_id}.jpg")
        regen_img.convert("RGB").save(regen_path, format="JPEG", quality=92)

        # Step B: CV template matching
        print(f"\n  Step B: CV template matching...")
        alignment = find_original_in_regen(current_img, regen_img)
        print(f"    Match score: {alignment['match_score']:.4f}")
        print(f"    Added: top={alignment['added_top']}px  bottom={alignment['added_bottom']}px  "
              f"left={alignment['added_left']}px  right={alignment['added_right']}px")

        # Step C: LLM hallucination check
        print(f"\n  Step C: LLM checking extensions...")
        t0 = time.time()
        hall = check_hallucination(vertex, current_bytes, regen_bytes, alignment)
        elapsed = time.time() - t0
        print(f"    Time: {elapsed:.1f}s")
        print(f"    Description: {hall['description']}")
        print(f"    Crop: top={hall['crop_top']}  bottom={hall['crop_bottom']}  "
              f"left={hall['crop_left']}  right={hall['crop_right']}")

        # Step D: Crop hallucinated edges
        crop_top = alignment["added_top"] if hall["crop_top"] else 0
        crop_bottom = alignment["added_bottom"] if hall["crop_bottom"] else 0
        crop_left = alignment["added_left"] if hall["crop_left"] else 0
        crop_right = alignment["added_right"] if hall["crop_right"] else 0

        has_crop = crop_top > 0 or crop_bottom > 0 or crop_left > 0 or crop_right > 0

        if not has_crop:
            # No hallucination — regen is clean, we're done
            print(f"\n  No hallucination — regen is clean!")
            current_img = regen_img
            current_bytes = regen_bytes
            break

        left = crop_left
        top = crop_top
        right = rw - crop_right
        bottom = rh - crop_bottom
        cropped = regen_img.crop((left, top, right, bottom))
        cw_new, ch_new = cropped.size
        print(f"\n  Step D: Cropped to {cw_new}x{ch_new} AR={cw_new/ch_new:.3f}")
        print(f"    Removed: top={crop_top}px  bottom={crop_bottom}px  "
              f"left={crop_left}px  right={crop_right}px")

        cropped_path = os.path.join(OUTPUT_DIR, f"{stem}_iter{iteration}_cropped_{run_id}.jpg")
        cropped.convert("RGB").save(cropped_path, format="JPEG", quality=92)

        # Prepare for next iteration: cropped result becomes the new input
        current_img = cropped
        current_bytes = pil_to_jpeg_bytes(cropped)

        # Upload cropped image to GCS for next iteration (fal needs URL, vertex uses bytes directly)
        if not use_vertex:
            gcs_path = f"test/crop_by_generation/{stem}_iter{iteration}.jpg"
            current_ref_url = gcs.upload_image_bytes(current_bytes, gcs_path, content_type="image/jpeg")
            print(f"    Uploaded for next iteration: {current_ref_url}")

    # --- Save final result ---
    final_w, final_h = current_img.size
    final_ar = final_w / final_h
    print(f"\n{'=' * 60}")
    print(f"FINAL: {final_w}x{final_h}  AR={final_ar:.3f}  (target={TARGET_AR:.4f})")
    print(f"{'=' * 60}")

    final_path = os.path.join(OUTPUT_DIR, f"{stem}_final_{run_id}.jpg")
    current_img.convert("RGB").save(final_path, format="JPEG", quality=92)
    print(f"  Saved: {final_path}")

    # Comparison: original vs final
    comp = make_comparison(image_bytes, pil_to_jpeg_bytes(current_img),
                           f"{image_name} {orig_w}x{orig_h} -> {final_w}x{final_h}")
    comp_path = os.path.join(OUTPUT_DIR, f"{stem}_comparison_{run_id}.jpg")
    comp.save(comp_path, format="JPEG", quality=92)
    print(f"  Comparison: {comp_path}")


if __name__ == "__main__":
    main()
