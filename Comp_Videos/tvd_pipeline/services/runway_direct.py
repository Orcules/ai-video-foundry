"""Direct Runway API integration for Gen4 Turbo and Gen 4.5."""
import requests
import time
import logging

from tvd_pipeline.utils import snap_duration

logger = logging.getLogger(__name__)

# Runway rejects images with aspect ratio > 2:1
_MAX_ASPECT_RATIO = 2.0


class RunwayDirectService:
    BASE_URL = "https://api.dev.runwayml.com/v1"

    def __init__(self, api_key: str, gcs_storage_service=None):
        self.api_key = api_key
        self.gcs = gcs_storage_service
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Runway-Version": "2024-11-06",
        }

    def _prepare_image(self, image_url: str) -> str:
        """Ensure image meets Runway's max aspect ratio. Center-crop if needed."""
        try:
            resp = requests.get(image_url, timeout=15)
            resp.raise_for_status()
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(resp.content))
            w, h = img.size
            ratio = max(w / h, h / w)
            if ratio <= _MAX_ASPECT_RATIO:
                return image_url
            # Center-crop the long dimension to 2:1
            if w > h:
                new_w = int(h * _MAX_ASPECT_RATIO)
                left = (w - new_w) // 2
                img = img.crop((left, 0, left + new_w, h))
            else:
                new_h = int(w * _MAX_ASPECT_RATIO)
                top = (h - new_h) // 2
                img = img.crop((0, top, w, top + new_h))
            logger.info(f"Cropped image for Runway: {w}x{h} -> {img.size[0]}x{img.size[1]}")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            if self.gcs:
                url = self.gcs.upload_image_bytes(buf.getvalue(), f"runway_prep_{int(time.time())}.jpg")
                if url:
                    return url
            logger.warning("GCS upload of cropped image failed, using original")
            return image_url
        except Exception as e:
            logger.warning(f"Image prep for Runway failed, using original: {e}")
            return image_url

    def generate_video(
        self,
        image_url: str,
        prompt: str,
        duration: int = 5,
        model: str = "gen4_turbo",
        resolution: int = 720,
        video_model: str = None,
    ) -> str | None:
        """Generate video from image + prompt using Runway direct API."""
        if video_model:
            duration = snap_duration(video_model, int(round(duration)))
        image_url = self._prepare_image(image_url)
        payload = {
            "model": model,
            "promptImage": image_url,
            "promptText": prompt,
            "duration": duration,
            "ratio": "720:1280" if resolution <= 720 else "1080:1920",
            "watermark": False,
        }
        try:
            resp = requests.post(
                f"{self.BASE_URL}/image_to_video",
                headers=self.headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            task_id = resp.json()["id"]
            return self._poll(task_id)
        except Exception as e:
            logger.error(f"Runway Direct generation failed: {e}")
            return None

    def _poll(self, task_id: str, timeout: int = 600) -> str | None:
        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = requests.get(
                    f"{self.BASE_URL}/tasks/{task_id}",
                    headers=self.headers,
                    timeout=30,
                )
                data = resp.json()
                if data["status"] == "SUCCEEDED":
                    output_url = data["output"][0]
                    if self.gcs:
                        gcs_url = self.gcs.upload_video_from_url(output_url, f"runway_direct_{task_id}.mp4")
                        return gcs_url or output_url
                    return output_url
                elif data["status"] == "FAILED":
                    logger.error(f"Runway Direct task {task_id} failed: {data.get('failure')}")
                    return None
            except Exception as e:
                logger.warning(f"Runway Direct poll error: {e}")
            time.sleep(10)
        logger.error(f"Runway Direct task {task_id} timed out")
        return None
