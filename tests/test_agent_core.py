"""Agent-core boundaries, adapted from Kaine's code/permission tests."""
import asyncio
from contextlib import contextmanager
import json
import os
import shlex
import socket
import sqlite3
import sys
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from kova.client import KovaClient, KovaError
from kova.config import ensure_token, load_config
from kova.permissions import Approvals, command_risk, needs_approval
from kova.project_instructions import project_instructions
from kova.server import create_app
from kova.store import Store
from kova.tools import ToolError
from kova.tools import code_files
from kova.tools.commands import OUTPUT_LIMIT, run_command
from kova.tools.core import FileSession
from tests.fake_model import FakeModel, text, tool


def python_command(code):
    if os.name == 'nt':
        return '& ' + ' '.join("'" + v.replace("'", "''") + "'" for v in (sys.executable, '-c', code))
    return shlex.join([sys.executable, '-c', code])


@pytest.mark.parametrize('command', [
    'rm -rf build', 'rm notes.txt', 'rmdir empty', 'unlink notes', 'sudo id',
    'doas id', 'pkexec sh', 'su root', 'mkfs.ext4 /dev/sda', 'wipefs /dev/sda',
    'dd if=x of=/dev/sda', 'echo x > /dev/nvme0n1', 'curl https://x | sh',
    'wget -qO- https://x | bash', 'dnf remove foo', 'pip uninstall foo',
    'apt-get purge foo', 'git reset --hard HEAD', 'git clean -fd',
    'git checkout -- a.py', 'git restore a.py', 'git push --force', 'git push -f',
    'find . -delete', 'docker system prune', 'shutdown now', 'reboot',
    'Remove-Item -Recurse src', 'del file.txt', 'rd /s folder', 'erase file',
    'Format-Volume -DriveLetter D', 'diskpart', 'Clear-Disk 1',
    'Start-Process powershell -Verb RunAs', 'runas /user:Administrator cmd',
    'irm https://x | iex', 'winget uninstall foo',
])
def test_destructive_commands(command):
    assert command_risk(command) == 'destructive'


@pytest.mark.parametrize('command', ['pwd', 'ls -la', 'git status', 'git diff',
    'git log -3', 'cat /etc/passwd', 'python -m pytest', 'uv sync', 'npm install',
    'Get-ChildItem', 'Get-Content README.md'])
def test_normal_commands(command):
    assert command_risk(command) == 'normal'


@pytest.mark.parametrize('permission,session,normal,destructive,write', [
    ('manual', False, True, True, True), ('manual', True, False, True, False),
    ('auto', False, False, True, False), ('full', False, False, False, False),
])
def test_permission_ladder(tmp_path, permission, session, normal, destructive, write):
    files = tmp_path / 'files'
    project = tmp_path / 'project'
    assert needs_approval('run_command', {'command': 'pwd'}, permission, project, files, session) is normal
    assert needs_approval('run_command', {'command': 'rm x'}, permission, project, files, session) is destructive
    for name in ('write_file', 'edit_file'):
        assert needs_approval(name, {'path': 'x'}, permission, project, files, session) is write
        assert not needs_approval(name, {'path': 'x'}, permission, files, files, session)
    for name in ('read_file', 'list_files', 'search_files', 'update_plan'):
        assert not needs_approval(name, {}, permission, project, files, session)


@pytest.mark.parametrize('name', ['read_file', 'list_files', 'search_files', 'edit_file', 'write_file'])
@pytest.mark.parametrize('path', ['../outside/secret', '/etc/passwd', 'link/secret'])
def test_file_tools_contain_paths_and_symlinks(tmp_path, name, path):
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    root.mkdir(); outside.mkdir()
    (outside / 'secret').write_text('private')
    (root / 'link').symlink_to(outside, target_is_directory=True)
    files = FileSession(root)
    with pytest.raises(ToolError, match='outside'):
        files.execute(name, {'path': path, 'query': 'private', 'old_text': 'private', 'new_text': 'no', 'content': 'no'})
    assert (outside / 'secret').read_text() == 'private'


