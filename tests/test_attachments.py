"""Local extraction, lifecycle, ownership and vision wire contract."""
import hashlib
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient

from kova import attachments
from kova.config import ensure_token, load_config
from kova.model_adapters import anthropic_request, responses_request
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text
from tests.test_server import server


def upload(client, headers, chat, name='demo.py', data=b'first\nsecond\nthird'):
    return client.post(f'/chats/{chat}/attachments', headers=headers, files={'file': (name, data)})


def link_message_attachment(store, message_id, row):
    """Write the name-only link the transcript branch adds, without that branch."""
    with store._lock:
        store._conn.execute(
            "CREATE TABLE IF NOT EXISTS message_attachments ("
            "message_id INTEGER NOT NULL, attachment_id TEXT NOT NULL, "
            "name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (message_id, attachment_id))")
        store._conn.execute(
            "INSERT OR REPLACE INTO message_attachments "
            "(message_id, attachment_id, name, position) VALUES (?, ?, ?, 0)",
            (message_id, row['id'], row['name']))
        store._conn.commit()


def test_upload_turn_and_delete(server):
    client, headers, cfg, fake = server
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    response = upload(client, headers, chat)
    assert response.status_code == 200
    row = response.json()
    assert row['sha256'] == hashlib.sha256(b'first\nsecond\nthird').hexdigest()
    assert row['pages'] is None and 'path' not in row
    paths = list((cfg.home / 'attachments' / chat).iterdir())
    assert len(paths) == 1
    if os.name != 'nt':  # Windows has no POSIX mode bits; the 0700 directory is what protects it there
        assert paths[0].stat().st_mode & 0o777 == 0o600
    assert attachments.read_text(paths[0], 2, 2) == 'lines 2-2 of 3\n2: second'
    response = client.post(f'/chats/{chat}/messages', headers=headers, json={'text': 'Explain', 'attachments': [row['id']]})
    assert response.status_code == 200
    history = client.get(f'/chats/{chat}', headers=headers).json()['messages']
    assert 'file: demo.py' in history[0]['content']
    assert '2: second' in history[0]['content']
    assert client.delete(f'/chats/{chat}/attachments/{row["id"]}', headers=headers).status_code == 200
    assert not paths[0].exists()
    assert client.delete(f'/chats/{chat}/attachments/{row["id"]}', headers=headers).status_code == 404


def test_ownership_limits_cleanup(server):
    client, headers, cfg, fake = server
    chats = [client.post('/chats', headers=headers, json={}).json()['id'] for _ in range(2)]
    row = upload(client, headers, chats[0]).json()
    endpoint = f'/chats/{chats[1]}/messages'
    assert client.post(endpoint, headers=headers, json={'text': 'x', 'attachments': [row['id']]}).status_code == 400
    assert client.post(endpoint, headers=headers, json={'text': 'x', 'attachments': ['a'] * 4}).status_code == 422
    assert client.delete(f'/chats/{chats[1]}/attachments/{row["id"]}', headers=headers).status_code == 404
    assert upload(client, headers, chats[0], 'bad.exe').status_code == 400
    assert upload(client, headers, chats[0], data=b'x' * (attachments.MAX_BYTES + 1)).status_code == 400
    assert client.post(f'/chats/{chats[0]}/attachments', files={'file': ('x.txt', b'x')}).status_code == 401
    assert client.delete(f'/chats/{chats[0]}', headers=headers).status_code == 200
    assert not (cfg.home / 'attachments' / chats[0]).exists()


def test_docx_and_bounded_text(tmp_path):
    path = tmp_path / 'demo.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:document>')
    assert attachments.extract(path) == ('Hello', None)
    path = tmp_path / 'large.txt'
    path.write_text('x' * 100_000)
    assert len(attachments.read_text(path)) <= 32_000


def test_clear_chat_attachment_bytes(server):
    client, headers, cfg, fake = server
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    upload(client, headers, chat)
    client.app.state.store.clear_chats()
    assert not list((cfg.home / 'attachments').rglob('*.py'))
    assert client.app.state.store.attachments(chat) == []


