import httpx
import pytest

from kova.config import ModelEntry
from kova.models import ModelClient, ModelError
from kova.providers import ProviderError, list_models, make_provider

pytestmark = pytest.mark.real_default


@pytest.mark.asyncio
async def test_empty_stream_is_actionable():
    model = ModelClient(ModelEntry('test', 'http://fake/v1', 'test'),
                        transport=httpx.MockTransport(lambda r: httpx.Response(200, text='data: [DONE]\n\n')))
    with pytest.raises(ModelError, match='empty|no reply'):
        _ = [event async for event in model.stream([])]


@pytest.mark.asyncio
async def test_unconfigured_model_rejected_before_network():
    model = ModelClient(ModelEntry('test', 'http://fake/v1', ''))
    with pytest.raises(ModelError, match='kova setup'):
        _ = [event async for event in model.stream([])]


def test_invalid_catalog_scalar_is_actionable():
    with pytest.raises(ProviderError, match='catalog|models'):
        list_models(make_provider('openai', api_key='fake'),
                    transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))


@pytest.mark.asyncio
async def test_tool_rejection_retries_without_tools():
    requests = []
    def respond(request):
        import json
        body = json.loads(request.content)
        requests.append(body)
        if 'tools' in body:
            return httpx.Response(400, json={'error': {'message': 'tools are not supported'}})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n')
    model = ModelClient(ModelEntry('test', 'http://fake/v1', 'test'), transport=httpx.MockTransport(respond))
    events = [event async for event in model.stream([], tools=[{'type': 'function'}])]
    assert any(event.text == 'hello' for event in events)
    assert len(requests) == 2 and 'tools' not in requests[-1]


def test_doctor_clean_home(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kova import cli
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    from kova.config import load_config, save_config
    cfg = load_config()
    cfg.port = 7893
    save_config(cfg)
    result = CliRunner().invoke(cli.app, ['doctor'])
    assert result.exit_code == 1
    assert 'kova setup' in result.output
    assert 'Traceback' not in result.output


def test_local_detection_offers_actual_models(tmp_path, monkeypatch):
    from kova import onboarding
    from kova.config import load_config
    monkeypatch.setattr(onboarding, '_running_client', lambda cfg: None)
    answers = iter(['', '', '', '1', '1'])
    monkeypatch.setattr('builtins.input', lambda: next(answers))
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'data': [{'id': 'real-local'}]}))
    onboarding.run_setup(load_config(tmp_path), provider_kind='local', transport=transport)
    assert load_config(tmp_path).active_model().model == 'real-local'


def test_api_key_retry(tmp_path, monkeypatch):
    from kova import onboarding
    from kova.config import load_config
    monkeypatch.setattr(onboarding, '_running_client', lambda cfg: None)
    keys = iter(['bad', 'good'])
    monkeypatch.setattr(onboarding, '_secret', lambda p: next(keys))
    answers = iter(['', 'y', '1', '1'])
    monkeypatch.setattr('builtins.input', lambda: next(answers))
    def respond(request):
        return httpx.Response(401) if request.headers.get('authorization') == 'Bearer bad' else httpx.Response(200, json={'data': [{'id': 'real'}]})
    onboarding.run_setup(load_config(tmp_path), provider_kind='openai', transport=httpx.MockTransport(respond))
    assert load_config(tmp_path).active_model().model == 'real'


def test_health_rejects_non_kova_service():
    from kova.client import KovaClient, KovaError
    with httpx.Client(base_url='http://fake', transport=httpx.MockTransport(lambda r: httpx.Response(200, text='other service'))) as http, pytest.raises(KovaError, match='port'):
        KovaClient('http://fake', 'fake', http).health()


def test_login_no_browser_fails_immediately(monkeypatch):
    from kova import provider_auth
    class Attempt:
        url = 'http://fake'
        cancelled = False
        def cancel(self):
            self.cancelled = True
    attempt = Attempt()
    monkeypatch.setattr(provider_auth, 'LoginAttempt', lambda p: attempt)
    monkeypatch.setattr(provider_auth.webbrowser, 'open', lambda url: False)
    with pytest.raises(ProviderError, match='browser'):
        provider_auth.login(make_provider('chatgpt'))
    assert attempt.cancelled


@pytest.mark.asyncio
async def test_timeout_is_actionable():
    def respond(request):
        raise httpx.ReadTimeout('fake', request=request)
    model = ModelClient(ModelEntry('test', 'http://fake/v1', 'test'), transport=httpx.MockTransport(respond))
    with pytest.raises(ModelError, match='timed out'):
        _ = [event async for event in model.stream([])]


def test_invalid_config_cli_no_traceback(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kova import cli
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    (tmp_path / 'config.toml').write_text('invalid = [')
    result = CliRunner().invoke(cli.app, ['status'])
    assert result.exit_code == 1 and 'config.toml' in result.output


def test_garbled_kova_stream_no_traceback():
    from kova.client import KovaClient, KovaError
    with httpx.Client(base_url='http://fake', transport=httpx.MockTransport(lambda r: httpx.Response(200, text='data: broken\n\n'))) as http, pytest.raises(KovaError, match='stream'):
        list(KovaClient('http://fake', 'fake', http).stream_message('chat', 'hello'))


@pytest.mark.asyncio
async def test_native_empty_reply_is_actionable():
    from kova.model_adapters import stream_native
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='data: {"type":"message_stop"}\n\n'))) as client:
        with pytest.raises(ModelError, match='empty'):
            _ = [e async for e in stream_native(client, 'http://fake', {}, 'anthropic')]


@pytest.mark.asyncio
async def test_provider_error_frame_is_not_silent():
    model = ModelClient(ModelEntry('test', 'http://fake/v1', 'test'), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text='data: {"error":{"message":"secret upstream text"}}\n\n')))
    with pytest.raises(ModelError, match='Provider') as caught:
        _ = [e async for e in model.stream([])]
    assert 'secret' not in str(caught.value)


def test_stale_pid_does_not_block_startup(tmp_path, monkeypatch):
    from kova import client
    from kova.config import load_config
    cfg = load_config(tmp_path)
    cfg.port = 7894
    (tmp_path / 'server.pid').write_text('99999999')
    class FakeClient:
        calls = 0
        def health(self):
            self.calls += 1
            if self.calls == 1:
                raise client.KovaError(None, 'offline')
            return {'version': 'test', 'model_ok': False}
    fake = FakeClient()
    spawned = []
    monkeypatch.setattr(client, 'KovaClient', lambda *a: fake)
    monkeypatch.setattr(client, '_spawn_server', lambda cfg: spawned.append(cfg.home))
    assert client.ensure_server(cfg) is fake
    assert spawned == [tmp_path]


def test_whitespace_model_needs_setup(tmp_path):
    from kova.config import load_config
    from kova.onboarding import needs_setup
    cfg = load_config(tmp_path)
    cfg.models[0].model = '   '
    assert needs_setup(cfg)


def test_interrupted_kova_stream_is_actionable():
    from kova.client import KovaClient, KovaError
    with httpx.Client(base_url='http://fake', transport=httpx.MockTransport(lambda r: httpx.Response(200, text=''))) as http, pytest.raises(KovaError, match='ended'):
        list(KovaClient('http://fake', 'fake', http).stream_message('chat', 'hello'))
