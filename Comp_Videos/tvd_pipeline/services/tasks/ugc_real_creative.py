"""Creative planning tasks for the UGC Real pipeline."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from tvd_pipeline.prompt_loader import get_prompt_loader
from tvd_pipeline.data_loader import get_speech_rate
from tvd_pipeline.services.tasks._helpers import extract_json_from_response
from tvd_pipeline.services.tasks.grid_cutter import describe_master_grid_split_for_prompt

# Nominal master-grid canvas (both dimensions divisible by 3) for prompt alignment with cut_grid_image_*.
_MASTER_GRID_TARGET_PX = {
    "1K": (1026, 1728),
    "2K": (1536, 2592),
    "4K": (2052, 3456),
}


def _llm_response_text(result: Any) -> str:
    """Normalize _call_llm return value to plain text (JSON or prose)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        t = result.get("text")
        if t is not None and str(t).strip():
            return str(t)
        for key in ("content", "output_text", "message"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _load_json(text: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Parse LLM JSON; handles ```json fences, leading prose, and partial extracts."""
    if not text or not str(text).strip():
        return fallback
    s = str(text).strip()
    parsed = extract_json_from_response(s)
    if isinstance(parsed, dict) and parsed:
        return parsed
    try:
        blob = json.loads(s)
        if isinstance(blob, dict) and blob:
            return blob
    except Exception:
        pass
    return fallback


def analyze_offer(
    call_fn,
    *,
    offer_type: str,
    description: str,
    benefits: str,
    audience: str,
    main_problem: str = "",
    offer_category: str = "",
    cta_text: str = "",
    delivery_format: str = "",
    device_type: str = "",
    ad_format: str = "",
    pace: str = "",
    realism_level: Any = None,
    drama_level: Any = None,
) -> Dict[str, Any]:
    loader = get_prompt_loader()
    system_prompt = loader.get("ugc_real_offer_analysis_system")
    user_prompt = loader.get(
        "ugc_real_offer_analysis_user",
        offer_type=offer_type or "service",
        description=description or "",
        benefits=benefits or "",
        audience=audience or "",
        main_problem=main_problem or "",
        offer_category=offer_category or "",
        cta_text=cta_text or "",
        delivery_format=delivery_format or "",
        device_type=device_type or "",
        ad_format=ad_format or "",
        pace=pace or "",
        realism_level=str(realism_level or ""),
        drama_level=str(drama_level or ""),
    )
    result = call_fn(
        "ugc_real_offer_analysis",
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    fallback = {
        "offer_type": offer_type or "service",
        "offer_category": offer_category or "",
        "offer_profile": "generic_offer",
        "visual_requirements": ["ugc_creator_presence"],
        "recommended_ad_patterns": ["hook_first", "problem_solution"],
        "cta_text": cta_text or "",
        "explainability": {
            "why": "Fallback offer analysis used because the model result was empty or invalid JSON.",
            "critical_inputs": [x for x in [offer_type, offer_category, audience, main_problem] if x],
        },
    }
    return _load_json(_llm_response_text(result), fallback)


def generate_creative_strategy(
    call_fn,
    *,
    offer_profile: Dict[str, Any],
    audience: str,
    duration: int,
    ad_format: str,
    pace: str = "",
    realism_level: Any = None,
    drama_level: Any = None,
    cta_text: str = "",
) -> Dict[str, Any]:
    loader = get_prompt_loader()
    system_prompt = loader.get("ugc_real_creative_strategy_system")
    user_prompt = loader.get(
        "ugc_real_creative_strategy_user",
        offer_profile_json=json.dumps(offer_profile or {}, ensure_ascii=True),
        audience=audience or "",
        duration_sec=str(duration or 30),
        ad_format=ad_format or "talking_head",
        pace=pace or "",
        realism_level=str(realism_level or ""),
        drama_level=str(drama_level or ""),
        cta_text=cta_text or "",
    )
    result = call_fn(
        "ugc_real_creative_strategy",
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    fallback = {
        "creative_angle": "pain_point",
        "hook_style": "confession_hook",
        "narrative_mode": "ugc_story",
        "proof_strategy": "quick_demo",
        "cta_strategy": "direct",
        "explainability": {
            "why": "Fallback strategy used because the model result was empty or invalid JSON.",
            "ad_format": ad_format or "talking_head",
        },
    }
    return _load_json(_llm_response_text(result), fallback)


def plan_narrative(
    call_fn,
    *,
    strategy: Dict[str, Any],
    duration: int,
    offer_type: str,
    variation_count: int = 1,
    offer_profile: Optional[Dict[str, Any]] = None,
    cta_text: str = "",
) -> Dict[str, Any]:
    loader = get_prompt_loader()
    system_prompt = loader.get("ugc_real_narrative_plan_system")
    user_prompt = loader.get(
        "ugc_real_narrative_plan_user",
        strategy_json=json.dumps(strategy or {}, ensure_ascii=True),
        offer_profile_json=json.dumps(offer_profile or {}, ensure_ascii=True),
        duration_sec=str(duration or 30),
        offer_type=offer_type or "service",
        variation_count=str(variation_count or 1),
        cta_text=cta_text or "",
    )
    result = call_fn(
        "ugc_real_narrative_plan",
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    default_scene_count = max(3, min(12, int(round((duration or 30) / 5))))
    fallback = {
        "hook_line": "I did not expect this to work.",
        "story_arc": "hook -> pain -> solution -> proof -> cta",
        "scene_count": default_scene_count,
        "scene_purposes": ["hook", "pain_point", "benefit_explanation", "proof", "cta"],
        "explainability": {
            "why": "Fallback narrative plan used because the model result was empty or invalid JSON.",
        },
    }
    return _load_json(_llm_response_text(result), fallback)


def generate_scene_plan(
    call_fn,
    *,
    narrative_plan: Dict[str, Any],
    offer_type: str,
    language: str,
    offer_profile: Optional[Dict[str, Any]] = None,
    creative_strategy: Optional[Dict[str, Any]] = None,
    cta_text: str = "",
) -> Dict[str, Any]:
    loader = get_prompt_loader()
    scene_count = int((narrative_plan or {}).get("scene_count") or 5)
    system_prompt = loader.get("ugc_real_scene_plan_system")
    user_prompt = loader.get(
        "ugc_real_scene_plan_user",
        narrative_plan_json=json.dumps(narrative_plan or {}, ensure_ascii=True),
        offer_profile_json=json.dumps(offer_profile or {}, ensure_ascii=True),
        creative_strategy_json=json.dumps(creative_strategy or {}, ensure_ascii=True),
        offer_type=offer_type or "service",
        language=language or "en",
        scene_count=str(scene_count),
        cta_text=cta_text or "",
    )
    result = call_fn(
        "ugc_real_scene_plan",
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    fallback_scenes: List[Dict[str, Any]] = []
    for i in range(scene_count):
        fallback_scenes.append(
            {
                "scene_id": f"scene_{i+1:03d}",
                "scene_index": i + 1,
                "purpose": "hook" if i == 0 else ("cta" if i == scene_count - 1 else "benefit_explanation"),
                "location": "creator_home",
                "ugc_present": True,
                "product_present": offer_type == "physical_product",
                "service_present": offer_type == "service",
                "digital_ui_present": offer_type == "digital_product",
                "speaking_required": i % 2 == 0,
                "voice_line": f"Scene {i+1} line",
                "primary_message": f"Scene {i+1} message",
                "continuity_anchor": "same_creator_world",
                "transition_reason": "narrative_progression" if i > 0 else "hook_start",
                "emotional_beat": "curiosity" if i == 0 else ("conversion" if i == scene_count - 1 else "belief"),
                "shot_family": "talking_head" if i % 2 == 0 else "cutaway",
                "explainability": {
                    "why": "Fallback scene used because the model result was empty or invalid JSON.",
                    "lip_sync_expected": bool(i % 2 == 0),
                },
            }
        )
    fallback = {"scenes": fallback_scenes}
    parsed = _load_json(_llm_response_text(result), fallback)
    scenes_out = (parsed or {}).get("scenes")
    if not isinstance(scenes_out, list) or len(scenes_out) == 0:
        return fallback
    return parsed


def build_grid_manifest(
    scene_plan: Dict[str, Any],
    *,
    layout: str = "3x3",
) -> List[Dict[str, Any]]:
    scenes = (scene_plan or {}).get("scenes") or []
    manifests: List[Dict[str, Any]] = []
    for scene in scenes:
        cells = []
        for idx in range(9):
            speaking_required = bool(scene.get("speaking_required")) and idx == 0
            cells.append(
                {
                    "cell_index": idx + 1,
                    "shot_type": "talking_head_medium" if speaking_required else "ugc_insert",
                    "description": scene.get("primary_message") or scene.get("voice_line") or "UGC scene frame",
                    "location": scene.get("location") or "creator_home",
                    "ugc_present": bool(scene.get("ugc_present", True)),
                    "product_present": bool(scene.get("product_present", False)),
                    "service_present": bool(scene.get("service_present", False)),
                    "digital_ui_present": bool(scene.get("digital_ui_present", False)),
                    "face_visible": speaking_required or idx < 4,
                    "speaking_required": speaking_required,
                    "lip_sync_candidate": speaking_required,
                    "animation_candidate": not speaking_required and idx < 5,
                    "primary_use": "lip_sync" if speaking_required else ("animation" if idx < 5 else "static"),
                    "framing": "medium" if speaking_required else ("closeup" if idx < 3 else "wide"),
                    "camera_angle": "eye_level",
                    "emotion": "engaged" if speaking_required else "natural",
                    "continuity_anchor": scene.get("continuity_anchor") or scene.get("location") or "ugc_world",
                }
            )
        manifests.append(
            {
                "grid_id": f"grid_{scene.get('scene_id')}",
                "scene_id": scene.get("scene_id"),
                "layout": layout,
                "global_prompt_context": scene.get("primary_message") or "",
                "locked_elements": ["character_identity", "location_family", "visual_style"],
                "variable_elements": ["expression", "framing", "camera_angle", "emotion"],
                "explainability": scene.get("explainability") or {},
                "cells": cells,
            }
        )
    return manifests


# ---------------------------------------------------------------------------
# Nine-cell plan (fixed 9 segments, single 3x3 grid)
# ---------------------------------------------------------------------------

_NINE_CELL_PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cell_index": {"type": "integer"},
                    "visual_prompt": {"type": "string"},
                    "voice_line": {"type": "string"},
                    "lipsync": {"type": "boolean"},
                    "shot_role": {"type": "string"},
                    "duration_seconds": {"type": "number"},
                },
                "required": ["cell_index", "visual_prompt", "voice_line", "lipsync", "shot_role", "duration_seconds"],
            },
        }
    },
    "required": ["cells"],
}


def generate_nine_cell_plan(
    call_fn,
    *,
    offer_profile: Dict[str, Any],
    creative_strategy: Dict[str, Any],
    duration: int,
    language: str,
    offer_type: str,
    cta_text: str = "",
    ad_format: str = "talking_head",
    target_audience: str = "",
    main_problem: str = "",
    key_benefits: str = "",
    original_prompt: str = "",
    character_description: str = "",
    gender: str = "",
) -> Dict[str, Any]:
    """Generate a fixed 9-cell storyboard plan."""
    loader = get_prompt_loader()
    brief = (original_prompt or "").strip()
    if len(brief) > 6000:
        brief = brief[:5997] + "..."
    system_prompt = loader.get("ugc_real_nine_cell_plan_system")
    user_prompt = loader.get(
        "ugc_real_nine_cell_plan_user",
        target_audience=(target_audience or "").strip() or "(not provided)",
        main_problem=(main_problem or "").strip() or "(not provided)",
        key_benefits=(key_benefits or "").strip() or "(not provided)",
        original_prompt=brief or "(not provided)",
        offer_profile_json=json.dumps(offer_profile or {}, ensure_ascii=True),
        creative_strategy_json=json.dumps(creative_strategy or {}, ensure_ascii=True),
        duration_sec=str(duration or 30),
        language=language or "en",
        offer_type=offer_type or "service",
        cta_text=cta_text or "",
        ad_format=ad_format or "talking_head",
        character_description=(character_description or "").strip() or "(not provided)",
        gender=(gender or "").strip() or "(not provided)",
        lipsync_words=str(lipsync_words),
        nonlipsync_words=str(nonlipsync_words),
        total_target_words=str(total_target_words),
        wps=f"{wps:.1f}",
    )
    avg_dur = round(max(2.0, float(duration or 30) / 9.0), 2)
    # Compute per-cell word count target from language speech rate
    wps = get_speech_rate(language or "en")
    total_target_words = int((duration or 30) * wps)
    # Lipsync cells (3 of 9) carry ~60% of VO; non-lipsync cells carry the rest.
    lipsync_words = round(total_target_words * 0.60 / 3)
    nonlipsync_words = round(total_target_words * 0.40 / 6)
    fallback_cells = []
    # Exactly 3 lipsync (cells 1, 5, 9); 6 product/service / B-roll cells
    fallback_roles = [
        "character_talking",
        "product_only",
        "character_with_product",
        "service_ui",
        "character_talking",
        "product_only",
        "b_roll",
        "character_with_product",
        "cta",
    ]
    for i in range(9):
        lip = i in (0, 4, 8)
        fallback_cells.append({
            "cell_index": i + 1,
            "visual_prompt": f"UGC scene {i + 1}",
            "voice_line": f"Scene {i + 1} line",
            "lipsync": lip,
            "shot_role": fallback_roles[i],
            "duration_seconds": avg_dur,
        })
    fallback = {"cells": fallback_cells}

    _VALID_SHOT_ROLES = {
        "character_talking", "character_with_product", "product_only",
        "service_ui", "b_roll", "cta",
    }

    def _enforce_lipsync_grid(cells_local: List[Dict[str, Any]]) -> None:
        """Exactly 3 talking-head / direct-address cells (indices 0,4,8 = cells 1,5,9)."""
        for i, c in enumerate(cells_local):
            c["lipsync"] = i in (0, 4, 8)

    def _normalize_cell(cell: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize alternate field names the LLM might use."""
        if not cell.get("visual_prompt"):
            cell["visual_prompt"] = (
                cell.pop("image_prompt", None)
                or cell.pop("prompt", None)
                or cell.pop("description", None)
                or cell.pop("scene_description", None)
                or ""
            )
        if not cell.get("voice_line"):
            cell["voice_line"] = (
                cell.pop("vo_line", None)
                or cell.pop("script_line", None)
                or cell.pop("narration", None)
                or ""
            )
        if not cell.get("shot_role") or cell["shot_role"] not in _VALID_SHOT_ROLES:
            cell["shot_role"] = "b_roll"
        return cell

    def _parse_nine_cell(result_obj: Any) -> Optional[Dict[str, Any]]:
        raw = _llm_response_text(result_obj)
        parsed_local = _load_json(raw, {})
        cells_local = (parsed_local or {}).get("cells")
        if not isinstance(cells_local, list) or len(cells_local) != 9:
            return None
        for c in cells_local:
            _normalize_cell(c)
        _enforce_lipsync_grid(cells_local)
        filled = sum(
            1 for c in cells_local
            if str(c.get("visual_prompt") or "").strip()
            and str(c.get("voice_line") or "").strip()
        )
        if filled < 7:
            return None
        return parsed_local

    result = call_fn(
        "ugc_real_nine_cell_plan",
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        responseSchema=_NINE_CELL_PLAN_SCHEMA,
        temperature=0.35,
        max_tokens=8192,
    )
    parsed = _parse_nine_cell(result)
    if not parsed:
        retry_suffix = (
            "\n\nReturn ONLY valid JSON (no markdown). "
            "Shape: {\"cells\":[<exactly 9 objects>]}. "
            "Each object must include: cell_index (1-9), visual_prompt (string), "
            "voice_line (string), lipsync (boolean), shot_role (string), duration_seconds (number). "
            "Exactly 3 cells must have lipsync true (cells 1, 5, and 9); the other 6 must have lipsync false."
        )
        result2 = call_fn(
            "ugc_real_nine_cell_plan",
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt + retry_suffix},
            ],
            temperature=0.45,
            max_tokens=8192,
        )
        parsed = _parse_nine_cell(result2)
    if not parsed:
        return fallback

    cells = parsed.get("cells")
    if not isinstance(cells, list) or len(cells) != 9:
        return fallback
    for i, cell in enumerate(cells):
        cell["cell_index"] = i + 1
        _normalize_cell(cell)
        if "lipsync" not in cell:
            cell["lipsync"] = cell.get("shot_role") in ("character_talking", "character_with_product")
        if "duration_seconds" not in cell:
            cell["duration_seconds"] = avg_dur
    _enforce_lipsync_grid(cells)
    return parsed


def extract_style_dna(
    call_fn,
    *,
    character_description: str = "",
    gender: str = "",
    visual_style: str = "Auto",
    offer_type: str = "service",
    ad_format: str = "talking_head",
) -> Dict[str, Any]:
    """Extract visual DNA JSON for consistent grid generation."""
    loader = get_prompt_loader()
    system_prompt = loader.get("ugc_real_style_dna_system")
    user_prompt = loader.get(
        "ugc_real_style_dna_user",
        character_description=character_description or "Natural, approachable UGC creator",
        gender=(gender or "").strip() or "(not provided)",
        visual_style=visual_style or "Auto",
        offer_type=offer_type or "service",
        ad_format=ad_format or "talking_head",
    )
    result = call_fn(
        "ugc_real_style_dna",
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    fallback = {
        "color_palette": "warm natural tones",
        "lighting": "soft studio high-key lighting",
        "composition": "centered portrait",
        "camera_lens": "85mm f/1.8",
        "character_details": character_description or "natural UGC creator",
        "stylistic_effects": "ultra-realistic, photorealistic",
        "background": "clean solid light grey background",
        "overall_mood": "authentic, approachable",
    }
    return _load_json(_llm_response_text(result), fallback)


def build_master_grid_prompt(
    call_fn,
    *,
    style_dna: Dict[str, Any],
    nine_cell_plan: Dict[str, Any],
    offer_type: str = "service",
    visual_style: str = "Auto",
    character_description: str = "",
    product_description: str = "",
    gender: str = "",
    image_resolution: str = "1K",
    grid_layout: str = "3x3",
) -> str:
    """Use LLM to compose a single Nano Banana prompt for the 3x3 grid image."""
    loader = get_prompt_loader()
    system_prompt = loader.get("ugc_real_master_grid_system")
    # Prevent style_dna JSON from inventing a different person than API/storyboard anchors.
    dna_for_prompt = dict(style_dna or {})
    cd_lock = (character_description or "").strip()
    g_lock = (gender or "").strip()
    if cd_lock and cd_lock != "(not provided)":
        if g_lock and g_lock not in ("(not provided)", "unspecified"):
            dna_for_prompt["character_details"] = f"Gender: {g_lock}. {cd_lock}"
        else:
            dna_for_prompt["character_details"] = cd_lock
    elif g_lock and g_lock not in ("(not provided)", "unspecified"):
        prev_d = str(dna_for_prompt.get("character_details") or "").strip()
        dna_for_prompt["character_details"] = f"Gender: {g_lock}. {prev_d}".strip(" .")
    user_prompt = loader.get(
        "ugc_real_master_grid_user",
        style_dna_json=json.dumps(dna_for_prompt, ensure_ascii=True),
        nine_cell_plan_json=json.dumps(nine_cell_plan or {}, ensure_ascii=True),
        offer_type=offer_type or "service",
        visual_style=visual_style or "Auto",
        character_description=(character_description or "").strip() or "(not provided)",
        product_description=(product_description or "").strip() or "(not provided)",
        gender=g_lock or "(not provided)",
    )
    result = call_fn(
        "ugc_real_master_grid",
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    text = _llm_response_text(result).strip()
    res_key = (image_resolution or "1K").strip().upper()
    tw, th = _MASTER_GRID_TARGET_PX.get(res_key, _MASTER_GRID_TARGET_PX["1K"])
    split_block = describe_master_grid_split_for_prompt(grid_layout or "3x3", tw, th)
    anchor_lines = [
        "GRID ANCHOR (mandatory — overrides any conflicting wording in the scene list):",
        "- The first attached reference image is a blank 9:16 template with a perfect 3×3 grid (nine equal cells, straight lines). The final image MUST copy this layout exactly: same proportions, equal tiles, no merged cells — only replace cell contents with the scenes below.",
        f"- Creator gender for all face-visible / talking cells: {g_lock or 'as in character description'}.",
        f"- Creator appearance: {cd_lock if cd_lock and cd_lock != '(not provided)' else 'match attached character reference images exactly'}.",
        "Do not change gender, face, hair, or ethnicity versus this anchor or the reference images.",
    ]
    pd_lock = (product_description or "").strip()
    if pd_lock and pd_lock != "(not provided)":
        anchor_lines.append(f"- Product / props / UI (match reference images): {pd_lock[:4000]}")
    anchor_prefix = "\n".join(anchor_lines) + "\n\n"
    if text:
        text = anchor_prefix + text + "\n\n" + split_block
    if not text:
        cells = (nine_cell_plan or {}).get("cells") or []
        char_desc = (character_description or "").strip() or (style_dna or {}).get(
            "character_details", "natural UGC creator"
        )
        prod_note = (product_description or "").strip()
        bg = (style_dna or {}).get("background", "clean solid light grey background")
        lighting = (style_dna or {}).get("lighting", "soft studio high-key lighting")
        lens = (style_dna or {}).get("camera_lens", "85mm f/1.8")
        rows = ["Top Row", "Middle Row", "Bottom Row"]
        lines = [
            anchor_prefix.strip(),
            f"A professional 3x3 grid layout of 9 high-quality images featuring {char_desc}.",
        ]
        if prod_note and prod_note != "(not provided)":
            lines.append(f"Product and offer context: {prod_note}")
        for row_idx in range(3):
            descs = []
            for col in range(3):
                ci = row_idx * 3 + col
                cell = cells[ci] if ci < len(cells) else {}
                descs.append(f"[{cell.get('visual_prompt', f'Scene {ci+1}')}]")
            lines.append(f"{rows[row_idx]}: {' | '.join(descs)}.")
        lines.append(f"Technical Settings: {bg}, {lighting}, 8k resolution, cinematic photography, shot on {lens}, hyper-detailed, photorealistic.")
        text = "\n\n".join(lines) + "\n\n" + split_block
    return text

