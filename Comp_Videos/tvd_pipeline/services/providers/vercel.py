"""Vercel AI provider client — OpenAI-compatible gateway for multi-provider LLM routing.

Uses the Vercel AI Gateway which provides a unified OpenAI-compatible API that can
route to multiple providers (Google Gemini, OpenAI, Anthropic, etc.). This allows
the monolith to call any model through a single interface.

Env var: VERCEL_AI_HUB_KEY
"""

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)


class VercelProvider:
    """OpenAI-compatible gateway via Vercel AI Hub.

    Key features:
    - Single endpoint for multiple LLM providers
    - Automatic conversion from Vertex AI formats (GCS fileData, responseSchema)
      to OpenAI-compatible formats (base64 image_url, response_format.json_schema)
    - Actual cost extraction from Vercel's providerMetadata
    """

    BASE_URL = "https://api.vercel.ai/v1"
    REQUEST_TIMEOUT = 300
    MAX_RETRIES = 3
    RETRY_DELAYS = (10, 25, 60)

    def __init__(self, api_key: str = None):
        """Initialize Vercel AI provider.

        Args:
            api_key: Vercel AI Hub API key. Falls back to VERCEL_AI_HUB_KEY env var.
        """
        self.api_key = api_key or os.environ.get("VERCEL_AI_HUB_KEY", "")
        self.initialized = bool(self.api_key)
        if self.initialized:
            logger.info("VercelProvider initialized")
        else:
            logger.debug("VercelProvider not available (no VERCEL_AI_HUB_KEY)")

    def call(self, model: str, messages: list, **kwargs) -> Dict[str, Any]:
        """Call the Vercel AI Hub with OpenAI-compatible messages.

        Args:
            model: Model identifier (e.g. "google/gemini-2.5-flash", "openai/gpt-4o").
            messages: List of message dicts in OpenAI format.
            **kwargs: Additional params (temperature, max_tokens, response_format, etc.)

        Returns:
            Dict with keys:
                text: str — the generated text response
                input_tokens: int — prompt token count
                output_tokens: int — completion token count
                model: str — model used
                actual_cost_usd: float or None — real billing cost from Vercel (if available)
        """
        if not self.initialized:
            raise RuntimeError("VercelProvider not initialized (missing API key)")

        # Convert any Vertex-style content in messages to OpenAI format
        converted_messages = [self._convert_message(m) for m in messages]

        # Build request body
        body = {
            "model": model,
            "messages": converted_messages,
        }

        # Handle response_format / responseSchema conversion
        if "responseSchema" in kwargs:
            schema = kwargs.pop("responseSchema")
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": schema,
                },
            }
        elif "response_format" in kwargs:
            body["response_format"] = kwargs.pop("response_format")

        # Pass through standard params
        for key in ("temperature", "max_tokens", "top_p", "stop"):
            if key in kwargs:
                body[key] = kwargs[key]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Retry loop
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

                log_external_api_call(
                    "vercel",
                    "chat_completions",
                    method="POST",
                    model=str(body.get("model") or ""),
                    url_hint="/chat/completions",
                )
                _t0 = time.perf_counter()
                resp = requests.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers=headers,
                    json=body,
                    timeout=self.REQUEST_TIMEOUT,
                )

                if resp.status_code == 429:
                    delay = self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)]
                    logger.warning(f"Vercel AI Hub rate limited, retrying in {delay}s (attempt {attempt + 1})")
                    time.sleep(delay)
                    continue

                resp.raise_for_status()
                data = resp.json()
                _ms = int((time.perf_counter() - _t0) * 1000)

                # Extract response text
                text = ""
                choices = data.get("choices", [])
                if choices:
                    text = choices[0].get("message", {}).get("content", "")

                # Extract token usage
                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)

                # Extract actual cost from Vercel providerMetadata
                actual_cost = None
                try:
                    actual_cost = data.get("providerMetadata", {}).get("gateway", {}).get("cost")
                except (KeyError, TypeError, AttributeError):
                    pass

                log_external_api_result(
                    "vercel",
                    "chat_completions",
                    duration_ms=_ms,
                    method="POST",
                    model=str(body.get("model") or ""),
                    http_status=resp.status_code,
                    ok=True,
                    detail=f"in_tok={input_tokens} out_tok={output_tokens}",
                )

                return {
                    "text": text,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "model": model,
                    "actual_cost_usd": actual_cost,
                }

            except requests.exceptions.HTTPError as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)]
                    logger.warning(f"Vercel AI Hub error: {e}, retrying in {delay}s")
                    time.sleep(delay)
                else:
                    raise
            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)]
                    logger.warning(f"Vercel AI Hub error: {e}, retrying in {delay}s")
                    time.sleep(delay)
                else:
                    raise

        raise RuntimeError(f"Vercel AI Hub call failed after {self.MAX_RETRIES} attempts: {last_error}")

    def _convert_message(self, message: dict) -> dict:
        """Convert a message from Vertex AI format to OpenAI-compatible format.

        Handles:
        - GCS fileData (Vertex format) -> base64 image_url (OpenAI format)
        - Simple string content -> unchanged
        - Already OpenAI-format content -> unchanged
        """
        if not isinstance(message, dict):
            return message

        content = message.get("content")
        if content is None:
            return message

        # String content — pass through
        if isinstance(content, str):
            return message

        # List content (multimodal) — convert each part
        if isinstance(content, list):
            converted_parts = []
            for part in content:
                converted_parts.append(self._convert_content_part(part))
            return {**message, "content": converted_parts}

        return message

    def _convert_content_part(self, part: dict) -> dict:
        """Convert a single content part from Vertex to OpenAI format.

        Vertex fileData format:
            {"fileData": {"mimeType": "image/jpeg", "fileUri": "gs://bucket/path"}}

        OpenAI image_url format:
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
        """
        if not isinstance(part, dict):
            return part

        # Already in OpenAI format
        if "type" in part:
            return part

        # Vertex fileData -> OpenAI image_url
        file_data = part.get("fileData")
        if file_data:
            mime_type = file_data.get("mimeType", "image/jpeg")
            file_uri = file_data.get("fileUri", "")

            # If it's a GCS URI, download and base64-encode
            if file_uri.startswith("gs://"):
                try:
                    image_b64 = self._download_gcs_to_base64(file_uri)
                    return {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_b64}",
                        },
                    }
                except Exception as e:
                    logger.warning(f"Failed to download GCS file {file_uri}: {e}")
                    # Fall through to return original part
            # If it's an HTTP URL, use it directly
            elif file_uri.startswith("http"):
                return {
                    "type": "image_url",
                    "image_url": {"url": file_uri},
                }

        # Vertex text part -> OpenAI text part
        if "text" in part:
            return {"type": "text", "text": part["text"]}

        # Vertex inlineData (base64 already) -> OpenAI image_url
        inline_data = part.get("inlineData")
        if inline_data:
            mime_type = inline_data.get("mimeType", "image/jpeg")
            data = inline_data.get("data", "")
            return {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{data}",
                },
            }

        return part

    def _download_gcs_to_base64(self, gcs_uri: str) -> str:
        """Download a GCS file and return its base64-encoded content.

        Args:
            gcs_uri: GCS URI (gs://bucket/path/to/file)

        Returns:
            Base64-encoded string of the file content.
        """
        try:
            from google.cloud import storage
            # Parse gs://bucket/path
            parts = gcs_uri.replace("gs://", "").split("/", 1)
            bucket_name = parts[0]
            blob_path = parts[1] if len(parts) > 1 else ""

            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            content = blob.download_as_bytes()
            return base64.b64encode(content).decode("utf-8")
        except ImportError:
            raise RuntimeError("google-cloud-storage is required for GCS file conversion")
