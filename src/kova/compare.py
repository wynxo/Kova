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
