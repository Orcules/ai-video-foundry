"""Analyze a pipeline output video to find quality issues.

Usage:
    cd Comp_Videos
    set -a && source .env && set +a
    python -m tvd_pipeline.dev_scripts.analyze_output_video <video_path_or_url> [options]

Examples:
    # Analyze a local file
    python -m tvd_pipeline.dev_scripts.analyze_output_video output.mp4

    # Analyze a GCS URL
    python -m tvd_pipeline.dev_scripts.analyze_output_video https://storage.googleapis.com/automatiq/...mp4

    # Technical-only (no LLM, no API cost)
    python -m tvd_pipeline.dev_scripts.analyze_output_video output.mp4 --technical-only

    # Save extracted frames for manual inspection
    python -m tvd_pipeline.dev_scripts.analyze_output_video output.mp4 --save-frames ./frames

    # Specify pipeline type for context-aware checks
    python -m tvd_pipeline.dev_scripts.analyze_output_video output.mp4 --pipeline influencer
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure Comp_Videos is on the path so tvd_pipeline is importable
_COMP_VIDEOS_DIR = str(Path(__file__).resolve().parents[2])
if _COMP_VIDEOS_DIR not in sys.path:
    sys.path.insert(0, _COMP_VIDEOS_DIR)

from tvd_pipeline.config import Config
from tvd_pipeline.services.ffmpeg_processor import FFmpegProcessor

config = Config()
logger = logging.getLogger("video_analyzer")

# ---------------------------------------------------------------------------
# ANSI colors for terminal output
# ---------------------------------------------------------------------------
class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    DIM = "\033[2m"


def _ok(msg: str) -> str:
    return f"{_C.GREEN}OK{_C.RESET}  {msg}"

def _warn(msg: str) -> str:
    return f"{_C.YELLOW}WARN{_C.RESET}  {msg}"

def _fail(msg: str) -> str:
    return f"{_C.RED}FAIL{_C.RESET}  {msg}"

def _info(msg: str) -> str:
    return f"{_C.CYAN}INFO{_C.RESET}  {msg}"

def _header(msg: str) -> str:
    return f"\n{_C.BOLD}{_C.CYAN}{'=' * 60}\n  {msg}\n{'=' * 60}{_C.RESET}"


# ---------------------------------------------------------------------------
# Technical checks (FFmpeg-based, no LLM cost)
# ---------------------------------------------------------------------------

def get_video_metadata(video_path: str) -> Dict[str, Any]:
    """Extract video metadata via ffprobe."""
    import subprocess
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", video_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"error": result.stderr}
        return json.loads(result.stdout)
    except Exception as e:
        return {"error": str(e)}


def check_technical(video_path: str) -> List[Dict[str, Any]]:
    """Run technical checks on the video file. Returns list of findings."""
    findings = []
    meta = get_video_metadata(video_path)
    if "error" in meta:
        findings.append({"level": "fail", "check": "metadata", "msg": f"Cannot read metadata: {meta['error']}"})
        return findings

    fmt = meta.get("format", {})
    streams = meta.get("streams", [])

    # Duration
    duration = float(fmt.get("duration", 0))
    findings.append({"level": "info", "check": "duration", "msg": f"Duration: {duration:.1f}s"})
    if duration < 3:
        findings.append({"level": "fail", "check": "duration", "msg": "Video is under 3 seconds"})
    elif duration > 120:
        findings.append({"level": "warn", "check": "duration", "msg": "Video is over 120 seconds"})

    # Video stream
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not video_streams:
        findings.append({"level": "fail", "check": "video_stream", "msg": "No video stream found"})
        return findings

    vs = video_streams[0]
    width = int(vs.get("width", 0))
    height = int(vs.get("height", 0))
    fps_str = vs.get("r_frame_rate", "0/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) > 0 else 0
    except (ValueError, ZeroDivisionError):
        fps = 0
    codec = vs.get("codec_name", "unknown")

    findings.append({"level": "info", "check": "resolution", "msg": f"Resolution: {width}x{height}"})
    findings.append({"level": "info", "check": "fps", "msg": f"FPS: {fps:.2f}"})
    findings.append({"level": "info", "check": "codec", "msg": f"Codec: {codec}"})

    # Aspect ratio check
    if width > 0 and height > 0:
        ratio = width / height
        if abs(ratio - 9/16) < 0.05:
            findings.append({"level": "ok", "check": "aspect", "msg": "Aspect ratio: 9:16 (vertical)"})
        elif abs(ratio - 16/9) < 0.05:
            findings.append({"level": "ok", "check": "aspect", "msg": "Aspect ratio: 16:9 (horizontal)"})
        elif abs(ratio - 1.0) < 0.05:
            findings.append({"level": "ok", "check": "aspect", "msg": "Aspect ratio: 1:1 (square)"})
        else:
            findings.append({"level": "warn", "check": "aspect", "msg": f"Non-standard aspect ratio: {ratio:.3f}"})

    # Resolution quality
    min_dim = min(width, height)
    if min_dim < 360:
        findings.append({"level": "fail", "check": "resolution", "msg": f"Very low resolution ({min_dim}p)"})
    elif min_dim < 720:
        findings.append({"level": "warn", "check": "resolution", "msg": f"Sub-HD resolution ({min_dim}p)"})

    # FPS check
    if fps < 20:
        findings.append({"level": "warn", "check": "fps", "msg": f"Low framerate ({fps:.1f} fps)"})
    elif fps > 60:
        findings.append({"level": "warn", "check": "fps", "msg": f"Unusually high framerate ({fps:.1f} fps)"})

    # Audio stream
    if not audio_streams:
        findings.append({"level": "warn", "check": "audio", "msg": "No audio stream — missing VO or music?"})
    else:
        aus = audio_streams[0]
        a_codec = aus.get("codec_name", "unknown")
        a_channels = int(aus.get("channels", 0))
        a_sr = int(aus.get("sample_rate", 0))
        findings.append({"level": "info", "check": "audio", "msg": f"Audio: {a_codec}, {a_channels}ch, {a_sr}Hz"})
        if a_channels == 0:
            findings.append({"level": "warn", "check": "audio", "msg": "Audio stream has 0 channels"})

    # File size
    file_size = int(fmt.get("size", 0))
    size_mb = file_size / (1024 * 1024)
    findings.append({"level": "info", "check": "filesize", "msg": f"File size: {size_mb:.1f} MB"})
    if size_mb < 0.1:
        findings.append({"level": "fail", "check": "filesize", "msg": "Suspiciously small file (<100KB)"})

    # Bitrate
    bitrate = int(fmt.get("bit_rate", 0))
    if bitrate > 0:
        bitrate_kbps = bitrate / 1000
        findings.append({"level": "info", "check": "bitrate", "msg": f"Bitrate: {bitrate_kbps:.0f} kbps"})
        if bitrate_kbps < 500:
            findings.append({"level": "warn", "check": "bitrate", "msg": "Low bitrate — may look blocky"})

    return findings


def check_black_frames(video_path: str, threshold: float = 0.98) -> List[Dict[str, Any]]:
    """Detect black frames (near-black) in the video."""
    import subprocess
    findings = []
    try:
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vf", f"blackdetect=d=0.3:pix_th={threshold}",
            "-an", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = result.stderr
        # Parse blackdetect output
        import re
        blacks = re.findall(
            r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)",
            stderr,
        )
        if blacks:
            for start, end, dur in blacks:
                findings.append({
                    "level": "warn",
                    "check": "black_frames",
                    "msg": f"Black segment: {float(start):.1f}s - {float(end):.1f}s ({float(dur):.1f}s)",
                })
        else:
            findings.append({"level": "ok", "check": "black_frames", "msg": "No black frame segments detected"})
    except Exception as e:
        findings.append({"level": "warn", "check": "black_frames", "msg": f"Could not run black frame check: {e}"})
    return findings


def check_frozen_frames(video_path: str, duration: float) -> List[Dict[str, Any]]:
    """Detect frozen/static frames using freezedetect filter.

    Catches a common pipeline bug: after a scene ends, its last frame
    stays frozen on screen for 1-3 seconds before the next scene starts.
    This happens when video generation produces trailing static frames
    or when concat doesn't trim them.
    """
    import subprocess
    findings = []
    try:
        # Low noise threshold (0.001) to catch even near-identical frames.
        # Duration 0.8s — anything frozen longer than 0.8s is suspicious in
        # a pipeline that generates 3-8s clips with constant motion.
        cmd = [
            "ffmpeg", "-i", video_path,
            "-vf", "freezedetect=n=0.001:d=0.8",
            "-an", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = result.stderr
        import re
        freezes = re.findall(
            r"freeze_start:\s*([\d.]+).*?freeze_duration:\s*([\d.]+)",
            stderr, re.DOTALL,
        )
        if freezes:
            for start, dur in freezes:
                fs, fd = float(start), float(dur)
                # Freeze at the very end (<1s from end) is checked separately
                # by check_abrupt_ending. Skip it here to avoid double-reporting.
                if fs + fd >= duration - 0.3:
                    continue
                level = "fail" if fd > 2.0 else "warn"
                findings.append({
                    "level": level,
                    "check": "frozen_frames",
                    "msg": (
                        f"Frozen at {fs:.1f}s for {fd:.1f}s — "
                        f"last frame of previous scene likely stuck on screen"
                    ),
                })
        if not any(f["check"] == "frozen_frames" for f in findings):
            findings.append({"level": "ok", "check": "frozen_frames", "msg": "No frozen segments detected"})
    except Exception as e:
        findings.append({"level": "warn", "check": "frozen_frames", "msg": f"Could not run freeze check: {e}"})
    return findings


def check_audio_levels(video_path: str) -> List[Dict[str, Any]]:
    """Check audio volume levels (silence, clipping)."""
    import subprocess
    findings = []
    try:
        # Detect silence
        cmd = [
            "ffmpeg", "-i", video_path,
            "-af", "silencedetect=noise=-40dB:d=2",
            "-vn", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        stderr = result.stderr
        import re
        silences = re.findall(
            r"silence_start:\s*([\d.]+).*?silence_end:\s*([\d.]+).*?silence_duration:\s*([\d.]+)",
            stderr, re.DOTALL,
        )
        if silences:
            for start, end, dur in silences:
                level = "warn" if float(dur) > 3 else "info"
                findings.append({
                    "level": level,
                    "check": "audio_silence",
                    "msg": f"Silence: {float(start):.1f}s - {float(end):.1f}s ({float(dur):.1f}s)",
                })
        else:
            findings.append({"level": "ok", "check": "audio_silence", "msg": "No long silence gaps detected"})

        # Check volume stats
        cmd2 = [
            "ffmpeg", "-i", video_path,
            "-af", "volumedetect",
            "-vn", "-f", "null", "-"
        ]
        result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
        stderr2 = result2.stderr
        mean_vol = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", stderr2)
        max_vol = re.search(r"max_volume:\s*([-\d.]+)\s*dB", stderr2)
        if mean_vol:
            mean_db = float(mean_vol.group(1))
            findings.append({"level": "info", "check": "audio_volume", "msg": f"Mean volume: {mean_db:.1f} dB"})
            if mean_db < -35:
                findings.append({"level": "warn", "check": "audio_volume", "msg": "Audio is very quiet"})
            elif mean_db > -5:
                findings.append({"level": "warn", "check": "audio_volume", "msg": "Audio may be clipping"})
        if max_vol:
            max_db = float(max_vol.group(1))
            findings.append({"level": "info", "check": "audio_volume", "msg": f"Max volume: {max_db:.1f} dB"})
            if max_db >= 0:
                findings.append({"level": "fail", "check": "audio_volume", "msg": "Audio is clipping (max >= 0 dB)"})

    except Exception as e:
        findings.append({"level": "warn", "check": "audio_levels", "msg": f"Could not check audio levels: {e}"})
    return findings


def check_abrupt_ending(video_path: str, duration: float) -> List[Dict[str, Any]]:
    """Detect abrupt video ending: audio cut off or no tail buffer.

    Common pipeline issue: the VO's last word gets cut mid-syllable because
    the video duration exactly matches (or is shorter than) the audio, leaving
    zero breathing room at the end. A good video needs at least ~0.5s of
    tail after the last spoken word.
    """
    import subprocess, re
    findings = []

    # --- 1. Check if audio extends to the very end (no tail silence) ---
    try:
        cmd = [
            "ffmpeg", "-i", video_path,
            "-af", "silencedetect=noise=-35dB:d=0.3",
            "-vn", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        stderr = result.stderr

        # Find the last silence_start — that's where audio effectively ends
        silence_starts = re.findall(r"silence_start:\s*([\d.]+)", stderr)
        silence_pairs = re.findall(
            r"silence_start:\s*([\d.]+).*?silence_end:\s*([\d.]+)",
            stderr, re.DOTALL,
        )

        # Check if audio is still playing right at the video end
        # If no silence detected in the last 0.5s, the audio runs to the edge
        last_silence_before_end = None
        for s_start, s_end in silence_pairs:
            if float(s_end) >= duration - 0.1:
                last_silence_before_end = float(s_start)

        if last_silence_before_end is not None:
            tail_buffer = duration - last_silence_before_end
            if tail_buffer < 0.3:
                # Audio goes silent right at the end — check if that silence
                # is long enough to feel natural
                pass  # borderline OK
            else:
                findings.append({
                    "level": "ok",
                    "check": "ending_buffer",
                    "msg": f"Audio ends ~{tail_buffer:.1f}s before video ends",
                })
        else:
            # No silence detected near the end — audio likely plays to the edge
            findings.append({
                "level": "warn",
                "check": "ending_buffer",
                "msg": (
                    "No silence detected in the final 0.5s — "
                    "voiceover may be cut off at the end"
                ),
            })
    except Exception as e:
        findings.append({"level": "warn", "check": "ending_buffer", "msg": f"Could not check ending buffer: {e}"})

    # --- 2. Check if the last ~1s of video is frozen (common: last frame held) ---
    try:
        # Extract the final 1.5s and check for freeze
        tail_start = max(0, duration - 1.5)
        cmd = [
            "ffmpeg", "-ss", str(tail_start), "-i", video_path,
            "-vf", "freezedetect=n=0.001:d=0.5",
            "-an", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        freezes = re.findall(r"freeze_duration:\s*([\d.]+)", result.stderr)
        if freezes:
            freeze_dur = max(float(d) for d in freezes)
            if freeze_dur > 0.5:
                findings.append({
                    "level": "warn",
                    "check": "ending_freeze",
                    "msg": (
                        f"Last {freeze_dur:.1f}s of video is frozen — "
                        f"video may end on a static frame instead of natural motion"
                    ),
                })
    except Exception:
        pass

    # --- 3. Check if video duration vs audio duration mismatch ---
    try:
        # Get audio stream duration separately
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            audio_dur = float(result.stdout.strip())
            diff = audio_dur - duration
            if diff > 0.3:
                findings.append({
                    "level": "fail",
                    "check": "audio_overflow",
                    "msg": (
                        f"Audio ({audio_dur:.1f}s) is {diff:.1f}s longer than video ({duration:.1f}s) — "
                        f"VO ending is definitely cut off"
                    ),
                })
            elif diff > 0:
                findings.append({
                    "level": "warn",
                    "check": "audio_overflow",
                    "msg": f"Audio ({audio_dur:.1f}s) slightly longer than video ({duration:.1f}s) by {diff:.2f}s",
                })
            elif abs(diff) < 0.1:
                findings.append({
                    "level": "warn",
                    "check": "audio_tight",
                    "msg": (
                        f"Audio and video have nearly identical duration ({duration:.1f}s) — "
                        f"no tail buffer, ending may feel abrupt"
                    ),
                })
    except Exception:
        pass

    if not findings:
        findings.append({"level": "ok", "check": "ending", "msg": "Video ending looks clean"})

    return findings


def check_scene_transitions(video_path: str, duration: float) -> List[Dict[str, Any]]:
    """Detect scene cuts and check transition quality."""
    findings = []
    try:
        timestamps = FFmpegProcessor.detect_scenes(
            video_path, threshold=27.0, min_scene_duration=0.5, use_adaptive=True,
        )
        num_scenes = len(timestamps)
        findings.append({"level": "info", "check": "scenes", "msg": f"Detected {num_scenes} scene(s)"})

        if num_scenes <= 1:
            findings.append({"level": "warn", "check": "scenes", "msg": "Only 1 scene detected — expected multiple"})
        elif num_scenes > 15:
            findings.append({"level": "warn", "check": "scenes", "msg": f"Many scenes ({num_scenes}) — possibly flickering"})

        # Check scene durations
        for i, ts in enumerate(timestamps):
            end = timestamps[i + 1] if i + 1 < len(timestamps) else duration
            scene_dur = end - ts
            if scene_dur < 0.5:
                findings.append({
                    "level": "warn",
                    "check": "scene_duration",
                    "msg": f"Scene {i+1} very short: {scene_dur:.1f}s at {ts:.1f}s",
                })
            elif scene_dur > 30:
                findings.append({
                    "level": "info",
                    "check": "scene_duration",
                    "msg": f"Scene {i+1} is long: {scene_dur:.1f}s at {ts:.1f}s",
                })

        # Report scene list
        scene_info = []
        for i, ts in enumerate(timestamps):
            end = timestamps[i + 1] if i + 1 < len(timestamps) else duration
            scene_info.append(f"S{i+1}: {ts:.1f}-{end:.1f}s ({end-ts:.1f}s)")
        findings.append({"level": "info", "check": "scene_list", "msg": " | ".join(scene_info)})

    except Exception as e:
        findings.append({"level": "warn", "check": "scenes", "msg": f"Scene detection failed: {e}"})
    return findings


# ---------------------------------------------------------------------------
# LLM-based visual quality analysis via Gemini
# ---------------------------------------------------------------------------

OUTPUT_VIDEO_ANALYSIS_PROMPT = """You are a video quality analyst reviewing the FINAL OUTPUT of an automated video generation pipeline. Your job is to find **production issues** — things that would make this video look unprofessional or broken.

