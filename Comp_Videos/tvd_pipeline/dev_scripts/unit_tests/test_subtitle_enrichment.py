"""Unit test: Subtitle enrichment (emoji + importance markers) for ZapCap BYOT.

Tests the full end-to-end flow:
  1. Normalize a realistic ElevenLabs word-segment transcript
  2. Call the LLM enrichment task (real Vertex call)
  3. Merge enrichments into ZapCap BYOT entries
  4. Send the enriched transcript + a real video to ZapCap
  5. Get back a subtitled video URL — visually verify emoji appear

Uses a hardcoded transcript + an existing GCS video from a previous pipeline
run, so the only costs are: 1 Gemini Flash call + 1 ZapCap subtitle job.

Run:
  cd Comp_Videos
  set -a && source .env && set +a
  python -m tvd_pipeline.dev_scripts.unit_tests.test_subtitle_enrichment
"""

import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tvd_pipeline.services.providers.vertex import VertexAIProvider
from tvd_pipeline.services.zapcap import ZapCapService
from tvd_pipeline.services.tasks.subtitle_enrichment import (
    enrich_transcript_for_subtitles,
    ENRICHMENT_SCHEMA,
)
from tvd_pipeline.data_loader import get_zapcap_config
from tvd_pipeline.config import Config

# ---------------------------------------------------------------------------
# Test fixture: realistic ElevenLabs word segments (influencer VO, English)
# Simulates a ~9s VO about a restaurant recommendation.
# ---------------------------------------------------------------------------

VO_SCRIPT = (
    "You have to try this amazing sushi place on Spalena street. "
    "The fish is incredibly fresh and the rolls just melt in your mouth. "
    "Trust me, once you visit you will keep coming back for more."
)

# ElevenLabs-style word segments with timing
WORD_SEGMENTS = [
    {"text": "You", "start_time": 0.0, "end_time": 0.18},
    {"text": "have", "start_time": 0.18, "end_time": 0.32},
    {"text": "to", "start_time": 0.32, "end_time": 0.40},
    {"text": "try", "start_time": 0.40, "end_time": 0.58},
    {"text": "this", "start_time": 0.58, "end_time": 0.72},
    {"text": "amazing", "start_time": 0.72, "end_time": 1.10},
    {"text": "sushi", "start_time": 1.10, "end_time": 1.42},
    {"text": "place", "start_time": 1.42, "end_time": 1.68},
    {"text": "on", "start_time": 1.68, "end_time": 1.80},
    {"text": "Spalena", "start_time": 1.80, "end_time": 2.20},
    {"text": "street.", "start_time": 2.20, "end_time": 2.60},
    {"text": "The", "start_time": 2.90, "end_time": 3.02},
    {"text": "fish", "start_time": 3.02, "end_time": 3.28},
    {"text": "is", "start_time": 3.28, "end_time": 3.38},
    {"text": "incredibly", "start_time": 3.38, "end_time": 3.90},
    {"text": "fresh", "start_time": 3.90, "end_time": 4.20},
    {"text": "and", "start_time": 4.20, "end_time": 4.35},
    {"text": "the", "start_time": 4.35, "end_time": 4.45},
    {"text": "rolls", "start_time": 4.45, "end_time": 4.78},
    {"text": "just", "start_time": 4.78, "end_time": 4.98},
    {"text": "melt", "start_time": 4.98, "end_time": 5.25},
    {"text": "in", "start_time": 5.25, "end_time": 5.35},
    {"text": "your", "start_time": 5.35, "end_time": 5.50},
    {"text": "mouth.", "start_time": 5.50, "end_time": 5.90},
    {"text": "Trust", "start_time": 6.20, "end_time": 6.48},
    {"text": "me,", "start_time": 6.48, "end_time": 6.65},
    {"text": "once", "start_time": 6.65, "end_time": 6.88},
    {"text": "you", "start_time": 6.88, "end_time": 7.00},
    {"text": "visit", "start_time": 7.00, "end_time": 7.32},
    {"text": "you", "start_time": 7.32, "end_time": 7.45},
    {"text": "will", "start_time": 7.45, "end_time": 7.60},
    {"text": "keep", "start_time": 7.60, "end_time": 7.85},
    {"text": "coming", "start_time": 7.85, "end_time": 8.15},
    {"text": "back", "start_time": 8.15, "end_time": 8.40},
    {"text": "for", "start_time": 8.40, "end_time": 8.55},
    {"text": "more.", "start_time": 8.55, "end_time": 8.90},
]

