"""Desktop startup keeps the token local and recovers from missing GUI backends."""

import sys
from types import SimpleNamespace

import pytest


def test_browser_launcher_uses_one_time_fragment_and_closes_client(monkeypatch):
    from kova import app as desktop
    calls = []
    class Client:
        base_url = 'http://127.0.0.1:7700'
        http = SimpleNamespace(close=lambda: calls.append('closed'))
        def app_ticket(self):
            return 'one-use-ticket'
    monkeypatch.setattr(desktop, 'ensure_server', lambda cfg: Client())
    monkeypatch.setattr(desktop, 'load_config', lambda: object())
    monkeypatch.setattr(desktop.webbrowser, 'open', lambda url: calls.append(url) or True)
    desktop.main(['--browser'])
    assert calls == ['http://127.0.0.1:7700/app#ticket=one-use-ticket', 'closed']


@pytest.mark.parametrize('backend', ['missing', 'broken', 'native'])
def test_native_or_fallback_mints_fresh_ticket(monkeypatch, backend):
    from kova import app as desktop
    opened = []
    tickets = iter(['first', 'fresh'])
    client = SimpleNamespace(base_url='http://127.0.0.1:7700',
                             app_ticket=lambda: next(tickets), http=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(desktop, 'ensure_server', lambda cfg: client)
    monkeypatch.setattr(desktop, 'load_config', lambda: object())
    monkeypatch.setattr(desktop.webbrowser, 'open', lambda url: opened.append(url) or True)
    def start(**kwargs):
        if backend == 'broken':
            raise RuntimeError('No GTK or Qt backend')
    fake = SimpleNamespace(create_window=lambda title, url, **kwargs: opened.append(url), start=start)
    monkeypatch.setitem(sys.modules, 'webview', None if backend == 'missing' else fake)
    desktop.main([])
    if backend == 'broken':
        assert opened == ['http://127.0.0.1:7700/app#ticket=first', 'http://127.0.0.1:7700/app#ticket=fresh']
    else:
        assert opened == ['http://127.0.0.1:7700/app#ticket=first']


def test_old_running_server_gives_restart_instruction(monkeypatch, capsys):
    from kova import app as desktop
    from kova.client import KovaError
    def old_server():
        raise KovaError(404, 'Not Found')
    client = SimpleNamespace(base_url='http://127.0.0.1:7700', app_ticket=old_server,
                             http=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(desktop, 'ensure_server', lambda cfg: client)
    monkeypatch.setattr(desktop, 'load_config', lambda: object())
    with pytest.raises(SystemExit) as exc:
        desktop.main(['--browser'])
    assert exc.value.code == 1
    assert 'kova stop' in capsys.readouterr().err
