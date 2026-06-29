"""Smart extend: LLM decides which edges are safe to extend, then NB2 executes.

Step 1: GPT 5.4 looks at the image + current AR vs target AR (9:16).
        Outputs which edges are safe to extend and which are dangerous (with reasoning).
Step 2: NB2 extends from the safe edges with a directed prompt.
        If no edges are safe → falls back to full "recreate in 9:16".

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_extend_bottom path/to/image.jpg
    python -m tvd_pipeline.dev_scripts.unit_tests.test_extend_bottom --provider vertex img1.jpg img2.jpg
    python -m tvd_pipeline.dev_scripts.unit_tests.test_extend_bottom --provider fal img1.jpg
"""

import argparse
import base64
import io
import json
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
from tvd_pipeline.services.providers.openai_provider import OpenAIProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUTPUT_DIR = os.environ.get("OUTPUT_DIR_OVERRIDE") or os.path.join(
    script_dir, "test_output", "extend_bottom"
)

TARGET_AR = 9 / 16  # 0.5625

GPT_MODEL = "gpt-5.4"

# Kie.ai
KIE_BASE = "https://api.kie.ai"
KIE_MODEL = "nano-banana-2"

# Vertex AI image gen
VERTEX_MODEL = "gemini-3-pro-image-preview"

# fal.ai
FAL_ENDPOINT = "https://queue.fal.run/fal-ai/nano-banana-2/edit"

