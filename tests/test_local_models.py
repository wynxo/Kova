"""Offline local-model fitting and Ollama lifecycle tests (synthetic hardware)."""
import json
from collections import namedtuple

import httpx
import pytest

from kova import config
from kova.tools import system_info as si

GIB = 1024 ** 3


def profile(**changes):
    return {'gpu': 'Demo GPU', 'vram_bytes': 8 * GIB, 'ram_bytes': 16 * GIB,
            'available_ram_bytes': 12 * GIB, 'free_disk': 50 * GIB, 'os': 'Demo OS',
            'backends': [], **changes}


def test_hardware_profile_is_structured_and_does_not_sum_gpus(monkeypatch, tmp_path):
    monkeypatch.setattr(si, '_gpus', lambda: [('Small demo GPU', 2 * GIB), ('Demo GPU', 8 * GIB)])
    monkeypatch.setattr(si, '_ram', lambda: (16 * GIB, 4 * GIB))
    monkeypatch.setattr(si.platform, 'system', lambda: 'Demo OS')
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(si.shutil, 'disk_usage', lambda path: usage(100 * GIB, 50 * GIB, 50 * GIB))
    assert hasattr(si, 'hardware_profile'), 'structured hardware profile is missing'
    data = si.hardware_profile(tmp_path)
    assert data == profile()


def test_hardware_profile_unknown_is_not_zero_or_an_exception(monkeypatch, tmp_path):
    def broken(*a):
        raise OSError('probe unavailable')
    monkeypatch.setattr(si, '_gpus', broken)
    monkeypatch.setattr(si, '_ram', broken)
    monkeypatch.setattr(si.shutil, 'disk_usage', broken)
    assert hasattr(si, 'hardware_profile'), 'structured hardware profile is missing'
    data = si.hardware_profile(tmp_path)
    assert data['vram_bytes'] is None and data['ram_bytes'] is None and data['free_disk'] is None


@pytest.mark.parametrize('vram,want', [(8, 'fits-comfortably'), (6, 'fits-tightly'), (4, 'too-big')])
def test_fit_reserves_gpu_memory(vram, want):
    from kova.local_models import CATALOG, rate
    model = next(m for m in CATALOG if m.id == 'qwen3:4b')
    assert rate(model, profile(vram_bytes=vram * GIB))['rating'] == want


def test_cpu_requires_explicit_choice_and_disk_reports_exact_shortfall():
    from kova.local_models import CATALOG, rate
    model = next(m for m in CATALOG if m.id == 'llama3.2:3b')
    hardware = profile(gpu=None, vram_bytes=None)
    assert rate(model, hardware)['rating'] == 'too-big'
    assert rate(model, hardware, cpu=True)['rating'] == 'fits-comfortably'
    result = rate(model, profile(free_disk=1), cpu=True)
    assert result['rating'] == 'too-big'
    assert result['disk_shortfall_bytes'] == model.download_bytes + GIB - 1
    assert str(result['disk_shortfall_bytes']) in result['reason']
    assert rate(model, profile(free_disk=None))['rating'] == 'too-big'


def ollama_handler(request):
    path = request.url.path
    assert request.url.host == '127.0.0.1', 'never use a remote host or proxy'
    if path == '/api/pull':
        body = json.loads(request.content)
        assert body == {'model': 'qwen3:4b', 'stream': True}
        return httpx.Response(200, text='\n'.join(map(json.dumps, [
            {'status': 'pulling manifest'},
            {'status': 'pulling demo-layer', 'digest': 'sha256:demo', 'total': 100, 'completed': 30},
            {'status': 'success'}])) + '\n')
    if path == '/api/create':
        body = json.loads(request.content)
        assert body['from'] == 'qwen3:4b'
        assert body['parameters']['num_ctx'] == 4096
        if body['model'].endswith('-cpu:latest'):
            assert body['parameters']['num_gpu'] == 0
        else:
            assert 'num_gpu' not in body['parameters']
        return httpx.Response(200, json={'status': 'success'})
    if path == '/api/generate':
        body = json.loads(request.content)
        assert body['prompt'] == '' and body['stream'] is False and body['keep_alive'] == '5m'
        return httpx.Response(200, json={'done': True, 'response': ''})
    if path == '/api/ps':
        return httpx.Response(200, json={'models': [{'name': 'kova-qwen3-4b-gpu:latest', 'size': 4 * GIB, 'size_vram': 4 * GIB}]})
    raise AssertionError(f'unexpected Ollama operation: {path}')


