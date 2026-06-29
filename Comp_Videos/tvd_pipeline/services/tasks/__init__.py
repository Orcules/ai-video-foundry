"""Provider-agnostic LLM task functions.

Every function that calls an LLM takes ``call_fn`` as its first argument::

    call_fn(messages: list, **kwargs) -> Dict[str, Any]

``call_fn`` is typically created in pipeline code as::

    lambda msgs, **kw: processor._call_llm("step_key", msgs, **kw)

This decouples *what* the LLM is asked from *which* provider answers.
"""

from .character import describe_character, describe_characters
from .image_eval import evaluate_image_cleanliness, evaluate_image_quality
