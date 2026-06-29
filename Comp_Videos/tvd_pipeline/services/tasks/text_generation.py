"""Text generation tasks -- opening text, headlines, and short copy.

Free functions that use ``call_fn`` for LLM routing.  Extracted from
``OpenAIService.generate_opening_text``.
"""

import logging
from typing import Any, Callable, Dict, Optional

from tvd_pipeline.config import Config
from tvd_pipeline.data_loader import get_language_name
from tvd_pipeline.prompt_loader import get_prompt_loader

config = Config()
logger = logging.getLogger(__name__)


def generate_opening_text(
    call_fn: Callable,
    article_text: str,
    language: str = "en",
    video_description: Optional[str] = None,
) -> Optional[str]:
    """Generate a short, compelling opening text based on VIDEO content with cultural adaptation.

    Creates a brief, attention-grabbing headline that matches what is shown in the
    video AND is culturally appropriate for the target region/language.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        article_text: Article content for context.
        language: Target language for the text.
        video_description: Description of what is shown in the video.

    Returns:
        Short opening text string, or ``None`` if failed.
    """
    try:
        logger.info(f"Generating opening text (language: {language})...")

        lang_name = get_language_name(language)

        # Get cultural region and hook style from config
        region = config.REGION_MAPPING.get(language, "namer")
        hook_style = config.HOOK_STYLES.get(region, "aspirational messaging, personal success")
        cultural_info = config.CULTURAL_STYLES.get(region, {})
        style_description = cultural_info.get("style", "confident, aspirational")

        logger.info(f"   Region: {region}, Hook style: {hook_style[:50]}...")

        # Build context - prioritize video content
        context_parts = []
        if video_description:
            context_parts.append(
                f"VIDEO CONTENT (MOST IMPORTANT - text must match this!):\n{video_description}"
            )
        if article_text:
            context_parts.append(f"Article context:\n{article_text[:300]}")

        context = "\n\n".join(context_parts) if context_parts else "General promotional content"

        _pl = get_prompt_loader()

        messages = [
            {
                "role": "system",
                "content": _pl.get(
                    "shared_opening_text_system",
                    lang_name=lang_name,
                    region=region,
                    hook_style=hook_style,
                    style_description=style_description,
                ),
            },
            {
                "role": "user",
                "content": _pl.get(
                    "shared_opening_text_user",
                    lang_name=lang_name,
                    hook_style=hook_style,
                    context=context,
                ),
            },
        ]

        result = call_fn(messages, temperature=0.7, max_tokens=50)
        text = result.get("text", "")
        if not text:
            logger.warning("Opening text generation returned empty")
            return None

        text = text.strip().strip("\"'")
        logger.info(f"Generated opening text ({region} style): '{text}'")
        return text

    except Exception as e:
        logger.warning(f"Could not generate opening text: {e}")
        return None
