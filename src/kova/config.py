"""Provider-aware config, with lossless migration of legacy model entries."""
from __future__ import annotations

import difflib
import os
import secrets
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from kova.memory import DEFAULT_MEMORY_BUDGET

DEFAULT_MODEL = {"name": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "", "context": 32768}

CONFIG_SCHEMA = 2

#: Every key this version reads or writes. Anything else is preserved for the
#: user (and for a newer Kova) but never interpreted here.
TOP_LEVEL_CONFIG_KEYS = frozenset({
    'schema_version', 'oauth_host_id', 'active', 'port', 'compact_threshold',
    'files_path', 'memory', 'tui', 'persona', 'providers', 'models', 'bind',
})
CONFIG_SECTION_KEYS = {
    'memory': frozenset({
        'path', 'budget', 'index_lines', 'review', 'review_every',
        'consolidate', 'consolidate_hour', 'consolidate_min_confidence',
        'consolidate_max_loss', 'write_subdir',
    }),
    'tui': frozenset({'theme', 'keys'}),
    'persona': frozenset({'name', 'tone', 'configured'}),
}
MODEL_CONFIG_KEYS = frozenset({
    'name', 'base_url', 'model', 'api_key', 'context', 'provider', 'vision',
})
PROVIDER_CONFIG_KEYS = frozenset({
    'id', 'name', 'base_url', 'kind', 'adapter', 'api_key', 'headers', 'auth',
})

#: Warnings already shown in this process, keyed by file and rendered message.
_REPORTED_CONFIG_WARNINGS: set[tuple[str, str]] = set()

#: Sentinel for "this optional key should not be in the file".
_UNSET = object()


def _closest(key: str, known: frozenset[str] | set[str]) -> str | None:
    matches = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _scan_unknown(table: Any, known: frozenset[str] | set[str],
                  prefix: str) -> list[tuple[str, str | None]]:
    if not isinstance(table, Mapping):
        return []
    found = []
    for key in table:
        if key in known:
            continue
        suggestion = _closest(str(key), known)
        found.append((f'{prefix}{key}', f'{prefix}{suggestion}' if suggestion else None))
    return found


def unknown_config_keys(data: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """Unrecognised config keys as ``(dotted_path, closest_known_path)``.

    Only key names are inspected; this never returns or formats values, so it
    is safe to show in diagnostics and logs.
    """
    found = _scan_unknown(data, TOP_LEVEL_CONFIG_KEYS, '')
    for section, known in CONFIG_SECTION_KEYS.items():
        found.extend(_scan_unknown(data.get(section), known, f'{section}.'))
    for section, known in (('models', MODEL_CONFIG_KEYS),
                           ('providers', PROVIDER_CONFIG_KEYS)):
        entries = data.get(section)
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                found.extend(_scan_unknown(entry, known, f'{section}[{index}].'))
    return found


def config_key_warnings(data: Mapping[str, Any]) -> list[str]:
    """One readable line per unknown key; names and suggestions only."""
    lines = []
    for key, suggestion in unknown_config_keys(data):
        hint = f' — did you mean {suggestion!r}?' if suggestion else ''
        lines.append(f'config.toml: unknown key {key!r}{hint}')
    return lines


def _report_unknown_config_keys(path: Path, data: Mapping[str, Any]) -> None:
    """Warn once per file and key in this process, never on every load."""
    for line in config_key_warnings(data):
        marker = (str(path), line)
        if marker in _REPORTED_CONFIG_WARNINGS:
            continue
        _REPORTED_CONFIG_WARNINGS.add(marker)
        print(line, file=sys.stderr)


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
    vision: bool = False
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
    bind: str = "loopback"  # "lan" also serves paired phones on the private LAN address
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
    _report_unknown_config_keys(path, data)
    # Unknown entry keys belong to the user (or a newer Kova); keep them in the
    # document rather than passing them to the dataclasses and failing.
    providers = [ProviderEntry(**{k: v for k, v in p.items() if k in PROVIDER_CONFIG_KEYS})
                 for p in data.get('providers', [])]
    models = [ModelEntry(**({'base_url': '', **{k: v for k, v in m.items() if k in MODEL_CONFIG_KEYS}}))
              for m in data.get('models', [])]
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
                 persona_configured=data.get('persona', {}).get('configured', False),
                 bind='lan' if data.get('bind') == 'lan' else 'loopback')
    # Kept for inspection and for recreating a deleted file; ordinary saves
    # preserve unknown keys by editing the document that is already on disk.
    cfg._extra = {k: v for k, v in data.items() if k not in TOP_LEVEL_CONFIG_KEYS}
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


