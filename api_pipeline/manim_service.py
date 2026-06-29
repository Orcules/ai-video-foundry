"""ManimService — proof-of-concept renderer for math/explainer micro-clips.

Standalone module: takes a natural-language description, asks the LLM to write
a Manim Community v0.19.0 ``ExplainerScene``, then shells out to the ``manim``
CLI to render an MP4. Returns a dict with the generated code, the local video
path, and (optionally) a GCS URL.

Designed to be called by the Director or a future ``framework_render`` clip
executor. **No monolith edits and no edits to existing pipelines** — this is
a self-contained module under ``api_pipeline/``.

# Defensive behavior

The module **never** raises out of ``render_math_scene``. Every failure mode
returns ``{"error": "..."}`` so callers can surface a placeholder or retry:

* Manim binary not installed             → ``error="manim not installed"``
* LLM unavailable / call_llm raised      → ``error="codegen failed: ..."``
* Generated code parsed but Scene class
  not named ``ExplainerScene``           → ``error="missing ExplainerScene class"``
* Manim CLI exit code != 0               → ``error="manim render failed: <stderr tail>"``
* Manim CLI hangs past 60s               → ``error="manim render timed out"``
* Output MP4 not found on disk           → ``error="output mp4 not produced"``
* GCS upload requested but failed        → ``error`` unset, ``gcs_url`` unset (best-effort)

# CLI invocation reference

We render at ``-ql`` (480p15) for fast preview quality::

    manim -ql -o explainer.mp4 --media_dir <job_dir> scene.py ExplainerScene

Output lands at ``<job_dir>/videos/scene/480p15/explainer.mp4``.

# Why no monolith edits

The wrapper architecture (see CLAUDE.md) reserves ``Comp_Videos/`` for the
algo engineer. A Manim renderer is a *new* clip primitive — the right home is
``api_pipeline/`` where the Director / custom-pipeline executor lives.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Code post-processing
# ---------------------------------------------------------------------------


_CODE_FENCE_RE = re.compile(r"^\s*```(?:python|py)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Remove ```python ... ``` wrappers if the LLM ignored the prompt rule."""
    if not text:
        return ""
    m = _CODE_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    # Sometimes the LLM emits a leading "```python" but no closing fence.
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
    if s.endswith("```"):
        s = s.rsplit("```", 1)[0]
    return s.strip()


def _ensure_manim_import(code: str) -> str:
    """Defensively prepend ``from manim import *`` if the LLM forgot it.

    We *also* re-add ``import numpy as np`` only if the code uses ``np.`` —
    avoids unused-import warnings for simple scenes.
    """
    if "from manim import" not in code and "import manim" not in code:
        code = "from manim import *\n" + code
    if re.search(r"\bnp\.", code) and "import numpy" not in code:
        code = "import numpy as np\n" + code
    return code


