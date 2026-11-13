import pytest
from kova.agent import Agent
from kova.config import load_config
from kova.memory import Memory
from kova.store import Store
from tests.fake_model import FakeModel, text

@pytest.mark.asyncio
async def test_compaction_persists_and_keeps_recent(tmp_path):
    cfg = load_config(tmp_path)
    cfg.models[0].model = 'fake'
    cfg.models[0].context = 3000
    store = Store(tmp_path / 'chat.db')
    cid = store.create_chat(mode='chat')['id']
    for i in range(8):
        store.add_message(cid, 'user', f'old {i} ' + 'x' * 1600)
        store.add_message(cid, 'assistant', 'reply')
    fake = FakeModel([text('Decision: use /tmp/project'), text('answer')])
    agent = Agent(cfg, store, Memory(cfg.memory_path), {}, transport=fake.transport)
    events = [e async for e in agent.run_turn(cid, 'latest')]
    assert any(e['type'] == 'compacted' for e in events)
    assert 'Decision: use /tmp/project' in str(fake.requests[1]['messages'])
    assert fake.requests[1]['messages'][-1]['content'] == 'latest'
    assert 'Decision: use /tmp/project' in str(Store(tmp_path / 'chat.db').get_context(cid))

@pytest.mark.asyncio
async def test_failed_summary_falls_back_to_trim(tmp_path):
    cfg = load_config(tmp_path)
    cfg.models[0].model = 'fake'
    cfg.models[0].context = 3000
    store = Store(tmp_path / 'chat.db')
    cid = store.create_chat(mode='chat')['id']
    for i in range(8):
        store.add_message(cid, 'user', 'old ' + 'x' * 1600)
        store.add_message(cid, 'assistant', 'reply')
    fake = FakeModel([[], text('answer')])
    agent = Agent(cfg, store, Memory(cfg.memory_path), {}, transport=fake.transport)
    events = [e async for e in agent.run_turn(cid, 'latest')]
    assert not any(e['type'] == 'compacted' for e in events)
    assert events[-1]['type'] == 'done'
    assert fake.requests[-1]['messages'][-1]['content'] == 'latest'
    assert store.context_snapshot(cid)[0] is None

@pytest.mark.asyncio
async def test_recompaction_incorporates_one_previous_summary(tmp_path):
    from kova.compact import compact
    from kova.models import ModelClient
    cfg = load_config(tmp_path)
    cfg.models[0].model = 'fake'
    store = Store(tmp_path / 'chat.db')
    cid = store.create_chat()['id']
    for i in range(4):
        store.add_message(cid, 'user', 'message ' + str(i) + 'x' * 200)
        store.add_message(cid, 'assistant', 'reply')
    fake = FakeModel([text('first summary'), text('updated summary')])
    client = ModelClient(cfg.models[0], transport=fake.transport)
    assert await compact(store, cid, client, context_tokens=4000, force=True)
    for i in range(3):
        store.add_message(cid, 'user', 'later ' + str(i) + 'x' * 200)
        store.add_message(cid, 'assistant', 'reply')
    assert await compact(store, cid, client, context_tokens=4000, force=True)
    assert str(fake.requests[1]['messages']).count('first summary') == 1
    context = store.get_context(cid)
    assert str(context).count('updated summary') == 1
    assert 'first summary' not in str(context)