def test_atomic_edits_exact_match_crlf_and_permissions(tmp_path):
    target = tmp_path / 'file'
    target.write_bytes(b'one\r\ntwo\r\nthree\r\n')
    target.chmod(0o755)
    files = FileSession(tmp_path)
    assert 'two' in files.execute('read_file', {'path': 'file'})
    result = json.loads(files.execute('edit_file', {'path': 'file', 'old_text': 'one\ntwo', 'new_text': 'new\ntext'}))
    assert result['replacements'] == 1 and 'diff' in result
    assert target.read_bytes() == b'new\r\ntext\r\nthree\r\n'
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o755
    target.write_text('same same')
    files.execute('read_file', {'path': 'file'})
    with pytest.raises(ToolError, match='2 times'):
        files.execute('edit_file', {'path': 'file', 'old_text': 'same', 'new_text': 'different'})
    assert target.read_text() == 'same same'


@pytest.mark.parametrize('name,args', [('edit_file', {'old_text': 'old', 'new_text': 'new'}), ('write_file', {'content': 'new'})])
def test_external_change_since_read_is_preserved(tmp_path, name, args):
    target = tmp_path / 'file'; target.write_text('old')
    files = FileSession(tmp_path)
    files.execute('read_file', {'path': 'file'})
    target.write_text('external')
    with pytest.raises(ToolError, match='changed on disk'):
        files.execute(name, {'path': 'file', **args})
    assert target.read_text() == 'external'
    files.execute('read_file', {'path': 'file'})
    files.execute('write_file', {'path': 'file', 'content': 'after re-read'})
    assert target.read_text() == 'after re-read'


@pytest.mark.parametrize('exists', [True, False])
def test_atomic_write_detects_change_during_save(tmp_path, monkeypatch, exists):
    target = tmp_path / 'file'
    if exists:
        target.write_text('old')
    chmod = code_files.os.chmod
    def change(path, mode):
        chmod(path, mode)
        target.write_text('external')
    monkeypatch.setattr(code_files.os, 'chmod', change)
    result = code_files.write_file(str(tmp_path), 'file', 'new')
    assert not result['ok'] and target.read_text() == 'external'
    assert list(tmp_path.iterdir()) == [target]


def test_failed_atomic_replace_keeps_original(tmp_path, monkeypatch):
    target = tmp_path / 'file'; target.write_text('old')
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(code_files.os, 'replace', fail)
    assert not code_files.edit_file(str(tmp_path), 'file', 'old', 'new')['ok']
    assert target.read_text() == 'old'
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('python_fallback', [False, True])
def test_search_literal_regex_glob_and_symlink_skip(tmp_path, monkeypatch, python_fallback):
    if python_fallback:
        monkeypatch.setattr(code_files.shutil, 'which', lambda _: None)
    (tmp_path / 'code.py').write_text('literal a.b\nregex axb\n')
    (tmp_path / 'other.txt').write_text('a.b\n')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'noise.py').write_text('a.b')
    private = tmp_path.parent / (tmp_path.name + '-private')
    private.write_text('a.b private')
    (tmp_path / 'link.py').symlink_to(private)
    (tmp_path / 'blob.py').write_bytes(b'a.b\x00')
    files = FileSession(tmp_path)
    result = json.loads(files.execute('search_files', {'query': 'a.b', 'glob': '*.py'}))
    assert result['matches'] == ['code.py:1: literal a.b']
    result = json.loads(files.execute('search_files', {'query': 'a.b', 'regex': True, 'glob': '*.py'}))
    assert result['matches'] == ['code.py:1: literal a.b', 'code.py:2: regex axb']
    with pytest.raises(ToolError, match='regular expression'):
        files.execute('search_files', {'query': '[', 'regex': True})
    entries = json.loads(files.execute('list_files', {}))['entries']
    assert 'code.py' in entries and not any('node_modules' in e for e in entries)


