"""Authenticated local-model lifecycle API; both UIs share this Python core."""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from kova import local_models, managed_engine, egress


class InstallBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str
    cpu: bool = False
    backend: Literal['ollama', 'managed'] = 'ollama'


def install_local_model_routes(app, config, require_token):
    router = APIRouter(dependencies=[Depends(require_token)])
    operation = asyncio.Lock()

    @router.get('/local-models')
    async def listing(cpu: bool = False):
        hardware = await local_models.inspect(home=config.home)
        backend = 'ollama' if 'Ollama' in hardware['backends'] else 'managed'
        managed_ids = {local_models._names(m, cpu)[0] for m in local_models.CATALOG for cpu in (False, True)}
        try:
            models = (local_models if backend == 'ollama' else managed_engine).catalog(hardware, cpu=cpu)
            engine_error = ''
        except local_models.LocalModelError as exc:
            models, engine_error = [], str(exc)
        return {'hardware': hardware, 'models': models, 'backend': backend, 'engine_error': engine_error,
                'ollama_present': 'Ollama' in hardware['backends'],
                'disk_note': ('Disk is measured on the Kova model volume. Includes room for the engine archive and extraction.'
                              if backend == 'managed' else
                              'Disk is an estimate for the current account / OLLAMA_MODELS volume. '
                              'A separately running Ollama service may store models elsewhere; check its disk too.'),
                'installed': [*managed_engine.installed(config),
                              *[{'provider': p.id, 'name': p.name, 'backend': 'ollama'} for p in config.providers
                                if p.id in managed_ids and p.kind == 'local' and p.base_url == local_models.OLLAMA_URL + '/v1']]}

    @router.post('/local-models/install')
    async def install(body: InstallBody):
        egress.check_external_process()
        if operation.locked():
            raise HTTPException(409, 'A local-model operation is already running. Wait for it to finish or cancel it.')
        # Validate before starting the SSE response, with a freshly measured fit.
        try:
            model = local_models._model(body.model)
            hardware = await asyncio.to_thread(local_models.measure_hardware, config.home) if body.backend == 'managed' else await asyncio.to_thread(local_models.measure_hardware)
            fit = (managed_engine.fit(body.model, hardware, cpu=body.cpu) if body.backend == 'managed'
                   else local_models.rate(model, hardware, cpu=body.cpu))
            if fit['rating'] == 'too-big':
                raise local_models.LocalModelError(fit['reason'])
        except local_models.LocalModelError as exc:
            raise HTTPException(400, str(exc)) from None

        async def events():
            # Do not queue a second download behind a slow first request.
            if operation.locked():
                yield frame({'type': 'error', 'message': 'Another local-model operation is running. Retry when it finishes.'})
                return
            async with operation:
                implementation = managed_engine if body.backend == 'managed' else local_models
                iterator = implementation.install(config, body.model, hardware, cpu=body.cpu)
                pending = None
                try:
                    while True:
                        pending = asyncio.create_task(anext(iterator))
                        while not pending.done():
                            done, _ = await asyncio.wait({pending}, timeout=10)
                            if not done:
                                yield frame({'type': 'ping'})
                        try:
                            event = pending.result()
                        except StopAsyncIteration:
                            break
                        yield frame(event)
                except local_models.LocalModelError as exc:
                    yield frame({'type': 'error', 'message': str(exc)})
                finally:
                    if pending is not None:
                        pending.cancel()
                        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, local_models.LocalModelError):
                            await pending
                    await iterator.aclose()

        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @router.delete('/local-models/{provider_id}')
    async def remove(provider_id: str):
        if operation.locked():
            raise HTTPException(409, 'A local-model operation is already running. Retry when it finishes.')
        async with operation:
            try:
                managed = any(p['provider'] == provider_id for p in managed_engine.installed(config))
                implementation = managed_engine if managed else local_models
                await implementation.remove(config, provider_id)
            except local_models.LocalModelError as exc:
                raise HTTPException(400, str(exc)) from None
        return {'ok': True, 'message': 'Model removed. Weights shared with another managed variant or Ollama tag may remain.'}

    app.include_router(router)


def frame(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
