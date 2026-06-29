"""Mux video upload service — uploads videos to Mux for streaming via CDN.

Uses the Mux direct upload API (REST, no edge function needed):
1. POST /video/v1/uploads → create direct upload, get upload URL + ID
2. PUT {upload_url} with video binary → upload the file
3. Poll GET /video/v1/uploads/{id} → wait for asset_id
4. GET /video/v1/assets/{asset_id} → get playback_id
5. Stream URL: https://stream.mux.com/{playback_id}.m3u8
6. MP4 URL:   https://stream.mux.com/{playback_id}/{rendition_filename} (from static_renditions API)

Uses the Static Renditions API (replaces deprecated mp4_support).
For 4K tiers, sets max_resolution_tier=2160p and requests both highest + 1080p renditions.
"""

import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MUX_API_URL = "https://api.mux.com/video/v1"


def _mux_settings_for_tier(output_resolution: str = "720p_low") -> dict:
    """Return Mux new_asset_settings based on the output resolution tier."""
    base = {
        "playback_policies": ["public"],
        "video_quality": "basic",
        "static_renditions": [{"resolution": "highest"}],
    }
    if output_resolution.startswith("4k"):
        base["max_resolution_tier"] = "2160p"
        base["static_renditions"].append({"resolution": "1080p"})
    return base


