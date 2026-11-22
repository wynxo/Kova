"""Independent read-only tool calls of one step run at the same time (#192)."""

import asyncio
import json
from contextlib import aclosing
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


# ------------------------------------------------- stopping one call (#192)
async def test_one_running_call_can_be_stopped_while_the_others_finish():
    release = asyncio.Event()
    started = []

    async def hang(args):
        started.append('hang')
        await asyncio.sleep(30)

    async def quick(args):
        started.append('quick')
        await release.wait()
        return 'fast result'

    batch = [(0, {'id': 'slow', 'name': 'web_fetch'}, Tool('web_fetch', '', {}, hang), {}),
             (1, {'id': 'fast', 'name': 'web_search'}, Tool('web_search', '', {}, quick), {})]
    results: dict = {}
    controls: dict = {}
    events = []

    async def consume():
        async for event in parallel.run_group(batch, 'm', results, controls=controls):
            events.append(event)

    task = asyncio.create_task(consume())
    while len(started) < 2:
        await asyncio.sleep(0)
    assert set(controls) == {'slow', 'fast'}
    assert controls['slow']() is True
    assert controls['slow']() is False, 'a second stop of the same call does nothing'
    release.set()
    await task

    assert results[0] == (parallel.STOPPED_RESULT, False)
    assert results[1] == ('fast result', True), 'the other call is untouched'
    stopped = next(e for e in events if e['type'] == 'task_error')
    assert stopped['id'] == 'slow' and stopped['stopped'] is True
    assert next(e for e in events if e['type'] == 'task_done' and e['id'] == 'fast')['ok'] is True
    assert events[-1]['type'] == 'task_done' and events[-1]['id'] == 'group-slow'
    assert events[-1]['failed'] == 1 and events[-1]['done'] == 2
    assert controls == {}, 'the controls are removed when the group ends'


async def test_a_call_still_waiting_for_its_turn_never_starts_when_stopped():
    started = []
    release = asyncio.Event()

    async def run(args):
        started.append(args['n'])
        await release.wait()
        return f"ran {args['n']}"

    tool = Tool('read_file', '', {}, run)
    batch = [(i, {'id': f'c{i}', 'name': 'read_file'}, tool, {'n': i}) for i in range(3)]
    results: dict = {}
    controls: dict = {}
    consumer = asyncio.create_task(_drain(parallel.run_group(batch, 'm', results, limit=1, controls=controls)))
    while not started:
        await asyncio.sleep(0)
    assert controls['c2']() is True
    release.set()
    await consumer
    assert started == [0, 1], 'the stopped call never ran'
    assert results[2] == (parallel.STOPPED_RESULT, False)
    assert results[0] == ('ran 0', True) and results[1] == ('ran 1', True)


async def _drain(stream):
    return [event async for event in stream]


async def test_stopping_a_finished_or_unknown_call_does_nothing():
    controls: dict = {}
    batch = [(0, {'id': 'a', 'name': 'read_file'}, Tool('read_file', '', {}, lambda a: 'x'), {}),
             (1, {'id': 'b', 'name': 'list_files'}, Tool('list_files', '', {}, lambda a: 'y'), {})]
    seen = []
    async for event in parallel.run_group(batch, 'm', {}, controls=controls):
        seen.append(event)
        if event['type'] == 'task_done' and event['id'] == 'a':
            assert controls['a']() is False, 'a finished call cannot be stopped'
    assert controls == {}
    assert all(not event.get('stopped') for event in seen)


async def test_the_agent_stops_one_call_and_the_model_carries_on(setup):
    cfg, store, memory, tools, chat_id = setup
    started = asyncio.Event()

    async def hang(args):
        started.set()
        await asyncio.sleep(30)

    tools.update({'recall': Tool('recall', '', {'type': 'object'}, hang),
                  'memory_list': Tool('memory_list', '', {'type': 'object'}, lambda a: 'notes')})
    fake = FakeModel([calls(('recall', {'query': 'a'}), ('memory_list', {})), text('Done without it')])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    assert agent.stop_task(chat_id, 'call_0') is False, 'nothing is running yet'
    events = []

    async def consume():
        async for event in agent.run_turn(chat_id, 'look'):
            events.append(event)
            if event['type'] == 'task_progress' and event['id'] == 'call_0' and event.get('status') == 'running':
                await started.wait()
                assert agent.stop_task('other-chat', 'call_0') is False, 'only this chat\'s calls'
                assert agent.stop_task(chat_id, 'call_0') is True

    await asyncio.wait_for(consume(), 5)
    assert events[-1]['type'] == 'done', 'the turn was not stopped'
    reply = next(m for m in fake.requests[1]['messages'] if m['role'] == 'tool' and m['tool_call_id'] == 'call_0')
    assert reply['content'] == parallel.STOPPED_RESULT
    other = next(m for m in fake.requests[1]['messages'] if m['role'] == 'tool' and m['tool_call_id'] == 'call_1')
    assert other['content'] == 'notes'
    result = next(e for e in events if e['type'] == 'tool_result' and e['id'] == 'call_0')
    assert result['ok'] is False
    assert agent.task_controls == {}, 'no control outlives its group'


