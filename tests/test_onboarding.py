"""Setup is explicit, catalog-only, repeatable and safe for scripted input."""
import builtins
import json

import httpx
import pytest
from typer.testing import CliRunner

from kova import cli, onboarding
from kova.config import load_config

pytestmark = pytest.mark.real_default


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    monkeypatch.setattr(onboarding, '_running_client', lambda cfg: None)
    return tmp_path


def feed(monkeypatch, *answers):
    values = iter(answers)
    monkeypatch.setattr(builtins, 'input', lambda: next(values))


def catalog(request):
    assert request.method == 'GET', 'Setup must never send an inference request'
    assert request.url.path.endswith('/models')
    return httpx.Response(200, json={'data': [{'id': 'alpha'}, {'id': 'beta'}]})


def test_setup_never_scans_or_chats(home, monkeypatch):
    monkeypatch.setattr(onboarding.server_finder, 'discover', lambda **kw: pytest.fail('network scan'))
    feed(monkeypatch, '11', 'homelab:8080', '', '1,2', '2')
    onboarding.run_setup(load_config(), transport=httpx.MockTransport(catalog))
    cfg = load_config()
    assert len(cfg.models) == 2
    assert cfg.active_model().model == 'beta'
    assert cfg.providers[0].kind == 'local'


def test_quick_setup_only_lists_models(home, monkeypatch):
    monkeypatch.setattr(builtins, 'input', lambda: pytest.fail('unexpected prompt'))
    onboarding.run_setup(load_config(), url='http://homelab:8080', model='alpha', name='home',
                         transport=httpx.MockTransport(catalog))
    cfg = load_config()
    assert cfg.active == 'home'
    assert cfg.active_model().model == 'alpha'
    assert cfg.active_model().provider == 'home'


def test_bad_key_never_saved_even_with_yes(home):
    cfg = load_config()
    before = (home / 'config.toml').read_text()
    with pytest.raises(onboarding.SetupError, match='key|sign in'):
        onboarding.run_setup(cfg, url='https://example.test/v1', api_key='wrong', yes=True,
                             transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert (home / 'config.toml').read_text() == before


def test_api_key_flow_uses_hidden_prompt_and_can_add_second_provider(home, monkeypatch):
    monkeypatch.setattr(onboarding, '_secret', lambda prompt: 'private')
    feed(monkeypatch, '1', '', '1', '1')
    onboarding.run_setup(load_config(), transport=httpx.MockTransport(catalog))
    feed(monkeypatch, '1', 'another', '2', '1')
    onboarding.run_setup(load_config(), transport=httpx.MockTransport(catalog))
    cfg = load_config()
    assert len(cfg.providers) == 2 and len(cfg.models) == 2
    assert cfg.active_model().model == 'beta'


def test_setup_cancel_does_not_save(home, monkeypatch):
    before = (home / 'config.toml').read_text() if (home / 'config.toml').exists() else None
    cfg = load_config()
    before = (home / 'config.toml').read_text()
    feed(monkeypatch, '')
    with pytest.raises(onboarding.SetupError, match='cancel'):
        onboarding.run_setup(cfg, transport=httpx.MockTransport(catalog))
    assert (home / 'config.toml').read_text() == before


def test_setup_filters_long_catalog(home, monkeypatch):
    names = [f'model-{i}' for i in range(100)]
    feed(monkeypatch, '1', '', 'model-99', '1', '1')
    monkeypatch.setattr(onboarding, '_secret', lambda p: 'private')
    onboarding.run_setup(load_config(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'data': [{'id': m} for m in names]})))
    assert load_config().active_model().model == 'model-99'


def test_cli_setup_flags_and_error(home, monkeypatch):
    # Exercise both address probing and model listing without a real homelab.
    # Mocking list_models alone left server_finder.connect on the live network.
    import socket
    requests = []
    def offline_catalog(request):
        requests.append(request)
        return catalog(request)
    run_setup = onboarding.run_setup
    def offline_setup(*args, **kwargs):
        return run_setup(*args, **kwargs, transport=httpx.MockTransport(offline_catalog))
    def no_dns(*args, **kwargs):
        pytest.fail('CLI setup test attempted live DNS')
    monkeypatch.setattr(socket, 'getaddrinfo', no_dns)
    monkeypatch.setattr(onboarding, 'run_setup', offline_setup)
    result = CliRunner().invoke(cli.app, ['setup', '--url', 'http://homelab:8080', '--model', 'alpha', '--name', 'home'])
    assert result.exit_code == 0, result.output
    assert load_config().active == 'home'
    assert requests and all(r.method == 'GET' for r in requests)
    assert all(r.url.host == 'homelab' for r in requests)
    def fail(*a, **kw):
        raise onboarding.SetupError('Provider unavailable.')
    monkeypatch.setattr(onboarding, 'run_setup', fail)
    result = CliRunner().invoke(cli.app, ['setup'])
    assert result.exit_code == 1 and 'Provider unavailable.' in result.output
