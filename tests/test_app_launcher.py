"""The kova app (Electron) and kova web (browser) launchers, without a real GUI."""

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
    """Record subprocesses and stand in for Electron's startup acknowledgement."""
    from kova import app as launcher

    recorded = []

    def run(command, **kwargs):
        recorded.append((command, kwargs))
        if ready := (kwargs.get('env') or {}).get('KOVA_DESKTOP_STARTUP'):
            Path(ready).write_text('ready')
        if 'ci' in command:
            native = launcher._electron_binary(kwargs['cwd'])
            native.parent.mkdir(parents=True, exist_ok=True)
            native.touch()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: f'/usr/bin/{name}')
    return recorded


def project_with(tmp_path, *, electron: bool) -> Path:
    from kova import app as launcher

    project = tmp_path / 'desktop'
    binary = project / 'node_modules' / '.bin'
    binary.mkdir(parents=True)
    (project / 'package.json').write_text('{}')
    if electron:
        (binary / 'electron').write_text('')
        native = launcher._electron_binary(project)
        native.parent.mkdir(parents=True)
        native.touch()
    return project


def test_web_launcher_uses_one_time_fragment_and_closes_client(browser):
    from kova import app as launcher

    with pytest.raises(SystemExit) as exc:
        launcher.web_main([])
    assert exc.value.code == 0
    assert browser == ['http://127.0.0.1:7700/app#ticket=first', 'closed']


def test_web_launcher_says_what_it_opened(browser, capsys):
    from kova import app as launcher

    with pytest.raises(SystemExit) as exc:
        launcher.web_main([])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "Kova: opened http://127.0.0.1:7700/app in your browser." in output
    assert "ticket=" not in output  # the one-use ticket never goes to the terminal


def test_browser_flag_keeps_working_as_the_web_alias(browser):
    from kova import app as launcher

    with pytest.raises(SystemExit) as exc:
        launcher.main(['--browser'])
    assert exc.value.code == 0
    assert browser == ['http://127.0.0.1:7700/app#ticket=first', 'closed']


def test_desktop_dir_prefers_override_then_staged_then_bundle(monkeypatch, tmp_path):
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
    checkout = module.with_name('desktop')
    checkout.mkdir()
    (checkout / 'package.json').write_text('{}')
    assert launcher.desktop_dir(home) == staged
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(checkout))
    assert launcher.desktop_dir(home) == checkout


def test_desktop_runs_electron_without_reinstalling(monkeypatch, tmp_path, runs):
    from kova import app as launcher

    project = project_with(tmp_path, electron=True)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    monkeypatch.setenv('KOVA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('ELECTRON_RUN_AS_NODE', '1')
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: None)
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 0
    assert len(runs) == 1
    assert runs[0][0][:2] == [str(launcher._electron_binary(project)), str(project)]
    assert runs[0][1]['cwd'] == project
    assert 'ELECTRON_RUN_AS_NODE' not in runs[0][1]['env']


def test_desktop_passes_chromium_switches_through_to_electron(monkeypatch, tmp_path, runs):
    from kova import app as launcher

    project = project_with(tmp_path, electron=True)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    monkeypatch.setenv('KOVA_HOME', str(tmp_path / 'data'))
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: None)
    with pytest.raises(SystemExit) as exc:
        launcher.main(['--remote-debugging-port=9333'])
    assert exc.value.code == 0
    assert runs[0][0][-1] == '--remote-debugging-port=9333'


def test_desktop_rejects_stray_positional_arguments(monkeypatch, tmp_path):
    from kova import app as launcher

    with pytest.raises(SystemExit) as exc:
        launcher.main(['oops'])
    assert exc.value.code == 2


def test_desktop_installs_missing_electron_dependencies(monkeypatch, tmp_path, runs, capsys):
    from kova import app as launcher

    project = project_with(tmp_path, electron=False)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    monkeypatch.setenv('KOVA_HOME', str(tmp_path / 'data'))
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 0
    assert [kwargs['cwd'] for _, kwargs in runs] == [project, project]
    assert runs[0][0] == launcher.npm_command('/usr/bin/npm', 'ci', '--include=dev')
    assert runs[1][0][0] == str(launcher._electron_binary(project))
    assert 'preparing Electron' in capsys.readouterr().out


