"""A tiny non-blocking fan-out event bus for the server's background tasks."""

import asyncio

import pytest

from kova.events import EventBus


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
