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
* One call can also be stopped on its own (``controls``): it ends as a failed
  call whose result tells the model the user stopped it, and the others carry on.

``run_group`` streams the group as task events with parent ids, so a client can
draw it as a live tree: ``task_start`` for the group and each call,
``task_progress`` when a call starts running and when the group's count moves,
then ``task_done`` or ``task_error``. A call the user stopped sends
``task_error`` with ``stopped: true``.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable

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

#: What the model reads when the user stopped one call of a group.
STOPPED_RESULT = ("Error: the user stopped this call before it finished. "
                  "Do not run it again; carry on without its result.")


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
                    clock: Callable[[], float] = time.monotonic,
                    controls: dict[str, Callable[[], bool]] | None = None,
                    run_child: Callable[[dict, Callable[[str, str], None]], Awaitable[str]] | None = None,
                    ) -> AsyncIterator[dict]:
    """Run ``calls`` concurrently and yield their task events.

    ``calls`` holds ``(index, call, tool, args)`` for each call, in request
    order. Each finished call's ``(result, ok)`` lands in ``results`` under its
    index; a tool error becomes an ``Error: …`` result, exactly as it would
    when run alone. Cancelling the consumer cancels every unfinished call.

    ``controls`` (optional) is filled with one ``stop()`` per call id while the
    group runs, and emptied again at the end. ``controls[id]()`` cancels just
    that call (running or still waiting its turn) and returns whether it did.
    A delegated group uses ``run_child`` with a callback for its model's text
    and reasoning. Those deltas go to the client, not to the parent model.
    """
    group = f"group-{calls[0][1]['id']}"
    total = len(calls)
    started = clock()
    gate = asyncio.Semaphore(max(1, limit))
    queue: asyncio.Queue[tuple[dict, tuple[int, str, bool] | None]] = asyncio.Queue()
    by_id = {call['id']: index for index, call, _, _ in calls}
    workers: dict[int, asyncio.Task] = {}
    entered: set[int] = set()
    stopped: set[int] = set()

    async def one(index: int, call: dict, tool: Tool, args: dict) -> None:
        call_id = call['id']
        entered.add(index)
        began = clock()

        def emit(step: str, text: str) -> None:
            queue.put_nowait(({'type': 'task_output', 'id': call_id, 'parent': group,
                               'step': step, 'text': text}, None))

        try:
            if index in stopped:
                raise asyncio.CancelledError
            async with gate:
                began = clock()
                queue.put_nowait(({'type': 'task_progress', 'id': call_id, 'parent': group,
                                   'status': 'running'}, None))
                output = await run_child(args, emit) if run_child is not None else await call_tool(tool, args)
                result, ok = str(output), True
        except asyncio.CancelledError:
            if index in stopped:
                # Only this call was stopped: swallow the cancellation so the
                # group, and the turn, carry on.
                asyncio.current_task().uncancel()
                result, ok = STOPPED_RESULT, False
            elif asyncio.current_task().cancelling():
                raise  # the turn was stopped
            else:
                # A tool that cancels its own inner work must not stall the group.
                result, ok = 'Error: the tool was cancelled', False
        except Exception as exc:  # a tool error is the model's to read, as in a single call
            result, ok = f'Error: {exc}', False
        event = {'id': call_id, 'parent': group, 'elapsed_ms': _elapsed(began, clock)}
        if ok:
            event.update(type='task_done', ok=True, summary=result[:200])
        else:
            event.update(type='task_error', message=result[:200])
            if index in stopped:
                event['stopped'] = True
        queue.put_nowait((event, (index, result, ok)))

    def stop_one(index: int) -> bool:
        task = workers.get(index)
        if index in stopped or (task is not None and task.done()):
            return False
        stopped.add(index)
        # A coroutine cancelled before its first instruction cannot report a
        # result. An announced but not yet entered call checks the flag itself.
        if task is not None and index in entered:
            task.cancel()
        return True

    if controls is not None:
        for call_id, index in by_id.items():
            controls[call_id] = lambda index=index: stop_one(index)
    finished = failed = 0
    try:
        delegated = run_child is not None
        yield {'type': 'task_start', 'id': group, 'parent': None, 'kind': 'delegated' if delegated else 'parallel',
               'label': (f'Delegated {total} analysis tasks' if delegated and total > 1 else
                         'Delegated analysis' if delegated else f'{total} tools in parallel'),
               'model': model, 'total': total}
        for _, call, _, args in calls:
            yield {'type': 'task_start', 'id': call['id'], 'parent': group, 'kind': 'agent' if delegated else 'tool',
                   'name': call['name'], 'label': call['name'], 'args': args, 'model': model,
                   'status': 'queued'}
            yield {'type': 'tool_call', 'id': call['id'], 'name': call['name'], 'args': args}
        for entry in calls:
            workers[entry[0]] = asyncio.create_task(one(*entry))
        while finished < total:
            event, outcome = await queue.get()
            if outcome is None:
                yield event
                continue
            index, result, ok = outcome
            results[index] = (result, ok)
            finished += 1
            if not ok:
                failed += 1
            yield event
            yield {'type': 'task_progress', 'id': group, 'parent': None,
                   'done': finished, 'total': total, 'failed': failed}
    finally:
        if controls is not None:
            for call_id in by_id:
                controls.pop(call_id, None)
        pending = [task for task in workers.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    yield {'type': 'task_done', 'id': group, 'parent': None, 'ok': failed == 0,
           'done': finished, 'failed': failed, 'elapsed_ms': _elapsed(started, clock)}
