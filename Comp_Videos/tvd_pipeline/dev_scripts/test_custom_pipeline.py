"""Direct e2e test of the custom storyboard pipeline in simulation mode.

Bypasses the API/Supabase layer and calls process_custom_video() directly
so we can verify the storyboard -> ugc.py adapter end-to-end without needing
a live container or auth.

Run from repo root:
  python Comp_Videos/tvd_pipeline/dev_scripts/test_custom_pipeline.py
"""

import json
import os
import sys

# Add Comp_Videos to path so video_scene_processor + tvd_pipeline resolve.
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_comp = os.path.join(_repo_root, "Comp_Videos")
if _comp not in sys.path:
    sys.path.insert(0, _comp)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Suppress noisy library logging
import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> int:
    from video_scene_processor import VideoSceneProcessor

    storyboard = {
        "meta": {
            "title": "Custom pipeline smoke test",
            "video_type": "custom",
            "target_duration_seconds": 12.0,
            "language": "en",
            "country": "usa",
            "style": "Cinematic photography",
            "fidelity_to_assets": 0.5,
            "aspect_ratio": "9:16",
        },
        "voiceover": {
            "script": "Welcome to the test. |||This is scene two. |||Buy now.",
            "language": "en",
        },
        "music": {
            "description": "Upbeat electronic test bed",
        },
        "assets": {
            "reference_image_urls": ["https://storage.googleapis.com/automatiq/simulation/placeholder.jpg"],
            "asset_video_urls": [],
        },
        "scenes": [
            {"scene_number": 1, "narrative_role": "hook", "vo_text": "Welcome to the test.", "duration": 4.0,
             "clips": [{"type": "generate", "duration": 4.0,
                        "first_prompt": "Cinematic sunrise over city skyline",
                        "motion_prompt": "Slow zoom in"}]},
            {"scene_number": 2, "narrative_role": "solution", "vo_text": "This is scene two.", "duration": 4.0,
             "clips": [{"type": "asset_image_animate", "duration": 4.0,
                        "source": {"reference_image_index": 0},
                        "motion_prompt": "Gentle pan right"}]},
            {"scene_number": 3, "narrative_role": "cta", "vo_text": "Buy now.", "duration": 4.0,
             "clips": [{"type": "ken_burns", "duration": 4.0,
                        "source": {"reference_image_index": 0},
                        "motion_prompt": "Soft Ken Burns push"}]},
        ],
    }

    events = []
    def on_progress(event_type, data):
        events.append((event_type, dict(data) if isinstance(data, dict) else data))

    print("=" * 70)
    print("Custom pipeline smoke test (simulation mode)")
    print(f"  Scenes: {len(storyboard['scenes'])}")
    print(f"  Target duration: {storyboard['meta']['target_duration_seconds']}s")
    print("=" * 70)

    p = VideoSceneProcessor()
    try:
        result = p.process_custom_video(
            storyboard,
            simulation=True,
            on_progress=on_progress,
            target_duration=int(storyboard["meta"]["target_duration_seconds"]),
            language=storyboard["meta"]["language"],
            country=storyboard["meta"]["country"],
        )
    except Exception as e:
        print(f"\nFAIL: pipeline raised: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print("\n" + "=" * 70)
    print("Result summary")
    print("=" * 70)
    print(f"Keys: {sorted(result.keys()) if isinstance(result, dict) else type(result).__name__}")
    if isinstance(result, dict):
        for k in ("final_video_url", "subtitled_video_url", "concat_url", "scene_images", "scene_videos", "vo_audio_url", "music_url"):
            v = result.get(k)
            if isinstance(v, list):
                print(f"  {k}: list[{len(v)}]")
            elif v:
                s = str(v)
                print(f"  {k}: {s[:80]}{'...' if len(s) > 80 else ''}")

    print()
    print(f"Total events emitted: {len(events)}")
    by_type = {}
    for et, _ in events:
        by_type[et] = by_type.get(et, 0) + 1
    for et, n in sorted(by_type.items()):
        print(f"  {et}: {n}")

    # Show the first few step_complete events
    step_events = [e for e in events if e[0] == "step_complete"]
    if step_events:
        print("\nSteps executed:")
        for _, data in step_events[:20]:
            print(f"  - {data.get('step', '?')}: {data.get('label', '?')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
