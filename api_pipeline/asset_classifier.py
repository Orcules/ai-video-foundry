"""Lightweight image classifier — given an URL, returns what KIND of asset it is.

Used by the upload endpoint to prevent silent mismatches (e.g. user uploads a logo
where the chat asked for a character photo). Surfaces a friendly warning to the UI
so the user can re-upload before the storyboard is built.

Single-call Gemini 2.5 Flash via Vertex (cheap: ~$0.0003/call). Returns:

    {"type": "person" | "product" | "logo" | "venue" | "document" | "other",
     "confidence": 0.0-1.0,
     "reason": "short human-readable explanation"}
"""

import base64
import json
import logging
import os
import re
from typing import Dict, Optional

import requests  # already a dependency via httpx/etc., but requests is also in monolith

logger = logging.getLogger(__name__)

# Hard cap: don't try to classify gigantic files. Most reasonable photos are <5 MB.
_MAX_BYTES_FOR_CLASSIFY = 8 * 1024 * 1024
_CLASSIFY_MODEL = "gemini-2.5-flash"

ASSET_TYPES = ("person", "product", "logo", "venue", "document", "video", "other")

_CLASSIFY_PROMPT = """You are an asset classifier for a video-generation pipeline. Look at this image and pick ONE category that best describes what it shows.

Categories:
- person: a portrait/headshot of a human face, or a person clearly in frame as the subject
- product: a physical object being sold (food, electronics, clothing, packaging, cosmetics, etc.)
- logo: a brand mark, wordmark, or icon — usually flat, on a solid background, designed for branding
- venue: a place / location / interior / exterior shot (restaurant, store, city street, landscape)
- document: a screenshot, text-heavy image, slide, infographic, chart, PDF page
- other: anything that doesn't fit the above categories

Return ONLY a JSON object on a single line with these keys:
{"type": "<one of: person|product|logo|venue|document|other>", "confidence": <0.0-1.0>, "reason": "<one short sentence>"}

No commentary. No markdown fences. Just the JSON."""


def _fetch_image_bytes(url: str) -> tuple[bytes, str]:
    """Download an image URL and return (bytes, mime_type). Raises on error."""
    headers = {"User-Agent": "VidBuddy-AssetClassifier/1.0"}
    resp = requests.get(url, headers=headers, timeout=15, stream=True)
    resp.raise_for_status()
    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        # Maybe video — record but don't try to classify yet
        if mime.startswith("video/"):
            raise ValueError("VIDEO_NOT_IMAGE")
        raise ValueError(f"Unsupported content-type: {mime}")
    # Read up to the cap
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_BYTES_FOR_CLASSIFY:
            raise ValueError(f"Image too large to classify ({total} bytes)")
    return b"".join(chunks), mime


def _safe_parse_json(text: str) -> Optional[dict]:
    """Extract a JSON object from LLM output that may have prose/code-fence wrappers."""
    if not text:
        return None
    # Strip ```json ... ``` fences
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def classify_asset(url: str) -> Dict[str, object]:
    """Return a classification dict for the given asset URL.

    Always returns a dict. On any error returns ``{"type": "other", "confidence": 0,
    "reason": "<error>"}`` — never raises. Caller decides whether to surface or ignore.
    """
    if not url or not isinstance(url, str):
        return {"type": "other", "confidence": 0.0, "reason": "no url"}

    # Fast path for obvious video URLs by extension — skip classification
    lower = url.lower().split("?")[0]
    if any(lower.endswith(ext) for ext in (".mp4", ".mov", ".webm", ".mkv", ".avi")):
        return {"type": "video", "confidence": 1.0, "reason": "url is a video file"}

    try:
        image_bytes, mime = _fetch_image_bytes(url)
    except ValueError as e:
        if str(e) == "VIDEO_NOT_IMAGE":
            return {"type": "video", "confidence": 1.0, "reason": "content-type is video"}
        return {"type": "other", "confidence": 0.0, "reason": f"fetch failed: {e}"}
    except Exception as e:
        logger.warning("classify_asset fetch failed for %s: %s", url, e)
        return {"type": "other", "confidence": 0.0, "reason": "could not download image"}

    # Build Vertex generateContent payload with inline image bytes
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _CLASSIFY_PROMPT},
                    {"inlineData": {"mimeType": mime, "data": b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 200,
            "responseMimeType": "application/json",
        },
    }

    # Lazy-import to keep server startup fast
    from api_pipeline.llm import _get_vertex_provider
    vertex = _get_vertex_provider()
    if vertex is None:
        return {"type": "other", "confidence": 0.0, "reason": "vertex provider not available"}

    try:
        result = vertex.raw_generate_content(payload, model=_CLASSIFY_MODEL)
        text = result.get("text", "")
    except Exception as e:
        logger.warning("classify_asset Vertex call failed: %s", e)
        return {"type": "other", "confidence": 0.0, "reason": "classifier call failed"}

    parsed = _safe_parse_json(text)
    if not parsed:
        return {"type": "other", "confidence": 0.0, "reason": "unparseable classifier output"}

    asset_type = str(parsed.get("type", "other")).lower().strip()
    if asset_type not in ASSET_TYPES:
        asset_type = "other"
    try:
        conf = float(parsed.get("confidence", 0.0))
        conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(parsed.get("reason", ""))[:200] or "classified"

    return {"type": asset_type, "confidence": conf, "reason": reason}


# Slot → expected asset type mapping. Used by the chat UI / upload endpoint to
# warn the user when their upload doesn't match the slot the agent asked for.
SLOT_EXPECTED_TYPES = {
    "uploads_character": ("person",),
    "uploads_product": ("product",),
    "uploads_logo": ("logo",),
    "uploads_assets": ("video", "venue", "product", "person"),  # broad; only warn on logo/document
}


def slot_mismatch_warning(slot_panel: str, classification: Dict) -> Optional[str]:
    """Return a friendly warning string when the upload doesn't fit the requested slot.

    Returns ``None`` when the upload looks right or confidence is too low to warn.
    """
    if not classification or classification.get("confidence", 0) < 0.55:
        return None
    expected = SLOT_EXPECTED_TYPES.get(slot_panel)
    if not expected:
        return None
    actual = classification.get("type", "other")
    if actual in expected:
        return None
    if actual == "other":
        return None  # don't warn on "we couldn't tell"
    # Friendly translations
    friendly = {
        "person": "a photo of a person",
        "product": "a product photo",
        "logo": "a logo",
        "venue": "a venue / location photo",
        "document": "a document / screenshot",
        "video": "a video file",
        "other": "something else",
    }
    asked = {
        "uploads_character": "a photo of you / your host",
        "uploads_product": "a product photo",
        "uploads_logo": "your logo",
        "uploads_assets": "an asset clip or photo",
    }.get(slot_panel, "an upload")
    return (
        f"Heads up — that looks like {friendly.get(actual, actual)}, "
        f"but I asked for {asked}. Want to re-upload, or use it as-is?"
    )
