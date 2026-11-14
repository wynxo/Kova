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
    assert not list((cfg.home / 'attachments' / chats[0]).iterdir())


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
