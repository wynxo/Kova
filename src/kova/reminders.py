"""Reminder loop: fire due reminders and scheduled jobs, push events.

One loop drives both: a due reminder publishes a ``reminder`` event and shows a
desktop toast, and a due job is handed to :class:`kova.scheduler.Scheduler`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime

from kova.events import EventBus
from kova.scheduler import Scheduler
from kova.store import Store

log = logging.getLogger(__name__)


def desktop_notify(text: str) -> None:
    """Show a desktop notification via ``notify-send`` if it exists.

    Never raises: a missing binary or a failed launch is silently ignored.
    """
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.Popen(["notify-send", "Kova", text])
    except Exception:  # pragma: no cover - defensive, never raises
        log.exception("desktop notification failed")


class ReminderLoop:
    """Fire due reminders and due jobs on an interval, one notification each.

    ``scheduler`` is optional: without it this loop only fires one-shot
    reminders, exactly as before jobs existed.
    """

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        now: Callable[[], datetime],
        notify: Callable[[str], None] = desktop_notify,
        interval: float = 15,
        scheduler: Scheduler | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.now = now
        self.notify = notify
        self.interval = interval
        self.scheduler = scheduler

    def tick(self) -> list[dict]:
        """Fire every reminder due now, once, and return the events published."""
        fired: list[dict] = []
        for reminder in self.store.due_reminders(self.now()):
            event = {"type": "reminder", "id": reminder["id"], "text": reminder["text"]}
            self.bus.publish(event)
            try:
                self.notify(reminder["text"])
            except Exception:
                log.exception("desktop notification failed")
            self.store.mark_reminder_done(reminder["id"])
            fired.append(event)
        return fired

    async def run(self) -> None:
        """Tick on the interval until the task is cancelled."""
        try:
            while True:
                try:
                    await asyncio.sleep(self.interval)
                    self.tick()
                    if self.scheduler is not None:
                        await self.scheduler.due(self.now())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("reminder loop tick failed")
        finally:
            if self.scheduler is not None:
                await self.scheduler.shutdown()
