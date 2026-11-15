"""Incognito content must never reach persistent storage, even while live."""
import zipfile

import pytest
from fastapi.testclient import TestClient

from kova.backup import export_archive
from kova.config import ensure_token, load_config
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text, tool

SECRET = 'incognito-canary-never-persist-73921'


def assert_clean(home):
    for path in home.rglob('*'):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path.name


@pytest.mark.parametrize('fts', [True, False])
def test_store_incognito_is_memory_only(tmp_path, fts):
    store = Store(tmp_path / 'kova.db')
    store.fts_enabled = fts and store.fts_enabled
    chat = store.create_chat(title=SECRET, ephemeral=True)
    cid = chat['id']
    assert chat['ephemeral'] is True
    mid = store.add_message(cid, 'user', SECRET)
    store.update_chat(cid, plan=[{'text': SECRET, 'status': 'pending'}])
    store.queue_message(cid, SECRET)
    assert store.save_compaction(cid, 0, mid, SECRET)
    assert store.get_chat(cid)['messages'][0]['content'] == SECRET
    assert store.get_context(cid)[0]['content'].endswith(SECRET)
    assert store.queued_messages(cid)[0]['text'] == SECRET
    assert not store.list_chats()
    assert not store.search_chats(SECRET)
    assert_clean(tmp_path)
    assert Store(tmp_path / 'kova.db').get_chat(cid) is None
    with pytest.raises(ValueError, match='Incognito'):
        store.fork_chat(cid)
    assert store.delete_chat(cid)
    assert store.get_chat(cid) is None
    # A late steer must not fall back into the disk store after deletion.
    store.queue_message(cid, SECRET)
    assert_clean(tmp_path)


def test_incognito_turn_disk_and_backup_stay_clean(tmp_path, monkeypatch):
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config()
    cfg.memory_review = cfg.memory_consolidate = True
    cfg.memory_review_every = 1
    fake = FakeModel([tool('memory_save', {'title': SECRET, 'body': SECRET}), text(SECRET)])
    app = create_app(cfg, transport=fake.transport)
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(app, headers=headers) as client:
        chat = client.post('/chats', json={'ephemeral': True}).json()
        assert chat['ephemeral'] is True
        cid = chat['id']
        memory_calls = []
        def forbidden(*args, **kwargs):
            memory_calls.append(True)
            raise AssertionError('Incognito invoked persistent memory')
        for method in ('search', 'usage', 'index_text'):
            monkeypatch.setattr(app.state.agent.memory, method, forbidden)
        monkeypatch.setattr(app.state.agent.memory_review, 'after_turn', forbidden)
        response = client.post(f'/chats/{cid}/messages', json={'text': SECRET})
        assert 'event: done' in response.text
        assert not memory_calls
        assert client.get(f'/chats/{cid}').json()['messages'][-1]['content'] == SECRET
        assert not client.get('/chats/search', params={'q': SECRET}).json()
        assert not app.state.store.list_chats()
        assert client.post(f'/chats/{cid}/fork').status_code == 409
        assert client.post(f'/chats/{cid}/attachments', files={'file': ('secret.txt', SECRET)}).status_code == 409
        archive = tmp_path / 'backup.zip'
        assert export_archive(cfg, archive, store=app.state.store)['chats'] == 0
        with zipfile.ZipFile(archive) as backup:
            assert all(SECRET.encode() not in backup.read(n) for n in backup.namelist())
        assert_clean(tmp_path)
        sent = fake.requests[0]
        assert not any(t['function']['name'] == 'memory_save' for t in sent.get('tools', []))
    assert app.state.store.get_chat(cid) is None
    assert_clean(tmp_path)


@pytest.mark.parametrize('failure', [False, True])
def test_ask_incognito_marks_output_and_cleans_up(tmp_path, monkeypatch, failure):
    import httpx
    from typer.testing import CliRunner
    from kova import cli
    from kova.client import KovaClient
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config()
    fake = FakeModel([text(SECRET)])
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text=SECRET)) if failure else fake.transport
    app = create_app(cfg, transport=transport)
    with TestClient(app) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        monkeypatch.setattr(cli, 'get_client', lambda: client)
        result = CliRunner().invoke(cli.app, ['ask', '--incognito', '--json', SECRET])
        assert result.exit_code == (1 if failure else 0), result.output
        assert 'Incognito' in result.stderr
        if not failure:
            import json
            assert json.loads(result.stdout)['incognito'] is True
        assert not app.state.store.list_chats(include_ephemeral=True)
        assert_clean(tmp_path)


def test_terminal_incognito_never_saves_editor_history(tmp_path):
    import io
    from types import SimpleNamespace
    from kova.tui.app import ChatApp
    deleted = []
    client = SimpleNamespace(delete_chat=lambda cid: deleted.append(cid))
    app = ChatApp(client, None, out=io.StringIO(), color=False, history_path=tmp_path / 'history.jsonl')
    app._cmd_incognito('')
    app.chat_id = 'incognito-test'
    app.editor.insert(SECRET)
    app.editor.submit()
    app._remember(SECRET)  # must not call a memory API
    app._shell(SECRET)  # must not spawn a shell
    app._cmd_new('')
    app._shutdown()
    assert deleted == ['incognito-test']
    assert_clean(tmp_path)


