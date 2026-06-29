"""Reproduce the xfade silent drop bug with real clip URLs from the OISHI HOUSE job.

The hypothesis: when a low-resolution video asset clip (478x850) is placed between
higher-resolution Veo clips (1072x1928) in an xfade concat, FFmpeg silently drops
the asset clip and holds the previous clip's last frame instead.

This test sends the exact same 3 clips (s1_c0, s1_c1, s1_c2) to Rendi xfade concat
and verifies whether the asset clip (s1_c1) appears in the output.

Test structure:
  - Test A: Original clips as-is (should reproduce the bug)
  - Test B: Pre-scale s1_c1 to 1080x1920 before concat (should fix it)
  - Test C: 3 Veo clips only, no asset clip (control — should work fine)

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.unit_tests.test_xfade_asset_drop
"""

import os
import sys
import json
import subprocess
import tempfile

# Ensure Comp_Videos is on the path
script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)

from tvd_pipeline.config import Config
from tvd_pipeline.services.rendi import RendiService

# ── Real clip URLs from job bc57e6d2-ba7c-48b7-b0c8-17ac664b0c33 ──
# s1_c0: generate (influencer), 1072x1928 @24fps, 2.0s
CLIP_S1_C0 = "https://storage.rendi.dev/files/a44dc509-4f97-45ab-bda5-98da7420a52a/3260fe66-fc44-473c-961d-98d3115dc8f4/trimmed_video.mp4"
# s1_c1: video asset (restaurant), 478x850 @30fps, 1.5s — THE CLIP THAT DROPS
CLIP_S1_C1 = "https://storage.rendi.dev/files/a44dc509-4f97-45ab-bda5-98da7420a52a/4e552fa8-c6c9-4d40-b2d0-b6224bb2a85d/trimmed_video.mp4"
# s1_c2: image animation (sushi), 720x1280 @24fps, 4.4s
CLIP_S1_C2 = "https://storage.googleapis.com/automatiq/Comp/Final_Video/veo3_videos/veo3_1772981955_5138.mp4"
# s2_c0: image animation (sushi rolls), used as control clip
CLIP_S2_C0 = "https://storage.rendi.dev/files/a44dc509-4f97-45ab-bda5-98da7420a52a/72427296-e5aa-4704-9155-1f2c186358a3/trimmed_video.mp4"

DISSOLVE = 0.075  # from pipeline_defaults.json


def probe_resolution(url):
    """Get width x height of a video URL."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=30
        )
        parts = result.stdout.strip().split(",")
        if len(parts) >= 3:
            return int(parts[0]), int(parts[1]), parts[2]
    except Exception as e:
        print(f"  probe failed: {e}")
    return None, None, None


def extract_frames(video_path, output_dir, fps=5):
    """Extract frames at given fps, return list of frame paths."""
    os.makedirs(output_dir, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vf", f"fps={fps}",
         os.path.join(output_dir, "f_%03d.png"), "-loglevel", "error"],
        timeout=60
    )
    frames = sorted([os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.endswith(".png")])
    return frames


def frame_diff(frame_a, frame_b):
    """Compute mean pixel difference between two frames. Resizes to match if needed."""
    try:
        from PIL import Image
        import numpy as np
        img_a = Image.open(frame_a).convert("RGB")
        img_b = Image.open(frame_b).convert("RGB")
        # Resize both to the smaller dimensions for fair comparison
        target_size = (min(img_a.width, img_b.width), min(img_a.height, img_b.height))
        if target_size[0] < 100:
            target_size = (360, 640)  # minimum useful size
        img_a = img_a.resize(target_size, Image.LANCZOS)
        img_b = img_b.resize(target_size, Image.LANCZOS)
        arr_a = np.array(img_a, dtype=float)
        arr_b = np.array(img_b, dtype=float)
        return float(np.mean(np.abs(arr_a - arr_b)))
    except ImportError:
        # Fallback: compare file sizes as rough proxy
        size_a = os.path.getsize(frame_a)
        size_b = os.path.getsize(frame_b)
        return abs(size_a - size_b) / max(size_a, size_b) * 100


def check_clip_appears(concat_url, clip_url, expected_start_s, clip_label, tmp_dir):
    """Check if a clip's content appears in the concat output at the expected position."""
    print(f"\n  Checking if {clip_label} appears at ~{expected_start_s:.1f}s...")

    # Extract frames from the clip itself
    clip_dir = os.path.join(tmp_dir, f"{clip_label}_clip")
    clip_path = os.path.join(tmp_dir, f"{clip_label}.mp4")
    subprocess.run(["curl", "-sL", "-o", clip_path, clip_url], timeout=60)
    clip_frames = extract_frames(clip_path, clip_dir, fps=5)
    if not clip_frames:
        print(f"  ERROR: Could not extract frames from {clip_label}")
        return False

    # Extract frames from the concat at the expected position
    concat_dir = os.path.join(tmp_dir, f"{clip_label}_concat_region")
    concat_path = os.path.join(tmp_dir, "concat.mp4")
    # Download concat if not already
    if not os.path.exists(concat_path):
        subprocess.run(["curl", "-sL", "-o", concat_path, concat_url], timeout=120)
    region_frames = extract_frames_region(concat_path, concat_dir,
                                          start=expected_start_s + 0.3,
                                          duration=0.6, fps=5)
    if not region_frames:
        print(f"  ERROR: Could not extract frames from concat region")
        return False

    # Compare: clip's first frame vs concat frame at expected position
    clip_first = clip_frames[0]
    best_diff = float('inf')
    for rf in region_frames:
        d = frame_diff(clip_first, rf)
        best_diff = min(best_diff, d)

    # Also compare: previous clip's last frame vs concat frame at expected position
    # (if the clip dropped, concat frame will match previous clip, not this clip)
    prev_clip_dir = os.path.join(tmp_dir, "prev_clip")
    if os.path.exists(prev_clip_dir):
        prev_frames = sorted([os.path.join(prev_clip_dir, f) for f in os.listdir(prev_clip_dir) if f.endswith(".png")])
        if prev_frames:
            prev_last = prev_frames[-1]
            prev_diffs = [frame_diff(prev_last, rf) for rf in region_frames]
            min_prev_diff = min(prev_diffs)
            print(f"  Diff from {clip_label} first frame to concat region: {best_diff:.1f}")
            print(f"  Diff from previous clip last frame to concat region: {min_prev_diff:.1f}")
            if min_prev_diff < best_diff and min_prev_diff < 10:
                print(f"  RESULT: {clip_label} DID NOT RENDER — previous clip's frame is held (xfade drop)")
                return False
            elif best_diff < 30:
                print(f"  RESULT: {clip_label} RENDERED correctly")
                return True
            else:
                print(f"  RESULT: Inconclusive (neither match well)")
                return None

    print(f"  Diff from {clip_label} first frame to concat region: {best_diff:.1f}")
    appears = best_diff < 30
    print(f"  RESULT: {clip_label} {'RENDERED' if appears else 'DID NOT RENDER'}")
    return appears


