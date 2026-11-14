"""Local extraction, lifecycle, ownership and vision wire contract."""
import hashlib
import io
import os
from pathlib import Path
import zipfile

import pytest

from kova import attachments
from kova.backup import export_archive
from kova.config import load_config
from kova.model_adapters import anthropic_request, responses_request
from tests.test_server import server


def upload(client, headers, chat, name='demo.py', data=b'first\nsecond\nthird'):
    return client.post(f'/chats/{chat}/attachments', headers=headers, files={'file': (name, data)})


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
    assert [message['content'] for message in history if message['role'] == 'user'] == ['Explain']
    sent = fake.requests[0]['messages'][-1]
    assert sent['role'] == 'user'
    assert 'file: demo.py' in sent['content']
    assert '2: second' in sent['content']
    assert client.delete(f'/chats/{chat}/attachments/{row["id"]}', headers=headers).status_code == 200
    assert not paths[0].exists()
    assert client.delete(f'/chats/{chat}/attachments/{row["id"]}', headers=headers).status_code == 404


def test_extracted_text_stays_out_of_transcript_search_and_backup(server, tmp_path):
    """Attachment text is model context only; it never enters persisted chat data."""
    client, headers, cfg, fake = server
    sentinel = 'zirconquartz'
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    row = upload(client, headers, chat, 'notes.txt', f'alpha\n{sentinel}\nomega'.encode()).json()

    response = client.post(f'/chats/{chat}/messages', headers=headers,
                           json={'text': 'Summarize', 'attachments': [row['id']]})
    assert 'event: done' in response.text
    request = fake.requests[0]['messages'][-1]
    assert request['role'] == 'user' and sentinel in request['content']
    assert row['id'] in request['content']

    body = client.get(f'/chats/{chat}', headers=headers).json()
    assert body['title'] == 'Summarize'
    assert all(sentinel not in (message.get('content') or '') for message in body['messages'])
    assert all('attachment_id' not in (message.get('content') or '') for message in body['messages'])
    assert sentinel not in str(client.app.state.store.get_context(chat))
    assert client.get('/chats/search', headers=headers, params={'q': sentinel}).json() == []

    target = tmp_path / 'backup.zip'
    export_archive(cfg, target, store=client.app.state.store)
    with zipfile.ZipFile(target) as archive:
        for name in archive.namelist():
            assert sentinel.encode() not in archive.read(name)


def test_attachment_only_turn_titles_with_filename(server):
    client, headers, cfg, fake = server
    sentinel = 'zirconquartz'
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    row = upload(client, headers, chat, 'quarterly-notes.txt', f'alpha\n{sentinel}'.encode()).json()

    response = client.post(f'/chats/{chat}/messages', headers=headers,
                           json={'text': '', 'attachments': [row['id']]})
    assert 'event: done' in response.text
    assert sentinel in fake.requests[0]['messages'][-1]['content']

    body = client.get(f'/chats/{chat}', headers=headers).json()
    assert body['title'] == 'quarterly-notes.txt'
    assert sentinel not in str(body) and 'attachment_id' not in str(body)
    assert [m['content'] for m in body['messages'] if m['role'] == 'user'] == ['']


def test_read_attachment_still_works_from_a_later_turn(server):
    """The transient index keeps ids usable after the text left the transcript."""
    from tests.fake_model import tool, text
    client, headers, cfg, fake = server
    sentinel = 'zirconquartz'
    chat = client.post('/chats', headers=headers, json={}).json()['id']
    row = upload(client, headers, chat, 'notes.txt', f'alpha\n{sentinel}\nomega'.encode()).json()
    fake.scripts = [text('Noted.'), tool('read_attachment', {'id': row['id'], 'start': 2, 'end': 2}),
                    text('Read it.')]

    assert 'event: done' in client.post(f'/chats/{chat}/messages', headers=headers,
                                        json={'text': 'Keep this', 'attachments': [row['id']]}).text
    assert 'event: done' in client.post(f'/chats/{chat}/messages', headers=headers,
                                        json={'text': 'Read line two'}).text
    prompt = fake.requests[1]['messages'][0]['content']
    assert row['id'] in prompt and sentinel not in prompt
    assert any(message.get('role') == 'tool' and sentinel in message['content']
               for message in fake.requests[2]['messages'])
    users = [m['content'] for m in client.get(f'/chats/{chat}', headers=headers).json()['messages']
             if m['role'] == 'user']
    assert users == ['Keep this', 'Read line two']


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
    # The extracted text follows the turn through its tool round, but the
    # stored user message is only what the user typed.
    assert any(message.get('role') == 'user' and '1: first' in (message.get('content') or '')
               and '3: third' in (message.get('content') or '')
               for message in fake.requests[1]['messages'])
    assert any(m.get('role') == 'tool' and m['content'] == 'lines 2-2 of 3\n2: second' for m in fake.requests[1]['messages'])
    users = [m['content'] for m in client.get(f'/chats/{chat}', headers=headers).json()['messages']
             if m['role'] == 'user']
    assert users == ['Read line two']


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
