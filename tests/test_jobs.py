"""Scheduled jobs: the repeat parser, storage, and the loop that fires them."""

import json
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone

import pytest

from kova.config import load_config
from kova.events import EventBus
from kova.memory import Memory
from kova.permissions import needs_approval
from kova.scheduler import MIN_INTERVAL_SECONDS, Scheduler, parse_schedule
from kova.store import Store
from kova.tools import ToolContext, ToolError
from kova.tools import jobs as job_tools

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


def python_command(code: str) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + value.replace("'", "''") + "'"
                               for value in (sys.executable, "-c", code))
    return shlex.join([sys.executable, "-c", code])


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "kova.db", default_cwd=str(tmp_path))


def _ctx(tmp_path, store) -> ToolContext:
    return ToolContext(load_config(tmp_path), store, Memory(tmp_path / "memory"), lambda: NOW)


def _tools(ctx: ToolContext) -> dict:
    return {tool.name: tool for tool in job_tools.tools(ctx)}


def test_parse_schedule_intervals_and_daily_times():
    every_half_hour = parse_schedule("every 30m", NOW)
    assert every_half_hour.seconds == 30 * 60
    assert every_half_hour.next_after(NOW) == NOW + timedelta(minutes=30)
    assert every_half_hour.describe() == "every 30m"

    assert parse_schedule("6h", NOW).seconds == 6 * 3600
    assert parse_schedule("hourly", NOW).seconds == 3600
    assert parse_schedule("every day", NOW).hour == 9

    daily = parse_schedule("daily 09:00", NOW)
    assert (daily.hour, daily.minute) == (9, 0)
    assert daily.next_after(NOW) == datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
    assert daily.describe() == "daily at 09:00"

    natural = parse_schedule("every day at 9am", NOW)
    assert (natural.hour, natural.minute) == (9, 0)


@pytest.mark.parametrize("spec", ["", "sometimes", "every 30s", "every 0m", "every fortnight"])
def test_parse_schedule_refuses_what_it_cannot_repeat(spec):
    with pytest.raises(ToolError):
        parse_schedule(spec, NOW)


def test_a_job_repeats_no_faster_than_the_minimum():
    assert parse_schedule(f"every {MIN_INTERVAL_SECONDS}s", NOW).seconds == MIN_INTERVAL_SECONDS


def test_jobs_and_reminders_share_a_table_without_mixing(store):
    reminder_id = store.add_reminder(NOW + timedelta(hours=1), "drink water")
    job_id = store.add_job("Back up", "every 1h", NOW, command="true")

    assert [r["id"] for r in store.list_reminders()] == [reminder_id]
    assert [j["id"] for j in store.list_jobs()] == [job_id]
    assert [j["id"] for j in store.due_jobs(NOW)] == [job_id]

    # A reminder command can never touch a scheduled job, and vice versa.
    assert store.delete_reminder(job_id) is False
    store.mark_reminder_done(job_id)
    assert store.list_jobs()[0]["done"] is False
    store.clear_reminders()
    assert store.list_jobs()[0]["id"] == job_id


async def test_a_script_job_runs_with_no_llm_call(store, tmp_path):
    bus = EventBus()
    events = bus.subscribe()
    job_id = store.add_job("Say hello", "every 1h", NOW, command=python_command("print('hello')"))
    scheduler = Scheduler(store, bus, lambda: NOW, cwd=tmp_path)

    started = await scheduler.due(NOW)
    assert [job["id"] for job in started] == [job_id]
    await scheduler.drain()

    event = events.get_nowait()
    assert event["type"] == "job" and event["id"] == job_id
    assert event["kind"] == "script" and event["ok"] is True
    assert "hello" in event["output"]
    job = store.list_jobs()[0]
    assert job["runs"] == 1
    assert datetime.fromisoformat(job["next_run"]) == NOW + timedelta(hours=1)


async def test_a_prompt_job_runs_one_turn_of_its_own(store, tmp_path):
    bus = EventBus()
    events = bus.subscribe()
    calls = []

    async def run_prompt(job: dict) -> str:
        calls.append(job["id"])
        return "checked the backups"

    job_id = store.add_job("Check backups", "every 2h", NOW, prompt="Check the backups.")
    scheduler = Scheduler(store, bus, lambda: NOW, cwd=tmp_path, run_prompt=run_prompt,
                          is_idle=lambda: True)

    await scheduler.due(NOW)
    await scheduler.drain()

    assert calls == [job_id]
    event = events.get_nowait()
    assert event["kind"] == "prompt" and event["ok"] is True
    assert event["output"] == "checked the backups"


