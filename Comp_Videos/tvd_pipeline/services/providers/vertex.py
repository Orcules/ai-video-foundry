"""Vertex AI provider client -- auth, retry, and raw API access."""

import os
import time
import logging
from typing import Any, Dict, Optional

import requests

from tvd_pipeline.config import Config

config = Config()
logger = logging.getLogger(__name__)


class VertexAIProvider:
    """Thin Vertex AI client implementing the LLMProvider protocol."""

    GEMINI_REQUEST_TIMEOUT = 120
    GEMINI_MAX_RETRIES = 3
    GEMINI_RETRY_DELAYS = (5, 15, 30)  # seconds between retries

    def __init__(self, api_key: str = None, gcs_storage_service=None):
        """Initialize Vertex AI provider.

        Args:
            api_key: Optional Kie.ai API key (kept for backward compatibility).
            gcs_storage_service: GCS storage service for uploading videos to get public URLs.
        """
        self.gcs_storage_service = gcs_storage_service
        self.initialized = False

        self.vertex_api_key = config.VERTEX_AI_API_KEY
        self.model = config.VERTEX_AI_MODEL
        self.project_id = config.VERTEX_AI_PROJECT_ID
        self.location = config.VERTEX_AI_LOCATION
        # When API key is set: Vertex with ?key= (same as Gemini Image service). Else: Vertex with OAuth Bearer.
        self._use_api_key = bool(self.vertex_api_key)
        self._endpoint_template = (
            f"https://aiplatform.googleapis.com/v1/projects/{config.VERTEX_AI_PROJECT_ID}/locations/{config.VERTEX_AI_LOCATION}/publishers/google/models"
        )

        self.kie_api_key = api_key

        if self._use_api_key:
            pass  # Key provided, use Vertex with key in URL
        elif not self._get_vertex_token_from_adc():
            logger.warning("Gemini not available - set VERTEX_AI_API_KEY or run: gcloud auth application-default login")
            return

        self.initialized = True
        auth_note = "API key" if self._use_api_key else "OAuth"
        logger.info(f"VertexAIProvider initialized (Vertex AI - {self.model}, {auth_note})")

    def _get_vertex_token_from_adc(self) -> Optional[str]:
        """Get access token from Application Default Credentials (no VERTEX_AI_API_KEY needed)."""
        try:
            from google.auth import default
            from google.auth.transport.requests import Request
            creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(Request())
            return creds.token
        except Exception:
            return None

    def _get_vertex_headers(self) -> Dict[str, str]:
        """Return headers: OAuth Bearer when no API key; else Content-Type only (key in URL)."""
        headers = {"Content-Type": "application/json"}
        if not self._use_api_key:
            token = self._get_vertex_token_from_adc()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get_vertex_url(self, model: str) -> str:
        """Return Vertex generateContent URL; when API key set, append ?key= (same as Gemini Image)."""
        base = f"{self._endpoint_template}/{model}:generateContent"
        if self._use_api_key:
            return f"{base}?key={self.vertex_api_key}"
        return base

    def _vertex_post_with_retry(
        self, url: str, headers: Dict[str, str], json_payload: Dict[str, Any],
        timeout: int = None, max_retries: int = None
    ) -> requests.Response:
        """POST to Vertex AI with retries on timeout/connection errors and longer timeout."""
        timeout = timeout if timeout is not None else self.GEMINI_REQUEST_TIMEOUT
        max_retries = max_retries if max_retries is not None else self.GEMINI_MAX_RETRIES
        delays = self.GEMINI_RETRY_DELAYS
        last_exc = None
        for attempt in range(max_retries):
            try:
                from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

                model_id = ""
                try:
                    idx = url.find("/models/")
                    if idx >= 0:
                        rest = url[idx + 8 :]
                        model_id = rest.split(":")[0].split("?")[0]
                except Exception:
                    pass
                log_external_api_call(
                    "vertex_gemini",
                    "generateContent",
                    method="POST",
                    model=model_id or self.model,
                )
                _t0 = time.perf_counter()
                response = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
                ms = int((time.perf_counter() - _t0) * 1000)
                err = ""
                if not response.ok:
                    try:
                        err = (response.text or "")[:300]
                    except Exception:
                        err = "non-ok"
                log_external_api_result(
                    "vertex_gemini",
                    "generateContent",
                    duration_ms=ms,
                    method="POST",
                    model=model_id or self.model,
                    http_status=response.status_code,
                    ok=response.ok,
                    error=err,
                )
                return response
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_retries - 1:
                    delay = delays[attempt] if attempt < len(delays) else delays[-1]
                    logger.warning(
                        f"Vertex AI request failed (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    raise
        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected retry loop exit")

    def call(self, model: str, messages: list, **kwargs) -> Dict[str, Any]:
        """Generic messages-based LLM call for _call_llm() dispatch.

        Converts OpenAI-format messages to Vertex AI format and calls the API.

        Returns:
            Dict with text, input_tokens, output_tokens, model.
        """
        if not self.initialized:
            raise RuntimeError("VertexAIProvider not initialized")

        # Convert OpenAI-format messages to Vertex AI contents
        contents = []
        system_instruction = None
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, str):
                    system_instruction = {"parts": [{"text": content}]}
                continue
            vertex_role = "user" if role == "user" else "model"
            if isinstance(content, str):
                parts = [{"text": content}]
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, str):
                        parts.append({"text": item})
                    elif isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append({"text": item["text"]})
                        elif item.get("type") == "image_url":
                            url = item["image_url"]["url"]
                            if url.startswith("data:"):
                                # Inline base64 data URI → Vertex inlineData
                                # Format: data:image/jpeg;base64,<data>
                                try:
                                    header, b64_data = url.split(",", 1)
                                    mime = header.split(":")[1].split(";")[0]
                                    parts.append({"inlineData": {"mimeType": mime, "data": b64_data}})
                                except (ValueError, IndexError):
                                    logger.warning("Malformed data URI in image_url, skipping image")
                            elif url.startswith("gs://"):
                                parts.append({"fileData": {"mimeType": "image/jpeg", "fileUri": url}})
                            elif url.startswith("https://storage.googleapis.com/"):
                                # GCS public URL → convert to gs:// fileData
                                gs_path = url.replace("https://storage.googleapis.com/", "")
                                parts.append({"fileData": {"mimeType": "image/jpeg", "fileUri": f"gs://{gs_path}"}})
                            else:
                                parts.append({"text": f"[Image: {url}]"})
                        else:
                            parts.append(item)
            else:
                parts = [{"text": str(content)}]
            contents.append({"role": vertex_role, "parts": parts})

        payload = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = system_instruction

        gen_config = {}
        if "temperature" in kwargs:
            gen_config["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            gen_config["maxOutputTokens"] = kwargs["max_tokens"]
        if "responseSchema" in kwargs:
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = kwargs["responseSchema"]
        # Gemini 2.5 models have thinking enabled by default, adding 30–60 s of latency
        # per LLM call. Disable it (budget=0) unless the caller explicitly passes
        # thinking_budget. gemini-3-flash-preview does not use thinking at all.
        if "2.5" in model:
            budget = kwargs.get("thinking_budget", 0)
            gen_config["thinkingConfig"] = {"thinkingBudget": int(budget)}
        if gen_config:
            payload["generationConfig"] = gen_config

        url = self._get_vertex_url(model)
        headers = self._get_vertex_headers()
        resp = self._vertex_post_with_retry(url, headers, payload)
        resp.raise_for_status()
        result = resp.json()

        text = ""
        finish_reason = ""
        candidates = result.get("candidates", [])
        if candidates:
            cand0 = candidates[0]
            finish_reason = str(cand0.get("finishReason") or "").lower()
            parts_out = cand0.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts_out)

        usage = result.get("usageMetadata", {})
        return {
            "text": text,
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
            "model": model,
            "finish_reason": finish_reason,
        }

    def raw_generate_content(self, payload: Dict[str, Any], model: str = None) -> Dict[str, Any]:
        """Send a raw Vertex AI generateContent payload (already in Vertex format).

        This is needed by video analysis methods that build Vertex-native payloads
        with video fileData parts, so no message conversion is performed.

        Args:
            payload: Complete Vertex AI generateContent request body (contents, etc.).
            model: Model identifier. Defaults to self.model if not specified.

        Returns:
            Dict with text, input_tokens, output_tokens, model.
        """
        if not self.initialized:
            raise RuntimeError("VertexAIProvider not initialized")

        url = self._get_vertex_url(model or self.model)
        headers = self._get_vertex_headers()
        resp = self._vertex_post_with_retry(url, headers, payload)
        resp.raise_for_status()
        result = resp.json()

        text = ""
        finish_reason = ""
        candidates = result.get("candidates", [])
        if candidates:
            cand0 = candidates[0]
            finish_reason = str(cand0.get("finishReason") or "").lower()
            parts_out = cand0.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts_out)

        usage = result.get("usageMetadata", {})
        return {
            "text": text,
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
            "model": model or self.model,
            "finish_reason": finish_reason,
        }

    def _upload_video_to_gcs(self, video_path: str) -> Optional[str]:
        """Upload video to GCS and return public URL.

        Args:
            video_path: Path to local video file.

        Returns:
            Public URL of the uploaded video, or None if failed.
        """
        if not self.gcs_storage_service:
            logger.warning("GCS storage service not available for video upload")
            return None

        try:
            import uuid

            vp = (video_path or "").strip()
            if not vp or vp.startswith(("http://", "https://", "gs://")):
                logger.warning(
                    "GCS video upload expects a local file path, not a URL (got %s...)",
                    vp[:60],
                )
                return None
            if not os.path.isfile(vp):
                logger.warning("GCS video upload: path is not a file: %s", vp[:80])
                return None

            # Generate unique filename
            video_id = str(uuid.uuid4())[:8]
            gcs_key = f"gemini_analysis/{video_id}.mp4"

            # Read video file
            with open(vp, "rb") as f:
                video_data = f.read()

            # Upload to GCS
            logger.info("Uploading video to GCS for Gemini analysis...")

            # Use the GCS storage service's bucket
            if not self.gcs_storage_service._initialize():
                return None

            blob = self.gcs_storage_service.bucket.blob(gcs_key)
            blob.upload_from_string(video_data, content_type='video/mp4')

            # Try to make public
            try:
                blob.make_public()
            except Exception:
                pass  # May fail if bucket uses uniform access

            # Generate public URL
            video_url = f"https://storage.googleapis.com/{self.gcs_storage_service.bucket_name}/{gcs_key}"
            logger.info(f"Video uploaded to GCS: {video_url[:60]}...")

            return video_url

        except Exception as e:
            logger.error(f"Error uploading video to GCS: {e}")
            return None

    def _cleanup_gcs_video(self, video_url: str):
        """Delete temporary video from GCS.

        Args:
            video_url: URL of the video to delete.
        """
        if not self.gcs_storage_service or not video_url:
            return

        try:
            # Extract key from URL
            parts = video_url.split('storage.googleapis.com/')
            if len(parts) > 1:
                # Remove bucket name from path
                path_parts = parts[1].split('/', 1)
                if len(path_parts) > 1:
                    gcs_key = path_parts[1]
                    if self.gcs_storage_service._initialize():
                        blob = self.gcs_storage_service.bucket.blob(gcs_key)
                        blob.delete()
                        logger.info("Cleaned up GCS video file")
        except Exception:
            pass  # Non-critical, ignore errors

    def _character_image_uri_for_vertex(self, image_url: str) -> Optional[str]:
        """Convert image URL to a URI Vertex AI accepts (gs://). Returns None if not GCS."""
        u = image_url.strip()
        if u.startswith("gs://"):
            return u
        if u.startswith("https://storage.googleapis.com/"):
            path = u.replace("https://storage.googleapis.com/", "")
            return f"gs://{path}"
        return None
