"""Image evaluation tasks — quality scoring and cleanliness checks.

Free functions that use ``call_fn`` for LLM routing.  Extracted from
``GeminiService.evaluate_image_quality`` / ``evaluate_image_cleanliness``.
"""

import base64
import logging
from typing import Callable, Dict, Optional

import requests

from tvd_pipeline.prompt_loader import get_prompt_loader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
        logger.warning(f"Could not fetch image for evaluation: {e}")
        return None


def _image_content_part(image_url: str) -> Optional[Dict]:
    """Build an OpenAI-format content part for an image URL.

    For http/https URLs the image is fetched and base64-encoded inline.
    For ``gs://`` URIs the raw URI is passed (the provider handles conversion).
    Returns ``None`` if neither path succeeds.
    """
    if image_url.startswith("gs://"):
        return {"type": "image_url", "image_url": {"url": image_url}}
    # http/https — fetch and base64-encode
    return _fetch_image_as_inline(image_url)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_image_quality(call_fn: Callable, image_url: str, original_prompt: str) -> int:
    """Rate image quality on a 1-10 scale for composition, relevance, and artifacts.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` — the LLM dispatch
            function.  Returns ``{"text": str, ...}``.
        image_url: URL of the image to evaluate (https or gs://).
        original_prompt: The prompt that was used to generate the image
            (provided to the LLM for relevance scoring).

    Returns:
        Integer score between 1 and 10.  Defaults to 7 on any error.
    """
    try:
        eval_prompt = get_prompt_loader().get(
            "shared_image_quality_eval", original_prompt=original_prompt
        )

        image_part = _image_content_part(image_url)
        if image_part is None:
            logger.warning("Could not build image part for quality evaluation")
            return 7

        user_content = [
            image_part,
            {"type": "text", "text": eval_prompt},
        ]

        messages = [
            {"role": "user", "content": user_content},
        ]

        llm_result = call_fn(messages, temperature=0.1, max_tokens=10)
        score_text = (llm_result.get("text") or "").strip()
        try:
            score = int(score_text)
            return max(1, min(10, score))
        except (ValueError, TypeError):
            return 7

    except Exception as e:
        logger.warning(f"Image quality evaluation failed: {e}")
        return 7


def evaluate_image_cleanliness(call_fn: Callable, image_url: str) -> bool:
    """Check if a product image has a clean/transparent background.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> dict`` — the LLM dispatch
            function.  Returns ``{"text": str, ...}``.
        image_url: URL of the product image to check (https or gs://).

    Returns:
        ``True`` if the background is clean/white/transparent, ``False``
        otherwise.  Defaults to ``True`` on error (optimistic).
    """
    try:
        eval_prompt = (
            'Look at this product image. Does it have a clean, white, or '
            'transparent background suitable for use in a video advertisement?\n'
            'Answer ONLY "yes" or "no".'
        )

        image_part = _image_content_part(image_url)
        if image_part is None:
            logger.warning("Could not build image part for cleanliness evaluation")
            return True

        user_content = [
            image_part,
            {"type": "text", "text": eval_prompt},
        ]

        messages = [
            {"role": "user", "content": user_content},
        ]

        llm_result = call_fn(messages, temperature=0.1, max_tokens=10)
        answer = (llm_result.get("text") or "").strip().lower()
        return answer.startswith("yes")

    except Exception as e:
        logger.warning(f"Image cleanliness evaluation failed: {e}")
        return True
