"""Load prompt templates from .md files under ``tvd_pipeline/config/prompts/``.

Usage::

    from tvd_pipeline.prompt_loader import PromptLoader

    loader = PromptLoader()
    prompt = loader.get("shared_opening_text", lang_name="Hebrew", hook_style="trust-building")

Template files live in ``tvd_pipeline/config/prompts/`` and use Python ``str.format``
placeholders (e.g. ``{lang_name}``).  The *key* passed to ``get()`` maps directly to
the filename without the ``.md`` extension.

Prefix convention (see also ``tvd_pipeline/config/prompts/``):

- ``shared_``              -- used across multiple pipeline types (product + UGC)
- ``product_``             -- product pipeline only
- ``ugc_``                 -- shared between both UGC subtypes (influencer + personal brand)
- ``ugc_influencer_``      -- influencer subtype only
- ``ugc_personal_brand_``  -- personal brand subtype only
"""

import os
import logging

logger = logging.getLogger(__name__)

_DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "config", "prompts")


class PromptLoader:
    """Lazy-loading, caching prompt template reader."""

    def __init__(self, prompts_dir: str = None):
        self.prompts_dir = prompts_dir or _DEFAULT_DIR
        self._cache: dict[str, str] = {}

    def get(self, key: str, **kwargs) -> str:
        """Return a rendered prompt template.

        Parameters
        ----------
        key : str
            Template name (maps to ``<prompts_dir>/<key>.md``).
        **kwargs :
            Values substituted into ``{placeholder}`` slots in the template.
            If no kwargs are given the raw template is returned.
        """
        if key not in self._cache:
            path = os.path.join(self.prompts_dir, f"{key}.md")
            try:
                with open(path, encoding="utf-8") as f:
                    self._cache[key] = f.read()
            except FileNotFoundError:
                logger.error("Prompt template not found: %s", path)
                raise
        template = self._cache[key]
        if kwargs:
            try:
                return template.format(**kwargs)
            except KeyError as exc:
                logger.warning(
                    "Missing placeholder %s in prompt template %s; returning raw template",
                    exc, key,
                )
                return template
        return template


# Module-level singleton for convenience
_LOADER: PromptLoader | None = None


def get_prompt_loader() -> PromptLoader:
    """Return the module-level PromptLoader singleton."""
    global _LOADER
    if _LOADER is None:
        _LOADER = PromptLoader()
    return _LOADER
