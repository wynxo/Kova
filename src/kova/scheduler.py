"""Recurring jobs: repeat schedules, script runs, and the idle-only heartbeat.

A job is a row in the same table as a reminder (``kind = 'job'``) and is driven
by the same loop, :class:`kova.reminders.ReminderLoop`. Two kinds exist:

- a **script job** runs a shell command through the same bounded, cancellable
  runner the ``run_command`` tool uses, so it costs no model call;
- a **prompt job** runs exactly one agent turn, in its own chat, so a repeating
  brief keeps one thread of history.

A job flagged ``idle_only`` is a proactive heartbeat: it only starts when
nothing else is running, so a background turn never competes with the user.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from kova.events import EventBus
from kova.store import Store
from kova.tools import ToolError
from kova.tools.commands import run_command
from kova.tools.reminders import parse_when

log = logging.getLogger(__name__)

#: How long one script job may run before it is killed, in seconds.
JOB_TIMEOUT = 300
#: Characters of a job's output kept in its published event.
EVENT_OUTPUT_CHARS = 2000
#: A job that repeats faster than this would just spin the loop.
MIN_INTERVAL_SECONDS = 60
#: Wall-clock time a bare ``daily`` schedule runs at.
DEFAULT_DAILY = (9, 0)

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
_ALIASES = {"hourly": "every 1 hour", "daily": "every day", "weekly": "every 1 week"}
_INTERVAL_RE = re.compile(r"^(?:every\s+)?(\d+)\s*([a-z]+)$")
_DAILY_RE = re.compile(r"^(?:daily|every\s+day)(?:\s+at)?\s*(.*)$")

_BAD_SCHEDULE = ("Give a repeat schedule like 'every 30m', 'every 2h', 'every 6 hours', 'hourly', "
                 "'daily', 'daily 09:00' or 'every day at 9am'. Jobs repeat; for a single "
                 "notification use a reminder.")


@dataclass(frozen=True)
class Schedule:
    """A repeat spec: a fixed interval, or a daily wall-clock time."""

    spec: str
    seconds: int | None = None
    hour: int | None = None
    minute: int | None = None

    def next_after(self, now: datetime) -> datetime:
        """The first run strictly after ``now``."""
        if self.seconds is not None:
            return now + timedelta(seconds=self.seconds)
        zone = now.tzinfo
        clock = time(self.hour or 0, self.minute or 0)
        when = datetime.combine(now.date(), clock, zone)
        if when <= now:
            when = datetime.combine(now.date() + timedelta(days=1), clock, zone)
        return when

    def describe(self) -> str:
        """One short human phrase, e.g. ``every 30m`` or ``daily at 09:00``."""
        if self.seconds is None:
            return f"daily at {self.hour:02d}:{self.minute:02d}"
        for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
            if self.seconds % size == 0:
                return f"every {self.seconds // size}{unit}"
        return f"every {self.seconds}s"


def _interval(spec: str) -> int | None:
    """Seconds for ``30m``, ``every 2 hours`` and friends, or None."""
    match = _INTERVAL_RE.match(spec)
    if match is None:
        return None
    size = _UNIT_SECONDS.get(match.group(2))
    if size is None:
        raise ToolError(_BAD_SCHEDULE)
    seconds = int(match.group(1)) * size
    if seconds < MIN_INTERVAL_SECONDS:
        raise ToolError(f"Jobs repeat no faster than every {MIN_INTERVAL_SECONDS // 60}m. {_BAD_SCHEDULE}")
    return seconds


def _daily(spec: str, now: datetime) -> tuple[int, int] | None:
    """The hour and minute of ``daily``/``every day [at 9am]``, or None."""
    match = _DAILY_RE.match(spec)
    if match is None:
        return None
    clock = match.group(1).strip()
    if not clock:
        return DEFAULT_DAILY
    if clock.isdigit() and 0 <= int(clock) <= 23:
        return int(clock), 0  # "every day at 9"
    try:
        when = parse_when(clock, now)
    except ToolError:
        raise ToolError(_BAD_SCHEDULE) from None
    return when.hour, when.minute


def parse_schedule(text: str, now: datetime) -> Schedule:
    """Parse a repeat schedule, so a job's next run can always be computed.

    Accepts fixed intervals (``30m``, ``every 2h``, ``every 12 hours``,
    ``hourly``, ``weekly``) and daily times (``daily``, ``daily 09:00``,
    ``every day at 9am``).
    """
    spec = " ".join((text or "").strip().lower().split())
    if not spec:
        raise ToolError(_BAD_SCHEDULE)
    spec = _ALIASES.get(spec, spec)
    seconds = _interval(spec)
    if seconds is not None:
        return Schedule(spec, seconds=seconds)
    daily = _daily(spec, now)
    if daily is not None:
        return Schedule(spec, hour=daily[0], minute=daily[1])
    raise ToolError(_BAD_SCHEDULE)


class Scheduler:
    """Start due jobs from the reminder loop; never block it.

    ``run_prompt`` runs one job's turn and returns its reply; when it is None
    prompt jobs are left untouched (nothing can run them yet). ``is_idle`` says
    whether a heartbeat may start.
    """

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        now: Callable[[], datetime],
        *,
        cwd: Path | None = None,
        run_prompt: Callable[[dict], Awaitable[str]] | None = None,
        is_idle: Callable[[], bool] | None = None,
        timeout: float = JOB_TIMEOUT,
    ) -> None:
        self.store = store
        self.bus = bus
        self.now = now
        self.cwd = Path(cwd or store.default_cwd or Path.cwd())
        self.run_prompt = run_prompt
        self.is_idle = is_idle
        self.timeout = timeout
        self._running: dict[int, asyncio.Task] = {}

    @property
    def busy(self) -> bool:
        """Whether any job is running right now."""
        return bool(self._running)

    async def due(self, now: datetime) -> list[dict]:
        """Start every job that is due now, and return those jobs."""
        started: list[dict] = []
        # Ordinary jobs first, so a heartbeat that is due at the same moment can
        # see them and stay quiet instead of running alongside them.
        jobs = sorted(self.store.due_jobs(now), key=lambda job: (job["idle_only"], job["next_run"]))
        for job in jobs:
            if job["id"] in self._running:
                continue
            # Re-ask for every job: one started earlier in this same tick already
            # means the machine is no longer idle.
            idle = not self._running and (self.is_idle() if self.is_idle is not None else True)
            if job["idle_only"] and not idle:
                continue  # a heartbeat waits until nothing else is running
            if job["prompt"] and self.run_prompt is None:
                continue  # no way to run a turn here; try again on the next tick
            try:
                schedule = parse_schedule(job["schedule"], now)
            except ToolError as exc:
                log.warning("job #%s has an unusable schedule %r: %s", job["id"], job["schedule"], exc)
                self.store.mark_job_run(job["id"], None)  # a broken job must not spin
                continue
            # Reschedule before running, so a slow job can never fire twice.
            self.store.mark_job_run(job["id"], schedule.next_after(now))
            task = asyncio.create_task(self._run(job))
            self._running[job["id"]] = task
            task.add_done_callback(lambda _done, jid=job["id"]: self._running.pop(jid, None))
            started.append(job)
        return started

    async def _run(self, job: dict) -> None:
        """Run one job and publish its outcome; a failure never breaks the loop."""
        script = bool(job["command"])
        try:
            if script:
                result = await run_command(job["command"], cwd=self.cwd, timeout=self.timeout)
                ok = result["ok"]
                output = result["output"]
                if result.get("error"):
                    output = f"{output}\nError: {result['error']}".strip()
            else:
                ok = True
                output = await self.run_prompt(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken job must never stop the loop
            ok, output = False, f"{type(exc).__name__}: {exc}"
            log.exception("job #%s failed", job["id"])
        if not ok:
            log.warning("job #%s (%s) failed: %s", job["id"], job["text"], output[:500])
        self.bus.publish({
            "type": "job", "id": job["id"], "text": job["text"],
            "kind": "script" if script else "prompt", "idle_only": job["idle_only"],
            "ok": ok, "output": output[:EVENT_OUTPUT_CHARS],
        })

    async def drain(self) -> None:
        """Wait for every job that is running."""
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel running jobs and wait for them to unwind."""
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()
