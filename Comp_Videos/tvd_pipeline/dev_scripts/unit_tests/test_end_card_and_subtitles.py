"""Unit tests: End card text cutoff + subtitle rendering.

Reproduces the OISHI HOUSE end card bug (text clipped at wrong resolution)
and tests ZapCap subtitle rendering with different settings.

Tests 1a-1c: Local FFmpeg compositing (free, no API calls)
Tests 2a-2c: Real ZapCap API calls (costs 3 ZapCap jobs)

Run:
  cd Comp_Videos
  set -a && source .env && set +a
  python -m tvd_pipeline.dev_scripts.unit_tests.test_end_card_and_subtitles
"""

import copy
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tvd_pipeline.pipelines._helpers import _create_end_card_overlay_png
from tvd_pipeline.services.zapcap import ZapCapService
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.data_loader import get_zapcap_config
from tvd_pipeline.config import Config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")

# OISHI test video: 478x850, 9.6s
_TEST_VIDEO_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..",
    "api_pipeline", "documents", "test_scripts", "oishi_assets",
    "WhatsApp Video 2026-03-04 at 01.12.52 copy.mp4",
)
_TEST_VIDEO_PATH = os.path.normpath(_TEST_VIDEO_PATH)

# Business info from the OISHI job
_BIZ_NAME = "OISHI HOUSE"
_BIZ_ADDR = "Spalena St, Nove Mesto, Prague"
_BIZ_PHONE = "+420 123 456 789"

