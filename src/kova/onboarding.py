"""Explicit provider setup. Only model catalogs are requested; never a test chat."""
from __future__ import annotations

import getpass
import json
import re
import sys
import warnings

import httpx
from rich.console import Console
from rich.text import Text

from kova import server_finder
from kova.client import KovaClient, KovaError
from kova.config import Config, ensure_token, save_config
from kova.providers import CATALOG, ProviderError, install_provider, list_models, make_provider
from kova.server_finder import normalize_base

console = Console()


class SetupError(RuntimeError):
    """An actionable setup error, with no upstream response bodies or credentials."""


def _ask(prompt: str, default: str | None = None) -> str:
    console.print(Text(prompt), end=' ')
    try:
        value = input().strip()
    except EOFError:
        raise SetupError('Setup cancelled. Run kova setup again any time.') from None
    return value or (default or '')


def _secret(prompt: str) -> str:
    # getpass never silently falls back to echoing a credential in a pipe.
    if not sys.stdin.isatty():
        raise SetupError('Use a terminal to paste credentials securely, or provide --key via a trusted script.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass(prompt + ' ').strip()
    except getpass.GetPassWarning:
        raise SetupError('No secure terminal available. Use a terminal to paste credentials.') from None


def _select(title: str, choices: list[str], *, default: int | None = 0) -> int:
    if console.is_terminal and sys.stdin.isatty():
        from prompt_toolkit.shortcuts import radiolist_dialog
        selected = radiolist_dialog(title=title, text='↑/↓ to move · Enter to choose · Cancel to leave',
                                    values=list(enumerate(choices))).run()
        if selected is None:
            raise SetupError('Setup cancelled. Nothing saved.')
        return selected
    console.print(Text(title, style='bold'))
    for i, label in enumerate(choices, 1):
        console.print(Text(f'  {i}. {label}'))
    while True:
        value = _ask('Choose a number' + (' (Enter = 1):' if default == 0 else ':'),
                     str(default + 1) if default is not None else None)
        if not value:
            raise SetupError('Setup cancelled. Nothing saved.')
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return int(value) - 1
        console.print('Choose one of the numbers above.')


def _running_client(cfg: Config) -> KovaClient | None:
    base = f'http://127.0.0.1:{cfg.port}'
    client = KovaClient(base, ensure_token(cfg.home), http=httpx.Client(base_url=base, timeout=2, trust_env=False))
    try:
        client.health()
        return client
    except KovaError:
        client.http.close()
        return None


def _save_provider(cfg, provider, selected, active=None, *, legacy_name=None):
    client = _running_client(cfg)
    if client:
        try:
            client.provider_add(provider, selected, active, legacy_name=legacy_name)
        finally:
            client.http.close()
    else:
        install_provider(cfg, provider, selected, active)
        if legacy_name:
            cfg.active_model().name = legacy_name
            cfg.active = legacy_name
            save_config(cfg)
    console.print(Text(f'✓ Connected {provider.name}. Active model: {active or selected[0]}.', style='green'))
    console.print('Run kova to chat. Add another provider any time with kova providers add.')


def _pick_models(models: list[str]) -> tuple[list[str], str]:
    if not models:
        raise SetupError('This provider did not list any models. Check access or load a model first.')
    filtered = models
    if len(models) > 20:
        while True:
            query = _ask(f'{len(models)} models available. Filter by name (blank = all):')
            filtered = [m for m in models if query.casefold() in m.casefold()]
            if filtered:
                break
            console.print('No matches. Try another filter.')
    if console.is_terminal and sys.stdin.isatty():
        from prompt_toolkit.shortcuts import checkboxlist_dialog
        selected = checkboxlist_dialog(title='Choose models', text='Space to select · Enter to continue',
                                       values=[(m, m) for m in filtered], default_values=[filtered[0]]).run()
        if not selected:
            raise SetupError('Setup cancelled. Nothing saved.')
        return selected, selected[_select('Active model', selected)]
    console.print(Text('Choose models', style='bold'))
    for i, name in enumerate(filtered, 1):
        console.print(Text(f'  {i}. {name}'))
    while True:
        raw = _ask('Model numbers, separated by commas (Enter = 1):', '1')
        try:
            indexes = list(dict.fromkeys(int(i.strip()) - 1 for i in raw.split(',')))
            if not indexes or any(i < 0 or i >= len(filtered) for i in indexes):
                raise ValueError()
            selected = [filtered[i] for i in indexes]
            break
        except ValueError:
            console.print('Use numbers from the list, such as 1 or 1,3.')
    active = selected[_select('Active model', selected)]
    return selected, active


def _identifier(cfg: Config, default: str) -> str:
    while any(p.id == default and (p.api_key or p.auth or p.headers or
              any(m.provider == p.id and m.model for m in cfg.models)) for p in cfg.providers):
        default += '-2'
    value = _ask(f'Connection name (Enter = {default}):', default)
    identifier = re.sub(r'[^a-zA-Z0-9_-]+', '-', value).strip('-')
    if not identifier:
        raise SetupError('Choose a connection name containing letters or numbers.')
    return identifier


def needs_setup(cfg: Config, **kwargs) -> bool:
    """Do not auto-discover or fetch catalogs on chat startup."""
    try:
        return not bool(cfg.active_model().model)
    except KeyError:
        return True


def run_setup(cfg: Config, *, url: str | None = None, model: str | None = None,
              name: str | None = None, api_key: str | None = None, yes: bool = False,
              provider_kind: str | None = None, transport=None, timeout: float = 10, **kwargs) -> None:
    try:
        if url:
            found = server_finder.connect(url, api_key=api_key, transport=transport, timeout=timeout)
            if not found.answered:
                raise SetupError('Could not reach that model server. Check its address and that it is running.')
            if found.needs_key:
                raise SetupError('Provider refused the API key. Supply --key or use interactive setup.')
            identifier = name or 'local'
            provider = make_provider('custom' if api_key else 'local', identifier=identifier, name=identifier,
                                     base_url=found.base_url, api_key=api_key)
            available = list_models(provider, transport=transport, timeout=timeout)
            if model and model not in available:
                raise SetupError('That model is not listed by this provider.')
            chosen = model or (available[0] if available else '')
            if not chosen:
                raise SetupError("The provider didn't list any models. Load a model first.")
            _save_provider(cfg, provider, [chosen], legacy_name=identifier)
            return
        if model or name or api_key:
            raise SetupError('Pass --url along with --model, --name, or --key.')
        console.print(Text('\nConnect a provider', style='bold'))
        console.print('Choose where your messages go. Setup only lists models; it never sends a chat.')
        if provider_kind:
            spec = next((s for s in CATALOG if s.id == provider_kind), None)
            if spec is None:
                raise SetupError('Unknown provider type.')
        else:
            spec = CATALOG[_select('Provider', [s.name for s in CATALOG], default=None)]
        if spec.id == 'local':
            address = _ask('Server address (for example homelab:8080):')
            if not address:
                raise SetupError('Setup cancelled. Nothing saved.')
            found = server_finder.connect(address, transport=transport, timeout=timeout)
            if not found.answered:
                raise SetupError('Could not reach that server. Check the address and try again.')
            key = _secret('API key:') if found.needs_key else None
            provider = make_provider('local', base_url=found.base_url, api_key=key)
        elif spec.id == 'custom':
            base_url = _ask('OpenAI-compatible base URL (include its API path):')
            provider = make_provider('custom', base_url=base_url, api_key=_secret('API key (optional):') or None)
            raw = _secret('Custom headers as a JSON object (optional):')
            if raw:
                try:
                    headers = json.loads(raw)
                    if not isinstance(headers, dict):
                        raise ValueError()
                    provider = make_provider('custom', base_url=base_url, api_key=provider.api_key, headers=headers)
                except (ValueError, AttributeError):
                    raise SetupError('Headers must be a JSON object of string values.') from None
        else:
            provider = make_provider(spec.id)
        provider.id = _identifier(cfg, spec.id)
        provider.name = provider.id if provider.id != spec.id else spec.name
        if spec.id == 'codex':
            from kova.provider_auth import reuse_codex
            consent = _ask('Read ~/.codex/auth.json for requests? Kova will not copy or change it. [y/N]:')
            reuse_codex(provider, consent=consent.lower() == 'y')
        elif spec.id == 'chatgpt':
            from kova.provider_auth import login
            console.print('Continue with ChatGPT in your browser. Waiting for sign-in…')
            save_config(cfg)  # Persist this host identity before first authorization.
            provider.auth['ext_agent_host_id'] = cfg.oauth_host_id
            login(provider)
        elif spec.key_url:
            console.print(Text(f'Get an API key: {spec.key_url}'))
            provider.api_key = _secret('API key:')
            if not provider.api_key:
                raise SetupError('An API key is required for this provider.')
        models = list_models(provider, transport=transport, timeout=timeout)
        selected, active = _pick_models(models)
        _save_provider(cfg, provider, selected, active)
    except (ProviderError, KovaError) as exc:
        raise SetupError(str(exc)) from None
