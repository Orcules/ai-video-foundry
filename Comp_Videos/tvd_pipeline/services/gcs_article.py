"""GCS Article service for fetching article data from Google Cloud Storage."""

import os
import re
import json
import time
import logging
from typing import Dict, Any, Optional

from google.cloud import storage
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)


class GCSArticleService:
    """Service for fetching article data from Google Cloud Storage.

    When the Article column contains a URL, this service looks up the corresponding
    JSON file in GCS and extracts the article content (Title, 1stP, Rest of Content).
    """

    def __init__(self, credentials_file: str = None, bucket_name: str = None, folder_name: str = None):
        """Initialize GCS Article Service.

        Args:
            credentials_file: Path to GCS service account JSON file.
            bucket_name: GCS bucket name.
            folder_name: Folder/prefix in the bucket containing article JSON files.
        """
        # Import config lazily to avoid circular imports
        from tvd_pipeline.config import Config
        _config = Config()

        self.credentials_file = credentials_file or _config.GCS_CREDENTIALS_FILE
        self.bucket_name = bucket_name or _config.GCS_BUCKET_NAME
        self.folder_name = folder_name or _config.GCS_FOLDER_NAME

        self.cache = {}  # In-memory cache for article data
        self.gcs_file_list_cache = None  # Cache for GCS file list
        self.gcs_file_list_cache_time = 0
        self.cache_ttl = 300  # 5 minutes TTL

        self.storage_client = None
        self.bucket = None
        self._initialized = False

    def _initialize(self):
        """Lazy initialization of GCS client."""
        if self._initialized:
            return True

        try:
            # Get absolute path to GCS credentials file
            if os.path.isabs(self.credentials_file):
                gcs_credentials_path = self.credentials_file
            else:
                # Try current working directory
                gcs_credentials_path = os.path.join(os.getcwd(), self.credentials_file)

            if not os.path.exists(gcs_credentials_path):
                # Try script directory
                script_dir = os.path.dirname(os.path.abspath(__file__))
                fallback_path = os.path.join(script_dir, self.credentials_file)
                if os.path.exists(fallback_path):
                    gcs_credentials_path = fallback_path
                else:
                    logger.warning(f"GCS credentials file not found: {self.credentials_file}")
                    logger.warning("   Article URL lookup will be disabled")
                    return False

            logger.info(f"Loading GCS credentials from: {gcs_credentials_path}")

            gcs_creds = Credentials.from_service_account_file(
                gcs_credentials_path,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )

            self.storage_client = storage.Client(credentials=gcs_creds)
            self.bucket = self.storage_client.bucket(self.bucket_name)

            self._initialized = True
            logger.info("GCS Article Service initialized successfully")
            return True

        except Exception as e:
            logger.warning(f"Failed to initialize GCS: {e}")
            logger.warning("   Article URL lookup will be disabled")
            return False

    def is_url(self, value: str) -> bool:
        """Check if a value is a URL."""
        if not value or not isinstance(value, str):
            return False
        return value.strip().startswith("http")

    def url_to_filename(self, url: str) -> str:
        """Convert URL to filename pattern for GCS lookup.

        Args:
            url: The article URL.

        Returns:
            Sanitized filename pattern.
        """
        if not url:
            return ""

        # Apply domain substitutions
        substitutions = {
            "legacy-site-1.example.com": "current-site-a.example.com",
            "legacy-site-2.example.com": "current-site-a.example.com",
            "legacy-site-3.example.com": "current-site-b.example.com",
            "legacy-site-4.example.com": "current-site-b.example.com",
            "legacy-site-5.example.com": "current-site-b.example.com"
        }

        for old_domain, new_domain in substitutions.items():
            url = url.replace(old_domain, new_domain)

        # Convert URL to filename pattern
        sanitized = str(url).strip()

        # Replace problematic characters with underscores
        sanitized = re.sub(r'[<>:"/\\|?*]', '_', sanitized)
        # Replace multiple spaces with single underscore
        sanitized = re.sub(r'\s+', '_', sanitized)
        # Remove multiple consecutive underscores
        sanitized = re.sub(r'_+', '_', sanitized)
        # Remove leading/trailing underscores
        sanitized = sanitized.strip('_')

        return sanitized

    def get_article_data(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch article data from GCS by URL.

        Args:
            url: The article URL to look up.

        Returns:
            Dictionary with 'Title', '1stp', 'Rest of Content' or None if not found.
        """
        if not url or not self.is_url(url):
            return None

        # Initialize GCS client if needed
        if not self._initialize():
            return None

        try:
            # Check cache first
            cache_key = f"gcs_{url}"
            if cache_key in self.cache:
                cached = self.cache[cache_key]
                if cached is not None:
                    logger.debug(f"Cache hit for: {url}")
                    return cached
                return None  # Negative cache hit

            url_filename = self.url_to_filename(url)
            logger.debug(f"Looking for GCS file matching: {url_filename[:50]}...")

            # Get cached file list or fetch new one
            current_time = time.time()
            if (self.gcs_file_list_cache is None or
                current_time - self.gcs_file_list_cache_time > self.cache_ttl):

                logger.info("Refreshing GCS file list cache...")
                blobs = list(self.bucket.list_blobs(prefix=f"{self.folder_name}/"))
                self.gcs_file_list_cache = blobs
                self.gcs_file_list_cache_time = current_time
                logger.info(f"Cached {len(blobs)} files from GCS")
            else:
                blobs = self.gcs_file_list_cache

            # Find matching file
            matched_file = None
            highest_version = 0

            for blob in blobs:
                filename = blob.name.split('/')[-1]

                if url_filename in filename:
                    # Extract version number if present
                    version_match = re.search(r'_v(\d+)\.json$', filename)
                    if version_match:
                        version = int(version_match.group(1))
                        if version > highest_version:
                            highest_version = version
                            matched_file = blob
                    elif matched_file is None:
                        matched_file = blob

            if matched_file:
                logger.info(f"Found GCS file: {matched_file.name}")
                json_content = matched_file.download_as_text()
                file_data = json.loads(json_content)

                # Cache the result
                self.cache[cache_key] = file_data
                return file_data

            logger.warning(f"No GCS file found for URL: {url[:50]}...")
            # Cache negative result
            self.cache[cache_key] = None
            return None

        except Exception as e:
            logger.error(f"Error fetching article from GCS: {e}")
        return None
