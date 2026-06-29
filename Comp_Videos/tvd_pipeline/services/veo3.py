"""Veo 3 video generation service (Google Vertex AI)."""

import os
import time
import base64
import random
import logging
from typing import Dict, Optional, Tuple

import requests

from tvd_pipeline.config import Config
from tvd_pipeline.data_loader import get_veo3_config
from tvd_pipeline.utils import snap_duration

config = Config()
logger = logging.getLogger(__name__)


class VeoRAIBlockedError(Exception):
    """Raised when Veo blocks a video due to RAI content filtering."""
    def __init__(self, reason: str = "unknown", support_code: str = ""):
        self.reason = reason
        self.support_code = support_code
        super().__init__(f"RAI blocked: {reason}")

class VeoPromptBlockedError(Exception):
    """Raised when Veo rejects the prompt text (code 3 — usage guidelines violation)."""
    def __init__(self, message: str = "", support_code: str = ""):
        self.original_message = message
        self.support_code = support_code
        super().__init__(f"Prompt blocked: {message}")

class Veo3Service:
    """Service for video generation using Google's Veo via Vertex AI.
    
    Supports Veo 3.0 (REST predictLongRunning) and Veo 3.1 (google-genai SDK).
    Supports text-to-video and image-to-video generation.
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
        self._use_genai_sdk = "3.1" in self.model
        self._genai_client = None
        self._sa_creds = None  # Service account credentials for REST auth (Veo 3.0)
        
        # Resolve service account file — VEO_SERVICE_ACCOUNT_FILE takes priority so a
        # Veo-specific SA (different project) can be used without affecting GCS auth.
        _sa_path = (
            os.environ.get("VEO_SERVICE_ACCOUNT_FILE")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or os.environ.get("SERVICE_ACCOUNT_FILE")
            or getattr(config, "SERVICE_ACCOUNT_FILE", "service_account.json")
        )
        if _sa_path and os.path.isfile(_sa_path):
            try:
                from google.oauth2 import service_account as _sa_mod
                self._sa_creds = _sa_mod.Credentials.from_service_account_file(
                    _sa_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                # Use the SA's own project when GOOGLE_CLOUD_PROJECT is not explicitly
                # set — avoids cross-project auth failures when the default VEO3_PROJECT_ID
                # doesn't match the service account's project.
                if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
                    try:
                        import json as _json
                        with open(_sa_path) as _f:
                            _sa_proj = _json.load(_f).get("project_id")
                        if _sa_proj and _sa_proj != self.project_id:
                            logger.info(
                                f"Veo: using SA project '{_sa_proj}' "
                                f"(config had '{self.project_id}')"
                            )
                            self.project_id = _sa_proj
                    except Exception:
                        pass
                logger.info(f"Veo: loaded service account credentials from {_sa_path}")
            except Exception as _sa_err:
                logger.warning(f"Veo: could not load service account from {_sa_path}: {_sa_err}")

        # Set up REST endpoints after project_id is potentially updated by SA lookup above
        self.generate_endpoint = config.VEO3_GENERATE_ENDPOINT.format(
            project_id=self.project_id,
            model=self.model
        )
        self.poll_endpoint = config.VEO3_POLL_ENDPOINT.format(
            project_id=self.project_id,
            model=self.model
        )
        self.headers = {"Content-Type": "application/json"}

        if self._use_genai_sdk:
            try:
                from google import genai as google_genai
                # Veo 3.1 predictLongRunning requires OAuth2 — API keys are NOT supported.
                # Always use Vertex AI mode with ADC (service account) for the direct provider.
                #
                # google.auth.default() returns scopeless credentials from the service
                # account file, which fail with "invalid_scope" when the SDK tries to
                # refresh them.  Load explicit scoped credentials instead.
                # Reuse the already-resolved _sa_creds (loaded from VEO_SERVICE_ACCOUNT_FILE
                # or GOOGLE_APPLICATION_CREDENTIALS earlier in __init__).
                creds = self._sa_creds

                client_kw = {
                    "vertexai": True,
                    "project": self.project_id,
                    "location": getattr(config, "VEO31_LOCATION", "us-central1"),
                }
                if creds:
                    client_kw["credentials"] = creds
                self._genai_client = google_genai.Client(**client_kw)
                self.initialized = True
                logger.info(f"Veo video service initialized ({self.model}) [google-genai SDK]")
            except Exception as e:
                logger.warning(f"Veo 3.1 SDK init failed: {e}. Install with: pip install --upgrade google-genai")
                # Fall back to REST mode — reference-to-video and standard generation
                # can still work via REST API with service account auth.
                if self._sa_creds or self.api_key:
                    self._use_genai_sdk = False
                    self.initialized = True
                    logger.info(f"Veo video service initialized ({self.model}) [REST API fallback, genai SDK unavailable]")
                else:
                    logger.warning("Veo 3.1 not available - no genai SDK, no service account, and no VERTEX_AI_API_KEY")
                return
        else:
            # Veo 3.0 — REST API only; prefer service account auth, fall back to API key
            if not self._sa_creds and not self.api_key:
                logger.warning("Veo not available - no service account credentials and no VERTEX_AI_API_KEY")
                return
            auth_method = "service account" if self._sa_creds else "API key"
            self.initialized = True
            logger.info(f"Veo video service initialized ({self.model}) [REST API, {auth_method}]")

    def _get_rest_auth_headers(self) -> dict:
        """Return Authorization headers for Vertex AI REST calls.

        Prefers service account OAuth2 Bearer token; falls back to API key URL auth
        (caller must still append ?key=... when using the fallback).
        """
        if self._sa_creds:
            try:
                import google.auth.transport.requests as _ga_tr
                req = _ga_tr.Request()
                self._sa_creds.refresh(req)
                return {"Authorization": f"Bearer {self._sa_creds.token}"}
            except Exception as _te:
                logger.warning(f"Veo: failed to refresh SA token: {_te}")
        return {}

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
        poll_t0 = time.perf_counter()
        from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

        log_external_api_call("vertex_veo", "operation_poll", method="POST", detail="poll_loop")

        while time.time() - start_time < config.VEO3_MAX_POLL_TIME:
            poll_count += 1

            try:
                payload = {
                    "operationName": operation_name
                }

                url = f"{self.poll_endpoint}?key={self.api_key}"
                auth_headers = self._get_rest_auth_headers()
                req_headers = {**self.headers, **auth_headers}
                if auth_headers:
                    # Using service account Bearer token — no ?key= needed
                    url = self.poll_endpoint
                response = requests.post(
                    url,
                    headers=req_headers,
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
                    err_in = result.get("error")
                    err_s = ""
                    if err_in:
                        err_s = str(err_in.get("message", err_in))[:120]
                    log_external_api_result(
                        "vertex_veo",
                        "operation_poll",
                        duration_ms=int((time.perf_counter() - poll_t0) * 1000),
                        method="POST",
                        http_status=response.status_code,
                        ok=not bool(err_in),
                        error=err_s,
                        detail=f"polls={poll_count}",
                    )
                    return result

                # Still running, wait and poll again
                elapsed = int(time.time() - start_time)
                logger.info(f"Veo 3 video generation in progress... ({elapsed}s elapsed)")
                time.sleep(config.VEO3_POLL_INTERVAL)

            except Exception as e:
                logger.warning(f"Error polling Veo 3 operation: {e}")
                time.sleep(config.VEO3_POLL_INTERVAL)

        logger.error(f"Veo 3 operation timed out after {config.VEO3_MAX_POLL_TIME}s")
        log_external_api_result(
            "vertex_veo",
            "operation_poll",
            duration_ms=int((time.perf_counter() - poll_t0) * 1000),
            method="POST",
            ok=False,
            error="timeout",
            detail=f"polls={poll_count}",
        )
        return None
    
    def generate_video(
        self,
        prompt: str,
        image_url: str = None,
        duration: float = 5.0,
        resolution: str = None,
        reference_image_urls: list = None,
        api_method: str = None,
    ) -> Optional[str]:
        """Generate a video using Veo 3.0 (REST) or Veo 3.1 (google-genai SDK).

        Args:
            prompt: Text prompt for video generation (motion/action description).
            image_url: Optional URL of an image to use as the first frame.
            duration: Desired video duration in seconds.
            resolution: Video resolution ('720p' or '1080p').
            reference_image_urls: Optional list of reference image URLs for
                reference-to-video mode (up to 3 "asset" references). When
                provided, forces the REST path (genai SDK doesn't support
                referenceImages).
            api_method: Explicit API method preference from models.json
                ("rest" or "sdk"). When set, overrides the default SDK/REST
                decision. reference_image_urls still forces REST as a safety net.

        Returns:
            URL of the generated video (from GCS), or None if failed.
        """
        if not self.initialized:
            logger.error("Veo service not initialized")
            return None

        # Decide SDK vs REST: explicit api_method > reference_image_urls > default
        use_rest = (
            api_method == "rest"
            or reference_image_urls
            or not self._use_genai_sdk
            or not self._genai_client
        )

        if use_rest:
            return self._generate_video_rest(
                prompt, image_url, duration, resolution,
                reference_image_urls=reference_image_urls,
            )

        # ---- Veo 3.1: google-genai SDK path ----
        return self._generate_video_genai(prompt, image_url, duration, resolution)
    
    def _generate_video_genai(
        self,
        prompt: str,
        image_url: str = None,
        duration: float = 5.0,
        resolution: str = None,
        _retry_count: int = 0
    ) -> Optional[str]:
        """Generate video via the google-genai SDK (Veo 3.1)."""
        retry_cfg = get_veo3_config().get("retry", {})
        _max_retries = retry_cfg.get("high_load_max_retries", 20)
        _initial_delay = retry_cfg.get("high_load_initial_delay", 15)
        _backoff_mult = retry_cfg.get("high_load_backoff_multiplier", 2.0)
        _max_delay = retry_cfg.get("high_load_max_delay", 300)
        try:
            from google.genai import types as genai_types

            duration_sec = snap_duration(self.model, int(round(duration)))
            res = resolution or config.VEO3_DEFAULT_RESOLUTION
            
            # Build source
            if image_url:
                image_data = self._fetch_image_as_base64(image_url)
                if image_data:
                    b64_data, mime = image_data
                    source = genai_types.GenerateVideosSource(
                        image=genai_types.Image(
                            image_bytes=base64.b64decode(b64_data),
                            mime_type=mime
                        ),
                        prompt=prompt
                    )
                    logger.info("Using image as first frame for Veo 3.1")
                else:
                    source = genai_types.GenerateVideosSource(prompt=prompt)
            else:
                source = genai_types.GenerateVideosSource(prompt=prompt)
            
            veo_cfg = get_veo3_config()
            gen_config = genai_types.GenerateVideosConfig(
                duration_seconds=duration_sec,
                resolution=res,
                **veo_cfg["veo3_1"],
            )
            
            logger.info(f"Starting Veo 3.1 video generation ({duration_sec}s, {res})...")
            operation = self._genai_client.models.generate_videos(
                model=self.model, source=source, config=gen_config
            )
            
            # Poll until done — adaptive interval: starts at 3s, grows to 8s after 30s elapsed
            start = time.time()
            poll_interval = config.VEO3_POLL_INTERVAL
            while not operation.done:
                elapsed = int(time.time() - start)
                if elapsed > config.VEO3_MAX_POLL_TIME:
                    logger.error(f"Veo 3.1 timed out after {elapsed}s")
                    return None
                logger.info(f"Veo 3.1 generating... ({elapsed}s elapsed)")
                time.sleep(poll_interval)
                if elapsed > 30:
                    poll_interval = min(poll_interval + 1, 8)
                operation = self._genai_client.operations.get(operation)
            
            response = operation.result
            if not response:
                logger.error("Veo 3.1 returned empty result")
                return None
            
            # Detect RAI content filtering (GenAI SDK path)
            rai_count = getattr(response, "rai_media_filtered_count", 0)
            if rai_count:
                reasons = getattr(response, "rai_media_filtered_reasons", ["unknown"])
                reason_str = ", ".join(reasons) if isinstance(reasons, list) else str(reasons)
                logger.warning(f"Veo 3.1 RAI blocked: {rai_count} filtered (reasons: {reason_str})")
                raise VeoRAIBlockedError(reason=reason_str)

            generated_videos = response.generated_videos
            if not generated_videos:
                logger.error("Veo 3.1: No videos generated")
                return None
            
            video_obj = generated_videos[0].video
            if not video_obj:
                logger.error("Veo 3.1: Video object is None")
                return None
            
            # Get raw bytes from the video object and upload to GCS
            video_bytes = None
            if hasattr(video_obj, "video_bytes") and video_obj.video_bytes:
                video_bytes = video_obj.video_bytes
            elif hasattr(video_obj, "uri") and video_obj.uri:
                # GCS URI returned by the SDK
                gcs_uri = video_obj.uri
                if gcs_uri.startswith("gs://"):
                    public_url = gcs_uri.replace("gs://", "https://storage.googleapis.com/")
                    logger.info(f"✅ Veo 3.1 video: {public_url[:60]}...")
                    return public_url
            
            if video_bytes and self.gcs_storage_service:
                key = f"veo3_videos/veo31_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
                video_url = self.gcs_storage_service.upload_video_bytes(
                    video_data=video_bytes,
                    key_name=key,
                    make_public=True
                )
                if video_url:
                    logger.info(f"✅ Veo 3.1 video uploaded: {video_url[:60]}...")
                    return video_url
            
            logger.error("Could not extract video from Veo 3.1 response")
            return None
            
        except (VeoRAIBlockedError, VeoPromptBlockedError):
            raise  # Let caller handle retry
        except Exception as e:
            err_msg = str(e).lower()
            if "usage guidelines" in err_msg or "violate" in err_msg:
                logger.warning(f"Veo 3.1 prompt blocked: {e}")
                raise VeoPromptBlockedError(message=str(e))
            if ("resource" in err_msg and "exhausted" in err_msg) or "high load" in err_msg or "429" in str(e):
                if _retry_count < _max_retries:
                    delay = min(_initial_delay * (_backoff_mult ** _retry_count), _max_delay)
                    logger.warning(f"Veo 3.1 high load, retrying in {delay:.0f}s (attempt {_retry_count + 1}/{_max_retries})...")
                    time.sleep(delay)
                    return self._generate_video_genai(prompt, image_url, duration, resolution, _retry_count + 1)
                else:
                    logger.error(f"Veo 3.1 high load after {_max_retries} retries, giving up")
                    return None
            logger.error(f"Error generating video with Veo 3.1 SDK: {e}")
            return None

    def _generate_video_rest(
        self,
        prompt: str,
        image_url: str = None,
        duration: float = 5.0,
        resolution: str = None,
        _retry_count: int = 0,
        reference_image_urls: list = None,
    ) -> Optional[str]:
        """Generate video via legacy REST API (Veo 3.0/3.1).

        When *reference_image_urls* is provided, uses referenceImages payload
        instead of single image-to-video. Vertex R2V only supports 8s duration.
        """
        retry_cfg = get_veo3_config().get("retry", {})
        VEO_HIGH_LOAD_MAX_RETRIES = retry_cfg.get("high_load_max_retries", 20)
        VEO_HIGH_LOAD_INITIAL_DELAY = retry_cfg.get("high_load_initial_delay", 15)
        VEO_HIGH_LOAD_BACKOFF_MULT = retry_cfg.get("high_load_backoff_multiplier", 2.0)
        VEO_HIGH_LOAD_MAX_DELAY = retry_cfg.get("high_load_max_delay", 300)
        try:
            instance = {"prompt": prompt}

            if reference_image_urls:
                # Reference-to-video mode: encode each image as a referenceImage
                ref_images = []
                for ref_url in reference_image_urls:
                    img_data = self._fetch_image_as_base64(ref_url)
                    if img_data:
                        b64, mime = img_data
                        ref_images.append({
                            "image": {"bytesBase64Encoded": b64, "mimeType": mime},
                            "referenceType": "asset",
                        })
                if ref_images:
                    instance["referenceImages"] = ref_images
                    logger.info(f"Using {len(ref_images)} reference images for Veo R2V")
                else:
                    logger.warning("No reference images could be fetched, falling back to text-only")
            elif image_url:
                image_data = self._fetch_image_as_base64(image_url)
                if image_data:
                    base64_data, mime_type = image_data
                    instance["image"] = {
                        "bytesBase64Encoded": base64_data,
                        "mimeType": mime_type
                    }
                    logger.info("Using image as first frame for Veo 3")

            duration_sec = snap_duration(self.model, int(round(duration)))
            veo_cfg = get_veo3_config()
            params = {
                "resolution": resolution or config.VEO3_DEFAULT_RESOLUTION,
                "durationSeconds": duration_sec,
                **veo_cfg["veo3_0"],
            }
            if reference_image_urls:
                # R2V needs person_generation and forces 8s duration
                params["personGeneration"] = "allow_all"
                params["durationSeconds"] = 8
            payload = {
                "instances": [instance],
                "parameters": params,
            }

            logger.info(f"Starting Veo 3 video generation ({duration_sec}s)...")
            auth_headers = self._get_rest_auth_headers()
            req_headers = {**self.headers, **auth_headers}
            if auth_headers:
                url = self.generate_endpoint
            else:
                url = f"{self.generate_endpoint}?key={self.api_key}"
            from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

            log_external_api_call("vertex_veo", "predict_long_running", method="POST", model="veo-3")
            _pred_t0 = time.perf_counter()
            response = requests.post(
                url,
                headers=req_headers,
                json=payload,
                timeout=120
            )
            _pred_ms = int((time.perf_counter() - _pred_t0) * 1000)

            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as e:
                log_external_api_result(
                    "vertex_veo",
                    "predict_long_running",
                    duration_ms=_pred_ms,
                    method="POST",
                    model="veo-3",
                    http_status=e.response.status_code if e.response else None,
                    ok=False,
                    error=(e.response.text or "")[:200] if e.response else str(e)[:200],
                )
                if e.response is not None:
                    logger.error(f"Veo 3 API error: {e.response.status_code} - {e.response.text[:200]}")
                else:
                    logger.error(f"Veo 3 API error: {e}")
                return None
            log_external_api_result(
                "vertex_veo",
                "predict_long_running",
                duration_ms=_pred_ms,
                method="POST",
                model="veo-3",
                http_status=response.status_code,
                ok=True,
            )

            result = response.json()
            operation_name = result.get("name")
            if not operation_name:
                logger.error("No operation name in Veo 3 response")
                return None

            logger.info(f"Veo 3 operation started: {operation_name}")
            final_result = self._poll_operation(operation_name)
            if not final_result:
                return None

            # Detect prompt-level block (code 3 — usage guidelines violation)
            error_data = final_result.get("error")
            if error_data and error_data.get("code") == 3:
                msg = error_data.get("message", "")
                logger.warning(f"Veo 3 prompt blocked (code 3): {msg[:200]}")
                raise VeoPromptBlockedError(message=msg)

            # Detect high load (code 8) — retry with exponential backoff
            if error_data and error_data.get("code") == 8:
                if _retry_count < VEO_HIGH_LOAD_MAX_RETRIES:
                    delay = min(VEO_HIGH_LOAD_INITIAL_DELAY * (VEO_HIGH_LOAD_BACKOFF_MULT ** _retry_count), VEO_HIGH_LOAD_MAX_DELAY)
                    logger.warning(f"Veo 3 high load (code 8), retrying in {delay:.0f}s (attempt {_retry_count + 1}/{VEO_HIGH_LOAD_MAX_RETRIES})...")
                    time.sleep(delay)
                    return self._generate_video_rest(prompt, image_url, duration, resolution, _retry_count + 1, reference_image_urls=reference_image_urls)
                else:
                    logger.error(f"Veo 3 high load (code 8) after {VEO_HIGH_LOAD_MAX_RETRIES} retries, giving up")
                    return None

            response_data = final_result.get("response", {})

            # Detect RAI content filtering
            rai_count = response_data.get("raiMediaFilteredCount", 0)
            if rai_count:
                reasons = response_data.get("raiMediaFilteredReasons", ["unknown"])
                reason_str = ", ".join(reasons) if isinstance(reasons, list) else str(reasons)
                logger.warning(f"Veo 3 RAI blocked: {rai_count} filtered (reasons: {reason_str})")
                raise VeoRAIBlockedError(reason=reason_str)

            videos = response_data.get("videos", [])

            if not videos:
                logger.error("No videos in Veo 3 response")
                logger.error(f"Full response_data: {response_data}")
                return None
            
            video_info = videos[0]
            gcs_uri = video_info.get("gcsUri")
            
            if gcs_uri and gcs_uri.startswith("gs://"):
                public_url = gcs_uri.replace("gs://", "https://storage.googleapis.com/")
                logger.info(f"Veo 3 video generated: {public_url[:60]}...")
                return public_url
            
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
            
        except (VeoRAIBlockedError, VeoPromptBlockedError):
            raise  # Let caller handle retry
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
