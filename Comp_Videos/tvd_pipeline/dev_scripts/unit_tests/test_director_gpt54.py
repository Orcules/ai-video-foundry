"""Unit test: Director step with GPT-5.4 + reasoning_effort + strict JSON schema.

Tests that the OpenAI provider can call GPT-5.4 with:
  - reasoning_effort="high"
  - strict JSON schema (responseSchema → json_schema response_format)
  - The exact Director system/user prompts used in the pipeline

Run:
  cd Comp_Videos
  set -a && source .env && set +a
  python -m pytest tvd_pipeline/unit_tests/test_director_gpt54.py -v -s
  # or directly:
  python tvd_pipeline/unit_tests/test_director_gpt54.py
"""

import json
import os
import sys

# Ensure Comp_Videos is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tvd_pipeline.services.providers.openai_provider import OpenAIProvider
from tvd_pipeline.services.tasks.prompt_parsing import DIRECTOR_SCHEMA
from tvd_pipeline.prompt_loader import get_prompt_loader


# ---------------------------------------------------------------------------
# Test fixture: realistic OISHI HOUSE scenario (3 beats, 7 clips)
# ---------------------------------------------------------------------------

BEATS_TEXT = """Beat 1 | duration: 9.6s | VO: "You absolutely HAVE to try this hidden gem on Spalena street - the freshest sashimi I've ever tasted!"
Beat 2 | duration: 8.1s | VO: "Their rolls are perfectly balanced, the fish just melts on your tongue. And the lychee ice tea is incredible."
Beat 3 | duration: 7.1s | VO: "Whether you're visiting or you live here, this place is an absolute must. Trust me, you'll keep coming back!"
"""

CLIP_LIST_TEXT = """Clip 0: [VIDEO] Seated person at dark wooden table (1.5s, FIXED) — moment from asset video
Clip 1: [VIDEO] Camera pans across restaurant interior, warm lighting (2.5s, FIXED) — moment from asset video
Clip 2: [VIDEO] Low-angle view of restaurant counter with dishes (1.5s, FIXED) — moment from asset video
Clip 3: [VIDEO] Pan toward bar area, decorative shelves (1.5s, FIXED) — moment from asset video
Clip 4: [VIDEO] Close-up of menu or table setting (1.0s, FIXED) — moment from asset video
Clip 5: [IMAGE] Sushi rolls beautifully arranged on white plate (FLEXIBLE, 2-8s)
Clip 6: [IMAGE] Decorative ceramic plate with Japanese motif (FLEXIBLE, 2-8s)
"""

INFLUENCER_DESC = "Young woman, mid-20s, natural makeup, warm smile"


def _build_director_messages():
    """Build the exact messages that the pipeline sends to the Director."""
    loader = get_prompt_loader()
    system_msg = loader.get("ugc_director_system")
    user_msg = loader.get(
        "ugc_director_user",
        beats_text=BEATS_TEXT,
        clip_list_text=CLIP_LIST_TEXT,
        influencer_description=INFLUENCER_DESC,
    )
    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def test_director_gpt54():
    """Call GPT-5.4 with reasoning_effort=high, strict JSON schema, Director prompt."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("SKIP: OPENAI_API_KEY not set")
        return

    provider = OpenAIProvider(api_key=api_key)
    messages = _build_director_messages()

    print("\n--- Calling GPT-5.4 with reasoning_effort=high ---")
    print(f"System prompt length: {len(messages[0]['content'])} chars")
    print(f"User prompt length:   {len(messages[1]['content'])} chars")

    result = provider.call(
        model="gpt-5.4",
        messages=messages,
        reasoning_effort="high",
        responseSchema=DIRECTOR_SCHEMA,
    )

    text = result.get("text", "")
    print(f"\nTokens: input={result.get('input_tokens', 0)}, output={result.get('output_tokens', 0)}")
    print(f"Response length: {len(text)} chars")
    print(f"Raw response:\n{text[:2000]}")

    # Parse JSON
    parsed = json.loads(text)
    beats = parsed.get("beats", [])
    print(f"\nParsed {len(beats)} beats:")

    total_duration = 0
    influencer_duration = 0

    for beat in beats:
        bn = beat["beat_number"]
        td = beat["total_duration"]
        clips = beat["clips"]
        clip_total = sum(c["duration"] for c in clips)
        print(f"\n  Beat {bn} (target={td}s, clip_total={clip_total:.1f}s):")
        for c in clips:
            inf = "INF" if c.get("shows_influencer") else "   "
            idx = c.get("clip_index", "gen")
            print(f"    [{inf}] type={c['type']}, clip={idx}, dur={c['duration']}s — {c.get('description') or c.get('reason','')[:60]}")
            total_duration += c["duration"]
            if c.get("shows_influencer"):
                influencer_duration += c["duration"]

        # Verify timing math
        assert abs(clip_total - td) < 0.5, f"Beat {bn}: timing mismatch {clip_total} vs {td}"

    # Verify we got all 3 beats
    assert len(beats) == 3, f"Expected 3 beats, got {len(beats)}"

    # Verify influencer ratio
    inf_ratio = influencer_duration / total_duration if total_duration > 0 else 0
    print(f"\nInfluencer ratio: {inf_ratio:.0%} ({influencer_duration:.1f}s / {total_duration:.1f}s)")
    assert inf_ratio >= 0.30, f"Influencer ratio too low: {inf_ratio:.0%}"

    # Verify no product-specific generate descriptions
    for beat in beats:
        for c in beat["clips"]:
            if c["type"] == "generate" and c.get("description"):
                desc_lower = c["description"].lower()
                for banned in ["restaurant sign", "storefront", "restaurant entrance", "spalena street", "oishi"]:
                    assert banned not in desc_lower, (
                        f"Beat {beat['beat_number']}: generate clip describes banned content '{banned}': {c['description']}"
                    )

    print("\nAll assertions passed!")


if __name__ == "__main__":
    test_director_gpt54()