def _existing_document(cfg: Config) -> tomlkit.TOMLDocument:
    """The file's document, so comments and unknown keys survive an update."""
    path = cfg.home / 'config.toml'
    try:
        text = path.read_text()
    except OSError:
        text = ''
    if text.strip():
        try:
            return tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError:
            pass
    # No usable file: rebuild only from values already parsed as unknown.
    doc = tomlkit.document()
    for key, value in cfg._extra.items():
        if key in doc:
            continue
        try:
            doc[key] = value
        except (TypeError, ValueError):
            pass
    return doc


def _section(parent, name: str):
    table = parent.get(name)
    if not isinstance(table, Mapping) or not hasattr(table, '__setitem__'):
        parent[name] = tomlkit.table()
        table = parent[name]
    return table


def _put(table, key: str, value) -> None:
    if value is _UNSET:
        if key in table:
            del table[key]
    else:
        table[key] = value


def _merge_map(table, values: Mapping[str, str]) -> None:
    for key in list(table):
        if key not in values:
            del table[key]
    for key, value in values.items():
        table[key] = value


def _merge_entry_tables(doc, name: str, entries: list, identity: str,
                        values_for) -> None:
    """Update known fields in place, leaving each table's extras and comments.

    The file order follows ``entries``: a caller that reorders models or
    providers must see that order after the next load, while each moved table
    keeps its own unknown keys and comments.
    """
    tables = doc.get(name)
    if not isinstance(tables, tomlkit.items.AoT):
        tables = tomlkit.aot()
        doc[name] = tables
    matched: set[int] = set()
    ordered: list = []
    for entry in entries:
        values = values_for(entry)
        table = None
        for index, item in enumerate(tables):
            if index not in matched and item.get(identity) == values[identity]:
                table = item
                matched.add(index)
                break
        if table is None:
            table = tomlkit.table()
            tables.append(table)
            matched.add(len(tables) - 1)
        ordered.append(table)
        for key, value in values.items():
            _put(table, key, value)
    for index in range(len(tables) - 1, -1, -1):
        if index not in matched:
            del tables[index]
    if any(tables[index] is not table for index, table in enumerate(ordered)):
        for index in range(len(tables) - 1, -1, -1):
            del tables[index]
        for table in ordered:
            tables.append(table)


def _provider_values(provider: ProviderEntry) -> dict[str, Any]:
    return {
        'id': provider.id,
        'name': provider.name,
        'base_url': provider.base_url,
        'kind': provider.kind,
        'adapter': provider.adapter,
        'headers': dict(provider.headers),
        'auth': dict(provider.auth),
        'api_key': provider.api_key if provider.api_key else _UNSET,
    }


def _model_values(model: ModelEntry) -> dict[str, Any]:
    return {
        'name': model.name,
        'provider': model.provider,
        'base_url': model.base_url,
        'model': model.model,
        'context': model.context,
        'vision': model.vision,
        # Hydrated from the provider; never duplicate the credential here.
        'api_key': _UNSET,
    }


def save_config(cfg: Config) -> None:
    # Legacy callers may still assign a model's key before saving.
    for m in cfg.models:
        if m._provider is not None and m.api_key != m._bound_api_key:
            m._provider.api_key = m.api_key
    cfg.bind_providers()
    doc = _existing_document(cfg)
    doc['schema_version'] = CONFIG_SCHEMA
    doc['oauth_host_id'] = cfg.oauth_host_id
    doc['active'] = cfg.active
    doc['port'] = cfg.port
    doc['compact_threshold'] = cfg.compact_threshold
    doc['files_path'] = str(cfg.files_path)
    memory = _section(doc, 'memory')
    _put(memory, 'path', str(cfg.memory_path))
    _put(memory, 'budget', cfg.memory_budget)
    _put(memory, 'index_lines', cfg.memory_index_lines)
    _put(memory, 'review', cfg.memory_review)
    _put(memory, 'review_every', cfg.memory_review_every)
    _put(memory, 'consolidate', cfg.memory_consolidate)
    _put(memory, 'consolidate_hour', cfg.memory_consolidate_hour)
    _put(memory, 'consolidate_min_confidence', cfg.memory_consolidate_min_confidence)
    _put(memory, 'consolidate_max_loss', cfg.memory_consolidate_max_loss)
    _put(memory, 'write_subdir', cfg.memory_write_subdir or _UNSET)
    tui = _section(doc, 'tui')
    _put(tui, 'theme', cfg.tui_theme)
    _merge_map(_section(tui, 'keys'), cfg.tui_keys)
    persona = _section(doc, 'persona')
    _put(persona, 'name', cfg.persona_name)
    _put(persona, 'tone', cfg.persona_tone)
    _put(persona, 'configured', cfg.persona_configured)
    _merge_entry_tables(doc, 'providers', cfg.providers, 'id', _provider_values)
    _merge_entry_tables(doc, 'models', cfg.models, 'name', _model_values)
    _put(doc, 'bind', 'lan' if cfg.bind == 'lan' else _UNSET)
    private_write(cfg.home / 'config.toml', tomlkit.dumps(doc))


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
