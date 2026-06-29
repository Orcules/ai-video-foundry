"""Veo3Service — extracted verbatim from Comp_Videos/video_scene_processor.py.

Lines 5760-6040 of the monolith.
"""

import os
import time
import base64
import logging
import random
import requests
from typing import Dict, Any, List, Optional, Tuple

from api_pipeline.services.base.config import config

logger = logging.getLogger(__name__)


class Veo3Service:
    """Service for video generation using Google's Veo via Vertex AI.
    
    Supports Veo 3.0 and Veo 3.1 Fast models.
    Supports text-to-video and image-to-video generation.
    Uses long-running operations that need to be polled for completion.
    Uses Vertex AI API key (same as Gemini Image).
    """
    
    def __init__(self, gcs_storage_service=None, model: str = None):
        """Initialize Veo service.
        
        Args:
            gcs_storage_service: GCS storage service for uploading/downloading videos.
            model: Model name override (default: config.VEO3_MODEL).
        """
        self.gcs_storage_service = gcs_storage_service
        self.initialized = False
        
        self.api_key = config.VERTEX_AI_API_KEY
        self.model = model or config.VEO3_MODEL
        self.project_id = config.VEO3_PROJECT_ID
        
        # Build endpoint URLs (template supports any Veo model)
        self.generate_endpoint = config.VEO3_GENERATE_ENDPOINT.format(
            project_id=self.project_id,
            model=self.model
        )
        self.poll_endpoint = config.VEO3_POLL_ENDPOINT.format(
            project_id=self.project_id,
            model=self.model
        )
        
        if not self.api_key:
            logger.warning("Veo not available - VERTEX_AI_API_KEY not set")
            return
        
        self.headers = {"Content-Type": "application/json"}
        self.initialized = True
        logger.info(f"Veo video service initialized ({self.model})")
    
    def _fetch_image_as_base64(self, image_url: str) -> Optional[Tuple[str, str]]:
        """Fetch an image from URL and encode as base64.
        
        Args:
            image_url: URL of the image to fetch.
            
        Returns:
            Tuple of (base64_data, mime_type) or None if failed.
        """
        try:
            # Use browser-like headers to avoid 403 Forbidden from websites
            fetch_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": image_url
            }
            response = requests.get(image_url, headers=fetch_headers, timeout=30)
            response.raise_for_status()
            
            # Determine MIME type
            content_type = response.headers.get("Content-Type", "").lower()
            if "png" in content_type or image_url.lower().endswith(".png"):
                mime_type = "image/png"
            elif "gif" in content_type or image_url.lower().endswith(".gif"):
                mime_type = "image/gif"
            elif "webp" in content_type or image_url.lower().endswith(".webp"):
                mime_type = "image/webp"
            else:
                mime_type = "image/jpeg"
            
            base64_data = base64.b64encode(response.content).decode("utf-8")
            return (base64_data, mime_type)
            
        except Exception as e:
            logger.warning(f"Failed to fetch image {image_url[:60]}...: {e}")
            return None
    
    def _poll_operation(self, operation_name: str) -> Optional[Dict]:
        """Poll a long-running operation until completion.
        
        Args:
            operation_name: Full operation name from the initial request.
            
        Returns:
            Operation result dict if successful, None if failed/timeout.
        """
        start_time = time.time()
        poll_count = 0
        
        while time.time() - start_time < config.VEO3_MAX_POLL_TIME:
            poll_count += 1
            
            try:
                payload = {
                    "operationName": operation_name
                }
                
                url = f"{self.poll_endpoint}?key={self.api_key}"
                response = requests.post(
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=60
                )
                response.raise_for_status()
                
                result = response.json()
                
                if result.get("done", False):
                    logger.info(f"Veo 3 operation completed after {poll_count} polls")
                    # Debug: log the full response structure
                    logger.info(f"Veo 3 response keys: {list(result.keys())}")
                    if "response" in result:
                        logger.info(f"Response keys: {list(result['response'].keys())}")
                    if "error" in result:
                        logger.error(f"Veo 3 error: {result['error']}")
                    return result
                
                # Still running, wait and poll again
                elapsed = int(time.time() - start_time)
                logger.info(f"Veo 3 video generation in progress... ({elapsed}s elapsed)")
                time.sleep(config.VEO3_POLL_INTERVAL)
                
            except Exception as e:
                logger.warning(f"Error polling Veo 3 operation: {e}")
                time.sleep(config.VEO3_POLL_INTERVAL)
        
        logger.error(f"Veo 3 operation timed out after {config.VEO3_MAX_POLL_TIME}s")
        return None
    
    def generate_video(
        self,
        prompt: str,
        image_url: str = None,
        duration: float = 5.0,
        resolution: str = None
    ) -> Optional[str]:
        """Generate a video using Veo 3.
        
        Args:
            prompt: Text prompt for video generation (motion/action description).
            image_url: Optional URL of an image to use as the first frame.
            duration: Desired video duration in seconds (informational).
            resolution: Video resolution ('720p' or '1080p').
            
        Returns:
            URL of the generated video (from GCS), or None if failed.
        """
        if not self.initialized:
            logger.error("Veo 3 service not initialized")
            return None
        
        try:
            # Build request payload
            instance = {
                "prompt": prompt
            }
            
            # Add image if provided (image-to-video)
            if image_url:
                image_data = self._fetch_image_as_base64(image_url)
                if image_data:
                    base64_data, mime_type = image_data
                    instance["image"] = {
                        "bytesBase64Encoded": base64_data,
                        "mimeType": mime_type
                    }
                    logger.info("Using image as first frame for Veo 3")
            
            # Do not pass storageUri: Vertex's service account cannot write to our bucket.
            # Without storageUri, the API returns base64 video bytes; we upload to GCS ourselves.
            payload = {
                "instances": [instance],
                "parameters": {
                    "sampleCount": 1,
                    "resolution": resolution or config.VEO3_DEFAULT_RESOLUTION,
                    "generateAudio": False  # We generate audio separately
                }
            }
            
            # Make API request with Vertex AI API key
            logger.info(f"Starting Veo 3 video generation...")
            url = f"{self.generate_endpoint}?key={self.api_key}"
            response = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=120
            )
            response.raise_for_status()
            
            result = response.json()
            
            # Get operation name for polling
            operation_name = result.get("name")
            if not operation_name:
                logger.error("No operation name in Veo 3 response")
                return None
            
            logger.info(f"Veo 3 operation started: {operation_name}")
            
            # Poll until completion
            final_result = self._poll_operation(operation_name)
            if not final_result:
                return None
            
            # Extract video URL from response
            response_data = final_result.get("response", {})
            videos = response_data.get("videos", [])
            
            if not videos:
                logger.error("No videos in Veo 3 response")
                logger.error(f"Full response_data: {response_data}")
                return None
            
            # Get the GCS URI of the generated video
            video_info = videos[0]
            gcs_uri = video_info.get("gcsUri")
            
            if gcs_uri:
                # Convert GCS URI to public URL
                # gs://bucket/path -> https://storage.googleapis.com/bucket/path
                if gcs_uri.startswith("gs://"):
                    public_url = gcs_uri.replace("gs://", "https://storage.googleapis.com/")
                    logger.info(f"Veo 3 video generated: {public_url[:60]}...")
                    return public_url
            
            # If we have base64 data instead, upload to GCS
            video_base64 = video_info.get("bytesBase64Encoded")
            if video_base64 and self.gcs_storage_service:
                video_bytes = base64.b64decode(video_base64)
                key = f"veo3_videos/veo3_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
                video_url = self.gcs_storage_service.upload_video_bytes(
                    video_data=video_bytes,
                    key_name=key,
                    make_public=True
                )
                if video_url:
                    logger.info(f"Veo 3 video uploaded: {video_url[:60]}...")
                    return video_url
            
            logger.error("Could not extract video from Veo 3 response")
            return None
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"Veo 3 API error: {e.response.status_code} - {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"Error generating video with Veo 3: {e}")
            return None
    
    def generate_video_from_image(
        self,
        image_url: str,
        motion_prompt: str,
        duration: float = 5.0
    ) -> Optional[str]:
        """Generate a video from an image with motion.
        
        This is image-to-video generation where the image becomes the first frame.
        
        Args:
            image_url: URL of the image to animate.
            motion_prompt: Text describing the motion/action.
            duration: Desired video duration in seconds.
            
        Returns:
            URL of the generated video, or None if failed.
        """
        return self.generate_video(
            prompt=motion_prompt,
            image_url=image_url,
            duration=duration
        )
