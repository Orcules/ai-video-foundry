"""UGC Real pipeline.

Grid-first, offer-aware UGC pipeline with frame routing and Kling Avatar Pro lip-sync.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tvd_pipeline.data_loader import get_kie_config, get_language_voice
from tvd_pipeline.utils import is_valid_voice_id
from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.services.tasks._helpers import extract_json_from_response
from tvd_pipeline.services.tasks.ugc_real_creative import (
    analyze_offer,
    generate_creative_strategy,
    plan_narrative,
    build_grid_manifest,
    generate_nine_cell_plan,
    extract_style_dna,
    build_master_grid_prompt,
)
from tvd_pipeline.services.tasks.grid_cutter import cut_grid_image_url
from tvd_pipeline.services.tasks.frame_classification import classify_scene_plan
from tvd_pipeline.config import Config, get_pipeline_defaults

logger = logging.getLogger(__name__)
config = Config()

# Bundled 3×3 / 9:16 layout template for master grid (first Nano Banana reference image).
_UGC_REAL_LAYOUT_TEMPLATE_BYTES: Optional[bytes] = None
_UGC_REAL_LAYOUT_TEMPLATE_URL_CACHE: Optional[str] = None


def _load_ugc_real_grid_layout_template_bytes() -> Optional[bytes]:
    global _UGC_REAL_LAYOUT_TEMPLATE_BYTES
    if _UGC_REAL_LAYOUT_TEMPLATE_BYTES is not None:
        return _UGC_REAL_LAYOUT_TEMPLATE_BYTES
    p = Path(__file__).resolve().parent.parent / "assets" / "ugc_real_grid_layout_template_9x16.png"
    if not p.is_file():
        logger.warning(
            "UGC Real: grid layout template missing at %s — master grid will not attach layout reference",
            p,
        )
        return None
    _UGC_REAL_LAYOUT_TEMPLATE_BYTES = p.read_bytes()
    return _UGC_REAL_LAYOUT_TEMPLATE_BYTES


def get_ugc_real_master_grid_layout_reference_url(processor) -> Optional[str]:
    """HTTPS URL for the bundled blank 3×3 grid template (published via GCS for Kie image_input)."""
    global _UGC_REAL_LAYOUT_TEMPLATE_URL_CACHE
    if _UGC_REAL_LAYOUT_TEMPLATE_URL_CACHE:
        return _UGC_REAL_LAYOUT_TEMPLATE_URL_CACHE
    data = _load_ugc_real_grid_layout_template_bytes()
    if not data:
        return None
    gcs = getattr(processor, "gcs_storage_service", None)
    if not gcs:
        logger.warning("UGC Real: no GCS — cannot publish grid layout template URL for Nano Banana")
        return None
    url = gcs.upload_image_bytes(
        image_data=data,
        key_name="ugc_real/static/master_grid_layout_template_9x16.png",
        content_type="image/png",
    )
    if url:
        _UGC_REAL_LAYOUT_TEMPLATE_URL_CACHE = url
    return url


def _emit(on_progress, event_type: str, data: Dict[str, Any]) -> None:
    if on_progress:
        on_progress(event_type, data)


def _emit_step(on_progress, step: str, label: str, progress: int, message: str) -> None:
    _emit(
        on_progress,
        "step_complete",
        {"step": step, "label": label, "progress": progress, "message": message},
    )


def _emit_step_start(on_progress, step: str, label: str, message: str) -> None:
    _emit(
        on_progress,
        "step_start",
        {"step": step, "label": label, "message": message},
    )


def _safe_json(text: Any) -> str:
    try:
        return json.dumps(text, ensure_ascii=True)
    except Exception:
        return "{}"


def _pick_first_cell(scene_id: str, rows: List[Dict[str, Any]], use: str) -> Optional[Dict[str, Any]]:
    for row in rows:
        if row.get("scene_id") == scene_id and row.get("primary_use") == use:
            return row
    return None


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _is_http_asset_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    u = value.strip()
    return bool(u) and u.lower().startswith("http")


def _scene_videos_list_has_any_url(scene_videos: Any) -> bool:
    if not isinstance(scene_videos, list):
        return False
    return any(_is_http_asset_url(v) for v in scene_videos)


def _apply_scene_images_to_ugc_real_grid_cells(
    grid_cells: List[Dict[str, Any]],
    scene_images: Any,
) -> bool:
    """Map Studio patched intermediates['scene_images'] onto grid_cells[].image_url for step_8 I2V.

    The Studio sends approved stills as scene_images before resume; step 8 historically read only
    grid_cells. Without this merge, animation can target stale crop URLs or empty slots while the
    user sees correct tiles in the UI (and providers may be hit again for images).
    """
    if not grid_cells or not scene_images or not isinstance(scene_images, list):
        return False
    urls: List[Optional[str]] = []
    for x in scene_images:
        urls.append(x.strip() if _is_http_asset_url(x) else None)
    if not any(urls):
        return False
    by_cell: Dict[int, Dict[str, Any]] = {}
    for gc in grid_cells:
        try:
            ci = int(gc.get("cell_index") or 0)
        except (TypeError, ValueError):
            ci = 0
        if ci > 0:
            by_cell[ci] = gc
    changed = False
    for i, u in enumerate(urls):
        if not u:
            continue
        cell_num = i + 1
        gc = by_cell.get(cell_num)
        if gc is None and i < len(grid_cells):
            gc = grid_cells[i]
        if gc is None:
            continue
        prev = str(gc.get("image_url") or "").strip()
        if prev != u:
            gc["image_url"] = u
            changed = True
    if changed:
        logger.info(
            "UGC Real: Applied intermediates scene_images onto grid_cells before animation (%s slot(s) updated)",
            sum(1 for x in urls if x),
        )
    return changed


_VALID_OFFER_TYPES = frozenset({"physical_product", "digital_product", "service"})
_VALID_AD_FORMATS = frozenset(
    {
        "talking_head",
        "podcast_style",
        "car_selfie",
        "product_demo",
        "lifestyle",
        "problem_solution",
    }
)

UGC_REAL_PARSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "offer_type": {"type": "string", "enum": ["physical_product", "digital_product", "service"]},
        "offer_category": {"type": "string"},
        "target_audience": {"type": "string"},
        "main_problem": {"type": "string"},
        "key_benefits": {"type": "string"},
        "cta_text": {"type": "string"},
        "ad_format": {"type": "string"},
    },
    "required": [
        "offer_type",
        "offer_category",
        "target_audience",
        "main_problem",
        "key_benefits",
        "cta_text",
        "ad_format",
    ],
    "additionalProperties": False,
}


def _normalize_offer_type(raw: str) -> str:
    x = (raw or "").strip().lower()
    return x if x in _VALID_OFFER_TYPES else "service"


def _normalize_ad_format(raw: str) -> str:
    x = (raw or "").strip().lower().replace("-", "_")
    return x if x in _VALID_AD_FORMATS else "talking_head"


def _ugc_real_gender_label(raw: str) -> str:
    """Normalize API `gender` to a short label for LLM prompts."""
    g = (raw or "f").strip().lower()
    if g in ("m", "male", "man", "masculine", "guy"):
        return "male"
    if g in ("f", "female", "woman", "feminine", "girl"):
        return "female"
    return g if g else "unspecified"


def _ugc_real_resolve_character_and_product_context(
    *,
    gender_raw: str,
    character_description_in: str,
    character_urls: List[str],
    offer_type: str,
    main_problem: str,
    key_benefits: str,
    product_image_urls: List[str],
    reference_image_urls: List[str],
) -> Tuple[str, str, str]:
    """Return (gender_label, character_description, product_description) for grid LLM steps."""
    gender_label = _ugc_real_gender_label(gender_raw)
    desc_in = (character_description_in or "").strip()
    if desc_in:
        char_desc = desc_in
    elif character_urls:
        if gender_label == "male":
            char_desc = (
                "Male UGC creator — strictly match the uploaded character reference portrait(s); "
                "same face, hair, and styling in every talking-head cell."
            )
        elif gender_label == "female":
            char_desc = (
                "Female UGC creator — strictly match the uploaded character reference portrait(s); "
                "same face, hair, and styling in every talking-head cell."
            )
        else:
            char_desc = (
                "UGC creator — strictly match the uploaded character reference portrait(s); "
                "same identity in every talking-head cell."
            )
    else:
        if gender_label == "male":
            char_desc = "Male UGC creator, natural and approachable."
        elif gender_label == "female":
            char_desc = "Female UGC creator, natural and approachable."
        else:
            char_desc = "Natural, approachable UGC creator."

    if character_urls:
        ref_c = ", ".join(character_urls[:4])
        char_desc = (
            f"{char_desc.strip()} Locked character reference portrait URL(s) — the face, hair, age, and gender must "
            f"match these images exactly in every talking-head cell (no stock replacement model): {ref_c}"
        )

    prod_lines = [
        f"Offer type: {offer_type}",
        f"Problem / pain point: {main_problem}",
        f"Benefits / what we sell: {key_benefits}",
    ]
    if product_image_urls:
        url_sample = ", ".join(product_image_urls[:5])
        prod_lines.append(
            f"Product images attached: {len(product_image_urls)} image URL(s). The grid must depict THIS exact product "
            f"(shape, color, materials, branding) — never a generic substitute. Reference URLs: {url_sample}"
        )
    if reference_image_urls:
        ref_sample = ", ".join(reference_image_urls[:5])
        prod_lines.append(
            f"Reference images attached: {len(reference_image_urls)} URL(s). Use for accurate product, app, or service visuals. "
            f"Reference URLs: {ref_sample}"
        )
    product_description = "\n".join(prod_lines)
    return gender_label, char_desc, product_description


def _ensure_lipsync_cell_vo_clips(
    processor: Any,
    *,
    lipsync_cells: List[Dict[str, Any]],
    cell_vo_audio: Dict[str, Any],
    voice_id: Optional[str],
    language: str,
) -> Tuple[Dict[str, str], bool]:
    """Upload one ElevenLabs MP3 per lip-sync cell (that cell's ``voice_line`` only).

    Kling Avatar Pro expects **short audio aligned to the still image**, not the full multi-cell VO.
    Returns ``(merged url map, True if any new clip was uploaded)``.
    """
    out: Dict[str, str] = {}
    for k, v in (cell_vo_audio or {}).items():
        if v and str(v).strip().startswith("http"):
            out[str(k)] = str(v).strip()
    added = False
    el = getattr(processor, "elevenlabs_service", None)
    gcs = getattr(processor, "gcs_storage_service", None)
    if not el or not gcs:
        return out, added
    for route_entry in lipsync_cells:
        ci = int(route_entry.get("cell_index") or 0)
        line = str(route_entry.get("voice_line") or "").strip()
        sk = str(ci)
        if ci < 1 or not line:
            continue
        if out.get(sk):
            continue
        tts_one = el.text_to_speech_with_timestamps(line, voice_id=voice_id, language=language)
        if not tts_one:
            logger.warning("UGC Real: ElevenLabs returned no audio for lip-sync cell %s", ci)
            continue
        b, _ = tts_one
        u = gcs.upload_audio_bytes(
            b,
            key_name=f"ugc_real/vo_lipsync_cell_{ci}_{abs(hash(line)) % 10_000_000}.mp3",
        ) or ""
        if u:
            out[sk] = u
            added = True
            logger.info("UGC Real: uploaded lip-sync VO clip for cell %s (%d chars)", ci, len(line))
    return out, added


def _normalize_nine_cell_lipsync_flags(cells: List[Dict[str, Any]]) -> bool:
    """Enforce exactly 3 lip-sync cells at positions 1, 5, 9 (indices 0, 4, 8).

    Matches ``generate_nine_cell_plan`` post-processing. Old checkpoints or hand-edited
    JSON often leave all ``lipsync`` false or wrong — that would skip Kling Avatar for every cell.

    Returns True if any cell was modified.
    """
    if not isinstance(cells, list) or len(cells) != 9:
        return False
    changed = False
    for i, c in enumerate(cells):
        if not isinstance(c, dict):
            continue
        want = i in (0, 4, 8)
        cur = bool(c.get("lipsync"))
        if cur != want:
            c["lipsync"] = want
            changed = True
    return changed


def _split_text3_cta(raw: str) -> Tuple[str, str]:
    t = (raw or "").strip()
    if not t:
        return "", ""
    m = re.search(r"(?:^|\n)\s*CTA\s*:\s*(.+)$", t, flags=re.IGNORECASE | re.DOTALL)
    if m:
        lead = t[: m.start()].strip()
        cta = (m.group(1) or "").strip()
        return lead, cta
    return t, ""


def _legacy_structured_complete(
    offer_type: str,
    target_audience: str,
    main_problem: str,
    key_benefits: str,
    cta_text: str,
) -> bool:
    o = (offer_type or "").strip().lower()
    if o not in _VALID_OFFER_TYPES:
        return False
    return all(_has_value(x) for x in (target_audience, main_problem, key_benefits, cta_text))


def _parse_ugc_real_prompt_llm(processor, prompt: str) -> Dict[str, Any]:
    loader = get_prompt_loader()
    system_prompt = loader.get("ugc_real_parse_prompt_system")
    user_prompt = loader.get("ugc_real_parse_prompt_user", prompt=prompt or "")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    fallback: Dict[str, Any] = {
        "offer_type": "service",
        "offer_category": "",
        "target_audience": "General audience interested in the offer",
        "main_problem": "Unclear positioning or slow results",
        "key_benefits": (prompt or "")[:500],
        "cta_text": "Learn more",
        "ad_format": "talking_head",
    }
    try:
        result = processor._call_llm(
            "ugc_real_parse_prompt",
            messages,
            temperature=0.2,
            max_tokens=2048,
            responseSchema=UGC_REAL_PARSE_SCHEMA,
        )
        raw = (result or {}).get("text", "") or ""
        data: Dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                data = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                data = extract_json_from_response(raw) or {}
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        logger.warning("UGC Real prompt parse failed, using fallback: %s", e)
        return fallback
    if not isinstance(data, dict):
        return fallback
    out = {
        "offer_type": _normalize_offer_type(str(data.get("offer_type", "") or "service")),
        "offer_category": str(data.get("offer_category", "") or "").strip(),
        "target_audience": str(data.get("target_audience", "") or "").strip(),
        "main_problem": str(data.get("main_problem", "") or "").strip(),
        "key_benefits": str(data.get("key_benefits", "") or "").strip(),
        "cta_text": str(data.get("cta_text", "") or "").strip(),
        "ad_format": _normalize_ad_format(str(data.get("ad_format", "") or "talking_head")),
    }
    if not out["target_audience"]:
        out["target_audience"] = fallback["target_audience"]
    if not out["main_problem"]:
        out["main_problem"] = fallback["main_problem"]
    if not out["key_benefits"]:
        out["key_benefits"] = fallback["key_benefits"]
    if not out["cta_text"]:
        out["cta_text"] = fallback["cta_text"]
    return out


def _merge_manifest_cells_into_grid_cells(
    grid_cells: List[Dict[str, Any]], grid_manifests: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Attach manifest cell metadata (face_visible, shot_type, …) to cut cell dicts for classification."""
    manifest_by_scene = {m.get("scene_id"): m for m in (grid_manifests or [])}
    out: List[Dict[str, Any]] = []
    for block in grid_cells or []:
        sid = block.get("scene_id")
        manifest = manifest_by_scene.get(sid) or {}
        meta_by_index = {
            int(c.get("cell_index") or 0): c for c in (manifest.get("cells") or [])
        }
        merged_cells: List[Dict[str, Any]] = []
        for uc in block.get("cells") or []:
            idx = int(uc.get("cell_index") or 0)
            meta = dict(meta_by_index.get(idx) or {})
            merged = {**meta, **uc}
            merged["cell_index"] = idx
            merged_cells.append(merged)
        out.append({"scene_id": sid, "cells": merged_cells})
    return out


def _build_grid_prompt(scene: Dict[str, Any], manifest: Dict[str, Any], scene_index: int) -> str:
    cell_lines = []
    for cell in (manifest.get("cells") or []):
        cell_lines.append(
            f"- Cell {cell['cell_index']}: {cell.get('description', '')} "
            f"(shot_type={cell.get('shot_type', 'unknown')}, framing={cell.get('framing', 'auto')}, "
            f"emotion={cell.get('emotion', 'natural')})"
        )
    return (
        f"Create a 3x3 storyboard grid for UGC ad scene {scene_index + 1}.\n"
        f"Scene purpose: {scene.get('purpose', 'benefit')}\n"
        f"Primary message: {scene.get('primary_message', '')}\n"
        f"Voice line: {scene.get('voice_line', '')}\n"
        f"Continuity anchor: {scene.get('continuity_anchor', scene.get('location', 'ugc_world'))}\n"
        f"Shot family: {scene.get('shot_family', scene.get('purpose', 'ugc_story'))}\n"
        "Keep the same character identity, outfit baseline, and world continuity across all cells. "
        "Natural UGC realism. Strong variety in camera angle, framing, and expression without drifting identity.\n"
        + "\n".join(cell_lines)
    )


def _estimate_duration_from_segments(word_segments: List[Dict[str, Any]]) -> Optional[float]:
    last_end = 0.0
    for seg in word_segments or []:
        try:
            last_end = max(last_end, float(seg.get("end", 0.0) or 0.0))
        except Exception:
            continue
    return round(last_end, 2) if last_end > 0 else None


def _scene_duration_plan(scenes: List[Dict[str, Any]], target_duration: int) -> List[float]:
    count = max(1, len(scenes or []))
    avg = max(2.5, round(float(target_duration or 30) / count, 2))
    out: List[float] = []
    for scene in scenes or []:
        raw = scene.get("duration_sec")
        try:
            dur = float(raw)
            if dur <= 0:
                dur = avg
        except Exception:
            dur = avg
        out.append(round(max(2.0, dur), 2))
    return out


def _count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", str(text or "").strip(), flags=re.UNICODE))


def _compute_cell_durations_from_vo(
    cells: List[Dict[str, Any]],
    *,
    vo_duration: Optional[float],
    fallback_total_duration: float,
    min_cell_duration: float = 1.6,
    end_buffer: float = 1.2,
) -> List[float]:
    """Build per-cell durations that track actual VO length.

    Uses each cell's `voice_line` word count as weight so longer spoken ideas get more screen time.
    """
    cell_count = len(cells or [])
    if cell_count <= 0:
        return []
    target_total = float(vo_duration or 0.0)
    if target_total <= 0:
        target_total = float(fallback_total_duration or 0.0)
    target_total = max(target_total + max(0.0, end_buffer), cell_count * float(min_cell_duration))
    word_weights: List[float] = []
    for c in cells:
        wc = _count_words(str(c.get("voice_line") or ""))
        # Keep a small baseline for cells with little/no spoken text (B-roll continuity).
        word_weights.append(max(2.0, float(wc)))
    total_weight = sum(word_weights) or float(cell_count)
    durations: List[float] = [max(float(min_cell_duration), round(target_total * (w / total_weight), 2)) for w in word_weights]
    # Normalize to keep exact total coverage.
    diff = round(target_total - sum(durations), 2)
    if abs(diff) > 0.01:
        durations[-1] = round(max(float(min_cell_duration), durations[-1] + diff), 2)
    return durations


def _choose_static_hold_url(
    scene_id: str,
    frame_classifications: List[Dict[str, Any]],
    grid_cells: List[Dict[str, Any]],
    scene_grids: List[str],
    scenes: List[Dict[str, Any]],
) -> str:
    candidate = _pick_first_cell(scene_id, frame_classifications, "static")
    cell_set = next((c for c in grid_cells if c.get("scene_id") == scene_id), {})
    cells = cell_set.get("cells") or []
    if candidate:
        cell_index = int(candidate.get("cell_index") or 1)
        source_cell = next((c for c in cells if int(c.get("cell_index") or 0) == cell_index), None)
        source_url = (source_cell or {}).get("image_url") or ""
        if source_url:
            return source_url
    idx = next((i for i, s in enumerate(scenes or []) if s.get("scene_id") == scene_id), -1)
    if idx >= 0 and idx < len(scene_grids):
        return scene_grids[idx] or ""
    return ""


_FALLBACK_VP_RE = re.compile(r"^UGC scene \d+$", re.IGNORECASE)
_FALLBACK_VL_RE = re.compile(r"^Scene \d+ (line|voiceover)$", re.IGNORECASE)


def _nine_cell_plan_ready_for_master_grid(plan: Dict[str, Any]) -> Tuple[bool, str]:
    """Block master grid generation until each cell has real image + VO text.

    Rejects empty fields AND known fallback placeholder patterns so the pipeline
    never sends garbage to Nano Banana.
    """
    if not isinstance(plan, dict):
        return False, "nine_cell_plan is missing or invalid"
    cells = plan.get("cells")
    if not isinstance(cells, list) or len(cells) != 9:
        n = len(cells) if isinstance(cells, list) else 0
        return False, f"expected 9 cells in nine_cell_plan, got {n}"
    placeholder_count = 0
    for i, c in enumerate(cells):
        if not isinstance(c, dict):
            return False, f"cell {i + 1} is not an object"
        vp = str(c.get("visual_prompt") or "").strip()
        vl = str(c.get("voice_line") or "").strip()
        if not vp or not vl:
            return (
                False,
                f"cell {i + 1} is missing visual_prompt or voice_line — regenerate or fix the nine-cell plan before the 3x3 grid",
            )
        if _FALLBACK_VP_RE.match(vp) or _FALLBACK_VL_RE.match(vl):
            placeholder_count += 1
    if placeholder_count > 2:
        return (
            False,
            f"{placeholder_count} of 9 cells contain fallback placeholder text — the LLM did not produce a real storyboard",
        )
    return True, ""


def _emit_restored_step(
    on_progress,
    *,
    step: str,
    label: str,
    progress: int,
    message: str,
    intermediates: Dict[str, Any],
) -> None:
    for key, value in intermediates.items():
        _emit(on_progress, "intermediate", {"key": key, "value": value})
    _emit_step(on_progress, step, label, progress, message)


def process_ugc_real_video(
    processor,
    *,
    prompt: str = "",
    offer_type: str = "service",
    offer_category: str = "",
    target_audience: str = "",
    main_problem: str = "",
    key_benefits: str = "",
    cta_text: str = "",
    ad_format: str = "talking_head",
    duration: int = 30,
    target_duration: Optional[int] = None,
    variation_count: int = 1,
    language: str = "en",
    visual_style: str = "Auto",
    image_model: str = "nano-banana-pro",
    image_provider: str = "kie",
    image_resolution: str = "1K",
    video_model: str = "veo-3.1-fast",
    video_provider: str = "direct",
    video_resolution: str = "720p",
    character_urls: Optional[List[str]] = None,
    reference_image_urls: Optional[List[str]] = None,
    product_image_urls: Optional[List[str]] = None,
    logo_url: str = "",
    slogan_text: str = "",
    voice_id: Optional[str] = None,
    add_subtitles: bool = True,
    subtitle_position: str = "middle",
    dissolve_seconds: float = 0.3,
    text_1: str = "",
    text_2: str = "",
    text_3: str = "",
    on_progress=None,
    existing_intermediates: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Run UGC Real generation flow."""
    tgt_duration = int(target_duration or duration or 30)
    existing_intermediates = dict(existing_intermediates or {})
    character_urls = [u for u in (character_urls or []) if u]
    _char_url_kw = (kwargs.get("character_url") or "").strip()
    if _char_url_kw and _char_url_kw not in character_urls:
        character_urls.insert(0, _char_url_kw)
    reference_image_urls = [u for u in (reference_image_urls or []) if u]
    product_image_urls = [u for u in (product_image_urls or []) if u]
    if not character_urls:
        character_urls = [u for u in (existing_intermediates.get("character_urls") or []) if u]

    text_1 = text_1 or kwargs.get("text_1") or ""
    text_2 = text_2 or kwargs.get("text_2") or ""
    text_3 = text_3 or kwargs.get("text_3") or ""

    ck_intake = existing_intermediates.get("ugc_real_intake")
    parsed_this_run = False
    base: Optional[Dict[str, Any]] = None

    if isinstance(ck_intake, dict) and _has_value(ck_intake.get("target_audience")):
        base = {
            "offer_type": _normalize_offer_type(str(ck_intake.get("offer_type", "") or "service")),
            "offer_category": str(ck_intake.get("offer_category", "") or "").strip(),
            "target_audience": str(ck_intake.get("target_audience", "") or "").strip(),
            "main_problem": str(ck_intake.get("main_problem", "") or "").strip(),
            "key_benefits": str(ck_intake.get("key_benefits", "") or "").strip(),
            "cta_text": str(ck_intake.get("cta_text", "") or "").strip(),
            "ad_format": _normalize_ad_format(str(ck_intake.get("ad_format", "") or "talking_head")),
        }
    elif _legacy_structured_complete(offer_type, target_audience, main_problem, key_benefits, cta_text):
        base = {
            "offer_type": _normalize_offer_type(offer_type),
            "offer_category": (offer_category or "").strip(),
            "target_audience": (target_audience or "").strip(),
            "main_problem": (main_problem or "").strip(),
            "key_benefits": (key_benefits or "").strip(),
            "cta_text": (cta_text or "").strip(),
            "ad_format": _normalize_ad_format(ad_format),
        }
    elif _has_value(text_1) and _has_value(text_2) and _has_value(text_3) and not _has_value(
        existing_intermediates.get("offer_profile")
    ):
        kb, cta_line = _split_text3_cta(text_3)
        base = {
            "offer_type": _normalize_offer_type(offer_type),
            "offer_category": (offer_category or "").strip(),
            "target_audience": text_1.strip(),
            "main_problem": text_2.strip(),
            "key_benefits": kb,
            "cta_text": (cta_line or (cta_text or "").strip()).strip(),
            "ad_format": _normalize_ad_format(ad_format),
        }
    elif not _has_value(existing_intermediates.get("offer_profile")):
        if not _has_value(prompt):
            raise ValueError(
                "UGC Real requires a non-empty prompt when structured offer fields are not provided"
            )
        base = _parse_ugc_real_prompt_llm(processor, prompt)
        parsed_this_run = True

    if base is None:
        base = {
            "offer_type": _normalize_offer_type(offer_type),
            "offer_category": (offer_category or "").strip(),
            "target_audience": (target_audience or "").strip(),
            "main_problem": (main_problem or "").strip(),
            "key_benefits": (key_benefits or "").strip(),
            "cta_text": (cta_text or "").strip(),
            "ad_format": _normalize_ad_format(ad_format),
        }

    if _has_value(text_1) and _has_value(text_2) and _has_value(text_3):
        kb, cta_line = _split_text3_cta(text_3)
        base["target_audience"] = text_1.strip()
        base["main_problem"] = text_2.strip()
        base["key_benefits"] = kb
        if (cta_line or "").strip():
            base["cta_text"] = cta_line.strip()

    offer_type = base["offer_type"]
    offer_category = base["offer_category"]
    target_audience = base["target_audience"]
    main_problem = base["main_problem"]
    key_benefits = base["key_benefits"]
    cta_text = base["cta_text"]
    ad_format = base["ad_format"]

    gender_label, ugc_character_description, product_description_for_grid = (
        _ugc_real_resolve_character_and_product_context(
            gender_raw=str(kwargs.get("gender") or "f"),
            character_description_in=str(kwargs.get("character_description") or "").strip(),
            character_urls=character_urls,
            offer_type=offer_type,
            main_problem=main_problem,
            key_benefits=key_benefits,
            product_image_urls=product_image_urls,
            reference_image_urls=reference_image_urls,
        )
    )

    # Align TTS with creator gender when API omitted or sent an invalid voice_id (ElevenLabs otherwise
    # falls back to Config.DEFAULT_VOICE_ID, which may not match the selected gender).
    _vi_raw = (voice_id or "").strip() if voice_id else ""
    if not is_valid_voice_id(_vi_raw):
        _g_voice = gender_label if gender_label in ("male", "female") else "female"
        _resolved = get_language_voice(language, _g_voice)
        if _resolved:
            voice_id = _resolved
            logger.info(
                "UGC Real: voice_id resolved from gender=%s language=%s → %s",
                _g_voice,
                language,
                _resolved,
            )

    text3_display = (
        f"{key_benefits}\n\nCTA: {cta_text}" if _has_value(cta_text) else key_benefits
    )
    parsed_texts_val = {
        "text_1": target_audience,
        "text_2": main_problem,
        "text_3": text3_display,
    }

    if isinstance(ck_intake, dict) and _has_value(ck_intake.get("target_audience")) and not parsed_this_run:
        _emit_restored_step(
            on_progress,
            step="step_parse",
            label="Parse UGC Real Brief",
            progress=3,
            message="Offer brief restored from checkpoint",
            intermediates={"ugc_real_intake": base, "parsed_texts": parsed_texts_val},
        )
    else:
        _emit_step_start(on_progress, "step_parse", "Parse UGC Real Brief", "Extracting offer profile from your prompt...")
        _emit(on_progress, "intermediate", {"key": "ugc_real_intake", "value": base})
        _emit(on_progress, "intermediate", {"key": "parsed_texts", "value": parsed_texts_val})
        _emit_step(
            on_progress,
            "step_parse",
            "Parse UGC Real Brief",
            3,
            "Offer brief ready" if parsed_this_run else "Offer brief resolved",
        )

    ad_context = {
        "offer_type": offer_type,
        "offer_category": offer_category,
        "target_audience": target_audience,
        "main_problem": main_problem,
        "key_benefits": key_benefits,
        "cta_text": cta_text,
        "delivery_format": kwargs.get("delivery_format", ""),
        "device_type": kwargs.get("device_type", ""),
        "ad_format": ad_format,
        "pace": kwargs.get("pace", ""),
        "realism_level": kwargs.get("realism_level"),
        "drama_level": kwargs.get("drama_level"),
        "language": language,
        "target_duration": tgt_duration,
    }
    _emit(on_progress, "intermediate", {"key": "ad_context", "value": ad_context})

    # Step 0: offer analysis
    if _has_value(existing_intermediates.get("offer_profile")):
        offer_profile = dict(existing_intermediates.get("offer_profile") or {})
        _emit_restored_step(
            on_progress,
            step="step_0",
            label="Offer Analysis",
            progress=6,
            message="Offer analysis restored from checkpoint",
            intermediates={"offer_profile": offer_profile},
        )
    else:
        _emit_step_start(on_progress, "step_0", "Offer Analysis", "Analyzing the offer and required ad patterns...")
        offer_profile = analyze_offer(
            processor._call_llm,
            offer_type=offer_type,
            description=prompt,
            benefits=key_benefits,
            audience=target_audience,
            main_problem=main_problem,
            offer_category=offer_category,
            cta_text=cta_text,
            delivery_format=kwargs.get("delivery_format", ""),
            device_type=kwargs.get("device_type", ""),
            ad_format=ad_format,
            pace=kwargs.get("pace", ""),
            realism_level=kwargs.get("realism_level"),
            drama_level=kwargs.get("drama_level"),
        )
        _emit(on_progress, "intermediate", {"key": "offer_profile", "value": offer_profile})
        _emit_step(on_progress, "step_0", "Offer Analysis", 6, "Offer analysis complete")

    # Step 0.5: strategy
    if _has_value(existing_intermediates.get("creative_strategy")):
        creative_strategy = dict(existing_intermediates.get("creative_strategy") or {})
        _emit_restored_step(
            on_progress,
            step="step_0.5",
            label="Creative Strategy",
            progress=12,
            message="Creative strategy restored from checkpoint",
            intermediates={"creative_strategy": creative_strategy},
        )
    else:
        _emit_step_start(on_progress, "step_0.5", "Creative Strategy", "Choosing the creative angle, hook, and proof structure...")
        creative_strategy = generate_creative_strategy(
            processor._call_llm,
            offer_profile=offer_profile,
            audience=target_audience,
            duration=tgt_duration,
            ad_format=ad_format,
            pace=kwargs.get("pace", ""),
            realism_level=kwargs.get("realism_level"),
            drama_level=kwargs.get("drama_level"),
            cta_text=cta_text,
        )
        _emit(on_progress, "intermediate", {"key": "creative_strategy", "value": creative_strategy})
        _emit_step(on_progress, "step_0.5", "Creative Strategy", 12, "Creative strategy complete")

    # Step 1: nine-cell plan (fixed 9 segments)
    if _has_value(existing_intermediates.get("nine_cell_plan")):
        nine_cell_plan = dict(existing_intermediates.get("nine_cell_plan") or {})
        cells = nine_cell_plan.get("cells") or []
        _emit_restored_step(
            on_progress,
            step="step_1",
            label="Nine-Cell Plan",
            progress=18,
            message=f"Nine-cell plan restored from checkpoint ({len(cells)} cells)",
            intermediates={"nine_cell_plan": nine_cell_plan},
        )
    else:
        _emit_step_start(on_progress, "step_1", "Nine-Cell Plan", "Planning 9-cell storyboard with VO lines...")
        nine_cell_plan = generate_nine_cell_plan(
            processor._call_llm,
            offer_profile=offer_profile,
            creative_strategy=creative_strategy,
            duration=tgt_duration,
            language=language,
            offer_type=offer_type,
            cta_text=cta_text,
            ad_format=ad_format,
            target_audience=target_audience,
            main_problem=main_problem,
            key_benefits=key_benefits,
            original_prompt=prompt or "",
            character_description=ugc_character_description,
            gender=gender_label,
        )
        _emit(on_progress, "intermediate", {"key": "nine_cell_plan", "value": nine_cell_plan})
        _emit_step(on_progress, "step_1", "Nine-Cell Plan", 18, "Nine-cell plan ready")

    cells = nine_cell_plan.get("cells") or []
    if _normalize_nine_cell_lipsync_flags(cells):
        nine_cell_plan["cells"] = cells
        _emit(on_progress, "intermediate", {"key": "nine_cell_plan", "value": nine_cell_plan})

    for _qi, _qc in enumerate(cells):
        _qvp = str(_qc.get("visual_prompt") or "").strip()
        _qvl = str(_qc.get("voice_line") or "").strip()
        if len(_qvp) < 20:
            _emit(on_progress, "warning", {
                "message": f"Cell {_qi + 1} visual_prompt is very short ({len(_qvp)} chars) — consider editing before continuing",
            })
        if not _qvl:
            _emit(on_progress, "warning", {
                "message": f"Cell {_qi + 1} has no voice_line — the VO segment will be empty",
            })
    cell_durations = [float(c.get("duration_seconds") or max(2.0, float(tgt_duration) / 9.0)) for c in cells]

    # Step 2: style DNA extraction
    if _has_value(existing_intermediates.get("style_dna")):
        style_dna = dict(existing_intermediates.get("style_dna") or {})
        _emit_restored_step(
            on_progress,
            step="step_2",
            label="Style DNA",
            progress=24,
            message="Style DNA restored from checkpoint",
            intermediates={"style_dna": style_dna},
        )
    else:
        _emit_step_start(on_progress, "step_2", "Style DNA", "Extracting visual DNA for consistent grid generation...")
        style_dna = extract_style_dna(
            processor._call_llm,
            character_description=ugc_character_description,
            gender=gender_label,
            visual_style=visual_style,
            offer_type=offer_type,
            ad_format=ad_format,
        )
        _emit(on_progress, "intermediate", {"key": "style_dna", "value": style_dna})
        _emit_step(on_progress, "step_2", "Style DNA", 24, "Style DNA extracted")

    # Step 3: master grid prompt + single 3x3 grid image
    master_grid_regenerated_this_run = False
    if _has_value(existing_intermediates.get("master_grid_prompt")) and _has_value(existing_intermediates.get("grid_image_url")):
        master_grid_prompt = str(existing_intermediates.get("master_grid_prompt") or "")
        grid_image_url = str(existing_intermediates.get("grid_image_url") or "")
        _emit_restored_step(
            on_progress,
            step="step_3",
            label="Grid Generation",
            progress=34,
            message="Grid image restored from checkpoint",
            intermediates={"master_grid_prompt": master_grid_prompt, "grid_image_url": grid_image_url},
        )
    else:
        _ok_grid, _grid_gate_msg = _nine_cell_plan_ready_for_master_grid(nine_cell_plan)
        if not _ok_grid:
            raise RuntimeError(f"UGC Real: {_grid_gate_msg}")
        _emit_step_start(on_progress, "step_3", "Grid Generation", "Building master grid prompt and generating 3x3 image...")
        _ugc_grid_layout = str(get_pipeline_defaults().get("ugc_real_grid_layout") or "3x3")
        master_grid_prompt = build_master_grid_prompt(
            processor._call_llm,
            style_dna=style_dna,
            nine_cell_plan=nine_cell_plan,
            offer_type=offer_type,
            visual_style=visual_style,
            character_description=ugc_character_description,
            product_description=product_description_for_grid,
            gender=gender_label,
            image_resolution=image_resolution,
            grid_layout=_ugc_grid_layout,
        )
        _emit(on_progress, "intermediate", {"key": "master_grid_prompt", "value": master_grid_prompt})
        # Merge reference + product URLs into product_reference_urls only. Older KieAIService.generate_scene_image
        # did not accept reference_image_urls; refs are still passed as image_input when product_visible is True.
        _grid_ref_urls: List[str] = []
        for u in product_image_urls:
            if u and u not in _grid_ref_urls:
                _grid_ref_urls.append(u)
        for u in reference_image_urls:
            if u and u not in _grid_ref_urls:
                _grid_ref_urls.append(u)
        _grid_has_ref_assets = bool(_grid_ref_urls)
        nb_cfg = get_kie_config().get("nano_banana") or {}
        nb_model_id = str(nb_cfg.get("model") or "").lower()
        _master_grid_res = image_resolution
        if "nano-banana-2" in nb_model_id:
            _cfg_res = nb_cfg.get("ugc_real_master_grid_resolution")
            if _cfg_res:
                _master_grid_res = str(_cfg_res).strip()
        if image_provider == "kie":
            _master_grid_res = str(_master_grid_res).strip() or "1K"
        _grid_kw: Dict[str, Any] = {
            "image_model": image_model,
            "image_provider": image_provider,
            "resolution": _master_grid_res,
            "image_prompt": master_grid_prompt,
            "visual_style": visual_style,
            "character_reference_urls": character_urls or None,
            "has_character": bool(character_urls),
            "product_visible": (offer_type == "physical_product") or _grid_has_ref_assets,
            "product_reference_urls": _grid_ref_urls or None,
            "logo_reference_url": logo_url or None,
        }
        _layout_ref = get_ugc_real_master_grid_layout_reference_url(processor)
        if _layout_ref:
            _grid_kw["prepend_reference_urls"] = [_layout_ref]
        grid_image_url = processor._generate_image(**_grid_kw)
        master_grid_regenerated_this_run = True
        _emit(on_progress, "intermediate", {"key": "grid_image_url", "value": grid_image_url})
        _emit_step(on_progress, "step_3", "Grid Generation", 34, "Grid image generated")

    # Step 4: grid cutting — single image to 9 cells
    # Do not reuse checkpoint grid_cells if the master grid image changed: animation/lip-sync
    # would still point at URLs cropped from the previous session's composite.
    _gurl_norm = str(grid_image_url or "").strip()
    _cut_provenance = str(existing_intermediates.get("ugc_grid_cut_from_url") or "").strip()
    _use_checkpoint_cells = _has_value(existing_intermediates.get("grid_cells"))
    if _use_checkpoint_cells and master_grid_regenerated_this_run:
        _use_checkpoint_cells = False
    if _use_checkpoint_cells and _cut_provenance and _gurl_norm and _cut_provenance != _gurl_norm:
        _use_checkpoint_cells = False
    if _use_checkpoint_cells and (not _cut_provenance) and master_grid_regenerated_this_run:
        _use_checkpoint_cells = False

    _invalidate_downstream_grid = False

    if _use_checkpoint_cells:
        grid_cells = list(existing_intermediates.get("grid_cells") or [])
        _emit_restored_step(
            on_progress,
            step="step_4",
            label="Grid Cutting",
            progress=44,
            message="Grid cells restored from checkpoint",
            intermediates={
                "grid_cells": grid_cells,
                "ugc_grid_cut_from_url": _gurl_norm or _cut_provenance,
            },
        )
    else:
        _invalidate_downstream_grid = True
        _emit_step_start(on_progress, "step_4", "Grid Cutting", "Splitting 3x3 grid into 9 individual cell images...")
        grid_cells = []
        if grid_image_url:
            raw_cells = cut_grid_image_url(grid_image_url, "3x3")
            for rc in raw_cells:
                cell_url = ""
                if processor.gcs_storage_service:
                    cell_url = processor.gcs_storage_service.upload_image_bytes(
                        rc["image_bytes"],
                        key_name=f"ugc_real/grid_cell_{rc['cell_index']}.jpg",
                        content_type="image/jpeg",
                    )
                plan_cell = cells[rc["cell_index"] - 1] if rc["cell_index"] - 1 < len(cells) else {}
                grid_cells.append({
                    "cell_index": rc["cell_index"],
                    "bbox": rc["bbox"],
                    "image_url": cell_url or grid_image_url,
                    "visual_prompt": plan_cell.get("visual_prompt", ""),
                    "voice_line": plan_cell.get("voice_line", ""),
                    "lipsync": plan_cell.get("lipsync", False),
                    "shot_role": plan_cell.get("shot_role", "b_roll"),
                })
        _emit(on_progress, "intermediate", {"key": "grid_cells", "value": grid_cells})
        _emit(on_progress, "intermediate", {"key": "ugc_grid_cut_from_url", "value": _gurl_norm})
        _emit_step(on_progress, "step_4", "Grid Cutting", 44, f"Grid cut into {len(grid_cells)} cells")

    # Step 5: frame routing — always derived from current cells + grid_cells (do not reuse stale
    # ``frame_routing`` from checkpoint; it may have been saved when lipsync flags were wrong).
    if _has_value(existing_intermediates.get("frame_routing")) and not grid_cells:
        frame_routing = list(existing_intermediates.get("frame_routing") or [])
        _emit_restored_step(
            on_progress,
            step="step_5",
            label="Frame Routing",
            progress=52,
            message="Frame routing restored from checkpoint (no grid cells to recompute)",
            intermediates={"frame_routing": frame_routing},
        )
    else:
        _emit_step_start(on_progress, "step_5", "Frame Routing", "Routing cells to Kling Avatar vs I2V animation...")
        frame_routing = []
        for gc in grid_cells:
            ci = int(gc.get("cell_index") or 0)
            plan_cell = cells[ci - 1] if 0 < ci <= len(cells) else {}
            is_lip = bool(plan_cell.get("lipsync") or gc.get("lipsync"))
            frame_routing.append({
                "cell_index": ci,
                "route": "kling_avatar" if is_lip else "i2v_animation",
                "shot_role": plan_cell.get("shot_role") or gc.get("shot_role", "b_roll"),
                "voice_line": plan_cell.get("voice_line") or gc.get("voice_line", ""),
                "lipsync": is_lip,
            })
        _emit(on_progress, "intermediate", {"key": "frame_routing", "value": frame_routing})
        _emit_step(on_progress, "step_5", "Frame Routing", 52, "Frame routing complete")

    # Step 6: TTS — full VO script + per-cell audio for lipsync cells
    lipsync_cells = [r for r in frame_routing if r.get("lipsync")]
    if _has_value(existing_intermediates.get("vo_script")) and (
        _has_value(existing_intermediates.get("vo_audio_url")) or _has_value(existing_intermediates.get("cell_vo_audio"))
    ):
        vo_script = str(existing_intermediates.get("vo_script") or "")
        vo_audio_url = str(existing_intermediates.get("vo_audio_url") or "")
        vo_word_segments = list(existing_intermediates.get("vo_word_segments") or [])
        cell_vo_audio = dict(existing_intermediates.get("cell_vo_audio") or existing_intermediates.get("scene_vo_audio") or {})
        vo_duration = existing_intermediates.get("vo_duration")
        if vo_duration is None:
            vo_duration = _estimate_duration_from_segments(vo_word_segments)
        _emit_restored_step(
            on_progress,
            step="step_6",
            label="Voiceover",
            progress=62,
            message="Voiceover restored from checkpoint",
            intermediates={
                "vo_script": vo_script,
                "vo_audio_url": vo_audio_url,
                "cell_vo_audio": cell_vo_audio,
                "vo_word_segments": vo_word_segments,
                "vo_duration": vo_duration,
            },
        )
        if cells:
            cell_durations = _compute_cell_durations_from_vo(
                cells,
                vo_duration=vo_duration,
                fallback_total_duration=float(tgt_duration or 30),
            )
            _emit(on_progress, "intermediate", {"key": "cell_durations", "value": cell_durations})
    else:
        _emit_step_start(
            on_progress,
            "step_6",
            "Voiceover",
            "Generating ElevenLabs clip per lip-sync cell, then full VO for assembly…",
        )
        all_lines = [str(c.get("voice_line") or "").strip() for c in cells if str(c.get("voice_line") or "").strip()]
        vo_script = " ||| ".join(all_lines)
        if not vo_script:
            vo_script = f"{prompt.strip()} {cta_text.strip()}".strip() or "UGC Real voiceover"
        vo_audio_url = ""
        vo_word_segments = []
        # One MP3 per lip-sync cell only (required for Kling Avatar — not the full concatenated VO).
        cell_vo_audio, _ = _ensure_lipsync_cell_vo_clips(
            processor,
            lipsync_cells=lipsync_cells,
            cell_vo_audio={},
            voice_id=voice_id,
            language=language,
        )
        tts_full = processor.elevenlabs_service.text_to_speech_with_timestamps(
            vo_script, voice_id=voice_id, language=language
        )
        if tts_full:
            audio_bytes, word_segments = tts_full
            vo_word_segments = word_segments or []
            if processor.gcs_storage_service:
                vo_audio_url = processor.gcs_storage_service.upload_audio_bytes(
                    audio_bytes,
                    key_name=f"ugc_real/vo_full_{abs(hash(vo_script)) % 10_000_000}.mp3",
                ) or ""
        vo_duration = _estimate_duration_from_segments(vo_word_segments)
        if cells:
            cell_durations = _compute_cell_durations_from_vo(
                cells,
                vo_duration=vo_duration,
                fallback_total_duration=float(tgt_duration or 30),
            )
        _emit(on_progress, "intermediate", {"key": "vo_script", "value": vo_script})
        _emit(on_progress, "intermediate", {"key": "vo_audio_url", "value": vo_audio_url})
        _emit(on_progress, "intermediate", {"key": "cell_vo_audio", "value": cell_vo_audio})
        _emit(on_progress, "intermediate", {"key": "vo_word_segments", "value": vo_word_segments})
        _emit(on_progress, "intermediate", {"key": "vo_duration", "value": vo_duration})
        _emit(on_progress, "intermediate", {"key": "cell_durations", "value": cell_durations})
        _emit_step(on_progress, "step_6", "Voiceover", 62, "Voiceover generated")

    # Fill any missing per-cell lip-sync clips (e.g. restored checkpoint before Avatar step).
    cell_vo_audio, _lip_vo_added = _ensure_lipsync_cell_vo_clips(
        processor,
        lipsync_cells=lipsync_cells,
        cell_vo_audio=cell_vo_audio,
        voice_id=voice_id,
        language=language,
    )
    if _lip_vo_added:
        _emit(on_progress, "intermediate", {"key": "cell_vo_audio", "value": cell_vo_audio})

    # Step 7: Kling Avatar Pro lip-sync for lipsync cells
    if _has_value(existing_intermediates.get("lip_sync_videos")) and not _invalidate_downstream_grid:
        lip_sync_videos = dict(existing_intermediates.get("lip_sync_videos") or {})
        _emit_restored_step(
            on_progress,
            step="step_7",
            label="Lip Sync Generation",
            progress=72,
            message="Lip-sync clips restored from checkpoint",
            intermediates={"lip_sync_videos": lip_sync_videos},
        )
    else:
        _emit_step_start(
            on_progress,
            "step_7",
            "Lip Sync Generation",
            "Sending lip-sync cells to Kling Avatar Pro (staggered parallel starts per kie.json spacing)…",
        )
        lip_sync_videos = {}
        _spacing_raw = get_kie_config().get("avatar_pro", {}).get("seconds_between_create_tasks")
        _avatar_spacing = float(_spacing_raw) if _spacing_raw is not None else 10.0
        avatar_work: List[Tuple[int, str, str, str]] = []
        for route_entry in lipsync_cells:
            ci = int(route_entry.get("cell_index") or 0)
            gc = next((c for c in grid_cells if int(c.get("cell_index") or 0) == ci), None)
            if not gc:
                continue
            source_image_url = gc.get("image_url") or ""
            lip_audio = (cell_vo_audio.get(str(ci)) or "").strip()
            if not lip_audio:
                _emit(on_progress, "warning", {
                    "message": (
                        f"Lip-sync cell {ci}: no per-cell ElevenLabs audio URL — skipping Kling Avatar for this cell. "
                        "Avatar requires the VO line for this cell only (not the full-track MP3). "
                        "Check ElevenLabs and GCS upload for this line."
                    ),
                })
                continue
            if not source_image_url:
                continue
            voice_line = str(route_entry.get("voice_line", "") or "")
            avatar_work.append((ci, source_image_url, lip_audio, voice_line))

        _lip_lock = threading.Lock()

        def _run_kling_avatar_staggered(slot_index: int, ci: int, image_url: str, audio_url: str, voice_line: str) -> None:
            delay = float(slot_index) * _avatar_spacing if _avatar_spacing > 0 else 0.0
            if delay > 0:
                time.sleep(delay)
            avatar_prompt = f"Natural talking-head UGC shot. Keep identity stable. Line: {voice_line}"
            avatar_video = processor.kie_service.generate_avatar_video(
                image_url=image_url,
                audio_url=audio_url,
                prompt=avatar_prompt,
            )
            with _lip_lock:
                if avatar_video:
                    lip_sync_videos[str(ci)] = avatar_video
                    _emit(
                        on_progress,
                        "usage",
                        {
                            "service": "kie",
                            "provider": "kie",
                            "model": "kling/ai-avatar-pro",
                            "category": "videos",
                            "count": 1,
                            "label": f"lip_sync_cell_{ci}",
                        },
                    )
                else:
                    kie_reason = getattr(processor.kie_service, "last_failure_reason", "") or "no details"
                    _emit(on_progress, "warning", {
                        "message": (
                            f"Kling Avatar Pro failed for lip-sync cell {ci}: {kie_reason}. "
                            "In Kie dashboard open the task for full failMsg. Common causes: image must show a clear "
                            "frontal face (grid crops that are hands/product/profile often fail); image_url and audio_url "
                            "must be publicly reachable (GCS object readable); jpeg/png/webp and audio mp3/wav ≤10MB per Kie docs."
                        ),
                    })

        _threads: List[threading.Thread] = []
        for slot_index, (ci, image_url, audio_url, voice_line) in enumerate(avatar_work):
            th = threading.Thread(
                target=_run_kling_avatar_staggered,
                args=(slot_index, ci, image_url, audio_url, voice_line),
                name=f"ugc_kling_avatar_cell_{ci}",
            )
            _threads.append(th)
            th.start()
        for th in _threads:
            th.join()
        _emit(on_progress, "intermediate", {"key": "lip_sync_videos", "value": lip_sync_videos})
        _emit_step(on_progress, "step_7", "Lip Sync Generation", 72, f"Lip-sync clips generated ({len(lip_sync_videos)} cells)")

    # Step 8: I2V animation for non-lipsync cells
    if _apply_scene_images_to_ugc_real_grid_cells(grid_cells, existing_intermediates.get("scene_images")):
        _emit(on_progress, "intermediate", {"key": "grid_cells", "value": grid_cells})

    _cached_scene_videos = existing_intermediates.get("scene_videos")
    if (
        _scene_videos_list_has_any_url(_cached_scene_videos)
        and not _invalidate_downstream_grid
    ):
        scene_videos = list(_cached_scene_videos or [])
        _emit_restored_step(
            on_progress,
            step="step_8",
            label="Animation Generation",
            progress=82,
            message="Scene videos restored from checkpoint",
            intermediates={"scene_videos": scene_videos},
        )
    else:
        _emit_step_start(on_progress, "step_8", "Animation Generation", "Generating video clips for all 9 cells...")
        scene_videos = [""] * 9
        routing_trace: List[Dict[str, Any]] = []
        route_by_cell: Dict[int, str] = {}
        for r in frame_routing:
            ri = int(r.get("cell_index") or 0)
            if ri > 0:
                route_by_cell[ri] = str(r.get("route") or "i2v_animation")

        for ci_idx in range(9):
            ci = ci_idx + 1
            gc = next((c for c in grid_cells if int(c.get("cell_index") or 0) == ci), None)
            dur = cell_durations[ci_idx] if ci_idx < len(cell_durations) else 4.0
            plan_cell = cells[ci_idx] if ci_idx < len(cells) else {}

            if str(ci) in lip_sync_videos and lip_sync_videos[str(ci)]:
                scene_videos[ci_idx] = lip_sync_videos[str(ci)]
                routing_trace.append({"cell_index": ci, "route": "kling_avatar", "duration_sec": dur, "reason": "lipsync_cell"})
                if on_progress:
                    try:
                        _emit(
                            on_progress,
                            "intermediate",
                            {"key": "scene_videos", "value": [u if u else None for u in scene_videos]},
                        )
                    except Exception as _pe:
                        logger.debug("UGC Real: scene_videos partial emit (avatar): %s", _pe)
                continue

            if route_by_cell.get(ci) == "kling_avatar":
                _emit(on_progress, "warning", {
                    "message": (
                        f"Cell {ci} is scheduled for Kling Avatar (lip-sync) but no avatar video was produced "
                        "(API failed or skipped). Using standard I2V animation for this cell instead. "
                        "Fix Avatar errors in step_7 logs to get real lip-sync on cells 1, 5, and 9."
                    ),
                })

            source_url = (gc or {}).get("image_url") or ""
            motion_prompt = f"Natural UGC motion: {plan_cell.get('visual_prompt', '')}"
            video_url = ""
            route = "i2v_animation"
            if source_url:
                video_url = processor._generate_video(
                    video_model=video_model,
                    video_provider=video_provider,
                    image_url=source_url,
                    motion_prompt=motion_prompt,
                    duration=dur,
                    resolution=video_resolution,
                )
            if not video_url and source_url:
                route = "static_hold_fallback"
                video_url = processor.rendi_service.create_video_from_image(source_url, duration=dur)
            if not video_url and grid_image_url:
                route = "grid_fallback"
                video_url = processor.rendi_service.create_video_from_image(grid_image_url, duration=dur)
            scene_videos[ci_idx] = video_url or ""
            routing_trace.append({"cell_index": ci, "route": route, "duration_sec": dur, "reason": "standard" if route == "i2v_animation" else route})
            # Per-cell emit so Studio polling/SSE can show clips as each Veo/Kling task finishes (not only after all 9).
            if on_progress:
                try:
                    _emit(
                        on_progress,
                        "intermediate",
                        {"key": "scene_videos", "value": [u if u else None for u in scene_videos]},
                    )
                except Exception as _pe:
                    logger.debug("UGC Real: scene_videos partial emit failed: %s", _pe)
        _emit(on_progress, "intermediate", {"key": "scene_videos", "value": scene_videos})
        _emit(on_progress, "intermediate", {"key": "scene_video_plan", "value": routing_trace})
        _emit_step(on_progress, "step_8", "Animation Generation", 82, "All 9 cell videos generated")

    # Step 9: music + assembly
    if (
        not _invalidate_downstream_grid
        and _has_value(existing_intermediates.get("concat_url"))
        and (
            _has_value(existing_intermediates.get("rendi_scene_voice_url"))
            or _has_value(existing_intermediates.get("music_url"))
        )
    ):
        music_url = str(existing_intermediates.get("music_url") or "")
        concat_url = str(existing_intermediates.get("concat_url") or "")
        rendi_scene_voice_url = str(existing_intermediates.get("rendi_scene_voice_url") or concat_url or "")
        _emit_restored_step(
            on_progress,
            step="step_9",
            label="Concatenate + Audio Mix",
            progress=92,
            message="Assembly restored from checkpoint",
            intermediates={
                "music_url": music_url,
                "concat_url": concat_url,
                "rendi_scene_voice_url": rendi_scene_voice_url,
            },
        )
    else:
        _emit_step_start(on_progress, "step_9", "Concatenate + Audio Mix", "Concatenating 9 cells and mixing audio...")
        missing = [i + 1 for i, url in enumerate(scene_videos) if not _has_value(url)]
        if missing:
            raise RuntimeError(f"UGC Real cannot assemble: missing video for cell(s): {missing}")
        music_prompt = f"UGC ad background music. Tone: {creative_strategy.get('creative_angle', 'dynamic')}."
        music_url = processor.suno_service.generate_pure_music(music_prompt)
        has_lip_sync = bool(lip_sync_videos)
        ds = max(0.0, float(dissolve_seconds or 0.0))
        video_data = []
        for ci_idx, u in enumerate(scene_videos):
            if not u:
                continue
            dur = cell_durations[ci_idx] if ci_idx < len(cell_durations) else 4.0
            ci = ci_idx + 1
            clip_url = u
            if has_lip_sync:
                lip_u = (lip_sync_videos or {}).get(str(ci)) if lip_sync_videos else None
                cell_has_lip_clip = bool(lip_u and str(lip_u).strip())
                if not cell_has_lip_clip and processor.rendi_service:
                    ensured = processor.rendi_service.ensure_stereo_audio_track(clip_url)
                    if ensured:
                        clip_url = ensured
            video_data.append({"video_url": clip_url, "duration": dur})
        planned_total = round(sum(float(v.get("duration") or 0.0) for v in video_data), 2)
        required_total = round(max(float(tgt_duration or 0), float(vo_duration or 0.0) + 1.2), 2)
        if video_data and planned_total + 0.2 < required_total:
            deficit = round(required_total - planned_total, 2)
            # Extend coverage to VO length by appending a hold clip from the final visual.
            last_idx = max(i for i, vu in enumerate(scene_videos) if _has_value(vu))
            fallback_img = ""
            if 0 <= last_idx < len(grid_cells):
                fallback_img = str((grid_cells[last_idx] or {}).get("image_url") or "").strip()
            if not fallback_img:
                fallback_img = str(grid_image_url or "").strip()
            if fallback_img:
                hold_clip = processor.rendi_service.create_video_from_image(fallback_img, duration=deficit)
                if hold_clip:
                    video_data.append({"video_url": hold_clip, "duration": deficit})
                    _emit(
                        on_progress,
                        "warning",
                        {
                            "message": (
                                f"UGC Real auto-extended final assembly by {deficit:.1f}s to match VO/target duration."
                            ),
                        },
                    )

        if has_lip_sync and ds > 0:
            logger.warning(
                "UGC Real: dissolve_seconds=%s ignored when concatenating with per-cell audio "
                "(lip-sync + I2V); using straight cuts so speech is preserved.",
                ds,
            )

        # video_only concat drops embedded Kling Avatar audio; add_background_music(..., assume_has_audio=True)
        # then targets [0:a] on a silent file → FFmpeg failure or mute. With lip-sync, mux silence on I2V cells
        # and concat with audio so VO + music mix works.
        if has_lip_sync:
            concat_url = processor.rendi_service.concatenate_videos(
                video_data=video_data,
                video_only=False,
                dissolve_seconds=0.0,
                assume_clips_have_audio=True,
            )
        else:
            concat_url = processor.rendi_service.concatenate_videos(
                video_data=video_data,
                video_only=True,
                dissolve_seconds=ds,
            )
        if not concat_url:
            raise RuntimeError("UGC Real concatenate_videos returned no URL")
        rendi_scene_voice_url = concat_url or ""
        if concat_url and music_url:
            if has_lip_sync:
                rendi_scene_voice_url = (
                    processor.rendi_service.add_background_music_to_video(
                        concat_url,
                        music_url,
                        music_volume=0.35,
                        assume_has_audio=True,
                    )
                    or concat_url
                )
            elif vo_audio_url:
                rendi_scene_voice_url = (
                    processor.rendi_service.add_vo_and_music_to_video(
                        video_url=concat_url,
                        vo_url=vo_audio_url,
                        music_url=music_url,
                    )
                    or concat_url
                )
            else:
                rendi_scene_voice_url = (
                    processor.rendi_service.add_background_music_to_video(concat_url, music_url) or concat_url
                )
        _emit(on_progress, "intermediate", {"key": "music_url", "value": music_url})
        _emit(on_progress, "intermediate", {"key": "concat_url", "value": concat_url})
        _emit(on_progress, "intermediate", {"key": "rendi_scene_voice_url", "value": rendi_scene_voice_url})
        _emit(
            on_progress,
            "intermediate",
            {"key": "video_before_subtitles_url", "value": rendi_scene_voice_url},
        )
        _emit_step(on_progress, "step_9", "Concatenate + Audio Mix", 92, "Assembly complete")

    # Step 10: subtitles
    if _has_value(existing_intermediates.get("final_video_url")) and not _invalidate_downstream_grid:
        subtitled_url = existing_intermediates.get("subtitled_url")
        final_video_url = str(existing_intermediates.get("final_video_url") or "")
        _emit_restored_step(
            on_progress,
            step="step_10",
            label="Add Subtitles",
            progress=98,
            message="Final output restored from checkpoint",
            intermediates={
                "subtitled_url": subtitled_url,
                "final_video_url": final_video_url,
            },
        )
    else:
        _emit_step_start(on_progress, "step_10", "Add Subtitles", "Applying final subtitles and publishing output...")
        subtitled_url = None
        if add_subtitles and processor.zapcap_service and rendi_scene_voice_url:
            subtitled_url = processor.zapcap_service.add_subtitles(
                rendi_scene_voice_url,
                language=language,
                subtitle_position=subtitle_position or "middle",
            )
        final_video_url = subtitled_url or rendi_scene_voice_url or concat_url
        _emit(on_progress, "intermediate", {"key": "subtitled_url", "value": subtitled_url})
        if subtitled_url:
            _emit(on_progress, "intermediate", {"key": "subtitled_video_url", "value": subtitled_url})
        _emit(on_progress, "intermediate", {"key": "final_video_url", "value": final_video_url})
        _emit_step(on_progress, "step_10", "Add Subtitles", 98, "UGC Real pipeline complete")

    return {
        "video_type": "ugc-real",
        "offer_type": offer_type,
        "offer_category": offer_category,
        "character_urls": character_urls,
        "slogan_text": slogan_text,
        "logo_url": logo_url,
        "offer_profile": offer_profile,
        "creative_strategy": creative_strategy,
        "nine_cell_plan": nine_cell_plan,
        "style_dna": style_dna,
        "master_grid_prompt": master_grid_prompt,
        "grid_image_url": grid_image_url,
        "grid_cells": grid_cells,
        "frame_routing": frame_routing,
        "cell_durations": cell_durations,
        "ad_context": ad_context,
        "vo_script": vo_script,
        "vo_audio_url": vo_audio_url,
        "vo_duration": vo_duration,
        "cell_vo_audio": cell_vo_audio,
        "vo_word_segments": vo_word_segments,
        "lip_sync_videos": lip_sync_videos,
        "scene_videos": scene_videos,
        "scene_video_plan": existing_intermediates.get("scene_video_plan") if _has_value(existing_intermediates.get("scene_video_plan")) else (routing_trace if 'routing_trace' in locals() else []),
        "music_url": music_url,
        "concat_url": concat_url,
        "rendi_scene_voice_url": rendi_scene_voice_url,
        "subtitled_url": subtitled_url,
        "final_video_url": final_video_url,
        "video_before_subtitles_url": rendi_scene_voice_url if subtitled_url else None,
        "subtitled_video_url": subtitled_url,
        "debug_nine_cell_plan_json": _safe_json(nine_cell_plan),
    }

