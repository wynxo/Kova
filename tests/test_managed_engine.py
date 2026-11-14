"""Managed runtime tests: tiny synthetic archives, mocked HTTP and processes only."""
import asyncio
import hashlib
import io
import json
import os
import tarfile

import httpx
import pytest

from kova import config
from tests.test_local_models import profile


def artifact(data=b'GGUFdemo', name='demo.gguf'):
    from kova.engine_downloads import Artifact
    return Artifact('https://downloads.test/' + name, name, len(data), hashlib.sha256(data).hexdigest())


async def collect(iterator):
    return [event async for event in iterator]


async def test_resume_uses_range_and_verifies_complete_file(tmp_path):
    from kova.engine_downloads import download
    target = tmp_path / 'demo.gguf'
    target.with_suffix('.gguf.part').write_bytes(b'GGUF')
    def handle(request):
        assert request.headers['range'] == 'bytes=4-'
        return httpx.Response(206, content=b'demo', headers={'Content-Range': 'bytes 4-7/8'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = await collect(download(client, artifact(), target))
    assert target.read_bytes() == b'GGUFdemo'
    assert not target.with_suffix('.gguf.part').exists()
    assert events[-1]['completed'] == events[-1]['total'] == 8


@pytest.mark.parametrize('mode', ['ignored-range', 'bad-range', 'truncated', 'hash', 'unreachable'])
async def test_bad_download_never_promotes_partial_or_exposes_upstream(tmp_path, mode):
    from kova.engine_downloads import download
    from kova.local_models import LocalModelError
    target = tmp_path / 'demo.gguf'
    target.with_suffix('.gguf.part').write_bytes(b'GGUF')
    def handle(request):
        if mode == 'unreachable':
            raise httpx.ConnectError('private-detail', request=request)
        if mode == 'ignored-range':
            return httpx.Response(200, content=b'GGUFdemo')
        if mode == 'bad-range':
            return httpx.Response(206, content=b'demo', headers={'Content-Range': 'bytes 0-3/8'})
        return httpx.Response(206, content=b'de' if mode == 'truncated' else b'xxxx',
                              headers={'Content-Range': 'bytes 4-7/8'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if mode == 'ignored-range':
            await collect(download(client, artifact(), target))
            assert target.read_bytes() == b'GGUFdemo'
        else:
            with pytest.raises(LocalModelError) as caught:
                await collect(download(client, artifact(), target))
            assert 'private-detail' not in str(caught.value)
            assert not target.exists()


async def test_cancel_preserves_part_and_closes_http(tmp_path):
    from kova.engine_downloads import download
    waiting, closed = asyncio.Event(), []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'GGUF'
            waiting.set()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.append(True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))) as client:
        iterator = download(client, artifact(), tmp_path / 'demo.gguf')
        await anext(iterator)
        task = asyncio.create_task(anext(iterator))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await iterator.aclose()
    assert closed and (tmp_path / 'demo.gguf.part').read_bytes() == b'GGUF'


def archive_bytes(malicious=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, data in [('bundle/llama-server', b'fake binary'), ('bundle/libggml.so.1', b'library')]:
            info = tarfile.TarInfo('../../escape' if malicious else name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if not malicious:
            link = tarfile.TarInfo('bundle/libggml.so')
            link.type, link.linkname = tarfile.SYMTYPE, 'libggml.so.1'
            archive.addfile(link)
    return buffer.getvalue()


async def test_engine_is_verified_before_extraction_and_keeps_runtime_libraries(tmp_path):
    from kova.engine_downloads import install_engine
    data = archive_bytes()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))) as client:
        await collect(install_engine(client, artifact(data, 'engine.tar.gz'), tmp_path))
    binaries = list(tmp_path.rglob('llama-server'))
    assert len(binaries) == 1
    if os.name != 'nt':  # Windows has no executable permission bit.
        assert binaries[0].stat().st_mode & 0o100
    assert (binaries[0].parent / 'libggml.so').read_bytes() == b'library'


async def test_archive_traversal_is_rejected(tmp_path):
    from kova.engine_downloads import install_engine
    from kova.local_models import LocalModelError
    data = archive_bytes(malicious=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))) as client:
        with pytest.raises(LocalModelError):
            await collect(install_engine(client, artifact(data, 'engine.tar.gz'), tmp_path / 'engine'))
    assert not (tmp_path / 'escape').exists()


