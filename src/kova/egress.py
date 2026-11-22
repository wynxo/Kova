"""Metadata-only accounting and Local only enforcement at HTTP transport boundaries.

Counts are HTTP body bytes, not TLS/header overhead. Tokens are provider-reported
usage, or unknown. No URL paths, queries, headers or bodies reach the ledger.
This is Kova's network policy, not an OS firewall for other applications.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, replace
from functools import wraps

import httpx

from kova.privacy import is_loopback_url


class LocalOnlyError(Exception):
    """A destination was refused before DNS, credentials or a socket."""

    def __init__(self):
        super().__init__('Local only is on. Choose a loopback model or turn it off in Privacy.')


@dataclass
class _Context:
    config: object = None
    config_loader: object = None
    store: object = None
    chat_id: str | None = None
    provider: str = ''
    model: str = ''
    kind: str = 'network'
    items: list | None = None
    audit: bool = True


_current: ContextVar[_Context | None] = ContextVar('kova_egress', default=None)


@contextmanager
def scope(**values):
    """Attribute work across async tasks and copied worker-thread contexts."""
    values = {key: value for key, value in values.items() if value is not None}
    context = replace(_current.get() or _Context(), **values)
    if context.store is None and context.config is not None:
        context.store = getattr(context.config, '_egress_store', None)
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def _config(context):
    if context is None:
        return None
    if context.config is None and context.config_loader is not None:
        context.config = context.config_loader()
    return context.config


def check_url(url: str, config=None) -> None:
    """Fail closed for every non-loopback URL; never perform DNS to trust a name."""
    context = _current.get()
    config = config if config is not None else _config(context)
    if getattr(config, 'privacy_local_only', False) and not is_loopback_url(url):
        raise LocalOnlyError()


def attributed(kind):
    """Keep synchronous credential/probe work attributed through worker threads."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            provider = getattr(args[0], 'id', None) if args else None
            with scope(kind=kind, provider=provider):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def bind_context(function):
    """Copy attribution/policy into each invocation of a thread-pool callback."""
    context = copy_context()
    @wraps(function)
    def bound(*args, **kwargs):
        return context.copy().run(function, *args, **kwargs)
    return bound


def check_external_process():
    context = _current.get()
    if context and getattr(_config(context), 'privacy_local_only', False):
        raise LocalOnlyError()


def get(url, **kwargs):
    with Client() as client:
        return client.get(url, **kwargs)


class _Observation:
    def __init__(self, request):
        self.context = _current.get()
        self.url = str(request.url)
        check_url(self.url)
        self.host = request.extensions.get('egress_host', request.url.host)
        self.address = request.extensions.get('egress_address', '')
        try:
            self.outgoing = len(request.content)
        except httpx.RequestNotRead:
            self.outgoing = int(request.headers.get('content-length', 0))
        self.incoming = 0
        self.tokens_in = self.tokens_out = None
        self.buffer = b''
        self.status = 'partial'
        self.finished = False

    def feed(self, data):
        self.incoming += len(data)
        # Only model SSE usage is inspected, in a bounded temporary line buffer.
        # It is never persisted. Unbounded/multimedia lines are discarded.
        if not self.context or self.context.kind != 'model':
            return
        self.buffer += data
        lines = self.buffer.split(b'\n')
        self.buffer = lines.pop()[-65536:]
        for line in lines:
            if len(line) > 65536 or not line.startswith(b'data:'):
                continue
            try:
                frame = json.loads(line[5:])
                usage = (frame.get('usage') or frame.get('message', {}).get('usage')
                         or frame.get('response', {}).get('usage') or {})
                for attr, keys in (('tokens_in', ('prompt_tokens', 'input_tokens')),
                                   ('tokens_out', ('completion_tokens', 'output_tokens'))):
                    for key in keys:
                        value = usage.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            setattr(self, attr, value)
            except (ValueError, AttributeError, TypeError):
                pass

    def finish(self):
        if self.finished:
            return
        self.finished = True
        self.buffer = b''
        ctx = self.context
        if ctx is None or not ctx.audit:
            return
        item = dict(chat_id=ctx.chat_id, kind=ctx.kind, host=self.host,
                    provider=ctx.provider, model=ctx.model,
                    local=is_loopback_url(self.url), resolved_ip=self.address,
                    bytes_in=self.incoming, bytes_out=self.outgoing,
                    tokens_in=self.tokens_in, tokens_out=self.tokens_out, status=self.status)
        store = ctx.store or getattr(ctx.config, '_egress_store', None)
        if store is None and ctx.config is not None:
            from kova.store import Store
            store = Store(ctx.config.home / 'kova.db')
            ctx.config._egress_store = store
        if store is not None:
            item = store.record_egress(**item)
        if ctx.items is not None:
            ctx.items.append(item)


class _SyncStream(httpx.SyncByteStream):
    def __init__(self, stream, observation):
        self.stream, self.observation = stream, observation

    def __iter__(self):
        for data in self.stream:
            self.observation.feed(data)
            yield data
        self.observation.status = 'complete'

    def close(self):
        try:
            self.stream.close()
        finally:
            self.observation.finish()


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, stream, observation):
        self.stream, self.observation = stream, observation

    async def __aiter__(self):
        async for data in self.stream:
            self.observation.feed(data)
            yield data
        self.observation.status = 'complete'

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            self.observation.finish()


class _Transport(httpx.BaseTransport):
    def __init__(self, inner):
        self.inner = inner

    def handle_request(self, request):
        observation = _Observation(request)
        try:
            response = self.inner.handle_request(request)
        except BaseException:
            observation.status = 'failed'
            observation.finish()
            raise
        if response.is_stream_consumed:
            observation.feed(response.content)
            observation.status = 'complete'
            observation.finish()
        else:
            response.stream = _SyncStream(response.stream, observation)
        return response

    def close(self):
        self.inner.close()


class _AsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        observation = _Observation(request)
        try:
            response = await self.inner.handle_async_request(request)
        except BaseException:
            observation.status = 'failed'
            observation.finish()
            raise
        if response.is_stream_consumed:
            observation.feed(response.content)
            observation.status = 'complete'
            observation.finish()
        else:
            response.stream = _AsyncStream(response.stream, observation)
        return response

    async def aclose(self):
        await self.inner.aclose()


class Client(httpx.Client):
    """HTTP client with policy enforcement on every hop, including redirects."""
    def __init__(self, *args, transport=None, **kwargs):
        kwargs['trust_env'] = False  # proxies must not bypass loopback policy
        super().__init__(*args, transport=_Transport(transport or httpx.HTTPTransport()), **kwargs)


class AsyncClient(httpx.AsyncClient):
    """Async counterpart; response accounting survives cancellation and errors."""
    def __init__(self, *args, transport=None, **kwargs):
        kwargs['trust_env'] = False
        super().__init__(*args, transport=_AsyncTransport(transport or httpx.AsyncHTTPTransport()), **kwargs)


class PolicyMiddleware:
    """Keep policy active for the entire response, including streaming tasks."""
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope_, receive, send):
        with scope(config=self.config):
            await self.app(scope_, receive, send)