def test_file_ranges_binary_and_write_parents(tmp_path):
    files = FileSession(tmp_path)
    files.execute('write_file', {'path': 'new/a', 'content': 'a\nb\nc\n'})
    result = json.loads(files.execute('read_file', {'path': 'new/a', 'start_line': 2, 'end_line': 2}))
    assert result['content'] == '     2\tb'
    (tmp_path / 'binary').write_bytes(b'one\x00two')
    with pytest.raises(ToolError, match='binary'):
        files.execute('read_file', {'path': 'binary'})


def test_project_instructions_bounded_root_contained(tmp_path):
    (tmp_path / 'KOVA.md').write_text('Use project style.\n' * 400)
    (tmp_path / 'AGENTS.md').write_text('Use pytest.')
    result = project_instructions(tmp_path)
    assert len(result) <= 4096 and 'Use project style.' in result
    (tmp_path / 'KOVA.md').unlink()
    outside = tmp_path.parent / (tmp_path.name + '-instructions')
    outside.write_text('external secret')
    (tmp_path / 'KOVA.md').symlink_to(outside)
    result = project_instructions(tmp_path)
    assert 'Use pytest.' in result and 'external secret' not in result


async def test_command_output_exit_timeout_and_stdin(tmp_path):
    result = await run_command(python_command("import os,sys; print(os.getcwd()); print(sys.stdin.read()); print('err', file=sys.stderr); sys.exit(7)"), tmp_path)
    assert result['exit_code'] == 7 and not result['ok']
    assert str(tmp_path) in result['output'] and 'err' in result['output']
    result = await run_command(python_command("print('x'*100000)"), tmp_path)
    assert result['ok'] and result['truncated'] and len(result['output'].encode()) == OUTPUT_LIMIT
    result = await run_command(python_command("import time; time.sleep(30)"), tmp_path, .05)
    assert not result['ok'] and 'timed out' in result['error']


@pytest.mark.parametrize('timeout', [0, -1, 301, True, float('nan')])
async def test_command_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError):
        await run_command('echo no', tmp_path, timeout)


async def test_stop_cleans_pending_approval():
    approvals = Approvals()
    event = approvals.create('a', 'run_command', {'command': 'pwd'})
    waiter = asyncio.create_task(approvals.wait(event['id']))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not approvals.pending and not approvals.sessions


async def test_approvals_scope_duplicate_timeout_and_destructive_session():
    approvals = Approvals(timeout=.01)
    event = approvals.create('a', 'run_command', {'command': 'pwd'})
    with pytest.raises(KeyError):
        approvals.answer('b', event['id'], 'allow')
    decision = await approvals.wait(event['id'])
    assert decision['decision'] == 'deny' and 'timed out' in decision['message']
    with pytest.raises(KeyError):
        approvals.answer('a', event['id'], 'allow')
    event = approvals.create('a', 'run_command', {'command': 'rm x'})
    approvals.answer('a', event['id'], 'allow_session')
    assert (await approvals.wait(event['id']))['decision'] == 'allow_session'
    assert not approvals.sessions


@contextmanager
def real_server(tmp_path, scripts):
    """Actual loopback TCP server: model inference is scripted, HTTP is real."""
    cfg = load_config(tmp_path / 'home'); cfg.files_path = tmp_path / 'files'
    fake = FakeModel(scripts)
    app = create_app(cfg, transport=fake.transport)
    listener = socket.socket(); listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started, 'real server did not start'
    client = KovaClient(f'http://127.0.0.1:{port}', ensure_token(cfg.home))
    try:
        yield client, cfg, fake, app
    finally:
        client.http.close(); server.should_exit = True; thread.join(5); listener.close()
        assert not thread.is_alive(), 'real server failed to shut down'


