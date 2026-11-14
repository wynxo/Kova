"""Provider catalog and inexpensive, explicitly requested model discovery.

This module owns the capability contract. Every capability is declared once —
by an adapter's wire format (see :mod:`kova.model_adapters`), by a provider
kind (here) or by a chat mode (see :mod:`kova.modes`) — and every UI layer
asks :func:`capabilities` or :func:`offered` before offering behaviour, so no
layer branches on a provider or mode name.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from kova.config import Config, ModelEntry, ProviderEntry, save_config


class ProviderError(RuntimeError):
    """A credential or provider operation failed; messages never include secrets."""


# The capability vocabulary, and the layers that must declare each one before a
# UI may offer it. ``provider`` means the adapter's wire format plus the
# provider kind; ``mode`` means the chat mode.
#
# A conversation capability:
#   tools          the model can call tools, so tool-backed work may be offered
#   approvals      the conversation asks before acting, so approval UI applies
#   reasoning      the provider streams the model's reasoning
#   steering       the provider accepts a message while a turn is running
#
# A connection capability, which belongs to the provider alone:
#   api_key             an API key is accepted
#   browser_login       connect by signing in through the browser
#   harness_login       reuse a locally installed agent login
#   editable_base_url   the user chooses the address
#   custom_headers      extra HTTP headers are accepted
CAPABILITY_GATES = {
    'vision': ('provider',),
    'tools': ('provider', 'mode'),
    'approvals': ('mode',),
    'reasoning': ('provider',),
    'steering': ('provider',),
    'api_key': ('provider',),
    'browser_login': ('provider',),
    'harness_login': ('provider',),
    'editable_base_url': ('provider',),
    'custom_headers': ('provider',),
}
CAPABILITIES = tuple(CAPABILITY_GATES)
CONVERSATION_CAPABILITIES = ('tools', 'approvals', 'reasoning', 'steering', 'vision')
CONNECTION_CAPABILITIES = ('api_key', 'browser_login', 'harness_login',
                           'editable_base_url', 'custom_headers')

_REASONING = frozenset({'api_key', 'reasoning'})


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    base_url: str
    key_url: str = ''
    adapter: str = 'openai'
    auth_style: str = 'bearer'
    capabilities: frozenset[str] = frozenset({'api_key'})
    base_url_hint: str = ''


CATALOG = (
    ProviderSpec('openai', 'OpenAI', 'https://api.openai.com/v1', 'https://platform.openai.com/api-keys'),
    ProviderSpec('anthropic', 'Anthropic', 'https://api.anthropic.com/v1', 'https://console.anthropic.com/settings/keys', 'anthropic', 'x-api-key',
                 capabilities=_REASONING),
    ProviderSpec('gemini', 'Google Gemini', 'https://generativelanguage.googleapis.com/v1beta/openai', 'https://aistudio.google.com/apikey'),
    ProviderSpec('openrouter', 'OpenRouter', 'https://openrouter.ai/api/v1', 'https://openrouter.ai/settings/keys',
                 capabilities=_REASONING),
    ProviderSpec('deepseek', 'DeepSeek', 'https://api.deepseek.com/v1', 'https://platform.deepseek.com/api_keys',
                 capabilities=_REASONING),
    ProviderSpec('groq', 'Groq', 'https://api.groq.com/openai/v1', 'https://console.groq.com/keys'),
    ProviderSpec('mistral', 'Mistral', 'https://api.mistral.ai/v1', 'https://console.mistral.ai/api-keys'),
    ProviderSpec('xai', 'xAI', 'https://api.x.ai/v1', 'https://console.x.ai'),
    ProviderSpec('chatgpt', 'ChatGPT subscription', 'https://api.openai.com/v1', adapter='responses', auth_style='oauth',
                 capabilities=frozenset({'browser_login', 'reasoning'})),
    ProviderSpec('codex', 'Installed Codex login', 'https://chatgpt.com/backend-api/codex', adapter='responses', auth_style='harness',
                 capabilities=frozenset({'harness_login', 'reasoning'})),
    ProviderSpec('local', 'Local server', '', auth_style='optional',
                 capabilities=frozenset({'api_key', 'editable_base_url', 'reasoning'}),
                 base_url_hint='homelab:8080'),
    ProviderSpec('custom', 'Custom provider', '', auth_style='optional',
                 capabilities=frozenset({'api_key', 'editable_base_url', 'custom_headers', 'reasoning'}),
                 base_url_hint='https://example.com/v1'),
)


def spec_for(kind: str) -> ProviderSpec | None:
    """The catalog entry for one provider kind, or ``None`` when it is unknown."""
    return next((s for s in CATALOG if s.id == kind), None)


def provider_capabilities(kind: str, adapter: str) -> frozenset[str]:
    """What a provider declares: its adapter's wire format plus its kind."""
    from kova.model_adapters import adapter_capabilities
    spec = spec_for(kind)
    declared = spec.capabilities if spec is not None else frozenset({'api_key'})
    return adapter_capabilities(adapter) | declared


