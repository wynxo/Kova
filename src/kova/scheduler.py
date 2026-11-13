"""Persistent recurring jobs and opt-in, idle-only agent heartbeats."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from kova.events import EventBus
from kova.store import Store
from kova.tools import Tool, ToolContext, ToolError
from kova.tools.commands import run_command
from kova.tools.core import schema, validate
from kova.tools.reminders import parse_when
from kova.tools.sessions import current_chat

log = logging.getLogger(__name__)


class Scheduler:
    """Dispatch due slots without blocking reminders or overlapping a job.

    The server owns this worker. Heartbeats are ordinary recurring queue rows,
    disabled by default (no rows), with an additional idle gate. Running scripts
    reuse the bounded, cancellable command runner and never construct an LLM.
    """

    def __init__(self, store: Store, bus: EventBus, now: Callable[[], datetime], *,
                 run_agent: Callable[[dict], Awaitable[dict]],
                 is_idle: Callable[[], bool]):
        self.store, self.bus, self.now = store, bus, now
        self.run_agent, self.is_idle = run_agent, is_idle
        self.last_activity = now()
        self.running: dict[int, tuple[dict, asyncio.Task]] = {}

    def user_activity(self) -> None:
        """Foreground work wins: reset idle time and stop any proactive turn."""
        self.last_activity = self.now()
        for job, task in self.running.values():
            if job['kind'] == 'heartbeat' and not task.done():
                task.cancel()

    def tick(self) -> list[int]:
        """Start due jobs; each can run at most once concurrently in this server."""
        now = self.now()
        idle = self.is_idle()
        if not idle:
            self.user_activity()
        jobs = self.store.list_jobs(include_done=True)
        live = {job['id'] for job in jobs}
        for jid, (job, task) in list(self.running.items()):
            if task.done():
                self.running.pop(jid)
            elif jid not in live:
                task.cancel()
        started = []
        for job in jobs:
            if len(self.running) >= 4:
                break
            if job['done'] or job['id'] in self.running or datetime.fromisoformat(job['when']) > now:
                continue
            if job['kind'] == 'heartbeat':
                if not idle or self.running or (now.timestamp() - self.last_activity.timestamp()) < job['every_seconds']:
                    continue
            if self.store.claim_job(job, now):
                task = asyncio.create_task(self._execute(job))
                self.running[job['id']] = (job, task)
                started.append(job['id'])
        return started

    async def _execute(self, job: dict) -> None:
        try:
            if job['kind'] == 'script':
                result = await run_command(job['text'], Path(job['cwd']), job['timeout'])
            else:
                # Recheck after task dispatch: a user turn may have arrived.
                if job['kind'] == 'heartbeat' and not self.is_idle():
                    result = {'ok': False, 'error': 'Heartbeat skipped: user is active'}
                else:
                    async with asyncio.timeout(job['timeout']):
                        result = await self.run_agent(job)
        except asyncio.CancelledError:
            self.store.finish_job(job['id'], {'ok': False, 'error': 'Job interrupted'})
            raise
        except Exception as exc:
            log.exception('Scheduled job %s failed', job['id'])
            result = {'ok': False, 'error': str(exc) or type(exc).__name__}
        self.store.finish_job(job['id'], result)
        self.bus.publish({'type': 'job', 'id': job['id'], 'kind': job['kind'], **result})

    async def close(self) -> None:
        tasks = [task for _, task in self.running.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.running.clear()


def tools(ctx: ToolContext) -> list[Tool]:
    def add(args: dict) -> str:
        chat = ctx.store.get_chat(current_chat()) if current_chat() else None
        cwd = (chat or {}).get('cwd') or str(ctx.config.files_path)
        jid = ctx.store.add_job(parse_when(args['when'], ctx.now()), args['text'],
                                kind=args['kind'], cwd=cwd,
                                every_seconds=args.get('every_seconds', 0), timeout=args.get('timeout', 60))
        return json.dumps({'id': jid, 'status': 'scheduled'})

    def remove(args: dict) -> str:
        if not ctx.store.delete_job(args['id']):
            raise ToolError('No scheduled job with that id')
        return 'Scheduled job removed; an active run stops on the next scheduler tick.'

    definitions = [
        ('job_add', 'Schedule a job while Kova is running. kind=script runs text as a shell command with NO LLM; kind=agent runs text as a prompt in a new manual-permission chat. kind=heartbeat is an opt-in recurring agent prompt that runs only after the user is idle for every_seconds; foreground activity cancels it. Working folder is this chat. every_seconds=0 means once; positive intervals repeat at fixed elapsed-time intervals, skipping missed slots. Creation authorizes future runs; background turns cannot approve new actions.',
         schema({'when': {'type': 'string', 'minLength': 1},
                 'text': {'type': 'string', 'minLength': 1, 'maxLength': 12_000},
                 'kind': {'type': 'string', 'enum': ['script', 'agent', 'heartbeat']},
                 'every_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 31_536_000},
                 'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, ['when', 'text', 'kind']), add),
        ('job_list', 'List scheduled jobs, next times and last results, including finished one-shot jobs.',
         schema({}), lambda args: json.dumps(ctx.store.list_jobs(include_done=True))),
        ('job_remove', 'Remove a scheduled job or disable a heartbeat by id.',
         schema({'id': {'type': 'integer', 'minimum': 1}}, ['id']), remove),
    ]
    result = []
    for name, description, parameters, action in definitions:
        def run(args, parameters=parameters, action=action):
            validate(parameters, args)
            return action(args)
        result.append(Tool(name, description, parameters, run))
    return result
