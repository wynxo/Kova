import os
"""Provider boundaries: migration, credentials, catalog validation and wire formats."""
import json
import stat

import httpx
import pytest

from kova import config
from kova.models import ModelClient, ModelError


def test_legacy_models_migrate_without_changing_names_or_active(tmp_path):
    (tmp_path / 'config.toml').write_text('''active = "fast"
[[models]]
name = "fast"
base_url = "https://example.test/v1"
model = "a"
api_key = "private"
[[models]]
name = "slow"
base_url = "https://example.test/v1"
model = "b"
api_key = "private"
''')
    cfg = config.load_config(tmp_path)
    assert len(cfg.providers) == 1
    assert cfg.active_model().provider == cfg.models[1].provider
    config.save_config(cfg)
    saved = (tmp_path / 'config.toml').read_text()
    assert saved.count('api_key = "private"') == 1
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / 'config.toml').stat().st_mode) == 0o600
    again = config.load_config(tmp_path)
    assert again.active == 'fast' and again.active_model().api_key == 'private'


def test_catalog_uses_key_and_never_sends_chat():
    from kova.providers import make_provider, list_models, ProviderError
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == '/v1/models'
        return httpx.Response(200 if request.headers.get('authorization') == 'Bearer private' else 401,
                              json={'data': [{'id': 'm'}]})
    transport = httpx.MockTransport(handle)
    assert list_models(make_provider('openai', api_key='private'), transport=transport) == ['m']
    with pytest.raises(ProviderError, match='key|sign in'):
        list_models(make_provider('openai', api_key='wrong'), transport=transport)
    assert len(requests) == 2


@pytest.mark.parametrize('adapter', ['anthropic', 'responses'])
async def test_native_adapters_stream_tools_and_text(adapter):
    from kova.config import ProviderEntry, ModelEntry
    provider = ProviderEntry('p', 'Provider', 'https://example.test/v1', adapter=adapter, api_key='private')
    entry = ModelEntry('m', provider.base_url, 'model', provider='p', _provider=provider)
    def handle(request):
        payload = json.loads(request.content)
        if adapter == 'anthropic':
            assert request.url.path == '/v1/messages'
            assert request.headers['x-api-key'] == 'private'
            assert payload['tools'][0]['input_schema']['type'] == 'object'
            events = [
                {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'call_1','name':'lookup','input':{}}},
                {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':'{"q":"hi"}'}},
                {'type':'content_block_delta','index':1,'delta':{'type':'text_delta','text':'hello'}},
                {'type':'message_stop'},
            ]
        else:
            assert request.url.path == '/v1/responses'
            assert payload['store'] is False and payload['stream'] is True
            assert payload['instructions'] == 'Be helpful'
            events = [
                {'type':'response.output_item.added','output_index':0,'item':{'type':'function_call','call_id':'call_1','name':'lookup','arguments':''}},
                {'type':'response.function_call_arguments.delta','output_index':0,'delta':'{"q":"hi"}'},
                {'type':'response.output_text.delta','delta':'hello'},
                {'type':'response.completed'},
            ]
        return httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
    tools = [{'type':'function','function':{'name':'lookup','description':'Find','parameters':{'type':'object'}}}]
    events = [e async for e in ModelClient(entry, transport=httpx.MockTransport(handle)).stream(
        [{'role':'system','content':'Be helpful'}, {'role':'user','content':'hi'}], tools)]
    assert ''.join(e.text for e in events if e.type == 'text') == 'hello'
    assert [e.tool_call for e in events if e.type == 'tool_call'] == [{'id':'call_1','name':'lookup','arguments':'{"q":"hi"}'}]
    assert events[-1].type == 'done'


def test_harness_reuse_requires_consent_and_does_not_copy_tokens(tmp_path, monkeypatch):
    from kova import provider_auth
    from kova.providers import make_provider, headers_for, ProviderError
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps({'tokens': {'access_token': 'private', 'account_id': 'account'}}))
    monkeypatch.setattr(provider_auth, 'codex_auth_path', lambda: path)
    p = make_provider('codex')
    with pytest.raises(ProviderError, match='consent'):
        provider_auth.reuse_codex(p, consent=False)
    before = path.read_bytes()
    provider_auth.reuse_codex(p, consent=True)
    assert p.auth == {'source': 'codex'}
    assert headers_for(p)['Authorization'] == 'Bearer private'
    assert provider_auth.logout(p)
    assert path.read_bytes() == before
    with pytest.raises(ProviderError, match='signed out'):
        headers_for(p)


