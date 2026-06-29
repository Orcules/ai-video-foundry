"""TVD Pipeline processor - thin re-export from the monolith.

The VideoSceneProcessor class lives in the monolith (video_scene_processor.py)
and is imported here so that `from tvd_pipeline.processor import VideoSceneProcessor`
works for the API wrapper.
"""

import sys
import os

# Add parent dir so we can import the monolith.
# In Docker, tvd_pipeline is at /app/tvd_pipeline and the monolith at /app/Comp_Videos/.
# Locally, tvd_pipeline is at Comp_Videos/tvd_pipeline and the monolith at Comp_Videos/.
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _parent)

# Also add Comp_Videos subdir (for Docker where parent=/app, monolith=/app/Comp_Videos/)
_comp = os.path.join(_parent, "Comp_Videos")
if os.path.isdir(_comp) and _comp not in sys.path:
    sys.path.insert(0, _comp)

from video_scene_processor import VideoSceneProcessor  # noqa: E402
