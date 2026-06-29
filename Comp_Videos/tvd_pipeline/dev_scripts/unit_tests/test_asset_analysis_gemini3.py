"""Compare asset video analysis: Gemini 2.5 Flash vs Gemini 3 Flash.

Runs both models on the same video with an enhanced prompt that asks for
richer moment descriptions (actions, interactions, emotions — not just static
descriptions of what is visible).

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.unit_tests.test_asset_analysis_gemini3
"""

import json
import os
import sys
import time

script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.services.providers.vertex import VertexAIProvider

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
                },
                "required": ["index", "description", "start_seconds", "end_seconds"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["asset_index", "duration_seconds", "content_summary", "key_moments"],
    "additionalProperties": False,
}

ENHANCED_PROMPT = """You are a video content analyst. Analyze this video clip in detail.

Return a JSON object with:
- asset_index: always 0 (single video test)
- duration_seconds: the approximate total duration of the video in seconds
- content_summary: A detailed 4-8 sentence description. Include: what is shown, who appears and what they DO (actions, gestures, expressions, interactions with objects or people), the setting/environment, lighting, camera movement, colors, mood/atmosphere, any text or signage, and notable objects.
- key_moments: An array of the most notable moments. Each moment is a self-contained segment that could be extracted as a standalone clip. For each moment:
  - index: sequential integer starting from 0
  - description: 2-3 sentences describing what HAPPENS during this moment — focus on ACTIONS, MOVEMENT, and INTERACTIONS, not just static descriptions. What are people doing? How are they moving? What changes? What emotions are visible?
  - start_seconds: when this moment begins (use whole or half seconds like 0.0, 1.5, 3.0)
  - end_seconds: when this moment ends

Guidelines:
- Identify 3-8 key moments representing distinct visual events or content changes.
- Moments must NOT overlap and should cover the video's most interesting parts.
- MINIMUM DURATION: every moment MUST be at least 1.0 second long. Merge shorter events with adjacent moments.
- Use whole or half seconds for timestamps (0.0, 1.5, 3.0) — no sub-frame precision.
- The first moment starts at 0.0. The last moment ends at or near the video duration.
- Describe what people DO, not just what they look like. "She playfully squeezes the plushie and laughs" is better than "A woman holds a plushie"."""


def run_model(vertex, gs_uri, model_name):
    print(f"\n{'='*60}")
    print(f"MODEL: {model_name}")
    print(f"{'='*60}")

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"fileData": {"mimeType": "video/mp4", "fileUri": gs_uri}},
                    {"text": ENHANCED_PROMPT},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 3000,
            "responseMimeType": "application/json",
            "responseSchema": ASSET_ANALYSIS_SCHEMA,
        },
    }

    t0 = time.time()
    result = vertex.raw_generate_content(payload, model=model_name)
    elapsed = time.time() - t0

    print(f"Response in {elapsed:.1f}s")
    print(f"Tokens: {result['input_tokens']} input, {result['output_tokens']} output")

    raw_text = result.get("text", "")
    try:
        parsed = json.loads(raw_text)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(raw_text[:1000])

    return parsed if raw_text else None


def main():
    video_path = os.path.join(comp_videos_dir, "temp", "WhatsApp Video 2026-03-04 at 01.12.52.mp4")
    if not os.path.exists(video_path):
        print(f"ERROR: Video not found: {video_path}")
        sys.exit(1)

    print(f"Video: {video_path}")
    print(f"Size: {os.path.getsize(video_path) / (1024*1024):.1f} MB")

    config = Config()
    gcs = GCSStorageService(
        credentials_file=config.SERVICE_ACCOUNT_FILE,
        bucket_name="automatiq",
        folder_path="temp_analysis",
    )
    vertex = VertexAIProvider(gcs_storage_service=gcs)
    if not vertex.initialized:
        print("ERROR: Vertex AI provider failed to initialize.")
        sys.exit(1)

    print(f"\nUploading video to GCS...")
    t0 = time.time()
    video_url = vertex._upload_video_to_gcs(video_path)
    if not video_url:
        print("ERROR: GCS upload failed")
        sys.exit(1)
    print(f"Uploaded in {time.time() - t0:.1f}s")

    if "storage.googleapis.com/" in video_url:
        gs_uri = "gs://" + video_url.split("storage.googleapis.com/", 1)[1]
    else:
        gs_uri = video_url

    # Run both models
    run_model(vertex, gs_uri, "gemini-2.5-flash")
    run_model(vertex, gs_uri, "gemini-3-flash-preview")

    # Cleanup
    print(f"\nCleaning up GCS...")
    vertex._cleanup_gcs_video(video_url)
    print("Done.")


if __name__ == "__main__":
    main()
