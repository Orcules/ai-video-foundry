"""Grid cutting helpers for UGC Real storyboard grids."""

from __future__ import annotations

import io
from typing import Dict, List, Tuple

import requests
from PIL import Image


def parse_layout(layout: str) -> Tuple[int, int]:
    raw = (layout or "3x3").strip().lower()
    if "x" not in raw:
        return 3, 3
    cols_s, rows_s = raw.split("x", 1)
    try:
        cols = max(1, int(cols_s))
        rows = max(1, int(rows_s))
        return cols, rows
    except Exception:
        return 3, 3


def cut_grid_image_bytes(image_bytes: bytes, layout: str = "3x3") -> List[Dict]:
    """Split a grid image into per-cell JPEG bytes with metadata."""
    cols, rows = parse_layout(layout)
    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        cell_w = width // cols
        cell_h = height // rows
        cells: List[Dict] = []
        idx = 0
        for r in range(rows):
            for c in range(cols):
                idx += 1
                left = c * cell_w
                top = r * cell_h
                right = width if c == cols - 1 else (c + 1) * cell_w
                bottom = height if r == rows - 1 else (r + 1) * cell_h
                crop = rgb.crop((left, top, right, bottom))
                out = io.BytesIO()
                crop.save(out, format="JPEG", quality=95)
                cells.append(
                    {
                        "cell_index": idx,
                        "bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
                        "image_bytes": out.getvalue(),
                    }
                )
        return cells


def cut_grid_image_url(grid_url: str, layout: str = "3x3", timeout: int = 60) -> List[Dict]:
    """Download grid image by URL and split into cell images."""
    resp = requests.get(grid_url, timeout=timeout)
    resp.raise_for_status()
    return cut_grid_image_bytes(resp.content, layout=layout)


def describe_master_grid_split_for_prompt(layout: str, width: int, height: int) -> str:
    """Text block for LLM / Nano Banana: align the 3x3 with downstream integer crop (no gutters)."""
    cols, rows = parse_layout(layout)
    cell_w = max(1, width // cols)
    cell_h = max(1, height // rows)
    return (
        "MASTER GRID TECHNICAL LAYOUT (mandatory — must match downstream splitting):\n"
        f"- One single flat composite image (no UI chrome, no rounded storyboard frames, no drop shadows between panels).\n"
        f"- Target canvas ~{width}×{height} pixels, portrait {cols}×{rows} ({layout}).\n"
        f"- Equal tiling: each cell is ~{cell_w}px wide × {cell_h}px tall (integer division; any 1–2px remainder is "
        "absorbed by the rightmost column and bottom row only, same as a simple grid crop).\n"
        "- ZERO white gutters, margins, inner borders, gaps, or spacer bands between cells — adjacent panels share edges flush.\n"
        "- No outer letterboxing inside the image that would break alignment with a strict 3×3 split; bleed content to all edges.\n"
        "- Each cell is one distinct shot; keep identity, outfit baseline, and product continuity consistent across cells."
    )

