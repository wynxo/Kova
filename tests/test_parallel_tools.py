"""Independent read-only tool calls of one step run at the same time (#192)."""

import asyncio
import json
from datetime import datetime

import pytest

from kova import parallel
from kova.agent import Agent
from kova.config import load_config
from kova.memory import Memory
from kova.permissions import Rule
from kova.store import Store
from kova.tools import Tool, ToolContext, build_tools
from tests.fake_model import FakeModel, text


def calls(*specs: tuple[str, dict]) -> list[dict]:
    """One model step that asks for several tool calls at once."""
    return [{"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                             "function": {"name": name, "arguments": json.dumps(args)}}]}
            for i, (name, args) in enumerate(specs)]


@pytest.fixture
def setup(tmp_path):
    cfg = load_config(tmp_path)
    store = Store(tmp_path / 'db.sqlite')
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    chat = store.create_chat()
    return cfg, store, memory, tools, chat['id']


def gate(count: int):
    """Async tools that only finish once ``count`` of them are running together.

    Run one after another they would wait forever, so passing proves overlap.
    """
    arrived = {'n': 0}
    together = asyncio.Event()

    def make(name: str, answer: str, delay: float = 0) -> Tool:
        async def run(args):
            arrived['n'] += 1
            if arrived['n'] >= count:
                together.set()
            await asyncio.wait_for(together.wait(), 2)
            await asyncio.sleep(delay)
            return answer
        return Tool(name, name, {'type': 'object'}, run)
    return make


# ------------------------------------------------------------------ runs()
def test_runs_groups_only_consecutive_ready_calls():
    assert parallel.runs([]) == {}
    assert parallel.runs([True]) == {}
    assert parallel.runs([True, False, True]) == {}
    assert parallel.runs([True, True, False, True, True, True]) == {0: 2, 3: 6}


# ------------------------------------------------------------- run_group()
async def test_group_runs_together_and_keeps_request_order():
    make = gate(3)
    # The first call finishes last, so completion order differs from request order.
    tools = [make('web_search', 'one', delay=0.05), make('recall', 'two'), make('memory_list', 'three')]
    batch = [(i, {'id': f'c{i}', 'name': t.name}, t, {}) for i, t in enumerate(tools)]
    results: dict = {}
    events = [e async for e in parallel.run_group(batch, 'local-model', results)]
    assert [results[i] for i in range(3)] == [('one', True), ('two', True), ('three', True)]
    group = events[0]
    assert group == {'type': 'task_start', 'id': 'group-c0', 'parent': None, 'kind': 'parallel',
                     'label': '3 tools in parallel', 'model': 'local-model', 'total': 3}
    children = [e for e in events if e['type'] == 'task_start' and e['parent'] == 'group-c0']
    assert [c['id'] for c in children] == ['c0', 'c1', 'c2']
    assert all(c['model'] == 'local-model' and c['status'] == 'queued' for c in children)
    done = [e['id'] for e in events if e['type'] == 'task_done' and e['parent'] == 'group-c0']
    assert done[-1] == 'c0', 'events follow real completion order'
    assert events[-1]['type'] == 'task_done' and events[-1]['id'] == 'group-c0' and events[-1]['ok']
    progress = [e for e in events if e['type'] == 'task_progress' and e['id'] == 'group-c0']
    assert [p['done'] for p in progress] == [1, 2, 3]


async def test_group_honours_the_concurrency_limit():
    running = {'now': 0, 'peak': 0}

    async def slow(args):
        running['now'] += 1
        running['peak'] = max(running['peak'], running['now'])
        await asyncio.sleep(0.01)
        running['now'] -= 1
        return 'ok'

    tool = Tool('read_file', 'read', {'type': 'object'}, slow)
    batch = [(i, {'id': f'c{i}', 'name': 'read_file'}, tool, {}) for i in range(7)]
    results: dict = {}
    [e async for e in parallel.run_group(batch, 'm', results, limit=3)]
    assert running['peak'] == 3
    assert len(results) == 7


async def test_a_failing_call_is_an_error_result_not_a_crash():
    def broken(args):
        raise ValueError('no such file')
    batch = [(0, {'id': 'a', 'name': 'read_file'}, Tool('read_file', '', {}, broken), {}),
             (1, {'id': 'b', 'name': 'list_files'}, Tool('list_files', '', {}, lambda a: 'x'), {})]
    results: dict = {}
    events = [e async for e in parallel.run_group(batch, 'm', results)]
    assert results[0] == ('Error: no such file', False)
    assert results[1] == ('x', True)
    error = next(e for e in events if e['type'] == 'task_error')
    assert error['id'] == 'a' and error['parent'] == 'group-a' and 'no such file' in error['message']
    assert events[-1]['ok'] is False and events[-1]['failed'] == 1


async def test_stopping_cancels_every_unfinished_call():
    started, cancelled = [], []

    async def hang(args):
        started.append(args['n'])
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(args['n'])
            raise
        return 'never'

    tool = Tool('web_fetch', '', {}, hang)
    batch = [(i, {'id': f'c{i}', 'name': 'web_fetch'}, tool, {'n': i}) for i in range(5)]

    async def consume():
        async for _ in parallel.run_group(batch, 'm', {}, limit=2):
            pass

    task = asyncio.create_task(consume())
    while len(started) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == [0, 1], 'running calls are cancelled'
    assert sorted(started) == [0, 1], 'queued calls never start'


# ------------------------------------------------------------ the agent loop
async def test_agent_runs_read_only_calls_in_parallel(setup):
    cfg, store, memory, tools, chat_id = setup
    make = gate(3)
    tools.update({'web_search': make('web_search', 'found', delay=0.05),
                  'recall': make('recall', 'remembered'),
                  'memory_list': make('memory_list', 'notes')})
    fake = FakeModel([calls(('web_search', {'query': 'a'}), ('recall', {'query': 'b'}),
                            ('memory_list', {})), text('Done')])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [e async for e in agent.run_turn(chat_id, 'look things up')]
    assert events[-1]['type'] == 'done'
    assert any(e['type'] == 'task_start' and e['kind'] == 'parallel' for e in events)
    # The model reads the replies in the order it asked for them.
    replies = [m for m in fake.requests[1]['messages'] if m['role'] == 'tool']
    assert [(m['tool_call_id'], m['content']) for m in replies] == [
        ('call_0', 'found'), ('call_1', 'remembered'), ('call_2', 'notes')]
    results = [e for e in events if e['type'] == 'tool_result']
    assert [e['id'] for e in results] == ['call_0', 'call_1', 'call_2']
    # Every grouped call still has its tool_call, announced before it runs.
    assert [e['id'] for e in events if e['type'] == 'tool_call'] == ['call_0', 'call_1', 'call_2']


async def test_a_write_between_reads_keeps_them_sequential(setup):
    cfg, store, memory, tools, chat_id = setup
    order = []

    def record(name):
        def run(args):
            order.append(name)
            return name
        return Tool(name, name, {'type': 'object'}, run)

    tools.update({'recall': record('recall'), 'memory_list': record('memory_list'),
                  'memory_add': record('memory_add')})
    fake = FakeModel([calls(('recall', {'query': 'a'}), ('memory_add', {'title': 't', 'body': 'b'}),
                            ('memory_list', {})), text('Done')])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [e async for e in agent.run_turn(chat_id, 'hi')]
    assert not any(e['type'].startswith('task_') for e in events)
    assert order == ['recall', 'memory_add', 'memory_list']


async def test_calls_that_would_ask_are_never_grouped(setup):
    cfg, store, memory, tools, chat_id = setup
    external = Tool('web_fetch', '', {'type': 'object'}, lambda a: 'page', external=True)
    tools.update({'web_fetch': external, 'recall': Tool('recall', '', {'type': 'object'}, lambda a: 'r')})
    fake = FakeModel([calls(('web_fetch', {'url': 'https://example.com'}), ('recall', {'query': 'x'})),
                      text('Done')])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    agent.approvals.rules.append(Rule('allow', 'web_fetch'))  # answer the question it does ask
    events = [e async for e in agent.run_turn(chat_id, 'hi')]
    assert not any(e['type'].startswith('task_') for e in events), 'an external tool is never grouped'

    other_chat = store.create_chat()['id']
    tools['web_fetch'] = Tool('web_fetch', '', {'type': 'object'}, lambda a: 'page')
    fake2 = FakeModel([calls(('web_fetch', {'url': 'https://example.com'}), ('recall', {'query': 'x'})),
                       text('Done')])
    agent2 = Agent(cfg, store, memory, tools, transport=fake2.transport)
    agent2.approvals.rules.append(Rule('ask', 'recall'))
    turn = agent2.run_turn(other_chat, 'hi')
    seen = []
    async for event in turn:
        seen.append(event)
        if event['type'] == 'approval':
            break
    await turn.aclose()
    assert seen[-1]['type'] == 'approval' and seen[-1]['tool'] == 'recall'
    assert not any(e['type'].startswith('task_') for e in seen), 'a call a rule asks about is never grouped'


async def test_stopping_a_turn_records_every_grouped_call_as_interrupted(setup):
    cfg, store, memory, tools, chat_id = setup
    started = []

    async def hang(args):
        started.append(1)
        await asyncio.sleep(30)

    tools.update({'recall': Tool('recall', '', {'type': 'object'}, hang),
                  'memory_list': Tool('memory_list', '', {'type': 'object'}, hang)})
    fake = FakeModel([calls(('recall', {'query': 'a'}), ('memory_list', {}))])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)

    async def consume():
        async for _ in agent.run_turn(chat_id, 'hi'):
            pass

    task = asyncio.create_task(consume())
    for _ in range(200):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = [m for m in store.get_messages(chat_id) if m['role'] == 'tool']
    assert [m['tool_call_id'] for m in stored] == ['call_0', 'call_1']
    assert all('stopped' in m['content'] for m in stored)


def test_terminal_pairs_results_with_their_call_by_id():
    from kova import ui
    turn = ui._Turn(False, False)
    turn.accept({'type': 'tool_call', 'id': 'a', 'name': 'read_file', 'args': {'path': 'one'}})
    turn.accept({'type': 'tool_call', 'id': 'b', 'name': 'read_file', 'args': {'path': 'two'}})
    turn.accept({'type': 'task_done', 'id': 'a', 'parent': 'g', 'ok': True})  # ignored by the terminal
    turn.accept({'type': 'tool_result', 'id': 'a', 'name': 'read_file', 'ok': True, 'summary': 'first'})
    turn.accept({'type': 'tool_result', 'id': 'b', 'name': 'read_file', 'ok': False, 'summary': 'second'})
    first, second = [block for block in turn.blocks if block.kind == 'tool']
    assert (first.ok, first.chunks) == (True, ['first'])
    assert (second.ok, second.chunks) == (False, ['second'])