def test_refresh_updates_owned_config_atomically(tmp_path):
    from kova.providers import make_provider
    from kova.provider_auth import credentials
    cfg = config.load_config(tmp_path)
    p = make_provider('chatgpt')
    p.auth = {'access_token': 'old', 'refresh_token': 'renew', 'client_id': 'issued', 'expires_at': 0}
    cfg.providers.append(p)
    config.save_config(cfg)
    def handle(request):
        assert request.url.path.endswith('/oauth/token')
        assert b'grant_type=refresh_token' in request.content
        return httpx.Response(200, json={'access_token':'new', 'refresh_token':'rotated', 'expires_in':3600})
    assert credentials(p, transport=httpx.MockTransport(handle))['access_token'] == 'new'
    assert config.load_config(tmp_path).providers[-1].auth['refresh_token'] == 'rotated'


async def test_responses_truncated_stream_is_not_success():
    p = config.ProviderEntry('p', 'Provider', 'https://example.test/v1', adapter='responses')
    entry = config.ModelEntry('m', p.base_url, 'model', provider='p', _provider=p)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n'))
    with pytest.raises(ModelError, match='before.*completed'):
        _ = [e async for e in ModelClient(entry, transport=transport).stream([{'role':'user','content':'hi'}])]


def test_provider_routes_validate_install_remove_and_redact(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from kova import providers
    from kova.server import create_app
    monkeypatch.setattr(providers, 'list_models', lambda *a, **kw: ['alpha', 'beta'])
    cfg = config.load_config(tmp_path)
    app = create_app(cfg)
    headers = {'Authorization': 'Bearer ' + config.ensure_token(tmp_path)}
    body = {'kind': 'custom', 'name': 'Mine', 'id': 'mine', 'base_url': 'https://example.test/v1',
            'api_key': 'private', 'headers': {'X-Private': 'header-secret'}}
    with TestClient(app) as client:
        assert client.get('/providers').status_code == 401
        assert client.post('/providers/validate', json=body, headers=headers).json()['models'] == ['alpha','beta']
        added = client.post('/providers', json={**body, 'models': ['beta']}, headers=headers)
        assert added.status_code == 200, added.text
        listing = client.get('/providers', headers=headers)
        assert 'private' not in listing.text and 'header-secret' not in listing.text
        models = client.get('/models', headers=headers).json()
        assert models[-1]['provider'] == 'mine' and models[-1]['provider_name'] == 'Mine'
        assert cfg.active == 'mine/beta'
        assert client.delete('/providers/mine', headers=headers).status_code == 200
        assert not any(p.id == 'mine' for p in config.load_config(tmp_path).providers)


def test_backup_excludes_all_provider_credentials(tmp_path):
    from kova.backup import _config_bytes
    cfg = config.load_config(tmp_path)
    p = cfg.providers[0]
    p.auth = {'access_token': 'access-private', 'refresh_token': 'refresh-private', 'id_token': 'identity-private'}
    p.headers = {'X-Secret': 'header-private'}
    config.save_config(cfg)
    exported = _config_bytes(cfg).decode()
    for secret in (*p.auth.values(), *p.headers.values()):
        assert secret not in exported


@pytest.mark.real_default
def test_provider_cli_commands_and_grouped_model_list(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from fastapi.testclient import TestClient
    from kova import cli, providers
    from kova.client import KovaClient
    from kova.server import create_app
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = config.load_config(tmp_path)
    p = providers.make_provider('custom', identifier='mine', name='Mine', base_url='https://example.test/v1')
    providers.install_provider(cfg, p, ['alpha'])
    monkeypatch.setattr(providers, 'list_models', lambda *a, **kw: ['alpha'])
    app = create_app(cfg)
    with TestClient(app) as http:
        client = KovaClient('http://testserver', config.ensure_token(tmp_path), http=http)
        monkeypatch.setattr(cli, 'get_client', lambda: client)
        runner = CliRunner()
        result = runner.invoke(cli.app, ['providers'])
        assert result.exit_code == 0 and 'Mine' in result.output
        assert runner.invoke(cli.app, ['providers', 'logout', 'mine']).exit_code == 0
        monkeypatch.setattr(cli.onboarding, '_secret', lambda p: 'new-key')
        assert runner.invoke(cli.app, ['providers', 'login', 'mine']).exit_code == 0
        result = runner.invoke(cli.app, ['model', 'list'])
        assert result.exit_code == 0 and 'Mine' in result.output and 'mine/alpha' in result.output
        assert runner.invoke(cli.app, ['model', 'use', 'mine/alpha']).exit_code == 0
        assert runner.invoke(cli.app, ['providers', 'remove', 'mine']).exit_code == 0
        assert not cfg.models


def test_terminal_model_picker_has_provider_headings():
    from kova.tui.widgets import Picker
    from kova.tui.widgets import Palette
    p = Palette(color=False)
    picker = Picker('Select model', [('alpha', '', 'a'), ('beta', '', 'b')], None,
                    groups={'a': 'Home', 'b': 'Cloud'})
    rendered = '\n'.join(picker.render(70, p))
    assert 'Home' in rendered and 'Cloud' in rendered
    picker.type('beta')
    assert picker.value == 'b'


def test_openrouter_public_catalog_does_not_accept_bad_key():
    from kova.providers import list_models, make_provider, ProviderError
    paths = []
    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/key'):
            return httpx.Response(401)
        return httpx.Response(200, json={'data': [{'id': 'public-model'}]})
    with pytest.raises(ProviderError, match='key|access'):
        list_models(make_provider('openrouter', api_key='wrong'), transport=httpx.MockTransport(handle))
    assert any(path.endswith('/key') for path in paths)


def test_custom_header_values_are_validated_without_echoing_them():
    from kova.providers import make_provider, ProviderError
    with pytest.raises(ProviderError):
        make_provider('custom', base_url='https://example.test/v1', headers={'X-Secret': 'private\r\ninjected'})
    with pytest.raises(ProviderError):
        make_provider('custom', base_url='https://example.test/v1', headers=['not-an-object'])


@pytest.mark.parametrize('kind', ['openai','anthropic','gemini','openrouter','deepseek','groq','mistral','xai'])
def test_each_key_provider_catalog_auth_and_endpoint(kind):
    from kova.providers import make_provider, list_models
    p = make_provider(kind, api_key='private')
    paths = []
    def handle(request):
        paths.append(request.url.path)
        assert request.method == 'GET'
        assert request.headers.get('x-api-key' if kind == 'anthropic' else 'authorization') == ('private' if kind == 'anthropic' else 'Bearer private')
        if request.url.path.endswith('/key'):
            return httpx.Response(200, json={'data': {'label':'test'}})
        return httpx.Response(200, json={'data': [{'id':'alpha'}]})
    assert list_models(p, transport=httpx.MockTransport(handle)) == ['alpha']
    assert paths[-1].endswith('/models')


def test_native_adapters_translate_tool_result_history():
    from kova.model_adapters import anthropic_request, responses_request
    history = [{'role':'system','content':'system'}, {'role':'user','content':'find'},
        {'role':'assistant','content':None,'tool_calls':[{'id':'call_1','function':{'name':'find','arguments':'{"q":"hi"}'}}]},
        {'role':'tool','tool_call_id':'call_1','content':'result'}]
    _, a = anthropic_request('model', history, None)
    assert a['system'] == 'system'
    assert a['messages'][-2]['content'][0]['type'] == 'tool_use'
    assert a['messages'][-1]['content'][0] == {'type':'tool_result','tool_use_id':'call_1','content':'result'}
    _, r = responses_request('model', history, None)
    assert r['input'][-1] == {'type':'function_call_output','call_id':'call_1','output':'result'}


def test_oauth_pkce_state_and_verified_identity(tmp_path, monkeypatch):
    import time
    from urllib.parse import parse_qs, urlsplit
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt
    from kova import provider_auth
    from kova.providers import make_provider, ProviderError
    p = make_provider('chatgpt')
    attempt = provider_auth.LoginAttempt(p)
    try:
        query = parse_qs(urlsplit(attempt.url).query)
        assert query['client_id'] == ['dynamic_agent_client']
        assert query['agent_name_hint'] == ['Kova']
        assert query['code_challenge_method'] == ['S256']
        wrong = httpx.get(attempt.redirect_uri + '?state=wrong&code=bad', trust_env=False)
        assert wrong.status_code == 400 and not attempt.done.is_set()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        public['kid'] = 'test-key'
        token = jwt.encode({'iss':provider_auth.ISSUER,'aud':'issued','exp':time.time()+300,
                            'sub':'user','nonce':attempt.nonce}, key, algorithm='RS256', headers={'kid':'test-key'})
        def exchange(payload, transport=None):
            assert payload['code_verifier'] == attempt.verifier
            assert payload['redirect_uri'] == attempt.redirect_uri
            return {'access_token':'private','id_token':token,'scope':'chatgpt.tokens.use.direct'}
        monkeypatch.setattr(provider_auth, '_exchange', exchange)
        real_client = httpx.Client
        def handle(request):
            if request.url.path.endswith('openid-configuration'):
                return httpx.Response(200, json={'jwks_uri':provider_auth.ISSUER+'/jwks'})
            return httpx.Response(200, json={'keys':[public]})
        monkeypatch.setattr(provider_auth.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handle)))
        attempt.complete({'code':'code','client_id':'issued'})
        assert attempt.result['subject'] == 'user' and attempt.result['client_id'] == 'issued'
        attempt.nonce = 'wrong-nonce'
        with pytest.raises(ProviderError, match='granted'):
            attempt.complete({'code':'code','client_id':'issued'})
        assert not p.auth  # Callback validation does not activate or persist a connection.
    finally:
        attempt.cancel()


