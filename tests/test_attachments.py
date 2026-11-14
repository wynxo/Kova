"""Local extraction, lifecycle, ownership and vision wire contract."""
import hashlib
import io
from pathlib import Path
import zipfile

import pytest

from kova import attachments
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
    assert len(paths) == 1 and paths[0].stat().st_mode & 0o777 == 0o600
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
