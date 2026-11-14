"""A pending approval survives a page reload: it can be listed and answered (#112)."""

import threading
import time

import pytest

from kova.client import KovaClient, KovaError
from kova.permissions import Approvals
from tests.fake_model import text, tool
from tests.test_agent_core import python_command, real_server


def _wait_for(events, kind):
    deadline = time.monotonic() + 10
    while not any(event['type'] == kind for event in events):
        assert time.monotonic() < deadline, f'the turn never sent {kind}'
        time.sleep(.01)
    return next(event for event in events if event['type'] == kind)


async def test_pending_for_lists_only_open_items_of_one_chat():
    approvals = Approvals(timeout=5)
    first = approvals.create('a', 'run_command', {'command': 'rm -rf build'})
    question = approvals.create('a', 'ask_user', {'question': 'Keep logs?'})
    approvals.create('b', 'run_command', {'command': 'pwd'})
    listed = approvals.pending_for('a')
    # The listing is the same serializer as the stream event, so the card matches.
    assert listed == [first, question]
    assert question['type'] == 'question' and first['type'] == 'approval'
    listed[0]['args']['command'] = 'tampered'
    assert approvals.pending_for('a')[0]['args']['command'] == 'rm -rf build'
    approvals.answer('a', first['id'], 'deny')
    assert [event['id'] for event in approvals.pending_for('a')] == [question['id']]
    assert (await approvals.wait(first['id']))['decision'] == 'deny'
    assert approvals.pending_for('nobody') == []


def test_reloaded_window_lists_and_answers_a_pending_approval(tmp_path, no_sandbox):
    command = python_command("print('approved after reload')")
    with real_server(tmp_path, [tool('run_command', {'command': command}), text('Finished')]) as (client, cfg, fake, app):
        chat = client.create_chat()
        events = []
        # The first window starts the turn; its stream is the only place the card was sent.
        thread = threading.Thread(target=lambda: events.extend(client.stream_message(chat['id'], 'run it')),
                                  daemon=True)
        thread.start()
        approval = _wait_for(events, 'approval')
        # A reloaded (or second) window has none of that stream.
        reloaded = KovaClient(client.base_url, client.token)
        try:
            assert reloaded.get_chat(chat['id'])['state'] == 'blocked-on-approval'
            pending = reloaded.pending_approvals(chat['id'])
            assert pending == [approval]
            reloaded.answer_approval(chat['id'], pending[0]['id'], 'allow')
            thread.join(10)
            assert not thread.is_alive()
            assert reloaded.pending_approvals(chat['id']) == []
        finally:
            reloaded.http.close()
        assert any(e['type'] == 'tool_result' and e['ok'] and 'approved after reload' in e['output'] for e in events)
        assert events[-1]['type'] == 'done'


def test_pending_questions_are_listed_too(tmp_path):
    with real_server(tmp_path, [tool('ask_user', {'question': 'Proceed?'}, 'q1'), text('Done')]) as (client, cfg, fake, app):
        chat = client.create_chat()
        events = []
        thread = threading.Thread(target=lambda: events.extend(client.stream_message(chat['id'], 'go')), daemon=True)
        thread.start()
        question = _wait_for(events, 'question')
        assert client.pending_approvals(chat['id']) == [question]
        client.answer_approval(chat['id'], question['id'], 'allow', 'yes')
        thread.join(10)
        assert not thread.is_alive() and events[-1]['type'] == 'done'


def test_pending_approvals_needs_auth_and_a_real_chat(tmp_path):
    with real_server(tmp_path, [text('hi')]) as (client, cfg, fake, app):
        chat = client.create_chat()
        assert client.pending_approvals(chat['id']) == []
        with pytest.raises(KovaError) as missing:
            client.pending_approvals('no-such-chat')
        assert missing.value.status == 404
        anonymous = client.http.get(f"/chats/{chat['id']}/approvals")
        assert anonymous.status_code == 401