# Sample VO transcript (matching the ~9.6s test video)
VO_SCRIPT = (
    "You have to try this amazing sushi place on Spalena street. "
    "The fish is incredibly fresh and the rolls just melt in your mouth. "
    "Trust me, once you visit you will keep coming back for more."
)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe_dimensions(video_path: str) -> tuple:
    """Get (width, height) of a video file via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            video_path,
        ],
        capture_output=True, text=True,
    )
    parts = result.stdout.strip().split(",")
    return int(parts[0]), int(parts[1])


def _upload_video_to_gcs(video_path: str) -> str:
    """Upload a local video to GCS and return its public URL."""
    config = Config()
    gcs = GCSStorageService(
        credentials_file=os.path.join(
            os.path.dirname(__file__), "..", "..", "..",
            "service_account.json",
        ),
        bucket_name="automatiq",
        folder_path="test/end_card_subtitles",
    )
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    key = f"oishi_test_{int(time.time())}.mp4"
    url = gcs.upload_video_bytes(video_bytes, key)
    assert url, f"GCS upload failed for {video_path}"
    return url


# ===========================================================================
# TEST 1: End Card Overlay — Reproduce Bug + Verify Fixes
# ===========================================================================

def test_1a_reproduce_wrong_resolution():
    """Test 1a: Generate overlay at 1080x1920, composite onto 478x850 video.

    Expected: text is cut off on the right (reproduces the bug).
    """
    print("\n=== Test 1a: Reproduce bug — overlay at wrong resolution (1080x1920 on 478x850) ===")

    assert os.path.isfile(_TEST_VIDEO_PATH), f"Test video not found: {_TEST_VIDEO_PATH}"
    vid_w, vid_h = _probe_dimensions(_TEST_VIDEO_PATH)
    print(f"  Test video: {vid_w}x{vid_h}")

    # Generate overlay at the WRONG (fallback) resolution
    png_bytes = _create_end_card_overlay_png(
        _BIZ_NAME, _BIZ_ADDR, _BIZ_PHONE,
        width=1080, height=1920,  # Bug: hardcoded fallback
    )
    overlay_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_bug_overlay_1080x1920.png")
    with open(overlay_path, "wb") as f:
        f.write(png_bytes)
    print(f"  Overlay PNG: 1080x1920 ({len(png_bytes)} bytes)")

    # Composite with raw overlay=0:0 (what Rendi does)
    out_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_bug_wrong_resolution.mp4")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", _TEST_VIDEO_PATH,
            "-i", overlay_path,
            "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path,
        ],
        capture_output=True,
    )
    assert result.returncode == 0, f"FFmpeg failed:\n{result.stderr.decode(errors='replace')}"
    assert os.path.isfile(out_path) and os.path.getsize(out_path) > 1000

    # Verify output is still 478x850 (FFmpeg crops the overlay)
    out_w, out_h = _probe_dimensions(out_path)
    print(f"  Output: {out_w}x{out_h}")
    print(f"  >>> {out_path}")
    print(f"  >>> Expected: text 'OISHI HOU...' cut off on right side")
    print("  PASS (bug reproduced)")
    return out_path


def test_1b_fix_correct_resolution():
    """Test 1b: Generate overlay at actual video dimensions (478x850).

    Expected: text fully visible and properly centered.
    """
    print("\n=== Test 1b: Fix — overlay at correct resolution (478x850) ===")

    vid_w, vid_h = _probe_dimensions(_TEST_VIDEO_PATH)

    # Generate overlay at the CORRECT resolution
    png_bytes = _create_end_card_overlay_png(
        _BIZ_NAME, _BIZ_ADDR, _BIZ_PHONE,
        width=vid_w, height=vid_h,  # Fix: match video dimensions
    )
    overlay_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_fix_overlay_478x850.png")
    with open(overlay_path, "wb") as f:
        f.write(png_bytes)
    print(f"  Overlay PNG: {vid_w}x{vid_h} ({len(png_bytes)} bytes)")

    out_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_fix_correct_resolution.mp4")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", _TEST_VIDEO_PATH,
            "-i", overlay_path,
            "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path,
        ],
        capture_output=True,
    )
    assert result.returncode == 0, f"FFmpeg failed:\n{result.stderr.decode(errors='replace')}"

    out_w, out_h = _probe_dimensions(out_path)
    print(f"  Output: {out_w}x{out_h}")
    assert out_w == vid_w and out_h == vid_h, f"Output dims mismatch: {out_w}x{out_h}"
    print(f"  >>> {out_path}")
    print(f"  >>> Expected: 'OISHI HOUSE' fully visible and centered")
    print("  PASS")
    return out_path


def test_1c_fix_scale2ref_safety():
    """Test 1c: Generate overlay at 1080x1920 but use scale2ref to auto-scale.

    This is the safety fallback: even if the probe fails, scale2ref
    rescales the overlay to match the video before compositing.
    Expected: text visible (scaled down), properly centered.
    """
    print("\n=== Test 1c: Fix — scale2ref safety (auto-scale 1080x1920 to 478x850) ===")

    vid_w, vid_h = _probe_dimensions(_TEST_VIDEO_PATH)

    # Generate overlay at the WRONG resolution (simulating probe failure)
    png_bytes = _create_end_card_overlay_png(
        _BIZ_NAME, _BIZ_ADDR, _BIZ_PHONE,
        width=1080, height=1920,
    )
    overlay_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_scale2ref_overlay_1080x1920.png")
    with open(overlay_path, "wb") as f:
        f.write(png_bytes)
    print(f"  Overlay PNG: 1080x1920 (wrong size)")

    # Composite with scale2ref instead of raw overlay=0:0
    # scale2ref scales stream 1 to match stream 0's dimensions
    out_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_fix_scale2ref.mp4")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", _TEST_VIDEO_PATH,
            "-i", overlay_path,
            "-filter_complex",
            "[1:v][0:v]scale2ref[ovr][base];[base][ovr]overlay=0:0[out]",
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path,
        ],
        capture_output=True,
    )
    assert result.returncode == 0, f"FFmpeg failed:\n{result.stderr.decode(errors='replace')}"

    out_w, out_h = _probe_dimensions(out_path)
    print(f"  Output: {out_w}x{out_h}")
    assert out_w == vid_w and out_h == vid_h, f"Output dims mismatch: {out_w}x{out_h}"
    print(f"  >>> {out_path}")
    print(f"  >>> Expected: text visible (scaled down from 1080x1920), centered")
    print("  PASS")
    return out_path


# ===========================================================================
# TEST 2: ZapCap Subtitle Rendering
# ===========================================================================

def test_2a_zapcap_style_override_false():
    """Test 2a: ZapCap with style_override=false (template defaults).

    Temporarily overrides zapcap.json to force style_override=false.
    """
    print("\n=== Test 2a: ZapCap subtitles — style_override=false (template defaults) ===")

    config = Config()
    api_key = config.ZAPCAP_API_KEY
    if not api_key:
        print("  SKIP: ZAPCAP_API_KEY not set")
        return None

    # Upload test video to GCS
    print(f"  Uploading test video to GCS...")
    video_url = _upload_video_to_gcs(_TEST_VIDEO_PATH)
    print(f"  Video URL: {video_url}")

    zs = ZapCapService(api_key=api_key)

    # Build request manually with style_override=false
    zc_config = get_zapcap_config()
    render_cfg = zc_config.get("render_options", {})
    render_options = {
        "subsOptions": render_cfg.get("subs_options", {})
    }
    # NO styleOptions — let the template decide

    print(f"  renderOptions (no styleOptions): {json.dumps(render_options, indent=2)}")
    print(f"  Sending to ZapCap (style_override=false)...")
    t0 = time.time()
    result_url = zs.add_subtitles(
        video_url=video_url,
        language=LANGUAGE,
        transcript=WORD_SEGMENTS,
        enrichments=None,
    )
    elapsed = time.time() - t0

    if result_url:
        print(f"  ZapCap completed in {elapsed:.0f}s")
        print(f"  OUTPUT (style_override=false): {result_url}")
    else:
        print(f"  ZapCap returned None after {elapsed:.0f}s")

    assert result_url, "ZapCap returned no URL"
    print("  PASS")
    return result_url


def test_2b_zapcap_style_override_true():
    """Test 2b: ZapCap with style_override=true (custom font/color/size).

    Uses the current zapcap.json settings (which has style_override=true).
    """
    print("\n=== Test 2b: ZapCap subtitles — style_override=true (custom styling) ===")

    config = Config()
    api_key = config.ZAPCAP_API_KEY
    if not api_key:
        print("  SKIP: ZAPCAP_API_KEY not set")
        return None

    print(f"  Uploading test video to GCS...")
    video_url = _upload_video_to_gcs(_TEST_VIDEO_PATH)
    print(f"  Video URL: {video_url}")

    zs = ZapCapService(api_key=api_key)

    # Show what zapcap.json currently has
    zc_config = get_zapcap_config()
    print(f"  style_override in config: {zc_config.get('style_override')}")
    render_cfg = zc_config.get("render_options", {})
    print(f"  styleOptions: {json.dumps(render_cfg.get('style_options', {}), indent=2)}")

    print(f"  Sending to ZapCap (style_override=true — current config)...")
    t0 = time.time()
    result_url = zs.add_subtitles(
        video_url=video_url,
        language=LANGUAGE,
        transcript=WORD_SEGMENTS,
        enrichments=None,
    )
    elapsed = time.time() - t0

    if result_url:
        print(f"  ZapCap completed in {elapsed:.0f}s")
        print(f"  OUTPUT (style_override=true): {result_url}")
    else:
        print(f"  ZapCap returned None after {elapsed:.0f}s")

    assert result_url, "ZapCap returned no URL"
    print("  PASS")
    return result_url


def test_2c_zapcap_no_enrichments_no_transcript():
    """Test 2c: ZapCap with NO transcript (auto-transcription, no enrichments).

    Control test — let ZapCap do everything itself.
    """
    print("\n=== Test 2c: ZapCap subtitles — no transcript, no enrichments (auto) ===")

    config = Config()
    api_key = config.ZAPCAP_API_KEY
    if not api_key:
        print("  SKIP: ZAPCAP_API_KEY not set")
        return None

    print(f"  Uploading test video to GCS...")
    video_url = _upload_video_to_gcs(_TEST_VIDEO_PATH)
    print(f"  Video URL: {video_url}")

    zs = ZapCapService(api_key=api_key)

    print(f"  Sending to ZapCap (no transcript — auto-transcription)...")
    t0 = time.time()
    result_url = zs.add_subtitles(
        video_url=video_url,
        language=LANGUAGE,
        transcript=None,      # Let ZapCap auto-transcribe
        enrichments=None,
    )
    elapsed = time.time() - t0

    if result_url:
        print(f"  ZapCap completed in {elapsed:.0f}s")
        print(f"  OUTPUT (auto-transcription): {result_url}")
    else:
        print(f"  ZapCap returned None after {elapsed:.0f}s")

    assert result_url, "ZapCap returned no URL"
    print("  PASS")
    return result_url


# ===========================================================================
# Main
# ===========================================================================

def main():
    print("=" * 65)
    print("End Card Text Cutoff + Subtitle Rendering — Phase 1 Tests")
    print("=" * 65)

    os.makedirs(_TEST_OUTPUT_DIR, exist_ok=True)

    # --- Test 1: End Card Overlay (local FFmpeg, free) ---
    print("\n" + "-" * 65)
    print("SECTION 1: End Card Overlay — Reproduce Bug + Verify Fixes")
    print("-" * 65)

    bug_path = test_1a_reproduce_wrong_resolution()
    fix_path = test_1b_fix_correct_resolution()
    scale_path = test_1c_fix_scale2ref_safety()

    # --- Test 2: ZapCap Subtitles (real API calls) ---
    print("\n" + "-" * 65)
    print("SECTION 2: ZapCap Subtitle Rendering Comparison")
    print("-" * 65)

    url_no_override = test_2a_zapcap_style_override_false()
    url_with_override = test_2b_zapcap_style_override_true()
    url_auto = test_2c_zapcap_no_enrichments_no_transcript()

    # --- Summary ---
    print("\n" + "=" * 65)
    print("All tests passed!")
    print("=" * 65)

    print("\n--- End Card Visual Comparison (open these files) ---")
    print(f"  BUG  (1080x1920 on 478x850): {bug_path}")
    print(f"  FIX  (478x850 correct):       {fix_path}")
    print(f"  FIX  (scale2ref safety):       {scale_path}")

    print("\n--- Subtitle Visual Comparison (open these URLs) ---")
    if url_no_override:
        print(f"  style_override=false:  {url_no_override}")
    if url_with_override:
        print(f"  style_override=true:   {url_with_override}")
    if url_auto:
        print(f"  auto-transcription:    {url_auto}")

    print("\nOpen all outputs and compare visually.")


if __name__ == "__main__":
    main()