@pytest.mark.parametrize('os_name,arch,cpu,want', [
    ('Linux', 'x86_64', False, 'ubuntu-vulkan-x64'),
    ('Windows', 'AMD64', False, 'win-vulkan-x64'),
    ('Windows', 'ARM64', True, 'win-cpu-arm64'),
    ('Darwin', 'arm64', False, 'macos-arm64'),
    ('Linux', 'aarch64', True, 'ubuntu-arm64'),
])
def test_release_selection_has_pinned_checksum(monkeypatch, os_name, arch, cpu, want):
    from kova import engine_downloads as downloads
    monkeypatch.setattr(downloads.platform, 'system', lambda: os_name)
    monkeypatch.setattr(downloads.platform, 'machine', lambda: arch)
    selected = downloads.engine_artifact(cpu=cpu)
    assert want in selected.name and len(selected.sha256) == 64
    assert '/latest/' not in selected.url


class FakeProcess:
    pid = 43210
    def __init__(self, args, **kwargs):
        self.args, self.kwargs, self.alive = args, kwargs, True
        self.stopped = 0
        kwargs['stdout'].write(b'offloaded 37/37 layers to GPU\n')
        kwargs['stdout'].flush()
    def poll(self):
        return None if self.alive else 0
    def cmdline(self):
        return self.args
    def create_time(self):
        return 12345.0
    def exe(self):
        return self.args[0]
    def terminate(self):
        self.stopped += 1
        self.alive = False
    def wait(self, timeout=None):
        return 0
    def kill(self):
        self.alive = False


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from kova import managed_engine as engine, engine_downloads as downloads
    cfg = config.load_config(tmp_path)
    gguf = artifact()
    monkeypatch.setattr(downloads, 'MODELS', {'qwen3:4b': gguf, 'qwen3:8b': gguf, 'llama3.2:3b': gguf})
    binary = tmp_path / 'engine' / 'demo' / 'llama-server'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'fake')
    async def install_engine(*args, **kwargs):
        yield {'type': 'progress', 'message': 'engine cached'}
    monkeypatch.setattr(downloads, 'install_engine', install_engine)
    monkeypatch.setattr(downloads, 'engine_binary', lambda *a: binary)
    monkeypatch.setattr(downloads, 'engine_artifact', lambda **kw: artifact(b'engine', 'engine.tar.gz'))
    monkeypatch.setattr(engine, 'hardware_profile', lambda *a: profile())
    processes = []
    def spawn(args, **kwargs):
        process = FakeProcess(args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(engine.subprocess, 'Popen', spawn)
    monkeypatch.setattr(engine.psutil, 'Process', lambda pid: processes[-1])
    def handle(request):
        if request.url.host == 'downloads.test':
            return httpx.Response(200, content=b'GGUFdemo')
        assert request.url.host == '127.0.0.1'
        assert request.headers['authorization'].startswith('Bearer ')
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok'})
        return httpx.Response(200, json={'data': [{'id': 'qwen3:4b'}, {'id': 'llama3.2:3b'}]})
    return cfg, processes, httpx.MockTransport(handle)


async def test_install_registers_only_ready_engine_and_restart_is_idempotent(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    events = await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    assert events[-1]['type'] == 'done'
    provider = cfg.active_model()._provider
    assert provider.kind == 'local' and provider.base_url.startswith('http://127.0.0.1:')
    assert provider.api_key
    assert (cfg.home / 'engine.pid').read_text().strip() == '43210'
    args = processes[0].args
    assert args[args.index('--host') + 1] == '127.0.0.1'
    assert args[args.index('--ctx-size') + 1] == '4096'
    assert args[args.index('-ngl') + 1] == '999'
    assert provider.api_key not in ' '.join(args)
    manager = engine.manager(cfg)
    async with manager.use(cfg.active_model(), transport=transport):
        pass
    assert len(processes) == 1
    await manager.stop()
    async with manager.use(cfg.active_model(), transport=transport):
        pass
    assert len(processes) == 2 and processes[0].stopped == 1
    assert cfg.active_model().base_url == cfg.active_model()._provider.base_url


async def test_idle_unload_waits_for_active_use_and_next_use_reloads(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    manager = engine.manager(cfg)
    cfg.local_idle_unload_minutes = 1
    async with manager.use(cfg.active_model(), transport=transport):
        await manager.unload_idle(now=manager.last_used + 61)
        assert processes[-1].alive
    await manager.unload_idle(now=manager.last_used + 61)
    assert not processes[-1].alive
    async with manager.use(cfg.active_model(), transport=transport):
        assert processes[-1].alive
    assert len(processes) == 2


async def test_cpu_no_gpu_and_remove_delete_weights(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'llama3.2:3b', profile(gpu=None, vram_bytes=None), cpu=True, transport=transport))
    args = processes[0].args
    assert args[args.index('-ngl') + 1] == '0'
    model = cfg.active_model()
    await engine.remove(cfg, model.provider)
    assert not list((cfg.home / 'models').glob('*.gguf'))
    assert not any(p.id == model.provider for p in cfg.providers)
    assert not processes[0].alive


async def test_gpu_fallback_and_save_failure_do_not_register(runtime, monkeypatch):
    from kova import managed_engine as engine, providers
    from kova.local_models import LocalModelError
    cfg, processes, transport = runtime
    real_spawn = engine.subprocess.Popen
    def spawn(args, **kwargs):
        process = real_spawn(args, **kwargs)
        kwargs['stdout'].seek(0)
        kwargs['stdout'].truncate()
        kwargs['stdout'].write(b'offloaded 0/37 layers to GPU\n')
        kwargs['stdout'].flush()
        return process
    monkeypatch.setattr(engine.subprocess, 'Popen', spawn)
    with pytest.raises(LocalModelError, match='GPU'):
        await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    assert not processes[-1].alive and cfg.active == 'local'
    monkeypatch.setattr(engine.subprocess, 'Popen', real_spawn)
    monkeypatch.setattr(providers, 'save_config', lambda cfg: (_ for _ in ()).throw(OSError('private-detail')))
    with pytest.raises(LocalModelError, match='save'):
        await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    assert not processes[-1].alive and cfg.active == 'local'


async def test_insufficient_disk_fails_before_download_or_process(runtime):
    from kova import managed_engine as engine
    from kova.local_models import LocalModelError
    cfg, processes, transport = runtime
    with pytest.raises(LocalModelError, match='bytes'):
        await collect(engine.install(cfg, 'qwen3:4b', profile(free_disk=1), transport=transport))
    assert not processes


def test_local_idle_config_roundtrips(tmp_path):
    cfg = config.load_config(tmp_path)
    assert hasattr(cfg, 'local_idle_unload_minutes'), 'managed idle setting missing'
    cfg.local_idle_unload_minutes = 2
    config.save_config(cfg)
    assert config.load_config(tmp_path).local_idle_unload_minutes == 2


def test_missing_ollama_offers_managed_route_and_download_needs_explicit_choice(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from kova import local_models, managed_engine as engine
    from kova.server import create_app
    cfg = config.load_config(tmp_path)
    monkeypatch.setattr(local_models, 'hardware_profile', lambda *a: profile())
    async def inspect(**kwargs):
        return profile()
    monkeypatch.setattr(local_models, 'inspect', inspect)
    called = []
    async def install(*args, **kwargs):
        called.append(args[1])
        yield {'type': 'done', 'model': args[1]}
    monkeypatch.setattr(engine, 'install', install)
    with TestClient(create_app(cfg)) as client:
        headers = {'Authorization': 'Bearer ' + config.ensure_token(tmp_path)}
        response = client.get('/local-models', headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data['backend'] == 'managed' and data['models'] and not called
        assert 'Kova' in data['disk_note']
        assert client.post('/local-models/install', json={'model': 'qwen3:4b', 'backend': 'managed'}).status_code == 401
        response = client.post('/local-models/install', headers=headers, json={'model': 'qwen3:4b', 'backend': 'managed'})
        assert '"type": "done"' in response.text and called == ['qwen3:4b']
        assert client.post('/local-models/install', headers=headers, json={'model': 'qwen3:4b', 'backend': 'arbitrary'}).status_code == 422


def test_setup_missing_ollama_can_decline_or_install_managed(tmp_path, monkeypatch):
    from kova import onboarding, local_models, managed_engine as engine
    cfg = config.load_config(tmp_path)
    monkeypatch.setattr(onboarding, '_running_client', lambda cfg: None)
    monkeypatch.setattr(local_models, 'hardware_profile', lambda *a: profile())
    monkeypatch.setattr(onboarding, '_select', lambda *a, **kw: 0)
    monkeypatch.setattr(onboarding, '_ask', lambda *a, **kw: 'n')
    calls = []
    async def install(*args, **kw):
        calls.append(args[1])
        yield {'type': 'done', 'message': 'Ready'}
    monkeypatch.setattr(engine, 'install', install)
    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    with pytest.raises(onboarding.SetupError, match='Nothing downloaded'):
        onboarding.run_setup(cfg, provider_kind='this-pc', transport=transport)
    assert not calls
    monkeypatch.setattr(onboarding, '_ask', lambda *a, **kw: 'y')
    onboarding.run_setup(cfg, provider_kind='this-pc', transport=transport)
    assert calls == ['qwen3:4b']


async def test_model_stream_reloads_managed_provider_before_request(runtime, monkeypatch):
    from kova import managed_engine as engine
    from kova.models import ModelClient
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    await engine.manager(cfg).stop()
    stream_requests = []
    def handle(request):
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok'})
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'qwen3:4b'}]})
        stream_requests.append(request)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    events = await collect(ModelClient(cfg.active_model(), config=cfg, transport=httpx.MockTransport(handle)).stream([{'role': 'user', 'content': 'hi'}]))
    assert ''.join(e.text for e in events) == 'hello'
    assert len(processes) == 2 and len(stream_requests) == 1
    assert str(stream_requests[0].url).startswith(cfg.active_model().base_url)
    assert engine.manager(cfg).users == 0


