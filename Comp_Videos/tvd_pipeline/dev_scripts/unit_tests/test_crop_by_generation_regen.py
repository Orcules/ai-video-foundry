"""Crop-by-generation v2: LLM safety advisor + Nano Banana 2 regen.

Step 1: Gemini Flash looks at the image edges and outputs warnings about
        what the AI must NOT invent (e.g. "don't invent what's above the stairs").
Step 2: Always regen via NB2, but with those warnings baked into the prompt.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation_regen path/to/image.jpg
    python -m tvd_pipeline.dev_scripts.unit_tests.test_crop_by_generation_regen https://storage.googleapis.com/...jpg
"""

import argparse
import base64
import io
import os
import sys
import time

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
OUTPUT_DIR = os.environ.get("OUTPUT_DIR_OVERRIDE") or os.path.join(script_dir, "test_output", "crop_by_generation_regen")

FAL_ENDPOINT = "https://queue.fal.run/fal-ai/nano-banana-2/edit"
POLL_INTERVAL = 2

GEMINI_MODEL = "gemini-3-flash-preview"

# ---------------------------------------------------------------------------
# Step 1: Edge safety analysis
# ---------------------------------------------------------------------------
SAFETY_PROMPT = """\
This image will be converted to 9:16 portrait by an AI image generator.
To achieve portrait framing, the AI can: crop the sides to make it narrower, \
and extend from one direction (top or bottom) to make it taller.

YOUR JOB: Decide which vertical direction (top or bottom) is SAFE to extend, \
and which is DANGEROUS. You must always fill BOTH lines.

SAFE means the edge has generic content that can be continued (table surface, floor, blurred background, sky, dark area).
DANGEROUS means extending would require inventing something unknown (stairs leading somewhere, a doorway, a person cut off, signage, architecture continuing).

If top is safe, then say so and explain what to fill with.
If top is dangerous, say so — and bottom is likely safe (or vice versa).
Both lines must always have a real answer — never write "none" unless the image is perfectly safe in all directions.

Respond in exactly this format (two lines only):
SAFE: <top or bottom> — <what to fill with>
DANGER: <top or bottom> — <why it must not be extended>\
"""

# ---------------------------------------------------------------------------
# Step 2: Regen prompt
# ---------------------------------------------------------------------------
REGEN_PROMPT_BASE = (
    "Recreate this image in vertical 9:16 aspect ratio. "
    "Super realistic, as similar as possible to the original. "
    "Keep all subjects, composition intent, colors, lighting, and details. "
    "To fit portrait: crop the left and right sides slightly to make the image narrower, "
    "then extend from the top or bottom to gain height. "
    "Only extend with safe, generic content (more of the same surface, blurred background, dark area)."
)

REGEN_PROMPT_DIRECTED = (
    "Recreate this image in vertical 9:16 aspect ratio. "
    "Super realistic, as similar as possible to the original. "
    "Keep all subjects, composition intent, colors, lighting, and details. "
    "To fit portrait: crop the left and right sides slightly to make the image narrower, "
    "then extend from a safe direction to gain height. "
    "EXTEND from: {safe_to_extend}. "
    "!!! CRITICAL WARNING !!! ABSOLUTELY DO NOT extend from {do_not_extend}. "
    "You CANNOT know what is there — inventing content in that direction will ruin the image. "
    "Crop that edge instead."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


def download_image(url: str) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def upload_local_image(gcs: GCSStorageService, image_path: str) -> str:
    with open(image_path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    content_type = mime_map.get(ext, "image/jpeg")
    basename = os.path.basename(image_path).replace(" ", "_")
    gcs_path = f"test/crop_by_generation/{basename}"
    return gcs.upload_image_bytes(data, gcs_path, content_type=content_type)


def make_comparison(orig_bytes: bytes, result_bytes: bytes, header: str) -> Image.Image:
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
# Step 1: LLM safety advisor
# ---------------------------------------------------------------------------

def analyze_edges(vertex: VertexAIProvider, image_bytes: bytes) -> dict:
    """Ask Gemini Flash which edges are safe/dangerous. Returns dict with safe_to_extend and do_not_extend."""
    mime = detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "text": SAFETY_PROMPT},
    ]}]

    result = vertex.call(GEMINI_MODEL, messages, temperature=0.1, max_tokens=300)
    raw = (result.get("text") or "").strip()
    print(f"  Raw LLM output: {repr(raw)}")

    safe, danger = "", ""
    for line in raw.split("\n"):
        line = line.strip()
        if line.upper().startswith("SAFE:"):
            safe = line[5:].strip()
            if safe.lower() == "none":
                safe = ""
        elif line.upper().startswith("DANGER:"):
            danger = line[7:].strip()
            if danger.lower() == "none":
                danger = ""

    # Auto-infer: if SAFE says "bottom" but DANGER is empty, the top is dangerous (and vice versa)
    if safe and not danger:
        safe_lower = safe.lower()
        if safe_lower.startswith("bottom"):
            danger = "top — unknown content, do not invent"
        elif safe_lower.startswith("top"):
            danger = "bottom — unknown content, do not invent"

    return {"safe_to_extend": safe, "do_not_extend": danger}


