"""Studio chat agent — multi-turn LLM session that picks the pipeline behind the scenes.

Sessions are in-memory only (recovery is by re-prompting). 2h TTL, swept on demand.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from api_pipeline.llm import call_llm

logger = logging.getLogger(__name__)

_SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours
_HISTORY_MAX_TURNS = 20  # cap context to last N user+assistant turns


@dataclass
class ChatSession:
    session_id: str
    tenant_id: str
    messages: List[Dict[str, str]] = field(default_factory=list)  # [{role, content}]
    slots: Dict[str, Any] = field(default_factory=dict)
    detected_language: str = "en"
    job_id: Optional[str] = None
    # Storyboard the user has approved (or is reviewing). When set, /commit-custom
    # routes through the custom pipeline instead of the four hardcoded presets.
    storyboard: Optional[Dict[str, Any]] = None
    # NEW (D3): chat mode. "concierge" = slot-filling wizard (default, legacy);
    # "director" = Director agent (Gemini 3 Pro) builds a full storyboard from
    # minimal back-and-forth. Both modes share the same session, history, and UI.
    mode: str = "concierge"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


_sessions: Dict[str, ChatSession] = {}
_sessions_lock = threading.Lock()


def _sweep_expired() -> None:
    cutoff = time.time() - _SESSION_TTL_SECONDS
    with _sessions_lock:
        stale = [sid for sid, s in _sessions.items() if s.last_active < cutoff]
        for sid in stale:
            _sessions.pop(sid, None)


def create_session(tenant_id: str, initial_message: Optional[str] = None) -> ChatSession:
    _sweep_expired()
    sid = uuid.uuid4().hex
    session = ChatSession(session_id=sid, tenant_id=tenant_id)
    with _sessions_lock:
        _sessions[sid] = session
    if initial_message:
        session.messages.append({"role": "user", "content": initial_message})
    return session


def get_session(session_id: str, tenant_id: str) -> Optional[ChatSession]:
    with _sessions_lock:
        s = _sessions.get(session_id)
    if s is None:
        return None
    if s.tenant_id != tenant_id:
        return None
    s.last_active = time.time()
    return s


def attach_job(session_id: str, tenant_id: str, job_id: str) -> bool:
    s = get_session(session_id, tenant_id)
    if s is None:
        return False
    s.job_id = job_id
    return True


def _format_history(messages: List[Dict[str, str]]) -> str:
    """Render the last N turns as a plain-text transcript the LLM can read."""
    if not messages:
        return "(no prior messages)"
    recent = messages[-(_HISTORY_MAX_TURNS * 2):]
    lines = []
    for msg in recent:
        role = msg.get("role", "user").upper()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no prior messages)"


def _format_job_state(session: ChatSession) -> str:
    if not session.job_id:
        return "(no active generation job)"
    return f"job_id={session.job_id} (generation in progress or complete)"


def _safe_json_parse(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON, tolerating ``` fences or leading/trailing prose."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        # strip code fences
        s = s.split("```", 2)
        s = s[1] if len(s) >= 2 else text
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    # Find first { and last }
    first = s.find("{")
    last = s.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = s[first : last + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        logger.warning("studio_chat_agent: JSON parse failed, raw text=%s", text[:300])
        return None


def _coerce_response(parsed: Optional[Dict[str, Any]], fallback_language: str) -> Dict[str, Any]:
    """Normalize the LLM response into the shape the chat UI expects.

    If parsing failed, return a minimal valid envelope so the UI does not crash.
    """
    if parsed is None:
        return {
            "reply": "Sorry — I had a hiccup understanding that. Could you rephrase?",
            "detected_language": fallback_language,
            "slots_update": {},
            "ui_action": {"type": "none", "panel": "none"},
            "needs_more_info": True,
            "missing_fields": [],
        }
    parsed.setdefault("reply", "")
    parsed.setdefault("detected_language", fallback_language)
    parsed.setdefault("slots_update", {})
    parsed.setdefault("needs_more_info", True)
    parsed.setdefault("missing_fields", [])
    ua = parsed.get("ui_action") or {}
    if not isinstance(ua, dict):
        ua = {}
    ua.setdefault("type", "none")
    ua.setdefault("panel", "none")
    # Defense in depth: never let the LLM kick off generation directly
    if ua.get("type") == "start_generation":
        ua["type"] = "show_summary"
    parsed["ui_action"] = ua
    return parsed


def _merge_slots(existing: Dict[str, Any], update: Dict[str, Any]) -> None:
    """Merge slots_update into existing slots. List slots are extended (deduped); scalars replaced."""
    if not isinstance(update, dict):
        return
    list_slots = {"character_urls", "product_image_urls", "reference_image_urls", "asset_urls"}
    for key, value in update.items():
        if value is None or value == "":
            continue
        if key in list_slots and isinstance(value, list):
            current = existing.get(key) or []
            if not isinstance(current, list):
                current = [current]
            seen = set(current)
            for item in value:
                if item and item not in seen:
                    current.append(item)
                    seen.add(item)
            existing[key] = current
        else:
            existing[key] = value


def chat_turn(
    session: ChatSession,
    user_message: str,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Run one chat turn: append user message, call LLM, parse, update slots, return envelope."""
    attachments = attachments or []

    # Synthesize "uploaded:" notes into the user-visible turn so the agent sees them
    rendered_user = user_message or ""
    if attachments:
        att_lines = [f"[uploaded {a.get('kind','file')}: {a.get('url')}]" for a in attachments if a.get("url")]
        if att_lines:
            rendered_user = (rendered_user + "\n" + "\n".join(att_lines)).strip()

    session.messages.append({"role": "user", "content": rendered_user})
    session.last_active = time.time()

    history_text = _format_history(session.messages[:-1])  # everything except the new user msg
    slots_text = json.dumps(session.slots, indent=2, ensure_ascii=False) if session.slots else "{}"
    attach_text = json.dumps(attachments, ensure_ascii=False) if attachments else "[]"
    job_text = _format_job_state(session)

    try:
        result = call_llm(
            "studio_chat_agent",
            history=history_text,
            slots_so_far=slots_text,
            job_state=job_text,
            user_message=rendered_user,
            attachments_json=attach_text,
        )
        raw_text = result.get("text", "")
    except Exception as e:
        logger.exception("studio_chat_agent call_llm failed: %s", e)
        envelope = _coerce_response(None, session.detected_language)
        session.messages.append({"role": "assistant", "content": envelope["reply"]})
        return envelope

    parsed = _safe_json_parse(raw_text)
    envelope = _coerce_response(parsed, session.detected_language)

    # Merge slots and update session-level state
    _merge_slots(session.slots, envelope.get("slots_update") or {})
    new_lang = envelope.get("detected_language") or session.detected_language
    if isinstance(new_lang, str) and new_lang.strip():
        session.detected_language = new_lang.strip()

    session.messages.append({"role": "assistant", "content": envelope.get("reply") or ""})
    return envelope


