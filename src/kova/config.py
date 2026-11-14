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
    compact_threshold: float = 0.75
    memory_write_subdir: str | None = None
    memory_budget: int = DEFAULT_MEMORY_BUDGET
    #: How much of the always-loaded index note may reach the prompt.
    memory_index_lines: int = 200
    #: Post-turn review (#37): opt-in, and how many user turns between runs.
    memory_review: bool = False
    memory_review_every: int = 12
    #: Nightly consolidation (#38): opt-in, when it may start, and its gates.
    memory_consolidate: bool = False
    memory_consolidate_hour: int = 3
    memory_consolidate_min_confidence: float = 0.8
    memory_consolidate_max_loss: float = 0.25
    providers: list[ProviderEntry] = field(default_factory=list)
    oauth_host_id: str = field(default_factory=lambda: 'urn:uuid:' + str(uuid.uuid4()))
    tui_theme: str = "dark"
    tui_keys: dict[str, str] = field(default_factory=dict)
    persona_name: str = "Kova"
    persona_tone: str = "warm"
    persona_configured: bool = False
    _extra: dict = field(default_factory=dict, repr=False)

    def bind_providers(self) -> None:
        """Bind entries, preserving a named custom provider when re-added."""
        for provider in self.providers:
            provider._home = self.home
        for m in self.models:
            p = next((p for p in self.providers if p.id == m.provider), None)
            if m.provider and p is None:
                raise ValueError(f"Model {m.name!r} references an unknown provider")
            if p is None:
                # Re-adding a named model is an update, not a new provider.
                # Never carry credentials across endpoint changes or kinds.
                p = next((p for p in self.providers if p.name == m.name
                          and p.base_url == m.base_url and p.kind == 'custom'), None)
                if p is not None and m.api_key is not None:
                    p.api_key = m.api_key
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
    # An empty path means "unset", never the process working directory (#90).
    mem_path = str(mem.get('path') or '').strip() or str(home / 'memory')
    cfg = Config(home=home, models=models, providers=providers,
                 active=data.get('active', models[0].name if models else ''),
                 memory_path=Path(mem_path).expanduser(),
                 memory_write_subdir=mem.get('write_subdir') or None,
                 memory_budget=int(mem.get('budget', DEFAULT_MEMORY_BUDGET)),
                 memory_index_lines=int(mem.get('index_lines', 200)),
                 memory_review=bool(mem.get('review', False)),
                 memory_review_every=int(mem.get('review_every', 12)),
                 memory_consolidate=bool(mem.get('consolidate', False)),
                 memory_consolidate_hour=int(mem.get('consolidate_hour', 3)),
                 memory_consolidate_min_confidence=float(mem.get('consolidate_min_confidence', 0.8)),
                 memory_consolidate_max_loss=float(mem.get('consolidate_max_loss', 0.25)),
                 files_path=Path(data.get('files_path', str(Path.home() / 'Kova Files'))).expanduser(),
                 port=int(data.get('port', 7700)),
                 compact_threshold=float(data.get('compact_threshold', 0.75)),
                 oauth_host_id=data.get('oauth_host_id', 'urn:uuid:' + str(uuid.uuid4())),
                 tui_theme=data.get('tui', {}).get('theme', 'dark'),
                 tui_keys=data.get('tui', {}).get('keys', {}),
                 persona_name=data.get('persona', {}).get('name', 'Kova'),
                 persona_tone=data.get('persona', {}).get('tone', 'warm'),
                 persona_configured=data.get('persona', {}).get('configured', False))
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
    memory = {'path': str(cfg.memory_path), 'budget': cfg.memory_budget,
              'index_lines': cfg.memory_index_lines,
              'review': cfg.memory_review, 'review_every': cfg.memory_review_every,
              'consolidate': cfg.memory_consolidate,
              'consolidate_hour': cfg.memory_consolidate_hour,
              'consolidate_min_confidence': cfg.memory_consolidate_min_confidence,
              'consolidate_max_loss': cfg.memory_consolidate_max_loss}
    if cfg.memory_write_subdir:
        memory['write_subdir'] = cfg.memory_write_subdir
    providers = []
    for p in cfg.providers:
        raw = {k: getattr(p, k) for k in ('id', 'name', 'base_url', 'kind', 'adapter', 'headers', 'auth')}
        if p.api_key:
            raw['api_key'] = p.api_key
        providers.append(raw)
    data = {'schema_version': 2, 'oauth_host_id': cfg.oauth_host_id, 'active': cfg.active, 'port': cfg.port, 'compact_threshold': cfg.compact_threshold, 'files_path': str(cfg.files_path),
            'memory': memory, 'tui': {'theme': cfg.tui_theme, 'keys': cfg.tui_keys}, 'persona': {'name': cfg.persona_name, 'tone': cfg.persona_tone,
                                           'configured': cfg.persona_configured}, 'providers': providers,
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