# ---------------------------------------------------------------------------
# Step 2: NB2 regen
# ---------------------------------------------------------------------------

def execute_regen(fal_key: str, image_url: str, prompt: str) -> str:
    headers = {
        "Authorization": f"Key {fal_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "image_urls": [image_url],
        "aspect_ratio": "9:16",
        "output_format": "png",
    }

    resp = requests.post(FAL_ENDPOINT, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    request_id = data["request_id"]
    print(f"  Submitted to fal.ai — request_id: {request_id}")

    response_url = data.get("response_url")
    status_url = data.get("status_url")

    while True:
        time.sleep(POLL_INTERVAL)
        status_resp = requests.get(status_url, headers=headers, timeout=15)
        status_resp.raise_for_status()
        status = status_resp.json()
        state = status.get("status")
        if state == "COMPLETED":
            break
        elif state in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"fal.ai job {state}: {status}")

    result_resp = requests.get(response_url, headers=headers, timeout=15)
    result_resp.raise_for_status()
    result = result_resp.json()
    images = result.get("images", [])
    if not images:
        raise RuntimeError(f"fal.ai returned no images: {result}")
    return images[0]["url"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crop-by-generation with safety advisor")
    parser.add_argument("image", help="Local file path or HTTP(S) URL of the image")
    args = parser.parse_args()

    fal_key = os.environ.get("FAL_KEY")
    if not fal_key:
        print("ERROR: FAL_KEY environment variable not set")
        sys.exit(1)

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

    # --- Load image ---
    is_url = args.image.startswith("http://") or args.image.startswith("https://")

    if is_url:
        print(f"Downloading: {args.image}")
        image_bytes = download_image(args.image)
        ref_url = args.image
        image_name = args.image.split("/")[-1].split("?")[0] or "image"
    else:
        image_path = os.path.abspath(args.image)
        if not os.path.isfile(image_path):
            print(f"ERROR: File not found: {image_path}")
            sys.exit(1)
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        image_name = os.path.basename(image_path)
        print(f"Uploading to GCS as reference...")
        ref_url = upload_local_image(gcs, image_path)
        print(f"Reference URL: {ref_url}")

    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    ar = w / h if h > 0 else 1.0
    stem = os.path.splitext(image_name)[0].replace(" ", "_")

    print(f"\nImage: {image_name}  {w}x{h}  AR={ar:.3f}")
    print(f"Output: {OUTPUT_DIR}\n")

    # --- Copy original ---
    orig_path = os.path.join(OUTPUT_DIR, f"{stem}_original.jpg")
    img.convert("RGB").save(orig_path, format="JPEG", quality=92)

    # --- Step 1: Safety analysis ---
    print("Step 1: Analyzing edges via Gemini Flash...")
    t0 = time.time()
    edges = analyze_edges(vertex, image_bytes)
    elapsed = time.time() - t0
    print(f"  Time: {elapsed:.1f}s")
    print(f"  Safe to extend: {edges['safe_to_extend'] or '(none)'}")
    print(f"  Do not extend: {edges['do_not_extend'] or '(none)'}")

    # --- Step 2: Regen with direction guidance ---
    if edges["safe_to_extend"] or edges["do_not_extend"]:
        prompt = REGEN_PROMPT_DIRECTED.format(
            safe_to_extend=edges["safe_to_extend"] or "any safe direction",
            do_not_extend=edges["do_not_extend"] or "nothing specific",
        )
    else:
        prompt = REGEN_PROMPT_BASE
    print(f"\nStep 2: Regenerating via Nano Banana 2...")
    print(f"  Prompt: {prompt}")
    t0 = time.time()
    result_url = execute_regen(fal_key, ref_url, prompt)
    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.1f}s -> {result_url}")

    result_bytes = download_image(result_url)
    result_img = Image.open(io.BytesIO(result_bytes))
    rw, rh = result_img.size
    print(f"  Result: {rw}x{rh} AR={rw/rh:.3f}")

    run_id = time.strftime("%H%M%S")
    result_path = os.path.join(OUTPUT_DIR, f"{stem}_regen_{run_id}.jpg")
    result_img.convert("RGB").save(result_path, format="JPEG", quality=92)
    print(f"  Saved: {result_path}")

    # --- Comparison ---
    warn_tag = "directed" if (edges["safe_to_extend"] or edges["do_not_extend"]) else "no warnings"
    header = f"{image_name} {w}x{h} | NB2 regen ({warn_tag}) -> {rw}x{rh}"
    comp = make_comparison(image_bytes, result_bytes, header)
    comp_path = os.path.join(OUTPUT_DIR, f"{stem}_comparison_{run_id}.jpg")
    comp.save(comp_path, format="JPEG", quality=92)
    print(f"  Comparison: {comp_path}")


if __name__ == "__main__":
    main()