# ---------------------------------------------------------------------------
# Step 1: LLM Advisor prompt (synced with shared_crop_by_generation_advisor_user.md)
# ---------------------------------------------------------------------------
ADVISOR_PROMPT = """\
You are an image framing advisor. You are given an image that needs to be \
converted to 9:16 portrait aspect ratio (width:height = 0.5625).

Current image size: {width}x{height} (aspect ratio: {current_ar:.3f})
Target aspect ratio: 0.5625 (9:16 portrait)

YOUR JOB: Decide the best strategy to convert this image to 9:16 portrait.

WHY THIS MATTERS: These images are used in ads for real businesses — restaurants, \
venues, hotels, bowling alleys, etc. If the AI extends an image and invents \
rooms, furniture, or architectural details that don't exist in reality, viewers \
will think the place looks different than it actually does. That's misleading. \
So we prefer to extend edges where the AI only needs to add generic, \
non-specific content that won't mislead anyone.

Look at each edge of the image and decide if it is SAFE or DANGEROUS to extend:
- SAFE: the edge has generic content that can be continued without misleading \
anyone — more of the same surface (table, floor, counter), blurred background, \
sky, dark/shadow area, solid color, grass, water, out-of-focus areas. \
Even if the AI invents a bit here, no one will think the real place looks \
different because of it.
- DANGEROUS: extending would require the AI to invent specific, recognizable \
content — stairs continuing, rooms or doorways appearing, people or body parts, \
text or signage, buildings or architecture extending, furniture that wasn't \
there, interior design details. This would make viewers think the real venue \
or product looks different than it actually does. \
The more specific the invented content, the more dangerous it is.

Think about what the image needs to reach 9:16:
- Image is wider than 9:16 (AR > 0.5625) → needs more height → extend top and/or bottom
- Image is taller than 9:16 (AR < 0.5625) → needs more width → extend left and/or right
- Image is much wider (AR > 1.0) → needs a LOT more height → may need top + bottom, \
or even recreate if edges are dangerous

IMPORTANT: Be conservative. Only extend edges that are TRULY safe. \
Do NOT add a direction just because you need more pixels — only add it if \
the content at that edge is genuinely generic and safe to continue. \
If one direction is safe and the other is questionable, extend from ONE \
direction only. The AI image generator will handle the rest by cropping \
or adjusting. Fewer extensions = less risk of misleading content.

Based on which edges are safe, pick ONE prompt from the list below.

For each axis (vertical and horizontal), you must decide independently:

VERTICAL axis (top/bottom) — decide between:
  a) Extend ONLY from bottom (if bottom is safe, top is dangerous)
  b) Extend ONLY from top (if top is safe, bottom is dangerous)
  c) Extend from bottom OR top (if both are safe but you only need one — pick the safer one)
  d) Extend from BOTH top and bottom (ONLY if both are truly safe AND you need a lot of height)
  e) Don't extend vertically (not needed, or both edges dangerous)

HORIZONTAL axis (left/right) — decide between:
  a) Extend ONLY from left (if left is safe, right is dangerous)
  b) Extend ONLY from right (if right is safe, left is dangerous)
  c) Extend from left OR right (if both are safe but you only need one — pick the safer one)
  d) Extend from BOTH left and right (ONLY if both are truly safe AND you need a lot of width)
  e) Don't extend horizontally (not needed, or both edges dangerous)

Then combine your vertical + horizontal decisions into one prompt:

Vertical only:
- "Extend the image from the bottom. Try to invent as little as possible."
- "Extend the image from the top. Try to invent as little as possible."
- "Extend the image from the bottom or top. Try to invent as little as possible."
- "Extend the image from the bottom and top. Try to invent as little as possible."

Horizontal only:
- "Extend the image from the left. Try to invent as little as possible."
- "Extend the image from the right. Try to invent as little as possible."
- "Extend the image from the left or right. Try to invent as little as possible."
- "Extend the image from the left and right. Try to invent as little as possible."

Vertical + horizontal combined:
- "Extend the image from the bottom and left. Try to invent as little as possible."
- "Extend the image from the bottom and right. Try to invent as little as possible."
- "Extend the image from the bottom, left, and right. Try to invent as little as possible."
- "Extend the image from the top and left. Try to invent as little as possible."
- "Extend the image from the top and right. Try to invent as little as possible."
- "Extend the image from the top, left, and right. Try to invent as little as possible."
- "Extend the image from the bottom, top, and left. Try to invent as little as possible."
- "Extend the image from the bottom, top, and right. Try to invent as little as possible."
- "Extend the image from all sides. Try to invent as little as possible."

If no edges are safe (all dangerous):
- "Recreate this image in 9:16 portrait. Super realistic, as similar as possible \
to the original. Try to invent as little as possible."

Examples:

Example 1: A 1200x800 landscape photo of a restaurant table. AR=1.5, needs height.
Top edge: ceiling with specific light fixtures and decorations → DANGEROUS
Bottom edge: wooden floor, blurred → SAFE
→ Only bottom is safe. Extend one direction only.
→ "Extend the image from the bottom. Try to invent as little as possible."

Example 2: A 1000x1000 square photo of a bowling alley lane. AR=1.0, needs height.
Top edge: dark ceiling, completely out of focus → SAFE
Bottom edge: generic lane floor, no markings → SAFE
→ Both top and bottom are truly safe generic content.
→ "Extend the image from the bottom and top. Try to invent as little as possible."

Example 3: A 500x1000 tall narrow photo of a person standing. AR=0.5, needs width.
Left edge: plain wall, blurred → SAFE
Right edge: doorway with sign → DANGEROUS
→ Only left is safe.
→ "Extend the image from the left. Try to invent as little as possible."

Example 4: A 1600x900 wide landscape of a sushi restaurant. AR=1.78, needs a LOT of height.
Top edge: stairs and specific interior architecture → DANGEROUS
Bottom edge: table surface with tablecloth → SAFE
→ Even though the image needs a LOT of height, top is dangerous (stairs, \
architecture). Only extend bottom — the AI will crop/adjust the rest.
→ "Extend the image from the bottom. Try to invent as little as possible."

Example 5: A 1600x900 wide landscape of a hotel pool. AR=1.78, needs a LOT of height.
Top edge: clear sky → SAFE
Bottom edge: pool water → SAFE
→ Both truly safe (sky and water are generic).
→ "Extend the image from the bottom and top. Try to invent as little as possible."

Example 6: A 1920x1080 wide photo of a person in a room. AR=1.78, needs a LOT of height.
Top edge: ceiling with specific lamp → DANGEROUS
Bottom edge: carpet leading to furniture → DANGEROUS
Left edge: wall with painting → DANGEROUS
Right edge: window with curtains → DANGEROUS
→ All edges are dangerous. Fall back to recreate.
→ "Recreate this image in 9:16 portrait. Super realistic, as similar as possible \
to the original. Try to invent as little as possible."

Example 7: A 400x1200 very tall narrow photo of food on a plate. AR=0.33, needs width.
Left edge: dark blurred background → SAFE
Right edge: dark blurred background → SAFE
→ Both sides are safe generic dark background.
→ "Extend the image from the left and right. Try to invent as little as possible."

Example 8: A 1200x1400 near-square photo of a venue interior. AR=0.857, needs some height.
Top edge: ceiling with chandeliers and specific decor → DANGEROUS
Bottom edge: tiled floor → SAFE
→ Only bottom is safe. Do NOT extend top just because you need more height.
→ "Extend the image from the bottom. Try to invent as little as possible."

Example 9: A 1400x1000 landscape photo of food on a tablecloth. AR=1.4, needs a lot of height.
Top edge: more tablecloth, no objects → SAFE
Bottom edge: more tablecloth, no objects → SAFE
→ Both edges are just plain tablecloth — truly generic. And the image needs \
a lot of height. Extending both sides splits the work and produces less distortion.
→ "Extend the image from the bottom and top. Try to invent as little as possible."

Example 10: A 1400x1100 landscape photo of a beach bar. AR=1.27, needs more height.
Top edge: open sky with some clouds → SAFE
Bottom edge: sandy ground → SAFE
→ Both safe, and the image needs a good amount of height (AR 1.27 → 0.56). \
Both directions can help. Sky and sand are both truly generic.
→ "Extend the image from the bottom and top. Try to invent as little as possible."

Example 11: A 1200x1400 photo of a drink on a bar counter. AR=0.857, needs some height.
Top edge: dark blurred background → SAFE
Bottom edge: bar counter surface → SAFE
→ Both edges are safe. Even though it only needs moderate height, using both \
directions means less extension per side.
→ "Extend the image from the bottom and top. Try to invent as little as possible."

Example 12: A 1000x1400 photo of a plate from above. AR=0.714, needs some height.
Top edge: table surface continues → SAFE
Bottom edge: person's hands holding cutlery → DANGEROUS
→ Only top is safe. Extend top only.
→ "Extend the image from the top. Try to invent as little as possible."

Respond with JSON containing two fields:
- "reasoning": Brief explanation of what you see at each edge (SAFE/DANGEROUS) and why you chose this strategy.
- "prompt": Your chosen prompt, copied exactly from the options above (including the "Try to invent as little as possible." part)."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of edge analysis and strategy choice",
        },
        "prompt": {
            "type": "string",
            "description": "The chosen extend/recreate prompt",
        },
    },
    "required": ["reasoning", "prompt"],
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
    gcs_path = f"test/extend_bottom/{basename}"
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


def call_llm_advisor(openai_provider: OpenAIProvider, image_bytes: bytes, w: int, h: int, ar: float):
    """Call GPT 5.4 with image + advisor prompt. Returns (prompt, reasoning)."""
    mime = detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt_text = ADVISOR_PROMPT.format(width=w, height=h, current_ar=ar)

    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "text": prompt_text},
    ]}]

    result = openai_provider.call(
        GPT_MODEL, messages, temperature=0.1,
        responseSchema=RESPONSE_SCHEMA,
    )

    raw = (result.get("text") or "").strip()
    try:
        parsed = json.loads(raw)
        return parsed.get("prompt", ""), parsed.get("reasoning", "")
    except json.JSONDecodeError:
        return raw, ""


# ---------------------------------------------------------------------------
# Provider: Kie.ai
# ---------------------------------------------------------------------------

def kie_regen(kie_key: str, ref_url: str, prompt: str, aspect_ratio: str = "9:16") -> bytes:
    """Generate via Kie.ai NB2. Returns image bytes."""
    headers = {"Authorization": f"Bearer {kie_key}", "Content-Type": "application/json"}
    payload = {
        "model": KIE_MODEL,
        "input": {
            "prompt": prompt,
            "image_input": [ref_url],
            "aspect_ratio": aspect_ratio,
            "resolution": "1K",
            "output_format": "jpg",
        },
    }
    print(f"  Creating Kie task...")
    resp = requests.post(f"{KIE_BASE}/api/v1/jobs/createTask", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    task_data = resp.json()
    task_id = (task_data.get("data") or {}).get("taskId") or task_data.get("taskId")
    print(f"  Task ID: {task_id}")

    t0 = time.time()
    while True:
        time.sleep(15)
        elapsed = time.time() - t0
        poll_resp = requests.get(
            f"{KIE_BASE}/api/v1/jobs/recordInfo?taskId={task_id}",
            headers=headers, timeout=15,
        )
        poll_resp.raise_for_status()
        poll_data = poll_resp.json().get("data", {})
        state = poll_data.get("state", "unknown")
        print(f"    Polling... {elapsed:.0f}s state={state}")

        if state in ("completed", "success"):
            result_json = json.loads(poll_data.get("resultJson", "{}"))
            result_urls = result_json.get("resultUrls", [])
            if not result_urls:
                raise RuntimeError(f"Kie returned no result URLs: {poll_data}")
            result_url = result_urls[0]
            print(f"    Done in {elapsed:.1f}s -> {result_url}")
            return download_image(result_url)

        if state in ("failed", "fail", "cancelled"):
            fail_msg = poll_data.get("failMsg", "unknown error")
            raise RuntimeError(f"Kie task {state}: {fail_msg}")
        if elapsed > 300:
            raise RuntimeError("Kie task timed out after 5 minutes")


# ---------------------------------------------------------------------------
# Provider: Vertex AI
# ---------------------------------------------------------------------------

def vertex_regen(image_bytes: bytes, prompt: str, aspect_ratio: str = "9:16") -> bytes:
    """Generate via Vertex AI direct REST. Returns image bytes."""
    api_key = os.environ.get("VERTEX_AI_API_KEY")
    if not api_key:
        raise RuntimeError("VERTEX_AI_API_KEY not set")

    mime = detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    endpoint = (
        f"https://aiplatform.googleapis.com/v1/projects/your-gcp-project"
        f"/locations/global/publishers/google/models/{VERTEX_MODEL}"
        f":generateContent?key={api_key}"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": mime, "data": b64}},
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {"aspectRatio": aspect_ratio},
        },
    }

    max_retries = 5
    for attempt in range(max_retries):
        resp = requests.post(endpoint, json=payload, timeout=180)
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"    Rate limit 429, waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        raise RuntimeError(f"Vertex AI rate limited after {max_retries} attempts")

    result = resp.json()
    for part in result.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])

    raise RuntimeError(f"Vertex AI returned no image: {json.dumps(result)[:300]}")


# ---------------------------------------------------------------------------
# Provider: fal.ai
# ---------------------------------------------------------------------------

def fal_regen(fal_key: str, ref_url: str, prompt: str, aspect_ratio: str = "9:16") -> bytes:
    """Generate via fal.ai NB2. Returns image bytes."""
    headers = {"Authorization": f"Key {fal_key}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "image_urls": [ref_url],
        "aspect_ratio": aspect_ratio,
        "output_format": "png",
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        resp = requests.post(FAL_ENDPOINT, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        request_id = data.get("request_id")
        print(f"  request_id: {request_id}")

        response_url = data.get("response_url")
        status_url = data.get("status_url")

        while True:
            time.sleep(2)
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
        images = result.get("images") or result.get("output", {}).get("images", [])
        if images:
            url = images[0].get("url") or images[0]
            print(f"    Done -> {url}")
            return download_image(url)

        print(f"    fal.ai returned no images (attempt {attempt}), retrying...")
        time.sleep(3)

    raise RuntimeError(f"fal.ai returned no images after {max_attempts} attempts")


# ---------------------------------------------------------------------------
# Process one image
# ---------------------------------------------------------------------------

def process_image(image_input: str, provider: str, openai_provider: OpenAIProvider,
                  gcs: GCSStorageService, output_dir: str):
    """Run Step 1 (advisor) + Step 2 (generation) on a single image."""

    is_url = image_input.startswith("http://") or image_input.startswith("https://")

    if is_url:
        print(f"Downloading: {image_input}")
        image_bytes = download_image(image_input)
        ref_url = image_input
        image_name = image_input.split("/")[-1].split("?")[0] or "image"
    else:
        image_path = os.path.abspath(image_input)
        if not os.path.isfile(image_path):
            print(f"ERROR: File not found: {image_path}")
            return
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        image_name = os.path.basename(image_path)
        print("Uploading to GCS...")
        ref_url = upload_local_image(gcs, image_path)
        print(f"Reference URL: {ref_url}")

    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    ar = w / h if h > 0 else 1.0
    stem = os.path.splitext(image_name)[0].replace(" ", "_")
    run_id = time.strftime("%H%M%S")

    provider_label = {
        "kie": f"Kie.ai ({KIE_MODEL})",
        "vertex": f"Vertex AI ({VERTEX_MODEL})",
        "fal": "fal.ai NB2",
    }[provider]

    print(f"\nImage: {image_name}  {w}x{h}  AR={ar:.3f}")
    print(f"Target AR: {TARGET_AR:.4f} (9:16)")
    print(f"Provider: {provider_label}\n")

    # =======================================================================
    # Step 1: LLM Advisor (GPT 5.4)
    # =======================================================================
    print("=" * 60)
    print("Step 1: LLM Advisor (GPT 5.4)")
    print("=" * 60)

    t0 = time.time()
    prompt, reasoning = call_llm_advisor(openai_provider, image_bytes, w, h, ar)
    elapsed = time.time() - t0

    print(f"  Time: {elapsed:.1f}s")
    print(f"  Reasoning: {reasoning}")
    print(f"  Prompt: {prompt}")

    # =======================================================================
    # Step 2: Execute
    # =======================================================================
    print(f"\n{'=' * 60}")

    strategy = "recreate" if "recreate" in prompt.lower() else "extend"
    print(f"Step 2: Execute — {strategy}")
    print(f"{'=' * 60}")

    print(f"  Prompt: {prompt}")
    print(f"  Provider: {provider_label}")
    t0 = time.time()

    if provider == "kie":
        kie_key = os.environ.get("KIE_API")
        if not kie_key:
            print("ERROR: KIE_API not set"); return
        result_bytes = kie_regen(kie_key, ref_url, prompt)

    elif provider == "vertex":
        result_bytes = vertex_regen(image_bytes, prompt)

    elif provider == "fal":
        fal_key = os.environ.get("FAL_KEY")
        if not fal_key:
            print("ERROR: FAL_KEY not set"); return
        result_bytes = fal_regen(fal_key, ref_url, prompt)

    elapsed = time.time() - t0
    result_img = Image.open(io.BytesIO(result_bytes))
    rw, rh = result_img.size
    result_ar = rw / rh
    print(f"\n  Result: {rw}x{rh}  AR={result_ar:.3f}  (took {elapsed:.1f}s)")

    # --- Save ---
    result_path = os.path.join(output_dir, f"{stem}_{strategy}_{provider}_{run_id}.jpg")
    result_img.convert("RGB").save(result_path, format="JPEG", quality=92)
    print(f"  Saved: {result_path}")

    # --- Comparison ---
    header = f"{image_name} {w}x{h} AR={ar:.3f} | {strategy} ({provider}) -> {rw}x{rh} AR={result_ar:.3f}"
    comp = make_comparison(image_bytes, result_bytes, header)
    comp_path = os.path.join(output_dir, f"{stem}_comparison_{provider}_{run_id}.jpg")
    comp.save(comp_path, format="JPEG", quality=92)
    print(f"  Comparison: {comp_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Smart extend: LLM advisor + NB2 execution")
    parser.add_argument("images", nargs="+", help="Local file paths or HTTP(S) URLs")
    parser.add_argument("--provider", choices=["kie", "vertex", "fal"], default="kie",
                        help="Image gen provider (default: kie)")
    parser.add_argument("--output-dir", help="Override output directory")
    args = parser.parse_args()

    output_dir = args.output_dir or OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    config = Config()

    # GPT 5.4 via OpenAI
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set")
        sys.exit(1)
    openai_provider = OpenAIProvider(api_key=api_key)

    # GCS for uploading local images as reference
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name=config.GCS_BUCKET_NAME,
        folder_path="Comp/Final_Video",
    )

    print(f"Output dir: {output_dir}")
    print(f"Provider: {args.provider}")
    print(f"Images: {len(args.images)}\n")

    for image_input in args.images:
        print(f"\n{'#' * 70}")
        try:
            process_image(image_input, args.provider, openai_provider, gcs, output_dir)
        except Exception as e:
            print(f"  ERROR: {e}")
        print(f"{'#' * 70}\n")


if __name__ == "__main__":
    main()