def test_the_task_stop_route_and_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from kova.client import KovaClient
    from kova.config import ensure_token
    from kova.server import create_app

    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config()
    token = ensure_token(cfg.home)
    with TestClient(create_app(cfg)) as http:
        auth = {'Authorization': f'Bearer {token}'}
        chat = http.post('/chats', headers=auth).json()['id']
        url = f'/chats/{chat}/tasks/call_0/stop'
        assert http.post(url).status_code == 401
        assert http.post('/chats/missing/tasks/call_0/stop', headers=auth).status_code == 404
        assert http.post(url, headers=auth).json() == {'stopped': False}
        stops = []
        http.app.state.agent.task_controls[chat] = {'call_0': lambda: stops.append('call_0') or True}
        assert http.post(url, headers=auth).json() == {'stopped': True}
        assert stops == ['call_0']
        client = KovaClient('http://127.0.0.1:7700', token, http=http)
        assert client.stop_task(chat, 'call_0') is True
        assert client.stop_task(chat, 'call_9') is False


@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_child_analysis_requires_approval_and_has_no_tools_or_history(setup, decision):
    cfg, store, memory, tools, chat_id = setup
    task = "Summarize this tradeoff: speed versus clarity."
    scripts = [calls(("delegate_analysis", {"task": task}))]
    if decision == "allow":
        scripts.append(text("Favor clarity for maintenance."))
    fake = FakeModel([*scripts, text("Done")])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    agent.approvals.rules.append(Rule("allow", "delegate_analysis"))
    events = []
    async for event in agent.run_turn(chat_id, "Delegate some analysis; private parent context."):
        events.append(event)
        if event["type"] == "approval":
            assert len(fake.requests) == 1
            assert task in event["summary"]
            agent.approvals.answer(chat_id, event["id"], decision)
    assert any(event["type"] == "approval" for event in events)
    assert len(fake.requests) == (3 if decision == "allow" else 2)
    children = [event for event in events if event["type"] == "task_start"]
    if decision == "deny":
        assert not children
    else:
        assert children[0]["kind"] == "delegated"
        request = fake.requests[1]
        assert not request.get("tools")
        assert request["messages"][-1]["content"] == task
        assert "private parent context" not in json.dumps(request)
        assert any(event.get("summary") == "Favor clarity for maintenance." for event in events)
    assert events[-1]["type"] == "done"