def spec_capabilities(spec: ProviderSpec) -> frozenset[str]:
    """What a provider of this kind declares, before it is configured."""
    return provider_capabilities(spec.id, spec.adapter)


def capabilities(provider: ProviderEntry | None) -> frozenset[str]:
    """Everything one provider declares: its adapter's wire format plus its kind.

    A model with no provider entry is a server Kova knows nothing about, so it
    keeps every capability and nothing is hidden from it.
    """
    if provider is None:
        return frozenset(CAPABILITIES)
    return provider_capabilities(provider.kind, provider.adapter)


def supports(provider: ProviderEntry | None, capability: str) -> bool:
    """Whether one provider declares a single capability."""
    return capability in capabilities(provider)


def offered(provider: ProviderEntry | None, mode: str) -> frozenset[str]:
    """The behaviours a UI may offer for one conversation.

    Every layer that gates a capability must declare it: the provider for its
    wire format and kind, and the mode for what a conversation may do at all.
    """
    from kova.modes import capabilities as mode_capabilities
    declared = {'provider': capabilities(provider), 'mode': mode_capabilities(mode)}
    return frozenset(name for name, layers in CAPABILITY_GATES.items()
                     if all(name in declared[layer] for layer in layers))


def validate_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderError('Use an http(s) base URL without credentials, query parameters or fragments.')
    return base_url.rstrip('/')


def make_provider(kind: str, *, name: str | None = None, base_url: str | None = None,
                  api_key: str | None = None, headers: dict | None = None, identifier: str | None = None) -> ProviderEntry:
    spec = spec_for(kind)
    if spec is None:
        raise ProviderError('Unknown provider type.')
    if base_url and base_url.rstrip('/') != spec.base_url and 'editable_base_url' not in spec.capabilities:
        raise ProviderError('Use Custom provider to connect a different base URL.')
    if api_key and 'api_key' not in spec.capabilities:
        raise ProviderError(f'{spec.name} does not use an API key.')
    custom = headers or {}
    if not isinstance(custom, dict):
        raise ProviderError('Custom headers must be a JSON object.')
    if custom and 'custom_headers' not in spec.capabilities:
        raise ProviderError('Only a Custom provider accepts extra headers.')
    if any(not isinstance(k, str) or not isinstance(v, str) or '\n' in k + v or '\r' in k + v for k, v in custom.items()):
        raise ProviderError('Custom headers must be single-line strings.')
    return ProviderEntry(identifier or kind, name or spec.name, validate_url(base_url or spec.base_url),
                         kind=kind, adapter=spec.adapter, api_key=api_key, headers=custom)


