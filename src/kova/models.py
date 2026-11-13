"""Streaming client for OpenAI-compatible models."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from kova.config import ModelEntry


class ModelError(Exception):
    """A model server could not be reached."""


@dataclass
class ModelEvent:
    type: str
    text: str = ""
    tool_call: dict | None = None


class ThinkSplitter:
    """Separate inline thinking tags, including tags split across chunks."""

    def __init__(self) -> None:
        self._buffer = ""
        self._thinking = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._buffer += chunk
        result: list[tuple[str, str]] = []
        while self._buffer:
            tag = "</think>" if self._thinking else "<think>"
            kind = "reasoning" if self._thinking else "text"
            pos = self._buffer.find(tag)
            if pos >= 0:
                if pos:
                    result.append((kind, self._buffer[:pos]))
                self._buffer = self._buffer[pos + len(tag):]
                self._thinking = not self._thinking
                continue
            hold = max((i for i in range(1, len(tag)) if self._buffer.endswith(tag[:i])), default=0)
            ready = self._buffer[:-hold] if hold else self._buffer
            if ready:
                result.append((kind, ready))
            self._buffer = self._buffer[-hold:] if hold else ""
            break
        return result

    def flush(self) -> list[tuple[str, str]]:
        if not self._buffer:
            return []
        result = [("reasoning" if self._thinking else "text", self._buffer)]
        self._buffer = ""
        return result


class ModelClient:
    """Send messages and stream model events."""

    def __init__(self, entry: ModelEntry, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 300):
        self.entry = entry
        self.transport = transport
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.entry.base_url.rstrip('/')}/{path}"

    def _headers(self) -> dict[str, str]:
        if self.entry._provider is not None:
            from kova.providers import headers_for
            return headers_for(self.entry._provider)
        return {"Authorization": f"Bearer {self.entry.api_key}"} if self.entry.api_key else {}

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[ModelEvent]:
        from kova.providers import ProviderError
        try:
            headers = await asyncio.to_thread(self._headers)
        except ProviderError as exc:
            raise ModelError(str(exc)) from None
        adapter = self.entry._provider.adapter if self.entry._provider else 'openai'
        if adapter != 'openai':
            from kova.model_adapters import anthropic_request, responses_request, stream_native
            build = anthropic_request if adapter == 'anthropic' else responses_request
            path, payload = build(self.entry.model, messages, tools)
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, headers=headers, trust_env=False) as client:
                    async for event in stream_native(client, self._url(path), payload, adapter):
                        yield event
            except httpx.RequestError:
                raise ModelError('Could not connect to the provider. Check your connection.') from None
            return
        payload = {"model": self.entry.model, "messages": messages, "stream": True}
        if tools is not None:
            payload["tools"] = tools
        splitter = ThinkSplitter()
        calls: dict[int, dict] = {}
        error = f"Can't reach model '{self.entry.name}' at {self.entry.base_url}"
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, headers=headers, trust_env=False) as client:
                async with client.stream("POST", self._url("chat/completions"), json=payload) as response:
                    if response.status_code >= 400:
                        raise ModelError(f"{error}: HTTP {response.status_code}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        if not data:
                            continue
                        try:
                            choices = json.loads(data).get("choices") or []
                        except (json.JSONDecodeError, AttributeError):
                            continue  # skip a garbled stream line instead of killing the turn
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            yield ModelEvent("reasoning", reasoning)
                        if delta.get("content"):
                            for kind, value in splitter.feed(delta["content"]):
                                yield ModelEvent(kind, value)
                        for part in delta.get("tool_calls") or []:
                            index = part["index"]
                            call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            call["id"] += part.get("id") or ""
                            # Opaque tool protocol extensions (e.g. signed reasoning) must
                            # survive history without provider logic in the agent loop.
                            if isinstance(part.get('extra_content'), dict):
                                call.setdefault('extra_fields', {})['extra_content'] = part['extra_content']
                            function = part.get("function") or {}
                            call["name"] += function.get("name") or ""
                            call["arguments"] += function.get("arguments") or ""
        except httpx.RequestError as exc:
            raise ModelError(error) from exc
        for kind, value in splitter.flush():
            yield ModelEvent(kind, value)
        for index, call in sorted(calls.items()):
            if not call["id"]:
                call["id"] = f"call_{index}"
            yield ModelEvent("tool_call", tool_call=call)
        yield ModelEvent("done")

    async def ping(self) -> bool:
        """Check whether the model server responds successfully."""
        from kova.providers import ProviderError
        try:
            headers = await asyncio.to_thread(self._headers)
            async with httpx.AsyncClient(transport=self.transport, timeout=min(self.timeout, 3), headers=headers, trust_env=False) as client:
                path = "models?client_version=0.0.0" if self.entry._provider and self.entry._provider.kind == "codex" else "models"
                response = await client.get(self._url(path))
                return response.status_code == 200
        except (httpx.HTTPError, ProviderError):
            return False