@pytest.mark.parametrize('adapter', ['anthropic','responses'])
async def test_native_adapter_http_errors_are_redacted(adapter):
    p = config.ProviderEntry('p', 'Provider', 'https://example.test/v1', adapter=adapter)
    entry = config.ModelEntry('m', p.base_url, 'model', _provider=p)
    with pytest.raises(ModelError) as exc:
        _ = [e async for e in ModelClient(entry, transport=httpx.MockTransport(
            lambda r: httpx.Response(401, text='sensitive-upstream-body'))).stream([{'role':'user','content':'hi'}])]
    assert 'HTTP 401' in str(exc.value) and 'sensitive' not in str(exc.value)


def test_provider_logout_forgets_credential_headers():
    from kova import provider_auth
    from kova.providers import make_provider
    p = make_provider('custom', base_url='https://example.test/v1', headers={'Authorization':'Bearer private'})
    assert provider_auth.logout(p)
    assert not p.headers


def test_preset_cannot_send_borrowed_token_to_custom_host():
    from kova.providers import make_provider, ProviderError
    with pytest.raises(ProviderError):
        make_provider('codex', base_url='https://example.test/v1')


def test_provider_validation_errors_never_echo_credentials(tmp_path):
    from fastapi.testclient import TestClient
    from kova.server import create_app
    app = create_app(config.load_config(tmp_path))
    headers = {'Authorization':'Bearer '+config.ensure_token(tmp_path)}
    with TestClient(app) as client:
        response = client.post('/providers/validate', headers=headers, json={
            'kind':'custom','base_url':'https://example.test/v1','api_key':{'secret':'private'}})
        assert response.status_code == 422 and 'private' not in response.text