async def test_forged_pid_is_not_signalled(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    state_path = cfg.home / 'engine' / 'process.json'
    state = json.loads(state_path.read_text())
    state['created'] = 0
    state_path.write_text(json.dumps(state))
    assert engine.owned_process(cfg.home) is None
    # Use the crash-recovery API: an unverified PID is forgotten, not killed.
    engine.stop_owned(cfg.home)
    assert processes[0].alive


async def test_dead_readiness_never_registers_and_stops_spawn(runtime, monkeypatch):
    from kova import managed_engine as engine
    from kova.local_models import LocalModelError
    cfg, processes, transport = runtime
    monkeypatch.setattr(engine, 'START_TIMEOUT', 0)
    with pytest.raises(LocalModelError, match='ready'):
        await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    assert processes and not processes[0].alive
    assert cfg.active == 'local' and not (cfg.home / 'engine.pid').exists()


async def test_busy_model_cannot_be_removed(runtime):
    from kova import managed_engine as engine
    from kova.local_models import LocalModelError
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    async with engine.manager(cfg).use(cfg.active_model(), transport=transport):
        with pytest.raises(LocalModelError, match='busy'):
            await engine.remove(cfg, cfg.active_model().provider)
    assert processes[-1].alive and list((cfg.home / 'models').glob('*.gguf'))


def test_cli_idle_setting_is_settable_and_visible(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kova.cli import app
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ['config', 'set', 'local.idle_unload_minutes', '2'])
    assert result.exit_code == 0, result.output
    shown = runner.invoke(app, ['config', 'show', '--json'])
    assert json.loads(shown.stdout)['local']['idle_unload_minutes'] == 2
    assert runner.invoke(app, ['config', 'set', 'local.idle_unload_minutes', '-1']).exit_code != 0


def test_cli_stop_cleans_orphan_even_without_server_pid(runtime, monkeypatch):
    from typer.testing import CliRunner
    from kova.cli import app
    from kova import managed_engine as engine, cli
    cfg, processes, transport = runtime
    asyncio.run(collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport)))
    monkeypatch.setenv('KOVA_HOME', str(cfg.home))
    monkeypatch.setattr(cli.KovaClient, 'health', lambda *a, **kw: {})
    result = CliRunner().invoke(app, ['stop'])
    assert result.exit_code == 0 and not processes[0].alive


