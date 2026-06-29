#!/usr/bin/env python3
"""
Run only Rendi (concat + audio) and ZapCap for specific sheet rows.
Uses EXISTING assets from the sheet (Scene N - new video, New music, New Voice / VO).
Use when assets are already generated and you only want to re-run the combine + subtitles step.

Usage:
  python run_rendi_zapcap_only.py 2
  python run_rendi_zapcap_only.py 2 3 5
  python run_rendi_zapcap_only.py --rows 2 3 5

Reads from the same Google Sheet and config as video_scene_processor.py.
"""

import argparse
import logging
import os
import sys

# Add parent so we can import from video_scene_processor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from video_scene_processor import (
    config,
    VideoSceneProcessor,
    logger,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def get_column_index_safe(headers: list, name: str, normalize: bool = True):
    """Return 0-based column index or None if not found.
    If normalize=True, match ignoring case and extra spaces (e.g. 'New Voice' matches ' new voice ').
    """
    if not name:
        return None
    target = name.strip().lower() if normalize else name
    for i, h in enumerate(headers):
        if not isinstance(h, str):
            continue
        cand = h.strip().lower() if normalize else h
        if cand == target:
            return i
    # Fallback: exact match as in original
    try:
        return headers.index(name)
    except ValueError:
        return None


def read_assets_from_row(row_data: list, headers: list) -> dict:
    """Read scene videos, music, VO from row using config column names."""
    # Pad so we can index by header position
    if len(row_data) < len(headers):
        row_data = list(row_data) + [""] * (len(headers) - len(row_data))
    out = {
        "scene_videos": [],
        "scene_durations": [],
        "music_url": None,
        "vo_script": None,
        "vo_audio_urls": [],  # per-scene (from New Voice comma-separated)
        "vo_audio_url": None,  # single (if New Voice is one URL)
        "target_duration": 30,
        "add_subtitles": False,
        "subtitle_language": "en",
    }
    n = len(row_data)
    # Scene 1 - new video ... Scene MAX - new video
    for i in range(1, config.MAX_SCENES + 1):
        col_name = config.SCENE_NEW_VIDEO_PREFIX.format(n=i)
        idx = get_column_index_safe(headers, col_name)
        if idx is not None and idx < n and row_data[idx].strip():
            out["scene_videos"].append(row_data[idx].strip())
    if not out["scene_videos"]:
        return out
    # New music
    idx = get_column_index_safe(headers, config.NEW_MUSIC_COLUMN)
    if idx is not None and idx < n and row_data[idx].strip():
        out["music_url"] = row_data[idx].strip()
    # VO script (column "VO")
    idx = get_column_index_safe(headers, config.VO_SCRIPT_COLUMN)
    if idx is not None and idx < n:
        out["vo_script"] = row_data[idx].strip() or None
    # VO is in column "New Voice": comma-separated URLs = per-scene VO; single URL = single VO
    idx = get_column_index_safe(headers, config.NEW_VOICE_COLUMN)
    if idx is not None and idx < n and row_data[idx].strip():
        raw = row_data[idx].strip()
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if len(urls) >= len(out["scene_videos"]):
            out["vo_audio_urls"] = urls[: len(out["scene_videos"])]
        elif len(urls) == 1:
            out["vo_audio_url"] = urls[0]
        else:
            out["vo_audio_urls"] = urls
    # Duration
    idx = get_column_index_safe(headers, config.DURATION_COLUMN)
    if idx is not None and idx < n and row_data[idx].strip():
        try:
            out["target_duration"] = int(float(row_data[idx].strip()))
        except (ValueError, TypeError):
            pass
    out["target_duration"] = max(10, min(40, out["target_duration"]))
    # Add subtitles (e.g. column "Add subtitles" or similar)
    add_col = get_column_index_safe(headers, "Add subtitles")
    if add_col is None:
        add_col = get_column_index_safe(headers, "Subtitles")
    if add_col is not None and add_col < n:
        out["add_subtitles"] = (row_data[add_col].strip().lower() == "yes")
    # Language
    lang_col = get_column_index_safe(headers, "Language")
    if lang_col is not None and lang_col < n and row_data[lang_col].strip():
        out["subtitle_language"] = row_data[lang_col].strip().lower()[:5]
    # Equal scene durations
    num_scenes = len(out["scene_videos"])
    per_scene = out["target_duration"] / num_scenes if num_scenes else 3.0
    out["scene_durations"] = [per_scene] * num_scenes
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Run Rendi + ZapCap only for specified rows (use existing assets from sheet)."
    )
    parser.add_argument(
        "rows",
        type=int,
        nargs="+",
        help="Row numbers (e.g. 2 or 2 3 5)",
    )
    parser.add_argument(
        "--buffer",
        type=float,
        default=0.5,
        help="Buffer seconds between scenes when using per-scene VO (default 0.5)",
    )
    args = parser.parse_args()
    row_numbers = args.rows
    buffer_seconds = args.buffer

    logger.info("RENDI + ZapCap only – rows: %s", row_numbers)
    processor = VideoSceneProcessor()
    sheet_id = config.GOOGLE_SHEET_ID
    tab = config.GOOGLE_SHEET_TAB
    headers, _ = processor.sheets_service.get_worksheet_data(sheet_id, tab)
    if not headers:
        logger.error("Could not read sheet headers")
        sys.exit(1)

    for row_num in row_numbers:
        logger.info("Processing row %s...", row_num)
        row_data = processor.sheets_service.get_row(sheet_id, tab, row_num)
        if not row_data:
            logger.warning("Row %s: no data", row_num)
            continue
        assets = read_assets_from_row(row_data, headers)
        if not assets["scene_videos"]:
            logger.warning("Row %s: no scene videos found in sheet", row_num)
            continue
        vo_urls = assets["vo_audio_urls"] if assets["vo_audio_urls"] else None
        result = processor.run_rendi_zapcap_only(
            row_num=row_num,
            headers=headers,
            scene_videos=assets["scene_videos"],
            scene_durations=assets["scene_durations"],
            music_url=assets["music_url"],
            vo_audio_urls=vo_urls,
            vo_audio_url=assets["vo_audio_url"],
            add_subtitles=assets["add_subtitles"],
            subtitle_language=assets["subtitle_language"],
            buffer_seconds=buffer_seconds,
        )
        if result["success"]:
            logger.info("Row %s: success – %s", row_num, result.get("final_video_url", "")[:60])
        else:
            logger.warning("Row %s: failed – %s", row_num, result.get("errors", []))

    logger.info("Done.")


if __name__ == "__main__":
    main()
