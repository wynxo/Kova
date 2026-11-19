"""Compare one prompt across several models concurrently.

A backend-first slice of issue #96: run a single user prompt against every
selected model at once and return per-model text, timing and error. One
model's failure never breaks the others.

This is deliberately side-effect free: no chat is written, no tools run, no
history is loaded. Each model gets the same single user message through
:class:`kova.models.ModelClient`.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from kova.config import Config, ModelEntry
from kova.models import ModelClient

#: Upper bound on models per compare request, so a client cannot fan out
#: without limit against paid providers.
MAX_COMPARE_MODELS = 8


async def run_single(
    entry: ModelEntry,
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    privacy: str = "off",
    config: Config | None = None,
) -> dict:
    """Run one prompt on one model; never raises, errors become ``error``."""
    started = time.monotonic()
    pieces: list[str] = []
    try:
        client = ModelClient(entry, transport=transport, privacy=privacy, config=config)
        async for event in client.stream([{"role": "user", "content": text}]):
            if event.type == "text":
                pieces.append(event.text)
        return {
            "model": entry.name,
            "text": "".join(pieces),
            "ms": int((time.monotonic() - started) * 1000),
            "error": None,
        }
    except Exception as exc:  # one failure must not break the other models
        message = str(exc).strip() or exc.__class__.__name__
        return {
            "model": entry.name,
            "text": "".join(pieces),
            "ms": int((time.monotonic() - started) * 1000),
            "error": message,
        }


async def compare_prompts(
    entries: list[ModelEntry],
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    privacy: str = "off",
    config: Config | None = None,
) -> list[dict]:
    """Run ``text`` on every entry concurrently, preserving entry order."""
    results = await asyncio.gather(
        *(run_single(entry, text, transport=transport, privacy=privacy, config=config) for entry in entries)
    )
    return list(results)


# -- side-by-side compare inside a chat (#96) ------------------------------

#: A chat compare asks between two and four models: enough to choose from,
#: few enough to read side by side on one screen.
MIN_CHAT_COMPARE_MODELS = 2
MAX_CHAT_COMPARE_MODELS = 4


def plain_history(entries: list[tuple[int | None, dict]]) -> list[dict]:
    """The chat's words only: user and assistant text, no tool rounds.

    A compare answer is a plain reply, so the models never see tool calls
    they could not answer or tool output from another model's turn.
    """
    history = []
    for _, message in entries:
        role, content = message.get("role"), message.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip() \
                and not message.get("tool_calls"):
            history.append({"role": role, "content": content})
    return history


async def _stream_one(entry: ModelEntry, messages: list[dict], queue: asyncio.Queue, *,
                      transport, privacy: str, config: Config | None, pricing) -> dict | None:
    """Stream one model's answer into ``queue``; return its result, or None on error."""
    from kova.usage import TurnMeter

    meter = TurnMeter()
    meter.start_round()
    pieces: list[str] = []
    try:
        client = ModelClient(entry, transport=transport, privacy=privacy, config=config,
                             reasoning_effort=entry.reasoning_effort)
        async for event in client.stream(messages):
            if event.type in ("text", "reasoning"):
                meter.output(event.text)
                if event.type == "text":
                    pieces.append(event.text)
                queue.put_nowait({"type": event.type, "model": entry.name, "text": event.text})
            elif event.type == "usage":
                meter.usage(event.usage)
            elif event.type == "privacy" and event.privacy is not None:
                queue.put_nowait({"type": "privacy", "model": entry.name, **event.privacy})
        meter.end_round(messages)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # one failure must not break the other models
        message = str(exc).strip() or exc.__class__.__name__
        queue.put_nowait({"type": "model_error", "model": entry.name, "message": message})
        return None
    result = {"model": entry.name, "text": "".join(pieces), "usage": meter.summary(entry, pricing)}
    queue.put_nowait({"type": "model_done", **result})
    return result


async def stream_compare(entries: list[ModelEntry], messages_for, *, transport=None,
                         privacy: str = "off", config: Config | None = None,
                         pricing=None, on_result=None):
    """Run every model at once and yield their events as they arrive.

    Each event carries the ``model`` it belongs to. ``messages_for(entry)``
    builds that model's request, so each one is trimmed to its own window.
    ``on_result(result)`` is called as soon as a model finishes, before the
    others do. Cancelling the consumer cancels every model request.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def run(entry: ModelEntry) -> None:
        result = await _stream_one(entry, messages_for(entry), queue, transport=transport,
                                   privacy=privacy, config=config, pricing=pricing)
        if result is not None and on_result is not None:
            on_result(result)

    tasks = [asyncio.create_task(run(entry)) for entry in entries]
    finished = asyncio.gather(*tasks, return_exceptions=True)
    finished.add_done_callback(lambda _: queue.put_nowait(None))
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
