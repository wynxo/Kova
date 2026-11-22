"""A tiny non-blocking fan-out event bus for the server's background tasks."""

import asyncio

import pytest

from kova.events import EventBus
from tests.fake_model import FakeModel, text, tool


def test_publish_fans_out_to_every_subscriber():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    event = {"type": "run", "id": 7}
    bus.publish(event)
    assert q1.get_nowait() == event
    assert q2.get_nowait() == event


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    queue = bus.subscribe()
    bus.unsubscribe(queue)
    bus.publish({"a": 1})
    assert queue.empty()


def test_publish_with_no_subscribers_is_a_noop():
    bus = EventBus()
    bus.publish({"a": 1})  # must not raise


def test_unsubscribe_unknown_queue_is_safe():
    bus = EventBus()
    bus.unsubscribe(asyncio.Queue())  # not subscribed; ValueError swallowed


def test_duplicate_subscribe_delivers_to_both():
    bus = EventBus()
    a = bus.subscribe()
    b = bus.subscribe()
    bus.publish({"n": 1})
    assert a.get_nowait() == {"n": 1}
    assert b.get_nowait() == {"n": 1}


async def test_publish_delivers_to_a_running_consumer():
    bus = EventBus()
    queue = bus.subscribe()

    async def consumer():
        return await queue.get()

    task = asyncio.create_task(consumer())
    bus.publish({"k": "v"})
    assert await task == {"k": "v"}


def test_conversation_lifecycle_is_scoped_and_broadcast():
    from kova.events import ConversationStates
    bus = EventBus()
    queue = bus.subscribe()
    states = ConversationStates(bus)
    for event, expected in [('turn_start', 'working'), ('approval', 'blocked-on-approval'),
                            ('approval_resolved', 'working'), ('turn_end', 'done')]:
        states.transition('chat-a', event)
        assert states.get('chat-a') == expected
        assert queue.get_nowait() == {'type': 'conversation_state', 'chat_id': 'chat-a', 'state': expected}
    assert states.get('chat-b') == 'done'


# ------------------------------------------------------- bot authors (task 1.4)
def _agent(tmp_path, fake):
    from datetime import datetime

    from kova.agent import Agent
    from kova.config import load_config
    from kova.memory import Memory
    from kova.store import Store
    from kova.tools import ToolContext, build_tools

    cfg = load_config(tmp_path)
    cfg.files_path = tmp_path / "Kova Files"
    cfg.files_path.mkdir()
    store = Store(tmp_path / "db.sqlite")
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    return cfg, store, agent, store.create_chat()["id"]


def _bot():
    from kova.bots import BotDefinition, BotModel, BotPermissions

    return BotDefinition(id="researcher", name="Researcher", handle="researcher",
                         persona="You research.",
                         model=BotModel(name=""),
                         permissions=BotPermissions(tools=("memory_*",)))


async def test_a_bot_turn_tags_every_event_and_every_row_with_its_author(tmp_path):
    from kova.actors import Actor

    fake = FakeModel([tool("memory_search", {"query": "x"}), text("Found it.")])
    cfg, store, agent, chat_id = _agent(tmp_path, fake)
    actor = Actor.for_chat(store.get_chat(chat_id), _bot(), files_path=cfg.files_path)
    events = [event async for event in agent.run_turn(chat_id, "hello", actor=actor)]
    assert events and all(event.get("author") == "researcher" for event in events)
    turn_ids = {event.get("bot_turn_id") for event in events}
    assert len(turn_ids) == 1 and None not in turn_ids
    assert {event["type"] for event in events} >= {"text", "tool_call", "tool_result", "done"}
    # A retry or a second answer gets its own bot_turn_id.
    turn_id = turn_ids.pop()
    saved = store.get_messages(chat_id, include_message_meta=True)
    assert saved[0].get("author") is None, "the user's row is never the bot's"
    for row in saved[1:]:
        assert row["author"] == "researcher" and row["author_name"] == "Researcher"
        assert row["bot_turn_id"] == turn_id
    assert {row["role"] for row in saved[1:]} == {"assistant", "tool"}


async def test_a_default_turn_carries_no_author_at_all(tmp_path):
    fake = FakeModel([text("Hi")])
    cfg, store, agent, chat_id = _agent(tmp_path, fake)
    events = [event async for event in agent.run_turn(chat_id, "hello")]
    assert events and all("author" not in event and "bot_turn_id" not in event for event in events)
    assert store.get_messages(chat_id, include_message_meta=True)[-1].get("author") is None


async def test_a_hand_off_hop_is_stored_with_the_bot_rows(tmp_path):
    from kova.actors import Actor

    fake = FakeModel([text("Done.")])
    cfg, store, agent, chat_id = _agent(tmp_path, fake)
    actor = Actor.for_chat(store.get_chat(chat_id), _bot(), files_path=cfg.files_path)
    [event async for event in agent.run_turn(chat_id, "hello", actor=actor, hop=2)]
    reply = store.get_messages(chat_id, include_message_meta=True)[-1]
    assert reply["hop"] == 2
