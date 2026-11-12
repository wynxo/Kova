import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kova.events import EventBus
from kova.reminders import ReminderLoop, desktop_notify
from kova.tools import ToolError
from kova.tools.reminders import format_relative, format_when, parse_when


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


# ---------------------------------------------------------------------------
# Natural-language reminder times
# ---------------------------------------------------------------------------

# A Thursday at 10:00 in local (winter) time.
_NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=1)))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # ISO 8601, as before.
        ("2026-11-20 18:30", "2026-11-20T18:30:00+01:00"),
        ("2026-11-20T18:30:00", "2026-11-20T18:30:00+01:00"),
        ("2026-11-20T18:30:00+02:00", "2026-11-20T18:30:00+02:00"),
        # Relative.
        ("in 10 min", "2026-11-12T10:10:00+01:00"),
        ("in 10 minutes", "2026-11-12T10:10:00+01:00"),
        ("in 2h", "2026-11-12T12:00:00+01:00"),
        ("in 1 hour 30 minutes", "2026-11-12T11:30:00+01:00"),
        ("in 3 days", "2026-11-15T10:00:00+01:00"),
        ("10m", "2026-11-12T10:10:00+01:00"),
        ("2h", "2026-11-12T12:00:00+01:00"),
        ("in 90 seconds", "2026-11-12T10:01:30+01:00"),
        # Day words.
        ("today 18:30", "2026-11-12T18:30:00+01:00"),
        ("tonight", "2026-11-12T20:00:00+01:00"),
        ("tomorrow", "2026-11-13T09:00:00+01:00"),
        ("tomorrow 5pm", "2026-11-13T17:00:00+01:00"),
        ("tomorrow at 9", "2026-11-13T09:00:00+01:00"),
        ("tomorrow at 11pm", "2026-11-13T23:00:00+01:00"),
        # Weekdays.
        ("monday 9am", "2026-11-16T09:00:00+01:00"),
        ("friday 9am", "2026-11-13T09:00:00+01:00"),
        ("next friday 14:00", "2026-11-13T14:00:00+01:00"),
        ("next monday 9am", "2026-11-16T09:00:00+01:00"),
        ("on monday at 9", "2026-11-16T09:00:00+01:00"),
        # Clock only: today when still ahead.
        ("17:30", "2026-11-12T17:30:00+01:00"),
        ("5pm", "2026-11-12T17:00:00+01:00"),
        ("5:30pm", "2026-11-12T17:30:00+01:00"),
        ("5:30 p.m.", "2026-11-12T17:30:00+01:00"),
        ("12pm", "2026-11-12T12:00:00+01:00"),
        # German.
        ("in 10 Minuten", "2026-11-12T10:10:00+01:00"),
        ("in 3 Tagen", "2026-11-15T10:00:00+01:00"),
        ("heute 18:30", "2026-11-12T18:30:00+01:00"),
        ("heute abend", "2026-11-12T20:00:00+01:00"),
        ("morgen 9 Uhr", "2026-11-13T09:00:00+01:00"),
        ("morgen um 9 Uhr", "2026-11-13T09:00:00+01:00"),
        ("übermorgen", "2026-11-14T09:00:00+01:00"),
        ("uebermorgen", "2026-11-14T09:00:00+01:00"),
        ("montag 9 Uhr", "2026-11-16T09:00:00+01:00"),
        ("am montag um 9 Uhr", "2026-11-16T09:00:00+01:00"),
    ],
)
def test_parse_when_natural_language(text, expected):
    assert parse_when(text, _NOW).isoformat() == expected


