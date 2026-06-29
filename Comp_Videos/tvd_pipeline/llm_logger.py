"""Provider-agnostic LLM call logger.

Logs every LLM call (regardless of provider or call path) to sequentially
numbered JSON files in a configurable directory.  Thread-safe.

Used by ``_call_llm()`` in the processor and passed explicitly to functions
that bypass ``_call_llm()`` (e.g. video analysis with native Vertex payloads).
"""

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class LLMLogger:
    """Write sequentially numbered JSON log files for LLM calls."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self._counter = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(
        self,
        step_key: str,
        provider: str,
        model: str,
        messages: Union[List[Dict], Dict],
        result: Any,
        **extra_kwargs,
    ) -> None:
        """Log an LLM call to a sequentially-numbered JSON file.

        Args:
            step_key: Logical step name (e.g. ``"parse_prompt"``).
            provider: Provider identifier (``"vertex"``, ``"openai"``, …).
            model: Model name/ID used.
            messages: Chat-style ``list[dict]`` **or** Vertex-native ``dict``
                with a ``"contents"`` key.  Both are handled transparently.
            result: Raw provider response (``dict`` with ``text``,
                ``input_tokens``, ``output_tokens``, or plain ``str``).
            **extra_kwargs: Additional metadata to include in the log entry
                (e.g. ``reasoning_effort``).  Keys named ``responseSchema``
                are automatically excluded.
        """
        try:
            with self._lock:
                self._counter += 1
                seq = self._counter

            safe_messages = self._sanitize_messages(messages)

            log_entry = {
                "step_key": step_key,
                "provider": provider,
                "model": model,
                "messages": safe_messages,
                "output": result.get("text", "") if isinstance(result, dict) else str(result),
                "input_tokens": result.get("input_tokens", 0) if isinstance(result, dict) else 0,
                "output_tokens": result.get("output_tokens", 0) if isinstance(result, dict) else 0,
            }
            if extra_kwargs:
                log_entry["kwargs"] = {
                    k: v for k, v in extra_kwargs.items() if k != "responseSchema"
                }

            os.makedirs(self.log_dir, exist_ok=True)
            safe_key = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(step_key))[:120] or "step"
            log_path = os.path.join(self.log_dir, f"{seq:02d}_{safe_key}.json")
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(log_entry, f, indent=2, ensure_ascii=False, default=str)
        except Exception as err:
            logger.warning(f"LLM log write failed: {err}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_messages(
        messages: Union[List[Dict], Dict],
    ) -> Union[List[Dict], Dict]:
        """Return a JSON-safe copy of *messages*, truncating large blobs.

        Handles two formats:
        * **Chat-style** (``list[dict]``): each dict has ``role`` and
          ``content`` (string or list of parts).
        * **Vertex-native** (``dict`` with ``"contents"``): the payload
          sent to ``raw_generate_content``.
        """
        if isinstance(messages, list):
            return LLMLogger._sanitize_chat_messages(messages)
        if isinstance(messages, dict):
            return LLMLogger._sanitize_vertex_payload(messages)
        return str(messages)[:5000]

    @staticmethod
    def _sanitize_chat_messages(messages: List[Dict]) -> List[Dict]:
        safe = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 50_000:
                content = content[:2000] + f"\n... [TRUNCATED {len(content)} chars total]"
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        parts.append({"type": "image_url", "url": "[base64 image omitted]"})
                    else:
                        parts.append(part)
                content = parts
            safe.append({"role": msg.get("role", "user"), "content": content})
        return safe

    @staticmethod
    def _sanitize_vertex_payload(payload: Dict) -> Dict:
        """Sanitize a Vertex ``generateContent`` payload dict.

        Keeps ``fileData`` URIs (useful for debugging) but truncates large
        ``inlineData`` blobs.
        """
        safe = {}
        for key, value in payload.items():
            if key == "contents" and isinstance(value, list):
                safe_contents = []
                for entry in value:
                    if not isinstance(entry, dict):
                        safe_contents.append(entry)
                        continue
                    safe_entry = dict(entry)
                    if "parts" in safe_entry and isinstance(safe_entry["parts"], list):
                        safe_parts = []
                        for part in safe_entry["parts"]:
                            if isinstance(part, dict) and "inlineData" in part:
                                safe_parts.append({
                                    "inlineData": {
                                        "mimeType": part["inlineData"].get("mimeType", "?"),
                                        "data": "[base64 blob omitted]",
                                    }
                                })
                            elif isinstance(part, dict) and "text" in part:
                                text = part["text"]
                                if isinstance(text, str) and len(text) > 50_000:
                                    text = text[:2000] + f"\n... [TRUNCATED {len(text)} chars total]"
                                safe_parts.append({"text": text})
                            else:
                                # fileData and other parts are kept as-is
                                safe_parts.append(part)
                        safe_entry["parts"] = safe_parts
                    safe_contents.append(safe_entry)
                safe[key] = safe_contents
            else:
                safe[key] = value
        return safe