async def test_install_streams_progress_then_persists_provider(tmp_path):
    from kova.local_models import install
    cfg = config.load_config(tmp_path)
    events = [e async for e in install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(ollama_handler))]
    assert events[-1]['type'] == 'done'
    progress = next(e for e in events if e.get('total') == 100)
    assert progress['completed'] == 30
    saved = config.load_config(tmp_path)
    provider = next(p for p in saved.providers if p.id == events[-1]['provider'])
    assert provider.kind == 'local' and provider.base_url == 'http://127.0.0.1:11434/v1'
    assert saved.active_model().model == 'kova-qwen3-4b-gpu:latest'
    assert saved.active_model().context == 4096


@pytest.mark.parametrize('failure', ['truncated', 'error', 'bad-json', 'warmup', 'refused'])
async def test_failed_install_never_registers_or_leaks_upstream(tmp_path, failure):
    from kova.local_models import install, LocalModelError
    cfg = config.load_config(tmp_path)
    before = cfg.active
    def handle(request):
        if request.url.path == '/api/pull':
            if failure == 'truncated':
                return httpx.Response(200, text='{"status":"pulling manifest"}\n')
            if failure == 'error':
                return httpx.Response(200, text='{"error":"private-upstream-body"}\n')
            if failure == 'bad-json':
                return httpx.Response(200, text='not json\n')
            if failure == 'refused':
                raise httpx.ConnectError('private-upstream-body', request=request)
        if request.url.path == '/api/generate' and failure == 'warmup':
            return httpx.Response(500, text='private-upstream-body')
        return ollama_handler(request)
    with pytest.raises(LocalModelError) as caught:
        _ = [e async for e in install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(handle))]
    assert 'private-upstream-body' not in str(caught.value)
    assert cfg.active == before and not any(p.id.startswith('kova-') for p in cfg.providers)


async def test_too_big_and_unknown_models_never_contact_ollama(tmp_path):
    from kova.local_models import install, LocalModelError
    def fail(request):
        pytest.fail('no network before fit validation')
    for identifier in ['qwen3:4b', 'arbitrary:tag']:
        with pytest.raises(LocalModelError):
            _ = [e async for e in install(config.load_config(tmp_path), identifier,
                 profile(vram_bytes=GIB), transport=httpx.MockTransport(fail))]


async def test_inspection_only_probes_loopback_and_never_installs(monkeypatch):
    from kova import local_models
    monkeypatch.setattr(local_models, 'hardware_profile', lambda path=None: profile())
    seen = []
    def handle(request):
        seen.append((request.method, request.url.port, request.url.path))
        assert request.url.host == '127.0.0.1'
        if request.url.path == '/api/version':
            return httpx.Response(200, json={'version': 'demo'})
        if request.url.port == 1234:
            return httpx.Response(200, json={'data': []})
        raise httpx.ConnectError('not listening', request=request)
    result = await local_models.inspect(transport=httpx.MockTransport(handle))
    assert result['backends'] == ['Ollama', 'LM Studio']
    assert len(seen) == 3 and all(method == 'GET' for method, _, _ in seen)


