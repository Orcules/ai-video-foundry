"""Gemini text service — backward-compat shim.

Delegates all provider operations to ``VertexAIProvider``.
Task methods have been extracted to ``tvd_pipeline.services.tasks.*``.
"""

import logging
from typing import Any, Dict, Optional

from tvd_pipeline.services.providers.vertex import VertexAIProvider
from tvd_pipeline.utils import get_cultural_adaptation_instructions

logger = logging.getLogger(__name__)


class GeminiService:
    """Backward-compat wrapper around VertexAIProvider.

    Pipeline code now imports task functions directly from
    ``tvd_pipeline.services.tasks.*``.  This class remains so that
    existing wiring (``processor.gemini_service``) keeps working.
    """

    def __init__(self, api_key: str = None, gcs_storage_service=None):
        """Initialize Gemini service via Vertex AI."""
        self._provider = VertexAIProvider(api_key=api_key, gcs_storage_service=gcs_storage_service)
        # Expose provider attributes for backward compatibility
        self.gcs_storage_service = self._provider.gcs_storage_service
        self.initialized = self._provider.initialized
        self.vertex_api_key = self._provider.vertex_api_key
        self.model = self._provider.model
        self.project_id = self._provider.project_id
        self.location = self._provider.location
        self.kie_api_key = self._provider.kie_api_key
        self._use_api_key = self._provider._use_api_key
        self._endpoint_template = self._provider._endpoint_template

    # -- Provider delegation --------------------------------------------------

    def call(self, model: str, messages: list, **kwargs) -> Dict[str, Any]:
        """Delegate to VertexAIProvider.call()."""
        return self._provider.call(model, messages, **kwargs)

    def _get_vertex_token_from_adc(self) -> Optional[str]:
        return self._provider._get_vertex_token_from_adc()

    def _get_vertex_headers(self) -> Dict[str, str]:
        return self._provider._get_vertex_headers()

    def _get_vertex_url(self, model: str) -> str:
        return self._provider._get_vertex_url(model)

    def _vertex_post_with_retry(self, url, headers, json_payload, timeout=None, max_retries=None):
        return self._provider._vertex_post_with_retry(url, headers, json_payload, timeout, max_retries)

    def _upload_video_to_gcs(self, video_path: str) -> Optional[str]:
        return self._provider._upload_video_to_gcs(video_path)

    def _cleanup_gcs_video(self, video_url: str):
        return self._provider._cleanup_gcs_video(video_url)

    def _character_image_uri_for_vertex(self, image_url: str) -> Optional[str]:
        return self._provider._character_image_uri_for_vertex(image_url)

    def _get_cultural_adaptation_instructions(self, target_language: str) -> str:
        return get_cultural_adaptation_instructions(target_language)
