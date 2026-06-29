"""OpenAI service — backward-compat shim.

Delegates all provider operations to ``OpenAIProvider``.
Task methods have been extracted to ``tvd_pipeline.services.tasks.*``.
"""

import logging
from typing import Any, Dict

from tvd_pipeline.services.providers.openai_provider import OpenAIProvider
from tvd_pipeline.utils import get_cultural_adaptation_instructions

logger = logging.getLogger(__name__)


class OpenAIService:
    """Backward-compat wrapper around OpenAIProvider.

    Pipeline code now imports task functions directly from
    ``tvd_pipeline.services.tasks.*``.  This class remains so that
    existing wiring (``processor.openai_service``) keeps working.
    """

    def __init__(self, api_key: str):
        """Initialize OpenAI service."""
        self._provider = OpenAIProvider(api_key=api_key)
        # Backward-compat: expose raw SDK client (used by ElevenLabs, Suno)
        self.client = self._provider.client

    def call(self, model: str, messages: list, **kwargs) -> Dict[str, Any]:
        """Delegate to OpenAIProvider.call()."""
        return self._provider.call(model, messages, **kwargs)

    def _get_cultural_style_instructions(self, language: str) -> str:
        return get_cultural_adaptation_instructions(language)