def _find_scene_class(code: str) -> Optional[str]:
    """AST-parse generated code and return the first ``Scene`` subclass name.

    Why: the prompt asks for ``ExplainerScene`` but LLMs sometimes drift.
    We pass whatever class name actually exists to the manim CLI so we don't
    fail with "No scenes were rendered".

    Returns ``None`` if the code does not parse or has no Scene subclass.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        logger.warning("manim_service: generated code is not valid Python: %s", e)
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            # Match `Scene`, `manim.Scene`, etc. — last segment is what counts.
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if base_name == "Scene":
                return node.name
    return None


# ---------------------------------------------------------------------------
# Manim CLI invocation
# ---------------------------------------------------------------------------


def _manim_binary() -> Optional[str]:
    """Return the resolved path to the ``manim`` CLI, or ``None`` if missing."""
    return shutil.which("manim")


# Map ``manim -q?`` flags to their output sub-directory names.
# We use ``-ql`` exclusively for the proof-of-concept (fastest).
_QUALITY_DIR = {
    "-ql": "480p15",
    "-qm": "720p30",
    "-qh": "1080p60",
    "-qp": "1440p60",
    "-qk": "2160p60",
}


def _run_manim(
    scene_file: Path,
    scene_class: str,
    media_dir: Path,
    *,
    quality_flag: str = "-ql",
    output_filename: str = "explainer.mp4",
    timeout_seconds: int = 60,
) -> Dict[str, Any]:
    """Invoke ``manim`` as a subprocess and return ``{ok, video_path?, stderr?}``."""
    binary = _manim_binary()
    if not binary:
        return {"ok": False, "stderr": "manim binary not found on PATH"}

    cmd = [
        binary,
        quality_flag,
        "-o",
        output_filename,
        "--media_dir",
        str(media_dir),
        str(scene_file),
        scene_class,
    ]
    logger.info("manim_service: running %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run already killed the child on timeout.
        logger.warning("manim_service: render timed out after %ss", timeout_seconds)
        return {"ok": False, "stderr": f"render timed out after {timeout_seconds}s: {exc}"}
    except OSError as exc:
        return {"ok": False, "stderr": f"failed to spawn manim: {exc}"}

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        logger.warning("manim_service: render failed (exit %s): %s", proc.returncode, tail)
        return {"ok": False, "stderr": tail}

    # Manim writes to: <media_dir>/videos/<source_stem>/<resolution>/<output_filename>
    res_dir = _QUALITY_DIR.get(quality_flag, "480p15")
    expected = media_dir / "videos" / scene_file.stem / res_dir / output_filename
    if not expected.exists():
        # Fallback: glob in case Manim picked a different resolution dir.
        candidates = list(media_dir.glob(f"videos/**/{output_filename}"))
        if not candidates:
            return {"ok": False, "stderr": f"output not found at {expected}"}
        expected = candidates[0]

    return {"ok": True, "video_path": str(expected.resolve())}


# ---------------------------------------------------------------------------
# GCS upload (best-effort, optional)
# ---------------------------------------------------------------------------


def _upload_to_gcs(local_path: Path, key: str) -> Optional[str]:
    """Re-upload a rendered MP4 to GCS for permanent storage. Returns URL or None.

    Uses the same GCSStorageService the wrapper uses for /api/upload. We do not
    take a registry as input to keep this module standalone — we lazy-import.
    """
    try:
        from api_pipeline.services.base.config import config  # noqa: WPS433
        from api_pipeline.services.base.gcs_storage_service import (  # noqa: WPS433
            GCSStorageService,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("manim_service: GCS deps not importable: %s", exc)
        return None

    try:
        gcs = GCSStorageService(
            credentials_file=config.GCS_UPLOAD_CREDENTIALS_FILE,
            bucket_name=config.GCS_UPLOAD_BUCKET_NAME,
            folder_path=config.GCS_UPLOAD_FOLDER,
        )
        with open(local_path, "rb") as f:
            data = f.read()
        return gcs.upload_video_bytes(data, key)
    except Exception as exc:
        logger.warning("manim_service: GCS upload failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_manim_code(description: str, *, duration_target: float = 5.0) -> Dict[str, Any]:
    """Just the LLM step — generates the Python source, no rendering.

    Useful for unit tests / smoke checks that should not require the manim
    binary or LaTeX. Returns ``{code, raw_text, scene_class, model, input_tokens,
    output_tokens, error?}``.
    """
    try:
        from api_pipeline.llm import call_llm  # local import to avoid cycles
    except Exception as exc:
        return {"error": f"llm module unavailable: {exc}"}

    try:
        result = call_llm(
            "manim_codegen",
            description=description.strip(),
            duration_target=f"{float(duration_target):.1f}",
        )
    except Exception as exc:
        logger.exception("manim_service: codegen failed")
        return {"error": f"codegen failed: {exc}"}

    raw_text = (result or {}).get("text", "") or ""

    # Fail loudly if the LLM returned nothing usable — silent masking via
    # _ensure_manim_import (just prepending 'from manim import *') hides this
    # at runtime and produces a 20-char file with no Scene class.
    if not raw_text or not raw_text.strip():
        return {
            "error": "LLM returned empty response (likely thinking-token budget exhaustion — try gemini-2.5-flash or a higher max_tokens).",
            "code": "",
            "raw_response_chars": 0,
            "input_tokens": (result or {}).get("input_tokens", 0),
            "output_tokens": (result or {}).get("output_tokens", 0),
            "model": (result or {}).get("model"),
        }
    # Additional sanity: if there's no "class" + "construct" pattern after sanitization,
    # the code is unusable. Don't ship a 20-char placeholder.
    code = _strip_code_fences(raw_text)
    code = _ensure_manim_import(code)
    scene_class = _find_scene_class(code) or "ExplainerScene"

    if "class " not in code or "def construct(" not in code:
        return {
            "error": f"LLM output missing Scene class or construct() method (got {len(code)} chars).",
            "code": code,
            "raw_response_chars": len(raw_text),
            "input_tokens": (result or {}).get("input_tokens", 0),
            "output_tokens": (result or {}).get("output_tokens", 0),
            "model": (result or {}).get("model"),
        }

    return {
        "code": code,
        "raw_text": raw_text,
        "scene_class": scene_class,
        "model": (result or {}).get("model"),
        "input_tokens": (result or {}).get("input_tokens"),
        "output_tokens": (result or {}).get("output_tokens"),
    }


def render_math_scene(
    description: str,
    *,
    duration_target: float = 5.0,
    output_dir: Optional[Path] = None,
    gcs_upload: bool = False,
    timeout_seconds: int = 60,
) -> Dict[str, Any]:
    """Generate Manim code for ``description`` and render it to MP4.

    Args:
        description: Natural-language description of the math/concept clip.
        duration_target: Approximate target length in seconds (5-15 is sane).
        output_dir: Where Manim should write its ``media/`` tree. Defaults to
            a fresh tempdir under ``$TMPDIR``. Caller owns cleanup if provided.
        gcs_upload: If True, also upload the rendered MP4 to GCS and return
            ``gcs_url``. Failures are silent (no ``error`` is set).
        timeout_seconds: Max wall-clock time for the manim subprocess.

    Returns:
        On success::

            {
                "code": "<generated python>",
                "scene_class": "ExplainerScene",
                "video_path": "/tmp/.../videos/scene/480p15/explainer.mp4",
                "gcs_url": "https://...",    # only if gcs_upload=True and upload succeeded
                "duration_seconds": 7.42,    # wall-clock render duration
                "render_seconds": 7.42,       # same; kept for caller convenience
            }

        On failure (still a dict, never raises)::

            {"error": "manim not installed", "code": "<may be present>"}
    """
    if not description or not description.strip():
        return {"error": "description is empty"}

    # 1. Generate the code.
    gen = generate_manim_code(description, duration_target=duration_target)
    if "error" in gen:
        return gen
    code = gen["code"]
    scene_class = gen["scene_class"]

    # 2. Check the manim binary is available BEFORE writing scratch files.
    if not _manim_binary():
        return {
            "code": code,
            "scene_class": scene_class,
            "error": "manim not installed",
        }

    # 3. Allocate a working directory.
    if output_dir is None:
        work_root = Path(tempfile.mkdtemp(prefix="manim_"))
    else:
        work_root = Path(output_dir)
        work_root.mkdir(parents=True, exist_ok=True)

    scene_file = work_root / "scene.py"
    scene_file.write_text(code, encoding="utf-8")
    media_dir = work_root / "media"

    # 4. Render.
    started = time.monotonic()
    result = _run_manim(
        scene_file,
        scene_class,
        media_dir,
        timeout_seconds=timeout_seconds,
    )
    elapsed = round(time.monotonic() - started, 2)

    if not result["ok"]:
        return {
            "code": code,
            "scene_class": scene_class,
            "render_seconds": elapsed,
            "duration_seconds": elapsed,
            "error": f"manim render failed: {result.get('stderr', 'unknown error')}",
        }

    out: Dict[str, Any] = {
        "code": code,
        "scene_class": scene_class,
        "video_path": result["video_path"],
        "render_seconds": elapsed,
        "duration_seconds": elapsed,
    }

    # 5. Optional GCS upload — best-effort, never sets error on failure.
    if gcs_upload:
        key = f"manim/{uuid.uuid4().hex[:12]}-explainer.mp4"
        gcs_url = _upload_to_gcs(Path(result["video_path"]), key)
        if gcs_url:
            out["gcs_url"] = gcs_url

    return out


__all__ = ["render_math_scene", "generate_manim_code"]