def extract_frames_region(video_path, output_dir, start, duration, fps=5):
    """Extract frames from a specific time region."""
    os.makedirs(output_dir, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
         "-i", video_path, "-vf", f"fps={fps}",
         os.path.join(output_dir, "f_%03d.png"), "-loglevel", "error"],
        timeout=60
    )
    frames = sorted([os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.endswith(".png")])
    return frames


def run_test(rendi, label, clips, durations, dissolve, tmp_dir):
    """Run a single xfade concat test and verify clip presence."""
    print(f"\n{'='*60}")
    print(f"TEST {label}")
    print(f"{'='*60}")

    # Show clip info
    for i, (url, dur) in enumerate(zip(clips, durations)):
        w, h, fps = probe_resolution(url)
        print(f"  Clip {i}: {w}x{h} @{fps}, {dur}s")
        print(f"    URL: {url[:80]}...")

    # Build video_data in the same format as the pipeline
    video_data = []
    for url, dur in zip(clips, durations):
        video_data.append({"video_url": url, "duration": dur})

    print(f"\n  Sending to Rendi xfade concat (dissolve={dissolve}s)...")
    result_url = rendi.concatenate_videos(
        video_data=video_data,
        video_only=True,
        dissolve_seconds=dissolve,
    )

    if not result_url:
        print("  FAILED: Rendi returned None")
        return None

    print(f"  Concat output: {result_url}")

    # Probe output
    w, h, fps = probe_resolution(result_url)
    print(f"  Output resolution: {w}x{h} @{fps}")

    # Download concat
    concat_path = os.path.join(tmp_dir, "concat.mp4")
    if os.path.exists(concat_path):
        os.remove(concat_path)
    subprocess.run(["curl", "-sL", "-o", concat_path, result_url], timeout=120)

    # Extract ALL frames at 5fps for analysis
    all_frames_dir = os.path.join(tmp_dir, "all_frames")
    if os.path.exists(all_frames_dir):
        import shutil
        shutil.rmtree(all_frames_dir)
    all_frames = extract_frames(concat_path, all_frames_dir, fps=5)
    print(f"  Extracted {len(all_frames)} frames at 5fps")

    # Extract frames from the first clip (for "previous clip last frame" comparison)
    prev_clip_dir = os.path.join(tmp_dir, "prev_clip")
    if os.path.exists(prev_clip_dir):
        import shutil
        shutil.rmtree(prev_clip_dir)
    prev_path = os.path.join(tmp_dir, "prev_clip.mp4")
    subprocess.run(["curl", "-sL", "-o", prev_path, clips[0]], timeout=60)
    extract_frames(prev_path, prev_clip_dir, fps=5)

    # Check if clip 1 (the asset clip) appears at its expected position
    # Expected start: duration_of_clip0 - dissolve
    expected_start = durations[0] - dissolve
    clip1_appears = check_clip_appears(
        result_url, clips[1], expected_start, "clip_1", tmp_dir
    )

    return {"url": result_url, "clip1_appears": clip1_appears}


