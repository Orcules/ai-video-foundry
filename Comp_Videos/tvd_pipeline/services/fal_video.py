"""fal.ai video generation service (reference-to-video via Veo 3.1)."""

import time
import random
import logging

import requests

from tvd_pipeline.data_loader import get_fal_config
from tvd_pipeline.utils import snap_duration

logger = logging.getLogger(__name__)


class FalVideoService:
    """Service for video generation using fal.ai queue API.

    Supports reference-to-video: send multiple reference images + prompt,
    get back a video where the model composes and animates the subjects.
    """

    def __init__(self, api_key: str, gcs_storage_service=None):
        self.api_key = api_key
        self.gcs = gcs_storage_service
        self.headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }

    def remove_background(self, image_url: str) -> str | None:
        """Remove background from an image using fal.ai birefnet.

        Args:
            image_url: URL of the image to process.

        Returns:
            URL of the background-removed image (GCS if available), or None on failure.
        """
        cfg = get_fal_config().get("background_removal", {})
        endpoint = f"https://queue.fal.run/{cfg.get('endpoint', 'fal-ai/birefnet')}"
        poll_interval = cfg.get("poll_interval", 2)
        max_poll_time = cfg.get("max_poll_time", 60)

        payload = {"image_url": image_url}

        try:
            logger.info(f"fal.ai background removal submitting to {endpoint}")
            resp = requests.post(endpoint, headers=self.headers, json=payload, timeout=30)
            if resp.status_code != 200:
                logger.error(f"fal.ai bg removal submit failed ({resp.status_code}): {resp.text[:500]}")
                return None
            data = resp.json()
            request_id = data.get("request_id", "unknown")
            status_url = data.get("status_url")
            response_url = data.get("response_url")
            logger.info(f"fal.ai bg removal submitted (request_id={request_id})")

            if not status_url or not response_url:
                logger.error(f"fal.ai bg removal missing status_url/response_url: {data}")
                return None

            # Poll until complete
            start = time.time()
            while time.time() - start < max_poll_time:
                time.sleep(poll_interval)
                try:
                    status_resp = requests.get(status_url, headers=self.headers, timeout=15)
                    status_resp.raise_for_status()
                    status = status_resp.json()
                    state = status.get("status")
                    if state == "COMPLETED":
                        break
                    elif state in ("FAILED", "CANCELLED"):
                        logger.error(f"fal.ai bg removal {state}: {status}")
                        return None
                    elapsed = int(time.time() - start)
                    logger.info(f"fal.ai bg removal processing... ({elapsed}s elapsed)")
                except Exception as poll_err:
                    logger.warning(f"fal.ai bg removal poll error: {poll_err}")
            else:
                logger.error(f"fal.ai bg removal timed out after {max_poll_time}s")
                return None

            # Fetch result
            result_resp = requests.get(response_url, headers=self.headers, timeout=30)
            if result_resp.status_code != 200:
                logger.error(f"fal.ai bg removal result fetch failed ({result_resp.status_code})")
                return None
            result = result_resp.json()
            image_result_url = result.get("image", {}).get("url")

            if not image_result_url:
                logger.error(f"fal.ai bg removal no image URL in result: {list(result.keys())}")
                return None

            logger.info(f"fal.ai bg removal completed: {image_result_url[:80]}...")

            # Re-upload to GCS for durable URL
            if self.gcs:
                try:
                    key = f"fal_bg_removed/fal_bgr_{int(time.time())}_{random.randint(1000, 9999)}.png"
                    gcs_url = self.gcs.upload_video_from_url(image_result_url, key)
                    if gcs_url:
                        logger.info(f"fal.ai bg-removed image re-uploaded to GCS: {gcs_url[:60]}...")
                        return gcs_url
                except Exception as gcs_err:
                    logger.warning(f"fal.ai GCS re-upload failed: {gcs_err}, using original URL")

            return image_result_url

        except Exception as e:
            logger.error(f"fal.ai background removal failed: {e}")
            return None

    def generate_video(
        self,
        prompt: str,
        image_urls: list,
        video_model: str = "veo-3.1-ref-fal",
        duration: float = 8,
        aspect_ratio: str = None,
        resolution: str = None,
    ) -> str | None:
        """Submit reference-to-video to fal.ai queue, poll until done.

        Args:
            prompt: Text prompt describing placement, action, and camera.
            image_urls: List of reference image URLs (e.g. [influencer, venue]).
            video_model: Model version name for duration snapping.
            duration: Desired duration in seconds (snapped internally).
            aspect_ratio: Aspect ratio override (default from fal.json).
            resolution: Resolution override (default from fal.json).

        Returns:
            Video URL (GCS if available, otherwise fal.ai CDN), or None on failure.
        """
        cfg = get_fal_config().get("reference_to_video", {})
        endpoint = cfg.get("endpoint", "https://queue.fal.run/fal-ai/veo3.1/reference-to-video")
        poll_interval = cfg.get("poll_interval", 3)
        max_poll_time = cfg.get("max_poll_time", 300)

        duration_sec = snap_duration(video_model, int(round(duration)))
        dur_str = f"{duration_sec}s"

        payload = {
            "prompt": prompt,
            "image_urls": image_urls,
            "duration": dur_str,
            "aspect_ratio": aspect_ratio or cfg.get("aspect_ratio", "9:16"),
            "resolution": resolution or cfg.get("resolution", "720p"),
            "generate_audio": cfg.get("generate_audio", False),
            "safety_tolerance": cfg.get("safety_tolerance", "5"),
        }

        try:
            logger.info(f"fal.ai ref-to-video submitting to {endpoint}")
            logger.info(f"fal.ai payload: {len(image_urls)} images, duration={duration}, ar={payload['aspect_ratio']}, res={payload['resolution']}")
            resp = requests.post(endpoint, headers=self.headers, json=payload, timeout=30)
            if resp.status_code != 200:
                logger.error(f"fal.ai submit failed ({resp.status_code}): {resp.text[:500]}")
                resp.raise_for_status()
            data = resp.json()
            request_id = data.get("request_id", "unknown")
            status_url = data.get("status_url")
            response_url = data.get("response_url")
            logger.info(f"fal.ai ref-to-video submitted (request_id={request_id})")

            if not status_url or not response_url:
                logger.error(f"fal.ai missing status_url/response_url: {data}")
                return None

            # Poll until complete
            start = time.time()
            while time.time() - start < max_poll_time:
                time.sleep(poll_interval)
                try:
                    status_resp = requests.get(status_url, headers=self.headers, timeout=15)
                    if status_resp.status_code != 200:
                        logger.warning(f"fal.ai status poll ({status_resp.status_code}): {status_resp.text[:300]}")
                    status_resp.raise_for_status()
                    status = status_resp.json()
                    state = status.get("status")
                    if state == "COMPLETED":
                        break
                    elif state in ("FAILED", "CANCELLED"):
                        logger.error(f"fal.ai job {state}: {status}")
                        return None
                    elapsed = int(time.time() - start)
                    logger.info(f"fal.ai ref-to-video generating... ({elapsed}s elapsed)")
                except Exception as poll_err:
                    logger.warning(f"fal.ai poll error: {poll_err}")
            else:
                logger.error(f"fal.ai ref-to-video timed out after {max_poll_time}s")
                return None

            # Fetch result
            result_resp = requests.get(response_url, headers=self.headers, timeout=30)
            if result_resp.status_code != 200:
                logger.error(f"fal.ai result fetch failed ({result_resp.status_code}): {result_resp.text[:500]}")
                result_resp.raise_for_status()
            result = result_resp.json()
            video_url = result.get("video", {}).get("url")

            if not video_url:
                logger.error(f"fal.ai no video URL in result: {list(result.keys())}")
                return None

            logger.info(f"fal.ai ref-to-video completed: {video_url[:80]}...")

            # Re-upload to GCS for durable URL
            if self.gcs:
                try:
                    key = f"fal_videos/fal_ref2vid_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
                    gcs_url = self.gcs.upload_video_from_url(video_url, key)
                    if gcs_url:
                        logger.info(f"fal.ai video re-uploaded to GCS: {gcs_url[:60]}...")
                        return gcs_url
                except Exception as gcs_err:
                    logger.warning(f"fal.ai GCS re-upload failed: {gcs_err}, using original URL")

            return video_url

        except Exception as e:
            logger.error(f"fal.ai ref-to-video failed: {e}")
            return None