async def test_cpu_defaults_survive_provider_registration(tmp_path):
    from kova.local_models import install
    def handle(request):
        if request.url.path == '/api/pull':
            assert json.loads(request.content)['model'] == 'llama3.2:3b'
            return httpx.Response(200, json={'status': 'success'})
        if request.url.path == '/api/create':
            body = json.loads(request.content)
            assert body['parameters'] == {'num_ctx': 4096, 'num_gpu': 0}
            assert body['from'] == 'llama3.2:3b'
            return httpx.Response(200, json={'status': 'success'})
        return ollama_handler(request)
    events = [e async for e in install(config.load_config(tmp_path), 'llama3.2:3b',
                                       profile(gpu=None, vram_bytes=None), cpu=True,
                                       transport=httpx.MockTransport(handle))]
    assert events[-1]['model'] == 'kova-llama3.2-3b-cpu:latest'
    assert config.load_config(tmp_path).active_model().model == events[-1]['model']


async def test_remove_deletes_only_managed_tags_and_connection(tmp_path):
    from kova.local_models import install, remove
    cfg = config.load_config(tmp_path)
    events = [e async for e in install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(ollama_handler))]
    deleted = []
    def handle(request):
        if request.url.path == '/api/generate':
            assert json.loads(request.content)['keep_alive'] == 0
            return httpx.Response(200, json={'done': True})
        assert request.method == 'DELETE' and request.url.path == '/api/delete'
        deleted.append(json.loads(request.content)['model'])
        return httpx.Response(200)
    await remove(cfg, events[-1]['provider'], transport=httpx.MockTransport(handle))
    assert deleted == ['kova-qwen3-4b-gpu:latest', 'qwen3:4b']
    assert not any(p.id == events[-1]['provider'] for p in config.load_config(tmp_path).providers)


def test_local_routes_require_auth_and_stream_real_install(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from kova import local_models
    from kova.server import create_app
    cfg = config.load_config(tmp_path)
    monkeypatch.setattr(local_models, 'hardware_profile', lambda path=None: profile())
    real_client = httpx.AsyncClient
    def handle(request):
        if request.method == 'GET' and request.url.path != '/api/ps':
            return httpx.Response(200, json={'version': 'demo'}) if request.url.port == 11434 else httpx.Response(404)
        return ollama_handler(request)
    monkeypatch.setattr(local_models.httpx, 'AsyncClient', lambda **kw: real_client(**{**kw, 'transport': httpx.MockTransport(handle)}))
    with TestClient(create_app(cfg)) as client:
        assert client.get('/local-models').status_code == 401
        assert client.post('/local-models/install', json={'model': 'qwen3:4b'}).status_code == 401
        headers = {'Authorization': 'Bearer ' + config.ensure_token(tmp_path)}
        listing = client.get('/local-models', headers=headers)
        assert listing.status_code == 200
        assert listing.json()['hardware']['backends'] == ['Ollama']
        response = client.post('/local-models/install', json={'model': 'qwen3:4b'}, headers=headers)
        assert response.headers['content-type'].startswith('text/event-stream')
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'done'
        assert config.load_config(tmp_path).active_model().context == 4096
        assert client.post('/local-models/install', json={'model': 'unknown'}, headers=headers).status_code == 400


def test_setup_pc_flow_uses_same_core_and_requires_download_consent(tmp_path, monkeypatch, capsys):
    from kova import onboarding, local_models
    cfg = config.load_config(tmp_path)
    monkeypatch.setattr(onboarding, '_running_client', lambda cfg: None)
    monkeypatch.setattr(local_models, 'hardware_profile', lambda path=None: profile())
    answers = iter(['y'])
    monkeypatch.setattr(onboarding, '_ask', lambda *a, **kw: next(answers))
    monkeypatch.setattr(onboarding, '_select', lambda *a, **kw: 0)
    def handle(request):
        if request.method == 'GET' and request.url.path != '/api/ps':
            return httpx.Response(200, json={'version': 'demo'}) if request.url.port == 11434 else httpx.Response(404)
        return ollama_handler(request)
    onboarding.run_setup(cfg, provider_kind='this-pc', transport=httpx.MockTransport(handle))
    saved = config.load_config(tmp_path)
    assert saved.active_model().model == 'kova-qwen3-4b-gpu:latest'
    assert 'Demo GPU' in capsys.readouterr().out


def test_client_rejects_incomplete_local_install_stream():
    from kova.client import KovaClient, KovaError
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text='data: {"type":"progress"}\n\n'))
    with httpx.Client(base_url='http://kova.test', transport=transport) as http:
        client = KovaClient('http://kova.test', 'demo', http=http)
        with pytest.raises(KovaError, match='before completion'):
            list(client.local_model_install('qwen3:4b'))


