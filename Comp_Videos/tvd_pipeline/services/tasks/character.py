"""Character description tasks — describe people in reference images.

Free functions that use ``call_fn`` for LLM routing.  Extracted from
``GeminiService.describe_character`` / ``describe_characters``.
"""

import base64
import logging
import re
from typing import Callable, Dict, List, Optional

import requests

from tvd_pipeline.prompt_loader import get_prompt_loader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _character_image_uri(url: str) -> Optional[str]:
    """Convert an image URL to a ``gs://`` URI if possible.

    * ``gs://`` — returned as-is.
    * ``https://storage.googleapis.com/…`` — converted to ``gs://…``.
    * Anything else — returns ``None``.
    """
    u = url.strip()
    if u.startswith("gs://"):
        return u
    if u.startswith("https://storage.googleapis.com/"):
        path = u.replace("https://storage.googleapis.com/", "")
        return f"gs://{path}"
    return None


def _fetch_image_as_inline(image_url: str) -> Optional[Dict]:
    """Fetch an http/https image and return an OpenAI-format inline image part.

    Returns ``None`` on failure so the caller can fall back.
    """
    try:
        fetch_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/*,*/*;q=0.8",
        }
        img_resp = requests.get(image_url.strip(), headers=fetch_headers, timeout=30)
        img_resp.raise_for_status()
        ct = img_resp.headers.get("Content-Type", "").lower()
        mime = "image/png" if "png" in ct else "image/webp" if "webp" in ct else "image/jpeg"
        b64 = base64.b64encode(img_resp.content).decode("utf-8")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }
    except Exception as e:
        logger.warning(f"Could not fetch character image for inline encoding: {e}")
        return None


def _image_content_part(image_url: str) -> Optional[Dict]:
    """Build an OpenAI-format content part for an image URL.

    For http/https URLs the image is fetched and base64-encoded inline.
    For ``gs://`` URIs the raw URI is passed (the provider handles conversion).
    Returns ``None`` if neither path succeeds.
    """
    if image_url.startswith("http://") or image_url.startswith("https://"):
        part = _fetch_image_as_inline(image_url)
        if part is not None:
            return part
        # http fetch failed — try converting to gs:// as fallback
        gs_uri = _character_image_uri(image_url)
        if gs_uri:
            return {"type": "image_url", "image_url": {"url": gs_uri}}
        return None

    gs_uri = _character_image_uri(image_url)
    if gs_uri:
        return {"type": "image_url", "image_url": {"url": gs_uri}}
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def describe_character(call_fn: Callable, image_url: str) -> Optional[str]:
    """Analyze a character image and return a brief description.

    Uses ``call_fn`` to route the request through the unified LLM dispatcher
    (``_call_llm``).

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` — the LLM dispatch
            function.  Returns ``{"text": str, ...}``.
        image_url: URL of the character image (https, gs://, or
            storage.googleapis.com).

    Returns:
        Brief 1-2 sentence description of the character, or ``None`` on failure.
    """
    logger.info("Analyzing character image with Gemini...")

    system_prompt = get_prompt_loader().get("shared_character_description_system")

    user_text = (
        "Briefly describe the person or people in this image. "
        "If multiple people: use 'Person 1: ... Person 2: ...'. "
        "Focus only on the most obvious visual features (hair, clothing, gender/age)."
    )

    image_part = _image_content_part(image_url)
    if image_part is None:
        logger.warning("Character image URL not supported (use https or gs://)")
        return None

    user_content = [
        image_part,
        {"type": "text", "text": user_text},
    ]

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        llm_result = call_fn(messages, temperature=0.3, max_tokens=250)
        description = (llm_result.get("text") or "").strip()
        if description:
            logger.info(f"Character described: {description[:100]}...")
            return description
        logger.warning("No character description from LLM response")
        return None
    except Exception as e:
        logger.error(f"Error describing character: {e}")
        return None


def describe_characters(call_fn: Callable, image_urls: List[str]) -> Optional[str]:
    """Describe one or more character images; returns a single combined string.

    For a single URL behaves identically to :func:`describe_character`.
    For multiple URLs each is described individually and merged into
    ``"Person 1: … Person 2: …"`` format.

    Args:
        call_fn: LLM dispatch function (see :func:`describe_character`).
        image_urls: List of character image URLs.

    Returns:
        Combined description string, or ``None`` if all failed or list empty.
    """
    if not image_urls:
        return None
    if len(image_urls) == 1:
        return describe_character(call_fn, image_urls[0])

    descriptions: List[str] = []
    for i, url in enumerate(image_urls, 1):
        desc = describe_character(call_fn, url)
        if desc:
            descriptions.append(f"Person {i}: {desc}")
    if not descriptions:
        return None
    return " ".join(descriptions)
