"""LLMProvider protocol — the contract every provider must satisfy."""

from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal interface for an LLM provider client.

    Every provider (Vertex AI, OpenAI, Vercel) exposes a single ``call()``
    method that accepts OpenAI-style messages and returns a standardised dict.

    The ``initialized`` attribute lets callers skip providers whose credentials
    are missing.
    """

    initialized: bool

    def call(self, model: str, messages: list, **kwargs) -> Dict[str, Any]:
        """Send a chat-completion request and return a normalised result.

        Parameters
        ----------
        model : str
            Model identifier (e.g. ``"gemini-2.5-flash"``, ``"gpt-4o"``).
        messages : list
            OpenAI-format messages (``[{"role": "system", "content": "..."},
            {"role": "user", "content": "..."}]``).
        **kwargs :
            Optional overrides — ``temperature``, ``max_tokens``,
            ``response_format`` / ``responseSchema``.

        Returns
        -------
        dict
            ``{"text": str, "input_tokens": int, "output_tokens": int,
            "model": str}``
        """
        ...
