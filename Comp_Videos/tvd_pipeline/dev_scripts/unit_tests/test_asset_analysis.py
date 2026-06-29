"""Quick proof-of-concept: analyze a video asset with Gemini 2.5 Flash.

Follows the exact same pattern as analyze_reference_video_structure() in
video_analysis.py — upload to GCS, send via fileData, get structured JSON back.

Uses responseSchema (planned for smart asset mode) so Vertex returns clean JSON
without markdown code blocks.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.unit_tests.test_asset_analysis
"""

import json
import os
import sys
import time

# Ensure Comp_Videos is on the path
script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.providers.vertex import VertexAIProvider


# --- Schema matching production (video_analysis.py ASSET_ANALYSIS_SCHEMA) ---
ASSET_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_index": {"type": "integer"},
        "duration_seconds": {"type": "number"},
        "content_summary": {"type": "string"},
        "key_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "description": {"type": "string"},
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "uniqueness": {"type": "string", "enum": ["high", "medium", "low"]},
                    "uniqueness_reason": {"type": "string"},
                },
                "required": ["index", "description", "start_seconds", "end_seconds", "uniqueness", "uniqueness_reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["asset_index", "duration_seconds", "content_summary", "key_moments"],
    "additionalProperties": False,
}


# --- Prompt matching production (shared_asset_analysis_system.md) ---
ANALYSIS_PROMPT = """You are a video content analyst. Analyze this video clip in detail.

Return a JSON object with:
- asset_index: The index of this video in the input sequence (will be set by the caller).
- duration_seconds: The approximate total duration of the video in seconds.
- content_summary: A detailed 4-8 sentence description of the video. Include: what is shown, who appears and what they DO (actions, gestures, expressions, interactions with objects or people), the setting/environment, lighting conditions, camera movement, colors, mood/atmosphere, any text or signage visible, and notable objects.
- key_moments: An array of the most notable moments in the video. Each moment is a self-contained segment that could be extracted as a standalone clip. For each moment:
  - index: Sequential integer starting from 0.
  - description: 2-3 sentences describing what HAPPENS during this moment — focus on ACTIONS, MOVEMENT, and INTERACTIONS, not just static descriptions. What are people doing? How are they moving? What changes? What emotions are visible?
  - start_seconds: When this moment begins (seconds from video start).
  - end_seconds: When this moment ends (seconds from video start).
  - uniqueness: Rate as "high", "medium", or "low":
    - **high**: Distinctive, memorable moments — unique interactions, unusual objects, expressive emotions, signature elements. These are "gold" for video editing.
    - **medium**: Interesting but not unique — good establishing shots, relevant activity, environment details.
    - **low**: Generic/common footage — standard architecture, empty spaces, common transitions.
  - uniqueness_reason: One sentence explaining why this moment received its uniqueness rating.

Guidelines for key_moments:
- Identify 3-8 key moments that represent distinct visual events, transitions, or notable content changes.
- Moments should NOT overlap. Together they should cover the most interesting parts of the video.
- MINIMUM DURATION: Every moment MUST be at least 1.0 second long. If a visual event is shorter than 1 second, merge it with the adjacent moment. Never output sub-second moments.
- Use whole seconds or half-seconds for timestamps (e.g. 0.0, 1.5, 3.0) — do NOT use sub-frame precision like 0.02 or 0.07.
- The first moment should start at 0.0 seconds.
- The last moment should end at or near the video duration.
- Describe what people DO, not just what they look like. "She playfully squeezes the plushie and laughs" is better than "A woman holds a plushie".

Uniqueness example (cat plushie restaurant video):
- Moment 0: uniqueness=high — "Person playfully interacting with oversized cat plushie, distinctive restaurant signature element"
- Moment 1: uniqueness=medium — "Restaurant interior pan with colorful chairs and Japanese murals"
- Moment 2: uniqueness=low — "Standard seating area and generic decor\""""


def main():
    video_path = os.path.join(comp_videos_dir, "temp", "WhatsApp Video 2026-03-04 at 01.12.52.mp4")
    if not os.path.exists(video_path):
        print(f"ERROR: Video not found: {video_path}")
        sys.exit(1)

    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    print(f"Video: {video_path}")
    print(f"Size: {file_size_mb:.1f} MB")

    # --- Init services ---
    config = Config()
    print(f"\nInitializing GCS storage...")
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name="automatiq",
        folder_path="temp_analysis",
    )

    print(f"Initializing Vertex AI provider...")
    vertex = VertexAIProvider(gcs_storage_service=gcs)
    if not vertex.initialized:
        print("ERROR: Vertex AI provider failed to initialize. Check VERTEX_AI_API_KEY or ADC.")
        sys.exit(1)
    print(f"Vertex AI ready (model: {vertex.model})")

    # --- Upload to GCS ---
    print(f"\nUploading video to GCS...")
    t0 = time.time()
    video_url = vertex._upload_video_to_gcs(video_path)
    if not video_url:
        print("ERROR: Failed to upload video to GCS")
        sys.exit(1)
    print(f"Uploaded in {time.time() - t0:.1f}s -> {video_url[:80]}...")

    # --- Build gs:// URI ---
    if "storage.googleapis.com/" in video_url:
        gs_uri = "gs://" + video_url.split("storage.googleapis.com/", 1)[1]
    else:
        gs_uri = video_url
    print(f"GCS URI: {gs_uri}")

    # --- Send to Gemini with responseSchema ---
    print(f"\nSending to Gemini 2.5 Flash with responseSchema...")
    t1 = time.time()

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"fileData": {"mimeType": "video/mp4", "fileUri": gs_uri}},
                    {"text": ANALYSIS_PROMPT},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
            "responseSchema": ASSET_ANALYSIS_SCHEMA,
        },
    }

    result = vertex.raw_generate_content(payload, model="gemini-2.5-flash")
    elapsed = time.time() - t1

    print(f"Response in {elapsed:.1f}s")
    print(f"Tokens: {result['input_tokens']} input, {result['output_tokens']} output")

    # --- Parse and display ---
    raw_text = result.get("text", "")
    print(f"\n{'='*60}")
    print("RAW LLM OUTPUT:")
    print(f"{'='*60}")
    print(raw_text)

    try:
        parsed = json.loads(raw_text)
        print(f"\n{'='*60}")
        print("PARSED (pretty):")
        print(f"{'='*60}")
        print(json.dumps(parsed, indent=2, ensure_ascii=False))

        print(f"\n{'='*60}")
        print("SUMMARY:")
        print(f"{'='*60}")
        print(f"Duration: {parsed.get('duration_seconds', '?')}s")
        print(f"Summary: {parsed.get('content_summary', '?')[:200]}")
        print(f"Key moments ({len(parsed.get('key_moments', []))}):")
        for km in parsed.get("key_moments", []):
            dur = km['end_seconds'] - km['start_seconds']
            print(f"  [{km['index']}] {km['start_seconds']:.1f}s - {km['end_seconds']:.1f}s ({dur:.1f}s) -- {km['description']}")
    except json.JSONDecodeError as e:
        print(f"\nERROR: Failed to parse JSON: {e}")

    # --- Cleanup GCS ---
    print(f"\nCleaning up GCS...")
    vertex._cleanup_gcs_video(video_url)
    print("Done.")


if __name__ == "__main__":
    main()
