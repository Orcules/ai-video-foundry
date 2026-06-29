"""Lightweight LLM caller for API server validation gates.

Uses Vertex AI (Gemini) with Vercel AI Hub as optional fallback.
Config: api_pipeline/config/llm.json
Prompts: api_pipeline/config/prompts/
"""

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
_PROMPTS_DIR = os.path.join(_CONFIG_DIR, "prompts")

# Lazy singletons (reset on module reload to pick up llm.json edits in dev)
_llm_config = None
_vertex_provider = None
_vercel_provider = None



def _get_config() -> dict:
    global _llm_config
    if _llm_config is None:
        with open(os.path.join(_CONFIG_DIR, "llm.json")) as f:
            _llm_config = json.load(f)
    return _llm_config


def _get_vertex_provider():
    """Lazy-init Vertex AI provider singleton.

    VertexAIProvider reads its own config (VERTEX_AI_API_KEY, model, project,
    location) from tvd_pipeline.config.Config(). No constructor kwargs needed.
    """
    global _vertex_provider
    if _vertex_provider is None:
        try:
            from tvd_pipeline.services.providers.vertex import VertexAIProvider
            p = VertexAIProvider()
            _vertex_provider = p if p.initialized else False
        except Exception as e:
            logger.warning("Vertex provider init failed: %s", e)
            _vertex_provider = False
    return _vertex_provider if _vertex_provider else None


def _get_vercel_provider():
    """Lazy-init Vercel provider singleton (optional)."""
    global _vercel_provider
    if _vercel_provider is None:
        try:
            from tvd_pipeline.services.providers.vercel import VercelProvider
            p = VercelProvider()
            _vercel_provider = p if p.initialized else False
        except Exception:
            _vercel_provider = False
    return _vercel_provider if _vercel_provider else None


def _load_prompt(name: str, _depth: int = 0) -> str:
    """Load a prompt by name; expand ``{{include:filename}}`` / ``{{include:filename.md}}``
    directives by splicing in another file from the same prompts directory.

    The include directive must appear on its own line (whitespace allowed). The
    referenced file is loaded recursively (max depth 3 to avoid cycles).

    Why: large reference catalogs (models / arsenal / routing decision trees)
    live in their own .md files for editability + reuse, but Director needs
    them in the same LLM call. Include lets us keep modular files and assemble
    at prompt-load time.
    """
    if _depth > 3:
        return ""  # avoid runaway recursion on a cycle
    path = os.path.join(_PROMPTS_DIR, f"{name}.md")
    if not os.path.exists(path) and not name.endswith(".md"):
        path = os.path.join(_PROMPTS_DIR, name)
    with open(path, encoding="utf-8") as f:
        text = f.read()

    # Expand {{include:OTHER}} (one per line)
    out_lines = []
    for line in text.splitlines(True):
        stripped = line.strip()
        if stripped.startswith("{{include:") and stripped.endswith("}}"):
            inner = stripped[len("{{include:"):-len("}}")].strip()
            inner_name = inner[:-3] if inner.endswith(".md") else inner
            try:
                included = _load_prompt(inner_name, _depth=_depth + 1)
                out_lines.append("\n<!-- included: " + inner + " -->\n")
                out_lines.append(included)
                out_lines.append("\n<!-- end-include: " + inner + " -->\n")
            except FileNotFoundError:
                logger.warning("prompt include not found: %s (in %s)", inner_name, name)
                out_lines.append(line)  # leave as-is so failure is visible
        else:
            out_lines.append(line)
    return "".join(out_lines)


def call_llm(step_key: str, **format_kwargs) -> Dict[str, Any]:
    """Make a quick LLM call for API-layer validation.

    Loads system/user prompts from config/prompts/{step_key}_system.md and
    {step_key}_user.md. Formats user prompt with kwargs.

    Tries Vertex AI first (always available), falls back to Vercel if set.

    Returns:
        Dict with text, input_tokens, output_tokens, model.
    """
    config = _get_config()
    step = config.get(step_key, {})
    # Strip Vercel-style "google/" prefix for Vertex — use the bare model name
    raw_model = step.get("model", "gemini-3-flash-preview")
    vertex_model = raw_model.split("/")[-1]  # "google/gemini-3-flash" → "gemini-3-flash"
    temperature = step.get("temperature", 0.1)
    max_tokens = step.get("max_tokens", 200)

    system_prompt = _load_prompt(f"{step_key}_system")
    user_prompt = _load_prompt(f"{step_key}_user").format(**format_kwargs)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Try Vertex first
    vertex = _get_vertex_provider()
    if vertex is not None:
        try:
            return vertex.call(
                vertex_model,
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.debug("Vertex call_llm failed for %s, trying Vercel: %s", step_key, e)

    # Fall back to Vercel
    vercel = _get_vercel_provider()
    if vercel is not None:
        schema = step.get("responseSchema")
        kwargs = dict(temperature=temperature, max_tokens=max_tokens)
        if schema:
            kwargs["responseSchema"] = schema
        return vercel.call(raw_model, messages, **kwargs)

    raise RuntimeError("No LLM provider available (Vertex and Vercel both unavailable)")