class MuxUploadService:
    """Upload videos to Mux and return playback info."""

    def __init__(self, token_id: str, token_secret: str):
        self.auth = (token_id, token_secret)

    def upload_video(self, video_url: str, output_resolution: str = "720p_low") -> dict:
        """Download video from URL, upload to Mux, return playback info.

        Returns: {"final_asset_id", "final_playback_id", "mux_stream_url" (.m3u8), "mux_mp4_url" (.mp4)}
        Raises on failure.
        """
        # 1. Create direct upload
        logger.info("Mux: creating direct upload session...")
        settings = _mux_settings_for_tier(output_resolution)
        resp = requests.post(
            f"{MUX_API_URL}/uploads",
            auth=self.auth,
            json={
                "new_asset_settings": settings,
                "cors_origin": "*",
            },
            timeout=30,
        )
        resp.raise_for_status()
        upload_data = resp.json()["data"]
        upload_url = upload_data["url"]
        upload_id = upload_data["id"]
        logger.info(f"Mux: upload session created — id={upload_id}")

        # 2. Download video from source URL
        logger.info(f"Mux: downloading video from source ({video_url[:80]}...)")
        dl_resp = requests.get(video_url, timeout=120)
        dl_resp.raise_for_status()
        video_bytes = dl_resp.content
        logger.info(f"Mux: downloaded {len(video_bytes)} bytes")

        # 3. PUT video to upload URL
        logger.info("Mux: uploading video to Mux...")
        put_resp = requests.put(
            upload_url,
            data=video_bytes,
            headers={"Content-Type": "video/mp4"},
            timeout=120,
        )
        put_resp.raise_for_status()
        logger.info("Mux: upload complete, waiting for processing...")

        # 4. Poll for asset_id (every 3s, max 60 attempts = 3 minutes)
        asset_id = None
        for attempt in range(60):
            time.sleep(3)
            check_resp = requests.get(
                f"{MUX_API_URL}/uploads/{upload_id}",
                auth=self.auth,
                timeout=15,
            )
            if not check_resp.ok:
                continue
            upload_status = check_resp.json()["data"]
            asset_id = upload_status.get("asset_id")
            if asset_id:
                logger.info(f"Mux: asset ready — asset_id={asset_id} (attempt {attempt + 1})")
                break

        if not asset_id:
            raise RuntimeError(f"Mux: timed out waiting for asset_id (upload_id={upload_id})")

        # 5. Poll asset until static renditions are ready (per-file status check)
        asset_data = None
        rend_files = []
        for attempt in range(60):
            asset_resp = requests.get(
                f"{MUX_API_URL}/assets/{asset_id}",
                auth=self.auth,
                timeout=15,
            )
            asset_resp.raise_for_status()
            asset_data = asset_resp.json()["data"]
            static_rend = asset_data.get("static_renditions") or {}
            rend_files = static_rend.get("files", []) if isinstance(static_rend, dict) else []
            all_ready = rend_files and all(f.get("status") == "ready" for f in rend_files)
            if asset_data.get("status") == "ready" and all_ready:
                logger.info(f"Mux: asset + static renditions ready (attempt {attempt + 1})")
                break
            time.sleep(3)
        else:
            raise RuntimeError(f"Mux: timed out waiting for static renditions (asset_id={asset_id})")

        playback_ids = asset_data.get("playback_ids", [])
        if not playback_ids:
            raise RuntimeError(f"Mux: no playback_ids on asset {asset_id}")
        playback_id = playback_ids[0]["id"]

        mp4_name = "highest.mp4"

        stream_url = f"https://stream.mux.com/{playback_id}.m3u8"
        mp4_url = f"https://stream.mux.com/{playback_id}/{mp4_name}"
        logger.info(f"Mux: upload complete — playback_id={playback_id}, stream={stream_url}, mp4={mp4_url}")

        return {
            "final_asset_id": asset_id,
            "final_playback_id": playback_id,
            "mux_stream_url": stream_url,
            "mux_mp4_url": mp4_url,
        }

    def upload_video_async(self, video_url: str, job_id: str, output_resolution: str = "720p_low") -> dict:
        """Download video from URL, upload to Mux, return immediately without polling.

        Embeds job_id in Mux passthrough so the webhook can correlate the asset.
        Returns: {"upload_id": str, "status": "uploading"}
        Raises on failure.
        """
        # 1. Create direct upload with job_id in passthrough
        logger.info("Mux async: creating direct upload session...")
        settings = _mux_settings_for_tier(output_resolution)
        settings["passthrough"] = json.dumps({"job_id": job_id})
        resp = requests.post(
            f"{MUX_API_URL}/uploads",
            auth=self.auth,
            json={
                "new_asset_settings": settings,
                "cors_origin": "*",
            },
            timeout=30,
        )
        resp.raise_for_status()
        upload_data = resp.json()["data"]
        upload_url = upload_data["url"]
        upload_id = upload_data["id"]
        logger.info(f"Mux async: upload session created — id={upload_id}")

        # 2. Download video from source URL
        logger.info(f"Mux async: downloading video from source ({video_url[:80]}...)")
        dl_resp = requests.get(video_url, timeout=120)
        dl_resp.raise_for_status()
        video_bytes = dl_resp.content
        logger.info(f"Mux async: downloaded {len(video_bytes)} bytes")

        # 3. PUT video to upload URL
        logger.info("Mux async: uploading video to Mux...")
        put_resp = requests.put(
            upload_url,
            data=video_bytes,
            headers={"Content-Type": "video/mp4"},
            timeout=120,
        )
        put_resp.raise_for_status()
        logger.info(f"Mux async: upload PUT complete — upload_id={upload_id}, returning immediately")

        return {"upload_id": upload_id, "status": "uploading"}

    def upload_local_file(self, file_path: str, output_resolution: str = "720p_low") -> dict:
        """Upload a local video file to Mux. Same return format as upload_video()."""
        # 1. Create direct upload
        logger.info("Mux: creating direct upload session for local file...")
        settings = _mux_settings_for_tier(output_resolution)
        resp = requests.post(
            f"{MUX_API_URL}/uploads",
            auth=self.auth,
            json={
                "new_asset_settings": settings,
                "cors_origin": "*",
            },
            timeout=30,
        )
        resp.raise_for_status()
        upload_data = resp.json()["data"]
        upload_url = upload_data["url"]
        upload_id = upload_data["id"]

        # 2. Read and upload local file
        with open(file_path, "rb") as f:
            video_bytes = f.read()
        logger.info(f"Mux: uploading {len(video_bytes)} bytes from {file_path}")

        put_resp = requests.put(
            upload_url,
            data=video_bytes,
            headers={"Content-Type": "video/mp4"},
            timeout=120,
        )
        put_resp.raise_for_status()

        # 3. Poll for asset_id
        asset_id = None
        for attempt in range(60):
            time.sleep(3)
            check_resp = requests.get(
                f"{MUX_API_URL}/uploads/{upload_id}",
                auth=self.auth,
                timeout=15,
            )
            if not check_resp.ok:
                continue
            upload_status = check_resp.json()["data"]
            asset_id = upload_status.get("asset_id")
            if asset_id:
                break

        if not asset_id:
            raise RuntimeError(f"Mux: timed out waiting for asset_id (upload_id={upload_id})")

        # 4. Poll asset until static renditions are ready (per-file status check)
        asset_data = None
        rend_files = []
        for attempt in range(60):
            asset_resp = requests.get(
                f"{MUX_API_URL}/assets/{asset_id}",
                auth=self.auth,
                timeout=15,
            )
            asset_resp.raise_for_status()
            asset_data = asset_resp.json()["data"]
            static_rend = asset_data.get("static_renditions") or {}
            rend_files = static_rend.get("files", []) if isinstance(static_rend, dict) else []
            all_ready = rend_files and all(f.get("status") == "ready" for f in rend_files)
            if asset_data.get("status") == "ready" and all_ready:
                logger.info(f"Mux: asset + static renditions ready (attempt {attempt + 1})")
                break
            time.sleep(3)
        else:
            raise RuntimeError(f"Mux: timed out waiting for static renditions (asset_id={asset_id})")

        playback_ids = asset_data.get("playback_ids", [])
        if not playback_ids:
            raise RuntimeError(f"Mux: no playback_ids on asset {asset_id}")
        playback_id = playback_ids[0]["id"]

        mp4_name = "highest.mp4"

        stream_url = f"https://stream.mux.com/{playback_id}.m3u8"
        mp4_url = f"https://stream.mux.com/{playback_id}/{mp4_name}"
        logger.info(f"Mux: local file upload complete — playback_id={playback_id}, mp4={mp4_url}")

        return {
            "final_asset_id": asset_id,
            "final_playback_id": playback_id,
            "mux_stream_url": stream_url,
            "mux_mp4_url": mp4_url,
        }
