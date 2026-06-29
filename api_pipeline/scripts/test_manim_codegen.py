"""Smoke test for ``api_pipeline.manim_service.generate_manim_code``.

Only exercises the LLM code-gen path — does NOT shell out to the manim CLI,
so it works on any machine with the project's normal Vertex AI / Vercel
credentials, regardless of whether Manim itself is installed.

Run from repo root::

    python -m api_pipeline.scripts.test_manim_codegen
    # or
    python api_pipeline/scripts/test_manim_codegen.py

Prints the generated code, the detected Scene class name, and basic token
metadata. Exits 0 on success, 1 if the LLM call failed.

Set ``MANIM_TEST_DESCRIPTION`` env var to override the default prompt::

    MANIM_TEST_DESCRIPTION="Show the equation A = pi r^2 with the area filling in" \\
        python api_pipeline/scripts/test_manim_codegen.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make repo root importable when invoked as ``python <path>`` rather than ``-m``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env so VERTEX_AI_API_KEY / VERCEL_AI_HUB_API_KEY are available.
try:
    from dotenv import load_dotenv  # type: ignore

    for candidate in (
        _REPO_ROOT / "api_pipeline" / ".env",
        _REPO_ROOT / ".env",
    ):
        if candidate.exists():
            load_dotenv(candidate)
            break
except Exception:
    pass  # dotenv is optional; env may already be set


def main() -> int:
    from api_pipeline.manim_service import generate_manim_code

    description = os.environ.get(
        "MANIM_TEST_DESCRIPTION",
        "Show the Pythagorean theorem: draw a right triangle with sides a, b, c, "
        "then write the equation a squared plus b squared equals c squared.",
    )
    duration = float(os.environ.get("MANIM_TEST_DURATION", "6.0"))

    print("=" * 72)
    print("manim_codegen smoke test")
    print(f"description: {description}")
    print(f"duration_target: {duration}s")
    print("=" * 72)

    result = generate_manim_code(description, duration_target=duration)

    if "error" in result:
        print(f"\nFAILED: {result['error']}")
        return 1

    print(f"\nmodel:           {result.get('model')}")
    print(f"scene_class:     {result.get('scene_class')}")
    print(f"input_tokens:    {result.get('input_tokens')}")
    print(f"output_tokens:   {result.get('output_tokens')}")
    print(f"code length:     {len(result['code'])} chars")
    print("\n--- Generated code ---")
    print(result["code"])
    print("--- End generated code ---")

    # Light sanity checks — informational only, don't fail the script.
    code = result["code"]
    checks = {
        "has 'from manim import'": "from manim import" in code,
        "has 'class ExplainerScene'": "class ExplainerScene" in code,
        "has 'def construct'": "def construct" in code,
        "no markdown fence": "```" not in code,
        "no config mutation": "config.quality" not in code and "config.output_file" not in code,
    }
    print("\n--- Sanity checks ---")
    for label, ok in checks.items():
        mark = "OK " if ok else "WARN"
        print(f"  [{mark}] {label}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