@pytest.mark.parametrize(
    ("now", "text", "expected"),
    [
        # Midnight: a clock time that already passed rolls to tomorrow.
        (
            datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=1))),
            "12am",
            "2026-11-13T00:00:00+01:00",
        ),
        (
            datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=1))),
            "00:30",
            "2026-11-13T00:30:00+01:00",
        ),
        (
            datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=1))),
            "tomorrow 00:00",
            "2026-11-13T00:00:00+01:00",
        ),
        # End of month.
        (
            datetime(2026, 11, 30, 23, 30, tzinfo=timezone(timedelta(hours=1))),
            "in 1 hour",
            "2026-12-01T00:30:00+01:00",
        ),
        (
            datetime(2026, 11, 30, 23, 30, tzinfo=timezone(timedelta(hours=1))),
            "tomorrow",
            "2026-12-01T09:00:00+01:00",
        ),
        # 5pm after 5pm means tomorrow.
        (
            datetime(2026, 11, 12, 18, 0, tzinfo=timezone(timedelta(hours=1))),
            "5pm",
            "2026-11-13T17:00:00+01:00",
        ),
        # Weekday already past today -> next week; "next" on the same day -> +7.
        (
            datetime(2026, 11, 13, 10, 0, tzinfo=timezone(timedelta(hours=1))),
            "friday 14:00",
            "2026-11-13T14:00:00+01:00",
        ),
        (
            datetime(2026, 11, 13, 15, 0, tzinfo=timezone(timedelta(hours=1))),
            "friday 14:00",
            "2026-11-20T14:00:00+01:00",
        ),
        (
            datetime(2026, 11, 13, 10, 0, tzinfo=timezone(timedelta(hours=1))),
            "next friday 14:00",
            "2026-11-20T14:00:00+01:00",
        ),
        # DST starts in Europe/Berlin on 2026-03-29: 09:00 is +02:00, not +01:00.
        (
            datetime(2026, 3, 28, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            "tomorrow 9am",
            "2026-03-29T09:00:00+02:00",
        ),
        (
            datetime(2026, 3, 28, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            "in 1 day",
            "2026-03-29T12:00:00+02:00",
        ),
        # DST ends in Europe/Berlin on 2026-10-25: 09:00 is +01:00 again.
        (
            datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            "tomorrow 9am",
            "2026-10-25T09:00:00+01:00",
        ),
        (
            datetime(2026, 10, 24, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            "in 1 day",
            "2026-10-25T12:00:00+01:00",
        ),
    ],
)
def test_parse_when_edge_cases(now, text, expected):
    assert parse_when(text, now).isoformat() == expected


@pytest.mark.parametrize(
    "text",
    ["2026-11-12T09:00", "today 09:00", "in 0 min", "in 0 seconds"],
)
def test_parse_when_rejects_past_times(text):
    with pytest.raises(ToolError, match="future"):
        parse_when(text, _NOW)


@pytest.mark.parametrize("text", ["banana", "", "yesterday", "10", "25:00", "5:99pm"])
def test_parse_when_error_is_one_line_with_examples(text):
    with pytest.raises(ToolError) as exc:
        parse_when(text, _NOW)
    message = str(exc.value)
    assert "\n" not in message
    assert '"in 10 min"' in message
    assert '"tomorrow 5pm"' in message
    assert '"2026-11-20 18:30"' in message


def test_human_time_formatting():
    now = datetime(2026, 11, 12, 22, 0, tzinfo=timezone(timedelta(hours=1)))
    tomorrow = datetime(2026, 11, 13, 17, 0, tzinfo=timezone(timedelta(hours=1)))
    assert format_when(tomorrow, now) == "tomorrow at 17:00"
    assert format_relative(tomorrow, now, spaces=True) == "in 19 h"
    assert format_relative(tomorrow, now) == "in 19h"

    later = datetime(2026, 11, 12, 22, 2, tzinfo=timezone(timedelta(hours=1)))
    assert format_when(later, now) == "today at 22:02"
    assert format_relative(later, now) == "in 2m"

    next_week = datetime(2026, 11, 17, 18, 30, tzinfo=timezone(timedelta(hours=1)))
    assert format_when(next_week, now) == "Tuesday at 18:30"
    assert format_relative(next_week, now) == "in 4d"