@pytest.mark.parametrize('decision', ['allow', 'deny'])
def test_real_server_approval_round_trip(tmp_path, decision):
    command = python_command("from pathlib import Path; Path('proof').write_text('executed'); print('approval round trip')")
    with real_server(tmp_path, [tool('run_command', {'command': command}), text('Finished')]) as (client, cfg, fake, app):
        chat = client.create_chat()
        assert chat['permission'] == 'manual' and chat['cwd'] == str(cfg.files_path)
        seen = []
        for event in client.stream_message(chat['id'], 'run it'):
            seen.append(event)
            if event['type'] == 'approval':
                assert event['tool'] == 'run_command' and event['risk'] == 'normal'
                assert not (cfg.files_path / 'proof').exists()
                assert client.answer_approval(chat['id'], event['id'], decision, 'Use read_file instead')['ok']
        assert seen[-1]['type'] == 'done'
        assert (cfg.files_path / 'proof').exists() is (decision == 'allow')
        result = next(e for e in seen if e['type'] == 'tool_result')
        assert result['ok'] is (decision == 'allow')
        history = fake.requests[1]['messages']
        reply = next(m['content'] for m in history if m['role'] == 'tool')
        assert ('approval round trip' in reply) if decision == 'allow' else ('Use read_file instead' in reply)
        assert not app.state.agent.approvals.pending


def test_real_server_allow_session_scoped_and_destructive_still_asks(tmp_path):
    scripts = [tool('run_command', {'command': 'echo first'}), tool('run_command', {'command': 'echo second'}),
               tool('run_command', {'command': 'rm missing'}), text('Done'),
               tool('run_command', {'command': 'echo other chat'}), text('Done')]
    with real_server(tmp_path, scripts) as (client, cfg, fake, app):
        chat = client.create_chat()
        approvals = []
        for event in client.stream_message(chat['id'], 'do work'):
            if event['type'] == 'approval':
                approvals.append(event)
                client.answer_approval(chat['id'], event['id'], 'deny' if event['risk'] == 'destructive' else 'allow_session')
        assert [e['risk'] for e in approvals] == ['normal', 'normal', 'destructive']
        other = client.create_chat()
        for event in client.stream_message(other['id'], 'another chat'):
            if event['type'] == 'approval':
                approvals.append(event); client.answer_approval(other['id'], event['id'], 'deny')
        assert len(approvals) == 4


def test_real_server_approval_timeout_and_stop(tmp_path):
    scripts = [tool('run_command', {'command': 'echo denied'}), text('Denied'),
               tool('run_command', {'command': 'echo stopped'})]
    with real_server(tmp_path, scripts) as (client, cfg, fake, app):
        app.state.agent.approvals.timeout = .03
        chat = client.create_chat()
        events = list(client.stream_message(chat['id'], 'timeout'))
        assert any(e['type'] == 'approval_resolved' and e['decision'] == 'deny' for e in events)
        assert any('timed out' in m.get('content', '') for m in fake.requests[1]['messages'] if m['role'] == 'tool')
        app.state.agent.approvals.timeout = 600
        events = []
        for event in client.stream_message(chat['id'], 'stop'):
            events.append(event)
            if event['type'] == 'approval':
                with pytest.raises(KovaError) as locked:
                    client.update_chat(chat['id'], permission='full')
                assert locked.value.status == 409
                client._request('POST', f"/chats/{chat['id']}/stop")
                with pytest.raises(KovaError) as missing:
                    client.answer_approval(chat['id'], event['id'], 'allow')
                assert missing.value.status == 404
        assert events[-1]['type'] == 'stopped'
        assert not app.state.agent.approvals.pending
        assert 'denied or interrupted' in client.get_chat(chat['id'])['messages'][-1]['content']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group integration; Windows uses taskkill /T')
