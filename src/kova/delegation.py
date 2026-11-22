"""One bounded, tool-free child completion for an approved work task."""

import asyncio
from collections.abc import Callable
from contextlib import aclosing

from kova.config import ModelEntry
from kova.models import ModelClient
from kova.providers import ProviderError, validate_url
from kova.tools import ToolError

NAME = "delegate_analysis"
ATTEMPT_LIMIT = 4
MAX_STEP_CHARS = 8_000
PARAMETERS = {
    "type": "object",
    "properties": {"task": {"type": "string", "minLength": 1, "maxLength": 8000}},
    "required": ["task"],
    "additionalProperties": False,
}


def destination(entry: ModelEntry) -> str:
    """Identify the configured endpoint and model without exposing credentials."""
    try:
        endpoint = validate_url(entry.base_url)
    except (ProviderError, ValueError):
        raise ToolError("Child analysis needs a valid model URL without embedded credentials.") from None
    return f"{entry.name} (model {entry.model} at {endpoint})"


async def analyze(client: ModelClient, task: str,
                  emit: Callable[[str, str], None] | None = None) -> str:
    messages = [
        {"role": "system", "content": "Complete this analysis task using only the supplied text. "
         "You have no tools. Return a concise result for the parent assistant."},
        {"role": "user", "content": task},
    ]
    parts = []
    remaining = {"text": MAX_STEP_CHARS, "reasoning": MAX_STEP_CHARS}
    try:
        async with asyncio.timeout(60), aclosing(client.stream(messages)) as stream:
            async for event in stream:
                step = event.type
                if step not in remaining or not remaining[step] or not isinstance(event.text, str) or not event.text:
                    continue
                chunk = event.text[:remaining[step]]
                remaining[step] -= len(chunk)
                truncated = "\n[truncated]" if not remaining[step] else ""
                if emit is not None:
                    emit(step, chunk + truncated)
                if step == "text":
                    parts.append(chunk)
                    if truncated:
                        break
    except TimeoutError:
        raise ToolError("The child model timed out.") from None
    result = "".join(parts).strip()
    if not result:
        raise ToolError("The child model returned no analysis.")
    return result + ("\n[truncated]" if not remaining["text"] else "")
