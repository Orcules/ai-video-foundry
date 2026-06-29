"""OpenAI provider client — SDK wrapper with LLMProvider-compatible call()."""

import copy
import logging
from typing import Any, Dict

from openai import OpenAI

logger = logging.getLogger(__name__)


def _convert_gemini_schema_to_openai(schema: dict) -> dict:
    """Convert Gemini-style responseSchema to OpenAI json_schema format.

    Handles:
      - "nullable": true  →  "type": ["<original_type>", "null"]
      - Ensures "additionalProperties": false on all objects
      - Ensures all properties are listed in "required"
    """
    schema = copy.deepcopy(schema)
    _convert_node(schema)
    return schema


def _convert_node(node: dict):
    if not isinstance(node, dict):
        return
    # Convert nullable
    if node.pop("nullable", None):
        t = node.get("type", "string")
        if isinstance(t, str):
            node["type"] = [t, "null"]
    # Ensure additionalProperties on objects
    if node.get("type") == "object" and "additionalProperties" not in node:
        node["additionalProperties"] = False
    # Ensure all properties in required
    if node.get("type") == "object" and "properties" in node:
        node.setdefault("required", list(node["properties"].keys()))
        for prop in node["properties"].values():
            _convert_node(prop)
    # Recurse into array items
    if node.get("type") == "array" and "items" in node:
        _convert_node(node["items"])


class OpenAIProvider:
    """Thin OpenAI SDK wrapper implementing the LLMProvider protocol.

    Attributes:
        client: The raw OpenAI client instance (exposed as property for
                ElevenLabsService and SunoMusicService which need it).
        initialized: Always True after construction (OpenAI SDK doesn't fail on init).
    """

    def __init__(self, api_key: str):
        """Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key.
        """
        self._client = OpenAI(api_key=api_key)
        self.initialized = True
        logger.info("OpenAI provider initialized")

    @property
    def client(self) -> OpenAI:
        """Raw OpenAI SDK client (needed by ElevenLabsService, SunoMusicService)."""
        return self._client

    def call(self, model: str, messages: list, **kwargs) -> Dict[str, Any]:
        """Generic messages-based LLM call for _call_llm() dispatch.

        Returns:
            Dict with text, input_tokens, output_tokens, model.
        """
        call_kwargs = {"model": model, "messages": messages}

        # Reasoning effort (GPT-5+ family) — when set to anything other than "none",
        # temperature/top_p are not supported by the API.
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort:
            call_kwargs["reasoning_effort"] = reasoning_effort

        if "temperature" in kwargs and not reasoning_effort:
            call_kwargs["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            call_kwargs["max_completion_tokens"] = kwargs["max_tokens"]
        if "response_format" in kwargs:
            call_kwargs["response_format"] = kwargs["response_format"]
        elif "responseSchema" in kwargs:
            converted = _convert_gemini_schema_to_openai(kwargs["responseSchema"])
            call_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": converted}
            }

        from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

        import time as _time

        t0 = _time.perf_counter()
        log_external_api_call(
            "openai",
            "chat.completions",
            method="POST",
            model=model,
            url_hint="/v1/chat/completions",
        )
        try:
            response = self._client.chat.completions.create(**call_kwargs)
        except Exception as e:
            log_external_api_result(
                "openai",
                "chat.completions",
                duration_ms=int((_time.perf_counter() - t0) * 1000),
                method="POST",
                model=model,
                ok=False,
                error=str(e)[:300],
            )
            raise
        choice = response.choices[0] if response.choices else None
        text = choice.message.content if choice else ""
        usage = response.usage
        finish_reason = ""
        if choice is not None:
            fr = getattr(choice, "finish_reason", None)
            finish_reason = str(fr or "").lower()

        log_external_api_result(
            "openai",
            "chat.completions",
            duration_ms=int((_time.perf_counter() - t0) * 1000),
            method="POST",
            model=model,
            http_status=200,
            ok=True,
            detail=f"in_tok={usage.prompt_tokens if usage else 0} out_tok={usage.completion_tokens if usage else 0}",
        )

        return {
            "text": text,
            "input_tokens": usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "model": model,
            "finish_reason": finish_reason,
        }
