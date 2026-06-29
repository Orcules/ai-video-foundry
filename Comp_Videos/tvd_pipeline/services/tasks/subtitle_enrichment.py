"""Subtitle enrichment task — adds emoji and importance markers to BYOT transcripts.

Sends the normalized word list + full VO script to an LLM, which returns
per-word annotations (emoji character, important flag). These are merged
into the ZapCap BYOT entries so subtitles display emoji and keyword highlights.
"""

import json
import logging
from typing import Callable, Dict, List, Optional

from tvd_pipeline.prompt_loader import get_prompt_loader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response schema for structured output
# ---------------------------------------------------------------------------

ENRICHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "enrichments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "emoji": {"type": "string"},
                    "important": {"type": "boolean"},
                },
                "required": ["index", "emoji", "important"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["enrichments"],
    "additionalProperties": False,
}


def enrich_transcript_for_subtitles(
    call_fn: Callable,
    word_segments: List[Dict],
    vo_script: str = "",
    language: str = "en",
    fallback_call_fn: Callable = None,
) -> Optional[Dict[int, Dict]]:
    """Enrich a normalized BYOT word list with emoji and importance markers.

    Parameters
    ----------
    call_fn : Callable
        Standard ``_call_llm`` wrapper (``lambda msgs, **kw: processor._call_llm("enrich_subtitles", msgs, **kw)``).
    word_segments : list[dict]
        Normalized word list from ``_normalize_transcript_for_zapcap()``.
        Each entry has ``text``, ``type``, ``start_time``, ``end_time``.
    vo_script : str
        Full voiceover script text (gives the LLM sentence context).
    language : str
        Language code (e.g., ``"en"``, ``"he"``).

    Returns
    -------
    dict[int, dict] | None
        Mapping of word index -> ``{"emoji": "...", "important": True}``
        for annotated words. Returns ``None`` on any failure (pipeline
        continues without enrichment).
    """
    if not word_segments:
        return None

    try:
        loader = get_prompt_loader()

        # Build indexed word list for the LLM
        word_lines = []
        for i, seg in enumerate(word_segments):
            word_lines.append(f"{i}: {seg['text']}")
        word_list_text = "\n".join(word_lines)

        system_prompt = loader.get("shared_subtitle_enrichment_system")
        user_prompt = loader.get(
            "shared_subtitle_enrichment_user",
            language=language,
            vo_script=vo_script or "(not available)",
            word_list=word_list_text,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        result = call_fn(
            messages,
            temperature=0.3,
            max_tokens=4000,
            responseSchema=ENRICHMENT_SCHEMA,
        )

        content = (result.get("text") or "").strip()
        if not content:
            logger.warning("Subtitle enrichment: LLM returned empty response")
            return None

        try:
            data = json.loads(content)
        except json.JSONDecodeError as parse_err:
            if fallback_call_fn is not None:
                logger.warning(
                    "Subtitle enrichment: primary LLM returned invalid JSON (%s), "
                    "retrying with fallback LLM", parse_err
                )
                result = fallback_call_fn(
                    messages, temperature=0.3, max_tokens=4000,
                    responseSchema=ENRICHMENT_SCHEMA,
                )
                content = (result.get("text") or "").strip()
                if not content:
                    logger.warning("Subtitle enrichment: fallback LLM returned empty")
                    return None
                data = json.loads(content)  # If this also fails, outer except catches it
            else:
                raise  # No fallback available, let outer except handle it
        enrichments_list = data.get("enrichments", [])

        # Build index -> annotation dict
        enrichments: Dict[int, Dict] = {}
        max_idx = len(word_segments) - 1
        for entry in enrichments_list:
            idx = entry.get("index")
            if idx is None or not isinstance(idx, int) or idx < 0 or idx > max_idx:
                continue
            annotation = {}
            if entry.get("emoji"):
                annotation["emoji"] = entry["emoji"]
            if entry.get("important"):
                annotation["important"] = True
            if annotation:
                enrichments[idx] = annotation

        logger.info(
            "Subtitle enrichment: %d/%d words annotated (%d emoji, %d important)",
            len(enrichments),
            len(word_segments),
            sum(1 for v in enrichments.values() if "emoji" in v),
            sum(1 for v in enrichments.values() if v.get("important")),
        )
        return enrichments if enrichments else None

    except Exception as e:
        logger.warning("Subtitle enrichment failed (non-blocking): %s", e)
        return None