@pytest.mark.parametrize('status', [0, 23, OSError('could not execute')])
def test_desktop_reports_and_logs_an_electron_launch_failure(monkeypatch, tmp_path, capsys, status):
    from kova import app as launcher

    project = project_with(tmp_path, electron=True)
    home = tmp_path / 'data'
    monkeypatch.setenv('KOVA_HOME', str(home))
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: f'/usr/bin/{name}')

    def fail(command, **kwargs):
        if isinstance(status, OSError):
            raise status
        if log := kwargs.get('stdout'):
            log.write('native startup diagnostics\n')
        return subprocess.CompletedProcess(command, status)

    monkeypatch.setattr(launcher.subprocess, 'run', fail)
    assert launcher.launch_desktop() != 0
    error = capsys.readouterr().err
    assert 'Electron' in error and 'desktop.log' in error and 'kova web' in error
    assert ('could not execute' if isinstance(status, OSError) else 'native startup diagnostics') in (
        home / 'desktop.log'
    ).read_text()


def test_desktop_dependency_failure_is_reported_not_hidden(monkeypatch, tmp_path, browser, capsys):
    from kova import app as launcher

    project = project_with(tmp_path, electron=False)
    monkeypatch.setenv('KOVA_DESKTOP_DIR', str(project))
    home = tmp_path / 'data'
    monkeypatch.setenv('KOVA_HOME', str(home))
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 1))
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 1
    assert 'npm ci failed' in capsys.readouterr().err
    assert 'npm ci failed' in (home / 'desktop.log').read_text()
    assert browser == []  # a failed install never silently swaps the client


def test_desktop_without_app_files_asks_for_a_reinstall(monkeypatch, tmp_path, browser, capsys):
    from kova import app as launcher

    monkeypatch.setattr(launcher, 'desktop_dir', lambda home=None: None)
    monkeypatch.setenv('KOVA_HOME', str(tmp_path / 'data'))
    monkeypatch.setattr(launcher.shutil, 'which', lambda name: f'/usr/bin/{name}')
    with pytest.raises(SystemExit) as exc:
        launcher.main([])
    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert 'desktop app files are missing' in error and 'kova web' in error
    assert browser == []


def test_desktop_falls_back_to_the_browser_without_node(monkeypatch, tmp_path, browser, capsys):
    from kova import app as launcher

    project = project_with(tmp_path, electron=False)
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
    assert 'kova stop' in error and 'kova web' in error


def test_refresh_repairs_legacy_stage_and_retries_failed_dependencies(monkeypatch, tmp_path, runs):
    from kova import app as launcher

    source = project_with(tmp_path / 'source', electron=False)
    staged = project_with(tmp_path / 'home', electron=True)
    (source / 'main.js').write_text('current')
    (source / 'package-lock.json').write_text('new lock')
    (staged / 'main.js').write_text('stale')
    (staged / 'package-lock.json').write_text('old lock')
    (staged / 'launch').write_text('keep shortcut')
    (staged.parent / 'chat.db').write_text('keep chats')
    monkeypatch.setattr(launcher, '_bundled_desktop', lambda: source)
    real_run = launcher._run
    monkeypatch.setattr(launcher, '_run', lambda *args: 1)
    with pytest.raises(launcher.DesktopError, match='npm ci failed'):
        launcher.refresh_desktop(staged.parent)
    monkeypatch.setattr(launcher, '_run', real_run)
    launcher.refresh_desktop(staged.parent)
    launcher.refresh_desktop(staged.parent)
    assert len(runs) == 1
    assert (staged / 'main.js').read_text() == 'current'
    assert (staged / 'launch').read_text() == 'keep shortcut'
    assert (staged.parent / 'chat.db').read_text() == 'keep chats'
