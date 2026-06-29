"""Director agent — Gemini 3 Pro structured-output call that produces a complete
storyboard JSON for the custom pipeline.

Public entry: :func:`direct_storyboard(session)`.

The Director sits alongside the existing slot-filling :func:`build_storyboard`
in ``chat_agent.py``. Concierge mode keeps using ``build_storyboard``; Director
mode (selected per-session) uses this module.

Today the prompt is text-only — image URLs from uploads are passed as strings
inside the user prompt. The Director treats them as references for the
production engine to read later (no pixel analysis is needed at planning time).
True multimodal vision is a follow-up enhancement.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from api_pipeline.llm import call_llm

logger = logging.getLogger(__name__)


def _safe_json_parse(text: str) -> Optional[Dict[str, Any]]:
    """Tolerate code fences / leading prose around the LLM's JSON."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        parts = s.split("```", 2)
        s = parts[1] if len(parts) >= 2 else text
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        return json.loads(s[first : last + 1])
    except json.JSONDecodeError:
        logger.warning("director: JSON parse failed, raw=%s", text[:300])
        return None


def _format_chat_history(session, max_turns: int = 12) -> str:
    """Render the most recent N turns of chat as text the Director can read."""
    msgs = getattr(session, "messages", None) or []
    if not msgs:
        return "(no prior conversation)"
    recent = msgs[-(max_turns * 2):]
    lines: List[str] = []
    for m in recent:
        role = (m.get("role") or "user").upper()
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no prior conversation)"


def _latest_user_brief(session) -> str:
    """Pull the most recent user message as 'the brief'."""
    msgs = getattr(session, "messages", None) or []
    for m in reversed(msgs):
        if (m.get("role") or "").lower() == "user" and (m.get("content") or "").strip():
            return m["content"].strip()
    return ""


def _collect_assets_block(slots: Dict[str, Any]) -> Dict[str, Any]:
    """Build the assets dict from the session's gathered slots."""
    return {
        "character_urls": list(slots.get("character_urls") or []),
        "product_image_urls": list(slots.get("product_image_urls") or []),
        "reference_image_urls": list(slots.get("reference_image_urls") or []),
        "asset_video_urls": list(slots.get("asset_video_urls") or []),
        "asset_urls": list(slots.get("asset_urls") or []),
        "logo_url": slots.get("logo_url"),
        "slogan_text": slots.get("slogan_text"),
    }


def _coerce_meta(storyboard: Dict[str, Any], slots: Dict[str, Any], session) -> None:
    """Fill required meta fields the LLM might have skimped on, in-place."""
    meta = storyboard.setdefault("meta", {})
    meta.setdefault("title", slots.get("user_goal") or slots.get("prompt") or "Custom video")
    meta.setdefault("video_type", "custom")
    target = int(slots.get("duration") or 30)
    meta["target_duration_seconds"] = float(meta.get("target_duration_seconds") or target)
    meta.setdefault("language", slots.get("language") or getattr(session, "detected_language", "en") or "en")
    meta.setdefault("country", slots.get("country") or "")
    meta.setdefault("style", slots.get("style") or "Auto")
    meta.setdefault("fidelity_to_assets", float(slots.get("fidelity_to_assets", 0.6)))
    meta.setdefault("aspect_ratio", "9:16")
    meta.setdefault("viral_structure", "hook_problem_solution_cta")
    meta.setdefault("pacing", "fast_first_3s")


def _coerce_assets(storyboard: Dict[str, Any], slots: Dict[str, Any]) -> None:
    """Ensure storyboard.assets reflects everything the user uploaded."""
    desired = _collect_assets_block(slots)
    current = storyboard.setdefault("assets", {})
    for k, v in desired.items():
        # Don't overwrite if Director provided something — but fill gaps.
        if not current.get(k):
            current[k] = v


def _coerce_voiceover(storyboard: Dict[str, Any], slots: Dict[str, Any]) -> None:
    vo = storyboard.setdefault("voiceover", {})
    vo.setdefault("language", storyboard["meta"].get("language", "en"))
    if slots.get("voice_id"):
        vo.setdefault("voice_id", slots["voice_id"])


