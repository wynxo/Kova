import asyncio
import threading
from datetime import datetime

import httpx
import pytest

from kova.agent import Agent, select_tools, trim_history
from kova.config import load_config
from kova.memory import Memory
from kova.store import Store
from kova.tools import Tool, ToolContext, build_tools
from tests.fake_model import FakeModel, text, tool


@pytest.fixture
def setup(tmp_path):
    cfg = load_config(tmp_path)
    store = Store(tmp_path / 'db.sqlite')
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    chat = store.create_chat()
    return cfg, store, memory, tools, chat['id']


async def run(setup, scripts, message='hello'):
    cfg, store, memory, tools, chat_id = setup
    fake = FakeModel(scripts)
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat_id, message)]
    return events, fake


def _sent_tools(fake, request=0):
    """The tool names of one request the model received."""
    return [t['function']['name'] for t in fake.requests[request].get('tools', [])]


def test_select_tools():
    tools = {name: Tool(name, 'test', {'type': 'object'}, lambda args: 'ok')
             for name in ('web_search', 'web_fetch', 'write_file', 'memory_save')}
    assert list(select_tools('work', True, tools)) == list(tools)
    assert list(select_tools('work', False, tools)) == ['write_file', 'memory_save']
    assert list(select_tools('chat', True, tools)) == ['web_search', 'web_fetch']
    assert select_tools('chat', False, tools) == {}


async def test_work_mode_sends_all_tools(setup):
    _, fake = await run(setup, [text('Hi')])
    assert _sent_tools(fake) == ['web_search', 'web_fetch', 'list_files', 'read_file',
                                 'write_file', 'search_files', 'edit_file', 'reminder_add', 'reminder_list',
                                 'memory_save', 'memory_search', 'system_info', 'run_command', 'update_plan']
    assert 'Current mode: work' in fake.requests[0]['messages'][0]['content']


async def test_chat_mode_sends_only_web_tools(setup):
    setup[1].update_chat(setup[4], mode='chat')
    _, fake = await run(setup, [text('Hi')])
    assert _sent_tools(fake) == ['web_search', 'web_fetch']
    prompt = fake.requests[0]['messages'][0]['content']
    assert 'Current mode: chat' in prompt
    assert 'chat mode' in prompt


async def test_chat_mode_without_web_sends_no_tools(setup):
    setup[1].update_chat(setup[4], mode='chat', web=False)
    _, fake = await run(setup, [text('Hi')])
    assert 'tools' not in fake.requests[0]


async def test_work_mode_without_web_keeps_other_tools(setup):
    setup[1].update_chat(setup[4], web=False)
    _, fake = await run(setup, [text('Hi')])
    assert 'web_search' not in _sent_tools(fake) and 'web_fetch' not in _sent_tools(fake)
    assert 'write_file' in _sent_tools(fake) and 'memory_save' in _sent_tools(fake)


async def test_chat_mode_remembers_but_never_saves(setup):
    setup[2].save('Dog', 'Rex is my dog')
    setup[1].update_chat(setup[4], mode='chat')
    _, fake = await run(setup, [text('Hi')], 'dog')
    prompt = fake.requests[0]['messages'][0]['content']
    assert '## Dog\nRex is my dog' in prompt  # memory is still read into the prompt
    assert 'memory_save' not in prompt


async def test_chat_mode_rejects_work_tools(setup):
    setup[1].update_chat(setup[4], mode='chat')
    cfg, store, memory, tools, chat_id = setup
    fake = FakeModel([tool('memory_save', {'title': 'Dog', 'body': 'Rex'}), text('Ok')])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    events = [event async for event in agent.run_turn(chat_id, 'hello')]
    assert any(e.get('summary') == 'Error: unknown tool memory_save' and not e['ok']
               for e in events if e['type'] == 'tool_result')
    assert memory.get('dog') is None  # nothing was actually saved