def test_local_provider_route_accepts_loose_address(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from kova import providers, server_finder
    from kova.server import create_app
    seen = []
    def connect(address, **kw):
        seen.append(address)
        return server_finder.Found('http://homelab:8080/v1', models=['alpha'])
    monkeypatch.setattr(server_finder, 'connect', connect)
    monkeypatch.setattr(providers, 'list_models', lambda *a, **kw: ['alpha'])
    app = create_app(config.load_config(tmp_path))
    with TestClient(app) as client:
        response = client.post('/providers/validate', headers={'Authorization':'Bearer '+config.ensure_token(tmp_path)},
                               json={'kind':'local','base_url':'homelab:8080'})
        assert response.status_code == 200 and response.json()['models'] == ['alpha']
    assert seen == ['homelab:8080']


async def test_installed_login_health_uses_catalog_query(monkeypatch, tmp_path):
    from kova import provider_auth
    from kova.providers import make_provider
    auth_path = tmp_path / 'auth.json'
    auth_path.write_text(json.dumps({'tokens':{'access_token':'private'}}))
    monkeypatch.setattr(provider_auth, 'codex_auth_path', lambda: auth_path)
    p = make_provider('codex')
    provider_auth.reuse_codex(p, consent=True)
    entry = config.ModelEntry('m', p.base_url, 'model', _provider=p)
    def handle(request):
        return httpx.Response(200 if request.url.params.get('client_version') else 422, json={'models':[]})
    assert await ModelClient(entry, transport=httpx.MockTransport(handle)).ping()
