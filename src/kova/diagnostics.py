"""Bounded, credential-safe first-run diagnostics; never starts a server."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx

from kova import sandbox
from kova.config import load_config
from kova.providers import ProviderError, list_models, supports

#: The config written by :func:`kova.config.save_config`; anything else is a
#: typo or a key from a newer version this install does not understand.
KNOWN_CONFIG_KEYS = frozenset({
    'schema_version', 'oauth_host_id', 'active', 'port', 'compact_threshold',
    'files_path', 'memory', 'tui', 'persona', 'providers', 'models', 'bind',
})
CONFIG_SCHEMA = 2
MIN_NODE_MAJOR = 22
MIN_FREE_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class Check:
    """One doctor line; ``name`` stays stable for ``doctor --json`` consumers."""

    name: str
    ok: bool
    detail: str
    next_step: str | None = None


def diagnose() -> list[Check]:
    """Run the local checks in a stable order without starting the server."""
    try:
        cfg = load_config()
    except (OSError, ValueError, TypeError, KeyError):
        return [Check(
            'config', False,
            'Cannot read config.toml. Fix its TOML and provider references, then run kova doctor.',
            'Fix config.toml, then run kova doctor.',
        )]
    checks = [Check('config', True, 'Configuration loaded.')]
    health = None
    try:
        with httpx.Client(timeout=4, trust_env=False) as client:
            response = client.get(f'http://127.0.0.1:{cfg.port}/health?probe=1')
            data = response.json()
            valid = response.status_code == 200 and isinstance(data, dict) and 'version' in data and 'model_ok' in data
            if valid:
                health = data
            checks.append(Check(
                'server', valid,
                'Kova server is running.' if valid
                else 'Port is occupied by another service. Choose a free port in config.toml.',
                None if valid else 'Choose another port in config.toml',
            ))
    except httpx.ConnectError:
        checks.append(Check('server', True, 'Server is stopped; kova starts it automatically.'))
    except (httpx.HTTPError, ValueError):
        checks.append(Check(
            'server', False,
            'Server is not responding correctly. Check server.log and the configured port.',
            'Check server.log',
        ))
    entry = None
    try:
        entry = cfg.active_model()
        if not entry.model.strip():
            raise KeyError()
        available = list_models(entry._provider, timeout=4)
        present = entry.model in available
        checks.append(Check(
            'model', present,
            'Provider reachable; active model is listed.' if present
            else 'Active model is not available. Load it or choose another with kova setup.',
            None if present else 'kova setup',
        ))
    except KeyError:
        checks.append(Check('model', False, 'No active model configured. Run kova setup.', 'kova setup'))
    except ProviderError as exc:
        checks.append(Check('model', False, str(exc) + ' Run kova setup.', 'kova setup'))
    path = cfg.memory_path
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    writable = (not path.exists() or path.is_dir()) and parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    checks.append(Check(
        'memory', writable,
        'Memory directory is writable (or can be created).' if writable
        else 'Memory directory is not writable. Fix permissions or use kova memory use PATH.',
        None if writable else 'kova memory use PATH',
    ))
    checks.append(_port_owner(cfg, health))
    checks.append(_sandbox_check())
    checks.append(_node_check())
    checks.append(_token_check(cfg))
    checks.append(_disk_check(cfg))
    checks.append(_schema_check(cfg))
    checks.append(_tools_check(entry))
    return checks


def _port_owner(cfg, health: dict | None) -> Check:
    """Tell this KOVA_HOME apart from another Kova already using the port."""
    if health is None:
        return Check('port-owner', True, 'No Kova server on the port to identify.')
    token_path = cfg.home / 'token'
    try:
        token = token_path.read_text().strip()
    except OSError:
        token = ''
    if not token:
        return Check(
            'port-owner', False,
            'A Kova server is running without this KOVA_HOME token; another Kova probably owns the port.',
            'Stop the other server or choose another port',
        )
    try:
        with httpx.Client(timeout=4, trust_env=False) as client:
            response = client.get(f'http://127.0.0.1:{cfg.port}/models',
                                  headers={'Authorization': f'Bearer {token}'})
    except httpx.HTTPError:
        return Check(
            'port-owner', False,
            'Kova is on the port but this KOVA_HOME could not verify that it owns it.',
            'Check server.log and the token',
        )
    if response.status_code == 200:
        return Check('port-owner', True, 'Port is owned by this KOVA_HOME.')
    if response.status_code in (401, 403):
        return Check(
            'port-owner', False,
            'Port is held by a different Kova (its token does not match this KOVA_HOME).',
            'Stop the other server or choose another port',
        )
    return Check(
        'port-owner', False,
        f'Kova is on the port but ownership could not be verified (HTTP {response.status_code}).',
        'Check server.log',
    )


def _sandbox_check() -> Check:
    if sandbox.available():
        return Check('sandbox', True, f'landlock abi {sandbox.abi_version()}')
    if sys.platform != 'linux':
        return Check('sandbox', True, 'Not available on this platform; run_command asks for approval.')
    if os.environ.get('KOVA_SANDBOX', '').strip().lower() in {'off', '0', 'false', 'no', 'none'}:
        return Check(
            'sandbox', True,
            'Disabled by KOVA_SANDBOX; run_command asks for approval.',
        )
    return Check(
        'sandbox', False,
        'Landlock is unavailable, so every run_command asks for approval. Linux kernel 5.13+ is required.',
        'Use a kernel with Landlock',
    )


def _node_check() -> Check:
    node, npm = shutil.which('node'), shutil.which('npm')
    if node is None and npm is None:
        return Check('node', True, 'Node.js/npm not found; kova-app opens the web UI (kova-web) instead.')
    if node is None or npm is None:
        missing = 'Node.js' if node is None else 'npm'
        return Check(
            'node', False,
            f'{missing} is missing; kova-app needs both Node.js 22+ and npm.',
            'Install Node.js 22+ or use kova-web',
        )
    try:
        result = subprocess.run([node, '--version'], capture_output=True, text=True,
                                timeout=2, check=False)
        version = result.stdout.strip().lstrip('v')
        major = int(version.split('.', 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return Check('node', True, 'Node.js and npm found; the Node version could not be read.')
    if major < MIN_NODE_MAJOR:
        return Check(
            'node', False,
            f'Node.js {version} is too old for kova-app; Node.js 22+ is required.',
            'Install Node.js 22+ or use kova-web',
        )
    return Check('node', True, f'Node.js {version} and npm found for kova-app.')


def _token_check(cfg) -> Check:
    path = cfg.home / 'token'
    if not path.exists():
        return Check('token', True, 'Token file will be created on first start.')
    if os.name == 'nt':
        return Check('token', True, 'Token file is protected by Windows file permissions.')
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return Check(
            'token', False,
            'Token file cannot be read; fix its permissions in KOVA_HOME.',
            'chmod 600 the token file',
        )
    if mode & 0o077:
        return Check(
            'token', False,
            f'Token file permissions are {mode:04o}; only you should be able to read it.',
            'chmod 600 the token file',
        )
    return Check('token', True, f'Token file permissions are {mode:04o}.')


def _disk_check(cfg) -> Check:
    try:
        usage = shutil.disk_usage(cfg.home)
    except OSError:
        return Check(
            'disk', False,
            'KOVA_HOME disk space could not be read.',
            'Check the KOVA_HOME filesystem',
        )
    if usage.free < MIN_FREE_BYTES:
        return Check(
            'disk', False,
            f'Only {usage.free // (1024 * 1024)} MB free in KOVA_HOME; chats and memory need space.',
            'Free disk space in KOVA_HOME',
        )
    return Check('disk', True, f'{usage.free / (1024 ** 3):.1f} GB free in KOVA_HOME.')


def _schema_check(cfg) -> Check:
    path = Path(cfg.home) / 'config.toml'
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return Check('schema', True, 'Config file not written yet; defaults will be used.')
    version = data.get('schema_version', 1)
    if not isinstance(version, int) or version > CONFIG_SCHEMA:
        return Check(
            'schema', False,
            f'Config schema version {version!r} is newer than this Kova understands ({CONFIG_SCHEMA}).',
            'Update Kova',
        )
    unknown = sorted(set(data) - KNOWN_CONFIG_KEYS)
    if unknown:
        return Check(
            'schema', False,
            f'Config has unknown keys: {", ".join(unknown)}. They are ignored until fixed.',
            'Fix config.toml',
        )
    if version < CONFIG_SCHEMA:
        return Check('schema', True, f'Config schema version {version}; the next save upgrades it to {CONFIG_SCHEMA}.')
    return Check('schema', True, f'Config schema version {version}; no unknown keys.')


def _tools_check(entry) -> Check:
    if entry is None:
        return Check('tools', False, 'No active model to check for tool calling. Run kova setup.', 'kova setup')
    provider = entry._provider
    adapter = provider.adapter if provider is not None else 'openai'
    if supports(provider, 'tools'):
        return Check('tools', True, f'{adapter} adapter supports tool calling.')
    return Check(
        'tools', False,
        f'{adapter} adapter does not carry tool calls; choose a model that supports them.',
        'kova setup',
    )