Watch the ENTIRE video carefully from start to end. Pay close attention to the issues described below.

⚠️⚠️⚠️ HIGH-PRIORITY CHECKS — THESE ARE THE MOST COMMON PIPELINE BUGS ⚠️⚠️⚠️

1. **FROZEN/STUCK FRAMES BETWEEN SCENES (critical)**
   This pipeline generates individual ~3-8 second video clips and concatenates them. A very common bug is:
   the LAST FRAME of one clip gets stuck/frozen on screen for 1-3 seconds before the next clip starts.
   - Watch every scene transition carefully: does the motion STOP and a single frame FREEZE before the next scene begins?
   - This is NOT the same as a slow dissolve. A dissolve blends two moving images. A freeze is one static image held.
   - If you see motion → sudden freeze on a single frame → then new scene starts: that's a "stuck_last_frame" bug.
   - Report the EXACT timestamp range where the frame is frozen (e.g., "1.2s - 3.0s frozen on last frame of scene 1").

2. **ABRUPT ENDING / CUT-OFF AUDIO (critical)**
   Another very common bug: the video ends too abruptly.
   - Does the LAST WORD of the voiceover get cut off mid-syllable? (e.g., "Pra—" instead of "Prague")
   - Does the video end the instant the last word finishes, with zero breathing room? A good ending needs at least 0.5-1s of space after the last spoken word.
   - Does the music get cut mid-note at the very end?
   - Does the video feel like it was chopped — no natural fade-out, no lingering on the final frame?
   - Report as "abrupt_ending" with details on what exactly gets cut.

