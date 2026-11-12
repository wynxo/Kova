"""A tiny fan-out event bus shared across the server's background tasks."""

from __future__ import annotations

import asyncio
import threading


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
