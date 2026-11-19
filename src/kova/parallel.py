"""Run independent read-only tool calls of one model step at the same time.

A model often asks for several lookups in one step: read three files, search
twice. When every call in a consecutive run only reads and needs no approval,
the agent runs that run concurrently instead of one after another (#192).

The rules that keep this safe:

* Only tools in ``PARALLEL_TOOLS`` qualify: they read and never change files,
  memory, reminders or anything else, so their order cannot matter.
* Only a *consecutive* run of such calls is grouped. A read that comes after a
  write in the same step still waits for the write.
* At most ``PARALLEL_LIMIT`` calls run at once; the rest wait their turn.
* Results go back to the model in the order it asked for them, whatever order
  they finished in.
* Stopping the turn cancels every call still running or waiting.

``run_group`` streams the group as task events with parent ids, so a client can
draw it as a live tree: ``task_start`` for the group and each call,
``task_progress`` when a call starts running and when the group's count moves,
then ``task_done`` or ``task_error``.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Callable

from kova.tools import Tool

#: Tools that only read. Anything that writes, runs a command, asks the user,
#: or runs code Kova cannot see into (MCP) is never grouped.
PARALLEL_TOOLS = frozenset({
    "read_file", "list_files", "search_files", "read_attachment",
    "web_search", "web_fetch",
    "memory_search", "memory_list", "recall", "search_chats",
    "reminder_list", "job_list", "skill_list", "skill_load", "system_info",
})

#: How many grouped calls may run at the same moment.
PARALLEL_LIMIT = 4


async def call_tool(tool: Tool, args: dict) -> str:
    """Run one tool: coroutines are awaited, sync tools run in a worker thread."""
    if inspect.iscoroutinefunction(tool.run):
        return await tool.run(args)
    result = await asyncio.to_thread(tool.run, args)
    if inspect.isawaitable(result):
        result = await result
    return result


def runs(ready: list[bool]) -> dict[int, int]:
    """Map the start of each consecutive run of at least two ready calls to its end.

    >>> runs([True, True, False, True, True, True])
    {0: 2, 3: 6}
    """
    found: dict[int, int] = {}
    start = None
    for index, flag in enumerate([*ready, False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if index - start >= 2:
                found[start] = index
            start = None
    return found


def _elapsed(started: float, clock: Callable[[], float]) -> int:
    return max(0, round((clock() - started) * 1000))


async def run_group(calls: list[tuple[int, dict, Tool, dict]], model: str,
                    results: dict[int, tuple[str, bool]], *,
                    limit: int = PARALLEL_LIMIT,
                    clock: Callable[[], float] = time.monotonic) -> AsyncIterator[dict]:
    """Run ``calls`` concurrently and yield their task events.

    ``calls`` holds ``(index, call, tool, args)`` for each call, in request
    order. Each finished call's ``(result, ok)`` lands in ``results`` under its
    index; a tool error becomes an ``Error: …`` result, exactly as it would
    when run alone. Cancelling the consumer cancels every unfinished call.
    """
    group = f"group-{calls[0][1]['id']}"
    total = len(calls)
    started = clock()
    yield {'type': 'task_start', 'id': group, 'parent': None, 'kind': 'parallel',
           'label': f'{total} tools in parallel', 'model': model, 'total': total}
    for _, call, _, args in calls:
        yield {'type': 'task_start', 'id': call['id'], 'parent': group, 'kind': 'tool',
               'name': call['name'], 'label': call['name'], 'args': args, 'model': model,
               'status': 'queued'}
        yield {'type': 'tool_call', 'id': call['id'], 'name': call['name'], 'args': args}

    gate = asyncio.Semaphore(max(1, limit))
    queue: asyncio.Queue = asyncio.Queue()

    async def one(index: int, call: dict, tool: Tool, args: dict) -> None:
        async with gate:
            began = clock()
            queue.put_nowait(('running', index, call, None, None, 0))
            try:
                result, ok = str(await call_tool(tool, args)), True
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise  # the turn was stopped
                # A tool that cancels its own inner work must not stall the group.
                result, ok = 'Error: the tool was cancelled', False
            except Exception as exc:  # a tool error is the model's to read, as in a single call
                result, ok = f'Error: {exc}', False
            queue.put_nowait(('finished', index, call, result, ok, _elapsed(began, clock)))

    tasks = [asyncio.create_task(one(*entry)) for entry in calls]
    finished = failed = 0
    try:
        while finished < total:
            state, index, call, result, ok, elapsed = await queue.get()
            if state == 'running':
                yield {'type': 'task_progress', 'id': call['id'], 'parent': group, 'status': 'running'}
                continue
            results[index] = (result, ok)
            finished += 1
            if ok:
                yield {'type': 'task_done', 'id': call['id'], 'parent': group, 'ok': True,
                       'summary': result[:200], 'elapsed_ms': elapsed}
            else:
                failed += 1
                yield {'type': 'task_error', 'id': call['id'], 'parent': group,
                       'message': result[:200], 'elapsed_ms': elapsed}
            yield {'type': 'task_progress', 'id': group, 'parent': None,
                   'done': finished, 'total': total, 'failed': failed}
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    yield {'type': 'task_done', 'id': group, 'parent': None, 'ok': failed == 0,
           'done': finished, 'failed': failed, 'elapsed_ms': _elapsed(started, clock)}
