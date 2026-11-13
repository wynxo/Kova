"""Jobs reuse reminders, fire by clock, survive restart, and gate idle turns."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kova import cli
from kova.client import KovaClient
from kova.config import ensure_token, load_config
from kova.events import EventBus
from kova.permissions import needs_approval
from kova.reminders import ReminderLoop
from kova.scheduler import Scheduler
from kova.server import create_app
from kova.store import Store
from tests.fake_model import FakeModel, text, tool

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


def add(store, tmp_path, *, kind='script', every=10, when=NOW, text='printf hello'):
    return store.add_job(when, text, kind=kind, cwd=str(tmp_path), every_seconds=every)


async def settle(scheduler):
    await asyncio.gather(*(task for _, task in scheduler.running.values()))


async def test_due_script_is_llm_free_recurs_and_survives_restart(tmp_path):
    store = Store(tmp_path / 'kova.db')
    jid = add(store, tmp_path, text='printf x >> result.txt')
    rid = store.add_reminder(NOW, 'notify')
    now = NOW - timedelta(seconds=1)
    agent = AsyncMock(side_effect=AssertionError('script must not call an LLM'))
    bus = EventBus()
    scheduler = Scheduler(store, bus, lambda: now, run_agent=agent, is_idle=lambda: False)
    assert scheduler.tick() == []
    now = NOW
    assert scheduler.tick() == [jid]
    assert scheduler.tick() == []
    await settle(scheduler)
    assert (tmp_path / 'result.txt').read_text() == 'x'
    assert store.list_jobs()[0]['last_result']['output'] == ''
    assert ReminderLoop(store, bus, lambda: now, notify=lambda _: None).tick()[0]['id'] == rid
    await scheduler.close()
    # Multiple missed slots become one run, with next due on the original cadence.
    store = Store(tmp_path / 'kova.db')
    now += timedelta(seconds=35)
    scheduler = Scheduler(store, bus, lambda: now, run_agent=agent, is_idle=lambda: False)
    assert scheduler.tick() == [jid]
    await settle(scheduler)
    assert (tmp_path / 'result.txt').read_text() == 'xx'
    assert datetime.fromisoformat(store.list_jobs()[0]['when']) == NOW + timedelta(seconds=40)
    assert scheduler.tick() == []
    agent.assert_not_called()
    await scheduler.close()


def test_migration_and_atomic_claim_share_reminder_table(tmp_path):
    path = tmp_path / 'kova.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE reminders (id INTEGER PRIMARY KEY, reminder_at TEXT NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)')
        conn.execute('INSERT INTO reminders VALUES (1, ?, ?, 0)', (NOW.isoformat(), 'legacy'))
    store, other = Store(path), Store(path)
    jid = add(store, tmp_path, every=0)
    snapshot = store.list_jobs()[0]
    assert store.claim_job(snapshot, NOW)
    assert not other.claim_job(snapshot, NOW)
    assert not store.list_jobs()
    assert store.list_jobs(include_done=True)[0]['id'] == jid
    assert [r['text'] for r in other.due_reminders(NOW)] == ['legacy']
    assert not store.delete_reminder(jid)
    store.clear_reminders()
    assert not store.list_reminders()
    assert store.list_jobs(include_done=True)


async def test_heartbeat_requires_idle_period_and_yields_to_user(tmp_path):
    store = Store(tmp_path / 'kova.db')
    now = NOW
    busy = False
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def run(job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scheduler = Scheduler(store, EventBus(), lambda: now, run_agent=run, is_idle=lambda: not busy)
    jid = add(store, tmp_path, kind='heartbeat')
    assert scheduler.tick() == []  # no idle interval elapsed since startup
    now += timedelta(seconds=10)
    busy = True
    assert scheduler.tick() == []
    busy = False
    now += timedelta(seconds=9)
    assert scheduler.tick() == []
    now += timedelta(seconds=1)
    assert scheduler.tick() == [jid]
    await asyncio.wait_for(started.wait(), 1)
    scheduler.user_activity()
    await asyncio.wait_for(cancelled.wait(), 1)
    assert store.list_jobs()[0]['last_result']['error'] == 'Job interrupted'
    assert scheduler.tick() == []
    await scheduler.close()


async def test_one_shot_job_and_failure_do_not_block_other_jobs(tmp_path):
    store = Store(tmp_path / 'kova.db')
    bad = add(store, tmp_path, every=0, text='exit 7')
    good = add(store, tmp_path, every=0, kind='agent', text='Summarize')
    agent = AsyncMock(return_value={'ok': True, 'chat_id': 'background'})
    scheduler = Scheduler(store, EventBus(), lambda: NOW, run_agent=agent, is_idle=lambda: True)
    assert scheduler.tick() == [bad, good]
    await settle(scheduler)
    rows = {row['id']: row for row in store.list_jobs(include_done=True)}
    assert rows[bad]['last_result']['exit_code'] == 7
    assert not rows[bad]['last_result']['ok']
    assert rows[good]['last_result']['chat_id'] == 'background'
    assert scheduler.tick() == []
    agent.assert_awaited_once()
    await scheduler.close()


@pytest.mark.parametrize('permission', ['manual', 'auto'])
def test_scheduling_requires_authorization_even_in_auto(tmp_path, permission):
    assert needs_approval('job_add', {'kind': 'script', 'text': 'echo hi'}, permission, tmp_path, tmp_path)
    assert not needs_approval('job_add', {'kind': 'script'}, 'full', tmp_path, tmp_path)


def test_cli_schedules_inspects_and_removes_job_through_server(tmp_path, monkeypatch):
    monkeypatch.setenv('KOVA_HOME', str(tmp_path))
    cfg = load_config(tmp_path)
    fake = FakeModel([])
    app = create_app(cfg, transport=fake.transport, now=lambda: NOW)
    with TestClient(app) as http:
        client = KovaClient('http://testserver', ensure_token(cfg.home), http=http)
        monkeypatch.setattr(cli, 'get_client', lambda: client)
        runner = CliRunner()
        result = runner.invoke(cli.app, ['jobs', 'add', 'in 5 min', 'printf hello', '--kind', 'script', '--every', '60', '--cwd', str(tmp_path)])
        assert result.exit_code == 0, result.output
        jid = app.state.store.list_jobs()[0]['id']
        result = runner.invoke(cli.app, ['jobs', 'show', str(jid)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)['text'] == 'printf hello'
        assert runner.invoke(cli.app, ['jobs', 'rm', str(jid)]).exit_code == 0
        assert not app.state.store.list_jobs()
        assert not fake.requests


def test_server_agent_job_denies_new_approvals_without_hanging(tmp_path):
    cfg = load_config(tmp_path)
    cfg.files_path = tmp_path
    fake = FakeModel([tool('run_command', {'command': 'echo should-not-run'}), text('Needs approval.')])
    app = create_app(cfg, transport=fake.transport, now=lambda: NOW)
    with TestClient(app) as http:
        jid = add(app.state.store, tmp_path, every=0, kind='agent', text='Run a command')
        async def execute():
            app.state.scheduler.tick()
            await asyncio.wait_for(settle(app.state.scheduler), 5)
        http.portal.call(execute)
        result = app.state.store.list_jobs(include_done=True)[0]['last_result']
        assert not result['ok']
        assert 'approval' in result['error']
        history = app.state.store.get_chat(result['chat_id'])['messages']
        assert any(m['role'] == 'tool' and 'denied' in m['content'] for m in history)
        assert not app.state.agent.approvals.pending