3. **SUBTITLE CUT-OFF**
   Related to abrupt ending: if subtitles are present, does the LAST subtitle word get cut or disappear before it can be read?

⚠️⚠️⚠️ END OF HIGH-PRIORITY CHECKS ⚠️⚠️⚠️

Analyze the video and return a JSON object with these sections:

{{
  "overall_quality": "good" | "acceptable" | "poor",
  "overall_score": <1-10>,
  "summary": "<2-3 sentence summary of the video and its quality>",

  "issues": [
    {{
      "severity": "critical" | "major" | "minor",
      "category": "<category>",
      "timestamp": "<approximate timestamp or 'throughout'>",
      "description": "<what's wrong — be very specific>",
      "suggestion": "<how to fix>"
    }}
  ],

  "scene_analysis": [
    {{
      "scene_number": <int>,
      "timestamp_range": "<start>-<end>",
      "quality": "good" | "acceptable" | "poor",
      "has_frozen_ending": true|false,
      "notes": "<brief notes on this scene>"
    }}
  ],

  "checks": {{
    "frozen_between_scenes": {{"pass": true|false, "notes": "<for EACH scene transition: does the last frame freeze before the next scene? give timestamps>"}},
    "ending_quality": {{"pass": true|false, "notes": "<does the video end naturally? is the last word complete? is there tail silence? does music fade out?>"}},
    "character_consistency": {{"pass": true|false, "notes": "<do characters look consistent across scenes?>"}},
    "style_consistency": {{"pass": true|false, "notes": "<is the visual style consistent throughout?>"}},
    "transition_quality": {{"pass": true|false, "notes": "<are transitions smooth? any hard cuts or glitches?>"}},
    "text_legibility": {{"pass": true|false, "notes": "<is any on-screen text readable and properly positioned?>"}},
    "subtitle_quality": {{"pass": true|false, "notes": "<if subtitles present: timing, positioning, readability. is the last subtitle word fully shown?>"}},
    "audio_visual_sync": {{"pass": true|false, "notes": "<does audio match what's shown?>"}},
    "color_grading": {{"pass": true|false, "notes": "<is color consistent? any washed out or oversaturated segments?>"}},
    "motion_quality": {{"pass": true|false, "notes": "<is movement natural? any AI artifacts, warping, morphing?>"}},
    "composition": {{"pass": true|false, "notes": "<framing, centering, visual balance>"}},
    "branding_clean": {{"pass": true|false, "notes": "<no unwanted logos, watermarks, or text on products>"}}
  }}
}}