async def test_a_heartbeat_only_starts_when_nothing_else_is_running(store, tmp_path):
    bus = EventBus()
    calls = []

    async def run_prompt(job: dict) -> str:
        calls.append(job["text"])
        return "quiet check"

    job_id = store.add_job("Heartbeat", "every 1h", NOW, prompt="Check quietly.", idle_only=True)
    scheduler = Scheduler(store, bus, lambda: NOW, cwd=tmp_path, run_prompt=run_prompt,
                          is_idle=lambda: False)

    assert await scheduler.due(NOW) == []
    assert calls == [] and store.list_jobs()[0]["runs"] == 0

    scheduler.is_idle = lambda: True
    assert [job["id"] for job in await scheduler.due(NOW)] == [job_id]
    await scheduler.drain()
    assert calls == ["Heartbeat"]


async def test_a_due_job_wins_over_a_heartbeat_in_the_same_tick(store, tmp_path):
    bus = EventBus()
    calls = []

    async def run_prompt(job: dict) -> str:
        calls.append(job["text"])
        return "done"

    heartbeat = store.add_job("Heartbeat", "every 1h", NOW, prompt="Check quietly.",
                              idle_only=True)
    ordinary = store.add_job("Report", "every 1h", NOW, prompt="Write the report.")
    scheduler = Scheduler(store, bus, lambda: NOW, cwd=tmp_path, run_prompt=run_prompt,
                          is_idle=lambda: True)

    started = await scheduler.due(NOW)
    assert [job["id"] for job in started] == [ordinary]
    await scheduler.drain()
    assert calls == ["Report"]
    assert next(job for job in store.list_jobs() if job["id"] == heartbeat)["runs"] == 0

    # The heartbeat is still due; it starts once nothing else is running.
    assert [job["id"] for job in await scheduler.due(NOW)] == [heartbeat]
    await scheduler.drain()
    assert calls == ["Report", "Heartbeat"]


async def test_a_broken_job_does_not_spin_or_stop_other_jobs(store, tmp_path):
    bus = EventBus()
    events = bus.subscribe()
    good = store.add_job("Good", "every 1h", NOW, command=python_command("print('ok')"))
    broken = store.add_job("Broken", "not a schedule", NOW, command="true")
    scheduler = Scheduler(store, bus, lambda: NOW, cwd=tmp_path)

    started = await scheduler.due(NOW)
    assert [job["id"] for job in started] == [good]
    await scheduler.drain()
    assert events.get_nowait()["id"] == good
    # A job whose schedule cannot be parsed is finished instead of retried forever.
    assert next(job for job in store.list_jobs(include_done=True) if job["id"] == broken)["done"] is True


def test_job_tools_add_list_and_remove(store, tmp_path):
    tools = _tools(_ctx(tmp_path, store))

    added = tools["job_add"].run({"text": "Back up", "schedule": "every 6h",
                                  "command": "true"})
    assert "Job #1 scheduled every 6h" in added
    listed = json.loads(tools["job_list"].run({}))
    assert listed["total"] == 1
    assert listed["jobs"][0]["kind"] == "script" and listed["jobs"][0]["runs"] == 0
    assert "in" in listed["jobs"][0]
    assert "Removed job #1." in tools["job_remove"].run({"id": 1})
    assert store.list_jobs() == []


def test_job_tools_explain_a_bad_job(store, tmp_path):
    tools = _tools(_ctx(tmp_path, store))
    with pytest.raises(ToolError, match="exactly one"):
        tools["job_add"].run({"text": "Back up", "schedule": "every 1h"})
    with pytest.raises(ToolError, match="description"):
        tools["job_add"].run({"text": "  ", "schedule": "every 1h", "command": "true"})
    with pytest.raises(ToolError, match="No job #9"):
        tools["job_remove"].run({"id": 9})


def test_a_job_shell_command_needs_approval_when_it_is_scheduled(tmp_path):
    files = tmp_path / "files"
    args = {"text": "Back up", "schedule": "every 1h", "command": "echo hi"}
    assert needs_approval("job_add", args, "manual", tmp_path, files) is True
    assert needs_approval("job_add", args, "auto", tmp_path, files) is True
    assert needs_approval("job_add", {**args, "command": "rm -rf build"}, "auto",
                          tmp_path, files) is True
    # A prompt-only job carries no command, so it needs no shell approval.
    prompt_only = {"text": "Report", "schedule": "every 1h", "prompt": "Write it."}
    assert needs_approval("job_add", prompt_only, "manual", tmp_path, files) is False


@pytest.mark.parametrize('permission', ['manual', 'auto'])
@pytest.mark.parametrize('sandboxed', [True, False, None])
def test_scheduled_commands_require_unconfined_approval(tmp_path, permission, sandboxed):
    args = {'command': 'echo hi', 'schedule': 'every 1h'}
    assert needs_approval('job_add', args, permission, tmp_path, tmp_path,
                          sandboxed=sandboxed) is True
    assert needs_approval('job_add', args, 'full', tmp_path, tmp_path,
                          sandboxed=sandboxed) is False
    assert needs_approval('job_add', {'prompt': 'Report'}, permission, tmp_path, tmp_path,
                          sandboxed=sandboxed) is False
