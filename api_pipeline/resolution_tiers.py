import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_TIERS = json.loads((Path(__file__).parent / "config" / "resolution_tiers.json").read_text())

def get_tier(name: str, pipeline: str = None) -> dict:
    """Get resolution tier config.

    Args:
        name: Tier name like "720p_low", "1080p_high"
        pipeline: Pipeline name ("product", "influencer", "personal_brand"). If None, tries flat lookup then "product".
    """
    if pipeline and pipeline in _TIERS:
        section = _TIERS[pipeline]
        tier = section.get(name)
        if tier is None:
            tier = section.get("720p_low", {})
            logger.warning(f"FALLBACK: tier '{name}' not found in pipeline '{pipeline}' — using 720p_low")
        return tier
    # Backward compat: if flat structure or pipeline not found, try flat lookup
    if name in _TIERS and isinstance(_TIERS[name], dict) and ("nb_res" in _TIERS.get(name, {}) or "video_model" in _TIERS.get(name, {})):
        return _TIERS[name]
    # Try "product" as default
    product = _TIERS.get("product", {})
    if isinstance(product, dict) and "720p_low" in product:
        tier = product.get(name)
        if tier is None:
            tier = product.get("720p_low", {})
            logger.warning(f"FALLBACK: tier '{name}' not found, pipeline not specified — using product/720p_low")
        return tier
    logger.warning(f"FALLBACK: tier '{name}' completely unresolved — returning empty dict")
    return _TIERS.get(name, {})
