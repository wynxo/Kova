"""One owned llama-server per Kova home; no model download on ordinary startup."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import subprocess
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

import httpx

from kova import engine_downloads as downloads, providers
from kova.config import Config, save_config
from kova.local_models import LocalModelError, _model, rate
from kova.tools.system_info import hardware_profile

START_TIMEOUT = 180


def identifier(model_id: str, cpu: bool) -> str:
    return 'managed-' + model_id.replace(':', '-') + ('-cpu' if cpu else '-gpu')


def _manifest(home: Path) -> dict:
    path = home / 'engine' / 'models.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or any(
            not isinstance(r, dict) or r.get('model') not in downloads.MODELS
            or type(r.get('cpu')) is not bool or r.get('context') not in (2048, 4096)
            or key != identifier(r['model'], r['cpu']) for key, r in data.items()
        ):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise LocalModelError('Managed model metadata is unreadable. Restore it or remove the managed connection and run setup again.') from None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    try:
        with temp.open('w', encoding='utf-8') as handle:
            temp.chmod(0o600)
            json.dump(data, handle)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def installed(cfg: Config) -> list[dict]:
    """List only connections backed by a curated managed model record."""
    records = _manifest(cfg.home)
    return [{'provider': p.id, 'name': p.name, 'backend': 'managed'} for p in cfg.providers
            if p.kind == 'local' and p.id in records]


def catalog(profile: dict, *, cpu=False) -> list[dict]:
    from dataclasses import asdict
    from kova.local_models import CATALOG
    engine = downloads.engine_artifact(cpu=cpu)
    return [{**asdict(m), 'download_bytes': downloads.MODELS[m.id].size,
             **fit(m.id, profile, cpu=cpu, engine=engine)} for m in CATALOG]


def fit(model_id: str, profile: dict, *, cpu=False, engine=None) -> dict:
    """Budget both the archive and extracted engine plus the GGUF reserve."""
    artifact = downloads.MODELS[model_id]
    engine = engine or downloads.engine_artifact(cpu=cpu)
    model = replace(_model(model_id), download_bytes=artifact.size + engine.size * 4)
    result = rate(model, profile, cpu=cpu)
    result['context'] = 2048 if result['rating'] == 'fits-tightly' else 4096
    result['engine_download_bytes'] = engine.size
    result['reason'] = result['reason'].replace('Ollama model volume', 'Kova model volume').replace('Ollama settings', 'engine settings')
    return result


def owned_process(home: Path):
    """Never signal a recycled PID: require birth time, executable AND argv."""
    try:
        pid = int((home / 'engine.pid').read_text().strip())
        state = json.loads((home / 'engine' / 'process.json').read_text())
        if pid <= 1 or pid != state['pid']:
            return None
        process = psutil.Process(pid)
        binary = Path(state['argv'][0]).resolve()
        if not binary.is_relative_to((home / 'engine').resolve()):
            return None
        if (process.create_time() != state['created'] or process.cmdline() != state['argv']
                or Path(process.exe()).resolve() != binary):
            return None
        return process
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None


def stop_owned(home: Path) -> None:
    """Stop an owned engine, including after the Kova server crashed."""
    try:
        process = owned_process(home)
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        (home / 'engine.pid').unlink(missing_ok=True)
        (home / 'engine' / 'process.json').unlink(missing_ok=True)
    except (OSError, psutil.Error):
        raise LocalModelError('Could not stop the managed engine. Close its process and retry kova stop.') from None


class EngineManager:
    """Serialize reloads, track active streams and unload only genuinely idle work."""
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.users = 0
        self.current = None
        self.last_used = time.monotonic()
        self.process = None  # retain Popen to reap our own children

    @asynccontextmanager
    async def exclusive(self):
        async with self.lock:
            # Also excludes a CLI setup process from the running server. Do not
            # block the event loop or queue a second install behind a download.
            filelock = FileLock(self.cfg.home / 'engine.lock', thread_local=False)
            try:
                filelock.acquire(timeout=0)
            except Timeout:
                raise LocalModelError('Another managed-engine operation is running. Wait for it to finish, then retry.') from None
            try:
                yield
            finally:
                filelock.release()

    async def _stop(self):
        await asyncio.to_thread(stop_owned, self.cfg.home)
        if self.process is not None:
            # A direct child handle remains ours even if its on-disk identity
            # was lost. poll() reaps an already-exited child before signalling.
            if self.process.poll() is None:
                self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)
            self.process = None
        self.current = None

    async def stop(self):
        async with self.exclusive():
            await self._stop()

    async def _start(self, provider, record: dict, *, transport=None):
        if self.users and self.current != provider.id:
            raise LocalModelError('The managed model is busy in another chat. Stop that turn before switching models.')
        state_path = self.cfg.home / 'engine' / 'process.json'
        existing = owned_process(self.cfg.home)
        if existing is not None:
            state = json.loads(state_path.read_text())
            if state.get('provider') == provider.id:
                # Check authenticated readiness too: a live PID may be hung,
                # unloading, or have credentials from an interrupted install.
                async with httpx.AsyncClient(transport=transport, timeout=2, trust_env=False) as client:
                    if await self._ready(client, state['base_url'], provider.api_key, record['model']):
                        self.current = provider.id
                        provider.base_url = state['base_url']
                        self.cfg.bind_providers()
                        return
                if self.users:
                    raise LocalModelError('The managed engine is not responding while another chat is using it. Stop that turn and retry.')
        await self._stop()
        model_id, cpu = record['model'], record['cpu']
        artifact = downloads.MODELS[model_id]
        model_path = self.cfg.home / 'models' / artifact.name
        if not model_path.is_file() or model_path.stat().st_size != artifact.size:
            raise LocalModelError('Managed GGUF is missing or incomplete. Remove this connection and run setup again to resume its download.')
        hardware = await asyncio.to_thread(hardware_profile, self.cfg.home)
        # The verified weights are already on disk; reload must not require
        # enough free space to download a second copy, but recheck RAM/VRAM.
        memory_fit = rate(_model(model_id), hardware, cpu=cpu, check_disk=False)
        if memory_fit['rating'] == 'too-big':
            raise LocalModelError(memory_fit['reason'])
        engine = downloads.engine_artifact(cpu=cpu)
        binary = downloads.engine_binary(self.cfg.home, engine).resolve()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        args = [str(binary), '--host', '127.0.0.1', '--port', str(port),
                '--model', str(model_path.resolve()), '--alias', model_id,
                '--ctx-size', str(record['context']), '-ngl', '0' if cpu else '999',
                '--parallel', '1', '--fit', 'off', '--split-mode', 'none', '--jinja']
        # Environment knobs must not override our bounded runtime or leak a
        # user's unrelated llama.cpp settings into this managed process.
        env = {k: v for k, v in os.environ.items() if not k.startswith(('LLAMA_', 'GGML_'))}
        env['LLAMA_API_KEY'] = provider.api_key
        base_url = f'http://127.0.0.1:{port}/v1'
        log_path = self.cfg.home / 'engine.log'
        process = None
        try:
            with log_path.open('wb') as log:
                log_path.chmod(0o600)
                kwargs = {'stdout': log, 'stderr': subprocess.STDOUT, 'stdin': subprocess.DEVNULL,
                          'cwd': binary.parent, 'env': env}
                if os.name == 'nt':
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    kwargs['start_new_session'] = True
                process = subprocess.Popen(args, **kwargs)
            self.process = process
            owner = psutil.Process(process.pid)
            _write_json(state_path, {'pid': process.pid, 'created': owner.create_time(), 'argv': args,
                                     'provider': provider.id, 'base_url': base_url})
            pid_path = self.cfg.home / 'engine.pid'
            pid_path.write_text(str(process.pid))
            pid_path.chmod(0o600)
            deadline = time.monotonic() + START_TIMEOUT
            async with httpx.AsyncClient(transport=transport, timeout=2, trust_env=False) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise LocalModelError('The managed engine exited while loading. Check GPU drivers (Vulkan/Metal), or choose the small CPU model. See engine.log for local diagnostics. No ready model was added.')
                    if await self._ready(client, base_url, provider.api_key, model_id):
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise LocalModelError('The managed engine did not become ready. Check engine.log, GPU drivers and available memory, then retry or choose CPU mode.')
            if not cpu:
                # A GPU build can still silently load on CPU. Require llama.cpp's
                # full-layer offload confirmation, never infer it from HTTP 200.
                with log_path.open('rb') as handle:
                    handle.seek(max(0, log_path.stat().st_size - 256_000))
                    tail = handle.read().decode('utf-8', errors='replace')
                layers = re.findall(r'offloaded (\d+)/(\d+) layers to GPU', tail)
                if not layers or not any(int(a) == int(b) and int(a) > 0 for a, b in layers):
                    raise LocalModelError('The engine did not confirm full GPU loading. Check Vulkan/Metal drivers or explicitly choose the small CPU model. No ready model was added.')
            provider.base_url = base_url
            self.cfg.bind_providers()
            self.current = provider.id
            self.last_used = time.monotonic()
        except BaseException:
            if process is not None:
                # We own this Popen even if recording its identity failed.
                if process.poll() is None:
                    process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
            self.process = None
            (self.cfg.home / 'engine.pid').unlink(missing_ok=True)
            state_path.unlink(missing_ok=True)
            raise

    @staticmethod
    async def _ready(client, base_url, key, model_id):
        try:
            health = await client.get(base_url.removesuffix('/v1') + '/health',
                                      headers={'Authorization': 'Bearer ' + key})
            if not health.is_success or health.json() != {'status': 'ok'}:
                return False
            response = await client.get(base_url + '/models', headers={'Authorization': 'Bearer ' + key})
            data = response.json()
            return response.is_success and isinstance(data, dict) and any(
                m.get('id') == model_id for m in data.get('data', []) if isinstance(m, dict))
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    @asynccontextmanager
    async def use(self, entry, *, transport=None):
        records = _manifest(self.cfg.home)
        record = records.get(entry.provider)
        if record is None:
            yield
            return
        async with self.exclusive():
            try:
                await self._start(entry._provider, record, transport=transport)
            except (OSError, psutil.Error, ValueError, KeyError):
                raise LocalModelError('Could not reload the managed engine. Check engine.log, permissions and GPU drivers, then retry setup.') from None
            # Update any independently-held entry before constructing HTTP URLs.
            entry.base_url = entry._provider.base_url
            self.users += 1
        try:
            yield
        finally:
            self.users -= 1
            self.last_used = time.monotonic()

    async def unload_idle(self, *, now=None):
        minutes = self.cfg.local_idle_unload_minutes
        if minutes <= 0:
            return
        async with self.exclusive():
            if not self.users and (time.monotonic() if now is None else now) - self.last_used >= minutes * 60:
                await self._stop()

    async def idle_loop(self):
        while True:
            await asyncio.sleep(15)
            try:
                await self.unload_idle()
            except LocalModelError:
                pass  # another process may be installing; try again next tick


_managers: dict[Path, EngineManager] = {}


def manager(cfg: Config) -> EngineManager:
    """Return the shared supervisor for this server/home."""
    key = cfg.home.resolve()
    result = _managers.get(key)
    if result is None or result.cfg is not cfg:
        result = _managers[key] = EngineManager(cfg)
    return result


async def install(cfg: Config, model_id: str, profile: dict, *, cpu=False, transport=None):
    """Download explicit curated artifacts, load successfully, then register."""
    _model(model_id)
    engine = downloads.engine_artifact(cpu=cpu)
    result = fit(model_id, profile, cpu=cpu, engine=engine)
    if result['rating'] == 'too-big':
        raise LocalModelError(result['reason'])
    provider_id = identifier(model_id, cpu)
    supervisor = manager(cfg)
    async with supervisor.exclusive():
        if supervisor.users:
            raise LocalModelError('A managed model is busy. Stop its chat before installing another model.')
        if any(p.id == provider_id for p in cfg.providers):
            raise LocalModelError('This managed model is already installed. Select it, or remove it before reinstalling.')
        before = (list(cfg.models), list(cfg.providers), cfg.active)
        records = _manifest(cfg.home)
        original_records = dict(records)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(180, connect=10), trust_env=False) as client:
                yield {'type': 'progress', 'message': 'Downloading the pinned llama.cpp engine…'}
                async for event in downloads.install_engine(client, engine, cfg.home):
                    yield event
                yield {'type': 'progress', 'message': 'Downloading GGUF; retry resumes the .part file…'}
                artifact = downloads.MODELS[model_id]
                async for event in downloads.download(client, artifact, cfg.home / 'models' / artifact.name):
                    yield event
            context = result['context']
            record = {'model': model_id, 'cpu': cpu, 'context': context}
            provider = providers.make_provider('local', identifier=provider_id,
                name=_model(model_id).name + ' on this PC (managed' + (', CPU)' if cpu else ')'),
                base_url='http://127.0.0.1:1/v1', api_key=secrets.token_urlsafe(32))
            yield {'type': 'progress', 'message': 'Loading and checking the managed engine (no test chat)…'}
            await supervisor._start(provider, record, transport=transport)
            records[provider_id] = record
            _write_json(cfg.home / 'engine' / 'models.json', records)
            try:
                providers.install_provider(cfg, provider, [model_id], context=context)
            except (OSError, providers.ProviderError):
                raise LocalModelError('The managed engine loaded, but Kova could not save its connection. Check config permissions and retry.') from None
        except BaseException as exc:
            cfg.models[:], cfg.providers[:], cfg.active = before
            await supervisor._stop()
            with suppress(OSError):
                _write_json(cfg.home / 'engine' / 'models.json', original_records)
            if isinstance(exc, (OSError, psutil.Error)):
                raise LocalModelError('The managed engine could not be installed or started. Check free disk space, file permissions and GPU drivers, then retry or use Ollama.') from None
            raise
        yield {'type': 'done', 'provider': provider_id, 'model': model_id,
               'message': 'Managed model ready. Your next chat uses this PC.'}


async def remove(cfg: Config, provider_id: str):
    """Delete only the curated GGUF; retain a file shared by CPU/GPU variants."""
    supervisor = manager(cfg)
    async with supervisor.exclusive():
        records = _manifest(cfg.home)
        record = records.get(provider_id)
        if record is None or not any(p.id == provider_id for p in cfg.providers):
            raise LocalModelError('No Kova-managed GGUF with that connection name.')
        if supervisor.users:
            raise LocalModelError('The managed model is busy. Stop its chat before removing it.')
        before = (list(cfg.models), list(cfg.providers), cfg.active)
        target = staging = None
        try:
            await supervisor._stop()
            artifact = downloads.MODELS[record['model']]
            remaining = {k: v for k, v in records.items() if k != provider_id}
            if not any(r['model'] == record['model'] for r in remaining.values()):
                target = cfg.home / 'models' / artifact.name
                staging = target.with_suffix(target.suffix + '.removing')
                # Stage atomically so config/metadata failures can put the
                # bytes back. Also recover an interrupted previous removal.
                if target.exists():
                    target.replace(staging)
            providers.remove_provider(cfg, provider_id)
            _write_json(cfg.home / 'engine' / 'models.json', remaining)
            if target is not None:
                target.with_suffix(target.suffix + '.part').unlink(missing_ok=True)
                staging.unlink(missing_ok=True)
        except (OSError, ValueError, providers.ProviderError):
            cfg.models[:], cfg.providers[:], cfg.active = before
            if staging is not None and staging.exists():
                with suppress(OSError):
                    staging.replace(target)
            with suppress(OSError):
                save_config(cfg)
            with suppress(OSError):
                _write_json(cfg.home / 'engine' / 'models.json', records)
            raise LocalModelError('Could not finish removing the GGUF. Check permissions and retry; the connection was kept.') from None
