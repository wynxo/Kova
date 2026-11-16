"""Conservative local-model recommendations and explicit Ollama operations.

Only curated tags and the fixed loopback Ollama endpoint are accepted. Ollama
owns its downloads, including resuming partially downloaded layers. The managed
llama.cpp alternative lives in managed_engine.py and engine_downloads.py.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass

import httpx

from kova import egress

from kova import providers
from kova.config import Config
from kova.tools.system_info import hardware_profile

GIB = 1024 ** 3
OLLAMA_URL = 'http://127.0.0.1:11434'
DISK_RESERVE = GIB
GPU_RESERVE = GIB
RAM_RESERVE = 2 * GIB


class LocalModelError(RuntimeError):
    """An actionable local-model failure without upstream bodies or local paths."""


@dataclass(frozen=True)
class LocalModel:
    id: str
    name: str
    params: str
    quant: str
    download_bytes: int
    recommended_vram_bytes: int
    recommended_ram_bytes: int
    context: int
    license_url: str
    cpu_candidate: bool = False


# Sizes are rounded up from the Ollama library, not promises about peak memory.
# Memory estimates include a 4K context; the OS/display reserve is added by rate().
CATALOG = (
    LocalModel('qwen3:4b', 'Qwen3 4B', '4.02B', 'Q4_K_M', 2_600_000_000,
               4 * GIB, 6 * GIB, 4096, 'https://huggingface.co/Qwen/Qwen3-4B/blob/main/LICENSE'),
    LocalModel('qwen3:8b', 'Qwen3 8B', '8.19B', 'Q4_K_M', 5_300_000_000,
               7 * GIB, 10 * GIB, 4096, 'https://huggingface.co/Qwen/Qwen3-8B/blob/main/LICENSE'),
    LocalModel('llama3.2:3b', 'Llama 3.2 3B', '3.21B', 'Q4_K_M', 2_100_000_000,
               4 * GIB, 5 * GIB, 4096, 'https://github.com/meta-llama/llama-models/blob/main/models/llama3_2/LICENSE', True),
)


def _model(identifier: str) -> LocalModel:
    model = next((m for m in CATALOG if m.id == identifier), None)
    if model is None:
        raise LocalModelError('Choose a model from the local catalog.')
    return model


def _names(model: LocalModel, cpu: bool) -> tuple[str, str]:
    stem = 'kova-' + model.id.replace(':', '-') + ('-cpu' if cpu else '-gpu')
    return stem, stem + ':latest'


def rate(model: LocalModel, profile: dict, *, cpu: bool = False, check_disk: bool = True) -> dict:
    """Estimate fit with explicit reserves; unknown resources never imply fit."""
    result = dict(rating='too-big', reason='', disk_shortfall_bytes=0)
    free = profile.get('free_disk')
    required_disk = model.download_bytes + DISK_RESERVE
    if check_disk and free is None:
        result['reason'] = 'Free disk space could not be measured. Check the Ollama model volume before retrying.'
        return result
    if check_disk and free < required_disk:
        shortfall = required_disk - free
        result.update(disk_shortfall_bytes=shortfall,
                      reason=f'Not enough disk space: need {shortfall} more bytes on the estimated model volume.')
        return result
    ram = profile.get('ram_bytes')
    available = profile.get('available_ram_bytes')
    # Even GPU inference needs host memory for loading/staging and the OS.
    if ram is None or ram < model.recommended_ram_bytes + RAM_RESERVE:
        result['reason'] = 'Not enough measured RAM for this model plus an operating-system reserve.'
        return result
    if available is not None and available < model.recommended_ram_bytes:
        result['reason'] = 'Not enough RAM is currently available. Close other apps or choose a smaller model.'
        return result
    if cpu:
        if not model.cpu_candidate:
            result['reason'] = 'CPU mode offers the small 3B model only; larger models may be impractically slow.'
            return result
        budget = min(ram - RAM_RESERVE, available if available is not None else ram - RAM_RESERVE)
        needed = model.recommended_ram_bytes
        warning = 'CPU-only inference can be slow. '
    else:
        vram = profile.get('vram_bytes')
        if not vram:
            result['reason'] = 'GPU memory could not be measured. Explicitly choose CPU mode for the small 3B model (slower).'
            return result
        budget, needed = vram - GPU_RESERVE, model.recommended_vram_bytes
        warning = ''
    if budget < needed:
        result['reason'] = 'This model exceeds the measured memory budget after reserves. Choose a smaller model or CPU mode.'
        return result
    tight = budget < needed * 1.5
    result.update(rating='fits-tightly' if tight else 'fits-comfortably',
                  reason=warning + ('Limited headroom; close GPU-heavy apps. ' if tight else '') +
                  'Estimate at 4096 tokens, not a guarantee. Other apps and Ollama settings affect memory use.')
    return result


def measure_hardware(home=None) -> dict:
    """Read hardware using the optional model-volume hint, without opening weights."""
    return hardware_profile(home if home is not None else os.environ.get('OLLAMA_MODELS'))


async def inspect(*, transport=None, home=None) -> dict:
    """Opt-in local hardware and backend check; never scan a LAN or pull a model."""
    # OLLAMA_MODELS is a hint only: a separately launched daemon may use another
    # account, volume or environment. Never expose that path to clients.
    profile = await asyncio.to_thread(measure_hardware)
    async with egress.AsyncClient(transport=transport, timeout=2, trust_env=False) as client:
        async def check(name, url, key):
            try:
                response = await client.get(url)
                data = response.json()
                return name if response.is_success and isinstance(data, dict) and key in data else None
            except (httpx.HTTPError, ValueError):
                return None
        found = await asyncio.gather(
            check('Ollama', OLLAMA_URL + '/api/version', 'version'),
            check('LM Studio', 'http://127.0.0.1:1234/v1/models', 'data'),
            check('llama.cpp', 'http://127.0.0.1:8080/v1/models', 'data'))
    profile['backends'] = [name for name in found if name]
    if home is not None and 'Ollama' not in profile['backends']:
        profile = {**await asyncio.to_thread(measure_hardware, home), 'backends': profile['backends']}
    return profile


def catalog(profile: dict, *, cpu: bool = False) -> list[dict]:
    """Public catalog with fit reasons for this explicitly requested profile."""
    return [{**asdict(m), **rate(m, profile, cpu=cpu)} for m in CATALOG]


def _failure(stage: str, identifier: str) -> LocalModelError:
    if stage == 'download':
        return LocalModelError(f'Ollama could not finish the download. Check disk space and internet access, then retry to resume. '
                               f'If the model host is unreachable, try `ollama pull {identifier}` later. '
                               'Install/start Ollama from https://ollama.com/download if needed (`ollama serve`). No provider was added.')
    return LocalModelError(f'The model downloaded, but Ollama could not {stage}. It may be out of memory or need an update. '
                           'Choose a smaller model or explicit CPU mode, then retry. No provider was added.')


def _object(response: httpx.Response) -> dict:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError('invalid Ollama response')
    return data


async def install(cfg: Config, identifier: str, profile: dict, *, cpu: bool = False, transport=None) -> AsyncIterator[dict]:
    """Pull, persist safe runtime defaults, warm up, then register the provider.

    Cancellation closes the stream. Retry delegates resume to Ollama's layer
    cache. Only an explicit terminal success and completed warmup permit saving.
    """
    model = _model(identifier)
    fit = rate(model, profile, cpu=cpu)
    if fit['rating'] == 'too-big':
        raise LocalModelError(fit['reason'])
    provider_id, runtime_model = _names(model, cpu)
    if any(p.id == provider_id for p in cfg.providers):
        raise LocalModelError('This model connection already exists. Select it in the model picker, or remove it before reinstalling.')
    stage = 'download'
    try:
        async with egress.AsyncClient(base_url=OLLAMA_URL, transport=transport,
                                     timeout=httpx.Timeout(180, connect=3), trust_env=False) as client:
            yield {'type': 'progress', 'stage': 'download', 'message': 'Downloading with Ollama; retry resumes interrupted layers.'}
            success = False
            async with client.stream('POST', '/api/pull', json={'model': model.id, 'stream': True}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if not isinstance(data, dict) or data.get('error') or not isinstance(data.get('status'), str):
                        raise ValueError('invalid pull status')
                    if data['status'] == 'success':
                        success = True
                        break
                    # Never forward arbitrary upstream status/error strings.
                    event = {'type': 'progress', 'stage': 'download', 'message': 'Downloading / verifying model layers…'}
                    total, completed = data.get('total'), data.get('completed')
                    if type(total) is int and total > 0 and type(completed) is int and completed >= 0:
                        event.update(total=total, completed=min(completed, total))
                    yield event
            if not success:
                raise ValueError('pull ended before success')
            stage = 'configure runtime settings'
            yield {'type': 'progress', 'stage': 'configure', 'message': 'Setting a bounded 4096-token context for future chats…'}
            parameters = {'num_ctx': model.context}
            if cpu:
                parameters['num_gpu'] = 0
            data = _object(await client.post('/api/create', json={
                'model': runtime_model, 'from': model.id, 'parameters': parameters, 'stream': False}))
            if data.get('status') != 'success':
                raise ValueError('create did not finish')
            stage = 'load the model'
            yield {'type': 'progress', 'stage': 'warmup', 'message': 'Loading the model (no chat message is sent)…'}
            data = _object(await client.post('/api/generate', json={
                'model': runtime_model, 'prompt': '', 'stream': False, 'keep_alive': '5m'}))
            if data.get('done') is not True:
                raise ValueError('warmup did not finish')
            if not cpu:
                stage = 'verify GPU loading'
                running = _object(await client.get('/api/ps')).get('models', [])
                loaded = next((m for m in running if isinstance(m, dict) and
                               m.get('name', m.get('model')) == runtime_model), {})
                size, vram = loaded.get('size'), loaded.get('size_vram')
                if not (type(size) is int and type(vram) is int and size > 0 and vram >= size):
                    raise LocalModelError('Ollama did not confirm full GPU loading. The model may be using CPU offload. '
                                          'Update GPU drivers/Ollama, close GPU-heavy apps or explicitly choose CPU mode. No provider was added.')
    except (httpx.HTTPError, ValueError, TypeError):
        raise _failure(stage, model.id) from None
    provider = providers.make_provider('local', identifier=provider_id,
                                       name=f'{model.name} on this PC' + (' (CPU)' if cpu else ''),
                                       base_url=OLLAMA_URL + '/v1')
    previous = (list(cfg.models), list(cfg.providers), cfg.active)
    try:
        providers.install_provider(cfg, provider, [runtime_model], context=model.context)
    except (providers.ProviderError, OSError):
        cfg.models[:], cfg.providers[:], cfg.active = previous
        raise LocalModelError('The model is ready in Ollama, but Kova could not save its connection. Check config permissions and retry.') from None
    yield {'type': 'done', 'provider': provider_id, 'model': runtime_model,
           'message': 'Model ready. Your next chat uses this PC.'}


async def remove(cfg: Config, provider_id: str, *, transport=None) -> None:
    """Remove only a catalog-managed connection and its Ollama tags, on request.

    Shared layers may remain if another tag uses them. The Ollama service and
    every unrelated provider/model are left alone.
    """
    match = next(((m, cpu) for m in CATALOG for cpu in (False, True)
                  if _names(m, cpu)[0] == provider_id), None)
    p = next((p for p in cfg.providers if p.id == provider_id), None)
    if match is None or p is None or p.kind != 'local' or p.base_url != OLLAMA_URL + '/v1':
        raise LocalModelError('No Kova-managed local model with that connection name.')
    model, cpu = match
    _, runtime = _names(model, cpu)
    if any(m.provider == provider_id and m.model != runtime for m in cfg.models):
        raise LocalModelError('This connection contains other models. Remove it through provider settings instead.')
    previous = None
    try:
        async with egress.AsyncClient(base_url=OLLAMA_URL, transport=transport, timeout=30, trust_env=False) as client:
            response = await client.post('/api/generate', json={'model': runtime, 'keep_alive': 0, 'stream': False})
            if response.status_code != 404:
                _object(response)
            # Keep a source tag referenced by another configured model/variant.
            shared = any(m.provider != provider_id and m.model in (model.id, _names(model, not cpu)[1]) for m in cfg.models)
            for tag in ([runtime] if shared else [runtime, model.id]):
                response = await client.request('DELETE', '/api/delete', json={'model': tag})
                if response.status_code != 404:
                    response.raise_for_status()
        previous = (list(cfg.models), list(cfg.providers), cfg.active)
        providers.remove_provider(cfg, provider_id)
    except (httpx.HTTPError, ValueError, OSError):
        if previous is not None:
            cfg.models[:], cfg.providers[:], cfg.active = previous
        raise LocalModelError('Ollama could not finish removing the model. Some tags may already be removed; retry. The connection was kept.') from None
