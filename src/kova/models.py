"""Streaming client for OpenAI-compatible models."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from kova import egress
from kova.config import Config, ModelEntry
from kova.privacy import is_loopback_url, protect_messages
from kova.usage import normalize_usage


class ModelError(Exception):
    """A model server could not be reached."""


@dataclass
class ModelEvent:
    type: str
    text: str = ""
    tool_call: dict | None = None
    privacy: dict | None = None
    #: ``{"prompt_tokens", "completion_tokens"}`` as the server reported them.
    usage: dict | None = None


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

    #: (base URL, model) pairs whose server rejected ``stream_options``. Clients
    #: are made per turn, so this remembers it for the life of the process.
    _rejects_usage_option: set[tuple[str, str]] = set()

    def __init__(self, entry: ModelEntry, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 300, privacy: str = "off", config: Config | None = None,
                 reasoning_effort: str | None = None, *, fixed_destination: bool = False,
                 max_output_tokens: int | None = None):
        self.entry = entry
        self.transport = transport
        self.timeout = timeout
        self.privacy = privacy
        self.config = config
        self.reasoning_effort = reasoning_effort
        #: A bot's per-turn output cap, sent as the request's own limit. None
        #: keeps the provider's default, exactly as before bots existed.
        self.max_output_tokens = max_output_tokens
        self._approved_url = entry.base_url if fixed_destination else None
        # Native tool continuations can require opaque reasoning blocks. Keep
        # them in this turn's client only, never in saved messages or events.
        self._reasoning_by_call: dict[str, list[dict]] = {}
        # Ask for token usage unless this server said it does not understand the option.
        self.include_usage = (entry.base_url, entry.model) not in self._rejects_usage_option

    def _url(self, path: str) -> str:
        return f"{self.entry.base_url.rstrip('/')}/{path}"

    def _headers(self) -> dict[str, str]:
        if self.entry._provider is not None:
            from kova.providers import headers_for
            return headers_for(self.entry._provider)
        return {"Authorization": f"Bearer {self.entry.api_key}"} if self.entry.api_key else {}

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[ModelEvent]:
        from kova.egress import LocalOnlyError
        try:
            with egress.scope(config=self.config, provider=self.entry.provider or self.entry.name,
                              model=self.entry.model, kind='model'):
                egress.check_url(self.entry.base_url)
                async for event in self._managed_stream(messages, tools):
                    yield event
        except LocalOnlyError as exc:
            raise ModelError(str(exc)) from None

    async def _managed_stream(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[ModelEvent]:
        if self.config is not None and (self.entry.provider or '').startswith('managed-'):
            from kova.managed_engine import manager
            from kova.local_models import LocalModelError
            try:
                async with manager(self.config).use(self.entry, transport=self.transport):
                    async for event in self._stream(messages, tools):
                        yield event
            except LocalModelError as exc:
                raise ModelError(str(exc)) from None
        else:
            async for event in self._stream(messages, tools):
                yield event

    async def _stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[ModelEvent]:
        from kova.providers import ProviderError
        from kova.reasoning_effort import payload_fields
        if self._approved_url is not None and self.entry.base_url != self._approved_url:
            raise ModelError("The child model destination changed. Request fresh approval before sending the task.")
        if not self.entry.model.strip():
            raise ModelError("No model is configured. Run: kova setup")
        # Scan before even resolving provider credentials: block mode must not
        # make any request when it refuses the turn. The stored messages and
        # tool arguments are not modified; only this request uses the copy.
        protection = None
        request_messages = messages
        if self.privacy != "off" and not is_loopback_url(self.entry.base_url):
            protection = protect_messages(messages, self.privacy)
            if protection.blocked:
                raise ModelError(protection.block_message())
            request_messages = protection.messages
        try:
            headers = await asyncio.to_thread(self._headers)
        except ProviderError as exc:
            raise ModelError(str(exc)) from None
        if protection is not None and (protection.count or protection.incomplete):
            yield ModelEvent("privacy", privacy=protection.status())
        adapter = self.entry._provider.adapter if self.entry._provider else 'openai'
        effort = payload_fields(self.entry, self.reasoning_effort)
        if adapter != 'openai':
            from kova.model_adapters import (
                anthropic_request,
                responses_request,
                stream_native,
            )
            build = anthropic_request if adapter == 'anthropic' else responses_request
            path, payload = build(self.entry.model, request_messages, tools, self._reasoning_by_call)
            payload.update(effort)
            if self.max_output_tokens:
                if adapter == 'responses':
                    payload['max_output_tokens'] = int(self.max_output_tokens)
                else:
                    payload['max_tokens'] = min(int(payload.get('max_tokens') or self.max_output_tokens),
                                                int(self.max_output_tokens))
            if adapter == 'responses' and effort.get('reasoning', {}).get('effort') not in (None, 'none'):
                payload['include'] = ['reasoning.encrypted_content']
            try:
                async with egress.AsyncClient(transport=self.transport, timeout=self.timeout, headers=headers, trust_env=False) as client:
                    async for event in stream_native(client, self._url(path), payload, adapter, self._reasoning_by_call):
                        yield event
            except httpx.RequestError:
                raise ModelError('Could not connect to the provider. Check your connection.') from None
            return
        payload = {"model": self.entry.model, "messages": request_messages, "stream": True, **effort}
        if self.max_output_tokens:
            payload["max_tokens"] = int(self.max_output_tokens)
        if self.include_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools is not None:
            payload["tools"] = tools
        splitter = ThinkSplitter()
        calls: dict[int, dict] = {}
        usage = None
        received_text = False
        complete = False
        error = f"Can't reach model '{self.entry.name}' at {self.entry.base_url}"
        try:
            async with egress.AsyncClient(transport=self.transport, timeout=self.timeout, headers=headers, trust_env=False) as client:
                async with client.stream("POST", self._url("chat/completions"), json=payload) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        # Retry only an explicit capability rejection, before any output.
                        body = response.text.lower()
                        # An older server that rejects the usage option still chats.
                        if self.include_usage and response.status_code in (400, 422) and 'stream_options' in body:
                            self.include_usage = False
                            self._rejects_usage_option.add((self.entry.base_url, self.entry.model))
                            async for event in self.stream(request_messages, tools):
                                yield event
                            return
                        if tools and response.status_code in (400, 422) and 'tool' in body and any(
                            word in body for word in ('not supported', 'unsupported', 'does not support')
                        ):
                            # Retry with the already-scanned copy: re-scanning the
                            # original here would re-emit the privacy event for the
                            # same turn.
                            async for event in self.stream(request_messages, tools=None):
                                yield event
                            return
                        hint = {
                            401: 'Check your API key or sign in again with kova setup.',
                            403: 'Check provider access or sign in again with kova setup.',
                            404: 'Check the base URL and model with kova setup.',
                            429: 'Check provider usage limits, then retry.',
                        }.get(response.status_code, 'Run kova doctor, then retry.')
                        raise ModelError(f"Provider request failed: HTTP {response.status_code}. {hint}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            complete = True
                            break
                        if not data:
                            continue
                        try:
                            frame = json.loads(data)
                            if isinstance(frame, dict) and frame.get('error'):
                                raise ModelError('Provider could not complete the response. Check access and usage limits, then retry.')
                            # The usage frame comes last, often with no choices at all.
                            usage = normalize_usage(frame.get("usage")) or usage
                            choices = frame.get("choices") or []
                        except (json.JSONDecodeError, AttributeError):
                            continue  # skip a garbled stream line instead of killing the turn
                        if not choices:
                            continue
                        if not isinstance(choices, list) or not isinstance(choices[0], dict):
                            raise ModelError('Provider returned an invalid stream. Run kova doctor, then retry.')
                        if choices[0].get("finish_reason"):
                            complete = True
                        delta = choices[0].get("delta") or {}
                        if not isinstance(delta, dict) or not isinstance(delta.get('content', ''), (str, type(None))):
                            raise ModelError('Provider returned an invalid stream. Run kova doctor, then retry.')
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            received_text = received_text or bool(str(reasoning).strip())
                            yield ModelEvent("reasoning", reasoning)
                        if delta.get("content"):
                            received_text = received_text or bool(delta["content"].strip())
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
                            yield ModelEvent("tool_call_delta", tool_call={"index": index, **call})
        except httpx.TimeoutException:
            raise ModelError('Model response timed out. Check the model server, then retry or select a smaller model with kova setup.') from None
        except httpx.RequestError as exc:
            raise ModelError(error + '. Start the model server or run kova setup.') from exc
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ModelError('Provider returned an invalid stream. Run kova doctor, then retry.') from None
        if not received_text and not calls:
            raise ModelError('Model returned an empty response (no reply). Load a chat model with kova setup, then retry.')
        # A clean HTTP EOF is not proof that generation finished. In particular,
        # never release accumulated tool calls from a truncated response. Some
        # compatible servers omit [DONE] but send a terminal finish_reason.
        if not complete:
            raise ModelError("Provider stream ended before completion. Please retry.")
        for kind, value in splitter.flush():
            yield ModelEvent(kind, value)
        for index, call in sorted(calls.items()):
            if not call["id"]:
                call["id"] = f"call_{index}"
            yield ModelEvent("tool_call", tool_call=call)
        if usage:
            yield ModelEvent("usage", usage=usage)
        yield ModelEvent("done")

    async def ping(self) -> bool:
        """Check whether the model server responds successfully."""
        with egress.scope(config=self.config, provider=self.entry.provider or self.entry.name,
                          model=self.entry.model, kind='probe'):
            return await self._ping()

    async def _ping(self) -> bool:
        from kova.providers import ProviderError
        try:
            egress.check_url(self.entry.base_url, self.config)
            headers = await asyncio.to_thread(self._headers)
            async with egress.AsyncClient(transport=self.transport, timeout=min(self.timeout, 3), headers=headers, trust_env=False) as client:
                path = "models?client_version=0.0.0" if self.entry._provider and self.entry._provider.kind == "codex" else "models"
                response = await client.get(self._url(path))
                return response.status_code == 200
        except (httpx.HTTPError, ProviderError, egress.LocalOnlyError):
            return False
