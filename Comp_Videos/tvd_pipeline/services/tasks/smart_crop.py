"""Smart crop: Gemini-guided focal-point cropping to 9:16 portrait.

Uses an LLM vision call to identify the most important region of an image,
then crops to exactly 9:16 around that focal point.  Designed as a
preprocessing step so all downstream analysis and animation sees only the
cropped content that will appear in the final video.

Follows the ``call_fn`` task pattern (see ``image_eval.py``).
"""

import base64
import io
import json
import logging
import re
import time
import uuid
from typing import Callable, Dict

import requests
from PIL import Image

from tvd_pipeline.prompt_loader import get_prompt_loader

logger = logging.getLogger(__name__)

TARGET_AR = 9 / 16  # 0.5625


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_mime(image_bytes: bytes) -> str:
    """Detect MIME type from the first bytes of the image data."""
    if image_bytes[:4] == b"\x89PNG":
        return "image/png"
    if image_bytes[:4] == b"RIFF":
        return "image/webp"
    return "image/jpeg"


def _download_image(image_url: str) -> bytes:
    """Download an image from a URL and return raw bytes."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/*,*/*;q=0.8",
    }
    resp = requests.get(image_url.strip(), headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.content


def _is_already_portrait(width: int, height: int, target_ar: float = TARGET_AR) -> bool:
    """Return True if the image aspect ratio is already 9:16 within 1% tolerance."""
    if height == 0:
        return False
    ar = width / height
    return abs(ar - target_ar) / target_ar < 0.01


FOCAL_POINT_SCHEMA = {
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

_MAX_RETRIES = 2


def _get_focal_point(call_fn: Callable, image_bytes: bytes) -> Dict:
    """Ask the LLM to identify the focal point of an image.

    Uses ``responseSchema`` for structured JSON enforcement.  Retries up to
    ``_MAX_RETRIES`` times on parse failure, with a regex fallback as last
    resort.

    Returns:
        dict with ``focus_x`` (float 0-1), ``focus_y`` (float 0-1),
        ``description`` (str).
    """
    mime = _detect_mime(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt_text = get_prompt_loader().get("shared_smart_crop_user")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
                {"type": "text", "text": prompt_text},
            ],
        }
    ]

    last_err = None
    last_raw = ""

    for attempt in range(_MAX_RETRIES):
        result = call_fn(
            messages,
            temperature=0.1,
            max_tokens=1000,
            responseSchema=FOCAL_POINT_SCHEMA,
        )

        raw = (result.get("text") or "").strip()
        last_raw = raw

        # Strip markdown code fences (some providers still wrap despite schema)
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
            logger.warning(f"Smart crop parse attempt {attempt + 1}/{_MAX_RETRIES} failed: {e}")

    # All retries exhausted — regex fallback on last raw response
    parsed = _parse_focal_regex(last_raw)
    if parsed:
        return parsed

    raise ValueError(f"Could not parse focal point after {_MAX_RETRIES} attempts: {last_err}")


def _parse_focal_regex(raw: str) -> Dict | None:
    """Last-resort regex extraction of focus_x/focus_y from malformed JSON."""
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


def crop_around_focus(
    img_w: int,
    img_h: int,
    focus_x: float,
    focus_y: float,
    target_ar: float = TARGET_AR,
) -> tuple:
    """Calculate crop box for target aspect ratio centred on focal point.

    Args:
        img_w: Image width in pixels.
        img_h: Image height in pixels.
        focus_x: Horizontal focal point (0.0-1.0).
        focus_y: Vertical focal point (0.0-1.0).
        target_ar: Target width/height ratio (default 9/16 = 0.5625).

    Returns:
        Tuple ``(left, top, right, bottom)`` in pixels.
    """
    # For landscape -> portrait: use full height, calculate width
    crop_w = int(img_h * target_ar)
    crop_h = img_h

    if crop_w > img_w:
        # Image is already narrower than target — use full width, crop height
        crop_w = img_w
        crop_h = int(img_w / target_ar)

    # Position crop box centred on focal point, clamped to image bounds
    cx = int(focus_x * img_w)
    cy = int(focus_y * img_h)

    left = max(0, min(cx - crop_w // 2, img_w - crop_w))
    top = max(0, min(cy - crop_h // 2, img_h - crop_h))

    return (left, top, left + crop_w, top + crop_h)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def smart_crop_for_portrait(
    call_fn: Callable,
    image_url: str,
    gcs_storage,
    target_ar: float = TARGET_AR,
) -> Dict:
    """Smart-crop an image to portrait using Gemini focal point detection.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` — LLM dispatch.
        image_url: URL of the image (https or gs://).
        gcs_storage: ``GCSStorageService`` for uploading the cropped result.
        target_ar: Target width/height ratio (default 9/16).

    Returns:
        dict: ``{"url": str, "cropped": bool, "original_url": str,
               "focus_x": float, "focus_y": float, "description": str}``
        On error or skip: ``{"url": original_url, "cropped": False, ...}``
    """
    skip_result = {
        "url": image_url,
        "cropped": False,
        "original_url": image_url,
        "focus_x": 0.5,
        "focus_y": 0.5,
        "description": "",
    }

    try:
        # 1. Download image
        image_bytes = _download_image(image_url)

        # 2. Get dimensions
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        # 3. Skip if already 9:16 within tolerance
        if _is_already_portrait(w, h, target_ar):
            logger.info(f"   Smart crop skip: already 9:16 ({w}x{h})")
            return skip_result

        # 4. Ask LLM for focal point
        focal = _get_focal_point(call_fn, image_bytes)

        # 5. Calculate crop box and apply
        box = crop_around_focus(w, h, focal["focus_x"], focal["focus_y"], target_ar)
        cropped_img = img.crop(box)

        # Convert RGBA/P to RGB for JPEG compatibility
        if cropped_img.mode in ("RGBA", "P"):
            cropped_img = cropped_img.convert("RGB")

        buf = io.BytesIO()
        cropped_img.save(buf, format="JPEG", quality=92)
        cropped_bytes = buf.getvalue()

        # 6. Upload to GCS
        ts = int(time.time())
        uid = uuid.uuid4().hex[:8]
        key_name = f"smart_crop/smart_crop_{uid}_{ts}.jpg"
        new_url = gcs_storage.upload_image_bytes(
            cropped_bytes, key_name, content_type="image/jpeg"
        )

        if not new_url:
            logger.warning("Smart crop: GCS upload failed, using original URL")
            return skip_result

        return {
            "url": new_url,
            "cropped": True,
            "original_url": image_url,
            "focus_x": focal["focus_x"],
            "focus_y": focal["focus_y"],
            "description": focal.get("description", ""),
            "crop_box": list(box),
            "original_size": f"{w}x{h}",
            "cropped_size": f"{box[2]-box[0]}x{box[3]-box[1]}",
        }

    except Exception as e:
        logger.warning(f"Smart crop failed: {e} — using original URL")
        return skip_result