def test_cli_remove_local_calls_weight_removal(monkeypatch):
    from typer.testing import CliRunner
    from kova import cli
    calls = []
    class Client:
        def local_model_remove(self, provider):
            calls.append(provider)
            return {'message': 'GGUF removed'}
    monkeypatch.setattr(cli, 'get_client', lambda: Client())
    result = CliRunner().invoke(cli.app, ['model', 'remove-local', 'managed-demo'])
    assert result.exit_code == 0 and calls == ['managed-demo']


async def test_models_list_during_loading_is_not_ready(runtime, monkeypatch):
    from kova import managed_engine as engine
    from kova.local_models import LocalModelError
    cfg, processes, transport = runtime
    monkeypatch.setattr(engine, 'START_TIMEOUT', 0.01)
    def handle(request):
        if request.url.host == 'downloads.test':
            return httpx.Response(200, content=b'GGUFdemo')
        if request.url.path == '/health':
            return httpx.Response(503, json={'status': 'loading model'})
        return httpx.Response(200, json={'data': [{'id': 'qwen3:4b'}]})
    with pytest.raises(LocalModelError, match='ready'):
        await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(handle)))
    assert not processes[-1].alive and cfg.active == 'local'


async def test_remove_keeps_gguf_until_last_cpu_gpu_variant(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    first = await collect(engine.install(cfg, 'llama3.2:3b', profile(), transport=transport))
    second = await collect(engine.install(cfg, 'llama3.2:3b', profile(), cpu=True, transport=transport))
    await engine.remove(cfg, first[-1]['provider'])
    assert list((cfg.home / 'models').glob('*.gguf'))
    await engine.remove(cfg, second[-1]['provider'])
    assert not list((cfg.home / 'models').glob('*.gguf'))


def test_actual_managed_route_runs_shared_core_and_shutdown_stops_engine(runtime, monkeypatch):
    from fastapi.testclient import TestClient
    from kova import local_models
    from kova.server import create_app
    cfg, processes, transport = runtime
    async def inspect(**kwargs):
        return profile()
    monkeypatch.setattr(local_models, 'inspect', inspect)
    monkeypatch.setattr(local_models, 'hardware_profile', lambda *a: profile())
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(**{**kw, 'transport': transport}))
    with TestClient(create_app(cfg)) as client:
        headers = {'Authorization': 'Bearer ' + config.ensure_token(cfg.home)}
        response = client.post('/local-models/install', headers=headers, json={'model': 'qwen3:4b', 'backend': 'managed'})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'done'
        listing = client.get('/local-models', headers=headers).json()
        assert listing['installed'][0]['backend'] == 'managed'
        assert processes[-1].alive
    assert not processes[-1].alive and not (cfg.home / 'engine.pid').exists()