@pytest.mark.parametrize("decision", ["allow", "deny", "stop"])
async def test_child_batch_overlap_approval_order_and_turn_limit(setup, decision):
    import httpx

    cfg, store, memory, tools, chat_id = setup
    tasks = ["First independent analysis", "Second independent analysis",
             "Third independent analysis", "Fourth independent analysis"]
    batch = tuple(("delegate_analysis", {"task": task}) for task in tasks)
    retry = calls(("delegate_analysis", {"task": "Over the turn limit"}))
    retry[0]["tool_calls"][0]["id"] = "retry"
    fake = FakeModel([calls(*batch), retry, text("Done")])
    first_finished = asyncio.Event()
    all_running = asyncio.Event()
    requested = []
    stopped = asyncio.Event()

    async def respond(request):
        payload = json.loads(request.content)
        task = payload["messages"][-1]["content"]
        if payload["messages"][-1]["role"] != "user" or task not in tasks:
            return fake._handle(request)
        assert not payload.get("tools")
        assert len(payload["messages"]) == 2
        requested.append(task)
        if len(requested) == len(tasks):
            all_running.set()
        try:
            await asyncio.wait_for(all_running.wait(), 2)
            if task == tasks[0]:
                await asyncio.wait_for(first_finished.wait(), 2)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return httpx.Response(200, text=f'data: {json.dumps({"choices": [{"delta": {"content": task}}]})}\n\ndata: [DONE]\n\n')

    agent = Agent(cfg, store, memory, tools, transport=httpx.MockTransport(respond))
    events = []
    async with asyncio.timeout(5):
        async for event in agent.run_turn(chat_id, "Analyze some independent tasks."):
            events.append(event)
            if event["type"] == "approval":
                assert not requested
                assert event["args"]["tasks"] == tasks
                for task in tasks:
                    assert task in event["summary"]
                model = cfg.active_model()
                assert model.base_url in event["summary"]
                assert model.model in event["summary"]
                agent.approvals.answer(chat_id, event["id"], "deny" if decision == "deny" else "allow")
            if event["type"] == "task_done" and event["id"] == "call_1":
                if decision == "stop":
                    assert agent.stop_task(chat_id, "call_0")
                else:
                    first_finished.set()

    assert sum(event["type"] == "approval" for event in events) == 1
    results = [event for event in events if event["type"] == "tool_result"]
    assert [event["id"] for event in results] == [*(f"call_{i}" for i in range(4)), "retry"]
    assert "per turn" in results[-1]["output"]
    if decision == "deny":
        assert not requested
        assert not any(event["type"] == "task_start" for event in events)
        assert all("Action denied" in event["output"] for event in results[:-1])
    else:
        assert sorted(requested) == sorted(tasks)
        roots = [event for event in events if event["type"] == "task_start" and event["parent"] is None]
        assert len(roots) == 1 and roots[0]["total"] == 4
        assert [event["output"] for event in results[1:4]] == tasks[1:]
        if decision == "stop":
            assert stopped.is_set()
            assert "user stopped" in results[0]["output"]
        else:
            assert results[0]["output"] == tasks[0]
    assert chat_id not in agent.task_controls
    assert not agent.approvals.pending
    assert events[-1]["type"] == "done"


async def test_child_transcript_streams_before_its_result(setup):
    cfg, store, memory, tools, chat_id = setup
    task = "Compare these approaches."
    fake = FakeModel([
        calls(("delegate_analysis", {"task": task})),
        [{"reasoning_content": "Checking the tradeoffs."}, {"content": "Prefer the simpler option."}],
        text("Done"),
    ])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = []
    async for event in agent.run_turn(chat_id, "Delegate one analysis."):
        events.append(event)
        if event["type"] == "approval":
            agent.approvals.answer(chat_id, event["id"], "allow")

    child = [event for event in events
             if event.get("id") == "call_0" and event["type"] != "tool_call_delta"]
    starts = [event for event in child if event["type"] == "task_start"]
    assert len(starts) == 1, [(event["type"], event.get("summary")) for event in child]
    assert starts[0]["kind"] == "agent"
    assert [(event["type"], event.get("step"), event.get("text")) for event in child] == [
        ("task_start", None, None),
        ("tool_call", None, None),
        ("task_progress", None, None),
        ("task_output", "reasoning", "Checking the tradeoffs."),
        ("task_output", "text", "Prefer the simpler option."),
        ("task_done", None, None),
        ("tool_result", None, None),
    ]
    assert all(event["parent"] == child[0]["parent"] for event in child if event["type"] == "task_output")
    assert child[-1]["output"] == "Prefer the simpler option."
    assert "Checking the tradeoffs." not in json.dumps(fake.requests[-1]["messages"])
    assert "Checking the tradeoffs." not in json.dumps(store.get_messages(chat_id))


async def test_a_delegated_task_can_be_stopped_as_soon_as_its_row_appears(setup):
    cfg, store, memory, tools, chat_id = setup
    fake = FakeModel([
        calls(("delegate_analysis", {"task": "Skip this task"}),
              ("delegate_analysis", {"task": "Run this task"})),
        text("Only the second child ran."), text("Parent finished."),
    ])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = []
    async with asyncio.timeout(5), aclosing(agent.run_turn(chat_id, "Delegate two tasks.")) as stream:
        async for event in stream:
            events.append(event)
            if event["type"] == "approval":
                agent.approvals.answer(chat_id, event["id"], "allow")
            if event["type"] == "task_start" and event["id"] == "call_0":
                assert agent.stop_task(chat_id, "call_0")
                assert not agent.stop_task(chat_id, "call_0")

    assert len(fake.requests) == 3
    assert fake.requests[1]["messages"][-1]["content"] == "Run this task"
    errors = [event for event in events if event["type"] == "task_error"]
    assert [event["id"] for event in errors if event["stopped"]] == ["call_0"]
    assert events[-1]["type"] == "done"
    assert agent.task_controls == {}