LANGUAGE = "en"

# An existing GCS video from a previous pipeline run (already has audio).
# Used as the base video for the ZapCap subtitle test.
TEST_VIDEO_URL = (
    "https://storage.googleapis.com/automatiq/"
    "Comp/Final_Video/Comp/Final_Video/product_videos/"
    "row_None_subtitled_1772328927.mp4"
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_normalize_transcript():
    """Test 1: ZapCap normalization produces correct BYOT format (offline)."""
    print("\n=== Test 1: Normalize transcript (offline) ===")
    zs = ZapCapService(api_key="test_key")
    normalized = zs._normalize_transcript_for_zapcap(WORD_SEGMENTS)

    assert normalized is not None, "Normalization returned None"
    assert len(normalized) == len(WORD_SEGMENTS), (
        f"Expected {len(WORD_SEGMENTS)} words, got {len(normalized)}"
    )

    for i, entry in enumerate(normalized):
        assert "text" in entry, f"Entry {i} missing 'text'"
        assert entry["type"] == "word", f"Entry {i} type is '{entry['type']}'"
        assert "start_time" in entry, f"Entry {i} missing 'start_time'"
        assert "end_time" in entry, f"Entry {i} missing 'end_time'"
        assert entry["end_time"] > entry["start_time"], f"Entry {i} end <= start"

    for i in range(1, len(normalized)):
        assert normalized[i]["start_time"] >= normalized[i - 1]["end_time"] - 0.01, (
            f"Entry {i} overlaps entry {i-1}"
        )

    print(f"  {len(normalized)} words normalized")
    print(f"  Span: {normalized[0]['start_time']:.2f}s - {normalized[-1]['end_time']:.2f}s")
    print("  PASS")
    return normalized


def test_config_loads():
    """Test 2: zapcap.json config loads with expected keys (offline)."""
    print("\n=== Test 2: ZapCap config loads (offline) ===")
    cfg = get_zapcap_config()

    assert "template_ids" in cfg, "Missing 'template_ids'"
    assert "style_override" in cfg, "Missing 'style_override'"
    assert "render_options" in cfg, "Missing 'render_options'"
    assert len(cfg["template_ids"]) == 15, f"Expected 15 templates, got {len(cfg['template_ids'])}"
    assert cfg["style_override"] is False, "style_override should be False by default"
    assert cfg["render_options"]["subs_options"]["emoji"] is True, "emoji should be True"

    print(f"  Templates: {len(cfg['template_ids'])}")
    print(f"  style_override: {cfg['style_override']}")
    print(f"  subs_options: {cfg['render_options']['subs_options']}")
    print("  PASS")


def test_render_options_config():
    """Test 3: renderOptions built correctly from config (offline)."""
    print("\n=== Test 3: renderOptions from config (offline) ===")

    cfg = get_zapcap_config()
    render_cfg = cfg.get("render_options", {})

    render_options = {"subsOptions": render_cfg.get("subs_options", {})}
    if cfg.get("style_override", False):
        render_options["styleOptions"] = render_cfg.get("style_options", {})

    assert "subsOptions" in render_options, "Missing subsOptions"
    assert "styleOptions" not in render_options, (
        "styleOptions should be absent when style_override=false"
    )

    print(f"  renderOptions keys: {list(render_options.keys())}")
    print("  PASS")


def test_enrichment_graceful_failure():
    """Test 4: Enrichment returns None on LLM failure (offline)."""
    print("\n=== Test 4: Graceful failure on bad call_fn (offline) ===")

    def failing_call_fn(messages, **kwargs):
        raise RuntimeError("Simulated LLM failure")

    zs = ZapCapService(api_key="test_key")
    normalized = zs._normalize_transcript_for_zapcap(WORD_SEGMENTS)

    result = enrich_transcript_for_subtitles(
        call_fn=failing_call_fn,
        word_segments=normalized,
        vo_script=VO_SCRIPT,
        language=LANGUAGE,
    )

    assert result is None, f"Expected None on failure, got {type(result)}"
    print("  Returned None (graceful degradation)")
    print("  PASS")


def test_enrichment_empty_transcript():
    """Test 5: Enrichment returns None on empty transcript (offline)."""
    print("\n=== Test 5: Empty transcript (offline) ===")

    def should_not_be_called(messages, **kwargs):
        raise AssertionError("call_fn should not be called for empty transcript")

    result = enrich_transcript_for_subtitles(
        call_fn=should_not_be_called,
        word_segments=[],
        vo_script=VO_SCRIPT,
        language=LANGUAGE,
    )

    assert result is None, f"Expected None for empty transcript, got {type(result)}"
    print("  Returned None for empty input")
    print("  PASS")


def test_enrichment_llm(normalized_transcript):
    """Test 6: Real LLM enrichment call (1 Gemini Flash call)."""
    print("\n=== Test 6: LLM enrichment (real Vertex call) ===")

    vertex = VertexAIProvider()
    if not vertex.initialized:
        print("  SKIP: Vertex AI provider failed to initialize")
        return None

    def call_fn(messages, **kwargs):
        return vertex.call(
            model="gemini-3-flash-preview",
            messages=messages,
            **kwargs,
        )

    print(f"  Sending {len(normalized_transcript)} words to Gemini Flash...")
    t0 = time.time()
    enrichments = enrich_transcript_for_subtitles(
        call_fn=call_fn,
        word_segments=normalized_transcript,
        vo_script=VO_SCRIPT,
        language=LANGUAGE,
    )
    elapsed = time.time() - t0
    print(f"  LLM call took {elapsed:.1f}s")

    assert enrichments is not None, "Enrichment returned None (LLM failed?)"
    assert isinstance(enrichments, dict), f"Expected dict, got {type(enrichments)}"

    emoji_count = sum(1 for v in enrichments.values() if "emoji" in v)
    important_count = sum(1 for v in enrichments.values() if v.get("important"))

    print(f"  Total annotated words: {len(enrichments)}/{len(normalized_transcript)}")
    print(f"  Emoji: {emoji_count} words")
    print(f"  Important: {important_count} words")

    # Validate index bounds
    max_idx = len(normalized_transcript) - 1
    for idx in enrichments:
        assert 0 <= idx <= max_idx, f"Index {idx} out of bounds (max={max_idx})"

    # Print annotations
    print("\n  Annotations:")
    for idx in sorted(enrichments.keys()):
        word = normalized_transcript[idx]["text"]
        ann = enrichments[idx]
        emoji_str = ann.get("emoji", "")
        imp_str = " [IMPORTANT]" if ann.get("important") else ""
        try:
            print(f"    [{idx:2d}] {word:15s} emoji={emoji_str}{imp_str}")
        except UnicodeEncodeError:
            print(f"    [{idx:2d}] {word:15s} emoji=(unicode){imp_str}")

    assert emoji_count >= 1, f"Expected at least 1 emoji, got {emoji_count}"
    assert emoji_count <= 10, f"Too many emoji ({emoji_count})"
    assert important_count >= 3, f"Expected at least 3 important words, got {important_count}"
    assert important_count <= 20, f"Too many important ({important_count})"

    print("  PASS")
    return enrichments


def test_apply_enrichments(normalized_transcript, enrichments):
    """Test 7: Merge enrichments into BYOT entries (offline)."""
    print("\n=== Test 7: Apply enrichments to BYOT (offline) ===")

    transcript_copy = copy.deepcopy(normalized_transcript)
    result = ZapCapService._apply_enrichments(transcript_copy, enrichments)

    assert result is transcript_copy, "Should modify in-place and return same list"
    assert len(result) == len(normalized_transcript), "Length should not change"

    enriched_count = sum(1 for e in result if e.get("emoji") or e.get("important"))
    print(f"  Enriched entries: {enriched_count}/{len(result)}")

    for i, entry in enumerate(result):
        assert "text" in entry, f"Entry {i} lost 'text'"
        assert entry["type"] == "word", f"Entry {i} lost 'type'"
        assert "start_time" in entry, f"Entry {i} lost 'start_time'"
        assert "end_time" in entry, f"Entry {i} lost 'end_time'"

    for idx, ann in enrichments.items():
        if "emoji" in ann:
            assert "emoji" in result[idx], f"Entry {idx} missing emoji after merge"
        if ann.get("important"):
            assert result[idx].get("important") is True, f"Entry {idx} missing important"

    print("  PASS")
    return result


def test_zapcap_with_enrichments(normalized_transcript, enrichments):
    """Test 8: Send enriched transcript + real video to ZapCap, get subtitled video.

    This is the real end-to-end test. Costs: 1 ZapCap subtitle job.
    Produces a video URL you can open to visually verify emoji appear.
    """
    print("\n=== Test 8: ZapCap with enriched BYOT (real ZapCap call) ===")

    config = Config()
    api_key = config.ZAPCAP_API_KEY
    if not api_key:
        print("  SKIP: ZAPCAP_API_KEY not set")
        return None

    zs = ZapCapService(api_key=api_key)

    # Show what we're sending
    transcript_copy = copy.deepcopy(normalized_transcript)
    enriched_transcript = ZapCapService._apply_enrichments(transcript_copy, enrichments)

    emoji_entries = [e for e in enriched_transcript if e.get("emoji")]
    important_entries = [e for e in enriched_transcript if e.get("important")]
    print(f"  Video: {TEST_VIDEO_URL[:80]}...")
    print(f"  Transcript: {len(enriched_transcript)} words")
    print(f"  Enriched: {len(emoji_entries)} emoji, {len(important_entries)} important")

    # Log a few sample entries for debugging
    print("\n  Sample enriched entries sent to ZapCap:")
    for e in enriched_transcript:
        if e.get("emoji") or e.get("important"):
            try:
                print(f"    {json.dumps(e, ensure_ascii=False)}")
            except UnicodeEncodeError:
                print(f"    {e['text']} (has emoji/important, can't display)")

    # Run ZapCap
    print(f"\n  Sending to ZapCap (upload + process, may take 1-3 minutes)...")
    t0 = time.time()
    result_url = zs.add_subtitles(
        video_url=TEST_VIDEO_URL,
        language=LANGUAGE,
        transcript=WORD_SEGMENTS,      # raw segments — add_subtitles normalizes internally
        enrichments=enrichments,        # LLM enrichments — merged after normalization
    )
    elapsed = time.time() - t0

    if result_url:
        print(f"\n  ZapCap completed in {elapsed:.0f}s")
        print(f"  OUTPUT VIDEO: {result_url}")
        print(f"\n  >>> Open this URL to visually check for emoji in subtitles <<<")
    else:
        print(f"\n  ZapCap returned None after {elapsed:.0f}s")
        print("  Check the ZapCap logs above for errors")

    assert result_url is not None, "ZapCap returned no URL"
    assert result_url.startswith("http"), f"Expected URL, got: {result_url}"

    print("  PASS")
    return result_url


def test_zapcap_without_enrichments():
    """Test 9: Send same video + transcript WITHOUT enrichments (control group).

    Produces a video URL without emoji — compare visually with Test 8.
    """
    print("\n=== Test 9: ZapCap WITHOUT enrichments — control (real ZapCap call) ===")

    config = Config()
    api_key = config.ZAPCAP_API_KEY
    if not api_key:
        print("  SKIP: ZAPCAP_API_KEY not set")
        return None

    zs = ZapCapService(api_key=api_key)

    print(f"  Video: {TEST_VIDEO_URL[:80]}...")
    print(f"  Transcript: {len(WORD_SEGMENTS)} words (no enrichments)")
    print(f"\n  Sending to ZapCap (control — no emoji/important)...")
    t0 = time.time()
    result_url = zs.add_subtitles(
        video_url=TEST_VIDEO_URL,
        language=LANGUAGE,
        transcript=WORD_SEGMENTS,
        enrichments=None,               # No enrichments
    )
    elapsed = time.time() - t0

    if result_url:
        print(f"\n  ZapCap completed in {elapsed:.0f}s")
        print(f"  CONTROL VIDEO (no emoji): {result_url}")
    else:
        print(f"\n  ZapCap returned None after {elapsed:.0f}s")

    assert result_url is not None, "ZapCap returned no URL"
    print("  PASS")
    return result_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Subtitle Enrichment — Full E2E Test Suite")
    print("=" * 60)

    # --- Offline tests (free) ---
    normalized = test_normalize_transcript()
    test_config_loads()
    test_render_options_config()
    test_enrichment_graceful_failure()
    test_enrichment_empty_transcript()

    # --- Real LLM test (1 Gemini Flash call) ---
    enrichments = test_enrichment_llm(normalized)
    if not enrichments:
        print("\nStopping: LLM enrichment failed, cannot run ZapCap tests")
        return

    # --- Merge test (offline, uses LLM result) ---
    test_apply_enrichments(normalized, enrichments)

    # --- Real ZapCap tests (2 ZapCap jobs) ---
    enriched_url = test_zapcap_with_enrichments(normalized, enrichments)
    control_url = test_zapcap_without_enrichments()

    # --- Summary ---
    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)
    print("\nVisual comparison:")
    if enriched_url:
        print(f"  WITH emoji:    {enriched_url}")
    if control_url:
        print(f"  WITHOUT emoji: {control_url}")
    print("\nOpen both URLs and compare subtitles visually.")


if __name__ == "__main__":
    main()