def headers_for(provider: ProviderEntry, *, transport=None) -> dict[str, str]:
    from kova.provider_auth import credentials
    auth = credentials(provider, transport=transport)
    headers = dict(provider.headers)
    if provider.api_key:
        if provider.adapter == 'anthropic':
            headers['x-api-key'] = provider.api_key
        else:
            headers['Authorization'] = f'Bearer {provider.api_key}'
    if provider.adapter == 'anthropic':
        headers['anthropic-version'] = '2023-06-01'
    if auth.get('access_token'):
        headers['Authorization'] = f"Bearer {auth['access_token']}"
    if provider.kind == 'codex' and auth.get('account_id'):
        headers['ChatGPT-Account-Id'] = auth['account_id']
    return headers


def list_models(provider: ProviderEntry, *, transport: httpx.BaseTransport | None = None, timeout: float = 10) -> list[str]:
    """Validate by listing, never inference. Follow Anthropic catalog pagination."""
    path = '/models?client_version=0.0.0' if provider.kind == 'codex' else '/models'
    result = []
    try:
        with httpx.Client(transport=transport, timeout=timeout, trust_env=False, headers=headers_for(provider, transport=transport)) as client:
            if provider.kind == 'openrouter':
                # Its model catalog is public, so it cannot validate a key by itself.
                check = client.get(provider.base_url.rstrip('/') + '/key')
                if check.status_code != 200:
                    raise ProviderError('Provider refused access. Check the API key and try again.')
            for _ in range(100):
                response = client.get(provider.base_url.rstrip('/') + path)
                if response.status_code in (401, 403):
                    raise ProviderError('Provider refused access. Check the API key or sign in again.')
                if response.status_code >= 400:
                    raise ProviderError(f'Could not list provider models (HTTP {response.status_code}).')
                data = response.json()
                raw = data.get('data', data.get('models'))
                if not isinstance(raw, list):
                    raise ProviderError('Provider returned an invalid model catalog.')
                for item in raw:
                    if isinstance(item, str):
                        result.append(item)
                    elif isinstance(item, dict):
                        identifier = item.get('id') or item.get('slug') or item.get('name')
                        if identifier and item.get('visibility', 'list') == 'list':
                            result.append(str(identifier))
                if provider.adapter != 'anthropic' or not data.get('has_more') or not data.get('last_id'):
                    break
                from urllib.parse import urlencode
                path = '/models?' + urlencode({'after_id': data['last_id']})
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise ProviderError('Could not read provider models. Check the address and connection.') from exc
    return list(dict.fromkeys(result))


def install_provider(cfg: Config, provider: ProviderEntry, selected: list[str], active: str | None = None) -> None:
    if not selected or any(not m.strip() for m in selected):
        raise ProviderError('Choose at least one model.')
    cfg.bind_providers()
    if any(p.id == provider.id and (p.api_key or p.auth or p.headers or
            any(m.provider == p.id and m.model for m in cfg.models)) for p in cfg.providers):
        raise ProviderError('A provider with this name already exists. Choose another name.')
    # Discard the empty first-run placeholder, never configured entries.
    cfg.models[:] = [m for m in cfg.models if m.model]
    used = {m.provider for m in cfg.models}
    cfg.providers[:] = [p for p in cfg.providers if p.id in used or p.auth or p.api_key or p.headers]
    cfg.providers.append(provider)
    for model in dict.fromkeys(selected):
        name = f'{provider.id}/{model}'
        cfg.models.append(ModelEntry(name, provider.base_url, model, provider=provider.id))
    cfg.active = f'{provider.id}/{active or selected[0]}'
    save_config(cfg)


def remove_provider(cfg: Config, identifier: str) -> None:
    if not any(p.id == identifier for p in cfg.providers):
        raise ProviderError('No provider with that name.')
    cfg.models[:] = [m for m in cfg.models if m.provider != identifier]
    cfg.providers[:] = [p for p in cfg.providers if p.id != identifier]
    if not any(m.name == cfg.active for m in cfg.models):
        cfg.active = cfg.models[0].name if cfg.models else ''
    save_config(cfg)
