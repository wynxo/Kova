"""The provider capability contract: declared once per layer, honoured by the UI.

Declarations live in ``kova.model_adapters`` (wire format), ``kova.providers``
(provider kinds) and ``kova.modes`` (chat modes); ``kova.providers.offered``
combines them. These tests pin the contract, the endpoints that publish it and
the refusals that enforce it.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from kova import config, modes, providers
from kova.config import ProviderEntry
from kova.server import create_app
from tests.fake_model import FakeModel, text


def provider(kind: str) -> ProviderEntry:
    """One provider of a catalog kind, with the credential that kind declares."""
    spec = providers.spec_for(kind)
    return ProviderEntry(kind, spec.name, spec.base_url, kind=kind, adapter=spec.adapter,
                         api_key='private' if 'api_key' in spec.capabilities else None)


def app_for(tmp_path, monkeypatch, install=None):
    """A live app over a temp home, optionally with one provider installed."""
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = config.load_config()
    if install is not None:
        kind, models = install
        providers.install_provider(cfg, provider(kind), models)
    app = create_app(cfg, transport=FakeModel([text('Hello there!')]).transport)
    return TestClient(app), {'Authorization': 'Bearer ' + config.ensure_token(cfg.home)}, cfg


# ------------------------------------------------------------------ declarations
def test_every_declaration_stays_inside_one_vocabulary():
    from kova.model_adapters import ADAPTER_CAPABILITIES
    declared = set(providers.CAPABILITIES)
    assert set(providers.CAPABILITY_GATES) == declared
    assert set(providers.CONVERSATION_CAPABILITIES) | set(providers.CONNECTION_CAPABILITIES) == declared
    assert not set(providers.CONVERSATION_CAPABILITIES) & set(providers.CONNECTION_CAPABILITIES)
    for spec in providers.CATALOG:
        assert spec.capabilities <= declared, spec.id
    for adapter, caps in ADAPTER_CAPABILITIES.items():
        assert caps <= declared, adapter
    for mode, caps in modes.MODE_CAPABILITIES.items():
        assert mode in modes.MODES and caps <= declared, mode


def test_a_provider_declares_its_adapter_plus_its_kind():
    assert providers.capabilities(provider('anthropic')) == frozenset({'api_key', 'reasoning', 'tools', 'vision'})
    assert providers.capabilities(provider('deepseek')) == frozenset({'api_key', 'reasoning', 'tools', 'vision'})
    assert providers.capabilities(provider('groq')) == frozenset({'api_key', 'tools', 'vision'})
    assert providers.capabilities(provider('chatgpt')) == frozenset({'browser_login', 'reasoning', 'tools', 'vision'})
    assert providers.capabilities(provider('codex')) == frozenset({'harness_login', 'reasoning', 'tools', 'vision'})
    assert providers.capabilities(provider('local')) == frozenset({'api_key', 'editable_base_url', 'reasoning', 'tools', 'vision'})
    assert providers.capabilities(provider('custom')) == frozenset(
        {'api_key', 'custom_headers', 'editable_base_url', 'reasoning', 'tools', 'vision'})
    # Chat completions has no reasoning channel: only providers that send one declare it.
    assert 'reasoning' not in providers.capabilities(provider('openai'))
    assert not providers.supports(provider('openai'), 'reasoning')


def test_a_model_with_no_provider_hides_nothing():
    assert providers.capabilities(None) == frozenset(providers.CAPABILITIES)
    assert providers.supports(None, 'reasoning')


def test_a_conversation_offers_only_what_every_gate_declares():
    openai, chatgpt = provider('openai'), provider('chatgpt')
    assert providers.offered(openai, 'work') == frozenset({'api_key', 'approvals', 'tools', 'vision'})
    # Chat mode drops tool-backed work and the approvals that gate it.
    assert providers.offered(openai, 'chat') == frozenset({'api_key', 'vision'})
    assert providers.offered(chatgpt, 'work') == frozenset({'approvals', 'browser_login', 'reasoning', 'tools', 'vision'})
    # Chat mode drops the tool-backed work; the provider's own declarations stay.
    assert providers.offered(chatgpt, 'chat') == frozenset({'browser_login', 'reasoning', 'vision'})
    # No adapter streams a message into a running turn, so steering is never offered.
    for kind in ('openai', 'anthropic', 'chatgpt', 'codex', 'local', 'custom'):
        assert 'steering' not in providers.offered(provider(kind), 'work')


def test_a_kind_refuses_the_fields_it_does_not_declare():
    with pytest.raises(providers.ProviderError, match='headers'):
        providers.make_provider('openai', headers={'X-Trace': 'on'})
    with pytest.raises(providers.ProviderError, match='API key'):
        providers.make_provider('chatgpt', api_key='private')
    with pytest.raises(providers.ProviderError, match='Custom provider'):
        providers.make_provider('anthropic', base_url='https://example.test/v1')


# ------------------------------------------------------------------- published
def test_the_catalog_and_the_contract_publish_capabilities(tmp_path, monkeypatch):
    client, headers, _ = app_for(tmp_path, monkeypatch, install=('deepseek', ['deepseek-reasoner']))
    with client:
        catalog = {s['id']: s for s in client.get('/providers/catalog', headers=headers).json()['providers']}
        assert catalog['chatgpt']['capabilities'] == ['browser_login', 'reasoning', 'tools', 'vision']
        assert catalog['custom']['capabilities'] == ['api_key', 'custom_headers', 'editable_base_url', 'reasoning', 'tools', 'vision']
        assert catalog['custom']['base_url_hint'] == 'https://example.com/v1'
        contract = client.get('/capabilities', headers=headers).json()
        assert contract['provider'] == 'deepseek'
        assert contract['capabilities'] == ['api_key', 'reasoning', 'tools']
        assert contract['offered'] == {'chat': ['api_key', 'reasoning'],
                                       'work': ['api_key', 'approvals', 'reasoning', 'tools']}
        listed = {p['id']: p for p in client.get('/providers', headers=headers).json()}
        assert listed['deepseek']['capabilities'] == sorted([*contract['capabilities'], 'vision'])


def test_the_contract_reports_the_active_providers_own_declarations(tmp_path, monkeypatch):
    client, headers, cfg = app_for(tmp_path, monkeypatch, install=('chatgpt', ['gpt-5']))
    with client:
        contract = client.get('/capabilities', headers=headers).json()
        assert contract['provider'] == 'chatgpt'
        assert 'api_key' not in contract['capabilities']
        assert contract['offered']['work'] == ['approvals', 'browser_login', 'reasoning', 'tools']


# ------------------------------------------------------------------- enforced
def test_forcing_an_unsupported_connection_field_is_refused(tmp_path, monkeypatch):
    client, headers, _ = app_for(tmp_path, monkeypatch)
    with client:
        headers_response = client.post('/providers/validate', headers=headers, json={
            'kind': 'openai', 'id': 'work', 'api_key': 'private', 'headers': {'X-Trace': 'on'}})
        assert headers_response.status_code == 400
        assert 'Custom provider' in headers_response.json()['detail']
        key_response = client.post('/providers', headers=headers, json={
            'kind': 'chatgpt', 'id': 'chatgpt', 'api_key': 'private', 'models': ['gpt-5']})
        assert key_response.status_code == 400
        assert 'API key' in key_response.json()['detail']


def test_login_refuses_a_credential_the_kind_does_not_take(tmp_path, monkeypatch):
    client, headers, _ = app_for(tmp_path, monkeypatch, install=('chatgpt', ['gpt-5']))
    with client:
        response = client.post('/providers/chatgpt/login', headers=headers, json={'api_key': 'private'})
        assert response.status_code == 400
        assert 'API key' in response.json()['detail']


def test_a_mode_without_approvals_refuses_approval_answers(tmp_path, monkeypatch):
    client, headers, _ = app_for(tmp_path, monkeypatch)
    with client:
        chat = client.post('/chats', headers=headers, json={'mode': 'chat'}).json()
        refused = client.post(f"/chats/{chat['id']}/approvals/anything", headers=headers,
                              json={'decision': 'allow'})
        assert refused.status_code == 409 and 'approval' in refused.json()['detail']
        work = client.post('/chats', headers=headers, json={'mode': 'work'}).json()
        unknown = client.post(f"/chats/{work['id']}/approvals/anything", headers=headers,
                              json={'decision': 'allow'})
        assert unknown.status_code == 404  # the mode asks; that approval just never existed


async def test_the_agent_sends_no_tool_schemas_when_the_provider_declares_none(tmp_path, monkeypatch):
    """Without the declaration no schema goes out, so no tool or approval can follow."""
    from datetime import datetime
    from kova.agent import Agent
    from kova.memory import Memory
    from kova.store import Store
    from kova.tools import ToolContext, build_tools
    cfg = config.load_config(tmp_path)
    store = Store(tmp_path / 'db.sqlite')
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    chat = store.create_chat()
    fake = FakeModel([text('Hi')])
    monkeypatch.setattr(providers, 'capabilities', lambda _provider: frozenset({'api_key'}))
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat['id'], 'hello')]
    assert 'tools' not in fake.requests[0]
    assert events[-1]['type'] == 'done'


# ---------------------------------------------------------------- CLI and TUI
def test_ask_refuses_think_when_the_provider_streams_no_reasoning(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kova import cli
    app_for(tmp_path, monkeypatch, install=('openai', ['gpt-5']))
    result = CliRunner().invoke(cli.app, ['ask', 'q', '--think'])
    assert result.exit_code == 1
    assert 'reasoning' in result.stderr and '--think' in result.stderr


def test_ask_reaches_the_server_for_a_reasoning_provider(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kova import cli
    app_for(tmp_path, monkeypatch, install=('deepseek', ['deepseek-reasoner']))

    def stop():
        raise cli.KovaError(None, 'reached the server')

    monkeypatch.setattr(cli, 'get_client', stop)
    result = CliRunner().invoke(cli.app, ['ask', 'q', '--think'])
    assert result.exit_code == 1 and 'reached the server' in result.stderr


def test_providers_list_shows_declared_capabilities(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kova import cli
    from kova.client import KovaClient
    client, _, cfg = app_for(tmp_path, monkeypatch, install=('deepseek', ['deepseek-reasoner']))
    with client:
        kova = KovaClient('http://testserver', config.ensure_token(cfg.home), http=client)
        monkeypatch.setattr(cli, 'get_client', lambda: kova)
        result = CliRunner().invoke(cli.app, ['providers', 'list'])
        assert result.exit_code == 0
        assert 'deepseek' in result.output and 'reasoning' in result.output


class _StubClient:
    """Just enough of KovaClient to hand the chat a contract."""

    def __init__(self, contract=None, error=None):
        self.contract, self.error = contract, error

    def capabilities(self):
        if self.error is not None:
            raise self.error
        return self.contract


def chat_app(client):
    from kova.tui.app import ChatApp
    return ChatApp(client, 'c1', out=io.StringIO(), color=False, clear=False)


def test_the_tui_withholds_commands_the_contract_does_not_offer():
    from kova.tui.app import COMMANDS
    app = chat_app(_StubClient({'provider': 'openai', 'capabilities': ['api_key', 'tools'],
                                'offered': {'chat': ['api_key'], 'work': ['api_key', 'approvals', 'tools']}}))
    app._refresh_capabilities()
    app.mode = 'work'
    assert 'think' not in app._commands()            # this provider streams no reasoning
    assert 'reasoning' in app._unavailable('think')
    assert {'permissions', 'cd'} <= set(app._commands())
    app.mode = 'chat'
    assert 'permissions' not in app._commands()      # chat mode never asks for approval
    assert 'cd' not in app._commands()               # ... and cannot use tools
    assert 'think' not in app._commands()
    assert set(COMMANDS) - {'think', 'permissions', 'cd'} <= set(app._commands())


def test_the_tui_reads_the_live_contract(tmp_path, monkeypatch):
    """The terminal chat against the real endpoint, not a stubbed payload."""
    from kova.client import KovaClient
    client, _, cfg = app_for(tmp_path, monkeypatch, install=('openai', ['gpt-5']))
    with client:
        app = chat_app(KovaClient('http://testserver', config.ensure_token(cfg.home), http=client))
        app._refresh_capabilities()
        app.mode = 'work'
        assert app.offers('tools') and app.offers('approvals')
        assert not app.offers('reasoning')  # this provider streams no reasoning
        app.mode = 'chat'
        assert not app.offers('tools') and not app.offers('approvals')


def test_the_tui_keeps_everything_when_the_server_declares_nothing():
    from kova.tui.app import COMMANDS
    app = chat_app(_StubClient(error=RuntimeError('an older server')))
    app._refresh_capabilities()
    assert app.offered is None
    assert set(app._commands()) == set(COMMANDS)
    assert app.offers('reasoning')