def test_real_stop_kills_running_command_and_descendant(tmp_path):
    ready = tmp_path / 'ready'
    child_marker = tmp_path / 'should-not-exist'
    child_code = f"import time; from pathlib import Path; time.sleep(1); Path({str(child_marker)!r}).touch()"
    code = f"import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); Path({str(ready)!r}).write_text(str(p.pid)); time.sleep(60)"
    with real_server(tmp_path, [tool('run_command', {'command': python_command(code)})]) as (client, cfg, fake, app):
        chat = client.create_chat(permission='full')
        events = []
        for event in client.stream_message(chat['id'], 'start command'):
            events.append(event)
            if event['type'] == 'tool_call':
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert ready.exists(), 'command never started'
                client._request('POST', f"/chats/{chat['id']}/stop")
        assert events[-1]['type'] == 'stopped'
        # A killed child may be briefly zombie-reaped by init; it must not execute.
        time.sleep(1.1)
        assert not child_marker.exists()


def test_real_project_write_rechecks_after_approval_and_plan_streams(tmp_path):
    project = tmp_path / 'project'; project.mkdir()
    target = project / 'file'; target.write_text('original')
    items = [{'text': 'Edit the file', 'status': 'in_progress'}]
    scripts = [tool('update_plan', {'items': items}), tool('edit_file', {'path': 'file', 'old_text': 'original', 'new_text': 'new'}), text('Finished')]
    with real_server(tmp_path, scripts) as (client, cfg, fake, app):
        chat = client.create_chat(cwd=str(project))
        events = []
        for event in client.stream_message(chat['id'], 'edit file'):
            events.append(event)
            if event['type'] == 'approval':
                assert event['tool'] == 'edit_file'
                target.write_text('external edit')
                client.answer_approval(chat['id'], event['id'], 'allow')
        assert {'type': 'plan', 'items': items} in events
        assert client.get_chat(chat['id'])['plan'] == items
        assert target.read_text() == 'external edit'
        assert any(e['type'] == 'tool_result' and not e['ok'] and 'changed' in e['summary'] for e in events)


@pytest.mark.parametrize('name,args', [
    ('run_command', {'command': 'echo no'}), ('read_file', {'path': 'x'}),
    ('write_file', {'path': 'x', 'content': 'no'}), ('edit_file', {'path': 'x', 'old_text': 'x', 'new_text': 'y'}),
    ('search_files', {'query': 'x'}), ('list_files', {}), ('update_plan', {'items': []}),
])
def test_chat_mode_never_offers_or_executes_core_tools(tmp_path, name, args):
    cfg = load_config(tmp_path); cfg.files_path = tmp_path / 'files'
    fake = FakeModel([tool(name, args), text('No')])
    app = create_app(cfg, transport=fake.transport)
    with TestClient(app) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        chat = client.create_chat(mode='chat')
        events = list(client.stream_message(chat['id'], 'hello'))
        assert {s['function']['name'] for s in fake.requests[0]['tools']} == {'web_search', 'web_fetch'}
        assert not any(e['type'] in {'approval', 'plan'} for e in events)
        assert any(e['type'] == 'tool_result' and not e['ok'] for e in events)
        assert not cfg.files_path.exists()


@pytest.mark.parametrize('name,args', [('run_command', {'command': 4}), ('run_command', {'command': 'echo x', 'timeout': 301}), ('run_command', {'command': 'echo x', 'cwd': '/'}), ('update_plan', {'items': [{'text': 'a', 'status': 'invalid'}]}), ('write_file', {'path': '../escape', 'content': 'no'})])
def test_invalid_calls_rejected_before_approval(tmp_path, name, args):
    cfg = load_config(tmp_path); cfg.files_path = tmp_path / 'files'
    fake = FakeModel([tool(name, args), text('No')])
    app = create_app(cfg, transport=fake.transport)
    with TestClient(app) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        chat = client.create_chat()
        events = list(client.stream_message(chat['id'], 'try it'))
        assert not any(e['type'] in {'approval', 'plan'} for e in events)
        assert any(e['type'] == 'tool_result' and not e['ok'] for e in events)