ISSUE CATEGORIES to check:
- "stuck_last_frame": The last frame of a scene clip freezes on screen before the next scene — THIS IS THE #1 BUG
- "abrupt_ending": Video/audio/subtitle cuts off too abruptly at the very end — THIS IS THE #2 BUG
- "ai_artifacts": Morphing, warping, extra fingers, melting faces, unnatural motion
- "transition_glitch": Hard cuts, flash frames, incomplete dissolves
- "character_inconsistency": Character appearance changes between scenes
- "style_mismatch": Sudden style/lighting/color shifts between scenes
- "frozen_frame": Static/frozen video segment (not at scene boundary — mid-scene freeze)
- "black_frame": Unexpected black segments
- "audio_issue": Missing audio, volume jumps, echo, clipping
- "text_issue": Unreadable text, bad positioning, spelling errors, branding on products
- "subtitle_issue": Subtitle timing off, overlapping, cut off, last word not fully displayed
- "composition_issue": Bad framing, subject cut off, poor centering
- "resolution_issue": Blurry, pixelated, upscaled artifacts
- "pacing_issue": Scene too long/short, rhythm feels off
- "watermark": Unwanted watermark or logo visible

{pipeline_context}

Return valid JSON only. Be specific about timestamps and locations of issues."""


def analyze_visual_quality(
    video_path: str,
    pipeline_type: str = "",
    original_url: str = "",
) -> Dict[str, Any]:
    """Analyze video visual quality using Gemini via Vertex AI.

    If the video is already on GCS (original_url starts with
    https://storage.googleapis.com/), it is used directly without
    re-uploading.  Otherwise the local file is uploaded to GCS first.
    """
    from tvd_pipeline.services.providers.vertex import VertexAIProvider

    # Check if the original URL is already on GCS — skip upload entirely
    already_on_gcs = original_url.startswith("https://storage.googleapis.com/")

    # Init provider (needs VERTEX_AI_API_KEY or gcloud ADC)
    gcs = None
    if not already_on_gcs:
        try:
            from tvd_pipeline.services.gcs_storage import GCSStorageService
            # Find service_account.json: check Comp_Videos/ first, then api_pipeline/
            sa_candidates = [
                os.path.join(_COMP_VIDEOS_DIR, "service_account.json"),
                os.path.join(_COMP_VIDEOS_DIR, "..", "api_pipeline", "service_account.json"),
            ]
            sa_path = next((p for p in sa_candidates if os.path.exists(p)), None)
            if sa_path:
                gcs = GCSStorageService(
                    credentials_file=sa_path,
                    bucket_name="automatiq",
                    folder_path="analyzer_tmp",
                )
            else:
                logger.warning("service_account.json not found — GCS upload unavailable for local files")
        except Exception as e:
            logger.warning(f"Could not init GCS: {e}")

    provider = VertexAIProvider(gcs_storage_service=gcs)
    if not provider.initialized:
        return {"error": "Vertex AI provider not initialized. Set VERTEX_AI_API_KEY or run gcloud auth."}

    video_url = None  # tracks whether we uploaded (so we can clean up)
    try:
        if already_on_gcs:
            # Convert https URL to gs:// URI directly
            gs_uri = "gs://" + original_url.split("storage.googleapis.com/", 1)[1]
            logger.info(f"Video already on GCS, using directly: {gs_uri}")
        else:
            # Upload local file to GCS
            video_url = provider._upload_video_to_gcs(video_path)
            if not video_url:
                return {"error": "Could not upload video to GCS. Provide a GCS URL or set up service_account.json."}
            if "storage.googleapis.com/" in video_url:
                gs_uri = "gs://" + video_url.split("storage.googleapis.com/", 1)[1]
            else:
                gs_uri = video_url

        # Pipeline-specific context
        pipeline_context = ""
        if pipeline_type:
            contexts = {
                "influencer": (
                    "PIPELINE CONTEXT: This is an INFLUENCER video.\n"
                    "Expected: First-person UGC/Instagram style, consistent influencer character throughout, "
                    "dissolve transitions (0.075s), asset clips interleaved, CTA at end with logo+slogan, "
                    "expressive voiceover, subtitles burned in.\n"
                    "Common issues: Character face changes between AI-generated scenes, "
                    "asset clips look different from generated scenes, dissolves not smooth."
                ),
                "product": (
                    "PIPELINE CONTEXT: This is a PRODUCT VIDEO.\n"
                    "Expected: Third-person VO, product shown in multiple scenes, "
                    "clean product images, professional style, subtitles.\n"
                    "Common issues: Product appearance inconsistency, text on products, "
                    "generated images not matching product reference."
                ),
                "personal-brand": (
                    "PIPELINE CONTEXT: This is a PERSONAL BRAND video.\n"
                    "Expected: VO-first flow, character descriptions, professional self-promotion, "
                    "dissolve transitions, beat-synced music.\n"
                    "Common issues: Character inconsistency, VO timing mismatch, "
                    "scene transitions not on beat."
                ),
            }
            pipeline_context = contexts.get(pipeline_type, "")

        prompt = OUTPUT_VIDEO_ANALYSIS_PROMPT.format(pipeline_context=pipeline_context)

        # Build Vertex AI payload
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"fileData": {"mimeType": "video/mp4", "fileUri": gs_uri}},
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 8192,
            },
        }

        model = getattr(config, "VERTEX_AI_MODEL", "gemini-2.5-flash")
        result = provider.raw_generate_content(payload, model=model)
        text = result.get("text", "")

        # Report cost
        in_tok = result.get("input_tokens", 0)
        out_tok = result.get("output_tokens", 0)
        logger.info(f"LLM analysis cost: {in_tok} input + {out_tok} output tokens")

        if not text:
            return {"error": "Gemini returned empty response"}

        # Clean markdown wrapping
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]

        return json.loads(text.strip())

    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse LLM response: {e}", "raw": text[:500] if text else ""}
    except Exception as e:
        return {"error": f"Visual analysis failed: {e}"}
    finally:
        if video_url:
            try:
                provider._cleanup_gcs_video(video_url)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Frame extraction for manual inspection
# ---------------------------------------------------------------------------

def extract_sample_frames(
    video_path: str, output_dir: str, duration: float, fps: int = 1,
) -> List[str]:
    """Extract frames for manual inspection."""
    os.makedirs(output_dir, exist_ok=True)
    frames = FFmpegProcessor.extract_frames_entire_video(
        video_path, duration, output_dir, fps=fps,
    )
    paths = [f[1] for f in frames]
    return paths


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def download_if_url(path_or_url: str) -> Tuple[str, bool, str]:
    """If input is a URL, download to temp file.

    Returns (local_path, is_temp, original_url).
    ``original_url`` is the input string when it was a URL, else empty.
    """
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        import requests
        logger.info(f"Downloading video from URL...")
        resp = requests.get(path_or_url, stream=True, timeout=120)
        resp.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
        tmp.close()
        size_mb = os.path.getsize(tmp.name) / (1024 * 1024)
        logger.info(f"Downloaded {size_mb:.1f} MB to {tmp.name}")
        return tmp.name, True, path_or_url
    return path_or_url, False, ""


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(
    technical_findings: List[Dict],
    llm_analysis: Optional[Dict] = None,
    frame_paths: Optional[List[str]] = None,
) -> None:
    """Print formatted analysis report."""

    # --- Technical findings ---
    print(_header("TECHNICAL ANALYSIS"))

    fails = [f for f in technical_findings if f["level"] == "fail"]
    warns = [f for f in technical_findings if f["level"] == "warn"]
    oks = [f for f in technical_findings if f["level"] == "ok"]
    infos = [f for f in technical_findings if f["level"] == "info"]

    for f in infos:
        print(f"  {_info(f['msg'])}")

    for f in oks:
        print(f"  {_ok(f['msg'])}")

    for f in warns:
        print(f"  {_warn(f['msg'])}")

    for f in fails:
        print(f"  {_fail(f['msg'])}")

    # Summary line
    print(f"\n  {_C.BOLD}Technical: {len(fails)} critical, {len(warns)} warnings, {len(oks)} passed{_C.RESET}")

    # --- LLM visual analysis ---
    if llm_analysis:
        if "error" in llm_analysis:
            print(_header("VISUAL ANALYSIS (LLM)"))
            print(f"  {_fail(llm_analysis['error'])}")
            return

        print(_header("VISUAL ANALYSIS (LLM)"))

        score = llm_analysis.get("overall_score", "?")
        quality = llm_analysis.get("overall_quality", "unknown")
        summary = llm_analysis.get("summary", "")

        color = _C.GREEN if quality == "good" else (_C.YELLOW if quality == "acceptable" else _C.RED)
        print(f"  {_C.BOLD}Score: {color}{score}/10{_C.RESET}  Quality: {color}{quality}{_C.RESET}")
        print(f"  {_C.DIM}{summary}{_C.RESET}")

        # Issues
        issues = llm_analysis.get("issues", [])
        if issues:
            print(f"\n  {_C.BOLD}Issues ({len(issues)}):{_C.RESET}")
            for issue in issues:
                sev = issue.get("severity", "minor")
                sev_color = _C.RED if sev == "critical" else (_C.YELLOW if sev == "major" else _C.DIM)
                cat = issue.get("category", "")
                ts = issue.get("timestamp", "")
                desc = issue.get("description", "")
                sugg = issue.get("suggestion", "")
                print(f"    {sev_color}[{sev.upper()}]{_C.RESET} {_C.BOLD}{cat}{_C.RESET} @ {ts}")
                print(f"      {desc}")
                if sugg:
                    print(f"      {_C.DIM}Fix: {sugg}{_C.RESET}")
        else:
            print(f"\n  {_ok('No issues found')}")

        # Quality checks
        checks = llm_analysis.get("checks", {})
        if checks:
            print(f"\n  {_C.BOLD}Quality Checks:{_C.RESET}")
            for check_name, check_data in checks.items():
                passed = check_data.get("pass", True)
                notes = check_data.get("notes", "")
                label = check_name.replace("_", " ").title()
                if passed:
                    print(f"    {_ok(f'{label}: {notes}')}")
                else:
                    print(f"    {_fail(f'{label}: {notes}')}")

        # Scene analysis
        scenes = llm_analysis.get("scene_analysis", [])
        if scenes:
            print(f"\n  {_C.BOLD}Scene Breakdown:{_C.RESET}")
            for scene in scenes:
                sq = scene.get("quality", "?")
                sq_color = _C.GREEN if sq == "good" else (_C.YELLOW if sq == "acceptable" else _C.RED)
                sn = scene.get("scene_number", "?")
                sr = scene.get("timestamp_range", "?")
                sn_notes = scene.get("notes", "")
                print(f"    Scene {sn} ({sr}): {sq_color}{sq}{_C.RESET} — {sn_notes}")

    # --- Frame paths ---
    if frame_paths:
        print(f"\n  {_C.BOLD}Extracted {len(frame_paths)} frames to:{_C.RESET}")
        print(f"    {os.path.dirname(frame_paths[0])}")

    # --- Final summary ---
    print(_header("SUMMARY"))
    total_issues = len([f for f in technical_findings if f["level"] in ("fail", "warn")])
    if llm_analysis and "issues" in llm_analysis:
        total_issues += len(llm_analysis["issues"])

    if total_issues == 0:
        print(f"  {_C.GREEN}{_C.BOLD}No issues found. Video looks clean.{_C.RESET}")
    else:
        print(f"  {_C.YELLOW}{_C.BOLD}Found {total_issues} total issue(s) to review.{_C.RESET}")


# ---------------------------------------------------------------------------
# Markdown report writer
# ---------------------------------------------------------------------------

_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")


def write_md_report(
    video_name: str,
    video_source: str,
    pipeline_type: str,
    technical_findings: List[Dict],
    llm_analysis: Optional[Dict] = None,
    output_path: str = "",
) -> str:
    """Write analysis results to a markdown file and return the path."""
    import datetime

    os.makedirs(_REPORTS_DIR, exist_ok=True)

    if not output_path:
        stem = Path(video_name).stem
        output_path = os.path.join(_REPORTS_DIR, f"{stem}.md")

    lines = [f"# Video Analysis Report\n"]
    lines.append(f"**Video:** `{video_name}`")
    if video_source:
        lines.append(f"**URL:** `{video_source}`")
    if pipeline_type:
        lines.append(f"**Pipeline:** {pipeline_type}")
    lines.append(f"**Date:** {datetime.date.today().isoformat()}")

    # Score from LLM
    if llm_analysis and "overall_score" in llm_analysis:
        score = llm_analysis["overall_score"]
        quality = llm_analysis.get("overall_quality", "unknown")
        lines.append(f"**Score:** {score}/10 ({quality})")

    # Summary
    if llm_analysis and llm_analysis.get("summary"):
        lines.append(f"\n## Summary\n")
        lines.append(llm_analysis["summary"])

    # Issues table
    if llm_analysis and llm_analysis.get("issues"):
        issues = llm_analysis["issues"]
        lines.append(f"\n## Issues\n")
        lines.append("| # | Severity | Category | Timestamp | Description |")
        lines.append("|---|----------|----------|-----------|-------------|")
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "minor").upper()
            sev_fmt = f"**{sev}**" if sev == "CRITICAL" else sev
            cat = issue.get("category", "")
            ts = issue.get("timestamp", "")
            desc = issue.get("description", "").replace("\n", " ")
            lines.append(f"| {i} | {sev_fmt} | {cat} | {ts} | {desc} |")

        # Suggestions
        suggestions = [(iss.get("category", ""), iss.get("suggestion", "")) for iss in issues if iss.get("suggestion")]
        if suggestions:
            lines.append(f"\n### Suggested Fixes\n")
            for cat, sugg in suggestions:
                lines.append(f"- **{cat}**: {sugg}")

    # Technical findings table
    lines.append(f"\n## Technical Findings\n")
    lines.append("| Check | Result | Detail |")
    lines.append("|-------|--------|--------|")
    for f in technical_findings:
        level = f["level"].upper()
        if level in ("FAIL", "WARN"):
            level = f"**{level}**"
        lines.append(f"| {f['check']} | {level} | {f['msg']} |")

    # Quality checks
    if llm_analysis and llm_analysis.get("checks"):
        checks = llm_analysis["checks"]
        lines.append(f"\n## Quality Checks (LLM)\n")
        lines.append("| Check | Pass | Notes |")
        lines.append("|-------|------|-------|")
        for name, data in checks.items():
            passed = data.get("pass", True)
            label = name.replace("_", " ").title()
            status = "OK" if passed else "**FAIL**"
            notes = data.get("notes", "").replace("\n", " ")
            lines.append(f"| {label} | {status} | {notes} |")

    # Scene breakdown
    if llm_analysis and llm_analysis.get("scene_analysis"):
        scenes = llm_analysis["scene_analysis"]
        lines.append(f"\n## Scene Breakdown\n")
        lines.append("| Scene | Time | Quality | Notes |")
        lines.append("|-------|------|---------|-------|")
        for scene in scenes:
            sn = scene.get("scene_number", "?")
            sr = scene.get("timestamp_range", "?")
            sq = scene.get("quality", "?")
            sq_fmt = f"**{sq}**" if sq == "poor" else sq
            notes = scene.get("notes", "").replace("\n", " ")
            lines.append(f"| {sn} | {sr} | {sq_fmt} | {notes} |")

    lines.append("")  # trailing newline
    content = "\n".join(lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze pipeline output video for quality issues",
    )
    parser.add_argument("video", help="Local file path or URL to the output video")
    parser.add_argument(
        "--technical-only", action="store_true",
        help="Run only technical checks (no LLM, no API cost)",
    )
    parser.add_argument(
        "--pipeline", choices=["influencer", "product", "personal-brand"],
        default="", help="Pipeline type for context-aware checks",
    )
    parser.add_argument(
        "--save-frames", metavar="DIR",
        help="Extract and save frames to this directory for manual inspection",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON instead of formatted report",
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip writing the markdown report file (report is written by default)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging",
    )

    args = parser.parse_args()

    # Logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Check FFmpeg
    if not FFmpegProcessor.check_ffmpeg_installed():
        print(f"{_C.RED}FFmpeg is not installed. Technical checks require FFmpeg.{_C.RESET}")
        sys.exit(1)

    # Download if URL
    video_path, is_temp, original_url = download_if_url(args.video)
    try:
        if not os.path.exists(video_path):
            print(f"{_C.RED}File not found: {video_path}{_C.RESET}")
            sys.exit(1)

        # Get duration for downstream checks
        duration = FFmpegProcessor.get_video_duration(video_path)

        # 1. Technical checks
        all_findings = []
        all_findings.extend(check_technical(video_path))
        all_findings.extend(check_black_frames(video_path))
        all_findings.extend(check_frozen_frames(video_path, duration))
        all_findings.extend(check_audio_levels(video_path))
        all_findings.extend(check_abrupt_ending(video_path, duration))
        all_findings.extend(check_scene_transitions(video_path, duration))

        # 2. LLM visual analysis (unless --technical-only)
        llm_result = None
        if not args.technical_only:
            print(f"\n{_C.DIM}Running LLM visual analysis (Gemini)...{_C.RESET}")
            llm_result = analyze_visual_quality(
                video_path, pipeline_type=args.pipeline, original_url=original_url,
            )

        # 3. Save frames if requested
        frame_paths = None
        if args.save_frames:
            print(f"\n{_C.DIM}Extracting frames...{_C.RESET}")
            frame_paths = extract_sample_frames(video_path, args.save_frames, duration)

        # Output
        if args.json:
            output = {
                "technical": all_findings,
                "visual_analysis": llm_result,
                "frames_saved": len(frame_paths) if frame_paths else 0,
            }
            print(json.dumps(output, indent=2, ensure_ascii=False))
        else:
            print_report(all_findings, llm_result, frame_paths)

        # Write markdown report (default on, skip with --no-report)
        if not args.no_report:
            video_name = os.path.basename(original_url) if original_url else os.path.basename(args.video)
            report_path = write_md_report(
                video_name=video_name,
                video_source=original_url,
                pipeline_type=args.pipeline,
                technical_findings=all_findings,
                llm_analysis=llm_result,
            )
            print(f"\n  {_C.BOLD}Report saved:{_C.RESET} {report_path}")

    finally:
        # Cleanup temp download
        if is_temp and os.path.exists(video_path):
            os.unlink(video_path)


if __name__ == "__main__":
    main()