async def test_idle_zero_disables_unload_and_cancel_releases_use(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    supervisor = engine.manager(cfg)
    cfg.local_idle_unload_minutes = 0
    await supervisor.unload_idle(now=supervisor.last_used + 999999)
    assert processes[-1].alive
    entered = asyncio.Event()
    async def use():
        async with supervisor.use(cfg.active_model(), transport=transport):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(use())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert supervisor.users == 0


async def test_remove_save_failure_preserves_connection_and_gguf(runtime, monkeypatch):
    from kova import managed_engine as engine, providers
    from kova.local_models import LocalModelError
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    provider = cfg.active_model().provider
    before = (cfg.home / 'config.toml').read_bytes()
    def fail(*args):
        raise OSError('private-detail')
    monkeypatch.setattr(providers, 'save_config', fail)
    monkeypatch.setattr(engine, 'save_config', fail)
    with pytest.raises(LocalModelError) as caught:
        await engine.remove(cfg, provider)
    assert 'private-detail' not in str(caught.value)
    assert cfg.active_model().provider == provider
    assert list((cfg.home / 'models').glob('*.gguf'))
    assert (cfg.home / 'config.toml').read_bytes() == before


async def test_cancel_during_engine_loading_stops_owned_process(runtime, monkeypatch):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    loading = asyncio.Event()
    async def handle(request):
        if request.url.host == 'downloads.test':
            return httpx.Response(200, content=b'GGUFdemo')
        loading.set()
        await asyncio.Event().wait()
    iterator = engine.install(cfg, 'qwen3:4b', profile(), transport=httpx.MockTransport(handle))
    task = asyncio.create_task(collect(iterator))
    await asyncio.wait_for(loading.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes and not processes[-1].alive and cfg.active == 'local'
    assert not (cfg.home / 'engine.pid').exists()


async def test_supervisor_reaps_own_child_if_pid_metadata_is_lost(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    (cfg.home / 'engine' / 'process.json').unlink()
    await engine.manager(cfg).stop()
    assert not processes[-1].alive


async def test_remove_delete_failure_restores_weights_and_saved_connection(runtime, monkeypatch):
    from pathlib import Path
    from kova import managed_engine as engine
    from kova.local_models import LocalModelError
    cfg, processes, transport = runtime
    await collect(engine.install(cfg, 'qwen3:4b', profile(), transport=transport))
    provider = cfg.active_model().provider
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path.suffix == '.removing':
            raise OSError('private-detail')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    with pytest.raises(LocalModelError):
        await engine.remove(cfg, provider)
    assert list((cfg.home / 'models').glob('*.gguf'))
    assert config.load_config(cfg.home).active_model().provider == provider


async def test_tight_fit_context_is_displayed_and_used(runtime):
    from kova import managed_engine as engine
    cfg, processes, transport = runtime
    hardware = profile(vram_bytes=6 * 1024 ** 3)
    row = next(r for r in engine.catalog(hardware) if r['id'] == 'qwen3:4b')
    assert row['context'] == 2048
    assert row['engine_download_bytes'] > 0
    await collect(engine.install(cfg, 'qwen3:4b', hardware, transport=transport))
    args = processes[-1].args
    assert cfg.active_model().context == 2048
    assert args[args.index('--ctx-size') + 1] == '2048'
