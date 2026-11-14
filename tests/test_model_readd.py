import httpx
from fastapi.testclient import TestClient
from kova.config import load_config, ensure_token
from kova.server import create_app


def test_readd_preserves_key_and_provider(tmp_path):
    cfg = load_config(tmp_path)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'data': []})) )) as client:
        headers = {'Authorization': f'Bearer {ensure_token(tmp_path)}'}
        body = {'name': 'cloud', 'base_url': 'https://cloud.test/v1', 'model': 'm', 'api_key': 'test-secret'}
        first = client.post('/models', json=body, headers=headers)
        assert first.status_code == 200
        provider = cfg.models[-1].provider
        count = len(cfg.providers)
        del body['api_key']
        body['model'] = 'updated'
        assert client.post('/models', json=body, headers=headers).status_code == 200
        assert cfg.models[-1].api_key == 'test-secret'
        assert cfg.models[-1].provider == provider
        assert len(cfg.providers) == count
        body['api_key'] = 'replacement'
        assert client.post('/models', json=body, headers=headers).status_code == 200
        assert cfg.models[-1].api_key == 'replacement'
        assert cfg.models[-1].provider == provider
        assert len(cfg.providers) == count
        persisted = load_config(tmp_path)
        assert persisted.models[-1].api_key == 'replacement'
        assert persisted.models[-1].provider == provider
        # A new endpoint must not inherit the old endpoint's secret.
        body.pop('api_key')
        body['base_url'] = 'https://different.test/v1'
        assert client.post('/models', json=body, headers=headers).status_code == 200
        assert cfg.models[-1].api_key is None
        assert cfg.models[-1].provider != provider
