"""TVD Pipeline - modular package extracted from video_scene_processor.py."""

# Lazy imports to avoid circular dependency with the monolith.
# The monolith imports from tvd_pipeline.config/utils at module level,
# so we cannot eagerly import VideoSceneProcessor (which comes from the monolith).


def __getattr__(name):
    if name == "VideoSceneProcessor":
        from tvd_pipeline.processor import VideoSceneProcessor
        return VideoSceneProcessor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["VideoSceneProcessor"]
