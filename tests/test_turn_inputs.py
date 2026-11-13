"""Durable queue and steering behavior across the API and agent loop."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from kova.config import ensure_token, load_config
from kova.server import create_app
from kova.store import Store
from kova.tools import Tool
from tests.fake_model import FakeModel, text, tool


def test_pending_queue_survives_reopening_database(tmp_path):
    path = tmp_path / 'test.db'
    store = Store(path)
    chat = store.create_chat()
    item = store.enqueue_input(chat['id'], 'later', 'queue', None)
    reopened = Store(path)
    assert reopened.pending_inputs(chat['id']) == [item]
    applied = reopened.apply_next_input(chat['id'], 'queue')
    assert applied['id'] == item['id']
    assert reopened.apply_next_input(chat['id'], 'queue') is None
    assert reopened.get_messages(chat['id'])[-1]['content'] == '[Queued] later'


def test_queue_and_steer_reach_the_right_turn_and_remain_labelled(tmp_path):
    cfg = load_config(tmp_path)
    entered, release = threading.Event(), threading.Event()
    async def wait(_args):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return 'ready'
    fake = FakeModel([tool('wait', {}), text('steered answer'), text('queued answer')])
    app = create_app(cfg, transport=fake.transport)
    headers = {'Authorization': f'Bearer {ensure_token(cfg.home)}'}
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        app.state.agent.tools['wait'] = Tool('wait', 'Wait', {'type': 'object'}, wait)
        chat_id = client.post('/chats', headers=headers).json()['id']
        route = f'/chats/{chat_id}'
        running = pool.submit(client.post, route + '/messages', headers=headers, json={'text': 'start'})
        try:
            assert entered.wait(5)
            queued = client.post(route + '/inputs', headers=headers, json={'text': 'later', 'kind': 'queue'})
            assert queued.status_code == 200
            steer = client.post(route + '/inputs', headers=headers, json={'text': 'use another approach', 'kind': 'steer'})
            assert steer.status_code == 200
            reloaded = client.get(route, headers=headers).json()
            assert reloaded['running']
            assert [(i['kind'], i['text']) for i in reloaded['pending_inputs']] == [
                ('queue', 'later'), ('steer', 'use another approach')]
        finally:
            release.set()
        response = running.result(10)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        streamed = ''.join(e['text'] for e in events if e['type'] == 'text')
        assert streamed == 'steered answerqueued answer'
        current = fake.requests[1]['messages']
        assert any(m.get('content') == '[Steer] use another approach' for m in current)
        assert not any(m.get('content') == '[Queued] later' for m in current)
        assert any(m.get('content') == '[Queued] later' for m in fake.requests[2]['messages'])
        saved = client.get(route, headers=headers).json()
        assert not saved['pending_inputs']
        assert [m['content'] for m in saved['messages'] if m['role'] == 'user'] == [
            'start', '[Steer] use another approach', '[Queued] later']
        assert client.post(route + '/inputs', headers=headers, json={'text': 'too late', 'kind': 'steer'}).status_code == 409


def test_tui_sends_durable_queue_and_steer_without_starting_a_second_local_turn(tmp_path):
    import io
    import time
    from kova.client import KovaClient
    from kova.tui.app import ChatApp

    cfg = load_config(tmp_path)
    entered, release = threading.Event(), threading.Event()
    async def wait(_args):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return 'ready'
    fake = FakeModel([tool('wait', {}), text('changed'), text('later reply')])
    server = create_app(cfg, transport=fake.transport)
    with TestClient(server) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        server.state.agent.tools['wait'] = Tool('wait', 'Wait', {'type': 'object'}, wait)
        chat_id = client.create_chat()['id']
        app = ChatApp(client, chat_id, out=io.StringIO(), color=False, clear=False)
        app.refresh_chat()
        app.submit('start')
        try:
            assert entered.wait(5)
            app.submit('later')
            app.submit('/steer change direction')
            assert [(i['kind'], i['text']) for i in client.get_chat(chat_id)['pending_inputs']] == [
                ('queue', 'later'), ('steer', 'change direction')]
        finally:
            release.set()
        deadline = time.monotonic() + 10
        while app.job is not None and time.monotonic() < deadline:
            app._pump()
            time.sleep(0.01)
        assert app.job is None and not app.queued
        app.screen.close()
        transcript = app.out.getvalue()
        assert '[Steer] change direction' in transcript
        assert '[Queued] later' in transcript
        assert len(fake.requests) == 3