async def test_gpu_choice_does_not_silently_fall_back_to_cpu(tmp_path):
    from kova.local_models import install, LocalModelError
    def handle(request):
        if request.url.path == '/api/ps':
            return httpx.Response(200, json={'models': [{'name': 'kova-qwen3-4b-gpu:latest', 'size': 4000000000, 'size_vram': 0}]})
        return ollama_handler(request)
    with pytest.raises(LocalModelError, match='GPU|CPU'):
        _ = [e async for e in install(config.load_config(tmp_path), 'qwen3:4b', profile(),
                                      transport=httpx.MockTransport(handle))]
    assert not any(p.id.startswith('kova-') for p in config.load_config(tmp_path).providers)


async def test_save_failure_does_not_change_live_active_provider(tmp_path, monkeypatch):
    from kova import local_models, providers
    cfg = config.load_config(tmp_path)
    before = (cfg.active, list(cfg.models), list(cfg.providers))
    def fail(cfg):
        raise OSError('private-upstream-body')
    monkeypatch.setattr(providers, 'save_config', fail)
    with pytest.raises(local_models.LocalModelError, match='save'):
        _ = [e async for e in local_models.install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(ollama_handler))]
    assert (cfg.active, cfg.models, cfg.providers) == before


async def test_cancel_closes_pull_and_retry_resumes_without_partial_registration(tmp_path):
    import asyncio
    from kova.local_models import install
    cfg = config.load_config(tmp_path)
    waiting = asyncio.Event()
    closed = []
    class SlowPull(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"status":"pulling manifest"}\n'
            waiting.set()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.append(True)
    def handle(request):
        if request.url.path == '/api/pull':
            return httpx.Response(200, stream=SlowPull())
        pytest.fail('cancelled pull must not warm up or create a provider')
    iterator = install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(handle))
    await anext(iterator)  # preparing
    await anext(iterator)  # manifest
    pending = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await iterator.aclose()
    assert closed and not any(p.id.startswith('kova-') for p in cfg.providers)
    events = [e async for e in install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(ollama_handler))]
    assert events[-1]['type'] == 'done'