def _coerce_sheets(storyboard: Dict[str, Any], slots: Dict[str, Any]) -> None:
    """If user uploaded a character/venue but Director didn't lock a sheet,
    fall back to populating the sheets from raw uploads. This guarantees
    consistency even if the Director was lazy."""
    if storyboard.get("character_sheet") is None and slots.get("character_urls"):
        storyboard["character_sheet"] = {
            "subject_description": slots.get("character_description") or "",
            "reference_image_urls": list(slots["character_urls"])[:3],
            "voice_id": slots.get("voice_id"),
            "gender": slots.get("gender") or "f",
        }
    if storyboard.get("venue_sheet") is None and slots.get("reference_image_urls"):
        storyboard["venue_sheet"] = {
            "description": "",
            "reference_image_urls": list(slots["reference_image_urls"])[:3],
        }


def direct_storyboard(session, *, on_thought: Optional[callable] = None) -> Optional[Dict[str, Any]]:
    """Generate a full Director storyboard for the current chat session.

    Args:
        session: a ``ChatSession`` (api_pipeline.chat_agent.ChatSession). Must
            have non-empty ``slots`` and at least one user message.
        on_thought: optional callback to stream the Director's reasoning to UI
            (not yet used — placeholder for D6 critic loop).

    Returns:
        Validated storyboard dict, or None if the LLM call failed and we
        couldn't recover. Caller (the API route) is responsible for storing
        the result on ``session.storyboard`` and surfacing errors to the UI.
    """
    slots = session.slots or {}
    target_duration = int(slots.get("duration") or 30)
    language = slots.get("language") or getattr(session, "detected_language", "en") or "en"
    style = slots.get("style") or "Auto"
    fidelity = float(slots.get("fidelity_to_assets", 0.6))
    aspect_ratio = slots.get("aspect_ratio") or "9:16"

    user_brief = _latest_user_brief(session)
    chat_history = _format_chat_history(session)
    slots_json = json.dumps(slots, ensure_ascii=False, indent=2)
    assets_json = json.dumps(_collect_assets_block(slots), ensure_ascii=False, indent=2)

    t0 = time.time()
    try:
        result = call_llm(
            "director",
            user_brief=user_brief,
            chat_history=chat_history,
            slots_json=slots_json,
            assets_json=assets_json,
            target_duration=target_duration,
            language=language,
            style=style,
            fidelity=fidelity,
            aspect_ratio=aspect_ratio,
        )
        raw_text = result.get("text", "")
        logger.info(
            "[director] LLM responded in %.1fs (model=%s, tokens=%s/%s)",
            time.time() - t0,
            result.get("model"),
            result.get("input_tokens"),
            result.get("output_tokens"),
        )
    except Exception as e:
        logger.exception("[director] LLM call failed: %s", e)
        return None

    storyboard = _safe_json_parse(raw_text)
    if not storyboard or not isinstance(storyboard, dict):
        logger.warning("[director] unparseable LLM output (first 300): %s", raw_text[:300])
        return None

    # NEW: Director may return a `needs_assets` payload INSTEAD of scenes when key
    # uploads are missing. In that case we surface the request to the chat and
    # skip the storyboard backfill — the caller (server route) checks for this
    # field and routes the response back to the user as a chat reply.
    needs = storyboard.get("needs_assets")
    if needs and isinstance(needs, list) and len(needs) > 0:
        # Don't backfill / store as storyboard — return a thin payload that the
        # API route recognises as "asset request, not storyboard".
        logger.info(
            "[director] returned needs_assets (%d items) instead of storyboard",
            len(needs),
        )
        if on_thought:
            try:
                on_thought("director_needs_assets", {
                    "count": len(needs),
                    "panels": [n.get("panel") for n in needs if isinstance(n, dict)],
                })
            except Exception:
                pass
        # Mark with a sentinel field so the route can branch
        return {
            "_is_needs_assets": True,
            "needs_assets": needs,
            "reply": storyboard.get("reply") or "I'll need a few uploads from you to make this video.",
        }

    # Backfill / sanity-fill required fields. The Director schema is permissive
    # (additionalProperties=true on most blocks), so we patch defaults rather
    # than reject. Validation against the strict storyboard validator happens
    # downstream in the API route before commit.
    _coerce_meta(storyboard, slots, session)
    _coerce_assets(storyboard, slots)
    _coerce_voiceover(storyboard, slots)
    _coerce_sheets(storyboard, slots)

    # Stash on session for future turns / commit
    try:
        session.storyboard = storyboard
        session.last_active = time.time()
    except Exception:
        pass

    if on_thought:
        try:
            on_thought("director_done", {
                "scene_count": len(storyboard.get("scenes") or []),
                "duration": storyboard["meta"].get("target_duration_seconds"),
                "preset_hint": storyboard["meta"].get("preset_hint"),
                "viral_structure": storyboard["meta"].get("viral_structure"),
            })
        except Exception:
            pass

    return storyboard
