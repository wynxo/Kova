"""The kova-app (Electron) and kova-web (browser) launchers, without a real GUI."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def browser(monkeypatch):
    """A fake server; the recorded calls make ticket reuse and client close visible."""
    from kova import app as launcher

    calls = []
    tickets = iter(['first', 'fresh'])

    class Client:
        base_url = 'http://127.0.0.1:7700'
        http = SimpleNamespace(close=lambda: calls.append('closed'))

        def app_ticket(self):
            return next(tickets)

    monkeypatch.setattr(launcher, 'ensure_server', lambda cfg: Client())
    monkeypatch.setattr(launcher, 'load_config', lambda: object())
    monkeypatch.setattr(launcher.webbrowser, 'open', lambda url: calls.append(url) or True)
    return calls


@pytest.fixture
def runs(monkeypatch):
    """Record npm subprocesses instead of running them."""
    from kova import app as launcher

    recorded = []

    def run(command, **kwargs):
        recorded.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: f'/usr/bin/{name}')
    return recorded


def project_with(tmp_path, *, electron: bool) -> Path:
    project = tmp_path / 'desktop'
    binary = project / 'node_modules' / '.bin'
    binary.mkdir(parents=True)
    (project / 'package.json').write_text('{}')
    if electron:
        (binary / 'electron').write_text('')
    return project


def test_web_launcher_uses_one_time_fragment_and_closes_client(browser):
    from kova import app as launcher

    with pytest.raises(SystemExit) as exc:
        launcher.web_main([])
    assert exc.value.code == 0
    assert browser == ['http://127.0.0.1:7700/app#ticket=first', 'closed']


def test_browser_flag_keeps_working_as_the_web_alias(browser):
    from kova import app as launcher

    with pytest.raises(SystemExit) as exc:
        launcher.main(['--browser'])
    assert exc.value.code == 0
    assert browser == ['http://127.0.0.1:7700/app#ticket=first', 'closed']


def test_desktop_dir_prefers_override_then_checkout_then_staged(monkeypatch, tmp_path):
    from kova import app as launcher

    module = tmp_path / 'checkout' / 'src' / 'kova' / 'app.py'
    module.parent.mkdir(parents=True)
    module.write_text('')
    monkeypatch.setattr(launcher, '__file__', str(module))
    monkeypatch.delenv('KOVA_DESKTOP_DIR', raising=False)
    home = tmp_path / 'data'
    assert launcher.desktop_dir(home) is None
    staged = home / 'desktop'
    (staged / 'node_modules').mkdir(parents=True)
    (staged / 'package.json').write_text('{}')
    assert launcher.desktop_dir(home) == staged
    checkout = module.resolve().parents[2] / 'desktop'
    checkout.mkdir()
    (checkout / 'package.json').write_text('{}')
    assert launcher.desktop_dir(home) == checkout
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(staged))
    assert launcher.desktop_dir(home) == staged


def test_desktop_runs_electron_without_reinstalling(monkeypatch, tmp_path, runs):
    from kova import app as launcher

    project = project_with(tmp_path, electron=True)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    monkeypatch.setenv('ELECTRON_RUN_AS_NODE', '1')
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 0
    assert [command for command, _ in runs] == [['/usr/bin/npm', 'start']]
    assert runs[0][1]['cwd'] == project
    assert 'ELECTRON_RUN_AS_NODE' not in runs[0][1]['env']


def test_desktop_installs_missing_electron_dependencies(monkeypatch, tmp_path, runs, capsys):
    from kova import app as launcher

    project = project_with(tmp_path, electron=False)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 0
    assert [(command, kwargs['cwd']) for command, kwargs in runs] == [
        (['/usr/bin/npm', 'ci'], project), (['/usr/bin/npm', 'start'], project),
    ]
    assert str(project) in capsys.readouterr().out


def test_desktop_dependency_failure_is_reported_not_hidden(monkeypatch, tmp_path, browser, capsys):
    from kova import app as launcher

    project = project_with(tmp_path, electron=False)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 1))
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 1
    assert 'npm ci failed' in capsys.readouterr().err
    assert browser == []  # a failed install never silently swaps the client


def test_desktop_without_app_files_asks_for_a_reinstall(monkeypatch, browser, capsys):
    from kova import app as launcher

    monkeypatch.setattr(launcher, 'desktop_dir', lambda home=None: None)
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: f'/usr/bin/{name}')
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert 'desktop app files are missing' in error and 'kova-web' in error
    assert browser == []


def test_desktop_falls_back_to_the_browser_without_node(monkeypatch, tmp_path, browser, capsys):
    from kova import app as launcher

    project = project_with(tmp_path, electron=True)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: None)
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 0
    assert browser == ['http://127.0.0.1:7700/app#ticket=first', 'closed']
    assert 'Node.js' in capsys.readouterr().err


def test_old_running_server_gives_restart_instruction(monkeypatch, capsys):
    from kova import app as launcher
    from kova.client import KovaError

    def old_server():
        raise KovaError(404, 'Not Found')

    client = SimpleNamespace(base_url='http://127.0.0.1:7700', app_ticket=old_server,
                             http=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(launcher, 'ensure_server', lambda cfg: client)
    monkeypatch.setattr(launcher, 'load_config', lambda: object())
    with pytest.raises(SystemExit) as exc:
        launcher.web_main([])
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert 'kova stop' in error and 'kova-web' in error
