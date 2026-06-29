"""Smart crop safety v2: 4-prompt architecture with validation convergence.

Two separate LLM paths based on AR zone, each with its own validation step:

  MUST CROP (AR > 1.2):
    1. Prompt B  -- "you MUST crop, find the best vertical strip"
    2. Crop image
    3. Prompt D  -- validate: send original + cropped, "is this good?"
    4. If not good -> Prompt B retry with feedback, crop again, done

  GRAY ZONE (0.5625 < AR < 1.2):
    1. Prompt A  -- "should I crop? + where is focal point?"
    2. If should_crop=false -> KEEP ORIGINAL, done
    3. If should_crop=true -> crop, then:
    4. Prompt C  -- validate: send original + cropped, "is this good?"
    5. If not good -> Prompt A retry with feedback
    6. Re-evaluate should_crop (may flip to false)

  SKIP (AR ~ 0.5625): no LLM call

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_smart_crop_safety
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
FLYMORE_DIR = os.path.normpath(os.path.join(
    script_dir, "..", "..", "..", "..", "api_pipeline", "documents",
    "test_scripts", "flymore_assets",
))

OUTPUT_DIR = os.path.join(script_dir, "test_output", "smart_crop_safety")

TARGET_AR = 9 / 16  # 0.5625
MUST_CROP_AR = 1.2
GEMINI_MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 5  # JSON parse retries per LLM call

# ===================================================================
# PROMPT B -- Must-crop: find the best vertical strip
# ===================================================================
PROMPT_B_MUST_CROP = """\
This is a LANDSCAPE image that MUST be cropped to 9:16 PORTRAIT format \
for a social media video. Cropping is mandatory -- your job is to find \
the BEST possible vertical strip to keep.

Think carefully about what makes the most compelling 9:16 crop:
- Where are the PEOPLE? Especially faces, actions, expressions, splashes
- Where is the ACTION or MOTION happening?
- What is the most visually dynamic or interesting part?
- A good crop tells a story even with the sides cut off
- Prefer the part with human activity over empty structures

Return the center of the BEST vertical strip as normalized coordinates:
- focus_x: 0.0 = left edge, 0.5 = center, 1.0 = right edge
- focus_y: 0.0 = top edge, 0.5 = center, 1.0 = bottom edge
- description: what you chose and why

IMPORTANT: Do NOT default to center (0.5, 0.5). The best content is \
rarely dead center. Look at the actual image and find where the action is."""

# ===================================================================
# PROMPT D -- Must-crop validation: compare original vs cropped
# ===================================================================
PROMPT_D_MUST_CROP_VALIDATE = """\
I cropped a landscape image to 9:16 portrait. You are seeing both versions.

The FIRST image is the ORIGINAL landscape.
The SECOND image is the CROPPED 9:16 portrait result.

Compare them carefully:
- Did the crop capture the most important/interesting content?
- Was any critical subject (person, action, key element) cut off \
that could have been included with a DIFFERENT crop position?
- Is there a clearly better vertical strip we should have used instead?
- Look especially for people, faces, or action that got cut off at the edges

If the crop is good and captures the best content: set is_good to true.
If a better crop position exists: set is_good to false, explain what \
was missed in reason, and provide better focal coordinates."""

# ===================================================================
# PROMPT A -- Gray zone: should I crop or keep original?
# ===================================================================
PROMPT_A_GRAY_ZONE = """\
Look at this image carefully. It is close to portrait but not exactly 9:16.

I need to decide: should I CROP it to exact 9:16, or KEEP the original \
as-is (it will have small black bars on the sides in the video)?

Evaluate the content layout:
- If important content is concentrated and cropping would NOT lose \
anything meaningful -> set should_crop to true
- If important content spans the full width/height and cropping \
would cut off subjects, text, products, or visual elements \
-> set should_crop to false (small black bars are acceptable)

If should_crop is true, identify the best focal point for cropping:
- focus_x: 0.0 = left, 0.5 = center, 1.0 = right
- focus_y: 0.0 = top, 0.5 = center, 1.0 = bottom

If should_crop is false, still return focus_x=0.5 and focus_y=0.5."""

# ===================================================================
# PROMPT C -- Gray zone validation: compare original vs cropped
# ===================================================================
PROMPT_C_GRAY_ZONE_VALIDATE = """\
I cropped a near-square image to exact 9:16 portrait. You are seeing \
both versions.

