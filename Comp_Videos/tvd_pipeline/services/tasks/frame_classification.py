"""Frame classification and routing for UGC Real."""

from __future__ import annotations

from typing import Any, Dict, List


def _score_face_size(cell: Dict[str, Any], shot_type: str) -> float:
    if not cell.get("face_visible"):
        return 0.15
    if "close" in shot_type:
        return 0.95
    if "medium" in shot_type or "selfie" in shot_type or "podcast" in shot_type:
        return 0.82
    return 0.58


def _score_mouth_visibility(face_size_score: float, shot_type: str, speaking_required: bool) -> float:
    score = 0.25 + (face_size_score * 0.6)
    if speaking_required and ("talking" in shot_type or "selfie" in shot_type or "podcast" in shot_type):
        score += 0.15
    return round(min(1.0, score), 2)


def _score_direct_address(shot_type: str, speaking_required: bool) -> float:
    if "selfie" in shot_type or "podcast" in shot_type or "talking" in shot_type:
        return 0.9
    if speaking_required:
        return 0.62
    return 0.28


def classify_cells_for_scene(scene: Dict[str, Any], cells: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classify each cell into lip-sync, animation, or static."""
    out: List[Dict[str, Any]] = []
    speaking_required = bool(scene.get("speaking_required"))
    for cell in cells:
        face_visible = bool(cell.get("face_visible"))
        ugc_present = bool(cell.get("ugc_present", True))
        shot_type = (cell.get("shot_type") or "").lower()
        cell_index = int(cell.get("cell_index") or 0)
        face_size_score = _score_face_size(cell, shot_type)
        mouth_visibility_score = _score_mouth_visibility(face_size_score, shot_type, speaking_required)
        direct_address_score = _score_direct_address(shot_type, speaking_required)
        product_present = bool(cell.get("product_present", False))
        service_present = bool(cell.get("service_present", False))
        digital_ui_present = bool(cell.get("digital_ui_present", False))

        lip_sync_candidate = (
            speaking_required
            and ugc_present
            and face_visible
            and face_size_score >= 0.6
            and mouth_visibility_score >= 0.55
            and direct_address_score >= 0.55
            and ("talking" in shot_type or "selfie" in shot_type or "podcast" in shot_type or cell_index == 1)
        )
        animation_candidate = (
            not lip_sync_candidate
            and (cell_index <= 5 or product_present or service_present or digital_ui_present)
        )
        static_candidate = not lip_sync_candidate and not animation_candidate

        if lip_sync_candidate:
            primary_use = "lip_sync"
            asset_role = "speaking_frame"
            rationale = "Visible face with strong direct-address framing and speaking scene requirements."
        elif animation_candidate:
            if digital_ui_present:
                primary_use = "animation"
                asset_role = "ui_insert"
                rationale = "UI/service insert works better as motion/B-roll than lip-sync."
            elif product_present:
                primary_use = "animation"
                asset_role = "product_insert"
                rationale = "Product-focused frame selected for motion/B-roll instead of speech."
            elif service_present:
                primary_use = "animation"
                asset_role = "service_insert"
                rationale = "Service-context insert selected for motion/B-roll."
            else:
                primary_use = "animation"
                asset_role = "b_roll"
                rationale = "Good non-speaking B-roll candidate for animation."
        else:
            primary_use = "static"
            asset_role = "static_hold"
            rationale = "Fallback hold frame: weak lip-sync suitability and lower motion value."

        out.append(
            {
                "scene_id": scene.get("scene_id"),
                "cell_index": cell_index,
                "ugc_present": ugc_present,
                "face_visible": face_visible,
                "product_present": product_present,
                "service_present": service_present,
                "digital_ui_present": digital_ui_present,
                "face_size_score": face_size_score,
                "mouth_visibility_score": mouth_visibility_score,
                "direct_address_score": direct_address_score,
                "shot_type": shot_type or "unknown",
                "speaking_required": speaking_required,
                "primary_use": primary_use,
                "asset_role": asset_role,
                "lip_sync_candidate": lip_sync_candidate,
                "animation_candidate": animation_candidate,
                "static_candidate": static_candidate,
                "routing_rationale": rationale,
                "continuity_anchor": cell.get("continuity_anchor") or scene.get("continuity_anchor"),
            }
        )
    return out


def classify_scene_plan(scene_plan: Dict[str, Any], cell_sets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classify all cells for all scenes."""
    scenes = (scene_plan or {}).get("scenes") or []
    cell_map = {c.get("scene_id"): (c.get("cells") or []) for c in (cell_sets or [])}
    all_rows: List[Dict[str, Any]] = []
    for scene in scenes:
        scene_cells = cell_map.get(scene.get("scene_id"), [])
        all_rows.extend(classify_cells_for_scene(scene, scene_cells))
    return all_rows