async def test_upload_rejected_before_body_is_read():
    from kova.attachments import AttachmentBodyLimit
    async def forbidden(*args):
        raise AssertionError('Incognito upload body reached the multipart parser')
    sent = []
    async def send(message):
        sent.append(message)
    middleware = AttachmentBodyLimit(forbidden)
    await middleware({'type': 'http', 'method': 'POST', 'path': '/chats/incognito-test/attachments'}, forbidden, send)
    assert sent[0]['status'] == 409


async def test_compaction_and_background_passes_never_persist(tmp_path):
    from datetime import datetime
    from kova.compact import compact
    from kova.memory import Memory
    from kova.models import ModelClient
    from kova.review import MemoryReview, ReviewSettings
    cfg = load_config(tmp_path)
    store = Store(tmp_path / 'kova.db')
    cid = store.create_chat(ephemeral=True)['id']
    for _ in range(3):
        store.add_message(cid, 'user', SECRET * 20)
        store.add_message(cid, 'assistant', SECRET * 20)
    fake = FakeModel([text(SECRET)])
    client = ModelClient(cfg.active_model(), transport=fake.transport)
    event = await compact(store, cid, client, context_tokens=4096, force=True)
    assert event['type'] == 'compacted'
    assert store.context_snapshot(cid)[0] == SECRET
    review = MemoryReview(store, ReviewSettings(review=True, every=1, consolidate=True))
    memory = Memory(cfg.memory_path)
    assert await review.review(cid, memory, client) == []
    assert await review.after_turn(chat_id=cid, mode='work', memory=memory, client=client, now=datetime.now().astimezone()) == []
    assert len(fake.requests) == 1
    assert not store.memory_diary()
    assert not store.memory_revisions()
    assert not store.list_checkpoints(cid)
    assert_clean(tmp_path)


def test_incognito_cannot_be_converted_or_exported(tmp_path, monkeypatch):
    from kova import cli
    from kova.client import KovaClient
    from typer.testing import CliRunner
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config()
    app = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(app) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        monkeypatch.setattr(cli, 'get_client', lambda: client)
        cid = client.create_chat(ephemeral=True)['id']
        app.state.store.add_message(cid, 'user', SECRET)
        assert http.patch(f'/chats/{cid}', headers={'Authorization': f'Bearer {client.token}'}, json={'ephemeral': False}).status_code == 409
        result = CliRunner().invoke(cli.app, ['chats', 'export', cid, '--json'])
        assert result.exit_code == 1
        assert SECRET not in result.output
        assert_clean(tmp_path)


@pytest.mark.parametrize('ending', ['finish', 'stop', 'delete'])
def test_live_incognito_queue_steer_and_cancellation(tmp_path, monkeypatch, ending):
    import asyncio
    import threading
    from kova.models import ModelClient, ModelEvent
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config()
    entered, release = threading.Event(), threading.Event()
    async def stream(self, messages, tools=None):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield ModelEvent('text', text=SECRET)
    monkeypatch.setattr(ModelClient, 'stream', stream)
    app = create_app(cfg, transport=FakeModel([]).transport)
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(app, headers=headers) as client:
        cid = client.post('/chats', json={'ephemeral': True}).json()['id']
        result = []
        thread = threading.Thread(target=lambda: result.append(client.post(f'/chats/{cid}/messages', json={'text': SECRET})))
        thread.start()
        try:
            assert entered.wait(5)
            assert client.post(f'/chats/{cid}/queue', json={'text': SECRET}).status_code == 200
            assert client.post(f'/chats/{cid}/steer', json={'text': SECRET}).status_code == 200
            assert_clean(tmp_path)
            if ending == 'stop':
                assert client.post(f'/chats/{cid}/stop').status_code == 200
            elif ending == 'delete':
                assert client.delete(f'/chats/{cid}').status_code == 200
            release.set()
            thread.join(5)
            assert not thread.is_alive()
            assert 'event: done' in result[0].text if ending == 'finish' else 'event: stopped' in result[0].text
            if ending == 'delete':
                assert client.get(f'/chats/{cid}').status_code == 404
            else:
                assert client.delete(f'/chats/{cid}').status_code == 200
            assert client.delete(f'/chats/{cid}').status_code == 200
            assert_clean(tmp_path)
        finally:
            release.set()
            thread.join(5)
    # A new server sharing the same home cannot recover any incognito state.
    with TestClient(create_app(cfg, transport=FakeModel([]).transport), headers=headers) as fresh:
        assert fresh.get(f'/chats/{cid}').status_code == 404
        assert not fresh.get('/chats').json()
    assert_clean(tmp_path)


def test_terminal_shutdown_after_server_stops_keeps_history_private(tmp_path):
    import io
    from types import SimpleNamespace
    from kova.client import KovaError
    from kova.tui.app import ChatApp
    def stopped(cid):
        raise KovaError(503, 'Server stopped')
    app = ChatApp(SimpleNamespace(delete_chat=stopped), 'incognito-test', out=io.StringIO(), color=False,
                  history_path=tmp_path / 'history.jsonl', incognito=True)
    app.editor.insert(SECRET)
    app.editor.submit()
    app._shutdown()
    assert_clean(tmp_path)