The FIRST image is the ORIGINAL image.
The SECOND image is the CROPPED 9:16 result.

Compare them carefully:
- Did the crop lose any important content (subjects, text, products)?
- Does the cropped version still tell the same visual story?
- Would it have been better to KEEP the original as-is (with small \
black bars) instead of cropping?

If the crop is good and no important content was lost: set is_good to true.
If the crop lost important content or a different position would be \
better: set is_good to false, explain what was lost in reason, and \
provide better focal coordinates (or 0.5/0.5 if it should not be cropped)."""

# ===================================================================
# Schemas
# ===================================================================
MUST_CROP_SCHEMA = {
    "type": "object",
    "properties": {
        "focus_x": {
            "type": "number",
            "description": "Horizontal center of best crop strip (0.0=left, 1.0=right)",
        },
        "focus_y": {
            "type": "number",
            "description": "Vertical center of best crop strip (0.0=top, 1.0=bottom)",
        },
        "description": {
            "type": "string",
            "description": "What content you chose to keep and why",
        },
    },
    "required": ["focus_x", "focus_y", "description"],
    "additionalProperties": False,
}

GRAY_ZONE_SCHEMA = {
    "type": "object",
    "properties": {
        "should_crop": {
            "type": "boolean",
            "description": "True=safe to crop to 9:16, False=keep original (black bars OK)",
        },
        "focus_x": {
            "type": "number",
            "description": "Horizontal center of focal region (0.0=left, 1.0=right)",
        },
        "focus_y": {
            "type": "number",
            "description": "Vertical center of focal region (0.0=top, 1.0=bottom)",
        },
        "description": {
            "type": "string",
            "description": "Content description and crop safety reasoning",
        },
    },
    "required": ["should_crop", "focus_x", "focus_y", "description"],
    "additionalProperties": False,
}

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_good": {
            "type": "boolean",
            "description": "True if the crop is good, False if a better crop exists",
        },
        "new_focus_x": {
            "type": "number",
            "description": "Suggested better horizontal focal point (0.0-1.0) if is_good=false",
        },
        "new_focus_y": {
            "type": "number",
            "description": "Suggested better vertical focal point (0.0-1.0) if is_good=false",
        },
        "reason": {
            "type": "string",
            "description": "Why the crop is good, or what important content was missed",
        },
    },
    "required": ["is_good", "new_focus_x", "new_focus_y", "reason"],
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


def classify_zone(ar: float) -> str:
    if abs(ar - TARGET_AR) / TARGET_AR < 0.01:
        return "SKIP"
    if ar > MUST_CROP_AR:
        return "MUST_CROP"
    return "GRAY"


def content_loss_pct(ar: float) -> float:
    if ar <= TARGET_AR:
        return (1.0 - ar / TARGET_AR) * 100
    return (1.0 - TARGET_AR / ar) * 100


def pil_to_jpeg_bytes(img: Image.Image) -> bytes:
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


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


def _b64_image_part(image_bytes: bytes) -> dict:
    mime = detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _parse_regex(raw: str, need_should_crop: bool = False) -> dict | None:
    """Regex fallback for malformed JSON."""
    fx = re.search(r'"focus_x"\s*:\s*([\d.]+)', raw)
    fy = re.search(r'"focus_y"\s*:\s*([\d.]+)', raw)
    if not (fx and fy):
        return None
    result = {
        "focus_x": max(0.0, min(1.0, float(fx.group(1)))),
        "focus_y": max(0.0, min(1.0, float(fy.group(1)))),
        "description": "",
    }
    desc = re.search(r'"description"\s*:\s*"([^"]*)', raw)
    if desc:
        result["description"] = desc.group(1)
    if need_should_crop:
        sc = re.search(r'"should_crop"\s*:\s*(true|false)', raw, re.IGNORECASE)
        result["should_crop"] = sc.group(1).lower() == "true" if sc else True
    return result


def _parse_validation_regex(raw: str) -> dict | None:
    ig = re.search(r'"is_good"\s*:\s*(true|false)', raw, re.IGNORECASE)
    if not ig:
        return None
    fx = re.search(r'"new_focus_x"\s*:\s*([\d.]+)', raw)
    fy = re.search(r'"new_focus_y"\s*:\s*([\d.]+)', raw)
    reason = re.search(r'"reason"\s*:\s*"([^"]*)', raw)
    return {
        "is_good": ig.group(1).lower() == "true",
        "new_focus_x": max(0.0, min(1.0, float(fx.group(1)))) if fx else 0.5,
        "new_focus_y": max(0.0, min(1.0, float(fy.group(1)))) if fy else 0.5,
        "reason": reason.group(1) if reason else "",
    }


# ---------------------------------------------------------------------------
# LLM call wrappers with retry + regex fallback
# ---------------------------------------------------------------------------

def _call_llm_with_retry(vertex, messages, schema, clamp_fn):
    """Call LLM up to MAX_RETRIES times, with regex fallback."""
    last_err = None
    last_raw = ""
    for attempt in range(MAX_RETRIES):
        result = vertex.call(
            GEMINI_MODEL, messages,
            temperature=0.1, max_tokens=1000,
            responseSchema=schema,
        )
        raw = (result.get("text") or "").strip()
        last_raw = raw
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        try:
            parsed = json.loads(raw)
            return clamp_fn(parsed)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            last_err = e
            print(f"      Parse attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
    return last_raw, last_err  # caller handles regex fallback


def call_must_crop(vertex, image_bytes, feedback=None):
    """Prompt B: find best crop strip. Returns {focus_x, focus_y, description}."""
    prompt = PROMPT_B_MUST_CROP
    if feedback:
        prompt += (
            f"\n\nIMPORTANT -- PREVIOUS ATTEMPT WAS REJECTED:\n"
            f"{feedback}\n"
            f"Choose a DIFFERENT focal point that addresses this issue."
        )
    messages = [{"role": "user", "content": [
        _b64_image_part(image_bytes),
        {"type": "text", "text": prompt},
    ]}]

    def clamp(p):
        p["focus_x"] = max(0.0, min(1.0, float(p["focus_x"])))
        p["focus_y"] = max(0.0, min(1.0, float(p["focus_y"])))
        return p

    result = _call_llm_with_retry(vertex, messages, MUST_CROP_SCHEMA, clamp)
    if isinstance(result, dict):
        return result
    raw, err = result
    parsed = _parse_regex(raw)
    if parsed:
        print("      Recovered via regex fallback")
        return parsed
    raise ValueError(f"Could not parse after {MAX_RETRIES} attempts: {err}")


def call_gray_zone(vertex, image_bytes, feedback=None):
    """Prompt A: should I crop + where. Returns {should_crop, focus_x, focus_y, description}."""
    prompt = PROMPT_A_GRAY_ZONE
    if feedback:
        prompt += (
            f"\n\nIMPORTANT -- PREVIOUS CROP ATTEMPT WAS REJECTED:\n"
            f"{feedback}\n"
            f"Please reconsider whether cropping is appropriate, or choose "
            f"a different focal point."
        )
    messages = [{"role": "user", "content": [
        _b64_image_part(image_bytes),
        {"type": "text", "text": prompt},
    ]}]

    def clamp(p):
        p["focus_x"] = max(0.0, min(1.0, float(p["focus_x"])))
        p["focus_y"] = max(0.0, min(1.0, float(p["focus_y"])))
        p["should_crop"] = bool(p["should_crop"])
        return p

    result = _call_llm_with_retry(vertex, messages, GRAY_ZONE_SCHEMA, clamp)
    if isinstance(result, dict):
        return result
    raw, err = result
    parsed = _parse_regex(raw, need_should_crop=True)
    if parsed:
        print("      Recovered via regex fallback")
        return parsed
    raise ValueError(f"Could not parse after {MAX_RETRIES} attempts: {err}")


def call_validate(vertex, original_bytes, cropped_bytes, is_must_crop):
    """Prompt C/D: validate crop. Returns {is_good, new_focus_x, new_focus_y, reason}."""
    prompt = PROMPT_D_MUST_CROP_VALIDATE if is_must_crop else PROMPT_C_GRAY_ZONE_VALIDATE
    messages = [{"role": "user", "content": [
        _b64_image_part(original_bytes),
        {"type": "text", "text": "IMAGE 1 -- ORIGINAL (before crop):"},
        _b64_image_part(cropped_bytes),
        {"type": "text", "text": "IMAGE 2 -- CROPPED to 9:16 (after crop):"},
        {"type": "text", "text": prompt},
    ]}]

    def clamp(p):
        p["is_good"] = bool(p["is_good"])
        p["new_focus_x"] = max(0.0, min(1.0, float(p["new_focus_x"])))
        p["new_focus_y"] = max(0.0, min(1.0, float(p["new_focus_y"])))
        return p

    result = _call_llm_with_retry(vertex, messages, VALIDATION_SCHEMA, clamp)
    if isinstance(result, dict):
        return result
    raw, err = result
    parsed = _parse_validation_regex(raw)
    if parsed:
        print("      Recovered via regex fallback")
        return parsed
    raise ValueError(f"Could not parse validation after {MAX_RETRIES} attempts: {err}")


# ---------------------------------------------------------------------------
# Comparison image builder
# ---------------------------------------------------------------------------

def make_comparison(original_bytes, crop_box, action, header_text):
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

    color = (0, 255, 0) if action == "CROP" else (255, 165, 0)
    ImageDraw.Draw(orig_r).rectangle(sb, outline=color, width=3)

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
    print(f"Model: {GEMINI_MODEL}  |  Must-crop AR > {MUST_CROP_AR}")
    print()

    results = []

    for source, image_name, image_path in image_entries:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        ar = w / h if h > 0 else 1.0
        zone = classify_zone(ar)
        loss = content_loss_pct(ar)
        stem = os.path.splitext(image_name)[0]
        prefix = f"{source}_{stem}"

        print(f"{'=' * 70}")
        print(f"[{source}] {image_name}  {w}x{h}  AR={ar:.3f}  Zone={zone}  Loss={loss:.0f}%")
        print(f"{'=' * 70}")

        # -- Save original copy to output --
        orig_out = os.path.join(OUTPUT_DIR, f"{prefix}_original.jpg")
        img_rgb = img.convert("RGB") if img.mode in ("RGBA", "P") else img
        img_rgb.save(orig_out, format="JPEG", quality=92)

        if zone == "SKIP":
            print(f"  Already 9:16 -- no LLM call")
            print(f"  Original: {orig_out}")
            results.append({
                "source": source, "image": image_name, "size": f"{w}x{h}",
                "ar": ar, "zone": zone, "action": "SKIP", "loss": loss,
                "original_path": image_path, "original_copy": orig_out,
            })
            print()
            continue

        # ==============================================================
        # MUST CROP path
        # ==============================================================
        if zone == "MUST_CROP":
            # Step 1: Prompt B -- find best strip
            print(f"  [Step 1] Prompt B: find best vertical strip...")
            t0 = time.time()
            try:
                resp = call_must_crop(vertex, image_bytes)
            except Exception as e:
                print(f"  ERROR: {e}")
                results.append({
                    "source": source, "image": image_name, "size": f"{w}x{h}",
                    "ar": ar, "zone": zone, "action": "ERROR", "loss": loss,
                    "error": str(e), "original_path": image_path,
                    "original_copy": orig_out,
                })
                print()
                continue
            fx, fy = resp["focus_x"], resp["focus_y"]
            print(f"    Focal: ({fx:.3f}, {fy:.3f})  [{time.time()-t0:.1f}s]")
            print(f"    Desc: {resp['description']}")

            # Step 2: Crop
            box = crop_around_focus(w, h, fx, fy)
            cropped_img = img.crop(box)
            cropped_bytes = pil_to_jpeg_bytes(cropped_img)

            # Step 3: Prompt D -- validate
            print(f"  [Step 2] Prompt D: validating crop...")
            t0 = time.time()
            try:
                val = call_validate(vertex, image_bytes, cropped_bytes, is_must_crop=True)
            except Exception as e:
                print(f"    Validation error: {e} -- using initial crop")
                val = {"is_good": True, "reason": f"validation failed: {e}"}
            print(f"    Result: {'GOOD' if val['is_good'] else 'NOT GOOD'}  [{time.time()-t0:.1f}s]")
            print(f"    Reason: {val['reason']}")

            validated = val["is_good"]

            if not val["is_good"]:
                # Step 4: Retry Prompt B with feedback
                feedback = (
                    f"The previous crop at focal point ({fx:.3f}, {fy:.3f}) was rejected. "
                    f"Issue: {val['reason']}. "
                    f"The validator suggested ({val.get('new_focus_x', 0.5):.3f}, "
                    f"{val.get('new_focus_y', 0.5):.3f}) as a better position."
                )
                print(f"  [Step 3] Prompt B retry with feedback...")
                t0 = time.time()
                try:
                    resp2 = call_must_crop(vertex, image_bytes, feedback=feedback)
                    fx, fy = resp2["focus_x"], resp2["focus_y"]
                    print(f"    New focal: ({fx:.3f}, {fy:.3f})  [{time.time()-t0:.1f}s]")
                    print(f"    Desc: {resp2['description']}")
                    box = crop_around_focus(w, h, fx, fy)
                    cropped_img = img.crop(box)
                    cropped_bytes = pil_to_jpeg_bytes(cropped_img)
                except Exception as e:
                    print(f"    Retry error: {e} -- using initial crop")

            # Save outputs
            action = "CROP"
            crop_path = os.path.join(OUTPUT_DIR, f"{prefix}_cropped.jpg")
            if cropped_img.mode in ("RGBA", "P"):
                cropped_img = cropped_img.convert("RGB")
            cropped_img.save(crop_path, format="JPEG", quality=92)

            header = f"Zone: {zone} | Action: {action} | Validated: {validated}"
            comp = make_comparison(image_bytes, box, action, header)
            comp_path = os.path.join(OUTPUT_DIR, f"{prefix}_comparison.jpg")
            comp.save(comp_path, format="JPEG", quality=92)

            print(f"  Action: CROP ({'validated' if validated else 'corrected'})")
            print(f"  Original: {orig_out}")
            print(f"  Cropped:  {crop_path}")
            print(f"  Compare:  {comp_path}")

            results.append({
                "source": source, "image": image_name, "size": f"{w}x{h}",
                "ar": ar, "zone": zone, "action": action, "loss": loss,
                "validated": validated, "focus_x": fx, "focus_y": fy,
                "original_path": image_path, "original_copy": orig_out,
                "cropped_path": crop_path, "comparison_path": comp_path,
            })
            print()
            continue

        # ==============================================================
        # GRAY ZONE path
        # ==============================================================
        # Step 1: Prompt A -- should I crop?
        print(f"  [Step 1] Prompt A: should I crop?...")
        t0 = time.time()
        try:
            resp = call_gray_zone(vertex, image_bytes)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({
                "source": source, "image": image_name, "size": f"{w}x{h}",
                "ar": ar, "zone": zone, "action": "ERROR", "loss": loss,
                "error": str(e), "original_path": image_path,
                "original_copy": orig_out,
            })
            print()
            continue

        should_crop = resp["should_crop"]
        fx, fy = resp["focus_x"], resp["focus_y"]
        print(f"    should_crop: {should_crop}  |  Focal: ({fx:.3f}, {fy:.3f})  [{time.time()-t0:.1f}s]")
        print(f"    Desc: {resp['description']}")

        if not should_crop:
            action = "KEEP_ORIGINAL"
            print(f"  Action: KEEP_ORIGINAL (LLM said don't crop)")
            print(f"  Original: {orig_out}")
            results.append({
                "source": source, "image": image_name, "size": f"{w}x{h}",
                "ar": ar, "zone": zone, "action": action, "loss": loss,
                "should_crop": False, "original_path": image_path,
                "original_copy": orig_out,
            })
            print()
            continue

        # Step 2: Crop
        box = crop_around_focus(w, h, fx, fy)
        cropped_img = img.crop(box)
        cropped_bytes = pil_to_jpeg_bytes(cropped_img)

        # Step 3: Prompt C -- validate
        print(f"  [Step 2] Prompt C: validating crop...")
        t0 = time.time()
        try:
            val = call_validate(vertex, image_bytes, cropped_bytes, is_must_crop=False)
        except Exception as e:
            print(f"    Validation error: {e} -- using initial crop")
            val = {"is_good": True, "reason": f"validation failed: {e}"}
        print(f"    Result: {'GOOD' if val['is_good'] else 'NOT GOOD'}  [{time.time()-t0:.1f}s]")
        print(f"    Reason: {val['reason']}")

        validated = val["is_good"]

        if not val["is_good"]:
            # Step 4: Retry Prompt A with feedback
            feedback = (
                f"The previous crop at focal point ({fx:.3f}, {fy:.3f}) was rejected. "
                f"Issue: {val['reason']}. "
                f"Reconsider: maybe the image should NOT be cropped at all, or "
                f"try a different focal point."
            )
            print(f"  [Step 3] Prompt A retry with feedback...")
            t0 = time.time()
            try:
                resp2 = call_gray_zone(vertex, image_bytes, feedback=feedback)
                should_crop = resp2["should_crop"]
                fx, fy = resp2["focus_x"], resp2["focus_y"]
                print(f"    should_crop: {should_crop}  |  New focal: ({fx:.3f}, {fy:.3f})  [{time.time()-t0:.1f}s]")
                print(f"    Desc: {resp2['description']}")

                if not should_crop:
                    # Changed mind -- keep original
                    action = "KEEP_ORIGINAL"
                    print(f"  Action: KEEP_ORIGINAL (LLM flipped after validation)")
                    print(f"  Original: {orig_out}")
                    results.append({
                        "source": source, "image": image_name, "size": f"{w}x{h}",
                        "ar": ar, "zone": zone, "action": action, "loss": loss,
                        "should_crop": False, "validated": False,
                        "original_path": image_path, "original_copy": orig_out,
                    })
                    print()
                    continue

                # Re-crop with new focal point
                box = crop_around_focus(w, h, fx, fy)
                cropped_img = img.crop(box)
                cropped_bytes = pil_to_jpeg_bytes(cropped_img)

            except Exception as e:
                print(f"    Retry error: {e} -- using initial crop")

        # Save outputs
        action = "CROP"
        crop_path = os.path.join(OUTPUT_DIR, f"{prefix}_cropped.jpg")
        if cropped_img.mode in ("RGBA", "P"):
            cropped_img = cropped_img.convert("RGB")
        cropped_img.save(crop_path, format="JPEG", quality=92)

        header = f"Zone: {zone} | should_crop={should_crop} | Validated: {validated}"
        comp = make_comparison(image_bytes, box, action, header)
        comp_path = os.path.join(OUTPUT_DIR, f"{prefix}_comparison.jpg")
        comp.save(comp_path, format="JPEG", quality=92)

        print(f"  Action: CROP ({'validated' if validated else 'corrected'})")
        print(f"  Original: {orig_out}")
        print(f"  Cropped:  {crop_path}")
        print(f"  Compare:  {comp_path}")

        results.append({
            "source": source, "image": image_name, "size": f"{w}x{h}",
            "ar": ar, "zone": zone, "action": action, "loss": loss,
            "should_crop": True, "validated": validated,
            "focus_x": fx, "focus_y": fy,
            "original_path": image_path, "original_copy": orig_out,
            "cropped_path": crop_path, "comparison_path": comp_path,
        })
        print()

    # --- Summary ---
    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"{'Source':<8} {'Image':<35} {'Size':<12} {'AR':>6} {'Loss':>5} "
          f"{'Zone':<10} {'Action':<15} {'Valid':>5}")
    print("-" * 100)

    for r in results:
        v = r.get("validated")
        v_str = "--" if v is None else ("yes" if v else "FIX")
        print(
            f"{r['source']:<8} {r['image']:<35} {r['size']:<12} "
            f"{r['ar']:>6.3f} {r['loss']:>4.0f}% {r['zone']:<10} "
            f"{r['action']:<15} {v_str:>5}"
        )

    # Counts
    actions = {}
    for r in results:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
    print(f"\nActions: {dict(actions)}")

    fixes = sum(1 for r in results if r.get("validated") is False)
    if fixes:
        print(f"Validation corrections: {fixes}")

    print(f"\nOutput: {OUTPUT_DIR}")
    print()

    # Per-image file listing
    print("=" * 100)
    print("FILE LISTING")
    print("=" * 100)
    for r in results:
        print(f"\n  [{r['source']}] {r['image']}  --  {r['action']}")
        print(f"    Original:   {r.get('original_path', '--')}")
        print(f"    Orig copy:  {r.get('original_copy', '--')}")
        if r.get("cropped_path"):
            print(f"    Cropped:    {r['cropped_path']}")
        if r.get("comparison_path"):
            print(f"    Comparison: {r['comparison_path']}")


if __name__ == "__main__":
    main()
