"""The loop guard stops repeated failures, not distinct actions or progress."""
import json

import pytest

from kova.agent import Agent
from kova.config import load_config
from kova.memory import Memory
from kova.store import Store
from kova.tools import Tool, ToolError
from tests.fake_model import FakeModel, text, tool


async def run(tmp_path, scripts, implementation):
    cfg = load_config(tmp_path)
    store = Store(tmp_path / 'test.db')
    chat = store.create_chat()
    fake = FakeModel(scripts)
    agent = Agent(cfg, store, Memory(cfg.memory_path), {
        'probe': Tool('probe', 'Probe', {'type': 'object'}, implementation),
    }, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat['id'], 'go')]
    return events, fake, store.get_messages(chat['id'])


def fail(_args):
    raise ToolError('same failure')


async def test_three_identical_errors_stop_before_fourth_model_request(tmp_path):
    events, fake, messages = await run(tmp_path, [tool('probe', {'a': 1, 'b': 2}),
        tool('probe', {'b': 2, 'a': 1}), tool('probe', {'a': 1, 'b': 2}), text('too late')], fail)
    assert len(fake.requests) == 3
    assert len([e for e in events if e['type'] == 'tool_result']) == 3
    assert events[-1]['reason'] == 'loop_guard'
    assert 'Loop guard' in messages[-1]['content']
    assert '3 identical tool errors' in messages[-1]['content']


async def test_distinct_arguments_do_not_count_as_identical_errors(tmp_path):
    events, fake, _ = await run(tmp_path, [tool('probe', {'n': n}) for n in range(4)] + [text('done')], fail)
    assert len(fake.requests) == 5
    assert events[-1].get('reason') != 'loop_guard'


async def test_success_resets_the_failure_streak(tmp_path):
    def probe(args):
        if args.get('ok'):
            return 'progress'
        return fail(args)
    scripts = [tool('probe', {}), tool('probe', {}), tool('probe', {'ok': True}),
               tool('probe', {}), tool('probe', {}), text('recovered')]
    events, fake, _ = await run(tmp_path, scripts, probe)
    assert len(fake.requests) == 6
    assert events[-1].get('reason') != 'loop_guard'


async def test_guard_skips_remaining_calls_and_preserves_tool_pairs(tmp_path):
    calls = [{'index': i, 'id': str(i), 'type': 'function',
              'function': {'name': 'probe', 'arguments': json.dumps({'ok': i == 3})}}
             for i in range(4)]
    executed = []
    def probe(args):
        executed.append(args)
        return fail(args)
    events, fake, messages = await run(tmp_path, [[{'tool_calls': calls}], text('too late')], probe)
    assert len(executed) == 3
    assert len(fake.requests) == 1
    assert [m['tool_call_id'] for m in messages if m['role'] == 'tool'] == ['0', '1', '2', '3']
    assert events[-1]['reason'] == 'loop_guard'
