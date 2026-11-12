import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kova.events import EventBus
from kova.reminders import ReminderLoop, desktop_notify


def _now() -> datetime:
    return datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path):
    from kova.store import Store

    return Store(tmp_path / "kova.db")


async def test_tick_fires_due_once(tmp_path):
    store = _store(tmp_path)
    store.add_reminder(_now() - timedelta(minutes=1), "buy milk")
    store.add_reminder(_now() + timedelta(hours=1), "later")
    bus = EventBus()
    q = bus.subscribe()
    clock = _now()
    loop = ReminderLoop(store, bus, lambda: clock)

    fired = loop.tick()
    assert [e["text"] for e in fired] == ["buy milk"]
    assert q.get_nowait()["text"] == "buy milk"

    # Already fired: a second tick is a no-op.
    assert loop.tick() == []

    # The future reminder only fires once the clock moves past it.
    clock = _now() + timedelta(hours=2)
    fired = loop.tick()
    assert [e["text"] for e in fired] == ["later"]
    assert loop.tick() == []


async def test_bus_fanout():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.publish({"type": "reminder", "id": 1, "text": "x"})
    assert q1.get_nowait()["type"] == "reminder"
    assert q2.get_nowait()["type"] == "reminder"


async def test_notify_failure_does_not_stop_tick(tmp_path):
    store = _store(tmp_path)
    store.add_reminder(_now() - timedelta(minutes=1), "boom")
    bus = EventBus()
    q = bus.subscribe()

    def boom(text):
        raise RuntimeError("notify down")

    loop = ReminderLoop(store, bus, _now, boom)

    fired = loop.tick()
    assert [e["text"] for e in fired] == ["boom"]
    assert q.get_nowait()["text"] == "boom"
    # Marked done even though notify raised.
    assert store.list_reminders(include_done=True)[0]["done"] is True


async def test_desktop_notify_skips_when_missing(monkeypatch):
    monkeypatch.setattr("kova.reminders.shutil.which", lambda name: None)
    events = []
    monkeypatch.setattr("kova.reminders.subprocess.Popen", lambda *a, **k: events.append(a))
    desktop_notify("hi")  # must not raise
    assert events == []


async def test_desktop_notify_calls_notify_send(monkeypatch):
    monkeypatch.setattr("kova.reminders.shutil.which", lambda name: "/usr/bin/notify-send")
    events = []
    monkeypatch.setattr("kova.reminders.subprocess.Popen", lambda *a, **k: events.append(a))
    desktop_notify("hello world")
    assert events[0][0] == ["notify-send", "Kova", "hello world"]


async def test_run_logs_tick_error_and_continues(tmp_path, caplog):
    store = _store(tmp_path)
    store.add_reminder(_now() - timedelta(minutes=1), "boom")
    bus = EventBus()
    loop = ReminderLoop(store, bus, _now, interval=0.02)
    loop.tick = lambda: (_ for _ in ()).throw(RuntimeError("tick down"))
    task = asyncio.ensure_future(loop.run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "reminder loop tick failed" in caplog.text