def test_settings_persist_validate_and_approval_auth(tmp_path):
    cfg = load_config(tmp_path / 'home'); cfg.files_path = tmp_path / 'files'
    app = create_app(cfg, transport=FakeModel([text('ok')]).transport)
    project = tmp_path / 'project'; project.mkdir()
    with TestClient(app) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        chat = client.create_chat()
        changed = client.update_chat(chat['id'], permission='auto', cwd=str(project))
        assert changed['permission'] == 'auto' and changed['cwd'] == str(project)
        assert client.list_chats()[0]['cwd'] == str(project)
        for payload in ({'permission': 'bad'}, {'cwd': ''}, {'cwd': 'relative'}, {'cwd': str(project / 'missing')}):
            assert http.patch(f"/chats/{chat['id']}", headers={'Authorization': f'Bearer {client.token}'}, json=payload).status_code == 422
        route = f"/chats/{chat['id']}/approvals/not-pending"
        assert http.post(route, json={'decision': 'allow'}).status_code == 401
        assert http.post(route, headers={'Authorization': f'Bearer {client.token}'}, json={'decision': 'bad'}).status_code == 422
    with TestClient(create_app(cfg, transport=FakeModel([]).transport)) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        assert client.get_chat(chat['id'])['permission'] == 'auto'
        assert client.get_chat(chat['id'])['cwd'] == str(project)
        assert not http.app.state.agent.approvals.sessions


def test_old_database_migrates_to_manual_with_configured_folder(tmp_path):
    database = tmp_path / 'old.db'
    conn = sqlite3.connect(database)
    conn.execute('CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT)')
    conn.execute("INSERT INTO chats VALUES ('old', 'legacy', '2020-01-01')")
    conn.commit(); conn.close()
    store = Store(database, default_cwd=str(tmp_path / 'configured-files'))
    chat = store.get_chat('old')
    assert chat['permission'] == 'manual' and chat['cwd'] == str(tmp_path / 'configured-files') and chat['plan'] == []


def test_windows_shell_selection_without_elevation(monkeypatch):
    from types import SimpleNamespace
    from kova.tools import commands
    monkeypatch.setattr(commands, 'os', SimpleNamespace(name='nt', environ={'COMSPEC': 'cmd.exe'}))
    monkeypatch.setattr(commands.shutil, 'which', lambda name: 'pwsh.exe' if name == 'pwsh' else None)
    assert commands.shell_command('Get-Location') == ['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', 'Get-Location']
    monkeypatch.setattr(commands.shutil, 'which', lambda _: None)
    assert commands.shell_command('dir') == ['cmd.exe', '/d', '/s', '/c', 'dir']


async def test_command_default_timeout_and_maximum(tmp_path, monkeypatch):
    from kova.tools import commands
    wait_for = asyncio.wait_for
    seen = []
    async def spy(awaitable, timeout):
        seen.append(timeout)
        return await wait_for(awaitable, timeout)
    monkeypatch.setattr(commands.asyncio, 'wait_for', spy)
    assert (await commands.run_command('echo default', tmp_path))['ok']
    assert seen[-1] == 60
    assert (await commands.run_command('echo max', tmp_path, 300))['ok']
    assert seen[-1] == 300


def test_real_working_folders_are_isolated_and_permissions_apply_to_writes(tmp_path):
    project = tmp_path / 'project'; project.mkdir()
    items = [{'text': 'Finish', 'status': 'done'}]
    scripts = [tool('write_file', {'path': 'a', 'content': 'default'}), text('Done'),
               tool('write_file', {'path': 'a', 'content': 'project'}), text('Done'),
               tool('write_file', {'path': 'b', 'content': 'auto'}), tool('update_plan', {'items': items}), text('Done')]
    with real_server(tmp_path, scripts) as (client, cfg, fake, app):
        default = client.create_chat()
        events = list(client.stream_message(default['id'], 'write default'))
        assert not any(e['type'] == 'approval' for e in events)
        assert (cfg.files_path / 'a').read_text() == 'default'
        manual = client.create_chat(cwd=str(project))
        for event in client.stream_message(manual['id'], 'write project'):
            if event['type'] == 'approval':
                assert event['tool'] == 'write_file'
                client.answer_approval(manual['id'], event['id'], 'allow')
        assert (project / 'a').read_text() == 'project'
        assert (cfg.files_path / 'a').read_text() == 'default'
        client.update_chat(manual['id'], permission='auto')
        events = list(client.stream_message(manual['id'], 'write auto'))
        assert not any(e['type'] == 'approval' for e in events)
        assert (project / 'b').read_text() == 'auto'
        assert client.get_chat(manual['id'])['plan'] == items
        assert client.get_chat(default['id'])['plan'] == []


