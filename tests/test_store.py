import time
from datetime import datetime, timedelta, timezone

import pytest

from kova.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "kova.db")


SAMPLE_TOOL_CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "memory_save",
            "arguments": '{"title": "Dog", "body": "Rex"}',
        },
    }
]


def test_chat_roundtrip(store):
    chat = store.create_chat("Test")
    assert chat["id"] and chat["created"]
    cid = chat["id"]

    store.add_message(cid, "user", "hello")
    store.add_message(cid, "assistant", None, tool_calls=SAMPLE_TOOL_CALLS)
    store.add_message(cid, "tool", None, tool_call_id="call_1")

    assert store.get_messages(cid) == [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": SAMPLE_TOOL_CALLS,
        },
        {"role": "tool", "content": None, "tool_call_id": "call_1"},
    ]


def test_list_newest_first(store):
    first = store.create_chat("first")
    time.sleep(0.001)
    second = store.create_chat("second")
    time.sleep(0.001)
    third = store.create_chat("third")
    time.sleep(0.001)
    fourth = store.create_chat("fourth")

    assert [c["id"] for c in store.list_chats()] == [
        fourth["id"],
        third["id"],
        second["id"],
        first["id"],
    ]


def test_delete_chat_removes_messages(store):
    chat = store.create_chat()
    cid = chat["id"]
    store.add_message(cid, "user", "hi")
    assert store.get_chat(cid)["messages"] == [{"role": "user", "content": "hi"}]

    assert store.delete_chat(cid) is True
    assert store.get_chat(cid) is None
    assert store.get_messages(cid) == []


def test_reminders_due_and_done(store):
    now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    future = now + timedelta(hours=1)

    past_id = store.add_reminder(past, "past reminder")
    store.add_reminder(future, "future reminder")

    assert [r["text"] for r in store.due_reminders(now)] == ["past reminder"]

    store.mark_reminder_done(past_id)
    assert store.due_reminders(now) == []

    listed = store.list_reminders()
    assert [r["text"] for r in listed] == ["future reminder"]
    assert listed[0]["done"] is False


def test_add_reminder_requires_tz(store):
    with pytest.raises(ValueError):
        store.add_reminder(datetime(2026, 11, 12, 10, 0, 0), "no tz")


def test_reminders_sorted_by_real_time_across_timezones(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path / "kova.db")
    berlin = timezone(timedelta(hours=1))
    # 10:00+01:00 is 09:00 UTC, so it comes before 09:30 UTC (text order would get this wrong)
    a = store.add_reminder(datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc), "later")
    b = store.add_reminder(datetime(2026, 11, 12, 10, 0, tzinfo=berlin), "earlier")
    assert [r["id"] for r in store.list_reminders()] == [b, a]
