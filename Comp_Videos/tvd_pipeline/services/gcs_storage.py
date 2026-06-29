"""Google Cloud Storage service for uploading videos, audio, and images."""

import io
import os
import logging

import requests
from google.cloud import storage

logger = logging.getLogger(__name__)


class GCSStorageService:
    """Unified service for Google Cloud Storage operations.

    Replaces S3Service with GCS functionality while maintaining the same
    method signatures for compatibility.
    """

    def __init__(
        self,
        credentials_file: str,
        bucket_name: str,
        folder_path: str
    ):
        """Initialize GCS Storage service.

        Args:
            credentials_file: Path to GCS service account JSON file.
            bucket_name: GCS bucket name.
            folder_path: Folder path within the bucket.
        """
        self.credentials_file = credentials_file
        self.bucket_name = bucket_name

        # Normalize folder path
        folder_path = folder_path.strip().lstrip('/')
        if folder_path and not folder_path.endswith('/'):
            folder_path += '/'
        self.folder = folder_path

        # Initialize GCS client
        self.storage_client = None
        self.bucket = None
        self._initialized = False
        self._initialize()

    def _initialize(self) -> bool:
        """Initialize the GCS client."""
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
                logger.error(f"GCS credentials file not found: {self.credentials_file}")
                return False

            from google.oauth2 import service_account

            gcs_creds = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )

            self.storage_client = storage.Client(credentials=gcs_creds)
            self.bucket = self.storage_client.bucket(self.bucket_name)

            self._initialized = True
            logger.info(f"GCS Storage Service initialized (bucket: {self.bucket_name})")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize GCS Storage Service: {e}")
            return False

    def upload_video_from_url(
        self,
        source_url: str,
        key_name: str,
        make_public: bool = True
    ):
        """Upload a video from URL to GCS.

        Args:
            source_url: URL of the video to upload.
            key_name: Name for the GCS object.
            make_public: Whether to make the object publicly readable.

        Returns:
            Public URL of the uploaded video, or None if failed.
        """
        if not self._initialize():
            return None

        try:
            logger.info(f"Uploading video to GCS bucket '{self.bucket_name}'...")

            # Download video
            with requests.get(source_url, stream=True, timeout=180) as r:
                r.raise_for_status()
                body = io.BytesIO()
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        body.write(chunk)
                body.seek(0)

            # Build GCS path
            blob_path = f"{self.folder}{key_name}"
            blob = self.bucket.blob(blob_path)

            # Upload to GCS
            blob.upload_from_file(body, content_type="video/mp4")

            # Try to make public (may fail if bucket has uniform bucket-level access)
            if make_public:
                try:
                    blob.make_public()
                except Exception:
                    logger.info("ACL not supported, using direct URL (bucket is likely public)")

            # Build public URL
            url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_path}"
            logger.info(f"Uploaded video to GCS: {url}")
            return url

        except Exception as e:
            logger.error(f"GCS video upload failed: {e}")
            return None

    def upload_video_bytes(
        self,
        video_data: bytes,
        key_name: str,
        make_public: bool = True,
        content_type: str = "video/mp4"
    ):
        """Upload video bytes to GCS.

        Args:
            video_data: Video data as bytes.
            key_name: Name for the GCS object.
            make_public: Whether to make the object publicly readable.
            content_type: MIME type of the video (default video/mp4).

        Returns:
            Public URL of the uploaded video, or None if failed.
        """
        if not self._initialize():
            return None

        try:
            logger.info(f"Uploading video bytes to GCS bucket '{self.bucket_name}'...")
            body = io.BytesIO(video_data)
            blob_path = f"{self.folder}{key_name}"
            blob = self.bucket.blob(blob_path)
            blob.upload_from_file(body, content_type=content_type)
            if make_public:
                try:
                    blob.make_public()
                except Exception:
                    logger.info("ACL not supported, using direct URL")
            url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_path}"
            logger.info(f"Uploaded video to GCS: {url}")
            return url
        except Exception as e:
            logger.error(f"GCS video bytes upload failed: {e}")
            return None

    def upload_audio_bytes(
        self,
        audio_data: bytes,
        key_name: str,
        make_public: bool = True
    ):
        """Upload audio bytes to GCS.

        Args:
            audio_data: Audio data as bytes.
            key_name: Name for the GCS object.
            make_public: Whether to make the object publicly readable.

        Returns:
            Public URL of the uploaded audio, or None if failed.
        """
        if not self._initialize():
            return None

        try:
            logger.info(f"Uploading audio to GCS bucket '{self.bucket_name}'...")

            body = io.BytesIO(audio_data)
            blob_path = f"{self.folder}{key_name}"
            blob = self.bucket.blob(blob_path)

            # Upload to GCS
            blob.upload_from_file(body, content_type="audio/mpeg")

            # Try to make public
            if make_public:
                try:
                    blob.make_public()
                except Exception:
                    logger.info("ACL not supported, using direct URL")

            url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_path}"
            logger.info(f"Uploaded audio to GCS: {url}")
            return url

        except Exception as e:
            logger.error(f"GCS audio upload failed: {e}")
            return None

    def upload_image_bytes(
        self,
        image_data: bytes,
        key_name: str,
        make_public: bool = True,
        content_type: str = "image/png"
    ):
        """Upload image bytes to GCS.

        Args:
            image_data: Image data as bytes.
            key_name: Name for the GCS object.
            make_public: Whether to make the object publicly readable.
            content_type: MIME type of the image.

        Returns:
            Public URL of the uploaded image, or None if failed.
        """
        if not self._initialize():
            return None

        try:
            logger.info(f"Uploading image to GCS bucket '{self.bucket_name}'...")

            body = io.BytesIO(image_data)
            blob_path = f"{self.folder}{key_name}"
            blob = self.bucket.blob(blob_path)

            # Upload to GCS
            blob.upload_from_file(body, content_type=content_type)

            # Try to make public
            if make_public:
                try:
                    blob.make_public()
                except Exception:
                    logger.info("ACL not supported, using direct URL")

            url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_path}"
            logger.info(f"Uploaded image to GCS: {url}")
            return url

        except Exception as e:
            logger.error(f"GCS image upload failed: {e}")
            return None

    def upload_image_from_url(
        self,
        source_url: str,
        key_name: str,
        make_public: bool = True,
        timeout: int = 30
    ):
        """Download image from URL and upload to GCS.

        Args:
            source_url: URL of the image to download.
            key_name: Name for the GCS object (e.g. ref_images/row2_char_123.jpg).
            make_public: Whether to make the object publicly readable.
            timeout: Download timeout in seconds.

        Returns:
            Public URL of the uploaded image, or None if failed.
        """
        if not self._initialize():
            return None
        if not source_url or not source_url.strip().startswith(("http://", "https://")):
            return None
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/*,*/*;q=0.8"
            }
            r = requests.get(source_url, headers=headers, timeout=timeout)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "").lower()
            if "png" in ct:
                content_type = "image/png"
                ext = ".png"
            elif "webp" in ct:
                content_type = "image/webp"
                ext = ".webp"
            else:
                content_type = "image/jpeg"
                ext = ".jpg"
            if not key_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                key_name = key_name.rstrip(".") + ext
            blob_path = f"{self.folder}{key_name}"
            blob = self.bucket.blob(blob_path)
            blob.upload_from_string(r.content, content_type=content_type)
            if make_public:
                try:
                    blob.make_public()
                except Exception:
                    pass
            url = f"https://storage.googleapis.com/{self.bucket_name}/{blob_path}"
            return url
        except Exception as e:
            logger.warning(f"Could not upload image from URL to GCS: {e}")
            return None
