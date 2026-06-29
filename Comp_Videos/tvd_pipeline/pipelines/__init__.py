"""Pipeline modules extracted from VideoSceneProcessor.

Each pipeline is a standalone function that accepts a processor instance
as its first argument.  The monolith delegates to these functions via
thin wrapper methods on VideoSceneProcessor.

- ``product.process_product_video(processor, ...)`` -- Product video pipeline
- ``ugc.process_ugc_video(processor, ...)`` -- UGC (influencer / personal-brand) pipeline
- ``_helpers`` -- Shared helper functions used by both pipelines
- ``legacy.process_single_video(processor, ...)`` -- Legacy scene-detection pipeline
"""

from tvd_pipeline.pipelines.product import process_product_video
from tvd_pipeline.pipelines.ugc import process_ugc_video
from tvd_pipeline.pipelines.legacy import (
    process_single_video,
    _process_single_scene,
    _process_cta_button,
)
from tvd_pipeline.pipelines._sheet_orchestrator import process_all_videos

__all__ = [
    "process_product_video",
    "process_ugc_video",
    "process_single_video",
    "_process_single_scene",
    "_process_cta_button",
    "process_all_videos",
]
