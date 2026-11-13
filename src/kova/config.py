"""Provider-aware config, with lossless migration of legacy model entries."""
from __future__ import annotations

import os
import secrets
import tempfile
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from kova.memory import DEFAULT_MEMORY_BUDGET

DEFAULT_MODEL = {"name": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "", "context": 32768}


@dataclass
class ProviderEntry:
    id: str
    name: str
    base_url: str
    kind: str = "custom"
    adapter: str = "openai"
    api_key: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    auth: dict = field(default_factory=dict, repr=False)
    _home: Path | None = field(default=None, repr=False, compare=False)


@dataclass
class ModelEntry:
    name: str
    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    context: int = 131072
    provider: str = ""
    _provider: ProviderEntry | None = field(default=None, repr=False, compare=False)
    _bound_api_key: str | None = field(default=None, repr=False, compare=False)


@dataclass
class Config:
    home: Path
    models: list[ModelEntry]
    active: str
    memory_path: Path
    files_path: Path
    port: int = 7700
    memory_write_subdir: str | None = None
    memory_budget: int = DEFAULT_MEMORY_BUDGET
    providers: list[ProviderEntry] = field(default_factory=list)
    oauth_host_id: str = field(default_factory=lambda: 'urn:uuid:' + str(uuid.uuid4()))
    _extra: dict = field(default_factory=dict, repr=False)

    def bind_providers(self) -> None:
        """Migrate unbound entries, deduplicating only identical credentials."""
        for provider in self.providers:
            provider._home = self.home
        for m in self.models:
            p = next((p for p in self.providers if p.id == m.provider), None)
            if m.provider and p is None:
                raise ValueError(f"Model {m.name!r} references an unknown provider")
            if p is None:
                p = next((p for p in self.providers if p.base_url == m.base_url and p.api_key == m.api_key
                          and p.kind == 'custom'), None)
                if p is None:
                    identifier = m.name or 'provider'
                    while any(p.id == identifier for p in self.providers):
                        identifier += '-2'
                    p = ProviderEntry(identifier, m.name, m.base_url, api_key=m.api_key)
                    self.providers.append(p)
                m.provider = p.id
            p._home = self.home
            m._provider = p
            m.base_url, m.api_key = p.base_url, p.api_key
            m._bound_api_key = p.api_key

    def active_model(self) -> ModelEntry:
        self.bind_providers()
        for m in self.models:
            if m.name == self.active:
                return m
        raise KeyError(self.active)


def kova_home() -> Path:
    env = os.environ.get('KOVA_HOME')
    return Path(env).expanduser() if env else Path.home() / '.kova'


def _default(home: Path) -> Config:
    return Config(home=home, models=[ModelEntry(**DEFAULT_MODEL)], active=DEFAULT_MODEL['name'],
                  memory_path=home / 'memory', files_path=Path.home() / 'Kova Files')


def load_config(home: Path | None = None) -> Config:
    home = Path(home) if home else kova_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / 'config.toml'
    if not path.exists():
        cfg = _default(home)
        save_config(cfg)
        return cfg
    data = tomllib.loads(path.read_text())
    providers = [ProviderEntry(**p) for p in data.get('providers', [])]
    models = [ModelEntry(**({'base_url': '', **m})) for m in data.get('models', [])]
    if not models and 'providers' not in data:
        models = [ModelEntry(**DEFAULT_MODEL)]
    mem = data.get('memory', {})
    cfg = Config(home=home, models=models, providers=providers,
                 active=data.get('active', models[0].name if models else ''),
                 memory_path=Path(mem.get('path', str(home / 'memory'))).expanduser(),
                 memory_write_subdir=mem.get('write_subdir') or None,
                 memory_budget=int(mem.get('budget', DEFAULT_MEMORY_BUDGET)),
                 files_path=Path(data.get('files_path', str(Path.home() / 'Kova Files'))).expanduser(),
                 port=int(data.get('port', 7700)),
                 oauth_host_id=data.get('oauth_host_id', 'urn:uuid:' + str(uuid.uuid4())))
    cfg.bind_providers()
    # Tighten legacy credential files too, not only new writes.
    path.chmod(0o600)
    return cfg


def private_write(path: Path, text: str) -> None:
    """Atomic replacement; secrets are never written through a public temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_config(cfg: Config) -> None:
    # Legacy callers may still assign a model's key before saving.
    for m in cfg.models:
        if m._provider is not None and m.api_key != m._bound_api_key:
            m._provider.api_key = m.api_key
    cfg.bind_providers()
    memory = {'path': str(cfg.memory_path), 'budget': cfg.memory_budget}
    if cfg.memory_write_subdir:
        memory['write_subdir'] = cfg.memory_write_subdir
    providers = []
    for p in cfg.providers:
        raw = {k: getattr(p, k) for k in ('id', 'name', 'base_url', 'kind', 'adapter', 'headers', 'auth')}
        if p.api_key:
            raw['api_key'] = p.api_key
        providers.append(raw)
    data = {'schema_version': 2, 'oauth_host_id': cfg.oauth_host_id, 'active': cfg.active, 'port': cfg.port, 'files_path': str(cfg.files_path),
            'memory': memory, 'providers': providers,
            'models': [{'name': m.name, 'provider': m.provider, 'base_url': m.base_url,
                        'model': m.model, 'context': m.context} for m in cfg.models]}
    private_write(cfg.home / 'config.toml', tomli_w.dumps(data))


def ensure_token(home: Path) -> str:
    path = Path(home) / 'token'
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(token)
    return token