def session_to_dict(session: ChatSession) -> Dict[str, Any]:
    """Serialize for /session/{id} restore endpoint."""
    return {
        "session_id": session.session_id,
        "messages": list(session.messages),
        "slots": dict(session.slots),
        "detected_language": session.detected_language,
        "job_id": session.job_id,
        "storyboard": dict(session.storyboard) if session.storyboard else None,
        "mode": getattr(session, "mode", "concierge"),
        "created_at": session.created_at,
        "last_active": session.last_active,
    }


def set_mode(session_id: str, tenant_id: str, mode: str) -> bool:
    """Switch a chat session between 'concierge' and 'director' modes."""
    if mode not in ("concierge", "director"):
        return False
    s = get_session(session_id, tenant_id)
    if s is None:
        return False
    s.mode = mode
    return True


# =====================================================================
# Storyboard builder — Gemini 3 Pro structured-output call.
# Produces a full custom-pipeline storyboard from the gathered slots + uploads.
# =====================================================================

def _format_chat_context(session: ChatSession, max_turns: int = 12) -> str:
    """Render recent turns in plain text for the builder prompt."""
    if not session.messages:
        return "(no prior conversation)"
    recent = session.messages[-(max_turns * 2):]
    lines = []
    for m in recent:
        role = m.get("role", "user").upper()
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no prior conversation)"


def _collect_assets_block(slots: Dict[str, Any]) -> Dict[str, Any]:
    """Gather all asset URLs from slots into the storyboard assets shape."""
    return {
        "character_urls": list(slots.get("character_urls") or []),
        "product_image_urls": list(slots.get("product_image_urls") or []),
        "reference_image_urls": list(slots.get("reference_image_urls") or []),
        "asset_video_urls": list(slots.get("asset_video_urls") or []),
        "asset_urls": list(slots.get("asset_urls") or []),
        "logo_url": slots.get("logo_url"),
        "slogan_text": slots.get("slogan_text"),
    }


def build_storyboard(session: ChatSession) -> Optional[Dict[str, Any]]:
    """Call the storyboard_builder LLM and return a validated storyboard dict.

    Returns None if the LLM call or parse fails. Caller decides how to recover
    (typically ask the user to retry or refine the chat slots).
    """
    slots = session.slots or {}
    target_duration = int(slots.get("duration") or 20)
    language = slots.get("language") or session.detected_language or "en"
    style = slots.get("style") or "Auto"
    fidelity = float(slots.get("fidelity_to_assets", 0.5))

    chat_context = _format_chat_context(session)
    slots_json = json.dumps(slots, ensure_ascii=False, indent=2)
    assets_json = json.dumps(_collect_assets_block(slots), ensure_ascii=False, indent=2)

    try:
        result = call_llm(
            "storyboard_builder",
            chat_context=chat_context,
            slots_json=slots_json,
            assets_json=assets_json,
            target_duration=target_duration,
            language=language,
            style=style,
            fidelity=fidelity,
        )
        raw_text = result.get("text", "")
    except Exception as e:
        logger.exception("storyboard_builder call_llm failed: %s", e)
        return None

    parsed = _safe_json_parse(raw_text)
    if not parsed or not isinstance(parsed, dict):
        logger.warning("storyboard_builder returned unparseable text (first 300 chars): %s", raw_text[:300])
        return None

    # Backfill required meta fields the LLM might have skimped on.
    meta = parsed.setdefault("meta", {})
    meta.setdefault("title", slots.get("user_goal") or slots.get("prompt") or "Custom video")
    meta.setdefault("video_type", "custom")
    meta["target_duration_seconds"] = float(meta.get("target_duration_seconds") or target_duration)
    meta.setdefault("language", language)
    meta.setdefault("country", slots.get("country") or "")
    meta.setdefault("style", style)
    meta.setdefault("fidelity_to_assets", fidelity)
    meta.setdefault("aspect_ratio", "9:16")

    # Ensure assets block is present (LLM may omit it when there are no uploads).
    parsed.setdefault("assets", _collect_assets_block(slots))

    # Voiceover defaults
    vo = parsed.setdefault("voiceover", {})
    vo.setdefault("language", language)
    if slots.get("voice_id"):
        vo.setdefault("voice_id", slots["voice_id"])

    session.storyboard = parsed
    session.last_active = time.time()
    return parsed
