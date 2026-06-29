"""Slim service registry — GCS + Mux only.

The wrapper branch delegates all pipeline logic to the monolith via
wrapper/monolith_bridge.py. Pipeline services (Gemini, OpenAI, Kie, etc.)
live inside the monolith and are never instantiated by the API server.

This registry only holds:
  - Config (API keys, endpoints — needed for health checks)
  - GCSStorageService (file upload endpoint uses it)
  - MuxUploadService (wrapper uploads final video to Mux CDN)
"""

import os
import logging

from api_pipeline.services.base.config import Config
from api_pipeline.services.base.gcs_storage_service import GCSStorageService
from api_pipeline.mux_service import MuxUploadService

logger = logging.getLogger(__name__)


def _upload_to_gcs_permanent(gcs: GCSStorageService, url: str, key: str) -> str:
    """Re-upload a temporary URL to GCS for permanent storage.

    Routes to the appropriate GCS upload method based on file extension in key:
      .mp4/.mov/.webm → upload_video_from_url
      .mp3/.wav       → upload_audio_from_url
      .jpg/.png/.webp → upload_image_from_url
      unknown         → upload_video_from_url (safe default for Rendi URLs)

    Returns the GCS URL, or the original URL on failure.
    """
    if not url:
        return url
    # Already a GCS URL from our bucket — skip
    if "storage.googleapis.com/automatiq" in url:
        return url
    try:
        key_lower = key.lower()
        if key_lower.endswith((".mp3", ".wav")):
            gcs_url = gcs.upload_audio_from_url(url, key)
        elif key_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            gcs_url = gcs.upload_image_from_url(url, key)
        else:
            # .mp4, .mov, .webm, or no extension (Rendi URLs)
            gcs_url = gcs.upload_video_from_url(url, key)
        if gcs_url:
            return gcs_url
    except Exception as e:
        logger.warning(f"GCS re-upload failed for {key}: {e}")
    return url


class ServiceRegistry:
    """Slim service container — GCS for uploads, Mux for CDN delivery."""

    def __init__(self):
        logger.info("Initializing service registry...")
        self.config = Config()

        # GCS storage (used by /api/upload and wrapper for re-uploads)
        self.gcs_storage = GCSStorageService(
            credentials_file=self.config.GCS_UPLOAD_CREDENTIALS_FILE,
            bucket_name=self.config.GCS_UPLOAD_BUCKET_NAME,
            folder_path=self.config.GCS_UPLOAD_FOLDER,
        )

        # Mux CDN (wrapper uploads final video here after monolith completes)
        mux_token = os.environ.get("MUX_TOKEN_ID")
        mux_secret = os.environ.get("MUX_TOKEN_SECRET")
        self.mux = MuxUploadService(mux_token, mux_secret) if mux_token and mux_secret else None

        logger.info(f"Service registry initialized (mux={'yes' if self.mux else 'no'})")