def main():
    print("=" * 60)
    print("XFADE ASSET DROP REPRODUCTION TEST")
    print("=" * 60)
    print(f"Hypothesis: Low-res video asset clip silently drops in xfade")
    print(f"Job: bc57e6d2-ba7c-48b7-b0c8-17ac664b0c33")
    print()

    config = Config()
    rendi = RendiService(config.RENDI_API_KEY)

    tmp_base = os.path.join(comp_videos_dir, "temp", "xfade_drop_test")
    os.makedirs(tmp_base, exist_ok=True)

    # ── TEST A: Original clips (should reproduce the bug) ──
    test_a_dir = os.path.join(tmp_base, "test_a")
    os.makedirs(test_a_dir, exist_ok=True)
    result_a = run_test(
        rendi, "A — Original clips (expect xfade drop)",
        clips=[CLIP_S1_C0, CLIP_S1_C1, CLIP_S1_C2],
        durations=[2.0, 1.5, 4.4],
        dissolve=DISSOLVE,
        tmp_dir=test_a_dir,
    )

    # ── TEST B: Pre-scale asset clip to 1080x1920 via Rendi, then concat ──
    print(f"\n{'='*60}")
    print("PRE-SCALING s1_c1 to 1080x1920 for Test B...")
    print(f"{'='*60}")
    # Use Rendi to scale the asset clip
    scaled_url = rendi.trim_video(
        video_url=CLIP_S1_C1, duration=1.5, has_audio=False
    )
    # The trim_video doesn't scale, so let's use a raw ffmpeg command
    import requests
    scale_payload = {
        "input_files": {"in_1": CLIP_S1_C1},
        "output_files": {"out_1": "scaled_video.mp4"},
        "ffmpeg_command": (
            f"-i {{{{in_1}}}} -vf \"fps=30,scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1\" -c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
            f"-an -movflags +faststart {{{{out_1}}}}"
        ),
        "max_command_run_seconds": 120,
        "vcpu_count": 4,
    }
    try:
        resp = requests.post(
            f"{rendi.base_url}/v1/run-ffmpeg-command",
            headers=rendi.headers,
            json=scale_payload,
            timeout=60
        )
        resp.raise_for_status()
        cmd_id = resp.json().get("command_id")
        if cmd_id:
            scaled_url = rendi._wait_for_command(cmd_id)
            if scaled_url:
                sw, sh, sfps = probe_resolution(scaled_url)
                print(f"  Scaled clip: {sw}x{sh} @{sfps}")
                print(f"  URL: {scaled_url}")
            else:
                print("  WARN: Scale failed, using original clip")
                scaled_url = CLIP_S1_C1
        else:
            print("  WARN: No command_id, using original clip")
            scaled_url = CLIP_S1_C1
    except Exception as e:
        print(f"  WARN: Scale failed ({e}), using original clip")
        scaled_url = CLIP_S1_C1

    test_b_dir = os.path.join(tmp_base, "test_b")
    os.makedirs(test_b_dir, exist_ok=True)
    result_b = run_test(
        rendi, "B — Pre-scaled asset clip (expect fix)",
        clips=[CLIP_S1_C0, scaled_url, CLIP_S1_C2],
        durations=[2.0, 1.5, 4.4],
        dissolve=DISSOLVE,
        tmp_dir=test_b_dir,
    )

    # ── TEST C: Control — 3 Veo clips only, no asset clip ──
    test_c_dir = os.path.join(tmp_base, "test_c")
    os.makedirs(test_c_dir, exist_ok=True)
    result_c = run_test(
        rendi, "C — Control: 3 Veo clips only (no asset clip)",
        clips=[CLIP_S1_C0, CLIP_S2_C0, CLIP_S1_C2],
        durations=[2.0, 2.0, 4.4],
        dissolve=DISSOLVE,
        tmp_dir=test_c_dir,
    )

    # ── Summary ──
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for label, result in [("A (original)", result_a), ("B (pre-scaled)", result_b), ("C (control)", result_c)]:
        if result:
            status = "RENDERED" if result["clip1_appears"] else "DROPPED" if result["clip1_appears"] is False else "INCONCLUSIVE"
            print(f"  Test {label}: clip_1 {status}")
            print(f"    Output: {result['url']}")
        else:
            print(f"  Test {label}: FAILED (no concat output)")

    print(f"\nIf A=DROPPED, B=RENDERED, C=RENDERED → resolution mismatch causes xfade drop")
    print(f"If A=DROPPED, B=DROPPED, C=RENDERED → issue is specific to video asset clips (not resolution)")
    print(f"If A=RENDERED → bug not reproduced (may be transient)")


if __name__ == "__main__":
    main()
