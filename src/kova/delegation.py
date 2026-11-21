"""One bounded, tool-free child completion for an approved work task."""

import asyncio
from contextlib import aclosing

from kova.models import ModelClient
from kova.tools import ToolError

NAME = "delegate_analysis"
PARAMETERS = {
    "type": "object",
    "properties": {"task": {"type": "string", "minLength": 1, "maxLength": 8000}},
    "required": ["task"],
    "additionalProperties": False,
}


async def analyze(client: ModelClient, task: str) -> str:
    messages = [
        {"role": "system", "content": "Complete this analysis task using only the supplied text. "
         "You have no tools. Return a concise result for the parent assistant."},
        {"role": "user", "content": task},
    ]
    parts = []
    remaining = 8_000
    try:
        async with asyncio.timeout(60), aclosing(client.stream(messages)) as stream:
            async for event in stream:
                if event.type == "text" and event.text:
                    parts.append(event.text[:remaining])
                    remaining -= len(parts[-1])
                    if not remaining:
                        break
    except TimeoutError:
        raise ToolError("The child model timed out.") from None
    result = "".join(parts).strip()
    if not result:
        raise ToolError("The child model returned no analysis.")
    return result + ("\n[truncated]" if not remaining else "")