def test_vision_gated_and_wire_formats(server):
    client, headers, cfg, fake = server
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    row = upload(client, headers, chat, 'shot.png', b'\x89PNG\r\n\x1a\n').json()
    assert 'vision' not in client.get('/capabilities', headers=headers).json()['capabilities']
    assert client.post(f'/chats/{chat}/messages', headers=headers, json={'text': 'describe', 'attachments': [row['id']]}).status_code == 400
    cfg.models[0].vision = True
    assert 'vision' in client.get('/capabilities', headers=headers).json()['capabilities']
    response = client.post(f'/chats/{chat}/messages', headers=headers, json={'text': 'describe', 'attachments': [row['id']]})
    assert 'event: done' in response.text
    assert fake.requests[0]['messages'][-1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    parts = [{'type': 'text', 'text': 'hi'}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='}}]
    messages = [{'role': 'user', 'content': parts}]
    _, payload = anthropic_request('demo', messages, None)
    assert payload['messages'][0]['content'][1]['source']['media_type'] == 'image/png'
    _, payload = responses_request('demo', messages, None)
    assert payload['input'][0]['content'][1]['type'] == 'input_image'


def test_vision_is_per_model_and_round_trips(server):
    """The contract and the API follow the selected model, not only the active one."""
    client, headers, cfg, fake = server
    assert 'vision' not in client.get('/capabilities', headers=headers).json()['capabilities']
    created = client.post('/models', headers=headers, json={
        'name': 'vision-model', 'base_url': 'http://model.test/v1', 'model': 'vision-test',
        'context': 32768, 'vision': True})
    assert created.status_code == 200
    assert created.json()['vision'] is True
    listed = {entry['name']: entry for entry in client.get('/models', headers=headers).json()}
    assert listed['vision-model']['vision'] is True
    assert listed['local']['vision'] is False
    reloaded = {entry.name: entry for entry in load_config(cfg.home).models}
    assert reloaded['vision-model'].vision is True

    selected = client.get('/capabilities?model=vision-model', headers=headers).json()
    assert 'vision' in selected['capabilities']
    assert 'vision' in selected['offered']['work']
    assert 'vision' not in client.get('/capabilities?model=local', headers=headers).json()['capabilities']
    assert 'vision' not in client.get('/capabilities', headers=headers).json()['capabilities']
    assert client.get('/capabilities?model=missing', headers=headers).status_code == 404

    # A named vision model accepts an image even while another model is active.
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    row = upload(client, headers, chat, 'shot.png', b'\x89PNG\r\n\x1a\n').json()
    response = client.post(f'/chats/{chat}/messages', headers=headers, json={
        'text': 'describe', 'model': 'vision-model', 'attachments': [row['id']]})
    assert response.status_code == 200 and 'event: done' in response.text

    patched = client.patch('/models', headers=headers, json={'name': 'vision-model', 'vision': False})
    assert patched.status_code == 200 and patched.json()['vision'] is False
    assert 'vision' not in client.get('/capabilities?model=vision-model', headers=headers).json()['capabilities']
    assert {entry.name: entry for entry in load_config(cfg.home).models}['vision-model'].vision is False
    assert client.patch('/models', headers=headers, json={'name': 'missing', 'vision': True}).status_code == 404


def test_optional_reader_errors(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name in {'pypdf', 'openpyxl', 'pptx'}:
            raise ImportError(name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    for suffix in ('.pdf', '.xlsx', '.pptx'):
        path = tmp_path / ('demo' + suffix)
        with zipfile.ZipFile(path, 'w'):
            pass
        with pytest.raises(attachments.AttachmentError, match='optional reader'):
            attachments.extract(path)


def test_pdf_encryption_and_pages(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    path = tmp_path / 'demo.pdf'
    reader = SimpleNamespace(is_encrypted=True, pages=[])
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=lambda _: reader))
    with pytest.raises(attachments.AttachmentError, match='Encrypted PDFs'):
        attachments.extract(path)
    reader.is_encrypted = False
    reader.pages = [SimpleNamespace(extract_text=lambda: 'invoice')]
    assert attachments.extract(path) == ('[page 1]\ninvoice', 1)
    reader.pages *= 501
    with pytest.raises(attachments.AttachmentError, match='500 pages'):
        attachments.extract(path)


def test_model_can_reread_attachment(server):
    from tests.fake_model import tool, text
    client, headers, cfg, fake = server
    chat = client.post('/chats', headers=headers, json={'mode': 'chat'}).json()['id']
    row = upload(client, headers, chat).json()
    fake.scripts = [tool('read_attachment', {'id': row['id'], 'start': 2, 'end': 2}), text('Read it')]
    result = client.post(f'/chats/{chat}/messages', headers=headers, json={'text': 'Read line two', 'attachments': [row['id']]})
    assert 'event: done' in result.text
    assert any(m.get('role') == 'tool' and m['content'] == 'lines 2-2 of 3\n2: second' for m in fake.requests[1]['messages'])


@pytest.mark.parametrize('declared', [True, False])
def test_request_limit_before_spooling(server, monkeypatch, declared):
    client, headers, cfg, _ = server
    monkeypatch.setattr(attachments, 'MAX_REQUEST_BYTES', 128)
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    h = dict(headers)
    if declared:
        h['Content-Length'] = '1000'
        response = client.post(f'/chats/{chat}/attachments', headers=h, content=b'x')
    else:
        h['Content-Type'] = 'multipart/form-data; boundary=demo'
        def chunks():
            yield b'--demo\r\nContent-Disposition: form-data; name="file"; filename="demo.txt"\r\n\r\n'
            yield b'x' * 256
            yield b'\r\n--demo--\r\n'
        response = client.post(f'/chats/{chat}/attachments', headers=h, content=chunks())
    assert response.status_code == 413
    assert not list((cfg.home / 'attachments').rglob('*'))


@pytest.mark.parametrize('declaration', ['<!DOCTYPE x [<!ENTITY a "boom">]>', '<!ENTITY a "boom">'])
def test_docx_rejects_entities(tmp_path, declaration):
    path = tmp_path / 'entity.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', declaration + '<document/>')
    with pytest.raises(attachments.AttachmentError, match='declarations'):
        attachments.extract(path)


@pytest.mark.parametrize('suffix', ['pdf', 'pptx'])
def test_extraction_stops_consuming_pages(tmp_path, monkeypatch, suffix):
    import sys
    from types import SimpleNamespace as NS
    calls = []
    def text():
        calls.append(1)
        assert len(calls) == 1
        return 'x' * 100_000
    path = tmp_path / ('demo.' + suffix)
    if suffix == 'pdf':
        monkeypatch.setitem(sys.modules, 'pypdf', NS(PdfReader=lambda _: NS(is_encrypted=False, pages=[NS(extract_text=text)] * 500)))
    else:
        with zipfile.ZipFile(path, 'w'):
            pass
        class Shape:
            has_text_frame = True
            @property
            def text(self):
                return text()
        monkeypatch.setitem(sys.modules, 'pptx', NS(Presentation=lambda _: NS(slides=[NS(shapes=[Shape()])] * 500)))
    result, _ = attachments.extract(path)
    assert len(result) == attachments.MAX_CHARS
    assert len(calls) == 1


def test_repeated_turn_and_tool_reads_use_cache(server, monkeypatch):
    client, headers, cfg, _ = server
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    row = upload(client, headers, chat).json()
    row = client.app.state.store.attachments(chat)[0]
    attachments.cached_text.cache_clear()
    original = attachments.extract
    calls = []
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(attachments, 'extract', counted)
    for _ in range(3):
        attachments.block(row)
        attachments.read_text(Path(row['path']), 2, 2, row=row)
    assert len(calls) == 1


@pytest.mark.parametrize('chat', ['../escape', '/escape', 'x' * 65, 'a/b'])
def test_invalid_chat_directory(tmp_path, chat):
    with pytest.raises(attachments.AttachmentError, match='chat ID'):
        attachments.save(tmp_path, None, chat, 'demo.txt', b'x')
    assert not list(tmp_path.iterdir())


def test_atomic_private_creation(server, monkeypatch):
    import os
    client, headers, cfg, _ = server
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    original = os.open
    calls = []
    def checked(path, flags, mode=0o777, **kwargs):
        calls.append((flags, mode))
        return original(path, flags, mode, **kwargs)
    monkeypatch.setattr(attachments.os, 'open', checked)
    assert upload(client, headers, chat).status_code == 200
    assert any(flags & os.O_EXCL and mode == 0o600 for flags, mode in calls)


@pytest.mark.parametrize('name', ['bad\x00.txt', 'demo.' + 'x' * 300])
def test_invalid_filenames_are_client_errors(tmp_path, name):
    with pytest.raises(attachments.AttachmentError):
        attachments.save(tmp_path, None, 'demo', name, b'x')


def test_delete_chat_removes_preexisting_empty_directory(server):
    client, headers, cfg, _ = server
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    folder = cfg.home / 'attachments' / chat
    folder.mkdir(parents=True)
    assert client.delete(f'/chats/{chat}', headers=headers).status_code == 200
    assert not folder.exists()


@pytest.mark.parametrize("delete_source", [True, False])
def test_fork_copies_attachments_independently(server, delete_source):
    client, headers, cfg, fake = server
    store = client.app.state.store
    chat = store.create_chat()['id']
    row = attachments.save(cfg.home, store, chat, 'demo.txt', b'hello')
    cutoff = store.add_message(chat, 'user', 'attachment_id: ' + row['id'])
    store.add_message(chat, 'assistant', tool_calls=[{'id': 'call', 'type': 'function', 'function': {'name': 'read_attachment', 'arguments': '{"attachment_id": "' + row['id'] + '"}'}}])
    assert store.save_compaction(chat, 0, cutoff, 'attachment_id: ' + row['id'])
    fork = store.fork_chat(chat)
    copied = store.attachments(fork['id'])
    assert len(copied) == 1
    assert copied[0]['id'] != row['id']
    assert copied[0]['id'] in fork['messages'][0]['content']
    assert copied[0]['id'] in fork['messages'][1]['tool_calls'][0]['function']['arguments']
    assert copied[0]['id'] in store.context_snapshot(fork['id'])[0]
    assert Path(copied[0]['path']).read_bytes() == b'hello'
    first, second = (chat, fork["id"]) if delete_source else (fork["id"], chat)
    surviving = copied[0] if delete_source else store.attachments(chat)[0]
    store.delete_chat(first)
    assert Path(surviving["path"]).read_bytes() == b"hello"
    store.delete_chat(second)
    assert not Path(copied[0]['path']).exists()


def test_failed_fork_rolls_back_attachment_copies(server, monkeypatch):
    client, headers, cfg, fake = server
    store = client.app.state.store
    chat = store.create_chat()['id']
    row = attachments.save(cfg.home, store, chat, 'demo.txt', b'hello')
    before = store.list_chats()
    def fail(*args):
        raise OSError('copy failed')
    monkeypatch.setattr('kova.store.shutil.copyfileobj', fail)
    with pytest.raises(OSError, match='copy failed'):
        store.fork_chat(chat)
    assert store.list_chats() == before
    assert list((cfg.home / 'attachments').iterdir()) == [cfg.home / 'attachments' / chat]
    assert store.attachments(chat)[0]['id'] == row['id']


def test_abandoned_upload_expires_but_sent_and_fresh_uploads_stay(server):
    """Turn consumed the clock, so expiry is deterministic and never a sleep."""
    client, headers, cfg, fake = server
    store = client.app.state.store
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    client.app.state.now = lambda: clock[0]
    sent = upload(client, headers, chat, 'sent.txt', b'keep me').json()
    response = client.post(f'/chats/{chat}/messages', headers=headers,
                           json={'text': 'Read it', 'attachments': [sent['id']]})
    assert response.status_code == 200 and 'event: done' in response.text
    abandoned = upload(client, headers, chat, 'gone.txt', b'drop me').json()
    paths = {row['id']: Path(row['path']) for row in store.attachments(chat)}
    assert paths[sent['id']].exists() and paths[abandoned['id']].exists()

    clock[0] += attachments.PENDING_TTL + timedelta(minutes=1)
    fresh = attachments.save(cfg.home, store, chat, 'fresh.txt', b'not sent yet',
                             now=lambda: clock[0])
    assert attachments.reap(store, now=lambda: clock[0]) == 1

    assert {row['id'] for row in store.attachments(chat)} == {sent['id'], fresh['id']}
    assert paths[sent['id']].exists() and not paths[abandoned['id']].exists()
    assert (cfg.home / 'attachments' / chat).is_dir()


def test_reap_keeps_a_legacy_transcript_reference_without_used_flag(server):
    client, headers, cfg, fake = server
    store = client.app.state.store
    chat = store.create_chat()['id']
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = attachments.save(cfg.home, store, chat, 'legacy.txt', b'legacy',
                           now=lambda: old)
    store.add_message(chat, 'user', f"attachment_id: {row['id']}")
    assert attachments.reap(store, now=lambda: old + attachments.PENDING_TTL + timedelta(seconds=1)) == 0
    assert [entry['id'] for entry in store.attachments(chat)] == [row['id']]


def test_reap_keeps_a_linked_message_attachment_without_used_flag(server):
    """A name-only link is usage metadata even when the old flag was never set."""
    client, headers, cfg, fake = server
    store = client.app.state.store
    chat = store.create_chat()['id']
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = attachments.save(cfg.home, store, chat, 'linked.txt', b'linked',
                           now=lambda: old)
    message_id = store.add_message(chat, 'user', '')
    link_message_attachment(store, message_id, row)
    with store._lock:
        store._conn.execute('UPDATE attachments SET used = 0')
        store._conn.commit()

    assert attachments.reap(store, now=lambda: old + attachments.PENDING_TTL + timedelta(seconds=1)) == 0
    [stored] = store.attachments(chat)
    assert stored['id'] == row['id']
    assert Path(stored['path']).exists()


def test_linked_message_attachment_frees_the_pending_quota(server, monkeypatch):
    """A linked upload is spoken for, so it is not an unsent attachment."""
    client, headers, cfg, fake = server
    store = client.app.state.store
    chat = store.create_chat()['id']
    monkeypatch.setattr(attachments, 'MAX_PENDING_UPLOADS', 1)
    row = attachments.save(cfg.home, store, chat, 'linked.txt', b'linked')
    message_id = store.add_message(chat, 'user', '')
    link_message_attachment(store, message_id, row)
    with store._lock:
        store._conn.execute('UPDATE attachments SET used = 0')
        store._conn.commit()

    attachments.ensure_capacity(store, chat, 1)  # no quota error

    attachments.save(cfg.home, store, chat, 'next.txt', b'next')
    assert {entry['name'] for entry in store.attachments(chat)} == {'linked.txt', 'next.txt'}


def test_used_flag_retains_an_upload_even_if_the_transcript_changes(server, monkeypatch):
    client, headers, cfg, fake = server
    store = client.app.state.store
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    client.app.state.now = lambda: clock[0]
    row = upload(client, headers, chat).json()
    assert client.post(f'/chats/{chat}/messages', headers=headers,
                       json={'text': 'Read it', 'attachments': [row['id']]}).status_code == 200
    # A future transcript format may stop spelling out the id. The explicit
    # marker, not the text, is what protects the file.
    monkeypatch.setattr(store, 'referenced_attachment_ids', lambda chat_id: set())
    clock[0] += attachments.PENDING_TTL + timedelta(days=1)
    assert attachments.reap(store, now=lambda: clock[0]) == 0
    assert Path(store.attachments(chat)[0]['path']).exists()


def test_pending_upload_quota_is_bounded_and_rejects_cleanly(server, monkeypatch):
    client, headers, cfg, fake = server
    monkeypatch.setattr(attachments, 'MAX_PENDING_UPLOADS', 2)
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    assert upload(client, headers, chat, 'one.txt').status_code == 200
    assert upload(client, headers, chat, 'two.txt').status_code == 200
    folder = cfg.home / 'attachments' / chat
    before = sorted(path.name for path in folder.iterdir())
    response = upload(client, headers, chat, 'three.txt')
    assert response.status_code == 409
    assert '2 unsent attachments' in response.json()['detail']
    assert sorted(path.name for path in folder.iterdir()) == before


def test_sent_attachments_free_the_pending_quota(server, monkeypatch):
    client, headers, cfg, fake = server
    monkeypatch.setattr(attachments, 'MAX_PENDING_UPLOADS', 1)
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    first = upload(client, headers, chat, 'one.txt').json()
    assert client.post(f'/chats/{chat}/messages', headers=headers,
                       json={'text': 'Read it', 'attachments': [first['id']]}).status_code == 200
    assert upload(client, headers, chat, 'two.txt').status_code == 200
    assert upload(client, headers, chat, 'three.txt').status_code == 409


def test_per_chat_count_and_byte_quotas(server, monkeypatch):
    client, headers, cfg, fake = server
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    monkeypatch.setattr(attachments, 'MAX_PENDING_UPLOADS', 10)
    monkeypatch.setattr(attachments, 'MAX_CHAT_ATTACHMENTS', 2)
    assert upload(client, headers, chat, 'one.txt').status_code == 200
    assert upload(client, headers, chat, 'two.txt').status_code == 200
    response = upload(client, headers, chat, 'three.txt')
    assert response.status_code == 409
    assert '2-attachment limit' in response.json()['detail']

    other = client.post('/chats', headers=headers, json={}).json()['id']
    monkeypatch.setattr(attachments, 'MAX_CHAT_ATTACHMENT_BYTES', 8)
    assert upload(client, headers, other, 'a.txt', b'12345').status_code == 200
    response = upload(client, headers, other, 'b.txt', b'1234')
    assert response.status_code == 409
    assert 'attachment storage limit' in response.json()['detail']


def test_total_attachment_storage_is_bounded(server, monkeypatch):
    client, headers, cfg, fake = server
    monkeypatch.setattr(attachments, 'MAX_TOTAL_ATTACHMENT_BYTES', 8)
    first = client.post('/chats', headers=headers, json={}).json()['id']
    second = client.post('/chats', headers=headers, json={}).json()['id']
    assert upload(client, headers, first, 'a.txt', b'12345').status_code == 200
    response = upload(client, headers, second, 'b.txt', b'1234')
    assert response.status_code == 409
    assert 'total attachment storage limit' in response.json()['detail']


def test_startup_sweeps_an_expired_abandoned_upload(tmp_path, monkeypatch):
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config()
    store = Store(cfg.home / 'kova.db')
    chat = store.create_chat()['id']
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    attachments.save(cfg.home, store, chat, 'old.txt', b'old', now=lambda: old)
    path = Path(store.attachments(chat)[0]['path'])
    assert path.exists()

    fake = FakeModel([text('unused')])
    app = create_app(cfg, transport=fake.transport,
                     now=lambda: old + attachments.PENDING_TTL + timedelta(seconds=1))
    with TestClient(app):
        pass
    assert store.attachments(chat) == []
    assert not path.exists()


def test_attachment_schema_migrates_in_place(tmp_path):
    import sqlite3
    db = tmp_path / 'kova.db'
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE attachments (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, name TEXT NOT NULL,"
        " mime TEXT NOT NULL, size INTEGER NOT NULL, pages INTEGER, sha256 TEXT NOT NULL, path TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO attachments VALUES (?, 'chat', 'old.txt', 'text/plain', 3, NULL, 'sha', '/tmp/old.txt')",
        ('a' * 32,))
    conn.commit()
    conn.close()

    store = Store(db)
    row = store.attachment_rows()[0]
    assert row['created'] is None and row['used'] == 0
    assert store.referenced_attachment_ids('chat') == set()