async def test_plain_answer(setup):
    events, _ = await run(setup, [text('Hello!')])
    assert events[-1]['type'] == 'done'
    assert setup[1].get_messages(setup[4]) == [
        {'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'Hello!'}]
    assert setup[1].get_chat(setup[4])['title'] == 'hello'


async def test_tool_loop(setup):
    events, fake = await run(setup, [tool('memory_save', {'title': 'Dog', 'body': 'Rex'}), text('Saved')])
    assert setup[2].get('dog').body == 'Rex'
    assert any(m['role'] == 'tool' and 'Saved memory' in m['content'] for m in fake.requests[1]['messages'])
    assert any(e['type'] == 'tool_result' and e['ok'] for e in events)


async def test_bad_tool_args_reported_to_model(setup):
    script = [{'tool_calls': [{'index': 0, 'id': 'x', 'function': {'name': 'memory_save', 'arguments': '{'}}]}]
    events, fake = await run(setup, [script, text('Oops')])
    assert any(e.get('summary') == 'Error: arguments were not valid JSON' and not e['ok'] for e in events if e['type'] == 'tool_result')
    assert fake.requests[1]['messages'][-1]['content'] == 'Error: arguments were not valid JSON'


async def test_unknown_tool(setup):
    events, _ = await run(setup, [tool('missing', {}), text('Oops')])
    assert any(e.get('summary') == 'Error: unknown tool missing' and not e['ok'] for e in events if e['type'] == 'tool_result')


async def test_max_steps_then_final_answer(setup):
    events, fake = await run(setup, [tool('missing', {}) for _ in range(11)] + [text('Done')])
    assert len(fake.requests) == 11
    assert 'tools' not in fake.requests[-1]
    assert events[-1]['type'] == 'done'


async def test_model_down_yields_error_and_keeps_user_message(setup):
    def fail(request):
        raise httpx.ConnectError('down', request=request)
    cfg, store, memory, tools, chat_id = setup
    agent = Agent(cfg, store, memory, tools, transport=httpx.MockTransport(fail))
    events = [event async for event in agent.run_turn(chat_id, 'hello')]
    assert events[-1]['type'] == 'error'
    assert store.get_messages(chat_id) == [{'role': 'user', 'content': 'hello'}]


async def test_system_prompt_identity(setup):
    setup[2].save('Dog', 'Rex is my dog')
    _, fake = await run(setup, [text('Hi')], 'dog')
    prompt = fake.requests[0]['messages'][0]['content']
    assert prompt.startswith('You are Kova')
    assert '## Dog\nRex is my dog' in prompt


def test_trim_history_keeps_latest_user():
    messages = [{'role': 'user' if i % 2 == 0 else 'tool', 'content': str(i) * 100} for i in range(20)]
    trimmed = trim_history(messages, 100, 0)
    assert messages[18] in trimmed
    assert trimmed[0]['role'] != 'tool'


async def test_sync_tool_does_not_block_event_loop_and_awaitable_is_awaited(setup):
    started, release = threading.Event(), threading.Event()
    def blocking(args):
        started.set()
        assert release.wait(2)
        async def result():
            return 'completed'
        return result()
    setup[3]['blocking'] = Tool('blocking', 'test', {'type': 'object'}, blocking)
    cfg, store, memory, tools, chat_id = setup
    fake = FakeModel([tool('blocking', {}), text('Done')])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    async def collect():
        return [event async for event in agent.run_turn(chat_id, 'hello')]
    task = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(started.wait, 1)
    finally:
        release.set()
    events = await task
    assert any(e.get('summary') == 'completed' and e['ok'] for e in events if e['type'] == 'tool_result')


def test_repair_tool_pairs_fills_missing_results():
    from kova.agent import repair_tool_pairs

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "user", "content": "again"},
    ]
    fixed = repair_tool_pairs(msgs)
    assert [m.get("tool_call_id") for m in fixed if m["role"] == "tool"] == ["a", "b"]
    assert fixed[-1] == {"role": "user", "content": "again"}
    assert repair_tool_pairs(fixed) == fixed


@pytest.mark.real_default
async def test_fresh_install_asks_for_setup_instead_of_calling_a_model(tmp_path):
    from kova.agent import Agent
    from kova.config import load_config
    from kova.memory import Memory
    from kova.store import Store

    cfg = load_config(tmp_path)
    agent = Agent(cfg, Store(tmp_path / "kova.db"), Memory(tmp_path / "memory"), {})
    chat = agent.store.create_chat()
    events = [e async for e in agent.run_turn(chat["id"], "hi")]
    assert events == [{"type": "error", "message": "No model is set up yet. Run: kova setup"}]
