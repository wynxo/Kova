"""A tiny fan-out event bus shared across the server's background tasks."""

from __future__ import annotations

import asyncio
import threading
from typing import TypedDict


class ToolCallDelta(TypedDict):
    """Display-only cumulative argument snapshot, keyed by index within a model pass.

    The arguments string may be incomplete JSON. Only the subsequent tool_call
    event is executable. Consumers that do not display previews may ignore this.
    """

    type: str
    index: int
    id: str
    name: str
    arguments: str



class EventBus:
    """Deliver a dict to every subscriber without blocking the publisher."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass

    def publish(self, event: dict) -> None:
        with self._lock:
            queues = list(self._subscribers)
        for queue in queues:
            queue.put_nowait(event)


class ConversationStates:
    """In-memory lifecycle state; a restarted server has no active turns."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.states: dict[str, str] = {}

    def get(self, chat_id: str) -> str:
        return self.states.get(chat_id, "done")

    def transition(self, chat_id: str, event: str) -> None:
        state = {"turn_start": "working", "approval": "blocked-on-approval",
                 "question": "blocked-on-approval", "approval_resolved": "working",
                 "question_resolved": "working", "turn_end": "done"}.get(event)
        if state is not None:
            self.states[chat_id] = state
            self.bus.publish({"type": "conversation_state", "chat_id": chat_id, "state": state})
