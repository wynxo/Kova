import httpx

from kova.tools import web


def test_rebinding_cannot_change_connection_target(monkeypatch):
    resolutions = []
    def resolve(host):
        resolutions.append(host)
        return ['93.184.216.34'] if len(resolutions) == 1 else ['127.0.0.1']

    seen = []
    def connect(request):
        seen.append(request)
        return httpx.Response(200, text='safe')

    monkeypatch.setattr(web, '_resolve', resolve)
    monkeypatch.setattr(web, '_transport', httpx.MockTransport(connect))
    assert web._fetch({'url': 'https://example.com:8443/page'}) == 'safe'
    assert seen[0].url.host == '93.184.216.34'
    assert seen[0].headers['host'] == 'example.com:8443'
    assert seen[0].extensions['sni_hostname'] == 'example.com'
    assert resolutions == ['example.com']


def test_redirect_is_resolved_and_pinned_separately(monkeypatch):
    calls = []
    def resolve(host):
        calls.append(host)
        return ['93.184.216.34'] if host == 'first.example' else ['2606:4700:4700::1111']
    seen = []
    def connect(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={'location': 'https://second.example/path'})
        return httpx.Response(200, text='done')
    monkeypatch.setattr(web, '_resolve', resolve)
    monkeypatch.setattr(web, '_transport', httpx.MockTransport(connect))
    assert web._fetch({'url': 'https://first.example'}) == 'done'
    assert calls == ['first.example', 'second.example']
    assert seen[1].url.host == '2606:4700:4700::1111'
    assert seen[1].headers['host'] == 'second.example'
    assert seen[1].extensions['sni_hostname'] == 'second.example'
