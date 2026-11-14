"""PWA HTTP contract, share isolation and bounded, single-use drafts."""
import time

from fastapi.testclient import TestClient
from kova.config import load_config
from kova.server import create_app


def test_manifest_and_shares(tmp_path):
    config = load_config(tmp_path)
    config.persona_name = 'Nova'
    app = create_app(config)
    with TestClient(app, base_url='http://localhost') as client:
        manifest = client.get('/app/manifest.webmanifest')
        assert manifest.headers['content-type'] == 'application/manifest+json'
        assert manifest.headers['cache-control'] == 'no-store'
        assert manifest.json()['name'] == 'Nova'
        assert manifest.json()['share_target']['method'] == 'POST'
        assert client.get('/app/sw.js').headers['cache-control'] == 'no-store'
        assert client.post('/app/share', data={'text': 'hello'}).status_code == 401
        app.state.web_sessions['demo'] = time.monotonic() + 60
        client.cookies.set('kova_session', 'demo')
        response = client.post('/app/share', data={'title': 'Title', 'text': 'hello', 'url': 'https://example.com'},
                               files={'files': ('note.txt', b'content', 'text/plain')}, follow_redirects=False)
        assert response.status_code == 303
        token = response.headers['location'].split('#share=')[1]
        app.state.web_sessions['other'] = time.monotonic() + 60
        client.cookies.set('kova_session', 'other')
        assert client.post('/app/share/' + token).status_code == 404
        client.cookies.set('kova_session', 'demo')
        draft = client.post('/app/share/' + token).json()['text']
        assert 'Title\n\nhello\n\nhttps://example.com' in draft
        assert 'note.txt' in draft and 'Y29udGVudA==' in draft
        assert client.post('/app/share/' + token).status_code == 404
        assert client.post('/app/share', content=b'x' * (2 * 1024 * 1024 + 1)).status_code == 413
        assert client.post('/app/share', data={'text': 'bad'}, headers={'Origin': 'https://example.com'}).status_code == 403
