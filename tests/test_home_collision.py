import httpx
import pytest
from fastapi.testclient import TestClient
from kova.client import KovaClient, KovaError, ensure_server
from kova.config import load_config, ensure_token
from kova.server import create_app


def test_other_home_is_rejected_before_client_reuse(tmp_path, monkeypatch):
    from kova import client as module
    owner = load_config(tmp_path / 'owner')
    other = load_config(tmp_path / 'other')
    with TestClient(create_app(owner, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': []})) )) as http:
        client = KovaClient('http://127.0.0.1:7700', ensure_token(other.home), http=http)
        monkeypatch.setattr(module, 'KovaClient', lambda *a: client)
        monkeypatch.setattr(module, '_spawn_server', lambda cfg: pytest.fail('must not spawn over existing server'))
        with pytest.raises(KovaError, match='KOVA_HOME') as exc:
            ensure_server(other)
        assert 'missing or invalid token' not in str(exc.value)
        assert '7700' in str(exc.value)
        # The first home remains available; no state is changed.
        assert http.get('/models', headers={'Authorization': f'Bearer {ensure_token(owner.home)}'}).status_code == 200


@pytest.mark.parametrize('command', ['status', 'stop'])
def test_cli_explains_home_collision(tmp_path, monkeypatch, command):
    from typer.testing import CliRunner
    from kova import cli
    owner = load_config(tmp_path / 'owner')
    other = load_config(tmp_path / 'other')
    with TestClient(create_app(owner, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': []})) )) as http:
        client = KovaClient('http://127.0.0.1:7700', ensure_token(other.home), http=http)
        monkeypatch.setattr(cli, 'load_config', lambda: other)
        monkeypatch.setattr(cli, 'KovaClient', lambda *a: client)
        result = CliRunner().invoke(cli.app, [command])
        assert result.exit_code == 1
        assert 'KOVA_HOME' in result.output
        assert '7700' in result.output
        assert 'missing or invalid token' not in result.output
