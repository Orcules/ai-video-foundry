"""Unit test for Step 1 only: LLM Advisor decides extend strategy.

No image generation — just the LLM call to see what it recommends.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_extend_advisor path/to/image.jpg
    python -m tvd_pipeline.dev_scripts.unit_tests.test_extend_advisor https://storage.googleapis.com/...jpg
"""

import base64
import io
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
from tvd_pipeline.services.providers.openai_provider import OpenAIProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GPT_MODEL = "gpt-5.4"

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Test LLM advisor only (no image gen)")
    parser.add_argument("images", nargs="+", help="Local file paths or HTTP(S) URLs")
    args = parser.parse_args()

    config = Config()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set")
        sys.exit(1)
    openai_provider = OpenAIProvider(api_key=api_key)

    for image_input in args.images:
        print(f"\n{'=' * 60}")

        is_url = image_input.startswith("http://") or image_input.startswith("https://")
        if is_url:
            image_name = image_input.split("/")[-1].split("?")[0] or "image"
            image_bytes = download_image(image_input)
        else:
            image_path = os.path.abspath(image_input)
            if not os.path.isfile(image_path):
                print(f"ERROR: File not found: {image_path}")
                continue
            image_name = os.path.basename(image_path)
            with open(image_path, "rb") as f:
                image_bytes = f.read()

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        ar = w / h if h > 0 else 1.0

        print(f"Image: {image_name}  {w}x{h}  AR={ar:.3f}")

        prompt = ADVISOR_PROMPT.format(width=w, height=h, current_ar=ar)

        mime = detect_mime(image_bytes)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}]

        t0 = time.time()
        result = openai_provider.call(
            GPT_MODEL, messages, temperature=0.1,
            responseSchema=RESPONSE_SCHEMA,
        )
        elapsed = time.time() - t0

        import json as _json
        raw = (result.get("text") or "").strip()
        try:
            parsed = _json.loads(raw)
            reasoning = parsed.get("reasoning", "")
            prompt_text = parsed.get("prompt", "")
        except _json.JSONDecodeError:
            reasoning = ""
            prompt_text = raw

        print(f"  Time: {elapsed:.1f}s")
        print(f"  Reasoning: {reasoning}")
        print(f"  Prompt: {prompt_text}")


if __name__ == "__main__":
    main()
