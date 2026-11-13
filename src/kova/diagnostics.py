"""Bounded, credential-safe first-run diagnostics; never starts a server."""
from __future__ import annotations

import os

import httpx

from kova.config import load_config
from kova.providers import ProviderError, list_models


def diagnose() -> list[tuple[bool, str]]:
    try:
        cfg = load_config()
    except (OSError, ValueError, TypeError, KeyError):
        return [(False, 'Cannot read config.toml. Fix its TOML and provider references, then run kova doctor.')]
    checks = [(True, 'Configuration loaded.')]
    try:
        with httpx.Client(timeout=4, trust_env=False) as client:
            response = client.get(f'http://127.0.0.1:{cfg.port}/health')
            data = response.json()
            valid = response.status_code == 200 and isinstance(data, dict) and 'version' in data and 'model_ok' in data
            checks.append((valid, 'Kova server is running.' if valid else 'Port is occupied by another service. Choose a free port in config.toml.'))
    except httpx.ConnectError:
        checks.append((True, 'Server is stopped; kova starts it automatically.'))
    except (httpx.HTTPError, ValueError):
        checks.append((False, 'Server is not responding correctly. Check server.log and the configured port.'))
    try:
        entry = cfg.active_model()
        if not entry.model.strip():
            raise KeyError()
        available = list_models(entry._provider, timeout=4)
        present = entry.model in available
        checks.append((present, 'Provider reachable; active model is listed.' if present else 'Active model is not available. Load it or choose another with kova setup.'))
    except KeyError:
        checks.append((False, 'No active model configured. Run kova setup.'))
    except ProviderError as exc:
        checks.append((False, str(exc) + ' Run kova setup.'))
    path = cfg.memory_path
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    writable = (not path.exists() or path.is_dir()) and parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    checks.append((writable, 'Memory directory is writable (or can be created).' if writable else 'Memory directory is not writable. Fix permissions or use kova memory use PATH.'))
    return checks
