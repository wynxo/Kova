"""Offline privacy ledger and hard outbound policy regressions."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kova.config import ensure_token, load_config, save_config
from kova.models import ModelClient, ModelError
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    return load_config()


def test_local_only_round_trip_and_unknown_setting(cfg):
    assert cfg.privacy_local_only is False
    cfg.privacy_local_only = True
    save_config(cfg)
    assert load_config(cfg.home).privacy_local_only is True
    from kova.config import unknown_config_keys
    assert unknown_config_keys({'privacy': {'local_only': True}}) == []


@pytest.mark.asyncio
async def test_cloud_blocked_before_credentials_or_transport(cfg, monkeypatch):
    cfg.privacy_local_only = True
    def forbidden(*args, **kwargs):
        pytest.fail('Local only must refuse before credentials, DNS or HTTP')
    monkeypatch.setattr(ModelClient, '_headers', forbidden)
    with pytest.raises(ModelError, match='Local only'):
        _ = [e async for e in ModelClient(cfg.active_model(), config=cfg,
             transport=httpx.MockTransport(forbidden)).stream([{'role': 'user', 'content': 'hi'}])]
    assert await ModelClient(cfg.active_model(), config=cfg).ping() is False


def test_http_policy_checks_redirect_hops_and_records_only_metadata(cfg):
    from kova import egress
    store = Store(cfg.home / 'ledger.db')
    cfg.privacy_local_only = True
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://cloud.example/path?key=secret'})
    with egress.scope(config=cfg, store=store, kind='probe'):
        with egress.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            with pytest.raises(egress.LocalOnlyError, match='Local only'):
                client.get('http://127.0.0.1/start')
    assert calls == ['http://127.0.0.1/start']
    report = store.egress_report()
    assert len(report['items']) == 1
    assert report['items'][0]['local'] is True
    assert 'secret' not in json.dumps(report)


def test_auth_and_search_block_before_network(cfg, monkeypatch):
    from kova import egress, provider_auth
    from kova.tools import web
    cfg.privacy_local_only = True
    def forbidden(*args, **kwargs):
        pytest.fail('No request or DNS is allowed')
    monkeypatch.setattr(web, '_resolve', forbidden)
    with egress.scope(config=cfg):
        with pytest.raises(egress.LocalOnlyError):
            provider_auth._exchange({'refresh_token': 'not-a-real-token'}, httpx.MockTransport(forbidden))
        with pytest.raises(egress.LocalOnlyError):
            web.search('test')
        with pytest.raises(egress.LocalOnlyError):
            web._fetch({'url': 'https://example.com'})


def test_privacy_api_auth_toggle_block_and_restore(cfg):
    fake = FakeModel([text('Hello')])
    app = create_app(cfg, transport=fake.transport)
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(app) as client:
        assert client.get('/privacy').status_code == 401
        assert client.patch('/privacy', json={'local_only': True}).status_code == 401
        assert client.get('/privacy?days=0', headers=headers).status_code == 422
        assert client.patch('/privacy', json={'local_only': True}, headers=headers).json()['local_only'] is True
        assert load_config(cfg.home).privacy_local_only is True
        chat = client.post('/chats', headers=headers).json()['id']
        blocked = client.post(f'/chats/{chat}/messages', headers=headers, json={'text': 'hi'})
        assert blocked.status_code == 409
        assert 'Local only' in blocked.json()['detail']
        assert client.post('/providers/auth/start', headers=headers).status_code == 409
        assert client.get('/health', headers=headers).json()['local_only'] is True
        client.patch('/privacy', json={'local_only': False}, headers=headers)
        response = client.post(f'/chats/{chat}/messages', headers=headers, json={'text': 'hi'})
        assert response.status_code == 200
        assert 'event: egress' in response.text
        report = client.get('/privacy', headers=headers).json()
        rows = [row for row in report['items'] if row['kind'] == 'model']
        assert len(rows) == 1
        row = rows[0]
        assert row['chat_id'] == chat and row['host'] == 'model.test'
        assert row['bytes_out'] > 0 and row['bytes_in'] > 0
        assert 'Hello' not in json.dumps(report)
        assert len(client.get('/privacy?chat_id=missing', headers=headers).json()['items']) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('url,local', [('http://127.0.0.1/v1', True), ('https://cloud.example/v1', False)])
async def test_model_metadata_and_usage(cfg, url, local):
    from kova import egress
    store = Store(cfg.home / 'ledger.db')
    entry = cfg.active_model()
    entry.base_url = url
    frame = {'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 17, 'completion_tokens': 3}}
    wire = 'data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n'
    with egress.scope(config=cfg, store=store, chat_id='chat-demo'):
        events = [e async for e in ModelClient(entry, config=cfg,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=wire))).stream(
            [{'role': 'user', 'content': 'private message'}])]
    assert events[-1].type == 'done'
    row = store.egress_report()['items'][0]
    assert row['local'] is local
    assert row['tokens_in'] == 17 and row['tokens_out'] == 3
    assert row['model'] == 'test-model'
    assert 'private message' not in json.dumps(row) and 'answer' not in json.dumps(row)


def test_incognito_ledger_never_written_to_disk(cfg):
    from kova import egress
    store = Store(cfg.home / 'ledger.db')
    with egress.scope(config=cfg, store=store, chat_id='incognito-demo'):
        with egress.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='ok'))) as client:
            client.get('https://example.com')
    assert store.egress_report()['items'] == []
    assert Store(cfg.home / 'ledger.db').egress_report()['items'] == []


def test_search_uses_audited_destination_and_does_not_log_query(cfg, monkeypatch):
    from kova import egress
    from kova.tools import web
    store = Store(cfg.home / 'ledger.db')
    markup = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">Example <b>page</b></a><a class="result__snippet">A useful snippet.</a>'
    monkeypatch.setattr(web, '_transport', httpx.MockTransport(lambda r: httpx.Response(200, text=markup)))
    with egress.scope(config=cfg, store=store, chat_id='chat-demo'):
        result = web.search('private query')
    assert result == [{'title': 'Example page', 'url': 'https://example.com', 'snippet': 'A useful snippet.'}]
    row = store.egress_report()['items'][0]
    assert row['kind'] == 'search' and row['host'] == 'html.duckduckgo.com'
    assert 'private query' not in json.dumps(row)


def test_privacy_cli_json_and_local_only_switch(cfg, monkeypatch):
    from kova import cli
    from kova.client import KovaClient
    from typer.testing import CliRunner
    fake = FakeModel([text('Hello')])
    app = create_app(cfg, transport=fake.transport)
    with TestClient(app) as client:
        api = KovaClient('http://testserver', ensure_token(cfg.home), http=client)
        monkeypatch.setattr(cli, 'get_client', lambda: api)
        result = CliRunner().invoke(cli.app, ['privacy', '--local-only', 'on', '--json'])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)['local_only'] is True
        assert load_config(cfg.home).privacy_local_only is True
        result = CliRunner().invoke(cli.app, ['privacy', '--days', '0'])
        assert result.exit_code != 0


def test_local_only_excludes_unauditable_tools():
    from kova.agent import select_tools
    from kova.tools import Tool
    tools = {name: Tool(name, '', {}, lambda args: '') for name in
             ('read_file', 'web_search', 'web_fetch', 'run_command', 'plugin__send')}
    assert set(select_tools('work', True, tools, local_only=True)) == {'read_file'}


@pytest.mark.asyncio
async def test_stream_cancellation_records_partial_bytes_without_content(cfg):
    import asyncio
    from kova import egress
    store = Store(cfg.home / 'ledger.db')
    started = asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"private answer"}}]}\n\n'
            started.set()
            await asyncio.Event().wait()
    async def consume():
        with egress.scope(config=cfg, store=store, chat_id='chat-demo'):
            async for _ in ModelClient(cfg.active_model(), config=cfg,
                transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))).stream(
                [{'role': 'user', 'content': 'private request'}]):
                pass
    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = store.egress_report()['items']
    assert len(rows) == 1 and rows[0]['status'] == 'partial'
    assert rows[0]['bytes_in'] > 0 and rows[0]['tokens_in'] is None
    assert 'private' not in json.dumps(rows)


def test_report_totals_cover_bounded_timeline_and_survive_restart(cfg):
    from kova import egress
    store = Store(cfg.home / 'ledger.db')
    with egress.scope(config=cfg, store=store, chat_id='chat-demo', kind='probe'):
        with egress.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'ok'))) as client:
            for _ in range(1002):
                client.post('https://example.com/secret?key=not-logged', content=b'hello')
    report = Store(cfg.home / 'ledger.db').egress_report()
    assert len(report['items']) == 1000 and report['truncated'] is True
    assert report['requests'] == 1002 and report['bytes_out'] == 5010 and report['bytes_in'] == 2004
    assert report['groups'][0]['requests'] == 1002
    assert report['groups'][0]['tokens_in'] is None
    assert 'not-logged' not in json.dumps(report)


def test_local_only_thread_pool_probe_and_scheduled_command_refused(cfg):
    import asyncio
    from kova import egress, server_finder
    from kova.tools.commands import run_command
    cfg.privacy_local_only = True
    def forbidden(request):
        pytest.fail('A worker thread lost the outbound policy')
    with egress.scope(config=cfg):
        assert server_finder.connect('http://127.0.0.1:8080', transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={'data': [{'id': 'demo'}]}))).usable
        with pytest.raises(egress.LocalOnlyError):
            server_finder.connect('https://cloud.example/v1', transport=httpx.MockTransport(forbidden))
        with pytest.raises(egress.LocalOnlyError):
            asyncio.run(run_command('echo must-not-run', cwd=cfg.home))


def test_mcp_can_be_explicitly_reconnected_after_local_only(cfg):
    from tests.test_mcp import stdio
    cfg.mcp = [stdio(enabled=False)]
    app = create_app(cfg, transport=FakeModel([]).transport)
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(app) as client:
        assert client.patch('/privacy', headers=headers, json={'local_only': True}).status_code == 200
        assert client.post('/mcp/reload', headers=headers).status_code == 409
        assert client.patch('/privacy', headers=headers, json={'local_only': False}).status_code == 200
        result = client.patch('/mcp/fake', headers=headers, json={'enabled': True})
        assert result.status_code == 200 and result.json()['state'] == 'connected'
        assert client.post('/mcp/reload', headers=headers).status_code == 200


def test_inflight_refresh_cannot_undo_persisted_local_only(cfg, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from kova import provider_auth, server
    p = cfg.providers[0]
    p.auth = {'access_token': 'expired-demo', 'refresh_token': 'refresh-demo', 'expires_at': 0, 'client_id': 'demo'}
    save_config(cfg)
    refreshing, release, toggle_saved = threading.Event(), threading.Event(), threading.Event()
    def exchange(*args, **kwargs):
        refreshing.set()
        assert release.wait(5)
        return {'access_token': 'renewed-demo', 'expires_at': 9999999999}
    original_save = server.save_config
    def observed_save(config):
        original_save(config)
        toggle_saved.set()
    monkeypatch.setattr(provider_auth, '_exchange', exchange)
    monkeypatch.setattr(server, 'save_config', observed_save)
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(create_app(cfg, transport=FakeModel([]).transport)) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            renew = pool.submit(provider_auth.credentials, p)
            assert refreshing.wait(2)
            toggle = pool.submit(client.patch, '/privacy', headers=headers, json={'local_only': True})
            # A correct toggle waits for the refresh's write lock, so release
            # it after giving an unlocked writer the chance to save first.
            toggle_saved.wait(.5)
            release.set()
            assert renew.result(timeout=5)['access_token'] == 'renewed-demo'
            assert toggle.result(timeout=5).status_code == 200
    assert load_config(cfg.home).privacy_local_only is True
    assert load_config(cfg.home).providers[0].auth['access_token'] == 'renewed-demo'


def test_local_only_mcp_http_close_skips_remote_session_delete(cfg):
    from kova import egress, mcp
    from kova.config import McpServerEntry
    cfg.privacy_local_only = True
    def forbidden(request):
        pytest.fail('Closing a remote MCP session must not make a network request')
    with egress.scope(config=cfg):
        link = mcp._Http(McpServerEntry(name='demo', url='https://example.com/mcp'), lambda *a: None,
                         transport=httpx.MockTransport(forbidden))
        link.session = 'demo-session'
        link.close()
        assert link.http.is_closed


def test_local_only_rejects_download_api_before_streaming(cfg):
    cfg.privacy_local_only = True
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(create_app(cfg, transport=FakeModel([]).transport)) as client:
        for backend in ('ollama', 'managed'):
            response = client.post('/local-models/install', headers=headers,
                                   json={'model': 'qwen3:4b', 'backend': backend})
            assert response.status_code == 409
            assert 'Local only' in response.json()['detail']


def test_local_only_rejects_self_update_before_external_installer(cfg, monkeypatch):
    from kova import cli, lifecycle
    from typer.testing import CliRunner
    cfg.privacy_local_only = True
    save_config(cfg)
    monkeypatch.setattr(lifecycle, 'find_uv', lambda: 'demo-uv')
    def forbidden(*args, **kwargs):
        pytest.fail('Local only must not invoke a package downloader')
    monkeypatch.setattr(lifecycle.subprocess, 'run', forbidden)
    result = CliRunner().invoke(cli.app, ['update'])
    assert result.exit_code == 1
    assert 'Local only' in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter', ['anthropic', 'responses'])
async def test_native_adapters_share_ledger_and_report_usage(cfg, adapter):
    from kova import egress
    cfg.providers[0].adapter = adapter
    frames = ([{'type': 'message_start', 'message': {'usage': {'input_tokens': 7, 'output_tokens': 0}}},
               {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'private answer'}},
               {'type': 'message_delta', 'usage': {'output_tokens': 2}}, {'type': 'message_stop'}]
              if adapter == 'anthropic' else
              [{'type': 'response.output_text.delta', 'delta': 'private answer'},
               {'type': 'response.completed', 'response': {'usage': {'input_tokens': 7, 'output_tokens': 2}}}])
    wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
    store = Store(cfg.home / 'ledger.db')
    with egress.scope(config=cfg, store=store):
        events = [e async for e in ModelClient(cfg.active_model(), config=cfg,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=wire))).stream(
            [{'role': 'user', 'content': 'private question'}])]
    assert events[-1].type == 'done'
    rows = store.egress_report()['items']
    assert len(rows) == 1
    assert rows[0]['tokens_in'] == 7 and rows[0]['tokens_out'] == 2
    assert 'private' not in json.dumps(rows)


def test_mcp_refusal_does_not_save_a_server_while_local_only(cfg):
    cfg.privacy_local_only = True
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(create_app(cfg, transport=FakeModel([]).transport)) as client:
        response = client.post('/mcp', headers=headers,
                               json={'name': 'demo', 'url': 'https://example.com/mcp'})
        assert response.status_code == 409
        assert cfg.mcp == [] and load_config(cfg.home).mcp == []