async def test_remove_keeps_shared_source_and_retains_config_on_http_failure(tmp_path):
    from kova.local_models import install, remove, LocalModelError
    from kova import providers
    cfg = config.load_config(tmp_path)
    events = [e async for e in install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(ollama_handler))]
    other = providers.make_provider('local', identifier='other', base_url='http://127.0.0.1:11434/v1')
    providers.install_provider(cfg, other, ['qwen3:4b'])
    deleted = []
    def handle(request):
        if request.url.path == '/api/generate':
            return httpx.Response(404)  # alias already absent after a partial removal
        deleted.append(json.loads(request.content)['model'])
        return httpx.Response(200)
    provider = events[-1]['provider']
    with pytest.raises(LocalModelError):
        await remove(cfg, provider, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert any(p.id == provider for p in cfg.providers)
    await remove(cfg, provider, transport=httpx.MockTransport(handle))
    assert deleted == ['kova-qwen3-4b-gpu:latest']
    assert any(p.id == 'other' for p in config.load_config(tmp_path).providers)


def test_setup_declining_download_and_missing_ollama_are_honest(tmp_path, monkeypatch):
    from kova import onboarding, local_models
    cfg = config.load_config(tmp_path)
    monkeypatch.setattr(onboarding, '_running_client', lambda cfg: None)
    monkeypatch.setattr(local_models, 'hardware_profile', lambda path=None: profile())
    monkeypatch.setattr(onboarding, '_select', lambda *a, **kw: 0)
    monkeypatch.setattr(onboarding, '_ask', lambda *a, **kw: 'n')
    before = (tmp_path / 'config.toml').read_bytes()
    def handle(request):
        assert request.method == 'GET', 'declining must never start a pull or warmup'
        return httpx.Response(200, json={'version': 'demo'})
    with pytest.raises(onboarding.SetupError, match='Nothing downloaded'):
        onboarding.run_setup(cfg, provider_kind='this-pc', transport=httpx.MockTransport(handle))
    assert (tmp_path / 'config.toml').read_bytes() == before
    with pytest.raises(onboarding.SetupError, match='ollama serve'):
        onboarding.run_setup(cfg, provider_kind='this-pc', transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert (tmp_path / 'config.toml').read_bytes() == before


async def test_remove_save_failure_restores_live_connection(tmp_path, monkeypatch):
    from kova import local_models, providers
    cfg = config.load_config(tmp_path)
    events = [e async for e in local_models.install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(ollama_handler))]
    before = (cfg.active, list(cfg.models), list(cfg.providers))
    def fail(cfg):
        raise OSError('could not write')
    monkeypatch.setattr(providers, 'save_config', fail)
    with pytest.raises(local_models.LocalModelError):
        await local_models.remove(cfg, events[-1]['provider'], transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert (cfg.active, cfg.models, cfg.providers) == before


def test_http_install_failure_is_terminal_sse_not_false_success(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from kova import local_models
    from kova.server import create_app
    monkeypatch.setattr(local_models, 'hardware_profile', lambda path=None: profile())
    real_client = httpx.AsyncClient
    def handle(request):
        if request.url.path == '/api/pull':
            return httpx.Response(200, text='{"error":"private-upstream-body"}\n')
        pytest.fail('pull failure must not run any later lifecycle step')
    monkeypatch.setattr(local_models.httpx, 'AsyncClient', lambda **kw: real_client(**{**kw, 'transport': httpx.MockTransport(handle)}))
    with TestClient(create_app(config.load_config(tmp_path))) as client:
        headers = {'Authorization': 'Bearer ' + config.ensure_token(tmp_path)}
        response = client.post('/local-models/install', json={'model': 'qwen3:4b'}, headers=headers)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'error' and not any(e['type'] == 'done' for e in events)
        assert 'retry' in events[-1]['message'] and 'private-upstream-body' not in response.text


async def test_route_cancel_releases_install_lock(tmp_path, monkeypatch):
    import asyncio
    from kova import local_models
    from kova.server import create_app
    monkeypatch.setattr(local_models, 'hardware_profile', lambda path=None: profile())
    waiting = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []
    async def install(*a, **kw):
        calls.append(True)
        yield {'type': 'progress', 'message': 'Downloading'}
        if len(calls) == 1:
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        yield {'type': 'done', 'model': 'demo'}
    monkeypatch.setattr(local_models, 'install', install)
    cfg = config.load_config(tmp_path)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://kova.test',
                                    headers={'Authorization': 'Bearer ' + config.ensure_token(tmp_path)}) as client:
            first = asyncio.create_task(client.post('/local-models/install', json={'model': 'qwen3:4b'}))
            await asyncio.wait_for(waiting.wait(), timeout=2)
            busy = await client.post('/local-models/install', json={'model': 'qwen3:4b'})
            assert busy.status_code == 409
            assert (await client.delete('/local-models/demo')).status_code == 409
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert cancelled.is_set()
            retry = await client.post('/local-models/install', json={'model': 'qwen3:4b'})
            assert retry.status_code == 200 and '"type": "done"' in retry.text
