"""Vercel AI Hub service — backward-compat shim.

Delegates all operations to ``VercelProvider`` in ``providers/vercel.py``.
"""

import logging
from typing import Any, Dict

from tvd_pipeline.services.providers.vercel import VercelProvider

logger = logging.getLogger(__name__)


class VercelAIHubService:
    """Backward-compat wrapper around VercelProvider."""

    def __init__(self, api_key: str = None):
        self._provider = VercelProvider(api_key=api_key)
        self.initialized = self._provider.initialized

    def call(self, model: str, messages: list, **kwargs) -> Dict[str, Any]:
        return self._provider.call(model, messages, **kwargs)
