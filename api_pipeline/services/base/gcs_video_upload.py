import io
import os
import time
import logging
from typing import Optional

import requests
from google.cloud import storage

from api_pipeline.services.base.config import config


logger = logging.getLogger(__name__)


class GCSVideoUploadService:
    """Service for uploading final videos to Google Cloud Storage."""

    def __init__(self, credentials_file: str = None, bucket_name: str = None):
        """Initialize GCS Video Upload Service.

        Args:
            credentials_file: Path to service account JSON file.
            bucket_name: GCS bucket name for video uploads.
        """
        self.credentials_file = credentials_file or "service_account.json"
        self.bucket_name = bucket_name
        self.storage_client = None
        self.bucket = None
        self._initialized = False

    def _initialize(self) -> bool:
        """Lazy initialization of GCS client."""
        if self._initialized:
            return True

        try:
            # Get absolute path to credentials file
            if os.path.isabs(self.credentials_file):
                creds_path = self.credentials_file
            else:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                creds_path = os.path.join(script_dir, self.credentials_file)

            if not os.path.exists(creds_path):
                # Try current working directory
                creds_path = os.path.join(os.getcwd(), self.credentials_file)

            if not os.path.exists(creds_path):
                logger.warning(f"⚠️ GCS credentials file not found: {self.credentials_file}")
                return False

            from google.oauth2 import service_account

            gcs_creds = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )

            self.storage_client = storage.Client(credentials=gcs_creds)
            self.bucket = self.storage_client.bucket(self.bucket_name)

            self._initialized = True
            logger.info(f"✅ GCS Video Upload Service initialized (bucket: {self.bucket_name})")
            return True

        except Exception as e:
            logger.warning(f"⚠️ Failed to initialize GCS Video Upload: {e}")
            return False

    def upload_video_from_url(
        self,
        source_url: str,
        key_name: str,
        folder: str = "influencer_videos"
    ) -> Optional[str]:
        """Upload a video from URL to GCS.

        Args:
            source_url: URL of the video to upload.
            key_name: Name for the GCS object.
            folder: Folder path within the bucket.

        Returns:
            Public URL of the uploaded video, or None if failed.
        """
        if not self._initialize():
            return None

        try:
            logger.info(f"📤 Uploading video to GCS bucket '{self.bucket_name}'...")

            # Download video
            with requests.get(source_url, stream=True, timeout=180) as r:
                r.raise_for_status()
                video_data = io.BytesIO()
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        video_data.write(chunk)
                video_data.seek(0)

            # Build GCS path
            blob_path = f"{folder}/{key_name}" if folder else key_name
            blob = self.bucket.blob(blob_path)

            # Upload to GCS
            blob.upload_from_file(video_data, content_type="video/mp4")

            # Try to make public (may fail if bucket has uniform bucket-level access)
            try:
                blob.make_public()
            except Exception as acl_error:
                logger.info(f"ℹ️ ACL not supported, using direct URL (bucket is likely public)")

            # Construct public URL directly
            public_url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_path}"
            logger.info(f"✅ Video uploaded to GCS: {public_url}")
            return public_url

        except Exception as e:
            logger.error(f"❌ Error uploading video to GCS: {e}")
            return None

    def upload_product_reference(
        self,
        frame_path: str,
        folder: str = None
    ) -> Optional[str]:
        """Upload a product reference frame to GCS for use in image generation.

        Args:
            frame_path: Path to the local frame image file.
            folder: GCS folder for reference images (defaults to config setting).

        Returns:
            Public URL of the uploaded reference image, or None if failed.
        """
        if not self._initialize():
            return None

        folder = folder or config.PRODUCT_REFERENCE_FOLDER

        try:
            if not os.path.exists(frame_path):
                logger.error(f"❌ [PRODUCT] Reference frame not found: {frame_path}")
                return None

            logger.info(f"📤 [PRODUCT] Uploading reference frame to GCS...")

            # Generate unique filename
            timestamp = int(time.time())
            filename = os.path.basename(frame_path)
            name_part = os.path.splitext(filename)[0]
            key_name = f"product_ref_{name_part}_{timestamp}.jpg"

            # Read the image file
            with open(frame_path, 'rb') as f:
                image_data = io.BytesIO(f.read())
                image_data.seek(0)

            # Build GCS path
            blob_path = f"{folder}/{key_name}" if folder else key_name
            blob = self.bucket.blob(blob_path)

            # Upload to GCS
            blob.upload_from_file(image_data, content_type="image/jpeg")

            # Try to make public
            try:
                blob.make_public()
            except Exception as acl_error:
                logger.info(f"ℹ️ [PRODUCT] ACL not supported, using direct URL")

            # Construct public URL
            public_url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_path}"
            logger.info(f"✅ [PRODUCT] Reference uploaded: {public_url}")
            return public_url

        except Exception as e:
            logger.error(f"❌ [PRODUCT] Error uploading reference: {e}")
            return None
