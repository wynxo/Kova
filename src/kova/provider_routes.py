"""Authenticated provider management shared by desktop and terminal clients."""
from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kova import provider_auth, providers
from kova.config import Config, ProviderEntry, save_config


class ProviderBody(BaseModel):
    kind: str = 'custom'
    id: str = ''
    name: str = ''
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    auth: dict = Field(default_factory=dict, repr=False)
    consent: bool = False
    models: list[str] = Field(default_factory=list)
    active: str | None = None
    legacy_name: str | None = None
    login_id: str | None = None


def public_provider(p: ProviderEntry) -> dict:
    return {'id': p.id, 'name': p.name, 'kind': p.kind, 'base_url': p.base_url,
            'adapter': p.adapter, 'authenticated': bool(p.api_key or p.auth.get('access_token') or p.auth.get('source')),
            'account': p.auth.get('email', '')}


def install_provider_routes(app, config: Config, require_token):
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request, exc):
        if request.url.path.startswith('/providers') or request.url.path == '/models':
            return JSONResponse(status_code=422, content={'detail': 'Invalid provider fields. Check the address, key and headers.'})
        return await request_validation_exception_handler(request, exc)

    router = APIRouter(dependencies=[Depends(require_token)])
    pending: dict = {}

    async def build(body: ProviderBody):
        identifier = body.id or body.kind
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', identifier):
            raise providers.ProviderError('Connection name must contain only letters, numbers, hyphens or underscores.')
        base_url = body.base_url
        if body.kind == 'local':
            from kova import server_finder
            found = await asyncio.to_thread(server_finder.connect, base_url or '', api_key=body.api_key)
            if not found.answered:
                raise providers.ProviderError('Could not reach that server. Check the address and try again.')
            base_url = found.base_url
        p = providers.make_provider(body.kind, identifier=identifier, name=body.name or identifier,
                                    base_url=base_url, api_key=body.api_key, headers=body.headers)
        if body.auth and (p.kind != 'chatgpt' or body.auth.get('source')):
            if p.kind != 'codex':
                raise providers.ProviderError('Sign in using the provider login flow.')
        if body.auth and p.kind == 'chatgpt':
            p.auth = body.auth  # CLI-owned login, sent over the authenticated loopback API only
        if p.kind == 'codex':
            provider_auth.reuse_codex(p, consent=body.consent)
        if body.login_id:
            attempt = pending.get(body.login_id)
            if not attempt or not attempt.done.is_set() or not attempt.result or attempt.error or p.kind != 'chatgpt':
                raise providers.ProviderError('Finish sign-in before adding this provider.')
            p.auth = dict(attempt.result)
        return p

    async def catalog(p):
        try:
            return await asyncio.to_thread(providers.list_models, p)
        except providers.ProviderError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get('/providers/catalog')
    async def choices():
        return {'providers': [asdict(s) for s in providers.CATALOG], 'harness_available': provider_auth.detect_harness()}

    @router.get('/providers')
    async def listing():
        return [public_provider(p) for p in config.providers]

    @router.post('/providers/validate')
    async def validate(body: ProviderBody):
        try:
            p = await build(body)
        except providers.ProviderError as exc:
            raise HTTPException(400, str(exc)) from None
        return {'models': await catalog(p)}

    @router.post('/providers')
    async def add(body: ProviderBody):
        try:
            p = await build(body)
            available = await catalog(p)
            if not body.models or any(m not in available for m in body.models) or (body.active and body.active not in body.models):
                raise providers.ProviderError('Choose models from this provider catalog, including an active model.')
            providers.install_provider(config, p, body.models, body.active)
            if body.legacy_name:
                config.active_model().name = body.legacy_name
                config.active = body.legacy_name
                save_config(config)
            if body.login_id:
                pending.pop(body.login_id, None)
            return public_provider(p)
        except providers.ProviderError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.delete('/providers/{identifier}')
    async def remove(identifier: str):
        try:
            providers.remove_provider(config, identifier)
        except providers.ProviderError as exc:
            raise HTTPException(404, str(exc)) from None
        return {'ok': True}

    @router.post('/providers/{identifier}/logout')
    async def logout(identifier: str):
        p = next((p for p in config.providers if p.id == identifier), None)
        if not p:
            raise HTTPException(404, 'No provider with that name.')
        confirmed = await asyncio.to_thread(provider_auth.logout, p)
        for model in config.models:
            if model.provider == identifier:
                model.api_key = None
        save_config(config)
        return {'ok': True, 'remote_revocation_confirmed': confirmed}

    @router.post('/providers/{identifier}/login')
    async def login(identifier: str, body: ProviderBody):
        p = next((p for p in config.providers if p.id == identifier), None)
        if not p:
            raise HTTPException(404, 'No provider with that name.')
        if body.auth and (p.kind != 'chatgpt' or body.auth.get('source')):
            raise HTTPException(400, 'Use the provider sign-in flow with explicit consent.')
        candidate = ProviderEntry(p.id, p.name, p.base_url, p.kind, p.adapter,
                                  api_key=body.api_key, headers=dict(p.headers), auth=dict(body.auth or p.auth))
        candidate._home = config.home
        try:
            if p.kind == 'codex':
                provider_auth.reuse_codex(candidate, consent=body.consent)
            await catalog(candidate)
        except providers.ProviderError as exc:
            raise HTTPException(400, str(exc)) from None
        p.auth, p.api_key = candidate.auth, candidate.api_key
        for model in config.models:
            if model.provider == identifier:
                model.api_key = p.api_key
        save_config(config)
        return public_provider(p)

    @router.post('/providers/auth/start')
    async def start():
        # Bound pending listeners and expire them in LoginAttempt, including cancelled UI flows.
        for key, attempt in list(pending.items()):
            if attempt.done.is_set():
                pending.pop(key)
        if len(pending) >= 4:
            raise HTTPException(429, 'A sign-in is already pending. Finish it or retry in five minutes.')
        key = secrets.token_urlsafe(24)
        save_config(config)  # Persist host identity before the first browser authorization.
        p = providers.make_provider('chatgpt')
        p.auth['ext_agent_host_id'] = config.oauth_host_id
        attempt = provider_auth.LoginAttempt(p)
        pending[key] = attempt
        return {'login_id': key, 'url': attempt.url}

    @router.get('/providers/auth/{key}')
    async def login_status(key: str):
        attempt = pending.get(key)
        if not attempt:
            raise HTTPException(404, 'Sign-in expired. Please retry.')
        if attempt.error:
            raise HTTPException(400, attempt.error)
        return {'complete': bool(attempt.done.is_set() and attempt.result)}

    @router.delete('/providers/auth/{key}')
    async def cancel(key: str):
        attempt = pending.pop(key, None)
        if attempt:
            attempt.cancel()
        return {'ok': True}

    app.include_router(router)