def test_work_instructions_loaded_in_system_prompt_but_not_history(tmp_path):
    cfg = load_config(tmp_path / 'home'); cfg.files_path = tmp_path / 'files'; cfg.files_path.mkdir()
    (cfg.files_path / 'KOVA.md').write_text('Build with uv; follow project conventions.')
    fake = FakeModel([text('Work'), text('Chat')])
    app = create_app(cfg, transport=fake.transport)
    with TestClient(app) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        chat = client.create_chat()
        list(client.stream_message(chat['id'], 'work'))
        assert 'Build with uv' in fake.requests[0]['messages'][0]['content']
        assert 'Build with uv' not in json.dumps(client.get_chat(chat['id'])['messages'])
        client.update_chat(chat['id'], mode='chat')
        list(client.stream_message(chat['id'], 'chat'))
        assert 'Build with uv' not in fake.requests[1]['messages'][0]['content']


def test_ask_denies_pending_approval_without_stalling(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from kova import cli
    scripts = [tool('run_command', {'command': 'echo should-not-run'}), text('Please continue in interactive chat.')]
    with real_server(tmp_path, scripts) as (client, cfg, fake, app):
        monkeypatch.setattr(cli, 'get_client', lambda: client)
        result = CliRunner().invoke(cli.app, ['ask', 'run command'], input='')
        assert result.exit_code == 0, result.output
        assert 'needs approval' in result.stderr
        reply = next(m['content'] for m in fake.requests[1]['messages'] if m['role'] == 'tool')
        assert 'denied' in reply and 'interactive Kova chat' in reply


def test_missing_project_folder_does_not_get_recreated(tmp_path):
    project = tmp_path / 'project'; project.mkdir()
    cfg = load_config(tmp_path / 'home'); cfg.files_path = tmp_path / 'files'
    fake = FakeModel([])
    with TestClient(create_app(cfg, transport=fake.transport)) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        chat = client.create_chat(cwd=str(project))
        project.rmdir()
        events = list(client.stream_message(chat['id'], 'work'))
        assert events[-1]['type'] == 'error' and 'no longer exists' in events[-1]['message']
        assert not project.exists() and not fake.requests


async def test_binary_command_output_is_bounded_after_utf8_decoding(tmp_path):
    result = await run_command(python_command("import sys; sys.stdout.buffer.write(b'\\xff'*20000)"), tmp_path)
    assert result['ok'] and result['truncated']
    assert len(result['output'].encode('utf-8')) <= OUTPUT_LIMIT


def test_rule_denial_overrides_full_mode_and_returns_comment(tmp_path):
    from kova.permissions import Rule
    with real_server(tmp_path, [tool('run_command', {'command':'echo forbidden'}), text('Understood')]) as (client, cfg, fake, app):
        app.state.agent.approvals.rules = [Rule('deny','run_command','echo *',message='Use read_file instead')]
        chat=client.create_chat(permission='full')
        events=list(client.stream_message(chat['id'],'do it'))
        assert not any(e['type']=='approval' for e in events)
        replies=[m['content'] for m in fake.requests[1]['messages'] if m['role']=='tool']
        assert any('Use read_file instead' in reply for reply in replies)
